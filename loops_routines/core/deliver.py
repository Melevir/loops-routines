"""What is kept and said about a finished run: a Markdown report file always; a notification from the
daemon only when the agent could not send one itself (a broken run, a forgotten alert)."""

from pathlib import Path

from loops_routines.core import paths

BROKEN_OUTCOMES = ("failed", "timeout")  # the agent may never have reached its last step


def daemon_should_post(notify: str, outcome: str, status: str, agent_posted: bool) -> bool:
    """Does the daemon notify about this run? A broken run (unless notify = never), or an alert the agent
    did not send although its routine wants alerts heard."""
    if notify == "never":
        return False
    if outcome in BROKEN_OUTCOMES:
        return True
    return status == "alert" and not agent_posted


def should_mark_unseen(notify: str, outcome: str, status: str) -> bool:
    """Does the run count as an alert the user has not looked at yet (the web page's badge)?"""
    if notify == "never":
        return False
    return status == "alert" or outcome in BROKEN_OUTCOMES


def headline(title: str, outcome: str, status: str, summary: str, error: str | None) -> str:
    """One line for a notification."""
    if outcome == "ok":
        return f"{title}: {summary or status}"
    words = {"failed": "failed", "timeout": "timed out", "stopped": "stopped", "lost": "was lost"}
    return f"{title} {words.get(outcome, outcome)}: {error or summary or 'no details'}"


def write_report(run: dict, title: str) -> Path:
    """reports/<routine>/<run id>.md: the run's result as a file the user can keep or open."""
    folder = paths.reports_dir() / run["routine"]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{run['id']:06d}.md"
    lines = [f"# {title}: run {run['id']}", "",
             f"- started: {run['started_at']}", f"- finished: {run.get('finished_at') or ''}",
             f"- outcome: {run.get('outcome')}, status: {run.get('status')}"]
    if run.get("error"):
        lines.append(f"- error: {run['error']}")
    if run.get("denied"):
        lines.append(f"- denied: {'; '.join(run['denied'])}")
    lines += ["", f"**{run.get('summary') or ''}**", "", run.get("report") or ""]
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path
