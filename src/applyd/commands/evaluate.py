from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..eligibility import evaluate_job
from ..local_store import get_local_store


def cmd_evaluate(args: argparse.Namespace) -> int:
    store = get_local_store()
    profile_path = Path(args.profile)
    if profile_path.exists():
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        profile_hash = store.set_profile(profile)
    else:
        stored = store.get_profile()
        if stored is None:
            print(
                f"✗ no profile found at {profile_path} or in the local database",
                file=sys.stderr,
            )
            return 2
        profile, profile_hash = stored

    stats = {"eligible": 0, "ineligible": 0, "uncertain": 0}
    shown = 0
    for job, facts in store.iter_for_evaluation(limit=args.limit):
        result = evaluate_job(job, facts, profile)
        store.set_evaluation(
            job.id,
            result.decision,
            result.reasons,
            profile_hash,
        )
        stats[result.decision] += 1
        if args.show_reasons and result.decision != "eligible":
            print(
                json.dumps(
                    {
                        "job_id": job.id,
                        "company": job.company,
                        "title": job.title,
                        "decision": result.decision,
                        "reasons": result.reasons,
                    },
                    ensure_ascii=False,
                )
            )
            shown += 1

    print(
        "✓ evaluation complete: "
        + " ".join(f"{key}={value}" for key, value in stats.items()),
        file=sys.stderr,
    )
    if args.show_reasons and shown == 0:
        print("  no ineligible/uncertain jobs in this slice", file=sys.stderr)
    return 0
