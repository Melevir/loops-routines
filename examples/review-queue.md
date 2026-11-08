+++
title = "Review queue"
schedule = "30 8 * * mon-fri"   # weekdays 08:30, before you sit down
window = "mon-fri 07:00-11:00"  # waiting for limits? still only in the morning
model = "sonnet"
notify = "always"
+++
Find the pull requests waiting for my review: `gh search prs --review-requested=@me --state=open`.
For each one: what it changes in one line, its size, CI state, and anything that needs a careful look.
Order them by how long they have waited. Summary: "N PRs waiting, the oldest: <title>".
