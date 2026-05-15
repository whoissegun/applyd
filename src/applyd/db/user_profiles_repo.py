from __future__ import annotations

from typing import Any, Optional

from supabase import Client


# Columns the auth-signup trigger pre-populates and the user can edit afterwards.
# Anything not in this set is silently dropped from update() to avoid PostgREST
# 400s on typos / FE-bug-introduced keys.
_ALLOWED_UPDATE_FIELDS = frozenset(
    {
        "full_name",
        "phone",
        "linkedin_url",
        "github_url",
        "portfolio_url",
        "work_auth_summary",
        "sponsorship_needed_countries",
        "target_levels",
        "target_specialties",
        "target_locations",
        "strategy",
        "profile_answers",
    }
)


class UserProfilesRepo:
    """Per-user profile metadata. Rows are auto-created by the
    internal.handle_new_auth_user() trigger on auth.users insert, so callers
    should treat get() returning None as a signal something is wrong with the
    trigger, not a normal case for an existing auth user.
    """

    def __init__(self, client: Client) -> None:
        self.client = client

    def get(self, user_id: str) -> Optional[dict]:
        res = (
            self.client.table("user_profiles")
            .select("*")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None

    def update(self, user_id: str, **fields: Any) -> Optional[dict]:
        """Patch the row in place. Unknown keys are dropped (defensive against
        FE typos / drift). Returns the updated row, or None if no row exists.
        """
        payload = {k: v for k, v in fields.items() if k in _ALLOWED_UPDATE_FIELDS}
        if not payload:
            # No-op update would round-trip the server for nothing — just return
            # the current state.
            return self.get(user_id)
        res = (
            self.client.table("user_profiles")
            .update(payload)
            .eq("id", user_id)
            .execute()
        )
        return res.data[0] if res.data else None
