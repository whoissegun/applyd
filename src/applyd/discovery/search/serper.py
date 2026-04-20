from __future__ import annotations

import os
from typing import Optional

import httpx

from .._base import http_client
from .base import SearchResult


class SerperSearch:
    name = "serper"

    def __init__(
        self,
        api_key: Optional[str] = None,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("SERPER_API_KEY")
        if not self.api_key:
            raise RuntimeError("SERPER_API_KEY not set")
        self._client = client

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        with http_client(self._client) as c:
            r = c.post(
                "https://google.serper.dev/search",
                json={"q": query, "num": limit},
                headers={
                    "X-API-KEY": self.api_key,
                    "Content-Type": "application/json",
                },
            )
            r.raise_for_status()
            data = r.json()
        return [
            SearchResult(
                url=hit.get("link", ""),
                title=hit.get("title", ""),
                description=hit.get("snippet", ""),
            )
            for hit in data.get("organic", [])
        ]
