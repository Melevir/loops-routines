"""~/.claude_tab/config.toml: the daemon, the Claude account the agents run as, and limit profiles.

A wrong value keeps its default and is reported as a problem (shown by `claude-tab status` and the UI),
so one typo never stops the daemon.
"""

import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

from claude_tab.core import paths


@dataclass(frozen=True)
class Limits:
    """How much of the plan a routine may spend. All numbers are % of a window (0-100), except
    before_reset (minutes)."""
    min_free: float = 0  # start only with at least this much of the 5-hour window free
    before_reset: int = 0  # start only if the 5-hour window resets within this many minutes; 0 = any time
    keep_free: float = 5  # leave this much of the 5-hour window alone: reaching it winds the run down
    keep_free_weekly: float = 5  # the same for the 7-day window

    @property
    def needs_usage(self) -> bool:
        return bool(self.min_free or self.before_reset or self.keep_free or self.keep_free_weekly)


LIMIT_BOUNDS = {"min_free": (0, 100), "before_reset": (0, 300), "keep_free": (0, 100), "keep_free_weekly": (0, 100)}

# any: whenever it is due, with a small reserve; spare: only what the 5-hour window would lose anyway
BUILTIN_PROFILES = {
    "any": Limits(),
    "spare": Limits(min_free=60, before_reset=120, keep_free=20, keep_free_weekly=10),
    "unlimited": Limits(min_free=0, before_reset=0, keep_free=0, keep_free_weekly=0),
}


@dataclass(frozen=True)
class DaemonConfig:
    port: int = 47920
    max_parallel: int = 2  # routine runs at the same time
    tick_seconds: int = 30
    timezone: str = ""  # "" = the system's; cron lines and windows are read in it
    max_snapshot_age_minutes: int = 30  # older usage numbers are refreshed with a probe before deciding
    probe_cooldown_minutes: int = 10  # at most one probe this often
    wind_down_minutes: int = 5  # a run past its keep_free gets this long to finish, then it is stopped
    keep_runs_days: int = 60


@dataclass(frozen=True)
class NotifyConfig:
    """macOS notifications. Agents send them through the daemon (notify, ask_user); the daemon itself only
    for runs that broke and alerts an agent did not send."""
    enabled: bool = True
    info_sound: str = ""  # a name from /System/Library/Sounds, e.g. "Glass"; "" = silent
    error_sound: str = "Basso"
    ask_timeout_minutes: int = 15  # how long a question waits by default before the agent decides alone


@dataclass(frozen=True)
class AgentConfig:
    command: str = "claude"
    claude_config_dir: str = ""  # CLAUDE_CONFIG_DIR = which Claude account runs; "" = ~/.claude
    model: str = ""
    permission_mode: str = "dontAsk"
    # {workdir} is the run's own folder: file tools are confined to it (// = an absolute path)
    allowed_tools: tuple[str, ...] = (
        "Read(//{workdir}/**)", "Edit(//{workdir}/**)",
        "Bash(git *)", "Bash(ls *)", "Bash(cat *)", "Bash(wc *)", "Bash(date *)",
        "Bash(gh pr list *)", "Bash(gh pr view *)", "Bash(gh pr diff *)", "Bash(gh pr checks *)",
        "Bash(gh issue list *)", "Bash(gh issue view *)", "Bash(gh repo view *)", "Bash(gh search *)",
        "Bash(gh run list *)", "Bash(gh run view *)", "Bash(gh release list *)",
    )
    disallowed_tools: tuple[str, ...] = (
        "Bash(git push --force*)", "Bash(git push -f*)", "Bash(git push * main*)", "Bash(git push * master*)",
        "Bash(gh pr merge *)",
    )
    mcp_config_file: str = ""  # own MCP servers in .mcp.json format; a routine picks them by name

    def config_dir(self) -> str:
        return str(Path(self.claude_config_dir).expanduser()) if self.claude_config_dir.strip() else ""


@dataclass(frozen=True)
class Config:
    daemon: DaemonConfig = DaemonConfig()
    agent: AgentConfig = AgentConfig()
    notify: NotifyConfig = NotifyConfig()
    profiles: dict = field(default_factory=lambda: dict(BUILTIN_PROFILES))
    problems: tuple[str, ...] = ()


DEFAULT_TOML = """\
# claude_tab: read on every daemon tick, nothing to restart.

[daemon]
port = 47920
max_parallel = 2              # routine runs at the same time
tick_seconds = 30
timezone = ""                 # "" = the system's; cron lines and windows use it
max_snapshot_age_minutes = 30 # older usage numbers are refreshed with a one-request probe
probe_cooldown_minutes = 10
wind_down_minutes = 5         # a run that reaches its keep_free gets this long, then it is stopped

[notify]
# macOS notifications: agents send them (notify) and ask you questions with buttons (ask_user);
# the daemon itself reports runs that broke.
enabled = true
info_sound = ""               # e.g. "Glass" (/System/Library/Sounds); "" = silent
error_sound = "Basso"
ask_timeout_minutes = 15      # a question nobody answers: the agent decides on its own after this

[agent]
command = "claude"
claude_config_dir = ""        # e.g. "~/.claude_work": run the routines on another Claude account
model = ""
permission_mode = "dontAsk"
# allowed_tools / disallowed_tools replace the defaults; routines add their own on top
mcp_config_file = ""          # your own MCP servers (.mcp.json format)

# Limit profiles a routine picks with `profile = "<name>"` (built in: any, spare, unlimited).
# [profiles.night]
# min_free = 30
# before_reset = 0
# keep_free = 10
# keep_free_weekly = 10
"""


def _section(cls, raw: dict, name: str, problems: list[str]):
    """A TOML table -> a dataclass; unknown keys and wrong types keep the default, with a problem."""
    base = cls()
    if not isinstance(raw, dict):
        problems.append(f"[{name}] must be a table")
        return base
    known = {f.name: f for f in fields(cls)}
    values = {}
    for key, value in raw.items():
        if key not in known:
            problems.append(f"[{name}] unknown key {key!r}")
            continue
        default = getattr(base, key)
        if isinstance(default, bool):
            ok = isinstance(value, bool)
        elif isinstance(default, (int, float)):
            ok = isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
        elif isinstance(default, tuple):
            ok = isinstance(value, list) and all(isinstance(v, str) for v in value)
            value = tuple(value) if ok else value
        else:
            ok = isinstance(value, str)
        if not ok:
            problems.append(f"[{name}] {key}: {value!r} is not a valid {type(default).__name__}")
            continue
        values[key] = int(value) if isinstance(default, int) and not isinstance(default, bool) else value
    return replace(base, **values)


def limits_from(raw: dict, base: Limits, where: str, problems: list[str]) -> Limits:
    """Limit keys of a table (profile or routine) over a base; bad values keep the base's."""
    values = {}
    for key, (low, high) in LIMIT_BOUNDS.items():
        if key not in raw:
            continue
        value = raw[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
            problems.append(f"{where} {key}: expected a number {low}..{high}, got {value!r}")
            continue
        values[key] = int(value) if key == "before_reset" else float(value)
    limits = replace(base, **values)
    if limits.min_free and limits.min_free <= limits.keep_free:
        problems.append(f"{where}: min_free ({limits.min_free:g}) must be above keep_free ({limits.keep_free:g}), "
                        "or a run would be wound down as soon as it starts")
    return limits


def parse(text: str) -> Config:
    problems: list[str] = []
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return Config(problems=(f"config.toml: {exc}",))
    for key in raw:
        if key not in ("daemon", "agent", "notify", "profiles"):
            problems.append(f"config.toml: unknown table [{key}]")
    daemon = _section(DaemonConfig, raw.get("daemon", {}), "daemon", problems)
    agent = _section(AgentConfig, raw.get("agent", {}), "agent", problems)
    notify = _section(NotifyConfig, raw.get("notify", {}), "notify", problems)
    if not 1 <= notify.ask_timeout_minutes <= 120:
        problems.append("[notify] ask_timeout_minutes must be 1..120")
        notify = replace(notify, ask_timeout_minutes=NotifyConfig.ask_timeout_minutes)
    if daemon.max_parallel < 1:
        problems.append("[daemon] max_parallel must be at least 1")
        daemon = replace(daemon, max_parallel=1)
    if daemon.tick_seconds < 5:
        daemon = replace(daemon, tick_seconds=5)
    profiles = dict(BUILTIN_PROFILES)
    for name, table in (raw.get("profiles") or {}).items():
        if not isinstance(table, dict):
            problems.append(f"[profiles.{name}] must be a table")
            continue
        unknown = set(table) - set(LIMIT_BOUNDS)
        if unknown:
            problems.append(f"[profiles.{name}] unknown keys: {', '.join(sorted(unknown))}")
        profiles[name] = limits_from(table, profiles.get(name, Limits()), f"[profiles.{name}]", problems)
    return Config(daemon, agent, notify, profiles, tuple(problems))


def load(path: Path | None = None) -> Config:
    """The config file, or the defaults when there is none (install writes it)."""
    path = path or paths.config_path()
    try:
        return parse(path.read_text())
    except FileNotFoundError:
        return Config()
    except OSError as exc:
        return Config(problems=(f"{path}: {exc}",))
