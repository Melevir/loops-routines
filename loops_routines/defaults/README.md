# ~/.loops_routines

Everything here is yours to edit. The daemon reads it on every tick (30 s), so nothing needs a restart.

- `config.toml` — the daemon (port, parallel runs, timezone), the Claude account the agents run as, the
  default tool permissions, and limit profiles.
- `routines/*.md` — one file per routine: a TOML header between `+++` lines, then the prompt. The file
  name (without `.md`) is the routine's name. The shipped examples are switched off (`enabled = false`).

## A routine's header

| key | meaning | default |
|---|---|---|
| `schedule` | cron (`0 9 * * mon`), `@daily`, or `every 30m` | required |
| `grace` | how long an occurrence may wait for its window and limits before it is skipped | until the next occurrence |
| `window` | only start inside it: `mon-fri 08:00-20:00`, several joined by `;` | `*` |
| `profile` | limit profile: `any`, `spare`, `unlimited`, or your own `[profiles.x]` | `any` |
| `min_free` | start only with this % of the 5-hour window free | profile's |
| `before_reset` | start only if the 5-hour window resets within N minutes (0 = any time) | profile's |
| `keep_free` / `keep_free_weekly` | leave this % alone; reaching it winds the run down | profile's |
| `priority` | 0-100, higher starts first when several are due | 50 |
| `model`, `effort`, `max_turns`, `max_runtime` | the run's model and bounds | `[agent] model`, -, 50, 30m |
| `repos` | `owner/name` repositories cloned into the run folder | none |
| `mcp_servers`, `plugins`, `skills` | integrations the run may use (`loops-routines integrations`) | none |
| `allowed_tools`, `disallowed_tools` | permission rules on top of `[agent]`'s | none |
| `notify` | macOS notifications and questions: `always` (the outcome), `on_alert` (alerts and broken runs), `never` (none, and no questions) | `on_alert` |
| `enabled` | the UI's switch overrides it | `true` |

Placeholders in the prompt: `{name} {title} {run_id} {now} {last_run_at} {previous_status}
{previous_summary} {previous_report} {repos}`. Any other `{...}` stays as written.

Every run ends with a result the agent writes: `{"status": "ok | alert | nothing", "summary", "report"}`.
The daemon keeps it and writes it to `reports/<routine>/<run>.md` in its state folder. The agent tells
you things itself as `notify` says: a macOS notification (`notify`), or a dialog with its own buttons
that waits for your click (`ask_user`; also answerable with `loops-routines answer` or on the web page).
The daemon notifies only runs that broke and alerts the agent did not send. `[notify]` in config.toml
sets the sounds and how long a question waits.
