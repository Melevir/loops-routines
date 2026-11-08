"""The daemon's SQLite file. Only the daemon opens it: the UI and the CLI go through its API.

    kv             small state: usage snapshots per account, the last integrations scan
    routine_state  per routine: when the daemon first saw it, the last occurrence it handled, the
                   enabled switch from the UI (NULL = as the file says)
    runs           every run and every skipped occurrence (state = skipped), with its result
    run_messages   what agents posted while they ran (report_status, report_blocker)
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
]


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

    def start_run(self, routine: str, trigger: str, occurrence_at: str | None, now: str, account: str = "") -> int:
        cur = self._exec(
            "INSERT INTO runs (routine, trigger, occurrence_at, state, started_at, account) "
            "VALUES (?, ?, ?, 'running', ?, ?)", routine, trigger, occurrence_at, now, account)
        return cur.lastrowid

    def set_run_paths(self, run_id: int, log_path: str, run_dir: str) -> None:
        self._exec("UPDATE runs SET log_path = ?, run_dir = ? WHERE id = ?", log_path, run_dir, run_id)

    def set_run_pid(self, run_id: int, pid: int) -> None:
        self._exec("UPDATE runs SET pid = ? WHERE id = ?", pid, run_id)

    def finish_run(self, run_id: int, now: str, *, outcome: str, status: str, summary: str = "", report: str = "",
                   error: str | None = None, denied=(), unavailable=(), usage: dict | None = None) -> None:
        self._exec(
            "UPDATE runs SET state = 'finished', finished_at = ?, outcome = ?, status = ?, summary = ?, report = ?, "
            "error = ?, denied = ?, unavailable = ?, usage = ?, pid = NULL WHERE id = ?",
            now, outcome, status, summary, report, error, json.dumps(list(denied)), json.dumps(list(unavailable)),
            json.dumps(usage) if usage is not None else None, run_id)

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
    row["seen"] = bool(row["seen"])
    return row


def _message(row: dict) -> dict:
    row["report"] = json.loads(row["report"]) if row.get("report") else None
    return row
