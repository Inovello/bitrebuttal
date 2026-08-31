"""v1.0.2: find brew-installed aria2c from a Finder-launched macOS .app.

A .app launched from Finder runs with launchd's minimal PATH
(/usr/bin:/bin:/usr/sbin:/sbin) - Homebrew's bin dir is not on it, so a plain
`which aria2c` misses a perfectly good brew install and the GUI shows
"aria2c not found" even after the user installed it. Discovery must fall back
to the well-known macOS install locations.
"""

import bitrebuttal.engine as engine


def _fake_aria2c(tmp_path):
    import sys
    fake = tmp_path / ("aria2c.exe" if sys.platform == "win32" else "aria2c")
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    return str(fake)


def test_resolve_aria2c_prefers_path_lookup(monkeypatch):
    monkeypatch.setattr(engine.shutil, "which", lambda c: "/somewhere/aria2c")
    assert engine.resolve_aria2c() == "/somewhere/aria2c"


def test_bundled_aria2c_wins_in_frozen_build(monkeypatch, tmp_path):
    """v1.1.0: the aria2c shipped inside the app beats any PATH lookup.

    The bundle is the tested combination and exists so non-technical users
    never have to install anything - it must be picked deterministically.
    """
    fake = _fake_aria2c(tmp_path)
    monkeypatch.setattr(engine.sys, "frozen", True, raising=False)
    monkeypatch.setattr(engine.sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr(engine.shutil, "which", lambda c: "/somewhere/else/aria2c")
    assert engine.bundled_aria2c() == fake
    assert engine.resolve_aria2c() == fake


def test_bundled_aria2c_empty_outside_frozen_build(monkeypatch):
    monkeypatch.delattr(engine.sys, "frozen", raising=False)
    assert engine.bundled_aria2c() == ""


def test_resolve_falls_back_to_path_when_bundle_lacks_aria2c(monkeypatch, tmp_path):
    monkeypatch.setattr(engine.sys, "frozen", True, raising=False)
    monkeypatch.setattr(engine.sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr(engine.shutil, "which", lambda c: "/somewhere/aria2c")
    assert engine.resolve_aria2c() == "/somewhere/aria2c"


def test_resolve_aria2c_falls_back_to_mac_install_dirs(monkeypatch, tmp_path):
    fake = _fake_aria2c(tmp_path)
    monkeypatch.setattr(engine.shutil, "which", lambda c: None)
    monkeypatch.setattr(engine, "MAC_ARIA2_PATHS", (fake,))
    monkeypatch.setattr(engine.sys, "platform", "darwin")
    assert engine.resolve_aria2c() == fake


def test_resolve_aria2c_empty_when_truly_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(engine.shutil, "which", lambda c: None)
    monkeypatch.setattr(engine, "MAC_ARIA2_PATHS", (str(tmp_path / "nope"),))
    monkeypatch.setattr(engine.sys, "platform", "darwin")
    assert engine.resolve_aria2c() == ""


def test_preflight_pins_resolved_path_for_spawn_and_version(monkeypatch, tmp_path):
    """preflight must store the absolute path so aria2 spawn/version use it."""
    eng = engine.Engine(data_dir=tmp_path / "data")
    fake = _fake_aria2c(tmp_path)
    monkeypatch.setattr(engine.shutil, "which", lambda c: None)
    monkeypatch.setattr(engine, "MAC_ARIA2_PATHS", (fake,))
    monkeypatch.setattr(engine.sys, "platform", "darwin")
    exe = eng.preflight()
    assert exe == fake
    assert eng.aria2_path == fake


def test_preflight_error_mentions_homebrew_site(monkeypatch, tmp_path):
    """A Mac user without brew needs the brew.sh pointer, not just the command."""
    eng = engine.Engine(data_dir=tmp_path / "data")
    monkeypatch.setattr(engine.shutil, "which", lambda c: None)
    monkeypatch.setattr(engine, "MAC_ARIA2_PATHS", ())
    monkeypatch.setattr(engine.sys, "platform", "darwin")
    try:
        eng.preflight()
    except engine.EngineError as exc:
        assert "brew install aria2" in str(exc)
        assert "https://brew.sh" in str(exc)
    else:
        raise AssertionError("preflight should have raised EngineError")
