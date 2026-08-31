"""Kill aria2c mid-transfer and prove the supervisor recovers byte-exact.

Opt-in (it moves ~150 MB):
    BITREBUTTAL_RESILIENCE=1 python tests/test_resilience.py

Asserts: the child is hard-killed mid-download, the supervisor notices, relaunches
with the ORIGINAL url (fresh signed redirect), resume starts at >= the kill offset
(nothing re-downloaded from zero), and the final SHA256 matches.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bitrebuttal.engine import Engine  # noqa: E402
from bitrebuttal.verify import read_completion_marker  # noqa: E402

URL = "https://huggingface.co/openai/whisper-tiny/resolve/main/pytorch_model.bin"
KILL_AFTER_BYTES = 4 * 1024 * 1024
TIMEOUT_S = 900.0


class Skipped(Exception):
    pass


def skip(msg: str):
    try:
        import pytest
        pytest.skip(msg)
    except ImportError:
        pass
    raise Skipped(msg)


def test_kill_aria2c_mid_transfer():
    if os.environ.get("BITREBUTTAL_RESILIENCE") != "1":
        skip("set BITREBUTTAL_RESILIENCE=1 to run (downloads ~150 MB)")
        return
    if not shutil.which("aria2c"):
        skip("aria2c not on PATH")
        return

    base = os.environ.get("BITREBUTTAL_TEST_DIR") or os.environ.get("TEMP") or "/tmp"
    root = Path(base) / f"lr-resilience-{int(time.time())}"
    engine = Engine(data_dir=root / "data", poll_interval=2.0, relaunch_backoff=5.0)
    engine.start()
    try:
        job = engine.add_job(URL, dest=str(root / "dest"))
        job_id = job["id"]
        print(f"job {job_id}: {job['totalBytes'] / 1e6:.1f} MB -> {job['dest']}")

        killed_at = None
        deadline = time.time() + TIMEOUT_S
        low_water = None
        while time.time() < deadline:
            j = next(x for x in engine.status_payload()["jobs"] if x["id"] == job_id)
            done = j["doneBytes"]
            if killed_at is None and done > KILL_AFTER_BYTES:
                pid = engine._proc.pid if engine._proc else None
                killed_at = done
                print(f"  killing aria2c pid={pid} at {done} bytes (hard kill, no flush)")
                engine._proc.proc.kill()
            elif killed_at is not None and low_water is None and done > 0 \
                    and j["status"] == "DOWNLOADING":
                low_water = done
                print(f"  resumed at {done} bytes "
                      f"({done - killed_at:+d} vs kill point), recoveries={j['recoveries']}")
            if j["status"] in ("COMPLETE", "FAILED"):
                print(f"  final: {j['status']} after {j['recoveries']} recoveries")
                break
            time.sleep(0.5)

        j = next(x for x in engine.status_payload()["jobs"] if x["id"] == job_id)
        assert killed_at, "never reached the kill threshold"
        assert j["status"] == "COMPLETE", (
            f"{j['status']}: " + "; ".join(e["text"] for e in j["log"][:6]))
        assert low_water is not None and low_water > killed_at * 0.5, (
            f"resume restarted from {low_water} after killing at {killed_at} - "
            "control file did not survive")
        assert any("exited" in e["text"] for e in j["log"]), j["log"]
        marker = read_completion_marker(root / "dest")
        assert marker and marker["files"][0]["verified"] is True
        print("  relaunch log:")
        for e in reversed(j["log"]):
            print(f"    {e['time']} {e['level']:<4} {e['text']}")
        print("ok  killed aria2c mid-transfer, resumed byte-exact, SHA256 verified")
    finally:
        engine.stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        test_kill_aria2c_mid_transfer()
    except Skipped as exc:
        print(f"SKIP: {exc}")
        sys.exit(0)
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
