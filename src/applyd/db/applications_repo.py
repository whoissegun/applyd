from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional

from supabase import Client


CLAIMABLE_DEFAULT: tuple[str, ...] = ("tailored", "failed")


class ApplicationsRepo:
    """Per-user application lifecycle. The 'claim' pattern guarantees that
    parallel workers can't double-submit the same application.
    """

    def __init__(self, client: Client) -> None:
        self.client = client

    # ---------- writes ----------

    def upsert_pending(self, user_id: str, job_id: str) -> dict:
        """Create the application row in 'pending' state (idempotent)."""
        row = (
            self.client.table("applications")
            .upsert(
                {"user_id": user_id, "job_id": job_id, "status": "pending"},
                on_conflict="user_id,job_id",
            )
            .execute()
        )
        return row.data[0]

    def mark_tailored(self, application_id: str, tailored_resume_id: str) -> Optional[dict]:
        res = (
            self.client.table("applications")
            .update(
                {
                    "status": "tailored",
                    "tailored_resume_id": tailored_resume_id,
                }
            )
            .eq("id", application_id)
            .execute()
        )
        return res.data[0] if res.data else None

    def claim(
        self,
        application_id: str,
        from_statuses: Iterable[str] = CLAIMABLE_DEFAULT,
    ) -> Optional[dict]:
        """Atomically transition the row to 'in_progress' if it's in an allowed
        starting state. Returns the claimed row, or None if another worker beat us.

        Uses a single UPDATE statement, so two parallel workers hitting the same
        application can never both succeed — Postgres serializes the writes.

        Note: attempted_count is NOT incremented here because PostgREST can't
        express atomic `col = col + 1` directly. Use the apply_attempts rowcount
        as the source of truth for retries; attempted_count is a denormalized
        approximation set by the worker after claim.
        """
        now = datetime.now(timezone.utc).isoformat()
        res = (
            self.client.table("applications")
            .update({"status": "in_progress", "last_attempt_at": now})
            .eq("id", application_id)
            .in_("status", list(from_statuses))
            .execute()
        )
        return res.data[0] if res.data else None

    def release(
        self,
        application_id: str,
        status: str,
        reason: Optional[str] = None,
    ) -> Optional[dict]:
        """Move from 'in_progress' to a terminal status."""
        if status not in {"applied", "skipped", "failed"}:
            raise ValueError(f"release status must be terminal, got {status!r}")
        now = datetime.now(timezone.utc).isoformat()
        payload: dict = {"status": status, "last_attempt_at": now}
        if status == "applied":
            payload["applied_at"] = now
        if reason is not None:
            payload["last_error"] = reason
        res = (
            self.client.table("applications")
            .update(payload)
            .eq("id", application_id)
            .eq("status", "in_progress")
            .execute()
        )
        return res.data[0] if res.data else None

    # ---------- reads ----------

    def get(self, application_id: str) -> Optional[dict]:
        res = (
            self.client.table("applications")
            .select("*")
            .eq("id", application_id)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None

    def get_by_user_job(self, user_id: str, job_id: str) -> Optional[dict]:
        res = (
            self.client.table("applications")
            .select("*")
            .eq("user_id", user_id)
            .eq("job_id", job_id)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None
