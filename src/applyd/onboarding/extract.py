"""One Claude call: resume PDF → plain text + contact + work-auth.

The tailor consumes the plain text directly. The small contact/work-auth
sub-blocks are used to prefill the onboarding basics + work-auth steps.
Output is plain-text delimited (not JSON) to dodge JSON-escape issues with
resume body content (URLs, percent signs, etc).
"""

from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass, field
from typing import Optional

from anthropic import Anthropic


_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 8192

SYSTEM_PROMPT = r"""You extract content from an attached resume PDF. Return
exactly four sections in this order, each starting at column zero with the
section header on its own line:

CONTACT
name: <full name>
phone: <phone or empty>
email: <email or empty>
linkedin: <full URL or empty>
github: <full URL or empty>
portfolio: <full URL or empty>

WORK_AUTH
summary: <one-line statement of work authorization if the resume mentions it, else empty>
sponsorship_needed: <comma-separated country names if mentioned, else empty>

TARGET_ROLES
roles: <one or two sentences inferring the kinds of roles this person is best
suited for, based purely on their experience, projects, and skills. Be specific:
discipline (e.g. backend, ML infra, full-stack, applied ML, frontend), seniority
(intern / new grad / mid / senior — pick from their dates and titles), and any
strong domain signal (e.g. AI labs, fintech, infra). One direct statement. No
hedging. Example: "New-grad SWE leaning backend / ML infra, strong React +
TypeScript on the side. Also a fit for applied-ML at AI labs." If the resume
is too sparse to tell, write "Hard to tell from this resume — needs the user's
input.">

TEXT
<the entire resume as plain text, kitchen-sink — every role, project, bullet,
skill. Preserve paragraph breaks. No markdown, no LaTeX, no headers like
"EXPERIENCE" in caps — just sections with natural casing. Bullets become
one-line statements prefixed with "- ". Numbers and units stay verbatim.>

Do not omit anything from TEXT. If a field in CONTACT or WORK_AUTH isn't on
the resume, leave it empty after the colon. Do not invent facts. TARGET_ROLES
is your inference, not a fact-extraction — that's allowed there only."""


@dataclass
class ExtractedResume:
    text: str
    contact: dict[str, Optional[str]] = field(default_factory=dict)
    work_auth: dict[str, object] = field(default_factory=dict)
    target_roles: Optional[str] = None


_SECTION_RE = re.compile(
    r"^(CONTACT|WORK_AUTH|TARGET_ROLES|TEXT)\s*$", re.MULTILINE
)


def _split_sections(raw: str) -> dict[str, str]:
    """Split the LLM output by top-level section markers."""
    out: dict[str, str] = {}
    matches = list(_SECTION_RE.finditer(raw))
    for i, m in enumerate(matches):
        name = m.group(1)
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        out[name] = raw[body_start:body_end].strip("\n")
    return out


_KV_RE = re.compile(r"^\s*([a-z_]+)\s*:\s*(.*)$", re.MULTILINE)


def _parse_kv(block: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _KV_RE.finditer(block):
        out[m.group(1)] = m.group(2).strip()
    return out


def extract_from_pdf(pdf_bytes: bytes) -> ExtractedResume:
    if not pdf_bytes:
        raise ValueError("empty PDF")
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    b64 = base64.standard_b64encode(pdf_bytes).decode("ascii")
    resp = Anthropic(api_key=key).messages.create(
        model=_MODEL,
        max_tokens=_MAX_TOKENS,
        temperature=0,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": "Extract per the schema. No commentary.",
                    },
                ],
            }
        ],
    )
    raw = "".join(b.text for b in resp.content if b.type == "text").strip()
    sections = _split_sections(raw)

    text = sections.get("TEXT", "").strip()
    if not text:
        raise ValueError("extractor returned no TEXT section")

    contact_kv = _parse_kv(sections.get("CONTACT", ""))
    contact: dict[str, Optional[str]] = {
        "name": contact_kv.get("name") or None,
        "phone": contact_kv.get("phone") or None,
        "email": contact_kv.get("email") or None,
        "linkedin_url": contact_kv.get("linkedin") or None,
        "github_url": contact_kv.get("github") or None,
        "portfolio_url": contact_kv.get("portfolio") or None,
    }

    wa_kv = _parse_kv(sections.get("WORK_AUTH", ""))
    sponsorship_raw = wa_kv.get("sponsorship_needed", "")
    sponsorship = [s.strip() for s in sponsorship_raw.split(",") if s.strip()]
    work_auth: dict[str, object] = {
        "summary": wa_kv.get("summary") or None,
        "sponsorship_needed_countries": sponsorship,
    }

    target_roles_kv = _parse_kv(sections.get("TARGET_ROLES", ""))
    target_roles = target_roles_kv.get("roles") or None

    return ExtractedResume(
        text=text,
        contact=contact,
        work_auth=work_auth,
        target_roles=target_roles,
    )
