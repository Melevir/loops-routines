# Contributing

Thanks for looking. Issues with a clear "what I did / what I expected / what happened" are as welcome as code.

## Setup

Needs macOS, [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```bash
uv sync
uv run pytest
uv run ruff check .
```

The tests never touch your real setup: they run in temporary folders, start the real runner against a fake
`claude` script (`tests/conftest.py`), and replace notifications with a fake notifier. Keep it that way: a test
must not call `launchctl`, `osascript` or a real `claude`.

To try a change against a real `claude` without disturbing an installed daemon, run a separate instance on
another port:

```bash
uv run claude-tab --root /tmp/ct install
# set [daemon] port in /tmp/ct/home/config.toml, then:
uv run claude-tab --root /tmp/ct daemon
```

## Changes

- One change per pull request, with a test that fails without it.
- `service.py` holds the behaviour; REST (`daemon/app.py`), MCP (`daemon/mcp.py`) and the CLI stay thin
  over it. A new action goes into `Service` first, then into whichever transports need it.
- MCP tools are `async def`: the MCP SDK runs sync tools in a worker thread, where the daemon's event loop
  isn't available.
- Add a line under "Unreleased" in `CHANGELOG.md` for anything a user would notice.

## Releases

Maintainers tag `vX.Y.Z` on `main`; CI builds and publishes to PyPI.
