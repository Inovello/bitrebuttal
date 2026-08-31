"""Acceptance tests for bitrebuttal.server (the FastAPI wiring).

Offline-safe: no network, no aria2c launch, no engine.start(). The server
module must expose `create_app(engine) -> FastAPI` (no side effects) and
`run(port=7451, headless=False)` (creates an Engine, engine.start(port=port),
serves uvicorn on 127.0.0.1).
"""

import json
import time

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from bitrebuttal.engine import Engine
from bitrebuttal import server


@pytest.fixture()
def engine(tmp_path):
    return Engine(data_dir=tmp_path / "data")


@pytest.fixture()
def client(engine):
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


# ---------------------------------------------------------------- v2 additions


def test_status_v2_fields_and_defaults(client):
    body = client.get("/api/status").json()
    assert set(body) >= {"backend", "disk", "settings", "jobs",
                         "recents", "completedToday", "connections", "library"}

    b = body["backend"]
    assert set(b) >= {"healthy", "label", "version", "uptime", "aria2cVersion", "gui"}
    assert isinstance(b["aria2cVersion"], str)
    assert b["gui"] is False                      # no folder_picker wired in

    d = body["disk"]
    assert set(d) >= {"path", "freeBytes", "volumeLabel", "afterQueueBytes"}
    assert d["afterQueueBytes"] == d["freeBytes"]   # nothing queued

    s = body["settings"]
    assert set(s) >= {"destination", "connections", "stallSensitivity", "serviceInstalled",
                      "verifyChecksums", "bandwidthCapMBs", "quietHours", "theme", "hfTokenSet"}
    assert s["verifyChecksums"] is True
    assert s["bandwidthCapMBs"] == 0
    assert s["quietHours"] == {"enabled": False, "start": "23:00", "end": "07:30",
                               "limitMBs": 5}
    assert s["theme"] == "mauve"
    assert s["hfTokenSet"] is False
    assert "hfToken" not in s

    assert body["recents"] == []
    assert body["completedToday"] == 0
    assert body["connections"] == []              # idle -> no active download
    assert body["library"] == []


def test_settings_put_round_trips_v2_fields(client):
    r = client.put("/api/settings", json={
        "verifyChecksums": False,
        "bandwidthCapMBs": 40,
        "quietHours": {"enabled": True, "start": "23:00", "end": "7:30"},
        "theme": "graphite",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["verifyChecksums"] is False
    assert body["bandwidthCapMBs"] == 40
    assert body["quietHours"] == {"enabled": True, "start": "23:00", "end": "07:30",
                                  "limitMBs": 5}
    assert body["theme"] == "graphite"

    s = client.get("/api/status").json()["settings"]
    assert s["verifyChecksums"] is False and s["bandwidthCapMBs"] == 40
    assert s["quietHours"]["enabled"] is True and s["theme"] == "graphite"

    # out-of-range values clamp; unknown enum values reset to the default (as v1's
    # stallSensitivity already does). Never a 500.
    r = client.put("/api/settings", json={"bandwidthCapMBs": 999, "theme": "chartreuse"})
    assert r.status_code == 200
    assert r.json()["bandwidthCapMBs"] == 120
    assert r.json()["theme"] == "mauve"


def test_hf_token_is_write_only(client, tmp_path):
    body = client.put("/api/settings", json={"hfToken": "hf_supersecrettoken"}).json()
    assert body["hfTokenSet"] is True
    assert "hfToken" not in body
    assert "hf_supersecrettoken" not in json.dumps(body)

    status = client.get("/api/status")
    assert status.json()["settings"]["hfTokenSet"] is True
    assert "hf_supersecrettoken" not in status.text

    # stored on disk (so it survives a restart) but never echoed
    saved = (tmp_path / "data" / "settings.json").read_text(encoding="utf-8")
    assert "hf_supersecrettoken" in saved

    # "" clears it
    cleared = client.put("/api/settings", json={"hfToken": ""}).json()
    assert cleared["hfTokenSet"] is False


def test_bulk_job_actions(client):
    for path in ("/api/jobs/pause-all", "/api/jobs/resume-all", "/api/jobs/clear-finished"):
        r = client.post(path)
        assert r.status_code == 200, (path, r.text)
        assert r.json() == {"ok": True}


def test_browse_dest_without_shell_is_an_error(client):
    r = client.post("/api/browse-dest")
    assert r.status_code == 400
    assert isinstance(r.json()["error"], str) and r.json()["error"]


def test_browse_dest_with_picker(engine, tmp_path):
    picked = str(tmp_path / "chosen")
    app = server.create_app(engine, folder_picker=lambda: picked)
    with TestClient(app) as c:
        assert c.get("/api/status").json()["backend"]["gui"] is True
        assert c.post("/api/browse-dest").json() == {"path": picked}

    cancelling = server.create_app(engine, folder_picker=lambda: None)
    with TestClient(cancelling) as c:
        assert c.post("/api/browse-dest").json() == {"path": None}


def test_open_folder_and_reverify_unknown_id(client):
    r = client.post("/api/jobs/nope/open-folder")
    assert 400 <= r.status_code < 500
    assert isinstance(r.json()["error"], str) and r.json()["error"]

    r = client.post("/api/jobs/nope/reverify")
    assert 400 <= r.status_code < 500
    assert isinstance(r.json()["error"], str) and r.json()["error"]


def test_jobs_carry_archived_flag(engine, client):
    from bitrebuttal.state import FileEntry, Job

    job = Job(id="job-lib001", name="org/repo", url="org/repo", dest=str(engine.store.dir),
              status="COMPLETE", completed_at=time.time(),
              files=[FileEntry(name="a.bin", url="https://x/a.bin", size=10,
                               state="done", verified=True, hashed=True)])
    with engine.lock:
        engine.jobs[job.id] = job
        engine._order.insert(0, job.id)

    body = client.get("/api/status").json()
    assert body["jobs"][0]["archived"] is False
    assert body["completedToday"] == 1
    entry = body["library"][0]
    assert entry["jobId"] == job.id and entry["integrity"] == "sha256 1/1"
    assert set(entry) == {"jobId", "name", "path", "sizeBytes", "integrity", "finishedLabel"}

    assert client.post("/api/jobs/clear-finished").json() == {"ok": True}
    after = client.get("/api/status").json()
    assert after["jobs"][0]["archived"] is True
    assert after["library"][0]["jobId"] == job.id      # Library keeps archived jobs


def test_repair_requeues_only_corrupt_files(engine, client, tmp_path):
    from bitrebuttal.state import FileEntry, Job

    dest = tmp_path / "repair-dest"
    dest.mkdir()
    good = dest / "good.bin"
    good.write_bytes(b"x" * 10)
    bad = dest / "bad.bin"
    bad.write_bytes(b"y" * 4)
    (dest / "bad.bin.aria2").write_bytes(b"ctrl")     # stale control file goes too

    job = Job(id="job-rep01", name="org/repo", url="org/repo", dest=str(dest),
              status="FAILED", completed_at=time.time(),
              files=[FileEntry(name="good.bin", url="https://x/good.bin", size=10,
                               state="done", verified=True, hashed=True),
                     FileEntry(name="bad.bin", url="https://x/bad.bin", size=9,
                               state="corrupt")])
    with engine.lock:
        engine.jobs[job.id] = job
        engine._order.insert(0, job.id)

    r = client.post("/api/jobs/job-rep01/repair")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}

    j = [x for x in client.get("/api/status").json()["jobs"] if x["id"] == "job-rep01"][0]
    assert j["status"] != "FAILED"                    # back in the queue, loudly alive
    states = {f["name"]: f["state"] for f in j["files"]}
    assert states["bad.bin"] == "queued"
    assert states["good.bin"] == "done"               # verified files untouched
    assert not bad.exists()                           # ONLY the corrupt file was deleted
    assert not (dest / "bad.bin.aria2").exists()
    assert good.exists()

    # a job with nothing corrupt refuses politely
    r2 = client.post("/api/jobs/job-rep01/repair")
    assert 400 <= r2.status_code < 500
    assert isinstance(r2.json()["error"], str) and r2.json()["error"]

    # unknown id -> the usual error shape
    r3 = client.post("/api/jobs/nope/repair")
    assert 400 <= r3.status_code < 500


def test_static_ui_served_at_root(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "BIT" in r.text.upper()
    # api routes must win over the static mount
    assert client.get("/api/status").headers["content-type"].startswith("application/json")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_quiet_hours_limit_round_trip(client):
    r = client.put("/api/settings", json={"quietHours": {
        "enabled": True, "start": "22:00", "end": "06:00", "limitMBs": 12}})
    assert r.status_code == 200
    assert r.json()["quietHours"] == {"enabled": True, "start": "22:00",
                                      "end": "06:00", "limitMBs": 12}
    over = client.put("/api/settings", json={"quietHours": {
        "enabled": False, "start": "23:00", "end": "07:30", "limitMBs": 500}})
    assert over.json()["quietHours"]["limitMBs"] == 50
    missing = client.put("/api/settings", json={"quietHours": {
        "enabled": False, "start": "23:00", "end": "07:30"}})
    assert missing.json()["quietHours"]["limitMBs"] == 5


def test_effective_limit_uses_configured_quiet_speed():
    from bitrebuttal.engine import effective_limit_mbs
    s = {"bandwidthCapMBs": 40,
         "quietHours": {"enabled": True, "start": "00:00", "end": "23:59", "limitMBs": 12}}
    assert effective_limit_mbs(s) == 12


def test_job_connections_endpoint(engine, client):
    from bitrebuttal.state import FileEntry, Job

    job = Job(id="job-conn1", name="o/r", url="o/r", dest=str(engine.store.dir),
              status="PAUSED",
              files=[FileEntry(name="a.bin", url="https://x/a.bin", size=5, state="queued")])
    with engine.lock:
        engine.jobs[job.id] = job
        engine._order.insert(0, job.id)

    def payload():
        return [x for x in client.get("/api/status").json()["jobs"]
                if x["id"] == "job-conn1"][0]

    assert payload()["connections"] == 4              # settings default

    r = client.post("/api/jobs/job-conn1/connections", json={"connections": 8})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert payload()["connections"] == 8

    client.post("/api/jobs/job-conn1/connections", json={"connections": 99})
    assert payload()["connections"] == 16             # clamped high
    client.post("/api/jobs/job-conn1/connections", json={"connections": 0})
    assert payload()["connections"] == 1              # clamped low

    r = client.post("/api/jobs/nope/connections", json={"connections": 4})
    assert 400 <= r.status_code < 500
    assert isinstance(r.json()["error"], str)
