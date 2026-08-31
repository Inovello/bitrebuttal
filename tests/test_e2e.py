"""End-to-end engine test WITHOUT the server: one real small file from HuggingFace.

    pytest tests/test_e2e.py -v -s
    python tests/test_e2e.py

Downloads ~3.5 MB from hf-internal-testing/tiny-random-gpt2 into a temp dir, waits
for COMPLETE, then checks the `.bitrebuttal-complete` marker and the SHA256 result.
Skips gracefully when aria2c is missing or HF is unreachable.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bitrebuttal.engine import Engine  # noqa: E402
from bitrebuttal.resolve import ResolveError  # noqa: E402
from bitrebuttal.verify import MARKER_NAME, read_completion_marker, sha256_file  # noqa: E402

FILE_URL = ("https://huggingface.co/hf-internal-testing/tiny-random-gpt2/resolve/main/"
            "pytorch_model.bin")
TIMEOUT_S = 240.0

CONTRACT_JOB_KEYS = {"id", "name", "subtitle", "status", "dest", "totalBytes", "doneBytes",
                     "speedBps", "etaSeconds", "recoveries", "bytesLost", "startedLabel",
                     "elapsedLabel", "avgSpeedBps", "files", "log"}


class Skipped(Exception):
    pass


def skip(msg: str):
    try:
        import pytest
        pytest.skip(msg)
    except ImportError:
        pass
    raise Skipped(msg)


def scratch_root() -> Path:
    base = os.environ.get("BITREBUTTAL_TEST_DIR") or tempfile.gettempdir()
    root = Path(base) / f"lr-e2e-{int(time.time())}"
    root.mkdir(parents=True, exist_ok=True)
    return root


def test_engine_end_to_end():
    if not shutil.which("aria2c"):
        skip("aria2c not on PATH")
        return

    root = scratch_root()
    data_dir, dest = root / "data", root / "dest"
    engine = Engine(data_dir=data_dir, poll_interval=2.0, relaunch_backoff=3.0)
    engine.start()
    try:
        try:
            job = engine.add_job(FILE_URL, dest=str(dest))
        except ResolveError as exc:
            skip(f"HF unreachable: {exc}")
            return
        job_id = job["id"]
        print(f"job {job_id}: {job['name']} -> {job['dest']} "
              f"({job['totalBytes']} bytes, {len(job['files'])} file)")
        assert CONTRACT_JOB_KEYS <= set(job), CONTRACT_JOB_KEYS - set(job)
        assert job["totalBytes"] > 0, "size must come from the source, not the client"

        deadline = time.time() + TIMEOUT_S
        seen = set()
        payload = None
        while time.time() < deadline:
            payload = engine.status_payload()
            j = next(x for x in payload["jobs"] if x["id"] == job_id)
            key = (j["status"], j["files"][0]["state"], j["doneBytes"])
            if key not in seen:
                seen.add(key)
                print(f"  {j['status']:<12} file={j['files'][0]['state']:<11} "
                      f"{j['doneBytes']}/{j['totalBytes']} bytes  "
                      f"{j['speedBps']} B/s  recoveries={j['recoveries']}")
            if j["status"] in ("COMPLETE", "FAILED"):
                break
            time.sleep(0.5)

        j = next(x for x in engine.status_payload()["jobs"] if x["id"] == job_id)
        assert j["status"] == "COMPLETE", (
            f"job ended {j['status']}: " + "; ".join(e["text"] for e in j["log"][:5]))
        assert j["doneBytes"] == j["totalBytes"]
        assert j["etaSeconds"] == 0
        assert j["files"][0]["state"] == "done" and j["files"][0]["progress"] == 100

        # -- payload shape (what server.py will hand the UI). v2 only ever ADDS fields,
        # so these are superset checks; the v1 keys must all still be there.
        top = engine.status_payload()
        assert set(top) >= {"backend", "disk", "settings", "jobs"}
        assert set(top["backend"]) >= {"healthy", "label", "version", "uptime"}
        assert set(top["disk"]) >= {"path", "freeBytes", "volumeLabel"}
        assert {"destination", "connections", "stallSensitivity", "serviceInstalled"} \
            <= set(top["settings"])

        # -- bytes on disk
        path = dest / "pytorch_model.bin"
        assert path.exists(), f"{path} missing"
        assert path.stat().st_size == j["totalBytes"]
        assert not Path(str(path) + ".aria2").exists(), "control file should be gone"

        # -- completion marker
        marker = read_completion_marker(dest)
        assert marker, f"{MARKER_NAME} not written in {dest}"
        print(f"  {MARKER_NAME}: {json.dumps(marker, indent=2)}")
        assert marker["jobId"] == job_id and marker["completedAt"].endswith("Z")
        entry = marker["files"][0]
        assert entry["name"] == "pytorch_model.bin"
        assert entry["bytes"] == path.stat().st_size
        assert entry["verified"] is True
        assert entry["sha256"] and len(entry["sha256"]) == 64

        # -- the recorded hash really is this file's hash
        actual = sha256_file(path)
        assert actual == entry["sha256"], f"{actual} != {entry['sha256']}"
        assert any("SHA256 verified" in e["text"] for e in j["log"]), j["log"]
        print(f"  SHA256 {actual} verified against HuggingFace LFS oid")

        # -- state survives a restart: a second engine loads the job as COMPLETE
        engine.stop()
        again = Engine(data_dir=data_dir, poll_interval=2.0)
        again.start()
        try:
            reloaded = next(x for x in again.status_payload()["jobs"] if x["id"] == job_id)
            assert reloaded["status"] == "COMPLETE"
            print("  state.json reload: job still COMPLETE after engine restart")
        finally:
            again.stop()
    finally:
        try:
            engine.stop()
        except Exception:
            pass
    print(f"ok  end-to-end download + verification ({root})")


def test_corrupt_file_fails_job_loudly():
    """A finished file whose hash is wrong must FAIL the job and delete nothing.

    Offline: the file is already on disk, so the engine adopts it straight into
    verification and never launches aria2c.
    """
    if not shutil.which("aria2c"):
        skip("aria2c not on PATH")
        return
    from bitrebuttal.state import FileEntry, Job

    root = scratch_root()
    dest = root / "dest"
    dest.mkdir(parents=True, exist_ok=True)
    payload = b"not the bytes huggingface published" * 1000
    (dest / "model.safetensors").write_bytes(payload)

    engine = Engine(data_dir=root / "data", poll_interval=2.0)
    engine.start()
    try:
        job = Job(id="job-corrupt", name="org/repo", url="org/repo", dest=str(dest),
                  files=[FileEntry(name="model.safetensors", url="https://example.invalid/x",
                                   size=len(payload), sha256="ab" * 32)])
        with engine.lock:
            engine.jobs[job.id] = job
            engine._order.insert(0, job.id)
            engine._adopt_existing(job)

        deadline = time.time() + 30
        while time.time() < deadline:
            j = next(x for x in engine.status_payload()["jobs"] if x["id"] == job.id)
            if j["status"] in ("FAILED", "COMPLETE"):
                break
            time.sleep(0.2)

        j = next(x for x in engine.status_payload()["jobs"] if x["id"] == job.id)
        assert j["status"] == "FAILED", j["status"]
        assert j["files"][0]["state"] == "corrupt", j["files"]
        assert j["bytesLost"] == len(payload)
        errs = [e for e in j["log"] if e["level"] == "err"]
        assert errs and "SHA256 mismatch" in errs[-1]["text"], j["log"]
        assert (dest / "model.safetensors").exists(), "verification must delete nothing"
        assert not (dest / MARKER_NAME).exists(), "no completion marker for a failed job"
        print(f"  job FAILED loudly: {errs[-1]['text']}")
        print("ok  corrupt file fails the job and nothing is deleted")
    finally:
        engine.stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    try:
        test_engine_end_to_end()
        test_corrupt_file_fails_job_loudly()
    except Skipped as exc:
        print(f"SKIP: {exc}")
        sys.exit(0)
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
    sys.exit(0)
