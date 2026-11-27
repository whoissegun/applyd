"""Pilot: find the hiring manager / recruiter for recent applications via Parvenu.

Read-only with respect to the outside world except for Parvenu lookups:
nothing is emailed. Results go to data/outreach/parvenu_pilot.json so the hit
rate and the share of executive fallbacks can be judged before building
outreach on top of it.

    PYTHONPATH=src .venv/bin/python scripts/pilot_parvenu.py --limit 10
    PYTHONPATH=src .venv/bin/python scripts/pilot_parvenu.py --limit 10 --dry-run

Requires PARVENU_API_KEY in .env (not needed for --dry-run).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

import httpx

from applyd.config import load_env

API_URL = "https://api.parvenu.io/find_hiring_manager"
OUT = Path("data/outreach/parvenu_pilot.json")

# Domains that are never the employer's own site.
NOT_EMPLOYER = re.compile(
    r"(?:^|\.)(?:linkedin|indeed|glassdoor|greenhouse|lever|ashbyhq|workable|smartrecruiters|"
    r"simplify|wellfound|angel|crunchbase|wikipedia|bloomberg|zoominfo|builtin|ycombinator|"
    r"twitter|x|facebook|instagram|youtube|github|medium|pitchbook|levels|teamblind|"
    r"tracxn|cbinsights|dnb|apollo|rocketreach|signalhire|google|apple|microsoft)\.",
    re.I,
)
SHARED_MAILBOX_HOSTS = re.compile(r"(?:gmail|outlook|hotmail|yahoo|greenhouse|lever|ashbyhq|workablemail)\.", re.I)


def reports_to_title(title: str) -> str:
    """Who a role most plausibly reports to; Parvenu accepts '&'-joined titles."""
    t = title.casefold()
    if re.search(r"machine learning|\bml\b|\bai\b|research", t):
        return "Engineering Manager & Head of AI & Head of Machine Learning & CTO"
    if re.search(r"ios|android|mobile", t):
        return "Engineering Manager & Head of Mobile & CTO"
    if re.search(r"product engineer|forward deployed|solutions", t):
        return "Engineering Manager & Head of Engineering & CTO"
    return "Engineering Manager & Head of Engineering & VP of Engineering & CTO"


def domain_from_description(description: str, company: str) -> str | None:
    """Employer domain from emails in the posting (careers@kaluza.com)."""
    hosts = Counter(
        m.lower() for m in re.findall(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)", description or "")
        if not SHARED_MAILBOX_HOSTS.search(m)
    )
    key = re.sub(r"[^a-z0-9]", "", company.casefold())
    for host, _ in hosts.most_common():
        root = host.split(".")[-2] if host.count(".") >= 1 else host
        if key and (key[:5] in re.sub(r"[^a-z0-9]", "", host) or root in key):
            return ".".join(host.split(".")[-2:]) if not host.endswith((".co.uk", ".com.au")) else ".".join(host.split(".")[-3:])
    return None


def domain_from_search(company: str, search) -> str | None:
    if search is None:
        return None
    try:
        results = search.search(f"{company} official website", limit=8)
    except Exception:  # noqa: BLE001
        return None
    key = re.sub(r"[^a-z0-9]", "", company.casefold())
    candidates = []
    for r in results:
        host = (urlparse(r.url).hostname or "").lower().removeprefix("www.")
        if not host or NOT_EMPLOYER.search("." + host):
            continue
        score = 2 if key and key[:5] in re.sub(r"[^a-z0-9]", "", host) else 0
        candidates.append((score, host))
    if not candidates:
        return None
    candidates.sort(key=lambda c: -c[0])
    return candidates[0][1] if candidates[0][0] > 0 else None


def recent_applied(limit: int) -> list[dict]:
    db = sqlite3.connect("data/applyd.sqlite3")
    db.row_factory = sqlite3.Row
    rows = db.execute(
        """SELECT j.id, j.company, j.title, j.description, j.locations_json, ap.updated_at
           FROM jobs j JOIN applications ap ON ap.job_id = j.id
           WHERE ap.status = 'applied'
           ORDER BY ap.updated_at DESC"""
    ).fetchall()
    seen, picked = set(), []
    for row in rows:
        key = row["company"].casefold()
        if key in seen:
            continue
        seen.add(key)
        picked.append(dict(row))
        if len(picked) >= limit:
            break
    return picked


def lookup(api_key: str, payload: dict) -> dict:
    body = dict(payload, api_key=api_key)
    for attempt in range(3):
        resp = httpx.post(API_URL, json=body, timeout=120)
        try:
            data = resp.json()
        except ValueError:
            data = {"status": "error", "error": {"code": "http_error", "message": resp.text[:200]}}
        throttled = (data.get("error") or {}).get("code") == "http_error" and resp.status_code == 429
        if not throttled:
            data["_http"] = resp.status_code
            return data
        time.sleep(2 ** attempt)
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true", help="resolve domains and payloads only; no Parvenu calls")
    args = parser.parse_args()
    load_env()

    api_key = os.environ.get("PARVENU_API_KEY", "").strip()
    if not args.dry_run and not api_key:
        print("✗ PARVENU_API_KEY is not set in .env", file=sys.stderr)
        return 2
    try:
        from applyd.discovery.search.brave import BraveSearch
        search = BraveSearch()
    except Exception:  # noqa: BLE001
        search = None

    results = []
    for job in recent_applied(args.limit):
        domain = domain_from_description(job["description"] or "", job["company"]) \
            or domain_from_search(job["company"], search)
        payload = {
            "organization_name": job["company"],
            "reports_to_title": reports_to_title(job["title"]),
        }
        if domain:
            payload["domain"] = domain
        row = {"job_id": job["id"], "company": job["company"], "title": job["title"],
               "domain": domain, "request": payload}
        if not domain:
            row["outcome"] = "no_domain"
        elif args.dry_run:
            row["outcome"] = "dry_run"
        else:
            resp = lookup(api_key, payload)
            row["outcome"] = resp.get("status")
            row["response"] = {k: v for k, v in resp.items() if k != "api_key"}
            if resp.get("status") == "error":
                code = (resp.get("error") or {}).get("code")
                if resp.get("_http") == 402:
                    results.append(row)
                    print("✗ Parvenu balance under 1 credit; stopping", file=sys.stderr)
                    break
                print(f"  ⚠ {job['company']}: {code} {(resp.get('error') or {}).get('message')}", file=sys.stderr)
            time.sleep(0.3)
        results.append(row)
        person = (row.get("response") or {}).get("data") or {}
        print(f"{row['outcome']:10} | {job['company'][:22]:22} | {domain or '-':24} | "
              f"{person.get('person_name', '')[:22]:22} | {person.get('current_title', '')[:34]:34} | "
              f"{person.get('hiring_manager_source', '')}", file=sys.stderr)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    outcomes = Counter(r["outcome"] for r in results)
    sources = Counter(((r.get("response") or {}).get("data") or {}).get("hiring_manager_source")
                      for r in results if r["outcome"] == "success")
    credits = sum(float((r.get("response") or {}).get("credits_used") or 0) for r in results)
    print(f"\n✓ {len(results)} companies | outcomes {dict(outcomes)} | sources {dict(sources)} | "
          f"credits used {credits:g} | saved {OUT}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
