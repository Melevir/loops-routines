"""Scenarios, phase 3: the apply hook (after a step, and for an `apply` decision) and `ask` questions."""

import asyncio
import json
import stat
import sys

import pytest
from conftest import FakeNotifier
from test_scenarios import Clock, given, make_scenario, say

from loops_routines import cli
from loops_routines.core import paths
from loops_routines.core.notify import Answer
from loops_routines.core.runner import Run
from loops_routines.core.scenarios import parse_applied, parse_decisions
from loops_routines.core.store import Store
from loops_routines.errors import NotFound
from loops_routines.service import Service

UNLIMITED = 'select_every = "5m"\n[defaults]\nprofile = "unlimited"\n'

# Appends what it was given to apply_inputs.json, prints apply_out.json (or nothing); apply_exit.txt fails it.
TOY_APPLY = """#!{python}
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
data = json.load(sys.stdin)
log = os.path.join(here, "apply_inputs.json")
seen = json.load(open(log)) if os.path.exists(log) else []
json.dump(seen + [data], open(log, "w"))
if os.path.exists(os.path.join(here, "apply_exit.txt")):
    print("cannot merge: branch protected", file=sys.stderr)
    sys.exit(1)
out = os.path.join(here, "apply_out.json")
if os.path.exists(out):
    print(open(out).read())
"""


def with_apply(folder, out: dict | None = None):
    path = folder / "apply"
    path.write_text(TOY_APPLY.format(python=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    if out is not None:
        (folder / "apply_out.json").write_text(json.dumps(out))
    return folder


def applied_inputs(folder) -> list[dict]:
    path = folder / "apply_inputs.json"
    return json.loads(path.read_text()) if path.exists() else []


@pytest.fixture
def setup(fake_claude):
    paths.config_path().write_text(
        f'[daemon]\ntimezone = "UTC"\nmax_parallel = 2\n[agent]\ncommand = "{fake_claude}"\n')
    notifier = FakeNotifier()

    def quick_run(spec, on_event, on_usage):
        return Run(spec, on_event, on_usage, end_grace=0.1)

    def make(start="2026-11-09T10:00:00"):
        return Service(Store(paths.db_path()), now=Clock(start), notifier=notifier, run_factory=quick_run), notifier

    return make


async def until(check, timeout: float = 20) -> None:
    for _ in range(int(timeout * 20)):
        if check():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("timed out waiting")


async def quiet(service: Service, timeout: float = 20) -> None:
    """No run, no select and no apply going (a question's waiter may stay)."""
    eng = service.scenarios
    await until(lambda: not service.active and not eng.applying and not any(eng.selecting(n) for n in eng.scenarios),
                timeout)


def item(service, key, name="toy"):
    return service.scenarios.item_detail(name, key)


def events(service, key, kind):
    return [e for e in item(service, key)["events"] if e["kind"] == kind]


# --- the contracts -------------------------------------------------------------------------------


def test_the_apply_contract():
    assert parse_applied("") == parse_applied("  \n")
    out = parse_applied('{"state": {"posted": "ab12"}, "notify": {"text": "Review  posted", "kind": "success"}}')
    assert (out.state, out.notify, out.kind, out.error) == ({"posted": "ab12"}, "Review posted", "success", None)
    assert parse_applied('{"notify": "Merged", "error": "half done"}').error == "half done"
    for bad, match in (("[1]", "JSON object"), ("nope", "did not print JSON"), ('{"state": 1}', "state must be"),
                       ('{"notify": {"text": "x", "kind": "loud"}}', "notify must be")):
        with pytest.raises(ValueError, match=match):
            parse_applied(bad)


def test_ask_decisions_need_answers_the_dialog_can_show():
    decisions, problems = parse_decisions(json.dumps([
        {"key": "a", "action": "ask", "text": "Merge now?", "answers": ["Merge", "Wait"], "timeout_minutes": 30},
        {"key": "b", "action": "ask", "text": "Default answers"},
        {"key": "c", "action": "ask", "text": "Too many", "answers": ["1", "2", "3", "4"]},
        {"key": "d", "action": "ask", "text": "Same", "answers": ["Yes", "yes"]},
        {"key": "e", "action": "ask", "text": "Forever", "timeout_minutes": 5000},
    ]), ())
    a, b = decisions
    assert (a.answers, a.timeout_minutes, a.name, b.answers) == (["Merge", "Wait"], 30, "ask", ["Yes", "No"])
    assert [p.split(":")[0] for p in problems] == ["decision 3 (c)", "decision 4 (d)", "decision 5 (e)"]


# --- apply after a step -----------------------------------------------------------------------------


def test_apply_gets_a_steps_result_and_patches_the_state(setup):
    folder = with_apply(make_scenario(toml=UNLIMITED, steps=("review",), decisions=[
        {"key": "o/r#1", "action": "run", "step": "review", "version": "v1", "vars": {"x": "it"},
         "state": {"rounds": 1}}]),
        out={"state": {"posted": "v1", "rounds": None}, "notify": {"text": "Review posted", "kind": "success"}})
    service, notes = setup()

    async def go():
        await service.scenarios.select_now("toy")
        await service.tick()
        await quiet(service)
        [sent] = applied_inputs(folder)
        assert (sent["source"], sent["step"], sent["version"], sent["vars"]) == ("step", "review", "v1", {"x": "it"})
        assert sent["result"]["outcome"] == "ok" and sent["result"]["data"]["prompt"].startswith("Do it")
        assert sent["item"]["state"] == {"rounds": 1}

        now = item(service, "o/r#1")
        # apply removed rounds; the select after it patched it back in
        assert now["state"] == {"posted": "v1", "rounds": 1} and now["notice"] == "apply: Review posted"
        assert [e for e in now["events"] if e["kind"] == "apply"][0]["data"]["notify"] == "Review posted"
        assert ("toy: o/r#1", "Review posted", "success") == notes.banners[-1][:3]
        # the select after it saw the run and the apply, in that order
        kinds = [(e["kind"], e["name"]) for e in given(folder)["items"]["o/r#1"]["events"]]
        assert kinds == [("run", "finished"), ("apply", "ok")]

    asyncio.run(go())


def test_a_failed_step_is_not_applied_and_a_failed_apply_is_told(setup, monkeypatch):
    folder = with_apply(make_scenario(toml=UNLIMITED, steps=("review",), decisions=[
        {"key": "o/r#1", "action": "run", "step": "review", "version": "v1"}]))
    (folder / "apply_exit.txt").write_text("1")
    service, notes = setup()

    async def go():
        monkeypatch.setenv("FAKE_MODE", "fail")
        await service.scenarios.select_now("toy")
        await service.tick()
        await quiet(service)
        assert applied_inputs(folder) == []  # no result: nothing to apply

        monkeypatch.setenv("FAKE_MODE", "ok")
        say(folder, [{"key": "o/r#1", "action": "run", "step": "review", "version": "v2"}])
        await service.scenarios.select_now("toy")
        await service.tick()
        await quiet(service)
        [event] = [e for e in given(folder)["items"]["o/r#1"]["events"] if e["kind"] == "apply"]
        assert event["name"] == "failed" and "cannot merge: branch protected" in event["data"]["error"]
        assert [b[1] for b in notes.banners if b[2] == "warning"] == \
            ["apply review failed: apply exited with 1: cannot merge: branch protected"]

    asyncio.run(go())


# --- the apply action ---------------------------------------------------------------------------------


def test_an_apply_decision_runs_once_per_version(setup):
    folder = with_apply(make_scenario(decisions=[
        {"key": "o/r#1", "action": "apply", "step": "merge", "version": "c3", "vars": {"method": "squash"},
         "reason": "approved and green"}]), out={"state": {"merged": True}})
    service, _ = setup()

    async def go():
        await service.scenarios.select_now("toy")
        await quiet(service)
        [sent] = applied_inputs(folder)
        assert (sent["source"], sent["step"], sent["vars"], sent["result"]) == ("decision", "merge", {"method": "squash"}, None)
        # the apply's own select ran after it; it still says apply merge c3: not again
        assert given(folder)["items"]["o/r#1"]["last"]["action"] == "applied"
        now = item(service, "o/r#1")
        assert now["state"] == {"merged": True} and now["queue"] == "apply merge already done for version c3"
        await service.scenarios.select_now("toy")
        await quiet(service)
        assert len(applied_inputs(folder)) == 1

    asyncio.run(go())


def test_an_apply_that_keeps_failing_makes_the_item_stuck(setup):
    folder = with_apply(make_scenario(toml='max_attempts = 2\nselect_every = "5m"', decisions=[
        {"key": "o/r#1", "action": "apply", "step": "merge", "version": "c3"}]))
    (folder / "apply_exit.txt").write_text("1")
    service, notes = setup()

    async def go():
        await service.scenarios.select_now("toy")  # tries, fails, selects again: tries again, fails
        await quiet(service)
        await service.scenarios.select_now("toy")
        await quiet(service)
        assert len(applied_inputs(folder)) == 2
        stuck = item(service, "o/r#1")
        assert stuck["action"] == "stuck" and stuck["reason"].startswith("stuck: apply merge failed 2 times")
        assert len([b for b in notes.banners if b[1].startswith("stuck")]) == 1

        say(folder, [{"key": "o/r#1", "action": "apply", "step": "merge", "version": "c4"}])
        (folder / "apply_exit.txt").unlink()
        await service.scenarios.select_now("toy")
        await quiet(service)
        assert given(folder)["items"]["o/r#1"]["last"]["action"] == "applied"  # a new version starts afresh
        assert item(service, "o/r#1")["queue"] == "apply merge already done for version c4"

    asyncio.run(go())


def test_apply_without_the_hook_is_a_problem(setup):
    make_scenario(decisions=[{"key": "o/r#1", "action": "apply", "step": "merge"}])
    service, _ = setup()
    detail = asyncio.run(service.scenarios.select_now("toy"))
    assert detail["problems"] == ["o/r#1: apply: the scenario has no `apply` executable"]


# --- ask --------------------------------------------------------------------------------------------


def test_a_question_is_asked_once_and_its_answer_comes_back(setup):
    folder = make_scenario(decisions=[
        {"key": "o/r#1", "action": "ask", "name": "merge", "text": "Merge o/r#1 now?", "answers": ["Merge", "Wait"],
         "once_per": "c3", "title": "Fix login"}])
    service, notes = setup()
    notes.block = True  # the dialog stays open

    async def go():
        await service.scenarios.select_now("toy")
        await until(lambda: notes.asks)
        assert notes.asks == [("toy: Fix login", "Merge o/r#1 now?", ["Merge", "Wait"], 24 * 60 * 60)]
        q = item(service, "o/r#1")["question"]
        assert (q["name"], q["text"], q["answers"]) == ("merge", "Merge o/r#1 now?", ["Merge", "Wait"])
        [pending] = service.pending_questions()
        assert (pending["scenario"], pending["key"], pending["run_id"]) == ("toy", "o/r#1", None)

        await service.scenarios.select_now("toy")  # select keeps asking: the same question stays open
        assert len(notes.asks) == 1
        assert given(folder)["items"]["o/r#1"]["asking"]["question_id"] == q["question_id"]

        cli_out = service.scenarios.answer("toy", "o/r#1", "merge")
        assert cli_out["answer"] == "Merge"

        def answered():
            return [e for e in given(folder)["items"]["o/r#1"]["events"] if e["kind"] == "answer"]
        await until(answered)
        await quiet(service)
        [event] = answered()
        assert event["name"] == "Merge" and event["data"]["status"] == "answered"
        assert event["data"]["via"] == "through the API"
        now = item(service, "o/r#1")
        assert now["question"] is None and now["notice"] == "merge: Merge"

        await service.scenarios.select_now("toy")  # answered for c3: not asked again
        assert len(notes.asks) == 1 and item(service, "o/r#1")["question"] is None
        with pytest.raises(NotFound, match="no open question"):
            service.scenarios.answer("toy", "o/r#1", "Merge")

        say(folder, [{"key": "o/r#1", "action": "ask", "name": "merge", "text": "Merge o/r#1 now?",
                      "answers": ["Merge", "Wait"], "once_per": "c4"}])  # a new commit: asked again
        await service.scenarios.select_now("toy")
        await until(lambda: len(notes.asks) == 2)
        say(folder, [{"key": "o/r#1", "action": "wait", "reason": "CI running"}])  # select stops asking
        await service.scenarios.select_now("toy")
        assert item(service, "o/r#1")["question"] is None and service.pending_questions() == []

    asyncio.run(go())


def test_an_unanswered_question_times_out_into_an_event(setup):
    folder = make_scenario(decisions=[{"key": "o/r#1", "action": "ask", "text": "Merge?", "timeout_minutes": 5}])
    service, notes = setup()
    notes.answer = Answer("timed_out")  # the dialog gives up at once

    async def go():
        await service.scenarios.select_now("toy")
        await quiet(service)
        [event] = [e for e in given(folder)["items"]["o/r#1"]["events"] if e["kind"] == "answer"]
        assert event["name"] is None and event["data"]["status"] == "timed_out"
        assert notes.asks[0][3] == 5 * 60
        await service.scenarios.select_now("toy")
        assert len(notes.asks) == 1  # once per text: not again

    asyncio.run(go())


def test_untrack_closes_the_question(setup):
    make_scenario(decisions=[{"key": "o/r#1", "action": "ask", "text": "Merge?"}])
    service, notes = setup()
    notes.block = True

    async def go():
        await service.scenarios.select_now("toy")
        assert service.pending_questions()
        line = cli._item_line(service.scenarios.list_items("toy")[0])
        assert "asks: Merge? (Yes | No)" in line
        service.scenarios.untrack("toy", "o/r#1")
        assert service.pending_questions() == []
        await quiet(service)
        assert not events(service, "o/r#1", "answer")  # a cancelled question is no answer

    asyncio.run(go())
