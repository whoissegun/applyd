"""Deterministic option equivalence shared by every dropdown/radio binder.

ATS forms phrase the same answer many ways ("I am not a protected veteran"
vs "I am not a veteran", "No" vs "I have never worked for a client"). These
helpers map a profile value and each rendered option to a conservative shared
meaning, so binders compare meanings instead of strings. Every caller still
requires a *unique* match; ambiguity stays review.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Iterable, Sequence

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
# Months each season label covers (academic-calendar convention).
_SEASONS = {
    "winter": (1, 2), "spring": (3, 4, 5), "summer": (6, 7, 8),
    "fall": (9, 10, 11, 12), "autumn": (9, 10, 11, 12),
}
_CA_PROVINCES = {
    "ontario", "quebec", "british columbia", "alberta", "manitoba",
    "saskatchewan", "nova scotia", "new brunswick", "newfoundland and labrador",
    "prince edward island", "yukon", "nunavut", "northwest territories",
}
_COUNTRIES = {
    "canada", "nigeria", "united kingdom", "india", "mexico", "germany",
    "france", "ireland", "australia", "china", "brazil",
}
# Options about legal status never take the loose sentence-polarity route.
_LEGAL = re.compile(
    r"\b(?:authori[sz]|sponsor|visa|citizen|permanent resident|green card|"
    r"work permit|immigra|clearance|export)", re.I)
_NEGATION = re.compile(
    r"\b(?:not|never|no|none|neither|don t|do not|didn t|did not|haven t|have not)\b")


def norm(value: object) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).split())


def semantic(value: object) -> str | None:
    """Return a shared meaning for a label/profile value, or None."""
    text = norm(value)
    if not text:
        return None
    if "veteran" in text:
        if re.search(r"\b(?:not|no)\b", text):
            return "veteran:no"
        if re.search(r"\b(?:decline|prefer not|don t wish|do not wish)\b", text):
            return "neutral:decline"
        return "veteran:yes"
    if "disabilit" in text:
        if re.search(r"\b(?:prefer not|decline|don t wish|do not wish|not to (?:answer|share|say))\b", text):
            return "neutral:decline"
        if re.search(r"\b(?:do not|don t|no)\b.*\bhave\b|^no\b", text):
            return "disability:no"
        if re.search(r"\b(?:yes|i have|i do have)\b", text):
            return "disability:yes"
    if any(phrase in text for phrase in (
        "not in the u s", "not in the us", "outside the u s", "outside the us",
        "outside of the u s", "outside of the us", "non u s", "i do not live in the u",
        "not a u s resident", "not located in the u",
    )):
        return "location:outside-us"
    if text in _CA_PROVINCES:
        return "location:outside-us-province"
    if any(phrase in text for phrase in (
        "prefer not to say", "prefer not to answer", "decline to self identify",
        "decline to answer", "do not wish to answer", "don t wish to answer",
        "i don t wish to", "i do not wish to",
    )):
        return "neutral:decline"
    if re.match(r"^(?:yes|true)\b", text) or re.match(r"^i (?:agree|consent|accept|acknowledge)\b", text):
        return "boolean:yes"
    if re.match(r"^(?:no|false)\b", text):
        return "boolean:no"
    return None


def sentence_polarity(label: object) -> bool | None:
    """Yes/no meaning of a first-person sentence option, never for legal text.

    "I have never worked for an X client" -> False,
    "I currently work for an X client"     -> True.
    """
    raw = str(label or "")
    if _LEGAL.search(raw) or semantic(raw) == "neutral:decline":
        return None
    text = norm(raw)
    if not re.match(r"^(?:i|i m|i am|i have|i ve|i do|i did|i currently|i previously|yes|no)\b", text):
        return None
    head = " ".join(text.split()[:6])
    return False if _NEGATION.search(head) else True


def bind_boolean(labels: dict[str, str], value: bool) -> str | None:
    """Unique option whose meaning is ``value`` (yes/no or sentence form)."""
    direct = [k for k, label in labels.items()
              if semantic(label) == ("boolean:yes" if value else "boolean:no")]
    if len(direct) == 1:
        return direct[0]
    if direct:
        return None
    polar = [k for k, label in labels.items() if sentence_polarity(label) is value]
    return polar[0] if len(polar) == 1 else None


def bind_semantic(labels: dict[str, str], value: object) -> str | None:
    """Unique option sharing the profile value's meaning."""
    wanted = semantic(value)
    if wanted == "location:outside-us-province":
        wanted = "location:outside-us"
    if not wanted:
        return None
    if wanted in {"boolean:yes", "boolean:no"}:
        return bind_boolean(labels, wanted == "boolean:yes")
    matches = [k for k, label in labels.items() if semantic(label) == wanted]
    return matches[0] if len(matches) == 1 else None


# ── dates ──────────────────────────────────────────────────────────────────

def _point(text: str) -> tuple[int, tuple[int, ...]] | None:
    """One date expression: "2027 04", "april 2027", "apr 27", "spring 2027"."""
    iso = re.fullmatch(r"(\d{4}) (\d{1,2})(?: \d{1,2})?", text)
    if iso and 1 <= int(iso.group(2)) <= 12:
        return int(iso.group(1)), (int(iso.group(2)),)
    year_match = re.search(r"\b(20\d{2})\b", text) or re.search(r"\b(\d{2})\b$", text)
    if not year_match:
        return None
    year = int(year_match.group(1))
    if year < 100:
        year += 2000
    words = text.split()
    months = [_MONTHS[w] for w in words if w in _MONTHS]
    seasons = [w for w in words if w in _SEASONS]
    if len(months) == 1 and not seasons:
        return year, (months[0],)
    if len(seasons) == 1 and not months:
        return year, _SEASONS[seasons[0]]
    return None


def parse_months(value: object) -> frozenset[tuple[int, int]] | None:
    """Set of (year, month) a label covers.

    Handles single months, seasons, and ranges such as "January 2027 - July
    2027", "April to July 2027" and "December 2026 - August 2027".
    """
    text = norm(value)
    if not text:
        return None
    # Open-ended labels ("Before September 2026", "September 2027 or later")
    # are not a bounded month set; never treat them as one month.
    if re.search(r"\b(?:before|prior to|after|earlier|later|onwards?|beyond)\b", text):
        return None
    point = _point(text)
    if point:
        return frozenset((point[0], m) for m in point[1])
    range_match = re.fullmatch(
        r"(?:from )?([a-z]+)(?: (\d{4}))? (?:to|through|until|thru) ([a-z]+) (\d{4})",
        text) or re.fullmatch(r"([a-z]+)(?: (\d{4}))? ([a-z]+) (\d{4})", text)
    if range_match:
        m1, y1, m2, y2 = range_match.groups()
        if m1 in _MONTHS and m2 in _MONTHS:
            end_year, end_month = int(y2), _MONTHS[m2]
            start_month = _MONTHS[m1]
            start_year = int(y1) if y1 else (end_year if start_month <= end_month else end_year - 1)
            covered = set()
            year, month = start_year, start_month
            while (year, month) <= (end_year, end_month) and len(covered) < 60:
                covered.add((year, month))
                year, month = (year + 1, 1) if month == 12 else (year, month + 1)
            return frozenset(covered) if covered else None
    return None


def parse_month(value: object) -> tuple[int, tuple[int, ...]] | None:
    """Single-year view of parse_months (kept for callers and tests)."""
    covered = parse_months(value)
    if not covered or len({y for y, _ in covered}) != 1:
        return None
    year = next(iter(covered))[0]
    return year, tuple(sorted(m for _, m in covered))


def _first_day(year: int, month: int) -> date:
    return date(year, month, 1)


def bind_same_month(labels: dict[str, str], value: object) -> str | None:
    """Unique option covering the same month (graduation must be truthful).

    The narrowest covering option wins ("April 2027" over "Spring 2027"
    over "January 2027 - July 2027"); a tie at that width is ambiguous.
    """
    wanted = parse_months(value)
    if not wanted or len(wanted) != 1:
        return None
    target = next(iter(wanted))
    matches = []
    for key, label in labels.items():
        covered = parse_months(label)
        if covered and target in covered:
            matches.append((len(covered), key))
    if not matches:
        return None
    matches.sort()
    narrowest = [key for size, key in matches if size == matches[0][0]]
    return narrowest[0] if len(narrowest) == 1 else None


def bind_nearest_month(labels: dict[str, str], anchor: object) -> str | None:
    """Option closest to ``anchor``, preferring options on or after it.

    Used for target start dates, which are a preference anchored on the
    graduation month rather than a historical fact.
    """
    wanted = parse_months(anchor)
    if not wanted or len(wanted) != 1:
        return None
    target = _first_day(*next(iter(wanted)))
    candidates: list[tuple[int, int, str]] = []
    for key, label in labels.items():
        covered = parse_months(label)
        if not covered:
            continue
        starts = [_first_day(y, m) for y, m in covered]
        # An option covering the anchor month counts as distance zero.
        if target in starts:
            distance, before = 0, 0
        else:
            after = [d for d in starts if d >= target]
            nearest = min(after) if after else max(starts)
            distance = abs((nearest.year - target.year) * 12 + nearest.month - target.month)
            before = 0 if nearest >= target else 1
        candidates.append((before, distance, key))
    if not candidates:
        return None
    candidates.sort()
    best = candidates[0]
    ties = [c for c in candidates if c[:2] == best[:2]]
    return best[2] if len(ties) == 1 else None


def bind_outside_option(labels: dict[str, str], value: object) -> str | None:
    """"Outside of United States and Costa Rica" for a country not listed."""
    wanted = norm(value)
    if wanted not in _COUNTRIES and wanted not in _CA_PROVINCES:
        return None
    if any(norm(label) == wanted for label in labels.values()):
        return None
    outside = [k for k, label in labels.items()
               if re.match(r"^(?:outside|other than|not in)\b", norm(label))
               and f" {wanted} " not in f" {norm(label)} "]
    # Only for country/region pickers: every other option is a place name.
    return outside[0] if len(outside) == 1 else None


def mostly_dates(labels: Iterable[str]) -> bool:
    values = [label for label in labels if norm(label) and not norm(label).startswith("select")]
    parsed = [label for label in values if parse_months(label)]
    return bool(values) and len(parsed) >= max(1, len(values) - 1)


def labels_of(options: Sequence[dict[str, str]]) -> dict[str, str]:
    return {str(i): str(o.get("text", "")) for i, o in enumerate(options)}
