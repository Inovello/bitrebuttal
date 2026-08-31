"""Persistent state: data dir discovery, settings, jobs model, atomic state.json.

aria2's own ``.aria2`` control files hold the byte-level resume state; this module
only holds job metadata, so a torn write here can never cost downloaded bytes.
Every write is temp-file + fsync + os.replace (atomic on both NTFS and ext4).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import APP_NAME

STATE_VERSION = 1

# ---------------------------------------------------------------- locations


def data_dir() -> Path:
    """%LOCALAPPDATA%\\longrebuttal (Windows), ~/Library/Application Support (macOS), ~/.local/share (Linux)."""
    override = os.environ.get("LONGREBUTTAL_DATA_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path.home() / ".local" / "share" / APP_NAME


def default_destination() -> Path:
    return Path.home() / "Downloads" / APP_NAME


COMPLETE_MARKER = ".longrebuttal-complete"
PORTFILE = "portfile"

# ---------------------------------------------------------------- atomic io


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, indent=2, sort_keys=False))


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


# ---------------------------------------------------------------- model

FILE_STATES = ("queued", "downloading", "done", "verifying", "corrupt")
JOB_STATES = ("DOWNLOADING", "RECOVERING", "VERIFYING", "PAUSED", "COMPLETE", "FAILED")


@dataclass
class FileEntry:
    name: str                      # path relative to job dest (may contain '/')
    url: str                       # ORIGINAL url - re-submitted on every relaunch
    size: int = 0                  # authoritative size from the source (0 = unknown)
    sha256: Optional[str] = None   # lower-case hex when the source publishes it
    state: str = "queued"
    completed: int = 0             # bytes, from aria2 RPC completedLength
    attempts: int = 0              # aria2 error count for this file
    error: Optional[str] = None
    verified: bool = False
    verify_read: int = 0           # bytes hashed so far (transient)

    def to_dict(self) -> Dict[str, Any]:
        d = dict(self.__dict__)
        d.pop("verify_read", None)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "FileEntry":
        known = {k: d.get(k) for k in cls.__dataclass_fields__ if k in d}
        known.setdefault("name", d.get("name", ""))
        return cls(**known)  # type: ignore[arg-type]


@dataclass
class Job:
    id: str
    name: str
    url: str                       # the original user input
    dest: str
    files: List[FileEntry] = field(default_factory=list)
    subtitle: str = ""
    repo: Optional[str] = None
    revision: Optional[str] = None
    status: str = "DOWNLOADING"
    recoveries: int = 0
    bytes_lost: int = 0
    paused: bool = False
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    active_seconds: float = 0.0
    log: List[Dict[str, str]] = field(default_factory=list)

    # ---- derived helpers -------------------------------------------------
    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def done_bytes(self) -> int:
        return sum(f.size if f.state in ("done", "verifying") else f.completed for f in self.files)

    def file(self, name: str) -> Optional[FileEntry]:
        for f in self.files:
            if f.name == name:
                return f
        return None

    def to_dict(self) -> Dict[str, Any]:
        d = dict(self.__dict__)
        d["files"] = [f.to_dict() for f in self.files]
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Job":
        d = dict(d)
        files = [FileEntry.from_dict(f) for f in d.pop("files", [])]
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        job = cls(files=files, **known)  # type: ignore[arg-type]
        return job


def new_job_id() -> str:
    return "job-" + uuid.uuid4().hex[:6]


# ---------------------------------------------------------------- settings

DEFAULT_SETTINGS: Dict[str, Any] = {
    "destination": str(default_destination()),
    "connections": 4,
    "stallSensitivity": "Normal",
}
STALL_SENSITIVITIES = ("Low", "Normal", "High")


def normalize_settings(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    s = dict(DEFAULT_SETTINGS)
    if raw:
        if raw.get("destination"):
            s["destination"] = str(raw["destination"])
        try:
            conn = int(raw.get("connections", s["connections"]))
            s["connections"] = max(1, min(16, conn))
        except (TypeError, ValueError):
            pass
        sens = raw.get("stallSensitivity")
        if sens in STALL_SENSITIVITIES:
            s["stallSensitivity"] = sens
    return s


# ---------------------------------------------------------------- store


class Store:
    """state.json + settings.json in the data dir. Thread-safe, atomic."""

    def __init__(self, directory: Optional[os.PathLike | str] = None):
        self.dir = Path(directory) if directory else data_dir()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir / "state.json"
        self.settings_path = self.dir / "settings.json"
        self.portfile_path = self.dir / PORTFILE
        self._lock = threading.Lock()

    # -- jobs
    def save_jobs(self, jobs: List[Job]) -> None:
        payload = {"version": STATE_VERSION, "savedAt": time.time(),
                   "jobs": [j.to_dict() for j in jobs]}
        with self._lock:
            atomic_write_json(self.state_path, payload)

    def load_jobs(self) -> List[Job]:
        raw = read_json(self.state_path, default=None)
        if not isinstance(raw, dict):
            return []
        out: List[Job] = []
        for jd in raw.get("jobs", []):
            try:
                out.append(Job.from_dict(jd))
            except Exception:
                continue
        return out

    # -- settings
    def load_settings(self) -> Dict[str, Any]:
        return normalize_settings(read_json(self.settings_path, default=None))

    def save_settings(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        s = normalize_settings(settings)
        with self._lock:
            atomic_write_json(self.settings_path, s)
        return s

    # -- portfile (so headless `longrebuttal add/status` can find a live instance)
    def write_portfile(self, port: int) -> None:
        atomic_write_json(self.portfile_path, {"port": int(port), "pid": os.getpid(),
                                               "started": time.time()})

    def read_portfile(self) -> Optional[Dict[str, Any]]:
        d = read_json(self.portfile_path, default=None)
        return d if isinstance(d, dict) and "port" in d else None

    def clear_portfile(self) -> None:
        try:
            os.unlink(self.portfile_path)
        except OSError:
            pass
