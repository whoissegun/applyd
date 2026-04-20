from __future__ import annotations

import json
import os
import re
from typing import Optional

import anthropic

from .prompts import SYSTEM_PROMPT


DEFAULT_MODEL = "claude-sonnet-4-6"


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
        max_tokens: int = 4096,
        temperature: float = 0.3,
    ) -> None:
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._client = anthropic.Anthropic(api_key=self.api_key)

    def tailor(
        self,
        base_resume_tex: str,
        jd_text: str,
        company: str,
        role: str,
    ) -> tuple[str, dict, dict]:
        """Return (tailored_tex, metadata, usage)."""
        user_content = [
            {
                "type": "text",
                "text": (
                    "Master resume (Jake's Resume LaTeX template):\n\n"
                    + base_resume_tex
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
        ]

        resp = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_content}],
        )

        parts = [b.text for b in resp.content if hasattr(b, "text")]
        raw = "\n".join(parts).strip()
        latex, metadata = _parse_response(raw)

        usage = {
            "input_tokens": resp.usage.input_tokens,
            "output_tokens": resp.usage.output_tokens,
            "cache_creation_input_tokens": getattr(
                resp.usage, "cache_creation_input_tokens", 0,
            ),
            "cache_read_input_tokens": getattr(
                resp.usage, "cache_read_input_tokens", 0,
            ),
        }
        return latex, metadata, usage
