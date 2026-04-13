"""
Load the shared profile row from Supabase.

The `profile` table is shared with Job-search-automation. It stores Igor's
parsed resume under `parsed` (JSONB) and arbitrary per-project config keys
that live on the same row — agency-hunter reads its own config namespace
`agency_config` from the parsed JSON.

Config keys agency-hunter looks for (all optional, defaults applied here):
    agency_target_countries    list[str], ISO alpha-2, e.g. ["US","GB","DE"]
    agency_send_cap            int, daily outreach send cap
    agency_fit_threshold       int, 0-100, default 65
    agency_sender_email        str, FROM address (usually Igor's personal Gmail)
    agency_excluded_domains    list[str], domains to drop at discovery time
"""

import os
import logging

from .supabase_client import get_supabase

logger = logging.getLogger(__name__)

_DEFAULTS = {
    "agency_target_countries": [
        # Must stay in sync with tools/config/serp_queries.json countries —
        # this list is passed to classify_agency as "Igor's target markets",
        # and market_fit sub-score depends on whether an agency's country
        # is in here. Out-of-list countries get scored as off-target and
        # typically fall below the 70 threshold.
        "US", "CA", "GB", "IE", "DE", "AT", "CH",
        "NL", "SE", "NO", "DK", "FI", "AU", "NZ",
        "SG", "IL", "AE", "ZA", "BE", "LU", "EE",
        "PL", "ES", "PT", "MX", "CO", "AR", "BR", "UY",
    ],
    "agency_send_cap": int(os.environ.get("AGENCY_DAILY_SEND_CAP", "15")),
    "agency_fit_threshold": int(os.environ.get("AGENCY_FIT_THRESHOLD", "65")),
    "agency_sender_email": os.environ.get("AGENCY_SENDER_EMAIL", ""),
    "agency_excluded_domains": [
        "clutch.co", "goodfirms.co", "sortlist.com", "designrush.com",
        "linkedin.com", "facebook.com", "twitter.com", "x.com",
        "instagram.com", "youtube.com", "wikipedia.org", "crunchbase.com",
        "glassdoor.com", "indeed.com", "g2.com", "capterra.com",
        "producthunt.com", "github.com", "medium.com",
    ],
}


def get_profile() -> dict | None:
    """Return the parsed resume JSONB for Igor.

    Same pattern as Job-search-automation/tools/process_jobs.py:get_profile().
    Returns the `parsed` dict or None if no profile row exists yet.
    """
    result = (
        get_supabase()
        .table("profile")
        .select("parsed")
        .order("updated_at", desc=True)
        .limit(1)
        .execute()
    )
    if result.data:
        return result.data[0]["parsed"]
    return None


def get_agency_config() -> dict:
    """Return agency-hunter config merged over the defaults.

    Reads the keys listed in `_DEFAULTS` from the parsed profile. Missing keys
    fall back to the defaults (which themselves respect env vars for the
    numeric caps).
    """
    profile = get_profile() or {}
    cfg = dict(_DEFAULTS)
    for key in _DEFAULTS:
        if key in profile and profile[key] not in (None, "", []):
            cfg[key] = profile[key]
    return cfg
