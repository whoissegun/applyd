from __future__ import annotations

from typing import Optional

from supabase import Client


class UserResumesRepo:
    """Master resume per user. One row per user is enforced by the
    user_resumes_one_per_user unique index, so set_latex() can upsert on user_id.
    """

    def __init__(self, client: Client) -> None:
        self.client = client

    def get(self, user_id: str) -> Optional[dict]:
        res = (
            self.client.table("user_resumes")
            .select("*")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None

    def set_latex(self, user_id: str, latex_source: str) -> dict:
        """Insert or update the user's resume in one call. Idempotent on user_id."""
        res = (
            self.client.table("user_resumes")
            .upsert(
                {"user_id": user_id, "latex_source": latex_source},
                on_conflict="user_id",
            )
            .execute()
        )
        return res.data[0]
