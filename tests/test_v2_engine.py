"""Unit tests for the v2 engine/state additions.

Offline and process-free: pure helpers, tmp-dir migration, marker compatibility.
Run: pytest tests/test_v2_engine.py -q
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from bitrebuttal.engine import (after_queue_bytes, effective_limit_mbs,  # noqa: E402
                                hhmm_to_minutes, in_quiet_window, is_same_local_day,
                                limit_option, quiet_hours_active, Engine)
from bitrebuttal.state import (LEGACY_APP_NAME, Job, RECENTS_CAP, Store,  # noqa: E402
                               app_data_dir, migrate_legacy_dir, normalize_settings,
                               push_recent)
from bitrebuttal.verify import (LEGACY_MARKER_NAME, MARKER_NAME,  # noqa: E402
                                find_completion_marker, is_marked_complete,
                                read_completion_marker, write_completion_marker)


# ---------------------------------------------------------------- quiet hours


def test_quiet_window_same_day():
    # 01:00 -> 06:00, half-open [start, end)
    assert not in_quiet_window(hhmm_to_minutes("00:59"), "01:00", "06:00")
    assert in_quiet_window(hhmm_to_minutes("01:00"), "01:00", "06:00")
    assert in_quiet_window(hhmm_to_minutes("03:30"), "01:00", "06:00")
    assert not in_quiet_window(hhmm_to_minutes("06:00"), "01:00", "06:00")
    assert not in_quiet_window(hhmm_to_minutes("23:00"), "01:00", "06:00")


def test_quiet_window_crosses_midnight():
    win = ("23:00", "07:30")
    for inside in ("23:00", "23:59", "00:00", "03:12", "07:29"):
        assert in_quiet_window(hhmm_to_minutes(inside), *win), inside
    for outside in ("07:30", "12:00", "22:59"):
        assert not in_quiet_window(hhmm_to_minutes(outside), *win), outside


def test_quiet_window_degenerate():
    assert not in_quiet_window(hhmm_to_minutes("12:00"), "08:00", "08:00")
    assert hhmm_to_minutes("garbage") == 0
    assert hhmm_to_minutes("7:05") == 7 * 60 + 5


def test_quiet_hours_gate_and_limits():
    lt = time.localtime()
    now_hm = f"{lt.tm_hour:02d}:{lt.tm_min:02d}"
    end_hm = f"{(lt.tm_hour + 2) % 24:02d}:{lt.tm_min:02d}"

    off = normalize_settings({"quietHours": {"enabled": False, "start": now_hm, "end": end_hm},
                              "bandwidthCapMBs": 40})
    assert not quiet_hours_active(off)
    assert effective_limit_mbs(off) == 40

    on = normalize_settings({"quietHours": {"enabled": True, "start": now_hm, "end": end_hm},
                             "bandwidthCapMBs": 40})
    assert quiet_hours_active(on)
    assert effective_limit_mbs(on) == 5           # inside the window -> 5 MB/s

    uncapped = normalize_settings({"bandwidthCapMBs": 0})
    assert effective_limit_mbs(uncapped) == 0

    assert limit_option(0) == "0"
    assert limit_option(5) == "5M"
    assert limit_option(120) == "120M"


def test_bandwidth_cap_normalization():
    assert normalize_settings({"bandwidthCapMBs": 0})["bandwidthCapMBs"] == 0
    assert normalize_settings({"bandwidthCapMBs": -3})["bandwidthCapMBs"] == 0
    assert normalize_settings({"bandwidthCapMBs": 1})["bandwidthCapMBs"] == 10     # clamp up
    assert normalize_settings({"bandwidthCapMBs": 999})["bandwidthCapMBs"] == 120  # clamp down
    assert normalize_settings({"bandwidthCapMBs": "nope"})["bandwidthCapMBs"] == 0
    # HH:MM is normalized; an unparseable half falls back to that field's default
    assert normalize_settings({"quietHours": {"enabled": 1, "start": "9:05", "end": "bad"}}) \
        ["quietHours"] == {"enabled": True, "start": "09:05", "end": "07:30"}
    assert normalize_settings({"theme": "ink"})["theme"] == "ink"
    # unknown enum values reset to the default, exactly like stallSensitivity in v1
    assert normalize_settings({"theme": "neon"})["theme"] == "mauve"
    assert normalize_settings({"hfToken": "  tok  "})["hfToken"] == "tok"


def test_bandwidth_cap_reaches_aria2_both_ways(tmp_path):
    """Live via changeGlobalOption, and on the command line at every launch."""
    from bitrebuttal.aria2 import build_argv

    argv = build_argv(port=1, secret="s", log_path=tmp_path / "a.log", download_limit="40M")
    assert "--max-overall-download-limit=40M" in argv
    argv = build_argv(port=1, secret="s", log_path=tmp_path / "a.log")
    assert "--max-overall-download-limit=0" in argv       # unlimited by default

    class FakeRpc:
        def __init__(self):
            self.calls = []

        def change_global_option(self, options):
            self.calls.append(options)

    class FakeProc:
        def __init__(self):
            self.rpc = FakeRpc()

        def alive(self):
            return True

    eng = Engine(data_dir=tmp_path / "data")
    proc = FakeProc()
    eng._proc = proc

    eng.update_settings({"bandwidthCapMBs": 40})
    assert proc.rpc.calls[-1] == {"max-overall-download-limit": "40M"}
    assert eng._applied_limit == "40M"

    eng._apply_bandwidth()                    # unchanged -> no redundant RPC
    assert len(proc.rpc.calls) == 1

    eng.update_settings({"bandwidthCapMBs": 0})
    assert proc.rpc.calls[-1] == {"max-overall-download-limit": "0"}

    # quiet hours win over the configured cap while the window is open
    lt = time.localtime()
    eng.update_settings({
        "bandwidthCapMBs": 100,
        "quietHours": {"enabled": True,
                       "start": f"{lt.tm_hour:02d}:{lt.tm_min:02d}",
                       "end": f"{(lt.tm_hour + 2) % 24:02d}:{lt.tm_min:02d}"},
    })
    assert proc.rpc.calls[-1] == {"max-overall-download-limit": "5M"}

    eng._proc = None                          # no child -> nothing to push, no crash
    eng._apply_bandwidth(force=True)
    assert eng._applied_limit is None


# ---------------------------------------------------------------- disk math


def test_after_queue_bytes():
    assert after_queue_bytes(200, 0) == 200
    assert after_queue_bytes(200_000_000_000, 10_900_000_000) == 189_100_000_000
    assert after_queue_bytes(100, 500) == 0        # never negative
    assert after_queue_bytes(100, -5) == 100


def test_remaining_bytes_ignores_finished_jobs(tmp_path):
    from bitrebuttal.state import FileEntry

    def job(jid, status, size, done):
        return Job(id=jid, name=jid, url="u", dest=str(tmp_path), status=status,
                   files=[FileEntry(name="f.bin", url="u", size=size, completed=done)])

    jobs = [job("a", "DOWNLOADING", 100, 40),
            job("b", "PAUSED", 50, 0),
            job("c", "COMPLETE", 999, 999),
            job("d", "FAILED", 999, 0)]
    assert Engine._remaining_bytes(jobs) == 60 + 50


def test_completed_today():
    now = time.time()
    assert is_same_local_day(now)
    assert is_same_local_day(now, now)
    assert not is_same_local_day(now - 3 * 86400, now)
    assert not is_same_local_day(None)
    assert not is_same_local_day(0)


# ---------------------------------------------------------------- recents


def test_push_recent_dedups_and_caps():
    r = []
    for name in ("a", "b", "c"):
        r = push_recent(r, name)
    assert r == ["c", "b", "a"]

    r = push_recent(r, "a")                       # moves to the front, no duplicate
    assert r == ["a", "c", "b"]
    assert len(set(r)) == len(r)

    r = push_recent(r, "  a  ")                   # trimmed, still the same entry
    assert r == ["a", "c", "b"]

    r = push_recent(r, "")                        # empty input changes nothing
    assert r == ["a", "c", "b"]

    for i in range(20):
        r = push_recent(r, f"src-{i}")
    assert len(r) == RECENTS_CAP
    assert r[0] == "src-19" and r[1] == "src-18"


def test_recents_persist_in_state_json(tmp_path):
    store = Store(tmp_path / "data")
    assert store.recents == []
    store.recents = push_recent(store.recents, "unsloth/Qwen3.8-Flash-Next-GGUF")
    store.save_jobs([])

    raw = json.loads(store.state_path.read_text(encoding="utf-8"))
    assert raw["recents"] == ["unsloth/Qwen3.8-Flash-Next-GGUF"]
    assert Store(tmp_path / "data").recents == ["unsloth/Qwen3.8-Flash-Next-GGUF"]


def test_engine_records_recents_on_resolve(tmp_path, monkeypatch):
    from bitrebuttal import engine as engine_mod
    from bitrebuttal.resolve import Manifest, ManifestFile

    def fake_resolve(url):
        return Manifest(kind="hf", name=url, repo=url, revision="main",
                        files=[ManifestFile(name="f.gguf", url="https://x/f.gguf", size=10)])

    monkeypatch.setattr(engine_mod, "resolve", fake_resolve)
    eng = Engine(data_dir=tmp_path / "data")
    eng.resolve_payload("org/one")
    eng.resolve_payload("org/two")
    eng.resolve_payload("org/one")
    assert eng.status_payload()["recents"] == ["org/one", "org/two"]


# ---------------------------------------------------------------- migration


def test_migrate_legacy_data_dir(tmp_path):
    old = tmp_path / LEGACY_APP_NAME
    new = tmp_path / "bitrebuttal"
    old.mkdir()
    (old / "state.json").write_text('{"version": 1, "jobs": []}', encoding="utf-8")

    assert migrate_legacy_dir(new, old) is True
    assert new.is_dir() and not old.exists()
    assert json.loads((new / "state.json").read_text(encoding="utf-8"))["version"] == 1

    # idempotent: a second call is a no-op, and never clobbers an existing new dir
    assert migrate_legacy_dir(new, old) is False


def test_migrate_skips_when_new_dir_exists(tmp_path):
    old, new = tmp_path / LEGACY_APP_NAME, tmp_path / "bitrebuttal"
    old.mkdir()
    new.mkdir()
    (old / "state.json").write_text("{}", encoding="utf-8")
    (new / "state.json").write_text('{"keep": true}', encoding="utf-8")

    assert migrate_legacy_dir(new, old) is False
    assert old.is_dir(), "the legacy dir is left alone, never deleted"
    assert json.loads((new / "state.json").read_text(encoding="utf-8")) == {"keep": True}


def test_migrate_no_legacy_dir(tmp_path):
    assert migrate_legacy_dir(tmp_path / "bitrebuttal", tmp_path / LEGACY_APP_NAME) is False
    assert not (tmp_path / "bitrebuttal").exists()


def test_data_dir_override_wins(tmp_path, monkeypatch):
    from bitrebuttal.state import data_dir

    monkeypatch.setenv("BITREBUTTAL_DATA_DIR", str(tmp_path / "explicit"))
    assert data_dir() == tmp_path / "explicit"
    monkeypatch.delenv("BITREBUTTAL_DATA_DIR")
    assert app_data_dir("bitrebuttal").name == "bitrebuttal"
    assert app_data_dir(LEGACY_APP_NAME).parent == app_data_dir("bitrebuttal").parent


# ---------------------------------------------------------------- markers


def test_legacy_completion_marker_is_accepted(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    assert not is_marked_complete(dest)
    assert find_completion_marker(dest) is None
    assert read_completion_marker(dest) is None

    legacy = dest / LEGACY_MARKER_NAME
    legacy.write_text(json.dumps({"tool": "longrebuttal", "jobId": "job-old01",
                                  "files": [{"name": "a.bin", "verified": True}]}),
                      encoding="utf-8")
    assert is_marked_complete(dest)
    assert find_completion_marker(dest) == legacy
    assert read_completion_marker(dest)["jobId"] == "job-old01"


def test_new_marker_wins_and_is_the_only_one_written(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / LEGACY_MARKER_NAME).write_text('{"jobId": "job-old01"}', encoding="utf-8")

    write_completion_marker(dest, "job-new01", "org/repo",
                            [{"name": "a.bin", "bytes": 1, "verified": True}])
    assert (dest / MARKER_NAME).is_file()
    assert find_completion_marker(dest) == dest / MARKER_NAME
    assert read_completion_marker(dest)["jobId"] == "job-new01"


# ---------------------------------------------------------------- verifyChecksums


def test_subtitle_never_claims_sha256_when_checksums_are_off():
    from bitrebuttal.resolve import ManifestFile

    files = [ManifestFile(name="a.gguf", url="u", size=1, sha256="ab" * 32),
             ManifestFile(name="b.gguf", url="u", size=1, sha256="cd" * 32)]
    assert "sha256" in Engine._subtitle(files, True)
    off = Engine._subtitle(files, False)
    assert "sha256" not in off.lower()
    assert "size-only" in off


def test_verify_checksums_off_skips_hashing_but_not_size(tmp_path):
    from bitrebuttal.state import FileEntry

    dest = tmp_path / "dest"
    dest.mkdir()
    payload = b"x" * 4096
    (dest / "a.bin").write_bytes(payload)

    eng = Engine(data_dir=tmp_path / "data")
    eng.update_settings({"verifyChecksums": False})
    job = Job(id="job-nohash", name="org/repo", url="org/repo", dest=str(dest),
              files=[FileEntry(name="a.bin", url="https://x/a.bin", size=len(payload),
                               sha256="ab" * 32, state="verifying")])
    with eng.lock:
        eng.jobs[job.id] = job
        eng._order.insert(0, job.id)
    eng._verify_one(job.id, "a.bin")

    f = job.file("a.bin")
    assert f.state == "done", f.error          # wrong sha256 ignored, size matched
    assert f.verified is True and f.hashed is False
    assert job.status == "COMPLETE"
    assert eng._integrity_label(job) == "size-only"
    assert not any("SHA256" in e["text"] for e in job.log), job.log

    # ...but a size mismatch still fails loudly with hashing off
    (dest / "b.bin").write_bytes(b"y" * 10)
    bad = Job(id="job-badsize", name="org/repo", url="org/repo", dest=str(dest),
              files=[FileEntry(name="b.bin", url="https://x/b.bin", size=99,
                               state="verifying")])
    with eng.lock:
        eng.jobs[bad.id] = bad
        eng._order.insert(0, bad.id)
    eng._verify_one(bad.id, "b.bin")
    assert bad.status == "FAILED"
    assert bad.file("b.bin").state == "corrupt"
    assert eng._integrity_label(bad) == "1 corrupt"


# ---------------------------------------------------------------- bulk actions


def test_clear_finished_only_archives_complete_jobs(tmp_path):
    from bitrebuttal.state import FileEntry

    eng = Engine(data_dir=tmp_path / "data")
    done = Job(id="job-done", name="a", url="u", dest=str(tmp_path), status="COMPLETE",
               completed_at=time.time(),
               files=[FileEntry(name="a", url="u", size=1, state="done", verified=True)])
    failed = Job(id="job-fail", name="b", url="u", dest=str(tmp_path), status="FAILED")
    live = Job(id="job-live", name="c", url="u", dest=str(tmp_path), status="DOWNLOADING")
    with eng.lock:
        for j in (done, failed, live):
            eng.jobs[j.id] = j
            eng._order.insert(0, j.id)

    assert eng.clear_finished() == {"ok": True}
    assert done.archived is True
    assert failed.archived is False and live.archived is False

    assert eng.pause_all() == {"ok": True}
    assert live.status == "PAUSED" and live.paused is True
    assert done.status == "COMPLETE" and failed.status == "FAILED"

    assert eng.resume_all() == {"ok": True}
    assert live.status == "RECOVERING" and live.paused is False


def test_reverify_requires_a_finished_job(tmp_path):
    from bitrebuttal.engine import EngineError
    from bitrebuttal.state import FileEntry

    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "a.bin").write_bytes(b"z" * 8)

    eng = Engine(data_dir=tmp_path / "data")
    job = Job(id="job-rv", name="a", url="u", dest=str(dest), status="DOWNLOADING",
              files=[FileEntry(name="a.bin", url="u", size=8, state="done", verified=True)])
    with eng.lock:
        eng.jobs[job.id] = job
        eng._order.insert(0, job.id)

    with pytest.raises(EngineError):
        eng.reverify_job(job.id)

    job.status = "COMPLETE"
    job.completed_at = time.time()
    assert eng.reverify_job(job.id) == {"ok": True}
    assert job.status == "VERIFYING"
    assert job.file("a.bin").state == "verifying"
    assert eng._verify_q.get_nowait() == (job.id, "a.bin")

    # a missing file is corrupt + FAILED, loudly, with nothing queued
    job.status = "COMPLETE"
    (dest / "a.bin").unlink()
    assert eng.reverify_job(job.id) == {"ok": True}
    assert job.status == "FAILED"
    assert job.file("a.bin").state == "corrupt"
    assert eng._verify_q.empty()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
