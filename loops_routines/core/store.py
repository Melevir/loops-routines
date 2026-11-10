"""The daemon's SQLite file. Only the daemon opens it: the UI and the CLI go through its API.

    kv             small state: usage snapshots per account, the last integrations scan
    routine_state  per routine: when the daemon first saw it, the last occurrence it handled, the
                   enabled switch from the UI (NULL = as the file says)
    runs           every run and every skipped occurrence (state = skipped), with its result
    run_messages   what agents posted while they ran (report_status, report_blocker)
    scenario_state per scenario: the enabled switch from the UI, the last select and its error
    items          per scenario and item key: the selector's state and last decision, tracked, paused
    item_events    what happened to an item: button presses and tracking (handed to the next select),
                   notices sent, decisions made
"""

import json
import sqlite3
import threading
from pathlib import Path

SCHEMA = [
    """
    CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE routine_state (
        name TEXT PRIMARY KEY,
        first_seen_at TEXT NOT NULL,
        last_fire_at TEXT,
        enabled INTEGER
    );
    CREATE TABLE runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        routine TEXT NOT NULL,
        trigger TEXT NOT NULL,           -- schedule | manual
        occurrence_at TEXT,
        state TEXT NOT NULL,             -- running | finished | skipped
        outcome TEXT,                    -- ok | failed | timeout | stopped | lost
        status TEXT,                     -- the agent's: ok | alert | nothing | unknown
        summary TEXT NOT NULL DEFAULT '',
        report TEXT NOT NULL DEFAULT '',
        error TEXT,
        denied TEXT NOT NULL DEFAULT '[]',
        unavailable TEXT NOT NULL DEFAULT '[]',
        usage TEXT,
        account TEXT NOT NULL DEFAULT '',
        started_at TEXT NOT NULL,
        finished_at TEXT,
        log_path TEXT,
        run_dir TEXT,
        pid INTEGER,
        seen INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX runs_routine ON runs (routine, id);
    CREATE TABLE run_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL,
        key TEXT,
        kind TEXT NOT NULL,              -- info | progress | success | warning | error | blocker
        text TEXT NOT NULL,
        report TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (run_id, key)
    );
    """,
    """
    CREATE TABLE scenario_state (
        name TEXT PRIMARY KEY,
        first_seen_at TEXT NOT NULL,
        enabled INTEGER,                 -- the switch from the UI; NULL = as scenario.toml says
        last_select_at TEXT,
        last_select_ok_at TEXT,
        last_error TEXT,
        last_problems TEXT NOT NULL DEFAULT '[]',
        last_duration_ms INTEGER
    );
    CREATE TABLE items (
        scenario TEXT NOT NULL,
        key TEXT NOT NULL,
        title TEXT,
        url TEXT,
        state TEXT NOT NULL DEFAULT '{}',   -- the selector's memory for this item
        action TEXT,                     -- the last decision: run | apply | wait | notify | ask | done
        step TEXT,
        reason TEXT NOT NULL DEFAULT '',
        version TEXT,
        notice TEXT,                     -- the last notification's kind and text, for the UI
        buttons TEXT NOT NULL DEFAULT '[]',
        notified TEXT NOT NULL DEFAULT '{}',  -- notify kind -> the once_per it was sent for
        tracked INTEGER NOT NULL DEFAULT 0,   -- added by a person (track), not only found by select
        paused INTEGER NOT NULL DEFAULT 0,
        done_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (scenario, key)
    );
    CREATE TABLE item_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scenario TEXT NOT NULL,
        key TEXT NOT NULL,
        kind TEXT NOT NULL,              -- button | track | notice | decision
        name TEXT,
        data TEXT,
        at TEXT NOT NULL,
        consumed_at TEXT                 -- when a select saw it (button, track); NULL = not yet
    );
    CREATE INDEX item_events_item ON item_events (scenario, key, id);
    """,
    """
    ALTER TABLE runs ADD COLUMN scenario TEXT;     -- a scenario's step run: which scenario, item and step
    ALTER TABLE runs ADD COLUMN item_key TEXT;
    ALTER TABLE runs ADD COLUMN step TEXT;
    ALTER TABLE runs ADD COLUMN version TEXT;      -- what the run was about (select's version)
    ALTER TABLE runs ADD COLUMN data TEXT;         -- the step's result data for the scenario
    CREATE INDEX runs_item ON runs (scenario, item_key, step, id);
    ALTER TABLE items ADD COLUMN vars TEXT NOT NULL DEFAULT '{}';  -- the last run decision's vars
    ALTER TABLE items ADD COLUMN queue TEXT;       -- why its step isn't running yet, or which run it is
    ALTER TABLE scenario_state ADD COLUMN step_switches TEXT NOT NULL DEFAULT '{}';  -- step -> on/off from the UI
    """,
]

ITEM_JSON = ("state", "buttons", "notified", "vars")
ITEM_FIELDS = {"title", "url", "state", "action", "step", "reason", "version", "notice", "buttons", "notified",
               "tracked", "paused", "done_at", "vars", "queue"}
FAILED_RUN = "(outcome != 'ok' OR status = 'unknown')"  # a step run that did not end with a usable result


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.RLock()
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            for number, script in enumerate(SCHEMA[version:], version + 1):
                self._db.executescript(f"BEGIN; {script}; PRAGMA user_version = {number}; COMMIT;")

    def _all(self, sql: str, *params) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, params).fetchall()]

    def _one(self, sql: str, *params) -> dict | None:
        rows = self._all(sql, *params)
        return rows[0] if rows else None

    def _exec(self, sql: str, *params) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, params)

    # --- kv ------------------------------------------------------------------------

    def get(self, key: str) -> str | None:
        row = self._one("SELECT value FROM kv WHERE key = ?", key)
        return row["value"] if row else None

    def put(self, key: str, value: str) -> None:
        self._exec("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                   key, value)

    def get_json(self, key: str):
        raw = self.get(key)
        return json.loads(raw) if raw else None

    def put_json(self, key: str, value) -> None:
        self.put(key, json.dumps(value))

    # --- routines ------------------------------------------------------------------

    def ensure_routine(self, name: str, now: str) -> None:
        self._exec("INSERT OR IGNORE INTO routine_state (name, first_seen_at) VALUES (?, ?)", name, now)

    def routine_state(self, name: str) -> dict:
        return self._one("SELECT * FROM routine_state WHERE name = ?", name) or {}

    def set_fired(self, name: str, occurrence_at: str) -> None:
        self._exec("UPDATE routine_state SET last_fire_at = ? WHERE name = ?", occurrence_at, name)

    def set_enabled(self, name: str, enabled: bool | None) -> None:
        self._exec("UPDATE routine_state SET enabled = ? WHERE name = ?",
                   None if enabled is None else int(bool(enabled)), name)

    # --- runs ----------------------------------------------------------------------

    def start_run(self, routine: str, trigger: str, occurrence_at: str | None, now: str, account: str = "",
                  item: tuple[str, str, str, str | None] | None = None) -> int:
        """item: (scenario, key, step, version) for a scenario's step."""
        scenario, key, step, version = item or (None, None, None, None)
        cur = self._exec(
            "INSERT INTO runs (routine, trigger, occurrence_at, state, started_at, account, scenario, item_key, step, "
            "version) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?)", routine, trigger, occurrence_at, now, account,
            scenario, key, step, version)
        return cur.lastrowid

    def set_run_paths(self, run_id: int, log_path: str, run_dir: str) -> None:
        self._exec("UPDATE runs SET log_path = ?, run_dir = ? WHERE id = ?", log_path, run_dir, run_id)

    def set_run_pid(self, run_id: int, pid: int) -> None:
        self._exec("UPDATE runs SET pid = ? WHERE id = ?", pid, run_id)

    def finish_run(self, run_id: int, now: str, *, outcome: str, status: str, summary: str = "", report: str = "",
                   error: str | None = None, denied=(), unavailable=(), usage: dict | None = None,
                   data: dict | None = None) -> None:
        self._exec(
            "UPDATE runs SET state = 'finished', finished_at = ?, outcome = ?, status = ?, summary = ?, report = ?, "
            "error = ?, denied = ?, unavailable = ?, usage = ?, data = ?, pid = NULL WHERE id = ?",
            now, outcome, status, summary, report, error, json.dumps(list(denied)), json.dumps(list(unavailable)),
            json.dumps(usage) if usage is not None else None, json.dumps(data) if data is not None else None, run_id)

    def add_skip(self, routine: str, occurrence_at: str, reason: str, now: str) -> int:
        cur = self._exec(
            "INSERT INTO runs (routine, trigger, occurrence_at, state, error, started_at, finished_at, seen) "
            "VALUES (?, 'schedule', ?, 'skipped', ?, ?, ?, 1)", routine, occurrence_at, reason, now, now)
        return cur.lastrowid

    def run(self, run_id: int) -> dict | None:
        row = self._one("SELECT * FROM runs WHERE id = ?", run_id)
        return _run(row) if row else None

    def runs(self, routine: str | None = None, limit: int = 50, before_id: int | None = None,
             include_skipped: bool = True) -> list[dict]:
        where, params = [], []
        if routine:
            where.append("routine = ?")
            params.append(routine)
        if before_id:
            where.append("id < ?")
            params.append(before_id)
        if not include_skipped:
            where.append("state != 'skipped'")
        sql = "SELECT * FROM runs" + (f" WHERE {' AND '.join(where)}" if where else "") + " ORDER BY id DESC LIMIT ?"
        return [_run(r) for r in self._all(sql, *params, limit)]

    def last_finished(self, routine: str) -> dict | None:
        row = self._one("SELECT * FROM runs WHERE routine = ? AND state = 'finished' ORDER BY id DESC LIMIT 1", routine)
        return _run(row) if row else None

    def last_skip(self, routine: str) -> dict | None:
        row = self._one("SELECT * FROM runs WHERE routine = ? AND state = 'skipped' ORDER BY id DESC LIMIT 1", routine)
        return _run(row) if row else None

    def running_rows(self) -> list[dict]:
        return [_run(r) for r in self._all("SELECT * FROM runs WHERE state = 'running'")]

    def close_lost(self, run_id: int, now: str) -> None:
        self._exec("UPDATE runs SET state = 'finished', outcome = 'lost', status = 'unknown', finished_at = ?, "
                   "error = 'the daemon stopped while it ran', pid = NULL WHERE id = ?", now, run_id)

    def mark_seen(self, run_ids: list[int]) -> None:
        with self._lock:
            self._db.executemany("UPDATE runs SET seen = 1 WHERE id = ?", [(i,) for i in run_ids])

    def unseen_alerts(self) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM runs WHERE seen = 0 AND state = 'finished' "
                        "AND (status = 'alert' OR outcome IN ('failed', 'timeout', 'lost'))")
        return row["n"] if row else 0

    def prune(self, before: str) -> int:
        with self._lock:
            old = [r["id"] for r in self._all("SELECT id FROM runs WHERE started_at < ? AND state != 'running'", before)]
            self._db.executemany("DELETE FROM run_messages WHERE run_id = ?", [(i,) for i in old])
            self._db.executemany("DELETE FROM runs WHERE id = ?", [(i,) for i in old])
        return len(old)

    # --- scenarios -------------------------------------------------------------------

    def ensure_scenario(self, name: str, now: str) -> None:
        self._exec("INSERT OR IGNORE INTO scenario_state (name, first_seen_at) VALUES (?, ?)", name, now)

    def scenario_state(self, name: str) -> dict:
        row = self._one("SELECT * FROM scenario_state WHERE name = ?", name) or {}
        if row:
            row["last_problems"] = json.loads(row["last_problems"] or "[]")
            row["step_switches"] = json.loads(row["step_switches"] or "{}")
        return row

    def set_scenario_enabled(self, name: str, enabled: bool | None) -> None:
        self._exec("UPDATE scenario_state SET enabled = ? WHERE name = ?",
                   None if enabled is None else int(bool(enabled)), name)

    def set_step_enabled(self, name: str, step: str, enabled: bool | None) -> None:
        with self._lock:
            switches = self.scenario_state(name).get("step_switches") or {}
            if enabled is None:
                switches.pop(step, None)
            else:
                switches[step] = bool(enabled)
            self._exec("UPDATE scenario_state SET step_switches = ? WHERE name = ?", json.dumps(switches), name)

    def step_attempts(self, scenario: str, key: str, step: str, version: str | None) -> tuple[int, int]:
        """Finished runs of a step on an item's version: (ended with a result, failed)."""
        row = self._one(f"SELECT COUNT(*) AS n, SUM(CASE WHEN {FAILED_RUN} THEN 1 ELSE 0 END) AS failed FROM runs "
                        "WHERE scenario = ? AND item_key = ? AND step = ? AND version IS ? AND state = 'finished'",
                        scenario, key, step, version)
        failed = (row or {}).get("failed") or 0
        return ((row or {}).get("n") or 0) - failed, failed

    def item_runs(self, scenario: str, key: str, limit: int = 20) -> list[dict]:
        return [_run(r) for r in self._all("SELECT * FROM runs WHERE scenario = ? AND item_key = ? ORDER BY id DESC "
                                           "LIMIT ?", scenario, key, limit)]

    def last_step_runs(self, scenario: str, key: str) -> dict[str, dict]:
        """The latest finished run of each step on an item."""
        rows = self._all("SELECT * FROM runs WHERE id IN (SELECT MAX(id) FROM runs WHERE scenario = ? AND item_key = ? "
                         "AND state = 'finished' GROUP BY step)", scenario, key)
        return {r["step"]: _run(r) for r in rows}

    def record_select(self, name: str, at: str, *, error: str | None, problems=(), duration_ms: int) -> None:
        self._exec("UPDATE scenario_state SET last_select_at = ?, last_error = ?, last_problems = ?, "
                   "last_duration_ms = ?, last_select_ok_at = CASE WHEN ? IS NULL THEN ? ELSE last_select_ok_at END "
                   "WHERE name = ?", at, error, json.dumps(list(problems)), duration_ms, error, at, name)

    def items(self, scenario: str, include_done: bool = False) -> list[dict]:
        sql = "SELECT * FROM items WHERE scenario = ?" + ("" if include_done else " AND done_at IS NULL")
        return [_item(r) for r in self._all(sql + " ORDER BY created_at, key", scenario)]

    def item(self, scenario: str, key: str) -> dict | None:
        row = self._one("SELECT * FROM items WHERE scenario = ? AND key = ?", scenario, key)
        return _item(row) if row else None

    def put_item(self, scenario: str, key: str, now: str, **values) -> dict:
        """Create the item if new, then set the given fields (JSON ones as Python values)."""
        unknown = set(values) - ITEM_FIELDS
        if unknown:
            raise ValueError(f"unknown item fields: {', '.join(sorted(unknown))}")
        with self._lock:
            self._exec("INSERT OR IGNORE INTO items (scenario, key, created_at, updated_at) VALUES (?, ?, ?, ?)",
                       scenario, key, now, now)
            if values:
                sets = ", ".join(f"{name} = ?" for name in values)
                params = [json.dumps(v) if name in ITEM_JSON else v for name, v in values.items()]
                self._exec(f"UPDATE items SET {sets}, updated_at = ? WHERE scenario = ? AND key = ?",
                           *params, now, scenario, key)
            return self.item(scenario, key)

    def delete_item(self, scenario: str, key: str) -> None:
        with self._lock:
            self._exec("DELETE FROM items WHERE scenario = ? AND key = ?", scenario, key)
            self._exec("DELETE FROM item_events WHERE scenario = ? AND key = ?", scenario, key)

    def add_event(self, scenario: str, key: str, kind: str, now: str, name: str | None = None,
                  data: dict | None = None, consumed: bool = False) -> int:
        cur = self._exec("INSERT INTO item_events (scenario, key, kind, name, data, at, consumed_at) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)", scenario, key, kind, name,
                         json.dumps(data) if data is not None else None, now, now if consumed else None)
        return cur.lastrowid

    def pending_events(self, scenario: str) -> list[dict]:
        return [_event(r) for r in self._all("SELECT * FROM item_events WHERE scenario = ? AND consumed_at IS NULL "
                                             "ORDER BY id", scenario)]

    def consume_events(self, event_ids: list[int], now: str) -> None:
        with self._lock:
            self._db.executemany("UPDATE item_events SET consumed_at = ? WHERE id = ?", [(now, i) for i in event_ids])

    def apply_attempts(self, scenario: str, key: str, what: str | None, version: str | None) -> tuple[int, int]:
        """(ok, failed) runs of the apply hook for an `apply` decision on this item and version."""
        rows = self._all("SELECT name, COUNT(*) AS n FROM item_events WHERE scenario = ? AND key = ? AND kind = 'apply' "
                         "AND json_extract(data, '$.source') = 'decision' AND json_extract(data, '$.step') IS ? "
                         "AND json_extract(data, '$.version') IS ? GROUP BY name", scenario, key, what, version)
        counts = {r["name"]: r["n"] for r in rows}
        return counts.get("ok", 0), counts.get("failed", 0)

    def item_events(self, scenario: str, key: str, limit: int = 50) -> list[dict]:
        return [_event(r) for r in self._all("SELECT * FROM item_events WHERE scenario = ? AND key = ? "
                                             "ORDER BY id DESC LIMIT ?", scenario, key, limit)]

    def prune_items(self, before: str) -> int:
        """Items done before `before`, with their events."""
        with self._lock:
            old = self._all("SELECT scenario, key FROM items WHERE done_at IS NOT NULL AND done_at < ?", before)
            for row in old:
                self.delete_item(row["scenario"], row["key"])
            self._exec("DELETE FROM item_events WHERE consumed_at IS NOT NULL AND at < ?", before)
        return len(old)

    # --- messages from running agents ------------------------------------------------

    def put_message(self, run_id: int, key: str | None, kind: str, text: str, now: str,
                    report: dict | None = None) -> tuple[dict, bool]:
        """A message; with a key, the run's earlier message with that key changes in place."""
        with self._lock:
            if key:
                existing = self._one("SELECT id FROM run_messages WHERE run_id = ? AND key = ?", run_id, key)
                if existing:
                    self._exec("UPDATE run_messages SET kind = ?, text = ?, report = ?, updated_at = ? WHERE id = ?",
                               kind, text, json.dumps(report) if report else None, now, existing["id"])
                    return self.message(existing["id"]), False
            cur = self._exec(
                "INSERT INTO run_messages (run_id, key, kind, text, report, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)", run_id, key, kind, text, json.dumps(report) if report else None, now, now)
            return self.message(cur.lastrowid), True

    def message(self, message_id: int) -> dict:
        return _message(self._one("SELECT * FROM run_messages WHERE id = ?", message_id))

    def messages(self, run_id: int) -> list[dict]:
        return [_message(r) for r in self._all("SELECT * FROM run_messages WHERE run_id = ? ORDER BY id", run_id)]


def _run(row: dict) -> dict:
    for key in ("denied", "unavailable"):
        row[key] = json.loads(row[key] or "[]")
    row["usage"] = json.loads(row["usage"]) if row.get("usage") else None
    row["data"] = json.loads(row["data"]) if row.get("data") else None
    row["seen"] = bool(row["seen"])
    return row


def _message(row: dict) -> dict:
    row["report"] = json.loads(row["report"]) if row.get("report") else None
    return row


def _item(row: dict) -> dict:
    for key in ITEM_JSON:
        row[key] = json.loads(row[key]) if row.get(key) else ({} if key != "buttons" else [])
    row["tracked"], row["paused"] = bool(row["tracked"]), bool(row["paused"])
    return row


def _event(row: dict) -> dict:
    row["data"] = json.loads(row["data"]) if row.get("data") else None
    return row
