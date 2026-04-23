from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .discovery.routing import detect_gate
from .models import Job


DEFAULT_STORE_PATH = Path("data/jobs.json")


class JobStore:
    def __init__(self, path: Path = DEFAULT_STORE_PATH) -> None:
        self.path = Path(path)
        self._jobs: dict[str, Job] = {}

    def load(self) -> None:
        if not self.path.exists():
            self._jobs = {}
            return
        with self.path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        self._jobs = {jid: Job.model_validate(j) for jid, j in raw.items()}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        payload = {jid: j.model_dump(mode="json") for jid, j in self._jobs.items()}
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)
        tmp.replace(self.path)

    def upsert(self, incoming: Iterable[Job]) -> tuple[int, int]:
        """Returns (new_count, updated_count)."""
        new, updated = 0, 0
        now = datetime.now(timezone.utc)
        for job in incoming:
            existing = self._jobs.get(job.id)
            if existing is None:
                job.last_seen_at = now
                if job.apply_gate is None:
                    job.apply_gate = detect_gate(job.url)
                self._jobs[job.id] = job
                new += 1
            else:
                existing.last_seen_at = now
                existing.title = job.title
                existing.locations = job.locations
                existing.remote = job.remote
                existing.description = job.description or existing.description
                existing.url = job.url or existing.url
                existing.active = True
                existing.raw = job.raw
                # Only set gate if not already set — runtime-reported values
                # (signup_required, captcha, etc.) must not be downgraded to "portal".
                if existing.apply_gate is None:
                    existing.apply_gate = detect_gate(existing.url)
                updated += 1
        return new, updated

    def all(self) -> list[Job]:
        return list(self._jobs.values())

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def pending_apply(self, limit: int = 100) -> list[Job]:
        """Jobs that have been tailored but not yet attempted by the apply agent.

        Default cap of 100 matches APPLYD_DAILY_CAP — the agent picks FIFO from here.
        """
        ready = [
            j for j in self._jobs.values()
            if j.resume_pdf_path and j.apply_status is None and j.active
            and j.apply_gate is None
        ]
        ready.sort(key=lambda j: j.last_seen_at)
        return ready[:limit]

    def mark_apply(self, job_id: str, status: str, note: str | None = None) -> None:
        """Record the outcome of an apply attempt. Status: applied | skipped | failed."""
        job = self._jobs[job_id]
        job.apply_status = status
        job.apply_attempted_at = datetime.now(timezone.utc)
        job.apply_note = note
