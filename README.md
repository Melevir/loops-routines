# claude_tab

[![CI](https://github.com/Melevir/claude_tab/actions/workflows/ci.yml/badge.svg)](https://github.com/Melevir/claude_tab/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

> An independent project, not made or endorsed by Anthropic. "Claude" and "Claude Code" are Anthropic's
> trademarks; claude_tab only starts the `claude` command you already have.

A daemon that runs Claude Code **routines** (on a cron line or an interval) **within your plan's usage
limits**. One process serves everything: the scheduler, the REST API with live events, the MCP server
for your Claude sessions, and a small web page. Agents tell you things with **macOS notifications** and
ask you questions in **system dialogs with their own buttons**, waiting for your click.

Examples it is made for:

- a weekly summary of the changes in several repositories (`0 9 * * mon`);
- a cleanup every 2 hours, but only with tokens the 5-hour window would lose anyway (`profile = "spare"`);
- a log monitor every 30 minutes that stays quiet until something new breaks (`notify = "on_alert"`).

## Install

Needs macOS, [uv](https://docs.astral.sh/uv/) and [Claude Code](https://claude.com/claude-code) (`claude`
on your `PATH`, signed in).

```bash
uv tool install git+https://github.com/Melevir/claude_tab   # the `claude-tab` command in ~/.local/bin
claude-tab install --launchd --mcp   # ~/.claude_tab, the daemon as a LaunchAgent, the MCP server in Claude Code
claude-tab status
```

`--launchd` writes `~/Library/LaunchAgents/com.claude-tab.daemon.plist` and loads it: the daemon starts now
and at every login, and launchd restarts it if it stops. It runs as you (a LaunchAgent, not a system
LaunchDaemon), so agents use your Claude sign-in, keychain and tools. Run the install from a terminal
whose `PATH` finds `claude` and whatever your routines call: the LaunchAgent keeps that `PATH`.

Upgrade, then restart the daemon so it runs the new code:

```bash
uv tool upgrade claude-tab
claude-tab restart
```

Remove: `claude-tab uninstall` (the LaunchAgent; `~/.claude_tab` is kept), then `uv tool uninstall claude-tab`.

### Before you start

- **macOS only.** Notifications, dialogs and the LaunchAgent are macOS's; the scheduler itself is not, but
  nothing else is supported yet.
- **Routines spend your plan.** Every run is a `claude -p` session on your own sign-in and counts against
  your 5-hour and 7-day windows. Limit profiles (`spare`, `keep_free`) decide how much a routine may take;
  start with `profile = "spare"` for anything that doesn't have to run.
- **Agents act as you.** They run with your files, keychain and `gh` login, limited by the allowed tools in
  `config.toml` and the routine. Read [SECURITY.md](SECURITY.md) before switching on a routine that pushes,
  posts or reads untrusted text.
- The example routines in `~/.claude_tab/routines/` are switched off and name placeholder repositories and
  services: edit them first.

## Quick start (from a checkout)

```bash
uv sync
uv run claude-tab install            # ~/.claude_tab: config.toml, README, three example routines (off)
uv run claude-tab daemon             # or: claude-tab install --launchd  (keeps it running, starts at login)
uv run claude-tab status
uv run claude-tab open               # the web page in the browser
uv run claude-tab install --mcp      # let your Claude Code sessions see and steer the routines
```

A routine is a file `~/.claude_tab/routines/<name>.md`: a TOML header between `+++` lines, then the
prompt. See `claude_tab/defaults/README.md` (copied to `~/.claude_tab/README.md`) for every key, and
`claude-tab check <file>` to validate one and see its next three runs.

## Architecture

```
 launchd (KeepAlive)
   └─ claude-tab daemon ───────────────────────────────────────────────────────────┐
        Service.tick (every 30 s): reload files → wind down runs past keep_free →  │
          evaluate each routine → expire / wait / launch by priority → probe usage   │
        Run × max_parallel → `claude -p` stream-json, own process group, own folder │
        Store (SQLite, the daemon's only)     deliver: reports/*.md                 │
        REST /api/* + SSE /api/events   MCP /mcp (user)   MCP /agent/mcp (runs)    │
        /ui/ — a web page on the same API                                           │
 ────────────────────────────────────────────────────────────────────────────────────┘
   ▲ REST + SSE              ▲ MCP                    ▲ REST
 browser                    Claude Code              CLI

 run ── /agent/mcp notify ──▶ daemon ──▶ banner (Notification Center)
 run ── /agent/mcp ask_user ─▶ daemon ──▶ dialog with buttons ──┐  first answer wins:
                               ◀── answer ── web page / CLI / MCP ┘  the dialog, or any of these
```

| Module | What it does |
|---|---|
| `core/triggers.py` | cron (5 fields, names, steps, `@daily`) and `every 30m`; `next_after(moment)` |
| `core/windows.py` | time windows `mon-fri 09:00-19:00` (from Loops) |
| `core/routines.py` | routine files → `Routine`; broken files are kept with their error |
| `core/evaluate.py` | the pure decision: idle / wait (why) / launch / expire for one routine |
| `core/usage.py`, `core/probe.py` | 5-hour and 7-day snapshots, the limit gates, the one-request probe (from Loops) |
| `core/integrations.py` | plugins, MCP servers, skills a routine names → `claude` flags (from Loops) |
| `core/runner.py` | one run: clones, per-run config files, stream-json in/out, stop, wrap-up, result file |
| `core/store.py` | SQLite: routine state, runs (and skipped occurrences), messages from agents |
| `service.py` | the scheduler tick and every query and action; REST, MCP and CLI are thin over it |
| `daemon/app.py`, `daemon/mcp.py`, `daemon/auth.py` | HTTP transports and the token check |
| `core/notify.py` | macOS banners and button dialogs through osascript; the prompt rules per `notify` |
| `ui/web/index.html` | the web page (`claude-tab open`) |

### Scheduling rules

- Occurrences come from the trigger; only the **latest** due one counts, so a Mac that slept for a day
  runs a routine once, not 48 times.
- An occurrence may wait for its window and limits until its deadline: `grace` after it, or (without
  `grace`) until the next occurrence. Past it, it is recorded as **skipped** with the reason it waited for.
- Limits: `min_free` and `before_reset` decide the start; `keep_free` / `keep_free_weekly` also wind a
  running run down (it gets `wind_down_minutes` to write its result, then it is stopped).
- When numbers are older than `max_snapshot_age_minutes`, the daemon spends one haiku request on a
  probe (at most every `probe_cooldown_minutes`). Every run also refreshes them from its own events.
- Several due routines: higher `priority` first, up to `max_parallel` at once; a routine never overlaps
  itself. "Run now" does not use up an occurrence.
- A routine switched off (in its file or the UI) owes nothing for the time it was off.

### Results

The agent ends by writing `{"status": "ok | alert | nothing", "summary", "report"}` to its result file.
The daemon stores it and writes `reports/<routine>/<run>.md`. Without a result file, the agent's last
words become the summary with status `unknown`. While it works, the agent may post `report_status`
lines through `/agent/mcp`, and you can message it (`claude-tab say RUN TEXT`).

### Notifications and questions

Agents use two tools of `/agent/mcp`, as their routine's `notify` allows (`always`: the outcome when it
finishes; `on_alert`: only an alert; `never`: neither tool):

- `notify(text, kind)` — a Notification Center banner titled with the routine; `kind` is info, success,
  warning or error (error and warning play `[notify] error_sound`).
- `ask_user(question, buttons, timeout_minutes)` — a system dialog with 1–3 buttons of the agent's wording
  (the recommended one is the default, on the right) and waits. The first answer wins: a click in the
  dialog, the web page, `claude-tab answer`, or `answer_question` over MCP; the dialog then closes. No
  answer in time, or the run ends: the agent gets `timed_out` / `cancelled` and takes the safe path.

The daemon notifies by itself only what no agent can: a run that failed or timed out, an alert the agent
did not send, and a `report_blocker`. Everything is kept with the run.

Why a dialog and not buttons on the banner: macOS gives notification actions only to a signed app with its
own notification permission; osascript banners can't have buttons. The first banner may make macOS ask
whether "Script Editor" may notify — allow it in System Settings → Notifications. `claude-tab
notify-test` (and `--ask`) checks both.

### Security

The daemon listens on 127.0.0.1 only and checks the Host header. `/api` and `/mcp` need the token in
`~/Library/Application Support/claude_tab/token` (0600); `/agent/mcp` accepts only a live run's own token,
so an agent can't post as another run. The UI page gets the token from its URL fragment (never sent to
the server) and keeps it for the tab only.

## API

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

MCP `/mcp`: `list_routines`, `routine_status`, `run_routine`, `run_routine_now` (outside the queue: on top
of `max_parallel`, whatever its limits and window say), `set_routine_enabled`, `list_runs`,
`get_run`, `run_log`, `stop_run`, `message_run`, `usage`, `pending_questions`, `answer_question`.
MCP `/agent/mcp` (runs only): `report_status`, `report_blocker`, `notify`, `ask_user`.

## Development

```bash
uv run pytest
uv run claude-tab --root /tmp/ct daemon     # a separate instance: /tmp/ct/home, state, logs
```

Tests run the real runner against a fake `claude` script (`tests/conftest.py`). See
[CONTRIBUTING.md](CONTRIBUTING.md) for the rest, and [CHANGELOG.md](CHANGELOG.md) for what changed.

## License

[MIT](LICENSE).
