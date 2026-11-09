"""Routines: ~/.loops_routines/routines/<name>.md, a TOML header between +++ lines and the prompt below.

    +++
    title = "Log monitor"            # the name its messages show under (default: the file name)
    schedule = "every 30m"           # cron ("0 9 * * mon"), @daily, or "every 30m"
    grace = "20m"                    # how long an occurrence may wait for its limits; default: until the next one
    window = "mon-fri 08:00-20:00"   # only start inside it; default * (any time)
    profile = "any"                  # limit profile (config.toml [profiles]); min_free, before_reset,
    keep_free = 10                   #   keep_free, keep_free_weekly override it here
    priority = 80                    # higher starts first when several are due; default 50
    model = "sonnet"
    effort = "low"
    max_turns = 30
    max_runtime = "10m"
    repos = ["owner/name"]           # cloned (shallow) into the run folder
    mcp_servers = ["plugin:observability:kibana"]
    plugins = []                     # installed Claude Code plugins (skills, commands)
    skills = []                      # standalone skills from ~/.claude/skills
    allowed_tools = []               # on top of [agent] allowed_tools
    disallowed_tools = []
    notify = "on_alert"              # always | on_alert | never
    enabled = true
    +++
    Check the production logs of ... since {last_run_at}. The previous report: {previous_report}

Placeholders: {name} {title} {run_id} {now} {last_run_at} {previous_status} {previous_summary}
{previous_report} {repos}. Any other {...} stays as written.
"""

import re
import tomllib
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from loops_routines.core.config import BUILTIN_PROFILES, LIMIT_BOUNDS, Config, Limits, limits_from
from loops_routines.core.triggers import Trigger, parse_duration, parse_trigger
from loops_routines.core.windows import parse_window

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
TITLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _#·.:-]{0,47}$")
REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
NOTIFY = ("always", "on_alert", "never")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_PRIORITY = 50
DEFAULT_MAX_TURNS = 50
DEFAULT_MAX_RUNTIME = timedelta(minutes=30)
PLACEHOLDERS = ("name", "title", "run_id", "now", "last_run_at", "previous_status", "previous_summary",
                "previous_report", "repos")
LIST_KEYS = ("repos", "mcp_servers", "plugins", "skills", "allowed_tools", "disallowed_tools")
KNOWN_KEYS = {"title", "schedule", "grace", "window", "profile", "priority", "model", "effort", "max_turns",
              "max_runtime", "notify", "enabled", *LIST_KEYS, *LIMIT_BOUNDS}
SCHEDULE_KEYS = {"schedule", "grace", "window"}
STEP_KEYS = KNOWN_KEYS - SCHEDULE_KEYS  # a scenario's step: a routine the scenario starts, not a schedule


@dataclass(frozen=True)
class Routine:
    name: str
    title: str
    schedule: str
    trigger: Trigger | None  # None for a scenario's step
    prompt: str
    limits: Limits = Limits()
    profile: str = "any"
    grace: timedelta | None = None  # None: an occurrence waits until the next one replaces it
    window: str = "*"
    priority: int = DEFAULT_PRIORITY
    model: str = ""
    effort: str = ""
    max_turns: int = DEFAULT_MAX_TURNS
    max_runtime: timedelta = DEFAULT_MAX_RUNTIME
    repos: tuple[str, ...] = ()
    mcp_servers: tuple[str, ...] = ()
    plugins: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    notify: str = "on_alert"
    enabled: bool = True
    path: str = ""
    problems: tuple[str, ...] = ()  # non-fatal: a value that kept its default


@dataclass(frozen=True)
class Broken:
    """A routine file that can't be used, and why (shown in the UI, never run)."""
    name: str
    path: str
    error: str


def split_front_matter(text: str) -> tuple[str, str]:
    """'+++\\n<toml>\\n+++\\n<body>' -> (toml, body); ValueError without the header."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "+++":
        raise ValueError("must start with a +++ line, then the TOML header, then another +++ line")
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "+++":
            return "".join(lines[1:i]), "".join(lines[i + 1:])
    raise ValueError("the TOML header has no closing +++ line")


def _strings(raw, key: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(v, str) and v.strip() for v in raw):
        raise ValueError(f"{key} must be a list of strings")
    return tuple(v.strip() for v in raw)


def parse_routine(name: str, text: str, cfg: Config, path: str = "") -> Routine:
    """One routine file -> Routine. ValueError for anything that makes it unusable."""
    if not NAME_RE.match(name):
        raise ValueError(f"file name {name!r}: use lowercase letters, digits, - and _ (e.g. monitor-logs.md)")
    raw, prompt = read_header(text, KNOWN_KEYS)
    if "schedule" not in raw:
        raise ValueError("schedule is missing: a cron line ('0 9 * * mon'), @daily, or 'every 30m'")
    return build_routine(name, raw, prompt, cfg, path)


def read_header(text: str, known: set[str]) -> tuple[dict, str]:
    """A routine-style file -> (its TOML header, its prompt)."""
    header, body = split_front_matter(text)
    try:
        raw = tomllib.loads(header)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"header: {exc}") from None
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"unknown keys: {', '.join(sorted(unknown))} (known: {', '.join(sorted(known))})")
    prompt = body.strip()
    if not prompt:
        raise ValueError("the prompt (the text after the header) is empty")
    return raw, prompt


def build_routine(name: str, raw: dict, prompt: str, cfg: Config, path: str = "", *, title: str = "") -> Routine:
    """A checked header and prompt -> Routine. Without `schedule` it is a scenario's step: no trigger, and
    `title` is its default title (not held to TITLE_RE, it comes from the scenario)."""
    trigger = parse_trigger(str(raw["schedule"]), cfg.daemon.timezone) if "schedule" in raw else None
    title = " ".join(str(raw.get("title", title or name)).split())
    if trigger is None:
        if not title or len(title) > 80:
            raise ValueError("title must be 1..80 characters")
    elif not TITLE_RE.match(title):
        raise ValueError(f"title {title!r}: letters, digits, spaces and - _ # · . : only, up to 48 characters")
    window = str(raw.get("window", "*"))
    parse_window(window)
    grace = parse_duration(raw["grace"]) if "grace" in raw else None
    max_runtime = parse_duration(raw["max_runtime"]) if "max_runtime" in raw else DEFAULT_MAX_RUNTIME
    if max_runtime < timedelta(minutes=1):
        raise ValueError("max_runtime must be at least 1m")

    profile = str(raw.get("profile", "any"))
    if profile not in cfg.profiles:
        raise ValueError(f"profile {profile!r} is not defined (known: {', '.join(sorted(cfg.profiles))})")
    problems: list[str] = []
    limits = limits_from(raw, cfg.profiles[profile], "", problems)

    def integer(key: str, default: int, low: int, high: int) -> int:
        value = raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"{key} must be a whole number {low}..{high}, got {value!r}")
        return value

    notify = str(raw.get("notify", "on_alert"))
    if notify not in NOTIFY:
        raise ValueError(f"notify must be one of {', '.join(NOTIFY)}")
    effort = str(raw.get("effort", ""))
    if effort and effort not in EFFORTS:
        raise ValueError(f"effort must be one of {', '.join(EFFORTS)}")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be true or false")
    lists = {key: _strings(raw.get(key), key) for key in LIST_KEYS}
    for repo in lists["repos"]:
        if not REPO_RE.match(repo):
            raise ValueError(f"repos: {repo!r} is not owner/name")
    return Routine(
        name=name, title=title, schedule=str(raw.get("schedule", "")).strip(), trigger=trigger, prompt=prompt,
        limits=limits, profile=profile, grace=grace, window=window,
        priority=integer("priority", DEFAULT_PRIORITY, 0, 100), model=str(raw.get("model", "")), effort=effort,
        max_turns=integer("max_turns", DEFAULT_MAX_TURNS, 1, 500), max_runtime=max_runtime,
        notify=notify, enabled=enabled, path=path, problems=tuple(p.strip() for p in problems), **lists,
    )


def load_routines(folder: Path, cfg: Config) -> tuple[dict[str, Routine], dict[str, Broken]]:
    """Every routines/*.md -> (usable by name, broken by name)."""
    good: dict[str, Routine] = {}
    broken: dict[str, Broken] = {}
    if not folder.is_dir():
        return good, broken
    for path in sorted(folder.glob("*.md")):
        if path.name.startswith(".") or path.name.lower() == "readme.md":
            continue
        name = path.stem
        try:
            good[name] = parse_routine(name, path.read_text(encoding="utf-8"), cfg, str(path))
        except (ValueError, OSError, UnicodeDecodeError) as exc:
            broken[name] = Broken(name, str(path), str(exc))
    return good, broken


def fingerprint(folder: Path) -> tuple:
    """Changes when a routine file is added, removed or edited: cheap enough for every tick."""
    if not folder.is_dir():
        return ()
    items = []
    for path in sorted(folder.glob("*.md")):
        try:
            stat = path.stat()
        except OSError:
            continue
        items.append((path.name, stat.st_mtime_ns, stat.st_size))
    return tuple(items)


def fill(template: str, **values) -> str:
    """Fill only the known placeholders; any other {...} in the prompt stays as written."""
    def replace(match: re.Match) -> str:
        key = match.group(1)
        return str(values[key]) if key in values else match.group(0)
    return re.sub(r"\{(\w+)\}", replace, template)


__all__ = ["Routine", "Broken", "parse_routine", "read_header", "build_routine", "load_routines", "fingerprint",
           "fill", "BUILTIN_PROFILES", "STEP_KEYS"]
