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
