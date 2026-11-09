# Scenarios: multi-step agent workflows over many items

Status: phase 1 (the core) is built: folders, `select`, the actions `wait`, `notify` and `done`, item
state and events, track / untrack / press / pause, CLI, MCP and REST. `run`, `apply` and `ask` decisions
are recorded and shown, not carried out yet (phases 2–3).

## Why

A routine is one prompt on a schedule. Some work is not: PR babysitting looks after each of several
pull requests, and for each one decides, every few minutes, which of seven different agent jobs (or
none) comes next: review, develop, fix CI, prepare for a person, update the branch, check the release,
write a release note. The decision is plain code (CI still running → wait), the jobs need different
models, tools and budgets, and people take part (approve, "sent for review", "fixes ok").

A **scenario** is that shape, made generic and pluggable: a self-contained folder that says which items
it looks after, how to decide each item's next step without an LLM, what each step's agent does, and
what to do with an agent's result. The daemon schedules, budgets, runs, remembers and shows it;
it knows nothing about GitHub, PRs or deploys.

## The module

```
~/.loops_routines/scenarios/babysit/
  scenario.toml        # name, schedule of the selector, defaults, settings (+ their schema for the UI)
  select               # executable: items + their state in → one decision per item out (no LLM)
  apply                # executable, optional: an agent's result → writes to the world → state patch
  steps/
    review.md          # a routine-style header (model, effort, tools, profile, notify, ...) + prompt
    develop.md
    fix_ci.md
    ...
  README.md            # what it does, what it needs (gh login, MCP servers), its settings
```

A scenario is one folder, so it can be a git repository: shared, versioned, installed with
`loops-routines scenario add <git-url>`, compared with its upstream after an update.

### scenario.toml

```toml
title = "PR babysitter"
select_every = "5m"          # how often `select` runs; also right after each step of an item ends
enabled = false
max_items = 3                # items with a step running at once (on top of the daemon's max_parallel)
max_attempts = 2             # failed runs of one step on one item version before it goes to you

[defaults]                   # every step's header inherits these
profile = "any"
model = "sonnet"
notify = "on_alert"

[settings]                   # free-form, handed to select, apply and the step prompts
login = "melevir"
max_rounds = 3
auto_merge = true
merge_window = "mon-thu 10:00-17:00"

[settings.schema]            # optional: types and help text, for validation now and the UI later
max_rounds = { type = "int", min = 1, max = 10, help = "Agent review rounds before it goes to you" }
```

### `select`: the brain, without an LLM

Any executable. The daemon runs it with JSON on stdin and reads JSON from stdout, with a timeout.

In (environment: `LOOPS_ROUTINES_SCENARIO`, `LOOPS_ROUTINES_STATE`; working directory: the scenario's
folder; stderr goes to `~/Library/Logs/loops_routines/scenario-<name>.log`):
```json
{
  "scenario": "babysit",
  "now": "2026-11-09T10:00:00+00:00",
  "settings": {"login": "melevir", "max_rounds": 3},
  "steps": ["develop", "fix_ci", "review"],
  "items": {
    "Melevir/x#12": {"title": "Fix the login race", "url": "https://...", "tracked": true, "paused": false,
                     "state": {"reviewed_sha": "ab12", "review_rounds": 1},
                     "buttons": ["sent_for_review"],
                     "last": {"action": "notify", "step": null, "reason": "your turn", "version": "ab12"},
                     "events": [{"id": 7, "kind": "button", "name": "sent_for_review", "data": null, "at": "..."}]}
  },
  "tracked": ["Melevir/x#12"]
}
```

`items` holds every followed item that is not done. `events` are the ones since the last select
(`button`, `track`); each is handed to one select only.

`tracked`: items a person added by hand (MCP `track`, CLI, UI). The selector may also discover items
itself (`gh pr list --label babysit`).

Out: one decision per item it looks after.
```json
[
  {"key": "Melevir/x#12", "title": "Fix the login race", "url": "https://github.com/...",
   "action": "run", "step": "review", "reason": "new commits since the last review",
   "version": "cd34", "vars": {"pr_url": "...", "head_sha": "cd34", "previous": "..."}},
  {"key": "Melevir/y#7", "action": "wait", "reason": "CI is running"},
  {"key": "Melevir/z#3", "action": "notify", "kind": "yours", "once_per": "ef56",
   "text": "Your review: comment what to change, or send it for review",
   "buttons": ["sent_for_review"]},
  {"key": "Melevir/w#1", "action": "done", "reason": "merged and released"}
]
```

Actions: `run` (an agent step), `apply` (the apply hook alone, no agent: a merge), `wait`, `notify`
(`kind` is the banner's level, `name` which notice it is; sent once per `name` and `once_per`, which
defaults to the text), `ask` (a dialog; the answer comes back as an event), `done` (stop following it;
it stays done until a person tracks it again). Any decision may carry `state` (a patch the daemon
stores; a key set to null is removed), `title`, `url` and `buttons` (what a person may press on the
item; a press becomes an event and starts a select at once). A paused item still gets decisions, but
nothing is done for it. Bad decisions are left out and shown as the scenario's problems; a select that
fails, times out or prints something else than JSON is the scenario's error (one notification per new
error).

The selector holds every rule. It's pure from the daemon's view (state in → decisions out), so a
scenario's rules are tested with plain unit tests, without the daemon or an LLM.

### Steps

`steps/<step>.md` is a routine without a schedule: the same header keys (model, effort, max_turns,
max_runtime, profile and limits, priority, repos, mcp_servers, plugins, skills, allowed_tools,
disallowed_tools, notify, enabled), defaults from `[defaults]`, and a prompt with `{vars.*}`,
`{settings.*}`, `{state.*}` and the usual placeholders. Each step is switched on and off on its own;
a switched-off step makes the selector's `run` a `wait` ("review is off").

The agent ends with the usual result file, plus `data`: whatever its apply needs (review findings,
thread replies).

### `apply`: the writes stay out of the agent's hands

Optional executable. After a step (or for an `apply` action) the daemon runs it with
`{item, step, result, state, settings}` and reads back `{state, notify?, error?}`. This is where the
review is posted, threads are resolved, the PR is merged. The step's agent gets no write tools (its
header denies them), so whatever its prompt or the PR text says, it can't merge or post as you.

### What the daemon adds

- **Item state**: per scenario and key, kept across restarts; the selector's patches merge into it.
- **Budget and slots**: a step run waits for its profile like a routine, shares `max_parallel` with the
  routines, by `priority`; `max_items` caps a scenario; one item never runs two steps at once.
- **Attempts**: a step that fails `max_attempts` times on the same item `version` is not run again;
  the item shows "stuck: needs you" and you get one notification.
- **People**: buttons and `ask` answers from the dialog, the web page, the CLI or MCP become events.
- **Visibility**: every item with its step, reason, notice and history of runs; switch the scenario, a
  step, or an item (pause) on and off.

### Control

| | |
|---|---|
| CLI | `scenarios`, `scenario show NAME`, `scenario enable/disable NAME [STEP]`, `items NAME`, `track NAME KEY`, `untrack`, `press NAME KEY BUTTON`, `step-now NAME KEY STEP`, `scenario check DIR`, `scenario add GIT-URL` |
| MCP | `list_scenarios`, `scenario_status`, `set_scenario_enabled` (scenario or step), `list_items`, `track_item`, `untrack_item`, `press_button`, `run_step_now`, plus the run tools as they are |
| REST | `/api/scenarios…`, `/api/scenarios/{name}/items…`; events on `/api/events` |

## Babysitting as the first module

`scenarios/babysit` in its own repository (`Melevir/loops-routines-babysit`), installed with
`scenario add`. Ported from the rules described above, not tied to the Loops app:

| Part | Content |
|---|---|
| `select` (Python, stdlib + `gh`) | read the followed PRs in one GraphQL call; the next-step rules: closed → done, draft/CI running → wait, new feedback → develop, CI failing → fix_ci (once per commit), unreviewed → review (≤ max_rounds per cycle), behind/conflicting and approved → update_branch, approved and green → apply merge (in merge_window), not prepared → prepare, else the author's turn ("yours", with the sent_for_review / fixes_ok buttons) |
| `steps/` | review, develop, fix_ci, prepare, update_branch; post-merge: release_check (optional, needs a logs MCP server) |
| `apply` (Python + `gh`) | post the review with markers, reply to and resolve threads, edit title/description, merge |
| settings | login, other authors, max_rounds, auto_merge, merge_window, retro on/off, labels |

What stays out of the first version: Jenkins and FlowFast steps (company-specific; a separate module
or settings later), the multi-PR deploy order.

## Plan

Each phase ends usable and tested; the babysitter is the acceptance test of phases 1–4.

1. **Scenarios core.** Loader and validation (`scenario check`), item state and events in SQLite, the
   select runner (subprocess, JSON contract, timeout, errors shown on the scenario), actions `wait`,
   `notify`, `done`; read-only CLI/MCP/REST views. Tested with a toy scenario.
2. **Steps.** Step files on the routine header parser; a run per (item, step) on the existing runner
   (`RunSpec` gets the step and the item instead of a routine), budget/priority/slots shared with
   routines, `max_items`, attempts and "stuck", result `data` into the item, re-select after each run;
   enable/disable per scenario, step and item; `step-now`.
3. **Apply and people.** The apply hook, `apply` actions, buttons and `ask` → events, from the dialog,
   CLI, MCP and REST.
4. **The babysit module.** Its own repo: select with unit tests on recorded PR snapshots, step prompts,
   apply; tried on real PRs with `auto_merge = false` first.
5. **Modules.** `scenario add/update/remove` from git, upstream diff after an update, a scenario's
   README in `scenario show`.
6. **UI.** Scenarios and items on the web page: the item list with step/reason/notice, buttons, step
   switches, settings edited from `[settings.schema]`.

Later, if it pays off: a routine becomes a one-item, one-step scenario internally, so both share one
engine.

## Decisions to confirm

1. **`select`/`apply` as executables with JSON in/out** (any language, own process, testable alone)
   rather than Python modules loaded into the daemon. Recommended.
2. **The babysit module in its own repo**, not inside loops-routines. Recommended: the core stays free
   of GitHub specifics.
3. **Which PRs**: a label (`babysit`) found by the selector, plus `track` by hand. Recommended both.
4. **Release follow-up** (CI on main, logs after the deploy) in the first babysit version, or later.
   Recommended later: phase 4 ends at the merge.
