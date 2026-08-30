"""Local semantic retrieval plus deterministic fit reranking."""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .discovery import ATS_MODULES
from .discovery.routing import parse_ats_url
from .eligibility import infer_country_codes
from .models import Job


DEFAULT_EMBED_MODEL = "BAAI/bge-small-en-v1.5"
EMBEDDING_VERSION = "1"
MAX_JOB_TEXT_CHARS = 12_000

ROLE_QUERY_LABELS = {
    "software_engineering": "software engineering and software development",
    "machine_learning": "machine learning engineering and ML infrastructure",
    "data": "data engineering and data science",
    "security": "security engineering",
    "product": "product management",
    "design": "product and user experience design",
    "sales": "sales",
    "operations": "operations",
}


SKILL_ALIASES: dict[str, tuple[str, ...]] = {
    "airflow": ("airflow", "apache airflow"),
    "aws": ("aws", "amazon web services"),
    "azure": ("azure",),
    "c++": ("c++", "cpp"),
    "css": ("css",),
    "databricks": ("databricks",),
    "docker": ("docker",),
    "fastapi": ("fastapi",),
    "flink": ("flink", "apache flink"),
    "gcp": ("gcp", "google cloud", "google cloud platform"),
    "git": ("git", "github"),
    "graphql": ("graphql",),
    "java": ("java",),
    "javascript": ("javascript", "js"),
    "jenkins": ("jenkins",),
    "kafka": ("kafka", "apache kafka"),
    "kubernetes": ("kubernetes", "k8s"),
    "mongodb": ("mongodb", "mongo"),
    "next.js": ("next.js", "nextjs"),
    "node.js": ("node.js", "nodejs"),
    "playwright": ("playwright",),
    "postgresql": ("postgresql", "postgres"),
    "pytorch": ("pytorch", "torch"),
    "python": ("python",),
    "react": ("react", "react.js", "reactjs"),
    "react native": ("react native",),
    "redis": ("redis",),
    "rest": ("rest api", "restful"),
    "sql": ("sql",),
    "supabase": ("supabase",),
    "tailwind css": ("tailwind", "tailwind css"),
    "terraform": ("terraform",),
    "typescript": ("typescript",),
}


@dataclass(frozen=True)
class MatchResult:
    job_id: str
    score: float
    band: str
    semantic_similarity: float
    components: dict[str, Any]
    reasons: list[str]


def _hash_text(*values: str) -> str:
    raw = "\0".join(values)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def resume_source_hash(resume: dict[str, Any]) -> str:
    return _hash_text(json.dumps(resume, sort_keys=True, ensure_ascii=False))


def _flatten_resume(resume: dict[str, Any]) -> str:
    lines: list[str] = []
    for experience in resume.get("experience") or []:
        lines.append(
            " | ".join(
                str(experience.get(key) or "")
                for key in ("title", "company")
            )
        )
        lines.extend(
            str(bullet.get("text") or "")
            for bullet in experience.get("bullets") or []
            if isinstance(bullet, dict)
        )
    for project in resume.get("projects") or []:
        lines.append(str(project.get("name") or ""))
        lines.extend(str(value) for value in project.get("technologies") or [])
        lines.extend(
            str(bullet.get("text") or "")
            for bullet in project.get("bullets") or []
            if isinstance(bullet, dict)
        )
    for group in (resume.get("skills") or {}).values():
        lines.extend(str(value) for value in group or [])
    return "\n".join(value for value in lines if value.strip())


def candidate_embedding_text(
    resume: dict[str, Any], profile: dict[str, Any]
) -> str:
    settings = profile.get("matchmaking") or {}
    target_roles = settings.get("target_role_families") or []
    preferred = settings.get("preferred_seniority") or []
    target_text = ", ".join(
        ROLE_QUERY_LABELS.get(str(role), str(role).replace("_", " "))
        for role in target_roles
    )
    return (
        f"TARGET ROLES: {target_text}\n"
        f"PREFERRED SENIORITY: {', '.join(map(str, preferred))}\n"
        f"RESUME:\n{_flatten_resume(resume)}"
    ).strip()


def candidate_skill_inventory(resume: dict[str, Any]) -> set[str]:
    return extract_known_skills(_flatten_resume(resume))


def job_embedding_text(job: Job, facts: dict[str, Any]) -> str:
    requirements = [
        str(item.get("value") or "")
        for item in facts.get("hard_requirements") or []
        if isinstance(item, dict)
    ]
    role = (facts.get("role_family") or {}).get("value", "unknown")
    seniority = (facts.get("seniority") or {}).get("value", "unknown")
    return (
        f"TITLE: {job.title}\n"
        f"COMPANY: {job.company}\n"
        f"ROLE: {role}\n"
        f"SENIORITY: {seniority}\n"
        f"REQUIREMENTS: {'; '.join(requirements)}\n"
        f"DESCRIPTION:\n{(job.description or '')[:MAX_JOB_TEXT_CHARS]}"
    ).strip()


def embedding_source_hash(text: str, model: str = DEFAULT_EMBED_MODEL) -> str:
    return _hash_text(EMBEDDING_VERSION, model, text)


def _contains_alias(text: str, alias: str) -> bool:
    pattern = r"(?<![a-z0-9])" + re.escape(alias.casefold()) + r"(?![a-z0-9])"
    return re.search(pattern, text.casefold()) is not None


def extract_known_skills(text: str) -> set[str]:
    return {
        canonical
        for canonical, aliases in SKILL_ALIASES.items()
        if any(_contains_alias(text, alias) for alias in aliases)
    }


def _recency(job: Job, now: datetime) -> tuple[int | None, float]:
    if job.posted_at is None:
        return None, 2.0
    posted = job.posted_at
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)
    days = max(0, (now - posted).days)
    if days <= 7:
        return days, 5.0
    if days <= 14:
        return days, 4.0
    if days <= 30:
        return days, 2.5
    return days, 1.0


def _supported_ats(job: Job) -> str | None:
    retrieval_url = str(job.raw.get("_applyd_retrieval_url") or job.url)
    parsed = parse_ats_url(retrieval_url)
    if parsed and parsed[0] in ATS_MODULES:
        return parsed[0]
    if job.source in ATS_MODULES:
        return job.source
    return None


def _authorization_fit(job: Job, profile: dict[str, Any]) -> tuple[list[str], float]:
    countries = infer_country_codes(job)
    if not countries:
        return [], 3.0
    authorization = profile.get("work_authorization") or {}
    statuses = [authorization.get(country) for country in countries]
    if any(
        isinstance(status, dict)
        and status.get("authorized")
        and not status.get("requires_sponsorship")
        for status in statuses
    ):
        return countries, 5.0
    if any(
        isinstance(status, dict) and status.get("requires_sponsorship")
        for status in statuses
    ):
        # Keep it in the funnel when sponsorship was not explicitly ruled out,
        # but rank an already-authorized market ahead of it.
        return countries, 1.0
    return countries, 3.0


def score_match(
    job: Job,
    facts: dict[str, Any],
    *,
    semantic_similarity: float,
    candidate_skills: set[str],
    profile: dict[str, Any],
    now: datetime | None = None,
) -> MatchResult:
    """Combine KNN similarity with explicit, reproducible policy features."""
    settings = profile.get("matchmaking") or {}
    target_roles = set(settings.get("target_role_families") or [])
    preferred = set(settings.get("preferred_seniority") or [])
    stretch = set(settings.get("stretch_seniority") or [])
    role = (facts.get("role_family") or {}).get("value", "unknown")
    seniority = (facts.get("seniority") or {}).get("value", "unknown")

    similarity = max(0.0, min(1.0, float(semantic_similarity)))
    semantic_points = similarity * 45.0

    job_skills = extract_known_skills(job_embedding_text(job, facts))
    matched_skills = candidate_skills & job_skills
    if job_skills:
        # Coverage alone over-rewards a posting that happens to mention one
        # generic matching skill. Reward breadth: five grounded matches earns
        # the full component, one match earns four points.
        skill_points = min(20.0, 4.0 * len(matched_skills))
    else:
        skill_points = 10.0

    if not target_roles or role in target_roles:
        role_points = 10.0
    elif role == "unknown":
        role_points = 5.0
    elif role in {"software_engineering", "machine_learning", "data"}:
        role_points = 6.0
    else:
        role_points = 0.0

    if not preferred or seniority in preferred:
        seniority_points = 10.0
    elif seniority in stretch:
        seniority_points = 6.0
    elif seniority == "unknown":
        seniority_points = 5.0
    else:
        seniority_points = 0.0

    days_old, recency_points = _recency(job, now or datetime.now(timezone.utc))
    ats = _supported_ats(job)
    readiness_points = 5.0 if ats else 0.0
    countries, authorization_points = _authorization_fit(job, profile)
    score = round(
        semantic_points
        + skill_points
        + role_points
        + seniority_points
        + recency_points
        + readiness_points
        + authorization_points,
        2,
    )
    band = (
        "excellent" if score >= 85 else
        "good" if score >= 70 else
        "stretch" if score >= 55 else
        "weak"
    )

    reasons: list[str] = []
    if role_points == 10:
        reasons.append(f"target role family: {role}")
    if matched_skills:
        reasons.append("matching technologies: " + ", ".join(sorted(matched_skills)))
    if seniority_points == 10:
        reasons.append(f"preferred seniority: {seniority}")
    elif seniority_points == 0:
        reasons.append(f"seniority mismatch: {seniority}")
    if days_old is not None and days_old <= 7:
        reasons.append(f"posted {days_old} days ago")
    if authorization_points == 5:
        reasons.append("already authorized in the job market")
    elif authorization_points == 1:
        reasons.append("requires sponsorship in the job market")

    components = {
        "semantic_similarity": {
            "value": round(similarity, 6),
            "points": round(semantic_points, 2),
            "maximum": 45,
        },
        "technology_overlap": {
            "matched": sorted(matched_skills),
            "job_technologies": sorted(job_skills),
            "points": round(skill_points, 2),
            "maximum": 20,
        },
        "role_alignment": {
            "candidate_targets": sorted(target_roles),
            "job_role": role,
            "points": role_points,
            "maximum": 10,
        },
        "seniority_alignment": {
            "candidate_preferred": sorted(preferred),
            "candidate_stretch": sorted(stretch),
            "job_seniority": seniority,
            "points": seniority_points,
            "maximum": 10,
        },
        "recency": {
            "days_old": days_old,
            "points": recency_points,
            "maximum": 5,
        },
        "application_readiness": {
            "ats": ats,
            "supported": ats is not None,
            "points": readiness_points,
            "maximum": 5,
        },
        "work_authorization_fit": {
            "countries": countries,
            "points": authorization_points,
            "maximum": 5,
        },
    }
    return MatchResult(job.id, score, band, similarity, components, reasons)


class LocalEmbedder:
    def __init__(
        self,
        model: str = DEFAULT_EMBED_MODEL,
        cache_dir: str | Path | None = None,
    ) -> None:
        from fastembed import TextEmbedding

        self.model_name = model
        resolved_cache = Path(
            cache_dir or os.environ.get("APPLYD_MODEL_CACHE", "data/models")
        )
        resolved_cache.mkdir(parents=True, exist_ok=True)
        self._model = TextEmbedding(model_name=model, cache_dir=str(resolved_cache))

    def passages(self, texts: Iterable[str]) -> list[np.ndarray]:
        return [
            np.asarray(value, dtype=np.float32)
            for value in self._model.passage_embed(list(texts))
        ]

    def query(self, text: str) -> np.ndarray:
        values = list(self._model.query_embed([text]))
        if not values:
            raise RuntimeError("local embedding model returned no query vector")
        return np.asarray(values[0], dtype=np.float32)
