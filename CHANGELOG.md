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
  steer them. Agent steps come next. See [docs/design/scenarios.md](docs/design/scenarios.md).

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
