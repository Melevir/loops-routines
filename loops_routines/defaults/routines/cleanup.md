+++
title = "Cleanup"
schedule = "every 2h"
profile = "spare"             # only when the 5-hour window has 60%+ free and resets within 2h:
                              # tokens that would expire anyway
priority = 20
max_runtime = "90m"
max_turns = 150
repos = ["your-org/your-repo"]
allowed_tools = ["Bash(make *)", "Bash(uv *)", "Bash(pytest *)", "Bash(git push -u origin *)",
                 "Bash(gh pr create --draft *)"]
notify = "on_alert"
enabled = false               # set the repository, then switch it on
+++
You work in the cloned repository {repos}. Pick one small, safe cleanup: dead code, a duplicated helper, a
missing test for existing behaviour, a misleading name. Read its CLAUDE.md first and follow it.

Before you start, check `gh pr list --author @me --state open` so you don't redo a cleanup that is already open.
Make the change on a new branch `cleanup/<short-name>`, run the tests, push it and open a draft PR with
`gh pr create --draft`.

status "ok" with the PR link in the summary; "nothing" if you found nothing worth a PR; "alert" only if you
found something broken that a person should look at.
