from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from ..filters import filter_jobs
from ..store import JobStore


def cmd_jobs(args: argparse.Namespace) -> int:
    store = JobStore(Path(args.store))
    store.load()

    remote_filter: Optional[bool] = True if args.remote else None
    gated_filter: Optional[bool] = None
    if args.gated:
        gated_filter = True
    elif args.no_gated:
        gated_filter = False

    jobs = filter_jobs(
        store.all(),
        level=args.level,
        specialty=args.specialty,
        location=args.location,
        remote=remote_filter,
        source=args.source,
        company=args.company,
        gated=gated_filter,
    )

    jobs.sort(
        key=lambda j: (j.posted_at is None, j.posted_at or 0),
        reverse=True,
    )

    if args.format == "json":
        print(json.dumps(
            [j.model_dump(mode="json") for j in jobs[: args.limit]],
            indent=2, default=str,
        ))
    else:
        shown = jobs[: args.limit]
        for j in shown:
            loc = ", ".join(j.locations) if j.locations else "—"
            print(
                f"[{j.source:16s}] "
                f"{j.company[:28]:28s} | "
                f"{j.title[:48]:48s} | "
                f"{loc[:28]:28s} | "
                f"{j.url}"
            )
        print(
            f"\n{len(jobs)} matching (showing {len(shown)})",
            file=sys.stderr,
        )
    return 0
