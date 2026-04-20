from __future__ import annotations

import re
from dataclasses import dataclass, field


SUBHEADING_RE = re.compile(
    r"\\resumeSubheading\s*"
    r"\{([^{}]*)\}\s*\{([^{}]*)\}\s*\{([^{}]*)\}\s*\{([^{}]*)\}",
    re.DOTALL,
)


def _section_body(tex: str, section_name: str) -> str:
    """Return the text between \\section{section_name} and the next \\section or \\end{document}."""
    pattern = re.compile(
        r"\\section\s*\{"
        + re.escape(section_name)
        + r"\}(.*?)(?=\\section\s*\{|\\end\{document\})",
        re.DOTALL,
    )
    m = pattern.search(tex)
    return m.group(1) if m else ""


def extract_experience_companies(tex: str) -> list[str]:
    """For Jake's template, Experience \\resumeSubheading is {role}{dates}{company}{loc}."""
    body = _section_body(tex, "Experience")
    return [m.group(3).strip() for m in SUBHEADING_RE.finditer(body)]


def extract_education(tex: str) -> list[tuple[str, str, str]]:
    """For Education, \\resumeSubheading is {institution}{loc}{degree}{dates}.
    Returns list of (institution, degree, dates)."""
    body = _section_body(tex, "Education")
    out = []
    for m in SUBHEADING_RE.finditer(body):
        out.append((m.group(1).strip(), m.group(3).strip(), m.group(4).strip()))
    return out


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def validate(base_tex: str, tailored_tex: str) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []

    # Basic LaTeX sanity
    if "\\documentclass" not in tailored_tex:
        errors.append("missing \\documentclass")
    if "\\end{document}" not in tailored_tex:
        errors.append("missing \\end{document}")

    open_braces = tailored_tex.count("{")
    close_braces = tailored_tex.count("}")
    if open_braces != close_braces:
        errors.append(
            f"unbalanced braces: {open_braces} '{{' vs {close_braces} '}}'"
        )

    # Company preservation
    base_companies = extract_experience_companies(base_tex)
    tailored_companies = extract_experience_companies(tailored_tex)
    base_set = set(base_companies)
    tailored_set = set(tailored_companies)

    new_companies = tailored_set - base_set
    if new_companies:
        errors.append(f"new companies invented: {sorted(new_companies)}")

    dropped_companies = base_set - tailored_set
    if dropped_companies:
        errors.append(f"companies dropped from experience: {sorted(dropped_companies)}")

    # Education preservation (exact match of {institution, degree, dates} triples)
    base_edu = set(extract_education(base_tex))
    tailored_edu = set(extract_education(tailored_tex))
    if base_edu != tailored_edu:
        if base_edu - tailored_edu:
            errors.append(f"education missing: {sorted(base_edu - tailored_edu)}")
        if tailored_edu - base_edu:
            errors.append(f"education invented/changed: {sorted(tailored_edu - base_edu)}")

    # Header preservation (name, email — quick sanity)
    for needle in ["Divine Jojolola", "jojololadivine05@gmail.com"]:
        if needle not in tailored_tex:
            errors.append(f"header field missing: {needle!r}")

    # Soft bullet-length check (warn only)
    for m in re.finditer(r"\\resumeItem\s*\{([^{}]|\{[^{}]*\})*?\}", tailored_tex, re.DOTALL):
        bullet = m.group(0)
        if len(bullet) > 220:  # rough upper bound allowing for \textbf{} noise
            warnings.append(f"long bullet ({len(bullet)} chars): {bullet[:80]}...")

    return ValidationResult(ok=len(errors) == 0, errors=errors, warnings=warnings)
