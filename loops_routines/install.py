"""`loops-routines install`: ~/.loops_routines with the config and example routines (never overwriting yours),
the API token, and optionally the LaunchAgent that keeps the daemon running and the MCP registration."""

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from loops_routines.core import config, paths
from loops_routines.daemon.auth import ensure_token

DEFAULTS = Path(__file__).resolve().parent / "defaults"
LABEL = "com.loops-routines.daemon"
MCP_NAME = "loops_routines"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def write_home() -> list[str]:
    """config.toml, README.md and routines/ examples where missing; returns what was created."""
    created = []
    home = paths.home_dir()
    home.mkdir(parents=True, exist_ok=True)
    if not paths.config_path().exists():
        paths.config_path().write_text(config.DEFAULT_TOML)
        created.append(str(paths.config_path()))
    readme = home / "README.md"
    shutil.copyfile(DEFAULTS / "README.md", readme)  # documentation: always the current version
    paths.routines_dir().mkdir(exist_ok=True)
    for source in sorted((DEFAULTS / "routines").glob("*.md")):
        target = paths.routines_dir() / source.name
        if not target.exists():
            shutil.copyfile(source, target)
            created.append(str(target))
    return created


def daemon_program() -> list[str]:
    """How launchd starts the daemon: through the `loops-routines` command, not the interpreter behind it.

    The command's path stays put across upgrades (~/.local/bin/loops-routines for `uv tool`,
    /opt/homebrew/bin/loops-routines for brew), while `sys.executable` points into a versioned environment
    that an upgrade or cleanup removes. Symlinks are kept as they are, never resolved, for that reason.
    """
    invoked = Path(sys.argv[0])
    if invoked.name == "loops-routines" and invoked.is_file():
        return [str(invoked.absolute()), "daemon"]
    if found := shutil.which("loops-routines"):
        return [found, "daemon"]
    return [sys.executable, "-m", "loops_routines", "daemon"]


def launch_agent_plist() -> dict:
    log_dir = paths.log_dir()
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    for name in ("LOOPS_ROUTINES_HOME", "LOOPS_ROUTINES_STATE", "LOOPS_ROUTINES_LOGS"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    return {
        "Label": LABEL,
        "ProgramArguments": daemon_program(),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "ThrottleInterval": 10,
        "EnvironmentVariables": env,
        "StandardOutPath": str(log_dir / "daemon.log"),
        "StandardErrorPath": str(log_dir / "daemon.log"),
        "WorkingDirectory": str(paths.state_dir()),
    }


def install_launch_agent() -> list[str]:
    paths.log_dir().mkdir(parents=True, exist_ok=True)
    paths.state_dir().mkdir(parents=True, exist_ok=True)
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    if path.exists():  # reinstall: the new plist takes effect only after a bootout
        subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
    with path.open("wb") as f:
        plistlib.dump(launch_agent_plist(), f)
    done = subprocess.run(["launchctl", "bootstrap", domain, str(path)], capture_output=True, text=True)
    if done.returncode:
        raise RuntimeError(f"launchctl bootstrap failed: {done.stderr.strip() or done.stdout.strip()}")
    return [f"LaunchAgent {path} loaded: the daemon starts now and at every login"]


def restart_launch_agent() -> list[str]:
    """Restart the daemon so it runs the code just installed (after `uv tool upgrade` or `brew upgrade`)."""
    path = plist_path()
    if not path.exists():
        return ["no LaunchAgent is installed: loops-routines install --launchd"]
    domain = f"gui/{os.getuid()}"
    done = subprocess.run(["launchctl", "kickstart", "-k", f"{domain}/{LABEL}"], capture_output=True, text=True)
    if done.returncode:  # installed but not loaded (e.g. booted out by hand): load it again
        done = subprocess.run(["launchctl", "bootstrap", domain, str(path)], capture_output=True, text=True)
        if done.returncode:
            raise RuntimeError(f"launchctl bootstrap failed: {done.stderr.strip() or done.stdout.strip()}")
        return [f"LaunchAgent {path} loaded: the daemon is starting"]
    return ["the daemon is restarting"]


def uninstall_launch_agent() -> list[str]:
    path = plist_path()
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    if path.exists():
        path.unlink()
        return [f"LaunchAgent {path} removed"]
    return ["no LaunchAgent was installed"]


def mcp_add_command(token: str) -> list[str]:
    port = config.load().daemon.port
    return ["claude", "mcp", "add", "-s", "user", "--transport", "http", MCP_NAME,
            f"http://127.0.0.1:{port}/mcp", "--header", f"Authorization: Bearer {token}"]


def register_mcp(token: str) -> list[str]:
    command = mcp_add_command(token)
    subprocess.run(["claude", "mcp", "remove", "-s", "user", MCP_NAME], capture_output=True)
    done = subprocess.run(command, capture_output=True, text=True)
    if done.returncode:
        raise RuntimeError(f"claude mcp add failed: {done.stderr.strip() or done.stdout.strip()}")
    return [f"MCP server {MCP_NAME!r} added to Claude Code (user scope)"]


def install(launchd: bool = False, mcp: bool = False) -> list[str]:
    lines = [f"created {p}" for p in write_home()] or ["~/.loops_routines: your files are kept"]
    token = ensure_token()
    lines.append(f"API token: {paths.token_path()}")
    if launchd:
        lines += install_launch_agent()
    else:
        lines.append("run the daemon now with `loops-routines daemon`, or keep it running: loops-routines install --launchd")
    if mcp:
        lines += register_mcp(token)
    else:
        lines.append("let Claude Code see the routines: loops-routines install --mcp")
    return lines
