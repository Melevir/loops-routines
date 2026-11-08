import sys

import pytest
from conftest import write_routine

from loops_routines import cli, install
from loops_routines.core import config, paths
from loops_routines.install import daemon_program, launch_agent_plist, mcp_add_command, restart_launch_agent, write_home


def test_write_home_keeps_the_users_files():
    created = write_home()
    assert str(paths.config_path()) in created
    assert (paths.routines_dir() / "monitor-logs.md").is_file()
    paths.config_path().write_text("[daemon]\nport = 1234\n")
    (paths.routines_dir() / "cleanup.md").write_text("mine")
    assert write_home() == []
    assert (paths.routines_dir() / "cleanup.md").read_text() == "mine"
    assert config.load().daemon.port == 1234


def test_default_config_parses_cleanly():
    assert config.parse(config.DEFAULT_TOML).problems == ()


def test_config_problems_keep_defaults():
    cfg = config.parse('[daemon]\nport = "x"\nbogus = 1\n[profiles.p]\nmin_free = 500\n[other]\n')
    assert cfg.daemon.port == 47920
    assert len(cfg.problems) == 4
    assert cfg.profiles["p"].min_free == 0


def test_launch_agent_runs_the_daemon():
    plist = launch_agent_plist()
    assert plist["ProgramArguments"][-1] == "daemon"
    assert plist["KeepAlive"] is True
    assert plist["EnvironmentVariables"]["LOOPS_ROUTINES_HOME"]  # an isolated install stays isolated


def test_daemon_program_keeps_the_invoked_command_path(tmp_path, monkeypatch):
    # ~/.local/bin/loops-routines (uv tool) or /opt/homebrew/bin/loops-routines (brew) is a symlink into a
    # versioned environment: launchd must get the symlink, which survives upgrades, not its target
    target = tmp_path / "env" / "bin" / "loops-routines"
    target.parent.mkdir(parents=True)
    target.write_text("#!/bin/sh\n")
    link = tmp_path / "bin" / "loops-routines"
    link.parent.mkdir()
    link.symlink_to(target)
    monkeypatch.setattr(sys, "argv", [str(link), "install", "--launchd"])
    assert daemon_program() == [str(link), "daemon"]


def test_daemon_program_finds_the_command_on_path(tmp_path, monkeypatch):
    command = tmp_path / "loops-routines"
    command.write_text("#!/bin/sh\n")
    command.chmod(0o755)
    monkeypatch.setattr(sys, "argv", ["-m"])
    monkeypatch.setenv("PATH", str(tmp_path))
    assert daemon_program() == [str(command), "daemon"]


def test_daemon_program_falls_back_to_the_interpreter(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["-m"])
    monkeypatch.setenv("PATH", str(tmp_path))
    assert daemon_program() == [sys.executable, "-m", "loops_routines", "daemon"]


def test_restart_without_launch_agent(tmp_path, monkeypatch):
    # never the real ~/Library/LaunchAgents: that would restart the user's own daemon
    monkeypatch.setattr(install, "plist_path", lambda: tmp_path / "missing.plist")
    assert restart_launch_agent() == ["no LaunchAgent is installed: loops-routines install --launchd"]


def test_mcp_command_carries_the_token():
    command = mcp_add_command("tok")
    assert command[-1] == "Authorization: Bearer tok" and command[-3].endswith("/mcp")


def test_check_command(capsys):
    path = write_routine("daily", 'schedule = "0 9 * * *"')
    cli.main(["check", str(path)])
    out = capsys.readouterr().out
    assert out.startswith("daily: ok") and "next:" in out
    bad = write_routine("bad", 'schedule = "never"')
    with pytest.raises(SystemExit):
        cli.main(["check", str(bad)])


def test_cli_without_daemon(capsys, monkeypatch):
    paths.config_path().write_text("[daemon]\nport = 1\n")
    with pytest.raises(SystemExit):
        cli.main(["status"])
    assert "not running" in capsys.readouterr().err
