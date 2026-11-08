"""Is a routine due, and may it start? A pure function of the routine, its state, the time and the usage.

Occurrences come from the trigger. The daemon remembers the last one it handled (`last_fire_at`: the
run started, or the occurrence expired); before the first one, the moment it first saw the routine.

    no occurrence since then             idle until the next one
    occurrences since then               only the latest counts (a week asleep is one catch-up, not 336)
      ... past its deadline              expire it (deadline = occurrence + grace; without grace the
                                         next occurrence, i.e. it waits as long as it can)
      ... routine disabled / running     wait
      ... outside its window             wait
      ... limits don't allow it          wait (with needs_probe when fresh numbers could change that)
      ... otherwise                      launch

A manual run ("Run now") is not an occurrence and leaves this alone.
"""

from dataclasses import dataclass
from datetime import datetime

from claude_tab.core import usage
from claude_tab.core.clock import from_iso
from claude_tab.core.routines import Routine
from claude_tab.core.windows import window_open, window_opens

MAX_STEPS = 100_000  # occurrences walked to find the latest one (every 1m for 69 days)


@dataclass(frozen=True)
class Verdict:
    action: str  # idle | wait | launch | expire | disabled
    reason: str
    occurrence: datetime | None = None
    deadline: datetime | None = None
    next_at: datetime | None = None  # idle: the next occurrence
    needs_probe: bool = False


def is_enabled(routine: Routine, state: dict) -> bool:
    override = state.get("enabled")
    return routine.enabled if override is None else bool(override)


def latest_occurrence(routine: Routine, since: datetime, now: datetime) -> datetime | None:
    """The last occurrence in (since, now], or None."""
    occurrence = routine.trigger.next_after(since)
    if occurrence > now:
        return None
    for _ in range(MAX_STEPS):
        following = routine.trigger.next_after(occurrence)
        if following > now:
            return occurrence
        occurrence = following
    return occurrence


def evaluate(routine: Routine, state: dict, now: datetime, snapshot: dict | None, *, running: bool,
             timezone: str = "", max_snapshot_age_minutes: float = 30) -> Verdict:
    if not is_enabled(routine, state):
        return Verdict("disabled", "disabled")
    since = from_iso(state.get("last_fire_at")) or from_iso(state.get("first_seen_at")) or now
    occurrence = latest_occurrence(routine, since, now)
    if occurrence is None:
        next_at = routine.trigger.next_after(max(since, now))
        return Verdict("idle", "not due yet", next_at=next_at)
    deadline = occurrence + routine.grace if routine.grace else routine.trigger.next_after(occurrence)
    if now >= deadline:
        return Verdict("expire", "its time passed", occurrence, deadline)
    if running:
        return Verdict("wait", "the previous run is still going", occurrence, deadline)
    if not window_open(routine.window, timezone, now):
        opens = window_opens(routine.window, timezone, now)
        if opens is not None and opens >= deadline:
            return Verdict("wait", f"outside its window ({routine.window}); it opens after this one expires",
                           occurrence, deadline)
        return Verdict("wait", f"outside its window ({routine.window})", occurrence, deadline)
    verdict = usage.gate(routine.limits, snapshot, now, max_snapshot_age_minutes)
    if not verdict.ok:
        return Verdict("wait", verdict.reason, occurrence, deadline, needs_probe=verdict.needs_probe)
    return Verdict("launch", verdict.reason, occurrence, deadline)
