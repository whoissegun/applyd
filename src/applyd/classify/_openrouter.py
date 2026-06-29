"""Shared OpenRouter (OpenAI-compatible) client for the classify stage.

The matcher, job classifier, and embeddings all route through OpenRouter rather
than the Anthropic SDK directly, so the whole classify stage runs on one
OPENROUTER_API_KEY (the same key the apply runner uses) and the model is
swappable per call. Pricing is still keyed by the canonical Anthropic model id
in db.pricing — the OpenRouter slug is only the API address.
"""
from __future__ import annotations

import os

from openai import OpenAI

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# max_retries=8 → SDK does exponential backoff on 429/5xx. The default of 2
# isn't enough for sustained backfill against the upstream per-minute caps.
def openrouter_client() -> OpenAI:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not set (needed for classify LLM calls)")
    return OpenAI(base_url=OPENROUTER_BASE_URL, api_key=key, max_retries=8)
