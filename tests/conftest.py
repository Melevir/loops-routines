import json
import os
import stat
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import pytest

from loops_routines.core.config import Config, parse


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Every test gets its own ~/.loops_routines, state and logs."""
    home, state, logs = tmp_path / "home", tmp_path / "state", tmp_path / "logs"
    for d in (home, state, logs):
        d.mkdir()
    monkeypatch.setenv("LOOPS_ROUTINES_HOME", str(home))
    monkeypatch.setenv("LOOPS_ROUTINES_STATE", str(state))
    monkeypatch.setenv("LOOPS_ROUTINES_LOGS", str(logs))
    return tmp_path


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC) if "+" not in text else datetime.fromisoformat(text)


def cfg(text: str = "") -> Config:
    return parse('[daemon]\ntimezone = "UTC"\n' + text)


def routine_text(header: str, prompt: str = "Do the thing. Last: {last_run_at}") -> str:
    return f"+++\n{textwrap.dedent(header).strip()}\n+++\n{prompt}\n"


def write_routine(name: str, header: str, prompt: str = "Do the thing. Last: {last_run_at}") -> Path:
    folder = Path(os.environ["LOOPS_ROUTINES_HOME"]) / "routines"
    folder.mkdir(exist_ok=True)
    path = folder / f"{name}.md"
    path.write_text(routine_text(header, prompt))
    return path


FAKE_CLAUDE = r'''#!{python}
"""A stand-in for `claude -p --input-format stream-json`: reads the first user message, finds the result
path in it, and behaves as FAKE_MODE says."""
import json, os, re, sys, time
mode = os.environ.get("FAKE_MODE", "ok")
argv_log = os.environ.get("FAKE_ARGV")
if argv_log:
    open(argv_log, "w").write(json.dumps(sys.argv))
line = sys.stdin.readline()
prompt = json.loads(line)["message"]["content"]
result_path = re.search(r"write your result as JSON to (\S+)", prompt).group(1)
print(json.dumps({{"type": "system", "subtype": "init", "mcp_servers": []}}), flush=True)
print(json.dumps({{"type": "rate_limit_event", "rate_limit_info": {{"unifiedWindows": {{
    "five_hour": {{"utilization": 0.3, "resetsAt": int(time.time()) + 3600}},
    "seven_day": {{"utilization": 0.2, "resetsAt": int(time.time()) + 86400}}}}}}}}), flush=True)
print(json.dumps({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "id": "t1", "name": "Bash",
    "input": {{"command": "gh pr list"}}}}]}}}}), flush=True)
if mode == "sleep":
    time.sleep(30)
if mode == "fail":
    print("boom: not logged in", flush=True)
    sys.exit(1)
if mode == "message":
    second = sys.stdin.readline()
    text = json.loads(second)["message"]["content"] if second else ""
    open(result_path, "w").write(json.dumps({{"status": "ok", "summary": "got: " + text[:40], "report": text}}))
elif mode != "noresult":
    status = "alert" if mode == "alert" else "ok"
    result = {{"status": status, "summary": "all " + status, "report": "# Report\n" + prompt[:80]}}
    open(result_path, "w").write(json.dumps(result))
print(json.dumps({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": "Done here."}}]}}}}), flush=True)
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "num_turns": 2, "result": "Done here.",
    "total_cost_usd": 0.12, "modelUsage": {{"sonnet": {{"inputTokens": 10}}}}}}), flush=True)
# with stream-json input the CLI waits for more input until stdin closes
for _ in sys.stdin:
    pass
'''


class FakeNotifier:
    """Records banners; answers questions with `answer`, or waits (block) until the dialog is closed."""

    def __init__(self):
        self.banners, self.asks = [], []
        self.answer = None
        self.block = False

    async def banner(self, title, text, kind="info", sound=""):
        self.banners.append((title, text, kind, sound))

    async def ask(self, title, question, buttons, timeout_seconds, caution=False):
        import asyncio

        from loops_routines.core.notify import Answer
        self.asks.append((title, question, list(buttons), timeout_seconds))
        if self.block:
            await asyncio.sleep(3600)
        return self.answer or Answer("timed_out")


@pytest.fixture
def fake_claude(tmp_path) -> Path:
    path = tmp_path / "bin" / "claude"
    path.parent.mkdir()
    path.write_text(FAKE_CLAUDE.format(python=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def snapshot(five_used: float, minutes_to_reset: float, week_used: float = 10, age_minutes: float = 0,
             now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    from datetime import timedelta

    from loops_routines.core.clock import to_iso
    return {"captured_at": to_iso(now - timedelta(minutes=age_minutes)), "source": "probe",
            "five_hour": {"used_percentage": five_used, "resets_at": int(now.timestamp() + minutes_to_reset * 60)},
            "seven_day": {"used_percentage": week_used, "resets_at": int(now.timestamp() + 3 * 86400)}}


__all__ = ["utc", "cfg", "routine_text", "write_routine", "snapshot", "json", "FakeNotifier"]
