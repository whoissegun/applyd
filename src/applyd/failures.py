"""Failure taxonomy, alerting, and systemic-failure detection.

Lessons from the June 2026 compile burn: a single environment bug (tectonic
choking on a pdflatex primitive) marked every job terminally `failed` and ran
silently for weeks because (a) errors were freeform text nobody aggregated and
(b) nothing noticed that *every* job was failing the same way.

This module gives three things:
- `categorize(reason)` — maps our prefixed reason strings to a finite set of
  operational categories, so failures are queryable ("how many compile vs
  credit vs dead_link") instead of regex-on-freeform.
- `notify(subject, body)` — loud log + optional webhook (Slack/Discord), so a
  burn surfaces same-day instead of whenever someone reads logs.
- `SystemicFailureTracker` — trips when N consecutive failures share a
  *systemic* category (compile/storage/validation), generalizing the LLM
  circuit-breaker to "anything that's broken for every job, not just this one".

NOTE: these categories are OPERATIONAL failure types — a genuinely finite,
code-owned set — not job/role taxonomies (which we deliberately keep freeform,
see [[feedback_no_enums_for_taxonomies]]).
"""
from __future__ import annotations

import json
import logging
import os
from collections import deque
from typing import Optional

logger = logging.getLogger("applyd.failures")


# ── failure categories ──────────────────────────────────────────────────────
COMPILE = "compile"        # tectonic / LaTeX compile failure
LLM_INFRA = "llm_infra"    # provider-wide: no credits, rate limit, outage
STORAGE = "storage"        # Supabase Storage upload/download failure
VALIDATION = "validation"  # structural validator rejected the tailored resume
JD_MISMATCH = "jd_mismatch"  # apply agent found a hard JD vs candidate mismatch
DEAD_LINK = "dead_link"    # posting gone / unreachable
NO_RESUME = "no_resume"    # user has no master resume
NO_JD = "no_jd"            # job has no description to tailor against
MATCHER = "matcher"        # matcher rejected (expected, per-job)
UNKNOWN = "unknown"

# Categories that, repeated, mean something is broken for EVERY job rather than
# wrong with one job. These drive the systemic detector. dead_link / jd_mismatch
# / matcher / no_resume are legitimately per-job-or-per-user and never trip it.
SYSTEMIC = frozenset({COMPILE, STORAGE, VALIDATION})

# reason-prefix → category. We own the reason strings, so prefix matching is
# reliable (no fragile NLP). Order matters: first hit wins.
_PREFIX_MAP: tuple[tuple[str, str], ...] = (
    ("compile_error", COMPILE),
    ("storage_error", STORAGE),
    ("tailor_call_error", LLM_INFRA),
    ("runner_crash", LLM_INFRA),
    ("infra:", LLM_INFRA),
    ("validation", VALIDATION),
    ("validator", VALIDATION),
    ("no_master_resume", NO_RESUME),
    ("no_resume", NO_RESUME),
    ("no_tailored_resume", NO_RESUME),
    ("job_missing", NO_JD),
    ("no_jd", NO_JD),
    ("gated:dead_link", DEAD_LINK),
    ("dead_link", DEAD_LINK),
    ("skipped:jd_mismatch", JD_MISMATCH),
    ("jd_mismatch", JD_MISMATCH),
    ("matcher:", MATCHER),
    ("prefilter:", MATCHER),  # seniority prefilter: same operational bucket as a judge reject
)


def categorize(reason: Optional[str]) -> str:
    """Map a freeform reason string to an operational failure category."""
    if not reason:
        return UNKNOWN
    r = reason.strip().lower()
    for prefix, cat in _PREFIX_MAP:
        if r.startswith(prefix) or (":" in prefix and prefix in r):
            return cat
    return UNKNOWN


# ── alerting ────────────────────────────────────────────────────────────────
def notify(subject: str, body: str = "") -> None:
    """Surface an operational alert. Always logs at ERROR; additionally POSTs to
    `APPLYD_ALERT_WEBHOOK` if set (Slack/Discord-compatible payload).

    Ship default = log (Railway captures it). Swap-path = webhook. Failures to
    deliver the webhook are swallowed — alerting must never crash the worker.
    """
    logger.error("ALERT: %s%s", subject, f" — {body}" if body else "")
    url = os.environ.get("APPLYD_ALERT_WEBHOOK")
    if not url:
        return
    text = f"*{subject}*\n{body}" if body else subject
    try:
        import httpx

        # `text` for Slack, `content` for Discord — each ignores the other key.
        httpx.post(url, json={"text": text, "content": text}, timeout=10.0)
    except Exception:  # noqa: BLE001 — alerting is best-effort
        logger.warning("notify: webhook POST failed", exc_info=True)


# ── systemic-failure detection ──────────────────────────────────────────────
class SystemicFailureError(Exception):
    """Raised when consecutive failures share a systemic category — something is
    broken for every job, not just this one. Workers back off + alert, the same
    way they do for a transient LLM outage."""


class SystemicFailureTracker:
    """Rolling detector: trips when `threshold` consecutive terminal failures all
    share one systemic category. Any success, or any per-job-normal outcome
    (dead_link, matcher reject, …), resets the streak — so a run of legitimately
    unfillable jobs never trips it, but a broken compile/storage env does.
    """

    def __init__(self, threshold: int = 5) -> None:
        self.threshold = max(2, threshold)
        self._cat: Optional[str] = None
        self._streak = 0
        self.recent: deque[str] = deque(maxlen=self.threshold)

    def record(self, *, status: str, category: str) -> None:
        """Feed one worker result. Raises SystemicFailureError if it trips."""
        if status != "failed" or category not in SYSTEMIC:
            # success, or a per-job/per-user failure: not systemic evidence.
            self._cat, self._streak = None, 0
            self.recent.clear()
            return
        if category == self._cat:
            self._streak += 1
        else:
            self._cat, self._streak = category, 1
        self.recent.append(category)
        if self._streak >= self.threshold:
            cat, n = category, self._streak
            self._cat, self._streak = None, 0
            self.recent.clear()
            raise SystemicFailureError(
                f"{n} consecutive '{cat}' failures — systemic, not per-job"
            )


def alert_payload_for(streak_error: SystemicFailureError) -> str:
    return json.dumps({"systemic_failure": str(streak_error)})
