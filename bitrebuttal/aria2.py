"""Minimal aria2 JSON-RPC client + child-process management.

Only the handful of methods the supervisor needs. No aria2p dependency.

Process rules that are load-bearing (field notes 5.3):
  * shutdown must be graceful so aria2 flushes its ``.aria2`` control files;
  * Linux -> SIGTERM, Windows -> RPC ``aria2.shutdown`` (no SIGTERM semantics);
  * wait up to 45 s, SIGKILL/TerminateProcess only as a last resort.
"""

from __future__ import annotations

import itertools
import logging
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import httpx

LOG = logging.getLogger("bitrebuttal.aria2")

GRACEFUL_TIMEOUT = 45.0  # seconds; field notes 5.3 / systemd TimeoutStopSec


class Aria2Error(RuntimeError):
    def __init__(self, message: str, code: Optional[int] = None):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- RPC client


class Aria2Rpc:
    def __init__(self, port: int, secret: str, timeout: float = 20.0):
        self.port = port
        self.secret = secret
        self.url = f"http://127.0.0.1:{port}/jsonrpc"
        self._token = f"token:{secret}"
        self._ids = itertools.count(1)
        self._client = httpx.Client(timeout=timeout, trust_env=False)

    def call(self, method: str, *params: Any, timeout: Optional[float] = None) -> Any:
        body = {"jsonrpc": "2.0", "id": str(next(self._ids)),
                "method": method, "params": [self._token, *params]}
        try:
            kwargs: Dict[str, Any] = {"json": body}
            if timeout is not None:
                kwargs["timeout"] = timeout      # keep display-only calls off the hot path
            resp = self._client.post(self.url, **kwargs)
        except httpx.HTTPError as exc:
            raise Aria2Error(f"RPC transport failure: {exc}") from exc
        if resp.status_code != 200:
            raise Aria2Error(f"RPC HTTP {resp.status_code}")
        data = resp.json()
        if "error" in data:
            err = data["error"]
            raise Aria2Error(str(err.get("message", err)), err.get("code"))
        return data.get("result")

    # -- the five methods we actually need, plus a couple of conveniences
    def add_uri(self, uris: Sequence[str], options: Optional[Dict[str, str]] = None) -> str:
        return self.call("aria2.addUri", list(uris), options or {})

    def tell_status(self, gid: str, keys: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        return self.call("aria2.tellStatus", gid, list(keys)) if keys else \
            self.call("aria2.tellStatus", gid)

    def tell_active(self, keys: Optional[Sequence[str]] = None,
                    timeout: Optional[float] = None) -> List[Dict[str, Any]]:
        if keys:
            return self.call("aria2.tellActive", list(keys), timeout=timeout)
        return self.call("aria2.tellActive", timeout=timeout)

    def tell_stopped(self, offset: int = 0, num: int = 100,
                     keys: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
        if keys:
            return self.call("aria2.tellStopped", offset, num, list(keys))
        return self.call("aria2.tellStopped", offset, num)

    def get_global_stat(self) -> Dict[str, Any]:
        return self.call("aria2.getGlobalStat")

    def get_servers(self, gid: str, timeout: Optional[float] = None) -> List[Dict[str, Any]]:
        """Per-server rates for one download. DISPLAY ONLY - never a health signal (5.1)."""
        return self.call("aria2.getServers", gid, timeout=timeout)

    def change_global_option(self, options: Dict[str, str]) -> Any:
        """Live global option change, e.g. ``max-overall-download-limit``."""
        return self.call("aria2.changeGlobalOption", dict(options))

    def get_version(self) -> Dict[str, Any]:
        return self.call("aria2.getVersion")

    def remove(self, gid: str) -> Any:
        return self.call("aria2.forceRemove", gid)

    def shutdown(self) -> Any:
        return self.call("aria2.shutdown")

    def force_shutdown(self) -> Any:
        return self.call("aria2.forceShutdown")

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass


# ---------------------------------------------------------------- spawning


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def new_secret() -> str:
    return secrets.token_hex(16)


def file_allocation() -> str:
    # falloc needs privileges on NTFS -> prealloc on Windows (ARCHITECTURE 3).
    if sys.platform == "win32":
        return "prealloc"
    if sys.platform == "darwin":
        return "none"  # no posix_fallocate on APFS; sparse writes are fine
    return "falloc"


_ipv6_cache: Optional[bool] = None


def ipv6_available() -> bool:
    """True if the host has a global IPv6 route (no packets are sent; UDP connect only).

    Hosts with an IPv6 address but no route make aria2 pick AAAA records first and
    retry ENETUNREACH forever (--max-tries=0), which looks exactly like a silent
    stall: connections churn, zero bytes move. Observed on the Windows test box.
    """
    global _ipv6_cache
    if _ipv6_cache is None:
        try:
            s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
            try:
                s.connect(("2606:4700:4700::1111", 53))
                _ipv6_cache = True
            finally:
                s.close()
        except OSError:
            _ipv6_cache = False
        if not _ipv6_cache:
            LOG.info("no IPv6 route detected - launching aria2c with --disable-ipv6=true")
    return _ipv6_cache


def build_argv(*, port: int, secret: str, log_path: Path, connections: int = 4,
               aria2_path: str = "aria2c", stop_with_process: Optional[int] = None,
               download_limit: str = "0") -> List[str]:
    """The annotated invocation from field notes 4, with the cross-platform deltas.

    ``--dir``/``--input-file`` are replaced by per-download ``dir``/``out`` RPC options.
    """
    argv = [
        aria2_path,
        "--continue=true", "--auto-file-renaming=false", "--allow-overwrite=false",
        "--conditional-get=false",
        "--max-concurrent-downloads=1",
        f"--max-connection-per-server={connections}", f"--split={connections}",
        "--min-split-size=128M",
        f"--file-allocation={file_allocation()}", "--disk-cache=64M", "--remote-time=true",
        # CRITICAL (field notes 5.1): never let aria2 kill its own connections and
        # never let it give up on a file. The watchdog owns stall policy.
        "--max-tries=0", "--retry-wait=20", "--timeout=60", "--connect-timeout=30",
        "--lowest-speed-limit=0", "--max-file-not-found=10",
        "--auto-save-interval=20",           # bound control-file staleness (5.3)
        "--summary-interval=60", "--console-log-level=notice",
        "--log-level=notice", f"--log={log_path}",
        "--enable-rpc=true", "--rpc-listen-all=false",
        f"--rpc-listen-port={port}", f"--rpc-secret={secret}",
        # Bandwidth cap / quiet hours. "0" = unlimited; changed live via changeGlobalOption.
        f"--max-overall-download-limit={download_limit or '0'}",
        "--no-conf=true",
    ]
    if not ipv6_available():
        argv.append("--disable-ipv6=true")
    if stop_with_process:
        # No orphaned aria2c if the supervisor is SIGKILLed; aria2 still exits gracefully.
        argv.append(f"--stop-with-process={stop_with_process}")
    return argv


class Aria2Process:
    """One aria2c child, with a random localhost RPC port and secret."""

    def __init__(self, argv: List[str], port: int, secret: str):
        self.argv = argv
        self.port = port
        self.secret = secret
        self.proc: Optional[subprocess.Popen] = None
        self.rpc = Aria2Rpc(port, secret)
        self.started_at = 0.0

    # -- lifecycle
    def start(self) -> None:
        kwargs: Dict[str, Any] = {}
        if sys.platform == "win32":
            # CREATE_NO_WINDOW: the windowed shell has no console, so without it
            # Windows pops a visible console for the aria2c child.
            kwargs["creationflags"] = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                                       | getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            kwargs["start_new_session"] = True
        self.proc = subprocess.Popen(
            self.argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, **kwargs)
        self.started_at = time.time()
        LOG.debug("spawned aria2c pid=%s port=%s", self.proc.pid, self.port)

    def wait_ready(self, timeout: float = 20.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.alive():
                return False
            try:
                self.rpc.get_version()
                return True
            except Aria2Error:
                time.sleep(0.25)
        return False

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def returncode(self) -> Optional[int]:
        return self.proc.poll() if self.proc else None

    @property
    def pid(self) -> Optional[int]:
        return self.proc.pid if self.proc else None

    def terminate(self, timeout: float = GRACEFUL_TIMEOUT) -> Optional[int]:
        """Graceful stop so control files flush; hard kill only as a last resort."""
        if self.proc is None:
            return None
        if self.proc.poll() is not None:
            self.rpc.close()
            return self.proc.returncode

        if sys.platform == "win32":
            # No SIGTERM semantics on Windows -> ask aria2 over RPC.
            try:
                self.rpc.shutdown()
            except Aria2Error as exc:
                LOG.warning("RPC shutdown failed (%s); falling back to terminate()", exc)
                try:
                    self.proc.terminate()
                except OSError:
                    pass
        else:
            try:
                self.proc.terminate()  # SIGTERM
            except OSError:
                pass

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                self.rpc.close()
                return self.proc.returncode
            time.sleep(0.25)

        LOG.error("aria2c pid=%s did not exit within %.0fs - killing", self.pid, timeout)
        try:
            self.proc.kill()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=10)
        except Exception:
            pass
        self.rpc.close()
        return self.proc.returncode


def spawn(*, log_path: Path, connections: int = 4, aria2_path: str = "aria2c",
          ready_timeout: float = 20.0, download_limit: str = "0") -> Aria2Process:
    """Spawn aria2c on a random free port with a random secret and wait for RPC."""
    port, secret = free_port(), new_secret()
    argv = build_argv(port=port, secret=secret, log_path=log_path, connections=connections,
                      aria2_path=aria2_path, stop_with_process=os.getpid(),
                      download_limit=download_limit)
    proc = Aria2Process(argv, port, secret)
    proc.start()
    if not proc.wait_ready(ready_timeout):
        rc = proc.returncode
        proc.terminate(timeout=5)
        raise Aria2Error(f"aria2c did not come up on RPC port {port} (exit code {rc})")
    return proc
