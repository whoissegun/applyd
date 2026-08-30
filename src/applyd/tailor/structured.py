"""Structured Kimi tailoring and deterministic LaTeX rendering."""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openai import OpenAI


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "moonshotai/kimi-k2.6"

SYSTEM_PROMPT = """You tailor a structured resume for one job.

Return exactly one JSON object. Do not emit Markdown or LaTeX. You may reorder,
select, combine, and persuasively rewrite source bullets, subject to the supplied
tailoring_policy. Every required_experience_id must be included. When
preserve_experience_order is true, experience_order must be a subsequence of the
source order. Preserve the underlying
employer, project, event, technology, metric, date, and scope. Language,
emphasis, professional motivation, and reasonable descriptions of purpose are
creative. Do not turn participation into leadership or add a new verifiable past
event.

Output shape:
{
  "experience_order": ["experience id"],
  "experiences": [{
    "id": "experience id",
    "bullets": [{
      "source_ids": ["one or more source bullet ids"],
      "segments": [{"text": "text", "style": "plain|bold"}]
    }]
  }],
  "project_order": ["project id"],
  "projects": [{
    "id": "project id",
    "bullets": [{
      "source_ids": ["one or more source bullet ids"],
      "segments": [{"text": "text", "style": "plain|bold"}]
    }]
  }],
  "skills": {"category": ["skill"]},
  "summary": "short explanation of the tailoring choices"
}

Use no more than two bold spans per bullet and never bold an entire bullet.
Prefer bolding metrics, concrete outcomes, or especially relevant technologies.
Respect every supplied bullet max_chars budget. Aim for one to two rendered
lines per bullet. Return only the JSON object."""


@dataclass(frozen=True)
class TailorResult:
    plan: dict[str, Any]
    model: str
    cost_usd: float


class TailorPlanError(ValueError):
    pass


def load_resume(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("resume JSON must be an object")
    for key in ("contact", "education", "experience", "projects", "skills"):
        if key not in value:
            raise ValueError(f"resume JSON missing {key!r}")
    return value


def _budgeted_resume(resume: dict[str, Any]) -> dict[str, Any]:
    value = json.loads(json.dumps(resume))
    for section in ("experience", "projects"):
        for entry in value.get(section) or []:
            total = 0
            for bullet in entry.get("bullets") or []:
                length = len(str(bullet.get("text") or ""))
                # Roughly two rendered lines in the 11pt Jake template. A
                # short source bullet should not force an unrealistically
                # small rewrite budget when useful context can still fit.
                maximum = max(170, min(190, round(length * 1.15)))
                bullet["target_chars"] = min(maximum, max(100, length))
                bullet["max_chars"] = maximum
                total += maximum
            entry["maximum_bullets"] = min(4, len(entry.get("bullets") or []))
            entry["total_character_budget"] = total
    return value


class StructuredTailorClient:
    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
    ) -> None:
        key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise RuntimeError("OPENROUTER_API_KEY not set")
        self.model = model
        self.client = OpenAI(
            base_url=OPENROUTER_BASE_URL,
            api_key=key,
            # A stalled upstream response must not hold an entire serial apply
            # batch indefinitely. One retry still covers transient provider
            # failures while keeping the operation bounded.
            timeout=90.0,
            max_retries=1,
        )

    def _call(self, messages: list[dict[str, Any]], max_tokens: int = 4000) -> TailorResult:
        response = self.client.chat.completions.create(
            model=self.model,
            temperature=0.25,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            extra_body={
                "usage": {"include": True},
                "reasoning": {"effort": "none"},
            },
            messages=messages,
        )
        raw = (response.choices[0].message.content or "{}").strip()
        plan = json.loads(raw)
        if not isinstance(plan, dict):
            raise TailorPlanError("Kimi returned non-object JSON")
        usage = response.usage
        return TailorResult(
            plan=plan,
            model=self.model,
            cost_usd=float(getattr(usage, "cost", 0.0) or 0.0),
        )

    def tailor(
        self,
        resume: dict[str, Any],
        *,
        company: str,
        role: str,
        description: str,
        job_facts: dict[str, Any] | None,
    ) -> TailorResult:
        payload = {
            "company": company,
            "role": role,
            "job_description": description[:18_000],
            "job_facts": job_facts or {},
            "source_resume": _budgeted_resume(resume),
        }
        return self._call(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ]
        )

    def shorten(
        self,
        plan: dict[str, Any],
        errors: list[str],
        resume: dict[str, Any] | None = None,
    ) -> TailorResult:
        prompt = {
            "instruction": (
                "Correct only the listed mechanical length/page-fit problems. "
                "Preserve all factual claims, IDs, ordering, and JSON shape."
            ),
            "errors": errors,
            "current_plan": plan,
        }
        if resume is not None:
            prompt["source_resume"] = _budgeted_resume(resume)
        return self._call(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
            max_tokens=3500,
        )


_LATEX_ESCAPE = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def escape_latex(value: Any) -> str:
    return "".join(_LATEX_ESCAPE.get(char, char) for char in str(value))


def _display_url(value: str) -> str:
    return re.sub(r"^https?://(?:www\.)?", "", value).rstrip("/")


def _entry_map(resume: dict[str, Any], section: str) -> dict[str, dict[str, Any]]:
    return {
        str(entry["id"]): entry
        for entry in resume.get(section) or []
        if isinstance(entry, dict) and entry.get("id")
    }


def _plan_map(plan: dict[str, Any], section: str) -> dict[str, dict[str, Any]]:
    return {
        str(entry["id"]): entry
        for entry in plan.get(section) or []
        if isinstance(entry, dict) and entry.get("id")
    }


def _source_bullets(entry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item["id"]): item
        for item in entry.get("bullets") or []
        if isinstance(item, dict) and item.get("id")
    }


def _experience_policy_errors(resume: dict[str, Any], plan: dict[str, Any]) -> list[str]:
    policy = resume.get("tailoring_policy") or {}
    source_order = [str(entry["id"]) for entry in resume.get("experience") or []]
    selected = [str(value) for value in plan.get("experience_order") or []]
    errors: list[str] = []
    if len(selected) != len(set(selected)):
        errors.append("experience_order contains duplicate IDs")
    required = [str(value) for value in policy.get("required_experience_ids") or []]
    missing = [value for value in required if value not in selected]
    if missing:
        errors.append(f"experience_order is missing required experience IDs: {missing}")
    if policy.get("preserve_experience_order"):
        expected = [value for value in source_order if value in selected]
        if selected != expected:
            errors.append(
                "experience_order must preserve source order; "
                f"expected {expected}, received {selected}"
            )
    return errors


def _render_segments(segments: Any) -> tuple[str, str]:
    if not isinstance(segments, list) or not segments:
        raise TailorPlanError("bullet has no styled segments")
    rendered: list[str] = []
    plain: list[str] = []
    bold_count = 0
    for segment in segments:
        if not isinstance(segment, dict):
            raise TailorPlanError("bullet segment must be an object")
        text = str(segment.get("text") or "")
        style = segment.get("style") or "plain"
        plain.append(text)
        escaped = escape_latex(text)
        if style == "bold":
            bold_count += 1
            rendered.append(r"\textbf{" + escaped + "}")
        elif style == "plain":
            rendered.append(escaped)
        else:
            raise TailorPlanError(f"unsupported segment style: {style!r}")
    if bold_count > 2:
        raise TailorPlanError("bullet contains more than two bold spans")
    return "".join(rendered), "".join(plain)


def _render_bullets(
    source_entry: dict[str, Any],
    planned_entry: dict[str, Any],
) -> tuple[str, list[str]]:
    sources = _source_bullets(source_entry)
    lines: list[str] = []
    errors: list[str] = []
    planned = planned_entry.get("bullets") or []
    if not planned:
        raise TailorPlanError(f"{source_entry['id']}: no bullets selected")
    maximum_bullets = min(4, len(source_entry.get("bullets") or []))
    if len(planned) > maximum_bullets:
        errors.append(
            f"{source_entry['id']}: {len(planned)} bullets exceeds {maximum_bullets}"
        )
    for index, bullet in enumerate(planned):
        source_ids = [str(value) for value in bullet.get("source_ids") or []]
        if not source_ids or any(value not in sources for value in source_ids):
            raise TailorPlanError(
                f"{source_entry['id']} bullet {index}: invalid source_ids {source_ids}"
            )
        rendered, plain = _render_segments(bullet.get("segments"))
        max_chars = max(
            max(170, min(190, round(len(str(sources[value].get("text") or "")) * 1.15)))
            for value in source_ids
        )
        # Character count only approximates rendered width. Give Kimi a small
        # tolerance and let the compiled one-page check be authoritative.
        # The compiled PDF is authoritative for layout. A few extra characters
        # should not trigger another paid repair call or block an application.
        effective_max = max_chars + 20
        if len(plain) > effective_max:
            errors.append(
                f"{source_entry['id']} bullet {index}: {len(plain)} characters; maximum {effective_max}"
            )
        lines.append(r"        \resumeItem{" + rendered + "}")
    return "\n".join(lines), errors


def render_latex(
    resume: dict[str, Any],
    plan: dict[str, Any],
    template_path: str | Path = "resume_template.tex",
) -> tuple[str, list[str]]:
    template = Path(template_path).read_text(encoding="utf-8")
    contact = resume.get("contact") or {}
    links: list[str] = []
    if contact.get("phone"):
        links.append(escape_latex(contact["phone"]))
    if contact.get("email"):
        email = escape_latex(contact["email"])
        links.append(r"\href{mailto:" + email + r"}{\underline{" + email + "}}")
    link_keys = ["linkedin", "github"]
    if contact.get("show_portfolio"):
        link_keys.append("portfolio")
    for key in link_keys:
        url = contact.get(key)
        if url:
            links.append(
                r"\href{" + escape_latex(url) + r"}{\underline{"
                + escape_latex(_display_url(str(url))) + "}}"
            )
    header = (
        "\\begin{center}\n"
        r"    \textbf{\Huge \scshape " + escape_latex(contact.get("name", "")) + r"} \\ \vspace{1pt}" + "\n"
        r"    \small " + r" $|$ ".join(links) + "\n"
        "\\end{center}"
    )

    education_lines = [r"\section{Education}", r"  \resumeSubHeadingListStart"]
    for entry in resume.get("education") or []:
        education_lines.extend(
            [
                r"    \resumeSubheading",
                "      {" + escape_latex(entry.get("institution", "")) + "}{" + escape_latex(entry.get("location", "")) + "}",
                "      {" + escape_latex(entry.get("degree", "")) + "}{" + escape_latex(entry.get("dates", "")) + "}",
            ]
        )
    education_lines.append(r"  \resumeSubHeadingListEnd")

    errors: list[str] = _experience_policy_errors(resume, plan)
    experience_sources = _entry_map(resume, "experience")
    experience_plans = _plan_map(plan, "experiences")
    experience_lines = [r"\section{Experience}", r"  \resumeSubHeadingListStart"]
    for entry_id in plan.get("experience_order") or []:
        entry_id = str(entry_id)
        if entry_id not in experience_sources or entry_id not in experience_plans:
            raise TailorPlanError(f"invalid experience id in order: {entry_id}")
        source = experience_sources[entry_id]
        bullets, bullet_errors = _render_bullets(source, experience_plans[entry_id])
        errors.extend(bullet_errors)
        experience_lines.extend(
            [
                r"    \resumeSubheading",
                "      {" + escape_latex(source.get("title", "")) + "}{" + escape_latex(source.get("dates", "")) + "}",
                "      {" + escape_latex(source.get("company", "")) + "}{" + escape_latex(source.get("location", "")) + "}",
                r"      \resumeItemListStart",
                bullets,
                r"      \resumeItemListEnd",
            ]
        )
    experience_lines.append(r"  \resumeSubHeadingListEnd")

    project_sources = _entry_map(resume, "projects")
    project_plans = _plan_map(plan, "projects")
    project_lines = [r"\section{Projects}", r"  \resumeSubHeadingListStart"]
    for entry_id in plan.get("project_order") or []:
        entry_id = str(entry_id)
        if entry_id not in project_sources or entry_id not in project_plans:
            raise TailorPlanError(f"invalid project id in order: {entry_id}")
        source = project_sources[entry_id]
        bullets, bullet_errors = _render_bullets(source, project_plans[entry_id])
        errors.extend(bullet_errors)
        name = escape_latex(source.get("name", ""))
        url = source.get("url")
        heading = (
            r"\textbf{\href{" + escape_latex(url) + "}{" + name + "}}"
            if url else r"\textbf{" + name + "}"
        )
        technologies = ", ".join(str(value) for value in source.get("technologies") or [])
        if technologies:
            heading += r" $|$ \emph{" + escape_latex(technologies) + "}"
        project_lines.extend(
            [
                r"    \resumeProjectHeading",
                "      {" + heading + "}{" + escape_latex(source.get("dates", "")) + "}",
                r"      \resumeItemListStart",
                bullets,
                r"      \resumeItemListEnd",
            ]
        )
    project_lines.append(r"  \resumeSubHeadingListEnd")

    skills = plan.get("skills") if isinstance(plan.get("skills"), dict) else resume.get("skills", {})
    skill_lines = [r"\section{Technical Skills}", r" \begin{itemize}[leftmargin=0.15in, label={}]", r"  \small{\item{"]
    for category, values in skills.items():
        skill_lines.append(
            r"   \textbf{" + escape_latex(category) + "}{: "
            + escape_latex(", ".join(str(value) for value in values)) + r"} \\"
        )
    skill_lines.extend([r"  }}", r" \end{itemize}"])

    replacements = {
        "%%APPLYD_HEADER%%": header,
        "%%APPLYD_EDUCATION%%": "\n".join(education_lines),
        "%%APPLYD_EXPERIENCE%%": "\n".join(experience_lines),
        "%%APPLYD_PROJECTS%%": "\n".join(project_lines),
        "%%APPLYD_SKILLS%%": "\n".join(skill_lines),
    }
    rendered = template
    for marker, value in replacements.items():
        rendered = rendered.replace(marker, value)
    return rendered, errors


def pdf_page_count(path: str | Path) -> int:
    result = subprocess.run(
        ["pdfinfo", str(path)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pdfinfo failed: {result.stderr[-300:]}")
    match = re.search(r"^Pages:\s+(\d+)$", result.stdout, re.MULTILINE)
    if not match:
        raise RuntimeError("pdfinfo did not report a page count")
    return int(match.group(1))
