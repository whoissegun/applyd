"""Deterministic resolution of common closed application questions.

The resolver deliberately separates three operations:

1. classify a question into a small canonical predicate;
2. evaluate that predicate against structured profile facts; and
3. bind the resulting value to exactly one rendered option.

It never treats a model's proposed answer as a profile fact.  Unsupported or
ambiguous questions return ``review``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class DeterministicAnswer:
    choice: str
    predicate: str
    value: Any
    reason: str


def _norm(value: Any) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).split())


def required_consent_authorized(question: str, profile: Mapping[str, Any], *,
                                required: bool = False) -> bool:
    """The user's standing permission covers required consent, not fact claims."""
    if not required or (profile.get("application_policy") or {}).get("required_consents") is not True:
        return False
    text = _norm(question)
    if any(term in text for term in (
        "have you", "have ever", "have never", "were you", "are you authorized",
        "are you legally", "do you require", "will you require", "do you currently",
        "do you have", "are you subject", "are you bound",
    )):
        return False
    if ('interview' in text and any(term in text for term in ('recording', 'records', 'transcribe'))
            and 'with your approval' in text):
        return True
    return bool(re.search(r"\bauthorize\b", text)) or any(term in text for term in (
        "consent", "agree", "privacy", "permission",
        "background check", "binding arbitration", "terms and conditions",
        "acknowledge", "waiver of rights", "non compete agreement",
        "i understand",
    ))


def _employer_name(value: Any) -> str:
    return re.sub(r"\s+(?:inc|incorporated|llc|ltd|limited|corporation|corp)$", "", _norm(value))


def _get(profile: Mapping[str, Any], *paths: tuple[str, ...]) -> Any:
    for path in paths:
        value: Any = profile
        for part in path:
            if not isinstance(value, Mapping) or part not in value:
                break
            value = value[part]
        else:
            return value
    return None


def _options(options: Mapping[str, str] | Sequence[Mapping[str, Any]]) -> dict[str, str]:
    if isinstance(options, Mapping):
        return {str(key): str(label) for key, label in options.items()}
    return {str(item.get("key")): str(item.get("label") or item.get("text") or "")
            for item in options if isinstance(item, Mapping) and item.get("key") is not None}


def _boolean_label(label: str) -> bool | None:
    text = _norm(label)
    if text == 'i do not have a disability':
        return False
    positive = bool(re.match(r"^(?:yes|true|i am|i do|i have|i agree|i consent|i accept|i acknowledge|by submitting i acknowledge|acknowledge confirm|authorized|eligible)\b", text))
    negative = bool(re.match(r"^(?:no|false|i am not|i do not|i don t|not authorized|not eligible)\b", text))
    if positive and not negative:
        return True
    if negative and not positive:
        return False
    return None


def _bind(options: Mapping[str, str] | Sequence[Mapping[str, Any]], value: Any) -> str | None:
    rendered = _options(options)
    if isinstance(value, bool):
        matches = [key for key, label in rendered.items() if _boolean_label(label) is value]
        return matches[0] if len(matches) == 1 else None
    wanted = _norm(value)
    exact = [key for key, label in rendered.items() if _norm(label) == wanted]
    if len(exact) == 1:
        return exact[0]
    for level in ("bachelor", "master", "associate", "doctor"):
        if level in wanted:
            matches = [key for key, label in rendered.items() if level in _norm(label)]
            if len(matches) == 1:
                return matches[0]
    compatible = [key for key, label in rendered.items()
                  if wanted and (wanted in _norm(label) or _norm(label) in wanted)]
    if len(compatible) == 1:
        return compatible[0]
    aliases = {
        "prefer not to say": (
            "prefer not to answer", "decline to answer", "decline",
            "i don t wish to answer", "i do not wish to answer",
        ),
        "not a protected veteran": ("i am not a protected veteran", "not veteran"),
        "black or african american": ("black", "black or of african descent"),
        "company careers page": ("company career page", "careers page", "career website"),
        "foreign person": ("other",),
    }
    candidates = aliases.get(wanted, ())
    matches = [key for key, label in rendered.items()
               if any(alias == _norm(label) or alias in _norm(label) for alias in candidates)]
    return matches[0] if len(matches) == 1 else None


def _review(options: Mapping[str, str] | Sequence[Mapping[str, Any]],
            predicate: str, reason: str) -> DeterministicAnswer:
    rendered = _options(options)
    choice = "review" if "review" in rendered else ""
    return DeterministicAnswer(choice, predicate, None, reason)


def _result(options: Mapping[str, str] | Sequence[Mapping[str, Any]], value: Any,
            predicate: str, reason: str) -> DeterministicAnswer:
    choice = _bind(options, value)
    if choice is None:
        rendered = _options(options)
        review_choice = "review" if "review" in rendered else ""
        return DeterministicAnswer(
            review_choice, predicate, value,
            "no unique rendered option matches the computed value",
        )
    return DeterministicAnswer(choice, predicate, value, reason)


def _bind_year_band(options: Mapping[str, str] | Sequence[Mapping[str, Any]],
                    years: float) -> str | None:
    """Bind a numeric experience fact to one visible, non-overlapping band."""
    matches: list[str] = []
    for key, label in _options(options).items():
        text = str(label).casefold()
        between = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:-|–|—|to|through)\s*(\d+(?:\.\d+)?)\b", text)
        plus = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:\+|plus|or more)\b", text)
        less = re.search(r"\b(?:less than|under)\s+(\d+(?:\.\d+)?)\b", text)
        if between and float(between.group(1)) <= years <= float(between.group(2)):
            matches.append(key)
        elif plus and years >= float(plus.group(1)):
            matches.append(key)
        elif less and years < float(less.group(1)):
            matches.append(key)
    return matches[0] if len(matches) == 1 else None


def _regions(question: str) -> list[str]:
    padded = f" {_norm(question)} "
    found = []
    tests = (
        ("US", (" united states ", " usa ", " u s ", " us ", " us based ", " h 1b ", " california ")),
        ("CA", (" canada ", " canadian ")),
        ("UK", (" united kingdom ", " uk ", " britain ", " british ")),
        ("EU", (" european union ", " eu ")),
    )
    for code, terms in tests:
        if any(term in padded for term in terms):
            found.append(code)
    return found


def _location_regions(profile: Mapping[str, Any], job_locations: Sequence[str]) -> list[str]:
    context = profile.get("job_context") if isinstance(profile.get("job_context"), Mapping) else {}
    raw: list[str] = list(job_locations)
    for key in ("required_initial_office", "location", "locations", "role_countries"):
        value = context.get(key)
        raw.extend(value if isinstance(value, list) else [value] if value else [])
    if raw and all(_norm(str(place)) in {"sf", "nyc", "south sf"} for place in raw):
        return ["US"]
    text = " | ".join(map(str, raw))
    if re.search(
        r",\s*(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|"
        r"MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|"
        r"TX|UT|VT|VA|WA|WV|WI|WY|DC)(?:\b|$)", text, re.I,
    ):
        return ["US"]
    regions = _regions(text)
    normalized = _norm(text)
    if not regions and any(place in normalized for place in (
        "california", "palo alto", "san francisco", "boston", "massachusetts", "santa clara",
        "connecticut", "rowayton", "waltham",
    )):
        regions.append("US")
    if not regions and any(place in normalized for place in ("ontario", "ottawa", "toronto")):
        regions.append("CA")
    return regions


def _profile_location(profile: Mapping[str, Any]) -> tuple[str, str, str]:
    address = profile.get("address") if isinstance(profile.get("address"), Mapping) else {}
    return (
        _norm(profile.get("address_city") or address.get("city")),
        _norm(profile.get("address_region") or address.get("region")),
        _norm(profile.get("address_country") or address.get("country")),
    )


def _profile_residence_region(profile: Mapping[str, Any]) -> str | None:
    """Map the candidate's structured residence to a legal region."""
    code = _norm(profile.get("address_country_code"))
    _city, _region, country = _profile_location(profile)
    aliases = {
        "ca": "CA", "canada": "CA",
        "us": "US", "usa": "US", "united states": "US",
        "united states of america": "US",
        "uk": "UK", "gb": "UK", "united kingdom": "UK",
    }
    return aliases.get(code) or aliases.get(country)


def _citizenship_country(profile: Mapping[str, Any]) -> str | None:
    """Return one unambiguous passport country from structured citizenship."""
    citizenships = profile.get("citizenships") or []
    if not isinstance(citizenships, Sequence) or isinstance(citizenships, (str, bytes)):
        return None
    values = [str(value).strip() for value in citizenships if str(value).strip()]
    if len(values) != 1:
        return None
    aliases = {
        "ng": "Nigeria", "nigeria": "Nigeria",
        "ca": "Canada", "canada": "Canada",
        "us": "United States", "usa": "United States",
        "united states": "United States", "united states of america": "United States",
        "gb": "United Kingdom", "uk": "United Kingdom",
        "united kingdom": "United Kingdom",
    }
    return aliases.get(_norm(values[0]))


def _preference(profile: Mapping[str, Any], key: str) -> Any:
    aliases = {
        "willing_to_relocate": "willing_relocate",
        "willing_to_work_onsite": "willing_onsite",
        "willing_to_work_hybrid": "willing_hybrid",
        "willing_to_work_remote": "willing_remote",
        "willing_to_work_any_onsite_schedule": "willing_any_onsite_schedule",
    }
    return _get(profile, ("employment_preferences", key), ("employment", aliases.get(key, key)))


def _work_record(profile: Mapping[str, Any], region: str) -> Mapping[str, Any] | None:
    record = (profile.get("work_authorization") or {}).get(region)
    return record if isinstance(record, Mapping) else None


def immigration_authorization_required_question(question: str) -> bool:
    text = _norm(question)
    return bool(re.search(r'\b(?:require\w*|need\w*)\b.*\b(?:visas?|work authorizations?|work authorisations?|work permits?|authorization to work|authorisation to work)\b', text))


def _legal(question: str, profile: Mapping[str, Any], options: Any,
           job_locations: Sequence[str]) -> DeterministicAnswer | None:
    text = _norm(question)
    if (text.startswith('skip this question if you are applying to work in canada or the uk')
            and _location_regions(profile, job_locations) in [['CA'], ['UK']]):
        return _review(options, 'legal_not_applicable', 'form explicitly says to skip for the role country')
    exact_policy = _get(profile, ("historical_application_answers",), ("user_policy",))
    if isinstance(exact_policy, Mapping):
        for saved, value in exact_policy.items():
            if _norm(saved) == text and isinstance(value, (bool, str)):
                return _result(options, value, "saved_exact_answer", "exact saved question policy")
        if (text == "do you require authorization to work in the u s or canada"
                and isinstance(exact_policy.get("combined_us_ca_require_authorization_answer"), bool)):
            return _result(options, exact_policy["combined_us_ca_require_authorization_answer"],
                           "saved_exact_answer", "explicit combined-jurisdiction policy")

    if "which immigration status" in text or "canadian work status" in text:
        record = _work_record(profile, "CA")
        status = (record or {}).get("status") or (record or {}).get("visa_status")
        return _result(options, status, "immigration_status", "structured Canadian status") \
            if status else _review(options, "immigration_status", "status missing")
    status_question = any(term in text for term in (
        "authorized to work", "authorised to work", "eligible to work", "legal right to work",
        "right to work", "authorized in", "work lawfully", "work authorization", "work authorisation",
        "valid authorization", "work permit", "sponsorship", "sponsor", "visa sponsorship",
    )) or immigration_authorization_required_question(question)
    if not status_question:
        return None
    if any(term in text for term in ("anywhere worldwide", "immigration assistance", "could your work authorization change")):
        return _review(options, "legal_ambiguous", "scope is not a concrete jurisdictional predicate")

    regions = _regions(question)
    residence_relative = any(phrase in text for phrase in (
        "country you are based in", "country you re based in",
        "country where you are based", "country where you currently reside",
        "country you currently reside in", "your country of residence",
        "location you currently reside", "location where you currently reside",
    ))
    role_relative = any(phrase in text for phrase in (
        "country where this role is based", "location s you selected", "locations you selected",
        "that country",
    ))
    if residence_relative:
        residence_region = _profile_residence_region(profile)
        if residence_region:
            regions = [residence_region]
        else:
            return _review(options, "legal_residence", "structured residence country is missing")
    elif role_relative or not regions:
        context_regions = _location_regions(profile, job_locations)
        if context_regions:
            regions = context_regions
        elif role_relative:
            return _review(options, "legal_relative", "job or selected location is missing")
    if not regions:
        return _review(options, "legal_jurisdiction", "jurisdiction is missing")

    if text == 'please select your right to work status' and regions == ['UK']:
        record = _work_record(profile, 'UK') or {}
        rendered = _options(options)
        named_statuses = {
            'british or irish citizen', 'eu pre settled status', 'eu settled status',
            'indefinite leave to remain ilr', 'skilled worker or tier 2 general visa',
            'global talent visa', 'family or spouse visa', 'tier 4 general student visa',
            'graduate visa',
        }
        labels = {_norm(label) for label in rendered.values()}
        explicit_status = _norm(record.get('visa_status') or record.get('status') or '')
        if (record.get('authorized') is False and record.get('requires_sponsorship') is True
                and explicit_status in {'none', 'no uk visa', 'no uk immigration status'}
                and 'other' in labels and labels - {'other', 'select', 'select an option'} <= named_statuses
                and bool(labels & named_statuses)):
            return _result(options, 'Other', 'uk_right_to_work_category',
                           'explicit profile records no UK visa/status; Other asserts no named visa')

    authorization_predicate = any(phrase in text for phrase in (
        "authorized to work", "authorised to work", "eligible to work", "right to work",
        "authorized in", "work lawfully", "valid authorization", "valid authorisation",
    )) or bool(re.search(r'\bhave\b.*\bwork authori[sz]ation\b', text))
    if re.search(r'\b(?:ever|previously|denied)\b', text):
        return _review(options, 'legal_ambiguous', 'historical immigration fact is not established by current authorization')
    without_sponsorship = authorization_predicate and bool(re.search(
        r"\bwithout\b.*\b(?:sponsor\w*|employer sponsored)\b", text))
    sponsorship = not authorization_predicate and bool(
        re.search(r"\b(?:require|requires|required|need|needs|needed|without)\b.*\bsponsor(?:ship|ed|ing)?\b", text)
        or re.search(r"\bsponsor(?:ship|ed|ing)?\b.*\b(?:require|need|you|work permit|visa)\b", text)
    )
    if immigration_authorization_required_question(question) and not sponsorship and not authorization_predicate:
        # Requiring authorization is NOT the same predicate as possessing it.
        # An unauthorized applicant necessarily needs authorization. An already
        # authorized applicant may still require/hold a visa (e.g. an open work
        # permit), so sponsorship=False alone must not be used to answer No.
        if re.search(r'\b(?:not|without|ever|previously|visit|tourism)\b', text):
            return _review(options, 'work_requires_authorization', 'negated or historical visa requirement needs exact policy')
        authorized = [(_work_record(profile, region) or {}).get('authorized') for region in regions]
        if len(authorized) == 1 and authorized[0] is False:
            return _result(options, True, 'work_requires_authorization', 'not currently authorized in the named jurisdiction; authorization is required')
        return _review(options, 'work_requires_authorization', 'existing authorization or multiple countries do not establish whether a visa is required')
    if not authorization_predicate and not sponsorship and text not in {
            'work authorization', 'work authorisation', 'work authorization status',
            'work authorisation status'} and not re.search(r'\bhave\b.*\bvalid (?:work )?authori[sz]ation\b', text):
        return _review(options, 'legal_ambiguous', 'legal wording does not establish a supported authorization or sponsorship predicate')
    field = "requires_sponsorship" if sponsorship else "authorized"
    values = [(record or {}).get(field) for record in (_work_record(profile, r) for r in regions)]
    if without_sponsorship:
        sponsorship_values = [(_work_record(profile, r) or {}).get('requires_sponsorship') for r in regions]
        if not all(isinstance(value, bool) for value in sponsorship_values):
            return _review(options, "work_authorized", "structured sponsorship fact is missing")
        values = [authorized and not needs for authorized, needs in zip(values, sponsorship_values)]
    if not all(isinstance(value, bool) for value in values):
        return _review(options, f"work_{field}", "structured jurisdictional fact is missing")
    if field == 'authorized' and 'unrestricted' in text and any(values):
        return _review(options, 'work_authorized', 'authorization boolean does not establish unrestricted permit conditions')
    if len(values) == 1:
        value = values[0]
    elif "at least one" in text:
        value = any(values)
    elif "both" in text or " and " in f" {text} ":
        value = all(values)
    elif " or " in f" {text} ":
        # Plain OR on a generic application is ambiguous (any jurisdiction vs
        # the role's selected jurisdiction). A concrete role context makes it
        # an ordinary any-of predicate.
        if not _location_regions(profile, job_locations):
            return _review(options, f"work_{field}", "plain multi-jurisdiction OR is ambiguous")
        value = any(values)
    else:
        return _review(options, f"work_{field}", "compound legal operator is unclear")

    if sponsorship and re.search(r"\bwithout\b.*\bsponsorship\b", text):
        value = not value
    if sponsorship and ("will not need" in text or "will not require" in text):
        value = not value
    if sponsorship and text.startswith("is it false that"):
        value = not value
    if not sponsorship and re.search(r"\bnot authorized\b", text):
        value = not value
    if sponsorship and value is True:
        rendered = _options(options)
        matches = [key for key, label in rendered.items() if _norm(label) == 'require sponsorship']
        if len(matches) == 1:
            return DeterministicAnswer(matches[0], 'work_requires_sponsorship', True,
                                       'structured sponsorship requirement matches the rendered category')
    return _result(options, value, f"work_{field}", "computed from structured regional facts")


def resolve_question(question: str, profile: Mapping[str, Any], options: Any, *,
                     job_locations: Sequence[str] = (), company: str = "",
                     required: bool = False) -> DeterministicAnswer:
    """Resolve a supported closed question or return the rendered review choice."""
    question = re.split(r"\n\s*[Ss]elect\s*(?:\.\.\.|…)?\s*\n", question, maxsplit=1)[0]
    text = _norm(question)
    legal = _legal(question, profile, options, job_locations)
    if legal is not None:
        return legal
    if required_consent_authorized(question, profile, required=required):
        rendered = _options(options)
        positive = [key for key, label in rendered.items() if (
            _boolean_label(label) is True or _norm(label) in {
                "i agree", "agree", "i consent", "consent", "accept", "i accept",
                "acknowledge", "i acknowledge",
            }
        )]
        if len(positive) == 1:
            return DeterministicAnswer(positive[0], "required_consent", True,
                                       "user authorized all required application consent")
        return _result(options, True, "required_consent", "standing required-consent policy")

    identity = profile.get("identity_facts") if isinstance(profile.get("identity_facts"), Mapping) else {}
    demographics = profile.get("demographics") if isinstance(profile.get("demographics"), Mapping) else {}
    background = profile.get("background") if isinstance(profile.get("background"), Mapping) else {}
    background = {**background, **(profile.get("background_defaults") or {})}
    policy = profile.get("policy") if isinstance(profile.get("policy"), Mapping) else {}
    policy = {**policy, **(profile.get("application_policy") or {})}
    if text.startswith('have you added your full legal name and surname'):
        if (profile.get('full_name') and profile.get('first_name') and profile.get('last_name')
                and policy.get('ordinary_accuracy_attestation') in {True, 'authorized'}):
            return _result(options, True, 'profile_name_acknowledgement',
                           'profile supplies full name and authorizes ordinary accuracy acknowledgement')
        return _review(options, 'profile_name_acknowledgement', 'full profile name or acknowledgement authority missing')
    credentials = profile.get("credentials") if isinstance(profile.get("credentials"), Mapping) else {}
    education = profile.get("education") if isinstance(profile.get("education"), Mapping) else {}
    employment = profile.get("employment") if isinstance(profile.get("employment"), Mapping) else {}

    # Citizenship and permanent residence are not the same as work authorization.
    if "citizen" in text and "export control" not in text:
        countries = [_norm(v) for v in (profile.get("citizenships") or identity.get("citizenships") or [])]
        target = "united states" if any(x in text for x in ("united states", "u s", " us ")) else "canada" if "canadian" in text else ""
        return _result(options, target in countries, "citizenship", "structured citizenship list")
    if ("permanent resident" in text or "green card" in text) and "export control" not in text:
        value = identity.get("us_lawful_permanent_resident") if any(x in text for x in (" us ", "u s", "green card")) else False
        return _result(options, bool(value), "permanent_residence", "structured permanent-residence fact")

    city, region, country = _profile_location(profile)
    if text in {"what is your location", "where are you currently located", "where are you located"}:
        residence = profile.get("address_country") or country
        if residence and _bind(options, residence) is not None:
            return _result(options, residence, "country_of_residence", "structured residence matches a visible country option")
    if "countries" in text and "seeking to work" in text:
        residence = profile.get("address_country") or country
        return _result(options, residence, "work_country_selection", "structured residence is the preferred offered work country") \
            if residence else _review(options, "work_country_selection", "residence country missing")
    if (text in {"country of residence", "current country"}
            or "what is your country of residence" in text
            or "what country do you currently live in" in text
            or "select the country where you currently reside" in text):
        value = profile.get("address_country") or country
        return _result(options, value, "country_of_residence", "structured residence country") \
            if value else _review(options, "country_of_residence", "residence country missing")
    if text == 'which u s state or canadian province do you reside in':
        value = profile.get('address_region')
        return _result(options, value, 'residence_region', 'structured residence state/province') \
            if value else _review(options, 'residence_region', 'residence state/province missing')
    if 'based in the same country this role is in' in text:
        residence = _profile_residence_region(profile)
        regions = _location_regions(profile, job_locations)
        return _result(options, residence == regions[0], 'role_country_residence', 'compared structured residence with role country') \
            if residence and len(regions) == 1 else _review(options, 'role_country_residence', 'role or residence country unresolved')
    if ("country or countries" in text and "anticipate working" in text):
        value = profile.get("address_country") or country
        return _result(options, value, "anticipated_work_country", "structured residence and remote-work country") \
            if value else _review(options, "anticipated_work_country", "work country missing")
    if re.search(r"\b(?:currently live|do you live in|currently based|currently reside|commut\w*|commuting distance)\b", text):
        named = next((place for place in ("canada", "ottawa", "toronto", "london", "seoul", "burlington", "rowayton", "san francisco bay area") if place in text), None)
        if named:
            local = named in {city, region, country}
            return _result(options, local, "current_location", "compared named place with structured residence")

    if "plan to work remotely" in text:
        value = profile.get("remote_work_plan")
        if not isinstance(value, bool):
            value = _preference(profile, "willing_to_work_remote")
        return _result(options, value, "remote_work_plan", "explicit remote-work plan") \
            if isinstance(value, bool) else _review(options, "remote_work_plan", "remote-work plan missing")

    if ('one of the countries listed on the job posting' in text
            and 'without relocation assistance' in text):
        residence = _profile_residence_region(profile)
        regions = _location_regions(profile, job_locations)
        if residence in regions and (_work_record(profile, residence) or {}).get('authorized') is True:
            return _result(options, True, 'existing_country_placement',
                           'already resident and authorized in an offered country; no international relocation needed')
        return _review(options, 'existing_country_placement', 'relocation without assistance is not established')

    if text == 'are you able to work from london':
        values = (_preference(profile, 'willing_to_work_onsite'), _preference(profile, 'willing_to_relocate'))
        if all(isinstance(value, bool) for value in values):
            residence = _profile_residence_region(profile)
            relocation = [key for key, label in _options(options).items()
                          if 'not currently live' in _norm(label) and 'open to relocation' in _norm(label)]
            if all(values) and residence and residence != 'UK' and len(relocation) == 1:
                return DeterministicAnswer(relocation[0], 'office_location_willingness', True,
                    'not currently in the UK; willing to relocate and work onsite, without claiming current London residence')
            return _result(options, all(values), 'office_location_willingness',
                           'office-location willingness; separate from the explicit UK legal-authorization question')
        return _review(options, 'office_location_willingness', 'onsite/relocation preference missing')

    preference_patterns = (
        (("relocat",), "willing_to_relocate"),
        (("onsite", "on site", "in person", "in office", "work from our", "come in if needed", "working out of our"), "willing_to_work_onsite"),
        (("hybrid",), "willing_to_work_hybrid"),
        (("work remotely", "work remote", "plan to work remotely"), "willing_to_work_remote"),
        (("travel for work",), "willing_to_travel"),
    )
    for terms, key in preference_patterns:
        if any(term in text for term in terms):
            if key == "willing_to_relocate" and any(
                    phrase in text for phrase in ("relocation assistance", "relocation help")):
                continue
            value = _preference(profile, key)
            if isinstance(value, bool):
                if key == "willing_to_work_onsite" and value is True:
                    rendered = _options(options)
                    relocation_choices = [
                        option_key for option_key, label in rendered.items()
                        if "open to relocation" in _norm(label) or (
                            _boolean_label(label) is True
                            and "willing to relocate" in _norm(label)
                            and "not currently live" in _norm(label)
                        )
                    ]
                    city, region, country = _profile_location(profile)
                    currently_local = any(
                        place and place in text for place in (city, region, country)
                    )
                    if (not currently_local
                            and _preference(profile, "willing_to_relocate") is True
                            and len(relocation_choices) == 1):
                        return DeterministicAnswer(
                            relocation_choices[0], "onsite_with_relocation", True,
                            "profile is not local and explicitly allows relocation",
                        )
                return _result(options, value, key, "structured employment preference")
    if "days per week" in text or "each work day" in text or "each week" in text:
        value = _preference(profile, "willing_to_work_any_onsite_schedule")
        if value is True:
            return _result(options, True, "onsite_schedule", "accepts any onsite schedule")
    if "only accept fully remote" in text:
        onsite = _preference(profile, "willing_to_work_onsite")
        return _result(options, not onsite, "remote_only", "derived from onsite preference") if isinstance(onsite, bool) else _review(options, "remote_only", "onsite preference missing")
    if "whichever office" in text:
        values = (_preference(profile, "willing_to_relocate"), _preference(profile, "willing_to_work_onsite"))
        return _result(options, all(v is True for v in values), "office_any", "relocation and onsite preferences")

    currently_student = education.get("currently_student")
    academic = profile.get("academic_status") or {}
    study_status = _norm(academic.get("status", "")) if isinstance(academic, Mapping) else ""
    if currently_student is None and study_status in {"in progress", "enrolled", "pursuing"}:
        currently_student = True
    degree = education.get("degree") or profile.get("degree")
    major = education.get("major") or profile.get("major")
    school = education.get("institution") or profile.get("school")
    grad = education.get("expected_graduation") or profile.get("expected_grad_date")
    start = education.get("start_date")
    if "currently a student" in text or "student or new graduate" in text:
        return _result(options, currently_student, "student_status", "structured education status") if isinstance(currently_student, bool) else _review(options, "student_status", "student status missing")
    if "already graduated" in text:
        return _result(options, not currently_student, "graduated", "inverse of current student status") if isinstance(currently_student, bool) else _review(options, "graduated", "student status missing")
    if "bachelor" in text or "master" in text:
        if not degree:
            return _review(options, "degree_level", "degree missing")
        levels = [level for level in ("bachelor", "master") if level in text]
        matches = any(level in _norm(degree) for level in levels)
        # Naming the enrolled degree does not establish that it was earned.
        # Future/compound qualification questions need their own grounded rule.
        if re.search(r"\b(?:by|before|upon|will|equivalent|experience|and have)\b", text):
            return _review(options, "degree_level", "compound or future degree requirement needs review")
        if re.search(r"\b(?:pursuing|enrolled|studying|working toward)\b", text):
            return _result(options, matches and currently_student is True,
                           "degree_enrollment", "structured enrolled degree and student status") \
                if isinstance(currently_student, bool) else _review(options, "degree_enrollment", "enrollment status missing")
        if not re.search(r"\b(?:have|hold|earned|completed|obtained|possess|received)\b", text):
            return _review(options, "degree_level", "degree question does not specify enrollment or completion")
        if not matches or study_status in {"in progress", "enrolled", "pursuing"}:
            return _result(options, False, "degree_completion", "specified degree is not a completed profile credential")
        if study_status not in {"completed", "graduated", "awarded"}:
            return _review(options, "degree_completion", "degree completion is not explicit")
        # Do not turn possession of one credential into an unrelated subject.
        if " in " in text:
            subject = _norm(major or degree)
            if "computer science" not in text or "computer science" not in subject:
                return _review(options, "degree_completion", "degree subject needs exact grounding")
        return _result(options, True, "degree_completion", "explicit completed profile credential")
    year = re.search(r"\b(20\d{2})\b", text)
    if year and "graduat" in text:
        return _result(options, bool(grad and str(grad).startswith(year.group(1))), "graduation_year", "structured graduation date")
    if text in {
        "degree", "discipline", "school", "end date month", "end date year",
        "graduation date", "expected graduation date", "graduation month",
        "graduation date or anticipated graduation date",
        "expected graduation month", "graduation year", "expected graduation year",
    } or "highest level of education" in text:
        if text == "degree": value = degree
        elif text == "discipline": value = major
        elif text == "school": value = school
        elif text in {"end date month", "graduation month", "expected graduation month"}:
            value = date(2000, int(str(grad)[5:7]), 1).strftime("%B") if grad else None
        elif text in {"end date year", "graduation year", "expected graduation year"}:
            value = str(grad)[:4] if grad else None
        elif text in {"graduation date", "expected graduation date", "graduation date or anticipated graduation date"}: value = grad
        else: value = "Currently pursuing a Bachelor's degree" if currently_student and degree and "bachelor" in _norm(degree) else None
        return _result(options, value, "education_control", "structured education field") if value else _review(options, "education_control", "education fact missing")
    if "years" in text and "experience" in text and any(
            term in text for term in ("professional", "relevant", "software")):
        years = employment.get("years_professional_experience", profile.get("years_professional_experience"))
        threshold = re.search(r"\b(\d+)\s*\+?\s*years", text)
        if not isinstance(years, (int, float)) or isinstance(years, bool):
            return _review(options, "experience_years", "experience years missing")
        if threshold:
            return _result(options, years >= int(threshold.group(1)), "experience_years", "structured experience years")
        choice = _bind_year_band(options, float(years))
        if choice is not None:
            return DeterministicAnswer(
                choice, "experience_years", years,
                "structured experience years matched one visible range",
            )
        return _review(options, "experience_years", "no unique visible experience range matches")

    if "programming language" in text and "interview" in text:
        value = profile.get("interview_language_preference")
        return _result(options, value, "interview_language", "explicit interview-language preference") \
            if value else _review(options, "interview_language", "interview-language preference missing")

    if "hourly rate" in text:
        value = profile.get("hourly_rate_usd_band")
        return _result(options, value, "hourly_rate_usd_band", "explicit USD hourly-rate band") \
            if value else _review(options, "hourly_rate_usd_band", "hourly-rate band missing")

    if "research experience" in text and "computer vision" in text:
        value = profile.get("computer_vision_research_experience")
        return _result(options, value, "computer_vision_research", "explicit research-experience answer") \
            if isinstance(value, bool) else _review(options, "computer_vision_research", "research-experience answer missing")

    if any(phrase in text for phrase in (
            "name pronunciation", "name pronounciation", "pronunciation of your name",
            "pronounce your name", "phonetic spelling")):
        value = profile.get("name_pronunciation")
        return _result(options, value, "name_pronunciation", "explicit name pronunciation") \
            if value else _review(options, "name_pronunciation", "name pronunciation missing")

    if "domain expertise" in text:
        value = profile.get("domain_expertise") or profile.get("major")
        return _result(options, value, "domain_expertise", "explicit domain expertise or matching structured major") \
            if value else _review(options, "domain_expertise", "domain expertise missing")

    consent_map = (
        (("recruiting marketing", "future job opportunities", "talent network", "whatsapp"), ("recruiting_marketing_consent", "recruiting_communications_consent")),
        (("text messages", "sms"), ("sms_recruiting_consent",)),
        (("recording", "recorded", "recording and transcribing"), ("interview_recording_consent",)),
    )
    for terms, keys in consent_map:
        if any(term in text for term in terms):
            value = next((policy.get(key) for key in keys if isinstance(policy.get(key), bool)), None)
            return _result(options, value, "consent", "explicit structured consent") if isinstance(value, bool) else _review(options, "consent", "explicit consent missing")
    if "privacy notice" in text:
        allowed = policy.get("ordinary_accuracy_attestation") in {True, "authorized"}
        return _result(options, allowed, "privacy_acknowledgement", "ordinary acknowledgement policy")
    if "application is accurate" in text:
        allowed = policy.get("ordinary_accuracy_attestation") in {True, "authorized"}
        return _result(options, allowed, "accuracy_attestation", "ordinary accuracy policy")
    if any(term in text for term in ("automated hiring decisions", "binding arbitration", "background check")):
        return _review(options, "sensitive_consent", "no explicit consent policy")

    if "security clearance" in text:
        clearance = (profile.get("security_clearance") or {}).get("US") or {}
        held = clearance.get("held", credentials.get("security_clearance_US"))
        if "eligible" in text:
            eligible = clearance.get("eligible")
            return _result(options, eligible, "clearance_eligible", "structured clearance eligibility") if isinstance(eligible, bool) else _review(options, "clearance_eligible", "eligibility missing")
        if held is False and any(term in text for term in ("what", "which", "level", "held in the past")):
            rendered = _options(options)
            matches = [key for key, label in rendered.items() if any(
                phrase in _norm(label) for phrase in (
                    "never held", "no security clearance", "not held", "n a",
                )
            )]
            if len(matches) == 1:
                return DeterministicAnswer(
                    matches[0], "clearance_level", "none",
                    "structured profile records no past U.S. security clearance",
                )
        return _result(options, held, "clearance_held", "structured clearance status") if isinstance(held, bool) else _review(options, "clearance_held", "clearance status missing")
    if "professional engineer" in text:
        value = credentials.get("licensed_professional_engineer")
        return _result(options, value, "professional_license", "structured credential") if isinstance(value, bool) else _review(options, "professional_license", "credential missing")
    if "driver s license" in text:
        value = credentials.get("drivers_license")
        return _result(options, value, "drivers_license", "structured credential") if isinstance(value, bool) else _review(options, "drivers_license", "credential missing")
    if any(term in text for term in ("criminal offence", "criminal offense", "non compete")):
        return _review(options, "sensitive_history", "historical fact missing")

    if "immediate family" in text and "government" in text:
        value = background.get("no_immediate_family_in_government")
        return _result(options, not value, "government_family", "structured background default") if isinstance(value, bool) else _review(options, "government_family", "family fact missing")
    if "relative" in text or "relatives" in text:
        value = background.get("no_immediate_family_at_target_company")
        return _result(options, not value, "company_relative", "structured background default") if isinstance(value, bool) else _review(options, "company_relative", "relative fact missing")
    current_employment = bool(re.search(
        r'\bare you (?:currently )?(?:a |an )?(?:current )?(?:employee|employed)\b', text))
    if current_employment or any(term in text for term in ("previously worked", "ever worked", "ever been employed", "previously employed", "previously been employed", "worked for", "worked at", "employed by")):
        value = background.get("never_employed_by_target_unless_listed")
        if value is None: value = background.get("never_employed_by_target_unless_listed_on_resume")
        if value is True:
            employers = profile.get("resume_employers")
            if isinstance(employers, list):
                if not company or any(term in text for term in (
                    "government", "oversight", "conflict of interest",
                    "family", "relative", "last 5 years", "last five years",
                )):
                    return _review(options, "prior_company", "target missing or compound employment history")
                target = _employer_name(company)
                if target not in text and not any(phrase in text for phrase in (
                    "this employer", "this company", "our company", "worked for us",
                    "employed by us", "work for us",
                )):
                    return _review(options, "prior_company", "question names a different employer")
                employed = any(_employer_name(name) == target for name in employers)
                if current_employment and employed:
                    current = profile.get('current_company')
                    if not isinstance(current, str) or _employer_name(current) != target:
                        return _review(options, 'prior_company', 'past employment does not establish current employment')
                if re.search(r"\b(?:never|not)\b.*\b(?:worked|employed)\b", text):
                    employed = not employed
                return _result(options, employed, "prior_company",
                               "full source resume employer list and user-authorized absent-employer default")
            return _result(options, False, "prior_company", "user-authorized absent-employer default")
        return _review(options, "prior_company", "employment history policy or target missing")
    if "at least 18" in text:
        value = profile.get("over_18", identity.get("over_18"))
        return _result(options, value, "over_18", "structured age threshold") if isinstance(value, bool) else _review(options, "over_18", "age threshold missing")

    export = profile.get("export_control") or identity
    if "us person under export control" in text:
        value = export.get("us_person")
        return _result(options, value, "export_us_person", "structured export classification") if isinstance(value, bool) else _review(options, "export_us_person", "export fact missing")
    if "export control purposes" in text:
        value = export.get("classification") or ("foreign_person" if export.get("us_person") is False else None)
        return _result(options, value, "export_classification", "structured export classification") if value else _review(options, "export_classification", "export classification missing")
    if "deemed export rule affect" in text:
        return _review(options, "export_effect", "effect on this employer is not encoded")

    # Native HTML selects may expose their option text as part of the field's
    # accessible label (for example, "Race Select ... Hispanic or Latino ...").
    # Classify the field heading before scanning substrings from its options;
    # otherwise a Race field can be mistaken for the Hispanic boolean field.
    if text == "race" or text.startswith("race select ") or "identify your race" in text:
        value = demographics.get("ethnicity", profile.get("ethnicity"))
        return _result(options, value, "ethnicity", "structured demographic field") \
            if value else _review(options, "ethnicity", "ethnicity missing")
    if text == "gender" or text.startswith("gender select "):
        value = demographics.get("gender", profile.get("gender"))
        return _result(options, value, "gender", "structured demographic field") \
            if value else _review(options, "gender", "gender missing")

    demographic_map = (
        (("hispanic", "latino"), "hispanic_latino"),
        (("veteran status",), "veteran_status"),
        (("veteran or active member",), "veteran_status"),
        (("disability", "chronic condition"), "disability_status"),
        (("gender identity",), "gender"),
        (("racial ethnic",), "ethnicity"),
    )
    for terms, key in demographic_map:
        if any(term in text for term in terms):
            value = demographics.get(key, profile.get(key))
            if key == "veteran_status" and "veteran or active" in text:
                value = False if value and "not" in _norm(value) else None
            elif key == "disability_status" and isinstance(value, str):
                value = not _norm(value).startswith("no")
            return _result(options, value, key, "structured demographic field") if value is not None else _review(options, key, "demographic fact missing")
    if "current age" in text:
        rendered = _options(options)
        decline = next((key for key, label in rendered.items()
                        if "prefer not to answer" in _norm(label)), None)
        if decline:
            return DeterministicAnswer(decline, "age_bracket", "Prefer not to answer",
                                       "exact age is not stored; use visible neutral option")
        return _review(options, "age_bracket", "exact age is not stored")
    if "pronouns" in text:
        value = demographics.get("pronouns", profile.get("pronouns"))
        if value:
            return _result(options, value, "pronouns", "structured pronouns")
        rendered = _options(options)
        decline = next((key for key, label in rendered.items()
                        if "prefer not to say" in _norm(label)
                        or "prefer not to answer" in _norm(label)), None)
        if decline:
            return DeterministicAnswer(decline, "demographic_unset", "Prefer not to say",
                                       "profile does not contain pronouns; use neutral option")
        return _review(options, "demographic_unset", "profile does not contain pronouns")
    if "transgender" in text:
        rendered = _options(options)
        decline = next((key for key, label in rendered.items()
                        if "prefer not to say" in _norm(label)
                        or "prefer not to answer" in _norm(label)), None)
        if decline:
            return DeterministicAnswer(decline, "demographic_unset", "Prefer not to say",
                                       "profile does not contain this demographic; use neutral option")
        return _review(options, "demographic_unset", "profile does not contain this demographic")

    earliest = employment.get("earliest_start") or profile.get("earliest_start_date")
    if "start immediately" in text:
        return _result(options, _norm(earliest) in {"immediately", "now", "available now"}, "start_availability", "structured start availability") if earliest else _review(options, "start_availability", "start availability missing")
    if "two weeks notice" in text:
        return _result(options, False, "notice_period", "immediate availability") if _norm(earliest) in {"immediately", "now", "available now"} else _review(options, "notice_period", "notice period missing")
    if "could you have started" in text:
        return _review(options, "past_hypothetical", "past hypothetical is not current availability")
    if "when are you available" in text:
        return _result(options, earliest, "start_availability", "structured start availability") if earliest else _review(options, "start_availability", "start availability missing")

    employment_types = _get(profile, ("employment_preferences", "employment_types"),
                            ("employment", "employment_types"))
    if any(term in text for term in ("contract employment", "full time employment", "part time employment")):
        if isinstance(employment_types, list):
            wanted = "contract" if "contract" in text else "full_time" if "full time" in text else "part_time"
            return _result(options, wanted in employment_types, "employment_type", "structured employment types")
        return _review(options, "employment_type", "employment-type preference missing")
    if "full time internship" in text:
        if isinstance(employment_types, list):
            value = "internship" in employment_types and "full_time" in employment_types
            return _result(options, value, "full_time_internship", "structured employment types")
        return _review(options, "full_time_internship", "employment-type preference missing")
    if "travel for work" in text:
        value = employment.get("willing_travel")
        return _result(options, value, "travel_preference", "structured travel preference") if isinstance(value, bool) else _review(options, "travel_preference", "travel preference missing")

    if (text in {"how did you hear about this job", "how did you connect with us"}
            or text.startswith("how did you hear about")
            or text.startswith("where did you hear about")
            or text.startswith("how did you first learn about")):
        source = profile.get("referral_source")
        if source and _bind(options, source) is not None:
            return _result(options, source, 'referral_source', 'structured referral source')
        if required:
            for fallback in policy.get('required_referral_source_fallbacks') or []:
                if _bind(options, fallback) is not None:
                    return _result(options, fallback, 'referral_source', 'user-authorized required source fallback')
        return _result(options, source, "referral_source", "structured referral source") if source else _review(options, "referral_source", "source missing")

    if "which of these three locations will you be working from" in text:
        _city, _region, residence_country = _profile_location(profile)
        continent = {
            "canada": "North America", "united states": "North America",
            "mexico": "North America",
        }.get(residence_country)
        return _result(options, continent, "work_continent", "continent derived from structured residence") if continent else _review(options, "work_continent", "residence cannot be mapped to a listed region")

    language = re.search(r"\bdo you speak ([a-z]+)\b", text)
    if language:
        languages = profile.get("spoken_languages")
        if isinstance(languages, list):
            value = any(_norm(item) == language.group(1) for item in languages)
            return _result(options, value, "spoken_language", "compared with structured spoken-language list")
        return _review(options, "spoken_language", "spoken-language list missing")

    # These are intentionally not inferred from adjacent facts.
    unknown_terms = (
        "salary", "interview in python", "fluent", "referred by",
        "through linkedin", "relocation assistance", "overnight shift", "pronounced",
        "need assistance", "weekends", "preferred work location",
        "military and allied forces",
    )
    if any(term in text for term in unknown_terms):
        return _review(options, "unsupported", "required fact or preference is not explicit")
    return _review(options, "unclassified", "no deterministic question mapping")


def resolve_text_question(question: str, profile: Mapping[str, Any], *,
                          job_locations: Sequence[str] = (), company: str = "",
                          input_type: str = "text", required: bool = False) -> DeterministicAnswer | None:
    """Return a deterministic value for a free-entry control when supported.

    This uses the same predicate evaluator as radio/select controls.  The
    rendered control type changes only formatting, never the underlying truth.
    Unclassified/compositional prompts return ``None`` so the model can handle
    them. A recognized consequential question with a missing fact remains a
    review result.
    """
    text = _norm(question)
    if text == 'reason for leaving or job search' and required:
        return DeterministicAnswer('text', 'job_search_motivation',
            'I am seeking a role where I can contribute, keep developing my skills, and take on meaningful technical work.',
            'job-search motivation alternative; no historical reason for leaving is invented')
    if text in {'date available', 'date of availability'}:
        earliest = _get(profile, ('employment', 'earliest_start'), ('earliest_start_date',))
        if _norm(earliest) in {'immediately', 'now', 'available now'}:
            value = date.today().strftime('%Y-%m' if input_type == 'month' else '%Y-%m-%d')
            return DeterministicAnswer('text', 'start_availability', value, 'current date from explicit immediate availability')
        return DeterministicAnswer('review', 'start_availability', None, 'availability date missing')
    if text in {"notice period", "what is your notice period", "notice what is your notice period"}:
        earliest = _get(profile, ("employment", "earliest_start"), ("earliest_start_date",))
        if _norm(earliest) in {"immediately", "now", "available now"}:
            return DeterministicAnswer("text", "start_availability", "Available immediately",
                                       "structured immediate availability")
        return DeterministicAnswer("review", "start_availability", None, "notice period missing")
    if text == "desired pay":
        policy = profile.get("salary_expectation")
        if isinstance(policy, Mapping) and policy.get("fallback") == "Negotiable":
            return DeterministicAnswer("text", "compensation", "Negotiable",
                                       "saved compensation fallback; no invented annual conversion")
    if "github" in text and "profile" in text and any(term in text for term in ("link", "url")):
        value = profile.get("github_url")
        if isinstance(value, str) and value.strip():
            return DeterministicAnswer("text", "github_url", value,
                                       "saved GitHub profile URL")
    if "subject to u s export controls" in text:
        export = profile.get("export_control") or profile.get("identity_facts") or {}
        classification = _norm(export.get("classification")) \
            if isinstance(export, Mapping) else ""
        if classification == "foreign person" or export.get("us_person") is False:
            return DeterministicAnswer(
                "text", "export_classification", "None of the above",
                "structured foreign-person export classification",
            )
        return DeterministicAnswer(
            "review", "export_classification", None,
            "export classification missing",
        )
    if text in {"passport country", "country of citizenship", "citizenship country"}:
        country = _citizenship_country(profile)
        return DeterministicAnswer(
            "text" if country else "review", "passport_country", country,
            "single structured citizenship" if country else
            "passport country requires one recognized structured citizenship",
        )
    answer = resolve_question(
        question, profile, {"yes": "Yes", "no": "No", "review": "Review"},
        job_locations=job_locations, company=company, required=required,
    )
    if answer.predicate in {"unclassified", "unsupported"}:
        return None
    value = answer.value
    if value is None:
        return answer
    if (answer.predicate == "education_control" and input_type == "date"
            and re.fullmatch(r"20\d{2}-\d{2}", str(value))):
        return DeterministicAnswer(
            "review", answer.predicate, None,
            "day-specific date control cannot be filled from a month-only graduation date",
        )
    if isinstance(value, bool):
        value = "Yes" if value else "No"
    elif answer.predicate == "education_control" and input_type == "month":
        grad = _get(profile, ("expected_grad_date",), ("education", "expected_graduation"))
        value = grad if grad else value
    return DeterministicAnswer("text", answer.predicate, str(value), answer.reason)
