"""Direct apply runner: tool-use loop driving Bright Data Chrome via CDP.

All LLM calls go through OpenRouter (OpenAI-compatible API). The default model
is `anthropic/claude-sonnet-4-6` but any OpenRouter slug works via --model.

`run_apply(...)` is the programmatic, stateless entry point used by the
multi-tenant orchestration in `applyd.apply.saas`. The CLI in `main()` is a
dev/admin shim that resolves a (user, job) pair into an application row and
dispatches through `apply_for_user`.

CLI exit codes: 0 applied, 1 skipped, 2 failed, 3 infra error.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from contextlib import contextmanager
from typing import Any

from openai import OpenAI

from ..config import load_env
from ..llm_errors import is_transient_llm_error
from .browser import brightdata_page
from .prompts import SYSTEM_PROMPT, build_user_blocks
from .tools import TOOL_DEFS, dispatch


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "anthropic/claude-sonnet-4-6"
MAX_TURNS = 40  # safety net; a normal apply is 12-20 tool calls
# Hard wall-clock ceiling per apply. MAX_TURNS bounds LLM round-trips but not
# a single blocking browser call: sync-Playwright `evaluate` has no timeout,
# so a silently-dead Bright Data CDP socket hangs the call — and the whole
# single-process worker — forever (froze prod for 40+ min on 2026-07-04).
MAX_WALL_SECONDS = int(os.environ.get("APPLYD_APPLY_MAX_SECONDS", "900"))
# Grace on top of MAX_WALL_SECONDS before the hard watchdog kills the process.
WATCHDOG_GRACE_SECONDS = 60


class ApplyTimeoutError(Exception):
    """The apply exceeded its wall-clock budget (hung browser call, most likely)."""


def _start_hard_watchdog(seconds: int, job_id: str) -> threading.Event:
    """Backstop for hangs SIGALRM can't interrupt. Sync-Playwright parks the
    main thread inside its transport wait, so the Python-level alarm handler
    never gets to run — a dead CDP socket froze prod for 12h on 2026-07-07
    with the alarm armed the whole time. A daemon thread force-exits the
    process past the deadline; the supervisor restarts it and the orphan
    reaper requeues the abandoned claim. Set the returned Event to disarm.
    """
    done = threading.Event()

    def _watch() -> None:
        if not done.wait(seconds):
            print(
                f"✗ hard watchdog: apply for {job_id} exceeded {seconds}s; "
                f"force-exiting so the supervisor restarts us",
                file=sys.stderr,
                flush=True,
            )
            os._exit(70)

    threading.Thread(target=_watch, daemon=True, name="apply-watchdog").start()
    return done


@contextmanager
def _wall_clock_deadline(seconds: int):
    """SIGALRM-based hard deadline. Interrupts blocking C calls (unlike any
    cooperative check). No-ops off the main thread (e.g. FastAPI handlers) and
    on platforms without SIGALRM — there it falls back to MAX_TURNS only.
    """
    if (
        seconds <= 0
        or not hasattr(signal, "SIGALRM")
        or threading.current_thread() is not threading.main_thread()
    ):
        yield
        return

    def _raise(_signum, _frame):
        raise ApplyTimeoutError(f"apply exceeded {seconds}s wall clock")

    old_handler = signal.signal(signal.SIGALRM, _raise)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


def _make_client() -> OpenAI:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit(
            "OPENROUTER_API_KEY not set. Get one from https://openrouter.ai/keys "
            "and add it to .env."
        )
    return OpenAI(base_url=OPENROUTER_BASE_URL, api_key=key)


def run_apply(
    *,
    job_id: str,
    company: str,
    title: str,
    job_url: str,
    resume_pdf_path: str,
    profile_md: str,
    resume_tex: str = "",
    tailor_metadata_json: str = "",
    model: str = DEFAULT_MODEL,
    test_mode: bool | None = None,
) -> dict[str, Any]:
    """Programmatic apply entry — stateless, no I/O outside the browser+LLM.

    Returns a dict with keys: status, note, tokens, browser_mb. The caller
    persists results (`applyd.apply.saas.apply_for_user` handles this for the
    multi-tenant flow).
    """
    load_env()
    if test_mode is None:
        test_mode = os.environ.get("APPLYD_TEST_MODE", "true").lower() == "true"

    user_blocks = build_user_blocks(
        job_id=job_id,
        company=company,
        title=title,
        job_url=job_url,
        resume_pdf_path=resume_pdf_path,
        profile_md=profile_md,
        resume_tex=resume_tex,
        tailor_metadata_json=tailor_metadata_json,
        test_mode=test_mode,
    )

    # System prompt cached. cache_control markers pass through OpenRouter to
    # Anthropic; on other upstreams (DeepSeek auto-cache, Llama via Together /
    # Fireworks / Groq) the marker is harmless and provider-side caching kicks
    # in automatically.
    system_message = {
        "role": "system",
        "content": [
            {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
        ],
    }
    user_message = {"role": "user", "content": user_blocks}

    client = _make_client()
    messages: list[dict[str, Any]] = [system_message, user_message]
    tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}

    final_status: str | None = None
    final_note: str = ""
    turns_done: int = 0
    tool_call_counts: dict[str, int] = {}

    print(
        f"→ direct-apply [{company}] {title} ({job_id})\n"
        f"  test_mode={test_mode} model={model}",
        file=sys.stderr,
    )

    watchdog_disarm = _start_hard_watchdog(
        MAX_WALL_SECONDS + WATCHDOG_GRACE_SECONDS, job_id
    )
    try:
        with _wall_clock_deadline(MAX_WALL_SECONDS), brightdata_page(block_heavy=True) as page:
            for turn in range(MAX_TURNS):
                # Force a deterministic opener:
                #   turn 0 = navigate (must hit the URL first)
                #   turn 1 = snapshot (must inspect before any click/fill)
                # Tried forcing turn 2 = fill_many but it back-fired on Ashby;
                # see git history for context.
                if turn == 0:
                    tool_choice: Any = {"type": "function", "function": {"name": "navigate"}}
                elif turn == 1:
                    tool_choice = {"type": "function", "function": {"name": "snapshot"}}
                else:
                    tool_choice = "auto"

                resp = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=TOOL_DEFS,
                    tool_choice=tool_choice,
                    max_tokens=4096,
                )

                _accumulate_tokens(tokens, resp.usage)
                turns_done = turn + 1

                asst = resp.choices[0].message
                # Append the assistant turn back to history. Some upstreams
                # return null content; OpenAI SDK accepts dicts or model objects.
                messages.append(asst.model_dump(exclude_none=True))

                tool_calls = asst.tool_calls or []
                if not tool_calls:
                    # Model gave a text answer instead of calling a tool — abort.
                    final_status = "failed"
                    final_note = f"agent stopped without tool call: {(asst.content or '')[:200]}"
                    break

                done = False
                for tc in tool_calls:
                    name = tc.function.name
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError as e:
                        result = f"error: malformed tool args: {e}"
                        messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
                        continue

                    tool_call_counts[name] = tool_call_counts.get(name, 0) + 1
                    print(f"  [turn {turn}] {name}({_summarize_args(args)})", file=sys.stderr)

                    if name == "report_done":
                        final_status = args.get("status", "failed")
                        final_note = args.get("note", "")
                        messages.append({"role": "tool", "tool_call_id": tc.id, "content": "ok: recorded"})
                        done = True
                        continue

                    result = dispatch(page, name, args, test_mode=test_mode)
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

                if done:
                    break
            else:
                final_status = "failed"
                final_note = f"hit MAX_TURNS={MAX_TURNS} without report_done"
    except Exception as e:
        # Transient infra (no credits, rate limit, provider outage) is not this
        # job's fault — surface a distinct status so the caller requeues instead
        # of burning the application to terminal 'failed'.
        final_status = "infra_error" if is_transient_llm_error(e) else "failed"
        final_note = f"runner exception: {type(e).__name__}: {str(e)[:200]}"
        print(f"✗ runner exception: {type(e).__name__}: {e}", file=sys.stderr)
    finally:
        watchdog_disarm.set()

    print(
        f"✓ direct-apply done: status={final_status} note={final_note!r}\n"
        f"  tokens: input={tokens['input']} output={tokens['output']} "
        f"cache_read={tokens['cache_read']} cache_write={tokens['cache_write']}",
        file=sys.stderr,
    )
    # browser_mb: we don't currently meter Bright Data bandwidth per-call.
    # 1.5 MB is the observed average on Greenhouse/Lever forms — see pricing.py.
    return {
        "status": final_status or "failed",
        "note": final_note,
        "tokens": tokens,
        "browser_mb": 1.5,
        "turn_count": turns_done,
        "tool_call_counts": tool_call_counts,
    }


def _accumulate_tokens(tokens: dict[str, int], usage: Any) -> None:
    """Best-effort token accumulation across providers. Field names vary."""
    if usage is None:
        return
    tokens["input"] += getattr(usage, "prompt_tokens", 0) or 0
    tokens["output"] += getattr(usage, "completion_tokens", 0) or 0
    # OpenAI / OpenRouter standard: prompt_tokens_details.cached_tokens (read).
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        cached = getattr(details, "cached_tokens", 0) or 0
        tokens["cache_read"] += cached
        # Subtract cached from input so "fresh input" reflects only un-cached.
        tokens["input"] -= cached
    # Anthropic-specific (when routed via OpenRouter to Anthropic, sometimes surfaces here):
    tokens["cache_read"] += getattr(usage, "cache_read_input_tokens", 0) or 0
    tokens["cache_write"] += getattr(usage, "cache_creation_input_tokens", 0) or 0


def _summarize_args(args: dict[str, Any]) -> str:
    out = []
    for k, v in args.items():
        s = str(v)
        if len(s) > 60:
            s = s[:57] + "..."
        out.append(f"{k}={s!r}")
    return ", ".join(out)


def main(argv: list[str] | None = None) -> int:
    """Dev/admin CLI: dispatch one apply for a (user, job) pair.

    Resolves the application row by (user_id, job_id) and hands off to
    `apply_for_user`. The `APPLYD_TEST_MODE` env var controls submit vs.
    fill-only; the model is set via `APPLYD_APPLY_MODEL`.
    """
    load_env()

    p = argparse.ArgumentParser(prog="python -m applyd.apply.runner")
    p.add_argument("job_id", help="job id (from `applyd jobs`)")
    p.add_argument(
        "--user",
        help="user UUID. Falls back to APPLYD_DEV_USER_ID if unset.",
    )
    p.add_argument(
        "--app",
        help=(
            "application UUID. If passed, --user and job_id are ignored — "
            "we go straight to apply_for_user."
        ),
    )
    args = p.parse_args(argv)

    # Lazy imports: keep `from .runner import run_apply` (used by saas.py)
    # free of Supabase deps so the worker module can import it cheaply.
    from ..db import ApplicationsRepo, get_client
    from .saas import apply_for_user

    sb = get_client()
    apps = ApplicationsRepo(sb)

    if args.app:
        application = apps.get(args.app)
        if application is None:
            print(f"✗ application {args.app!r} not found", file=sys.stderr)
            return 3
        user_id = application["user_id"]
        application_id = application["id"]
    else:
        user_id = args.user or os.environ.get("APPLYD_DEV_USER_ID")
        if not user_id:
            print(
                "✗ pass --user <uuid>, --app <uuid>, or set APPLYD_DEV_USER_ID.",
                file=sys.stderr,
            )
            return 3
        application = apps.get_by_user_job(user_id, args.job_id)
        if application is None:
            print(
                f"✗ no application for user={user_id} job={args.job_id}. "
                "Run `applyd tailor` first to create one.",
                file=sys.stderr,
            )
            return 3
        application_id = application["id"]

    result = apply_for_user(user_id=user_id, application_id=application_id)
    print(
        f"✓ apply done: status={result.get('status')} "
        f"reason={result.get('reason')} "
        f"cost_cents={result.get('cost_cents')}",
        file=sys.stderr,
    )
    return {
        "applied": 0,
        "skipped": 1,
        "failed": 2,
        "lost_race": 5,
    }.get(result.get("status", "failed"), 3)


if __name__ == "__main__":
    sys.exit(main())
