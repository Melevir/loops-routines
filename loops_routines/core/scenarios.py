"""Scenarios: multi-step agent workflows over many items (see docs/design/scenarios.md).

A scenario is a folder, ~/.loops_routines/scenarios/<name>/:

    scenario.toml   title, how often `select` runs, limits, step defaults, settings (+ their schema)
    select          executable: JSON in on stdin (items, their state, events), JSON out (one decision each)
    steps/*.md      the agent steps (run in a later version; listed now)
    apply           executable, optional (used in a later version)

This module is pure apart from run_select: parsing, validation, the select contract and state merging.
The daemon side lives in loops_routines.scenario_engine.
"""

import asyncio
import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from loops_routines.core.triggers import fmt_duration, parse_duration

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
STEP_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
KNOWN_KEYS = {"title", "select_every", "select_timeout", "enabled", "max_items", "max_attempts", "defaults",
              "settings"}
SCHEMA_TYPES = {"int": int, "float": (int, float), "str": str, "bool": bool, "list": list}
ACTIONS = ("run", "apply", "wait", "notify", "ask", "done")
LATER_ACTIONS = {"run": "agent steps", "apply": "apply hooks", "ask": "questions"}  # not executed yet
NOTICE_KINDS = ("info", "success", "warning", "error")
MIN_SELECT_EVERY = 60
MAX_KEY = 200
MAX_TEXT = 280
MAX_BUTTONS = 5
MAX_OUTPUT = 2_000_000  # bytes a select may print
MAX_STATE = 200_000  # bytes of one item's state


@dataclass(frozen=True)
class Scenario:
    name: str
    title: str
    path: str  # the folder
    select_every: int  # seconds
    select_timeout: int  # seconds
    enabled: bool
    max_items: int
    max_attempts: int
    defaults: dict
    settings: dict
    schema: dict
    steps: tuple[str, ...]
    has_apply: bool
    problems: tuple[str, ...] = ()  # usable, but worth a look

    @property
    def select_path(self) -> Path:
        return Path(self.path) / "select"

    @property
    def apply_path(self) -> Path:
        return Path(self.path) / "apply"


@dataclass(frozen=True)
class BrokenScenario:
    name: str
    path: str
    error: str


def parse_scenario(folder: Path) -> Scenario:
    """A scenario folder -> Scenario; ValueError with what to fix when it can't be used."""
    folder = Path(folder)
    name = folder.name
    if not NAME_RE.match(name):
        raise ValueError(f"folder name {name!r}: use lowercase letters, digits, - and _")
    toml_path = folder / "scenario.toml"
    if not toml_path.is_file():
        raise ValueError("no scenario.toml")
    try:
        raw = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"scenario.toml: {exc}") from None
    problems = [f"scenario.toml: unknown key {key!r}" for key in raw if key not in KNOWN_KEYS]

    select = folder / "select"
    if not select.is_file():
        raise ValueError("no `select` executable in the folder")
    if not os.access(select, os.X_OK):
        raise ValueError(f"`select` is not executable: chmod +x {select}")
    apply = folder / "apply"
    if apply.exists() and not os.access(apply, os.X_OK):
        raise ValueError(f"`apply` is not executable: chmod +x {apply}")

    every = _duration(raw, "select_every", "5m")
    if every < MIN_SELECT_EVERY:
        raise ValueError(f"select_every must be at least {fmt_duration(timedelta(seconds=MIN_SELECT_EVERY))}")
    timeout = _duration(raw, "select_timeout", "60s")
    if not 1 <= timeout <= 600:
        raise ValueError("select_timeout must be 1s..10m")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be true or false")
    max_items = _int(raw, "max_items", 3, 1, 50)
    max_attempts = _int(raw, "max_attempts", 2, 1, 10)
    defaults = raw.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ValueError("[defaults] must be a table")
    settings = dict(raw.get("settings", {}))
    if not isinstance(raw.get("settings", {}), dict):
        raise ValueError("[settings] must be a table")
    schema = settings.pop("schema", {})
    if not isinstance(schema, dict):
        raise ValueError("[settings.schema] must be a table")
    check_settings(settings, schema)

    steps = []
    steps_dir = folder / "steps"
    if steps_dir.is_dir():
        for path in sorted(steps_dir.glob("*.md")):
            if STEP_RE.match(path.stem):
                steps.append(path.stem)
            else:
                problems.append(f"steps/{path.name}: use lowercase letters, digits, - and _ in step names")
    title = raw.get("title") or name
    if not isinstance(title, str):
        raise ValueError("title must be text")
    return Scenario(name=name, title=title, path=str(folder), select_every=every, select_timeout=timeout,
                    enabled=enabled, max_items=max_items, max_attempts=max_attempts, defaults=defaults,
                    settings=settings, schema=schema, steps=tuple(steps), has_apply=apply.is_file(),
                    problems=tuple(problems))


def _duration(raw: dict, key: str, default: str) -> int:
    value = raw.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a duration like \"5m\"")
    try:
        return int(parse_duration(value).total_seconds())
    except ValueError as exc:
        raise ValueError(f"{key}: {exc}") from None


def _int(raw: dict, key: str, default: int, low: int, high: int) -> int:
    value = raw.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ValueError(f"{key} must be a whole number {low}..{high}")
    return value


def check_settings(settings: dict, schema: dict) -> None:
    """[settings] against [settings.schema]: each described setting has the right type and range."""
    for key, spec in schema.items():
        if not isinstance(spec, dict):
            raise ValueError(f"[settings.schema] {key} must be a table like {{ type = \"int\" }}")
        kind = spec.get("type", "str")
        if kind not in SCHEMA_TYPES:
            raise ValueError(f"[settings.schema] {key}: type must be one of {', '.join(SCHEMA_TYPES)}")
        if key not in settings:
            if spec.get("required"):
                raise ValueError(f"[settings] {key} is required")
            continue
        value = settings[key]
        if isinstance(value, bool) and kind in ("int", "float") or not isinstance(value, SCHEMA_TYPES[kind]):
            raise ValueError(f"[settings] {key} must be {kind}")
        if "min" in spec and value < spec["min"] or "max" in spec and value > spec["max"]:
            raise ValueError(f"[settings] {key} must be {spec.get('min', '…')}..{spec.get('max', '…')}")
        if "choices" in spec and value not in spec["choices"]:
            raise ValueError(f"[settings] {key} must be one of {', '.join(map(str, spec['choices']))}")


def load_scenarios(folder: Path) -> tuple[dict[str, Scenario], dict[str, BrokenScenario]]:
    good, broken = {}, {}
    if not folder.is_dir():
        return good, broken
    for path in sorted(p for p in folder.iterdir() if p.is_dir() and not p.name.startswith(".")):
        try:
            good[path.name] = parse_scenario(path)
        except (ValueError, OSError) as exc:
            broken[path.name] = BrokenScenario(path.name, str(path), str(exc))
    return good, broken


def fingerprint(folder: Path) -> tuple:
    """Changes when any scenario's toml, select, apply or step file changes."""
    if not folder.is_dir():
        return ()
    marks = []
    for path in sorted(folder.glob("*/*")) + sorted(folder.glob("*/steps/*.md")):
        try:
            stat = path.stat()
        except OSError:
            continue
        marks.append((str(path), stat.st_mtime_ns, stat.st_size, stat.st_mode))
    return tuple(marks)


# --- the select contract ---------------------------------------------------------------------


@dataclass
class Decision:
    key: str
    action: str
    reason: str = ""
    title: str | None = None
    url: str | None = None
    step: str | None = None
    version: str | None = None
    vars: dict = field(default_factory=dict)
    state: dict | None = None  # a patch for the item's state
    kind: str = "info"  # notify: the banner's level
    name: str = ""  # notify: which notice this is ("yours", "stuck"); once_per counts per name
    text: str = ""  # notify, ask
    once_per: str | None = None  # notify: sent once per this value (default: its text)
    buttons: list | None = None  # what a person may press on the item; None leaves them as they are


def parse_decisions(output: str, steps: tuple[str, ...]) -> tuple[list[Decision], list[str]]:
    """select's stdout -> decisions and the problems with the ones left out. A list, or {"items": [...]}."""
    if len(output.encode()) > MAX_OUTPUT:
        raise ValueError(f"select printed more than {MAX_OUTPUT // 1_000_000} MB")
    try:
        data = json.loads(output or "null")
    except json.JSONDecodeError as exc:
        raise ValueError(f"select did not print JSON: {exc}") from None
    if isinstance(data, dict):
        data = data.get("items")
    if not isinstance(data, list):
        raise ValueError('select must print a JSON list of decisions (or {"items": [...]})')
    decisions, problems, seen = [], [], set()
    for number, raw in enumerate(data, 1):
        try:
            decision = _decision(raw, steps)
        except ValueError as exc:
            key = raw.get("key") if isinstance(raw, dict) else None
            problems.append(f"decision {number}{f' ({key})' if key else ''}: {exc}")
            continue
        if decision.key in seen:
            problems.append(f"decision {number} ({decision.key}): the item already has a decision")
            continue
        seen.add(decision.key)
        decisions.append(decision)
    return decisions, problems


def _decision(raw, steps: tuple[str, ...]) -> Decision:
    if not isinstance(raw, dict):
        raise ValueError("must be an object")
    key = raw.get("key")
    if not isinstance(key, str) or not key.strip() or len(key) > MAX_KEY:
        raise ValueError(f"key must be text of 1..{MAX_KEY} characters")
    action = raw.get("action")
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
    d = Decision(key=key.strip(), action=action, reason=_text(raw, "reason", MAX_TEXT),
                 title=_opt_text(raw, "title", 300), url=_opt_text(raw, "url", 1000),
                 step=_opt_text(raw, "step", 64), version=_opt_text(raw, "version", 200))
    for name in ("vars", "state"):
        value = raw.get(name)
        if value is not None and not isinstance(value, dict):
            raise ValueError(f"{name} must be an object")
    d.vars = raw.get("vars") or {}
    d.state = raw.get("state")
    if action == "run":
        if not d.step:
            raise ValueError("run needs a step")
        if d.step not in steps:
            raise ValueError(f"no step {d.step!r} (steps: {', '.join(steps) or 'none'})")
    if action in ("notify", "ask"):
        d.text = _text(raw, "text", 600 if action == "ask" else MAX_TEXT)
        if not d.text:
            raise ValueError(f"{action} needs a text")
        d.kind = raw.get("kind", "info")
        if d.kind not in NOTICE_KINDS:
            raise ValueError(f"kind must be one of {', '.join(NOTICE_KINDS)}")
        d.once_per = _opt_text(raw, "once_per", 200)
        d.name = _opt_text(raw, "name", 40) or d.kind
    buttons = raw.get("buttons")
    if buttons is not None:
        if not isinstance(buttons, list) or len(buttons) > MAX_BUTTONS or \
                not all(isinstance(b, str) and 0 < len(b) <= 40 for b in buttons):
            raise ValueError(f"buttons must be a list of up to {MAX_BUTTONS} short names")
        d.buttons = list(dict.fromkeys(buttons))
    return d


def _text(raw: dict, key: str, limit: int) -> str:
    value = raw.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text")
    return " ".join(value.split())[:limit]


def _opt_text(raw: dict, key: str, limit: int) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ValueError(f"{key} must be text")
    return str(value).strip()[:limit] or None


def merge_state(state: dict, patch: dict | None) -> dict:
    """The item's state with select's patch on top: a key set to null is removed."""
    merged = dict(state)
    for key, value in (patch or {}).items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    if len(json.dumps(merged).encode()) > MAX_STATE:
        raise ValueError(f"the item's state would be over {MAX_STATE // 1000} KB")
    return merged


async def run_select(scenario: Scenario, payload: dict, env: dict | None = None, log=None) -> str:
    """Run the scenario's select with payload as JSON on stdin; its stdout. ValueError on a failure, a
    non-zero exit or the timeout (the process is killed). log: a file stderr goes to."""
    proc = await asyncio.create_subprocess_exec(
        str(scenario.select_path), cwd=scenario.path, env=env, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
    try:
        out, err = await asyncio.wait_for(proc.communicate(json.dumps(payload).encode()), scenario.select_timeout)
    except TimeoutError:
        _kill(proc)
        await proc.wait()
        took = fmt_duration(timedelta(seconds=scenario.select_timeout))
        raise ValueError(f"select took longer than {took} and was stopped") from None
    except asyncio.CancelledError:
        _kill(proc)
        raise
    text = err.decode("utf-8", "replace")
    if log is not None and text.strip():
        log.write(text if text.endswith("\n") else text + "\n")
    if proc.returncode:
        tail = " ".join(text.strip().splitlines()[-3:])[:300]
        raise ValueError(f"select exited with {proc.returncode}" + (f": {tail}" if tail else ""))
    return out.decode("utf-8", "replace")


def _kill(proc) -> None:
    try:
        os.killpg(proc.pid, 9)
    except (ProcessLookupError, PermissionError, OSError):
        pass
