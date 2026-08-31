"""Acceptance tests for longrebuttal.server (the FastAPI wiring).

Offline-safe: no network, no aria2c launch, no engine.start(). The server
module must expose `create_app(engine) -> FastAPI` (no side effects) and
`run(port=7451, headless=False)` (creates an Engine, engine.start(port=port),
serves uvicorn on 127.0.0.1).
"""

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from longrebuttal.engine import Engine
from longrebuttal import server


@pytest.fixture()
def client(tmp_path):
    engine = Engine(data_dir=tmp_path / "data")
    app = server.create_app(engine)
    with TestClient(app) as c:
        yield c


def test_status_shape(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"backend", "disk", "settings", "jobs"}
    assert isinstance(body["jobs"], list)
    assert body["backend"]["healthy"] is True
    assert set(body["settings"]) >= {
        "destination", "connections", "stallSensitivity", "serviceInstalled",
    }


def test_resolve_rejects_garbage_with_error_shape(client):
    r = client.post("/api/resolve", json={"url": ""})
    assert 400 <= r.status_code < 500
    assert isinstance(r.json()["error"], str) and r.json()["error"]


def test_add_job_rejects_garbage_with_error_shape(client):
    r = client.post("/api/jobs", json={"url": ""})
    assert 400 <= r.status_code < 500
    assert isinstance(r.json()["error"], str) and r.json()["error"]


def test_job_actions_unknown_id(client):
    assert 400 <= client.post("/api/jobs/nope/pause").status_code < 500
    assert 400 <= client.post("/api/jobs/nope/resume").status_code < 500
    r = client.delete("/api/jobs/nope", params={"deleteFiles": "false"})
    assert 400 <= r.status_code < 500
    assert isinstance(r.json()["error"], str)


def test_settings_put_echoes_saved_object(client, tmp_path):
    dest = str(tmp_path / "models")
    r = client.put("/api/settings", json={
        "destination": dest, "connections": 6, "stallSensitivity": "High",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["destination"] == dest
    assert body["connections"] == 6
    assert body["stallSensitivity"] == "High"
    assert "serviceInstalled" in body
    # persisted: the next status poll reflects it
    assert client.get("/api/status").json()["settings"]["connections"] == 6


def test_static_ui_served_at_root(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "LONG" in r.text.upper()
    # api routes must win over the static mount
    assert client.get("/api/status").headers["content-type"].startswith("application/json")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
