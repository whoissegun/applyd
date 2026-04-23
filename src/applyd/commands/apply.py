from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx

from ..config import load_env
from ..store import JobStore


def cmd_callback(args: argparse.Namespace) -> int:
    """Run the HTTP callback server OpenClaw's apply skill POSTs to."""
    load_env()
    import uvicorn
    from ..callback import create_app

    app = create_app(Path(args.store))
    host = args.host
    port = args.port
    print(f"→ applyd callback listening on http://{host}:{port}", file=sys.stderr)
    print(f"  POST /apply-result  {{job_id, status, note}}", file=sys.stderr)
    if not os.environ.get("APPLYD_CALLBACK_TOKEN"):
        print("  ⚠ APPLYD_CALLBACK_TOKEN not set — endpoint is unauthenticated (OK for loopback)",
              file=sys.stderr)
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


def cmd_apply_one(args: argparse.Namespace) -> int:
    """Pick one pending job from the store and dispatch it to OpenClaw's apply skill."""
    load_env()
    store = JobStore(Path(args.store))
    store.load()

    pending = store.pending_apply(limit=1)
    if not pending:
        print("no pending jobs (all tailored jobs have been attempted)", file=sys.stderr)
        return 0
    job = pending[0]

    openclaw_url = os.environ.get(
        "OPENCLAW_URL", "http://127.0.0.1:18789/v1/chat/completions"
    )
    openclaw_token = os.environ.get("OPENCLAW_TOKEN")
    if not openclaw_token:
        print("✗ OPENCLAW_TOKEN not set in .env", file=sys.stderr)
        return 1

    callback_url = os.environ.get(
        "APPLYD_CALLBACK_URL", "http://127.0.0.1:9000/apply-result"
    )
    callback_token = os.environ.get("APPLYD_CALLBACK_TOKEN", "")
    test_mode = os.environ.get("APPLYD_TEST_MODE", "true").lower() == "true"

    resume_dir = str(Path(job.resume_pdf_path).parent) if job.resume_pdf_path else ""
    screenshot_dir = str((Path(args.store).resolve().parent / "apply_screenshots"))
    Path(screenshot_dir).mkdir(parents=True, exist_ok=True)

    user_content = (
        "Run the applyd_apply skill for this job.\n\n"
        f"job_id: {job.id}\n"
        f"job_url: {job.url}\n"
        f"company: {job.company}\n"
        f"title: {job.title}\n"
        f"resume_pdf_path: {job.resume_pdf_path}\n"
        f"resume_dir: {resume_dir}\n"
        f"screenshot_dir: {screenshot_dir}\n"
        f"test_mode: {'true' if test_mode else 'false'}\n"
        f"callback_url: {callback_url}\n"
        f"callback_token: {callback_token}\n"
    )

    payload = {
        "model": args.model,
        "messages": [{"role": "user", "content": user_content}],
    }

    print(
        f"→ dispatching [{job.company}] {job.title} ({job.id}) to OpenClaw\n"
        f"  test_mode={test_mode} model={args.model}",
        file=sys.stderr,
    )

    timeout = float(os.environ.get("APPLYD_DISPATCH_TIMEOUT", "600"))
    with httpx.Client(timeout=timeout) as client:
        try:
            resp = client.post(
                openclaw_url,
                json=payload,
                headers={"Authorization": f"Bearer {openclaw_token}"},
            )
        except httpx.HTTPError as e:
            # Agent may have partially run; mark failed so it's not re-dispatched.
            store.mark_apply(job.id, "failed", f"dispatch error: {type(e).__name__}: {e}")
            store.save()
            print(f"✗ dispatch failed: {e}", file=sys.stderr)
            return 2

    if resp.status_code >= 400:
        store.mark_apply(job.id, "failed", f"openclaw {resp.status_code}: {resp.text[:200]}")
        store.save()
        print(f"✗ openclaw returned {resp.status_code}: {resp.text[:500]}", file=sys.stderr)
        return 3

    # If the skill called /apply-result, the store was already updated by the callback.
    # Re-read to check whether it did.
    store.load()
    updated = store.get(job.id)
    if updated and updated.apply_status:
        print(f"✓ agent completed: status={updated.apply_status} note={updated.apply_note!r}",
              file=sys.stderr)
    else:
        # Agent returned but never hit the callback — suspicious.
        store.mark_apply(job.id, "failed", "agent returned without callback")
        store.save()
        print("⚠ agent returned without calling /apply-result; marked failed", file=sys.stderr)
    return 0
