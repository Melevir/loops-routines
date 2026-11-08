"""`claude-tab daemon`: the service and its HTTP app on 127.0.0.1."""

import sys

from claude_tab.core import paths
from claude_tab.core.clock import utc_now
from claude_tab.core.store import Store
from claude_tab.daemon.app import serve
from claude_tab.service import Service


def main() -> None:
    paths.state_dir().mkdir(parents=True, exist_ok=True)
    paths.log_dir().mkdir(parents=True, exist_ok=True)
    store = Store(paths.db_path())
    service = Service(store)
    for problem in service.cfg.problems:
        print(f"config: {problem}", file=sys.stderr)
    print(f"{utc_now()} claude-tab daemon on http://127.0.0.1:{service.port} "
          f"({len(service.routines)} routines, {len(service.broken)} broken)", flush=True)
    serve(service, service.port)
