"""The daemon's HTTP app: REST + SSE for the UI and the CLI, MCP for Claude, the UI page itself.

    GET  /health                          open: liveness, version, counts
    GET  /ui/                             open: the UI page (it reads the token from its URL fragment)
    GET  /api/summary                     health + usage + routines + recent runs, for one UI render
    GET  /api/routines                    POST /api/routines/{name}/run {force, out_of_queue}
    GET  /api/routines/{name}             POST /api/routines/{name}/enabled {enabled: true|false|null}
    GET  /api/runs?routine=&limit=&before=&skipped=0
    GET  /api/runs/{id}                   GET  /api/runs/{id}/log?lines=
    POST /api/runs/{id}/stop              POST /api/runs/{id}/message {text}
    POST /api/runs/seen {ids}
    GET  /api/usage                       POST /api/usage/probe
    GET  /api/integrations                POST /api/integrations/scan
    GET  /api/questions                   POST /api/questions/{id}/answer {answer}
    POST /api/notify/test {kind, ask}     a banner, or a dialog that waits for the click
    POST /api/reload
    GET  /api/events                      Server-Sent Events: run.*, routine.*, routines.*, question.*, usage.updated
    /mcp, /agent/mcp                      see daemon.mcp
"""

import asyncio
import json
import sys
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from loops_routines.daemon import mcp as mcp_mod
from loops_routines.daemon.auth import AuthMiddleware, ensure_token
from loops_routines.service import Conflict, NotFound, Service

UI_DIR = Path(__file__).resolve().parent.parent / "ui" / "web"
PING_SECONDS = 15


def _error(exc: Exception) -> JSONResponse:
    if isinstance(exc, NotFound):
        return JSONResponse({"error": str(exc)}, 404)
    if isinstance(exc, Conflict):
        return JSONResponse({"error": str(exc)}, 409)
    return JSONResponse({"error": str(exc)}, 400)


def endpoint(fn):
    """Service errors -> 404 / 409 / 400 with {"error": ...}."""
    async def wrapper(request: Request) -> Response:
        try:
            result = fn(request)
            if asyncio.iscoroutine(result):
                result = await result
        except (NotFound, Conflict, ValueError, KeyError, TypeError) as exc:
            return _error(exc)
        return result if isinstance(result, Response) else JSONResponse(result)
    return wrapper


async def _body(request: Request) -> dict:
    raw = await request.body()
    if not raw:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("the body must be a JSON object")
    return data


def _int(value, name: str, default: int | None = None) -> int | None:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except ValueError:
        raise ValueError(f"{name} must be a number") from None


def build_app(service: Service, *, token: str | None = None, run_scheduler: bool = True,
              allow_any_host: bool = False) -> Starlette:
    token = token or ensure_token()
    user_mcp, agent_mcp = mcp_mod.user_server(service), mcp_mod.agent_server(service)
    user_mcp_app = user_mcp.streamable_http_app(streamable_http_path="/mcp")
    agent_mcp_app = agent_mcp.streamable_http_app(streamable_http_path="/agent/mcp")

    s = service

    @endpoint
    def health(request):
        return s.health()

    @endpoint
    def summary(request):
        return s.summary()

    @endpoint
    def routines(request):
        return s.list_routines()

    @endpoint
    def routine(request):
        return s.routine_detail(request.path_params["name"])

    @endpoint
    async def run_routine(request):
        body = await _body(request)
        return s.run_now(request.path_params["name"], bool(body.get("force")), bool(body.get("out_of_queue")))

    @endpoint
    async def set_enabled(request):
        body = await _body(request)
        if "enabled" not in body or body["enabled"] not in (True, False, None):
            raise ValueError("send {\"enabled\": true | false | null}")
        return s.set_enabled(request.path_params["name"], body["enabled"])

    @endpoint
    def runs(request):
        q = request.query_params
        return s.list_runs(q.get("routine") or None, _int(q.get("limit"), "limit", 50),
                           _int(q.get("before"), "before"), q.get("skipped", "1") not in ("0", "false", "no"))

    @endpoint
    def run(request):
        return s.get_run(_int(request.path_params["id"], "id"))

    @endpoint
    def run_log(request):
        lines = _int(request.query_params.get("lines"), "lines", 200)
        return PlainTextResponse(s.log_tail(_int(request.path_params["id"], "id"), lines))

    @endpoint
    def stop_run(request):
        return s.stop_run(_int(request.path_params["id"], "id"))

    @endpoint
    async def message_run(request):
        body = await _body(request)
        return s.message_run(_int(request.path_params["id"], "id"), str(body.get("text") or ""))

    @endpoint
    async def seen(request):
        body = await _body(request)
        ids = body.get("ids")
        if not isinstance(ids, list):
            raise ValueError("send {\"ids\": [run ids]}")
        return s.mark_seen(ids)

    @endpoint
    def usage(request):
        return s.usage_view()

    @endpoint
    async def probe(request):
        return await s.probe_now()

    @endpoint
    def questions(request):
        return s.pending_questions()

    @endpoint
    async def answer(request):
        body = await _body(request)
        return s.answer_question(_int(request.path_params["id"], "id"), str(body.get("answer") or ""))

    @endpoint
    async def notify_test(request):
        body = await _body(request)
        return await s.notify_test(str(body.get("kind") or "info"), bool(body.get("ask")))

    @endpoint
    def integrations(request):
        return s.integrations_view()

    @endpoint
    def scan(request):
        return {"started": s.start_scan()}

    @endpoint
    async def reload(request):
        changed = s.reload(force=True)
        await s.tick()
        return {"reloaded": changed, "routines": len(s.routines), "broken": len(s.broken)}

    async def events(request: Request) -> Response:
        queue = s.bus.subscribe()

        async def stream():
            try:
                yield "retry: 3000\n\n"
                yield _sse({"id": 0, "type": "hello", "data": s.health()})
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), PING_SECONDS)
                    except TimeoutError:
                        yield ": ping\n\n"
                        continue
                    yield _sse(event)
            finally:
                s.bus.unsubscribe(queue)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def ui_index(request: Request) -> Response:
        return FileResponse(UI_DIR / "index.html", headers={"Cache-Control": "no-store"})

    @asynccontextmanager
    async def lifespan(app):
        async with user_mcp.session_manager.run(), agent_mcp.session_manager.run():
            loop_task = asyncio.create_task(s.run_forever()) if run_scheduler else None
            try:
                yield
            finally:
                if loop_task:
                    loop_task.cancel()
                try:
                    await s.shutdown()
                except Exception:
                    traceback.print_exc(file=sys.stderr)

    routes = [
        Route("/", lambda r: RedirectResponse("/ui/")),
        Route("/health", health),
        Route("/ui", lambda r: RedirectResponse("/ui/")),
        Route("/ui/", ui_index),
        Mount("/ui", StaticFiles(directory=str(UI_DIR)), name="ui"),
        Route("/api/summary", summary),
        Route("/api/routines", routines),
        Route("/api/routines/{name}", routine),
        Route("/api/routines/{name}/run", run_routine, methods=["POST"]),
        Route("/api/routines/{name}/enabled", set_enabled, methods=["POST"]),
        Route("/api/runs", runs),
        Route("/api/runs/seen", seen, methods=["POST"]),
        Route("/api/runs/{id}", run),
        Route("/api/runs/{id}/log", run_log),
        Route("/api/runs/{id}/stop", stop_run, methods=["POST"]),
        Route("/api/runs/{id}/message", message_run, methods=["POST"]),
        Route("/api/usage", usage),
        Route("/api/usage/probe", probe, methods=["POST"]),
        Route("/api/questions", questions),
        Route("/api/questions/{id}/answer", answer, methods=["POST"]),
        Route("/api/notify/test", notify_test, methods=["POST"]),
        Route("/api/integrations", integrations),
        Route("/api/integrations/scan", scan, methods=["POST"]),
        Route("/api/reload", reload, methods=["POST"]),
        Route("/api/events", events),
        *user_mcp_app.routes,
        *agent_mcp_app.routes,
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.add_middleware(AuthMiddleware, user_token=token, run_for_token=s.run_for_token,
                       allow_any_host=allow_any_host)
    return app


def _sse(event: dict) -> str:
    return f"id: {event['id']}\nevent: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


def serve(service: Service, port: int) -> None:
    import uvicorn

    app = build_app(service)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
