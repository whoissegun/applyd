from __future__ import annotations

from typing import Optional, Sequence
from urllib.parse import unquote, urlparse

import httpx

from ...models import Job
from .._base import http_client
from ..ats import ATS_MODULES
from ..cache import BroadSearchCache
from ..routing import detect_ats
from ..search.base import SearchProvider


DEFAULT_DORKS = [
    "software engineer",
    "machine learning engineer",
    "backend engineer",
    "frontend engineer",
    "new grad software",
    "software engineer intern",
]

ATS_SITE_RESTRICT = (
    "(site:boards.greenhouse.io OR site:jobs.lever.co "
    "OR site:jobs.ashbyhq.com OR site:apply.workable.com "
    "OR site:jobs.smartrecruiters.com)"
)


def _extract_slug(url: str) -> Optional[str]:
    parts = [p for p in urlparse(url).path.split("/") if p]
    return unquote(parts[0]) if parts else None


def build_dork(keyword: str) -> str:
    return f'"{keyword}" {ATS_SITE_RESTRICT}'


def discover(
    provider: Optional[SearchProvider] = None,
    keyword_queries: Optional[Sequence[str]] = None,
    results_per_query: int = 10,
    client: Optional[httpx.Client] = None,
    cache: Optional[BroadSearchCache] = None,
) -> tuple[list[Job], dict]:
    """Run broad dorks, extract (ATS, slug) pairs, fetch each company via ATS API.

    If `cache` is given, we check each dork there first (respects TTL). Only
    dorks with a cache miss or expired entry hit the search provider. That
    keeps Brave usage low when `discover` runs frequently.

    If `provider` is None AND the cache has everything, we still make progress.
    If both are missing for a keyword, we skip that keyword.
    """
    queries = list(keyword_queries) if keyword_queries is not None else list(DEFAULT_DORKS)

    discovered: set[tuple[str, str]] = set()
    queries_run = 0
    cache_hits = 0
    search_errors = 0
    skipped = 0

    with http_client(client) as c:
        for kw in queries:
            if cache is not None:
                hit = cache.get(kw)
                if hit is not None:
                    cache_hits += 1
                    discovered.update(hit)
                    continue
            if provider is None:
                skipped += 1
                continue
            queries_run += 1
            try:
                results = provider.search(build_dork(kw), limit=results_per_query)
            except Exception:
                search_errors += 1
                continue
            pairs: list[tuple[str, str]] = []
            for r in results:
                ats = detect_ats(r.url)
                slug = _extract_slug(r.url)
                if ats and slug:
                    pairs.append((ats, slug))
            discovered.update(pairs)
            if cache is not None:
                cache.set(kw, pairs)

        jobs: list[Job] = []
        fetch_errors = 0
        per_ats: dict[str, int] = {}
        for ats, slug in sorted(discovered):
            module = ATS_MODULES.get(ats)
            if module is None:
                continue
            try:
                fetched = module.fetch(slug, c)
                jobs.extend(fetched)
                per_ats[ats] = per_ats.get(ats, 0) + len(fetched)
            except Exception:
                fetch_errors += 1
                continue

    stats = {
        "queries_total": len(queries),
        "queries_run": queries_run,
        "cache_hits": cache_hits,
        "skipped_no_provider": skipped,
        "search_errors": search_errors,
        "fetch_errors": fetch_errors,
        "discovered_companies": len(discovered),
        "per_ats": per_ats,
    }
    return jobs, stats
