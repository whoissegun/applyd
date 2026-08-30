from __future__ import annotations

import argparse
import sys
from collections import Counter

from ..liveness import check_jobs_liveness
from ..local_store import get_local_store


def cmd_verify_live(args: argparse.Namespace) -> int:
    store = get_local_store()
    ranked = list(store.iter_ranked_matches(limit=max(1, args.top)))
    jobs = [store.get(row["job_id"]) for row in ranked]
    results = check_jobs_liveness(
        [job for job in jobs if job is not None], workers=args.workers
    )
    for result in results:
        store.record_liveness(
            result.job_id, result.status, result.method, result.detail
        )
        print(f"  {result.status:7s} {result.job_id} — {result.detail}")
    counts = Counter(result.status for result in results)
    print(
        f"✓ liveness complete: checked={len(results)} live={counts['live']} "
        f"closed={counts['closed']} unknown={counts['unknown']}",
        file=sys.stderr,
    )
    return 0
