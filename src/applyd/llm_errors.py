"""Transient vs terminal LLM-failure classification.

The apply/tailor pipelines call LLMs through OpenRouter (OpenAI SDK). Some
failures are the *job's* fault (a JD that can't be tailored, a form that can't
be filled) — those are terminal. Others are *account/provider-wide* (no
credits, rate limit, provider outage) — those say nothing about the job and
will hit every row equally. Burning a perfectly good application to terminal
'failed' because the platform briefly ran out of credits is the bug this
guards against.

Workers use `is_transient_llm_error` to decide: transient → requeue the row
(stays claimable) and raise `TransientInfraError` so the worker backs off
globally instead of grinding the whole queue into the ground; terminal →
release to 'failed' as before.
"""
from __future__ import annotations

import openai

# 402 no-credits, 408 timeout, 409 conflict, 429 rate-limit, 5xx provider
# errors, 529 Anthropic-overloaded (surfaced through OpenRouter).
_TRANSIENT_STATUS = {402, 408, 409, 425, 429, 500, 502, 503, 504, 529}


class TransientInfraError(Exception):
    """An LLM call failed for an account/provider-wide reason, not a job-specific
    one. Signals the worker to requeue and back off rather than mark terminal."""


def is_transient_llm_error(exc: BaseException) -> bool:
    """True if `exc` is an account/provider-wide LLM failure worth retrying."""
    if isinstance(
        exc,
        (
            openai.APITimeoutError,
            openai.APIConnectionError,
            openai.RateLimitError,
            openai.InternalServerError,
        ),
    ):
        return True
    if isinstance(exc, openai.APIStatusError):
        status = getattr(exc, "status_code", None)
        if status in _TRANSIENT_STATUS:
            return True
        # OpenRouter reports a spend-capped key as 403 "Key limit exceeded" —
        # account-wide like 402 no-credits, not a per-job permission problem.
        # Plain 403s (model not allowed, etc.) stay terminal.
        if status == 403 and "limit" in str(exc).lower():
            return True
        return False
    return False
