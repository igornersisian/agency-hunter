"""
Lazy singleton Supabase client. Shared with Job-search-automation (same
self-hosted instance, different table prefixes).

All agency-hunter tables are prefixed `agency_`.
"""

import os

from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

_supabase: Client | None = None


def get_supabase() -> Client:
    global _supabase
    if _supabase is None:
        _supabase = create_client(
            os.environ["SUPABASE_URL"],
            os.environ["SUPABASE_SERVICE_ROLE_KEY"],
        )
    return _supabase


def reset_supabase() -> Client:
    """Drop the cached client and build a fresh one.

    Needed when the underlying httpx HTTP/2 connection enters an invalid
    state (observed after thousands of sequential inserts: RECV_WINDOW_UPDATE
    in CLOSED). Callers catch the httpx error, reset, and retry.
    """
    global _supabase
    _supabase = None
    return get_supabase()
