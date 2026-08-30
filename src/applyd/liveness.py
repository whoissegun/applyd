"""Free, conservative liveness checks for supported ATS postings."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

import httpx

from .discovery import ATS_MODULES
from .discovery.routing import parse_ats_url
from .enrichment.fetcher import job_is_live
from .models import Job


@dataclass(frozen=True)
class LivenessResult:
    job_id: str
    status: str
    method: str
    detail: str


def _retrieval_url(job: Job) -> str:
    return str(job.raw.get("_applyd_retrieval_url") or job.url)


def _deadline_closed(job: Job, now: datetime) -> bool:
    raw = job.raw.get("application_deadline")
    if not raw:
        return False
    text = str(raw)
    if len(text) == 10:
        try:
            return datetime.fromisoformat(text).date() < now.date()
        except ValueError:
            return False
    try:
        deadline = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return deadline < now


def check_jobs_liveness(
    jobs: Iterable[Job], *, workers: int = 8
) -> list[LivenessResult]:
    values = list(jobs)
    if not values:
        return []
    workers = max(1, workers)
    board_cache: dict = {}
    routes: dict[tuple[str, str], object] = {}
    for job in values:
        parsed = parse_ats_url(_retrieval_url(job))
        if parsed and parsed[0] in ATS_MODULES:
            ats, company, _job_id = parsed
            routes[(ats, company)] = ATS_MODULES[ats]

    limits = httpx.Limits(
        max_connections=workers * 2,
        max_keepalive_connections=workers,
    )
    with httpx.Client(
        timeout=30.0,
        headers={"User-Agent": "applyd/0.1"},
        limits=limits,
    ) as client:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            pending = {
                pool.submit(module.fetch, key[1], client=client): key
                for key, module in routes.items()
            }
            for future in as_completed(pending):
                key = pending[future]
                try:
                    board_cache[key] = future.result()
                except Exception:
                    board_cache[key] = []

        now = datetime.now(timezone.utc)
        results: list[LivenessResult] = []
        for job in values:
            if _deadline_closed(job, now):
                results.append(LivenessResult(
                    job.id, "closed", "application_deadline",
                    "stored application deadline has passed",
                ))
                continue
            url = _retrieval_url(job)
            if not parse_ats_url(url):
                results.append(LivenessResult(
                    job.id, "unknown", "unsupported_identity",
                    "no parseable supported ATS identity",
                ))
                continue
            verdict = job_is_live(url, client=client, board_cache=board_cache)
            if verdict is True:
                results.append(LivenessResult(
                    job.id, "live", "ats_api", "posting is currently available",
                ))
            elif verdict is False:
                results.append(LivenessResult(
                    job.id, "closed", "ats_api",
                    "ATS confirms the posting is absent or closed",
                ))
            else:
                results.append(LivenessResult(
                    job.id, "unknown", "ats_inconclusive",
                    "ATS check was inconclusive; keep in funnel",
                ))
    return results
