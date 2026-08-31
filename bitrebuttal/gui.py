"""Native desktop shell: the local web UI inside a pywebview window.

WebView2 on Windows, WKWebView on macOS. Closing the window stops the app;
unfinished downloads resume on the next launch (the engine's existing
behaviour - ``engine.stop()`` flushes the aria2 control files).
"""

from __future__ import annotations

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


def _create_window(port: int):
    window = webview.create_window(WINDOW_TITLE, f"http://127.0.0.1:{port}",
                                   width=1440, height=920, min_size=(1200, 760))
    if sys.platform == "win32":
        # Dark title bar (DWMWA_USE_IMMERSIVE_DARK_MODE); without it Windows
        # paints the default light-grey chrome over the dark UI.
        def _darken(*_args) -> None:
            try:
                import ctypes
                hwnd = ctypes.windll.user32.FindWindowW(None, WINDOW_TITLE)
                if not hwnd:
                    return
                value = ctypes.c_int(1)
                for attr in (20, 19):        # 19 on pre-20H1 builds
                    if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                            hwnd, attr, ctypes.byref(value),
                            ctypes.sizeof(value)) == 0:
                        break
            except Exception:                # cosmetic only - never fatal
                pass
        window.events.shown += _darken
    return window


def run(port: int = 7451) -> int:
    if webview is None:
        print(FALLBACK_MSG)
        return 1

    import httpx
    import uvicorn

    from .engine import Engine
    from .server import create_app

    # Single-instance guard: if a Bit Rebuttal server already answers on this
    # port, attach a window to it instead of starting a second engine — two
    # engines sharing one state file clobber each other's job records.
    try:
        probe = httpx.get(f"http://127.0.0.1:{port}/api/status",
                          timeout=1.5, trust_env=False)
        already_running = probe.status_code == 200 and "backend" in probe.json()
    except Exception:
        already_running = False
    if already_running:
        _create_window(port)
        webview.start()               # window onto the existing instance
        return 0

    engine = Engine()
    engine.start(port=port)

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

    try:
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
            print(f"error: server did not answer at "
                  f"http://127.0.0.1:{port}/api/status within 15s")
            return 1

        _create_window(port)
        webview.start()               # blocks until the window closes
    finally:
        server_.should_exit = True
        thread.join(timeout=10)
        engine.stop()
    return 0
