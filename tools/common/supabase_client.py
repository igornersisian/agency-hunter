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
