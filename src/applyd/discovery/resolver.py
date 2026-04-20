from __future__ import annotations

import re
from typing import Optional
from urllib.parse import unquote, urlparse

from .routing import detect_ats
from .search.base import SearchProvider


CORP_SUFFIXES = {
    "inc", "llc", "corp", "co", "ltd", "pbc", "gmbh",
    "sa", "ag", "plc", "limited", "incorporated",
}

ATS_DORK = (
    "(site:boards.greenhouse.io OR site:jobs.lever.co "
    "OR site:jobs.ashbyhq.com OR site:apply.workable.com "
    "OR site:jobs.smartrecruiters.com)"
)


def _normalize(s: str) -> str:
    """Lowercase, strip punctuation + corp suffixes, return [a-z0-9] only."""
    s = (s or "").lower().strip()
    s = re.sub(r"[\"'.,()]", " ", s)
    tokens = [t for t in s.split() if t and t not in CORP_SUFFIXES]
    return re.sub(r"[^a-z0-9]", "", "".join(tokens))


def _extract_slug(url: str) -> Optional[str]:
    parts = [p for p in urlparse(url).path.split("/") if p]
    if not parts:
        return None
    return unquote(parts[0])


def _slug_matches(company: str, slug: str) -> bool:
    """Strict-ish match: normalized slug == normalized company OR starts with it."""
    n_co = _normalize(company)
    n_slug = _normalize(slug)
    if not n_co or not n_slug:
        return False
    if n_co == n_slug:
        return True
    # runway -> runwayml; stripeinc -> stripeinc matches stripe
    if n_slug.startswith(n_co):
        return True
    if n_co.startswith(n_slug):
        # short slugs ("ramp" slug for "Ramp") — n_co could equal n_slug already
        return True
    return False


def build_dork(company: str) -> str:
    return f'"{company}" {ATS_DORK}'


def resolve(
    company: str,
    provider: SearchProvider,
    top_n: int = 10,
) -> Optional[tuple[str, str]]:
    """Given a company name, return (ats, slug) or None if we can't match confidently."""
    results = provider.search(build_dork(company), limit=top_n)
    for r in results:
        ats = detect_ats(r.url)
        if not ats:
            continue
        slug = _extract_slug(r.url)
        if not slug:
            continue
        if _slug_matches(company, slug):
            return (ats, slug)
    return None
