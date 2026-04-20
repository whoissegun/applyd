from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import httpx

from ...models import Job
from .._base import http_client, parse_iso


def fetch(company: str, client: Optional[httpx.Client] = None) -> list[Job]:
    with http_client(client) as c:
        entries: list[dict] = []
        offset = 0
        while True:
            resp = c.get(
                f"https://api.smartrecruiters.com/v1/companies/{company}/postings"
                f"?limit=100&offset={offset}",
                follow_redirects=True,
            )
            if resp.status_code == 404:
                return []
            resp.raise_for_status()
            data = resp.json()
            content = data.get("content", [])
            entries.extend(content)
            total = data.get("totalFound", 0)
            offset += len(content)
            if not content or offset >= total:
                break

    jobs: list[Job] = []
    now = datetime.now(timezone.utc)
    for entry in entries:
        ext_id = str(entry.get("id") or entry.get("uuid") or "")
        if not ext_id:
            continue

        locations: list[str] = []
        loc = entry.get("location") or {}
        is_remote = False
        if isinstance(loc, dict):
            is_remote = bool(loc.get("remote"))
            city = loc.get("city")
            country = loc.get("country")
            bits = [b for b in (city, country) if b]
            if bits:
                locations.append(", ".join(str(b) for b in bits))
        remote = is_remote or any("remote" in l.lower() for l in locations)

        posted_at = parse_iso(entry.get("releasedDate") or entry.get("createdOn"))

        ref_num = entry.get("refNumber") or ext_id
        jobs.append(
            Job(
                id=f"smartrecruiters:{company}:{ext_id}",
                source="smartrecruiters",
                external_id=ext_id,
                company=company,
                title=str(entry.get("name") or ""),
                url=f"https://jobs.smartrecruiters.com/{company}/{ref_num}",
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
