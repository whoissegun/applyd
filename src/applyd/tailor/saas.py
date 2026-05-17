"""Multi-tenant tailor entry point.

Wraps the existing single-tenant TailorClient + compile pipeline behind a
per-user Supabase-backed flow. Workers (and any other server-side caller)
should use `tailor_for_user(user_id, job_id)` — never the CLI path.

Lifecycle (mirrors the contract in CLAUDE.md):
    pending -> in_progress -> tailored | failed

The function is structured so each step that does real work (LLM call, PDF
compile, storage upload) sits inside one try/except. Any unhandled error
releases the application back to 'failed' with a short reason rather than
leaving it stuck in 'in_progress'.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from ..db import (
    ApplicationsRepo,
    JobsRepo,
    TailoredResumesRepo,
    UsageEventsRepo,
    UserResumesRepo,
    cost_cents_for_tailor,
    get_client,
)
from .compile import compile_pdf, tectonic_available
from .render import TailorClient
from .validate import validate as validate_tailored

logger = logging.getLogger("applyd.tailor.saas")

STORAGE_BUCKET = "resumes"


def _strip_fences(text: str) -> str:
    """Strip leading/trailing markdown code fences if the model wrapped output."""
    text = text.strip()
    if text.startswith("```"):
        nl = text.find("\n")
        if nl != -1:
            text = text[nl + 1 :]
    if text.endswith("```"):
        text = text[: text.rfind("```")].rstrip()
    return text.strip()


def _storage_path(user_id: str, job_id: str) -> str:
    return f"{user_id}/tailored/{job_id}.pdf"


def _upload_pdf(path: str, pdf_bytes: bytes) -> None:
    """Upload bytes to the resumes bucket. Falls back to update() if the
    object already exists — supabase-py's upload() doesn't surface a clean
    upsert flag across SDK versions.
    """
    sb = get_client()
    storage = sb.storage.from_(STORAGE_BUCKET)
    try:
        storage.upload(
            path=path,
            file=pdf_bytes,
            file_options={
                "content-type": "application/pdf",
                "upsert": "true",
            },
        )
    except Exception as exc:  # noqa: BLE001 — storage SDK raises broad exceptions
        msg = str(exc).lower()
        # Already-exists path on older SDKs that don't honor upsert.
        if "exists" in msg or "duplicate" in msg or "409" in msg:
            storage.update(
                path=path,
                file=pdf_bytes,
                file_options={"content-type": "application/pdf"},
            )
        else:
            raise


def tailor_for_user(user_id: str, job_id: str) -> dict:
    """Run the full tailor pipeline for one (user, job) pair.

    Returns a dict describing the terminal outcome — never raises for
    expected failure modes (missing resume, missing JD, compile error).
    Raises only for genuinely unexpected errors (after a best-effort
    release back to 'failed').
    """
    sb = get_client()
    apps = ApplicationsRepo(sb)
    resumes = UserResumesRepo(sb)
    jobs = JobsRepo(sb)
    tailored_repo = TailoredResumesRepo(sb)
    usage = UsageEventsRepo(sb)

    # 1. Resolve the application row.
    app_row = apps.get_by_user_job(user_id, job_id)
    if app_row is None:
        raise ValueError(
            f"no application row for user={user_id} job={job_id}; "
            "caller must upsert_pending before dispatching tailor"
        )
    application_id = app_row["id"]

    # 2. Atomic claim. Lose the race -> bail without touching anything.
    claimed = apps.claim(application_id, from_statuses=("pending",))
    if claimed is None:
        logger.info("lost race on application %s", application_id)
        return {"status": "lost_race", "application_id": application_id}

    def _fail(reason: str) -> dict:
        apps.release(application_id, status="failed", reason=reason)
        return {
            "status": "failed",
            "application_id": application_id,
            "reason": reason,
        }

    try:
        # 3. Master resume.
        master = resumes.get(user_id)
        if master is None or not master.get("resume_text"):
            return _fail("no_master_resume")
        master_text: str = master["resume_text"]
        source_resume_id: str | None = master.get("id")

        # 4. Job + description.
        job = jobs.get(job_id)
        if job is None or not job.description:
            return _fail("job_missing_or_no_description")

        # 5. LLM tailoring.
        try:
            client = TailorClient()
        except RuntimeError as exc:
            return _fail(f"tailor_init_error: {exc}")

        try:
            tailored_tex, metadata, llm_usage = client.tailor(
                master_text=master_text,
                jd_text=job.description,
                company=job.company,
                role=job.title,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("tailor call failed for app %s", application_id)
            return _fail(f"tailor_call_error: {type(exc).__name__}: {exc}")

        tailored_tex = _strip_fences(tailored_tex)

        # 6. Token bookkeeping.
        model_used = client.model
        # Anthropic usage split: cache_read counts cached input; we treat
        # cache_creation as fresh input for billing (it's billed at the
        # write rate, but pricing.py doesn't distinguish — close enough).
        cached_tokens = int(llm_usage.get("cache_read_input_tokens") or 0)
        cache_write = int(llm_usage.get("cache_creation_input_tokens") or 0)
        fresh_input = int(llm_usage.get("input_tokens") or 0) + cache_write
        completion_tokens = int(llm_usage.get("output_tokens") or 0)

        # 7. Validate, then compile to PDF.
        validation = validate_tailored(master_text, tailored_tex)
        if not tectonic_available():
            return _fail("compile_error: tectonic_not_installed")

        with tempfile.TemporaryDirectory() as tmp:
            tex_path = Path(tmp) / "resume.tex"
            tex_path.write_text(tailored_tex, encoding="utf-8")
            try:
                pdf_path = compile_pdf(tex_path, outdir=Path(tmp))
            except RuntimeError as exc:
                # Trim the tectonic stderr blob to a short reason.
                short = str(exc).splitlines()[0][:200]
                return _fail(f"compile_error: {short}")
            pdf_bytes = pdf_path.read_bytes()

        # 8. Upload to Storage.
        storage_path = _storage_path(user_id, job_id)
        try:
            _upload_pdf(storage_path, pdf_bytes)
        except Exception as exc:  # noqa: BLE001
            logger.exception("storage upload failed for %s", storage_path)
            return _fail(f"storage_error: {type(exc).__name__}: {exc}")

        # 9. Persist tailored_resumes row.
        tailored_row = tailored_repo.upsert(
            user_id=user_id,
            job_id=job_id,
            latex_source=tailored_tex,
            source_resume_id=source_resume_id,
            pdf_storage_path=storage_path,
            tailor_metadata=metadata or {},
            model_used=model_used,
            prompt_tokens=fresh_input,
            completion_tokens=completion_tokens,
            cached_tokens=cached_tokens,
            validator_passed=validation.ok,
        )
        tailored_resume_id = tailored_row["id"]

        # 10. Record billing.
        cost_cents = cost_cents_for_tailor(
            model=model_used,
            prompt_tokens=fresh_input,
            completion_tokens=completion_tokens,
            cached_tokens=cached_tokens,
        )
        usage.record(
            user_id=user_id,
            event_type="tailor",
            cost_cents=cost_cents,
            metadata={
                "application_id": application_id,
                "job_id": job_id,
                "tailored_resume_id": tailored_resume_id,
                "model": model_used,
                "validator_passed": validation.ok,
            },
        )

        # 11. Transition the application to 'tailored'.
        apps.mark_tailored(application_id, tailored_resume_id)

        return {
            "status": "tailored",
            "application_id": application_id,
            "tailored_resume_id": tailored_resume_id,
            "cost_cents": cost_cents,
            "tokens": {
                "prompt": fresh_input,
                "completion": completion_tokens,
                "cached": cached_tokens,
            },
            "validator_passed": validation.ok,
            "pdf_storage_path": storage_path,
        }

    except Exception as exc:  # noqa: BLE001
        # Catch-all so a stuck 'in_progress' row never survives a crash.
        logger.exception("unexpected error tailoring app %s", application_id)
        apps.release(
            application_id,
            status="failed",
            reason=f"unexpected: {type(exc).__name__}: {exc}",
        )
        raise
