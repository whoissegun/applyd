"""Deterministically convert a Jake-style LaTeX resume to canonical JSON."""
from __future__ import annotations

import re
from typing import Iterator


def _group(text: str, start: int) -> tuple[str, int]:
    while start < len(text) and text[start].isspace():
        start += 1
    if start >= len(text) or text[start] != "{":
        raise ValueError(f"expected '{{' at offset {start}")
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{" and (index == 0 or text[index - 1] != "\\"):
            depth += 1
        elif text[index] == "}" and (index == 0 or text[index - 1] != "\\"):
            depth -= 1
            if depth == 0:
                return text[start + 1 : index], index + 1
    raise ValueError("unbalanced LaTeX group")


def _commands(text: str, command: str, nargs: int) -> Iterator[tuple[int, int, list[str]]]:
    needle = "\\" + command
    offset = 0
    while True:
        start = text.find(needle, offset)
        if start == -1:
            return
        cursor = start + len(needle)
        args: list[str] = []
        try:
            for _ in range(nargs):
                value, cursor = _group(text, cursor)
                args.append(value)
        except ValueError:
            offset = start + len(needle)
            continue
        yield start, cursor, args
        offset = cursor


def _section(tex: str, name: str) -> str:
    marker = f"\\section{{{name}}}"
    start = tex.find(marker)
    if start == -1:
        return ""
    end = tex.find("\\section{", start + len(marker))
    if end == -1:
        end = tex.find("\\end{document}", start)
    return tex[start:end]


def _plain(value: str) -> str:
    text = value
    href = re.compile(r"\\href\{[^{}]*\}\{([^{}]*)\}")
    simple = re.compile(r"\\(?:textbf|emph|underline|textit)\{([^{}]*)\}")
    for _ in range(8):
        updated = href.sub(r"\1", text)
        updated = simple.sub(r"\1", updated)
        if updated == text:
            break
        text = updated
    text = text.replace(r"\%", "%").replace(r"\&", "&").replace("~", " ")
    text = re.sub(r"\\[A-Za-z]+", "", text)
    text = text.replace("{", "").replace("}", "")
    return re.sub(r"\s+", " ", text).strip()


def _slug(*parts: str) -> str:
    value = "-".join(parts).casefold()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value or "entry"


def _unique_id(base: str, seen: dict[str, int]) -> str:
    """Keep IDs stable when unrelated entries are inserted or reordered."""
    seen[base] = seen.get(base, 0) + 1
    return base if seen[base] == 1 else f"{base}-{seen[base]}"


def _bullets(block: str, prefix: str) -> list[dict[str, str]]:
    return [
        {"id": f"{prefix}-{index}", "text": _plain(args[0])}
        for index, (_, _, args) in enumerate(_commands(block, "resumeItem", 1), start=1)
    ]


def convert_resume_tex(tex: str, profile: dict) -> dict:
    """Return canonical resume data without asking a model to reinterpret facts."""
    education: list[dict] = []
    education_ids: dict[str, int] = {}
    for _, _, args in _commands(_section(tex, "Education"), "resumeSubheading", 4):
        education.append({
            "id": _unique_id(_slug(args[0], args[2]), education_ids),
            "institution": _plain(args[0]), "location": _plain(args[1]),
            "degree": _plain(args[2]), "dates": _plain(args[3]),
        })

    experience_block = _section(tex, "Experience")
    headings = list(_commands(experience_block, "resumeSubheading", 4))
    experience: list[dict] = []
    experience_ids: dict[str, int] = {}
    for index, (_, end, args) in enumerate(headings):
        stop = headings[index + 1][0] if index + 1 < len(headings) else len(experience_block)
        entry_id = _unique_id(_slug(args[2], args[0]), experience_ids)
        experience.append({
            "id": entry_id, "title": _plain(args[0]), "dates": _plain(args[1]),
            "company": _plain(args[2]), "location": _plain(args[3]),
            "bullets": _bullets(experience_block[end:stop], entry_id),
        })

    project_block = _section(tex, "Projects")
    headings = list(_commands(project_block, "resumeProjectHeading", 2))
    projects: list[dict] = []
    project_ids: dict[str, int] = {}
    for index, (_, end, args) in enumerate(headings):
        stop = headings[index + 1][0] if index + 1 < len(headings) else len(project_block)
        url_match = re.search(r"\\href\{([^{}]+)\}\{([^{}]+)\}", args[0])
        tech_match = re.search(r"\\emph\{([^{}]+)\}", args[0])
        name = _plain(url_match.group(2) if url_match else args[0].split("$|$")[0])
        entry_id = _unique_id(_slug(name), project_ids)
        projects.append({
            "id": entry_id, "name": name,
            "url": url_match.group(1) if url_match else None,
            "technologies": [item.strip() for item in (tech_match.group(1) if tech_match else "").split(",") if item.strip()],
            "dates": _plain(args[1]),
            "bullets": _bullets(project_block[end:stop], entry_id),
        })

    skills: dict[str, list[str]] = {}
    for category, values in re.findall(
        r"\\textbf\{([^{}]+)\}\{:\s*([^{}\\]+)\}", _section(tex, "Technical Skills")
    ):
        skills[_plain(category)] = [item.strip() for item in _plain(values).split(",")]

    tailoring_policy = {
        "required_experience_ids": [experience[0]["id"]] if experience else [],
        "preserve_experience_order": True,
    }
    return {
        "contact": {
            "name": profile.get("full_name") or " ".join(filter(None, [profile.get("first_name"), profile.get("last_name")])),
            "phone": profile.get("phone"), "email": profile.get("email"),
            "linkedin": profile.get("linkedin_url"), "github": profile.get("github_url"),
            "portfolio": profile.get("portfolio_url"),
        },
        "education": education, "experience": experience, "projects": projects, "skills": skills,
        "tailoring_policy": tailoring_policy,
    }
