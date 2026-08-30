"""Evaluate local semantic extraction on real job descriptions.

This does not modify job data or contact a paid model. It reads selected legacy
records and calls Ollama's OpenAI-compatible endpoint.
"""
from __future__ import annotations

import html
import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from openai import OpenAI

from applyd.config import load_env


DEFAULT_MODEL = "applyd-ministral"
DEFAULT_BASE_URL = "http://localhost:11434/v1"
MAX_DESCRIPTION_CHARS = 12_000

CASES: dict[str, dict[str, str]] = {
    "simplifyjobs:711e8836-13f2-4b00-873e-10d28b8c9753": {
        "seniority": "new_grad",
        "education": "doctorate_required",
        "workplace": "unspecified",
    },
    "simplifyjobs:61096eee-4019-4502-8944-597bb2489fb6": {
        "clearance": "active_required",
        "citizenship": "required",
    },
    "simplifyjobs:de2aa833-1aeb-4eec-b942-b25dd9ec163a": {
        "sponsorship": "unavailable",
        "clearance": "eligibility_required",
        "citizenship": "not_stated",
    },
    "simplifyjobs:67b134e0-653d-4b37-bc63-6df9e5585e40": {
        "seniority": "new_grad",
        "workplace": "hybrid",
    },
    "ashby:clickup:046f0bfe-1932-4e36-8154-92b542dedd17": {
        "sponsorship": "unavailable",
    },
}

SYSTEM = """Extract decision-relevant facts from a job posting.

Return exactly one JSON object with these keys:
- seniority: intern | new_grad | entry | mid | senior | staff_plus | manager | unknown
- workplace: remote | hybrid | onsite | unspecified
- education: none_stated | bachelors_required | masters_required | doctorate_required | other
- sponsorship: available | unavailable | conditional | not_stated
- citizenship: required | not_stated
- clearance: active_required | eligibility_required | not_stated
- minimum_years_experience: integer or null
- hard_requirements: array of {category, value, evidence}

Evidence must be a short exact quote from the posting. Distinguish already having
an active clearance from merely being eligible to obtain one. Do not interpret
silence about sponsorship as availability. For tiered roles, do not treat a
requirement for only the highest tier as universal. Return JSON only."""


def clean_description(value: str) -> str:
    text = html.unescape(html.unescape(value))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def evidence_is_grounded(result: dict[str, Any], description: str) -> bool:
    haystack = description.casefold()
    requirements = result.get("hard_requirements")
    if not isinstance(requirements, list):
        return False
    for item in requirements:
        if not isinstance(item, dict):
            return False
        evidence = str(item.get("evidence") or "").strip()
        if not evidence or evidence.casefold() not in haystack:
            return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    args = parser.parse_args()
    load_env()
    local_ollama = args.base_url.startswith("http://localhost:11434/")
    api_key = "ollama" if local_ollama else os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY is not configured")

    jobs = json.loads(Path("data/jobs.json").read_text(encoding="utf-8"))
    client = OpenAI(base_url=args.base_url, api_key=api_key, max_retries=2)
    passed = total = 0
    cost_usd = 0.0
    started = time.monotonic()

    for job_id, expected in CASES.items():
        job = jobs[job_id]
        description = clean_description(job.get("description") or "")[:MAX_DESCRIPTION_CHARS]
        response = client.chat.completions.create(
            model=args.model,
            temperature=0,
            max_tokens=1500,
            response_format={"type": "json_object"},
            extra_body=(
                {"usage": {"include": True}, "reasoning": {"effort": "none"}}
                if not local_ollama
                else {}
            ),
            messages=[
                {"role": "system", "content": SYSTEM},
                {
                    "role": "user",
                    "content": f"TITLE: {job['title']}\nCOMPANY: {job['company']}\nDESCRIPTION: {description}",
                },
            ],
        )
        raw = response.choices[0].message.content or "{}"
        if response.usage:
            cost_usd += float(getattr(response.usage, "cost", 0.0) or 0.0)
        result = json.loads(raw)
        checks = {key: result.get(key) == value for key, value in expected.items()}
        checks["evidence_grounded"] = evidence_is_grounded(result, description)
        passed += sum(checks.values())
        total += len(checks)
        print(json.dumps({
            "job": f"{job['company']} — {job['title']}",
            "expected": expected,
            "result": result,
            "checks": checks,
        }, ensure_ascii=False), flush=True)

    print(json.dumps({
        "model": args.model,
        "score": f"{passed}/{total}",
        "seconds": round(time.monotonic() - started, 2),
        "cost_usd": round(cost_usd, 6),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
