from datetime import timedelta
from pathlib import Path

import pytest
from conftest import cfg, routine_text

from claude_tab.core.routines import fill, load_routines, parse_routine
from claude_tab.install import DEFAULTS


def test_parse_full_header():
    text = routine_text('''
        title = "Log monitor"
        schedule = "every 30m"
        grace = "20m"
        window = "mon-fri 08:00-20:00"
        profile = "spare"
        keep_free = 25
        priority = 80
        model = "sonnet"
        effort = "low"
        max_runtime = "10m"
        repos = ["owner/a"]
        mcp_servers = ["plugin:x:y"]
        notify = "always"
    ''')
    r = parse_routine("monitor-logs", text, cfg())
    assert r.title == "Log monitor"
    assert r.grace == timedelta(minutes=20)
    assert r.limits.min_free == 60 and r.limits.keep_free == 25 and r.limits.before_reset == 120  # spare + override
    assert r.priority == 80 and r.model == "sonnet" and r.effort == "low"
    assert r.max_runtime == timedelta(minutes=10)
    assert r.repos == ("owner/a",) and r.mcp_servers == ("plugin:x:y",)
    assert r.notify == "always" and r.enabled


def test_defaults():
    r = parse_routine("x", routine_text('schedule = "@daily"'), cfg())
    assert r.title == "x" and r.profile == "any" and r.grace is None and r.window == "*"
    assert r.limits.keep_free == 5 and r.notify == "on_alert" and r.priority == 50


@pytest.mark.parametrize("header,message", [
    ('title = "x"', "schedule is missing"),
    ('schedule = "every 30m"\nbogus = 1', "unknown keys"),
    ('schedule = "every 30m"\nprofile = "nope"', "profile"),
    ('schedule = "every 30m"\nnotify = "sometimes"', "notify"),
    ('schedule = "every 30m"\nrepos = ["not a repo"]', "repos"),
    ('schedule = "every 30m"\nwindow = "weekdays"', "days"),
    ('schedule = "every 30m"\npriority = 500', "priority"),
    ('schedule = "every 30m"\nmax_runtime = "10s"', "max_runtime"),
    ('schedule = "0 9 * *"', "cron"),
])
def test_errors(header, message):
    with pytest.raises(ValueError, match=message):
        parse_routine("x", routine_text(header), cfg())


def test_header_is_required_and_prompt_too():
    with pytest.raises(ValueError, match=r"\+\+\+"):
        parse_routine("x", "schedule = 1\n", cfg())
    with pytest.raises(ValueError, match="prompt"):
        parse_routine("x", '+++\nschedule = "@daily"\n+++\n\n', cfg())
    with pytest.raises(ValueError, match="file name"):
        parse_routine("Bad Name", routine_text('schedule = "@daily"'), cfg())


def test_profiles_from_config():
    config = cfg('[profiles.night]\nmin_free = 30\nkeep_free = 10\n')
    r = parse_routine("x", routine_text('schedule = "@daily"\nprofile = "night"'), config)
    assert r.limits.min_free == 30 and r.limits.keep_free == 10


def test_load_splits_good_and_broken(tmp_path):
    folder = tmp_path / "routines"
    folder.mkdir()
    (folder / "good.md").write_text(routine_text('schedule = "@daily"'))
    (folder / "bad.md").write_text(routine_text('schedule = "whenever"'))
    (folder / "README.md").write_text("docs")
    good, broken = load_routines(folder, cfg())
    assert list(good) == ["good"] and list(broken) == ["bad"]
    assert "cron" in broken["bad"].error


def test_fill_leaves_unknown_braces():
    assert fill("a {name} {json} {", name="x") == "a x {json} {"


def test_shipped_examples_parse():
    for path in sorted(Path(DEFAULTS / "routines").glob("*.md")):
        routine = parse_routine(path.stem, path.read_text(), cfg())
        assert routine.enabled is False  # examples stay off until the user edits them
