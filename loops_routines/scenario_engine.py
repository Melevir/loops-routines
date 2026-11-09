"""The daemon's side of scenarios: when each select runs, what its decisions do, and every query and
action on scenarios and their items. Service owns one engine; REST, MCP and the CLI go through Service.

A select runs every `select_every`, right after a person tracks an item or presses one of its buttons,
and on demand. Its input is every followed item (not done) with its state, its last decision and the
events since the last select; its output, one decision per item, is applied here:

    wait     the item waits; its reason is shown
    notify   a macOS notification, once per (notice name, once_per)
    done     the item is no longer followed (it stays visible until it is pruned)
    run, apply, ask   recorded and shown, not carried out yet (agent steps come next)

Every decision may patch the item's state and set its title, url and buttons.
"""

import asyncio
import os
import time
import traceback
from datetime import timedelta

from loops_routines.core import clock, paths
from loops_routines.core.scenarios import (
    LATER_ACTIONS,
    BrokenScenario,
    Decision,
    Scenario,
    fingerprint,
    load_scenarios,
    merge_state,
    parse_decisions,
    run_select,
)
from loops_routines.core.triggers import fmt_duration
from loops_routines.errors import Conflict, NotFound

MAX_EVENTS_IN = 50  # per item, handed to one select
MAX_ITEMS_IN = 500


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
        print_ = fingerprint(folder)
        if not force and print_ == self._fingerprint:
            return False
        self.scenarios, self.broken = load_scenarios(folder)
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
            items[item["key"]] = {
                "title": item["title"], "url": item["url"], "state": item["state"], "tracked": item["tracked"],
                "paused": item["paused"], "buttons": item["buttons"],
                "last": {k: item[k] for k in ("action", "step", "reason", "version")},
                "events": by_key.get(item["key"], [])[-MAX_EVENTS_IN:],
            }
        payload = {"scenario": sc.name, "now": now, "settings": sc.settings, "steps": list(sc.steps),
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
        values: dict = {"state": state, "action": d.action, "step": d.step, "reason": d.reason,
                        "version": d.version}
        if d.title is not None:
            values["title"] = d.title
        if d.url is not None:
            values["url"] = d.url
        if d.buttons is not None:
            values["buttons"] = d.buttons
        paused = bool(item and item["paused"])
        if d.action in LATER_ACTIONS:
            what = f"{d.action} {d.step}" if d.step else d.action
            values["reason"] = f"{d.reason} ({what} not carried out: {LATER_ACTIONS[d.action]} aren't supported yet)".lstrip()
        if d.action == "done":
            values["done_at"], values["tracked"] = now, 0
        if paused:
            values["reason"] = f"paused · {values['reason']}"
        changed = not item or any(item.get(k) != values.get(k) for k in ("action", "step", "reason"))
        if d.action == "notify" and not paused:
            notified = dict((item or {}).get("notified") or {})
            once = d.once_per or d.text
            if notified.get(d.name) != once:
                title = values.get("title") or (item or {}).get("title") or d.key
                result = await self.s._banner(None, f"{sc.title}: {title}"[:120], d.text, d.kind, by_daemon=True)
                notified[d.name] = once
                values["notified"] = notified
                values["notice"] = f"{d.name}: {d.text}"
                store.add_event(sc.name, d.key, "notice", now, d.name,
                                {"text": d.text, "kind": d.kind, "shown": result.get("shown")}, consumed=True)
        store.put_item(sc.name, d.key, now, **values)
        if changed:
            store.add_event(sc.name, d.key, "decision", now, d.action,
                            {"step": d.step, "reason": values["reason"], "version": d.version}, consumed=True)
            self.s.bus.publish("item.updated", {"scenario": sc.name, "key": d.key, "action": d.action,
                                                "reason": values["reason"]})

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
        return {
            "name": sc.name, "title": sc.title, "state": status, "enabled": enabled, "enabled_in_file": sc.enabled,
            "select_every": fmt_duration(timedelta(seconds=sc.select_every)),
            "last_select_at": st.get("last_select_at"), "last_select_ok_at": st.get("last_select_ok_at"),
            "last_duration_ms": st.get("last_duration_ms"), "next_select_at": next_at,
            "error": st.get("last_error"), "problems": [*sc.problems, *(st.get("last_problems") or [])],
            "steps": list(sc.steps), "has_apply": sc.has_apply, "max_items": sc.max_items,
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
        return [_item_view(i) for i in self.s.store.items(name, include_done)]

    def item_detail(self, name: str, key: str) -> dict:
        self.scenario(name)
        item = self._item(name, key)
        return {**_item_view(item), "state": item["state"], "notified": item["notified"],
                "events": self.s.store.item_events(name, key)}

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
        return _item_view(self._item(name, key))

    def untrack(self, name: str, key: str) -> dict:
        """Stop following an item: it is done, and its select no longer gets it (track brings it back)."""
        self.scenario(name)
        self._item(name, key)
        now = self._iso()
        self.s.store.put_item(name, key, now, tracked=0, done_at=now, action="done", reason="untracked")
        self.s.store.add_event(name, key, "track", now, "untrack", consumed=True)
        self.s.bus.publish("item.updated", {"scenario": name, "key": key, "action": "done", "reason": "untracked"})
        return _item_view(self._item(name, key))

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
        return _item_view(self._item(name, key))

    async def shutdown(self) -> None:
        for task in self.tasks.values():
            if not task.done():
                task.cancel()


def _item_view(item: dict) -> dict:
    return {k: item[k] for k in ("key", "title", "url", "action", "step", "reason", "version", "notice", "buttons",
                                 "tracked", "paused", "done_at", "created_at", "updated_at")}
