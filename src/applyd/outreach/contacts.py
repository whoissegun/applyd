"""Hiring-contact lookup. Parvenu is the default provider; keep it swappable."""
from __future__ import annotations

import os
import re
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

PARVENU_URL = "https://api.parvenu.io/find_hiring_manager"

NOT_EMPLOYER = re.compile(
    r"(?:^|\.)(?:linkedin|indeed|glassdoor|greenhouse|lever|ashbyhq|workable|smartrecruiters|"
    r"simplify|wellfound|angel|crunchbase|wikipedia|bloomberg|zoominfo|builtin|ycombinator|"
    r"twitter|x|facebook|instagram|youtube|github|medium|pitchbook|levels|teamblind|"
    r"tracxn|cbinsights|dnb|apollo|rocketreach|signalhire|google|apple|microsoft)\.",
    re.I,
)
SHARED_MAILBOX_HOSTS = re.compile(
    r"(?:gmail|outlook|hotmail|yahoo|greenhouse|lever|ashbyhq|workablemail)\.", re.I)


@dataclass(frozen=True)
class Contact:
    status: str                      # "found" | "not_found" | "error"
    first_name: str = ""
    full_name: str = ""
    title: str = ""
    email: str = ""
    email_status: str = ""           # "valid" | "catch_all" | ...
    linkedin_url: str = ""
    source: str = ""                 # job_poster | reports_to | fallback_title
    credits_used: float = 0.0
    detail: str = ""


def _key(company: str) -> str:
    return re.sub(r"[^a-z0-9]", "", company.casefold())


def domain_from_description(description: str, company: str) -> Optional[str]:
    """Employer domain from emails in the posting (careers@kaluza.com)."""
    hosts = Counter(
        h.lower() for h in re.findall(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)", description or "")
        if not SHARED_MAILBOX_HOSTS.search(h)
    )
    key = _key(company)
    for host, _ in hosts.most_common():
        flat = re.sub(r"[^a-z0-9]", "", host)
        root = host.split(".")[-2] if "." in host else host
        if key and (key[:5] in flat or root in key):
            parts = host.split(".")
            keep = 3 if host.endswith((".co.uk", ".com.au")) else 2
            return ".".join(parts[-keep:])
    return None


def domain_from_search(company: str, search: Any) -> Optional[str]:
    if search is None:
        return None
    try:
        results = search.search(f"{company} official website", limit=8)
    except Exception:  # noqa: BLE001
        return None
    key = _key(company)
    for result in results:
        host = (urlparse(result.url).hostname or "").lower().removeprefix("www.")
        if host and not NOT_EMPLOYER.search("." + host) and key and key[:5] in re.sub(r"[^a-z0-9]", "", host):
            return host
    return None


def resolve_domain(company: str, description: str, search: Any = None) -> Optional[str]:
    return domain_from_description(description, company) or domain_from_search(company, search)


def reports_to_title(title: str) -> str:
    """Who a role most plausibly reports to; Parvenu accepts '&'-joined titles."""
    t = title.casefold()
    if re.search(r"machine learning|\bml\b|\bai\b|research", t):
        return "Engineering Manager & Head of AI & Head of Machine Learning & CTO"
    if re.search(r"ios|android|mobile", t):
        return "Engineering Manager & Head of Mobile & CTO"
    return "Engineering Manager & Head of Engineering & VP of Engineering & CTO"


class ParvenuFinder:
    name = "parvenu"

    def __init__(self, api_key: Optional[str] = None, client: Any = None) -> None:
        self.api_key = (api_key or os.environ.get("PARVENU_API_KEY", "")).strip()
        if not self.api_key:
            raise RuntimeError("PARVENU_API_KEY is not set")
        self._post = (client or httpx).post

    def find(self, *, company: str, title: str, domain: str) -> Contact:
        body = {"api_key": self.api_key, "domain": domain, "organization_name": company,
                "reports_to_title": reports_to_title(title)}
        data: dict = {}
        for attempt in range(3):
            try:
                resp = self._post(PARVENU_URL, json=body, timeout=120)
                data = resp.json()
            except Exception as exc:  # noqa: BLE001
                return Contact("error", detail=f"{type(exc).__name__}: {exc}"[:200])
            message = str((data.get("error") or {}).get("message") or "")
            # A freshly generated key is briefly unknown to Parvenu.
            if "No user found for the provided API key" in message and attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            break
        status = data.get("status")
        credits = float(data.get("credits_used") or 0)
        if status == "success":
            d = data.get("data") or {}
            return Contact(
                "found", first_name=str(d.get("first_name") or ""),
                full_name=str(d.get("person_name") or ""), title=str(d.get("current_title") or ""),
                email=str(d.get("email") or ""), email_status=str(d.get("email_status") or ""),
                linkedin_url=str(d.get("person_linkedin_url") or ""),
                source=str(d.get("hiring_manager_source") or ""), credits_used=credits)
        if status == "not_found":
            return Contact("not_found", credits_used=credits, detail=str(data.get("message") or ""))
        err = data.get("error") or {}
        return Contact("error", credits_used=credits,
                       detail=f"{err.get('code')}: {err.get('message')}"[:200])
