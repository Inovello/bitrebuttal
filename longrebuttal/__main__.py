"""Long Rebuttal CLI: serve | add | status | service.

``add`` and ``status`` work headless by talking to a running instance's HTTP API,
found through the ``portfile`` the engine writes into the data dir on start.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any, Dict, List, Optional

from . import DISPLAY_NAME, __version__
from .state import Store

DEFAULT_PORT = 7451
NO_INSTANCE = ("no running instance - start `longrebuttal serve` first")


# ---------------------------------------------------------------- HTTP helpers


def _instance(store: Store) -> Optional[int]:
    """Port of a reachable running instance, or None."""
    info = store.read_portfile()
    if not info:
        return None
    port = int(info["port"])
    try:
        import httpx
        r = httpx.get(f"http://127.0.0.1:{port}/api/status", timeout=3.0, trust_env=False)
        if r.status_code == 200:
            return port
    except Exception:
        return None
    return None


def _post(port: int, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    import httpx
    r = httpx.post(f"http://127.0.0.1:{port}{path}", json=body, timeout=120.0, trust_env=False)
    try:
        data = r.json()
    except ValueError:
        data = {"error": f"HTTP {r.status_code}"}
    if r.status_code >= 400:
        raise SystemExit(f"error: {data.get('error', data)}")
    return data


def _get(port: int, path: str) -> Dict[str, Any]:
    import httpx
    r = httpx.get(f"http://127.0.0.1:{port}{path}", timeout=30.0, trust_env=False)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------- formatting


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1000.0
    return f"{n:.1f}TB"


def _fmt_eta(secs: Optional[int]) -> str:
    if secs is None:
        return "-"
    if secs <= 0:
        return "0s"
    h, rem = divmod(int(secs), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m{s:02d}s" if m else f"{s}s"


def _table(rows: List[List[str]], headers: List[str]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    out = [line, "  ".join("-" * w for w in widths)]
    for row in rows:
        out.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
    return "\n".join(out)


def _render_status(payload: Dict[str, Any], live: bool) -> str:
    b = payload.get("backend", {})
    d = payload.get("disk", {})
    head = (f"{DISPLAY_NAME} {b.get('version', __version__)} - "
            f"{'supervisor online, uptime ' + str(b.get('uptime')) if live else 'offline (state.json)'}"
            f"\ndestination: {d.get('path')}   free: {_fmt_bytes(d.get('freeBytes', 0))}")
    jobs = payload.get("jobs", [])
    if not jobs:
        return head + "\n\nNo jobs."
    rows = []
    for j in jobs:
        total, done = j.get("totalBytes", 0), j.get("doneBytes", 0)
        pct = (done / total * 100) if total else 0.0
        rows.append([
            j.get("id", "?"),
            (j.get("name", "") or "")[:44],
            j.get("status", "?"),
            f"{pct:5.1f}%",
            f"{_fmt_bytes(done)}/{_fmt_bytes(total)}",
            _fmt_bytes(j.get("speedBps", 0)) + "/s",
            _fmt_eta(j.get("etaSeconds")),
            str(j.get("recoveries", 0)),
        ])
    body = _table(rows, ["ID", "NAME", "STATUS", "PCT", "BYTES", "SPEED", "ETA", "RECOV"])
    failed = [j for j in jobs if j.get("status") == "FAILED"]
    tail = ""
    for j in failed:
        errs = [e for e in j.get("log", []) if e.get("level") == "err"][:1]
        if errs:
            tail += f"\n! {j.get('id')}: {errs[0].get('text')}"
    return head + "\n\n" + body + tail


# ---------------------------------------------------------------- commands


def cmd_serve(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    try:
        from . import server  # noqa: F401  (built by the server pass)
    except ImportError:
        print("server module not built yet - `longrebuttal serve` needs longrebuttal/server.py.\n"
              "The engine itself is ready: from longrebuttal.engine import Engine; "
              "Engine().start(port).")
        return 1
    run = getattr(server, "run", None)
    if run is None:
        print("longrebuttal/server.py exists but exposes no run(port=..., headless=...) entry "
              "point.")
        return 1
    return int(run(port=args.port, headless=args.headless) or 0)


def cmd_add(args: argparse.Namespace) -> int:
    store = Store()
    port = _instance(store)
    if port is None:
        print(f"error: {NO_INSTANCE}", file=sys.stderr)
        return 1
    body: Dict[str, Any] = {"url": args.url}
    if args.dest:
        body["dest"] = args.dest
    if args.files:
        body["files"] = [f.strip() for f in args.files.split(",") if f.strip()]
    job = _post(port, "/api/jobs", body)
    print(f"created {job.get('id')}  {job.get('name')}  "
          f"{len(job.get('files', []))} file(s)  {_fmt_bytes(job.get('totalBytes', 0))}")
    print(f"dest: {job.get('dest')}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    store = Store()
    port = _instance(store)
    if port is not None:
        payload = _get(port, "/api/status")
        live = True
    else:
        from .engine import Engine
        eng = Engine()
        jobs = eng.store.load_jobs()
        for j in jobs:
            eng.jobs[j.id] = j
            eng._order.insert(0, j.id)
        payload = eng.status_payload()
        live = False
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    print(_render_status(payload, live))
    if not live:
        print("\n(no running instance - showing the last persisted state)")
    return 0


def cmd_service(args: argparse.Namespace) -> int:
    from . import service
    if args.action == "install":
        res = service.install(port=args.port)
    elif args.action == "uninstall":
        res = service.uninstall()
    else:
        res = service.status()
    for key in ("message", "detail", "error", "command", "hint"):
        if res.get(key):
            prefix = {"error": "error: ", "command": "run this yourself: ", "hint": "hint: "}
            print(prefix.get(key, "") + str(res[key]))
    if args.action == "status":
        print("installed: " + ("yes" if res.get("installed") else "no"))
    return 0 if (res.get("installed") or args.action in ("uninstall", "status")) else 1


# ---------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="longrebuttal",
        description=f"{DISPLAY_NAME} - resilient aria2c downloader for huge model files.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("serve", help="run the local web UI + API")
    s.add_argument("--headless", action="store_true", help="do not open a browser window")
    s.add_argument("--port", type=int, default=DEFAULT_PORT, help="HTTP port (default 7451)")
    s.set_defaults(func=cmd_serve)

    a = sub.add_parser("add", help="queue a download on a running instance")
    a.add_argument("url", help="HF repo id (org/repo[@rev]), HF URL, or any direct URL")
    a.add_argument("--dest", help="destination directory (default: configured destination)")
    a.add_argument("--files", help="comma-separated file names to select (default: all)")
    a.set_defaults(func=cmd_add)

    st = sub.add_parser("status", help="show jobs (live instance if one is running)")
    st.add_argument("--json", action="store_true", help="raw /api/status JSON")
    st.set_defaults(func=cmd_status)

    sv = sub.add_parser("service", help="install/uninstall the reboot-survival service")
    sv.add_argument("action", choices=["install", "uninstall", "status"])
    sv.add_argument("--port", type=int, default=DEFAULT_PORT, help="port the service serves on")
    sv.set_defaults(func=cmd_service)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:                       # fail loudly, never silently
        from .engine import EngineError
        from .resolve import ResolveError
        if isinstance(exc, (EngineError, ResolveError)):
            print(f"error: {exc}", file=sys.stderr)
            return 2
        raise


if __name__ == "__main__":
    sys.exit(main())
