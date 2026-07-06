from __future__ import annotations

import argparse
import sys
from typing import Optional

import httpx

from ..classify import classify_job, embed_texts, job_embedding_text
from ..config import load_env
from ..db import JobsRepo, get_client
from ..enrichment import MIN_USEFUL_CHARS, SpiderClient, fetch_text
from ..models import Job


def cmd_enrich(args: argparse.Namespace) -> int:
    """Fetch full descriptions for jobs that lack them.
    Cascade: plain httpx+trafilatura → spider.cloud smart → spider.cloud chrome."""
    load_env()
    repo = JobsRepo(get_client())

    backfill: bool = getattr(args, "classify_backfill", False)
    candidates: list[Job] = []
    if backfill:
        # Described-but-unclassified jobs (bulk-list descriptions skip the
        # normal enrich pass entirely). No fetching — classify + embed only.
        for job in repo.iter_unclassified():
            if not job.description or len(job.description) < MIN_USEFUL_CHARS:
                continue
            if args.source and job.source != args.source:
                continue
            candidates.append(job)
            if args.limit and len(candidates) >= args.limit:
                break
    else:
        for job in repo.iter_pending_enrichment(
            include_failed=args.retry_failed,
            source=args.source,
        ):
            if not job.url:
                continue
            if job.description and len(job.description) >= MIN_USEFUL_CHARS:
                continue
            candidates.append(job)
            if args.limit and len(candidates) >= args.limit:
                break

    mode = "classify-backfill" if backfill else "enrich"
    print(f"→ {len(candidates)} jobs to {mode}", file=sys.stderr)
    if args.dry_run:
        by_source: dict[str, int] = {}
        for j in candidates:
            by_source[j.source] = by_source.get(j.source, 0) + 1
        for src, n in sorted(by_source.items(), key=lambda kv: -kv[1]):
            print(f"  {src:16s} {n}", file=sys.stderr)
        return 0

    spider: Optional[SpiderClient] = None
    try:
        spider = SpiderClient()
        print("  (spider.cloud available for tier 3)", file=sys.stderr)
    except RuntimeError as e:
        print(f"  (tier 3 disabled: {e})", file=sys.stderr)

    stats = {"ats": 0, "http": 0, "spider": 0, "spider-chrome": 0, "failed": 0}
    classify_stats = {"ok": 0, "skipped": 0, "errored": 0}
    board_cache: dict = {}  # (ats, company) -> list[Job]; tier-1 session cache
    workers = max(1, args.workers)

    limits = httpx.Limits(
        max_connections=workers * 3, max_keepalive_connections=workers * 2,
    )
    with httpx.Client(
        timeout=60.0,
        headers={"User-Agent": "applyd/0.1"},
        limits=limits,
    ) as client:
        if spider is not None:
            spider._client = client

        def work(job: Job) -> tuple[Job, str, str, Optional[str], Optional[dict], Optional[list], Optional[str]]:
            if backfill:
                text, tier, err = "", "cached", None
            else:
                try:
                    text, tier, err = fetch_text(
                        job.url, spider=spider, client=client, board_cache=board_cache,
                    )
                except Exception as e:
                    text, tier, err = "", "failed", f"{type(e).__name__}: {e}"

            classification: Optional[dict] = None
            embedding: Optional[list] = None
            classify_err: Optional[str] = None
            description = text or job.description
            if description and len(description) >= MIN_USEFUL_CHARS:
                try:
                    raw = classify_job(job.title or "", description)
                    raw.pop("_usage", None)
                    classification = raw
                    embedding = embed_texts(
                        [job_embedding_text(job.title or "", classification)]
                    )[0]
                except Exception as e:
                    classify_err = f"{type(e).__name__}: {e}"
            return job, text, tier, err, classification, embedding, classify_err

        from concurrent.futures import ThreadPoolExecutor, as_completed

        processed = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(work, j) for j in candidates]
            try:
                for fut in as_completed(futures):
                    job, text, tier, err, classification, embedding, classify_err = fut.result()
                    if backfill:
                        # Description/tier/enriched_at are already correct —
                        # only write the classification (when we got one).
                        if classification is not None:
                            repo.set_classification(job.id, classification, embedding)
                    else:
                        description = text or job.description
                        repo.mark_enriched(
                            job.id,
                            description=description,
                            tier=tier,
                            error=err,
                            classification=classification,
                            embedding=embedding,
                        )
                    stats[tier] = stats.get(tier, 0) + 1
                    if classification is not None:
                        classify_stats["ok"] += 1
                    elif classify_err is not None:
                        classify_stats["errored"] += 1
                        print(f"  ⚠ classify {job.id}: {classify_err}", file=sys.stderr)
                    else:
                        classify_stats["skipped"] += 1

                    processed += 1
                    if processed % args.save_every == 0:
                        running = " ".join(f"{k}={v}" for k, v in stats.items())
                        cls = " ".join(f"cls-{k}={v}" for k, v in classify_stats.items())
                        print(
                            f"  {processed}/{len(candidates)}  {running}  {cls}",
                            file=sys.stderr,
                        )
            except KeyboardInterrupt:
                print("\n  interrupted; partial progress already persisted", file=sys.stderr)
                for f in futures:
                    f.cancel()
                raise

    running = " ".join(f"{k}={v}" for k, v in stats.items())
    cls = " ".join(f"cls-{k}={v}" for k, v in classify_stats.items())
    print(f"\n✓ enrichment complete: {running}  {cls}", file=sys.stderr)
    return 0
