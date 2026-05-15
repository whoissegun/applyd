from __future__ import annotations

import os
from functools import lru_cache

from supabase import Client, create_client


@lru_cache(maxsize=1)
def get_client() -> Client:
    """Worker-side Supabase client using the secret key (bypasses RLS).

    Frontend / per-user contexts should build their own client with the
    publishable key + a user JWT — never reuse this one.
    """
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SECRET_KEY"]
    return create_client(url, key)
