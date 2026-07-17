"""FastAPI app for applyd.

Endpoints are intentionally thin: validate the JWT (via Depends), then call into
the per-user repos. The repos accept user_id as an arg — we always pass the
caller's verified id so cross-user reads/writes are impossible at the API
boundary even though the service-role client used here would technically allow
them.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from applyd.config import load_env

# Load .env before anything touches os.environ.
load_env()

from applyd.api.auth import get_current_user_id  # noqa: E402
from applyd.db import (  # noqa: E402
    ApplicationsRepo,
    TailoredResumesRepo,
    UserProfilesRepo,
    UserResumesRepo,
    get_client,
)

logger = logging.getLogger("applyd.api")

app = FastAPI(title="applyd", version="0.1.0")

_default_origins = "http://localhost:3000,https://applyd-beryl.vercel.app"
_cors_origins = [
    o.strip()
    for o in os.environ.get("APPLYD_CORS_ORIGINS", _default_origins).split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    # Match Vercel preview deploys (applyd-beryl-<hash>-<scope>.vercel.app).
    allow_origin_regex=r"https://applyd-beryl-[\w-]+\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- request models ----------


class ProfileUpdate(BaseModel):
    # All optional; UserProfilesRepo.update drops unknown keys.
    full_name: Optional[str] = None
    phone: Optional[str] = None
    linkedin_url: Optional[str] = None
    github_url: Optional[str] = None
    portfolio_url: Optional[str] = None
    profile_answers: Optional[Any] = None


class ResumeExtractBody(BaseModel):
    # base64-encoded PDF bytes
    pdf_base64: str = Field(..., min_length=1)


class TailorQueueBody(BaseModel):
    job_id: str = Field(..., min_length=1)


# ---------- repo accessors ----------
# Repos are stateless wrappers over a Supabase client — cheap to build per
# request, no pooling concerns.


def _profiles() -> UserProfilesRepo:
    return UserProfilesRepo(get_client())


def _resumes() -> UserResumesRepo:
    return UserResumesRepo(get_client())


def _applications() -> ApplicationsRepo:
    return ApplicationsRepo(get_client())


def _tailored() -> TailoredResumesRepo:
    return TailoredResumesRepo(get_client())


# ---------- endpoints ----------


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/profile")
def get_profile(user_id: str = Depends(get_current_user_id)) -> dict:
    row = _profiles().get(user_id)
    if row is None:
        # The signup trigger should have created this; surface as 500 not 404
        # so we notice in monitoring.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="user_profiles row missing for authenticated user",
        )
    return row


@app.put("/profile")
def put_profile(
    body: ProfileUpdate,
    user_id: str = Depends(get_current_user_id),
) -> dict:
    fields = body.dict(exclude_unset=True)
    row = _profiles().update(user_id, **fields)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="profile not found")
    return row


@app.get("/resume")
def get_resume(user_id: str = Depends(get_current_user_id)) -> dict:
    row = _resumes().get(user_id)
    if row is None:
        # Unlike profile, a missing resume is normal for a fresh user.
        return {"user_id": user_id, "resume_text": None}
    return row


STORAGE_BUCKET = "resumes"


def _master_pdf_storage_path(user_id: str) -> str:
    return f"{user_id}/master.pdf"


def _upload_master_pdf(user_id: str, pdf_bytes: bytes) -> str:
    """Upsert the user's master PDF in Storage. Returns the storage path."""
    path = _master_pdf_storage_path(user_id)
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
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if "exists" in msg or "duplicate" in msg or "409" in msg:
            storage.update(
                path=path,
                file=pdf_bytes,
                file_options={"content-type": "application/pdf"},
            )
        else:
            raise
    return path


@app.post("/resume/extract")
def post_resume_extract(
    body: ResumeExtractBody,
    user_id: str = Depends(get_current_user_id),
) -> dict:
    """One-shot resume onboarding: decode PDF → extract text + contact + work-auth
    via Claude, upload the original PDF to Storage, persist text on
    user_resumes, prefill user_profiles. Returns the extracted shape.
    """
    from applyd.onboarding import extract_from_pdf

    try:
        pdf_bytes = base64.b64decode(body.pdf_base64, validate=True)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid base64 PDF payload: {exc}",
        )

    try:
        extracted = extract_from_pdf(pdf_bytes)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )

    pdf_path = _upload_master_pdf(user_id, pdf_bytes)

    _resumes().set_master(
        user_id,
        extracted.text,
        pdf_storage_path=pdf_path,
    )

    contact = extracted.contact
    prefill: dict[str, Any] = {}
    for k_in, k_out in (
        ("name", "full_name"),
        ("phone", "phone"),
        ("linkedin_url", "linkedin_url"),
        ("github_url", "github_url"),
        ("portfolio_url", "portfolio_url"),
    ):
        if contact.get(k_in):
            prefill[k_out] = contact[k_in]

    # Everything narrative (target roles, work auth, sponsorship) → one
    # labeled markdown blob written to profile_answers. The agent reads this
    # at apply time; the matcher reads it at match time.
    answer_parts: list[str] = []
    if extracted.target_roles:
        answer_parts.append(f"## Target roles\n{extracted.target_roles}")
    wa_bits: list[str] = []
    if extracted.work_auth.get("summary"):
        wa_bits.append(str(extracted.work_auth["summary"]))
    sponsorship = extracted.work_auth.get("sponsorship_needed_countries") or []
    if sponsorship:
        wa_bits.append(f"Need sponsorship to work in: {', '.join(sponsorship)}")
    if wa_bits:
        answer_parts.append("## Work authorization\n" + "\n".join(wa_bits))
    if answer_parts:
        prefill["profile_answers"] = "\n\n".join(answer_parts)

    if prefill:
        _profiles().update(user_id, **prefill)

    return {
        "text_chars": len(extracted.text),
        "contact": extracted.contact,
        "work_auth": extracted.work_auth,
        "target_roles": extracted.target_roles,
        "master_pdf_storage_path": pdf_path,
        "profile_prefill": prefill,
    }


@app.post("/tailor/queue")
def tailor_queue(
    body: TailorQueueBody,
    user_id: str = Depends(get_current_user_id),
) -> dict:
    # Idempotent — repeated calls just return the existing pending row.
    row = _applications().upsert_pending(user_id=user_id, job_id=body.job_id)
    return {"application": row}


@app.get("/applications/{application_id}/resume")
def get_application_resume(
    application_id: str,
    user_id: str = Depends(get_current_user_id),
) -> dict:
    """Signed, short-lived URL to the exact tailored resume this application was
    submitted with. The `resumes` bucket is private, so the browser can't read
    the storage path directly — this hands it a time-limited link."""
    app_row = _applications().get(application_id)
    if app_row is None or app_row.get("user_id") != user_id:
        # Don't leak existence of other users' applications.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="application not found")

    tailored = None
    tr_id = app_row.get("tailored_resume_id")
    if tr_id:
        tailored = _tailored().get_by_id(tr_id)
    if tailored is None and app_row.get("job_id"):
        # Pre-versioning rows have no bound id — fall back to the newest version.
        tailored = _tailored().get(user_id, app_row["job_id"])

    path = (tailored or {}).get("pdf_storage_path")
    if not path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no tailored resume for this application yet",
        )

    try:
        signed = get_client().storage.from_(STORAGE_BUCKET).create_signed_url(path, 3600)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[resume] signing failed for %s", path)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"could not sign resume url: {type(exc).__name__}",
        )
    url = signed.get("signedURL") or signed.get("signedUrl") or signed.get("signed_url")
    if not url:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="storage did not return a signed url",
        )
    return {
        "application_id": application_id,
        "tailored_resume_id": (tailored or {}).get("id"),
        "url": url,
        "expires_in": 3600,
        "generated_at": (tailored or {}).get("generated_at"),
        "model_used": (tailored or {}).get("model_used"),
    }


@app.post("/webhook/stripe")
async def stripe_webhook(request: Request) -> dict:
    # Stub. Signature verification + event dispatch come later.
    body = await request.body()
    try:
        preview = body[:500].decode("utf-8", errors="replace")
    except Exception:
        preview = "<binary>"
    logger.info("[stripe webhook] received %d bytes: %s", len(body), preview)
    return {"received": True}
