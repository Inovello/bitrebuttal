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

TASK_NAME = "BitRebuttal"
UNIT_NAME = "bitrebuttal.service"
LAUNCHD_LABEL = "com.bitrebuttal.serve"
DEFAULT_PORT = 7451

UNIT_TEMPLATE = """\
[Unit]
Description=Bit Rebuttal - resilient download supervisor
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
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "serve"]
    else:
        cmd = [_python(), "-m", "bitrebuttal", "serve"]
    if headless:
        cmd.append("--headless")
    if port and port != DEFAULT_PORT:
        cmd += ["--port", str(port)]
    return cmd


def _run(argv: List[str]) -> subprocess.CompletedProcess:
    kwargs = {}
    if sys.platform == "win32":
        # No console flash when called from the windowed shell (schtasks etc.).
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(argv, capture_output=True, text=True, shell=False, **kwargs)


# ---------------------------------------------------------------- Windows
#
# Autostart lives in the per-user HKCU ...\CurrentVersion\Run value: the same
# at-logon semantics as a Task Scheduler ONLOGON task, but writable with NO
# elevation. schtasks /SC ONLOGON needs admin (v1.1.1's Install button died
# with "Access is denied" - and running the app elevated does not help, since
# the elevated relaunch just attaches a window to the already-running
# unelevated instance). Pre-1.1.2 installs that did create the scheduled task
# are still detected and cleaned up.

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = TASK_NAME


def _run_key_set(cmd: str) -> None:
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, cmd)


def _run_key_get() -> Optional[str]:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, kind = winreg.QueryValueEx(key, RUN_VALUE)
            return str(value) if kind == winreg.REG_SZ else None
    except OSError:
        return None


def _run_key_delete() -> bool:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, RUN_VALUE)
            return True
    except OSError:
        return False


def _autostart_cmd(port: int) -> str:
    exe, *rest = _launch_cmd(port)
    return " ".join([f'"{exe}"', *rest])


def _delete_legacy_task() -> None:
    """Best-effort removal of the pre-1.1.2 scheduled task (needed admin)."""
    exe = shutil.which("schtasks")
    if exe:
        _run([exe, "/Delete", "/TN", TASK_NAME, "/F"])


def _legacy_task_present() -> bool:
    exe = shutil.which("schtasks")
    if not exe:
        return False
    return _run([exe, "/Query", "/TN", TASK_NAME]).returncode == 0


def _win_install(port: int) -> Dict[str, Any]:
    cmd = _autostart_cmd(port)
    manual = (f'reg add "HKCU\\{RUN_KEY}" /v {RUN_VALUE} /t REG_SZ '
              f'/d "{cmd.replace(chr(34), chr(92) + chr(34))}" /f')
    try:
        _run_key_set(cmd)
    except OSError as exc:
        return {"installed": False,
                "error": f"Could not write the autostart registry value: {exc}",
                "command": manual}
    if _run_key_get() != cmd:
        return {"installed": False,
                "error": "Autostart value did not read back; write it manually:",
                "command": manual}
    _delete_legacy_task()          # a leftover ONLOGON task would double-start
    return {"installed": True,
            "message": "Autostart entry written (starts at logon, no admin needed)."}


def _win_uninstall() -> Dict[str, Any]:
    _run_key_delete()
    _delete_legacy_task()
    return {"installed": False, "message": "Autostart entry removed."}


def _win_status() -> Dict[str, Any]:
    value = _run_key_get()
    if value:
        return {"installed": True, "detail": f"HKCU Run entry: {value}"}
    if _legacy_task_present():
        return {"installed": True,
                "detail": f"legacy scheduled task '{TASK_NAME}' (pre-1.1.2); "
                          "Remove + Install migrates it to the admin-free entry."}
    return {"installed": False, "detail": "No autostart entry."}


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
    log = str(Path.home() / "Library" / "Logs" / "bitrebuttal.log")
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
