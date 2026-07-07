from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from supabase import Client


class ApplyAttemptsRepo:
    """One row per apply dispatch. insert() opens the attempt; end() closes it
    with terminal status + telemetry. We let the DB default fill started_at
    (timestamptz default now()).
    """

    def __init__(self, client: Client) -> None:
        self.client = client

    def insert(
        self,
        application_id: str,
        user_id: str,
        status: str = "pending",
        reason: Optional[str] = None,
        agent_transcript_storage_path: Optional[str] = None,
    ) -> dict:
        payload: dict = {
            "application_id": application_id,
            "user_id": user_id,
            "status": status,
        }
        if reason is not None:
            payload["reason"] = reason
        if agent_transcript_storage_path is not None:
            payload["agent_transcript_storage_path"] = agent_transcript_storage_path
        res = self.client.table("apply_attempts").insert(payload).execute()
        return res.data[0]

    def count_for_application(self, application_id: str) -> int:
        """How many attempts exist for this application (including open ones).
        Used to cap unbounded retries on a persistently-failing row."""
        res = (
            self.client.table("apply_attempts")
            .select("id", count="exact", head=True)
            .eq("application_id", application_id)
            .execute()
        )
        return res.count or 0

    def end(
        self,
        attempt_id: str,
        status: str,
        reason: Optional[str] = None,
        prompt_tokens: Optional[int] = None,
        completion_tokens: Optional[int] = None,
        browser_seconds: Optional[int] = None,
        turn_count: Optional[int] = None,
        tool_call_counts: Optional[dict] = None,
    ) -> Optional[dict]:
        if status not in {"applied", "skipped", "failed"}:
            raise ValueError(f"end() status must be terminal, got {status!r}")
        payload: dict = {
            "status": status,
            "ended_at": datetime.now(timezone.utc).isoformat(),
        }
        if reason is not None:
            payload["reason"] = reason
        if prompt_tokens is not None:
            payload["prompt_tokens"] = prompt_tokens
        if completion_tokens is not None:
            payload["completion_tokens"] = completion_tokens
        if browser_seconds is not None:
            payload["browser_seconds"] = browser_seconds
        if turn_count is not None:
            payload["turn_count"] = turn_count
        if tool_call_counts is not None:
            payload["tool_call_counts"] = tool_call_counts
        res = (
            self.client.table("apply_attempts")
            .update(payload)
            .eq("id", attempt_id)
            .execute()
        )
        return res.data[0] if res.data else None
