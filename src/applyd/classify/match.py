"""LLM matcher: decides whether a user should apply to given jobs.

Inputs:
  - user's profile_answers (freeform prose; their preferences live in here)
  - user's master resume LaTeX (truncated to ~8k chars)
  - job classifications (jsonb output of classify_job), judged in BATCHES

Batching is the cost lever: the profile + resume (~2k tokens) dominate the
prompt, and a per-job call re-sends them at full price every time. One call
judging BATCH_SIZE jobs amortizes them ~10×. The static blocks also carry
cache_control markers so consecutive sweeps for the same user hit the
Anthropic prompt cache when routed within its TTL.

Output per job:
  {
    "job_id":     echoed back from the input
    "decision":   "accept" | "reject" | "borderline",
    "reason":     "1-2 sentence rationale",
    "confidence": 0.0-1.0
  }

The matchmaker worker handles batching windows, dedup, and persistence.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from ._openrouter import openrouter_client


logger = logging.getLogger("applyd.classify.match")

MODEL = "claude-haiku-4-5"          # pricing key (db.pricing.PRICING)
OPENROUTER_MODEL = "anthropic/claude-haiku-4.5"  # OpenRouter API slug
MAX_RESUME_CHARS = 8000  # ~2000 tokens of LaTeX — covers a full base resume
BATCH_SIZE = 10  # jobs judged per LLM call

SYSTEM = """You decide whether a candidate should apply to jobs. We are aggressively volume-first: when in doubt, ACCEPT. The apply agent will do a second, deeper check at form-fill time and skip if the form reveals a hard mismatch. Your job is to filter out CLEAR mismatches only — do not gatekeep on stretches.

You'll get three inputs:
1. profile_answers: the candidate's own prose — preferences, targets, "skip these" lists. AUTHORITATIVE for their stated preferences.
2. master_resume (LaTeX): read the content, ignore markup.
3. jobs: a JSON array of jobs, each {job_id, classification}. Judge EVERY job, each independently on its own merits.

REJECT only when:
- The role is in a function the profile_answers explicitly excludes ("no sales" + sales role).
- The job has a HARD requirement the candidate clearly can't meet — note: "preferred", "ideally", "a plus", "5+ years preferred" are SOFT and not deal-breakers. Only literal hard floors like "requires 5+ years" or "must have PhD" count.
- Seniority gap is 3+ levels above (e.g. principal/staff/director for a junior/intern). A new-grad applying to a "mid-level / 3-5 yrs preferred" role is NOT a 3-level gap.
- Domain is completely outside the candidate's experience AND outside their stated targets (e.g. radiology AI for someone with no medical background and no stated interest in healthcare).

ACCEPT in all other cases. Domain adjacency, single-level seniority stretches, "preferred but not required" experience gaps — these are accepts.

BORDERLINE is rare — only for cases where you genuinely cannot tell whether profile_answers excludes the role.

Return ONLY a JSON array with one object per input job, in the same order, each with these exact keys:
  job_id     (echoed verbatim from the input)
  decision   ("accept" | "reject" | "borderline")
  reason     (one or two sentences, plain English; reference specifics from the inputs)
  confidence (0.0 to 1.0)

No prose, no markdown fences."""


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        # ```json\n[...]\n``` or ```\n[...]\n```
        text = text.split("```", 2)[1].strip()
        if text.startswith("json"):
            text = text[4:].strip()
    return text


def _static_user_block(profile_answers: str, resume_text: str) -> str:
    return (
        "## profile_answers (candidate's own description and preferences)\n"
        f"{(profile_answers or '').strip()}\n\n"
        "## master_resume (plain text)\n"
        f"{(resume_text or '').strip()[:MAX_RESUME_CHARS]}\n"
    )


def match_user_to_jobs(
    profile_answers: str,
    resume_text: str,
    jobs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Judge a batch of jobs in ONE call. `jobs` items need `id` and
    `classification`. Returns one verdict dict per *matched* job — a job the
    model dropped or mangled is simply absent (no applications row is written,
    so it re-ranks into a later sweep). The batch `_usage` rides on the first
    verdict only, so cost accounting stays once-per-call.
    """
    if not jobs:
        return []

    jobs_payload = [
        {
            "job_id": j["id"],
            # _usage is classifier bookkeeping, not signal for the judge.
            "classification": {
                k: v for k, v in (j.get("classification") or {}).items() if k != "_usage"
            },
        }
        for j in jobs
    ]

    resp = openrouter_client().chat.completions.create(
        model=OPENROUTER_MODEL,
        max_tokens=200 * len(jobs_payload) + 256,
        messages=[
            {
                "role": "system",
                "content": [
                    {"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": _static_user_block(profile_answers, resume_text),
                        "cache_control": {"type": "ephemeral"},
                    },
                    {
                        "type": "text",
                        "text": "## jobs\n" + json.dumps(jobs_payload, ensure_ascii=False, indent=2),
                    },
                ],
            },
        ],
    )

    text = _strip_fences(resp.choices[0].message.content or "")
    parsed = json.loads(text)
    if isinstance(parsed, dict):  # model returned a lone object for a 1-job batch
        parsed = [parsed]

    known_ids = {j["id"] for j in jobs}
    order = [j["id"] for j in jobs]
    verdicts: list[dict[str, Any]] = []
    for i, entry in enumerate(parsed):
        if not isinstance(entry, dict):
            continue
        job_id = entry.get("job_id")
        if job_id not in known_ids:
            # Echo got mangled — fall back to input order if the array is
            # positionally aligned, otherwise drop the entry.
            if len(parsed) == len(order):
                job_id = order[i]
            else:
                logger.warning("matcher: unmatchable verdict entry dropped: %r", entry)
                continue
        decision = str(entry.get("decision", "")).lower()
        if decision not in {"accept", "reject", "borderline"}:
            entry["reason"] = (
                f"(matcher returned unexpected decision={decision!r}) "
                + str(entry.get("reason", ""))
            )
            decision = "borderline"
        verdicts.append(
            {
                "job_id": job_id,
                "decision": decision,
                "reason": entry.get("reason", ""),
                "confidence": entry.get("confidence"),
            }
        )

    if len(verdicts) < len(jobs):
        logger.warning(
            "matcher: batch returned %d/%d verdicts; unjudged jobs re-rank next sweep",
            len(verdicts), len(jobs),
        )

    usage = {
        "input_tokens": resp.usage.prompt_tokens,
        "output_tokens": resp.usage.completion_tokens,
        # OpenRouter doesn't surface Anthropic prompt-cache splits; cost falls
        # back to the uncached input rate, which is the safe over-estimate.
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    if verdicts:
        verdicts[0]["_usage"] = usage
    return verdicts


def match_user_to_job(
    profile_answers: str,
    resume_text: str,
    classification: dict[str, Any],
) -> dict[str, Any]:
    """Single-job convenience wrapper over the batch call (dev/CLI use)."""
    verdicts = match_user_to_jobs(
        profile_answers, resume_text, [{"id": "job", "classification": classification}]
    )
    if not verdicts:
        return {"decision": "borderline", "reason": "matcher returned no verdict", "confidence": 0.0}
    return verdicts[0]
