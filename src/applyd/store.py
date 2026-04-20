from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

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
                updated += 1
        return new, updated

    def all(self) -> list[Job]:
        return list(self._jobs.values())
