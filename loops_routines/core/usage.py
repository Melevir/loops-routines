"""The plan's usage: snapshots of the 5-hour and 7-day windows, and the limit gates built on them.

A snapshot, one per Claude account (CLAUDE_CONFIG_DIR, "" for ~/.claude):
    {"captured_at": ISO UTC, "source": agent|probe|statusline,
     "five_hour": {"used_percentage": 0-100, "resets_at": unix seconds} | None, "seven_day": ... | None}

Claude Code reports the limits in `rate_limit_event` lines of `claude -p --output-format stream-json`
(seen in practice, not documented): `rate_limit_info.unifiedWindows.<window> = {utilization: 0-1,
resetsAt}`. Every run feeds them; when the numbers are too old to decide on, the daemon spends one
tiny haiku request on a probe (core.probe). (Taken from Loops' usage.py.)
"""

from dataclasses import dataclass
from datetime import datetime

from loops_routines.core.config import Limits

WINDOWS = ("five_hour", "seven_day")


@dataclass(frozen=True)
class Gate:
    ok: bool
    reason: str
    needs_probe: bool = False  # it could pass with fresh numbers


def snapshot_from_rate_limit_event(event, now: str, source: str) -> dict | None:
    """Limits from a stream-json `rate_limit_event`; None for anything else or without windows."""
    if not isinstance(event, dict) or event.get("type") != "rate_limit_event":
        return None
    info = event.get("rate_limit_info")
    unified = info.get("unifiedWindows") if isinstance(info, dict) else None
    if not isinstance(unified, dict):
        return None
    windows = {}
    for name in WINDOWS:
        window = unified.get(name)
        if isinstance(window, dict) and isinstance(window.get("utilization"), (int, float)) \
                and isinstance(window.get("resetsAt"), (int, float)):
            windows[name] = {"used_percentage": round(float(window["utilization"]) * 100, 1),
                             "resets_at": int(window["resetsAt"])}
    if not windows:
        return None
    return {"captured_at": now, "source": source, **{name: windows.get(name) for name in WINDOWS}}


def snapshot_from_statusline(payload: dict, now: str) -> dict | None:
    """The limits in a Claude Code status line payload (`rate_limits`, documented); None without them."""
    limits = payload.get("rate_limits") or {}
    windows = {}
    for name in WINDOWS:
        window = limits.get(name)
        if isinstance(window, dict) and "used_percentage" in window and "resets_at" in window:
            windows[name] = {"used_percentage": float(window["used_percentage"]), "resets_at": int(window["resets_at"])}
    if not windows:
        return None
    return {"captured_at": now, "source": "statusline", **{name: windows.get(name) for name in WINDOWS}}


def age_minutes(snapshot: dict | None, now: datetime) -> float | None:
    if not snapshot:
        return None
    return (now - datetime.fromisoformat(snapshot["captured_at"])).total_seconds() / 60


def fmt_minutes(minutes: float) -> str:
    minutes = max(0, round(minutes))
    if minutes >= 48 * 60:
        return f"{minutes // 1440}d{minutes % 1440 // 60}h"
    return f"{minutes // 60}h{minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def free(snapshot: dict | None, window: str, now: datetime) -> float | None:
    """% of a window still free; a window that has reset since the snapshot is all free; None unknown."""
    w = (snapshot or {}).get(window)
    if not w:
        return None
    return 100.0 if w["resets_at"] <= now.timestamp() else round(100.0 - w["used_percentage"], 1)


def gate(limits: Limits, snapshot: dict | None, now: datetime, max_age_minutes: float) -> Gate:
    """May a run with these limits start now on this account's numbers?"""
    if not limits.needs_usage:
        return Gate(True, "no usage limits")
    age = age_minutes(snapshot, now)
    if age is None:
        return Gate(False, "no usage numbers for this Claude account yet", needs_probe=True)
    if age > max_age_minutes:
        return Gate(False, f"usage numbers are {fmt_minutes(age)} old", needs_probe=True)
    five = snapshot.get("five_hour")
    if not five:
        return Gate(False, "Claude Code did not report the 5-hour window", needs_probe=True)
    minutes_left = (five["resets_at"] - now.timestamp()) / 60
    if minutes_left <= 0:
        return Gate(False, "the 5-hour window has reset: waiting for fresh numbers", needs_probe=True)
    weekly = free(snapshot, "seven_day", now)
    if weekly is not None and weekly <= limits.keep_free_weekly:
        return Gate(False, f"7-day window {weekly:.0f}% free, keeping {limits.keep_free_weekly:.0f}%")
    five_free = free(snapshot, "five_hour", now)
    if five_free <= limits.keep_free:
        return Gate(False, f"5-hour window {five_free:.0f}% free, keeping {limits.keep_free:.0f}%")
    if five_free < limits.min_free:
        return Gate(False, f"5-hour window {five_free:.0f}% free, starts from {limits.min_free:.0f}%")
    if limits.before_reset and minutes_left > limits.before_reset:
        return Gate(False, f"5-hour window resets in {fmt_minutes(minutes_left)}, "
                           f"starts only within {fmt_minutes(limits.before_reset)} of it")
    return Gate(True, f"5-hour window {five_free:.0f}% free")


def wind_down_reason(limits: Limits, snapshot: dict | None, now: datetime) -> str | None:
    """Why a running run must finish now (it reached what its routine keeps free), or None."""
    weekly = free(snapshot, "seven_day", now)
    if weekly is not None and limits.keep_free_weekly and weekly <= limits.keep_free_weekly:
        return f"the 7-day window is down to {weekly:.0f}% free (keep_free_weekly {limits.keep_free_weekly:.0f}%)"
    five = free(snapshot, "five_hour", now)
    if five is not None and limits.keep_free and five <= limits.keep_free:
        return f"the 5-hour window is down to {five:.0f}% free (keep_free {limits.keep_free:.0f}%)"
    return None
