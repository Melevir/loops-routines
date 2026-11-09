import asyncio
import json
import os
import stat
import sys
import textwrap
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import FakeNotifier, utc

from loops_routines import cli
from loops_routines.core import paths
from loops_routines.core.scenarios import merge_state, parse_decisions, parse_scenario
from loops_routines.core.store import Store
from loops_routines.service import Service

# Prints the decisions in decisions.json next to it, after saving what it was given to last_input.json.
# exit.txt makes it fail with that code; sleep.txt makes it hang.
TOY_SELECT = """#!{python}
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
data = json.load(sys.stdin)
json.dump(data, open(os.path.join(here, "last_input.json"), "w"))
if os.path.exists(os.path.join(here, "sleep.txt")):
    time.sleep(30)
if os.path.exists(os.path.join(here, "exit.txt")):
    print("selector trouble", file=sys.stderr)
    sys.exit(int(open(os.path.join(here, "exit.txt")).read()))
print(open(os.path.join(here, "decisions.json")).read())
"""


def make_scenario(name="toy", toml='select_every = "5m"', decisions=(), steps=()) -> Path:
    folder = paths.scenarios_dir() / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "scenario.toml").write_text(textwrap.dedent(toml))
    select = folder / "select"
    select.write_text(TOY_SELECT.format(python=sys.executable))
    select.chmod(select.stat().st_mode | stat.S_IXUSR)
    for step in steps:
        (folder / "steps").mkdir(exist_ok=True)
        (folder / "steps" / f"{step}.md").write_text("+++\nmodel = \"sonnet\"\n+++\nDo {vars.x}\n")
    say(folder, decisions)
    return folder


def say(folder: Path, decisions) -> None:
    (folder / "decisions.json").write_text(json.dumps(list(decisions)))


def given(folder: Path) -> dict:
    return json.loads((folder / "last_input.json").read_text())


class Clock:
    def __init__(self, start: str):
        self.at = utc(start)

    def __call__(self):
        return self.at


@pytest.fixture
def make():
    paths.config_path().write_text('[daemon]\ntimezone = "UTC"\n')
    notifier = FakeNotifier()

    def build(start="2026-11-09T10:00:00"):
        clock = Clock(start)
        return Service(Store(paths.db_path()), now=clock, notifier=notifier), clock, notifier

    return build


# --- the folder ------------------------------------------------------------------------------


def test_parse_a_scenario():
    folder = make_scenario(toml="""
        title = "Toy"
        select_every = "2m"
        bogus = 1
        [defaults]
        model = "sonnet"
        [settings]
        rounds = 3
        [settings.schema]
        rounds = { type = "int", min = 1, max = 5 }
        """, steps=("review", "develop"))
    sc = parse_scenario(folder)
    assert (sc.title, sc.select_every, sc.steps) == ("Toy", 120, ("develop", "review"))
    assert sc.settings == {"rounds": 3} and sc.schema["rounds"]["max"] == 5
    assert sc.defaults == {"model": "sonnet"} and not sc.has_apply
    assert sc.problems == ("scenario.toml: unknown key 'bogus'",)


@pytest.mark.parametrize("toml, error", [
    ('select_every = "10s"', "at least"),
    ('max_items = 0', "max_items"),
    ('[settings]\nrounds = 9\n[settings.schema]\nrounds = { type = "int", max = 5 }', "rounds must be"),
    ('[settings]\nrounds = "3"\n[settings.schema]\nrounds = { type = "int" }', "must be int"),
    ('[settings.schema]\nlogin = { type = "str", required = true }', "login is required"),
    ('select_every = ', "scenario.toml"),
])
def test_bad_scenarios_say_what_to_fix(toml, error):
    with pytest.raises(ValueError, match=error):
        parse_scenario(make_scenario(toml=toml))


def test_select_must_be_executable():
    folder = make_scenario()
    (folder / "select").chmod(0o644)
    with pytest.raises(ValueError, match="not executable"):
        parse_scenario(folder)
    (folder / "select").unlink()
    with pytest.raises(ValueError, match="no `select`"):
        parse_scenario(folder)


# --- the contract ----------------------------------------------------------------------------


def test_decisions_are_checked_one_by_one():
    output = json.dumps({"items": [
        {"key": "a#1", "action": "wait", "reason": "CI   is\nrunning", "state": {"n": 1}},
        {"key": "a#2", "action": "notify", "text": "Your turn", "name": "yours", "buttons": ["sent"]},
        {"key": "a#3", "action": "run", "step": "nope"},
        {"key": "a#1", "action": "done"},
        {"key": "", "action": "wait"},
        {"key": "a#4", "action": "explode"},
        {"key": "a#5", "action": "notify"},
    ]})
    decisions, problems = parse_decisions(output, ("review",))
    assert [(d.key, d.action) for d in decisions] == [("a#1", "wait"), ("a#2", "notify")]
    assert decisions[0].reason == "CI is running" and decisions[0].state == {"n": 1}
    assert decisions[1].name == "yours" and decisions[1].buttons == ["sent"]
    assert len(problems) == 5
    assert "no step 'nope'" in problems[0] and "already has a decision" in problems[1]


def test_select_output_must_be_a_list():
    with pytest.raises(ValueError, match="JSON"):
        parse_decisions("not json", ())
    with pytest.raises(ValueError, match="list of decisions"):
        parse_decisions('{"nope": 1}', ())


def test_state_patches_merge_and_null_removes():
    assert merge_state({"a": 1, "b": 2}, {"b": None, "c": 3}) == {"a": 1, "c": 3}
    assert merge_state({"a": 1}, None) == {"a": 1}


# --- the engine ------------------------------------------------------------------------------


def test_select_applies_wait_notify_and_done(make):
    folder = make_scenario(decisions=[
        {"key": "r#1", "action": "wait", "reason": "CI is running", "title": "Fix login", "state": {"seen": "ab"}},
        {"key": "r#2", "action": "notify", "name": "yours", "text": "Your review", "once_per": "sha1",
         "buttons": ["sent_for_review"]},
        {"key": "r#3", "action": "done", "reason": "merged"},
    ])
    service, _, notes = make()

    async def go():
        detail = await service.scenarios.select_now("toy")
        assert detail["state"] == "idle" and detail["error"] is None
        items = {i["key"]: i for i in service.scenarios.list_items("toy", include_done=True)}
        assert items["r#1"]["reason"] == "CI is running" and items["r#1"]["title"] == "Fix login"
        assert items["r#2"]["buttons"] == ["sent_for_review"] and items["r#2"]["notice"] == "yours: Your review"
        assert items["r#3"]["done_at"]
        assert notes.banners == [("toy: r#2", "Your review", "info", "")]

        # the same notice for the same once_per is not sent again; a new once_per is
        await service.scenarios.select_now("toy")
        assert len(notes.banners) == 1
        say(folder, [{"key": "r#2", "action": "notify", "name": "yours", "text": "Your review", "once_per": "sha2"}])
        await service.scenarios.select_now("toy")
        assert len(notes.banners) == 2

        # what select remembers comes back to it; done items don't
        seen = given(folder)
        assert seen["items"]["r#1"]["state"] == {"seen": "ab"}
        assert seen["items"]["r#1"]["last"]["action"] == "wait"
        assert "r#3" not in seen["items"]
        say(folder, [{"key": "r#3", "action": "wait", "reason": "back from the dead?"}])
        await service.scenarios.select_now("toy")
        assert service.scenarios.item_detail("toy", "r#3")["reason"] == "merged"  # done stays done
        service.scenarios.track("toy", "r#3")  # until a person tracks it again
        await asyncio.shield(service.scenarios.tasks["toy"])
        assert service.scenarios.item_detail("toy", "r#3")["reason"] == "back from the dead?"
        history = service.scenarios.item_detail("toy", "r#2")["events"]
        assert [e["kind"] for e in history].count("notice") == 2

    asyncio.run(go())


def test_track_press_and_pause(make):
    folder = make_scenario(decisions=[])
    service, _, notes = make()

    async def go():
        service.scenarios.track("toy", "r#7", title="New thing", url="https://example.com/7")
        await asyncio.shield(service.scenarios.tasks["toy"])
        seen = given(folder)
        assert seen["tracked"] == ["r#7"]
        assert [e["kind"] for e in seen["items"]["r#7"]["events"]] == ["track"]

        say(folder, [{"key": "r#7", "action": "wait", "reason": "your turn", "buttons": ["sent_for_review"]}])
        await service.scenarios.select_now("toy")
        assert given(folder)["items"]["r#7"]["events"] == []  # a select sees an event once
        with pytest.raises(ValueError, match="press one of: sent_for_review"):
            service.scenarios.press("toy", "r#7", "merge")
        service.scenarios.press("toy", "r#7", "SENT_FOR_REVIEW")
        await asyncio.shield(service.scenarios.tasks["toy"])
        assert given(folder)["items"]["r#7"]["events"][0]["name"] == "sent_for_review"

        service.scenarios.pause("toy", "r#7", True)
        say(folder, [{"key": "r#7", "action": "notify", "text": "ping"}])
        await service.scenarios.select_now("toy")
        assert notes.banners == []
        assert service.scenarios.item_detail("toy", "r#7")["reason"].startswith("paused")

        service.scenarios.untrack("toy", "r#7")
        say(folder, [{"key": "r#7", "action": "wait", "reason": "rediscovered"}])
        await service.scenarios.select_now("toy")
        assert service.scenarios.item_detail("toy", "r#7")["reason"] == "untracked"  # stays done

    asyncio.run(go())


def test_actions_of_later_phases_are_shown_not_run(make):
    make_scenario(steps=("review",), decisions=[{"key": "r#1", "action": "run", "step": "review",
                                                 "reason": "first review"}])
    service, _, _ = make()
    asyncio.run(service.scenarios.select_now("toy"))
    item = service.scenarios.item_detail("toy", "r#1")
    assert item["action"] == "run" and item["step"] == "review"
    assert "not carried out" in item["reason"] and not service.active


def test_a_failing_select_is_an_error_on_the_scenario(make):
    folder = make_scenario(toml='select_every = "5m"\nselect_timeout = "1s"')
    (folder / "exit.txt").write_text("3")
    service, _, notes = make()

    async def go():
        detail = await service.scenarios.select_now("toy")
        assert detail["state"] == "error" and "exited with 3: selector trouble" in detail["error"]
        await service.scenarios.select_now("toy")
        assert len(notes.banners) == 1  # the same error is told once
        (folder / "exit.txt").unlink()
        (folder / "sleep.txt").write_text("")
        detail = await service.scenarios.select_now("toy")
        assert "longer than 1s" in detail["error"]
        (folder / "sleep.txt").unlink()
        (folder / "decisions.json").write_text("garbage")
        detail = await service.scenarios.select_now("toy")
        assert "did not print JSON" in detail["error"]
        say(folder, [])
        detail = await service.scenarios.select_now("toy")
        assert detail["state"] == "idle" and detail["last_select_ok_at"]

    asyncio.run(go())


def test_tick_runs_due_selects_of_enabled_scenarios(make):
    folder = make_scenario(toml='select_every = "5m"')
    make_scenario(name="off", toml='enabled = false')
    service, clock, _ = make()

    async def go():
        await service.tick()
        await asyncio.shield(service.scenarios.tasks["toy"])
        assert "off" not in service.scenarios.tasks
        first = given(folder)["now"]
        clock.at += timedelta(minutes=2)
        await service.tick()
        assert service.scenarios.tasks["toy"].done() and given(folder)["now"] == first
        clock.at += timedelta(minutes=4)
        await service.tick()
        await asyncio.shield(service.scenarios.tasks["toy"])
        assert given(folder)["now"] != first

        service.scenarios.set_enabled("toy", False)
        clock.at += timedelta(minutes=10)
        await service.tick()
        assert service.scenarios.scenario_view(service.scenarios.scenario("toy"))["state"] == "disabled"
        service.scenarios.set_enabled("off", True)  # switching on selects at once
        await asyncio.shield(service.scenarios.tasks["off"])

    asyncio.run(go())


def test_broken_scenarios_are_listed_with_their_error(make):
    folder = make_scenario()
    (folder / "scenario.toml").write_text('max_items = "many"')
    service, _, _ = make()
    [view] = service.scenarios.list_scenarios()
    assert view["state"] == "broken" and "max_items" in view["error"]


def test_scenario_check_command(capsys):
    folder = make_scenario(toml='title = "Toy"\n[settings]\nlogin = "me"', steps=("review",))
    cli.main(["scenario", "check", str(folder)])
    out = capsys.readouterr().out
    assert out.startswith("toy: ok — Toy, select every 5m, on") and "steps: review" in out and "login='me'" in out
    (folder / "select").unlink()
    with pytest.raises(SystemExit):
        cli.main(["scenario", "check", str(folder)])


def test_select_gets_the_scenario_in_its_environment(make):
    folder = make_scenario()
    script = folder / "select"
    script.write_text(TOY_SELECT.format(python=sys.executable).replace(
        "data = json.load(sys.stdin)", "data = json.load(sys.stdin); data['env'] = os.environ['LOOPS_ROUTINES_SCENARIO']"))
    service, _, _ = make()
    asyncio.run(service.scenarios.select_now("toy"))
    assert given(folder)["env"] == "toy" and given(folder)["settings"] == {}
    assert os.path.isfile(paths.log_dir() / "scenario-toy.log")
