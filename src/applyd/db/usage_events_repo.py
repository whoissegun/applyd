from __future__ import annotations

from typing import Optional

from supabase import Client


_ALLOWED_EVENT_TYPES = frozenset({"tailor", "apply", "enrich"})


class UsageEventsRepo:
    """Granular billing line items. One row per cost-incurring operation.
    Aggregations (monthly spend, per-user usage) are read queries on top —
    not this repo's job.
    """

    def __init__(self, client: Client) -> None:
        self.client = client

    def record(
        self,
        user_id: str,
        event_type: str,
        cost_cents: int,
        metadata: Optional[dict] = None,
    ) -> dict:
        if event_type not in _ALLOWED_EVENT_TYPES:
            raise ValueError(
                f"event_type must be one of {sorted(_ALLOWED_EVENT_TYPES)}, got {event_type!r}"
            )
        payload = {
            "user_id": user_id,
            "event_type": event_type,
            "cost_cents": int(cost_cents),
            "metadata": metadata if metadata is not None else {},
        }
        res = self.client.table("usage_events").insert(payload).execute()
        return res.data[0]
