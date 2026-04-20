from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from typing import Iterator, Optional

import httpx


USER_AGENT = "applyd/0.1"


@contextmanager
def http_client(client: Optional[httpx.Client] = None) -> Iterator[httpx.Client]:
    """Yield a borrowed client or own a fresh one, closing it on exit."""
    if client is not None:
        yield client
        return
    owned = httpx.Client(timeout=30.0, headers={"User-Agent": USER_AGENT})
    try:
        yield owned
    finally:
        owned.close()


def parse_iso(value: object) -> Optional[datetime]:
    """Parse an ISO-8601 datetime string (handling 'Z'). Returns None on failure."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
