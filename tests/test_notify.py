import pytest

from claude_tab.core.deliver import daemon_should_post
from claude_tab.core.notify import check_buttons, instructions


def test_buttons():
    assert check_buttons(None) == ["Yes", "No"]
    assert check_buttons([" Roll  back ", "Keep"]) == ["Roll back", "Keep"]
    for bad in ([], ["a", "b", "c", "d"], ["x" * 31], ["Yes", "yes"], [""]):
        with pytest.raises(ValueError):
            check_buttons(bad)


def test_instructions_per_notify():
    assert "only about an alert" in instructions("on_alert") and "ask_user" in instructions("on_alert")
    assert "When you finish" in instructions("always")
    assert "Do not notify or ask" in instructions("never")


def test_when_the_daemon_notifies():
    assert daemon_should_post("on_alert", "failed", "unknown", False)
    assert daemon_should_post("always", "timeout", "unknown", False)
    assert not daemon_should_post("never", "failed", "unknown", False)
    assert daemon_should_post("on_alert", "ok", "alert", agent_posted=False)
    assert not daemon_should_post("on_alert", "ok", "alert", agent_posted=True)
    assert not daemon_should_post("always", "ok", "ok", agent_posted=False)  # the agent's job
    assert not daemon_should_post("on_alert", "stopped", "unknown", False)  # the user stopped it
