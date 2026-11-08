"""Time windows: "mon-fri 09:00-19:00", several joined by ";", "*" = any time.

Hours may cross midnight ("19:00-09:00"): the part after midnight belongs to the day the slot
started on, so "fri 19:00-09:00" covers Friday night into Saturday. (Taken from Loops' schedule.py.)
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
HOURS_RE = re.compile(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$")


@dataclass(frozen=True)
class Slot:
    days: str = "*"  # "mon-fri", "sat,sun", "mon,wed-fri", "*"
    hours: str = "*"  # "09:00-19:00", "19:00-09:00" (over midnight), "*"


def parse_days(text: str) -> set[int]:
    """'mon-fri,sun' -> {0..4, 6}; ValueError with a readable message."""
    text = text.strip().lower()
    if text in ("*", ""):
        return set(range(7))
    days = set()
    for part in text.split(","):
        start, _, end = part.strip().partition("-")
        if start not in DAYS or (end and end not in DAYS):
            raise ValueError(f"days {text!r}: use mon..sun, ranges like mon-fri, lists like sat,sun, or *")
        a, b = DAYS.index(start), DAYS.index(end or start)
        days.update(range(a, b + 1) if a <= b else [*range(a, 7), *range(0, b + 1)])  # fri-mon wraps
    return days


def parse_hours(text: str) -> tuple[int, int]:
    """'09:00-19:00' -> minutes (540, 1140); '*' -> (0, 1440). End may be 24:00."""
    text = text.strip()
    if text in ("*", ""):
        return 0, 1440
    match = HOURS_RE.match(text)
    if not match:
        raise ValueError(f"hours {text!r}: use HH:MM-HH:MM, e.g. 09:00-19:00 or 19:00-09:00, or *")
    h1, m1, h2, m2 = map(int, match.groups())
    start, end = h1 * 60 + m1, h2 * 60 + m2
    if not (0 <= start < 1440 and 0 < end <= 1440 and m1 < 60 and m2 < 60) or start == end:
        raise ValueError(f"hours {text!r}: times must be between 00:00 and 24:00 and differ")
    return start, end


def slot_covers(slot: Slot, weekday: int, minute: int) -> bool:
    days = parse_days(slot.days)
    start, end = parse_hours(slot.hours)
    if start < end:
        return weekday in days and start <= minute < end
    return (weekday in days and minute >= start) or ((weekday - 1) % 7 in days and minute < end)


def parse_window(text: str) -> list[Slot]:
    """"mon-fri 09:00-17:00", several joined by ";" ("mon-thu 09:00-18:00; fri 09:00-15:00"), "*" = any
    time. ValueError with a readable message."""
    text = " ".join((text or "").split())
    if text in ("", "*"):
        return [Slot("*", "*")]
    slots = []
    for part in text.split(";"):
        words = part.split()
        if not words:
            continue
        if len(words) > 2:
            raise ValueError(f"window {part.strip()!r}: use days and hours, e.g. mon-fri 09:00-17:00")
        days, hours = (words + ["*"])[:2] if not HOURS_RE.match(words[0]) else ("*", words[0])
        parse_days(days)
        parse_hours(hours)
        slots.append(Slot(days, hours))
    if not slots:
        raise ValueError("window is empty: use e.g. mon-fri 09:00-17:00, or * for any time")
    return slots


def zone(name: str) -> tzinfo | None:
    """The configured timezone, or None for the system one."""
    if not (name or "").strip():
        return None
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError):
        return None


def window_open(text: str, timezone: str, moment: datetime) -> bool:
    """Whether `moment` falls in the window (parse_window), in the timezone ("" = the system's)."""
    local = moment.astimezone(zone(timezone))
    weekday, minute = local.weekday(), local.hour * 60 + local.minute
    return any(slot_covers(slot, weekday, minute) for slot in parse_window(text))


def window_opens(text: str, timezone: str, moment: datetime) -> datetime | None:
    """The next moment after `moment` the window opens (the start of one of its slots), within a week."""
    local = moment.astimezone(zone(timezone))
    starts = []
    for slot in parse_window(text):
        start, _ = parse_hours(slot.hours)
        days = parse_days(slot.days)
        for ahead in range(8):
            day = (local + timedelta(days=ahead)).replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)
            if day > local and day.weekday() in days:
                starts.append(day)
                break
    return min(starts) if starts else None
