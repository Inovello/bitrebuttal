"""Post-download verification: size always, SHA256 when the source publishes one.

Rules (ARCHITECTURE 5): verify only once the ``.aria2`` control file is gone;
on mismatch DELETE NOTHING - mark the file corrupt and fail the job loudly.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

CHUNK = 8 * 1024 * 1024          # 8 MB streaming reads
ProgressCb = Optional[Callable[[int, int], None]]


@dataclass
class VerifyResult:
    ok: bool
    size: int = 0
    sha256: Optional[str] = None
    error: Optional[str] = None
    hashed: bool = False


def control_file(path: os.PathLike | str) -> Path:
    return Path(str(path) + ".aria2")


def is_settled(path: os.PathLike | str) -> bool:
    """True when the file exists and aria2 has removed its control file."""
    p = Path(path)
    return p.exists() and not control_file(p).exists()


def sha256_file(path: os.PathLike | str, progress: ProgressCb = None,
                chunk: int = CHUNK) -> str:
    total = os.path.getsize(path)
    h = hashlib.sha256()
    read = 0
    with open(path, "rb", buffering=0) as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
            read += len(block)
            if progress:
                progress(read, total)
    return h.hexdigest()


def verify_file(path: os.PathLike | str, expected_size: int = 0,
                expected_sha256: Optional[str] = None,
                progress: ProgressCb = None) -> VerifyResult:
    p = Path(path)
    if not p.exists():
        return VerifyResult(False, error=f"{p.name}: file is missing after download")
    if control_file(p).exists():
        return VerifyResult(False, error=f"{p.name}: still incomplete (.aria2 control file present)")

    size = p.stat().st_size
    if expected_size and size != expected_size:
        return VerifyResult(False, size=size,
                            error=(f"{p.name}: size mismatch - expected {expected_size} bytes, "
                                   f"found {size}"))
    if not expected_sha256:
        return VerifyResult(True, size=size, sha256=None, hashed=False)

    digest = sha256_file(p, progress=progress)
    if digest.lower() != expected_sha256.lower():
        return VerifyResult(False, size=size, sha256=digest, hashed=True,
                            error=(f"{p.name}: SHA256 mismatch - expected "
                                   f"{expected_sha256[:8]}...{expected_sha256[-4:]}, got "
                                   f"{digest[:8]}...{digest[-4:]}"))
    return VerifyResult(True, size=size, sha256=digest, hashed=True)


# ---------------------------------------------------------------- completion marker

MARKER_NAME = ".longrebuttal-complete"


def write_completion_marker(dest: os.PathLike | str, job_id: str, job_name: str,
                            files: List[Dict[str, Any]], extra: Optional[Dict[str, Any]] = None
                            ) -> Path:
    """`.longrebuttal-complete` in the destination dir: timestamp + per-file results."""
    from . import __version__
    payload: Dict[str, Any] = {
        "tool": "longrebuttal",
        "version": __version__,
        "jobId": job_id,
        "name": job_name,
        "completedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": files,
    }
    if extra:
        payload.update(extra)
    path = Path(dest) / MARKER_NAME
    from .state import atomic_write_json
    atomic_write_json(path, payload)
    return path


def read_completion_marker(dest: os.PathLike | str) -> Optional[Dict[str, Any]]:
    try:
        with open(Path(dest) / MARKER_NAME, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None
