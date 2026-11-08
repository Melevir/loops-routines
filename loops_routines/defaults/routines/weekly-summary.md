+++
title = "Weekly summary"
schedule = "0 9 * * mon"      # Mondays 09:00 in the daemon's timezone
grace = "1d"                  # the Mac slept through it: catch up during the day, not later
priority = 40
profile = "any"
model = "sonnet"
max_runtime = "30m"
notify = "always"
enabled = false               # edit the list of repositories, then switch it on
+++
Write a summary of what changed last week (the 7 days before {now}) in these repositories:
- your-org/backend
- your-org/frontend

For each repository use `gh pr list --repo <repo> --state merged --search "merged:>=YYYY-MM-DD" --json
number,title,author,mergedAt,url --limit 200` and read the descriptions of the larger PRs with `gh pr view`.

The report: per repository, the changes grouped by theme (features, fixes, internal), each with its PR links
and authors; then a short "worth knowing" section (risky changes, migrations, removed things). The summary:
one line with the PR count and the most important change. status is "ok" ("nothing" for a quiet week).
