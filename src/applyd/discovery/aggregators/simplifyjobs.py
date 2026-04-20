from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import httpx

from ...models import Job
from .._base import http_client


LISTINGS_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/"
    "dev/.github/scripts/listings.json"
)


def fetch(client: Optional[httpx.Client] = None) -> list[Job]:
    with http_client(client) as c:
        resp = c.get(LISTINGS_URL, follow_redirects=True, timeout=60.0)
        resp.raise_for_status()
        raw = resp.json()

    jobs: list[Job] = []
    now = datetime.now(timezone.utc)
    for entry in raw:
        if not entry.get("active", True):
            continue
        if entry.get("is_visible") is False:
            continue
        ext_id = str(entry.get("id") or "")
        if not ext_id:
            continue
        posted_at: Optional[datetime] = None
        posted_ts = entry.get("date_posted")
        if posted_ts:
            try:
                posted_at = datetime.fromtimestamp(int(posted_ts), tz=timezone.utc)
            except (ValueError, TypeError):
                posted_at = None
        locations = [str(loc) for loc in (entry.get("locations") or []) if loc]
        remote = any("remote" in loc.lower() for loc in locations)
        jobs.append(
            Job(
                id=f"simplifyjobs:{ext_id}",
                source="simplifyjobs",
                external_id=ext_id,
                company=str(entry.get("company_name") or ""),
                title=str(entry.get("title") or ""),
                url=str(entry.get("url") or ""),
                locations=locations,
                remote=remote,
                posted_at=posted_at,
                description=None,
                raw=entry,
                first_seen_at=now,
                last_seen_at=now,
            )
        )
    return jobs
