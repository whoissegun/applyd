from __future__ import annotations

from typing import Optional

from supabase import Client


class TailoredResumesRepo:
    """LLM-tailored resume versions per (user, job).

    Append-only: each tailoring inserts a NEW row (with its own unique PDF path)
    so an application's `tailored_resume_id` always resolves to the exact resume
    that was submitted, even after the job is re-tailored. `get()` returns the
    newest version; `get_by_id()` fetches a specific one.
    """

    def __init__(self, client: Client) -> None:
        self.client = client

    def create_version(
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

        try:
            res = self.client.table("tailored_resumes").insert(payload).execute()
            return res.data[0]
        except Exception as exc:  # noqa: BLE001
            # Transitional safety: until the append-only migration drops the
            # UNIQUE (user_id, job_id) index, a re-tailor's insert hits a
            # duplicate-key violation. Fall back to updating the existing row in
            # place (old behavior) so a re-tailor can't spin in a failing-retry
            # cost loop. Once the migration is applied, inserts always succeed
            # and versioning kicks in. Remove this fallback after the migration
            # has been live for a while.
            msg = str(exc).lower()
            if "duplicate" in msg or "unique" in msg or "23505" in msg:
                res = (
                    self.client.table("tailored_resumes")
                    .update(payload)
                    .eq("user_id", user_id)
                    .eq("job_id", job_id)
                    .execute()
                )
                if res.data:
                    return res.data[0]
            raise

    def get(self, user_id: str, job_id: str) -> Optional[dict]:
        """Newest tailored version for (user, job)."""
        res = (
            self.client.table("tailored_resumes")
            .select("*")
            .eq("user_id", user_id)
            .eq("job_id", job_id)
            .order("generated_at", desc=True)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None

    def get_by_id(self, tailored_resume_id: str) -> Optional[dict]:
        """A specific tailored version by its id (what an application is bound to)."""
        res = (
            self.client.table("tailored_resumes")
            .select("*")
            .eq("id", tailored_resume_id)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None
