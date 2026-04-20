from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import httpx

from ...models import Job
from .._base import http_client, parse_iso


def fetch(company: str, client: Optional[httpx.Client] = None) -> list[Job]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{company}"
    with http_client(client) as c:
        resp = c.get(url, follow_redirects=True)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        data = resp.json()

    jobs: list[Job] = []
    now = datetime.now(timezone.utc)
    for entry in data.get("jobs", []):
        ext_id = str(entry.get("id") or "")
        if not ext_id:
            continue

        locations: list[str] = []
        primary = entry.get("location")
        if primary:
            locations.append(str(primary))
        secondary = entry.get("secondaryLocations") or []
        if isinstance(secondary, list):
            for s in secondary:
                if isinstance(s, dict):
                    loc = s.get("location") or s.get("locationName")
                    if loc:
                        locations.append(str(loc))
                elif s:
                    locations.append(str(s))

        remote = bool(entry.get("isRemote")) or any(
            "remote" in loc.lower() for loc in locations
        )

        posted_at = parse_iso(entry.get("publishedAt"))

        jobs.append(
            Job(
                id=f"ashby:{company}:{ext_id}",
                source="ashby",
                external_id=ext_id,
                company=company,
                title=str(entry.get("title") or ""),
                url=str(entry.get("jobUrl") or entry.get("applyUrl") or ""),
                locations=locations,
                remote=remote,
                posted_at=posted_at,
                description=str(
                    entry.get("descriptionPlain") or entry.get("descriptionHtml") or ""
                )
                or None,
                raw=entry,
                first_seen_at=now,
                last_seen_at=now,
            )
        )
    return jobs
