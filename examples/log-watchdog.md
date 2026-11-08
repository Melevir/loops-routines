+++
title = "Log watchdog"
schedule = "every 30m"
grace = "20m"                # a check that couldn't start in 20 minutes is skipped, not piled up
priority = 80
model = "sonnet"
effort = "low"
max_runtime = "10m"
mcp_servers = ["sentry"]     # whatever logs server you have: `loops-routines integrations` lists them
notify = "on_alert"          # silent while all is well
+++
Check production errors for my-api and my-worker since {last_run_at}. Group them by message, with
counts and first/last seen.

Your previous report, to tell new errors from known ones:
{previous_report}

Status "alert" only for an error that is new or whose rate jumped; otherwise "ok".
