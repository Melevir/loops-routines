from datetime import timedelta

import pytest
from conftest import utc

from loops_routines.core.triggers import Cron, Every, fmt_duration, parse_duration, parse_trigger


def test_durations():
    assert parse_duration("30m") == timedelta(minutes=30)
    assert parse_duration("1h30m") == timedelta(minutes=90)
    assert parse_duration("2d") == timedelta(days=2)
    assert parse_duration("45") == timedelta(minutes=45)
    assert fmt_duration(timedelta(minutes=90)) == "1h30m"
    for bad in ("", "soon", "3w", "m"):
        with pytest.raises(ValueError):
            parse_duration(bad)


def test_every_sits_on_multiples_of_its_interval():
    trigger = parse_trigger("every 30m")
    assert isinstance(trigger, Every)
    assert trigger.next_after(utc("2026-11-09T10:07:00")) == utc("2026-11-09T10:30:00")
    assert trigger.next_after(utc("2026-11-09T10:30:00")) == utc("2026-11-09T11:00:00")  # strictly after
    assert parse_trigger("every 2h").next_after(utc("2026-11-09T01:00:00")) == utc("2026-11-09T02:00:00")


def test_every_too_short():
    with pytest.raises(ValueError, match="shortest"):
        parse_trigger("every 30s")


def test_cron_weekly_monday_nine():
    trigger = parse_trigger("0 9 * * mon", "UTC")
    assert isinstance(trigger, Cron)
    # 2026-11-09 is a Monday
    assert trigger.next_after(utc("2026-11-08T12:00:00")) == utc("2026-11-09T09:00:00")
    assert trigger.next_after(utc("2026-11-09T09:00:00")) == utc("2026-11-16T09:00:00")


def test_cron_steps_ranges_lists_and_aliases():
    assert parse_trigger("*/15 * * * *", "UTC").next_after(utc("2026-11-09T10:16:00")) == utc("2026-11-09T10:30:00")
    trigger = parse_trigger("0 8-10,18 * * mon-fri", "UTC")
    assert trigger.next_after(utc("2026-11-09T10:30:00")) == utc("2026-11-09T18:00:00")
    assert trigger.next_after(utc("2026-11-13T18:00:00")) == utc("2026-11-16T08:00:00")  # Fri -> Mon
    assert parse_trigger("@daily", "UTC").next_after(utc("2026-11-09T10:00:00")) == utc("2026-11-10T00:00:00")
    assert parse_trigger("0 0 * * 7", "UTC").next_after(utc("2026-11-09T00:00:00")) == utc("2026-11-15T00:00:00")


def test_cron_day_of_month_or_weekday():
    # both restricted: either matches (crontab(5)): the 1st, or any Sunday
    trigger = parse_trigger("0 0 1 * sun", "UTC")
    assert trigger.next_after(utc("2026-11-09T00:00:00")) == utc("2026-11-15T00:00:00")
    assert trigger.next_after(utc("2026-11-29T00:00:00")) == utc("2026-12-01T00:00:00")


def test_cron_timezone():
    trigger = parse_trigger("0 9 * * *", "Europe/London")
    # 09:00 London in summer (BST) is 08:00 UTC
    assert trigger.next_after(utc("2026-07-01T00:00:00")) == utc("2026-07-01T08:00:00")
    assert trigger.next_after(utc("2026-12-01T00:00:00")) == utc("2026-12-01T09:00:00")


@pytest.mark.parametrize("bad", ["* * * *", "60 * * * *", "0 25 * * *", "0 0 * * funday", "0 0 */0 * *", "nonsense"])
def test_cron_errors_are_readable(bad):
    with pytest.raises(ValueError, match="cron"):
        parse_trigger(bad)


def test_feb_29():
    assert parse_trigger("0 0 29 2 *", "UTC").next_after(utc("2026-11-09T00:00:00")) == utc("2028-02-29T00:00:00")
