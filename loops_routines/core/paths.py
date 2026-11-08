"""Where loops-routines keeps things. Read from the environment on every call, so tests (and a second
instance) can point them elsewhere.

    LOOPS_ROUTINES_HOME   ~/.loops_routines                          yours: config.toml, routines/*.md
    LOOPS_ROUTINES_STATE  ~/Library/Application Support/loops_routines  the daemon's: state.db, token, runs/, reports/
    LOOPS_ROUTINES_LOGS   ~/Library/Logs/loops_routines              daemon.log, run-<id>.log
"""

import os
from pathlib import Path


def _dir(env: str, default: Path) -> Path:
    value = os.environ.get(env, "").strip()
    return Path(value).expanduser() if value else default


def home_dir() -> Path:
    return _dir("LOOPS_ROUTINES_HOME", Path.home() / ".loops_routines")


def state_dir() -> Path:
    return _dir("LOOPS_ROUTINES_STATE", Path.home() / "Library" / "Application Support" / "loops_routines")


def log_dir() -> Path:
    return _dir("LOOPS_ROUTINES_LOGS", Path.home() / "Library" / "Logs" / "loops_routines")


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
