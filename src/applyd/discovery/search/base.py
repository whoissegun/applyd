from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class SearchResult:
    url: str
    title: str = ""
    description: str = ""


class SearchProvider(Protocol):
    """Any provider we plug in must match this shape."""

    name: str

    def search(self, query: str, limit: int = 10) -> list[SearchResult]: ...
