"""Recovery: reset terminally-failed applications back to a claimable state so
they re-run after a systemic bug is fixed (e.g. the compile burn). Replaces the
ad-hoc reset script written during the June 2026 incident.

Only resets the categories you name — leaves genuinely per-job failures alone.

Usage:
    python scripts/requeue_failures.py --category compile [--category llm_infra] [--dry-run]
    python scripts/requeue_failures.py --all-fixable [--dry-run]
"""
from __future__ import annotations

import argparse
import sys

from applyd.config import load_env
from applyd.failures import COMPILE, LLM_INFRA, STORAGE, VALIDATION

# Categories generally safe to retry after fixing the underlying environment.
# Excludes per-job/per-user reasons (dead_link, jd_mismatch, matcher, no_resume)
# where a retry would just fail the same way.
_FIXABLE = [COMPILE, LLM_INFRA, STORAGE, VALIDATION]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--category", action="append", default=[], help="failure_category to requeue (repeatable)")
    p.add_argument("--all-fixable", action="store_true", help=f"requeue all of: {', '.join(_FIXABLE)}")
    p.add_argument("--to-status", default="pending", choices=["pending", "tailored"], help="claimable status to reset to")
    p.add_argument("--dry-run", action="store_true", help="report what would change, don't write")
    args = p.parse_args()

    cats = list(dict.fromkeys(_FIXABLE if args.all_fixable else args.category))
    if not cats:
        print("nothing to do: pass --category <cat> or --all-fixable", file=sys.stderr)
        return 2

    load_env()
    from applyd.db import get_client
    sb = get_client()

    total = 0
    for cat in cats:
        rows = (
            sb.table("applications")
            .select("id")
            .eq("status", "failed")
            .eq("failure_category", cat)
            .execute().data
        )
        print(f"{cat}: {len(rows)} failed rows")
        if args.dry_run:
            total += len(rows)
            continue
        for r in rows:
            sb.table("applications").update(
                {"status": args.to_status, "last_error": None, "failure_category": None}
            ).eq("id", r["id"]).eq("status", "failed").execute()
        total += len(rows)

    verb = "would requeue" if args.dry_run else f"requeued -> {args.to_status}"
    print(f"\n{verb}: {total} applications")
    return 0


if __name__ == "__main__":
    sys.exit(main())
