import time

import pytest
from conftest import FakeNotifier, write_routine
from starlette.testclient import TestClient

from loops_routines.core import paths
from loops_routines.core.store import Store
from loops_routines.daemon.app import build_app
from loops_routines.daemon.auth import ensure_token, host_ok
from loops_routines.service import Service

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(fake_claude):
    paths.config_path().write_text(f'[daemon]\ntimezone = "UTC"\n[agent]\ncommand = "{fake_claude}"\n')
    write_routine("mon", 'title = "Log monitor"\nschedule = "every 30m"\nprofile = "unlimited"')

    service = Service(Store(paths.db_path()), notifier=FakeNotifier())
    app = build_app(service, token=TOKEN, run_scheduler=False, allow_any_host=True)
    with TestClient(app) as c:
        c.service = service
        yield c


def test_auth(client):
    assert client.get("/health").status_code == 200
    assert client.get("/api/routines").status_code == 401
    assert client.get("/api/routines", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/routines", headers=AUTH).status_code == 200
    assert client.post("/agent/mcp", headers=AUTH, json={}).status_code == 401  # the user's token is not a run's
    assert client.get("/ui/").status_code == 200


def test_host_check():
    assert host_ok([(b"host", b"127.0.0.1:47920")])
    assert host_ok([(b"host", b"localhost:47920")])
    assert not host_ok([(b"host", b"evil.example:47920")])


def test_token_file_is_private():
    token = ensure_token()
    assert token == ensure_token()
    assert oct(paths.token_path().stat().st_mode)[-3:] == "600"


def test_routines_and_errors(client):
    routines = client.get("/api/routines", headers=AUTH).json()
    assert routines[0]["name"] == "mon" and routines[0]["title"] == "Log monitor"
    assert client.get("/api/routines/nope", headers=AUTH).status_code == 404
    response = client.post("/api/routines/mon/enabled", headers=AUTH, json={"enabled": "yes"})
    assert response.status_code == 400
    assert client.post("/api/routines/mon/enabled", headers=AUTH, json={"enabled": False}).json()["enabled"] is False
    assert client.post("/api/routines/mon/enabled", headers=AUTH, json={"enabled": None}).json()["enabled"] is True
    summary = client.get("/api/summary", headers=AUTH).json()
    assert set(summary) == {"health", "usage", "routines", "runs", "questions", "scenarios"}
    assert client.get("/api/questions", headers=AUTH).json() == []
    assert client.post("/api/questions/9/answer", headers=AUTH, json={"answer": "Yes"}).status_code == 404
    assert client.post("/api/notify/test", headers=AUTH, json={"kind": "error"}).json()["shown"] is True
    assert client.post("/api/notify/test", headers=AUTH, json={"kind": "loud"}).status_code == 400


def test_an_items_question_through_the_api(client):
    from test_scenarios import make_scenario
    make_scenario(decisions=[{"key": "o/r#1", "action": "ask", "text": "Merge now?", "answers": ["Merge", "Wait"]}])
    client.service.notifier.block = True
    client.post("/api/reload", headers=AUTH)
    assert client.post("/api/scenarios/toy/select", headers=AUTH).status_code == 200
    [q] = client.get("/api/questions", headers=AUTH).json()
    assert (q["scenario"], q["key"], q["question"]) == ("toy", "o/r#1", "Merge now?")
    item = client.get("/api/scenarios/toy/item", headers=AUTH, params={"key": "o/r#1"}).json()
    assert item["question"]["answers"] == ["Merge", "Wait"]
    bad = client.post("/api/scenarios/toy/item/answer", headers=AUTH, json={"key": "o/r#1", "answer": "Never"})
    assert bad.status_code == 400
    answer = client.post("/api/scenarios/toy/item/answer", headers=AUTH, json={"key": "o/r#1", "answer": "wait"})
    assert answer.json()["answer"] == "Wait" and client.get("/api/questions", headers=AUTH).json() == []


def test_run_through_the_api(client):
    run_id = client.post("/api/routines/mon/run", headers=AUTH, json={}).json()["run_id"]
    assert client.post("/api/routines/mon/run", headers=AUTH, json={}).status_code in (200, 409)
    for _ in range(200):
        run = client.get(f"/api/runs/{run_id}", headers=AUTH).json()
        if run["state"] == "finished":
            break
        time.sleep(0.05)
    assert run["outcome"] == "ok" and run["summary"] == "all ok"
    log = client.get(f"/api/runs/{run_id}/log?lines=5", headers=AUTH)
    assert log.status_code == 200 and "exit 0" in log.text
    assert client.post(f"/api/runs/{run_id}/stop", headers=AUTH).status_code == 409  # not running any more
    assert client.post("/api/runs/seen", headers=AUTH, json={"ids": [run_id]}).status_code == 200
    runs = client.get("/api/runs?routine=mon&limit=5", headers=AUTH).json()
    assert runs and runs[0]["id"] in (run_id, run_id + 1)
