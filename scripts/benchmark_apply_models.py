"""Replay the apply-agent tool loop without opening or submitting a real form.

This is a model-selection benchmark, not a browser test. It uses applyd's real
system prompt, profile/resume context, and tool schemas, while returning fixed
ATS-like tool results. OpenRouter still charges for model tokens.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openai import OpenAI

from applyd.apply.prompts import SYSTEM_PROMPT, build_user_blocks
from applyd.apply.tools import TOOL_DEFS
from applyd.config import load_env


DEFAULT_MODELS = ("qwen/qwen3.5-9b", "moonshotai/kimi-k2.6")

STANDARD_FORM = """\
r0: [textbox/text *] 'First name'
r1: [textbox/text *] 'Last name'
r2: [textbox/email *] 'Email'
r3: [textbox/tel *] 'Phone'
r4: [combobox/text *] 'Current location (select a suggestion)'
r5: [input/file *] 'Resume/CV'
r6: [combobox *] 'Will you now or in the future require US visa sponsorship?'
r7: [combobox/text *] 'School (select a suggestion)'
r8: [textbox/url] 'LinkedIn profile (optional)'
r9: [textarea] 'Cover letter (optional)'
r10: [button/submit] 'Submit application'"""

SKIP_FORM = """\
r0: [textbox/text *] 'First name'
r1: [textbox/text *] 'Last name'
r2: [textbox/email *] 'Email'
r3: [input/file *] 'Resume/CV'
r4: [button/submit] 'Submit application'"""


@dataclass
class ReplayState:
    calls: list[dict[str, Any]] = field(default_factory=list)
    filled: dict[str, tuple[str, str]] = field(default_factory=dict)
    opened_dropdown_turn: int | None = None
    picked_option: str | None = None
    submitted: bool = False
    terminal: dict[str, Any] | None = None
    malformed_args: int = 0
    prose_stalls: int = 0
    same_turn_dropdown_pick: bool = False


def _profile() -> dict[str, Any]:
    profile = json.loads(Path("profile.json").read_text(encoding="utf-8"))
    # The checked-in legacy profile is stale. Benchmark the truthful policy.
    profile["work_authorization"]["US"] = {
        "authorized": False,
        "requires_sponsorship": True,
        "visa_status": None,
    }
    return profile


def _digits(value: str) -> str:
    return "".join(char for char in value if char.isdigit())


def _simulate_tool(
    *,
    name: str,
    args: dict[str, Any],
    turn: int,
    scenario: str,
    state: ReplayState,
) -> str:
    state.calls.append({"name": name, "args": args, "turn": turn})
    if name == "navigate":
        return "ok: loaded benchmark application"
    if name == "snapshot":
        return SKIP_FORM if scenario == "hard_requirement_skip" else STANDARD_FORM
    if name == "fill_many":
        for item in args.get("fields", []):
            state.filled[str(item.get("ref"))] = ("fill_many", str(item.get("value", "")))
        return "ok: all requested text fields filled"
    if name in {"fill", "fill_autocomplete"}:
        state.filled[str(args.get("ref"))] = (name, str(args.get("value", "")))
        return "ok: field filled"
    if name == "upload_file":
        state.filled[str(args.get("ref"))] = (name, str(args.get("file_path", "")))
        return "ok: resume uploaded"
    if name == "open_dropdown":
        if args.get("ref") != "r6":
            return "error: this is a typeahead; use fill_autocomplete"
        state.opened_dropdown_turn = turn
        return "o0: 'No'\no1: 'Yes'"
    if name == "pick_option":
        if state.opened_dropdown_turn == turn:
            state.same_turn_dropdown_pick = True
        state.picked_option = str(args.get("option_ref"))
        return "ok: option picked"
    if name == "submit":
        state.submitted = True
        return "ok: TEST MODE — would have submitted; no click performed"
    if name == "report_done":
        state.terminal = args
        return "ok: verdict recorded"
    if name in {"click", "click_many"}:
        return "ok: clicked"
    return f"error: unsupported benchmark tool {name}"


def _score_standard(state: ReplayState, profile: dict[str, Any]) -> dict[str, bool]:
    names = [call["name"] for call in state.calls]
    index = {name: i for i, name in enumerate(names)}
    identity = {
        "r0": str(profile["first_name"]),
        "r1": str(profile["last_name"]),
        "r2": str(profile["email"]),
    }
    identity_ok = all(
        ref in state.filled and state.filled[ref][1].strip().casefold() == expected.casefold()
        for ref, expected in identity.items()
    )
    phone = state.filled.get("r3", ("", ""))[1]
    location_tool, location = state.filled.get("r4", ("", ""))
    school_tool, school = state.filled.get("r7", ("", ""))
    upload_tool, upload_path = state.filled.get("r5", ("", ""))
    return {
        "navigate_then_snapshot": names[:2] == ["navigate", "snapshot"],
        "identity_exact": identity_ok,
        "phone_grounded": _digits(str(profile["phone"])) in _digits(phone),
        "location_autocomplete": location_tool == "fill_autocomplete" and "ottawa" in location.casefold(),
        "school_autocomplete": school_tool == "fill_autocomplete" and "carleton" in school.casefold(),
        "resume_uploaded": upload_tool == "upload_file" and upload_path == "/tmp/resume.pdf",
        "sponsorship_yes": state.picked_option == "o1",
        "dropdown_sequential": not state.same_turn_dropdown_pick,
        "optional_fields_blank": not ({"r8", "r9"} & state.filled.keys()),
        "submitted": state.submitted,
        "reported_after_submit": (
            state.terminal is not None
            and state.terminal.get("status") == "applied"
            and "submit" in index
            and "report_done" in index
            and index["submit"] < index["report_done"]
        ),
        "no_malformed_tools": state.malformed_args == 0,
    }


def _score_skip(state: ReplayState) -> dict[str, bool]:
    names = [call["name"] for call in state.calls]
    note = str((state.terminal or {}).get("note", "")).casefold()
    work_tools = {"fill", "fill_many", "fill_autocomplete", "upload_file", "submit"}
    return {
        "navigate_then_snapshot": names[:2] == ["navigate", "snapshot"],
        "correct_skip": (state.terminal or {}).get("status") == "skipped",
        "clearance_reason": "clearance" in note,
        "no_form_work": not any(name in work_tools for name in names),
        "no_malformed_tools": state.malformed_args == 0,
    }


def run_replay(
    model: str,
    scenario: str,
    max_turns: int,
    *,
    base_url: str,
    api_key: str,
) -> dict[str, Any]:
    profile = _profile()
    resume = Path("resume_base.tex").read_text(encoding="utf-8")
    metadata = (
        {
            "risk_flags": ["Active US Secret clearance required; candidate has no clearance"],
            "keywords_missing": ["active Secret clearance"],
        }
        if scenario == "hard_requirement_skip"
        else {"risk_flags": [], "keywords_missing": []}
    )
    blocks = build_user_blocks(
        job_id="benchmark-001",
        company="Benchmark Corp",
        title="Software Engineer",
        job_url="https://example.com/jobs/benchmark-001",
        resume_pdf_path="/tmp/resume.pdf",
        profile_md=json.dumps(profile, indent=2),
        resume_tex=resume,
        tailor_metadata_json=json.dumps(metadata),
        test_mode=True,
    )
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": [
                {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
            ],
        },
        {"role": "user", "content": blocks},
    ]
    state = ReplayState()
    client = OpenAI(
        base_url=base_url,
        api_key=api_key,
        max_retries=2,
    )
    usage = {"input": 0, "output": 0, "cached": 0, "cost_usd": 0.0}
    started = time.monotonic()
    nudged = False
    error: str | None = None

    for turn in range(max_turns):
        if turn == 0:
            tool_choice: Any = {"type": "function", "function": {"name": "navigate"}}
        elif turn == 1:
            tool_choice = {"type": "function", "function": {"name": "snapshot"}}
        elif nudged:
            tool_choice = "required"
        else:
            tool_choice = "auto"
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=TOOL_DEFS,
                tool_choice=tool_choice,
                temperature=0,
                max_tokens=512,
                extra_body={
                    "cache_control": {"type": "ephemeral"},
                    "usage": {"include": True},
                    "reasoning": {"effort": "none"},
                },
            )
        except Exception as exc:  # noqa: BLE001 - benchmark should report provider errors
            error = f"{type(exc).__name__}: {exc}"
            break

        if response.usage:
            usage["input"] += response.usage.prompt_tokens or 0
            usage["output"] += response.usage.completion_tokens or 0
            usage["cost_usd"] += float(getattr(response.usage, "cost", 0.0) or 0.0)
            details = getattr(response.usage, "prompt_tokens_details", None)
            usage["cached"] += int(getattr(details, "cached_tokens", 0) or 0) if details else 0

        assistant = response.choices[0].message
        messages.append(assistant.model_dump(exclude_none=True))
        calls = assistant.tool_calls or []
        if not calls:
            state.prose_stalls += 1
            if not nudged:
                nudged = True
                messages.append(
                    {
                        "role": "user",
                        "content": "Continue using tools only. Finish with report_done.",
                    }
                )
                continue
            error = "model stopped twice without a tool call"
            break
        nudged = False

        for call in calls:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
                state.malformed_args += 1
            result = _simulate_tool(
                name=call.function.name,
                args=args,
                turn=turn,
                scenario=scenario,
                state=state,
            )
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
        if state.terminal:
            break

    checks = _score_skip(state) if scenario == "hard_requirement_skip" else _score_standard(state, profile)
    return {
        "model": model,
        "scenario": scenario,
        "score": f"{sum(checks.values())}/{len(checks)}",
        "failed_checks": [name for name, passed in checks.items() if not passed],
        "tool_calls": [call["name"] for call in state.calls],
        "terminal": state.terminal,
        "prose_stalls": state.prose_stalls,
        "input_tokens": usage["input"],
        "cached_tokens": usage["cached"],
        "output_tokens": usage["output"],
        "cost_usd": round(usage["cost_usd"], 6),
        "seconds": round(time.monotonic() - started, 2),
        "error": error,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", nargs="+", default=list(DEFAULT_MODELS))
    parser.add_argument(
        "--scenarios",
        nargs="+",
        choices=("standard_form", "hard_requirement_skip"),
        default=["standard_form", "hard_requirement_skip"],
    )
    parser.add_argument("--max-turns", type=int, default=12)
    parser.add_argument("--base-url", default="https://openrouter.ai/api/v1")
    parser.add_argument("--api-key", default=None)
    args = parser.parse_args()
    load_env()
    local_ollama = args.base_url.startswith("http://localhost:11434/")
    api_key = args.api_key or ("ollama" if local_ollama else os.environ.get("OPENROUTER_API_KEY"))
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY is not configured; or pass --api-key")

    results = []
    for model in args.models:
        for scenario in args.scenarios:
            result = run_replay(
                model,
                scenario,
                args.max_turns,
                base_url=args.base_url,
                api_key=api_key,
            )
            results.append(result)
            print(json.dumps(result), flush=True)

    print("SUMMARY")
    for model in args.models:
        rows = [row for row in results if row["model"] == model]
        passed = sum(int(row["score"].split("/")[0]) for row in rows)
        total = sum(int(row["score"].split("/")[1]) for row in rows)
        print(
            json.dumps(
                {
                    "model": model,
                    "score": f"{passed}/{total}",
                    "cost_usd": round(sum(row["cost_usd"] for row in rows), 6),
                    "seconds": round(sum(row["seconds"] for row in rows), 2),
                }
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
