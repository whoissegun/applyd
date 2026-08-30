"""Conservative, network-free cross-source job deduplication."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from .discovery import ATS_MODULES
from .discovery.routing import parse_ats_url
from .models import Job


@dataclass(frozen=True)
class DuplicateAssignment:
    job_id: str
    canonical_job_id: str
    method: str
    duplicate_key: str


def _normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def canonical_ats_identity(job: Job) -> str | None:
    url = str(job.raw.get("_applyd_retrieval_url") or job.url)
    parsed = parse_ats_url(url)
    if not parsed:
        return None
    ats, company, job_id = parsed
    if not job_id:
        return None
    return ":".join((ats.casefold(), company.casefold(), job_id.casefold()))


def exact_posting_fingerprint(job: Job) -> str | None:
    if not job.description:
        return None
    payload = {
        "company": _normalized(job.company),
        "title": _normalized(job.title),
        "locations": sorted(_normalized(value) for value in job.locations),
        "description": _normalized(job.description),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _timestamp(value: datetime | None) -> float:
    return value.timestamp() if value is not None else 0.0


def _canonical(group: list[Job]) -> Job:
    """Choose a stable, application-ready representative without fetching."""
    return max(
        group,
        key=lambda job: (
            job.source in ATS_MODULES,
            parse_ats_url(job.url) is not None,
            job.apply_gate is None,
            bool(job.description),
            _timestamp(job.posted_at),
            job.id,
        ),
    )


def deduplicate_jobs(jobs: Iterable[Job]) -> list[DuplicateAssignment]:
    """Return duplicate-to-canonical mappings; never deletes or merges rows."""
    values = list(jobs)
    ats_keys = {job.id: canonical_ats_identity(job) for job in values}
    assignments: dict[str, DuplicateAssignment] = {}

    by_ats: dict[str, list[Job]] = {}
    for job in values:
        key = ats_keys[job.id]
        if key:
            by_ats.setdefault(key, []).append(job)
    for key, group in by_ats.items():
        if len(group) < 2:
            continue
        canonical = _canonical(group)
        for job in group:
            if job.id != canonical.id:
                assignments[job.id] = DuplicateAssignment(
                    job.id, canonical.id, "ats_identity", key
                )

    by_fingerprint: dict[str, list[Job]] = {}
    for job in values:
        fingerprint = exact_posting_fingerprint(job)
        if fingerprint:
            by_fingerprint.setdefault(fingerprint, []).append(job)
    for fingerprint, group in by_fingerprint.items():
        if len(group) < 2:
            continue
        distinct_ats = {ats_keys[job.id] for job in group if ats_keys[job.id]}
        # Two different ATS IDs may be separate requisitions with copied text.
        # Exact content cannot override conflicting stable identities.
        if len(distinct_ats) > 1:
            continue
        canonical = _canonical(group)
        for job in group:
            if job.id != canonical.id and job.id not in assignments:
                assignments[job.id] = DuplicateAssignment(
                    job.id, canonical.id, "exact_fingerprint", fingerprint
                )

    return sorted(assignments.values(), key=lambda value: value.job_id)
