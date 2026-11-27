"""`applyd outreach`: draft outreach for past confirmed applications."""
from __future__ import annotations

import argparse
import sys

from ..config import load_env
from ..local_store import get_local_store
from ..outreach import draft_outreach_for_job
from ..outreach.pipeline import report


def cmd_outreach(args: argparse.Namespace) -> int:
    load_env()
    store = get_local_store()
    record = store.get_profile()
    if record is None:
        print("✗ no profile in SQLite; run `applyd evaluate --profile profile.json`", file=sys.stderr)
        return 2
    profile = record[0]
    if (profile.get("outreach") or {}).get("enabled") is not True:
        print("✗ outreach is disabled; set outreach.enabled in profile.json", file=sys.stderr)
        return 2
    try:
        from ..discovery.search.brave import BraveSearch
        search = BraveSearch()
    except Exception:  # noqa: BLE001
        search = None
    rows = store._conn.execute(
        """SELECT j.id FROM jobs j JOIN applications ap ON ap.job_id = j.id
           WHERE ap.status = 'applied' ORDER BY ap.updated_at DESC"""
    ).fetchall()
    done, counts = 0, {}
    for (job_id,) in rows:
        if done >= args.limit:
            break
        job = store.get(job_id)
        if job is None:
            continue
        summary = draft_outreach_for_job(store, job, profile, search=search, dry_run=args.dry_run)
        status = summary["status"]
        if status in {"disabled", "skipped_company_done"}:
            continue
        done += 1
        counts[status] = counts.get(status, 0) + 1
        print(f"{job.company[:28]:28} | {job.title[:40]:40}", file=sys.stderr)
        report(summary)
        if status == "not_configured":
            break
    print(f"\n✓ outreach: {counts}", file=sys.stderr)
    return 0
