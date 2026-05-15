"""LLM matcher: decides whether a user should apply to a given job.

Inputs:
  - user's profile_answers (freeform prose; their preferences live in here)
  - user's master resume LaTeX (truncated to ~8k chars)
  - job's classification jsonb (output of classify_job)

Output:
  {
    "decision": "accept" | "reject" | "borderline",
    "reason": "1-2 sentence rationale",
    "confidence": 0.0–1.0
  }

Costs ~$0.001 per call with Haiku 4.5. Run only on unmatched (user × job)
pairs; the matchmaker worker handles batching and dedup.
"""
from __future__ import annotations

import json
import os
from typing import Any

from anthropic import Anthropic


MODEL = "claude-haiku-4-5"
MAX_RESUME_CHARS = 8000  # ~2000 tokens of LaTeX — covers a full base resume

SYSTEM = """You decide whether a candidate should apply to a job. We are aggressively volume-first: when in doubt, ACCEPT. The apply agent will do a second, deeper check at form-fill time and skip if the form reveals a hard mismatch. Your job is to filter out CLEAR mismatches only — do not gatekeep on stretches.

You'll get three inputs:
1. profile_answers: the candidate's own prose — preferences, targets, "skip these" lists. AUTHORITATIVE for their stated preferences.
2. master_resume (LaTeX): read the content, ignore markup.
3. job_classification: structured description of the role.

REJECT only when:
- The role is in a function the profile_answers explicitly excludes ("no sales" + sales role).
- The job has a HARD requirement the candidate clearly can't meet — note: "preferred", "ideally", "a plus", "5+ years preferred" are SOFT and not deal-breakers. Only literal hard floors like "requires 5+ years" or "must have PhD" count.
- Seniority gap is 3+ levels above (e.g. principal/staff/director for a junior/intern). A new-grad applying to a "mid-level / 3-5 yrs preferred" role is NOT a 3-level gap.
- Domain is completely outside the candidate's experience AND outside their stated targets (e.g. radiology AI for someone with no medical background and no stated interest in healthcare).

ACCEPT in all other cases. Domain adjacency, single-level seniority stretches, "preferred but not required" experience gaps — these are accepts.

BORDERLINE is rare — only for cases where you genuinely cannot tell whether profile_answers excludes the role.

Return ONLY a JSON object with these exact keys:
  decision   ("accept" | "reject" | "borderline")
  reason     (one or two sentences, plain English; reference specifics from the inputs)
  confidence (0.0 to 1.0)

No prose, no markdown fences."""


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        # ```json\n{...}\n``` or ```\n{...}\n```
        text = text.split("```", 2)[1].strip()
        if text.startswith("json"):
            text = text[4:].strip()
    return text


def match_user_to_job(
    profile_answers: str,
    resume_latex: str,
    classification: dict[str, Any],
) -> dict[str, Any]:
    """One match decision. Returns the parsed JSON dict with an extra `_usage` key."""
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=8)

    # Drop _usage from classification if present (we attach it on the classifier side).
    classification_for_prompt = {k: v for k, v in classification.items() if k != "_usage"}

    user_content = (
        "## profile_answers (candidate's own description and preferences)\n"
        f"{(profile_answers or '').strip()}\n\n"
        "## master_resume (LaTeX — extract content, ignore markup)\n"
        f"{(resume_latex or '').strip()[:MAX_RESUME_CHARS]}\n\n"
        "## job_classification\n"
        f"{json.dumps(classification_for_prompt, ensure_ascii=False, indent=2)}\n"
    )

    resp = client.messages.create(
        model=MODEL,
        max_tokens=512,
        system=SYSTEM,
        messages=[{"role": "user", "content": user_content}],
    )

    text = _strip_fences("".join(b.text for b in resp.content if b.type == "text"))
    parsed = json.loads(text)

    decision = parsed.get("decision", "").lower()
    if decision not in {"accept", "reject", "borderline"}:
        # Defensive: if the model returns something off-script, treat as borderline.
        parsed["decision"] = "borderline"
        parsed["reason"] = f"(matcher returned unexpected decision={decision!r}) " + parsed.get("reason", "")
    else:
        parsed["decision"] = decision

    parsed["_usage"] = {
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0) or 0,
    }
    return parsed
