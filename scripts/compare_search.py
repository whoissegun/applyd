"""Compare Brave Search API vs Serper for our company->ATS resolution dork."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx


def load_env() -> None:
    env_path = Path(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k.strip(), v)


ATS_DOMAINS = {
    "boards.greenhouse.io": "greenhouse",
    "job-boards.greenhouse.io": "greenhouse",
    "jobs.lever.co": "lever",
    "jobs.ashbyhq.com": "ashby",
    "apply.workable.com": "workable",
    "jobs.smartrecruiters.com": "smartrecruiters",
    "careers.smartrecruiters.com": "smartrecruiters",
}


def detect_ats(url: str):
    host = (urlparse(url).hostname or "").lower()
    if host in ATS_DOMAINS:
        return ATS_DOMAINS[host]
    if host.endswith(".greenhouse.io"):
        return "greenhouse"
    return None


def extract_slug(url: str):
    parts = [p for p in urlparse(url).path.split("/") if p]
    return parts[0] if parts else None


def make_dork(company: str) -> str:
    return (
        f'"{company}" (site:boards.greenhouse.io OR site:jobs.lever.co '
        f"OR site:jobs.ashbyhq.com OR site:apply.workable.com "
        f"OR site:jobs.smartrecruiters.com)"
    )


def brave_search(query: str, key: str, n: int = 10):
    r = httpx.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": n},
        headers={"X-Subscription-Token": key, "Accept": "application/json"},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    return [
        {"url": hit.get("url", ""), "title": hit.get("title", "")}
        for hit in (data.get("web") or {}).get("results", [])
    ]


def serper_search(query: str, key: str, n: int = 10):
    r = httpx.post(
        "https://google.serper.dev/search",
        json={"q": query, "num": n},
        headers={"X-API-KEY": key, "Content-Type": "application/json"},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    return [
        {"url": hit.get("link", ""), "title": hit.get("title", "")}
        for hit in data.get("organic", [])
    ]


def evaluate(results):
    ats_hits = []
    first_ats, first_slug = None, None
    for r in results:
        ats = detect_ats(r["url"])
        if ats:
            slug = extract_slug(r["url"])
            ats_hits.append((ats, slug, r["url"]))
            if first_ats is None:
                first_ats, first_slug = ats, slug
    return {
        "total": len(results),
        "ats_hits": len(ats_hits),
        "first_ats": first_ats,
        "first_slug": first_slug,
        "ats_details": ats_hits[:3],
        "top_urls": [r["url"] for r in results[:3]],
    }


COMPANIES = [
    ("Stripe", "greenhouse"),
    ("Anthropic", "greenhouse"),
    ("Airbnb", "greenhouse"),
    ("Figma", "greenhouse"),
    ("Runway", "ashby"),
    ("OpenAI", "ashby"),
    ("Linear", "ashby"),
    ("Ramp", "ashby"),
    ("Palantir", "lever"),
    ("Plaid", "lever"),
    ("Mercury", "ashby"),
    ("Retool", None),   # unknown to me, let's see
    ("Vercel", None),   # unknown
    ("Replicate", None),
    ("Cohere", None),
    ("Pave", None),
]


def fmt(x):
    if "error" in x:
        return f"ERR: {x['error'][:45]}"
    ats = x["first_ats"] or "-"
    slug = x["first_slug"] or "-"
    return f"{ats:15s} slug={slug:20.20s} (ATS hits {x['ats_hits']}/{x['total']})"


def main() -> int:
    load_env()
    brave_key = os.environ.get("BRAVE_SEARCH_API_KEY")
    serper_key = os.environ.get("SERPER_API_KEY")
    if not brave_key:
        print("missing BRAVE_SEARCH_API_KEY", file=sys.stderr)
        return 1
    if not serper_key:
        print("missing SERPER_API_KEY", file=sys.stderr)
        return 1

    print(f"{'company':12s} | {'expected':15s} | {'Brave':55s} | {'Serper':55s}")
    print("-" * 145)

    brave_correct = serper_correct = brave_any = serper_any = total = 0

    for company, expected in COMPANIES:
        q = make_dork(company)
        try:
            b = evaluate(brave_search(q, brave_key))
        except Exception as e:
            b = {"error": f"{type(e).__name__}: {e}"}
        try:
            s = evaluate(serper_search(q, serper_key))
        except Exception as e:
            s = {"error": f"{type(e).__name__}: {e}"}

        print(f"{company:12s} | {expected or '?':15s} | {fmt(b):55s} | {fmt(s):55s}")

        total += 1
        if "error" not in b:
            if b["first_ats"]: brave_any += 1
            if expected and b["first_ats"] == expected: brave_correct += 1
        if "error" not in s:
            if s["first_ats"]: serper_any += 1
            if expected and s["first_ats"] == expected: serper_correct += 1

        time.sleep(0.5)  # politeness

    known = sum(1 for _, e in COMPANIES if e)
    print()
    print(f"Totals across {total} companies:")
    print(f"  Brave:  top-1 ATS = {brave_any}/{total}, correct ATS (of {known} known) = {brave_correct}/{known}")
    print(f"  Serper: top-1 ATS = {serper_any}/{total}, correct ATS (of {known} known) = {serper_correct}/{known}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
