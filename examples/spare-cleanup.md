+++
title = "Spare-token cleanup"
schedule = "every 2h"
profile = "spare"            # only with 60%+ of the 5-hour window free and a reset within 2 hours
priority = 20                # anything more important goes first
repos = ["your-org/backend"] # cloned fresh into the run's own folder
allowed_tools = ["Bash(make *)", "Bash(uv *)", "Bash(git push -u origin cleanup/*)",
                 "Bash(gh pr create --draft *)"]
max_runtime = "90m"
notify = "on_alert"
+++
In {repos}, pick one small, safe cleanup: dead code, a duplicated helper, a missing test, a misleading
name. Check `gh pr list --author @me` first so you don't redo an open one. Work on a branch
`cleanup/<name>`, run the tests, push, and open a draft PR. Summary: the PR link, or "nothing" if
nothing was worth a PR.
