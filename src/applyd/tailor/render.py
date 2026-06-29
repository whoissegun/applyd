from __future__ import annotations

import json
import os
import re
from typing import Optional

from openai import OpenAI

from .prompts import SYSTEM_PROMPT


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# OpenRouter slug — also a valid pricing key in db.pricing.PRICING, so it doubles
# as the model_used label stored on tailored_resumes / usage_events.
DEFAULT_MODEL = "anthropic/claude-sonnet-4-6"


def _parse_response(text: str) -> tuple[str, dict]:
    """Extract (latex, metadata) from Claude's structured response.

    Expected shape:
      <json object>

      ```latex
      \\documentclass...
      \\end{document}
      ```
    Falls back gracefully if the model deviates.
    """
    latex_match = re.search(r"```latex\s*\n(.*?)\n```", text, re.DOTALL)
    if latex_match is None:
        # fallback: entire body as latex, no metadata
        return text.strip(), {}

    latex = latex_match.group(1).strip()

    before = text[: latex_match.start()].strip()
    metadata: dict = {}
    # find first balanced JSON object in the prefix
    obj_match = re.search(r"\{.*?\}\s*$", before, re.DOTALL)
    if obj_match is None:
        obj_match = re.search(r"\{.*\}", before, re.DOTALL)
    if obj_match is not None:
        try:
            metadata = json.loads(obj_match.group(0))
        except json.JSONDecodeError as e:
            metadata = {"parse_error": f"{type(e).__name__}: {e}"}

    return latex, metadata


class TailorClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        max_tokens: int = 8192,
        temperature: float = 0.3,
    ) -> None:
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY not set")
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._client = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=self.api_key, max_retries=8)

    def tailor(
        self,
        master_text: str,
        jd_text: str,
        company: str,
        role: str,
    ) -> tuple[str, dict, dict]:
        """Return (tailored_tex, metadata, usage).

        Runs through OpenRouter (OpenAI-compatible). The cache_control markers on
        the system prompt and master resume pass through to Anthropic for prompt
        caching — same ~10x cheaper cached reads as the direct SDK; harmless on
        non-Anthropic upstreams.
        """
        messages = [
            {
                "role": "system",
                "content": [
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Master resume (plain text, kitchen-sink version):\n\n"
                            + master_text
                        ),
                        "cache_control": {"type": "ephemeral"},
                    },
                    {
                        "type": "text",
                        "text": (
                            f"Target role\nCompany: {company}\nRole: {role}\n\n"
                            f"Job description:\n{jd_text}"
                        ),
                    },
                ],
            },
        ]

        resp = self._client.chat.completions.create(
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            messages=messages,
        )

        raw = (resp.choices[0].message.content or "").strip()
        latex, metadata = _parse_response(raw)

        # OpenRouter reports cached reads under prompt_tokens_details; prompt_tokens
        # is the full input (cached + fresh). It doesn't surface Anthropic's cache
        # *write* count separately, so we report fresh = total - cached and leave
        # cache_creation at 0 (the write is folded into prompt_tokens on a miss).
        u = resp.usage
        details = getattr(u, "prompt_tokens_details", None)
        cache_read = (getattr(details, "cached_tokens", 0) or 0) if details else 0
        usage = {
            "input_tokens": max((u.prompt_tokens or 0) - cache_read, 0),
            "output_tokens": u.completion_tokens or 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": cache_read,
        }
        return latex, metadata, usage
