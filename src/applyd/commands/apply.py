from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..apply.runner import DEFAULT_MODEL, run_apply
from ..config import load_env
from ..discovery.routing import detect_ats, preferred_apply_url
from ..liveness import check_jobs_liveness
from ..local_store import get_local_store


def cmd_apply(args: argparse.Namespace) -> int:
    load_env()
    store = get_local_store()
    job = store.get(args.job_id)
    profile_record = store.get_profile()
    if job is None:
        print(f"✗ job {args.job_id!r} not found", file=sys.stderr)
        return 3
    if profile_record is None:
        print("✗ no profile in SQLite; run `applyd evaluate --profile profile.json`", file=sys.stderr)
        return 3

    liveness = store.get_liveness(job.id)
    fresh_after = datetime.now(timezone.utc) - timedelta(
        minutes=max(0, args.liveness_ttl_minutes)
    )
    checked_at = None
    if liveness:
        try:
            checked_at = datetime.fromisoformat(liveness["checked_at"])
        except (TypeError, ValueError):
            checked_at = None
    if checked_at is None or checked_at < fresh_after:
        result = check_jobs_liveness([job], workers=1)[0]
        store.record_liveness(job.id, result.status, result.method, result.detail)
        liveness = {
            "status": result.status,
            "method": result.method,
            "detail": result.detail,
        }
    if liveness and liveness.get("status") == "closed":
        print(
            f"✗ posting is closed: {liveness.get('detail', '')}",
            file=sys.stderr,
        )
        return 1

    try:
        application, attempt_id = store.start_apply_attempt(job.id, args.model)
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 3

    pdf_path = Path(application["pdf_path"])
    latex_path = Path(application["latex_path"])
    if not pdf_path.exists() or not latex_path.exists():
        store.finish_apply_attempt(
            attempt_id,
            status="failed",
            reason="tailored resume artifact is missing",
        )
        print("✗ tailored resume PDF or LaTeX file is missing", file=sys.stderr)
        return 3

    profile, _ = profile_record
    persisted = False

    def persist_event(event: dict[str, Any]) -> None:
        if event.get("event_type") == "profile_gap":
            payload = event.get("payload", {})
            store.record_profile_question_gap(
                label=str(payload.get("label", "")),
                category=str(payload.get("category", "other_fact")),
                job_id=job.id,
                company=job.company,
            )
        store.record_apply_trace_event(
            attempt_id,
            sequence=int(event["sequence"]),
            turn=event.get("turn"),
            event_type=str(event["event_type"]),
            name=event.get("name"),
            payload=event.get("payload", {}),
        )

    def persist(result: dict[str, Any]) -> None:
        nonlocal persisted
        status = result.get("status", "failed")
        if args.test_mode and status == "applied":
            status = "tested"
        store.finish_apply_attempt(
            attempt_id,
            status=status,
            reason=result.get("note", ""),
            cost_usd=float(result.get("actual_cost_usd", 0.0)),
            turn_count=int(result.get("turn_count", 0)),
            tool_calls=result.get("tool_call_counts", {}),
        )
        persisted = True

    apply_url = preferred_apply_url(job.id, job.url, company=job.company)
    browser_provider = args.browser
    if browser_provider == "auto":
        browser_provider = "brightdata" if detect_ats(apply_url) == "lever" else "local"

    try:
        result = run_apply(
            job_id=job.id,
            company=job.company,
            title=job.title,
            job_url=apply_url,
            resume_pdf_path=str(pdf_path.resolve()),
            profile_md=json.dumps(profile, indent=2, ensure_ascii=False),
            job_locations=list(job.locations),
            resume_tex=latex_path.read_text(encoding="utf-8"),
            tailor_metadata_json=json.dumps(
                application.get("edit_plan", {}), ensure_ascii=False
            ),
            model=args.model,
            test_mode=args.test_mode,
            browser_provider=browser_provider,
            on_verdict=persist,
            on_event=persist_event,
            max_cost_usd=args.max_cost_usd,
        )
        if not persisted:
            persist(result)
    except BaseException as exc:
        if not persisted:
            store.finish_apply_attempt(
                attempt_id,
                status="infra_error",
                reason=f"command exception: {type(exc).__name__}: {str(exc)[:200]}",
            )
        raise

    stored_status = "tested" if args.test_mode and result["status"] == "applied" else result["status"]
    print(
        f"✓ apply finished: status={stored_status} cost=${result['actual_cost_usd']:.4f} "
        f"turns={result['turn_count']} note={result['note']}",
        file=sys.stderr,
    )
    if float(result.get("actual_cost_usd", 0.0)) > 0.10:
        print("⚠ apply cost exceeded $0.10", file=sys.stderr)
    return {
        "applied": 0, "tested": 0, "review": 1, "skipped": 1, "failed": 2
    }.get(stored_status, 3)
