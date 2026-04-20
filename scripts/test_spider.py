"""Compare plain httpx vs spider.cloud on 4 real non-ATS job URLs from our store."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import httpx

# load .env
for line in Path(".env").read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

SPIDER_KEY = os.environ["SPIDER_API_KEY"]

URLS = [
    ("Stripe (may be SSR)",
     "https://stripe.com/jobs/listing/software-engineer-new-grad/7210112"),
    ("TikTok (custom CSR)",
     "https://lifeattiktok.com/search/7535220808265681159"),
    ("NVIDIA Workday (SPA + protection)",
     "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/US-CA-Santa-Clara/Developer-Technology-Engineer--Public-Sector---New-College-Grad-2026_JR2008990"),
    ("Oracle HCM (heavy SPA)",
     "https://hdhe.fa.em3.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/400045311"),
]


def plain_fetch(url):
    t0 = time.time()
    try:
        r = httpx.get(url, follow_redirects=True, timeout=20.0, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        return {
            "ok": r.status_code == 200,
            "status": r.status_code,
            "bytes": len(r.text),
            "ms": int((time.time() - t0) * 1000),
            "body": r.text[:400],
        }
    except Exception as e:
        return {"ok": False, "status": "ERR", "bytes": 0, "ms": int((time.time()-t0)*1000),
                "body": f"{type(e).__name__}: {e}"}


def spider_fetch(url, chrome_only=False):
    t0 = time.time()
    body = {
        "url": url,
        "request": "chrome" if chrome_only else "smart",
        "return_format": "markdown",
        "proxy_enabled": True,
    }
    try:
        r = httpx.post(
            "https://api.spider.cloud/v1/scrape",
            json=body,
            headers={
                "Authorization": f"Bearer {SPIDER_KEY}",
                "Content-Type": "application/json",
            },
            timeout=120.0,
        )
        ms = int((time.time() - t0) * 1000)
        if r.status_code != 200:
            return {"ok": False, "status": r.status_code, "bytes": 0, "ms": ms,
                    "body": r.text[:300]}
        data = r.json()
        # response shape: usually a list with one dict, or dict with "content"/"data"
        if isinstance(data, list) and data:
            item = data[0]
        elif isinstance(data, dict):
            item = data
        else:
            return {"ok": False, "status": 200, "bytes": 0, "ms": ms,
                    "body": f"unexpected type: {type(data)}"}
        content = item.get("content") or item.get("data") or ""
        cost = item.get("costs") or item.get("cost") or "?"
        return {
            "ok": bool(content),
            "status": 200,
            "bytes": len(content),
            "ms": ms,
            "body": (content if isinstance(content, str) else str(content))[:400],
            "cost": cost,
            "raw_keys": list(item.keys()),
        }
    except Exception as e:
        return {"ok": False, "status": "ERR", "bytes": 0, "ms": int((time.time()-t0)*1000),
                "body": f"{type(e).__name__}: {e}"}


def main():
    for name, url in URLS:
        print(f"\n=== {name} ===")
        print(f"    {url}")

        p = plain_fetch(url)
        print(f"  plain httpx:    status={p['status']}  {p['bytes']} bytes  {p['ms']}ms")
        if p["bytes"] < 500:
            print(f"    body: {p['body']!r}")

        s = spider_fetch(url)
        print(f"  spider smart:   status={s['status']}  {s['bytes']} chars markdown  {s['ms']}ms"
              + (f"  cost={s.get('cost')}" if s.get("cost") else ""))
        if s.get("raw_keys"):
            print(f"    response keys: {s['raw_keys']}")
        if s["bytes"] < 2000 and s["bytes"] > 0:
            print(f"    preview: {s['body'][:300]}...")
        elif s["bytes"] == 0:
            print(f"    response: {s['body']!r}")


if __name__ == "__main__":
    main()
