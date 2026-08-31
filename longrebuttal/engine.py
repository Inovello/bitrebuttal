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
import shutil
import statistics
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

from . import __version__
from .aria2 import Aria2Error, Aria2Process, spawn
from .resolve import Manifest, resolve
from .state import (COMPLETE_MARKER, FileEntry, Job, Store, new_job_id, normalize_settings)
from .verify import control_file, verify_file, write_completion_marker

LOG = logging.getLogger("longrebuttal.engine")

STALL_FLOOR_BPS = 10 * 1024          # absolute floor: 10 KB/s
STALL_FRACTION = 0.05                # 5% of trailing 30-min median aggregate speed
STALL_WINDOW_S = 30 * 60
STALL_POLLS = 12                     # consecutive stalled polls before kill+relaunch
RELAUNCH_BACKOFF_S = 15.0
MAX_FILE_ATTEMPTS = 8                # aria2 errors on one file before the job FAILS
DISK_HEADROOM = 1.05                 # total size + 5% (ARCHITECTURE 4)
SENSITIVITY_MULT = {"Low": 0.5, "Normal": 1.0, "High": 2.0}

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


# ---------------------------------------------------------------- engine


class Engine:
    def __init__(self, data_dir=None, poll_interval: float = 60.0,
                 relaunch_backoff: float = RELAUNCH_BACKOFF_S,
                 aria2_path: str = "aria2c", stall_polls: int = STALL_POLLS):
        self.store = Store(data_dir)
        self.settings = self.store.load_settings()
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

    # ------------------------------------------------------------ lifecycle
    def preflight(self) -> str:
        exe = shutil.which(self.aria2_path)
        if not exe:
            hint = ("winget install aria2.aria2" if sys.platform == "win32"
                    else "brew install aria2" if sys.platform == "darwin"
                    else "sudo apt install aria2")
            raise EngineError(f"aria2c not found on PATH. Install it: {hint}")
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
        return resolve(url).payload()

    def add_job(self, url: str, files: Optional[List[str]] = None,
                dest: Optional[str] = None) -> Dict[str, Any]:
        manifest: Manifest = resolve(url)          # never trust client-provided sizes
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

        job = Job(
            id=new_job_id(),
            name=manifest.name,
            url=url,
            dest=str(base),
            files=[FileEntry(name=f.name, url=f.url, size=f.size, sha256=f.sha256)
                   for f in selected],
            subtitle=self._subtitle(selected),
            repo=manifest.repo,
            revision=manifest.revision,
            status="DOWNLOADING",
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
        """
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

    @staticmethod
    def _subtitle(files) -> str:
        n = len(files)
        hashed = sum(1 for f in files if f.sha256)
        kinds = {Path(f.name).suffix.lstrip(".").lower() for f in files if Path(f.name).suffix}
        if n and hashed == n:
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

    def update_settings(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(self.settings)
        merged.update({k: v for k, v in (raw or {}).items() if v is not None})
        self.settings = self.store.save_settings(normalize_settings(merged))
        return self.settings_payload()

    def _job(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise EngineError(f"No such job: {job_id}")
        return job

    # ------------------------------------------------------------ payloads
    def settings_payload(self) -> Dict[str, Any]:
        s = dict(self.settings)
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
            jobs = [self.job_payload(self.jobs[j]) for j in self._order if j in self.jobs]
        dest = self.settings["destination"]
        return {
            "backend": {
                "healthy": True,
                "label": "supervisor online",
                "version": __version__,
                "uptime": uptime_label(time.time() - self._started_at),
            },
            "disk": {
                "path": dest,
                "freeBytes": disk_free(dest),
                "volumeLabel": volume_label(dest),
            },
            "settings": self.settings_payload(),
            "jobs": jobs,
        }

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
            "files": [self._file_payload(f) for f in job.files],
            "log": list(job.log),
        }

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
        if not self._speed_samples or self._proc is None or not self._proc.alive():
            return 0.0
        if job.paused or job.status in ("COMPLETE", "FAILED", "PAUSED"):
            return 0.0
        if not any(f.state == "downloading" for f in job.files):
            return 0.0
        return self._speed_samples[-1][1]

    # ------------------------------------------------------------ supervisor
    def _supervise(self) -> None:
        last_poll = 0.0
        last_tick = time.time()
        while not self._stop_evt.is_set():
            try:
                now = time.time()
                dt, last_tick = now - last_tick, now

                if self._proc is not None and not self._proc.alive():
                    self._on_aria_exit()

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
        try:
            proc = spawn(log_path=log_path, connections=int(self.settings["connections"]),
                         aria2_path=self.aria2_path)
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
            options = {"dir": job.dest, "out": out}
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

    def _on_aria_exit(self) -> None:
        proc, self._proc = self._proc, None
        rc = proc.returncode if proc else None
        if proc is not None:
            proc.rpc.close()
        self._gids.clear()
        self._keys.clear()
        self._speed_samples.clear()
        self._stalls = 0
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

    def _refresh_files(self) -> None:
        """Progress + per-file completion straight from RPC (never file sizes: falloc)."""
        proc = self._proc
        if proc is None:
            return
        finished: List[Tuple[str, str]] = []
        with self.lock:
            for gid, (job_id, name) in list(self._gids.items()):
                job = self.jobs.get(job_id)
                if job is None:
                    continue
                f = job.file(name)
                if f is None or f.state in ("done", "verifying", "corrupt"):
                    continue
                try:
                    st = proc.rpc.tell_status(gid, STATUS_KEYS)
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
                    "hashChecked": bool(f.sha256)} for f in job.files]
        try:
            write_completion_marker(job.dest, job.id, job.name, results,
                                    extra={"url": job.url, "repo": job.repo,
                                           "revision": job.revision,
                                           "recoveries": job.recoveries,
                                           "totalBytes": job.total_bytes})
        except OSError as exc:
            LOG.error("could not write %s: %s", COMPLETE_MARKER, exc)
        hashed = sum(1 for f in job.files if f.sha256)
        self.event(job, "ok",
                   f"Job complete - {human_bytes(job.total_bytes)}, {hashed}/{len(job.files)} "
                   f"files SHA256 verified, {job.recoveries} recoveries, 0 bytes lost")
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
            expected_size, expected_sha = f.size, f.sha256

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
                f.completed = result.size
                f.size = f.size or result.size
                self.event(job, "ok", f"{name} SHA256 verified" if result.hashed
                           else f"{name} size verified ({human_bytes(result.size)})")
            else:
                f.state = "corrupt"
                f.verified = False
                f.error = result.error
                job.bytes_lost += f.size or result.size
                self.event(job, "err", result.error or f"{name} failed verification")
                self._fail_job(job, result.error or f"{name} failed verification")
            self._recompute(job)
        self.save()
