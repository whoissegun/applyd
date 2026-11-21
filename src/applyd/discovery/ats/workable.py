from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import httpx

from ...models import Job
from .._base import http_client, parse_iso


def _format_location(loc: dict) -> Optional[str]:
    bits = [loc.get("city"), loc.get("region"), loc.get("country")]
    parts = [str(b) for b in bits if b]
    return ", ".join(parts) if parts else None


def fetch_description(company: str, shortcode: str,
                      client: Optional[httpx.Client] = None) -> Optional[str]:
    """The list endpoint omits the public posting's description sections."""
    with http_client(client) as c:
        response = c.get(
            f"https://apply.workable.com/api/v2/accounts/{company}/jobs/{shortcode}",
            follow_redirects=True, timeout=15.0,
        )
        if response.status_code in (400, 404):
            return None
        response.raise_for_status()
        data = response.json()
    if str(data.get('shortcode') or '') != shortcode:
        return None
    if data.get('state') and data['state'] != 'published':
        return None
    parts = [data.get(key) for key in ('description', 'requirements', 'benefits')]
    return '\n\n'.join(part for part in parts if isinstance(part, str) and part.strip()) or None


def fetch(company: str, client: Optional[httpx.Client] = None) -> list[Job]:
    url = f"https://apply.workable.com/api/v3/accounts/{company}/jobs"
    with http_client(client) as c:
        resp = c.post(
            url,
            json={},
            headers={"Content-Type": "application/json"},
            follow_redirects=True,
            timeout=8.0,
        )
        if resp.status_code in (400, 404):
            return []
        resp.raise_for_status()
        data = resp.json()

    jobs: list[Job] = []
    now = datetime.now(timezone.utc)
    for entry in data.get("results", []):
        ext_id = str(entry.get("shortcode") or entry.get("id") or "")
        if not ext_id:
            continue
        if entry.get("state") and entry.get("state") != "published":
            continue

        locations: list[str] = []
        primary = entry.get("location")
        if isinstance(primary, dict):
            loc = _format_location(primary)
            if loc:
                locations.append(loc)
        multi = entry.get("locations")
        if isinstance(multi, list):
            for m in multi:
                if isinstance(m, dict):
                    loc = _format_location(m)
                    if loc and loc not in locations:
                        locations.append(loc)

        remote = bool(entry.get("remote")) or any(
            "remote" in loc.lower() for loc in locations
        )

        posted_at = parse_iso(entry.get("published"))

        jobs.append(
            Job(
                id=f"workable:{company}:{ext_id}",
                source="workable",
                external_id=ext_id,
                company=company,
                title=str(entry.get("title") or ""),
                url=f"https://apply.workable.com/{company}/j/{ext_id}/",
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
