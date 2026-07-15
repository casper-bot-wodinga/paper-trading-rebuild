"""Market calendar — knows when US equities are open.

Provides market-hours awareness for traders so they can switch
between live trading mode and historical simulation mode.

Usage:
    from src.market_calendar import is_market_open, next_market_open, market_open_close

    if is_market_open():
        # live trading mode
    else:
        # historical simulation mode
        next_open = next_market_open()
"""

from __future__ import annotations

import datetime
from typing import Optional, Tuple

# ── US Market Holidays 2026 ──────────────────────────────────────────────
# Source: NYSE holiday calendar (approximate — month/day)
_HOLIDAYS_2026: set[tuple[int, int]] = {
    (1, 1),   # New Year's Day
    (1, 19),  # MLK Jr. Day (3rd Mon Jan)
    (2, 16),  # Presidents' Day (3rd Mon Feb)
    (4, 3),   # Good Friday
    (5, 25),  # Memorial Day (last Mon May)
    (6, 19),  # Juneteenth
    (7, 3),   # Independence Day (observed Fri)
    (9, 7),   # Labor Day (1st Mon Sep)
    (11, 26), # Thanksgiving (4th Thu Nov)
    (12, 25), # Christmas
}
"""NYSE holidays as (month, day) tuples for 2026."""


def _is_holiday(d: datetime.date) -> bool:
    """Check if date is a known market holiday."""
    return (d.month, d.day) in _HOLIDAYS_2026


def is_market_open(dt: Optional[datetime.datetime] = None) -> bool:
    """Return True if US equities are currently trading.

    Market hours: Mon-Fri, 9:30-16:00 ET.
    Early close days (day before holiday, day after Thanksgiving) close at 13:00 ET,
    but for simplicity we use standard hours here.
    """
    if dt is None:
        dt = datetime.datetime.now(datetime.timezone.utc)

    # Convert to ET (UTC-4 during EDT, UTC-5 during EST)
    # Approximate: Mar-Nov is EDT (UTC-4), Nov-Mar is EST (UTC-5)
    month = dt.month
    et_offset = 4 if 3 <= month <= 11 else 5  # approximate DST
    et = dt - datetime.timedelta(hours=et_offset)

    # Weekend check
    if et.weekday() >= 5:  # Saturday=5, Sunday=6
        return False

    # Holiday check
    if _is_holiday(et.date()):
        return False

    # Market hours: 9:30 AM - 4:00 PM ET
    minutes_since_midnight = et.hour * 60 + et.minute
    market_open = 9 * 60 + 30   # 9:30 AM
    market_close = 16 * 60       # 4:00 PM

    return market_open <= minutes_since_midnight < market_close


def next_market_open(dt: Optional[datetime.datetime] = None) -> datetime.datetime:
    """Return the next datetime when market opens.

    If currently during market hours, returns current time.
    """
    if dt is None:
        dt = datetime.datetime.now(datetime.timezone.utc)

    if is_market_open(dt):
        return dt

    # Convert to ET
    month = dt.month
    et_offset = 4 if 3 <= month <= 11 else 5
    et = dt - datetime.timedelta(hours=et_offset)

    # Move forward minute by minute until market opens
    probe = et
    for _ in range(60 * 24 * 14):  # look ahead up to 14 days
        probe += datetime.timedelta(minutes=1)
        if probe.weekday() < 5 and not _is_holiday(probe.date()):
            minutes = probe.hour * 60 + probe.minute
            if 9 * 60 + 30 <= minutes < 16 * 60:
                return probe + datetime.timedelta(hours=et_offset)

    return probe + datetime.timedelta(hours=et_offset)  # fallback


def market_open_close(dt: Optional[datetime.date] = None) -> Tuple[Optional[datetime.datetime], Optional[datetime.datetime]]:
    """Return (open_time, close_time) in UTC for a given date.

    Returns (None, None) if the market is closed that day.
    """
    if dt is None:
        dt = datetime.datetime.now(datetime.timezone.utc).date()

    if dt.weekday() >= 5 or _is_holiday(dt):
        return None, None

    # Determine UTC offset for this date
    month = dt.month
    et_offset = 4 if 3 <= month <= 11 else 5

    open_et = datetime.datetime(dt.year, dt.month, dt.day, 9, 30, 0)
    close_et = datetime.datetime(dt.year, dt.month, dt.day, 16, 0, 0)

    open_utc = open_et + datetime.timedelta(hours=et_offset)
    close_utc = close_et + datetime.timedelta(hours=et_offset)

    return open_utc, close_utc


def market_mode(dt: Optional[datetime.datetime] = None) -> str:
    """Return 'LIVE', 'HISTORICAL', or 'CLOSED'."""
    if dt is None:
        dt = datetime.datetime.now(datetime.timezone.utc)

    if is_market_open(dt):
        return "LIVE"

    # Check if it's a trading day at all
    month = dt.month
    et_offset = 4 if 3 <= month <= 11 else 5
    et = dt - datetime.timedelta(hours=et_offset)

    if et.weekday() < 5 and not _is_holiday(et.date()):
        return "HISTORICAL"  # Trading day but outside hours — run replay
    return "CLOSED"  # Weekend or holiday