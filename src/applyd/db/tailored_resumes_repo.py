from __future__ import annotations

from typing import Optional

from supabase import Client


class TailoredResumesRepo:
    """LLM-tailored resume per (user, job). Unique (user_id, job_id) index means
    re-tailoring the same job replaces the existing row instead of duplicating.
    """

    def __init__(self, client: Client) -> None:
        self.client = client

    def upsert(
        self,
        user_id: str,
        job_id: str,
        latex_source: str,
        source_resume_id: Optional[str] = None,
        pdf_storage_path: Optional[str] = None,
        tailor_metadata: Optional[dict] = None,
        model_used: Optional[str] = None,
        prompt_tokens: Optional[int] = None,
        completion_tokens: Optional[int] = None,
        cached_tokens: Optional[int] = None,
        validator_passed: bool = False,
    ) -> dict:
        payload: dict = {
            "user_id": user_id,
            "job_id": job_id,
            "latex_source": latex_source,
            "tailor_metadata": tailor_metadata if tailor_metadata is not None else {},
            "validator_passed": validator_passed,
        }
        if source_resume_id is not None:
            payload["source_resume_id"] = source_resume_id
        if pdf_storage_path is not None:
            payload["pdf_storage_path"] = pdf_storage_path
        if model_used is not None:
            payload["model_used"] = model_used
        if prompt_tokens is not None:
            payload["prompt_tokens"] = prompt_tokens
        if completion_tokens is not None:
            payload["completion_tokens"] = completion_tokens
        if cached_tokens is not None:
            payload["cached_tokens"] = cached_tokens

        res = (
            self.client.table("tailored_resumes")
            .upsert(payload, on_conflict="user_id,job_id")
            .execute()
        )
        return res.data[0]

    def get(self, user_id: str, job_id: str) -> Optional[dict]:
        res = (
            self.client.table("tailored_resumes")
            .select("*")
            .eq("user_id", user_id)
            .eq("job_id", job_id)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None
