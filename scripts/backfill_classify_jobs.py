"""One-time backfill: classify every job in public.jobs that doesn't already
have a classification and has a non-null description.

Uses Haiku 4.5 via Anthropic SDK. Costs ~$0.001 per job → ~$8 for 7,714 jobs.
Re-runnable: skips any job that already has classification set.

Usage:
    python scripts/backfill_classify_jobs.py [--limit N] [--workers 4]
"""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from applyd.classify import classify_job
from applyd.classify.job import cost_cents_for_classification
from applyd.config import load_env


def _classify_one(job: dict) -> tuple[str, dict | None, int, str | None]:
    """Returns (job_id, classification_dict, cost_cents, error_str)."""
    try:
        result = classify_job(job["title"] or "", job.get("description") or "")
        cost = cost_cents_for_classification(result.pop("_usage"))
        return job["id"], result, cost, None
    except Exception as e:
        return job["id"], None, 0, f"{type(e).__name__}: {e}"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=0, help="cap rows (0 = no cap)")
    p.add_argument("--workers", type=int, default=2, help="parallel classifier calls (Anthropic Tier-1 caps at 50k tok/min input — 2 is the sustainable rate)")
    p.add_argument("--batch", type=int, default=200, help="rows per fetch from supabase")
    args = p.parse_args()

    load_env()
    from applyd.db import get_client
    sb = get_client()

    # Build the list of jobs to classify: classification IS NULL and description IS NOT NULL.
    # Page through to avoid loading 7k into memory at once.
    print("→ counting jobs to classify…")
    total_pending = (
        sb.table("jobs").select("*", count="exact")
        .is_("classification", "null")
        .not_.is_("description", "null")
        .limit(0).execute().count
    )
    cap = min(args.limit, total_pending) if args.limit else total_pending
    print(f"  {total_pending} jobs missing classification; will process {cap}")

    if cap == 0:
        print("nothing to do")
        return 0

    print(f"→ classifying with workers={args.workers}…")
    done = 0
    total_cost = 0
    errors = 0
    t0 = time.time()
    offset = 0

    while done < cap:
        # Fetch the next batch of un-classified jobs.
        page = (
            sb.table("jobs")
            .select("id, title, description")
            .is_("classification", "null")
            .not_.is_("description", "null")
            .order("posted_at", desc=True)
            .range(offset, offset + args.batch - 1)
            .execute().data
        )
        if not page:
            break

        # Filter again in case parallel runs raced us
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = [ex.submit(_classify_one, j) for j in page]
            updates = []
            for fut in as_completed(futures):
                job_id, classification, cost, err = fut.result()
                if err:
                    errors += 1
                    if errors <= 5:
                        print(f"  ✗ {job_id}: {err}", file=sys.stderr)
                    continue
                updates.append((job_id, classification))
                total_cost += cost

            # Persist this batch's updates
            for job_id, classification in updates:
                try:
                    sb.table("jobs").update({"classification": classification})\
                      .eq("id", job_id).execute()
                except Exception as e:
                    errors += 1
                    if errors <= 5:
                        print(f"  ✗ persist {job_id}: {e}", file=sys.stderr)

        done += len(page)
        elapsed = time.time() - t0
        rate = done / max(elapsed, 0.001)
        eta_s = (cap - done) / max(rate, 0.001)
        print(
            f"  {done}/{cap}  cost={total_cost}¢ (${total_cost/100:.2f})  "
            f"errors={errors}  rate={rate:.1f}/s  eta={eta_s/60:.1f}m"
        )
        if done >= cap:
            break
        # Don't bump offset — we re-query for null-classification so processed rows fall out naturally.

    print(
        f"\n→ done. processed={done} errors={errors} total_cost={total_cost}¢ "
        f"(${total_cost/100:.2f}) in {time.time()-t0:.1f}s"
    )
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
