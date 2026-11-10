"""loops-routines: the core daemon and commands that talk to its API.

    loops-routines daemon                    run the daemon in the foreground (launchd runs this)
    loops-routines open                      the daemon's web page in the browser
    loops-routines status                    health, usage and every routine
    loops-routines show NAME                 one routine and its last runs
    loops-routines run NAME [--force|--now]  start a routine now (--now: outside the queue)
    loops-routines enable|disable|reset NAME switch a routine on / off / back to its file
    loops-routines runs [NAME] [-n N]        recent runs
    loops-routines get RUN                   one run's result and report
    loops-routines log RUN [-n N]            the end of a run's log
    loops-routines stop RUN                  stop a run
    loops-routines say RUN TEXT              send a running agent a message
    loops-routines usage [--probe]           the plan's windows
    loops-routines integrations [--scan]     what routines can name in mcp_servers / plugins / skills
    loops-routines questions                 what running agents are asking you
    loops-routines answer QID BUTTON         answer one (the dialog closes)
    loops-routines notify-test [--kind K] [--ask]   a test notification, or a test question dialog
    loops-routines reload                    re-read config.toml and routines now
    loops-routines watch                     follow the daemon's live events
    loops-routines check FILE.md             validate a routine file without the daemon
    loops-routines install [--launchd] [--mcp]   set up ~/.loops_routines, the LaunchAgent, the MCP registration
    loops-routines restart                   restart the LaunchAgent's daemon (after an upgrade)
    loops-routines uninstall                 remove the LaunchAgent

    loops-routines scenarios                 every scenario and its state
    loops-routines scenario show NAME [--done]          one scenario and the items it follows
    loops-routines scenario enable|disable|reset NAME   switch it on / off / back to scenario.toml
    loops-routines scenario select NAME                 run its select now and show the result
    loops-routines scenario item NAME KEY               one item: state, decision, history
    loops-routines scenario track NAME KEY [--title T] [--url U]   follow an item
    loops-routines scenario untrack NAME KEY            stop following it
    loops-routines scenario press NAME KEY BUTTON       press one of the item's buttons
    loops-routines scenario answer NAME KEY ANSWER      answer the item's open question
    loops-routines scenario pause|resume NAME KEY       nothing is done for a paused item
    loops-routines scenario run NAME KEY STEP [--force] start a step for the item now
    loops-routines scenario step NAME STEP on|off|reset switch one step on / off / back to its file
    loops-routines scenario check DIR                   validate a scenario folder without the daemon
"""

import argparse
import json
import os
import sys
import webbrowser
from datetime import UTC, datetime

from loops_routines.client import Client, DaemonError


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
    parser = argparse.ArgumentParser(prog="loops-routines", description="Claude Code routines on a schedule and a budget")
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
    sub.add_parser("scenarios")
    sc = sub.add_parser("scenario").add_subparsers(dest="scenario_command", required=True)
    p = sc.add_parser("show")
    p.add_argument("name")
    p.add_argument("--done", action="store_true", help="also the items it no longer follows")
    for name in ("enable", "disable", "reset", "select"):
        sc.add_parser(name).add_argument("name")
    for name in ("item", "untrack", "pause", "resume"):
        p = sc.add_parser(name)
        p.add_argument("name")
        p.add_argument("key")
    p = sc.add_parser("track")
    p.add_argument("name")
    p.add_argument("key")
    p.add_argument("--title")
    p.add_argument("--url")
    p = sc.add_parser("press")
    p.add_argument("name")
    p.add_argument("key")
    p.add_argument("button", nargs="+")
    p = sc.add_parser("answer")
    p.add_argument("name")
    p.add_argument("key")
    p.add_argument("answer", nargs="+")
    p = sc.add_parser("run")
    p.add_argument("name")
    p.add_argument("key")
    p.add_argument("step")
    p.add_argument("--force", action="store_true", help="even when the step's usage limits say no")
    p = sc.add_parser("step")
    p.add_argument("name")
    p.add_argument("step")
    p.add_argument("switch", choices=("on", "off", "reset"))
    sc.add_parser("check").add_argument("folder")
    args = parser.parse_args(argv)
    if args.root:
        root = os.path.abspath(os.path.expanduser(args.root))
        for env, folder in (("LOOPS_ROUTINES_HOME", "home"), ("LOOPS_ROUTINES_STATE", "state"), ("LOOPS_ROUTINES_LOGS", "logs")):
            os.environ[env] = os.path.join(root, folder)

    try:
        _dispatch(args, parser)
    except DaemonError as exc:
        print(f"loops-routines: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        pass


def _dispatch(args, parser) -> None:
    command = args.command
    if command is None:
        parser.print_help()
        return
    if command == "daemon":
        from loops_routines.daemon.main import main as daemon_main
        daemon_main()
        return
    if command == "install":
        from loops_routines.install import install
        for line in install(launchd=args.launchd, mcp=args.mcp):
            print(line)
        return
    if command == "restart":
        from loops_routines.install import restart_launch_agent
        for line in restart_launch_agent():
            print(line)
        return
    if command == "uninstall":
        from loops_routines.install import uninstall_launch_agent
        for line in uninstall_launch_agent():
            print(line)
        return
    if command == "check":
        _check(args.file)
        return
    if command == "scenario" and args.scenario_command == "check":
        _check_scenario(args.folder)
        return

    client = Client()
    if command in ("scenarios", "scenario"):
        _scenario(client, args)
        return
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
            whose = f"run {q['run_id']}" if q.get("run_id") is not None else f"{q.get('scenario')}: {q.get('key')}"
            print(f"#{q['id']}  {q['title']} ({whose}), until {_local(q['deadline'])}: {q['question']}")
            print(f"     answer: loops-routines answer {q['id']} {' | '.join(q['buttons'])}")
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


def _scenario(client: Client, args) -> None:
    if args.command == "scenarios":
        result = client.get("/api/scenarios")
        if args.json:
            _print_json(result)
            return
        for s in result:
            print(_scenario_line(s))
        if not result:
            print("no scenarios: add a folder to ~/.loops_routines/scenarios/")
        return
    command, base = args.scenario_command, f"/api/scenarios/{args.name}"
    key = getattr(args, "key", None)
    result = {
        "show": lambda: client.get(base, done="1" if args.done else None),
        "enable": lambda: client.post(f"{base}/enabled", {"enabled": True}),
        "disable": lambda: client.post(f"{base}/enabled", {"enabled": False}),
        "reset": lambda: client.post(f"{base}/enabled", {"enabled": None}),
        "select": lambda: client.post(f"{base}/select", timeout=660),
        "item": lambda: client.get(f"{base}/item", key=key),
        "track": lambda: client.post(f"{base}/items", {"key": key, "title": args.title, "url": args.url}),
        "untrack": lambda: client.post(f"{base}/item/untrack", {"key": key}),
        "press": lambda: client.post(f"{base}/item/press", {"key": key, "button": " ".join(args.button)}),
        "answer": lambda: client.post(f"{base}/item/answer", {"key": key, "answer": " ".join(args.answer)}),
        "pause": lambda: client.post(f"{base}/item/pause", {"key": key, "paused": True}),
        "resume": lambda: client.post(f"{base}/item/pause", {"key": key, "paused": False}),
        "run": lambda: client.post(f"{base}/item/run", {"key": key, "step": args.step, "force": args.force}),
        "step": lambda: client.post(f"{base}/steps/{getattr(args, 'step', '')}/enabled",
                                    {"enabled": {"on": True, "off": False, "reset": None}[getattr(args, "switch", "reset")]}),
    }[command]()
    if args.json or command in ("item",):
        _print_json(result)
        return
    if command in ("show", "select"):
        print(_scenario_line(result))
        for problem in result.get("problems") or []:
            print(f"  ! {problem}")
        for step in result.get("step_list") or []:
            print(f"  step {step['name']:<16} {'on ' if step['enabled'] else 'off'}{' (switched)' if step['switched'] else ''}"
                  f"  {step['model'] or 'default model'}, {step['profile']}, priority {step['priority']}, "
                  f"max {step['max_runtime']}")
        print()
        for item in result.get("item_list") or []:
            print(_item_line(item))
        if not result.get("item_list"):
            print("no items yet")
        return
    if command in ("enable", "disable", "reset", "step"):
        print(_scenario_line(result))
        return
    if command == "run":
        print(f"started {result['step']} for {result['key']}: run {result['run_id']} "
              f"(loops-routines get {result['run_id']})")
        return
    if command == "press":
        print(f"pressed {result['pressed']} on {result['key']}: its select runs now")
        return
    if command == "answer":
        print(f"answered {result['answer']} on {result['key']}: its select runs now")
        return
    print(_item_line(result))


def _scenario_line(s: dict) -> str:
    if s["state"] == "broken":
        return f"{s['name']:<22} broken    {s.get('error') or ''}"
    counts = ", ".join(f"{n} {a}" for a, n in sorted((s.get("by_action") or {}).items()))
    detail = s.get("error") if s["state"] == "error" else f"every {s['select_every']}, last {_local(s.get('last_select_at'))}"
    return f"{s['name']:<22} {s['state']:<9} {s['items']} items{f' ({counts})' if counts else ''}  {detail}"[:220]


def _item_line(i: dict) -> str:
    what = (i.get("action") or "new") + (f" {i['step']}" if i.get("step") else "")
    flags = "".join(f" [{f}]" for f, on in (("tracked", i.get("tracked")), ("paused", i.get("paused")),
                                            ("done", i.get("done_at"))) if on)
    buttons = f"  buttons: {' | '.join(i['buttons'])}" if i.get("buttons") else ""
    queue = f" · {i['queue']}" if i.get("queue") else ""
    q = i.get("question")
    question = f"  asks: {q['text']} ({' | '.join(q['answers'])})" if q else ""
    return f"{i['key']:<28} {what:<16} {i.get('reason') or ''}{queue}{flags}{buttons}{question}"[:300]


def _check_scenario(folder: str) -> None:
    from pathlib import Path

    from loops_routines.core import config
    from loops_routines.core.scenarios import parse_scenario
    path = Path(folder).expanduser()
    try:
        sc = parse_scenario(path, config.load())
    except (ValueError, OSError) as exc:
        print(f"{path}: {exc}", file=sys.stderr)
        sys.exit(1)
    from datetime import timedelta

    from loops_routines.core.triggers import fmt_duration
    print(f"{sc.name}: ok — {sc.title}, select every {fmt_duration(timedelta(seconds=sc.select_every))}, "
          f"{'on' if sc.enabled else 'off'}")
    print(f"  steps: {', '.join(sc.steps) or 'none'}   apply: {'yes' if sc.has_apply else 'no'}   "
          f"max_items {sc.max_items}   max_attempts {sc.max_attempts}")
    if sc.settings:
        print(f"  settings: {', '.join(f'{k}={v!r}' for k, v in sc.settings.items())}")
    for step, spec in sc.specs.items():
        print(f"  step {step}: {spec.model or 'default model'}, profile {spec.profile}, "
              f"max_runtime {fmt_duration(spec.max_runtime)}{'' if spec.enabled else ', off'}")
    for problem in sc.problems:
        print(f"  ! {problem}")


def _check(file: str) -> None:
    from pathlib import Path

    from loops_routines.core import config
    from loops_routines.core.routines import parse_routine
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
