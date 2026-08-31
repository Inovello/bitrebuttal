"""Long Rebuttal smoke tests.

Run either way:
    pytest tests/test_smoke.py -v
    python tests/test_smoke.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from longrebuttal.engine import (STALL_FLOOR_BPS, adaptive_threshold, elapsed_label,  # noqa: E402
                                 uptime_label)
from longrebuttal.resolve import ResolveError, parse_input, resolve  # noqa: E402
from longrebuttal.state import FileEntry, Job, Store, atomic_write_json, read_json  # noqa: E402
from longrebuttal.verify import sha256_file, verify_file  # noqa: E402

TINY_REPO = "hf-internal-testing/tiny-random-gpt2"


class Skipped(Exception):
    pass


def skip(msg: str):
    try:
        import pytest
        pytest.skip(msg)
    except ImportError:
        pass
    raise Skipped(msg)


# ---------------------------------------------------------------- state


def test_state_atomic_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        store = Store(td)
        job = Job(id="job-test01", name="org/repo", url="org/repo",
                  dest=str(Path(td) / "dl"), subtitle="1 file",
                  files=[FileEntry(name="a.bin", url="https://x/a.bin", size=1234,
                                   sha256="ab" * 32, state="done", completed=1234,
                                   verified=True)])
        job.log.append({"time": "10:00", "level": "info", "text": "created"})
        store.save_jobs([job])

        raw = read_json(store.state_path)
        assert raw["version"] == 1 and len(raw["jobs"]) == 1

        back = store.load_jobs()
        assert len(back) == 1
        j = back[0]
        assert j.id == job.id and j.dest == job.dest and j.total_bytes == 1234
        assert j.files[0].sha256 == "ab" * 32 and j.files[0].state == "done"
        assert j.done_bytes == 1234
        assert j.log[0]["text"] == "created"

        # atomic: no temp files left behind, and the file is valid JSON after a rewrite
        store.save_jobs([job, Job(id="job-test02", name="second", url="u", dest=td)])
        assert len(store.load_jobs()) == 2
        leftovers = [p for p in Path(td).iterdir() if p.name.endswith(".tmp")]
        assert not leftovers, leftovers

        # settings round-trip + clamping
        s = store.save_settings({"connections": 99, "stallSensitivity": "bogus",
                                 "destination": td})
        assert s["connections"] == 16 and s["stallSensitivity"] == "Normal"
        assert store.load_settings()["destination"] == td

        # portfile
        store.write_portfile(7451)
        assert store.read_portfile()["port"] == 7451
        store.clear_portfile()
        assert store.read_portfile() is None

        # atomic_write_json overwrite keeps the file parseable
        p = Path(td) / "x.json"
        atomic_write_json(p, {"a": 1})
        atomic_write_json(p, {"a": 2})
        assert json.loads(p.read_text())["a"] == 2
    print("ok  state.json atomic round-trip")


# ---------------------------------------------------------------- threshold math


def test_adaptive_threshold():
    # no history -> the absolute floor (10 KB/s)
    assert adaptive_threshold([]) == float(STALL_FLOOR_BPS)

    # slow link: 5% of a 1 MB/s median is 50 KB/s > floor
    slow = [1_000_000] * 30
    assert adaptive_threshold(slow) == 50_000.0

    # very slow link: 5% of 100 KB/s = 5 KB/s -> floor wins
    assert adaptive_threshold([100_000] * 10) == float(STALL_FLOOR_BPS)

    # gigabit: floor would be far too low; adaptive raises it (field notes 7.3)
    assert adaptive_threshold([100_000_000] * 5) == 5_000_000.0

    # accepts (timestamp, speed) samples too, and uses the MEDIAN not the mean
    samples = [(i, 1_000_000) for i in range(29)] + [(29, 100_000_000)]
    assert adaptive_threshold(samples) == 50_000.0

    # sensitivity scaling
    assert adaptive_threshold(slow, "High") == 100_000.0
    assert adaptive_threshold(slow, "Low") == 25_000.0

    assert elapsed_label(3 * 86400 + 3 * 3600 + 37 * 60) == "3d 03h 37m"
    assert elapsed_label(2 * 3600 + 5 * 60) == "02h 05m"
    assert uptime_label(6 * 86400 + 4 * 3600) == "6d 04h"
    print("ok  adaptive threshold math")


# ---------------------------------------------------------------- resolve


def test_parse_input():
    kind, det = parse_input("org/repo")
    assert kind == "hf_repo" and det["repo"] == "org/repo" and det["revision"] == "main"
    kind, det = parse_input("org/repo@refs/pr/1")
    assert det["revision"] == "refs/pr/1"
    kind, det = parse_input("https://huggingface.co/org/repo/tree/v2")
    assert kind == "hf_repo" and det["revision"] == "v2"
    kind, det = parse_input("https://huggingface.co/org/repo/resolve/main/sub/f.gguf")
    assert kind == "hf_file" and det["path"] == "sub/f.gguf"
    kind, det = parse_input("https://example.com/big.bin")
    assert kind == "direct"
    for bad in ("", "not a url", "ftp://x/y"):
        try:
            parse_input(bad)
            raise AssertionError(f"expected ResolveError for {bad!r}")
        except ResolveError:
            pass
    print("ok  input parsing")


def test_resolve_tiny_hf_repo():
    try:
        m = resolve(TINY_REPO)
    except ResolveError as exc:
        skip(f"offline or HF unreachable: {exc}")
        return
    assert m.kind == "hf" and m.repo == TINY_REPO and m.revision == "main"
    names = {f.name for f in m.files}
    assert "config.json" in names, names
    assert any(n.endswith(".bin") or n.endswith(".safetensors") for n in names), names
    for f in m.files:
        assert f.url.startswith(f"https://huggingface.co/{TINY_REPO}/resolve/main/")
        assert f.size >= 0
        if f.sha256:
            assert len(f.sha256) == 64
    payload = m.payload()
    assert payload["repo"] == TINY_REPO and payload["revision"] == "main"
    assert all({"name", "bytes", "sha256", "selected"} <= set(fp) for fp in payload["files"])
    assert all(fp["selected"] is True for fp in payload["files"])

    # single-file resolve of the same repo via a /resolve/ URL, with LFS sha256
    url = f"https://huggingface.co/{TINY_REPO}/resolve/main/pytorch_model.bin"
    one = resolve(url)
    assert len(one.files) == 1 and one.files[0].name == "pytorch_model.bin"
    assert one.files[0].size > 0
    print(f"ok  resolved {TINY_REPO}: {len(m.files)} files, "
          f"{sum(1 for f in m.files if f.sha256)} with sha256; "
          f"pytorch_model.bin = {one.files[0].size} bytes, sha256="
          f"{(one.files[0].sha256 or 'none')[:12]}")


# ---------------------------------------------------------------- verify


def test_verify_catches_corruption():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "payload.bin"
        data = bytes(range(256)) * 4096          # 1 MiB
        p.write_bytes(data)
        good = sha256_file(p)

        r = verify_file(p, len(data), good)
        assert r.ok and r.hashed and r.sha256 == good and r.error is None

        # size-only path (no published hash)
        r = verify_file(p, len(data), None)
        assert r.ok and not r.hashed

        # flip one byte -> same size, different hash (the quantized-tensor case)
        blob = bytearray(p.read_bytes())
        blob[123456] ^= 0x01
        p.write_bytes(bytes(blob))
        r = verify_file(p, len(data), good)
        assert not r.ok and "SHA256 mismatch" in r.error
        assert p.exists(), "verify must delete nothing"

        # truncation -> size mismatch, hashing never even runs
        p.write_bytes(bytes(blob[:-10]))
        r = verify_file(p, len(data), good)
        assert not r.ok and "size mismatch" in r.error and not r.hashed

        # a still-downloading file (control file present) is never verified
        p.write_bytes(data)
        ctrl = Path(str(p) + ".aria2")
        ctrl.write_bytes(b"x")
        r = verify_file(p, len(data), good)
        assert not r.ok and "control file" in r.error
        ctrl.unlink()

        r = verify_file(Path(td) / "missing.bin", 10, None)
        assert not r.ok and "missing" in r.error
    print("ok  verify catches corruption, truncation, and in-flight files")


# ---------------------------------------------------------------- manual runner


def _main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = skipped = 0
    for fn in tests:
        t0 = time.time()
        try:
            fn()
        except Skipped as exc:
            skipped += 1
            print(f"SKIP {fn.__name__}: {exc}")
        except Exception as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {type(exc).__name__}: {exc}")
            import traceback
            traceback.print_exc()
        else:
            print(f"     {fn.__name__} ({time.time() - t0:.2f}s)")
    print(f"\n{len(tests) - failed - skipped} passed, {skipped} skipped, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
