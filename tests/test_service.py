import asyncio
import json
from datetime import timedelta

import pytest
from conftest import FakeNotifier, utc, write_routine

from loops_routines.core import paths
from loops_routines.core.notify import Answer
from loops_routines.core.runner import Run
from loops_routines.core.store import Store
from loops_routines.service import Conflict, Service


class Clock:
    def __init__(self, start: str):
        self.at = utc(start)

    def __call__(self):
        return self.at

    def advance(self, **kwargs):
        self.at += timedelta(**kwargs)


@pytest.fixture
def setup(fake_claude, monkeypatch):
    paths.config_path().write_text(
        f'[daemon]\ntimezone = "UTC"\nmax_parallel = 2\n[agent]\ncommand = "{fake_claude}"\n')
    notifier = FakeNotifier()

    def quick_run(spec, on_event, on_usage):  # the real CLI waits a few seconds for more input after a result
        return Run(spec, on_event, on_usage, end_grace=0.1)

    def make(start="2026-11-09T10:05:00", **kwargs):
        clock = Clock(start)
        service = Service(Store(paths.db_path()), now=clock, notifier=notifier, run_factory=quick_run, **kwargs)
        return service, clock

    return make, notifier


async def wait_idle(service: Service, timeout: float = 20) -> None:
    for _ in range(int(timeout * 20)):
        if not service.active and not service._background - {t for t in service._background if t.done()}:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"runs still going: {list(service.active)}")


def test_manual_run_end_to_end(setup):
    make, notes = setup
    write_routine("summary", 'schedule = "@weekly"\nprofile = "unlimited"\nnotify = "always"',
                  "Summarise. Previous: {previous_summary}")
    service, _ = make()

    async def go():
        started = service.run_now("summary")
        await wait_idle(service)
        return started["run_id"]

    run_id = asyncio.run(go())
    run = service.get_run(run_id)
    assert run["state"] == "finished" and run["outcome"] == "ok" and run["status"] == "ok"
    assert run["summary"] == "all ok" and run["report"].startswith("# Report")
    assert run["cost_usd"] == 0.12
    assert "Previous: none" in run["report"]  # placeholders filled
    assert notes.banners == []  # all went well: telling the user is the agent's job, the daemon stays out
    report_file = paths.reports_dir() / "summary" / f"{run_id:06d}.md"
    assert report_file.is_file() and "all ok" in report_file.read_text()
    assert service.snapshot()["five_hour"]["used_percentage"] == 30.0  # from the run's rate_limit_event
    servers = json.loads((paths.runs_dir() / str(run_id) / "mcp.json").read_text())["mcpServers"]
    assert list(servers) == ["loops_routines"] and servers["loops_routines"]["url"].endswith("/agent/mcp")


def test_scheduled_run_and_the_previous_report(setup, monkeypatch):
    make, notes = setup
    write_routine("mon", 'schedule = "every 30m"\nprofile = "unlimited"', "Check. Before: {previous_summary}")
    service, clock = make("2026-11-09T10:05:00")

    async def go():
        await service.tick()
        assert not service.active  # first occurrence is 10:30
        assert service.list_routines()[0]["next_at"] == "2026-11-09T10:30:00+00:00"
        clock.advance(minutes=26)
        await service.tick()
        assert len(service.active) == 1
        await wait_idle(service)
        clock.advance(minutes=30)
        await service.tick()
        await wait_idle(service)

    asyncio.run(go())
    runs = service.list_runs("mon")
    assert [r["trigger"] for r in runs] == ["schedule", "schedule"]
    assert runs[1]["occurrence_at"] == "2026-11-09T10:30:00+00:00"
    assert "Before: all ok" in service.get_run(runs[0]["id"])["report"]
    assert notes.banners == []  # on_alert and nothing alarming


def test_alert_notifies(setup, monkeypatch):
    make, notes = setup
    monkeypatch.setenv("FAKE_MODE", "alert")
    write_routine("mon", 'schedule = "every 30m"\nprofile = "unlimited"')
    service, _ = make()

    async def go():
        service.run_now("mon")
        await wait_idle(service)

    asyncio.run(go())
    # the agent did not send its alert: the daemon does it for it
    assert notes.banners == [("mon", "mon: all alert", "error", "Basso")]
    assert service.health()["unseen_alerts"] == 1


def test_agent_notifies_and_asks(setup, monkeypatch):
    make, notes = setup
    monkeypatch.setenv("FAKE_MODE", "sleep")
    write_routine("mon", 'title = "Log monitor"\nschedule = "every 30m"\nprofile = "unlimited"')
    write_routine("quiet", 'schedule = "every 30m"\nprofile = "unlimited"\nnotify = "never"')
    service, _ = make()

    async def go():
        run_id = service.run_now("mon")["run_id"]
        shown = await service.notify_user(run_id, "New error in payments", "error")
        assert shown["shown"] and notes.banners[-1] == ("Log monitor", "New error in payments", "error", "Basso")
        assert service.active[run_id].notified_error
        with pytest.raises(ValueError, match="kind"):
            await service.notify_user(run_id, "x", "loud")

        notes.answer = Answer("answered", "Roll back")  # the user clicks in the dialog
        answer = await service.ask_user(run_id, "Roll back the deploy?", ["Roll back", "Keep"], 5)
        assert answer == {"status": "answered", "answer": "Roll back"}
        assert notes.asks[-1] == ("Log monitor", "Roll back the deploy?", ["Roll back", "Keep"], 300)

        notes.block = True  # the dialog stays open; the answer comes from the web page / CLI / MCP
        asking = asyncio.ensure_future(service.ask_user(run_id, "Open an issue?"))
        await asyncio.sleep(0.05)
        [question] = service.pending_questions()
        assert question["buttons"] == ["Yes", "No"]
        with pytest.raises(ValueError, match="one of"):
            service.answer_question(question["id"], "maybe")
        service.answer_question(question["id"], "no")
        assert await asking == {"status": "answered", "answer": "No"}

        last = asyncio.ensure_future(service.ask_user(run_id, "Still there?"))  # the run ends first
        await asyncio.sleep(0.05)
        service.stop_run(run_id)
        assert (await last)["status"] == "cancelled"
        await wait_idle(service)

        quiet = service.run_now("quiet")["run_id"]
        with pytest.raises(ValueError, match="never"):
            await service.ask_user(quiet, "May I?")
        service.stop_run(quiet)
        await wait_idle(service)
        return run_id

    run = service.get_run(asyncio.run(go()))
    kinds = [(m["kind"], (m["report"] or {}).get("answer")) for m in run["messages"]]
    assert kinds == [("notify", None), ("question", "Roll back"), ("question", "No"), ("question", None)]
    assert service.pending_questions() == []
    assert notes.banners[-1][2] != "error" or "Log monitor" not in notes.banners[-1][1]  # no second alert banner


def test_failure_is_reported(setup, monkeypatch):
    make, notes = setup
    monkeypatch.setenv("FAKE_MODE", "fail")
    write_routine("mon", 'schedule = "every 30m"\nprofile = "unlimited"')
    service, _ = make()

    async def go():
        return service.run_now("mon")["run_id"]

    async def run():
        run_id = await go()
        await wait_idle(service)
        return run_id

    run = service.get_run(asyncio.run(run()))
    assert run["outcome"] == "failed" and "boom" in (run["error"] or "")
    assert notes.banners == [("mon", "mon failed: boom: not logged in", "error", "Basso")]
    assert run["messages"][-1]["report"]["by"] == "daemon"


def test_no_result_file_falls_back_to_last_words(setup, monkeypatch):
    make, _ = setup
    monkeypatch.setenv("FAKE_MODE", "noresult")
    write_routine("mon", 'schedule = "every 30m"\nprofile = "unlimited"')
    service, _ = make()

    async def go():
        run_id = service.run_now("mon")["run_id"]
        await wait_idle(service)
        return run_id

    run = service.get_run(asyncio.run(go()))
    assert run["outcome"] == "ok" and run["status"] == "unknown" and run["summary"] == "Done here."
    assert "result.json" in run["error"]


def test_message_and_stop(setup, monkeypatch):
    make, _ = setup
    monkeypatch.setenv("FAKE_MODE", "message")
    write_routine("talk", 'schedule = "every 30m"\nprofile = "unlimited"')
    write_routine("slow", 'schedule = "every 30m"\nprofile = "unlimited"')
    service, _ = make()

    async def go():
        talk = service.run_now("talk")["run_id"]
        for _ in range(200):
            if service.active[talk].run.accepts_messages and service.active[talk].run.last_event != "starting":
                break
            await asyncio.sleep(0.05)
        service.message_run(talk, "please also check X")
        with pytest.raises(Conflict, match="already running"):
            service.run_now("talk")
        await wait_idle(service)
        return talk

    talk = asyncio.run(go())
    run = service.get_run(talk)
    assert "please also check X" in run["report"]
    assert [m["kind"] for m in run["messages"]] == ["user"]

    monkeypatch.setenv("FAKE_MODE", "sleep")

    async def stop():
        slow = service.run_now("slow")["run_id"]
        for _ in range(200):
            if service.active[slow].run.running:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.3)
        service.stop_run(slow)
        await wait_idle(service)
        return slow

    assert service.get_run(asyncio.run(stop()))["outcome"] == "stopped"


def test_expired_occurrence_is_recorded_with_its_reason(setup):
    make, _ = setup
    write_routine("daily", 'schedule = "0 9 * * *"\ngrace = "1h"\nprofile = "spare"')
    service, clock = make("2026-11-09T08:00:00")
    probes = []

    async def failing_probe(agent):
        probes.append(1)
        raise RuntimeError("not logged in")

    service._prober = failing_probe

    async def go():
        await service.tick()
        clock.advance(hours=1, minutes=5)  # 09:05: due, but no usage numbers
        await service.tick()
        await asyncio.sleep(0.05)
        assert service.list_routines()[0]["state"] == "waiting"
        clock.advance(hours=1)  # 10:05: past its grace
        await service.tick()

    asyncio.run(go())
    skipped = service.list_runs("daily")
    assert skipped[0]["state"] == "skipped" and "usage" in skipped[0]["error"]
    assert probes == [1]  # one probe, then the cooldown
    assert service.usage_view()["probe"]["error"] == "not logged in"


def test_slots_and_priority(setup, monkeypatch):
    make, _ = setup
    monkeypatch.setenv("FAKE_MODE", "sleep")
    paths.config_path().write_text(paths.config_path().read_text().replace("max_parallel = 2", "max_parallel = 1"))
    write_routine("low", 'schedule = "every 30m"\nprofile = "unlimited"\npriority = 10')
    write_routine("high", 'schedule = "every 30m"\nprofile = "unlimited"\npriority = 90')
    service, clock = make("2026-11-09T10:05:00")

    async def go():
        await service.tick()
        clock.advance(minutes=26)
        await service.tick()
        assert [a.routine.name for a in service.active.values()] == ["high"]
        low = next(r for r in service.list_routines() if r["name"] == "low")
        assert low["state"] == "waiting" and "slots" in low["reason"]
        with pytest.raises(Conflict, match="slots"):
            service.run_now("low", force=True)
        for run_id in list(service.active):
            service.stop_run(run_id)
        for _ in range(200):  # the freed slot goes to the routine that waited for it
            if [a.routine.name for a in service.active.values()] == ["low"]:
                break
            await asyncio.sleep(0.05)
        assert [a.routine.name for a in service.active.values()] == ["low"]
        await asyncio.sleep(0.3)
        for run_id in list(service.active):
            service.stop_run(run_id)
        await wait_idle(service)

    asyncio.run(go())


def test_disable_from_the_ui_skips_the_backlog(setup):
    make, _ = setup
    write_routine("mon", 'schedule = "every 30m"\nprofile = "unlimited"')
    service, clock = make("2026-11-09T10:05:00")

    async def go():
        await service.tick()
        service.set_enabled("mon", False)
        clock.advance(hours=3)
        await service.tick()
        assert service.list_routines()[0]["state"] == "disabled"
        service.set_enabled("mon", None)
        clock.advance(minutes=1)
        await service.tick()
        assert not service.active  # nothing owed from the hours it was off

    asyncio.run(go())


def test_limits_refuse_a_manual_run_unless_forced(setup):
    make, _ = setup
    write_routine("cleanup", 'schedule = "every 2h"\nprofile = "spare"')
    service, _ = make()
    service._prober = lambda agent: asyncio.sleep(0, result=None)

    async def go():
        with pytest.raises(Conflict, match="limits"):
            service.run_now("cleanup")
        run_id = service.run_now("cleanup", force=True)["run_id"]
        await wait_idle(service)
        return run_id

    assert service.get_run(asyncio.run(go()))["trigger"] == "manual"


def test_agent_reports(setup, monkeypatch):
    make, _ = setup
    monkeypatch.setenv("FAKE_MODE", "sleep")
    write_routine("mon", 'schedule = "every 30m"\nprofile = "unlimited"')
    service, _ = make()

    async def go():
        run_id = service.run_now("mon")["run_id"]
        token = service.active[run_id].token
        assert service.run_for_token(token) == run_id
        service.report_status(run_id, "step 1", "progress", key="s")
        service.report_status(run_id, "step 1 done", "success", key="s")
        service.report_blocker(run_id, "Kibana MCP not signed in", "mcp_not_authenticated", "read logs",
                               "401 from search", ["claude mcp auth kibana"])
        with pytest.raises(ValueError):
            service.report_status(run_id, "x" * 300)
        service.stop_run(run_id)
        await wait_idle(service)
        assert service.run_for_token(token) is None  # the token dies with the run
        return run_id

    run = service.get_run(asyncio.run(go()))
    assert [(m["kind"], m["text"]) for m in run["messages"]] == [
        ("success", "step 1 done"), ("blocker", "Kibana MCP not signed in"), ("notify", "Kibana MCP not signed in")]
    assert run["messages"][1]["report"]["fix"] == ["claude mcp auth kibana"]
    assert run["messages"][2]["report"]["kind"] == "warning"  # a blocker also shows a banner


def test_recover_closes_lost_runs(setup):
    make, _ = setup
    write_routine("mon", 'schedule = "every 30m"')
    service, _ = make()
    run_id = service.store.start_run("mon", "schedule", None, "2026-11-09T10:00:00+00:00")
    assert service.recover() == [run_id]
    assert service.get_run(run_id)["outcome"] == "lost"


def test_broken_routine_is_listed(setup):
    make, _ = setup
    write_routine("bad", 'schedule = "sometimes"')
    service, _ = make()
    [view] = service.list_routines()
    assert view["state"] == "broken" and "cron" in view["reason"]
    with pytest.raises(Conflict):
        service.run_now("bad")
