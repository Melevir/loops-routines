"""Scenarios: multi-step agent workflows over many items (see docs/design/scenarios.md).

A scenario is a folder, ~/.loops_routines/scenarios/<name>/:

    scenario.toml   title, how often `select` runs, limits, step defaults, settings (+ their schema)
    select          executable: JSON in on stdin (items, their state, events), JSON out (one decision each)
    steps/*.md      the agent steps: a routine header without a schedule, and a prompt
    apply           executable, optional: a step's result (or an `apply` decision) in, writes to the world,
                    a state patch out

This module is pure apart from run_hook: parsing, validation, the select and apply contracts, state merging.
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

from loops_routines.core.config import Config
from loops_routines.core.notify import check_buttons
from loops_routines.core.routines import STEP_KEYS, Routine, build_routine, read_header
from loops_routines.core.triggers import fmt_duration, parse_duration

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
STEP_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
KNOWN_KEYS = {"title", "select_every", "select_timeout", "apply_timeout", "enabled", "max_items", "max_attempts",
              "defaults", "settings"}
SCHEMA_TYPES = {"int": int, "float": (int, float), "str": str, "bool": bool, "list": list}
ACTIONS = ("run", "apply", "wait", "notify", "ask", "done")
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
    specs: dict = field(default_factory=dict, compare=False)  # step -> Routine (when parsed with a config)
    apply_timeout: int = 120  # seconds

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


def parse_scenario(folder: Path, cfg: Config | None = None) -> Scenario:
    """A scenario folder -> Scenario; ValueError with what to fix when it can't be used. With a config the
    step files are parsed too (their profiles live in it), and a broken step makes the scenario broken."""
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
    apply_timeout = _duration(raw, "apply_timeout", "2m")
    if not 1 <= apply_timeout <= 600:
        raise ValueError("apply_timeout must be 1s..10m")
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
    unknown = set(defaults) - STEP_KEYS
    if unknown:
        raise ValueError(f"[defaults]: unknown keys {', '.join(sorted(unknown))} (a step's header keys only)")
    specs = {}
    if cfg is not None:
        for step in steps:
            try:
                specs[step] = parse_step(name, title, step, (steps_dir / f"{step}.md").read_text(encoding="utf-8"),
                                         defaults, cfg, str(steps_dir / f"{step}.md"))
            except (ValueError, OSError, UnicodeDecodeError) as exc:
                raise ValueError(f"steps/{step}.md: {exc}") from None
    return Scenario(name=name, title=title, path=str(folder), select_every=every, select_timeout=timeout,
                    enabled=enabled, max_items=max_items, max_attempts=max_attempts, defaults=defaults,
                    settings=settings, schema=schema, steps=tuple(steps), has_apply=apply.is_file(),
                    problems=tuple(problems), specs=specs, apply_timeout=apply_timeout)


def parse_step(scenario: str, title: str, step: str, text: str, defaults: dict, cfg: Config, path: str = "") -> Routine:
    """steps/<step>.md -> a Routine without a schedule, named <scenario>/<step>; [defaults] under its header."""
    raw, prompt = read_header(text, STEP_KEYS)
    return build_routine(f"{scenario}/{step}", {**defaults, **raw}, prompt, cfg, path, title=f"{title} · {step}")


def fill_step(template: str, values: dict) -> str:
    """A step's prompt: {name} and dotted {vars.x}, {settings.x}, {state.x}, {item.x}; a dict or list goes in
    as JSON. Anything it does not know stays as written."""
    def replace(match: re.Match) -> str:
        value = values
        for part in match.group(1).split("."):
            if not isinstance(value, dict) or part not in value:
                return match.group(0)
            value = value[part]
        if value is None:
            return ""
        return json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    return re.sub(r"\{(\w+(?:\.\w+)*)\}", replace, template)


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


def load_scenarios(folder: Path, cfg: Config | None = None) -> tuple[dict[str, Scenario], dict[str, BrokenScenario]]:
    good, broken = {}, {}
    if not folder.is_dir():
        return good, broken
    for path in sorted(p for p in folder.iterdir() if p.is_dir() and not p.name.startswith(".")):
        try:
            good[path.name] = parse_scenario(path, cfg)
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
    step: str | None = None  # run: the step; apply: what to apply ("merge"), handed to the apply hook
    version: str | None = None
    vars: dict = field(default_factory=dict)
    state: dict | None = None  # a patch for the item's state
    kind: str = "info"  # notify: the banner's level
    name: str = ""  # notify, ask: which notice or question this is; once_per counts per name
    text: str = ""  # notify, ask
    once_per: str | None = None  # notify, ask: sent once per this value (default: its text)
    buttons: list | None = None  # what a person may press on the item; None leaves them as they are
    answers: list = field(default_factory=list)  # ask: the dialog's buttons (1-3)
    timeout_minutes: int | None = None  # ask: how long the question stays open


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
        d.name = _opt_text(raw, "name", 40) or ("ask" if action == "ask" else d.kind)
    if action == "ask":
        answers = raw.get("answers")
        if answers is not None and not isinstance(answers, list):
            raise ValueError("answers must be a list of 1-3 button names")
        d.answers = check_buttons(answers)
        minutes = raw.get("timeout_minutes")
        if minutes is not None and (not isinstance(minutes, int) or isinstance(minutes, bool)
                                    or not 1 <= minutes <= 1440):
            raise ValueError("timeout_minutes must be a whole number 1..1440")
        d.timeout_minutes = minutes
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


@dataclass
class Applied:
    """What the apply hook printed: a state patch, a notification, an error."""
    state: dict | None = None
    notify: str | None = None
    kind: str = "info"
    error: str | None = None


def parse_applied(output: str) -> Applied:
    """apply's stdout -> Applied. Nothing printed means it had nothing to say; ValueError when it's not the
    contract: {"state": {...}, "notify": "text" or {"text", "kind"}, "error": "text"}, every key optional."""
    if len(output.encode()) > MAX_STATE:
        raise ValueError(f"apply printed more than {MAX_STATE // 1000} KB")
    if not output.strip():
        return Applied()
    try:
        data = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ValueError(f"apply did not print JSON: {exc}") from None
    if not isinstance(data, dict):
        raise ValueError("apply must print a JSON object")
    state = data.get("state")
    if state is not None and not isinstance(state, dict):
        raise ValueError("apply's state must be an object")
    out = Applied(state=state)
    notify = data.get("notify")
    if isinstance(notify, dict):
        out.kind = notify.get("kind", "info")
        notify = notify.get("text")
    if notify is not None:
        if not isinstance(notify, str) or out.kind not in NOTICE_KINDS:
            raise ValueError(f"apply's notify must be text or {{text, kind}} with kind one of {', '.join(NOTICE_KINDS)}")
        out.notify = " ".join(notify.split())[:MAX_TEXT] or None
    error = data.get("error")
    if error is not None:
        out.error = " ".join(str(error).split())[:500] or "apply reported an error"
    return out


async def run_select(scenario: Scenario, payload: dict, env: dict | None = None, log=None) -> str:
    return await run_hook(scenario, "select", payload, scenario.select_timeout, env=env, log=log)


async def run_hook(scenario: Scenario, what: str, payload: dict, timeout: int, env: dict | None = None,
                   log=None) -> str:
    """Run the scenario's `select` or `apply` with payload as JSON on stdin; its stdout. ValueError on a
    failure, a non-zero exit or the timeout (the process is killed). log: a file stderr goes to."""
    try:
        proc = await asyncio.create_subprocess_exec(
            str(Path(scenario.path) / what), cwd=scenario.path, env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
    except OSError as exc:
        raise ValueError(f"{what} could not start: {exc}") from None
    try:
        out, err = await asyncio.wait_for(proc.communicate(json.dumps(payload).encode()), timeout)
    except TimeoutError:
        _kill(proc)
        await proc.wait()
        raise ValueError(f"{what} took longer than {fmt_duration(timedelta(seconds=timeout))} and was stopped") from None
    except asyncio.CancelledError:
        _kill(proc)
        raise
    text = err.decode("utf-8", "replace")
    if log is not None and text.strip():
        log.write(text if text.endswith("\n") else text + "\n")
    if proc.returncode:
        tail = " ".join(text.strip().splitlines()[-3:])[:300]
        raise ValueError(f"{what} exited with {proc.returncode}" + (f": {tail}" if tail else ""))
    return out.decode("utf-8", "replace")


def _kill(proc) -> None:
    try:
        os.killpg(proc.pid, 9)
    except (ProcessLookupError, PermissionError, OSError):
        pass
