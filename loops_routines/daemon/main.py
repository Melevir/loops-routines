"""`loops-routines daemon`: the service and its HTTP app on 127.0.0.1."""

import sys

from loops_routines.core import paths
from loops_routines.core.clock import utc_now
from loops_routines.core.store import Store
from loops_routines.daemon.app import serve
from loops_routines.service import Service


def main() -> None:
    paths.state_dir().mkdir(parents=True, exist_ok=True)
    paths.log_dir().mkdir(parents=True, exist_ok=True)
    store = Store(paths.db_path())
    service = Service(store)
    for problem in service.cfg.problems:
        print(f"config: {problem}", file=sys.stderr)
    print(f"{utc_now()} loops-routines daemon on http://127.0.0.1:{service.port} "
          f"({len(service.routines)} routines, {len(service.broken)} broken)", flush=True)
    serve(service, service.port)
