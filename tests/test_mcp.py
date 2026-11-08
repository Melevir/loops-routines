"""The MCP tools through the SDK's own call path (MCPServer.call_tool): a plain `def` tool runs in a worker
thread there, which is how run_routine once failed with "no running event loop"."""

import asyncio
import inspect

import pytest
from conftest import FakeNotifier, write_routine
from mcp.server.mcpserver.exceptions import ToolError

from loops_routines.core import paths
from loops_routines.core.runner import Run
from loops_routines.core.store import Store
from loops_routines.daemon.mcp import agent_server, user_server
from loops_routines.service import Service


@pytest.fixture
def service(fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "sleep")
    paths.config_path().write_text(f'[daemon]\nmax_parallel = 1\n[agent]\ncommand = "{fake_claude}"\n')
    write_routine("first", 'schedule = "every 30m"\nprofile = "unlimited"')
    write_routine("urgent", 'schedule = "every 30m"\nprofile = "spare"\nwindow = "sat 03:00-04:00"')
    return Service(Store(paths.db_path()), notifier=FakeNotifier(),
                   run_factory=lambda spec, e, u: Run(spec, e, u, end_grace=0.1))


def test_every_tool_runs_on_the_event_loop(service):
    for server in (user_server(service), agent_server(service)):
        for tool in server._tool_manager.list_tools():
            assert inspect.iscoroutinefunction(tool.fn), f"{tool.name} must be async def"


def test_run_routine_and_run_routine_now(service):
    server = user_server(service)

    async def go():
        result = await server.call_tool("run_routine", {"name": "first"})
        assert not result.is_error, result
        assert len(service.active) == 1
        # the only slot is taken: the queue says no...
        with pytest.raises(ToolError, match="slots are busy"):
            await server.call_tool("run_routine", {"name": "urgent", "force": True})
        # ...run_routine_now starts it anyway: on top of max_parallel, no usage numbers, outside its window
        result = await server.call_tool("run_routine_now", {"name": "urgent"})
        assert not result.is_error, result
        assert sorted(a.routine.name for a in service.active.values()) == ["first", "urgent"]
        with pytest.raises(ToolError, match="already running"):
            await server.call_tool("run_routine_now", {"name": "urgent"})
        with pytest.raises(ToolError, match="no routine"):
            await server.call_tool("run_routine_now", {"name": "nope"})
        for run_id in list(service.active):
            await server.call_tool("stop_run", {"run_id": run_id})
        for _ in range(200):
            if not service.active:
                break
            await asyncio.sleep(0.05)

    asyncio.run(go())
    triggers = {r["routine"]: r["trigger"] for r in service.list_runs()}
    assert triggers == {"first": "manual", "urgent": "out_of_queue"}


def test_a_refused_run_still_starts_a_probe(service):
    """run_routine without numbers asks for a probe: that used to crash the tool off the loop."""
    started = []

    async def probe(agent):
        started.append(1)
        raise RuntimeError("offline")

    service._prober = probe
    server = user_server(service)

    async def go():
        with pytest.raises(ToolError, match="limits"):
            await server.call_tool("run_routine", {"name": "urgent"})
        await asyncio.sleep(0.05)

    asyncio.run(go())
    assert started == [1]
