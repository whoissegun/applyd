from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from ..local_store import get_local_store


def _run_metadata(events: list[dict[str, Any]]) -> dict[str, Any]:
    for event in events:
        if event.get("event_type") == "run_started":
            return event.get("payload") or {}
    return {}


def _is_error_event(event: dict[str, Any]) -> bool:
    payload = event.get("payload") or {}
    result = str(payload.get("result") or "")
    if result.startswith("error:") or "  err:" in result:
        return True
    return (
        event.get("event_type") == "run_finished"
        and payload.get("status") not in {"applied", "review"}
    )


def _summarize_result(name: str | None, result: str) -> str:
    lines = result.splitlines()
    refs = sum(1 for line in lines if line.startswith("r"))
    if name == "snapshot":
        return f"{refs} controls discovered"
    if refs:
        first = lines[0] if lines else ""
        return f"{first} | {refs} refreshed controls"
    first = lines[0] if lines else ""
    return first if len(first) <= 220 else first[:217] + "..."


def _attempt_record(attempt: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        **attempt,
        "run": _run_metadata(events),
        "events": events,
    }


def _print_compare(records: list[dict[str, Any]]) -> None:
    print("Provider     Mode   Status       Turns   Cost      Reason")
    print("-----------  -----  -----------  ------  --------  ------------------------------")
    for record in records:
        run = record.get("run") or {}
        provider = str(run.get("browser_provider") or "unknown")[:11]
        mode = (
            "unknown" if "test_mode" not in run
            else ("test" if run.get("test_mode") else "real")
        )
        reason = str(record.get("reason") or "")
        if len(reason) > 30:
            reason = reason[:27] + "..."
        print(
            f"{provider:<11}  {mode:<5}  {str(record.get('status') or ''):<11}  "
            f"{int(record.get('turn_count') or 0):>6}  "
            f"${float(record.get('cost_usd') or 0):>7.4f}  {reason}"
        )


def _print_timeline(record: dict[str, Any], *, errors_only: bool) -> None:
    run = record.get("run") or {}
    print(
        f"\nAttempt {record['id']} | provider={run.get('browser_provider', 'unknown')} "
        f"mode={('unknown' if 'test_mode' not in run else ('test' if run.get('test_mode') else 'real'))} | "
        f"status={record.get('status')} | cost=${float(record.get('cost_usd') or 0):.4f} "
        f"| turns={int(record.get('turn_count') or 0)}"
    )
    if record.get("reason"):
        print(f"Reason: {record['reason']}")
    for event in record.get("events") or []:
        if errors_only and not _is_error_event(event):
            continue
        kind = event.get("event_type")
        turn = event.get("turn")
        prefix = "Final" if turn is None else f"Turn {turn}"
        name = event.get("name") or ""
        payload = event.get("payload") or {}
        if kind == "model_turn":
            tools = ", ".join(payload.get("tool_names") or []) or "no tools"
            detail = f"model -> {tools} | cumulative=${float(payload.get('cumulative_cost_usd') or 0):.4f}"
        elif kind == "tool_call":
            detail = f"call {name} {json.dumps(payload.get('args') or {}, ensure_ascii=False)}"
        elif kind == "tool_result":
            result = str(payload.get("result") or "")
            marker = "✗" if _is_error_event(event) else "✓"
            detail = f"{marker} {name}: {_summarize_result(name, result)}"
        elif kind == "run_started":
            detail = f"started model={payload.get('model')}"
        elif kind == "run_finished":
            detail = f"finished status={payload.get('status')} note={payload.get('note', '')}"
        else:
            detail = f"{kind} {name}".strip()
        print(f"{prefix:<8} {detail}")


def cmd_trace(args: argparse.Namespace) -> int:
    store = get_local_store()
    job = store.get(args.job_id)
    if job is None:
        print(f"✗ job {args.job_id!r} not found", file=sys.stderr)
        return 1
    attempts = store.get_apply_attempts(job.id)
    if not attempts:
        print(f"✗ no apply attempts for {job.company} — {job.title}", file=sys.stderr)
        return 1
    selected = attempts if (args.all or args.compare) else [attempts[-1]]
    records = [
        _attempt_record(attempt, store.get_apply_trace(attempt["id"]))
        for attempt in selected
    ]
    if args.format == "json":
        print(json.dumps({
            "job": {"id": job.id, "company": job.company, "title": job.title},
            "attempts": records,
        }, indent=2, ensure_ascii=False))
        return 0
    print(f"{job.company} — {job.title}\n{job.id}")
    if args.compare:
        _print_compare(records)
        return 0
    for record in records:
        _print_timeline(record, errors_only=args.errors_only)
    return 0
