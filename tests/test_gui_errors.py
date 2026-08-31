"""v1.0.1 Bug 1: the windowed shell must NEVER fail silently on startup.

Contract under test (macOS DOA fix):
  * any startup failure (engine preflight, unexpected exception, missing
    webview runtime) is surfaced VISIBLY - an error window when webview is
    available, a native OS alert when it is not - never only stdout/stderr;
  * the surfaced text keeps the actionable message (e.g. the aria2c install
    hint) so the user can fix it themselves;
  * run() returns nonzero instead of raising/killing the process silently.
"""

from __future__ import annotations

import socket
import types

import pytest

import bitrebuttal.gui as gui
from bitrebuttal.engine import EngineError


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeWebview:
    """Stands in for the pywebview module: records windows, start() returns."""

    FOLDER_DIALOG = "folder-dialog"

    def __init__(self):
        self.windows = []
        self.created = []
        self.started = 0

    def create_window(self, title, url=None, html=None, **kwargs):
        w = types.SimpleNamespace(title=title, url=url, html=html, kwargs=kwargs)
        w.destroy = lambda: None
        self.created.append(w)
        self.windows.append(w)
        return w

    def start(self, *args, **kwargs):
        self.started += 1

    def shown_text(self) -> str:
        return " ".join(str(w.html or "") + " " + str(w.url or "")
                        for w in self.created)


class BoomEngine:
    """Engine whose start() fails like a machine without aria2c."""

    exc: Exception = EngineError(
        "aria2c not found on PATH. Install it: brew install aria2")

    def __init__(self, *args, **kwargs):
        pass

    def start(self, port=None):
        raise self.exc

    def stop(self, timeout=60.0):
        pass


@pytest.fixture
def fake_webview(monkeypatch):
    fake = FakeWebview()
    monkeypatch.setattr(gui, "webview", fake)
    return fake


def test_engine_error_shows_visible_dialog(fake_webview, monkeypatch):
    """aria2c missing -> a visible error surface with the install hint, rc != 0."""
    monkeypatch.setattr("bitrebuttal.engine.Engine", BoomEngine)
    alerts = []
    monkeypatch.setattr(gui, "_native_alert",
                        lambda title, text: alerts.append((title, text)),
                        raising=False)

    rc = gui.run(port=free_port())

    assert rc != 0
    surfaced = fake_webview.shown_text() + " ".join(t for _, t in alerts)
    assert fake_webview.started >= 1 or alerts, \
        "startup failure produced no visible error surface"
    assert "aria2c" in surfaced
    assert "brew install aria2" in surfaced


def test_unexpected_error_is_surfaced_not_raised(fake_webview, monkeypatch):
    """Even an unexpected exception must surface visibly instead of propagating."""

    class WeirdEngine(BoomEngine):
        exc = RuntimeError("something exploded deep inside")

    monkeypatch.setattr("bitrebuttal.engine.Engine", WeirdEngine)
    alerts = []
    monkeypatch.setattr(gui, "_native_alert",
                        lambda title, text: alerts.append((title, text)),
                        raising=False)

    rc = gui.run(port=free_port())          # must NOT raise

    assert rc != 0
    surfaced = fake_webview.shown_text() + " ".join(t for _, t in alerts)
    assert fake_webview.started >= 1 or alerts
    assert "something exploded deep inside" in surfaced


def test_missing_webview_uses_native_alert(monkeypatch):
    """No pywebview at all -> the native OS alert path, rc != 0."""
    monkeypatch.setattr(gui, "webview", None)
    alerts = []
    monkeypatch.setattr(gui, "_native_alert",
                        lambda title, text: alerts.append((title, text)),
                        raising=False)

    rc = gui.run(port=free_port())

    assert rc != 0
    assert alerts, "missing webview produced no native alert"
    assert "pywebview" in " ".join(t for _, t in alerts)


def test_native_alert_exists():
    """The fallback itself must exist and be callable per-platform."""
    assert callable(getattr(gui, "_native_alert", None))
