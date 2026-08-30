from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..local_store import LocalStore
from ..models import Job


def cmd_init(args: argparse.Namespace) -> int:
    store = LocalStore(args.db)
    imported = 0

    if args.import_legacy:
        source = Path(args.import_legacy)
        if not source.exists():
            print(f"✗ legacy catalog not found: {source}", file=sys.stderr)
            return 2
        raw = json.loads(source.read_text(encoding="utf-8"))
        records = list(raw.values()) if isinstance(raw, dict) else list(raw)
        jobs: list[Job] = []
        for record in records:
            try:
                jobs.append(Job.model_validate(record))
            except Exception as exc:  # noqa: BLE001
                print(f"  ⚠ skipped malformed legacy job: {exc}", file=sys.stderr)
        new, _ = store.upsert(jobs)
        imported = new

    profile_path = Path(args.profile)
    if profile_path.exists():
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        store.set_profile(profile)

    counts = store.counts()
    print(f"✓ local database ready: {store.path}")
    print(
        f"  jobs={counts['jobs']} active={counts['active']} "
        f"described={counts['described']} extracted={counts['extracted']}"
    )
    if imported:
        print(f"  imported={imported} from {args.import_legacy}")
    if not profile_path.exists():
        print(f"  profile not imported (missing {profile_path})")
    return 0
