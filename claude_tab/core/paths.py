"""Where claude_tab keeps things. Read from the environment on every call, so tests (and a second
instance) can point them elsewhere.

    CLAUDE_TAB_HOME   ~/.claude_tab                          yours: config.toml, routines/*.md
    CLAUDE_TAB_STATE  ~/Library/Application Support/claude_tab  the daemon's: state.db, token, runs/, reports/
    CLAUDE_TAB_LOGS   ~/Library/Logs/claude_tab              daemon.log, run-<id>.log
"""

import os
from pathlib import Path


def _dir(env: str, default: Path) -> Path:
    value = os.environ.get(env, "").strip()
    return Path(value).expanduser() if value else default


def home_dir() -> Path:
    return _dir("CLAUDE_TAB_HOME", Path.home() / ".claude_tab")


def state_dir() -> Path:
    return _dir("CLAUDE_TAB_STATE", Path.home() / "Library" / "Application Support" / "claude_tab")


def log_dir() -> Path:
    return _dir("CLAUDE_TAB_LOGS", Path.home() / "Library" / "Logs" / "claude_tab")


def config_path() -> Path:
    return home_dir() / "config.toml"


def routines_dir() -> Path:
    return home_dir() / "routines"


def db_path() -> Path:
    return state_dir() / "state.db"


def token_path() -> Path:
    return state_dir() / "token"


def runs_dir() -> Path:
    return state_dir() / "runs"


def reports_dir() -> Path:
    return state_dir() / "reports"
