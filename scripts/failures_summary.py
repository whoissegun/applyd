"""Failure visibility: counts of failed/skipped applications by category over a
window. Run on demand, or on a cron with --notify for a daily digest so silent
burns surface same-day instead of weeks later.

Usage:
    python scripts/failures_summary.py [--hours 24] [--notify]
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

from applyd.config import load_env
from applyd.failures import (
    COMPILE, DEAD_LINK, JD_MISMATCH, LLM_INFRA, MATCHER, NO_JD, NO_RESUME,
    STORAGE, SYSTEMIC, UNKNOWN, VALIDATION, notify,
)

_CATEGORIES = [
    COMPILE, LLM_INFRA, STORAGE, VALIDATION,
    JD_MISMATCH, DEAD_LINK, MATCHER, NO_RESUME, NO_JD, UNKNOWN,
]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--hours", type=int, default=24, help="window to summarize")
    p.add_argument("--notify", action="store_true", help="also send via notify() (for cron digests)")
    args = p.parse_args()

    load_env()
    from applyd.db import get_client
    sb = get_client()

    since = (datetime.now(timezone.utc) - timedelta(hours=args.hours)).isoformat()

    def count(**eq) -> int:
        q = sb.table("applications").select("id", count="exact").gte("updated_at", since)
        for k, v in eq.items():
            q = q.eq(k, v)
        return q.limit(0).execute().count or 0

    applied = count(status="applied")
    rows = []
    systemic_total = 0
    for cat in _CATEGORIES:
        n = count(failure_category=cat)
        if n:
            rows.append((cat, n))
            if cat in SYSTEMIC:
                systemic_total += n

    width = max((len(c) for c, _ in rows), default=8)
    lines = [f"Failures by category (last {args.hours}h):", f"  {'applied':<{width}}  {applied}"]
    for cat, n in sorted(rows, key=lambda x: -x[1]):
        flag = "  ⚠ systemic" if cat in SYSTEMIC else ""
        lines.append(f"  {cat:<{width}}  {n}{flag}")
    report = "\n".join(lines)
    print(report)

    if systemic_total:
        print(f"\n⚠ {systemic_total} systemic-category failures in window — investigate.", file=sys.stderr)
    if args.notify:
        notify(f"applyd failures digest ({args.hours}h)", report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
