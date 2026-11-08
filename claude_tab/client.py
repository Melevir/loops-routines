"""A small client of the daemon's REST API, for the CLI and the menu bar app."""

import json
from collections.abc import Iterator

import httpx

from claude_tab.core import config, paths


class DaemonError(RuntimeError):
    pass


class Client:
    def __init__(self, base_url: str | None = None, token: str | None = None, timeout: float = 30):
        port = config.load().daemon.port
        self.base_url = (base_url or f"http://127.0.0.1:{port}").rstrip("/")
        self.token = token if token is not None else read_token()
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout,
                                  headers={"Authorization": f"Bearer {self.token}"} if self.token else {})

    def _call(self, method: str, path: str, **kwargs):
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.ConnectError:
            raise DaemonError(f"the daemon is not running at {self.base_url} (start it: claude-tab daemon, "
                              "or claude-tab install --launchd)") from None
        if response.status_code >= 400:
            try:
                message = response.json().get("error") or response.text
            except ValueError:
                message = response.text
            raise DaemonError(f"{response.status_code}: {message}")
        if response.headers.get("content-type", "").startswith("application/json"):
            return response.json()
        return response.text

    def get(self, path: str, **params):
        return self._call("GET", path, params={k: v for k, v in params.items() if v is not None})

    def post(self, path: str, body: dict | None = None, timeout: float | None = None):
        kwargs = {"json": body or {}}
        if timeout:
            kwargs["timeout"] = timeout
        return self._call("POST", path, **kwargs)

    def events(self) -> Iterator[dict]:
        """The daemon's live events, until the connection ends."""
        try:
            with self._http.stream("GET", "/api/events", timeout=httpx.Timeout(10, read=None)) as response:
                if response.status_code >= 400:
                    raise DaemonError(f"{response.status_code}: {response.read().decode(errors='replace')}")
                data = []
                for line in response.iter_lines():
                    if line.startswith("data:"):
                        data.append(line[5:].strip())
                    elif not line and data:
                        yield json.loads("\n".join(data))
                        data = []
        except httpx.ConnectError:
            raise DaemonError(f"the daemon is not running at {self.base_url}") from None


def read_token() -> str:
    try:
        return paths.token_path().read_text().strip()
    except FileNotFoundError:
        return ""
