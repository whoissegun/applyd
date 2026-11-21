"""Deterministic policy evaluation over model-extracted job facts."""
from __future__ import annotations

import re
from datetime import date
from dataclasses import dataclass
from typing import Any

from .models import Job


@dataclass(frozen=True)
class EligibilityResult:
    decision: str
    reasons: list[dict[str, Any]]


_US_LOCATION = re.compile(
    r"\b(?:united states|u\.?s\.?a?|(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)),?\b",
    re.IGNORECASE,
)
_CA_LOCATION = re.compile(
    r"\b(?:canada|ontario|quebec|british columbia|alberta|manitoba|nova scotia|new brunswick|saskatchewan|newfoundland|ottawa|toronto|vancouver|montreal)\b",
    re.IGNORECASE,
)
_GB_LOCATION = re.compile(
    r"\b(?:united kingdom|u\.?k\.?|england|scotland|wales|london|manchester|edinburgh)\b",
    re.IGNORECASE,
)

_EU_COUNTRY_CODES = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DE", "DK", "EE", "ES", "FI",
    "FR", "GR", "HU", "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PL",
    "PT", "RO", "SE", "SI", "SK",
}

_HUMAN_LANGUAGES = {
    "arabic", "cantonese", "chinese", "dutch", "english", "french",
    "german", "greek", "hebrew", "hindi", "italian", "japanese",
    "korean", "mandarin", "polish", "portuguese", "punjabi", "russian",
    "spanish", "swedish", "tamil", "turkish", "ukrainian", "urdu",
}
_LANGUAGE_ROLE = re.compile(
    r"\b(?:language|linguist|translator|translation|interpreter|localization)\b",
    re.IGNORECASE,
)


def _fact(facts: dict[str, Any], name: str, default: Any = None) -> Any:
    field = facts.get(name)
    return field.get("value", default) if isinstance(field, dict) else default


def _evidence(facts: dict[str, Any], name: str) -> str | None:
    field = facts.get(name)
    if not isinstance(field, dict):
        return None
    value = field.get("evidence")
    return str(value) if value else None


def infer_country_codes(job: Job, fact: dict[str, Any] | None = None) -> list[str]:
    if fact:
        explicit = [str(item).upper() for item in fact.get("countries") or []]
        if explicit:
            return explicit
    text = " ".join(job.locations)
    countries: list[str] = []
    if _US_LOCATION.search(text):
        countries.append("US")
    if _CA_LOCATION.search(text):
        countries.append("CA")
    if _GB_LOCATION.search(text):
        countries.append("GB")
    return countries


def _work_authorization(profile: dict[str, Any], country: str) -> Any:
    authorization = profile.get("work_authorization") or {}
    direct = authorization.get(country)
    if isinstance(direct, dict):
        return direct
    if country in _EU_COUNTRY_CODES:
        regional = authorization.get("EU")
        if isinstance(regional, dict):
            return regional
    return None


def academic_policy_reasons(job: Job, facts: dict[str, Any], profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Exclude required postgraduate education, not preferred degrees or alternatives."""
    excluded = set((profile.get("preferences") or {}).get("excluded_required_education") or [])
    education = facts.get("education") or {}
    if not isinstance(education, dict):
        return []
    level = {"masters_required": "masters", "doctorate_required": "doctorate"}.get(education.get("value"))
    if level not in excluded:
        return []
    evidence = str(education.get("evidence") or "").strip()
    normalize = lambda value: " ".join(re.sub(r"[^a-z0-9]+", " ", value.casefold()).split())
    # Do not trust a stale boolean flag alone: recheck the actual source quote.
    source = f"{job.title}\n{job.description or ''}"
    if not evidence or " ".join(evidence.casefold().split()) not in " ".join(source.casefold().split()):
        return []
    text = normalize(evidence)
    degree_pattern = (r"\b(?:master(?: s|s)?|m s|ms(?! excel)|m sc|msc|m eng|meng|mba|m b a)\b"
                      if level == "masters" else r"\b(?:ph ?d|doctorate|doctoral|doctor of philosophy|dphil)\b")
    if (re.search(degree_pattern + r"(?:(?!\b(?:required|must|minimum|essential)\b).){0,60}\b(?:preferred|desired|desirable|a plus)\b", text)
            or re.search(r"\b(?:preferred|desired|desirable)\b.{0,30}" + degree_pattern, text)):
        return []
    bachelor = r"\b(?:bachelor(?: s|s)?|b s|b a|bs|ba|bsc|beng|undergraduate)\b"
    advanced = r"\b(?:master(?: s|s)?|m s|ms|m sc|msc|m eng|meng|mba|m b a|ph ?d|doctorate|doctoral|doctor of philosophy|dphil)\b"
    if re.search(bachelor, text):
        alternatives = (re.search(bachelor + r".{0,100}\bor\b.{0,40}" + advanced, text)
                        or re.search(advanced + r".{0,100}\bor\b.{0,40}" + bachelor, text)
                        or '/' in evidence)
        if alternatives or not re.search(advanced, text) or not re.search(
            r"\b(?:required|must|minimum|need|at least|essential|and)\b", text
        ):
            return []
    if re.search(r"\bor\b.{0,30}\bequivalent\b.{0,30}\bexperience\b", text):
        return []
    if re.search(r"\bor\s+(?:an?\s+)?early career\s+(?:researcher|engineer)", text):
        return []
    # 'Preferably from a top university' qualifies the institution, not the
    # required degree. Only neutralize preference language about the degree.
    if re.search(r"\b(?:preferred|desirable|nice to have|bonus|a plus)\b", text) and not re.search(
        r"\b(?:required|must|minimum|need|at least|essential)\b", text
    ):
        return []
    if not re.search(degree_pattern, text):
        return []
    # Catch a shortened model quote such as "Master's degree" when the source
    # immediately continues with "preferred".
    flat_source = " ".join(source.casefold().split())
    quote = " ".join(evidence.casefold().split())
    end = flat_source.find(quote) + len(quote)
    if re.match(r"\s*(?:is\s+|are\s+)?(?:strongly\s+)?(?:preferred|desirable)\b", flat_source[end:]):
        return []
    return [{"code": "required_education_excluded", "job_value": level,
             "job_evidence": evidence, "user_rule": sorted(excluded),
             "user_fact": profile.get("academic_status") or profile.get("degree")}]


def _company_key(name: Any) -> str:
    words = re.sub(r"[^a-z0-9]+", " ", str(name or "").casefold()).split()
    while len(words) > 1 and words[-1] in {"inc", "llc", "ltd", "corp", "ai", "labs", "technologies"}:
        words.pop()
    return " ".join(words)


def company_exclusion_match(company: str, profile: dict[str, Any], today: date | None = None) -> str | None:
    """Return the matching rule when the user excluded this company.

    Entries in employment_preferences.excluded_companies are a company name or
    {"name": ..., "until": "YYYY-MM-DD", "reason": ...}; an expired ``until``
    no longer excludes (e.g. a per-company application cap that resets).
    """
    prefs = profile.get("employment_preferences") or {}
    rules = prefs.get("excluded_companies") if isinstance(prefs, dict) else None
    target = _company_key(company)
    today = today or date.today()
    for rule in rules or []:
        name, until = (rule.get("name"), rule.get("until")) if isinstance(rule, dict) else (rule, None)
        if not name or _company_key(name) != target:
            continue
        if until:
            try:
                if date.fromisoformat(str(until)) < today:
                    continue
            except ValueError:
                pass
        return str(name) + (f" (until {until})" if until else "")
    return None


def title_exclusion_match(title: str, profile: dict[str, Any]) -> tuple[str, str] | None:
    """Return (pattern, matched text) for the first user title exclusion hit.

    Title exclusions need no extracted facts, so enrichment calls this before
    paying for retrieval or Kimi extraction.
    """
    preferences = profile.get("preferences") or {}
    for pattern in preferences.get("exclude_title_patterns") or []:
        try:
            matched = re.search(str(pattern), title or "", re.IGNORECASE)
        except re.error:
            continue
        if matched:
            return str(pattern), matched.group(0)
    return None


def evaluate_job(
    job: Job,
    facts: dict[str, Any],
    profile: dict[str, Any],
) -> EligibilityResult:
    preferences = profile.get("preferences") or {}
    blockers: list[dict[str, Any]] = academic_policy_reasons(job, facts, profile)
    uncertainties: list[dict[str, Any]] = []

    role_family_fact = facts.get("role_family") or {}
    role_family = _fact(facts, "role_family", "unknown")
    excluded_role_families = {
        str(value).strip().casefold()
        for value in preferences.get("excluded_role_families") or []
        if str(value).strip()
    }
    if (
        role_family != "unknown"
        and str(role_family).casefold() in excluded_role_families
        and isinstance(role_family_fact, dict)
        and role_family_fact.get("evidence_grounded") is True
    ):
        blockers.append(
            {
                "code": "role_family_excluded",
                "job_evidence": _evidence(facts, "role_family"),
                "job_value": role_family,
                "user_rule": sorted(excluded_role_families),
            }
        )

    # Titles such as "French Language Specialist" are themselves authoritative
    # evidence of the required spoken language. Keep this deliberately narrow so
    # programming-language roles and generic NLP titles are not excluded.
    title_words = {
        word.casefold() for word in re.findall(r"[A-Za-z]+", job.title)
    }
    required_title_languages = sorted(title_words.intersection(_HUMAN_LANGUAGES))
    spoken_languages = {
        str(value).strip().casefold()
        for value in profile.get("spoken_languages") or []
        if str(value).strip()
    }
    if (
        required_title_languages
        and _LANGUAGE_ROLE.search(job.title)
        and not set(required_title_languages).intersection(spoken_languages)
    ):
        blockers.append(
            {
                "code": "spoken_language_required_title",
                "job_evidence": job.title,
                "required": required_title_languages,
                "user_fact": sorted(spoken_languages),
            }
        )

    company_rule = company_exclusion_match(job.company, profile)
    if company_rule:
        blockers.append({
            "code": "company_excluded",
            "job_evidence": job.company,
            "user_rule": company_rule,
        })

    title_match = title_exclusion_match(job.title, profile)
    if title_match:
        blockers.append(
            {
                "code": "title_excluded",
                "job_evidence": title_match[1],
                "user_rule": title_match[0],
            }
        )

    seniority = _fact(facts, "seniority", "unknown")
    allowed = preferences.get("allowed_seniority")
    if isinstance(allowed, list) and seniority != "unknown" and seniority not in allowed:
        blockers.append(
            {
                "code": "seniority_excluded",
                "job_evidence": _evidence(facts, "seniority"),
                "job_value": seniority,
                "user_rule": allowed,
            }
        )
    if preferences.get("allow_internships") is False and seniority == "intern":
        blockers.append(
            {
                "code": "internships_excluded",
                "job_evidence": _evidence(facts, "seniority"),
            }
        )

    clearance = _fact(facts, "clearance", "not_stated")
    if clearance == "active_required":
        held = [str(value).casefold() for value in profile.get("security_clearances") or []]
        required_level = str((facts.get("clearance") or {}).get("level") or "").casefold()
        if not held or (required_level and not any(required_level in value for value in held)):
            blockers.append(
                {
                    "code": "active_clearance_required",
                    "job_evidence": _evidence(facts, "clearance"),
                    "user_fact": held or "no active clearance recorded",
                }
            )

    citizenship = facts.get("citizenship") or {}
    if citizenship.get("value") == "required":
        required_countries = infer_country_codes(job, citizenship)
        held_citizenships = {
            str(value).upper() for value in profile.get("citizenships") or []
        }
        if required_countries and held_citizenships:
            if not held_citizenships.intersection(required_countries):
                blockers.append(
                    {
                        "code": "citizenship_required",
                        "job_evidence": citizenship.get("evidence"),
                        "required": required_countries,
                        "user_fact": sorted(held_citizenships),
                    }
                )
        elif not held_citizenships:
            uncertainties.append(
                {
                    "code": "citizenship_missing_from_profile",
                    "job_evidence": citizenship.get("evidence"),
                }
            )

    sponsorship = facts.get("sponsorship") or {}
    if sponsorship.get("value") == "unavailable":
        countries = infer_country_codes(job, sponsorship)
        for country in countries:
            status = _work_authorization(profile, country)
            if not isinstance(status, dict):
                uncertainties.append(
                    {
                        "code": "work_authorization_missing",
                        "country": country,
                        "job_evidence": sponsorship.get("evidence"),
                    }
                )
                continue
            if not status.get("authorized") or status.get("requires_sponsorship"):
                blockers.append(
                    {
                        "code": "sponsorship_unavailable",
                        "country": country,
                        "job_evidence": sponsorship.get("evidence"),
                        "user_fact": status,
                    }
                )

    required_years = _fact(facts, "minimum_years_experience")
    known_years = profile.get("years_professional_experience")
    if isinstance(required_years, int) and isinstance(known_years, (int, float)):
        if known_years < required_years:
            blockers.append(
                {
                    "code": "minimum_experience_not_met",
                    "job_evidence": _evidence(facts, "minimum_years_experience"),
                    "required": required_years,
                    "user_fact": known_years,
                }
            )

    if blockers:
        return EligibilityResult("ineligible", blockers + uncertainties)
    unknown_policy = str(preferences.get("unknown_policy") or "eligible")
    if uncertainties and unknown_policy == "review":
        return EligibilityResult("uncertain", uncertainties)
    return EligibilityResult("eligible", uncertainties)
