"""Direct apply runner: tool-use loop driving a persistent local Chrome profile.

All LLM calls go through OpenRouter's OpenAI-compatible API. Kimi K2.6 is the
default, but any tool-capable OpenRouter model can be selected with --model.

`run_apply(...)` is the stateless browser entry point. SQLite claiming and
attempt persistence live in `applyd.commands.apply`.

CLI exit codes: 0 applied, 1 skipped, 2 failed, 3 infra error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import threading
from contextlib import contextmanager
from typing import Any, Callable

from openai import OpenAI

from ..config import load_env
from ..llm_errors import is_transient_llm_error
from .browser import BrowserConnectError, browser_page
from .email_verify import build_code_reader
from .prompts import SYSTEM_PROMPT, build_user_blocks
from .tools import TOOL_DEFS, dispatch


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# Hosted Kimi passed the current apply eval more reliably than the downloaded
# local Ministral build while remaining inexpensive. Keep this caller-swappable.
DEFAULT_MODEL = "moonshotai/kimi-k2.6"
# Pilot evidence: routine successful forms finish in 7-18 model turns, and 10
# of 11 successful attempts finished by turn 24. Runs beyond that were mostly
# captcha/control loops. Override only for a deliberately inspected long form.
MAX_TURNS = int(os.environ.get("APPLYD_APPLY_MAX_TURNS", "25"))
# Hard wall-clock ceiling per apply. MAX_TURNS bounds LLM round-trips but not
# a single blocking browser call: sync-Playwright `evaluate` has no timeout,
# so a silently-dead Bright Data CDP socket hangs the call — and the whole
# single-process worker — forever (froze prod for 40+ min on 2026-07-04).
# Per-attempt wall-clock cap. A healthy apply is ~95s; anything past a few
# minutes is a stuck form, a captcha stall, or a hung browser call. Capped low
# so one bad job can't hog the single-worker queue (a 30-min hang stalled ~14
# tailored jobs behind it). Failing fast + MAX_APPLY_ATTEMPTS is cheaper than
# grinding. Override with APPLYD_APPLY_MAX_SECONDS if a slow ATS needs it.
# 420s (not 300) leaves room for the email-verification path: a wall-hitting
# submit waits for the security code to arrive by email (~30s typical) then
# resubmits in-session. Non-wall applies still return in ~95s — this only raises
# the ceiling for the rare wall case, it doesn't slow the common path.
MAX_WALL_SECONDS = int(os.environ.get("APPLYD_APPLY_MAX_SECONDS", "420"))
# Stop buying more model turns once one application reaches this amount. The
# already-returned tool calls still run, so a final submit/review action is not
# discarded after it has already been paid for. Set 0 to disable.
# Cost is observed and reported, not a default stopping condition. Turn and
# no-progress bounds stop runaway agents. Callers may still opt into a limit.
DEFAULT_MAX_COST_USD = 0.0
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
    job_locations: list[str] | None = None,
    resume_tex: str = "",
    tailor_metadata_json: str = "",
    model: str = DEFAULT_MODEL,
    test_mode: bool | None = None,
    browser_provider: str | None = None,
    on_verdict: Callable[[dict[str, Any]], None] | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    max_cost_usd: float | None = None,
) -> dict[str, Any]:
    """Programmatic apply entry — stateless, no I/O outside the browser+LLM.

    Returns a dict with keys: status, note, tokens, browser_mb. The caller
    persists results (`applyd.apply.saas.apply_for_user` handles this for the
    multi-tenant flow).

    `on_verdict` fires with the full result dict the moment report_done lands —
    BEFORE the browser closes. browser.close() over a stalled CDP socket can
    hang past the hard watchdog, and a verdict that dies unpersisted gets
    requeued — for status='applied' that means re-submitting a form a company
    already received (near-miss, 2026-07-11 crash). Persist in the callback;
    the return value is the same dict.
    """
    load_env()
    if test_mode is None:
        test_mode = os.environ.get("APPLYD_TEST_MODE", "true").lower() == "true"
    if max_cost_usd is None:
        max_cost_usd = float(
            os.environ.get("APPLYD_APPLY_MAX_COST_USD", str(DEFAULT_MAX_COST_USD))
        )

    user_blocks = build_user_blocks(
        job_id=job_id,
        company=company,
        title=title,
        job_url=job_url,
        resume_pdf_path=resume_pdf_path,
        profile_md=profile_md,
        job_locations=job_locations or [],
        resume_tex=resume_tex,
        tailor_metadata_json=tailor_metadata_json,
        test_mode=test_mode,
    )
    try:
        profile_context = json.loads(profile_md)
        if not isinstance(profile_context, dict):
            profile_context = {}
    except json.JSONDecodeError:
        profile_context = {}

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
    actual_cost_usd = 0.0

    # OpenRouter request extras, all cost levers:
    # - top-level cache_control = "automatic caching": OpenRouter slides the
    #   cache breakpoint to the newest message every request, so the entire
    #   growing tool-loop transcript is read back at the cached rate (0.1x)
    #   instead of full input price each turn. Without this only the static
    #   system/profile prefix was cached and input cost grew quadratically
    #   with turn count (83% of all apply spend, July 2026 audit).
    # - usage.include: OpenRouter returns its actually-billed cost per call
    #   (usage.cost, USD) — recorded to usage_events as ground truth against
    #   our computed pricing.
    # - provider pinning (Anthropic models only): the prompt cache lives at
    #   the upstream provider; letting OpenRouter route consecutive turns to
    #   different upstreams (Anthropic vs Vertex/Bedrock) forfeits every hit.
    extra_body: dict[str, Any] = {
        "cache_control": {"type": "ephemeral"},
        "usage": {"include": True},
    }
    if model.startswith("anthropic/"):
        extra_body["provider"] = {"order": ["anthropic"]}
    elif "kimi" in model.lower() or model.startswith("moonshotai/"):
        # Kimi's default reasoning mode is slower and unnecessary for the
        # tightly-scoped browser loop. Tool use remains available.
        extra_body["reasoning"] = {"enabled": False}

    final_status: str | None = None
    final_note: str = ""
    turns_done: int = 0
    tool_call_counts: dict[str, int] = {}
    nudged = False
    submit_confirmed_turn: int | None = None
    failed_action_counts: dict[str, int] = {}
    event_sequence = 0
    preflight_required = False

    def _emit(
        event_type: str,
        *,
        turn: int | None = None,
        name: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Best-effort sanitized event stream; tracing must never break apply."""
        nonlocal event_sequence
        if on_event is None:
            return
        event_sequence += 1
        try:
            on_event({
                "sequence": event_sequence,
                "turn": turn,
                "event_type": event_type,
                "name": name,
                "payload": payload or {},
            })
        except Exception as exc:  # noqa: BLE001
            print(
                f"⚠ apply trace persist failed ({type(exc).__name__}: "
                f"{str(exc)[:160]}); continuing",
                file=sys.stderr,
            )

    def _result() -> dict[str, Any]:
        # browser_mb: we don't currently meter Bright Data bandwidth per-call.
        # 1.5 MB is the observed average on Greenhouse/Lever forms — pricing.py.
        return {
            "status": final_status or "failed",
            "note": final_note,
            "tokens": tokens,
            "actual_cost_usd": actual_cost_usd,
            "browser_mb": 1.5 if (browser_provider or os.environ.get(
                "APPLYD_BROWSER_PROVIDER", "local"
            )).lower() == "brightdata" else 0.0,
            "turn_count": turns_done,
            "tool_call_counts": tool_call_counts,
        }

    print(
        f"→ direct-apply [{company}] {title} ({job_id})\n"
        f"  test_mode={test_mode} model={model}",
        file=sys.stderr,
    )
    _emit(
        "run_started",
        payload={
            "job_id": job_id,
            "company": company,
            "title": title,
            "model": model,
            "test_mode": bool(test_mode),
            "browser_provider": (
                browser_provider or os.environ.get("APPLYD_BROWSER_PROVIDER", "local")
            ).lower(),
            "max_cost_usd": max_cost_usd,
        },
    )

    # Email-verification context for submit(): if IMAP creds are configured, a
    # low-score submit that hits Greenhouse's security-code wall is completed
    # in-session (read the emailed code, enter it, resubmit). Unconfigured →
    # code_reader is None → submit reports gated:email_verification (no false
    # 'applied'). See apply/email_verify.py.
    verify_ctx = {
        "company": company,
        "code_reader": build_code_reader(),
        "browser_provider": (
            browser_provider or os.environ.get("APPLYD_BROWSER_PROVIDER", "local")
        ).lower(),
    }

    watchdog_disarm = _start_hard_watchdog(
        MAX_WALL_SECONDS + WATCHDOG_GRACE_SECONDS, job_id
    )
    try:
        with _wall_clock_deadline(MAX_WALL_SECONDS), browser_page(
            browser_provider, test_mode=bool(test_mode)
        ) as page:
            for turn in range(MAX_TURNS):
                if max_cost_usd > 0 and actual_cost_usd >= max_cost_usd:
                    final_status = "review"
                    final_note = (
                        f"review:cost_budget | spent=${actual_cost_usd:.4f} "
                        f"limit=${max_cost_usd:.4f}"
                    )
                    break
                # Force a deterministic opener:
                #   turn 0 = navigate (must hit the URL first)
                #   turn 1 = snapshot (must inspect before any click/fill)
                # Tried forcing turn 2 = fill_many but it back-fired on Ashby;
                # see git history for context.
                if turn == 0:
                    tool_choice: Any = {"type": "function", "function": {"name": "navigate"}}
                elif turn == 1:
                    tool_choice = {"type": "function", "function": {"name": "snapshot"}}
                elif nudged:
                    # Just nudged after a prose stall — force a tool call.
                    tool_choice = "required"
                else:
                    tool_choice = "auto"

                resp = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=TOOL_DEFS,
                    tool_choice=tool_choice,
                    max_tokens=4096,
                    extra_body=extra_body,
                )

                _accumulate_tokens(tokens, resp.usage)
                actual_cost_usd += getattr(resp.usage, "cost", 0.0) or 0.0
                turns_done = turn + 1

                asst = resp.choices[0].message
                # Append the assistant turn back to history. Some upstreams
                # return null content; OpenAI SDK accepts dicts or model objects.
                messages.append(asst.model_dump(exclude_none=True))

                tool_calls = asst.tool_calls or []
                _emit(
                    "model_turn",
                    turn=turn,
                    payload={
                        "tool_names": [tc.function.name for tc in tool_calls],
                        "has_text": bool(asst.content),
                        "cumulative_cost_usd": actual_cost_usd,
                        "tokens": dict(tokens),
                    },
                )
                if not tool_calls:
                    # Model gave a text answer instead of calling a tool. Haiku
                    # does this under pressure (e.g. thinking aloud about a
                    # truncated snapshot, 2026-07-10 — killed a run that was 80%
                    # done). Nudge once with tool_choice forced; only a second
                    # stall aborts the run.
                    if not nudged:
                        nudged = True
                        messages.append({
                            "role": "user",
                            "content": (
                                "Do not reply with prose. Continue the task via "
                                "tool calls only; when finished (or skipping), "
                                "call report_done."
                            ),
                        })
                        continue
                    final_status = "failed"
                    final_note = f"agent stopped without tool call: {(asst.content or '')[:200]}"
                    break
                nudged = False

                done = False
                opened_dropdown = False
                ref_barrier: str | None = None
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
                    _emit(
                        "tool_call",
                        turn=turn,
                        name=name,
                        payload={"args": _sanitize_tool_args(name, args)},
                    )

                    if name == "preflight":
                        reported_gaps = [
                            gap for gap in args.get("missing_fields", [])
                            if isinstance(gap, dict) and str(gap.get("label", "")).strip()
                        ]
                        overridden = [
                            gap for gap in reported_gaps
                            if _profile_already_answers(
                                str(gap.get("label", "")), profile_context,
                                job_locations=job_locations,
                                resume_text=resume_tex,
                                company=company,
                            )
                        ]
                        gaps = [gap for gap in reported_gaps if gap not in overridden]
                        if overridden:
                            _emit(
                                "preflight_override",
                                turn=turn,
                                name="preflight",
                                payload={
                                    "labels": [
                                        str(gap.get("label", ""))[:500]
                                        for gap in overridden
                                    ]
                                },
                            )
                        if gaps:
                            for gap in gaps:
                                _emit(
                                    "profile_gap",
                                    turn=turn,
                                    name="preflight",
                                    payload={
                                        "job_id": job_id,
                                        "company": company,
                                        "label": str(gap.get("label", ""))[:500],
                                        "category": str(gap.get("category", "other_fact"))[:80],
                                        "reason": str(gap.get("reason", ""))[:500],
                                    },
                                )
                            labels = "; ".join(str(gap["label"]) for gap in gaps[:5])
                            result = f"ok: preflight found missing required facts: {labels}"
                            messages.append({
                                "role": "tool", "tool_call_id": tc.id, "content": result
                            })
                            _emit(
                                "tool_result", turn=turn, name=name,
                                payload={"result": result},
                            )
                            final_status = "review"
                            final_note = f"review:missing_info | fields={labels}"
                            done = True
                            break
                        preflight_required = False
                        override_note = ""
                        if overridden:
                            override_note = (
                                " The structured profile already answers these labels; "
                                "re-read it and use its exact values: "
                                + "; ".join(
                                    str(gap.get("label", "")) for gap in overridden
                                )
                                + "."
                            )
                        result = (
                            "ok: preflight complete; all visible required questions "
                            "are answerable. You may fill this form step." + override_note
                        )
                        messages.append({
                            "role": "tool", "tool_call_id": tc.id, "content": result
                        })
                        _emit(
                            "tool_result", turn=turn, name=name,
                            payload={"result": result},
                        )
                        continue

                    if preflight_required and name not in {"snapshot", "report_done"}:
                        result = (
                            f"error: skipped {name}; call preflight first and inspect "
                            "every visible required question before changing the form"
                        )
                        messages.append({
                            "role": "tool", "tool_call_id": tc.id, "content": result
                        })
                        _emit(
                            "tool_result", turn=turn, name=name,
                            payload={"result": result},
                        )
                        continue

                    if ref_barrier is not None:
                        result = (
                            f"error: skipped {name}; {ref_barrier} changed or reserved "
                            "DOM refs. Continue in the next turn using the refs returned "
                            "by that tool."
                        )
                        messages.append({
                            "role": "tool", "tool_call_id": tc.id, "content": result
                        })
                        _emit(
                            "tool_result", turn=turn, name=name,
                            payload={"result": result},
                        )
                        continue

                    if name == "open_dropdown":
                        if opened_dropdown:
                            result = (
                                "error: open one dropdown per turn; opening another "
                                "would invalidate the first dropdown's option refs"
                            )
                            messages.append({
                                "role": "tool", "tool_call_id": tc.id, "content": result
                            })
                            _emit(
                                "tool_result", turn=turn, name=name,
                                payload={"result": result},
                            )
                            continue
                        opened_dropdown = True

                    if name == "report_done":
                        requested_status = args.get("status", "failed")
                        # The agent must observe submit's result before it may
                        # report applied. This rejects a single assistant turn
                        # that batches submit + report_done and blindly assumes
                        # success.
                        if requested_status == "applied" and (
                            submit_confirmed_turn is None or submit_confirmed_turn >= turn
                        ):
                            result = (
                                "error: cannot report applied until a prior turn's "
                                "submit tool returned a confirmed result"
                            )
                            messages.append({
                                "role": "tool", "tool_call_id": tc.id, "content": result
                            })
                            continue
                        final_note = args.get("note", "")
                        final_status = _normalize_report_status(
                            requested_status, final_note
                        )
                        messages.append({"role": "tool", "tool_call_id": tc.id, "content": "ok: recorded"})
                        _emit(
                            "tool_result", turn=turn, name=name,
                            payload={"result": "ok: recorded"},
                        )
                        done = True
                        continue

                    result = dispatch(
                        page,
                        name,
                        args,
                        test_mode=test_mode,
                        verify_ctx=verify_ctx,
                        job_url=job_url,
                        resume_pdf_path=resume_pdf_path,
                        profile=profile_context,
                        company=company,
                        title=title,
                        resume_text=resume_tex,
                        job_locations=job_locations,
                    )
                    if not result.startswith("error:") and name == "open_dropdown":
                        # The option refs must be consumed on the next model
                        # turn, after the model has actually seen them. Other
                        # mutations in this response could invalidate o* refs.
                        ref_barrier = "open_dropdown"
                    if not result.startswith("error:") and name == "snapshot":
                        preflight_required = True
                    elif result.startswith("ok:") and name in {
                        "upload_resume", "upload_cover_letter"
                    }:
                        # Upload tools return a newly minted canonical snapshot.
                        ref_barrier = name
                    if name == "submit" and (
                        result.startswith("ok: submission_confirmed")
                        or result.startswith("ok: test_mode=true")
                    ):
                        submit_confirmed_turn = turn
                    if name == "submit" and result.startswith("error:"):
                        # dispatch appends a fresh snapshot after a failed
                        # submit, which may expose conditional required fields.
                        preflight_required = True
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
                    _emit(
                        "tool_result",
                        turn=turn,
                        name=name,
                        payload={"result": _sanitize_tool_result(result)},
                    )
                    terminal_tool_verdict = _terminal_tool_verdict(name, result)
                    if terminal_tool_verdict is not None:
                        # Captcha exhaustion is a human-review boundary, not a
                        # question for another paid model turn. Ending here also
                        # prevents automatic retries from hammering the ATS.
                        final_status, final_note = terminal_tool_verdict
                        done = True
                        break
                    signature = _tool_signature(name, args)
                    if result.startswith("error:"):
                        failed_action_counts[signature] = (
                            failed_action_counts.get(signature, 0) + 1
                        )
                        if failed_action_counts[signature] >= 2:
                            final_status = "review"
                            final_note = f"review:repeated_tool_failure | tool={name}"
                            done = True
                            break
                    if (
                        name == "submit"
                        and test_mode
                        and result.startswith("ok: test_mode=true")
                    ):
                        # Stop at the review boundary. Asking the model to
                        # interpret an intentionally unchanged form caused
                        # false CAPTCHA diagnoses and needless extra clicks.
                        final_status = "review"
                        final_note = "test_mode:ready_for_review"
                        done = True
                        break

                if done:
                    # Verdict is final — hand it to the caller for persistence
                    # while the browser is still open. A persist error must not
                    # crash a finished run: fall through and let the caller's
                    # post-return path retry the write.
                    if on_verdict is not None:
                        try:
                            on_verdict(_result())
                        except Exception as e:  # noqa: BLE001
                            print(
                                f"⚠ on_verdict persist failed ({type(e).__name__}: "
                                f"{str(e)[:200]}); will persist after browser close",
                                file=sys.stderr,
                            )
                    break
            else:
                final_status = "failed"
                final_note = f"hit MAX_TURNS={MAX_TURNS} without report_done"
    except Exception as e:
        # Transient infra (no credits, rate limit, provider outage, Bright Data
        # refusing the CDP connect) is not this job's fault — surface a distinct
        # status so the caller requeues instead of burning the application to
        # terminal 'failed'. BrowserConnectError fires before any LLM spend.
        transient = is_transient_llm_error(e) or isinstance(e, BrowserConnectError)
        final_status = "infra_error" if transient else "failed"
        final_note = f"runner exception: {type(e).__name__}: {str(e)[:200]}"
        print(f"✗ runner exception: {type(e).__name__}: {e}", file=sys.stderr)
    finally:
        watchdog_disarm.set()

    _emit(
        "run_finished",
        payload={
            "status": final_status or "failed",
            "note": final_note,
            "cost_usd": actual_cost_usd,
            "turn_count": turns_done,
            "tool_calls": dict(tool_call_counts),
        },
    )

    print(
        f"✓ direct-apply done: status={final_status} note={final_note!r}\n"
        f"  tokens: input={tokens['input']} output={tokens['output']} "
        f"cache_read={tokens['cache_read']} cache_write={tokens['cache_write']}",
        file=sys.stderr,
    )
    return _result()


def _accumulate_tokens(tokens: dict[str, int], usage: Any) -> None:
    """Best-effort token accumulation across providers. Field names vary."""
    if usage is None:
        return
    tokens["input"] += getattr(usage, "prompt_tokens", 0) or 0
    tokens["output"] += getattr(usage, "completion_tokens", 0) or 0
    # OpenAI / OpenRouter standard: prompt_tokens_details.cached_tokens (read)
    # and prompt_tokens_details.cache_write_tokens (write, OpenRouter name).
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        cached = getattr(details, "cached_tokens", 0) or 0
        written = getattr(details, "cache_write_tokens", 0) or 0
        tokens["cache_read"] += cached
        tokens["cache_write"] += written
        # OpenRouter's prompt_tokens is the TOTAL input (cached reads and cache
        # writes included). Subtract both so "input" is only full-price tokens.
        tokens["input"] -= cached + written
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


def _tool_signature(name: str, args: dict[str, Any]) -> str:
    return f"{name}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}"


def _normalize_report_status(status: str, note: str) -> str:
    """Human-solvable gates stay reviewable instead of becoming terminal."""
    if note.strip().lower().startswith(("gated:captcha", "gated:email_verification")):
        return "review"
    return status


def _terminal_tool_verdict(name: str, result: str) -> tuple[str, str] | None:
    if name != "submit":
        return None
    if "captcha did not resolve within" in result:
        return "review", "gated:captcha"
    if "EMAIL VERIFICATION WALL" in result:
        return "review", "gated:email_verification"
    return None


def _profile_already_answers(
    label: str,
    profile: dict[str, Any],
    *,
    job_locations: list[str] | None = None,
    resume_text: str = "",
    company: str = "",
) -> bool:
    """Reject false model gaps only when the structured profile has the fact."""
    text = " ".join(re.sub(r"[^a-z0-9]+", " ", label.casefold()).split())
    country_code = str(profile.get("address_country_code", "")).upper()
    country_name = str(profile.get("address_country", "")).casefold()
    if (
        ("if located in the us" in text or "if located in the united states" in text)
        and country_code not in {"", "US"}
        and country_name not in {"", "united states", "united states of america"}
    ):
        return True

    background = profile.get("background_defaults") or {}
    if any(phrase in text for phrase in (
        "immediate family", "family member", "member of your family", "relative",
    )):
        if "government" in text and background.get("no_immediate_family_in_government") is True:
            return True
        if background.get("no_immediate_family_at_target_company") is True:
            return True

    employment_history_question = any(phrase in text for phrase in (
        "ever been employed", "previously employed", "worked for this company",
        "worked for us", "engaged as a contractor", "history with",
    ))
    if (
        employment_history_question
        and background.get("never_employed_by_target_unless_listed_on_resume") is True
        and company
    ):
        normalized_resume = " ".join(
            re.sub(r"[^a-z0-9]+", " ", resume_text.casefold()).split()
        )
        normalized_company = " ".join(
            re.sub(r"[^a-z0-9]+", " ", company.casefold()).split()
        )
        if normalized_company and normalized_company not in normalized_resume:
            return True

    if "security clearance" in text:
        clearance = (profile.get("security_clearance") or {}).get("US") or {}
        if any(phrase in text for phrase in ("held", "have you had", "in the past")):
            return isinstance(clearance.get("held"), bool)
        return isinstance(clearance.get("eligible"), bool)

    if "export control" in text:
        export_control = profile.get("export_control") or {}
        return (
            isinstance(export_control.get("us_person"), bool)
            and bool(export_control.get("classification"))
        )
    authorization = profile.get("work_authorization") or {}
    padded = f" {text} "
    region = None
    if any(token in padded for token in (
        " united states ", " u s ", " us ", " h 1b ", " h1b ",
    )):
        region = "US"
    elif "canada" in text or "canadian" in text:
        region = "CA"
    elif any(term in padded for term in (
        " uk ", " united kingdom ", " britain ", " british ",
    )):
        region = "UK"
    elif "european union" in text or " eu " in padded:
        region = "EU"
    auth_question = any(
        phrase in text for phrase in (
            "authorized to work", "authorised to work", "work authorization",
            "work authorisation", "immigration sponsorship", "visa sponsorship",
            "require sponsorship", "requires sponsorship", "legal right to work",
            "legally permitted to work", "work permit",
        )
    )
    if auth_question and region is None and job_locations:
        location = " | ".join(job_locations).casefold()
        us_states = (
            "AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|"
            "MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|"
            "SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC"
        )
        if (
            any(term in location for term in ("united states", "usa", "u.s."))
            or re.search(rf",\s*(?:{us_states})(?:\b|$)", location, re.I)
        ):
            region = "US"
        elif any(term in location for term in (
            "canada", "ontario", "toronto", "ottawa", "vancouver", "montreal",
        )) or re.search(r",\s*(?:ON|BC|AB|QC|NS|NB|MB|SK|PE|NL)(?:\b|$)", location, re.I):
            region = "CA"
        elif any(term in location for term in (
            "united kingdom", "england", "scotland", "wales", "london", " uk",
        )):
            region = "UK"
    if auth_question and region and isinstance(authorization.get(region), dict):
        record = authorization[region]
        if "sponsor" in text:
            return isinstance(record.get("requires_sponsorship"), bool)
        return isinstance(record.get("authorized"), bool)
    if auth_question and region is None:
        # Wording such as "the locations selected above" is still answerable
        # when the structured profile explicitly records authorization for its
        # supported regions. The agent can use the visible selection to choose
        # the matching record; it must not turn this into a profile gap.
        records = [record for record in authorization.values() if isinstance(record, dict)]
        if records and all(isinstance(record.get("authorized"), bool) for record in records):
            if "sponsor" in text:
                return all(
                    isinstance(record.get("requires_sponsorship"), bool)
                    for record in records
                )
            return True

    known_fields = (
        (("over 18", "at least 18"), "over_18"),
        (("veteran",), "veteran_status"),
        (("disability", "disabled"), "disability_status"),
        (("race", "ethnicity"), "ethnicity"),
        (("hispanic", "latino"), "hispanic_latino"),
        (("gender",), "gender"),
        (("gpa", "grade point"), "gpa"),
        (("salary", "compensation", "pay expectation"), "salary_expectation"),
        (("start date", "available to start"), "earliest_start_date"),
        (("contact your previous", "contact previous employer"), "previous_employers_may_be_contacted"),
        (("citizenship", "citizen of"), "citizenships"),
        (("how did you hear", "referral source", "source did you hear"), "referral_source"),
        (("location city", "current city", "city of residence"), "address_city"),
    )
    if any(
        any(phrase in text for phrase in phrases) and profile.get(key) is not None
        for phrases, key in known_fields
    ):
        return True

    if resume_text.strip() and any(phrase in text for phrase in (
        "current employer", "previous employer", "current or previous employer",
        "current job title", "previous job title", "current or previous job title",
    )):
        return True

    preferences = profile.get("employment_preferences") or {}
    preference_questions = (
        (("relocate", "relocation"), "willing_to_relocate"),
        (("work onsite", "work on site", "onsite work"), "willing_to_work_onsite"),
        (("hybrid",), "willing_to_work_hybrid"),
        (("work remote", "remote work"), "willing_to_work_remote"),
        (("travel",), "willing_to_travel"),
    )
    return any(
        any(phrase in text for phrase in phrases)
        and isinstance(preferences.get(key), bool)
        for phrases, key in preference_questions
    )


def _sanitize_tool_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Retain tool structure for replay/debugging without storing typed PII."""
    if name == "fill":
        return {"ref": args.get("ref"), "value_chars": len(str(args.get("value", "")))}
    if name == "fill_many":
        return {
            "fields": [
                {"ref": field.get("ref"), "value_chars": len(str(field.get("value", "")))}
                for field in args.get("fields", [])
                if isinstance(field, dict)
            ]
        }
    if name == "fill_autocomplete":
        return {"ref": args.get("ref"), "value_chars": len(str(args.get("value", "")))}
    if name in {"upload_resume", "upload_cover_letter"}:
        safe = {"ref": args.get("ref")}
        if name == "upload_cover_letter":
            safe["content_chars"] = len(str(args.get("content", "")))
        return safe
    return args


def _sanitize_tool_result(result: str) -> str:
    """Redact values surfaced by snapshots while preserving labels and refs."""
    lines = []
    for line in result.splitlines():
        if " value=" in line:
            line = line.split(" value=", 1)[0] + " value='<redacted>'"
        lines.append(line)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Compatibility entry point for the local SQLite apply command."""
    load_env()

    p = argparse.ArgumentParser(prog="python -m applyd.apply.runner")
    p.add_argument("job_id", help="job id (from `applyd jobs`)")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--test-mode", choices=["true", "false"], default="true")
    p.add_argument("--browser", choices=["local", "brightdata"], default="local")
    p.add_argument("--max-cost-usd", type=float, default=None)
    p.add_argument("--liveness-ttl-minutes", type=int, default=15)
    args = p.parse_args(argv)
    from ..commands.apply import cmd_apply

    args.test_mode = args.test_mode == "true"
    return cmd_apply(args)


if __name__ == "__main__":
    sys.exit(main())
