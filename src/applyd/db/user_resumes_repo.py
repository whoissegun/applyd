from __future__ import annotations

from typing import Optional

from supabase import Client


class UserResumesRepo:
    """Master resume per user. One row per user (unique index on user_id).

    Schema:
      - resume_text:               extracted plain text the tailor reads
      - master_pdf_storage_path:   pointer to original PDF in the `resumes` bucket
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

    def set_master(
        self,
        user_id: str,
        resume_text: str,
        *,
        pdf_storage_path: Optional[str] = None,
    ) -> dict:
        payload: dict = {
            "user_id": user_id,
            "resume_text": resume_text,
        }
        if pdf_storage_path is not None:
            payload["master_pdf_storage_path"] = pdf_storage_path
        res = (
            self.client.table("user_resumes")
            .upsert(payload, on_conflict="user_id")
            .execute()
        )
        return res.data[0]
