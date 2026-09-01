"""v1.1.2: Windows autostart must need NO admin rights.

schtasks /SC ONLOGON requires elevation (the v1.1.1 Install button died with
"Access is denied", and running the app elevated does not help - the elevated
relaunch attaches to the already-running unelevated instance). The install now
writes the per-user HKCU ...\\CurrentVersion\\Run value instead: same
at-logon semantics, zero elevation. These tests drive the _win_* functions
directly with the registry wrappers faked, so they run on any OS.
"""

from __future__ import annotations

import bitrebuttal.service as service


class FakeReg:
    def __init__(self):
        self.values = {}

    def patch(self, monkeypatch):
        monkeypatch.setattr(service, "_run_key_set",
                            lambda cmd: self.values.__setitem__(service.RUN_VALUE, cmd))
        monkeypatch.setattr(service, "_run_key_get",
                            lambda: self.values.get(service.RUN_VALUE))
        monkeypatch.setattr(service, "_run_key_delete",
                            lambda: self.values.pop(service.RUN_VALUE, None) is not None)
        # The CI runners for Linux/macOS have no schtasks; these tests exercise
        # the Windows logic, so pretend it is present everywhere.
        monkeypatch.setattr(service.shutil, "which",
                            lambda name: "C:\\Windows\\System32\\schtasks.exe"
                            if name == "schtasks" else None)


class RecordingRun:
    """Stands in for service._run; records schtasks invocations."""

    def __init__(self, query_rc: int = 1):
        self.calls = []
        self.query_rc = query_rc

    def __call__(self, argv):
        self.calls.append(argv)
        import subprocess
        rc = self.query_rc if any("/Query" in a for a in argv) else 0
        return subprocess.CompletedProcess(argv, rc, stdout="", stderr="")


def test_install_writes_run_key_without_schtasks_create(monkeypatch):
    reg = FakeReg()
    reg.patch(monkeypatch)
    run = RecordingRun()
    monkeypatch.setattr(service, "_run", run)

    res = service._win_install(7451)

    assert res["installed"] is True
    cmd = reg.values[service.RUN_VALUE]
    assert "serve" in cmd and "--headless" in cmd
    assert cmd.startswith('"')            # exe always quoted (paths with spaces)
    # No /Create anywhere: elevation-free is the whole point.
    assert not any("/Create" in a for call in run.calls for a in call)


def test_install_cleans_up_legacy_scheduled_task(monkeypatch):
    reg = FakeReg()
    reg.patch(monkeypatch)
    run = RecordingRun()
    monkeypatch.setattr(service, "_run", run)

    service._win_install(7451)

    deletes = [c for c in run.calls if "/Delete" in c]
    assert deletes, "legacy ONLOGON task should be removed best-effort"


def test_status_reads_run_key(monkeypatch):
    reg = FakeReg()
    reg.patch(monkeypatch)
    monkeypatch.setattr(service, "_run", RecordingRun(query_rc=1))

    assert service._win_status()["installed"] is False
    reg.values[service.RUN_VALUE] = '"C:\\x\\BitRebuttal.exe" serve --headless'
    assert service._win_status()["installed"] is True


def test_status_detects_legacy_task(monkeypatch):
    reg = FakeReg()
    reg.patch(monkeypatch)
    monkeypatch.setattr(service, "_run", RecordingRun(query_rc=0))  # task exists

    res = service._win_status()
    assert res["installed"] is True
    assert "legacy" in res.get("detail", "").lower()


def test_uninstall_removes_run_key_and_legacy_task(monkeypatch):
    reg = FakeReg()
    reg.patch(monkeypatch)
    reg.values[service.RUN_VALUE] = '"C:\\x\\BitRebuttal.exe" serve --headless'
    run = RecordingRun()
    monkeypatch.setattr(service, "_run", run)

    res = service._win_uninstall()

    assert res["installed"] is False
    assert service.RUN_VALUE not in reg.values
    assert any("/Delete" in c for c in run.calls)
