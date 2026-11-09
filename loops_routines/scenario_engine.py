"""The daemon's side of scenarios: when each select runs, what its decisions do, when steps run, and every
query and action on scenarios and their items. Service owns one engine; REST, MCP and the CLI go through
Service.

A select runs every `select_every`, right after a person tracks an item or presses one of its buttons,
right after a step's run ends, and on demand. Its input is every followed item (not done) with its state,
its last decision, its runs and the events since the last select; its output, one decision per item, is
applied here:

    run      queue the step for the item; the daemon's tick starts it when a slot, the step's limits and
             the scenario's max_items allow, sharing the slots with the routines by priority
    wait     the item waits; its reason is shown
    notify   a macOS notification, once per (notice name, once_per)
    done     the item is no longer followed (it stays visible until it is pruned)
    apply, ask   recorded and shown, not carried out yet

Every decision may patch the item's state and set its title, url and buttons. While a step runs for an
item, select's decisions for it only do that: the run is not interrupted.

A step runs once per (item, step, version) when it ends well (a result file); select gives a new version
to run it again. A step that fails max_attempts times on the same version is not run again: the item is
"stuck" and you get one notification.
"""

import asyncio
import os
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta

from loops_routines.core import clock, paths
from loops_routines.core.claude_cli import account_problem
from loops_routines.core.routines import Routine
from loops_routines.core.scenarios import (
    LATER_ACTIONS,
    BrokenScenario,
    Decision,
    Scenario,
    fill_step,
    fingerprint,
    load_scenarios,
    merge_state,
    parse_decisions,
    run_select,
)
from loops_routines.core.triggers import fmt_duration
from loops_routines.core.usage import gate
from loops_routines.core.windows import zone
from loops_routines.errors import Conflict, NotFound

MAX_EVENTS_IN = 50  # per item, handed to one select
MAX_ITEMS_IN = 500
MAX_PREVIOUS_REPORT = 20_000


@dataclass
class StepCandidate:
    """An item whose step may start now, if a slot is free."""
    scenario: Scenario
    key: str
    step: str
    priority: int
    at: datetime  # when it was queued: older first among equal priorities


class ScenarioEngine:
    def __init__(self, service):
        self.s = service
        self.scenarios: dict[str, Scenario] = {}
        self.broken: dict[str, BrokenScenario] = {}
        self._fingerprint = None
        self.tasks: dict[str, asyncio.Task] = {}
        self.reload(force=True)

    def _iso(self) -> str:
        """Now, from the service's clock (tests move it)."""
        return clock.to_iso(self.s._now())

    # --- loading ---------------------------------------------------------------------------

    def reload(self, force: bool = False) -> bool:
        folder = paths.scenarios_dir()
        print_ = (fingerprint(folder), self.s._cfg_mtime)  # steps use config.toml's profiles
        if not force and print_ == self._fingerprint:
            return False
        self.scenarios, self.broken = load_scenarios(folder, self.s.cfg)
        self._fingerprint = print_
        now = self._iso()
        for name in self.scenarios:
            self.s.store.ensure_scenario(name, now)
        self.s.bus.publish("scenarios.changed", {"names": sorted([*self.scenarios, *self.broken])})
        return True

    def scenario(self, name: str) -> Scenario:
        if name in self.broken:
            raise Conflict(f"scenario {name} can't be used: {self.broken[name].error}")
        sc = self.scenarios.get(name)
        if not sc:
            raise NotFound(f"no scenario {name!r} (known: {', '.join(sorted(self.scenarios)) or 'none'})")
        return sc

    def enabled(self, sc: Scenario) -> bool:
        override = self.s.store.scenario_state(sc.name).get("enabled")
        return sc.enabled if override is None else bool(override)

    def selecting(self, name: str) -> bool:
        task = self.tasks.get(name)
        return bool(task and not task.done())

    def step_enabled(self, sc: Scenario, step: str) -> bool:
        override = (self.s.store.scenario_state(sc.name).get("step_switches") or {}).get(step)
        spec = sc.specs.get(step)
        return (spec.enabled if spec else False) if override is None else bool(override)

    def running(self, name: str, key: str | None = None) -> list[tuple[int, object]]:
        """(run id, Active) of the steps running for a scenario, or for one of its items."""
        return [(rid, a) for rid, a in self.s.active.items()
                if a.item and a.item[0] == name and (key is None or a.item[1] == key)]

    def spec_for(self, row: dict) -> Routine | None:
        """The step a run row belongs to, if its scenario and step still exist."""
        sc = self.scenarios.get(row.get("scenario") or "")
        return sc.specs.get(row.get("step") or "") if sc else None

    # --- when selects run ------------------------------------------------------------------

    def tick(self, now) -> None:
        """Start the select of every switched-on scenario that is due."""
        self.reload()
        for sc in self.scenarios.values():
            if not self.enabled(sc) or self.selecting(sc.name):
                continue
            last = self.s.store.scenario_state(sc.name).get("last_select_at")
            if last is None or now - clock.from_iso(last) >= timedelta(seconds=sc.select_every):
                self.start_select(sc.name)

    def start_select(self, name: str) -> bool:
        """Run a scenario's select in the background; False if one is already going."""
        if self.selecting(name):
            return False
        sc = self.scenario(name)
        self.tasks[name] = self.s._spawn(self._select(sc))
        return True

    async def select_now(self, name: str) -> dict:
        """Run the select now and wait for it (API, CLI, MCP)."""
        self.reload()
        self.start_select(name)
        task = self.tasks.get(name)
        if task:
            await asyncio.shield(task)
        return self.scenario_detail(name)

    def _payload(self, sc: Scenario, now: str) -> tuple[dict, list[int]]:
        events = self.s.store.pending_events(sc.name)
        by_key: dict[str, list] = {}
        for e in events:
            by_key.setdefault(e["key"], []).append({k: e[k] for k in ("id", "kind", "name", "data", "at")})
        items = {}
        for item in self.s.store.items(sc.name)[:MAX_ITEMS_IN]:
            running = self.running(sc.name, item["key"])
            items[item["key"]] = {
                "title": item["title"], "url": item["url"], "state": item["state"], "tracked": item["tracked"],
                "paused": item["paused"], "buttons": item["buttons"],
                "last": {k: item[k] for k in ("action", "step", "reason", "version")},
                "running": {"step": running[0][1].item[2], "run_id": running[0][0]} if running else None,
                "runs": {step: _run_brief(r) for step, r in self.s.store.last_step_runs(sc.name, item["key"]).items()},
                "events": by_key.get(item["key"], [])[-MAX_EVENTS_IN:],
            }
        steps = {step: self.step_enabled(sc, step) for step in sc.steps}
        payload = {"scenario": sc.name, "now": now, "settings": sc.settings, "steps": list(sc.steps),
                   "steps_on": sorted(s for s, on in steps.items() if on),
                   "items": items, "tracked": sorted(k for k, v in items.items() if v["tracked"])}
        return payload, [e["id"] for e in events]

    async def _select(self, sc: Scenario) -> None:
        now = self._iso()
        started = time.monotonic()
        payload, event_ids = self._payload(sc, now)
        env = {**os.environ, "LOOPS_ROUTINES_SCENARIO": sc.name, "LOOPS_ROUTINES_STATE": str(paths.state_dir())}
        log_path = paths.log_dir() / f"scenario-{sc.name}.log"
        error, problems, decisions = None, [], []
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as log:
                log.write(f"--- {now} select\n")
                output = await run_select(sc, payload, env=env, log=log)
            decisions, problems = parse_decisions(output, sc.steps)
        except (ValueError, OSError) as exc:
            error = str(exc)[:500]
        previous_error = self.s.store.scenario_state(sc.name).get("last_error")
        if error is None:
            for decision in decisions:
                try:
                    await self._apply(sc, decision, now)
                except ValueError as exc:
                    problems.append(f"{decision.key}: {exc}")
                except Exception as exc:  # one item's trouble never stops the others
                    traceback.print_exc()
                    problems.append(f"{decision.key}: {exc!r}"[:300])
            self.s.store.consume_events(event_ids, now)
        duration = int((time.monotonic() - started) * 1000)
        self.s.store.record_select(sc.name, now, error=error, problems=problems[:20], duration_ms=duration)
        if error and error != previous_error and self.s.cfg.notify.enabled:
            await self.s._banner(None, f"{sc.title}: select failed", error[:280], "error", by_daemon=True)
        self.s.bus.publish("scenario.selected", {"name": sc.name, "error": error, "decisions": len(decisions),
                                                 "problems": len(problems)})

    async def _apply(self, sc: Scenario, d: Decision, now: str) -> None:
        store = self.s.store
        item = store.item(sc.name, d.key)
        if item and item["done_at"]:
            return  # done (by select or untrack) stays done; `track` brings it back
        state = merge_state((item or {}).get("state") or {}, d.state)
        values: dict = {"state": state}
        if d.title is not None:
            values["title"] = d.title
        if d.url is not None:
            values["url"] = d.url
        if d.buttons is not None:
            values["buttons"] = d.buttons
        if item and self.running(sc.name, d.key):  # a running step is not interrupted
            store.put_item(sc.name, d.key, now, **values)
            return
        values.update({"action": d.action, "step": d.step, "reason": d.reason, "version": d.version,
                       "vars": d.vars, "queue": None})
        paused = bool(item and item["paused"])
        if d.action in LATER_ACTIONS:
            what = f"{d.action} {d.step}" if d.step else d.action
            values["reason"] = f"{d.reason} ({what} not carried out: {LATER_ACTIONS[d.action]} aren't supported yet)".lstrip()
        if d.action == "done":
            values["done_at"], values["tracked"] = now, 0
        if paused:
            values["reason"] = f"paused · {values['reason']}"
        elif d.action == "run":
            values["queue"], stuck = self._run_block(sc, d.key, d.step, d.version)
            values["queue"] = values["queue"] or "queued"
            if stuck:
                values["action"], values["reason"] = "stuck", values["queue"]
                await self._notice(sc, d.key, item, values, now, "stuck", values["queue"], "warning",
                                   f"{d.step}@{d.version}")
        changed = not item or any(item.get(k) != values.get(k) for k in ("action", "step", "reason"))
        if d.action == "notify" and not paused:
            await self._notice(sc, d.key, item, values, now, d.name, d.text, d.kind, d.once_per or d.text)
        store.put_item(sc.name, d.key, now, **values)
        if changed:
            store.add_event(sc.name, d.key, "decision", now, d.action,
                            {"step": d.step, "reason": values["reason"], "version": d.version}, consumed=True)
            self.s.bus.publish("item.updated", {"scenario": sc.name, "key": d.key, "action": values["action"],
                                                "reason": values["reason"]})

    async def _notice(self, sc: Scenario, key: str, item: dict | None, values: dict, now: str, name: str, text: str,
                      kind: str, once: str) -> None:
        """A banner about an item, once per (name, once): values gets what to store with the item."""
        notified = dict(values.get("notified") or (item or {}).get("notified") or {})
        if notified.get(name) == once:
            return
        title = values.get("title") or (item or {}).get("title") or key
        result = await self.s._banner(None, f"{sc.title}: {title}"[:120], text[:280], kind, by_daemon=True)
        notified[name] = once
        values["notified"] = notified
        values["notice"] = f"{name}: {text}"
        self.s.store.add_event(sc.name, key, "notice", now, name,
                               {"text": text, "kind": kind, "shown": result.get("shown")}, consumed=True)

    def _run_block(self, sc: Scenario, key: str, step: str, version: str | None) -> tuple[str | None, bool]:
        """Why a step can't be queued for an item: (reason or None, stuck)."""
        if step not in sc.specs:
            return f"no step {step}", False
        if not self.step_enabled(sc, step):
            return f"step {step} is off", False
        ok, failed = self.s.store.step_attempts(sc.name, key, step, version)
        if ok:
            return f"{step} already ran for {f'version {version}' if version else 'this item'}", False
        if failed >= sc.max_attempts:
            return f"stuck: {step} failed {failed} times{f' on version {version}' if version else ''}", True
        return None, False

    # --- running steps -----------------------------------------------------------------------

    def _queue(self, sc: Scenario, item: dict, note: str) -> None:
        """Say why an item's step waits; written only when it changes."""
        if item.get("queue") != note:
            self.s.store.put_item(sc.name, item["key"], self._iso(), queue=note)
            self.s.bus.publish("item.updated", {"scenario": sc.name, "key": item["key"], "queue": note})

    def candidates(self, now, problem: str | None) -> tuple[list[StepCandidate], bool]:
        """Items whose step may start now (the tick launches them if slots are free); wants a probe?"""
        self.reload()
        snapshot = self.s.snapshot()
        out, wants_probe = [], False
        for sc in self.scenarios.values():
            enabled = self.enabled(sc)
            for item in self.s.store.items(sc.name):
                if item["action"] != "run" or item["paused"] or self.running(sc.name, item["key"]):
                    continue
                if not enabled:
                    self._queue(sc, item, "the scenario is off")
                    continue
                note, _ = self._run_block(sc, item["key"], item["step"], item["version"])
                if note is None and problem:
                    note = problem
                if note is None and len(self.running(sc.name)) >= sc.max_items:
                    note = f"{sc.max_items} of this scenario's items have a step running (max_items)"
                if note is None:
                    verdict = gate(sc.specs[item["step"]].limits, snapshot, now, self.s.cfg.daemon.max_snapshot_age_minutes)
                    wants_probe = wants_probe or verdict.needs_probe
                    note = None if verdict.ok else verdict.reason
                if note:
                    self._queue(sc, item, note)
                    continue
                out.append(StepCandidate(sc, item["key"], item["step"], sc.specs[item["step"]].priority,
                                         clock.from_iso(item["updated_at"])))
        return out, wants_probe

    def hold(self, c: StepCandidate, note: str) -> None:
        item = self.s.store.item(c.scenario.name, c.key)
        if item:
            self._queue(c.scenario, item, note)

    def launch(self, c: StepCandidate, trigger: str = "scenario") -> int | None:
        """Start the step for the item (the caller checked the slots); None when max_items is reached."""
        sc = c.scenario
        if trigger == "scenario" and len(self.running(sc.name)) >= sc.max_items:
            self.hold(c, f"{sc.max_items} of this scenario's items have a step running (max_items)")
            return None
        item = self.s.store.item(sc.name, c.key)
        spec = sc.specs[c.step]
        version = item["version"]
        run_id = self.s._launch(spec, trigger, None, prompt=lambda rid: self._prompt(sc, item, c.step, spec, rid),
                                item=(sc.name, c.key, c.step, version))
        now = self._iso()
        self.s.store.put_item(sc.name, c.key, now, queue=f"running {c.step} (run {run_id})")
        self.s.store.add_event(sc.name, c.key, "run", now, "started",
                               {"run_id": run_id, "step": c.step, "version": version, "trigger": trigger}, consumed=True)
        self.s.bus.publish("item.updated", {"scenario": sc.name, "key": c.key, "queue": f"running {c.step}",
                                            "run_id": run_id})
        return run_id

    def _prompt(self, sc: Scenario, item: dict, step: str, spec: Routine, run_id: int) -> str:
        last = self.s.store.last_step_runs(sc.name, item["key"]).get(step)
        report = (last or {}).get("report") or ""
        if len(report) > MAX_PREVIOUS_REPORT:
            report = report[:MAX_PREVIOUS_REPORT] + "\n…(cut)"
        local_now = self.s._now().astimezone(zone(self.s.cfg.daemon.timezone))
        values = {
            "name": spec.name, "title": spec.title, "run_id": run_id, "now": local_now.isoformat(),
            "repos": ", ".join(spec.repos) or "none", "scenario": sc.name, "step": step,
            "key": item["key"], "url": item["url"] or "", "version": item["version"] or "", "reason": item["reason"],
            "item": {"key": item["key"], "title": item["title"] or item["key"], "url": item["url"] or ""},
            "vars": item["vars"] if item["step"] == step else {}, "settings": sc.settings, "state": item["state"],
            "previous_status": (last or {}).get("status") or "none",
            "previous_summary": (last or {}).get("summary") or "none", "previous_report": report or "none",
        }
        return fill_step(spec.prompt, values)

    async def run_finished(self, item_ref: tuple, run: dict) -> None:
        """A step's run ended: the next select sees it (once, as a `run` event) and starts now. A step that
        failed max_attempts times on this version makes the item stuck."""
        name, key, step, version = item_ref
        now = self._iso()
        store = self.s.store
        store.add_event(name, key, "run", now, "finished", {
            "run_id": run["id"], "step": step, "version": version, "outcome": run["outcome"], "status": run["status"],
            "summary": run["summary"], "error": run["error"], "data": run["data"]})
        item = store.item(name, key)
        sc = self.scenarios.get(name)
        if not item or item["done_at"] or not sc:
            return
        ok, failed = store.step_attempts(name, key, step, version)
        values: dict = {"queue": None}
        if item["action"] == "run" and item["step"] == step and item["version"] == version:
            values["action"] = "ran"  # this decision is used up: the next select says what comes next
            values["reason"] = f"{step}: {run['summary'] or run['outcome']}"[:280]
        if not ok and failed >= sc.max_attempts:
            text = f"stuck: {step} failed {failed} times{f' on version {version}' if version else ''}"
            values.update({"action": "stuck", "reason": f"{text}: {run['error'] or run['summary'] or run['outcome']}"[:280]})
            await self._notice(sc, key, item, values, now, "stuck", values["reason"], "warning", f"{step}@{version}")
        store.put_item(name, key, now, **values)
        self.s.bus.publish("item.updated", {"scenario": name, "key": key, "action": values.get("action", item["action"]),
                                            "run_id": run["id"]})
        if self.enabled(sc):
            self.start_select(name)

    def run_step_now(self, name: str, key: str, step: str, force: bool = False) -> dict:
        """Start a step for an item now: outside the queue, its attempts, its switch and max_items. The
        daemon's slots still apply; without force the step's limits do too."""
        sc = self.scenario(name)
        if step not in sc.specs:
            raise NotFound(f"scenario {name} has no step {step!r} (steps: {', '.join(sc.steps) or 'none'})")
        self._item(name, key)
        if self.running(name, key):
            raise Conflict(f"{key} already has a step running")
        if len(self.s.active) >= self.s.cfg.daemon.max_parallel:
            raise Conflict(f"all {self.s.cfg.daemon.max_parallel} slots are busy")
        if problem := account_problem(self.s.cfg.agent):
            raise Conflict(problem)
        if not force:
            verdict = gate(sc.specs[step].limits, self.s.snapshot(), self.s._now(),
                           self.s.cfg.daemon.max_snapshot_age_minutes)
            if not verdict.ok:
                if verdict.needs_probe:
                    self.s.start_probe()
                raise Conflict(f"the step's limits don't allow it now: {verdict.reason} (force runs it anyway)")
        run_id = self.launch(StepCandidate(sc, key, step, 0, self.s._now()), trigger="manual")
        return {"run_id": run_id, "scenario": name, "key": key, "step": step}

    def set_step_enabled(self, name: str, step: str, enabled: bool | None) -> dict:
        """Switch one step of a scenario on or off; None goes back to its file. A step that is off is not
        started (select's `run` for it waits); a running one finishes."""
        sc = self.scenario(name)
        if step not in sc.specs:
            raise NotFound(f"scenario {name} has no step {step!r} (steps: {', '.join(sc.steps) or 'none'})")
        self.s.store.set_step_enabled(name, step, enabled)
        self.s.bus.publish("scenarios.updated", {"names": [name]})
        return self.scenario_view(sc)

    # --- queries ---------------------------------------------------------------------------

    def scenario_view(self, sc: Scenario) -> dict:
        st = self.s.store.scenario_state(sc.name)
        enabled = self.enabled(sc)
        items = self.s.store.items(sc.name)
        if self.selecting(sc.name):
            status = "selecting"
        elif not enabled:
            status = "disabled"
        elif st.get("last_error"):
            status = "error"
        else:
            status = "idle"
        next_at = None
        if enabled:
            last = st.get("last_select_at")
            next_at = clock.to_iso(clock.from_iso(last) + timedelta(seconds=sc.select_every)) if last else None
        counts: dict[str, int] = {}
        for item in items:
            counts[item["action"] or "new"] = counts.get(item["action"] or "new", 0) + 1
        switches = st.get("step_switches") or {}
        step_list = [{"name": step, "enabled": self.step_enabled(sc, step), "enabled_in_file": spec.enabled,
                      "switched": step in switches, "title": spec.title, "model": spec.model or self.s.cfg.agent.model or None,
                      "profile": spec.profile, "priority": spec.priority, "max_runtime": fmt_duration(spec.max_runtime)}
                     for step, spec in sc.specs.items()]
        return {
            "name": sc.name, "title": sc.title, "state": status, "enabled": enabled, "enabled_in_file": sc.enabled,
            "select_every": fmt_duration(timedelta(seconds=sc.select_every)),
            "last_select_at": st.get("last_select_at"), "last_select_ok_at": st.get("last_select_ok_at"),
            "last_duration_ms": st.get("last_duration_ms"), "next_select_at": next_at,
            "error": st.get("last_error"), "problems": [*sc.problems, *(st.get("last_problems") or [])],
            "steps": list(sc.steps), "step_list": step_list, "has_apply": sc.has_apply, "max_items": sc.max_items,
            "max_attempts": sc.max_attempts, "running": len(self.running(sc.name)),
            "items": len(items), "by_action": counts, "path": sc.path,
        }

    def list_scenarios(self) -> list[dict]:
        views = [self.scenario_view(sc) for sc in self.scenarios.values()]
        views += [{"name": b.name, "title": b.name, "state": "broken", "error": b.error, "path": b.path,
                   "enabled": False} for b in self.broken.values()]
        order = {"selecting": 0, "error": 1, "idle": 2, "disabled": 3, "broken": 4}
        return sorted(views, key=lambda v: (order[v["state"]], v["name"]))

    def scenario_detail(self, name: str, include_done: bool = False) -> dict:
        if name in self.broken:
            b = self.broken[name]
            return {"name": name, "title": name, "state": "broken", "error": b.error, "path": b.path}
        sc = self.scenario(name)
        return {**self.scenario_view(sc), "settings": sc.settings, "schema": sc.schema, "defaults": sc.defaults,
                "item_list": self.list_items(name, include_done)}

    def list_items(self, name: str, include_done: bool = False) -> list[dict]:
        self.scenario(name)
        return [self._item_view(i) for i in self.s.store.items(name, include_done)]

    def item_detail(self, name: str, key: str) -> dict:
        self.scenario(name)
        item = self._item(name, key)
        return {**self._item_view(item), "state": item["state"], "vars": item["vars"], "notified": item["notified"],
                "runs": [_run_brief(r) for r in self.s.store.item_runs(name, key)],
                "events": self.s.store.item_events(name, key)}

    def _item_view(self, item: dict) -> dict:
        running = self.running(item["scenario"], item["key"])
        return {**_item_view(item), "running_run_id": running[0][0] if running else None}

    def _item(self, name: str, key: str) -> dict:
        item = self.s.store.item(name, key)
        if not item:
            raise NotFound(f"scenario {name} has no item {key!r}")
        return item

    # --- actions ---------------------------------------------------------------------------

    def set_enabled(self, name: str, enabled: bool | None) -> dict:
        sc = self.scenario(name)
        self.s.store.set_scenario_enabled(name, enabled)
        self.s.bus.publish("scenarios.updated", {"names": [name]})
        if self.enabled(sc):
            self.start_select(name)
        return self.scenario_view(sc)

    def track(self, name: str, key: str, title: str | None = None, url: str | None = None) -> dict:
        """A person asks the scenario to look after this item; its select sees it at once."""
        self.scenario(name)
        key = " ".join((key or "").split())
        if not key or len(key) > 200:
            raise ValueError("key must be 1..200 characters")
        now = self._iso()
        values = {"tracked": 1, "done_at": None}
        if title:
            values["title"] = title[:300]
        if url:
            values["url"] = url[:1000]
        self.s.store.put_item(name, key, now, **values)
        self.s.store.add_event(name, key, "track", now, "track")
        self.start_select(name)
        return self._item_view(self._item(name, key))

    def untrack(self, name: str, key: str) -> dict:
        """Stop following an item: it is done, its running step is stopped, and its select no longer gets it
        (track brings it back)."""
        self.scenario(name)
        self._item(name, key)
        now = self._iso()
        for _, active in self.running(name, key):
            active.run.stop()
        self.s.store.put_item(name, key, now, tracked=0, done_at=now, action="done", reason="untracked", queue=None)
        self.s.store.add_event(name, key, "track", now, "untrack", consumed=True)
        self.s.bus.publish("item.updated", {"scenario": name, "key": key, "action": "done", "reason": "untracked"})
        return self._item_view(self._item(name, key))

    def press(self, name: str, key: str, button: str) -> dict:
        """A person presses one of the item's buttons: an event for the next select, which starts now."""
        self.scenario(name)
        item = self._item(name, key)
        wanted = " ".join((button or "").split()).lower()
        match = next((b for b in item["buttons"] if b.lower() == wanted), None)
        if match is None:
            raise ValueError(f"press one of: {', '.join(item['buttons']) or '(this item has no buttons now)'}")
        now = self._iso()
        self.s.store.add_event(name, key, "button", now, match)
        self.start_select(name)
        return {"scenario": name, "key": key, "pressed": match}

    def pause(self, name: str, key: str, paused: bool) -> dict:
        """A paused item keeps being selected (its state stays current) but nothing is done for it."""
        self.scenario(name)
        self._item(name, key)
        self.s.store.put_item(name, key, self._iso(), paused=int(bool(paused)))
        self.s.bus.publish("item.updated", {"scenario": name, "key": key, "paused": bool(paused)})
        return self._item_view(self._item(name, key))

    async def shutdown(self) -> None:
        for task in self.tasks.values():
            if not task.done():
                task.cancel()


def _item_view(item: dict) -> dict:
    return {k: item[k] for k in ("key", "title", "url", "action", "step", "reason", "version", "queue", "notice",
                                 "buttons", "tracked", "paused", "done_at", "created_at", "updated_at")}


def _run_brief(run: dict) -> dict:
    return {"run_id": run["id"], "step": run["step"], "version": run["version"], "state": run["state"],
            "outcome": run["outcome"], "status": run["status"], "summary": run["summary"], "error": run["error"],
            "data": run["data"], "started_at": run["started_at"], "finished_at": run["finished_at"]}
