from __future__ import annotations

import argparse
import sys
from collections import Counter

from ..deduplication import deduplicate_jobs
from ..local_store import get_local_store


def cmd_dedupe(_args: argparse.Namespace) -> int:
    store = get_local_store()
    jobs = list(store.iter_all(max_rows=100_000))
    assignments = deduplicate_jobs(jobs)
    store.replace_duplicate_assignments(
        {
            "job_id": item.job_id,
            "canonical_job_id": item.canonical_job_id,
            "method": item.method,
            "duplicate_key": item.duplicate_key,
        }
        for item in assignments
    )
    methods = Counter(item.method for item in assignments)
    print(
        f"✓ deduplication complete: jobs={len(jobs)} "
        f"duplicates={len(assignments)} "
        f"ats_identity={methods['ats_identity']} "
        f"exact_fingerprint={methods['exact_fingerprint']}",
        file=sys.stderr,
    )
    return 0
