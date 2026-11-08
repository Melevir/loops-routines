"""Who may talk to the daemon. It listens on 127.0.0.1 only, and that is not enough: any local process
could start agents that run shell commands and spend the plan. So:

    /health, /ui/*      open (the page holds no data; it gets the token from its URL fragment)
    /api/*, /mcp        the user's token (state/token, 0600): the UI, the CLI, the user's Claude Code
    /agent/mcp          a run's own token, made for that run and gone when it ends: the run is known
                        from the token, so an agent can't post as another run

The Host header must name the loopback address, which stops DNS-rebinding pages in a browser.
"""

import hmac
import json
import os
import secrets

from loops_routines.core import paths

OPEN_PREFIXES = ("/health", "/ui")
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")


def ensure_token() -> str:
    """The user's token, created on first use."""
    path = paths.token_path()
    try:
        token = path.read_text().strip()
        if token:
            return token
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    return token


def bearer(headers: list[tuple[bytes, bytes]]) -> str | None:
    for name, value in headers:
        if name.lower() == b"authorization":
            text = value.decode("latin-1")
            if text.lower().startswith("bearer "):
                return text[7:].strip()
    return None


def host_ok(headers: list[tuple[bytes, bytes]], allow_any_host: bool = False) -> bool:
    if allow_any_host:
        return True
    for name, value in headers:
        if name.lower() == b"host":
            host = value.decode("latin-1").rsplit(":", 1)[0] if not value.startswith(b"[") \
                else value.decode("latin-1").split("]")[0] + "]"
            return host in LOOPBACK_HOSTS
    return False


class AuthMiddleware:
    def __init__(self, app, user_token: str, run_for_token, allow_any_host: bool = False):
        self.app = app
        self.user_token = user_token
        self.run_for_token = run_for_token
        self.allow_any_host = allow_any_host

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = scope.get("headers") or []
        path = scope.get("path", "")
        if not host_ok(headers, self.allow_any_host):
            return await _deny(send, 421, "the daemon answers only on 127.0.0.1")
        if path == "/" or path.startswith(OPEN_PREFIXES):
            return await self.app(scope, receive, send)
        token = bearer(headers)
        if path.startswith("/agent/mcp"):
            if self.run_for_token(token) is None:
                return await _deny(send, 401, "this endpoint is for loops-routines's own runs")
            return await self.app(scope, receive, send)
        if not token or not hmac.compare_digest(token, self.user_token):
            return await _deny(send, 401, f"missing or wrong token (it is in {paths.token_path()})")
        return await self.app(scope, receive, send)


async def _deny(send, status: int, message: str) -> None:
    body = json.dumps({"error": message}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
