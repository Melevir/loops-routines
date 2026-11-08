from conftest import cfg, routine_text, snapshot, utc

from claude_tab.core.evaluate import evaluate
from claude_tab.core.routines import parse_routine
from claude_tab.core.usage import gate, wind_down_reason

FRESH = None  # filled per test with snapshot(...)


def routine(header: str):
    return parse_routine("r", routine_text(header), cfg())


def test_idle_until_first_occurrence():
    r = routine('schedule = "every 30m"\nprofile = "unlimited"')
    state = {"first_seen_at": "2026-11-09T10:05:00+00:00"}
    v = evaluate(r, state, utc("2026-11-09T10:20:00"), None, running=False)
    assert v.action == "idle" and v.next_at == utc("2026-11-09T10:30:00")


def test_launch_when_due_and_no_limits():
    r = routine('schedule = "every 30m"\nprofile = "unlimited"')
    state = {"first_seen_at": "2026-11-09T10:05:00+00:00"}
    v = evaluate(r, state, utc("2026-11-09T10:31:00"), None, running=False)
    assert v.action == "launch" and v.occurrence == utc("2026-11-09T10:30:00")
    assert v.deadline == utc("2026-11-09T11:00:00")  # without grace: until the next occurrence


def test_only_the_latest_occurrence_counts_after_a_sleep():
    r = routine('schedule = "every 30m"\nprofile = "unlimited"')
    state = {"last_fire_at": "2026-11-09T10:30:00+00:00"}
    v = evaluate(r, state, utc("2026-11-10T08:10:00"), None, running=False)
    assert v.action == "launch" and v.occurrence == utc("2026-11-10T08:00:00")


def test_grace_expires_an_occurrence():
    r = routine('schedule = "0 9 * * *"\ngrace = "1h"\nprofile = "unlimited"')
    state = {"last_fire_at": "2026-11-08T09:00:00+00:00"}
    assert evaluate(r, state, utc("2026-11-09T09:30:00"), None, running=False).action == "launch"
    v = evaluate(r, state, utc("2026-11-09T10:30:00"), None, running=False)
    assert v.action == "expire" and v.occurrence == utc("2026-11-09T09:00:00")


def test_waits_while_running_and_outside_window():
    r = routine('schedule = "every 30m"\nprofile = "unlimited"\nwindow = "mon-fri 09:00-18:00"')
    state = {"last_fire_at": "2026-11-09T08:00:00+00:00"}
    assert evaluate(r, state, utc("2026-11-09T10:05:00"), None, running=True).action == "wait"
    v = evaluate(r, state, utc("2026-11-09T08:35:00"), None, running=False, timezone="UTC")
    assert v.action == "wait" and "window" in v.reason
    v = evaluate(r, state, utc("2026-11-14T10:05:00"), None, running=False, timezone="UTC")  # Saturday
    assert v.action == "wait"


def test_limits_gate_and_probe_request():
    now = utc("2026-11-09T10:05:00")
    r = routine('schedule = "every 30m"\nprofile = "spare"')  # min_free 60, before_reset 120, keep_free 20
    state = {"last_fire_at": "2026-11-09T09:30:00+00:00"}
    v = evaluate(r, state, now, None, running=False)
    assert v.action == "wait" and v.needs_probe
    v = evaluate(r, state, now, snapshot(20, 200, now=now), running=False)
    assert v.action == "wait" and "resets in" in v.reason and not v.needs_probe
    v = evaluate(r, state, now, snapshot(70, 60, now=now), running=False)
    assert v.action == "wait" and "starts from 60%" in v.reason
    assert evaluate(r, state, now, snapshot(20, 60, now=now), running=False).action == "launch"
    v = evaluate(r, state, now, snapshot(20, 60, now=now, age_minutes=45), running=False)
    assert v.action == "wait" and v.needs_probe


def test_weekly_reserve():
    now = utc("2026-11-09T10:00:00")
    r = routine('schedule = "@daily"\nkeep_free_weekly = 15')
    assert not gate(r.limits, snapshot(10, 60, week_used=90, now=now), now, 30).ok


def test_disabled_and_override():
    r = routine('schedule = "every 30m"\nenabled = false')
    assert evaluate(r, {}, utc("2026-11-09T10:05:00"), None, running=False).action == "disabled"
    state = {"enabled": 1, "last_fire_at": "2026-11-09T09:30:00+00:00"}
    r2 = routine('schedule = "every 30m"\nenabled = false\nprofile = "unlimited"')
    assert evaluate(r2, state, utc("2026-11-09T10:05:00"), None, running=False).action == "launch"


def test_wind_down():
    now = utc("2026-11-09T10:00:00")
    r = routine('schedule = "@daily"\nkeep_free = 20')
    assert wind_down_reason(r.limits, snapshot(70, 60, now=now), now) is None
    assert "5-hour" in wind_down_reason(r.limits, snapshot(85, 60, now=now), now)
