"""The supervisor: one aria2c child, a watchdog, a verifier, and the /api/status payload.

Control flow (ARCHITECTURE 3, ported from the bash reference in field notes 9):

    loop forever:
        if all files complete+verified -> mark job done, idle
        launch aria2c with ORIGINAL urls (never cached redirects)   <- restart-to-re-resolve
        watchdog polls RPC every 60s:
            numActive==0 && numWaiting==0        -> kill aria2c (queue drained)
            aggregate speed < adaptive threshold -> stalls++; at 12 -> kill aria2c, log recovery
            else stalls = 0
        on aria2c exit -> verify what finished -> relaunch if work remains (15s backoff)

Non-negotiables honoured here:
  * stall detection reads ONLY the aggregate ``downloadSpeed`` from getGlobalStat;
  * relaunch always re-submits the ORIGINAL url so signed CDNs re-sign;
  * graceful shutdown (never aria2's RPC pause) so control files flush;
  * a file that cannot complete fails the job loudly - never skip-and-continue.
"""

from __future__ import annotations

import logging
import os
import queue
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from . import __version__
from .aria2 import Aria2Error, Aria2Process, spawn
from .resolve import Manifest, resolve, set_hf_token
from .state import (COMPLETE_MARKER, QUIET_HOURS_LIMIT_MBS, FileEntry, Job, Store, new_job_id,
                    normalize_settings, push_recent)
from .verify import (control_file, is_marked_complete, verify_file, write_completion_marker)

LOG = logging.getLogger("bitrebuttal.engine")

STALL_FLOOR_BPS = 10 * 1024          # absolute floor: 10 KB/s
STALL_FRACTION = 0.05                # 5% of trailing 30-min median aggregate speed
STALL_WINDOW_S = 30 * 60
STALL_POLLS = 12                     # consecutive stalled polls before kill+relaunch
RELAUNCH_BACKOFF_S = 15.0
MAX_FILE_ATTEMPTS = 8                # aria2 errors on one file before the job FAILS
DISK_HEADROOM = 1.05                 # total size + 5% (ARCHITECTURE 4)
SENSITIVITY_MULT = {"Low": 0.5, "Normal": 1.0, "High": 2.0}
CONNECTIONS_REFRESH_S = 2.0          # getServers cadence (display only)
CONNECTIONS_RPC_TIMEOUT = 4.0        # never let a cosmetic call stall the supervisor

STATUS_KEYS = ["gid", "status", "completedLength", "totalLength", "errorCode", "errorMessage"]


class EngineError(RuntimeError):
    """User-facing engine failure (bad request, no disk space, aria2c missing)."""


class DiskSpaceError(EngineError):
    """Preflight failed: not enough room for total size + 5% (server -> HTTP 409)."""


class _HashAborted(Exception):
    """Raised inside the SHA256 stream when the engine is shutting down."""


# ---------------------------------------------------------------- pure helpers


def adaptive_threshold(samples, sensitivity: str = "Normal",
                       floor: int = STALL_FLOOR_BPS) -> float:
    """max(10 KB/s, 5% of trailing 30-min median aggregate speed), scaled by sensitivity.

    ``samples`` is an iterable of speeds (B/s) or of (timestamp, speed) pairs.
    """
    speeds: List[float] = []
    for s in samples:
        if isinstance(s, (tuple, list)):
            speeds.append(float(s[1]))
        else:
            speeds.append(float(s))
    median = statistics.median(speeds) if speeds else 0.0
    base = max(float(floor), STALL_FRACTION * median)
    return base * SENSITIVITY_MULT.get(sensitivity, 1.0)


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000.0
    return f"{n:.1f} TB"


def elapsed_label(seconds: float) -> str:
    seconds = max(0, int(seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    return f"{d}d {h:02d}h {m:02d}m" if d else f"{h:02d}h {m:02d}m"


def uptime_label(seconds: float) -> str:
    seconds = max(0, int(seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    return f"{d}d {h:02d}h" if d else f"{h:02d}h {m:02d}m"


def started_label(ts: float) -> str:
    return time.strftime("%b %d, %Y · %H:%M", time.localtime(ts))


def volume_label(path: os.PathLike | str) -> str:
    p = Path(path)
    if sys.platform == "win32":
        drive = os.path.splitdrive(str(p.absolute()))[0]
        return drive or str(p)
    try:
        cur = p.absolute()
        while not cur.is_mount() and cur.parent != cur:
            cur = cur.parent
        return str(cur)
    except OSError:
        return str(p)


def existing_ancestor(path: os.PathLike | str) -> Path:
    p = Path(path).absolute()
    while not p.exists() and p.parent != p:
        p = p.parent
    return p


def disk_free(path: os.PathLike | str) -> int:
    try:
        return shutil.disk_usage(str(existing_ancestor(path))).free
    except OSError:
        return 0


def finished_label(ts: float) -> str:
    return time.strftime("%b %d · %H:%M", time.localtime(ts))


def is_same_local_day(ts: Optional[float], now: Optional[float] = None) -> bool:
    if not ts:
        return False
    ref = time.time() if now is None else now
    return time.localtime(ts)[:3] == time.localtime(ref)[:3]


def after_queue_bytes(free_bytes: int, remaining_bytes: int) -> int:
    """Free space once everything still queued has landed. Never negative."""
    return max(0, int(free_bytes) - max(0, int(remaining_bytes)))


# -- quiet hours / bandwidth ------------------------------------------------


def hhmm_to_minutes(value: str) -> int:
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(value or ""))
    if not m:
        return 0
    return (int(m.group(1)) % 24) * 60 + min(59, int(m.group(2)))


def in_quiet_window(minute_of_day: int, start: str, end: str) -> bool:
    """True inside [start, end). Handles windows that cross midnight (23:00 -> 07:30)."""
    s, e = hhmm_to_minutes(start), hhmm_to_minutes(end)
    if s == e:
        return False                              # zero-length window: never quiet
    m = int(minute_of_day) % (24 * 60)
    if s < e:
        return s <= m < e
    return m >= s or m < e                        # crosses midnight


def quiet_hours_active(settings: Dict[str, Any], now: Optional[float] = None) -> bool:
    q = settings.get("quietHours") or {}
    if not q.get("enabled"):
        return False
    lt = time.localtime(time.time() if now is None else now)
    return in_quiet_window(lt.tm_hour * 60 + lt.tm_min, q.get("start", "23:00"),
                           q.get("end", "07:30"))


def effective_limit_mbs(settings: Dict[str, Any], now: Optional[float] = None) -> int:
    """MB/s cap to apply right now: the quiet-hours limitMBs inside the window, else the configured cap (0 = off)."""
    if quiet_hours_active(settings, now):
        q = settings.get("quietHours") or {}
        try:
            return max(1, min(50, int(q.get("limitMBs", QUIET_HOURS_LIMIT_MBS))))
        except (TypeError, ValueError):
            return QUIET_HOURS_LIMIT_MBS
    try:
        return max(0, int(settings.get("bandwidthCapMBs", 0) or 0))
    except (TypeError, ValueError):
        return 0


def limit_option(mbs: int) -> str:
    """aria2 ``max-overall-download-limit`` value: "0" = unlimited, else "<n>M"."""
    return "0" if not mbs else f"{int(mbs)}M"


MAC_ARIA2_PATHS = ("/opt/homebrew/bin/aria2c",    # Homebrew, Apple Silicon
                   "/usr/local/bin/aria2c",       # Homebrew, Intel
                   "/opt/local/bin/aria2c")       # MacPorts


def resolve_aria2c(candidate: str = "aria2c") -> str:
    """Absolute path of the aria2c binary, or "" when none can be found.

    PATH lookup first. On macOS a Finder-launched .app runs with launchd's
    minimal PATH (/usr/bin:/bin:/usr/sbin:/sbin), which does not include
    Homebrew's bin dir - a brew-installed aria2 is invisible to `which` there,
    so the well-known install locations are checked directly before giving up.
    """
    exe = shutil.which(candidate)
    if exe:
        return exe
    if sys.platform == "darwin" and candidate == "aria2c":
        for p in MAC_ARIA2_PATHS:
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
    return ""


def aria2c_version(aria2_path: str = "aria2c") -> str:
    """`aria2c --version` -> "1.37.0". Empty string when aria2c is not installed."""
    exe = resolve_aria2c(aria2_path)
    if not exe:
        return ""
    kwargs: Dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10,
                             **kwargs).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    m = re.search(r"aria2\s+version\s+([0-9][\w.\-]*)", out or "", re.I)
    return m.group(1) if m else ""


def host_of(uri: str) -> str:
    try:
        netloc = urlparse(uri).netloc
    except ValueError:
        return uri
    return netloc.split("@")[-1].split(":")[0] or uri


# ---------------------------------------------------------------- engine


class Engine:
    def __init__(self, data_dir=None, poll_interval: float = 60.0,
                 relaunch_backoff: float = RELAUNCH_BACKOFF_S,
                 aria2_path: str = "aria2c", stall_polls: int = STALL_POLLS):
        self.store = Store(data_dir)
        self.settings = self.store.load_settings()
        set_hf_token(self.settings.get("hfToken"))
        self.poll_interval = float(poll_interval)
        self.relaunch_backoff = float(relaunch_backoff)
        self.stall_polls = int(stall_polls)
        self.aria2_path = aria2_path

        self.lock = threading.RLock()
        self.jobs: Dict[str, Job] = {}
        self._order: List[str] = []          # newest first

        self._proc: Optional[Aria2Process] = None
        self._gids: Dict[str, Tuple[str, str]] = {}   # gid -> (job_id, file name)
        self._keys: Dict[Tuple[str, str], str] = {}   # (job_id, name) -> gid
        self._speed_samples: Deque[Tuple[float, float]] = deque()
        self._stalls = 0
        self._next_launch_at = 0.0
        self._requeue = False
        self._launches = 0

        self._verify_q: "queue.Queue[Tuple[str, str]]" = queue.Queue()
        self._stop_evt = threading.Event()
        self._threads: List[threading.Thread] = []
        self._started_at = time.time()
        self._port: Optional[int] = None
        self._dirty = False
        self._service_cache: Tuple[float, bool] = (0.0, False)

        # v2
        self.folder_picker: Optional[Callable[[], Optional[str]]] = None  # set by create_app
        self._applied_limit: Optional[str] = None      # last max-overall-download-limit pushed
        self._connections: List[Dict[str, Any]] = []   # aria2 getServers, display only (5.1)
        self._display_speed: Tuple[float, float] = (0.0, 0.0)  # (ts, bps) for the UI only
        self._aria2c_version: Optional[str] = None

    # ------------------------------------------------------------ lifecycle
    def preflight(self) -> str:
        exe = resolve_aria2c(self.aria2_path)
        if not exe:
            hint = ("winget install aria2.aria2" if sys.platform == "win32"
                    else "brew install aria2 (get Homebrew first: https://brew.sh)"
                    if sys.platform == "darwin"
                    else "sudo apt install aria2")
            raise EngineError(f"aria2c not found on PATH. Install it: {hint}")
        self.aria2_path = exe      # pin the absolute path: spawn + version reuse it
        return exe

    def start(self, port: Optional[int] = None) -> None:
        self.preflight()
        with self.lock:
            for job in self.store.load_jobs():
                self.jobs[job.id] = job
                self._order.insert(0, job.id)
                for f in job.files:
                    f.verify_read = 0
                    if f.state in ("downloading", "verifying"):
                        f.state = "queued"     # re-adopted below if it is actually finished
                if job.status not in ("COMPLETE", "FAILED") and not job.paused:
                    job.status = "RECOVERING"
                    self.event(job, "info", "Supervisor started - resuming from control files")
                    self._adopt_existing(job)
            self._order = [jid for jid in dict.fromkeys(self._order) if jid in self.jobs]
        if port:
            self._port = int(port)
            self.store.write_portfile(self._port)
        self._stop_evt.clear()
        self._spawn_thread(self._supervise, "lr-supervisor")
        self._spawn_thread(self._verify_worker, "lr-verifier")
        LOG.info("engine started (data dir %s)", self.store.dir)

    def _spawn_thread(self, target, name: str) -> None:
        t = threading.Thread(target=target, name=name, daemon=True)
        t.start()
        self._threads.append(t)

    def stop(self, timeout: float = 60.0) -> None:
        self._stop_evt.set()
        for t in self._threads:
            t.join(timeout=timeout)
        self._threads.clear()
        self._shutdown_aria("engine stopping")
        self.save()
        self.store.clear_portfile()
        LOG.info("engine stopped")

    # ------------------------------------------------------------ persistence
    def save(self) -> None:
        with self.lock:
            jobs = [self.jobs[j] for j in self._order if j in self.jobs]
        try:
            self.store.save_jobs(jobs)
        except OSError as exc:
            LOG.error("failed to persist state.json: %s", exc)

    def event(self, job: Job, level: str, text: str) -> None:
        """Append to the job log (newest first, capped at 100) - call under lock."""
        job.log.insert(0, {"time": time.strftime("%H:%M", time.localtime()),
                           "level": level, "text": text})
        del job.log[100:]
        LOG.info("[%s] %s: %s", job.id, level, text)
        self._dirty = True

    # ------------------------------------------------------------ public API
    def resolve_payload(self, url: str) -> Dict[str, Any]:
        payload = resolve(url).payload()
        self._push_recent(url)
        return payload

    def _push_recent(self, url: str) -> None:
        """Remember a successfully-resolved source input (newest first, capped, distinct)."""
        with self.lock:
            updated = push_recent(self.store.recents, url)
            if updated == self.store.recents:
                return
            self.store.recents = updated
        self.save()

    def add_job(self, url: str, files: Optional[List[str]] = None,
                dest: Optional[str] = None,
                connections: Optional[int] = None) -> Dict[str, Any]:
        manifest: Manifest = resolve(url)          # never trust client-provided sizes
        self._push_recent(url)
        selected = list(manifest.files)
        if files:
            wanted = set(files)
            selected = [f for f in manifest.files if f.name in wanted]
            missing = wanted - {f.name for f in selected}
            if missing:
                raise EngineError("Not in the resolved file list: " + ", ".join(sorted(missing)))
        if not selected:
            raise EngineError("No files selected.")

        base = Path(dest) if dest else Path(self.settings["destination"])
        if not dest and manifest.repo:
            base = base / manifest.repo.replace("/", "_")
        base = base.absolute()

        total = sum(f.size for f in selected)
        if total:
            free = disk_free(base)
            need = int(total * DISK_HEADROOM)
            if free and free < need:
                raise DiskSpaceError(
                    f"Not enough free space on {volume_label(base)}: need "
                    f"{human_bytes(need)} (total + 5% headroom), {human_bytes(free)} free.")
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise EngineError(f"Cannot create destination {base}: {exc}") from exc

        conn = 0                                   # 0 = "use the settings default"
        if connections is not None:
            try:
                conn = max(1, min(16, int(connections)))
            except (TypeError, ValueError):
                conn = 0
        job = Job(
            id=new_job_id(),
            name=manifest.name,
            url=url,
            dest=str(base),
            files=[FileEntry(name=f.name, url=f.url, size=f.size, sha256=f.sha256)
                   for f in selected],
            subtitle=self._subtitle(selected, self._verify_checksums()),
            repo=manifest.repo,
            revision=manifest.revision,
            status="DOWNLOADING",
            connections=conn,
        )
        with self.lock:
            self.jobs[job.id] = job
            self._order.insert(0, job.id)
            self.event(job, "info",
                       f"Job created - {len(selected)} files queued, {human_bytes(total)} total")
            for w in manifest.warnings:
                self.event(job, "warn", w)
            self._adopt_existing(job)
            if self._proc is not None and self._proc.alive():
                self._enqueue_files(job)       # join the running aria2 queue, no restart
        self.save()
        return self.job_payload(job)

    def _adopt_existing(self, job: Job) -> None:
        """Files already on disk (settled, right size) skip straight to verification.

        Covers re-adding a job over a previous download and aria2's refusal to
        overwrite an existing complete file (--allow-overwrite=false).

        A completion marker left by a previous run - under either the current or the
        pre-rename ``.longrebuttal-complete`` name - is noted but never trusted in
        place of verification: bytes get re-checked, always.
        """
        if is_marked_complete(job.dest):
            self.event(job, "info",
                       f"Previous completion record found in {job.dest} - re-verifying anyway")
        for f in job.files:
            if f.state in ("done", "corrupt"):
                continue                      # never re-hash what is already settled
            path = Path(job.dest) / f.name
            if not path.exists() or control_file(path).exists():
                continue
            size = path.stat().st_size
            if f.size and size != f.size:
                continue                      # partial leftover: let aria2 continue it
            f.completed = size
            f.size = f.size or size
            f.state = "verifying"
            self.event(job, "info", f"{f.name} already present ({human_bytes(size)}) - verifying")
            self._verify_q.put((job.id, f.name))

    def _verify_checksums(self) -> bool:
        return bool(self.settings.get("verifyChecksums", True))

    @staticmethod
    def _subtitle(files, verify_checksums: bool = True) -> str:
        n = len(files)
        hashed = sum(1 for f in files if f.sha256) if verify_checksums else 0
        kinds = {Path(f.name).suffix.lstrip(".").lower() for f in files if Path(f.name).suffix}
        if not verify_checksums:
            tail = "size-only verification (checksums off)"
        elif n and hashed == n:
            tail = "sha256 available"
        elif hashed:
            tail = f"sha256 for {hashed}/{n}"
        else:
            tail = "size-only verification"
        parts = [f"{n} file{'s' if n != 1 else ''}"]
        if len(kinds) == 1:
            parts.append(next(iter(kinds)))
        parts.append(tail)
        return " · ".join(parts)

    def pause_job(self, job_id: str) -> Dict[str, Any]:
        with self.lock:
            job = self._job(job_id)
            if job.status == "COMPLETE":
                raise EngineError("Job already complete.")
            job.paused = True
            job.status = "PAUSED"
            self.event(job, "warn", "Paused - stopping aria2c cleanly (control files flushed)")
            self._requeue = True
        self.save()
        return {"ok": True}

    def resume_job(self, job_id: str) -> Dict[str, Any]:
        with self.lock:
            job = self._job(job_id)
            if job.status == "COMPLETE":
                raise EngineError("Job already complete.")
            corrupt = [f.name for f in job.files if f.state == "corrupt"]
            if corrupt:
                raise EngineError(
                    "Cannot resume: " + ", ".join(corrupt) + " failed verification. Nothing was "
                    "deleted - remove the file(s) manually, then create a new job.")
            job.paused = False
            job.error = None
            job.completed_at = None
            job.status = "RECOVERING"
            for f in job.files:
                if f.state not in ("done", "verifying"):
                    f.state = "queued"
                    f.attempts = 0
                    f.error = None
            self.event(job, "info", "Resumed by user")
            self._requeue = True
            self._next_launch_at = 0.0
        self.save()
        return {"ok": True}

    def delete_job(self, job_id: str, delete_files: bool = False) -> Dict[str, Any]:
        with self.lock:
            job = self._job(job_id)
            self.jobs.pop(job_id, None)
            self._order = [j for j in self._order if j != job_id]
            self._requeue = True
        if delete_files:
            for f in job.files:
                target = Path(job.dest) / f.name
                for p in (target, control_file(target)):
                    try:
                        os.unlink(p)
                    except OSError:
                        pass
        self.save()
        return {"ok": True}

    def set_connections(self, job_id: str, value: Any) -> Dict[str, Any]:
        """Per-job aria2 split/max-connection-per-server. Applies from the next aria2c launch."""
        with self.lock:
            job = self._job(job_id)
            try:
                n = int(value)
            except (TypeError, ValueError):
                raise EngineError("connections must be an integer.")
            n = max(1, min(16, n))
            job.connections = n
            self.event(job, "info",
                       f"Connections set to {n} - applies from the next aria2c launch")
        self.save()
        return {"ok": True}

    def update_settings(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(self.settings)
        merged.update({k: v for k, v in (raw or {}).items() if v is not None})
        self.settings = self.store.save_settings(normalize_settings(merged))
        set_hf_token(self.settings.get("hfToken"))
        self._apply_bandwidth(force=True)      # bandwidthCapMBs takes effect immediately
        return self.settings_payload()

    # -- v2 job actions -------------------------------------------
    def pause_all(self) -> Dict[str, Any]:
        """Clean stop for every job that can be paused. Never aria2's RPC pause."""
        with self.lock:
            targets = [j for j in self.jobs.values()
                       if not j.paused and j.status not in ("COMPLETE", "FAILED")]
            for job in targets:
                job.paused = True
                job.status = "PAUSED"
                self.event(job, "warn", "Paused - stopping aria2c cleanly (control files flushed)")
            if targets:
                self._requeue = True
        self.save()
        return {"ok": True}

    def resume_all(self) -> Dict[str, Any]:
        with self.lock:
            resumed = 0
            for job in self.jobs.values():
                if job.status == "COMPLETE" or not job.paused:
                    continue
                if any(f.state == "corrupt" for f in job.files):
                    continue          # needs the loud per-job error, not a silent skip
                job.paused = False
                job.error = None
                job.completed_at = None
                job.status = "RECOVERING"
                for f in job.files:
                    if f.state not in ("done", "verifying"):
                        f.state = "queued"
                        f.attempts = 0
                        f.error = None
                self.event(job, "info", "Resumed by user")
                resumed += 1
            if resumed:
                self._requeue = True
                self._next_launch_at = 0.0
        self.save()
        return {"ok": True}

    def clear_finished(self) -> Dict[str, Any]:
        """Archive COMPLETE jobs off the dashboard. Files and library entries untouched."""
        with self.lock:
            for job in self.jobs.values():
                if job.status == "COMPLETE" and not job.archived:
                    job.archived = True
        self.save()
        return {"ok": True}

    def reverify_job(self, job_id: str) -> Dict[str, Any]:
        """Re-run the verification pass on a finished job. Corruption fails it loudly."""
        with self.lock:
            job = self._job(job_id)
            if job.status not in ("COMPLETE", "FAILED"):
                raise EngineError("Re-verify is only available for a COMPLETE or FAILED job.")
            targets: List[str] = []
            missing: List[str] = []
            for f in job.files:
                f.verified = False
                f.hashed = False
                f.verify_read = 0
                if not (Path(job.dest) / f.name).exists():
                    f.state = "corrupt"
                    f.error = f"{f.name}: file is missing from {job.dest}"
                    missing.append(f.name)
                    continue
                f.state = "verifying"
                f.error = None
                targets.append(f.name)
            job.error = None
            job.bytes_lost = 0
            job.completed_at = None
            job.status = "VERIFYING"
            self.event(job, "info", f"Re-verifying {len(targets)} file(s) on disk")
            for name in missing:
                self.event(job, "err", f"{name} is missing from the destination")
            if not targets:
                self._fail_job(job, "Re-verify found no files on disk to check.")
        for name in targets:
            self._verify_q.put((job_id, name))
        self.save()
        return {"ok": True}

    def repair_job(self, job_id: str) -> Dict[str, Any]:
        """Re-queue ONLY the corrupt files of a settled job. Verified files are untouched.

        User-invoked deletion: the corrupt file(s) and their .aria2 control files are
        removed from disk, the entries reset to queued, and the job's failure is cleared
        so the supervisor re-resolves URLs and re-downloads just those files.
        """
        with self.lock:
            job = self._job(job_id)
            if job.status not in ("COMPLETE", "FAILED"):
                raise EngineError("Repair is only available for a COMPLETE or FAILED job.")
            corrupt = [f for f in job.files if f.state == "corrupt"]
            if not corrupt:
                raise EngineError("No corrupt files to redownload.")
            for f in corrupt:
                path = Path(job.dest) / f.name
                for p in (path, control_file(path)):
                    try:
                        os.unlink(p)
                    except OSError:
                        pass                  # already gone: this file was lost/corrupt
                f.state = "queued"
                f.completed = 0
                f.verified = False
                f.hashed = False
                f.verify_read = 0
                f.attempts = 0
                f.error = None
            job.error = None
            job.bytes_lost = 0
            job.paused = False
            job.completed_at = None
            job.archived = False
            job.status = "RECOVERING"         # _recompute refuses to leave COMPLETE
            self.event(job, "warn",
                       f"Repair: {len(corrupt)} corrupt file(s) deleted and re-queued for download")
            self._recompute(job)
            self._requeue = True
            self._next_launch_at = 0.0
        self.save()
        return {"ok": True}

    def open_folder(self, job_id: str) -> Dict[str, Any]:
        with self.lock:
            dest = self._job(job_id).dest
        path = Path(dest)
        if not path.is_dir():
            raise EngineError(f"Destination folder does not exist: {dest}")
        try:
            if sys.platform == "win32":
                os.startfile(str(path))                       # noqa: S606 (Windows only)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except (OSError, subprocess.SubprocessError) as exc:
            raise EngineError(f"Could not open {dest}: {exc}") from exc
        return {"ok": True}

    def browse_dest(self) -> Dict[str, Any]:
        """Native folder dialog - only available when the GUI shell wired a picker in."""
        picker = self.folder_picker
        if picker is None:
            raise EngineError("Folder browsing needs the desktop shell - type the path instead.")
        try:
            chosen = picker()
        except Exception as exc:                              # a broken shell must not 500
            raise EngineError(f"Folder dialog failed: {exc}") from exc
        return {"path": str(chosen) if chosen else None}

    def _job(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise EngineError(f"No such job: {job_id}")
        return job

    # ------------------------------------------------------------ payloads
    def settings_payload(self) -> Dict[str, Any]:
        s = dict(self.settings)
        token = s.pop("hfToken", "")           # WRITE-ONLY: never echoed, anywhere
        s["hfTokenSet"] = bool(str(token or "").strip())
        q = dict(s.get("quietHours") or {})
        try:
            q["limitMBs"] = max(1, min(50, int(q.get("limitMBs", QUIET_HOURS_LIMIT_MBS))))
        except (TypeError, ValueError):
            q["limitMBs"] = QUIET_HOURS_LIMIT_MBS
        s["quietHours"] = q
        s["serviceInstalled"] = self._service_installed()
        return s

    def _service_installed(self, ttl: float = 30.0) -> bool:
        """Cached - the check shells out to schtasks/systemctl and /api/status polls at 1 Hz."""
        checked, value = self._service_cache
        if time.time() - checked < ttl:
            return value
        from . import service
        try:
            value = bool(service.status().get("installed"))
        except Exception:
            value = False
        self._service_cache = (time.time(), value)
        return value

    def status_payload(self) -> Dict[str, Any]:
        """The complete GET /api/status body - server.py is a thin wrapper over this."""
        with self.lock:
            ordered = [self.jobs[j] for j in self._order if j in self.jobs]
            jobs = [self.job_payload(j) for j in ordered]
            remaining = self._remaining_bytes(ordered)
            library = self._library_payload(ordered)
            completed_today = sum(1 for j in ordered
                                  if j.status == "COMPLETE" and is_same_local_day(j.completed_at))
            recents = list(self.store.recents)
        dest = self.settings["destination"]
        free = disk_free(dest)
        return {
            "backend": {
                "healthy": True,
                "label": "supervisor online",
                "version": __version__,
                "uptime": uptime_label(time.time() - self._started_at),
                "aria2cVersion": self.aria2c_version(),
                "gui": self.folder_picker is not None,
            },
            "disk": {
                "path": dest,
                "freeBytes": free,
                "volumeLabel": volume_label(dest),
                "afterQueueBytes": after_queue_bytes(free, remaining),
            },
            "settings": self.settings_payload(),
            "recents": recents,
            "completedToday": completed_today,
            "connections": list(self._connections),
            "library": library,
            "jobs": jobs,
        }

    def aria2c_version(self) -> str:
        """Cached: /api/status polls at 1 Hz and this shells out to aria2c."""
        if self._aria2c_version is None:
            self._aria2c_version = aria2c_version(self.aria2_path)
        return self._aria2c_version

    @staticmethod
    def _remaining_bytes(jobs: List[Job]) -> int:
        """Bytes still to download across everything that is not finished."""
        total = 0
        for job in jobs:
            if job.status in ("COMPLETE", "FAILED"):
                continue
            total += max(0, job.total_bytes - job.done_bytes)
        return total

    @staticmethod
    def _integrity_label(job: Job) -> str:
        n = len(job.files)
        corrupt = sum(1 for f in job.files if f.state == "corrupt")
        if corrupt:
            return f"{corrupt} corrupt"
        hashed = sum(1 for f in job.files if f.hashed)
        return f"sha256 {hashed}/{n}" if hashed else "size-only"

    def _library_payload(self, jobs: List[Job]) -> List[Dict[str, Any]]:
        """COMPLETE and FAILED jobs, newest first, archived or not."""
        out: List[Dict[str, Any]] = []
        for job in jobs:
            if job.status not in ("COMPLETE", "FAILED"):
                continue
            out.append({
                "jobId": job.id,
                "name": job.name,
                "path": job.dest,
                "sizeBytes": job.total_bytes,
                "integrity": self._integrity_label(job),
                "finishedLabel": finished_label(job.completed_at or job.created_at),
            })
        return out

    def job_payload(self, job: Job) -> Dict[str, Any]:
        total = job.total_bytes
        done = job.done_bytes
        speed = self._job_speed(job)
        eta: Optional[int]
        if job.status == "COMPLETE":
            eta = 0
        elif speed > 0 and total > done:
            eta = int((total - done) / speed)
        else:
            eta = None
        end = job.completed_at or time.time()
        avg = int(done / job.active_seconds) if job.active_seconds > 1 else 0
        return {
            "id": job.id,
            "name": job.name,
            "subtitle": job.subtitle,
            "status": job.status,
            "dest": job.dest,
            "totalBytes": total,
            "doneBytes": done,
            "speedBps": int(speed),
            "etaSeconds": eta,
            "recoveries": job.recoveries,
            "bytesLost": job.bytes_lost,
            "startedLabel": started_label(job.created_at),
            "elapsedLabel": elapsed_label(end - job.created_at),
            "avgSpeedBps": avg,
            "archived": bool(job.archived),
            "connections": self._effective_connections(job),
            "files": [self._file_payload(f) for f in job.files],
            "log": list(job.log),
        }

    def _effective_connections(self, job: Job) -> int:
        """The job's operative split/max-connection-per-server value (1..16).

        ``job.connections == 0`` means "use the settings default".
        """
        try:
            value = job.connections or int(self.settings.get("connections", 4))
        except (TypeError, ValueError):
            value = 4
        return max(1, min(16, int(value)))

    @staticmethod
    def _file_payload(f: FileEntry) -> Dict[str, Any]:
        if f.state in ("done", "verifying", "corrupt"):
            progress = 100
        elif f.size:
            progress = max(0, min(100, int(f.completed * 100 / f.size)))
        else:
            progress = 0
        return {"name": f.name, "bytes": f.size, "progress": progress, "state": f.state}

    def _job_speed(self, job: Job) -> float:
        """Aggregate speed, attributed to whichever job is actually transferring."""
        if self._proc is None or not self._proc.alive():
            return 0.0
        if job.paused or job.status in ("COMPLETE", "FAILED", "PAUSED"):
            return 0.0
        if not any(f.state == "downloading" for f in job.files):
            return 0.0
        ts, bps = self._display_speed
        if time.time() - ts <= 10.0:              # fresh 2s display sample wins
            return bps
        return self._speed_samples[-1][1] if self._speed_samples else 0.0

    # ------------------------------------------------------------ bandwidth / quiet hours
    def _apply_bandwidth(self, force: bool = False) -> None:
        """Push ``max-overall-download-limit`` to the running child when it should change.

        Called on every watchdog poll (so quiet hours start/stop on time) and on
        every settings write (so a cap takes effect immediately, per the contract).
        """
        want = limit_option(effective_limit_mbs(self.settings))
        proc = self._proc
        if proc is None or not proc.alive():
            self._applied_limit = None
            return
        if not force and want == self._applied_limit:
            return
        try:
            proc.rpc.change_global_option({"max-overall-download-limit": want})
        except Aria2Error as exc:
            LOG.warning("could not set max-overall-download-limit=%s: %s", want, exc)
            return
        if want != self._applied_limit:
            quiet = quiet_hours_active(self.settings)
            with self.lock:
                for job in self._jobs_with_work():
                    self.event(job, "info",
                               ("Quiet hours - throttled to " if quiet else
                                "Bandwidth cap set to ") +
                               (f"{effective_limit_mbs(self.settings)} MB/s" if want != "0"
                                else "unlimited"))
        self._applied_limit = want

    def _refresh_connections(self) -> None:
        """aria2 getServers for the ACTIVE download. Display only - errors swallow to []."""
        proc = self._proc
        if proc is None or not proc.alive():
            self._connections = []
            return
        rows: List[Dict[str, Any]] = []
        try:
            # Short timeout: this runs on the supervisor thread and must never delay
            # a watchdog poll. It is cosmetic - an empty list is a fine answer.
            active = proc.rpc.tell_active(["gid"], timeout=CONNECTIONS_RPC_TIMEOUT)
            gid = (active or [{}])[0].get("gid")
            entries = proc.rpc.get_servers(gid, timeout=CONNECTIONS_RPC_TIMEOUT) if gid else []
            for entry in entries or []:
                for srv in entry.get("servers") or []:
                    rows.append({
                        "id": f"c-{len(rows) + 1:02d}",
                        "speedBps": int(srv.get("downloadSpeed", 0) or 0),
                        "host": host_of(srv.get("currentUri") or srv.get("uri") or ""),
                    })
        except Exception:                       # never a health signal, never fatal (5.1)
            rows = []
        self._connections = rows

    def _display_refresh(self) -> None:
        """Fresh doneBytes/speed for the UI between watchdog polls.

        DISPLAY ONLY: short timeouts, errors ignored, and nothing here feeds
        ``_speed_samples`` - the stall math stays owned by the watchdog (5.1).
        """
        proc = self._proc
        if proc is None or not proc.alive():
            self._display_speed = (0.0, 0.0)
            return
        try:
            stat = proc.rpc.get_global_stat(timeout=CONNECTIONS_RPC_TIMEOUT)
            self._display_speed = (time.time(),
                                   float(stat.get("downloadSpeed", 0) or 0))
        except Exception:
            pass
        try:
            self._refresh_files(active_only=True, rpc_timeout=CONNECTIONS_RPC_TIMEOUT)
        except Exception:
            pass

    # ------------------------------------------------------------ supervisor
    def _supervise(self) -> None:
        last_poll = 0.0
        last_conn = 0.0
        last_tick = time.time()
        while not self._stop_evt.is_set():
            try:
                now = time.time()
                dt, last_tick = now - last_tick, now

                if self._proc is not None and not self._proc.alive():
                    self._on_aria_exit()

                if now - last_conn >= CONNECTIONS_REFRESH_S:
                    last_conn = now
                    self._refresh_connections()
                    self._display_refresh()

                with self.lock:
                    self._accumulate_active(dt)
                    self._recompute_all()
                    work = self._jobs_with_work()
                    requeue = self._requeue
                    self._requeue = False
                    if self._dirty:
                        self._dirty = False
                        dirty = True
                    else:
                        dirty = False
                if dirty:
                    self.save()

                if requeue and self._proc is not None:
                    self._shutdown_aria("job set changed")
                    self._next_launch_at = 0.0
                    continue

                if not work:
                    if self._proc is not None:
                        self._shutdown_aria("no work remaining")
                    self._stop_evt.wait(0.5)
                    continue

                if self._proc is None:
                    if now >= self._next_launch_at:
                        self._launch(work)
                        last_poll = time.time()
                    else:
                        self._stop_evt.wait(0.5)
                    continue

                if now - last_poll >= self.poll_interval:
                    last_poll = now
                    self._watchdog_poll()
                self._stop_evt.wait(0.5)
            except Exception:                     # never let the supervisor die silently
                LOG.exception("supervisor loop error")
                self._stop_evt.wait(2.0)

    def _accumulate_active(self, dt: float) -> None:
        if dt <= 0 or dt > 30:
            return
        for job in self.jobs.values():
            if job.status in ("DOWNLOADING", "RECOVERING", "VERIFYING"):
                job.active_seconds += dt

    def _jobs_with_work(self) -> List[Job]:
        out = []
        for jid in self._order:
            job = self.jobs.get(jid)
            if job is None or job.paused or job.status in ("COMPLETE", "FAILED"):
                continue
            if any(f.state in ("queued", "downloading") for f in job.files):
                out.append(job)
        return out

    # -- launching ------------------------------------------------
    def _launch(self, work: List[Job]) -> None:
        log_path = self.store.dir / "aria2.log"
        limit = limit_option(effective_limit_mbs(self.settings))
        try:
            proc = spawn(log_path=log_path, connections=int(self.settings["connections"]),
                         aria2_path=self.aria2_path, download_limit=limit)
        except Aria2Error as exc:
            LOG.error("aria2c launch failed: %s", exc)
            with self.lock:
                for job in work:
                    self.event(job, "err", f"aria2c failed to start: {exc}")
                    job.status = "RECOVERING"
            self._next_launch_at = time.time() + self.relaunch_backoff
            self.save()
            return

        self._proc = proc
        self._launches += 1
        self._stalls = 0
        self._speed_samples.clear()
        self._gids.clear()
        self._keys.clear()
        self._applied_limit = limit          # already on the command line
        with self.lock:
            for job in work:
                n = self._enqueue_files(job)
                job.status = "DOWNLOADING"
                self.event(job, "info",
                           f"Launched aria2c (RPC port {proc.port}) - {n} file(s) queued, "
                           "URLs re-resolved from source")
        self.save()

    def _enqueue_files(self, job: Job) -> int:
        """Submit the ORIGINAL urls (fresh signed redirects on every relaunch)."""
        if self._proc is None or not self._proc.alive():
            return 0
        count = 0
        for f in job.files:
            if f.state not in ("queued", "downloading"):
                continue
            if (job.id, f.name) in self._keys:
                continue
            out = f.name.replace("\\", "/")
            conn = str(self._effective_connections(job))
            options = {"dir": job.dest, "out": out,
                       "split": conn, "max-connection-per-server": conn}
            try:
                gid = self._proc.rpc.add_uri([f.url], options)
            except Aria2Error as exc:
                self.event(job, "err", f"{f.name}: could not queue - {exc}")
                continue
            self._gids[gid] = (job.id, f.name)
            self._keys[(job.id, f.name)] = gid
            f.state = "queued"
            count += 1
        return count

    def _shutdown_aria(self, reason: str) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        LOG.info("stopping aria2c (%s)", reason)
        try:
            proc.terminate()
        except Exception:
            LOG.exception("error terminating aria2c")
        self._gids.clear()
        self._keys.clear()
        self._speed_samples.clear()
        self._stalls = 0
        self._applied_limit = None
        self._connections = []

    def _on_aria_exit(self) -> None:
        proc, self._proc = self._proc, None
        rc = proc.returncode if proc else None
        if proc is not None:
            proc.rpc.close()
        self._gids.clear()
        self._keys.clear()
        self._speed_samples.clear()
        self._stalls = 0
        self._applied_limit = None
        self._connections = []
        with self.lock:
            work = self._jobs_with_work()
            for job in work:
                job.status = "RECOVERING"
                job.recoveries += 1        # an unplanned exit costs one supervisor relaunch
                self.event(job, "warn",
                           f"aria2c exited (code {rc}) - relaunching in "
                           f"{int(self.relaunch_backoff)}s with fresh URLs")
        self._next_launch_at = time.time() + (self.relaunch_backoff if work else 0.0)
        self.save()

    # -- watchdog -------------------------------------------------
    def _watchdog_poll(self) -> None:
        proc = self._proc
        if proc is None or not proc.alive():
            return
        # Quiet hours / bandwidth cap are re-evaluated on every poll so the window
        # opens and closes on its own without a relaunch.
        self._apply_bandwidth()
        try:
            stat = proc.rpc.get_global_stat()
        except Aria2Error as exc:
            LOG.warning("getGlobalStat failed (%s) - restarting aria2c", exc)
            with self.lock:
                for job in self._jobs_with_work():
                    self.event(job, "warn", "aria2 RPC unreachable - restarting aria2c")
            self._kill_and_relaunch("rpc unreachable", recovery=True)
            return

        speed = float(stat.get("downloadSpeed", 0) or 0)
        active = int(stat.get("numActive", 0) or 0)
        waiting = int(stat.get("numWaiting", 0) or 0)

        self._refresh_files()

        now = time.time()
        self._speed_samples.append((now, speed))
        while self._speed_samples and now - self._speed_samples[0][0] > STALL_WINDOW_S:
            self._speed_samples.popleft()

        # queue drained: aria2 in RPC mode never exits on its own (field notes 4)
        if active == 0 and waiting == 0:
            LOG.info("aria2 queue drained - stopping child")
            self._shutdown_aria("queue drained")
            self._next_launch_at = time.time() + self.relaunch_backoff
            self.save()
            return

        # Stall detection: AGGREGATE downloadSpeed only. Never per-connection (5.1).
        threshold = adaptive_threshold(self._speed_samples,
                                       self.settings.get("stallSensitivity", "Normal"))
        if speed < threshold:
            self._stalls += 1
            LOG.info("stall poll %d/%d (%.0f B/s < %.0f B/s)",
                     self._stalls, self.stall_polls, speed, threshold)
            if self._stalls >= self.stall_polls:
                mins = int(self._stalls * self.poll_interval / 60) or 1
                with self.lock:
                    for job in self._jobs_with_work():
                        job.recoveries += 1
                        self.event(job, "warn",
                                   f"Stall detected (speed {speed/1024:.0f} KB/s < "
                                   f"{threshold/1024:.0f} KB/s for {mins} min) - restarting aria2c")
                        self.event(job, "info",
                                   "Re-resolving download URL (previous CDN token may have expired)")
                self._kill_and_relaunch("stalled", recovery=False)
        else:
            self._stalls = 0

    def _kill_and_relaunch(self, reason: str, recovery: bool) -> None:
        if recovery:
            with self.lock:
                for job in self._jobs_with_work():
                    job.recoveries += 1
        self._shutdown_aria(reason)
        self._next_launch_at = time.time() + self.relaunch_backoff
        self.save()

    def _refresh_files(self, active_only: bool = False,
                       rpc_timeout: Optional[float] = None) -> None:
        """Progress + per-file completion straight from RPC (never file sizes: falloc).

        ``active_only`` polls just the files currently downloading (the cheap 2s
        display path); it falls back to a full pass when none are downloading so
        a file handoff is picked up without waiting for the next watchdog poll.
        """
        proc = self._proc
        if proc is None:
            return
        finished: List[Tuple[str, str]] = []
        with self.lock:
            items = list(self._gids.items())
            if active_only:
                downloading = [
                    (gid, tag) for gid, tag in items
                    if (jb := self.jobs.get(tag[0])) is not None
                    and (fe := jb.file(tag[1])) is not None
                    and fe.state == "downloading"
                ]
                if downloading:
                    items = downloading
            for gid, (job_id, name) in items:
                job = self.jobs.get(job_id)
                if job is None:
                    continue
                f = job.file(name)
                if f is None or f.state in ("done", "verifying", "corrupt"):
                    continue
                try:
                    st = proc.rpc.tell_status(gid, STATUS_KEYS, timeout=rpc_timeout)
                except Aria2Error as exc:
                    LOG.debug("tellStatus(%s) failed: %s", gid, exc)
                    continue
                status = st.get("status")
                completed = int(st.get("completedLength", 0) or 0)
                total = int(st.get("totalLength", 0) or 0)
                f.completed = completed
                if total and not f.size:
                    f.size = total
                if status == "active":
                    if f.state != "downloading":
                        self.event(job, "info",
                                   f"{f.name} resumed at offset {human_bytes(completed)}"
                                   if completed else f"{f.name} downloading")
                    f.state = "downloading"
                elif status in ("waiting", "paused"):
                    f.state = "queued"
                elif status == "complete":
                    f.state = "verifying"
                    f.completed = f.size or completed
                    self.event(job, "info",
                               f"{f.name} transfer complete - {human_bytes(f.completed)}")
                    finished.append((job_id, f.name))
                    self._gids.pop(gid, None)
                    self._keys.pop((job_id, f.name), None)
                elif status in ("error", "removed"):
                    f.attempts += 1
                    f.error = st.get("errorMessage") or f"aria2 errorCode={st.get('errorCode')}"
                    self.event(job, "err",
                               f"{f.name}: aria2 reported {f.error} "
                               f"(attempt {f.attempts}/{MAX_FILE_ATTEMPTS})")
                    self._gids.pop(gid, None)
                    self._keys.pop((job_id, f.name), None)
                    if f.attempts >= MAX_FILE_ATTEMPTS:
                        self._fail_job(job, f"{f.name} could not be downloaded: {f.error}")
                    else:
                        f.state = "queued"
            self._recompute_all()
        for item in finished:
            self._verify_q.put(item)

    # -- status transitions ---------------------------------------
    def _fail_job(self, job: Job, message: str) -> None:
        job.error = message
        job.status = "FAILED"
        job.completed_at = job.completed_at or time.time()   # freezes elapsed + Library label
        self.event(job, "err", f"Job FAILED - {message}")
        self._dirty = True
        # Drop this job's remaining files out of the shared aria2 queue cleanly.
        self._requeue = True

    def _recompute_all(self) -> None:
        for job in self.jobs.values():
            self._recompute(job)

    def _recompute(self, job: Job) -> None:
        if job.status == "COMPLETE":
            return
        if job.error or any(f.state == "corrupt" for f in job.files):
            if job.status != "FAILED":
                job.status = "FAILED"
                job.completed_at = job.completed_at or time.time()
                self._dirty = True
            return
        if job.paused:
            job.status = "PAUSED"
            return
        states = [f.state for f in job.files]
        if all(s == "done" for s in states):
            self._complete(job)
            return
        pending = any(s in ("queued", "downloading") for s in states)
        if not pending and "verifying" in states:
            job.status = "VERIFYING"
            return
        job.status = "DOWNLOADING" if (self._proc is not None and self._proc.alive()) \
            else "RECOVERING"

    def _complete(self, job: Job) -> None:
        job.status = "COMPLETE"
        job.completed_at = time.time()
        results = [{"name": f.name, "bytes": f.size, "sha256": f.sha256,
                    "verified": bool(f.verified),
                    "hashChecked": bool(f.hashed)} for f in job.files]
        try:
            write_completion_marker(job.dest, job.id, job.name, results,
                                    extra={"url": job.url, "repo": job.repo,
                                           "revision": job.revision,
                                           "recoveries": job.recoveries,
                                           "totalBytes": job.total_bytes})
        except OSError as exc:
            LOG.error("could not write %s: %s", COMPLETE_MARKER, exc)
        n = len(job.files)
        hashed = sum(1 for f in job.files if f.hashed)
        checked = (f"{hashed}/{n} files SHA256 verified" if hashed
                   else f"{n} file{'s' if n != 1 else ''} size-verified (no checksum run)")
        self.event(job, "ok",
                   f"Job complete - {human_bytes(job.total_bytes)}, {checked}, "
                   f"{job.recoveries} recoveries, 0 bytes lost")
        self._dirty = True

    # -- verification worker ---------------------------------------
    def _verify_worker(self) -> None:
        while not self._stop_evt.is_set():
            try:
                job_id, name = self._verify_q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._verify_one(job_id, name)
            except Exception:
                LOG.exception("verification error for %s/%s", job_id, name)

    def _verify_one(self, job_id: str, name: str) -> None:
        with self.lock:
            job = self.jobs.get(job_id)
            f = job.file(name) if job else None
            if job is None or f is None or f.state not in ("verifying",):
                return
            path = Path(job.dest) / f.name
            expected_size = f.size
            # verifyChecksums=false: hashing is skipped, the size check stays mandatory.
            expected_sha = f.sha256 if self._verify_checksums() else None

        # Wait for aria2 to drop the .aria2 control file (ARCHITECTURE 5).
        deadline = time.time() + 60
        while control_file(path).exists() and time.time() < deadline:
            if self._stop_evt.wait(1.0):
                return
        if control_file(path).exists():
            with self.lock:
                f.state = "queued"
                self.event(job, "warn", f"{name}: control file still present - re-queued")
            return

        with self.lock:
            self.event(job, "info", "Hashing " + name if expected_sha
                       else f"Checking size of {name}")

        last = [time.time()]      # first progress event no sooner than 30s in

        def progress(read: int, total: int) -> None:
            if self._stop_evt.is_set():
                raise _HashAborted()          # leaves the file 'verifying' -> retried on restart
            f.verify_read = read
            now = time.time()
            if total and now - last[0] > 30:
                last[0] = now
                with self.lock:
                    self.event(job, "info",
                               f"Hashing {name} - {human_bytes(read)} / {human_bytes(total)} read")

        try:
            result = verify_file(path, expected_size, expected_sha, progress=progress)
        except _HashAborted:
            LOG.info("verification of %s aborted by shutdown - will resume next start", name)
            return

        with self.lock:
            if result.ok:
                f.state = "done"
                f.verified = True
                f.hashed = bool(result.hashed)
                f.completed = result.size
                f.size = f.size or result.size
                self.event(job, "ok", f"{name} SHA256 verified" if result.hashed
                           else f"{name} size verified ({human_bytes(result.size)})")
            else:
                f.state = "corrupt"
                f.verified = False
                f.hashed = bool(result.hashed)
                f.error = result.error
                job.bytes_lost += f.size or result.size
                self.event(job, "err", result.error or f"{name} failed verification")
                self._fail_job(job, result.error or f"{name} failed verification")
            self._recompute(job)
        self.save()
