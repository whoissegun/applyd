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
import sys
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

    try:
        with brightdata_page(block_heavy=True) as page:
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
