"""Per-job classifier. Run once per job to produce a structured-but-freeform
JSON describing the role. Stored on jobs.classification (jsonb).

Costs ~$0.001 per call with Haiku 4.5. Schema is intentionally open: keys are
typical but not strictly enforced — the matcher reads whatever is present.
"""
from __future__ import annotations

import json
import os
from typing import Any

from anthropic import Anthropic

from ..db import cost_cents_for_tailor


MODEL = "claude-haiku-4-5"
MAX_DESCRIPTION_CHARS = 6000  # ~1500 tokens; plenty for classification

SYSTEM = """You classify job postings into a structured JSON object so a matcher can decide whether a candidate is a fit.

Output a single JSON object with these keys (omit a key if you can't infer it):
- role_summary: 1-2 sentence plain description of the role. Plain English, no buzzwords.
- seniority_signal: experience expectation ("intern", "new grad / 0-2 years", "mid / 3-5 years", "senior / 5-8", "staff+ / 8+", "manager", "director", etc.). Use whatever phrasing fits.
- domain_focus: array of the actual problem areas (e.g. ["recommender systems", "feature engineering", "offline evaluation"], or ["enterprise sales", "channel partnerships"], or ["UI/UX", "design systems"]).
- tech_stack_required: array of technologies the listing explicitly requires.
- soft_signals: array of nuance the matcher should know (e.g. "startup pre-Series A", "remote-first", "in-person SF required", "equity-heavy comp", "expects on-call").
- deal_breakers: array of hard requirements that would disqualify many candidates (e.g. "5+ years sales experience", "PhD in physics required", "must be onsite NYC").

Return ONLY the JSON. No prose, no markdown fences."""


def classify_job(title: str, description: str | None) -> dict[str, Any]:
    """Run a single classifier call. Returns the parsed JSON dict.

    Caller is responsible for storing the result (and computing cost via
    `cost_cents_for_tailor(MODEL, prompt_tokens, completion_tokens, cached_tokens)`
    — Haiku pricing is in PRICING).
    """
    # max_retries=8 → SDK does exponential backoff on 429/5xx. The default of 2
    # isn't enough for sustained backfill against Anthropic's per-minute tokens cap.
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=8)

    user_content = (
        f"TITLE: {title.strip()}\n\n"
        f"DESCRIPTION:\n{(description or '').strip()[:MAX_DESCRIPTION_CHARS]}"
    )

    resp = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=SYSTEM,
        messages=[{"role": "user", "content": user_content}],
    )

    text = "".join(block.text for block in resp.content if block.type == "text").strip()
    # Strip accidental ``` fences if model adds them despite instructions.
    if text.startswith("```"):
        text = text.split("```", 2)[1].strip()
        if text.startswith("json"):
            text = text[4:].strip()

    parsed = json.loads(text)

    # Attach usage so callers can compute cost without re-running.
    parsed["_usage"] = {
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0) or 0,
    }
    return parsed


def cost_cents_for_classification(usage: dict[str, int]) -> int:
    """Compute classifier-call cost from the usage dict returned in the result."""
    prompt = usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
    completion = usage.get("output_tokens", 0)
    cached = usage.get("cache_read_input_tokens", 0)
    return cost_cents_for_tailor(
        model=MODEL,
        prompt_tokens=prompt,
        completion_tokens=completion,
        cached_tokens=cached,
    )
