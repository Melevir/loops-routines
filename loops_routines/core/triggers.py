"""When a routine is due: a crontab line or a fixed interval.

    "*/30 * * * *"      cron: minute hour day-of-month month day-of-week (sun = 0 or 7, names allowed)
    "0 9 * * mon"       every Monday at 09:00 in the daemon's timezone
    "@daily"            @hourly, @daily, @weekly, @monthly, @yearly
    "every 30m"         a fixed interval: 45s is too short, 30m, 2h, 1d, 1h30m; occurrences sit on
                        multiples of the interval since the Unix epoch, so they don't drift with
                        restarts (every 2h fires at 00:00, 02:00, ... UTC). Use cron for wall-clock times.

Every trigger answers one question: next_after(moment), the first occurrence strictly after it.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from loops_routines.core.windows import zone

DURATION_RE = re.compile(r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")
ALIASES = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *", "@weekly": "0 0 * * 0",
           "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *"}
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
CRON_DAYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
MIN_INTERVAL = timedelta(minutes=1)
SEARCH_DAYS = 366 * 5  # Feb 29 is the rarest day a cron line can ask for


def parse_duration(text: str) -> timedelta:
    """'30m', '2h', '1d', '1h30m', '90s' -> timedelta; a bare number is minutes. ValueError otherwise."""
    raw = str(text).strip().lower().replace(" ", "")
    if raw.isdigit():
        return timedelta(minutes=int(raw))
    match = DURATION_RE.match(raw)
    if not raw or not match or not any(match.groups()):
        raise ValueError(f"duration {text!r}: use e.g. 30m, 2h, 1d or 1h30m")
    d, h, m, s = (int(g or 0) for g in match.groups())
    return timedelta(days=d, hours=h, minutes=m, seconds=s)


def fmt_duration(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    parts = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= size:
            parts.append(f"{seconds // size}{unit}")
            seconds %= size
    return "".join(parts) or "0m"


@dataclass(frozen=True)
class Every:
    interval: timedelta
    text: str

    def next_after(self, moment: datetime) -> datetime:
        step = self.interval.total_seconds()
        ts = moment.timestamp()
        return datetime.fromtimestamp((ts // step + 1) * step, UTC)

    def describe(self) -> str:
        return f"every {fmt_duration(self.interval)}"


def _field(text: str, low: int, high: int, names: list[str] | None = None, name_base: int = 0) -> frozenset[int]:
    """One cron field -> the set of values it allows. Names (mon, jan) count from name_base."""
    values: set[int] = set()

    def number(token: str) -> int:
        token = token.strip().lower()
        if names and token in names:
            return names.index(token) + name_base
        if not token.isdigit():
            raise ValueError(f"{token!r} is not a number{' or a name' if names else ''}")
        return int(token)

    for part in text.split(","):
        part, _, step_text = part.partition("/")
        step = int(step_text) if step_text else 1
        if step_text and (not step_text.isdigit() or step < 1):
            raise ValueError(f"step {step_text!r} must be a positive number")
        if part in ("*", ""):
            start, end = low, high
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = number(a), number(b)
        else:
            start = number(part)
            end = high if step_text else start
        if not (low <= start <= high and low <= end <= high) or start > end:
            raise ValueError(f"{part!r} is outside {low}-{high}")
        values.update(range(start, end + 1, step))
    return frozenset(values)


@dataclass(frozen=True)
class Cron:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]  # day of month
    months: frozenset[int]
    weekdays: frozenset[int]  # Python weekdays: mon = 0
    days_any: bool  # the day-of-month field is "*"
    weekdays_any: bool
    text: str
    tz: str = ""

    @classmethod
    def parse(cls, text: str, tz: str = "") -> "Cron":
        line = ALIASES.get(text.strip().lower(), text.strip())
        fields = line.split()
        if len(fields) != 5:
            raise ValueError(f"cron {text!r}: needs 5 fields (minute hour day month weekday), "
                             "e.g. '0 9 * * mon', or @daily, or 'every 30m'")
        names = ("minute", "hour", "day of month", "month", "weekday")
        try:
            minute = _field(fields[0], 0, 59)
            hour = _field(fields[1], 0, 23)
            day = _field(fields[2], 1, 31)
            month = _field(fields[3], 1, 12, MONTHS, 1)
            cron_days = _field(fields[4], 0, 7, CRON_DAYS, 0)
        except ValueError as exc:
            field_index = next(i for i, f in enumerate(fields) if _bad(f, i))
            raise ValueError(f"cron {text!r}: {names[field_index]}: {exc}") from None
        weekdays = frozenset((d - 1) % 7 for d in cron_days)  # cron sun = 0 = 7 -> Python 6
        return cls(minute, hour, day, month, weekdays, fields[2] == "*", fields[4] == "*", text.strip(), tz)

    def _day_matches(self, day: datetime) -> bool:
        if day.month not in self.months:
            return False
        dom, dow = day.day in self.days, day.weekday() in self.weekdays
        if self.days_any or self.weekdays_any:  # one restricted: it alone decides
            return dom and dow
        return dom or dow  # both restricted: either one (crontab(5))

    def next_after(self, moment: datetime) -> datetime:
        tz = zone(self.tz)
        local = moment.astimezone(tz)
        first_day = local.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
        for ahead in range(SEARCH_DAYS):
            day = first_day + timedelta(days=ahead)
            if not self._day_matches(day):
                continue
            for hour in sorted(self.hours):
                for minute in sorted(self.minutes):
                    naive = day.replace(hour=hour, minute=minute)
                    at = naive.replace(tzinfo=tz) if isinstance(tz, ZoneInfo) else naive.astimezone()
                    if at > moment:
                        return at.astimezone(UTC)
        raise ValueError(f"cron {self.text!r} never fires")

    def describe(self) -> str:
        return self.text


def _bad(field: str, index: int) -> bool:
    bounds = [(0, 59, None, 0), (0, 23, None, 0), (1, 31, None, 0), (1, 12, MONTHS, 1), (0, 7, CRON_DAYS, 0)][index]
    try:
        _field(field, *bounds)
    except ValueError:
        return True
    return False


Trigger = Every | Cron


def parse_trigger(text: str, tz: str = "") -> Trigger:
    """A routine's `schedule` -> a trigger. ValueError with a readable message."""
    raw = " ".join(str(text or "").split())
    if not raw:
        raise ValueError("schedule is empty: use a cron line ('0 9 * * mon'), @daily, or 'every 30m'")
    if raw.lower().startswith("every "):
        interval = parse_duration(raw[6:])
        if interval < MIN_INTERVAL:
            raise ValueError(f"schedule {raw!r}: the shortest interval is {fmt_duration(MIN_INTERVAL)}")
        return Every(interval, raw)
    return Cron.parse(raw, tz)
