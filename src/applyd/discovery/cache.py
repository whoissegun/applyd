from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ._base import parse_iso


DEFAULT_RESOLVER_CACHE = Path("data/resolver_cache.json")
DEFAULT_BROAD_CACHE = Path("data/broad_search_cache.json")


class ResolverCache:
    """company name -> (ATS, slug). No TTL; invalidate manually."""

    def __init__(self, path: Path = DEFAULT_RESOLVER_CACHE) -> None:
        self.path = Path(path)
        self._entries: dict[str, dict] = {}

    def load(self) -> None:
        if not self.path.exists():
            self._entries = {}
            return
        with self.path.open("r", encoding="utf-8") as f:
            self._entries = json.load(f)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(self._entries, f, indent=2)
        tmp.replace(self.path)

    @staticmethod
    def _key(company: str) -> str:
        return company.strip().lower()

    def get(self, company: str) -> Optional[tuple[str, str]]:
        entry = self._entries.get(self._key(company))
        if not entry:
            return None
        return (entry["ats"], entry["slug"])

    def set(self, company: str, ats: str, slug: str, source: str = "brave") -> None:
        self._entries[self._key(company)] = {
            "ats": ats,
            "slug": slug,
            "source": source,
            "resolved_at": datetime.now(timezone.utc).isoformat(),
            "original_name": company,
        }

    def delete(self, company: str) -> None:
        self._entries.pop(self._key(company), None)


class BroadSearchCache:
    """dork keyword -> list of (ATS, slug) pairs discovered, with TTL.

    Lets us skip Brave queries when the same dorks were run recently.
    """

    def __init__(
        self,
        path: Path = DEFAULT_BROAD_CACHE,
        ttl_hours: float = 6.0,
    ) -> None:
        self.path = Path(path)
        self.ttl_hours = ttl_hours
        self._entries: dict[str, dict] = {}

    def load(self) -> None:
        if not self.path.exists():
            self._entries = {}
            return
        with self.path.open("r", encoding="utf-8") as f:
            self._entries = json.load(f)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(self._entries, f, indent=2)
        tmp.replace(self.path)

    @staticmethod
    def _key(keyword: str) -> str:
        return keyword.strip().lower()

    def get(self, keyword: str) -> Optional[list[tuple[str, str]]]:
        """Return fresh cached pairs or None if missing/expired."""
        entry = self._entries.get(self._key(keyword))
        if not entry:
            return None
        ts = parse_iso(entry.get("discovered_at"))
        if ts is None:
            return None
        age_seconds = (datetime.now(timezone.utc) - ts).total_seconds()
        if age_seconds > self.ttl_hours * 3600:
            return None
        return [(r["ats"], r["slug"]) for r in entry.get("results", [])]

    def set(self, keyword: str, pairs: list[tuple[str, str]]) -> None:
        self._entries[self._key(keyword)] = {
            "discovered_at": datetime.now(timezone.utc).isoformat(),
            "results": [{"ats": a, "slug": s} for a, s in pairs],
        }

    def delete(self, keyword: str) -> None:
        self._entries.pop(self._key(keyword), None)
