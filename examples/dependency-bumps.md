+++
title = "Dependency bumps"
schedule = "0 16 * * fri"
profile = "spare"
allowed_tools = ["Bash(gh pr review * --approve*)"]
notify = "always"            # a banner with the outcome; the agent may also ask you
+++
List the open Dependabot PRs in your-org/backend with green CI. Read each changelog for breaking
changes. Then ask me with `ask_user`: "N bumps are green, M look risky. Approve the safe ones?" with
the buttons "Approve safe ones", "Show me first", "Skip". Approve only on "Approve safe ones"; on any
other answer, or no answer, approve nothing and list them in your report.
