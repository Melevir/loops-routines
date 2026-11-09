"""Two MCP servers over the same service.

    /mcp         for the user's Claude sessions: see and steer the routines (claude mcp add ... --header)
    /agent/mcp   for loops-routines's own runs only: progress lines, blocker reports, notifications and
                 questions; the run is the one whose token came with the request, never an argument the
                 model could change

MCP has no per-tool permissions (every client sees what a server declares), so the agent tools are kept
apart by living on their own endpoint.

Every tool is `async def`: the SDK runs a plain `def` tool in a worker thread, and the service lives on
the daemon's event loop (it starts runs and probes as tasks there). A sync tool that started a run failed
with "no running event loop".
"""

from typing import Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from loops_routines.daemon.auth import bearer
from loops_routines.service import Conflict, NotFound, Service

INSTRUCTIONS = """\
loops-routines is the user's daemon that runs Claude Code routines on a schedule (cron or "every 30m") and
within their plan's usage limits: e.g. a weekly summary of changes, a cleanup when tokens would expire
anyway, a log monitor every half hour. Routines are files in ~/.loops_routines/routines/*.md (a TOML header
between +++ lines, the prompt below). list_routines shows each one's state (running, waiting and why,
idle with its next run); get_run gives a run's result and report. run_routine starts one now within the
rules (free slot, its usage limits); run_routine_now starts it at once, outside the queue.

Scenarios are multi-step workflows over many items (e.g. babysitting pull requests), folders in
~/.loops_routines/scenarios/: a `select` program decides each item's next step without an LLM, and the
daemon runs that step's agent (steps/<step>.md) within the same slots and limits as the routines.
list_scenarios and scenario_status show them and their items; track_item / untrack_item choose the items;
press_item_button and pause_item act on one; set_step_enabled switches a step, run_step_now starts one.
"""

AGENT_INSTRUCTIONS = """\
You are running as a loops-routines routine. Post short progress lines with report_status (use a `key`
for a step still running and send its end state with the same key). `notify` shows the user a macOS
notification; `ask_user` shows a dialog with your buttons and waits for the click. When your environment
keeps you from the goal (an MCP server not signed in or down, a missing plugin, skill, CLI or credentials,
a denied tool call), call report_blocker with a concrete report.
"""


async def _call(fn, *args, **kwargs):
    """Run a service call on the daemon's loop; its errors reach the model as readable tool errors,
    not a bare 'Error executing tool'."""
    try:
        result = fn(*args, **kwargs)
        if hasattr(result, "__await__"):
            result = await result
        return result
    except (ValueError, NotFound, Conflict) as exc:
        raise ToolError(str(exc)) from exc


def user_server(service: Service) -> MCPServer:
    server = MCPServer("loops_routines", instructions=INSTRUCTIONS)

    @server.tool()
    async def list_routines() -> list[dict]:
        """Every routine with its state: running (with its live step), waiting (and why: limits, window,
        slots), idle (with next_at), disabled or broken (with the file's error); its last run and skip."""
        return await _call(service.list_routines)

    @server.tool()
    async def routine_status(name: str) -> dict:
        """One routine in full: its schedule, limits, integrations, prompt and its last 20 runs.

        Args:
            name: The routine's file name without .md, e.g. "monitor-logs".
        """
        return await _call(service.routine_detail, name)

    @server.tool()
    async def run_routine(name: str, force: bool = False) -> dict:
        """Start a routine now, outside its schedule, within the queue's rules: refused while it runs or
        while all slots are busy. Its occurrences stay as they are.

        Args:
            name: The routine's file name without .md.
            force: True starts it even when its usage limits say no (min_free, before_reset, keep_free).
        """
        return await _call(service.run_now, name, force)

    @server.tool()
    async def run_routine_now(name: str) -> dict:
        """Start a routine immediately, outside the queue: even when all slots are busy (it runs on top of
        max_parallel), outside its window, and whatever its usage limits say. Refused only while the same
        routine is already running, or when its file is broken. Its occurrences stay as they are.

        Args:
            name: The routine's file name without .md.
        """
        return await _call(service.run_now, name, True, True)

    @server.tool()
    async def set_routine_enabled(name: str, enabled: bool | None) -> dict:
        """Switch a routine on or off (kept by the daemon over its file's `enabled`); null goes back to the file.

        Args:
            name: The routine's file name without .md.
            enabled: true, false, or null for "as the file says".
        """
        return await _call(service.set_enabled, name, enabled)

    @server.tool()
    async def list_runs(routine: str | None = None, limit: int = 20, include_skipped: bool = True) -> list[dict]:
        """Recent runs, newest first: outcome (ok, failed, timeout, stopped, lost), the agent's status
        (ok, alert, nothing, unknown), summary and cost. Skipped occurrences carry why in `error`.

        Args:
            routine: Only this routine's runs.
            limit: At most this many (up to 500).
            include_skipped: False leaves the skipped occurrences out.
        """
        return await _call(service.list_runs, routine, limit, None, include_skipped)

    @server.tool()
    async def get_run(run_id: int) -> dict:
        """One run in full: its Markdown report, errors, denied tool calls, cost, and what the agent posted."""
        return await _call(service.get_run, run_id)

    @server.tool()
    async def run_log(run_id: int, lines: int = 100) -> str:
        """The last lines of a run's log (the claude command line, its prompt and every stream-json event)."""
        return await _call(service.log_tail, run_id, lines)

    @server.tool()
    async def stop_run(run_id: int) -> dict:
        """Stop a running run (SIGTERM to its process group, SIGKILL after 5 s)."""
        return await _call(service.stop_run, run_id)

    @server.tool()
    async def message_run(run_id: int, text: str) -> dict:
        """Send the agent of a running run a message; it reads it between its steps."""
        return await _call(service.message_run, run_id, text)

    @server.tool()
    async def usage(probe: bool = False) -> dict:
        """The Claude plan's 5-hour and 7-day windows as the daemon last saw them.

        Args:
            probe: True refreshes them first with one tiny haiku request.
        """
        return await _call(service.probe_now) if probe else await _call(service.usage_view)

    @server.tool()
    async def pending_questions() -> list[dict]:
        """Questions running agents asked the user and are waiting on: id, routine, question, buttons, deadline."""
        return await _call(service.pending_questions)

    @server.tool()
    async def answer_question(question_id: int, answer: str) -> dict:
        """Answer an agent's waiting question on the user's behalf, only with what the user told you.

        Args:
            question_id: From pending_questions.
            answer: One of the question's buttons, exactly as listed (any case).
        """
        return await _call(service.answer_question, question_id, answer)

    # --- scenarios --------------------------------------------------------------------

    @server.tool()
    async def list_scenarios() -> list[dict]:
        """Every scenario (a multi-step workflow over many items, e.g. PRs): selecting, idle, error (with
        the select's error), disabled or broken; its item counts by last decision and its steps."""
        return await _call(service.scenarios.list_scenarios)

    @server.tool()
    async def scenario_status(name: str, include_done: bool = False) -> dict:
        """One scenario in full: settings, steps, last select and every item it follows with its last
        decision (wait, notify, done, ...), reason, buttons and whether it is paused.

        Args:
            name: The scenario's folder name.
            include_done: True also lists items it no longer follows.
        """
        return await _call(service.scenarios.scenario_detail, name, include_done)

    @server.tool()
    async def set_scenario_enabled(name: str, enabled: bool | None) -> dict:
        """Switch a scenario on or off (kept by the daemon over its scenario.toml); null goes back to the file.

        Args:
            name: The scenario's folder name.
            enabled: true, false, or null for "as scenario.toml says".
        """
        return await _call(service.scenarios.set_enabled, name, enabled)

    @server.tool()
    async def run_scenario_select(name: str) -> dict:
        """Run a scenario's select now (it decides every item's next step) and wait for it."""
        return await _call(service.scenarios.select_now, name)

    @server.tool()
    async def scenario_item(name: str, key: str) -> dict:
        """One item of a scenario: its state, last decision, buttons and history (decisions, notices,
        button presses).

        Args:
            name: The scenario's folder name.
            key: The item's key as the scenario lists it, e.g. "owner/repo#123".
        """
        return await _call(service.scenarios.item_detail, name, key)

    @server.tool()
    async def track_item(name: str, key: str, title: str | None = None, url: str | None = None) -> dict:
        """Ask a scenario to look after an item (or again, after it was done); its select runs at once.

        Args:
            name: The scenario's folder name.
            key: The item's key in the form the scenario expects (its README says), e.g. "owner/repo#123".
            title: Optional display title.
            url: Optional link.
        """
        return await _call(service.scenarios.track, name, key, title, url)

    @server.tool()
    async def untrack_item(name: str, key: str) -> dict:
        """Stop a scenario following an item (it becomes done; track_item brings it back)."""
        return await _call(service.scenarios.untrack, name, key)

    @server.tool()
    async def press_item_button(name: str, key: str, button: str) -> dict:
        """Press one of an item's buttons on the user's behalf, only when the user told you to: the
        scenario's select sees the press at once (e.g. "sent_for_review").

        Args:
            name: The scenario's folder name.
            key: The item's key.
            button: One of the item's buttons as listed (any case).
        """
        return await _call(service.scenarios.press, name, key, button)

    @server.tool()
    async def pause_item(name: str, key: str, paused: bool = True) -> dict:
        """Pause an item (its select keeps deciding, nothing is done for it) or resume it."""
        return await _call(service.scenarios.pause, name, key, paused)

    @server.tool()
    async def set_step_enabled(name: str, step: str, enabled: bool | None) -> dict:
        """Switch one step of a scenario on or off (e.g. stop it merging, keep it reviewing); null goes back
        to the step file. A switched-off step is not started; one already running finishes.

        Args:
            name: The scenario's folder name.
            step: The step's name (steps/<step>.md), as scenario_status lists it.
            enabled: true, false, or null.
        """
        return await _call(service.scenarios.set_step_enabled, name, step, enabled)

    @server.tool()
    async def run_step_now(name: str, key: str, step: str, force: bool = False) -> dict:
        """Start a step's agent for one item now, outside the scenario's queue, its attempts and switches;
        only when the user asked. A free slot is still needed, and the step's usage limits unless force.

        Args:
            name: The scenario's folder name.
            key: The item's key.
            step: The step to run.
            force: Start even when the step's usage limits say no.
        """
        return await _call(service.scenarios.run_step_now, name, key, step, force)

    return server


def _run_id(service: Service, ctx: Context) -> int:
    run_id = service.run_for_token(bearer([(k.encode(), v.encode()) for k, v in (ctx.headers or {}).items()]))
    if run_id is None:
        raise ToolError("this run has ended")
    return run_id


def agent_server(service: Service) -> MCPServer:
    server = MCPServer("loops_routines", instructions=AGENT_INSTRUCTIONS)

    @server.tool()
    async def report_status(text: str, kind: Literal["info", "progress", "success", "warning", "error"] = "info",
                            key: str | None = None, *, ctx: Context) -> dict:
        """Post one short progress line to the user's loops-routines view of this run (no notification).

        Args:
            text: One line, at most 280 characters, e.g. "Read 3 services' logs, 2 new errors".
            kind: progress - a step is running; success / warning / error - an outcome; info - anything else.
            key: Optional id of a running step: sending again with the same key updates that line in place.
        """
        return await _call(service.report_status, _run_id(service, ctx), text, kind, key)

    @server.tool()
    async def report_blocker(
        summary: str,
        category: Literal["mcp_not_authenticated", "mcp_unavailable", "plugin_missing", "skill_missing",
                          "permission_denied", "credentials", "tool_missing", "network", "other"],
        goal: str, details: str, fix: list[str] | None = None, tried: list[str] | None = None,
        outcome: Literal["stopped", "continuing"] = "stopped", *, ctx: Context,
    ) -> dict:
        """Tell the user your environment keeps you from your goal, with a report they can act on: an MCP
        server not signed in or down, a plugin or skill not installed, a denied tool call, missing
        credentials or CLI, a network failure. Not for problems in the work itself.

        Args:
            summary: One line, at most 280 characters.
            category: What kind of obstacle it is.
            goal: What you were trying to achieve and why it needs this.
            details: What exactly happened: the tool or command, the exact error, what you checked.
            fix: Concrete steps for the user, one line each.
            tried: What you already tried, one line each.
            outcome: stopped - you give up the goal; continuing - you carry on with a weaker result.
        """
        return await _call(service.report_blocker, _run_id(service, ctx), summary, category, goal, details, fix,
                           tried, outcome)

    @server.tool()
    async def notify(text: str, kind: Literal["info", "success", "warning", "error"] = "info", *,
                     ctx: Context) -> dict:
        """Show the user a macOS notification, titled with your routine's name.

        Args:
            text: One line, at most 280 characters: what happened and what (if anything) they should do.
            kind: error - an alert someone should look at now (with a sound); warning; success; info.
        """
        return await _call(service.notify_user, _run_id(service, ctx), text, kind)

    @server.tool()
    async def ask_user(question: str, buttons: list[str] | None = None, timeout_minutes: int | None = None, *,
                       ctx: Context) -> dict:
        """Ask the user something only they can decide, in a macOS dialog, and wait for their click.

        Returns {"status": "answered", "answer": "<the button>"}; or status timed_out / cancelled /
        unavailable with answer null: then take the safe path and say so in your report.

        Args:
            question: The question with the context they need to answer it, up to 600 characters.
            buttons: 1-3 short answers (up to 30 characters each), the one you recommend first; default Yes, No.
            timeout_minutes: How long to wait, 1-120; default from the daemon's config (15).
        """
        return await _call(service.ask_user, _run_id(service, ctx), question, buttons, timeout_minutes)

    return server
