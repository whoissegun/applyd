"""Deterministic policy evaluation over model-extracted job facts."""
from __future__ import annotations

import re
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


def evaluate_job(
    job: Job,
    facts: dict[str, Any],
    profile: dict[str, Any],
) -> EligibilityResult:
    preferences = profile.get("preferences") or {}
    blockers: list[dict[str, Any]] = []
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

    for pattern in preferences.get("exclude_title_patterns") or []:
        try:
            matched = re.search(str(pattern), job.title, re.IGNORECASE)
        except re.error:
            continue
        if matched:
            blockers.append(
                {
                    "code": "title_excluded",
                    "job_evidence": matched.group(0),
                    "user_rule": str(pattern),
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
