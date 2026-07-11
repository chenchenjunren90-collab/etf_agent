"""A-share trading calendar helpers (weekends + SSE/SZSE published holidays).

Used by run-gates, price freshness targets, and news/econ lookbacks so
weekday public holidays are not treated as trading sessions.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Iterable

import pandas as pd

# Weekday closed sessions from SSE/SZSE notices (weekends handled separately).
# 2025: official exchange holiday schedule; 2026: SSE notice 2025-12-22.
_A_SHARE_CLOSED_RANGES: tuple[tuple[str, str], ...] = (
    # 2025
    ("2025-01-01", "2025-01-01"),
    ("2025-01-28", "2025-02-04"),
    ("2025-04-04", "2025-04-06"),
    ("2025-05-01", "2025-05-05"),
    ("2025-05-31", "2025-06-02"),
    ("2025-10-01", "2025-10-08"),
    # 2026 (SSE 2025-12-22)
    ("2026-01-01", "2026-01-03"),
    ("2026-02-15", "2026-02-23"),
    ("2026-04-04", "2026-04-06"),
    ("2026-05-01", "2026-05-05"),
    ("2026-06-19", "2026-06-21"),
    ("2026-09-25", "2026-09-27"),
    ("2026-10-01", "2026-10-07"),
)


def _expand_ranges(ranges: Iterable[tuple[str, str]]) -> frozenset[str]:
    out: set[str] = set()
    for start_s, end_s in ranges:
        cur = pd.to_datetime(start_s).date()
        end = pd.to_datetime(end_s).date()
        while cur <= end:
            out.add(cur.isoformat())
            cur += timedelta(days=1)
    return frozenset(out)


A_SHARE_CLOSED_DATES: frozenset[str] = _expand_ranges(_A_SHARE_CLOSED_RANGES)


def _as_date(value: str | date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return pd.to_datetime(value).date()


def is_trading_day(value: str | date | datetime) -> bool:
    """True if A-share cash market is open (not weekend / not published holiday)."""
    d = _as_date(value)
    if d.weekday() >= 5:
        return False
    if d.isoformat() in A_SHARE_CLOSED_DATES:
        return False
    # Never infer a market closure from a missing local CSV row. A data-source
    # outage or damaged reference file must remain visible as a freshness error.
    return True


def previous_trading_day(value: str | date | datetime) -> date:
    """Strict previous trading session before ``value``."""
    d = _as_date(value) - timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def latest_trading_day(as_of: str | date | datetime | None = None) -> date:
    """Nearest trading day on or before ``as_of`` (default: today)."""
    d = _as_date(as_of) if as_of is not None else datetime.now().date()
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d
