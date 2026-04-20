from __future__ import annotations

import os
from typing import Optional

import httpx

from ..discovery._base import http_client


API_URL = "https://api.spider.cloud/v1/scrape"


class SpiderClient:
    """Thin wrapper around spider.cloud's /v1/scrape endpoint."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("SPIDER_API_KEY")
        if not self.api_key:
            raise RuntimeError("SPIDER_API_KEY not set")
        self._client = client

    def scrape(
        self,
        url: str,
        chrome: bool = False,
        proxy: bool = True,
        timeout: float = 120.0,
    ) -> str:
        """Return markdown content (may be empty if page had no extractable text)."""
        body = {
            "url": url,
            "request": "chrome" if chrome else "smart",
            "return_format": "markdown",
            "proxy_enabled": proxy,
        }
        with http_client(self._client) as c:
            resp = c.post(
                API_URL,
                json=body,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=timeout,
            )
        resp.raise_for_status()
        data = resp.json()
        # response shape: list[dict] OR dict (single-page scrape varies)
        item = data[0] if isinstance(data, list) and data else data
        if not isinstance(item, dict):
            return ""
        content = item.get("content") or ""
        return content if isinstance(content, str) else ""
