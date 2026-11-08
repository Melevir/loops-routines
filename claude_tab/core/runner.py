"""One headless Claude Code run of a routine (`claude -p`, stream-json in and out).

A run is a batch job, not a service: no restart on failure, a hard runtime limit, everything it prints
kept in a log file. Each run has its own folder (state/runs/<id>/): the MCP config, the settings file,
the work folder the agent's file tools are confined to (with the routine's repositories cloned into it),
and the result file it leaves. Nothing is shared between runs, so several can go at once.
(The process handling is taken from Loops' agent_runner.py.)

The agent ends by writing {"status", "summary", "report"} to its result file; the daemon decides from it
what the user hears. While it works, user messages reach it through stdin.
"""

import asyncio
import json
import os
import signal
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from claude_tab.core import notify
from claude_tab.core.claude_cli import account_home, agent_env, resolve_command
from claude_tab.core.clock import utc_now
from claude_tab.core.config import AgentConfig
from claude_tab.core.integrations import RunSetup, describe, plan_run
from claude_tab.core.routines import Routine
from claude_tab.core.usage import snapshot_from_rate_limit_event

AGENT_SERVER = "claude_tab"  # the daemon's MCP server for agents, as the run's MCP config names it
AGENT_TOOLS = [f"mcp__{AGENT_SERVER}__{name}" for name in ("report_status", "report_blocker", "notify", "ask_user")]
RESULT_FILE = "result.json"
STATUSES = ("ok", "alert", "nothing")
MAX_SUMMARY = 300
STOP_GRACE_SECONDS = 5
# With stream-json input the CLI waits for more input after each `result`. A message queued while it
# worked starts a new turn right after; if none starts within this grace period, stdin is closed and
# the session ends.
END_GRACE_SECONDS = 5
SEND_GRACE_SECONDS = 30

CONTRACT = """

---
You are running unattended as the claude_tab routine "{title}" (run {run_id}); nobody watches this
session. When you are done, write your result as JSON to {result_path} (and nothing else there):

{{"status": "ok | alert | nothing", "summary": "one line, at most 200 characters", "report": "Markdown: the full result"}}

- status: alert - something a person should look at now (it reaches the user as a notification);
  ok - done, nothing alarming; nothing - there was nothing to do this time.
- summary: the one line the user sees first. report: everything else, as Markdown.

While you work you may post short progress lines with the `report_status` tool of the `{server}` MCP
server (use a `key` for a step that is still running and send its end state with the same key).
{notify_text}
When your environment keeps you from the goal (an MCP server not signed in or down, a missing plugin,
skill, CLI or credentials, a denied tool call), call `report_blocker` with a concrete report instead of
guessing.
Write every shell command on one line: a command split with `\\` does not match the permission rules."""

WRAP_UP = """claude_tab: {reason}. You have {minutes} minutes left: start nothing new, write your result to
{result_path} with what you have (say in the report what is unfinished), then stop."""

USER_MESSAGE = """A message from the user, sent through claude_tab while you work:

{text}

Take it into account; acknowledge it with report_status, then carry on."""


@dataclass
class RunSpec:
    run_id: int
    routine: Routine
    agent: AgentConfig
    prompt: str  # the routine's prompt with its placeholders filled
    run_dir: Path  # state/runs/<id>
    log_path: Path
    agent_url: str  # the daemon's /agent/mcp
    agent_token: str  # this run's own bearer token for it
    listed: dict | None = None  # the last `claude mcp list` of the account (connector URLs)

    @property
    def workdir(self) -> Path:
        return self.run_dir / "work"

    @property
    def result_path(self) -> Path:
        return self.workdir / RESULT_FILE


class RunResult(NamedTuple):
    exit_code: int | None
    outcome: str  # ok | failed | timeout | stopped
    error: str | None = None
    denied: tuple[str, ...] = ()  # tool calls the permission rules refused
    usage: dict | None = None  # {cost_usd, turns, api_ms, models} from the last result event
    status: str = "unknown"  # the agent's: ok | alert | nothing | unknown (no usable result file)
    summary: str = ""
    report: str = ""
    unavailable: tuple[str, ...] = ()  # integrations the routine asked for that did not work


def describe_event(event: dict) -> str | None:
    """One short line about a stream-json event, for the live status."""
    kind = event.get("type")
    if kind == "assistant":
        for block in event.get("message", {}).get("content", []):
            if block.get("type") == "tool_use":
                args = block.get("input") or {}
                detail = args.get("command") or args.get("file_path") or args.get("pattern") or ""
                return f"{block.get('name')}: {detail}"[:160].rstrip(": ")
            if block.get("type") == "text" and block.get("text", "").strip():
                return block["text"].strip().splitlines()[0][:160]
    if kind == "result":
        return f"result: {event.get('subtype')} after {event.get('num_turns')} turns"
    if kind == "system" and event.get("subtype") == "api_retry":
        return f"api retry: {event.get('error')}"
    return None


class RunTracker:
    """What a run did, read from its stream-json events."""

    def __init__(self, selected: list[str] | tuple[str, ...] = ()) -> None:
        self.error: str | None = None
        self.denied: list[str] = []
        self.usage: dict | None = None
        self.last_text: str = ""  # the agent's last words: the summary when it wrote no result file
        self.selected = list(selected)
        self.unavailable: list[str] = []
        self._calls: dict[str, str] = {}

    def feed(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "result":
            self.error = str(event.get("result") or event.get("subtype") or "failed")[:300] \
                if event.get("is_error") else None
            models = event.get("modelUsage")
            self.usage = {"cost_usd": event.get("total_cost_usd"), "turns": event.get("num_turns"),
                          "api_ms": event.get("duration_api_ms"),
                          "models": {n: m for n, m in models.items() if isinstance(m, dict)}
                          if isinstance(models, dict) else {}}
            if not event.get("is_error") and isinstance(event.get("result"), str) and event["result"].strip():
                self.last_text = event["result"].strip()
        elif kind == "system" and event.get("subtype") == "init" and self.selected:
            statuses = {s.get("name"): s.get("status") for s in event.get("mcp_servers") or [] if isinstance(s, dict)}
            for name in self.selected:
                status = statuses.get(name)
                if status in ("failed", "needs-auth") and not any(u.startswith(f"{name}:") for u in self.unavailable):
                    self.unavailable.append(f"{name}: {status}")
            self.selected = []
        elif kind == "system" and event.get("subtype") == "permission_denied":
            call = self._calls.get(event.get("tool_use_id")) or str(event.get("tool_name") or "a tool")
            if call not in self.denied:
                self.denied.append(call)
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                args = block.get("input") or {}
                detail = args.get("command") or args.get("file_path") or args.get("pattern") or ""
                self._calls[block.get("id")] = f"{block.get('name')}: {detail}"[:160].rstrip(": ")
            elif block.get("type") == "text" and kind == "assistant" and block.get("text", "").strip():
                self.last_text = block["text"].strip()


def read_result(path: Path) -> tuple[dict | None, str | None]:
    """The agent's result file -> ({status, summary, report}, problem)."""
    if not path.is_file():
        return None, f"the agent wrote no {path.name}"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"{path.name} is not valid JSON: {exc}"
    if not isinstance(raw, dict):
        return None, f"{path.name} is not a JSON object"
    status = str(raw.get("status", "")).strip().lower()
    if status not in STATUSES:
        return None, f"{path.name}: status must be one of {', '.join(STATUSES)}, got {status!r}"
    summary = " ".join(str(raw.get("summary") or "").split())
    if len(summary) > MAX_SUMMARY:
        summary = summary[:MAX_SUMMARY - 1].rstrip() + "…"
    return {"status": status, "summary": summary, "report": str(raw.get("report") or "").strip()}, None


async def gh_clone(repo: str, dest: Path, env: dict) -> None:
    proc = await asyncio.create_subprocess_exec(
        resolve_command("gh"), "repo", "clone", repo, str(dest), "--", "--depth", "50",
        env=env, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await proc.communicate()
    if proc.returncode:
        raise RuntimeError(f"gh repo clone {repo} failed: {output.decode(errors='replace').strip()[-300:]}")


def write_mcp_config(spec: RunSpec, extra: dict) -> Path:
    path = spec.run_dir / "mcp.json"
    servers = {**extra, AGENT_SERVER: {"type": "http", "url": spec.agent_url,
                                       "headers": {"Authorization": f"Bearer {spec.agent_token}"}}}
    path.write_text(json.dumps({"mcpServers": servers}, indent=2))
    os.chmod(path, 0o600)  # it holds the run's token
    return path


def build_argv(spec: RunSpec, setup: RunSetup, mcp_config: Path) -> list[str]:
    routine, agent = spec.routine, spec.agent
    rule_path = str(spec.workdir).lstrip("/")  # permission rules spell absolute paths as //Users/...
    allowed = [rule.replace("{workdir}", rule_path) for rule in [*agent.allowed_tools, *routine.allowed_tools]]
    allowed += AGENT_TOOLS + setup.allowed
    argv = [
        resolve_command(agent.command), "-p", "--input-format", "stream-json",
        "--output-format", "stream-json", "--verbose",  # stream-json in print mode requires --verbose
        # no --strict-mcp-config: it would also drop plugin servers and connectors; allowedMcpServers in
        # the run's --settings file decides which servers load (claude_tab's own is always on it)
        "--mcp-config", str(mcp_config), *setup.argv,
        "--permission-mode", agent.permission_mode,
        "--permission-prompts", "none",  # nobody is there to answer
        "--allowedTools", ",".join(allowed),
        "--max-turns", str(routine.max_turns),
        "--name", f"claude-tab {routine.name} #{spec.run_id}",
        # the user's own settings stay out: their hooks rewrite commands (breaking the permission
        # match), their plugins and output style are noise for an unattended agent. Auth is unaffected.
        "--setting-sources", "project,local",
    ]
    disallowed = [*agent.disallowed_tools, *routine.disallowed_tools]
    if disallowed:
        argv += ["--disallowedTools", ",".join(disallowed)]
    model = routine.model or agent.model
    if model:
        argv += ["--model", model]
    if routine.effort:
        argv += ["--effort", routine.effort]
    return argv


def user_line(text: str) -> bytes:
    """One stream-json input message."""
    return (json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n").encode()


class Run:
    """One run's process. Its methods are called on the daemon's event loop."""

    def __init__(self, spec: RunSpec, on_event: Callable[[str], None] | None = None,
                 on_usage: Callable[[dict], None] | None = None, *, end_grace: float = END_GRACE_SECONDS,
                 clone=gh_clone):
        self.spec = spec
        self._on_event = on_event or (lambda summary: None)
        self._on_usage = on_usage or (lambda snapshot: None)
        self._end_grace = end_grace
        self._clone = clone
        self.proc: asyncio.subprocess.Process | None = None
        self.last_event = "starting"
        self._stop_requested = False
        self._end_timer: asyncio.TimerHandle | None = None
        self._wrap_reason: str | None = None
        self._wrap_timer: asyncio.TimerHandle | None = None

    # --- control -----------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    @property
    def accepts_messages(self) -> bool:
        return self.running and self.proc.stdin is not None and not self.proc.stdin.is_closing()

    @property
    def wrapping_up(self) -> str | None:
        return self._wrap_reason

    def send_message(self, text: str) -> None:
        """Deliver a user message; RuntimeError if the run can no longer take one."""
        if not text.strip():
            raise ValueError("message is empty")
        if not self.accepts_messages:
            raise RuntimeError("the run is finishing and can't take messages now")
        self._write(user_line(USER_MESSAGE.format(text=text.strip())))
        if self._end_timer:  # between turns: give the new turn time to start
            self._schedule_end(max(SEND_GRACE_SECONDS, self._end_grace))

    def wrap_up(self, reason: str, grace_seconds: float) -> bool:
        """Ask the agent to finish and write its result; stop it after the grace period. Once per run."""
        if self._wrap_reason is not None or not self.running:
            return False
        self._wrap_reason = reason
        if self.accepts_messages:
            try:
                self._write(user_line(WRAP_UP.format(reason=reason, minutes=max(1, round(grace_seconds / 60)),
                                                     result_path=self.spec.result_path)))
                if self._end_timer:
                    self._schedule_end(max(SEND_GRACE_SECONDS, self._end_grace))
            except RuntimeError:
                grace_seconds = 0
        self._wrap_timer = asyncio.get_running_loop().call_later(grace_seconds, self.stop)
        return True

    def stop(self) -> None:
        """SIGTERM to the run's process group now, SIGKILL if it is still there after a grace period.
        Before the process exists (cloning, setting up) it simply never starts."""
        self._stop_requested = True
        if self.running:
            self._signal(signal.SIGTERM)
            asyncio.get_running_loop().call_later(
                STOP_GRACE_SECONDS, lambda: self._signal(signal.SIGKILL) if self.running else None)

    def _signal(self, sig: int) -> None:
        try:
            os.killpg(self.proc.pid, sig)  # claude plus whatever it started
        except (ProcessLookupError, PermissionError):
            pass

    def _write(self, data: bytes) -> None:
        try:
            self.proc.stdin.write(data)
        except (BrokenPipeError, ConnectionResetError, RuntimeError) as exc:
            raise RuntimeError(f"could not reach the agent: {exc}") from None

    def _schedule_end(self, delay: float) -> None:
        self._cancel_end()
        self._end_timer = asyncio.get_running_loop().call_later(delay, self._end_input)

    def _cancel_end(self) -> None:
        if self._end_timer:
            self._end_timer.cancel()
            self._end_timer = None

    def _end_input(self) -> None:
        self._end_timer = None
        if self.accepts_messages:
            self.proc.stdin.close()

    def _event(self, summary: str) -> None:
        self.last_event = summary
        try:
            self._on_event(summary)
        except Exception:
            pass

    # --- the run -----------------------------------------------------------------

    async def prepare(self) -> tuple[list[str], str, RunSetup, Path]:
        """Folders, clones, config files -> (argv, prompt, setup, cwd)."""
        spec = self.spec
        spec.workdir.mkdir(parents=True, exist_ok=True)
        env = agent_env(spec.agent)
        clones = []
        for repo in spec.routine.repos:
            self._event(f"cloning {repo}")
            dest = spec.workdir / repo.split("/")[-1]
            if dest.exists():
                dest = spec.workdir / repo.replace("/", "__")
            await self._clone(repo, dest, env)
            clones.append(dest)
        cwd = clones[0] if len(clones) == 1 else spec.workdir
        setup = plan_run(plugins_wanted=spec.routine.plugins, servers_wanted=spec.routine.mcp_servers,
                         skills_wanted=spec.routine.skills, mcp_config_file=spec.agent.mcp_config_file,
                         agent_url=spec.agent_url, listed=spec.listed, account_home=account_home(spec.agent),
                         out_dir=spec.run_dir)
        argv = build_argv(spec, setup, write_mcp_config(spec, setup.mcp_servers))
        prompt = spec.prompt + describe(setup)
        if clones:
            prompt += "\n\nRepositories cloned for this run: " + ", ".join(
                f"{repo} in {path}" for repo, path in zip(spec.routine.repos, clones, strict=False))
        if setup.problems:
            prompt += ("\n\nThese integrations the routine asked for can't be used in this run: "
                       + "; ".join(setup.problems) + ". Say so in your report if it matters.")
        notify_text = notify.instructions(spec.routine.notify)
        prompt += CONTRACT.format(title=spec.routine.title, run_id=spec.run_id, result_path=spec.result_path,
                                  server=AGENT_SERVER, notify_text=notify_text)
        return argv, prompt, setup, cwd

    async def execute(self) -> RunResult:
        spec = self.spec
        spec.log_path.parent.mkdir(parents=True, exist_ok=True)
        tracker = RunTracker()
        with spec.log_path.open("a", encoding="utf-8") as log:
            try:
                argv, prompt, setup, cwd = await self.prepare()
                tracker.selected = list(setup.servers)
                tracker.unavailable = list(setup.problems)
                log.write(f"--- {utc_now()} run {spec.run_id} ({spec.routine.name}) in {cwd}\n$ {' '.join(argv)}\n")
                log.write(f"> {json.dumps(prompt)}\n")
                if self._stop_requested:
                    log.write("--- claude_tab: stopped before it started\n")
                    self._event("finished: stopped")
                    return RunResult(None, "stopped", error="stopped before it started")
                self.proc = await asyncio.create_subprocess_exec(
                    *argv, cwd=cwd, env=agent_env(spec.agent), stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    limit=16 * 1024 * 1024,  # stream-json lines can be long
                    start_new_session=True,  # its own process group: stop and timeout reach its children
                )
            except Exception as exc:
                log.write(f"failed to start: {exc}\n")
                self._event(f"failed to start: {exc}")
                return RunResult(None, "stopped" if self._stop_requested else "failed",
                                 error=f"failed to start: {exc}"[:300])
            if self._stop_requested:  # Stop came while the process was being started
                self._signal(signal.SIGTERM)
            try:
                self._write(user_line(prompt))
            except RuntimeError as exc:  # exited at once; its output says why
                log.write(f"--- claude_tab: {exc}\n")
            self._event("started")
            last_plain = None

            async def pump():
                # Keep draining stdout until EOF whatever happens: a full pipe would block the agent.
                nonlocal last_plain
                async for raw in self.proc.stdout:
                    line = raw.decode("utf-8", "replace").rstrip("\n")
                    log.write(line + "\n")
                    log.flush()
                    try:
                        event = json.loads(line)
                    except ValueError:
                        if line.strip():
                            last_plain = line.strip()[:300]
                            self._event(line.strip()[:160])
                        continue
                    if not isinstance(event, dict):
                        continue
                    try:
                        if event.get("type") == "result":
                            self._schedule_end(self._end_grace)
                        elif event.get("type") == "system" and event.get("subtype") == "init":
                            self._cancel_end()  # a queued message started a new turn
                        tracker.feed(event)
                        snapshot = snapshot_from_rate_limit_event(event, utc_now(), "agent")
                        if snapshot:
                            self._on_usage(snapshot)
                        summary = describe_event(event)
                    except Exception as exc:
                        log.write(f"--- claude_tab: could not read this event: {exc!r}\n")
                        summary = None
                    if summary:
                        self._event(summary)

            pumping = asyncio.create_task(pump())
            timed_out = False
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=spec.routine.max_runtime.total_seconds())
            except TimeoutError:
                timed_out = True
                self._signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(self.proc.wait(), STOP_GRACE_SECONDS)
                except TimeoutError:
                    self._signal(signal.SIGKILL)
                    await self.proc.wait()
            await pumping
            self._cancel_end()
            if self._wrap_timer:
                self._wrap_timer.cancel()
            code = self.proc.returncode
            if timed_out:
                outcome = "timeout"
            elif self._stop_requested:
                outcome = "stopped"
            elif code != 0:
                outcome = "failed"
            else:
                outcome = "ok"
            error = tracker.error or (last_plain if outcome == "failed" else None)
            if timed_out:
                error = f"ran out of time (max_runtime {spec.routine.max_runtime})"
            if self._wrap_reason and outcome == "stopped":
                error = f"wound down: {self._wrap_reason}"
            result, problem = read_result(spec.result_path)
            if result:
                status, summary, report = result["status"], result["summary"], result["report"]
            else:
                status, report = "unknown", tracker.last_text
                summary = " ".join(tracker.last_text.split())[:MAX_SUMMARY] or (problem or "")
                if outcome == "ok" and problem:
                    error = error or problem
            log.write(f"--- claude_tab: result {json.dumps(result) if result else problem}\n")
            log.write(f"--- {utc_now()} exit {code} ({outcome})\n")
        self._event(f"finished: {outcome}")
        return RunResult(code, outcome, error, tuple(tracker.denied), tracker.usage, status, summary, report,
                         tuple(tracker.unavailable))
