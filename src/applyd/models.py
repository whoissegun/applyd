from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class Job(BaseModel):
    id: str
    source: str
    external_id: str
    company: str
    title: str
    url: str
    locations: list[str] = Field(default_factory=list)
    remote: bool = False
    posted_at: Optional[datetime] = None
    description: Optional[str] = None
    raw: dict[str, Any] = Field(default_factory=dict)
    first_seen_at: datetime
    last_seen_at: datetime
    active: bool = True
    # enrichment metadata (populated by `applyd enrich`)
    description_fetched_at: Optional[datetime] = None
    fetch_tier: Optional[str] = None  # "ats" | "http" | "spider" | "spider-chrome" | "failed"
    fetch_error: Optional[str] = None
    # tailoring output (populated by `applyd tailor`)
    resume_pdf_path: Optional[str] = None
    # apply state — legacy per-user fields, slated to move to public.applications
    # once the auth-aware apply layer rewrite lands. Keep here so the existing
    # JSON JobStore continues to work during the Supabase migration.
    apply_status: Optional[str] = None
    apply_attempted_at: Optional[datetime] = None
    apply_note: Optional[str] = None
    # URL-time: "portal" (known gated domain) or None (direct-apply).
    # Runtime agent may overwrite with specific reasons: "signup_required",
    # "login_required", "captcha", "cover_letter_required", "work_auth_block",
    # "dead_link", "unknown". Gated jobs are excluded from pending_apply.
    apply_gate: Optional[str] = None
