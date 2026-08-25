from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from ynab_auto_sync import cron


def test_next_fire_at_evaluates_cron_in_configured_timezone(monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            base = datetime(2026, 8, 24, 5, 0, 0, tzinfo=UTC)
            return base.astimezone(tz) if tz else base

    monkeypatch.setattr(cron, "datetime", FixedDatetime)

    # 05:00 UTC == 07:00 Europe/Oslo in August (CEST, UTC+2). Next fire in
    # "0 8,12,17 * * *" after 07:00 local is 08:00 local == 06:00 UTC.
    result = cron.next_fire_at("0 8,12,17 * * *", "Europe/Oslo")

    assert result.tzinfo == ZoneInfo("Europe/Oslo")
    assert (result.hour, result.minute) == (8, 0)
    assert result.astimezone(UTC).hour == 6


def test_next_fire_at_invalid_timezone_raises():
    with pytest.raises(ZoneInfoNotFoundError):
        cron.next_fire_at("0 8,12,17 * * *", "Not/A_Real_Zone")


def test_previous_fire_at_evaluates_cron_in_configured_timezone(monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            base = datetime(2026, 8, 24, 5, 0, 0, tzinfo=UTC)
            return base.astimezone(tz) if tz else base

    monkeypatch.setattr(cron, "datetime", FixedDatetime)

    # 05:00 UTC == 07:00 Europe/Oslo in August (CEST, UTC+2). Previous fire
    # in "0 8,12,17 * * *" before 07:00 local is the prior day's 17:00
    # local == 15:00 UTC.
    result = cron.previous_fire_at("0 8,12,17 * * *", "Europe/Oslo")

    assert result.tzinfo == ZoneInfo("Europe/Oslo")
    assert (result.hour, result.minute) == (17, 0)
    assert result.astimezone(UTC).hour == 15
    assert result.date().day == 23
