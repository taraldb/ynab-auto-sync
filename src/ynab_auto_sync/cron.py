from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from croniter import croniter


def next_fire_at(cron_expression: str, timezone: str) -> datetime:
    """The next time cron_expression fires, evaluated in timezone (an IANA
    name, e.g. "Europe/Oslo") rather than UTC - the cron's hour digits are
    meant as local wall-clock time, not UTC. Single source of truth shared
    by scheduler.py and webapp/routes/status.py so the two can never drift
    (see CLAUDE.md's "Resolved: cron ran in UTC, not local time").

    Returns a timezone-aware datetime whose tzinfo is the given zone.
    """
    now = datetime.now(ZoneInfo(timezone))
    return croniter(cron_expression, now).get_next(datetime)


def previous_fire_at(cron_expression: str, timezone: str) -> datetime:
    """The most recent time cron_expression fired at or before now - the
    mirror image of next_fire_at() above, using croniter's get_prev()
    instead of get_next(). Used by scheduler.py at startup to detect
    whether a scheduled fire was missed while the process wasn't running
    (see "don't sync at startup unless a cron trigger was missed" in
    CLAUDE.md).

    Returns a timezone-aware datetime whose tzinfo is the given zone.
    """
    now = datetime.now(ZoneInfo(timezone))
    return croniter(cron_expression, now).get_prev(datetime)
