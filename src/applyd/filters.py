from __future__ import annotations

import re
from typing import Optional

from .models import Job


# Regex/keyword matching is intentional. Considered per-query LLM matching
# but rejected at our volume: avg JD ~3k tokens × ~7.7k jobs (growing with
# every `discover`) ≈ $23/query with Haiku 4.5 ($0.80/MTok in). Volume-first
# mass-apply means we re-browse constantly — cost compounds per browse, not
# per apply. Also non-deterministic, and this layer only powers `applyd jobs`;
# apply-time ignores it. When nuance matters: add structured JD extraction
# at enrichment (cached on Job) and query those fields here.
LEVEL_PATTERNS = {
    "intern": re.compile(
        r"\b(intern|internship|co[\s\-]?op|coop|working\s+student|student\s+(engineer|developer))\b",
        re.IGNORECASE,
    ),
    "new_grad": re.compile(
        r"\b(new\s*grad|new\s*graduate|entry[\s\-]?level|junior|jr\.?|graduate|university)\b",
        re.IGNORECASE,
    ),
    "senior": re.compile(
        r"\b(senior|sr\.?|staff|principal|lead|architect|director|head\s+of|vp\b)\b",
        re.IGNORECASE,
    ),
}

SPECIALTY_KEYWORDS = {
    "ml": [
        "machine learning", "ml engineer", "ai engineer", "applied scientist",
        "research engineer", "research scientist", "deep learning", "nlp",
        "computer vision", "mlops", "ml infra",
    ],
    "backend": [
        "backend", "back-end", "back end", "server", "api engineer",
        "platform engineer", "distributed systems",
    ],
    "frontend": [
        "frontend", "front-end", "front end", "react", "web engineer",
        "ui engineer", "client engineer",
    ],
    "fullstack": ["full stack", "fullstack", "full-stack"],
    "mobile": [
        "mobile", "ios engineer", "android engineer", "react native",
        "swift engineer", "kotlin engineer",
    ],
    "infra": [
        "infrastructure", "devops", "sre", "site reliability", "platform",
        "cloud engineer", "kubernetes", "reliability engineer",
    ],
    "data": [
        "data engineer", "data scientist", "analytics engineer",
        "data platform", "etl",
    ],
    "security": [
        "security engineer", "appsec", "infosec", "security analyst",
        "product security",
    ],
}


def detect_level(job: Job) -> str:
    title = job.title or ""
    if LEVEL_PATTERNS["senior"].search(title):
        return "senior"
    if LEVEL_PATTERNS["intern"].search(title):
        return "intern"
    if LEVEL_PATTERNS["new_grad"].search(title):
        return "new_grad"
    return "mid"


def matches_specialty(job: Job, specialty: str) -> bool:
    key = specialty.lower()
    if key not in SPECIALTY_KEYWORDS:
        return False
    haystack = f"{job.title} {job.description or ''}".lower()
    return any(kw in haystack for kw in SPECIALTY_KEYWORDS[key])


def matches_location(job: Job, query: str) -> bool:
    q = query.lower().strip()
    if q == "remote":
        return job.remote
    return any(q in loc.lower() for loc in job.locations)


def filter_jobs(
    jobs: list[Job],
    level: Optional[str] = None,
    specialty: Optional[str] = None,
    location: Optional[str] = None,
    remote: Optional[bool] = None,
    source: Optional[str] = None,
    company: Optional[str] = None,
    gated: Optional[bool] = None,
) -> list[Job]:
    out: list[Job] = []
    for job in jobs:
        if level and detect_level(job) != level:
            continue
        if specialty and not matches_specialty(job, specialty):
            continue
        if location and not matches_location(job, location):
            continue
        if remote is True and not job.remote:
            continue
        if source and job.source != source:
            continue
        if company and company.lower() not in job.company.lower():
            continue
        if gated is True and job.apply_gate is None:
            continue
        if gated is False and job.apply_gate is not None:
            continue
        out.append(job)
    return out
