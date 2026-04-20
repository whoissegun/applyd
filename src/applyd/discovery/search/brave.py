from __future__ import annotations

import os
from typing import Optional

import httpx

from .._base import http_client
from .base import SearchResult


class BraveSearch:
    name = "brave"

    def __init__(
        self,
        api_key: Optional[str] = None,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("BRAVE_SEARCH_API_KEY")
        if not self.api_key:
            raise RuntimeError("BRAVE_SEARCH_API_KEY not set")
        self._client = client

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        with http_client(self._client) as c:
            r = c.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": limit},
                headers={
                    "X-Subscription-Token": self.api_key,
                    "Accept": "application/json",
                },
            )
            r.raise_for_status()
            data = r.json()
        return [
            SearchResult(
                url=hit.get("url", ""),
                title=hit.get("title", ""),
                description=hit.get("description", ""),
            )
            for hit in (data.get("web") or {}).get("results", [])
        ]
