from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import httpx

from ...models import Job
from .._base import http_client


def fetch(company: str, client: Optional[httpx.Client] = None) -> list[Job]:
    url = f"https://api.lever.co/v0/postings/{company}?mode=json"
    with http_client(client) as c:
        resp = c.get(url, follow_redirects=True)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        data = resp.json()

    jobs: list[Job] = []
    now = datetime.now(timezone.utc)
    for entry in data:
        ext_id = str(entry.get("id") or "")
        if not ext_id:
            continue

        cats = entry.get("categories") or {}
        location_str = cats.get("location") if isinstance(cats, dict) else None
        locations = [str(location_str)] if location_str else []

        workplace = str(entry.get("workplaceType") or "")
        remote = "remote" in workplace.lower() or any(
            "remote" in loc.lower() for loc in locations
        )

        # Lever uses ms-epoch, not ISO
        posted_at: Optional[datetime] = None
        created = entry.get("createdAt")
        if created:
            try:
                posted_at = datetime.fromtimestamp(int(created) / 1000, tz=timezone.utc)
            except (ValueError, TypeError):
                posted_at = None

        jobs.append(
            Job(
                id=f"lever:{company}:{ext_id}",
                source="lever",
                external_id=ext_id,
                company=company,
                title=str(entry.get("text") or ""),
                url=str(entry.get("hostedUrl") or entry.get("applyUrl") or ""),
                locations=locations,
                remote=remote,
                posted_at=posted_at,
                description=str(
                    entry.get("descriptionPlain") or entry.get("description") or ""
                )
                or None,
                raw=entry,
                first_seen_at=now,
                last_seen_at=now,
            )
        )
    return jobs
