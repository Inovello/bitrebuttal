"""Native desktop shell: the local web UI inside a pywebview window.

WebView2 on Windows, WKWebView on macOS. Closing the window stops the app;
unfinished downloads resume on the next launch (the engine's existing
behaviour - ``engine.stop()`` flushes the aria2 control files).
"""

from __future__ import annotations

import html
import os
import subprocess
import sys
import threading
import time
from typing import Optional

try:
    import webview
except ImportError:      # no pywebview / no webview runtime on this machine
    webview = None

FALLBACK_MSG = ("native shell needs pywebview + a webview runtime; "
                "falling back: run `bitrebuttal serve` for the browser UI")

WINDOW_TITLE = "Bit Rebuttal"
ERROR_TITLE = "Bit Rebuttal - startup error"


def _native_alert(title: str, text: str) -> None:
    """OS-level error dialog, needing nothing but the OS itself.

    This is the surface of last resort: it runs when the webview runtime is
    missing or already broken, so every branch is guarded - it must never
    raise a second error on top of the one it is reporting.
    """
    try:
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)  # MB_ICONERROR
        elif sys.platform == "darwin":
            # title/text ride in as argv rather than being spliced into the
            # script text: an apostrophe or quote in an error message would
            # otherwise turn into an AppleScript syntax error.
            script = ("on run argv\n"
                      "  display alert (item 1 of argv) message (item 2 of argv) "
                      "as critical\n"
                      "end run")
            subprocess.run(["osascript", "-e", script, title, text],
                           timeout=300, check=False)
        else:                             # Linux/unknown: no dialog we can count on
            print(f"{title}: {text}", file=sys.stderr, flush=True)
    except Exception:
        try:
            print(f"{title}: {text}", file=sys.stderr, flush=True)
        except Exception:
            pass


_ERROR_PAGE = """<!doctype html>
<meta charset="utf-8">
<style>
  html, body {{ margin: 0; height: 100%; }}
  body {{
    background: #16181d; color: #e6e8ec; padding: 22px 24px; box-sizing: border-box;
    font: 13px/1.55 -apple-system, "Segoe UI", system-ui, sans-serif;
    user-select: text; -webkit-user-select: text;
  }}
  h1 {{ font-size: 15px; margin: 0 0 12px; color: #ff8a8a; }}
  pre {{
    margin: 0; padding: 12px 14px; border-radius: 6px;
    background: #0e1014; border: 1px solid #2a2e37; color: #e6e8ec;
    font: 12px/1.5 ui-monospace, Menlo, Consolas, monospace;
    white-space: pre-wrap; word-break: break-word;
  }}
</style>
<h1>{title}</h1>
<pre>{text}</pre>
"""


def _show_error(title: str, text: str) -> None:
    """Make a startup failure *visible*, whatever shell we are running in.

    The windowed build has no console, so printing alone loses the message
    entirely (the .app just vanishes). Prefer a real webview window; fall back
    to the OS dialog when pywebview is missing or the window itself fails.

    ``webview.start()`` blocks until the user closes the error window - that is
    deliberate: run() is on its way out anyway, and returning early would let
    the process exit before anyone read the message.
    """
    print(f"{title}: {text}", file=sys.stderr, flush=True)   # -cli / Terminal runs
    if webview is not None:
        try:
            page = _ERROR_PAGE.format(title=html.escape(title),
                                      text=html.escape(text))
            webview.create_window(title, html=page, width=600, height=340)
            webview.start()
            return
        except Exception:
            pass                          # webview runtime unusable - drop to the OS
    _native_alert(title, text)


class _WindowApi:
    """JS bridge for the frameless window's in-UI title strip.

    The page shows its own minimize/maximize/close buttons (``.shellbar`` in
    the static UI) and calls these via ``window.pywebview.api``.

    Everything except the bridge methods lives on underscore attributes:
    pywebview recursively introspects every PUBLIC attribute of this object on
    page load, and a public reference to the Window dragged that walk through
    the whole native object graph - seconds of GIL-bound interop right after
    first paint, freezing the UI thread ("not responding" on Windows).
    """

    def __init__(self) -> None:
        self._window = None
        self._maximized = False

    def minimize(self) -> None:
        if self._window is not None:
            self._window.minimize()

    def toggle_maximize(self) -> None:
        if self._window is None:
            return
        if self._maximized:
            self._window.restore()
        elif hasattr(self._window, "maximize"):
            self._window.maximize()
        else:                                 # very old pywebview fallback
            self._window.toggle_fullscreen()
        self._maximized = not self._maximized

    def close(self) -> None:
        if self._window is not None:
            # Deferred: pywebview's bridge evaluates a JS callback AFTER this
            # method returns. Destroying the window first strands that
            # evaluate_js on a never-signalled wait inside a non-daemon thread,
            # and a window-less process that cannot exit is a Dock/taskbar
            # "hang" (observed as the macOS force-quit report in v1.1.0).
            t = threading.Timer(0.3, self._window.destroy)
            t.daemon = True
            t.start()


def _create_window(port: int):
    # Frameless: the OS title bar never matched the dark UI; the page renders
    # its own slim window strip (drag region + window buttons) instead.
    api = _WindowApi()
    window = webview.create_window(WINDOW_TITLE, f"http://127.0.0.1:{port}",
                                   width=1440, height=920, min_size=(1200, 760),
                                   frameless=True, easy_drag=False, js_api=api,
                                   background_color="#16181d",  # dark pre-paint, no flash
                                   text_select=True)  # error banners must be copyable
    api._window = window
    return window


def run(port: int = 7451) -> int:
    if webview is None:
        # No webview module -> _show_error goes straight to the native alert.
        _show_error(ERROR_TITLE, FALLBACK_MSG)
        return 1

    import httpx
    import uvicorn

    from .engine import Engine, EngineError
    from .server import create_app

    # Startup runs inside one try: on a windowed build a raised exception dies
    # into a console nobody can see, so every failure below routes to
    # _show_error and a nonzero return instead.
    engine = None
    engine_started = False        # engine.start() succeeded -> we owe it a stop()
    server_ = None
    thread = None
    try:
        # Single-instance guard: if a Bit Rebuttal server already OWNS this port,
        # attach a window to it instead of starting a second engine — two engines
        # sharing one state file clobber each other's job records.
        #
        # The test is socket-first, not HTTP-first: a dying instance keeps
        # answering HTTP for a moment after its window closes, and attaching to it
        # leaves a window onto nothing (observed twice on rapid close+relaunch).
        # Port bindable -> no listener -> run our own engine. Port busy -> probe;
        # busy but not answering -> the old instance is draining, wait for it.
        import socket

        def port_is_free() -> bool:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.bind(("127.0.0.1", port))
                return True
            except OSError:
                return False
            finally:
                s.close()

        def probe_ok() -> bool:
            try:
                r = httpx.get(f"http://127.0.0.1:{port}/api/status",
                              timeout=1.5, trust_env=False)
                return r.status_code == 200 and "backend" in r.json()
            except Exception:
                return False

        attach = False
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline:
            if port_is_free():
                break                     # no listener: we own the port
            if probe_ok():
                attach = True             # live instance: attach a window to it
                break
            time.sleep(0.4)               # busy but silent: old instance draining
        if attach:
            _create_window(port)
            webview.start()               # window onto the existing instance
            return 0

        engine = Engine()
        engine.start(port=port)
        engine_started = True

        def folder_picker() -> Optional[str]:
            window = webview.windows[0] if webview.windows else None
            if window is None:            # window not up yet
                return None
            result = window.create_file_dialog(webview.FOLDER_DIALOG)
            if not result:                # cancelled
                return None
            return str(result[0])

        app = create_app(engine, folder_picker=folder_picker)

        server_ = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                                log_level="warning"))
        thread = threading.Thread(target=server_.run, daemon=True)
        thread.start()

        # Wait for the server to answer before opening the window.
        deadline = time.monotonic() + 15.0
        ready = False
        while time.monotonic() < deadline:
            try:
                r = httpx.get(f"http://127.0.0.1:{port}/api/status",
                              timeout=2.0, trust_env=False)
                if r.status_code == 200:
                    ready = True
                    break
            except Exception:
                pass
            time.sleep(0.25)
        if not ready:
            raise RuntimeError(f"server did not answer at "
                               f"http://127.0.0.1:{port}/api/status within 15s")
    except Exception as exc:
        # Unwind whatever came up before the failure - each step guarded so a
        # cleanup error cannot bury the error we are here to report.
        if thread is not None:
            try:
                server_.should_exit = True
                thread.join(timeout=10)
            except Exception:
                pass
        if engine_started:
            try:
                engine.stop()
            except Exception:
                pass
        # EngineError text is already written for the user (e.g. the aria2c
        # install hint) - show it verbatim; anything else is a bug, so name the
        # type too.
        detail = str(exc) if isinstance(exc, EngineError) \
            else f"{type(exc).__name__}: {exc}"
        _show_error(ERROR_TITLE, detail)
        return 1

    try:
        _create_window(port)
        webview.start()               # blocks until the window closes
    except Exception as exc:
        # e.g. the WebView2 runtime is missing: creating/starting the window is
        # the first call that needs it. _show_error retries a webview window and
        # falls back to the native alert when that fails again.
        _show_error(ERROR_TITLE, f"could not open the app window - "
                    f"{type(exc).__name__}: {exc}")
        return 1
    finally:
        server_.should_exit = True
        thread.join(timeout=10)
        engine.stop()
    if getattr(sys, "frozen", False):
        # Cleanup is complete: state saved, aria2 control files flushed. In the
        # frozen app, hard-exit so a thread stranded by window teardown (e.g. a
        # pywebview bridge thread mid evaluate_js) can never keep a window-less
        # process "running" in the Dock/taskbar - that reads as a hang.
        os._exit(0)
    return 0
