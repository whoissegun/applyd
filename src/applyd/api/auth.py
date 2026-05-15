"""JWT auth dependency for FastAPI endpoints.

Validates the user's bearer token against Supabase Auth using a publishable-key
client (NOT the service-role client — that bypasses RLS and must never be used
for auth verification). Returns the auth user's id, which the endpoint then uses
to scope its repo calls.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Optional

from fastapi import Header, HTTPException, status
from supabase import Client, create_client


@lru_cache(maxsize=1)
def _auth_client() -> Client:
    url = os.environ["SUPABASE_URL"]
    # Publishable / anon key — safe to use as an auth verifier.
    key = os.environ["SUPABASE_PUBLISHABLE_KEY"]
    return create_client(url, key)


def get_current_user_id(authorization: Optional[str] = Header(default=None)) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
        )
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="empty bearer token",
        )

    try:
        resp = _auth_client().auth.get_user(token)
    except Exception as exc:  # supabase-py raises various AuthApi errors
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"invalid token: {exc.__class__.__name__}",
        )

    user = getattr(resp, "user", None)
    if user is None or not getattr(user, "id", None):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token did not resolve to a user",
        )
    return user.id
