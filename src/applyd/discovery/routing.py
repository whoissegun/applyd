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


# Domains that effectively always require account creation or login
# before the apply form is reachable. `apply_gate="portal"` at URL-time.
# See filters.py comment for why this is a hardcoded list, not LLM-classified.
GATED_EXACT = {
    "linkedin.com",
    "www.linkedin.com",
    "wellfound.com",
    "www.wellfound.com",
    "angel.co",
    "www.angel.co",
    "glassdoor.com",
    "www.glassdoor.com",
    "dice.com",
    "www.dice.com",
    "indeed.com",
    "www.indeed.com",
    "ziprecruiter.com",
    "www.ziprecruiter.com",
    "joinhandshake.com",
    "www.joinhandshake.com",
}

GATED_SUFFIXES = (
    ".myworkdayjobs.com",   # Workday
    ".myworkdaysite.com",   # Workday (alt)
    ".taleo.net",           # Taleo
    ".oraclecloud.com",     # Oracle HCM / Taleo
    ".successfactors.com",  # SAP SuccessFactors
    ".icims.com",           # iCIMS
)


def detect_gate(url: str) -> Optional[str]:
    """URL-level gate detection.

    Returns "portal" for known account-required domains, else None.
    Runtime (agent) can overwrite with specific reasons like
    "signup_required", "captcha", etc.
    """
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return None
    if not host:
        return None
    if host in GATED_EXACT:
        return "portal"
    if any(host.endswith(suf) for suf in GATED_SUFFIXES):
        return "portal"
    return None


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


# Canonical posting-URL templates per ATS, keyed to the `{ats}:{slug}:{id}`
# job_id we store. Used to reconstruct a parseable ATS URL when the display
# URL is a company-careers wrapper (e.g. Stripe's stripe.com/jobs/search?
# gh_jid=NNN) that parse_ats_url can't read.
_CANONICAL_URL_TEMPLATES = {
    "greenhouse": "https://boards.greenhouse.io/{slug}/jobs/{job_id}",
    "lever": "https://jobs.lever.co/{slug}/{job_id}",
    "ashby": "https://jobs.ashbyhq.com/{slug}/{job_id}",
    "workable": "https://apply.workable.com/{slug}/j/{job_id}",
    "smartrecruiters": "https://jobs.smartrecruiters.com/{slug}/{job_id}",
}


def canonical_ats_url(job_id: str) -> Optional[str]:
    """Build a canonical, parse_ats_url-readable posting URL from a stored
    `{ats}:{slug}:{job_id}` id. Returns None for an unknown/malformed id."""
    parts = job_id.split(":", 2)
    if len(parts) != 3:
        return None
    ats, slug, jid = parts
    template = _CANONICAL_URL_TEMPLATES.get(ats)
    if not template or not slug or not jid:
        return None
    return template.format(slug=slug, job_id=jid)


def preferred_apply_url(job_id: str, display_url: str) -> str:
    """The best URL for the apply agent to navigate to.

    Greenhouse's stored/display URL is often a company-careers wrapper
    (stripe.com/jobs/search?gh_jid=NNN) — a search page, not the form — or a
    JD page that needs an extra "Apply" click. The `embed/job_app` endpoint
    renders the fillable application form directly for EVERY greenhouse board
    (verified on self-hosted Stripe and direct-board Anthropic), and 404s for
    dead postings. Route greenhouse applies straight to it; everything else
    keeps its display URL (lever/ashby pages already land on the form).
    """
    parts = job_id.split(":", 2)
    if len(parts) == 3 and parts[0] == "greenhouse":
        _, slug, jid = parts
        if slug and jid:
            return f"https://job-boards.greenhouse.io/embed/job_app?token={jid}&for={slug}"
    return display_url


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
