from __future__ import annotations

from typing import Optional

import httpx
import trafilatura

from ..discovery._base import http_client
from ..discovery.ats import ATS_MODULES
from ..discovery.routing import parse_ats_url
from ..models import Job
from .spider import SpiderClient


# session-level type alias: {(ats, company_slug): [Job, ...]}
AtsBoardCache = dict


MIN_USEFUL_CHARS = 500

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_BROWSER_HEADERS = {
    "User-Agent": _BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _tier2_httpx(url: str, client: Optional[httpx.Client]) -> Optional[str]:
    """Plain httpx + trafilatura for SSR pages. Returns None if unusable."""
    try:
        with http_client(client) as c:
            r = c.get(url, follow_redirects=True, timeout=20.0, headers=_BROWSER_HEADERS)
        if r.status_code != 200 or not r.text:
            return None
        html = r.text
    except Exception:
        return None
    text = trafilatura.extract(
        html, output_format="markdown", include_tables=False, favor_precision=True,
    ) or ""
    return text if len(text) >= MIN_USEFUL_CHARS else None


def _job_id_matches(job: Job, target_id: str) -> bool:
    if not target_id:
        return False
    if job.external_id == target_id:
        return True
    # Some listings embed id differently; check URL as a fallback
    if job.url and target_id in job.url:
        return True
    return False


def _fetch_smartrecruiters_description(
    company: str, internal_id: str, client: Optional[httpx.Client],
) -> Optional[str]:
    """SmartRecruiters bulk list omits descriptions; hit the per-job endpoint."""
    url = f"https://api.smartrecruiters.com/v1/companies/{company}/postings/{internal_id}"
    try:
        with http_client(client) as c:
            r = c.get(url, follow_redirects=True, timeout=15.0)
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception:
        return None
    sections = (data.get("jobAd") or {}).get("sections") or {}
    parts: list[str] = []
    for key in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"):
        block = sections.get(key)
        if isinstance(block, dict):
            text = block.get("text")
            if text:
                parts.append(str(text))
    return "\n\n".join(parts) if parts else None


def _tier1_ats_api(
    url: str,
    client: Optional[httpx.Client],
    board_cache: AtsBoardCache,
) -> Optional[str]:
    """If URL is on a known ATS, pull the job's description via that ATS's bulk API.
    Falls back to per-job endpoints for ATSes whose bulk list omits descriptions
    (e.g., SmartRecruiters). Caches full-board fetches per (ats, company)."""
    parsed = parse_ats_url(url)
    if not parsed:
        return None
    ats, company, job_id = parsed
    module = ATS_MODULES.get(ats)
    if module is None:
        return None

    key = (ats, company)
    if key not in board_cache:
        try:
            board_cache[key] = module.fetch(company, client=client)
        except Exception:
            board_cache[key] = []

    target_job: Optional[Job] = None
    for job in board_cache[key]:
        if _job_id_matches(job, job_id):
            target_job = job
            break
    if target_job is None:
        return None

    # bulk response already had a description → use it
    desc = target_job.description
    if desc and len(desc) >= MIN_USEFUL_CHARS:
        return desc

    # per-ATS fallback for bulk-omits-description
    if ats == "smartrecruiters":
        desc = _fetch_smartrecruiters_description(company, target_job.external_id, client)
        if desc and len(desc) >= MIN_USEFUL_CHARS:
            target_job.description = desc  # update cached copy
            return desc

    return None


def _tier3_spider(url: str, spider: SpiderClient, chrome: bool) -> Optional[str]:
    try:
        content = spider.scrape(url, chrome=chrome)
    except Exception:
        return None
    return content if len(content) >= MIN_USEFUL_CHARS else None


def fetch_text(
    url: str,
    spider: Optional[SpiderClient] = None,
    client: Optional[httpx.Client] = None,
    board_cache: Optional[AtsBoardCache] = None,
) -> tuple[str, str, Optional[str]]:
    """
    Cascade: ATS bulk API → httpx+trafilatura → spider smart → spider chrome → failed.
    Returns (text, tier, error). tier is one of:
      "ats" | "http" | "spider" | "spider-chrome" | "failed"
    """
    if board_cache is None:
        board_cache = {}

    # Tier 1: ATS API (free, usually fastest on second hit per board)
    text = _tier1_ats_api(url, client, board_cache)
    if text:
        return text, "ats", None

    # Tier 2: plain httpx + trafilatura (free)
    text = _tier2_httpx(url, client)
    if text:
        return text, "http", None

    if spider is None:
        return "", "failed", "tier 1/2 empty and no spider fallback"

    # Tier 3a: spider.cloud smart (paid, cheap)
    text = _tier3_spider(url, spider, chrome=False)
    if text:
        return text, "spider", None

    # Tier 3b: spider.cloud chrome (paid, slower, renders full SPA)
    text = _tier3_spider(url, spider, chrome=True)
    if text:
        return text, "spider-chrome", None

    return "", "failed", "all tiers returned empty or too-short content"
