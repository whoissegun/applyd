"""FastAPI app for applyd.

Endpoints are intentionally thin: validate the JWT (via Depends), then call into
the per-user repos. The repos accept user_id as an arg — we always pass the
caller's verified id so cross-user reads/writes are impossible at the API
boundary even though the service-role client used here would technically allow
them.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, Field

from applyd.config import load_env

# Load .env before anything touches os.environ.
load_env()

from applyd.api.auth import get_current_user_id  # noqa: E402
from applyd.db import (  # noqa: E402
    ApplicationsRepo,
    UserProfilesRepo,
    UserResumesRepo,
    get_client,
)

logger = logging.getLogger("applyd.api")

app = FastAPI(title="applyd", version="0.1.0")


# ---------- request models ----------


class ProfileUpdate(BaseModel):
    # All optional; UserProfilesRepo.update drops unknown keys.
    full_name: Optional[str] = None
    phone: Optional[str] = None
    linkedin_url: Optional[str] = None
    github_url: Optional[str] = None
    portfolio_url: Optional[str] = None
    work_auth_summary: Optional[str] = None
    sponsorship_needed_countries: Optional[List[str]] = None
    target_levels: Optional[List[str]] = None
    target_specialties: Optional[List[str]] = None
    target_locations: Optional[List[str]] = None
    strategy: Optional[str] = None
    profile_answers: Optional[Any] = None


class ResumePut(BaseModel):
    latex_source: str = Field(..., min_length=1)


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
        return {"user_id": user_id, "latex_source": None}
    return row


@app.put("/resume")
def put_resume(
    body: ResumePut,
    user_id: str = Depends(get_current_user_id),
) -> dict:
    return _resumes().set_latex(user_id, body.latex_source)


@app.post("/tailor/queue")
def tailor_queue(
    body: TailorQueueBody,
    user_id: str = Depends(get_current_user_id),
) -> dict:
    # Idempotent — repeated calls just return the existing pending row.
    row = _applications().upsert_pending(user_id=user_id, job_id=body.job_id)
    return {"application": row}


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
