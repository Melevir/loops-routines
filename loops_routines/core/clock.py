"""Times are stored as fixed-width ISO UTC text: text order is time order."""

from datetime import UTC, datetime


def to_iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def from_iso(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text) if text else None


def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def utc_now() -> str:
    return to_iso(now())
