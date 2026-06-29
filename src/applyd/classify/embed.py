"""Embeddings for matchmaker candidate ranking.

Jobs: embed the classification (not the raw JD — boilerplate drags vectors
together; the classification is the JD's signal in a consistent format).

Users: embed resume + LLM-extracted *positive* targets from profile_answers.
Never embed exclusions — "no frontend roles" moves the vector TOWARD frontend
jobs. Negation handling stays with the Haiku judge in match.py.

Vectors live on jobs.embedding / user_profiles.embedding (pgvector, 1536-dim);
ranking happens in SQL via rank_jobs_for_user(). Cost: ~$0.02/M tokens via
OpenRouter — a few cents for the whole catalog, fractions of a cent per user.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from ._openrouter import openrouter_client

EMBED_MODEL = "openai/text-embedding-3-small"
EMBED_DIMS = 1536
MAX_EMBED_CHARS = 12000  # well under the model's 8k-token input cap

# Via OpenRouter (not direct Anthropic) so this module needs only
# OPENROUTER_API_KEY — same key as the embeddings themselves.
TARGETS_MODEL = "anthropic/claude-haiku-4.5"
TARGETS_SYSTEM = """From a candidate's freeform profile notes, extract ONLY what they positively want: target roles, domains, technologies, company types, locations they're drawn to.

OMIT entirely: anything they want to avoid or exclude, work-authorization caveats, negations of any kind. If a sentence says "no X" or "not interested in Y", X and Y must not appear in your output at all.

Return 1-3 plain sentences. No preamble, no bullets. If there are no positive targets, return an empty string."""


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts in one API call, order-preserving."""
    if not texts:
        return []
    resp = openrouter_client().embeddings.create(
        model=EMBED_MODEL,
        input=[t[:MAX_EMBED_CHARS] for t in texts],
    )
    by_index = sorted(resp.data, key=lambda d: d.index)
    return [d.embedding for d in by_index]


def job_embedding_text(title: str, classification: dict[str, Any]) -> str:
    """Render a classification into the canonical text we embed."""
    c = {k: v for k, v in classification.items() if k != "_usage"}

    def join(val: Any) -> str:
        if isinstance(val, list):
            return ", ".join(str(v) for v in val)
        return str(val or "")

    lines = [f"TITLE: {(title or '').strip()}"]
    for label, key in (
        ("ROLE", "role_summary"),
        ("SENIORITY", "seniority_signal"),
        ("DOMAINS", "domain_focus"),
        ("STACK", "tech_stack_required"),
        ("SIGNALS", "soft_signals"),
        ("REQUIREMENTS", "deal_breakers"),
    ):
        if c.get(key):
            lines.append(f"{label}: {join(c[key])}")
    return "\n".join(lines)


def extract_positive_targets(profile_answers: str) -> str:
    """One cheap Haiku call: positive preferences only, exclusions dropped."""
    if not (profile_answers or "").strip():
        return ""
    resp = openrouter_client().chat.completions.create(
        model=TARGETS_MODEL,
        max_tokens=300,
        messages=[
            {"role": "system", "content": TARGETS_SYSTEM},
            {"role": "user", "content": profile_answers.strip()[:8000]},
        ],
    )
    return (resp.choices[0].message.content or "").strip()


def user_embedding_text(resume_text: str, positive_targets: str) -> str:
    parts = []
    if positive_targets:
        parts.append(f"TARGETS: {positive_targets}")
    parts.append(f"RESUME:\n{(resume_text or '').strip()}")
    return "\n\n".join(parts)


def _source_hash(profile_answers: str, resume_text: str) -> str:
    raw = "\x00".join([EMBED_MODEL, profile_answers or "", resume_text or ""])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def ensure_user_embedding(sb, user_id: str, profile_answers: str, resume_text: str) -> bool:
    """Embed the user if their profile/resume changed since last time.

    Hash-guarded so the steady state is one SELECT comparison per sweep.
    Returns True if a (re-)embed happened.
    """
    want = _source_hash(profile_answers, resume_text)
    row = (
        sb.table("user_profiles")
        .select("embedding_source_hash")
        .eq("id", user_id).single().execute().data
    )
    if row and row.get("embedding_source_hash") == want:
        return False

    targets = extract_positive_targets(profile_answers)
    vector = embed_texts([user_embedding_text(resume_text, targets)])[0]
    sb.table("user_profiles").update(
        {"embedding": json.dumps(vector), "embedding_source_hash": want}
    ).eq("id", user_id).execute()
    return True
