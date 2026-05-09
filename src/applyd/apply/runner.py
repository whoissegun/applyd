"""Direct apply runner: tool-use loop driving Bright Data Chrome via CDP.

All LLM calls go through OpenRouter (OpenAI-compatible API). The default model
is `anthropic/claude-sonnet-4-6` but any OpenRouter slug works via --model.

Usage as a script (spike):
    python -m applyd.apply.runner <job_id>
    python -m applyd.apply.runner <job_id> --model deepseek/deepseek-chat
    python -m applyd.apply.runner <job_id> --model meta-llama/llama-3.3-70b-instruct

Returns 0 on applied, 1 on skipped, 2 on failed, 3 on infra error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from openai import OpenAI

from ..config import load_env
from ..store import JobStore
from .browser import brightdata_page
from .prompts import SYSTEM_PROMPT, build_user_blocks, load_profile
from .tools import TOOL_DEFS, dispatch


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "anthropic/claude-sonnet-4-6"
MAX_TURNS = 40  # safety net; a normal apply is 12-20 tool calls


def _read_or_blank(path: str | Path) -> str:
    p = Path(path)
    return p.read_text() if p.exists() else ""


def _make_client() -> OpenAI:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit(
            "OPENROUTER_API_KEY not set. Get one from https://openrouter.ai/keys "
            "and add it to .env."
        )
    return OpenAI(base_url=OPENROUTER_BASE_URL, api_key=key)


def run(
    job_id: str,
    *,
    store_path: str = "data/jobs.json",
    model: str = DEFAULT_MODEL,
    test_mode: bool | None = None,
    profile_path: str = "~/.openclaw/workspace/USER.md",
) -> tuple[str, str, dict[str, int]]:
    """Run one apply. Returns (status, note, token_usage). Always writes back to store."""
    load_env()
    if test_mode is None:
        test_mode = os.environ.get("APPLYD_TEST_MODE", "true").lower() == "true"

    store = JobStore(Path(store_path))
    store.load()
    job = store.get(job_id)
    if not job:
        raise SystemExit(f"job {job_id} not found in {store_path}")
    if not job.resume_pdf_path:
        raise SystemExit(f"job {job_id} has no resume_pdf_path; run `applyd tailor {job_id}` first")

    resume_dir = Path(job.resume_pdf_path).parent
    profile_md = load_profile(profile_path)
    resume_tex = _read_or_blank(resume_dir / "resume.tex")
    metadata_json = _read_or_blank(resume_dir / "metadata.json")

    user_blocks = build_user_blocks(
        job_id=job.id,
        company=job.company,
        title=job.title,
        job_url=job.url,
        resume_pdf_path=job.resume_pdf_path,
        profile_md=profile_md,
        resume_tex=resume_tex,
        tailor_metadata_json=metadata_json,
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

    print(
        f"→ direct-apply [{job.company}] {job.title} ({job.id})\n"
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
        final_status = "failed"
        final_note = f"runner exception: {type(e).__name__}: {str(e)[:200]}"
        print(f"✗ runner exception: {type(e).__name__}: {e}", file=sys.stderr)

    store.load()
    store.mark_apply(job.id, final_status or "failed", final_note)
    store.save()

    print(
        f"✓ direct-apply done: status={final_status} note={final_note!r}\n"
        f"  tokens: input={tokens['input']} output={tokens['output']} "
        f"cache_read={tokens['cache_read']} cache_write={tokens['cache_write']}",
        file=sys.stderr,
    )
    return final_status or "failed", final_note, tokens


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
    p = argparse.ArgumentParser(prog="python -m applyd.apply.runner")
    p.add_argument("job_id")
    p.add_argument("--store", default="data/jobs.json")
    p.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=(
            "OpenRouter model slug. Default: anthropic/claude-sonnet-4-6. "
            "Other examples: deepseek/deepseek-chat, meta-llama/llama-3.3-70b-instruct, "
            "qwen/qwen-2.5-72b-instruct."
        ),
    )
    p.add_argument(
        "--test-mode",
        choices=["true", "false"],
        default=None,
        help="override APPLYD_TEST_MODE from env",
    )
    p.add_argument("--profile", default="~/.openclaw/workspace/USER.md")
    args = p.parse_args(argv)

    test_mode = (
        None if args.test_mode is None else args.test_mode == "true"
    )

    status, _note, _tokens = run(
        args.job_id,
        store_path=args.store,
        model=args.model,
        test_mode=test_mode,
        profile_path=args.profile,
    )
    return {"applied": 0, "skipped": 1, "failed": 2}.get(status, 3)


if __name__ == "__main__":
    sys.exit(main())
