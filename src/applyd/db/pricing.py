"""Cost computation for usage_events.

Prices are stored in CENTS per million tokens (or per GB for bandwidth) so
integer math gets us to integer cents at the end without intermediate floats
for the common path. Per-million keeps the table readable and matches how the
providers publish their pricing.

Anthropic (direct):
    claude-sonnet-4-6: $3 input / $15 output / $0.30 cached-read per 1M tokens
    claude-haiku-4-5:  $1 input /  $5 output / $0.10 cached-read per 1M tokens

OpenRouter (passthrough; we treat it as the same per-token cost — OpenRouter's
margin is the 5.5% top-up on credit purchase, not per-call):
    anthropic/claude-sonnet-4-6: same as Anthropic direct

Bright Data Scraping Browser bandwidth: $8/GB.

Unknown models raise ValueError — silent estimation in a billing path would be
worse than a loud failure.
"""

from __future__ import annotations

from typing import TypedDict


class _ModelPrice(TypedDict):
    input_cents_per_mtok: float       # cents per 1M input tokens
    output_cents_per_mtok: float      # cents per 1M output tokens
    cached_read_cents_per_mtok: float  # cents per 1M cached-read input tokens


# $1 = 100 cents; $3/Mtok = 300 cents/Mtok.
PRICING: dict[str, _ModelPrice] = {
    "claude-sonnet-4-6": {
        "input_cents_per_mtok": 300.0,
        "output_cents_per_mtok": 1500.0,
        "cached_read_cents_per_mtok": 30.0,
    },
    "anthropic/claude-sonnet-4-6": {
        "input_cents_per_mtok": 300.0,
        "output_cents_per_mtok": 1500.0,
        "cached_read_cents_per_mtok": 30.0,
    },
    "claude-haiku-4-5": {
        "input_cents_per_mtok": 100.0,
        "output_cents_per_mtok": 500.0,
        "cached_read_cents_per_mtok": 10.0,
    },
}

# Bright Data Scraping Browser: $8 per GB == 800 cents per 1024 MB.
BRIGHTDATA_CENTS_PER_GB = 800.0


def _round_cents(raw: float) -> int:
    """Round to nearest cent, but never return 0 for non-zero raw usage."""
    if raw <= 0:
        return 0
    cents = int(round(raw))
    return max(cents, 1)


def _llm_cost_cents(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int,
) -> float:
    if model not in PRICING:
        raise ValueError(
            f"unknown model {model!r}; add it to applyd.db.pricing.PRICING "
            f"before billing for it"
        )
    p = PRICING[model]
    # Anthropic-style accounting: prompt_tokens is the *uncached* input count;
    # cached_tokens is reported separately and billed at the cached read rate.
    # If a caller double-counts (passes prompt = total, then also cached) we'll
    # over-bill — that's on them; the SDK exposes the split cleanly.
    fresh_input = max(prompt_tokens, 0)
    cached = max(cached_tokens, 0)
    output = max(completion_tokens, 0)
    return (
        fresh_input * p["input_cents_per_mtok"] / 1_000_000
        + cached * p["cached_read_cents_per_mtok"] / 1_000_000
        + output * p["output_cents_per_mtok"] / 1_000_000
    )


def cost_cents_for_tailor(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int = 0,
) -> int:
    """Cost in integer cents for one tailor call. Raises ValueError on unknown model."""
    return _round_cents(
        _llm_cost_cents(model, prompt_tokens, completion_tokens, cached_tokens)
    )


def cost_cents_for_apply(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int = 0,
    browser_mb: float = 1.5,
) -> int:
    """Cost in integer cents for one apply call (LLM + Bright Data bandwidth).

    browser_mb is per-apply bandwidth used by the Scraping Browser; default
    ~1.5MB matches observed averages on Greenhouse/Lever forms.
    """
    llm = _llm_cost_cents(model, prompt_tokens, completion_tokens, cached_tokens)
    bandwidth = max(browser_mb, 0.0) / 1024.0 * BRIGHTDATA_CENTS_PER_GB
    return _round_cents(llm + bandwidth)
