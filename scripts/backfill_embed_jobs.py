"""One-time backfill: embed every classified job in public.jobs that doesn't
already have an embedding.

Uses openai/text-embedding-3-small via OpenRouter, 64 jobs per API call.
~150 tokens/job at $0.02/M → a few cents for the whole catalog.
Re-runnable: skips any job that already has embedding set.

Usage:
    python scripts/backfill_embed_jobs.py [--limit N] [--batch 64]
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from applyd.classify import embed_texts, job_embedding_text
from applyd.config import load_env


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=0, help="cap rows (0 = no cap)")
    p.add_argument("--batch", type=int, default=64, help="jobs per embedding API call")
    args = p.parse_args()

    load_env()
    from applyd.db import get_client
    sb = get_client()

    total_pending = (
        sb.table("jobs").select("*", count="exact")
        .is_("embedding", "null")
        .not_.is_("classification", "null")
        .limit(0).execute().count
    )
    print(f"→ {total_pending} classified jobs without embeddings")
    if args.limit:
        total_pending = min(total_pending, args.limit)

    done = errors = 0
    started = time.time()
    while done + errors < total_pending:
        try:
            rows = (
                sb.table("jobs")
                .select("id, title, classification")
                .is_("embedding", "null")
                .not_.is_("classification", "null")
                .order("id")
                .limit(args.batch)
                .execute().data
            )
        except Exception as e:
            print(f"  ✗ fetch failed, retrying: {type(e).__name__}: {e}", file=sys.stderr)
            time.sleep(5)
            continue
        if not rows:
            break

        try:
            vectors = embed_texts(
                [job_embedding_text(r["title"] or "", r["classification"]) for r in rows]
            )
        except Exception as e:
            print(f"  ✗ embed batch failed: {type(e).__name__}: {e}", file=sys.stderr)
            errors += len(rows)
            time.sleep(5)
            continue

        for row, vec in zip(rows, vectors):
            for attempt in range(3):
                try:
                    sb.table("jobs").update(
                        {"embedding": json.dumps(vec)}
                    ).eq("id", row["id"]).execute()
                    break
                except Exception as e:
                    if attempt == 2:
                        print(f"  ✗ update {row['id']}: {type(e).__name__}: {e}", file=sys.stderr)
                        errors += 1
                    else:
                        time.sleep(2 * (attempt + 1))
        done += len(rows)

        rate = done / max(time.time() - started, 1)
        print(f"  {done}/{total_pending}  ({rate:.0f} jobs/s)")

    print(f"✓ embedded={done} errors={errors}")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
