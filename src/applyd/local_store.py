"""SQLite persistence for the local-first applyd runtime."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from .discovery.routing import detect_gate
from .models import Job


SCHEMA_VERSION = 6


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _normalize_question_label(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.casefold()).split())[:300]


def default_db_path() -> Path:
    return Path(os.environ.get("APPLYD_DB_PATH", "data/applyd.sqlite3"))


class LocalStore:
    """Small, thread-safe SQLite repository used by all local CLI stages."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path,
            timeout=30,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA busy_timeout = 30000")
        self.initialize()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def initialize(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    company TEXT NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    locations_json TEXT NOT NULL DEFAULT '[]',
                    remote INTEGER NOT NULL DEFAULT 0,
                    posted_at TEXT,
                    description TEXT,
                    description_hash TEXT,
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    inactive_reason TEXT,
                    description_fetched_at TEXT,
                    fetch_tier TEXT,
                    fetch_error TEXT,
                    apply_gate TEXT,
                    facts_json TEXT,
                    facts_model TEXT,
                    facts_source_hash TEXT,
                    facts_cost_usd REAL NOT NULL DEFAULT 0,
                    facts_extracted_at TEXT
                );

                CREATE INDEX IF NOT EXISTS jobs_pending_enrichment_idx
                    ON jobs(active, description_fetched_at, fetch_tier);
                CREATE INDEX IF NOT EXISTS jobs_facts_idx
                    ON jobs(active, facts_source_hash);
                CREATE INDEX IF NOT EXISTS jobs_company_idx
                    ON jobs(company);

                CREATE TABLE IF NOT EXISTS profile (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    profile_json TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS evaluations (
                    job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
                    decision TEXT NOT NULL,
                    reasons_json TEXT NOT NULL DEFAULT '[]',
                    facts_source_hash TEXT,
                    profile_source_hash TEXT,
                    evaluated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS evaluations_decision_idx
                    ON evaluations(decision, evaluated_at);

                CREATE TABLE IF NOT EXISTS job_embeddings (
                    job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
                    model TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    dimensions INTEGER NOT NULL,
                    embedding BLOB NOT NULL,
                    embedded_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS job_embeddings_source_idx
                    ON job_embeddings(model, source_hash);

                CREATE TABLE IF NOT EXISTS candidate_embeddings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    model TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    dimensions INTEGER NOT NULL,
                    embedding BLOB NOT NULL,
                    embedded_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS match_scores (
                    job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
                    profile_source_hash TEXT NOT NULL,
                    resume_source_hash TEXT NOT NULL,
                    job_embedding_source_hash TEXT NOT NULL,
                    embedding_model TEXT NOT NULL,
                    semantic_similarity REAL NOT NULL,
                    score REAL NOT NULL,
                    band TEXT NOT NULL,
                    components_json TEXT NOT NULL,
                    reasons_json TEXT NOT NULL,
                    matched_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS match_scores_rank_idx
                    ON match_scores(score DESC, job_id);

                CREATE TABLE IF NOT EXISTS job_duplicates (
                    job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
                    canonical_job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    method TEXT NOT NULL,
                    duplicate_key TEXT NOT NULL,
                    detected_at TEXT NOT NULL,
                    CHECK(job_id != canonical_job_id)
                );

                CREATE INDEX IF NOT EXISTS job_duplicates_canonical_idx
                    ON job_duplicates(canonical_job_id);

                CREATE TABLE IF NOT EXISTS job_liveness (
                    job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
                    status TEXT NOT NULL CHECK(status IN ('live','closed','unknown')),
                    method TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    checked_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tailored_resumes (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    source_resume_hash TEXT NOT NULL,
                    job_description_hash TEXT,
                    model TEXT NOT NULL,
                    edit_plan_json TEXT NOT NULL,
                    latex_path TEXT NOT NULL,
                    pdf_path TEXT,
                    cost_usd REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS tailored_job_idx
                    ON tailored_resumes(job_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS applications (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
                    tailored_resume_id TEXT REFERENCES tailored_resumes(id),
                    status TEXT NOT NULL DEFAULT 'eligible',
                    reason TEXT,
                    claimed_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS applications_status_idx
                    ON applications(status, updated_at);

                CREATE TABLE IF NOT EXISTS apply_attempts (
                    id TEXT PRIMARY KEY,
                    application_id TEXT NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    reason TEXT,
                    model TEXT,
                    cost_usd REAL NOT NULL DEFAULT 0,
                    turn_count INTEGER,
                    tool_calls_json TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT
                );

                CREATE TABLE IF NOT EXISTS apply_trace_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    attempt_id TEXT NOT NULL REFERENCES apply_attempts(id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    turn INTEGER,
                    event_type TEXT NOT NULL,
                    name TEXT,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    UNIQUE(attempt_id, sequence)
                );

                CREATE INDEX IF NOT EXISTS apply_trace_attempt_idx
                    ON apply_trace_events(attempt_id, sequence);

                CREATE TABLE IF NOT EXISTS profile_question_gaps (
                    normalized_label TEXT PRIMARY KEY,
                    sample_label TEXT NOT NULL,
                    category TEXT NOT NULL,
                    first_job_id TEXT,
                    last_job_id TEXT,
                    last_company TEXT,
                    occurrences INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','resolved','ignored')),
                    answer_json TEXT,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS profile_question_gaps_status_idx
                    ON profile_question_gaps(status, occurrences DESC, last_seen_at DESC);
                """
            )
            self._conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            self._conn.commit()

    # ---- jobs ---------------------------------------------------------

    @staticmethod
    def _description_hash(description: str | None) -> str | None:
        if not description:
            return None
        normalized = " ".join(description.split())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def upsert(self, incoming: Iterable[Job]) -> tuple[int, int]:
        rows = list(incoming)
        if not rows:
            return 0, 0
        ids = [job.id for job in rows]
        placeholders = ",".join("?" for _ in ids)
        with self.transaction() as conn:
            existing = {
                row["id"]
                for row in conn.execute(
                    f"SELECT id FROM jobs WHERE id IN ({placeholders})", ids
                )
            }
            now = _utcnow()
            for job in rows:
                gate = job.apply_gate if job.apply_gate is not None else detect_gate(job.url)
                description_hash = self._description_hash(job.description)
                conn.execute(
                    """
                    INSERT INTO jobs(
                        id, source, external_id, company, title, url,
                        locations_json, remote, posted_at, description,
                        description_hash, raw_json, first_seen_at, last_seen_at,
                        active, description_fetched_at, fetch_tier, fetch_error,
                        apply_gate
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                        source=excluded.source,
                        external_id=excluded.external_id,
                        company=excluded.company,
                        title=excluded.title,
                        url=excluded.url,
                        locations_json=excluded.locations_json,
                        remote=excluded.remote,
                        posted_at=COALESCE(excluded.posted_at, jobs.posted_at),
                        description=COALESCE(excluded.description, jobs.description),
                        description_hash=COALESCE(excluded.description_hash, jobs.description_hash),
                        raw_json=excluded.raw_json,
                        last_seen_at=excluded.last_seen_at,
                        active=excluded.active,
                        apply_gate=COALESCE(excluded.apply_gate, jobs.apply_gate)
                    """,
                    (
                        job.id,
                        job.source,
                        job.external_id,
                        job.company,
                        job.title,
                        job.url,
                        _json(job.locations),
                        int(job.remote),
                        job.posted_at.isoformat() if job.posted_at else None,
                        job.description,
                        description_hash,
                        _json(job.raw),
                        job.first_seen_at.isoformat(),
                        now,
                        int(job.active),
                        job.description_fetched_at.isoformat()
                        if job.description_fetched_at
                        else (now if job.description else None),
                        job.fetch_tier or ("source" if job.description else None),
                        job.fetch_error,
                        gate,
                    ),
                )
        new = sum(job.id not in existing for job in rows)
        return new, len(rows) - new

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"],
            source=row["source"],
            external_id=row["external_id"],
            company=row["company"],
            title=row["title"],
            url=row["url"],
            locations=_loads(row["locations_json"], []),
            remote=bool(row["remote"]),
            posted_at=datetime.fromisoformat(row["posted_at"]) if row["posted_at"] else None,
            description=row["description"],
            raw=_loads(row["raw_json"], {}),
            first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
            last_seen_at=datetime.fromisoformat(row["last_seen_at"]),
            active=bool(row["active"]),
            description_fetched_at=(
                datetime.fromisoformat(row["description_fetched_at"])
                if row["description_fetched_at"]
                else None
            ),
            fetch_tier=row["fetch_tier"],
            fetch_error=row["fetch_error"],
            apply_gate=row["apply_gate"],
        )

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._row_to_job(row) if row else None

    def iter_all(
        self,
        batch: int = 1000,
        max_rows: Optional[int] = None,
        active_only: bool = True,
    ) -> Iterator[Job]:
        query = "SELECT * FROM jobs"
        params: list[Any] = []
        if active_only:
            query += " WHERE active=1"
        query += " ORDER BY first_seen_at DESC, id"
        if max_rows is not None:
            query += " LIMIT ?"
            params.append(max_rows)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        for row in rows:
            yield self._row_to_job(row)

    def iter_pending_enrichment(
        self,
        batch: int = 500,
        include_failed: bool = False,
        source: Optional[str] = None,
    ) -> Iterator[Job]:
        clauses = ["active=1"]
        if include_failed:
            clauses.append("(description_fetched_at IS NULL OR fetch_tier='failed')")
        else:
            clauses.append("description_fetched_at IS NULL")
        params: list[Any] = []
        if source:
            clauses.append("source=?")
            params.append(source)
        query = "SELECT * FROM jobs WHERE " + " AND ".join(clauses) + " ORDER BY first_seen_at, id"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        for row in rows:
            yield self._row_to_job(row)

    def iter_unclassified(self, batch: int = 500) -> Iterator[Job]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE active=1 AND description IS NOT NULL "
                "AND (facts_json IS NULL OR facts_source_hash != description_hash) "
                "ORDER BY first_seen_at, id"
            ).fetchall()
        for row in rows:
            yield self._row_to_job(row)

    def mark_enriched(
        self,
        job_id: str,
        description: Optional[str],
        tier: str,
        error: Optional[str] = None,
        classification: Optional[dict] = None,
        embedding: Optional[list[float]] = None,
    ) -> None:
        del embedding
        description_hash = self._description_hash(description)
        with self.transaction() as conn:
            conn.execute(
                """UPDATE jobs SET description=?, description_hash=?,
                   description_fetched_at=?, fetch_tier=?, fetch_error=?
                   WHERE id=?""",
                (description, description_hash, _utcnow(), tier, error, job_id),
            )
            if classification is not None:
                conn.execute(
                    """UPDATE jobs SET facts_json=?, facts_source_hash=?,
                       facts_extracted_at=? WHERE id=?""",
                    (_json(classification), description_hash, _utcnow(), job_id),
                )

    def set_classification(
        self,
        job_id: str,
        classification: dict,
        embedding: Optional[list[float]] = None,
        *,
        model: str | None = None,
        cost_usd: float = 0.0,
    ) -> None:
        del embedding
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT description_hash FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if not row:
                raise KeyError(job_id)
            conn.execute(
                """UPDATE jobs SET facts_json=?, facts_model=?, facts_source_hash=?,
                   facts_cost_usd=?, facts_extracted_at=? WHERE id=?""",
                (
                    _json(classification),
                    model,
                    row["description_hash"],
                    cost_usd,
                    _utcnow(),
                    job_id,
                ),
            )

    def get_facts(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT facts_json FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
        return _loads(row["facts_json"], None) if row else None

    def mark_inactive(self, job_id: str, reason: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE jobs SET active=0, inactive_reason=? WHERE id=?",
                (reason, job_id),
            )

    def set_apply_gate(self, job_id: str, gate: str) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE jobs SET apply_gate=? WHERE id=?", (gate, job_id))

    # ---- profile/evaluation ------------------------------------------

    def set_profile(self, profile: dict[str, Any]) -> str:
        raw = _json(profile)
        source_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO profile(id, profile_json, source_hash, updated_at)
                   VALUES(1,?,?,?) ON CONFLICT(id) DO UPDATE SET
                   profile_json=excluded.profile_json,
                   source_hash=excluded.source_hash,
                   updated_at=excluded.updated_at""",
                (raw, source_hash, _utcnow()),
            )
        return source_hash

    def get_profile(self) -> tuple[dict[str, Any], str] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM profile WHERE id=1").fetchone()
        if not row:
            return None
        return _loads(row["profile_json"], {}), row["source_hash"]

    def set_evaluation(
        self,
        job_id: str,
        decision: str,
        reasons: list[dict[str, Any]],
        profile_hash: str,
    ) -> None:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT facts_source_hash FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            conn.execute(
                """INSERT INTO evaluations(
                       job_id, decision, reasons_json, facts_source_hash,
                       profile_source_hash, evaluated_at
                   ) VALUES(?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET
                       decision=excluded.decision,
                       reasons_json=excluded.reasons_json,
                       facts_source_hash=excluded.facts_source_hash,
                       profile_source_hash=excluded.profile_source_hash,
                       evaluated_at=excluded.evaluated_at""",
                (
                    job_id,
                    decision,
                    _json(reasons),
                    row["facts_source_hash"] if row else None,
                    profile_hash,
                    _utcnow(),
                ),
            )
            if decision == "eligible":
                conn.execute(
                    """INSERT INTO applications(id, job_id, status, updated_at)
                       VALUES(?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET
                       status=CASE
                           WHEN applications.status IN ('applied','in_progress','tailored')
                           THEN applications.status ELSE 'eligible' END,
                       reason=NULL,
                       updated_at=excluded.updated_at""",
                    (uuid.uuid4().hex, job_id, "eligible", _utcnow()),
                )
            else:
                conn.execute(
                    """UPDATE applications SET status='skipped', reason=?, updated_at=?
                       WHERE job_id=? AND status NOT IN ('applied','in_progress')""",
                    (
                        reasons[0].get("code") if reasons else decision,
                        _utcnow(),
                        job_id,
                    ),
                )

    def iter_for_evaluation(self, limit: int = 0) -> Iterator[tuple[Job, dict[str, Any]]]:
        query = (
            "SELECT * FROM jobs WHERE active=1 AND facts_json IS NOT NULL "
            "ORDER BY first_seen_at, id"
        )
        params: list[Any] = []
        if limit:
            query += " LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        for row in rows:
            yield self._row_to_job(row), _loads(row["facts_json"], {})

    def get_evaluation(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM evaluations WHERE job_id=?", (job_id,)
            ).fetchone()
        if not row:
            return None
        return {
            "decision": row["decision"],
            "reasons": _loads(row["reasons_json"], []),
            "evaluated_at": row["evaluated_at"],
        }

    # ---- local matchmaking ------------------------------------------

    def iter_eligible_for_matching(
        self,
    ) -> Iterator[tuple[Job, dict[str, Any], str]]:
        """Yield eligible jobs only when their facts and profile are current."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT j.* FROM jobs j
                   JOIN evaluations e ON e.job_id=j.id
                   JOIN profile p ON p.id=1
                   WHERE j.active=1 AND j.facts_json IS NOT NULL
                     AND e.decision='eligible'
                     AND e.facts_source_hash=j.facts_source_hash
                     AND e.profile_source_hash=p.source_hash
                   ORDER BY j.first_seen_at, j.id"""
            ).fetchall()
        for row in rows:
            yield (
                self._row_to_job(row),
                _loads(row["facts_json"], {}),
                str(row["facts_source_hash"] or ""),
            )

    def load_job_embeddings(
        self, model: str
    ) -> dict[str, tuple[str, int, bytes]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT job_id, source_hash, dimensions, embedding
                   FROM job_embeddings WHERE model=?""",
                (model,),
            ).fetchall()
        return {
            row["job_id"]: (
                row["source_hash"],
                int(row["dimensions"]),
                bytes(row["embedding"]),
            )
            for row in rows
        }

    def save_job_embeddings(
        self,
        rows: Iterable[tuple[str, str, str, int, bytes]],
    ) -> None:
        values = list(rows)
        if not values:
            return
        now = _utcnow()
        with self.transaction() as conn:
            conn.executemany(
                """INSERT INTO job_embeddings(
                       job_id, model, source_hash, dimensions, embedding, embedded_at
                   ) VALUES(?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET
                       model=excluded.model,
                       source_hash=excluded.source_hash,
                       dimensions=excluded.dimensions,
                       embedding=excluded.embedding,
                       embedded_at=excluded.embedded_at""",
                [(*row, now) for row in values],
            )

    def save_candidate_embedding(
        self,
        *,
        model: str,
        source_hash: str,
        dimensions: int,
        embedding: bytes,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO candidate_embeddings(
                       id, model, source_hash, dimensions, embedding, embedded_at
                   ) VALUES(1,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                       model=excluded.model,
                       source_hash=excluded.source_hash,
                       dimensions=excluded.dimensions,
                       embedding=excluded.embedding,
                       embedded_at=excluded.embedded_at""",
                (model, source_hash, dimensions, embedding, _utcnow()),
            )

    def replace_match_scores(
        self,
        rows: Iterable[dict[str, Any]],
    ) -> None:
        values = list(rows)
        now = _utcnow()
        with self.transaction() as conn:
            conn.execute("DELETE FROM match_scores")
            conn.executemany(
                """INSERT INTO match_scores(
                       job_id, profile_source_hash, resume_source_hash,
                       job_embedding_source_hash, embedding_model,
                       semantic_similarity, score, band, components_json,
                       reasons_json, matched_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        row["job_id"],
                        row["profile_source_hash"],
                        row["resume_source_hash"],
                        row["job_embedding_source_hash"],
                        row["embedding_model"],
                        row["semantic_similarity"],
                        row["score"],
                        row["band"],
                        _json(row["components"]),
                        _json(row["reasons"]),
                        now,
                    )
                    for row in values
                ],
            )

    def replace_duplicate_assignments(
        self,
        rows: Iterable[dict[str, str]],
    ) -> None:
        values = list(rows)
        now = _utcnow()
        with self.transaction() as conn:
            conn.execute("DELETE FROM job_duplicates")
            conn.executemany(
                """INSERT INTO job_duplicates(
                       job_id, canonical_job_id, method, duplicate_key, detected_at
                   ) VALUES(?,?,?,?,?)""",
                [
                    (
                        row["job_id"], row["canonical_job_id"], row["method"],
                        row["duplicate_key"], now,
                    )
                    for row in values
                ],
            )

    def record_liveness(
        self, job_id: str, status: str, method: str, detail: str
    ) -> None:
        now = _utcnow()
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO job_liveness(job_id,status,method,detail,checked_at)
                   VALUES(?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET
                     status=excluded.status, method=excluded.method,
                     detail=excluded.detail, checked_at=excluded.checked_at""",
                (job_id, status, method, detail, now),
            )
            if status == "closed":
                conn.execute(
                    "UPDATE jobs SET active=0, inactive_reason='posting_closed' WHERE id=?",
                    (job_id,),
                )
                conn.execute(
                    """UPDATE applications SET status='skipped', reason='posting_closed',
                       updated_at=? WHERE job_id=?
                       AND status NOT IN ('applied','in_progress')""",
                    (now, job_id),
                )

    def get_liveness(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM job_liveness WHERE job_id=?", (job_id,)
            ).fetchone()
        return dict(row) if row else None

    def iter_ranked_matches(
        self,
        *,
        limit: int = 50,
        minimum_score: float = 0.0,
    ) -> Iterator[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT m.*, j.company, j.title, j.url, j.locations_json,
                          j.posted_at, j.source
                   FROM match_scores m JOIN jobs j ON j.id=m.job_id
                   LEFT JOIN job_duplicates d ON d.job_id=m.job_id
                   WHERE m.score>=? AND d.job_id IS NULL AND j.active=1
                   ORDER BY m.score DESC, j.posted_at DESC, m.job_id
                   LIMIT ?""",
                (minimum_score, limit),
            ).fetchall()
        for row in rows:
            yield {
                "job_id": row["job_id"],
                "company": row["company"],
                "title": row["title"],
                "url": row["url"],
                "locations": _loads(row["locations_json"], []),
                "posted_at": row["posted_at"],
                "source": row["source"],
                "score": row["score"],
                "band": row["band"],
                "semantic_similarity": row["semantic_similarity"],
                "components": _loads(row["components_json"], {}),
                "reasons": _loads(row["reasons_json"], []),
            }

    # ---- tailoring/applications -------------------------------------

    def save_tailored_resume(
        self,
        *,
        job_id: str,
        source_resume_hash: str,
        model: str,
        edit_plan: dict[str, Any],
        latex_path: str,
        pdf_path: str | None,
        cost_usd: float,
    ) -> str:
        tailored_id = uuid.uuid4().hex
        now = _utcnow()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT description_hash FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if not row:
                raise KeyError(job_id)
            conn.execute(
                """INSERT INTO tailored_resumes(
                       id, job_id, source_resume_hash, job_description_hash,
                       model, edit_plan_json, latex_path, pdf_path, cost_usd,
                       created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    tailored_id,
                    job_id,
                    source_resume_hash,
                    row["description_hash"],
                    model,
                    _json(edit_plan),
                    latex_path,
                    pdf_path,
                    cost_usd,
                    now,
                ),
            )
            conn.execute(
                """INSERT INTO applications(
                       id, job_id, tailored_resume_id, status, updated_at
                   ) VALUES(?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET
                       tailored_resume_id=excluded.tailored_resume_id,
                       status=CASE
                           WHEN applications.status IN ('applied','in_progress')
                           THEN applications.status ELSE 'tailored' END,
                       reason=CASE
                           WHEN applications.status IN ('applied','in_progress')
                           THEN applications.reason ELSE NULL END,
                       updated_at=excluded.updated_at""",
                (uuid.uuid4().hex, job_id, tailored_id, "tailored", now),
            )
        return tailored_id

    def get_application_by_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """SELECT a.*, t.latex_path, t.pdf_path, t.edit_plan_json,
                          t.model AS tailor_model
                   FROM applications a
                   LEFT JOIN tailored_resumes t ON t.id=a.tailored_resume_id
                   WHERE a.job_id=?""",
                (job_id,),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["edit_plan"] = _loads(result.pop("edit_plan_json", None), {})
        return result

    def mark_application_review(self, job_id: str, reason: str) -> None:
        """Route an eligible application to human review without tailoring it."""
        now = _utcnow()
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO applications(id, job_id, status, reason, updated_at)
                   VALUES(?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET
                       status=CASE
                           WHEN applications.status IN ('applied','in_progress')
                           THEN applications.status ELSE 'review' END,
                       reason=CASE
                           WHEN applications.status IN ('applied','in_progress')
                           THEN applications.reason ELSE excluded.reason END,
                       updated_at=excluded.updated_at""",
                (uuid.uuid4().hex, job_id, "review", reason, now),
            )

    def start_apply_attempt(self, job_id: str, model: str) -> tuple[dict[str, Any], str]:
        """Claim one tailored application and create its durable attempt row."""
        attempt_id = uuid.uuid4().hex
        now = _utcnow()
        with self.transaction() as conn:
            row = conn.execute(
                """SELECT a.*, t.latex_path, t.pdf_path, t.edit_plan_json,
                          t.model AS tailor_model
                   FROM applications a
                   LEFT JOIN tailored_resumes t ON t.id=a.tailored_resume_id
                   WHERE a.job_id=?""",
                (job_id,),
            ).fetchone()
            if not row:
                raise ValueError("tailor this job before applying")
            if not row["pdf_path"]:
                raise ValueError("tailored application has no compiled PDF")
            if row["status"] == "applied":
                raise ValueError("job is already marked applied")
            if row["status"] == "in_progress":
                raise ValueError("an apply attempt is already in progress")
            conn.execute(
                "UPDATE applications SET status='in_progress', claimed_at=?, updated_at=? WHERE id=?",
                (now, now, row["id"]),
            )
            conn.execute(
                """INSERT INTO apply_attempts(
                       id, application_id, status, model, started_at
                   ) VALUES(?,?,?,?,?)""",
                (attempt_id, row["id"], "in_progress", model, now),
            )
        result = dict(row)
        result["edit_plan"] = _loads(result.pop("edit_plan_json", None), {})
        return result, attempt_id

    def finish_apply_attempt(
        self,
        attempt_id: str,
        *,
        status: str,
        reason: str,
        cost_usd: float = 0.0,
        turn_count: int = 0,
        tool_calls: dict[str, int] | None = None,
    ) -> None:
        """Persist the attempt verdict and mirror it onto the application."""
        now = _utcnow()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT application_id FROM apply_attempts WHERE id=?", (attempt_id,)
            ).fetchone()
            if not row:
                raise KeyError(attempt_id)
            conn.execute(
                """UPDATE apply_attempts SET status=?, reason=?, cost_usd=?,
                       turn_count=?, tool_calls_json=?, finished_at=? WHERE id=?""",
                (
                    status,
                    reason,
                    cost_usd,
                    turn_count,
                    _json(tool_calls or {}),
                    now,
                    attempt_id,
                ),
            )
            # Infrastructure failures remain retryable. Test runs have their
            # own visible state and can be re-run or promoted to a real apply.
            app_status = "tailored" if status == "infra_error" else status
            conn.execute(
                """UPDATE applications SET status=?, reason=?, claimed_at=NULL,
                       updated_at=? WHERE id=?""",
                (app_status, reason, now, row["application_id"]),
            )

    def record_apply_trace_event(
        self,
        attempt_id: str,
        *,
        sequence: int,
        turn: int | None,
        event_type: str,
        name: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Append one sanitized event while an apply attempt is running."""
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO apply_trace_events(
                       attempt_id, sequence, turn, event_type, name,
                       payload_json, created_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    attempt_id,
                    sequence,
                    turn,
                    event_type,
                    name,
                    _json(payload or {}),
                    _utcnow(),
                ),
            )

    def get_apply_trace(self, attempt_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT sequence, turn, event_type, name, payload_json, created_at
                   FROM apply_trace_events WHERE attempt_id=? ORDER BY sequence""",
                (attempt_id,),
            ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = _loads(item.pop("payload_json", None), {})
            events.append(item)
        return events

    def get_apply_attempts(self, job_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT aa.* FROM apply_attempts aa
                   JOIN applications a ON a.id=aa.application_id
                   WHERE a.job_id=? ORDER BY aa.started_at""",
                (job_id,),
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["tool_calls"] = _loads(item.pop("tool_calls_json", None), {})
            out.append(item)
        return out

    def record_profile_question_gap(
        self,
        *,
        label: str,
        category: str,
        job_id: str,
        company: str,
    ) -> None:
        normalized = _normalize_question_label(label)
        if not normalized:
            return
        now = _utcnow()
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO profile_question_gaps(
                       normalized_label, sample_label, category, first_job_id,
                       last_job_id, last_company, occurrences, status,
                       first_seen_at, last_seen_at
                   ) VALUES(?,?,?,?,?,?,1,'open',?,?)
                   ON CONFLICT(normalized_label) DO UPDATE SET
                       sample_label=excluded.sample_label,
                       category=excluded.category,
                       last_job_id=excluded.last_job_id,
                       last_company=excluded.last_company,
                       occurrences=profile_question_gaps.occurrences + 1,
                       last_seen_at=excluded.last_seen_at""",
                (
                    normalized, label.strip()[:500], category.strip()[:80],
                    job_id, job_id, company.strip()[:200], now, now,
                ),
            )

    def list_profile_question_gaps(self, *, include_closed: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM profile_question_gaps"
        params: tuple[Any, ...] = ()
        if not include_closed:
            query += " WHERE status='open'"
        query += " ORDER BY occurrences DESC, last_seen_at DESC"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["answer"] = _loads(item.pop("answer_json", None), None)
            out.append(item)
        return out

    def counts(self) -> dict[str, int]:
        with self._lock:
            row = self._conn.execute(
                """SELECT
                   COUNT(*) AS jobs,
                   SUM(CASE WHEN description IS NOT NULL THEN 1 ELSE 0 END) AS described,
                   SUM(CASE WHEN facts_json IS NOT NULL THEN 1 ELSE 0 END) AS extracted,
                   SUM(CASE WHEN active=1 THEN 1 ELSE 0 END) AS active
                   FROM jobs"""
            ).fetchone()
        return {key: int(row[key] or 0) for key in row.keys()}


_default_store: LocalStore | None = None


def get_local_store(path: str | Path | None = None) -> LocalStore:
    global _default_store
    resolved = Path(path) if path is not None else default_db_path()
    if _default_store is None or _default_store.path != resolved:
        if _default_store is not None:
            _default_store.close()
        _default_store = LocalStore(resolved)
    return _default_store
