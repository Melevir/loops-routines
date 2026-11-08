# How loops-routines works

One process serves everything: the scheduler, the REST API with live events, the MCP server for your
Claude sessions, and the web page. It runs as a LaunchAgent (`loops-routines install --launchd`): it
starts at login, launchd restarts it if it stops, and it runs as you, so agents use your Claude sign-in,
keychain and tools. The LaunchAgent keeps the `PATH` of the terminal you installed it from.

```
 launchd (KeepAlive)
   └─ loops-routines daemon ──────────────────────────────────────────────────────┐
        Service.tick (every 30 s): reload files → wind down runs past keep_free →  │
          evaluate each routine → expire / wait / launch by priority → probe usage │
        Run × max_parallel → `claude -p` stream-json, own process group, own folder│
        Store (SQLite, the daemon's only)     deliver: reports/*.md                │
        REST /api/* + SSE /api/events   MCP /mcp (user)   MCP /agent/mcp (runs)    │
        /ui/ — a web page on the same API                                          │
 ──────────────────────────────────────────────────────────────────────────────────┘
   ▲ REST + SSE              ▲ MCP                    ▲ REST
 browser                    Claude Code              CLI

 run ── /agent/mcp notify ──▶ daemon ──▶ banner (Notification Center)
 run ── /agent/mcp ask_user ─▶ daemon ──▶ dialog with buttons ──┐  first answer wins:
                               ◀── answer ── web page / CLI / MCP ┘  the dialog, or any of these
```

## Where things live

| Path | What |
|---|---|
| `~/.loops_routines/config.toml` | the daemon, the Claude account agents run as, default permissions, limit profiles |
| `~/.loops_routines/routines/*.md` | one file per routine; re-read on every tick |
| `~/Library/Application Support/loops_routines/` | `state.db`, the API token, run folders, `reports/<routine>/<run>.md` |
| `~/Library/Logs/loops_routines/` | `daemon.log`, one log per run |

## Modules

| Module | What it does |
|---|---|
| `core/triggers.py` | cron (5 fields, names, steps, `@daily`) and `every 30m`; `next_after(moment)` |
| `core/windows.py` | time windows `mon-fri 09:00-19:00` |
| `core/routines.py` | routine files → `Routine`; broken files are kept with their error |
| `core/evaluate.py` | the pure decision: idle / wait (why) / launch / expire for one routine |
| `core/usage.py`, `core/probe.py` | 5-hour and 7-day snapshots, the limit gates, the one-request probe |
| `core/integrations.py` | plugins, MCP servers, skills a routine names → `claude` flags |
| `core/runner.py` | one run: clones, per-run config files, stream-json in/out, stop, wrap-up, result file |
| `core/store.py` | SQLite: routine state, runs (and skipped occurrences), messages from agents |
| `service.py` | the scheduler tick and every query and action; REST, MCP and CLI are thin over it |
| `daemon/app.py`, `daemon/mcp.py`, `daemon/auth.py` | HTTP transports and the token check |
| `core/notify.py` | macOS banners and button dialogs through osascript; the prompt rules per `notify` |
| `ui/web/index.html` | the web page (`loops-routines open`) |

## Scheduling rules

- Occurrences come from the trigger; only the **latest** due one counts, so a Mac that slept for a day
  runs a routine once, not 48 times.
- An occurrence may wait for its window and limits until its deadline: `grace` after it, or (without
  `grace`) until the next occurrence. Past it, it is recorded as **skipped** with the reason it waited for.
- Limits: `min_free` and `before_reset` decide the start; `keep_free` / `keep_free_weekly` also wind a
  running run down (it gets `wind_down_minutes` to write its result, then it is stopped).
- When numbers are older than `max_snapshot_age_minutes`, the daemon spends one haiku request on a
  probe (at most every `probe_cooldown_minutes`). Every run also refreshes them from its own events.
- Several due routines: higher `priority` first, up to `max_parallel` at once; a routine never overlaps
  itself. "Run now" does not use up an occurrence; "run now, outside the queue" also ignores
  `max_parallel`, the window and the limits.
- A routine switched off (in its file or the UI) owes nothing for the time it was off.

## Results

The agent ends by writing `{"status": "ok | alert | nothing", "summary", "report"}` to its result file.
The daemon stores it and writes `reports/<routine>/<run>.md`. Without a result file, the agent's last
words become the summary with status `unknown`. While it works, the agent may post `report_status`
lines through `/agent/mcp`, and you can message it (`loops-routines say RUN TEXT`).

## Notifications and questions

Agents use two tools of `/agent/mcp`, as their routine's `notify` allows (`always`: the outcome when it
finishes; `on_alert`: only an alert; `never`: neither tool):

- `notify(text, kind)` — a Notification Center banner titled with the routine; `kind` is info, success,
  warning or error (error and warning play `[notify] error_sound`).
- `ask_user(question, buttons, timeout_minutes)` — a system dialog with 1–3 buttons of the agent's wording
  (the recommended one is the default, on the right) and waits. The first answer wins: a click in the
  dialog, the web page, `loops-routines answer`, or `answer_question` over MCP; the dialog then closes. No
  answer in time, or the run ends: the agent gets `timed_out` / `cancelled` and takes the safe path.

The daemon notifies by itself only what no agent can: a run that failed or timed out, an alert the agent
did not send, and a `report_blocker`. Everything is kept with the run.

Why a dialog and not buttons on the banner: macOS gives notification actions only to a signed app with its
own notification permission; osascript banners can't have buttons. The first banner may make macOS ask
whether "Script Editor" may notify — allow it in System Settings → Notifications. `loops-routines
notify-test` (and `--ask`) checks both.

## Security

The daemon listens on 127.0.0.1 only and checks the Host header. `/api` and `/mcp` need the token in
`~/Library/Application Support/loops_routines/token` (0600); `/agent/mcp` accepts only a live run's own token,
so an agent can't post as another run. The UI page gets the token from its URL fragment (never sent to
the server) and keeps it for the tab only. See [SECURITY.md](../SECURITY.md) for the threat model.

## REST API

```
GET  /health                         GET  /api/summary
GET  /api/routines                   GET  /api/routines/{name}
POST /api/routines/{name}/run {force, out_of_queue}
POST /api/routines/{name}/enabled {enabled: true|false|null}
GET  /api/runs?routine=&limit=&before=&skipped=0
GET  /api/runs/{id}                  GET  /api/runs/{id}/log?lines=
POST /api/runs/{id}/stop             POST /api/runs/{id}/message {text}
POST /api/runs/seen {ids}
GET  /api/usage                      POST /api/usage/probe
GET  /api/integrations               POST /api/integrations/scan
GET  /api/questions                  POST /api/questions/{id}/answer {answer}
POST /api/notify/test {kind, ask}
POST /api/reload                     GET  /api/events  (SSE)
```

## MCP tools

`/mcp` (your Claude Code sessions): `list_routines`, `routine_status`, `run_routine`, `run_routine_now`
(outside the queue: on top of `max_parallel`, whatever its limits and window say), `set_routine_enabled`,
`list_runs`, `get_run`, `run_log`, `stop_run`, `message_run`, `usage`, `pending_questions`,
`answer_question`.

`/agent/mcp` (runs only): `report_status`, `report_blocker`, `notify`, `ask_user`.

## Running from a checkout

```bash
uv sync
uv run loops-routines --root /tmp/lr install   # a separate instance: /tmp/lr/home, state, logs
# set [daemon] port in /tmp/lr/home/config.toml so it doesn't clash with an installed daemon, then:
uv run loops-routines --root /tmp/lr daemon
uv run pytest
```
