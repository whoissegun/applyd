"""Direct apply runner: tool-use loop driving a persistent local Chrome profile.

Jev owns bounded decisions by default; Kimi K2.6 handles navigation and
grounded free-form writing. The restricted pure-Jev controller remains a
separate test surface.

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
import time
from contextlib import contextmanager
from typing import Any, Callable

from openai import OpenAI

from ..config import load_env
from ..llm_errors import is_transient_llm_error
from .browser import BrowserConnectError, browser_page
from .email_verify import build_code_reader
from .prompts import SYSTEM_PROMPT, build_user_blocks
from .tools import (
    TOOL_DEFS, _historical_application_answer, _historical_production_claim,
    _narrative_question, dispatch,
)


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
# OpenRouter has occasionally returned an HTTP-success response from Kimi with
# finish_reason="stop", no content, no tool calls, no usage, and no charge even
# when a tool was explicitly forced. Retry only that provider-empty signature;
# ordinary prose stalls still follow the one-nudge agent policy below.
EMPTY_COMPLETION_RETRIES = int(
    os.environ.get("APPLYD_EMPTY_COMPLETION_RETRIES", "5")
)


_TEST_AUDIT_SENSITIVE_LABEL = re.compile(
    r"\b(?:first|last|full|legal|preferred|family|given|sur)\s*name\b|^name$|"
    r"\b(?:e-?mail|phone|mobile|telephone|street address|address line|postal|zip|"
    r"linkedin|github|portfolio|website|date of birth|birth date|gender|sex|race|"
    r"ethnicity|hispanic|disability|veteran)\b",
    re.I,
)


def _test_mode_answer_audit(page: Any) -> list[dict[str, str]]:
    """Capture non-PII answers locally before a test or real submission."""
    rows = page.evaluate(r"""() => {
      const visible = e => { const r=e.getBoundingClientRect(); const s=getComputedStyle(e);
        return (r.width || r.height) && s.display !== 'none' && s.visibility !== 'hidden'; };
      const out=[];
      for (const e of document.querySelectorAll('[data-applyd-ref]')) {
        if (!visible(e)) continue;
        const tag=e.tagName.toLowerCase(), type=(e.type || '').toLowerCase();
        const question=(e.getAttribute('data-applyd-question') ||
          e.getAttribute('data-applyd-label') || '').trim();
        const option=(e.getAttribute('data-applyd-option') || '').trim();
        let value='';
        if (tag === 'select') value=e.selectedOptions?.[0]?.text?.trim() || '';
        else if (type === 'radio' || type === 'checkbox') {
          if (e.checked || e.getAttribute('aria-checked') === 'true' ||
              e.getAttribute('data-applyd-picked')) value=option || e.value || 'selected';
        } else if (tag === 'button' || e.getAttribute('role') === 'radio' ||
                   e.getAttribute('role') === 'checkbox') {
          if (e.getAttribute('aria-checked') === 'true' || e.getAttribute('aria-pressed') === 'true' || e.getAttribute('data-applyd-picked'))
            value=option || e.textContent?.trim() || 'selected';
        } else if (type !== 'file') value=(e.getAttribute('data-applyd-picked') || e.value || '').trim();
        if (question && value) out.push({question, value});
      }
      return out;
    }""")
    if not isinstance(rows, list):
        return []
    answers: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        question = str(row.get("question") or "").strip()
        value = str(row.get("value") or "").strip()
        if not question or not value or _TEST_AUDIT_SENSITIVE_LABEL.search(question):
            continue
        identity = (question, value)
        if identity in seen:
            continue
        seen.add(identity)
        answers.append({"question": question[:500], "value": value[:4000]})
    return answers


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


def _empty_zero_usage_completion(response: Any) -> bool:
    """Return true for the observed unbilled OpenRouter/Kimi empty response."""
    choices = getattr(response, "choices", None) or []
    if not choices:
        return False
    message = getattr(choices[0], "message", None)
    if message is None or (getattr(message, "tool_calls", None) or []):
        return False
    content = getattr(message, "content", None)
    if isinstance(content, str):
        if content.strip():
            return False
    elif content:
        return False
    usage = getattr(response, "usage", None)
    if usage is None:
        return True
    return not any(
        (getattr(usage, field, 0) or 0) > 0
        for field in ("prompt_tokens", "completion_tokens", "total_tokens", "cost")
    )


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

    controller_enabled = os.environ.get('APPLYD_JEV_CONTROLLER', 'false').lower() == 'true'
    # Closed-form application answers are always routed through Jev. Prefer a
    # direct TypeSafe key, but use the already-configured OpenRouter account
    # when no separate TypeSafe credential exists.
    assisted_enabled = not controller_enabled
    expected_schema = None
    if controller_enabled:
        from .jev_validation import application_url, validate_manifest
        job_url = application_url(job_url)
    # Live Jev is a per-job canary, never a blind --test-mode false override.
    if controller_enabled and not test_mode:
        try:
            from pathlib import Path
            record=json.loads(Path(os.environ.get('APPLYD_JEV_VALIDATION','')).read_text())
            expected_schema=validate_manifest(record,job_id,profile_context,resume_pdf_path,job_url)
        except (OSError,ValueError):
            expected_schema=None
        if expected_schema is None:
            return {'status': 'review', 'note': 'review:jev_controller | fresh matching test-mode validation required',
                    'tokens': {}, 'actual_cost_usd': 0.0, 'browser_mb': 0.0,
                    'turn_count': 0, 'tool_call_counts': {}}
    controller = None
    if controller_enabled:
        from .jev_controller import JevController
        controller = JevController(os.environ.get('TYPESAFE_API_KEY'),expected_schema)
    client = None if controller_enabled else _make_client()
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
        # DigitalOcean repeatedly returned unbilled empty completions for
        # forced tool calls while the same request succeeded through another
        # Kimi endpoint. Keep fallbacks enabled and require support for the
        # tool parameters this loop depends on.
        ignored = [
            provider.strip().casefold()
            for provider in os.environ.get(
                "APPLYD_KIMI_IGNORE_PROVIDERS", "digitalocean"
            ).split(",")
            if provider.strip()
        ]
        extra_body["provider"] = {
            "ignore": ignored,
            "allow_fallbacks": True,
            "require_parameters": True,
        }

    final_status: str | None = None
    final_note: str = ""
    turns_done: int = 0
    tool_call_counts: dict[str, int] = {}
    nudged = False
    submit_confirmed_turn: int | None = None
    submit_confirmation_note = ""
    resume_uploaded = False
    # Refs the deterministic/Jev layer answered in this attempt.
    controller_refs: set[str] = set()
    failed_action_counts: dict[str, int] = {}
    event_sequence = 0
    preflight_required = False
    preflight_violations = 0
    jev_planner = None
    jev_pending = False
    if assisted_enabled:
        from .jev import JevPlanner
        typesafe_key = os.environ.get('TYPESAFE_API_KEY')
        openrouter_key = os.environ.get('OPENROUTER_API_KEY')
        jev_planner = JevPlanner(
            typesafe_key or openrouter_key,
            max_requests=25,
            use_openrouter=not bool(typesafe_key),
        )

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
            "jev_estimated_cost_usd": controller.cost if controller else (jev_planner.cost if jev_planner else 0.0),
            "jev_validated_schema": controller.validated_schema if controller else None,
            "browser_mb": 1.5 if (browser_provider or os.environ.get(
                "APPLYD_BROWSER_PROVIDER", "local"
            )).lower() == "brightdata" else 0.0,
            "turn_count": turns_done,
            "tool_call_counts": tool_call_counts,
        }

    print(
        f"→ direct-apply [{company}] {title} ({job_id})\n"
        f"  test_mode={test_mode} model={'jev-1.13.0/controller' if controller else model}",
        file=sys.stderr,
    )
    _emit(
        "run_started",
        payload={
            "job_id": job_id,
            "company": company,
            "title": title,
            "model": 'jev-1.13.0/controller' if controller else model,
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
                budget_cost = actual_cost_usd + (controller.cost if controller else (jev_planner.cost if jev_planner else 0))
                if max_cost_usd > 0 and budget_cost >= max_cost_usd:
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
                elif preflight_required:
                    tool_choice = {"type": "function", "function": {"name": "preflight"}}
                elif nudged:
                    # Just nudged after a prose stall — force a tool call.
                    tool_choice = "required"
                else:
                    tool_choice = "auto"

                empty_retries = 0
                hybrid_response = None
                if controller is not None:
                    hybrid_response, controller_metrics = controller.next(
                        page, profile_context, turn, preflight_required)
                    _emit('jev_controller_decision', turn=turn, payload=controller_metrics)
                if jev_pending and not preflight_required and jev_planner is not None:
                    from .jev import tool_response
                    jev_pending = False
                    actions, jev_metrics = jev_planner.propose(
                        page, profile_context, job_locations=job_locations,
                        company=company,
                    )
                    _emit('jev_decision', turn=turn, payload=jev_metrics)
                    if actions:
                        hybrid_response = tool_response(actions, turn)
                    elif assisted_enabled:
                        _emit('jev_fallback', turn=turn, payload={
                            'provider': 'kimi_freeform_only',
                            'reason': jev_metrics.get('reason', 'no_authorized_actions')})
                while True:
                    resp = hybrid_response or client.chat.completions.create(
                        model=model,
                        messages=messages,
                        tools=TOOL_DEFS,
                        tool_choice=tool_choice,
                        max_tokens=4096,
                        extra_body=extra_body,
                    )
                    forced_tool = tool_choice != "auto"
                    if not forced_tool or not _empty_zero_usage_completion(resp):
                        break
                    if empty_retries >= EMPTY_COMPLETION_RETRIES:
                        break
                    empty_retries += 1
                    choice = resp.choices[0]
                    _emit(
                        "model_retry",
                        turn=turn,
                        payload={
                            "reason": "empty_zero_usage_forced_tool_completion",
                            "retry": empty_retries,
                            "max_retries": EMPTY_COMPLETION_RETRIES,
                            "finish_reason": getattr(choice, "finish_reason", None),
                            "response_id": getattr(resp, "id", None),
                        },
                    )
                    print(
                        "  ↻ retrying empty zero-usage forced-tool response "
                        f"({empty_retries}/{EMPTY_COMPLETION_RETRIES})",
                        file=sys.stderr,
                    )
                    time.sleep(min(1.0 * (2 ** (empty_retries - 1)), 8.0))

                if (
                    tool_choice != "auto"
                    and _empty_zero_usage_completion(resp)
                    and empty_retries >= EMPTY_COMPLETION_RETRIES
                ):
                    turns_done = turn + 1
                    final_status = "infra_error"
                    final_note = (
                        "empty zero-usage forced-tool completion after "
                        f"{EMPTY_COMPLETION_RETRIES + 1} attempts"
                    )
                    _emit(
                        "model_turn",
                        turn=turn,
                        payload={
                            "tool_names": [],
                            "has_text": False,
                            "cumulative_cost_usd": actual_cost_usd,
                            "tokens": dict(tokens),
                            "empty_completion_retries_exhausted": True,
                        },
                    )
                    break

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
                        "provider": "jev_controller" if controller else ("jev_assisted" if hybrid_response else "kimi"),
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
                            and " ".join(re.sub(
                                r"[^a-z0-9]+", " ",
                                str(gap.get("label", "")).casefold(),
                            ).split()) not in {
                                "apply", "apply now", "apply for this job",
                                "continue", "next", "start application",
                            }
                        ]
                        overridden = [
                            gap for gap in reported_gaps
                            if _profile_already_answers(
                                str(gap.get("label", "")), profile_context,
                                job_locations=job_locations,
                                resume_text=resume_tex,
                                company=company,
                            )
                            or _visible_optional_gap(page, str(gap.get('label', '')))
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
                        preflight_violations = 0
                        jev_pending = jev_planner is not None
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
                        preflight_violations += 1
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
                        if preflight_violations >= 2:
                            final_status = "review"
                            final_note = "review:agent_stall | required preflight ignored twice"
                            done = True
                            break
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
                        requested_note = str(args.get("note", ""))
                        missing_labels = _missing_info_labels(requested_note)
                        if missing_labels and all(
                            _profile_already_answers(
                                label,
                                profile_context,
                                job_locations=job_locations,
                                resume_text=resume_tex,
                                company=company,
                            )
                            or _visible_optional_gap(page, label)
                            for label in missing_labels
                        ):
                            result = (
                                "error: cannot report missing_info; the structured "
                                "profile already answers: "
                                + "; ".join(missing_labels)
                                + ". Use the exact profile values and continue."
                            )
                            messages.append({
                                "role": "tool", "tool_call_id": tc.id, "content": result
                            })
                            _emit(
                                "tool_result", turn=turn, name=name,
                                payload={"result": result},
                            )
                            continue
                        # The agent must observe submit's result before it may
                        # report applied. This rejects a single assistant turn
                        # that batches submit + report_done and blindly assumes
                        # success.
                        if requested_status == "applied" and (
                            submit_confirmed_turn is None or submit_confirmed_turn >= turn
                            or (controller is not None and not controller.confirmation_observed)
                        ):
                            result = (
                                "error: cannot report applied until a prior turn's "
                                "submit tool returned a confirmed result"
                            )
                            messages.append({
                                "role": "tool", "tool_call_id": tc.id, "content": result
                            })
                            continue
                        if (
                            submit_confirmed_turn is not None
                            and submit_confirmed_turn < turn
                            and requested_status != "applied"
                            and (controller is None or controller.confirmation_observed)
                        ):
                            # Browser evidence outranks a contradictory model
                            # summary. This specifically covers Greenhouse's
                            # email-code path: submit can confirm after entering
                            # the code, while Kimi repeats the earlier wall label
                            # and incorrectly downgrades a completed application.
                            _emit(
                                "report_status_override",
                                turn=turn,
                                name=name,
                                payload={
                                    "requested_status": requested_status,
                                    "requested_note": requested_note[:500],
                                    "reason": "prior submit tool confirmed application",
                                },
                            )
                            requested_status = "applied"
                            requested_note = submit_confirmation_note or (
                                "application submission confirmed by runner"
                            )
                        final_note = requested_note
                        final_status = _normalize_report_status(
                            requested_status, final_note
                        )
                        messages.append({"role": "tool", "tool_call_id": tc.id, "content": "ok: recorded"})
                        _emit(
                            "tool_result", turn=turn, name=name,
                            payload={"result": "ok: recorded"},
                        )
                        done = True
                        # A verdict is a hard boundary. Kimi can emit several
                        # tool calls in one response; do not execute anything
                        # that happens to follow an accepted report_done.
                        break

                    if jev_planner is not None and hybrid_response is None:
                        from .jev import is_bounded_answer_action, is_optional_answer_action, is_satisfied_answer_action
                        if name in {'fill', 'fill_autocomplete'} and is_optional_answer_action(page, name, args):
                            result = 'skipped: optional field left unchanged; only required free-form fields may be composed.'
                            messages.append({'role':'tool','tool_call_id':tc.id,'content':result})
                            _emit('tool_result', turn=turn, name=name, payload={'result':result})
                            continue
                        if name == 'fill_many':
                            args = dict(args, fields=[field for field in args.get('fields', [])
                                if not is_optional_answer_action(page, 'fill', field)])
                        if is_bounded_answer_action(page, name, args):
                            action_refs = (args.get('refs') if name == 'click_many'
                                           else [args.get('ref')])
                            if name == 'pick_option':
                                # pick_option names an option (o*); its field is
                                # the combobox currently marked open.
                                try:
                                    open_ref = page.evaluate(
                                        "() => document.querySelector('[data-applyd-combobox-open]')"
                                        "?.getAttribute('data-applyd-ref') || ''")
                                except Exception:  # noqa: BLE001
                                    open_ref = ''
                                action_refs = [open_ref] if open_ref else []
                            if action_refs and all(ref in controller_refs for ref in action_refs):
                                # The deterministic layer already answered this
                                # field (e.g. the closest graduation option);
                                # Kimi may not overwrite it, and that is not a
                                # reason to abandon the application.
                                result = ('skipped: this question was already answered by the '
                                          'controller from the structured profile. The answer was '
                                          'not changed. Do not retry it; continue with other fields.')
                                messages.append({'role': 'tool', 'tool_call_id': tc.id, 'content': result})
                                _emit('tool_result', turn=turn, name=name, payload={'result': result})
                                continue
                            if is_satisfied_answer_action(page, name, args, jev_planner.decision_attempts):
                                result = ('skipped: these closed questions are optional or already answered '
                                          'by the controller. Existing answers were not changed. Do not retry them.')
                                messages.append({'role': 'tool', 'tool_call_id': tc.id, 'content': result})
                                _emit('tool_result', turn=turn, name=name, payload={'result': result})
                                continue
                            if is_optional_answer_action(page, name, args):
                                result = ('skipped: optional bounded question left unchanged. '
                                          'Do not retry it. Continue with required fields only.')
                                messages.append({'role': 'tool', 'tool_call_id': tc.id, 'content': result})
                                _emit('tool_result', turn=turn, name=name, payload={'result': result})
                                continue
                            result = (
                                f"error: {name} blocked; bounded application answers "
                                "are owned by Jev and cannot be selected by Kimi"
                            )
                            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
                            _emit("tool_result", turn=turn, name=name, payload={"result": result})
                            final_status = "review"
                            final_note = "review:jev_required | Kimi attempted a bounded answer"
                            done = True
                            break

                    if (name == "submit" and not resume_uploaded
                            and _required_resume_input_empty(page)):
                        result = (
                            "error: submit blocked; this form has a required resume "
                            "upload and upload_resume has not succeeded. Upload the "
                            "resume to its file input first."
                        )
                        messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
                        _emit("tool_result", turn=turn, name=name, payload={"result": result})
                        continue

                    if name == "submit":
                        _emit(
                            "test_answer_audit" if test_mode else "answer_audit",
                            turn=turn,
                            name=name,
                            payload={"answers": _test_mode_answer_audit(page)},
                        )

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
                    # Constrained-chooser calls happen inside tools/planner;
                    # count them toward this attempt's spend and budget.
                    from . import option_chooser
                    actual_cost_usd += option_chooser.drain_cost()
                    if name == "upload_resume" and result.startswith("ok:"):
                        resume_uploaded = True
                    if (hybrid_response is not None and result.startswith("ok:")
                            and name in {"select_option", "click", "click_many", "pick_option",
                                         "fill_autocomplete"}):
                        controller_refs.update(
                            ref for ref in (args.get("refs") if name == "click_many"
                                            else [args.get("ref")])
                            if isinstance(ref, str))
                    if controller is not None:
                        controller.observe(name, result)
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
                        if result.startswith("ok: submission_confirmed"):
                            submit_confirmation_note = result.removeprefix("ok: ")
                    if name == "submit" and result.startswith("error:"):
                        # dispatch appends a fresh snapshot after a failed
                        # submit, which may expose conditional required fields.
                        preflight_required = True
                    if (
                        jev_planner is not None
                        and result.startswith("ok:")
                        and name not in {"navigate", "snapshot", "preflight", "open_dropdown", "report_done"}
                    ):
                        # Re-enter Jev after every successful mutation. It gets
                        # first refusal on the next still-empty closed question;
                        # Kimi is used only when no bounded decision remains.
                        jev_pending = True
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
                            final_note = _repeated_failure_note(name, result)
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
                final_status, final_note = _max_turns_verdict(
                    submit_confirmed_turn, MAX_TURNS
                )
                if controller is not None and submit_confirmed_turn is not None and not controller.confirmation_observed:
                    final_status='review'
                    final_note='review:submission_confirmed_unobserved | do not resubmit'
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


_REQUIRED_RESUME_INPUT_JS = r"""() => [...document.querySelectorAll('input[type=file]')].some(el => {
    if (el.files && el.files.length) return false;
    const required = el.required || el.getAttribute('aria-required') === 'true'
        || /^\s*\*/.test(el.getAttribute('data-applyd-label') || '');
    if (!required) return false;
    const ids = (el.getAttribute('aria-labelledby') || '').trim().split(/\s+/).filter(Boolean);
    const identity = [el.id, el.name, el.getAttribute('data-ui'), el.getAttribute('aria-label'),
        el.getAttribute('data-applyd-label'),
        ...ids.map(id => document.getElementById(id)?.innerText || '')].join(' ');
    return /\b(resume|cv|curriculum vitae)\b/i.test(identity);
})"""


def _required_resume_input_empty(page: Any) -> bool:
    """True when a required resume file input is still empty.

    The runner owns resume upload, so submit must not proceed (even in test
    mode) until upload_resume has succeeded. Inspection failure fails open so
    this guard never blocks forms it cannot read.
    """
    try:
        return page.evaluate(_REQUIRED_RESUME_INPUT_JS) is True
    except Exception:  # noqa: BLE001
        return False


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


def _repeated_failure_note(name: str, result: str) -> str:
    if any(marker in result for marker in (
        "refused false", "date selection blocked", "refused invented",
        "refused unrecorded",
    )):
        return f"review:policy_guard | tool={name}"
    if any(marker in result for marker in (
        "no unambiguous match", "no selectable suggestion",
        "combobox requires", "no options rendered",
    )):
        return f"review:agent_stall | tool={name}"
    return f"review:repeated_tool_failure | tool={name}"


def _terminal_tool_verdict(name: str, result: str) -> tuple[str, str] | None:
    if "legal answer blocked:" in result:
        return "review", "review:missing_info | legal answer country or profile fact is unresolved"
    if result.startswith("error:") and (
        "TargetClosedError" in result
        or "Target page, context or browser has been closed" in result
    ):
        return "infra_error", "infra:browser_closed | submission unconfirmed; inspect before retry"
    if "is not in the structured profile; send to review" in result:
        return "review", "review:missing_info | consequential answer is not in profile"
    if name != "submit":
        return None
    if result.startswith('error:') and 'flagged as possible spam' in result.casefold():
        return 'review', 'gated:spam | employer rejected submission as possible spam; not confirmed; inspect before retry'
    if "captcha did not resolve within" in result:
        return "review", "gated:captcha"
    if "EMAIL VERIFICATION WALL" in result:
        return "review", "gated:email_verification"
    return None


def _max_turns_verdict(
    submit_confirmed_turn: int | None, max_turns: int,
) -> tuple[str, str]:
    if submit_confirmed_turn is not None:
        return (
            "review",
            "review:submission_confirmation_unacknowledged | "
            f"submit confirmed on final turn before MAX_TURNS={max_turns}",
        )
    return "failed", f"hit MAX_TURNS={max_turns} without report_done"


def _missing_info_labels(note: str) -> list[str]:
    if "missing_info" not in note.casefold():
        return []
    match = re.search(r"\bfields?\s*=\s*(.+)$", note, re.I)
    if not match:
        return []
    raw = match.group(1).strip().strip("[]")
    labels = []
    for value in raw.split(";"):
        label = value.strip().strip("'\"")
        if label:
            labels.append(label)
    return labels


def _visible_optional_gap(page: Any, label: str) -> bool:
    """Dismiss only a unique visible, positively optional DOM field."""
    result = page.evaluate(r"""label => {
      const norm=s=>s.toLowerCase().replace(/[^a-z0-9]+/g,' ').trim();
      const wanted=norm(label);
      if (!wanted) return false;
      const matches=[...document.querySelectorAll('[data-applyd-ref]')].filter(e=>{
        const r=e.getBoundingClientRect(), s=getComputedStyle(e);
        return (r.width || r.height) && s.display!=='none' && s.visibility!=='hidden'
          && ['INPUT','SELECT','TEXTAREA'].includes(e.tagName)
          && [e.getAttribute('data-applyd-question')||'',e.getAttribute('data-applyd-label')||'']
            .some(text=>norm(text)===wanted);
      });
      return matches.length===1 && matches[0].getAttribute('data-applyd-required')==='false'
        && !matches[0].required && matches[0].getAttribute('aria-required')!=='true';
    }""", label)
    return result is True


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
    from .deterministic_questions import required_consent_authorized, resolve_text_question
    # Identity/contact fields with an exact label mapping and a stored value
    # are never "missing" (Sirion: the model listed First/Last Name and Email).
    from .jev import FIELDS
    bare = re.sub(r"\s+(?:required|optional)$", "", text)
    for key, pattern in FIELDS.items():
        value = profile.get(key)
        if re.fullmatch(pattern, bare) and isinstance(value, str) and value.strip():
            return True
    if required_consent_authorized(label, profile, required=True):
        return True
    if ((profile.get('application_policy') or {}).get('required_narrative_invention') is True
            and _narrative_question(label)):
        return True
    deterministic = resolve_text_question(label, profile, company=company,
                                          job_locations=job_locations or (), required=True)
    if deterministic is not None and deterministic.predicate in {
        "prior_company", "github_url", "start_availability", "compensation",
        "role_country_residence", "residence_region",
        "job_search_motivation", "education_control", "work_authorized",
        "work_requires_sponsorship", "saved_exact_answer", "skill_experience",
        "general_software_experience", "nationality", "gpa_band", "visa_details",
        "commute_relocation", "work_permanent_right", "restrictive_covenants",
        "criminal_history", "essential_functions", "interview_accommodations",
        "accuracy_attestation", "required_consent", "notice_period", "relocation_timeline",
        "onsite_with_relocation", "work_status_category",
    }:
        return deterministic.value is not None
    # Work-eligibility labels ("Eligibility to work in the United Kingdom.")
    # are answered from the structured regional record once options render.
    if re.search(r"\b(?:eligib\w*|authori[sz]\w*|right to work|sponsor\w*)\b", text):
        from .deterministic_questions import _location_regions, _regions, _work_record
        regions = _regions(label) or _location_regions(profile, job_locations or [])
        if len(regions) == 1:
            record = _work_record(profile, regions[0]) or {}
            if isinstance(record.get("authorized"), bool) and isinstance(record.get("requires_sponsorship"), bool):
                return True

    if _historical_production_claim(text):
        return isinstance(_historical_application_answer(profile, text), bool)

    # The resume path is runner-bound. A model-generated generic attachment
    # gap is not a missing candidate fact, especially on Greenhouse pages that
    # expose several unlabeled optional `Attach` inputs.
    if text in {
        "resume", "resume cv", "cv", "file upload area", "file upload areas",
    } and resume_text.strip():
        return True

    # These prompts are explicitly delegated composition, not missing facts.
    # Manual artifacts and historical explanations remain grounded/human.
    if not any(phrase in text for phrase in (
        "record a video", "video response", "video introduction",
        "link to your video", "video link", "why did you leave",
        "reason for leaving",
    )) and any(phrase in text for phrase in (
        "why do you want to join", "why do you want to work",
        "why are you applying", "why do you want this role",
        "why this company", "why this role", "why us",
        "what interests you about", "what excites you about",
        "why are you interested in", "motivation for applying",
    )):
        return True
    country_code = str(profile.get("address_country_code", "")).upper()
    country_name = str(profile.get("address_country", "")).casefold()
    if (
        ("if located in the us" in text or "if located in the united states" in text)
        and country_code not in {"", "US"}
        and country_name not in {"", "united states", "united states of america"}
    ):
        return True

    background = profile.get("background_defaults") or {}
    recruitment_events = background.get("recruitment_events") or {}
    company_events = next((
        events for event_company, events in recruitment_events.items()
        if str(event_company).casefold() == company.casefold()
    ), None) if isinstance(recruitment_events, dict) and company else None
    if any(phrase in text for phrase in (
        "recruitment event", "event you attended", "which event",
    )) and isinstance(company_events, list):
        return True

    application_policy = profile.get("application_policy") or {}
    if (
        any(phrase in text for phrase in (
            "privacy notice", "privacy acknowledgement",
            "privacy acknowledgment", "applicant privacy",
        ))
        and application_policy.get("ordinary_accuracy_attestation") == "authorized"
    ):
        return True
    generic_recruiting_consent = (
        any(phrase in text for phrase in (
            "recruitment marketing", "recruiting marketing", "talent network",
            "future job opportunities", "training opportunities", "job alerts",
        ))
        and any(term in text for term in (
            "communication", "contact me", "receive information", "receive updates",
        ))
    )
    if generic_recruiting_consent:
        return isinstance(
            application_policy.get("recruiting_communications_consent"), bool
        )
    if (
        any(term in text for term in ("sms", "whatsapp", "text message"))
        and any(term in text for term in ("communication", "message", "contact"))
    ):
        return isinstance(application_policy.get("sms_recruiting_consent"), bool)

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
            "require sponsorship", "requires sponsorship", "sponsor",
            "legal right to work",
            "right to work", "legally permitted to work", "work permit", "require a visa",
            "requires a visa", "visa for employment",
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

    if text in {"name", "full name", "legal name"}:
        return bool(
            str(profile.get("full_name", "")).strip()
            or (
                str(profile.get("first_name", "")).strip()
                and str(profile.get("last_name", "")).strip()
            )
        )

    if text in {"phone", "phone number", "mobile", "mobile number"}:
        return bool(str(profile.get("phone", "")).strip())

    if text == "current location":
        return any(
            str(profile.get(key, "")).strip()
            for key in ("address_city", "address_region", "address_country")
        )

    residence_boolean = (
        any(phrase in text for phrase in (
            "currently living", "currently reside", "currently based",
            "do you live", "do you reside", "are you located",
        ))
        and any(term in text for term in (
            " area", " city", " state", " province", " country", "office",
            "london", "seoul", "new york", "san francisco", "canada",
            "united states", "united kingdom",
        ))
    )
    if residence_boolean and all(
            str(profile.get(key, "")).strip()
            for key in ("address_city", "address_region", "address_country")
        ):
        return True

    # Boolean education-status questions are answerable from the combination
    # of the grounded degree and graduation date. Do not confuse this with a
    # preference question merely mentioning a degree (for example, interest
    # in pursuing a Master's), which must remain a real profile gap.
    education_status_question = (
        "degree" in text
        and any(term in text for term in (
            "completed", "complete", "pursuing", "enrolled",
            "graduated", "graduate", "attending",
        ))
        and any(term in text for term in (
            "are you", "have you", "does this apply", "do you",
        ))
        and "interested" not in text
    )
    if education_status_question:
        return bool(profile.get("degree") and profile.get("expected_grad_date"))
    if text in {"degree", "degree type"}:
        return bool(profile.get("degree"))
    if any(phrase in text for phrase in (
        "does your degree focus", "is your degree focused",
        "degree concentration", "degree specialization",
    )):
        return isinstance(profile.get("degree_focus_areas"), list)

    # A school name does not ground academic rank or subject performance.
    # Canonical's form uses labels such as "How did you perform in
    # mathematics at high school?"; broad matching on the word "school"
    # previously overrode a legitimate profile gap.
    if "high school" in text and any(term in text for term in (
        "perform", "performance", "rank", "ranking", "score",
    )):
        performance = profile.get("high_school_performance")
        return isinstance(performance, dict) and bool(performance)

    # Knowing which languages a candidate speaks does not establish a CEFR
    # level, native status, fluency, or bilingualism.  Keep proficiency forms
    # in review unless the profile records that separate consequential fact.
    language_proficiency_question = (
        any(term in text for term in ("language", "english", "french", "spanish"))
        and any(term in text for term in (
            "proficiency", "fluency", "fluent", "conversational", "cefr",
        ))
    )
    if 'native language' in text:
        return bool(profile.get('native_language'))
    if language_proficiency_question:
        proficiency = profile.get("language_proficiency")
        if not isinstance(proficiency, dict) or not proficiency:
            return False
        named = [
            value for language, value in proficiency.items()
            if str(language).casefold() in text and str(value or "").strip()
        ]
        return bool(named) or (
            len(proficiency) == 1
            and any(str(value or "").strip() for value in proficiency.values())
        )

    known_fields = (
        (("linkedin profile", "linkedin url"), "linkedin_url"),
        (("preferred first name", "preferred name", "nickname"), "first_name"),
        (("name pronunciation", "name pronounciation", "phonetic spelling"), "name_pronunciation"),
        (("education", "school", "university", "institution"), "school"),
        # A structured degree answers degree-type controls, not every sentence
        # containing the word "degree". Broad substring matching previously
        # hid genuinely unknown preference questions such as interest in a
        # Master's program during preflight.
        ((
            "which degree", "what degree", "select your degree",
            "highest degree", "highest level of education",
            "level of education", "type of degree",
        ), "degree"),
        (("major", "discipline", "field of study", "degree subject"), "major"),
        (("over 18", "at least 18"), "over_18"),
        (("veteran",), "veteran_status"),
        (("disability", "disabled"), "disability_status"),
        (("race", "ethnicity"), "ethnicity"),
        (("hispanic", "latino"), "hispanic_latino"),
        (("gender",), "gender"),
        (("gpa", "grade point"), "gpa"),
        ((
            "salary", "compensation", "pay expectation", "pay rate",
            "hourly rate", "expected rate", "desired rate",
        ), "salary_expectation"),
        ((
            "start date", "available to start", "when are you available",
            "when would you be available", "availability date",
            "when can you start a new role",
        ), "earliest_start_date"),
        (("contact your previous", "contact previous employer"), "previous_employers_may_be_contacted"),
        (("citizenship", "citizen"), "citizenships"),
        (("language", "languages do you speak"), "spoken_languages"),
        (("how did you hear", "referral source", "source did you hear"), "referral_source"),
        (("street address", "address line 1", "mailing address"), "address_line1"),
        (("location city", "current city", "city of residence"), "address_city"),
        (("current residence", "country of residence", "current country"), "address_country"),
        (("state are you located", "state province", "province territory"), "address_region"),
        (("postal code", "zip code", "zip postal"), "postal_code"),
        (("graduation date", "graduate date", "expected graduation"), "expected_grad_date"),
        (("current company",), "current_company"),
    )
    if any(
        any(phrase in text for phrase in phrases) and profile.get(key) is not None
        for phrases, key in known_fields
    ):
        return True

    if any(phrase in text for phrase in (
        "are you located in", "are you based in", "do you live in",
        "do you reside in", "currently reside in",
    )) and any(profile.get(key) for key in (
        "address_city", "address_region", "address_country",
    )):
        return True

    if resume_text.strip() and any(phrase in text for phrase in (
        "current employer", "current company", "previous employer", "current or previous employer",
        "current job title", "previous job title", "current or previous job title",
    )):
        return True

    preferences = profile.get("employment_preferences") or {}
    if (
        any(phrase in text for phrase in (
            "what role are you applying for", "which role are you applying for",
            "which roles are you applying for", "which program are you applying for",
            "which programme are you applying for",
        ))
        and preferences.get("accept_any_role_option") is True
    ):
        return True
    preference_questions = (
        (("relocate", "relocation"), "willing_to_relocate"),
        ((
            "work onsite", "work on site", "onsite work", "work from our office",
            "work from the office", "in office", "in-office", "office days",
        ), "willing_to_work_onsite"),
        (("hybrid",), "willing_to_work_hybrid"),
        (("work remote", "remote work"), "willing_to_work_remote"),
        (("travel",), "willing_to_travel"),
        ((
            "interested in gaining a master", "interest in gaining a master",
            "interested in pursuing a master", "interest in pursuing a master",
        ), "willing_to_pursue_masters_while_working"),
    )
    if any(
        any(phrase in text for phrase in phrases)
        and isinstance(preferences.get(key), bool)
        for phrases, key in preference_questions
    ):
        return True
    return (
        preferences.get("willing_to_work_any_onsite_schedule") is True
        and any(phrase in text for phrase in (
            "days a week", "days per week", "office schedule", "onsite schedule",
        ))
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
