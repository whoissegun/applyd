from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse


ATS_DOMAINS = {
    "boards.greenhouse.io": "greenhouse",
    "job-boards.greenhouse.io": "greenhouse",
    "jobs.lever.co": "lever",
    "jobs.ashbyhq.com": "ashby",
    "apply.workable.com": "workable",
    "jobs.smartrecruiters.com": "smartrecruiters",
    "careers.smartrecruiters.com": "smartrecruiters",
}


def detect_ats(url: str) -> Optional[str]:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return None
    if not host:
        return None
    if host in ATS_DOMAINS:
        return ATS_DOMAINS[host]
    if host.endswith(".greenhouse.io"):
        return "greenhouse"
    return None


def extract_company_slug(url: str) -> Optional[str]:
    ats = detect_ats(url)
    if not ats:
        return None
    parts = [p for p in urlparse(url).path.split("/") if p]
    return parts[0] if parts else None


def parse_ats_url(url: str) -> Optional[tuple[str, str, str]]:
    """Parse an ATS posting URL into (ats, company_slug, job_id).

    Returns None if URL isn't on a known ATS or doesn't contain a job id.
    Strips trailing /application, /apply, query strings, etc.
    """
    ats = detect_ats(url)
    if not ats:
        return None
    parts = [p for p in urlparse(url).path.split("/") if p]
    if not parts:
        return None
    company = parts[0]

    if ats == "greenhouse":
        # boards.greenhouse.io/{company}/jobs/{id}
        # job-boards.greenhouse.io/{company}/jobs/{id}
        if len(parts) >= 3 and parts[1] == "jobs":
            return (ats, company, parts[2])
        return None
    if ats == "lever":
        # jobs.lever.co/{company}/{posting_id}[/apply]
        if len(parts) >= 2:
            return (ats, company, parts[1])
        return None
    if ats == "ashby":
        # jobs.ashbyhq.com/{company}/{uuid}[/application]
        if len(parts) >= 2:
            return (ats, company, parts[1])
        return None
    if ats == "workable":
        # apply.workable.com/{company}/j/{shortcode}[/]
        if len(parts) >= 3 and parts[1] == "j":
            return (ats, company, parts[2])
        if len(parts) >= 2:
            return (ats, company, parts[1])
        return None
    if ats == "smartrecruiters":
        # jobs.smartrecruiters.com/{company}/{posting_id}
        if len(parts) >= 2:
            return (ats, company, parts[1])
        return None
    return None
