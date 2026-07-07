from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from supabase import Client

from ..failures import categorize


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

    def requeue(
        self,
        application_id: str,
        to_status: str,
        reason: Optional[str] = None,
    ) -> Optional[dict]:
        """Release a claimed row back to a *claimable* (non-terminal) status after
        a transient infra failure, so it's retried once the condition clears.

        Unlike `release`, this never lands on a terminal state — the job wasn't
        at fault, the platform was. `to_status` is 'pending' (re-tailor) or
        'tailored' (re-apply); `reason` is stashed in last_error for visibility.
        """
        if to_status not in {"pending", "tailored"}:
            raise ValueError(f"requeue target must be claimable, got {to_status!r}")
        now = datetime.now(timezone.utc).isoformat()
        payload: dict = {"status": to_status, "last_attempt_at": now}
        if reason is not None:
            payload["last_error"] = reason[:300]
            payload["failure_category"] = categorize(reason)
        res = (
            self.client.table("applications")
            .update(payload)
            .eq("id", application_id)
            .eq("status", "in_progress")
            .execute()
        )
        return res.data[0] if res.data else None

    def requeue_orphaned(self, stale_minutes: int = 20) -> list[dict]:
        """Requeue rows stuck 'in_progress' past `stale_minutes` — the worker
        that claimed them died without releasing (deploy restart mid-apply,
        hard-watchdog force-exit, crash). Without this they sit 'in_progress'
        forever and render as Pending in the dashboard.

        Rows with a tailored resume go back to 'tailored' (re-apply); the rest
        to 'pending' (re-tailor). Rows whose job was deleted go to 'failed'.
        Also closes their never-ended apply_attempts rows as orphaned.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)
        ).isoformat()
        stuck = (
            self.client.table("applications")
            .select("id, user_id, job_id")
            .eq("status", "in_progress")
            .lt("last_attempt_at", cutoff)
            .execute()
            .data
            or []
        )
        requeued: list[dict] = []
        for row in stuck:
            if row["job_id"] is None:
                target = "failed"
            else:
                has_resume = bool(
                    self.client.table("tailored_resumes")
                    .select("id")
                    .eq("user_id", row["user_id"])
                    .eq("job_id", row["job_id"])
                    .limit(1)
                    .execute()
                    .data
                )
                target = "tailored" if has_resume else "pending"
            reason = "requeued: orphaned in_progress claim (worker died mid-run)"
            res = (
                self.client.table("applications")
                .update(
                    {
                        "status": target,
                        "last_error": reason,
                        "failure_category": categorize(reason),
                    }
                )
                .eq("id", row["id"])
                .eq("status", "in_progress")  # guard: skip if a live worker just released it
                .lt("last_attempt_at", cutoff)
                .execute()
                .data
            )
            if res:
                self.client.table("apply_attempts").update(
                    {
                        "status": "failed",
                        "ended_at": datetime.now(timezone.utc).isoformat(),
                        "reason": "orphaned: worker died mid-run",
                    }
                ).eq("application_id", row["id"]).is_("ended_at", "null").execute()
                requeued.append({**row, "requeued_to": target})
        return requeued

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
            payload["failure_category"] = None
        if reason is not None:
            payload["last_error"] = reason
            payload["failure_category"] = categorize(reason)
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

    def count_unapplied_backlog(self, user_id: str) -> int:
        """Accepted-but-not-yet-applied rows for a user.

        These are the matcher's accepts still waiting for tailor+apply
        (status pending → tailored → in_progress). The matchmaker uses this to
        stop spending Haiku once a user's apply queue is already primed —
        rejects ('skipped') and terminal rows ('applied'/'failed') don't count.
        """
        res = (
            self.client.table("applications")
            .select("id", count="exact")
            .eq("user_id", user_id)
            .in_("status", ["pending", "tailored", "in_progress"])
            .limit(0)
            .execute()
        )
        return res.count or 0
