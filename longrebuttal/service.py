"""Reboot survival: user systemd unit (Linux) / Task Scheduler ONLOGON task (Windows) / launchd LaunchAgent (macOS).

Every function returns a dict; when a step needs elevation or a manual action we
return the exact command for the user to run instead of failing silently.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

TASK_NAME = "LongRebuttal"
UNIT_NAME = "longrebuttal.service"
LAUNCHD_LABEL = "com.longrebuttal.serve"
DEFAULT_PORT = 7451

UNIT_TEMPLATE = """\
[Unit]
Description=Long Rebuttal - resilient download supervisor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure
RestartSec=20
TimeoutStopSec=45
KillMode=control-group

[Install]
WantedBy=default.target
"""


def _python() -> str:
    return sys.executable or "python"


def _launch_cmd(port: int = DEFAULT_PORT, headless: bool = True) -> List[str]:
    cmd = [_python(), "-m", "longrebuttal", "serve"]
    if headless:
        cmd.append("--headless")
    if port and port != DEFAULT_PORT:
        cmd += ["--port", str(port)]
    return cmd


def _run(argv: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, shell=False)


# ---------------------------------------------------------------- Windows


def _schtasks() -> Optional[str]:
    return shutil.which("schtasks")


def _task_run_string(port: int) -> str:
    """The /TR value. Quote the interpreter only when it needs it - schtasks mangles
    nested quotes, and the common install has no spaces in the path."""
    exe, *rest = _launch_cmd(port)
    exe = f'\\"{exe}\\"' if " " in exe else exe
    return " ".join([exe, *rest])


def _win_install(port: int) -> Dict[str, Any]:
    exe = _schtasks()
    tr = _task_run_string(port)
    manual = f'schtasks /Create /SC ONLOGON /TN {TASK_NAME} /TR "{tr}" /F'
    if not exe:
        return {"installed": False, "error": "schtasks.exe not found on PATH.",
                "command": manual}
    # ONLOGON needs no admin rights (unlike ONSTART).
    res = _run([exe, "/Create", "/SC", "ONLOGON", "/TN", TASK_NAME, "/TR", tr, "/F"])
    if res.returncode != 0:
        return {"installed": False,
                "error": (res.stderr or res.stdout or "schtasks failed").strip(),
                "command": manual}
    check = _run([exe, "/Query", "/TN", TASK_NAME, "/FO", "LIST", "/V"])
    if "longrebuttal" not in (check.stdout or "").lower():
        return {"installed": False,
                "error": "schtasks accepted the task but did not record the command line "
                         "(quoting of the interpreter path). Create it manually:",
                "command": manual}
    return {"installed": True,
            "message": f"Scheduled task '{TASK_NAME}' created (runs at logon).",
            "command": manual}


def _win_uninstall() -> Dict[str, Any]:
    exe = _schtasks()
    manual = f"schtasks /Delete /TN {TASK_NAME} /F"
    if not exe:
        return {"installed": False, "error": "schtasks.exe not found on PATH.", "command": manual}
    res = _run([exe, "/Delete", "/TN", TASK_NAME, "/F"])
    if res.returncode != 0 and "cannot find" not in (res.stderr + res.stdout).lower():
        return {"installed": True, "error": (res.stderr or res.stdout).strip(), "command": manual}
    return {"installed": False, "message": f"Scheduled task '{TASK_NAME}' removed."}


def _win_status() -> Dict[str, Any]:
    exe = _schtasks()
    if not exe:
        return {"installed": False, "detail": "schtasks.exe not found on PATH."}
    res = _run([exe, "/Query", "/TN", TASK_NAME])
    if res.returncode != 0:
        return {"installed": False, "detail": f"No scheduled task '{TASK_NAME}'."}
    return {"installed": True,
            "detail": (res.stdout or "").strip().splitlines()[-1] if res.stdout else "installed"}


# ---------------------------------------------------------------- Linux


def _unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / UNIT_NAME


def _systemctl() -> Optional[str]:
    return shutil.which("systemctl")


def _linux_install(port: int) -> Dict[str, Any]:
    path = _unit_path()
    exec_start = " ".join(_launch_cmd(port))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(UNIT_TEMPLATE.format(exec_start=exec_start), encoding="utf-8")
    except OSError as exc:
        return {"installed": False, "error": f"Could not write {path}: {exc}"}

    hint = (f"loginctl enable-linger {os.environ.get('USER', '$USER')}   "
            "# so the unit runs without an active login session")
    sc = _systemctl()
    if not sc:
        return {"installed": False, "error": "systemctl not found.",
                "command": f"systemctl --user daemon-reload && "
                           f"systemctl --user enable --now {UNIT_NAME}",
                "hint": hint}
    _run([sc, "--user", "daemon-reload"])
    res = _run([sc, "--user", "enable", "--now", UNIT_NAME])
    if res.returncode != 0:
        return {"installed": False, "error": (res.stderr or res.stdout).strip(),
                "command": f"systemctl --user enable --now {UNIT_NAME}", "hint": hint}
    return {"installed": True, "message": f"Wrote {path} and enabled {UNIT_NAME}.", "hint": hint}


def _linux_uninstall() -> Dict[str, Any]:
    sc = _systemctl()
    if sc:
        _run([sc, "--user", "disable", "--now", UNIT_NAME])
    path = _unit_path()
    try:
        path.unlink()
    except OSError:
        pass
    if sc:
        _run([sc, "--user", "daemon-reload"])
    return {"installed": False, "message": f"Removed {path} and disabled {UNIT_NAME}."}


def _linux_status() -> Dict[str, Any]:
    sc = _systemctl()
    if not sc:
        return {"installed": _unit_path().exists(),
                "detail": "systemctl not found; unit file "
                          + ("present" if _unit_path().exists() else "absent")}
    res = _run([sc, "--user", "is-enabled", UNIT_NAME])
    enabled = res.stdout.strip() or res.stderr.strip()
    active = _run([sc, "--user", "is-active", UNIT_NAME]).stdout.strip()
    return {"installed": enabled == "enabled", "detail": f"{enabled}, {active or 'inactive'}"}


# ---------------------------------------------------------------- macOS


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _launchctl() -> Optional[str]:
    return shutil.which("launchctl")


def _launchd_plist(cmd: List[str]) -> str:
    """The LaunchAgent XML, built with plistlib (pure - no filesystem access)."""
    log = str(Path.home() / "Library" / "Logs" / "longrebuttal.log")
    pl = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": cmd,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},  # relaunch on failure (Restart=on-failure)
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }
    return plistlib.dumps(pl, fmt=plistlib.FMT_XML).decode()


def _mac_install(port: int) -> Dict[str, Any]:
    path = _plist_path()
    manual = f"launchctl load -w {path}"
    lc = _launchctl()
    if not lc:
        return {"installed": False, "error": "launchctl not found on PATH.", "command": manual}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_launchd_plist(_launch_cmd(port)), encoding="utf-8")
    except OSError as exc:
        return {"installed": False, "error": f"Could not write {path}: {exc}", "command": manual}
    _run([lc, "unload", "-w", str(path)])  # clear a stale load; ignore failure
    res = _run([lc, "load", "-w", str(path)])
    if res.returncode != 0:
        return {"installed": False,
                "error": (res.stderr or res.stdout or "launchctl load failed").strip(),
                "command": manual}
    return {"installed": True,
            "message": f"Wrote {path} and loaded {LAUNCHD_LABEL} (runs at login).",
            "command": manual}


def _mac_uninstall() -> Dict[str, Any]:
    path = _plist_path()
    lc = _launchctl()
    if lc:
        _run([lc, "unload", "-w", str(path)])  # ignore failure
    try:
        path.unlink()
    except OSError:
        pass
    return {"installed": False, "message": f"Removed {path} and unloaded {LAUNCHD_LABEL}."}


def _mac_status() -> Dict[str, Any]:
    path = _plist_path()
    if not path.exists():
        return {"installed": False, "detail": f"LaunchAgent {LAUNCHD_LABEL} absent."}
    lc = _launchctl()
    if not lc:
        return {"installed": False,
                "detail": "launchctl not found; plist present but load state unknown."}
    res = _run([lc, "list"])
    if LAUNCHD_LABEL in (res.stdout or ""):
        return {"installed": True, "detail": f"LaunchAgent {LAUNCHD_LABEL} loaded."}
    return {"installed": False,
            "detail": f"Plist present but {LAUNCHD_LABEL} is not loaded."}


# ---------------------------------------------------------------- dispatch


def install(port: int = DEFAULT_PORT) -> Dict[str, Any]:
    if sys.platform == "win32":
        return _win_install(port)
    if sys.platform == "darwin":
        return _mac_install(port)
    return _linux_install(port)


def uninstall() -> Dict[str, Any]:
    if sys.platform == "win32":
        return _win_uninstall()
    if sys.platform == "darwin":
        return _mac_uninstall()
    return _linux_uninstall()


def status() -> Dict[str, Any]:
    if sys.platform == "win32":
        return _win_status()
    if sys.platform == "darwin":
        return _mac_status()
    return _linux_status()
