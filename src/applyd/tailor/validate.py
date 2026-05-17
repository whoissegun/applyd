from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _candidate_name_from_master(master_text: str) -> str | None:
    """Heuristic: the first non-empty line of a resume's plain text is almost
    always the candidate's name. Used to guard against the tailor renaming the
    header.
    """
    for line in master_text.splitlines():
        s = line.strip()
        if s and not s.startswith(("http", "mailto:", "+", "(")):
            # cheap "looks like a name" filter — 2-5 tokens, mostly letters
            tokens = s.split()
            if 1 < len(tokens) <= 5 and all(t.replace(".", "").replace("-", "").isalpha() for t in tokens):
                return s
    return None


def validate(master_text: str, tailored_tex: str) -> ValidationResult:
    """Sanity checks on the tailored LaTeX. Master is plain text, so we can't
    do a structural set-diff anymore; instead we check:
      - tailored compiles structurally (preamble + end)
      - braces balance
      - the candidate's name from the master still appears in tailored
      - bullets aren't absurdly long
    """
    errors: list[str] = []
    warnings: list[str] = []

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

    name = _candidate_name_from_master(master_text)
    if name and name not in tailored_tex:
        errors.append(f"candidate name missing from tailored output: {name!r}")

    for m in re.finditer(r"\\resumeItem\s*\{([^{}]|\{[^{}]*\})*?\}", tailored_tex, re.DOTALL):
        bullet = m.group(0)
        if len(bullet) > 220:
            warnings.append(f"long bullet ({len(bullet)} chars): {bullet[:80]}...")

    return ValidationResult(ok=len(errors) == 0, errors=errors, warnings=warnings)
