"""Scenario steps: a select's `run` decision starts the step's agent, within the daemon's slots and limits."""

import asyncio

import pytest
from conftest import FakeNotifier, cfg
from test_scenarios import Clock, given, make_scenario, say

from loops_routines.core import paths
from loops_routines.core.runner import Run
from loops_routines.core.scenarios import fill_step, parse_scenario
from loops_routines.core.store import Store
from loops_routines.errors import Conflict
from loops_routines.service import Service

UNLIMITED = 'select_every = "5m"\n[defaults]\nprofile = "unlimited"\n'


def write_step(folder, step: str, text: str) -> None:
    (folder / "steps").mkdir(exist_ok=True)
    (folder / "steps" / f"{step}.md").write_text(text)


@pytest.fixture
def setup(fake_claude):
    paths.config_path().write_text(
        f'[daemon]\ntimezone = "UTC"\nmax_parallel = 2\n[agent]\ncommand = "{fake_claude}"\n')
    notifier = FakeNotifier()

    def quick_run(spec, on_event, on_usage):
        return Run(spec, on_event, on_usage, end_grace=0.1)

    def make(start="2026-11-09T10:00:00"):
        clock = Clock(start)
        return Service(Store(paths.db_path()), now=clock, notifier=notifier, run_factory=quick_run), clock, notifier

    return make


async def settle(service: Service, timeout: float = 20) -> None:
    """Until no run, select or other background task is going."""
    for _ in range(int(timeout * 20)):
        if not service.active and all(t.done() for t in service._background):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"still going: runs {list(service.active)}")


async def stop_all(service: Service) -> None:
    """Stop runs until none is left (a freed slot may start the next one)."""
    for _ in range(400):
        for run_id in list(service.active):
            if not service.active[run_id].run._stop_requested:
                service.stop_run(run_id)
        if not service.active and all(t.done() for t in service._background):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("runs keep going")


def item(service, key, name="toy"):
    return service.scenarios.item_detail(name, key)


# --- step files --------------------------------------------------------------------------------


def test_steps_are_routines_without_a_schedule():
    folder = make_scenario(toml='title = "PR babysitter"\n[defaults]\nmodel = "sonnet"\nprofile = "unlimited"\n'
                                'notify = "never"')
    write_step(folder, "review", '+++\nmodel = "opus"\nmax_runtime = "20m"\nrepos = ["o/r"]\n+++\nReview {item.url}\n')
    sc = parse_scenario(folder, cfg())
    spec = sc.specs["review"]
    assert (spec.name, spec.title, spec.model, spec.profile, spec.notify) == \
        ("toy/review", "PR babysitter · review", "opus", "unlimited", "never")
    assert spec.trigger is None and spec.repos == ("o/r",)
    assert parse_scenario(folder).specs == {}  # without a config only the names are known

    write_step(folder, "bad", '+++\nschedule = "@daily"\n+++\nNo.\n')
    with pytest.raises(ValueError, match="steps/bad.md: unknown keys: schedule"):
        parse_scenario(folder, cfg())
    (folder / "steps" / "bad.md").unlink()
    (folder / "scenario.toml").write_text('[defaults]\nschedule = "@daily"\n')
    with pytest.raises(ValueError, match=r"\[defaults\]: unknown keys schedule"):
        parse_scenario(folder, cfg())


def test_step_prompts_fill_dotted_placeholders():
    values = {"key": "o/r#1", "vars": {"sha": "ab12", "files": ["a.py"]}, "settings": {"rounds": 3},
              "state": {"seen": None}}
    text = fill_step("{key} at {vars.sha} of {vars.files}; {settings.rounds} rounds; {state.seen}; {vars.nope} {x}", values)
    assert text == 'o/r#1 at ab12 of ["a.py"]; 3 rounds; ; {vars.nope} {x}'


# --- running them ------------------------------------------------------------------------------


def test_a_run_decision_runs_the_step_once_per_version(setup):
    folder = make_scenario(toml=UNLIMITED, steps=("review",), decisions=[
        {"key": "o/r#1", "action": "run", "step": "review", "version": "v1", "vars": {"x": "the login fix"},
         "reason": "new commits", "title": "Fix login"}])
    service, _, _ = setup()

    async def go():
        await service.scenarios.select_now("toy")
        assert item(service, "o/r#1")["queue"] == "queued" and not service.active
        await service.tick()  # the tick starts it
        [(run_id, active)] = service.scenarios.running("toy")
        assert active.item == ("toy", "o/r#1", "review", "v1")
        assert item(service, "o/r#1")["queue"] == f"running review (run {run_id})"
        await settle(service)

        run = service.get_run(run_id)
        assert (run["scenario"], run["item_key"], run["step"], run["version"]) == ("toy", "o/r#1", "review", "v1")
        assert run["title"] == "toy · review" and run["outcome"] == "ok"
        assert run["data"]["prompt"].startswith("Do the login fix")  # {vars.x} filled

        # the select that ran after it saw the run once, with its data
        seen = given(folder)["items"]["o/r#1"]
        [event] = [e for e in seen["events"] if e["kind"] == "run"]
        assert event["data"]["outcome"] == "ok" and event["data"]["data"]["prompt"].startswith("Do the login fix")
        assert seen["runs"]["review"]["version"] == "v1" and seen["running"] is None
        assert seen["last"]["action"] == "ran" and seen["last"]["reason"] == "review: all ok"

        # select still says run review v1: the same version is not run again; a new one is
        assert item(service, "o/r#1")["queue"] == "review already ran for version v1"
        await service.tick()
        assert not service.active
        say(folder, [{"key": "o/r#1", "action": "run", "step": "review", "version": "v2"}])
        await service.scenarios.select_now("toy")
        await service.tick()
        assert len(service.active) == 1
        await settle(service)
        assert [r["version"] for r in item(service, "o/r#1")["runs"]] == ["v2", "v1"]

    asyncio.run(go())


def test_a_step_that_keeps_failing_makes_the_item_stuck(setup, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "fail")
    folder = make_scenario(toml="max_attempts = 2\n" + UNLIMITED, steps=("review",),
                           decisions=[{"key": "o/r#1", "action": "run", "step": "review", "version": "v1"}])
    service, _, notes = setup()

    async def go():
        await service.scenarios.select_now("toy")
        await service.tick()
        await settle(service)  # attempt 1 failed: select runs again and queues it
        assert item(service, "o/r#1")["queue"] == "queued"
        await service.tick()
        await settle(service)  # attempt 2
        stuck = item(service, "o/r#1")
        assert stuck["action"] == "stuck" and stuck["reason"].startswith("stuck: review failed 2 times on version v1")
        stuck_banners = [b for b in notes.banners if b[1].startswith("stuck")]
        assert len(stuck_banners) == 1 and stuck_banners[0][2] == "warning"
        await service.tick()
        assert not service.active  # not run a third time
        await service.scenarios.select_now("toy")
        assert item(service, "o/r#1")["action"] == "stuck"
        assert len([b for b in notes.banners if b[1].startswith("stuck")]) == 1  # told once

        say(folder, [{"key": "o/r#1", "action": "run", "step": "review", "version": "v2"}])  # a new version
        await service.scenarios.select_now("toy")
        assert item(service, "o/r#1")["queue"] == "queued"

    asyncio.run(go())


def test_switches_slots_and_max_items(setup, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "sleep")
    folder = make_scenario(toml='max_items = 1\n' + UNLIMITED, steps=("review",), decisions=[
        {"key": f"o/r#{n}", "action": "run", "step": "review", "version": "v1"} for n in (1, 2, 3)])
    service, _, _ = setup()

    async def go():
        service.scenarios.set_step_enabled("toy", "review", False)
        await service.scenarios.select_now("toy")
        assert item(service, "o/r#1")["queue"] == "step review is off"
        assert given(folder)["steps_on"] == []
        view = service.scenarios.scenario_detail("toy")
        assert view["step_list"][0]["enabled"] is False and view["step_list"][0]["switched"]

        service.scenarios.set_step_enabled("toy", "review", None)
        service.scenarios.pause("toy", "o/r#3", True)
        await service.scenarios.select_now("toy")
        await service.tick()
        assert len(service.active) == 1  # max_items = 1
        assert item(service, "o/r#2")["queue"].startswith("1 of this scenario's items")
        assert item(service, "o/r#3")["reason"].startswith("paused") and item(service, "o/r#3")["queue"] is None

        # a select while the step runs does not interrupt it, but its state still lands
        say(folder, [{"key": "o/r#1", "action": "wait", "reason": "CI running", "state": {"n": 1}}])
        await service.scenarios.select_now("toy")
        running = item(service, "o/r#1")
        assert running["action"] == "run" and running["state"] == {"n": 1} and running["running_run_id"]

        # by hand: outside max_items, but not outside the slots
        started = service.scenarios.run_step_now("toy", "o/r#2", "review")
        assert len(service.active) == 2
        with pytest.raises(Conflict, match="slots are busy"):
            service.scenarios.run_step_now("toy", "o/r#3", "review")
        with pytest.raises(Conflict, match="already has a step running"):
            service.scenarios.run_step_now("toy", "o/r#2", "review")

        service.scenarios.untrack("toy", "o/r#1")  # stops its run
        service.stop_run(started["run_id"])
        await settle(service)
        assert service.get_run(started["run_id"])["outcome"] == "stopped"
        assert item(service, "o/r#1")["done_at"] and item(service, "o/r#1")["runs"][0]["outcome"] == "stopped"

    asyncio.run(go())


def test_the_queue_shares_slots_with_routines_by_priority(setup, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "sleep")
    paths.config_path().write_text(paths.config_path().read_text().replace("max_parallel = 2", "max_parallel = 1"))
    folder = make_scenario(toml=UNLIMITED, steps=("review",), decisions=[
        {"key": "o/r#1", "action": "run", "step": "review", "version": "v1"}])
    write_step(folder, "review", '+++\npriority = 90\n+++\nReview.\n')
    from conftest import write_routine
    write_routine("low", 'schedule = "every 30m"\nprofile = "unlimited"\npriority = 10')
    service, clock, _ = setup("2026-11-09T10:29:00")

    async def go():
        await service.scenarios.select_now("toy")
        from datetime import timedelta
        clock.at += timedelta(minutes=1)  # the routine is due at 10:30 too
        await service.tick()
        [active] = service.active.values()
        assert active.item and active.item[2] == "review"  # priority 90 beats 10
        assert service.list_routines()[0]["reason"] == "all 1 slots are busy"
        await stop_all(service)

    asyncio.run(go())


def test_cli_shows_steps_and_switches(setup, capsys):
    folder = make_scenario(toml=UNLIMITED, steps=("review",))
    from loops_routines import cli
    cli.main(["scenario", "check", str(folder)])
    out = capsys.readouterr().out
    assert "step review: sonnet, profile unlimited, max_runtime 30m" in out
