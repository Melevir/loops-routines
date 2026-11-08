+++
title = "Weekly changelog"
schedule = "0 9 * * mon"     # Mondays 09:00
grace = "1d"                 # the Mac slept through it? catch up during the day
model = "sonnet"
notify = "always"            # a banner with the one-line summary when it's ready
+++
Summarise what was merged in the 7 days before {now} in these repositories:
- your-org/backend
- your-org/frontend

Use `gh pr list --repo <repo> --state merged --search "merged:>=YYYY-MM-DD"`. Group the changes by
theme (features, fixes, internal) with PR links and authors, then a short "worth knowing" section:
risky changes, migrations, removed things. Summary: the PR count and the most important change.
