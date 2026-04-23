from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from ..config import load_env
from ..enrichment import MIN_USEFUL_CHARS, SpiderClient, fetch_text
from ..models import Job
from ..store import JobStore


def cmd_enrich(args: argparse.Namespace) -> int:
    """Fetch full descriptions for jobs that lack them.
    Cascade: plain httpx+trafilatura → spider.cloud smart → spider.cloud chrome."""
    load_env()
    store = JobStore(Path(args.store))
    store.load()

    def needs_enrichment(j: Job) -> bool:
        if not j.url:
            return False
        if j.description and len(j.description) >= MIN_USEFUL_CHARS:
            return False
        if j.fetch_tier == "failed" and not args.retry_failed:
            return False
        return True

    candidates = [j for j in store.all() if needs_enrichment(j)]

    if args.source:
        candidates = [j for j in candidates if j.source == args.source]

    if args.limit:
        candidates = candidates[: args.limit]

    print(
        f"→ {len(candidates)} jobs to enrich "
        f"(store has {len(store.all())} total)",
        file=sys.stderr,
    )
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
    save_every = max(1, args.save_every)
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

        def work(job: Job) -> tuple[Job, str, str, Optional[str]]:
            try:
                text, tier, err = fetch_text(
                    job.url, spider=spider, client=client, board_cache=board_cache,
                )
            except Exception as e:
                text, tier, err = "", "failed", f"{type(e).__name__}: {e}"
            return job, text, tier, err

        from concurrent.futures import ThreadPoolExecutor, as_completed

        processed = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(work, j) for j in candidates]
            try:
                for fut in as_completed(futures):
                    job, text, tier, err = fut.result()
                    if text:
                        job.description = text
                    job.description_fetched_at = datetime.now(timezone.utc)
                    job.fetch_tier = tier
                    job.fetch_error = err
                    stats[tier] = stats.get(tier, 0) + 1

                    processed += 1
                    if processed % save_every == 0:
                        store.save()
                        running = " ".join(f"{k}={v}" for k, v in stats.items())
                        print(
                            f"  {processed}/{len(candidates)}  {running}",
                            file=sys.stderr,
                        )
            except KeyboardInterrupt:
                print("\n  interrupted; saving partial progress...", file=sys.stderr)
                for f in futures:
                    f.cancel()
                raise

    store.save()
    running = " ".join(f"{k}={v}" for k, v in stats.items())
    print(f"\n✓ enrichment complete: {running}", file=sys.stderr)
    return 0
