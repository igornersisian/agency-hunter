"""
Send-window helper for the outreach scheduler.

Policy (Igor, 2026-04-12):
    - Window:   Mon-Fri 09:00-17:00 **recipient-local time**
    - Spacing:  random 5-20 minutes between consecutive sends (global)
    - Overflow: if the computed slot is outside the window or on a
                weekend, roll forward to the next Mon-Fri 09:00 local.

Each agency carries an ISO-3166 alpha-2 country code from classification.
`country_timezone()` maps that to a zoneinfo key. Mapping is hand-curated
for the 14 target countries — for US/CA/AU we pick one canonical
metropolitan zone (most AI agencies cluster on the east coast). Anything
off-list falls back to UTC, which is a safe Monday-morning fallback.

The scheduler calls `compute_next_slot(country, last_scheduled_utc)` to
pick the next `scheduled_for` value. `last_scheduled_utc` is the
currently-latest slot in the queue — the new slot lands 5-20 minutes
later, advanced forward into the window as needed.

Stateless module, no DB access. Pass in the data you have.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Canonical zone per target country. For federated countries (US, CA, AU)
# we pick one time zone per country — cold-outreach timing doesn't need
# city-level precision, and most AI/automation agencies cluster on a
# single coast anyway (US east, AU east, CA east).
_COUNTRY_TZ = {
    # Original 14
    "US": "America/New_York",
    "CA": "America/Toronto",
    "GB": "Europe/London",
    "IE": "Europe/Dublin",
    "DE": "Europe/Berlin",
    "AT": "Europe/Vienna",
    "CH": "Europe/Zurich",
    "NL": "Europe/Amsterdam",
    "SE": "Europe/Stockholm",
    "NO": "Europe/Oslo",
    "DK": "Europe/Copenhagen",
    "FI": "Europe/Helsinki",
    "AU": "Australia/Sydney",
    "NZ": "Pacific/Auckland",
    # Expansion 2026-04 — matches serp_queries.json countries
    "SG": "Asia/Singapore",
    "IL": "Asia/Jerusalem",
    "AE": "Asia/Dubai",
    "ZA": "Africa/Johannesburg",
    "BE": "Europe/Brussels",
    "LU": "Europe/Luxembourg",
    "EE": "Europe/Tallinn",
    "PL": "Europe/Warsaw",
    "ES": "Europe/Madrid",
    "PT": "Europe/Lisbon",
    "MX": "America/Mexico_City",
    "CO": "America/Bogota",
    "AR": "America/Argentina/Buenos_Aires",
    "BR": "America/Sao_Paulo",
    "UY": "America/Montevideo",
}

# Mon-Fri 09:00-17:00
_WINDOW_START_HOUR = 9
_WINDOW_END_HOUR = 17  # exclusive; last valid send minute is 16:59
_WEEKDAYS = (0, 1, 2, 3, 4)  # Mon-Fri (Python weekday: Mon=0)

# Random spacing, minutes
_SPACING_MIN = 5
_SPACING_MAX = 20


def country_timezone(country: str | None) -> ZoneInfo:
    """Return the canonical recipient time zone for a 2-letter country,
    or UTC as a safe fallback for unknown/missing countries."""
    if not country:
        return ZoneInfo("UTC")
    return ZoneInfo(_COUNTRY_TZ.get(country.upper(), "UTC"))


def _roll_into_window(local_dt: datetime) -> datetime:
    """Advance `local_dt` forward until it sits inside a valid send slot.
    Assumes `local_dt.tzinfo` is set. Returns a tz-aware datetime in the
    same zone."""
    dt = local_dt
    # Walk forward day-by-day until we land on a weekday with valid time
    while True:
        weekday_ok = dt.weekday() in _WEEKDAYS
        time_ok = _WINDOW_START_HOUR <= dt.hour < _WINDOW_END_HOUR

        if weekday_ok and time_ok:
            return dt

        if not weekday_ok:
            # Weekend — jump to Monday 09:00 local
            days_to_monday = (7 - dt.weekday()) % 7
            if days_to_monday == 0:  # already Monday but still failed time check
                days_to_monday = 0
            dt = (dt + timedelta(days=days_to_monday)).replace(
                hour=_WINDOW_START_HOUR, minute=0, second=0, microsecond=0
            )
            continue

        # Weekday, but time is out of window
        if dt.hour < _WINDOW_START_HOUR:
            dt = dt.replace(hour=_WINDOW_START_HOUR, minute=0, second=0, microsecond=0)
            continue
        # After window → move to next day 09:00 and re-check (may land on weekend)
        dt = (dt + timedelta(days=1)).replace(
            hour=_WINDOW_START_HOUR, minute=0, second=0, microsecond=0
        )


def compute_next_slot(
    country: str | None,
    last_scheduled_utc: datetime | None,
    now_utc: datetime | None = None,
    rng: random.Random | None = None,
) -> datetime:
    """Pick the next `scheduled_for` timestamp for a draft being queued.

    Args:
        country: ISO-3166 alpha-2 of the recipient agency.
        last_scheduled_utc: UTC timestamp of the most recently queued
            outreach (global max across all pending sends). Used to
            enforce the random 5-20 min spacing. None when the queue is
            empty.
        now_utc: Override "now" for deterministic tests. Defaults to
            datetime.now(UTC).
        rng: Optional random.Random for reproducible spacing in tests.

    Returns:
        UTC tz-aware datetime at which this draft should be sent.
    """
    rng = rng or random
    now_utc = now_utc or datetime.now(timezone.utc)

    # Earliest possible UTC time this new send can happen
    spacing = timedelta(minutes=rng.randint(_SPACING_MIN, _SPACING_MAX))
    if last_scheduled_utc is None:
        earliest_utc = now_utc
    else:
        earliest_utc = max(now_utc, last_scheduled_utc + spacing)

    # Convert to recipient-local and roll into the window
    tz = country_timezone(country)
    local = earliest_utc.astimezone(tz)
    local_in_window = _roll_into_window(local)

    # Back to UTC for storage
    return local_in_window.astimezone(timezone.utc)
