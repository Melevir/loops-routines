+++
title = "Log monitor"
schedule = "every 30m"
grace = "20m"                 # a check that could not start within 20 minutes is skipped, not caught up
priority = 80                 # before the others when several are due
profile = "any"
keep_free = 5
model = "sonnet"
effort = "low"
max_turns = 30
max_runtime = "10m"
mcp_servers = []              # your logs server, as `claude-tab integrations` lists it,
                              # e.g. ["sentry"] or ["plugin:<plugin>:<server>"]
notify = "on_alert"           # quiet while everything is fine
enabled = false               # name your services and logs server, then switch it on (here or in the UI)
+++
Check the production logs of these services for errors since {last_run_at}:
- my-api
- my-worker

Use the logs MCP server you have. Group the errors by message and service, with counts and the first and last time
each one happened.

Your previous report, to tell new errors from known ones:
{previous_report}

status "alert" only for an error that is new since the previous report, or one whose rate went up a lot;
"ok" when the logs are clean or show only known errors at their usual rate. The summary names the worst
new error in one line; the report lists every error group with what changed.
