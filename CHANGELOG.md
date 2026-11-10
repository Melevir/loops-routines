# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Scenarios, first part: multi-step workflows over many items, as folders in
  `~/.loops_routines/scenarios/`. A `select` program decides each item's next step without an LLM;
  the daemon runs it on a schedule, keeps each item's state, sends notifications once, and takes
  button presses. `loops-routines scenarios` / `scenario …`, MCP tools and REST endpoints to see and
  steer them. See [docs/design/scenarios.md](docs/design/scenarios.md).
- Scenario steps: `steps/<step>.md` (a routine header without a schedule, `[defaults]` underneath) run
  as agents when `select` decides `run`, sharing the slots and usage limits with the routines by
  priority, within the scenario's `max_items`. A step runs once per item version when it ends well,
  hands `data` back to the scenario, and makes the item "stuck" (one notification) after `max_attempts`
  failures. Steps switch on and off one by one (`scenario step`, `set_step_enabled`) and start by hand
  (`scenario run`, `run_step_now`).
- Scenario apply hooks and questions: an optional `apply` program gets each step's result (and runs
  alone for an `apply` decision, once per item version, with attempts like steps) to write it to the
  world, so step agents need no write tools; its state patch, notification and outcome come back to
  `select`. An `ask` decision opens a question with 1-3 answers, once per `once_per`; the dialog,
  `loops-routines answer` / `scenario answer`, MCP `answer_item_question` or REST answer it, and the
  answer reaches `select` as an event. A press or an answer during a select now starts another one
  right after it.

## [0.1.0] - 2026-11-08

The first release.

### Added

- The daemon: routines on a cron line or an interval, within the plan's 5-hour and 7-day usage limits,
  with priorities, parallel slots, grace periods and time windows.
- REST API with live events, an MCP server for Claude Code sessions, a CLI and a web page.
- `run_routine_now` (MCP) and `loops-routines run --now`: start a routine outside the queue.
- Agent tools: `report_status`, `report_blocker`, `notify` (macOS banners) and `ask_user` (dialogs with
  the agent's own buttons).
- `loops-routines install --launchd --mcp` and `loops-routines restart`.

[Unreleased]: https://github.com/Melevir/loops-routines/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Melevir/loops-routines/releases/tag/v0.1.0
