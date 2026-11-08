"""The daemon's one service layer: the scheduler tick, runs, and every query and action the API offers.

REST, MCP and the CLI are thin transports over this class; tests call it directly. Everything runs on
the daemon's event loop (the store is the only thing other threads might touch, and it locks).
"""

import asyncio
import os
import secrets
import signal
import subprocess
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from loops_routines import __version__
from loops_routines.core import clock, deliver, paths
from loops_routines.core import config as config_mod
from loops_routines.core.claude_cli import account_home, account_problem, agent_env, resolve_command
from loops_routines.core.evaluate import Verdict, evaluate, is_enabled
from loops_routines.core.integrations import inventory, scan
from loops_routines.core.notify import KINDS as NOTIFY_KINDS
from loops_routines.core.notify import MAX_QUESTION, Answer, MacNotifier, check_buttons
from loops_routines.core.probe import probe_usage
from loops_routines.core.routines import Broken, Routine, fill, fingerprint, load_routines
from loops_routines.core.runner import Run, RunResult, RunSpec
from loops_routines.core.triggers import fmt_duration
from loops_routines.core.usage import Gate, age_minutes, free, gate, wind_down_reason
from loops_routines.core.windows import zone

MESSAGE_KINDS = ("info", "progress", "success", "warning", "error")
BLOCKER_CATEGORIES = ("mcp_not_authenticated", "mcp_unavailable", "plugin_missing", "skill_missing",
                      "permission_denied", "credentials", "tool_missing", "network", "other")
MAX_TEXT = 280
MAX_PREVIOUS_REPORT = 20_000
MAX_EXPIRES_PER_TICK = 50


class NotFound(LookupError):
    pass


class Conflict(RuntimeError):
    """The action can't happen in the current state (already running, no free slot, ...)."""


@dataclass
class Active:
    run: Run
    routine: Routine
    token: str
    task: asyncio.Task | None = None
    account: str = ""
    notified_error: bool = False  # the agent sent an error notification itself


@dataclass
class Question:
    """An agent's question waiting for a click: in the dialog, the web page, the CLI or MCP, whichever is first."""
    id: int
    run_id: int
    routine: str
    title: str
    text: str
    buttons: list[str]
    asked_at: str
    deadline: str
    future: asyncio.Future
    dialog: asyncio.Task | None = None

    def view(self) -> dict:
        return {"id": self.id, "run_id": self.run_id, "routine": self.routine, "title": self.title,
                "question": self.text, "buttons": self.buttons, "asked_at": self.asked_at, "deadline": self.deadline}


class EventBus:
    """Live events for SSE subscribers; a slow subscriber loses events, never blocks the daemon."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._seq = 0

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def publish(self, kind: str, data: dict | None = None) -> None:
        self._seq += 1
        event = {"id": self._seq, "type": kind, "at": clock.utc_now(), "data": data or {}}
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass


def snapshot_key(account: str) -> str:
    return f"usage:{account}"


def scan_key(account: str) -> str:
    return f"integrations.scan:{account}"


class Service:
    def __init__(self, store, *, load_config=config_mod.load, now=clock.now, prober=probe_usage,
                 notifier=None, run_factory=Run, scanner=None, port: int | None = None):
        self.store = store
        self._load_config = load_config
        self._now = now
        self._prober = prober
        self.notifier = notifier or MacNotifier()
        self.questions: dict[int, Question] = {}
        self._question_seq = 0
        self._run_factory = run_factory
        self._scanner = scanner
        self.started_at = clock.utc_now()
        self.cfg = load_config()
        self.port = port or self.cfg.daemon.port
        self.routines: dict[str, Routine] = {}
        self.broken: dict[str, Broken] = {}
        self._fingerprint = None
        self._cfg_mtime = None
        self.active: dict[int, Active] = {}
        self._tokens: dict[str, int] = {}
        self.verdicts: dict[str, Verdict] = {}
        self.bus = EventBus()
        self.probe_task: asyncio.Task | None = None
        self.last_probe_at = None
        self.probe_error: str | None = None
        self.scan_task: asyncio.Task | None = None
        self.scan_error: str | None = None
        self._background: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()  # one tick at a time
        self.reload(force=True)

    # --- setup ---------------------------------------------------------------------

    @property
    def agent_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/agent/mcp"

    @property
    def account(self) -> str:
        return self.cfg.agent.config_dir()

    def reload(self, force: bool = False) -> bool:
        """Re-read config.toml and routines/*.md if they changed; True when something did."""
        try:
            cfg_mtime = paths.config_path().stat().st_mtime_ns
        except OSError:
            cfg_mtime = None
        folder = paths.routines_dir()
        print_ = fingerprint(folder)
        if not force and print_ == self._fingerprint and cfg_mtime == self._cfg_mtime:
            return False
        self.cfg = self._load_config()
        self.routines, self.broken = load_routines(folder, self.cfg)
        self._fingerprint, self._cfg_mtime = print_, cfg_mtime
        now = clock.to_iso(self._now())
        for name in self.routines:
            self.store.ensure_routine(name, now)
        self.verdicts = {k: v for k, v in self.verdicts.items() if k in self.routines}
        self.bus.publish("routines.changed", {"names": sorted([*self.routines, *self.broken])})
        return True

    def recover(self) -> list[int]:
        """At start: runs a previous daemon left running are closed as lost, their processes stopped."""
        now = clock.utc_now()
        closed = []
        for row in self.store.running_rows():
            if row.get("pid"):
                _stop_orphan(row["pid"])
            self.store.close_lost(row["id"], now)
            closed.append(row["id"])
        return closed

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    # --- usage ---------------------------------------------------------------------

    def snapshot(self) -> dict | None:
        return self.store.get_json(snapshot_key(self.account))

    def save_snapshot(self, snapshot: dict, account: str | None = None) -> None:
        account = self.account if account is None else account
        self.store.put_json(snapshot_key(account), snapshot)
        if account == self.account:
            self.bus.publish("usage.updated", self.usage_view())

    def usage_view(self) -> dict:
        now = self._now()
        snap = self.snapshot()
        windows = {}
        for name in ("five_hour", "seven_day"):
            w = (snap or {}).get(name)
            windows[name] = None if not w else {
                "used_percentage": w["used_percentage"], "free_percentage": free(snap, name, now),
                "resets_at": clock.to_iso(datetime.fromtimestamp(w["resets_at"], UTC)),
            }
        age = age_minutes(snap, now)
        return {"account": self.account or "~/.claude", "captured_at": (snap or {}).get("captured_at"),
                "source": (snap or {}).get("source"), "age_minutes": None if age is None else round(age, 1),
                "stale": age is None or age > self.cfg.daemon.max_snapshot_age_minutes, **windows,
                "probe": {"running": bool(self.probe_task and not self.probe_task.done()),
                          "last_at": clock.to_iso(self.last_probe_at) if self.last_probe_at else None,
                          "error": self.probe_error}}

    def start_probe(self) -> bool:
        """Refresh the usage numbers with one tiny request, in the background. False if one is going."""
        if self.probe_task and not self.probe_task.done():
            return False

        async def go():
            try:
                snapshot = await self._prober(self.cfg.agent)
            except Exception as exc:
                self.probe_error = str(exc)[:300]
                self.bus.publish("usage.updated", self.usage_view())
                return
            self.probe_error = None
            self.save_snapshot(snapshot)
            await self.tick()  # a routine that waited for numbers may start now

        self.probe_task = self._spawn(go())
        self.last_probe_at = self._now()  # after the spawn: a probe that never started holds no cooldown
        self.bus.publish("usage.updated", self.usage_view())
        return True

    async def probe_now(self) -> dict:
        """Probe and wait for it (API / CLI)."""
        self.start_probe()
        if self.probe_task:
            await asyncio.shield(self.probe_task)
        return self.usage_view()

    # --- integrations ----------------------------------------------------------------

    def listed(self) -> dict | None:
        return (self.store.get_json(scan_key(self.account)) or {}).get("listed")

    def start_scan(self) -> bool:
        """`claude mcp list` as the agent's account, for claude.ai connector URLs (background)."""
        if self.scan_task and not self.scan_task.done():
            return False

        async def go():
            try:
                scanner = self._scanner or (lambda: scan(resolve_command(self.cfg.agent.command),
                                                         agent_env(self.cfg.agent), paths.state_dir()))
                listed = await scanner()
            except Exception as exc:
                self.scan_error = str(exc)[:300]
                return
            self.scan_error = None
            self.store.put_json(scan_key(self.account), {"at": clock.utc_now(), "listed": listed})
            self.bus.publish("integrations.scanned", {"count": len(listed)})

        self.scan_task = self._spawn(go())
        return True

    def integrations_view(self) -> dict:
        scanned = self.store.get_json(scan_key(self.account)) or {}
        data = inventory(account_home(self.cfg.agent), self.cfg.agent.mcp_config_file, scanned.get("listed"))
        return {**data, "scanned_at": scanned.get("at"),
                "scanning": bool(self.scan_task and not self.scan_task.done()), "scan_error": self.scan_error}

    # --- the scheduler -----------------------------------------------------------------

    def _evaluate(self, routine: Routine, now) -> Verdict:
        running = any(a.routine.name == routine.name for a in self.active.values())
        return evaluate(routine, self.store.routine_state(routine.name), now, self.snapshot(), running=running,
                        timezone=self.cfg.daemon.timezone,
                        max_snapshot_age_minutes=self.cfg.daemon.max_snapshot_age_minutes)

    async def tick(self) -> None:
        """One pass: reload, wind down runs past their limits, expire, launch by priority, probe."""
        async with self._lock:
            try:
                self._tick()
            except Exception:
                traceback.print_exc()

    def _tick(self) -> None:
        self.reload()
        now = self._now()
        self._guard_running(now)
        problem = account_problem(self.cfg.agent)
        candidates: list[tuple[Routine, Verdict]] = []
        wants_probe = False
        verdicts: dict[str, Verdict] = {}
        for routine in self.routines.values():
            verdict = self._evaluate(routine, now)
            if verdict.action == "disabled":  # occurrences while off are not owed: switching it on starts afresh
                self.store.set_fired(routine.name, clock.to_iso(now))
            for _ in range(MAX_EXPIRES_PER_TICK):
                if verdict.action != "expire":
                    break
                previous = self.verdicts.get(routine.name)
                reason = previous.reason if previous and previous.action == "wait" else verdict.reason
                self.store.add_skip(routine.name, clock.to_iso(verdict.occurrence), reason, clock.to_iso(now))
                self.store.set_fired(routine.name, clock.to_iso(verdict.occurrence))
                self.bus.publish("routine.skipped", {"routine": routine.name,
                                                     "occurrence": clock.to_iso(verdict.occurrence), "reason": reason})
                verdict = self._evaluate(routine, now)
            if verdict.action == "launch" and problem:
                verdict = Verdict("wait", problem, verdict.occurrence, verdict.deadline)
            if verdict.action == "launch":
                candidates.append((routine, verdict))
            wants_probe = wants_probe or verdict.needs_probe
            verdicts[routine.name] = verdict
        candidates.sort(key=lambda c: (-c[0].priority, c[1].occurrence))
        for routine, verdict in candidates:
            if len(self.active) >= self.cfg.daemon.max_parallel:
                verdicts[routine.name] = Verdict("wait", f"all {self.cfg.daemon.max_parallel} slots are busy",
                                                 verdict.occurrence, verdict.deadline)
                continue
            self.store.set_fired(routine.name, clock.to_iso(verdict.occurrence))
            run_id = self._launch(routine, "schedule", verdict.occurrence)
            verdicts[routine.name] = Verdict("idle", f"started run {run_id}")
        changed = {n for n, v in verdicts.items() if self.verdicts.get(n) != v}
        self.verdicts = verdicts
        if changed:
            self.bus.publish("routines.updated", {"names": sorted(changed)})
        cooldown = timedelta(minutes=self.cfg.daemon.probe_cooldown_minutes)
        if wants_probe and not problem and (self.last_probe_at is None or now - self.last_probe_at >= cooldown):
            self.start_probe()

    def _guard_running(self, now) -> None:
        snapshot = self.snapshot()
        for run_id, active in list(self.active.items()):
            if active.run.wrapping_up or not active.run.running:
                continue
            reason = wind_down_reason(active.routine.limits, snapshot, now)
            if reason and active.run.wrap_up(reason, self.cfg.daemon.wind_down_minutes * 60):
                self.bus.publish("run.winding_down", {"run_id": run_id, "routine": active.routine.name,
                                                      "reason": reason})

    # --- runs ---------------------------------------------------------------------------

    def _placeholders(self, routine: Routine, run_id: int) -> dict:
        last = self.store.last_finished(routine.name)
        report = (last or {}).get("report") or ""
        if len(report) > MAX_PREVIOUS_REPORT:
            report = report[:MAX_PREVIOUS_REPORT] + "\n…(cut)"
        local_now = self._now().astimezone(zone(self.cfg.daemon.timezone))
        return {"name": routine.name, "title": routine.title, "run_id": run_id, "now": local_now.isoformat(),
                "last_run_at": (last or {}).get("started_at") or "never (this is the first run)",
                "previous_status": (last or {}).get("status") or "none",
                "previous_summary": (last or {}).get("summary") or "none",
                "previous_report": report or "none", "repos": ", ".join(routine.repos) or "none"}

    def _launch(self, routine: Routine, trigger: str, occurrence) -> int:
        now = clock.to_iso(self._now())
        run_id = self.store.start_run(routine.name, trigger, clock.to_iso(occurrence) if occurrence else None, now,
                                      self.account)
        run_dir = paths.runs_dir() / str(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        run_dir = run_dir.resolve()  # /var -> /private/var: permission rules match real paths
        log_path = paths.log_dir() / f"run-{run_id}.log"
        self.store.set_run_paths(run_id, str(log_path), str(run_dir))
        token = secrets.token_urlsafe(24)
        spec = RunSpec(run_id=run_id, routine=routine, agent=self.cfg.agent,
                       prompt=fill(routine.prompt, **self._placeholders(routine, run_id)), run_dir=run_dir,
                       log_path=log_path, agent_url=self.agent_url, agent_token=token, listed=self.listed())
        account = self.account

        def on_event(summary: str) -> None:
            active = self.active.get(run_id)
            if active and active.run.proc and summary == "started":
                self.store.set_run_pid(run_id, active.run.proc.pid)
            self.bus.publish("run.event", {"run_id": run_id, "routine": routine.name, "event": summary})

        def on_usage(snapshot: dict) -> None:
            self.save_snapshot(snapshot, account)

        run = self._run_factory(spec, on_event, on_usage)
        active = Active(run, routine, token, account=account)
        self.active[run_id] = active
        self._tokens[token] = run_id
        active.task = self._spawn(self._execute(run_id, active))
        self.bus.publish("run.started", {"run_id": run_id, "routine": routine.name, "trigger": trigger})
        return run_id

    async def _execute(self, run_id: int, active: Active) -> None:
        try:
            result = await active.run.execute()
        except Exception as exc:
            traceback.print_exc()
            result = RunResult(None, "failed", error=f"loops-routines error: {exc!r}"[:300])
        finally:
            self.active.pop(run_id, None)
            self._tokens.pop(active.token, None)
            for question in [q for q in self.questions.values() if q.run_id == run_id]:
                self._close_question(question, Answer("cancelled"), "the run ended")
        await self._finish(run_id, active, result)

    async def _finish(self, run_id: int, active: Active, result: RunResult) -> None:
        routine = active.routine
        now = clock.utc_now()
        self.store.finish_run(run_id, now, outcome=result.outcome, status=result.status, summary=result.summary,
                              report=result.report, error=result.error, denied=result.denied,
                              unavailable=result.unavailable, usage=result.usage)
        run = self.store.run(run_id)
        try:
            deliver.write_report(run, routine.title)
        except OSError:
            traceback.print_exc()
        if not deliver.should_mark_unseen(routine.notify, result.outcome, result.status):
            self.store.mark_seen([run_id])
        if self.cfg.notify.enabled and deliver.daemon_should_post(routine.notify, result.outcome, result.status,
                                                                  active.notified_error):
            text = deliver.headline(routine.title, result.outcome, result.status, result.summary, result.error)
            await self._banner(run_id, routine.title, text, "error", by_daemon=True)
        self.bus.publish("run.finished", {"run_id": run_id, "routine": routine.name, "outcome": result.outcome,
                                          "status": result.status, "summary": result.summary})
        await self.tick()  # a slot is free: the next routine may start

    # --- queries ---------------------------------------------------------------------------

    def routine_view(self, routine: Routine) -> dict:
        state = self.store.routine_state(routine.name)
        verdict = self.verdicts.get(routine.name)
        running = [rid for rid, a in self.active.items() if a.routine.name == routine.name]
        last = self.store.last_finished(routine.name)
        skip = self.store.last_skip(routine.name)
        if running:
            status, reason = "running", self.active[running[0]].run.last_event
        elif not is_enabled(routine, state):
            status, reason = "disabled", "disabled"
        elif verdict and verdict.action == "wait":
            status, reason = "waiting", verdict.reason
        else:
            status, reason = "idle", verdict.reason if verdict else ""
        next_at = verdict.next_at if verdict and verdict.next_at else None
        if next_at is None and status in ("idle", "running"):
            next_at = routine.trigger.next_after(self._now())
        return {
            "name": routine.name, "title": routine.title, "state": status, "reason": reason,
            "enabled": is_enabled(routine, state), "enabled_in_file": routine.enabled,
            "schedule": routine.schedule, "schedule_text": routine.trigger.describe(),
            "next_at": clock.to_iso(next_at) if next_at else None,
            "occurrence": clock.to_iso(verdict.occurrence) if verdict and verdict.occurrence else None,
            "deadline": clock.to_iso(verdict.deadline) if verdict and verdict.deadline else None,
            "running_run_id": running[0] if running else None,
            "priority": routine.priority, "profile": routine.profile,
            "limits": {"min_free": routine.limits.min_free, "before_reset": routine.limits.before_reset,
                       "keep_free": routine.limits.keep_free, "keep_free_weekly": routine.limits.keep_free_weekly},
            "window": routine.window, "grace": fmt_duration(routine.grace) if routine.grace else None,
            "notify": routine.notify, "model": routine.model or self.cfg.agent.model or None,
            "max_runtime": fmt_duration(routine.max_runtime), "max_turns": routine.max_turns,
            "repos": list(routine.repos), "mcp_servers": list(routine.mcp_servers),
            "plugins": list(routine.plugins), "skills": list(routine.skills),
            "last_run": _brief(last), "last_skip": _brief(skip),
            "problems": list(routine.problems), "path": routine.path,
        }

    def list_routines(self) -> list[dict]:
        views = [self.routine_view(r) for r in self.routines.values()]
        views += [{"name": b.name, "title": b.name, "state": "broken", "reason": b.error, "path": b.path,
                   "enabled": False} for b in self.broken.values()]
        order = {"running": 0, "waiting": 1, "idle": 2, "disabled": 3, "broken": 4}
        return sorted(views, key=lambda v: (order[v["state"]], -(v.get("priority") or 0), v["name"]))

    def routine_detail(self, name: str) -> dict:
        if name in self.broken:
            b = self.broken[name]
            return {"name": name, "title": name, "state": "broken", "reason": b.error, "path": b.path,
                    "runs": self.store.runs(name, 20)}
        routine = self._routine(name)
        return {**self.routine_view(routine), "prompt": routine.prompt, "runs": self.store.runs(name, 20)}

    def list_runs(self, routine: str | None = None, limit: int = 50, before_id: int | None = None,
                  include_skipped: bool = True) -> list[dict]:
        return [self._run_view(r, full=False) for r in self.store.runs(routine, min(limit, 500), before_id,
                                                                      include_skipped)]

    def get_run(self, run_id: int) -> dict:
        row = self.store.run(run_id)
        if not row:
            raise NotFound(f"no run {run_id}")
        return {**self._run_view(row, full=True), "messages": self.store.messages(run_id)}

    def _run_view(self, row: dict, full: bool) -> dict:
        routine = self.routines.get(row["routine"])
        view = {k: row[k] for k in ("id", "routine", "trigger", "occurrence_at", "state", "outcome", "status",
                                    "summary", "error", "started_at", "finished_at", "seen")}
        view["title"] = routine.title if routine else row["routine"]
        view["cost_usd"] = (row.get("usage") or {}).get("cost_usd")
        active = self.active.get(row["id"])
        if active:
            view["live"] = {"last_event": active.run.last_event, "wrapping_up": active.run.wrapping_up,
                            "accepts_messages": active.run.accepts_messages}
        if full:
            view.update({k: row[k] for k in ("report", "denied", "unavailable", "usage", "log_path", "run_dir",
                                             "account")})
        return view

    def log_tail(self, run_id: int, lines: int = 200) -> str:
        row = self.store.run(run_id)
        if not row:
            raise NotFound(f"no run {run_id}")
        path = Path(row.get("log_path") or "")
        if not row.get("log_path") or not path.is_file():
            return ""
        lines = max(1, min(lines, 5000))
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - lines * 2000))
            text = f.read().decode("utf-8", "replace")
        return "\n".join(text.splitlines()[-lines:])

    def health(self) -> dict:
        return {"service": "loops-routines", "version": __version__, "pid": os.getpid(), "started_at": self.started_at,
                "port": self.port, "routines": len(self.routines), "broken": len(self.broken),
                "running": len(self.active), "max_parallel": self.cfg.daemon.max_parallel,
                "unseen_alerts": self.store.unseen_alerts(), "problems": list(self.cfg.problems),
                "account_problem": account_problem(self.cfg.agent)}

    def summary(self) -> dict:
        """Everything the UI's main view needs, in one call."""
        return {"health": self.health(), "usage": self.usage_view(), "routines": self.list_routines(),
                "runs": self.list_runs(limit=30), "questions": self.pending_questions()}

    # --- actions ----------------------------------------------------------------------------

    def _routine(self, name: str) -> Routine:
        if name in self.broken:
            raise Conflict(f"routine {name} can't be used: {self.broken[name].error}")
        routine = self.routines.get(name)
        if not routine:
            raise NotFound(f"no routine {name!r} (known: {', '.join(sorted(self.routines)) or 'none'})")
        return routine

    def run_now(self, name: str, force: bool = False, jump_queue: bool = False) -> dict:
        """Start a routine now, outside its schedule (its occurrences stay as they are). Without force the
        routine's own limits still apply; with force only the free slots and its running run do.
        jump_queue: outside the queue altogether: on top of max_parallel and whatever the limits say;
        only a run of the same routine still going stops it."""
        self.reload()
        routine = self._routine(name)
        if any(a.routine.name == name for a in self.active.values()):
            raise Conflict(f"{name} is already running")
        force = force or jump_queue
        if not jump_queue and len(self.active) >= self.cfg.daemon.max_parallel:
            raise Conflict(f"all {self.cfg.daemon.max_parallel} slots are busy (run_routine_now starts it anyway)")
        if problem := account_problem(self.cfg.agent):
            raise Conflict(problem)
        if not force:
            verdict: Gate = gate(routine.limits, self.snapshot(), self._now(), self.cfg.daemon.max_snapshot_age_minutes)
            if not verdict.ok:
                if verdict.needs_probe:
                    self.start_probe()
                raise Conflict(f"its limits don't allow it now: {verdict.reason} (force runs it anyway)")
        run_id = self._launch(routine, "out_of_queue" if jump_queue else "manual", None)
        return {"run_id": run_id, "routine": name, "trigger": "out_of_queue" if jump_queue else "manual"}

    def set_enabled(self, name: str, enabled: bool | None) -> dict:
        """Switch a routine on or off from the UI; None goes back to what its file says."""
        routine = self._routine(name)
        self.store.set_enabled(name, enabled)  # while off, the tick keeps last_fire_at at now: no catch-up later
        self.verdicts.pop(name, None)
        self.bus.publish("routines.updated", {"names": [name]})
        return self.routine_view(routine)

    def _active(self, run_id: int) -> Active:
        active = self.active.get(run_id)
        if not active:
            if self.store.run(run_id):
                raise Conflict(f"run {run_id} is not running")
            raise NotFound(f"no run {run_id}")
        return active

    def stop_run(self, run_id: int) -> dict:
        active = self._active(run_id)
        active.run.stop()
        self.bus.publish("run.stopping", {"run_id": run_id, "routine": active.routine.name})
        return {"run_id": run_id, "stopping": True}

    def message_run(self, run_id: int, text: str) -> dict:
        active = self._active(run_id)
        try:
            active.run.send_message(text)
        except RuntimeError as exc:
            raise Conflict(str(exc)) from None
        message, _ = self.store.put_message(run_id, None, "user", " ".join(text.split())[:2000], clock.utc_now())
        self.bus.publish("run.message", {"run_id": run_id, "message": message})
        return message

    def mark_seen(self, run_ids: list[int]) -> dict:
        self.store.mark_seen([int(i) for i in run_ids])
        self.bus.publish("runs.seen", {"run_ids": run_ids})
        return {"unseen_alerts": self.store.unseen_alerts()}

    # --- the agents' side (/agent/mcp) ---------------------------------------------------

    def run_for_token(self, token: str | None) -> int | None:
        return self._tokens.get(token or "")

    def report_status(self, run_id: int, text: str, kind: str = "info", key: str | None = None) -> dict:
        text = " ".join((text or "").split())
        if not text:
            raise ValueError("text must not be empty")
        if len(text) > MAX_TEXT:
            raise ValueError(f"text is {len(text)} characters; keep a status under {MAX_TEXT}")
        if kind not in MESSAGE_KINDS:
            raise ValueError(f"kind must be one of {', '.join(MESSAGE_KINDS)}")
        message, created = self.store.put_message(run_id, (key or "").strip() or None, kind, text, clock.utc_now())
        self.bus.publish("run.message", {"run_id": run_id, "message": message})
        return {"id": message["id"], "key": message["key"], "created": created}

    def report_blocker(self, run_id: int, summary: str, category: str, goal: str, details: str,
                       fix: list[str] | None = None, tried: list[str] | None = None,
                       outcome: str = "stopped") -> dict:
        summary = " ".join((summary or "").split())
        if not summary or len(summary) > MAX_TEXT:
            raise ValueError(f"summary must be 1..{MAX_TEXT} characters")
        if category not in BLOCKER_CATEGORIES:
            raise ValueError(f"category must be one of {', '.join(BLOCKER_CATEGORIES)}")
        if outcome not in ("stopped", "continuing"):
            raise ValueError("outcome must be stopped or continuing")
        for name, value, limit in (("goal", goal, 1000), ("details", details, 6000)):
            if not (value or "").strip() or len(value) > limit:
                raise ValueError(f"{name} must be 1..{limit} characters")
        report = {"category": category, "outcome": outcome, "goal": goal.strip(), "details": details.strip(),
                  "fix": [str(f).strip() for f in fix or [] if str(f).strip()][:10],
                  "tried": [str(t).strip() for t in tried or [] if str(t).strip()][:10]}
        message, _ = self.store.put_message(run_id, None, "blocker", summary, clock.utc_now(), report=report)
        self.bus.publish("run.message", {"run_id": run_id, "message": message})
        active = self.active.get(run_id)
        if active and active.routine.notify != "never" and self.cfg.notify.enabled:
            self._spawn(self._banner(run_id, f"{active.routine.title}: blocked", summary, "warning", by_daemon=True))
        return {"id": message["id"], "reported": True}

    # --- notifications and questions ----------------------------------------------------

    def _sound(self, kind: str) -> str:
        return self.cfg.notify.error_sound if kind in ("error", "warning") else self.cfg.notify.info_sound

    async def _banner(self, run_id: int | None, title: str, text: str, kind: str, by_daemon: bool = False) -> dict:
        """Show a banner and keep it with the run; a banner that could not be shown says why."""
        problem = None
        if not self.cfg.notify.enabled:
            problem = "notifications are off ([notify] enabled = false)"
        else:
            try:
                await self.notifier.banner(title, text, kind, self._sound(kind))
            except Exception as exc:
                problem = f"the notification failed: {exc}"[:300]
        result = {"shown": problem is None, **({"problem": problem} if problem else {})}
        if run_id is not None:
            report = {"kind": kind, "by": "daemon" if by_daemon else "agent", **result}
            message, _ = self.store.put_message(run_id, None, "notify", text[:MAX_TEXT], clock.utc_now(), report=report)
            self.bus.publish("run.message", {"run_id": run_id, "message": message})
            result["id"] = message["id"]
        return result

    def _agent_active(self, run_id: int) -> Active:
        active = self.active.get(run_id)
        if not active:
            raise Conflict("this run has ended")
        if active.routine.notify == "never":
            raise ValueError(f"routine {active.routine.name} has notify = \"never\": it does not notify or ask the user")
        return active

    async def notify_user(self, run_id: int, text: str, kind: str = "info") -> dict:
        """An agent's notification: a banner titled with its routine."""
        text = " ".join((text or "").split())
        if not text or len(text) > MAX_TEXT:
            raise ValueError(f"text must be 1..{MAX_TEXT} characters")
        if kind not in NOTIFY_KINDS:
            raise ValueError(f"kind must be one of {', '.join(NOTIFY_KINDS)}")
        active = self._agent_active(run_id)
        if kind == "error":
            active.notified_error = True
        return await self._banner(run_id, active.routine.title, text, kind)

    async def ask_user(self, run_id: int, question: str, buttons: list[str] | None = None,
                       timeout_minutes: int | None = None) -> dict:
        """An agent's question: a dialog with its buttons; waits for the first answer from any channel."""
        question = (question or "").strip()
        if not question or len(question) > MAX_QUESTION:
            raise ValueError(f"question must be 1..{MAX_QUESTION} characters")
        buttons = check_buttons(buttons)
        active = self._agent_active(run_id)
        minutes = timeout_minutes or self.cfg.notify.ask_timeout_minutes
        if not 1 <= minutes <= 120:
            raise ValueError("timeout_minutes must be 1..120")
        if not self.cfg.notify.enabled:
            return {"status": "unavailable", "answer": None,
                    "note": "notifications are off: decide on your own and say so in your report"}
        self._question_seq += 1
        now = self._now()
        q = Question(self._question_seq, run_id, active.routine.name, active.routine.title, question, buttons,
                     clock.to_iso(now), clock.to_iso(now + timedelta(minutes=minutes)),
                     asyncio.get_running_loop().create_future())
        self.questions[q.id] = q
        self.store.put_message(run_id, f"question-{q.id}", "question", question[:MAX_TEXT], clock.utc_now(),
                               report={"question_id": q.id, "buttons": buttons, "status": "waiting",
                                       "deadline": q.deadline})
        self.bus.publish("question.asked", q.view())
        q.dialog = self._spawn(self._dialog(q, minutes * 60))
        try:
            answer = await asyncio.wait_for(asyncio.shield(q.future), minutes * 60 + 5)
        except TimeoutError:
            answer = Answer("timed_out")
            self._close_question(q, answer, "nobody answered in time")
        return {"status": answer.status, "answer": answer.answer,
                **({"note": "no answer: take the safe path and say so in your report"}
                   if answer.status != "answered" else {})}

    async def _dialog(self, q: Question, seconds: int) -> None:
        try:
            answer = await self.notifier.ask(q.title, q.text, q.buttons, seconds)
        except asyncio.CancelledError:
            return  # answered elsewhere
        except Exception as exc:
            answer = Answer("failed", error=str(exc)[:300])
        if answer.status == "failed":  # no dialog: the web page, the CLI and MCP can still answer
            self.store.put_message(q.run_id, None, "notify", f"the question dialog could not open: {answer.error}"[:MAX_TEXT],
                                   clock.utc_now(), report={"kind": "warning", "by": "daemon", "shown": False})
            return
        self._close_question(q, answer, "in the dialog")

    def _close_question(self, q: Question, answer: Answer, via: str) -> None:
        if q.future.done():
            return
        q.future.set_result(answer)
        self.questions.pop(q.id, None)
        if q.dialog and not q.dialog.done() and q.dialog is not asyncio.current_task():
            q.dialog.cancel()
        self.store.put_message(q.run_id, f"question-{q.id}", "question", q.text[:MAX_TEXT], clock.utc_now(),
                               report={"question_id": q.id, "buttons": q.buttons, "status": answer.status,
                                       "answer": answer.answer, "via": via, "deadline": q.deadline})
        self.bus.publish("question.closed", {**q.view(), "status": answer.status, "answer": answer.answer})

    def pending_questions(self) -> list[dict]:
        return [q.view() for q in self.questions.values()]

    def answer_question(self, question_id: int, answer: str) -> dict:
        q = self.questions.get(int(question_id))
        if not q:
            raise NotFound(f"no open question {question_id} (answered, timed out, or its run ended)")
        button = next((b for b in q.buttons if b.lower() == " ".join(str(answer).split()).lower()), None)
        if button is None:
            raise ValueError(f"answer with one of: {', '.join(q.buttons)}")
        self._close_question(q, Answer("answered", button), "through the API")
        return {"question_id": q.id, "answer": button}

    async def notify_test(self, kind: str = "info", ask: bool = False) -> dict:
        """Show a test banner, or a test dialog and wait for the click (for macOS permissions)."""
        if kind not in NOTIFY_KINDS:
            raise ValueError(f"kind must be one of {', '.join(NOTIFY_KINDS)}")
        if ask:
            answer = await self.notifier.ask("loops-routines", "Can you see this question? Pick an answer.",
                                             ["Yes", "No", "Later"], 120)
            return {"status": answer.status, "answer": answer.answer, "error": answer.error}
        return await self._banner(None, "loops-routines", f"A test notification ({kind}).", kind)

    # --- lifecycle ---------------------------------------------------------------------------

    async def run_forever(self) -> None:
        self.recover()
        if any(s.startswith("claude.ai ") for r in self.routines.values() for s in r.mcp_servers) and not self.listed():
            self.start_scan()
        while True:
            await self.tick()
            self._prune()
            await asyncio.sleep(self.cfg.daemon.tick_seconds)

    def _prune(self) -> None:
        cutoff = clock.to_iso(self._now() - timedelta(days=self.cfg.daemon.keep_runs_days))
        self.store.prune(cutoff)

    async def shutdown(self) -> None:
        for active in list(self.active.values()):
            active.run.stop()
        tasks = [a.task for a in self.active.values() if a.task]
        if tasks:
            await asyncio.wait(tasks, timeout=10)


def _brief(row: dict | None) -> dict | None:
    if not row:
        return None
    return {k: row.get(k) for k in ("id", "state", "outcome", "status", "summary", "error", "started_at",
                                    "finished_at", "occurrence_at")}


def _stop_orphan(pid: int) -> None:
    """A run's process group a previous daemon left behind; only if it still is a claude process."""
    try:
        if os.getpgid(pid) != pid:
            return
        command = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                                 timeout=5).stdout
        if "claude" in command:
            os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError, subprocess.SubprocessError):
        pass
