from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Iterable, Iterator, Optional

from supabase import Client

from ..discovery.routing import detect_gate
from ..models import Job
from .companies_repo import CompaniesRepo


_COLUMNS = [
    "id",
    "company_id",
    "ats",
    "ats_job_id",
    "url",
    "apply_url",
    "apply_gate",
    "title",
    "description",
    "level",
    "specialty",
    "locations",
    "is_remote",
    "posted_at",
    "discovered_at",
    "enriched_at",
    "last_seen_at",
    "active",
    "fetch_tier",
    "fetch_error",
    "raw_payload",
]


def _job_to_row(job: Job, company_id: str, now: datetime) -> dict:
    return {
        "id": job.id,
        "company_id": company_id,
        "ats": job.source,
        "ats_job_id": job.external_id,
        "url": job.url,
        "apply_gate": job.apply_gate,
        "title": job.title,
        "description": job.description,
        "locations": job.locations,
        "is_remote": job.remote,
        "posted_at": job.posted_at.isoformat() if job.posted_at else None,
        "discovered_at": job.first_seen_at.isoformat(),
        "enriched_at": (
            job.description_fetched_at.isoformat()
            if job.description_fetched_at
            else None
        ),
        "last_seen_at": now.isoformat(),
        "active": job.active,
        "fetch_tier": job.fetch_tier,
        "fetch_error": job.fetch_error,
        "raw_payload": job.raw,
    }


def _row_to_job(row: dict, company_name: str) -> Job:
    posted = row.get("posted_at")
    enriched = row.get("enriched_at")
    return Job(
        id=row["id"],
        source=row.get("ats") or "",
        external_id=row.get("ats_job_id") or "",
        company=company_name,
        title=row["title"],
        url=row["url"],
        locations=row.get("locations") or [],
        remote=row.get("is_remote") or False,
        posted_at=datetime.fromisoformat(posted) if posted else None,
        description=row.get("description"),
        raw=row.get("raw_payload") or {},
        first_seen_at=datetime.fromisoformat(row["discovered_at"]),
        last_seen_at=datetime.fromisoformat(row["last_seen_at"]),
        active=row["active"],
        description_fetched_at=datetime.fromisoformat(enriched) if enriched else None,
        fetch_tier=row.get("fetch_tier"),
        fetch_error=row.get("fetch_error"),
        apply_gate=row.get("apply_gate"),
    )


class JobsRepo:
    """Read/write access to the shared public.jobs catalog.

    Per-user state (application status, tailored resume) lives in
    applications / tailored_resumes — handled by other repos, not here.
    """

    def __init__(self, client: Client) -> None:
        self.client = client
        self.companies = CompaniesRepo(client)

    # ---------- writes ----------

    # Both chunk sizes exist because big boards break bulk writes two ways:
    # the existing-ids check is a GET whose querystring grows with every id
    # (SimplifyJobs' ~2.5k batch exceeded the URL limit), and a single huge
    # upsert can trip the Postgres statement timeout (Anthropic's board did).
    _SELECT_CHUNK = 100
    _UPSERT_CHUNK = 200

    def upsert(self, incoming: Iterable[Job]) -> tuple[int, int]:
        """Upsert a batch of jobs. Returns (new, updated)."""
        now = datetime.now(timezone.utc)
        rows: list[dict] = []
        for job in incoming:
            if job.apply_gate is None:
                job.apply_gate = detect_gate(job.url)
            company_id = self.companies.upsert(job.company, ats=job.source)
            rows.append(_job_to_row(job, company_id, now))

        if not rows:
            return (0, 0)

        ids = [r["id"] for r in rows]
        existing_ids: set[str] = set()
        for i in range(0, len(ids), self._SELECT_CHUNK):
            existing_ids.update(
                r["id"]
                for r in self.client.table("jobs")
                .select("id")
                .in_("id", ids[i : i + self._SELECT_CHUNK])
                .execute()
                .data
            )
        new = sum(1 for r in rows if r["id"] not in existing_ids)
        updated = len(rows) - new

        for i in range(0, len(rows), self._UPSERT_CHUNK):
            self.client.table("jobs").upsert(
                rows[i : i + self._UPSERT_CHUNK], on_conflict="id"
            ).execute()
        return new, updated

    def mark_enriched(
        self,
        job_id: str,
        description: Optional[str],
        tier: str,
        error: Optional[str] = None,
        classification: Optional[dict] = None,
        embedding: Optional[list[float]] = None,
    ) -> None:
        payload: dict = {
            "description": description,
            "enriched_at": datetime.now(timezone.utc).isoformat(),
            "fetch_tier": tier,
            "fetch_error": error,
        }
        if classification is not None:
            payload["classification"] = classification
        if embedding is not None:
            payload["embedding"] = json.dumps(embedding)
        self.client.table("jobs").update(payload).eq("id", job_id).execute()

    def mark_inactive(self, job_id: str, reason: str) -> None:
        """Flip a job to inactive on a strong dead-link signal.

        Reasons we'd pass: 'not_in_ats_list', '404', 'position_closed',
        'last_seen_threshold'. Soft-delete only — the row sticks around for
        history / application joins / re-listing correlation.
        """
        now = datetime.now(timezone.utc).isoformat()
        self.client.table("jobs").update(
            {
                "active": False,
                "inactive_reason": reason,
                "marked_inactive_at": now,
            }
        ).eq("id", job_id).eq("active", True).execute()

    def set_classification(
        self, job_id: str, classification: dict, embedding: Optional[list[float]]
    ) -> None:
        """Classification-only write for the backfill path — leaves the
        description/tier/enriched_at fields alone (they're already correct
        for jobs whose description arrived via the ATS bulk list)."""
        payload: dict = {"classification": classification}
        if embedding is not None:
            payload["embedding"] = json.dumps(embedding)
        self.client.table("jobs").update(payload).eq("id", job_id).execute()

    def set_apply_gate(self, job_id: str, gate: str) -> None:
        """Record a form-level gate discovered at apply time (login wall,
        mandatory cover letter, ...). `rank_jobs_for_user` filters
        `apply_gate is null`, so one discovery spares every tenant the
        tailor+apply cost on the same wall.
        """
        self.client.table("jobs").update({"apply_gate": gate}).eq(
            "id", job_id
        ).execute()

    def sweep_stale(self, stale_after_days: int = 30) -> int:
        """Nightly safety net: mark active jobs we haven't seen in N days inactive.

        Returns the number of rows flipped. Reason: 'stale_{N}d'.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=stale_after_days)
        ).isoformat()
        res = (
            self.client.table("jobs")
            .update(
                {
                    "active": False,
                    "inactive_reason": f"stale_{stale_after_days}d",
                    "marked_inactive_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            .eq("active", True)
            .lt("last_seen_at", cutoff)
            .execute()
        )
        return len(res.data) if res.data else 0

    # ---------- reads ----------

    def get(self, job_id: str) -> Optional[Job]:
        res = (
            self.client.table("jobs")
            .select(", ".join(_COLUMNS) + ", companies(canonical_name)")
            .eq("id", job_id)
            .limit(1)
            .execute()
        )
        if not res.data:
            return None
        row = res.data[0]
        company_name = (row.get("companies") or {}).get("canonical_name", "")
        return _row_to_job(row, company_name)

    def iter_all(
        self,
        batch: int = 1000,
        max_rows: Optional[int] = None,
        active_only: bool = True,
    ) -> Iterator[Job]:
        """Iterate the shared catalog. Use sparingly — at scale, prefer
        filtering server-side. `max_rows` is a hard ceiling so a dev-CLI call
        can't accidentally pull millions of rows.
        """
        offset = 0
        cols = ", ".join(_COLUMNS) + ", companies(canonical_name)"
        yielded = 0
        while True:
            q = (
                self.client.table("jobs")
                .select(cols)
                .order("discovered_at", desc=True)
                .order("id")
                .range(offset, offset + batch - 1)
            )
            if active_only:
                q = q.eq("active", True)
            res = q.execute()
            if not res.data:
                return
            for row in res.data:
                company_name = (row.get("companies") or {}).get("canonical_name", "")
                yield _row_to_job(row, company_name)
                yielded += 1
                if max_rows is not None and yielded >= max_rows:
                    return
            if len(res.data) < batch:
                return
            offset += batch

    def iter_unclassified(self, batch: int = 500) -> Iterator[Job]:
        """Active jobs that HAVE a description but no classification yet.

        These are invisible to the matchmaker (`rank_jobs_for_user` requires
        classification + embedding). Jobs whose description arrived free via
        the ATS bulk list at discover time land here — `iter_pending_enrichment`
        never yields them because enriched_at is already set.
        """
        offset = 0
        cols = ", ".join(_COLUMNS) + ", companies(canonical_name)"
        while True:
            res = (
                self.client.table("jobs")
                .select(cols)
                .eq("active", True)
                .not_.is_("description", "null")
                .is_("classification", "null")
                .order("discovered_at", desc=True)
                .order("id")
                .range(offset, offset + batch - 1)
                .execute()
            )
            if not res.data:
                return
            for row in res.data:
                company_name = (row.get("companies") or {}).get("canonical_name", "")
                yield _row_to_job(row, company_name)
            if len(res.data) < batch:
                return
            offset += batch

    def iter_pending_enrichment(
        self,
        batch: int = 500,
        include_failed: bool = False,
        source: Optional[str] = None,
    ) -> Iterator[Job]:
        """Active jobs needing enrichment. Yields in batches.

        By default: rows where enriched_at is null. With include_failed=True,
        also yields rows previously marked fetch_tier='failed' so callers can
        retry the cheapest/highest-success paths.
        """
        offset = 0
        cols = ", ".join(_COLUMNS) + ", companies(canonical_name)"
        while True:
            q = (
                self.client.table("jobs")
                .select(cols)
                .eq("active", True)
                .order("discovered_at", desc=False)
                .order("id")
                .range(offset, offset + batch - 1)
            )
            if include_failed:
                q = q.or_("enriched_at.is.null,fetch_tier.eq.failed")
            else:
                q = q.is_("enriched_at", "null")
            if source:
                q = q.eq("ats", source)
            res = q.execute()
            if not res.data:
                return
            for row in res.data:
                company_name = (row.get("companies") or {}).get("canonical_name", "")
                yield _row_to_job(row, company_name)
            if len(res.data) < batch:
                return
            offset += batch
