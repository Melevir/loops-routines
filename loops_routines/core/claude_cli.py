"""Starting `claude` from a daemon: the environment and the command. (Taken from Loops' agent_runner.py.)"""

import os
import shutil
from pathlib import Path

from loops_routines.core.config import AgentConfig
from loops_routines.core.integrations import CLAUDE_HOME

# Set by a Claude Code session for its own child processes. If the daemon was started from inside a
# session (e.g. `loops-routines daemon` typed in Claude's terminal), passing them on would make the agent look
# like part of that session, or hand it the session's messaging socket and token.
SESSION_ENV = {
    "CLAUDECODE", "CLAUDE_PID", "CLAUDE_JOB_DIR", "CLAUDE_EFFORT", "CLAUDE_AGENTS_AUTO_RELAUNCHED_AT",
    "CLAUDE_CODE_AGENT", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_SSE_PORT",
}
# Credentials that outrank a CLAUDE_CONFIG_DIR login; dropped whenever the account is picked, or the
# agent would silently run on someone else's.
COMPETING_AUTH_ENV = {
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
}
# launchd does not give the login shell's PATH; look where claude and gh are usually installed too.
EXTRA_PATH = [str(Path.home() / ".local" / "bin"), str(Path.home() / ".claude" / "local"), "/opt/homebrew/bin",
              "/usr/local/bin"]


def agent_env(cfg: AgentConfig | None = None) -> dict[str, str]:
    """Environment for the claude processes the daemon starts. With cfg, the account is pinned to
    cfg.claude_config_dir (or the default one: an inherited CLAUDE_CONFIG_DIR can't switch it)."""
    env = {k: v for k, v in os.environ.items() if k not in SESSION_ENV}
    env["PATH"] = os.pathsep.join([p for p in [env.get("PATH", ""), *EXTRA_PATH] if p])
    if cfg is not None:
        env.pop("CLAUDE_CONFIG_DIR", None)
        if cfg.config_dir():
            env["CLAUDE_CONFIG_DIR"] = cfg.config_dir()
            for name in COMPETING_AUTH_ENV:
                env.pop(name, None)
    return env


def resolve_command(command: str) -> str:
    found = shutil.which(os.path.expanduser(command), path=agent_env()["PATH"])
    if not found:
        raise RuntimeError(f"command {command!r} not found (set [agent] command to a full path)")
    return found


def account_home(cfg: AgentConfig) -> Path:
    """The agent account's Claude folder (CLAUDE_CONFIG_DIR, or ~/.claude)."""
    return Path(cfg.config_dir()) if cfg.config_dir() else CLAUDE_HOME


def account_problem(cfg: AgentConfig) -> str | None:
    """Why the configured account can't be used, or None."""
    if cfg.claude_config_dir.strip() and not Path(cfg.config_dir()).is_dir():
        return (f"Claude account folder {cfg.claude_config_dir} does not exist: "
                f"log in with CLAUDE_CONFIG_DIR={cfg.claude_config_dir} claude")
    return None
