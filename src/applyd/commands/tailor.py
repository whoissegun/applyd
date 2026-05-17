from __future__ import annotations

import argparse
import os
import sys

from ..config import load_env
from ..db import ApplicationsRepo, get_client
from ..tailor.saas import tailor_for_user


def cmd_tailor(args: argparse.Namespace) -> int:
    """Run the multi-tenant tailor pipeline for one (user, job) pair.

    The CLI is a thin shim over `tailor_for_user`. Production workflows go
    through the worker (`applyd.worker.tailor_runner`); this is dev/admin
    tooling for one-off runs.
    """
    load_env()
    user_id = args.user or os.environ.get("APPLYD_DEV_USER_ID")
    if not user_id:
        print(
            "✗ pass --user <uuid> or set APPLYD_DEV_USER_ID. Tailoring is "
            "per-user (master resume + profile live in Supabase).",
            file=sys.stderr,
        )
        return 1

    sb = get_client()
    apps = ApplicationsRepo(sb)
    apps.upsert_pending(user_id=user_id, job_id=args.job_id)

    result = tailor_for_user(user_id=user_id, job_id=args.job_id)

    status = result.get("status")
    if status == "tailored":
        tokens = result.get("tokens") or {}
        print(
            f"✓ tailored: app={result.get('application_id')} "
            f"resume={result.get('tailored_resume_id')} "
            f"validator_passed={result.get('validator_passed')} "
            f"pdf={result.get('pdf_storage_path')} "
            f"tokens(prompt={tokens.get('prompt', 0)} "
            f"completion={tokens.get('completion', 0)} "
            f"cached={tokens.get('cached', 0)}) "
            f"cost_cents={result.get('cost_cents')}",
            file=sys.stderr,
        )
        return 0
    if status == "lost_race":
        print(
            f"✗ another worker claimed app {result.get('application_id')} first",
            file=sys.stderr,
        )
        return 5
    print(
        f"✗ tailor failed: app={result.get('application_id')} "
        f"reason={result.get('reason')}",
        file=sys.stderr,
    )
    return 2
