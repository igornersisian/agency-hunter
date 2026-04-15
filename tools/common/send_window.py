"""
Send-window helper for the outreach scheduler.

Policy (Igor, 2026-04-15):
    - Window:   Mon-Fri 16:00-22:00 **sender-local time** (Asia/Bangkok, GMT+7)
    - Spacing:  random 5-20 minutes between consecutive sends (global)
    - Overflow: if the computed slot is outside the window or on a
                weekend, roll forward to the next Mon-Fri 16:00 local.

Recipient timezone is intentionally ignored. Globally-scattered
recipients were stretching the queue across days and starving Igor's
actual approval throughput. One sender-side window keeps sends
predictable and reply handling inside his working hours.

The scheduler calls `compute_next_slot(country, last_scheduled_utc)` to
pick the next `scheduled_for` value. `country` is accepted for
signature compatibility but no longer affects the slot.
`last_scheduled_utc` is the currently-latest slot in the queue — the
new slot lands 5-20 minutes later, advanced forward into the window as
needed.

Stateless module, no DB access. Pass in the data you have.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_SENDER_TZ = ZoneInfo("Asia/Bangkok")  # GMT+7, no DST

# Mon-Fri 16:00-22:00 sender-local
_WINDOW_START_HOUR = 16
_WINDOW_END_HOUR = 22  # exclusive; last valid send minute is 21:59
_WEEKDAYS = (0, 1, 2, 3, 4)  # Mon-Fri (Python weekday: Mon=0)

# Random spacing, minutes
_SPACING_MIN = 5
_SPACING_MAX = 20


def country_timezone(country: str | None) -> ZoneInfo:
    """Deprecated. Kept for import compatibility; sender-side window
    doesn't use recipient tz. Always returns the sender zone."""
    return _SENDER_TZ


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
            # Weekend — jump to Monday start-of-window local
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
        # After window → move to next day start-of-window and re-check (may land on weekend)
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
        country: Accepted for signature compatibility; ignored. The
            window is sender-local (Asia/Bangkok), not recipient-local.
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
    del country  # intentionally unused; kept for call-site compatibility
    rng = rng or random
    now_utc = now_utc or datetime.now(timezone.utc)

    # Earliest possible UTC time this new send can happen
    spacing = timedelta(minutes=rng.randint(_SPACING_MIN, _SPACING_MAX))
    if last_scheduled_utc is None:
        earliest_utc = now_utc
    else:
        earliest_utc = max(now_utc, last_scheduled_utc + spacing)

    # Convert to sender-local and roll into the window
    local = earliest_utc.astimezone(_SENDER_TZ)
    local_in_window = _roll_into_window(local)

    # Back to UTC for storage
    return local_in_window.astimezone(timezone.utc)
