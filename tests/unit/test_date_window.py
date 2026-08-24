from datetime import date

from ynab_auto_sync.sync.date_window import days_between


def test_calendar_days_matches_raw_abs_delta():
    d1 = date(2026, 8, 21)
    d2 = date(2026, 8, 24)
    assert days_between(d1, d2, "calendar_days") == 3
    assert days_between(d2, d1, "calendar_days") == 3


def test_working_days_excludes_a_weekend():
    friday = date(2026, 8, 21)
    monday = date(2026, 8, 24)
    assert days_between(friday, monday, "working_days") == 1
    assert days_between(monday, friday, "working_days") == 1


def test_working_days_excludes_a_norwegian_public_holiday():
    good_friday = date(2026, 4, 3)  # a Norwegian public holiday, itself a Friday
    following_tuesday = date(2026, 4, 7)  # Easter Monday (Apr 6) is also a holiday
    assert days_between(good_friday, following_tuesday, "calendar_days") == 4
    assert days_between(good_friday, following_tuesday, "working_days") == 1
