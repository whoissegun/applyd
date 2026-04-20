from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import httpx

from ...models import Job
from .._base import http_client, parse_iso


def fetch(company: str, client: Optional[httpx.Client] = None) -> list[Job]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs?content=true"
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
        loc_field = entry.get("location")
        if isinstance(loc_field, dict):
            name = loc_field.get("name")
            if name:
                locations.append(str(name))
        elif loc_field:
            locations.append(str(loc_field))
        remote = any("remote" in loc.lower() for loc in locations)

        posted_at = parse_iso(entry.get("updated_at") or entry.get("first_published"))

        jobs.append(
            Job(
                id=f"greenhouse:{company}:{ext_id}",
                source="greenhouse",
                external_id=ext_id,
                company=company,
                title=str(entry.get("title") or ""),
                url=str(entry.get("absolute_url") or ""),
                locations=locations,
                remote=remote,
                posted_at=posted_at,
                description=str(entry.get("content") or "") or None,
                raw=entry,
                first_seen_at=now,
                last_seen_at=now,
            )
        )
    return jobs
