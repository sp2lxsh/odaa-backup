from datetime import datetime, timedelta

import pytest

from odaa_backup.schedule import ScheduleError, describe, is_due, parse_schedule


def test_parse():
    assert parse_schedule("manual").kind == "manual"
    assert parse_schedule("every 6h").hours == 6
    s = parse_schedule("daily 3:05")
    assert (s.kind, s.hour, s.minute) == ("daily", 3, 5)
    s = parse_schedule("Weekly SUN 04:00")
    assert (s.kind, s.weekday, s.hour) == ("weekly", 6, 4)
    for bad in ("daily 25:00", "every 0h", "weekly xyz 03:00", "sometimes"):
        with pytest.raises(ScheduleError):
            parse_schedule(bad)


def test_describe_ru():
    assert describe("daily 03:00") == "ежедневно в 03:00"
    assert describe("every 1h") == "каждый час"
    assert "воскресенье" in describe("weekly sun 03:00")


NOW = datetime(2026, 11, 12, 10, 7)  # четверг


def test_daily_due():
    assert is_due("daily 03:00", NOW, None)
    assert not is_due("daily 03:00", NOW, NOW.replace(hour=3, minute=10))
    assert is_due("daily 03:00", NOW, NOW - timedelta(days=1))
    # компьютер был выключен в 03:00 — догоняем при включении
    assert is_due("daily 03:00", NOW, datetime(2026, 11, 10, 3, 5))
    # до 03:00 сегодняшняя копия ещё не нужна
    assert not is_due("daily 03:00", NOW.replace(hour=2), datetime(2026, 11, 11, 3, 5))


def test_interval_and_weekly_and_manual():
    assert is_due("every 6h", NOW, NOW - timedelta(hours=6))
    assert not is_due("every 6h", NOW, NOW - timedelta(hours=3))
    assert is_due("weekly mon 03:00", NOW, datetime(2026, 11, 8, 3, 0))
    assert not is_due("weekly mon 03:00", NOW, datetime(2026, 11, 9, 3, 30))
    assert not is_due("manual", NOW, None)


def test_retry_backoff_after_failure():
    last_ok = NOW - timedelta(days=2)
    assert not is_due("daily 03:00", NOW, last_ok, last_attempt=NOW - timedelta(minutes=20), retry_minutes=60)
    assert is_due("daily 03:00", NOW, last_ok, last_attempt=NOW - timedelta(minutes=70), retry_minutes=60)
