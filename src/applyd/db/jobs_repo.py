from __future__ import annotations

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

    Per-user state (apply_status, resume_pdf_path, tailor outputs) lives in
    applications / tailored_resumes — handled by other repos, not here.
    """

    def __init__(self, client: Client) -> None:
        self.client = client
        self.companies = CompaniesRepo(client)

    # ---------- writes ----------

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

        existing_ids = {
            r["id"]
            for r in self.client.table("jobs")
            .select("id")
            .in_("id", [r["id"] for r in rows])
            .execute()
            .data
        }
        new = sum(1 for r in rows if r["id"] not in existing_ids)
        updated = len(rows) - new

        self.client.table("jobs").upsert(rows, on_conflict="id").execute()
        return new, updated

    def mark_enriched(
        self,
        job_id: str,
        description: Optional[str],
        tier: str,
        error: Optional[str] = None,
    ) -> None:
        self.client.table("jobs").update(
            {
                "description": description,
                "enriched_at": datetime.now(timezone.utc).isoformat(),
                "fetch_tier": tier,
                "fetch_error": error,
            }
        ).eq("id", job_id).execute()

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

    def iter_pending_enrichment(self, batch: int = 500) -> Iterator[Job]:
        """Active jobs without a description yet. Yields in batches."""
        offset = 0
        cols = ", ".join(_COLUMNS) + ", companies(canonical_name)"
        while True:
            res = (
                self.client.table("jobs")
                .select(cols)
                .is_("enriched_at", "null")
                .eq("active", True)
                .order("discovered_at", desc=False)
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
