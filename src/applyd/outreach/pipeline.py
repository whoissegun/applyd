"""Find the hiring contact for an applied job and save an outreach draft."""
from __future__ import annotations

import re
import sys
from typing import Any, Mapping

from .compose import company_line, compose
from .contacts import ParvenuFinder, resolve_domain
from .drafts import save_draft


def outreach_settings(profile: Mapping[str, Any]) -> dict[str, Any]:
    settings = profile.get("outreach") if isinstance(profile.get("outreach"), Mapping) else {}
    return dict(settings)


def _company_key(company: str) -> str:
    return re.sub(r"[^a-z0-9]", "", company.casefold())


def draft_outreach_for_job(store: Any, job: Any, profile: Mapping[str, Any], *,
                           finder: Any = None, search: Any = None, llm: Any = None,
                           save: Any = save_draft, dry_run: bool = False) -> dict[str, Any]:
    """Look up the job's hiring contact and save one Gmail draft.

    One contact per company: a company already contacted (or looked up and
    not found) is skipped, so repeat applications never re-spend credits.
    Drafts are only created for verified emails unless the profile allows
    catch-all domains. Returns a summary dict; never raises.
    """
    settings = outreach_settings(profile)
    key = _company_key(job.company)
    result: dict[str, Any] = {"job_id": job.id, "company": job.company}
    if settings.get("enabled") is not True:
        return {**result, "status": "disabled"}
    prior = store.outreach_for_company(key) or store.outreach_for_job(job.id)
    if prior:
        return {**result, "status": "skipped_company_done", "previous": prior.get("status")}

    domain = resolve_domain(job.company, job.description or "", search)
    if not domain:
        store.record_outreach(job.id, key, "no_domain", provider="parvenu")
        return {**result, "status": "no_domain"}
    if dry_run:
        return {**result, "status": "dry_run", "domain": domain}
    try:
        finder = finder or ParvenuFinder()
    except RuntimeError as exc:
        return {**result, "status": "not_configured", "detail": str(exc)}

    contact = finder.find(company=job.company, title=job.title, domain=domain)
    common = dict(contact_name=contact.full_name, contact_title=contact.title,
                  contact_email=contact.email, email_status=contact.email_status,
                  source=contact.source, provider=getattr(finder, "name", "parvenu"),
                  credits_used=contact.credits_used)
    if contact.status != "found" or not contact.email:
        store.record_outreach(job.id, key, contact.status, detail=contact.detail, **common)
        return {**result, "status": contact.status, "detail": contact.detail}
    allow_catch_all = settings.get("allow_catch_all_emails") is True
    if contact.email_status != "valid" and not allow_catch_all:
        store.record_outreach(job.id, key, "unverified_email", **common)
        return {**result, "status": "unverified_email", "contact": contact.full_name,
                "email_status": contact.email_status}

    line = company_line(job.company, job.description or "", client=llm)
    email = compose(settings, company=job.company, title=job.title,
                    first_name=contact.first_name or contact.full_name.split(" ")[0],
                    company_sentence=line)
    error = save(email, contact.email)
    status = "drafted" if error is None else "draft_failed"
    store.record_outreach(job.id, key, status, detail=error, **common)
    return {**result, "status": status, "contact": contact.full_name, "title": contact.title,
            "email": contact.email, "source": contact.source, "subject": email.subject,
            "detail": error}


def report(summary: Mapping[str, Any]) -> None:
    status = summary.get("status")
    if status == "drafted":
        print(f"  ✉ outreach draft saved: {summary.get('contact')} ({summary.get('title')}) "
              f"<{summary.get('email')}> via {summary.get('source')}", file=sys.stderr, flush=True)
    elif status not in {"disabled", "skipped_company_done"}:
        print(f"  ✉ outreach: {status} {summary.get('detail') or ''}".rstrip(), file=sys.stderr, flush=True)
