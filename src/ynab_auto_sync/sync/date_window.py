from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

from workalendar.europe import Norway

MatchWindowUnit = Literal["calendar_days", "working_days"]

# Stateless and safe to reuse across every call - no I/O, no per-instance
# state (confirmed by reading workalendar's own source: get_working_days_delta
# takes its calendar's holiday table as a pure function of the year involved).
_NORWAY_CALENDAR = Norway()


def days_between(d1: date, d2: date, unit: MatchWindowUnit) -> int:
    """Non-negative day distance between two dates, in the given unit.

    "working_days" excludes weekends and Norwegian public holidays (via
    workalendar.europe.Norway) - a closer match to how long a bank
    transaction can actually take to settle (see transfers.py's and
    engine.py's own docs on why a calendar-day window is a blunt
    approximation of that) than raw calendar days. Confirmed live against
    workalendar 17.0.0: get_working_days_delta() is already
    order-independent (swaps start/end internally) and a Friday->Monday
    span correctly counts as 1 working day, not 3.
    """
    if unit == "calendar_days":
        return abs((d2 - d1).days)
    return _NORWAY_CALENDAR.get_working_days_delta(d1, d2)


def since_date_bound(ref: date, lookback_days: int) -> date:
    """Earliest date worth fetching for a since_date-bounded YNAB GET,
    anchored to a REAL reference date - never wall-clock "now". A tracked
    row can sit for a long time (e.g. a still-PENDING transaction awaiting
    its eventual booked correction) well past `lookback_days` from today,
    so every caller anchors to its own known transaction/tracked-row date
    instead of "now minus lookback".
    """
    return ref - timedelta(days=lookback_days)
