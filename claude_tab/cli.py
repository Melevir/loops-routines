"""claude-tab: the core daemon and commands that talk to its API.

    claude-tab daemon                    run the daemon in the foreground (launchd runs this)
    claude-tab open                      the daemon's web page in the browser
    claude-tab status                    health, usage and every routine
    claude-tab show NAME                 one routine and its last runs
    claude-tab run NAME [--force|--now]  start a routine now (--now: outside the queue)
    claude-tab enable|disable|reset NAME switch a routine on / off / back to its file
    claude-tab runs [NAME] [-n N]        recent runs
    claude-tab get RUN                   one run's result and report
    claude-tab log RUN [-n N]            the end of a run's log
    claude-tab stop RUN                  stop a run
    claude-tab say RUN TEXT              send a running agent a message
    claude-tab usage [--probe]           the plan's windows
    claude-tab integrations [--scan]     what routines can name in mcp_servers / plugins / skills
    claude-tab questions                 what running agents are asking you
    claude-tab answer QID BUTTON         answer one (the dialog closes)
    claude-tab notify-test [--kind K] [--ask]   a test notification, or a test question dialog
    claude-tab reload                    re-read config.toml and routines now
    claude-tab watch                     follow the daemon's live events
    claude-tab check FILE.md             validate a routine file without the daemon
    claude-tab install [--launchd] [--mcp]   set up ~/.claude_tab, the LaunchAgent, the MCP registration
    claude-tab restart                   restart the LaunchAgent's daemon (after an upgrade)
    claude-tab uninstall                 remove the LaunchAgent
"""

import argparse
import json
import os
import sys
import webbrowser
from datetime import UTC, datetime

from claude_tab.client import Client, DaemonError


def _local(iso: str | None) -> str:
    if not iso:
        return "-"
    return datetime.fromisoformat(iso).astimezone().strftime("%a %d %b %H:%M")


def _print_json(data) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _routine_line(r: dict) -> str:
    state = r["state"]
    detail = {"running": f"run {r.get('running_run_id')}: {r.get('reason') or ''}",
              "waiting": r.get("reason") or "", "idle": f"next {_local(r.get('next_at'))}",
              "disabled": "off", "broken": r.get("reason") or ""}[state]
    last = r.get("last_run") or {}
    last_text = f"  last: {last.get('status') or last.get('outcome')} {last.get('summary') or ''}".rstrip() \
        if last else ""
    return f"{r['name']:<22} {state:<9} {r.get('schedule_text', ''):<16} {detail}{last_text}"[:200]


def _run_line(r: dict) -> str:
    what = r["state"] if r["state"] != "finished" else f"{r['outcome']}/{r['status']}"
    text = r.get("summary") or r.get("error") or (r.get("live") or {}).get("last_event") or ""
    cost = f" ${r['cost_usd']:.2f}" if r.get("cost_usd") else ""
    return f"#{r['id']:<5} {_local(r['started_at']):<17} {r['routine']:<20} {what:<14}{cost} {text}"[:220]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="claude-tab", description="Claude Code routines on a schedule and a budget")
    parser.add_argument("--json", action="store_true", help="print the API's JSON")
    parser.add_argument("--root", metavar="DIR",
                        help="a separate instance: DIR/home, DIR/state and DIR/logs instead of the usual folders")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("daemon")
    sub.add_parser("open")
    sub.add_parser("status")
    p = sub.add_parser("show")
    p.add_argument("name")
    p = sub.add_parser("run")
    p.add_argument("name")
    p.add_argument("--force", action="store_true", help="ignore the routine's usage limits")
    p.add_argument("--now", action="store_true",
                   help="outside the queue: even with all slots busy, whatever the limits and window say")
    for name in ("enable", "disable", "reset"):
        sub.add_parser(name).add_argument("name")
    p = sub.add_parser("runs")
    p.add_argument("name", nargs="?")
    p.add_argument("-n", type=int, default=20)
    p.add_argument("--no-skipped", action="store_true")
    sub.add_parser("get").add_argument("run", type=int)
    p = sub.add_parser("log")
    p.add_argument("run", type=int)
    p.add_argument("-n", type=int, default=60)
    sub.add_parser("stop").add_argument("run", type=int)
    p = sub.add_parser("say")
    p.add_argument("run", type=int)
    p.add_argument("text", nargs="+")
    sub.add_parser("usage").add_argument("--probe", action="store_true")
    sub.add_parser("integrations").add_argument("--scan", action="store_true")
    sub.add_parser("questions")
    p = sub.add_parser("answer")
    p.add_argument("question", type=int)
    p.add_argument("button", nargs="+")
    p = sub.add_parser("notify-test")
    p.add_argument("--kind", default="info", choices=["info", "success", "warning", "error"])
    p.add_argument("--ask", action="store_true", help="a question dialog instead of a banner")
    sub.add_parser("reload")
    sub.add_parser("watch")
    sub.add_parser("check").add_argument("file")
    p = sub.add_parser("install")
    p.add_argument("--launchd", action="store_true", help="install and load the LaunchAgent")
    p.add_argument("--mcp", action="store_true", help="register the MCP server in Claude Code")
    sub.add_parser("restart")
    sub.add_parser("uninstall")
    args = parser.parse_args(argv)
    if args.root:
        root = os.path.abspath(os.path.expanduser(args.root))
        for env, folder in (("CLAUDE_TAB_HOME", "home"), ("CLAUDE_TAB_STATE", "state"), ("CLAUDE_TAB_LOGS", "logs")):
            os.environ[env] = os.path.join(root, folder)

    try:
        _dispatch(args, parser)
    except DaemonError as exc:
        print(f"claude-tab: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        pass


def _dispatch(args, parser) -> None:
    command = args.command
    if command is None:
        parser.print_help()
        return
    if command == "daemon":
        from claude_tab.daemon.main import main as daemon_main
        daemon_main()
        return
    if command == "install":
        from claude_tab.install import install
        for line in install(launchd=args.launchd, mcp=args.mcp):
            print(line)
        return
    if command == "restart":
        from claude_tab.install import restart_launch_agent
        for line in restart_launch_agent():
            print(line)
        return
    if command == "uninstall":
        from claude_tab.install import uninstall_launch_agent
        for line in uninstall_launch_agent():
            print(line)
        return
    if command == "check":
        _check(args.file)
        return

    client = Client()
    if command == "open":
        url = f"{client.base_url}/ui/#token={client.token}"
        webbrowser.open(url)
        return
    if command == "watch":
        for event in client.events():
            print(f"{_local(event['at'])}  {event['type']:<20} {json.dumps(event['data'], ensure_ascii=False)[:160]}")
        return

    result = {
        "status": lambda: client.get("/api/summary"),
        "show": lambda: client.get(f"/api/routines/{args.name}"),
        "run": lambda: client.post(f"/api/routines/{args.name}/run", {"force": args.force, "out_of_queue": args.now}),
        "enable": lambda: client.post(f"/api/routines/{args.name}/enabled", {"enabled": True}),
        "disable": lambda: client.post(f"/api/routines/{args.name}/enabled", {"enabled": False}),
        "reset": lambda: client.post(f"/api/routines/{args.name}/enabled", {"enabled": None}),
        "runs": lambda: client.get("/api/runs", routine=args.name, limit=args.n,
                                   skipped="0" if args.no_skipped else None),
        "get": lambda: client.get(f"/api/runs/{args.run}"),
        "log": lambda: client.get(f"/api/runs/{args.run}/log", lines=args.n),
        "stop": lambda: client.post(f"/api/runs/{args.run}/stop"),
        "say": lambda: client.post(f"/api/runs/{args.run}/message", {"text": " ".join(args.text)}),
        "usage": lambda: client.post("/api/usage/probe", timeout=120) if args.probe else client.get("/api/usage"),
        "integrations": lambda: client.post("/api/integrations/scan") if args.scan else client.get("/api/integrations"),
        "reload": lambda: client.post("/api/reload"),
        "questions": lambda: client.get("/api/questions"),
        "answer": lambda: client.post(f"/api/questions/{args.question}/answer", {"answer": " ".join(args.button)}),
        "notify-test": lambda: client.post("/api/notify/test", {"kind": args.kind, "ask": args.ask}, timeout=150),
    }[command]()
    if args.json or command in ("integrations",):
        _print_json(result)
        return
    _render(command, result)


def _render(command: str, result) -> None:
    if command == "status":
        health, usage = result["health"], result["usage"]
        print(f"daemon {health['version']} pid {health['pid']} on :{health['port']}  "
              f"running {health['running']}/{health['max_parallel']}  unseen alerts {health['unseen_alerts']}")
        for problem in health["problems"] + ([health["account_problem"]] if health.get("account_problem") else []):
            print(f"  ! {problem}")
        print(_usage_line(usage))
        print()
        for routine in result["routines"]:
            print(_routine_line(routine))
        return
    if command == "show":
        print(_routine_line(result))
        for key in ("schedule", "window", "grace", "profile", "limits", "priority", "notify", "model",
                    "max_runtime", "repos", "mcp_servers", "path"):
            if key in result:
                print(f"  {key:<12} {result[key]}")
        for problem in result.get("problems") or []:
            print(f"  ! {problem}")
        print()
        for run in result.get("runs") or []:
            print(_run_line(run))
        return
    if command == "runs":
        for run in result:
            print(_run_line(run))
        return
    if command == "get":
        print(_run_line(result))
        if result.get("error"):
            print(f"error: {result['error']}")
        for denied in result.get("denied") or []:
            print(f"denied: {denied}")
        for message in result.get("messages") or []:
            print(f"  [{message['kind']}] {message['text']}")
        if result.get("report"):
            print()
            print(result["report"])
        return
    if command == "log":
        print(result)
        return
    if command == "usage":
        print(_usage_line(result))
        return
    if command == "questions":
        for q in result:
            print(f"#{q['id']}  {q['title']} (run {q['run_id']}), until {_local(q['deadline'])}: {q['question']}")
            print(f"     answer: claude-tab answer {q['id']} {' | '.join(q['buttons'])}")
        if not result:
            print("no open questions")
        return
    if isinstance(result, (dict, list)):
        _print_json(result)
    else:
        print(result)


def _usage_line(usage: dict) -> str:
    parts = [f"account {usage['account']}"]
    for name, label in (("five_hour", "5h"), ("seven_day", "7d")):
        w = usage.get(name)
        parts.append(f"{label} {w['free_percentage']:.0f}% free (resets {_local(w['resets_at'])})" if w else f"{label} ?")
    age = usage.get("age_minutes")
    parts.append(f"numbers {age:.0f}m old" if age is not None else "no numbers yet")
    if usage.get("probe", {}).get("error"):
        parts.append(f"probe: {usage['probe']['error']}")
    return "  ".join(parts)


def _check(file: str) -> None:
    from pathlib import Path

    from claude_tab.core import config
    from claude_tab.core.routines import parse_routine
    path = Path(file).expanduser()
    try:
        routine = parse_routine(path.stem, path.read_text(encoding="utf-8"), config.load(), str(path))
    except (ValueError, OSError) as exc:
        print(f"{path}: {exc}", file=sys.stderr)
        sys.exit(1)
    from datetime import datetime
    now = datetime.now(UTC)
    upcoming, at = [], now
    for _ in range(3):
        at = routine.trigger.next_after(at)
        upcoming.append(_local(at.isoformat()))
    print(f"{routine.name}: ok — {routine.trigger.describe()}, next: {', '.join(upcoming)}")
    print(f"  limits {routine.limits} window {routine.window} priority {routine.priority} notify {routine.notify}")
    for problem in routine.problems:
        print(f"  ! {problem}")
