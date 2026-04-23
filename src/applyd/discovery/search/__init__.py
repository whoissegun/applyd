import httpx

from .base import SearchProvider, SearchResult
from .brave import BraveSearch
from .serper import SerperSearch


def make_provider(name: str, client: httpx.Client) -> SearchProvider:
    name = name.lower()
    if name == "brave":
        return BraveSearch(client=client)
    if name == "serper":
        return SerperSearch(client=client)
    raise RuntimeError(f"unknown SEARCH_PROVIDER: {name}")


__all__ = [
    "SearchProvider", "SearchResult", "BraveSearch", "SerperSearch",
    "make_provider",
]
