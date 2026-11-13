"""Unit tests for live probes and setup wiring in `adapt_adb.doctor`.

Uses mocked subprocess/Path/env seams only — no physical USB devices, no real
serials or usernames, and never invokes `adb kill-server`.
"""

from __future__ import annotations

import builtins
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import adapt_adb.doctor as doctor_mod
from adapt_adb.doctor import (
    EXIT_DEVICE_OR_PERMISSIONS,
    EXIT_OK,
    EXIT_UNSAFE_SHARED_SERVER,
    DoctorEnv,
    Server5037State,
    redact_serial,
    run_doctor_checks,
    run_setup_workflow,
)

FAKE_CLIENT_VERSION_35 = (
    "Android Debug Bridge version 1.0.41\n"
    "Version 35.0.2-12147458\n"
    "Installed as /home/devuser/.local/share/android/platform-tools/adb\n"
)

FAKE_SERVER_VERSION_34 = (
    "Android Debug Bridge version 1.0.41\n"
    "Version 34.0.5-10900879\n"
    "Installed as /opt/old-sdk/platform-tools/adb\n"
)

FAKE_SERVER_VERSION_LEGACY = (
    "Android Debug Bridge version 1.0.39\n"
    "Revision 1:8.1.0+r23-5\n"
    "Installed as /usr/lib/android-sdk/platform-tools/adb\n"
)


def _make_win_doctor_env(tmp_path: Path, *, with_adb: bool = True) -> tuple[DoctorEnv, Path]:
    home = tmp_path / "home"
    pt_dir = tmp_path / "AppData" / "Local" / "Android" / "platform-tools"
    adb_path = pt_dir / "adb.exe"
    src_props = pt_dir / "source.properties"
    existing = {pt_dir, adb_path, src_props} if with_adb else set()
    return (
        DoctorEnv(
            host_os="windows",
            arch="amd64",
            home_dir=home,
            environ={
                "PATH": f"{pt_dir};C:\\Windows\\System32",
                "LOCALAPPDATA": str(tmp_path / "AppData" / "Local"),
                "USERNAME": "devuser",
            },
            which_adb=adb_path if with_adb else None,
            which_set=True,
            path_executables=[adb_path] if with_adb else [],
            existing_paths=existing,
            version_outputs={
                adb_path: (
                    "Android Debug Bridge version 1.0.41\n"
                    "Version 35.0.2-12147458\n"
                    f"Installed as {adb_path}\n"
                )
            }
            if with_adb
            else {},
            source_properties={src_props: "Pkg.Revision=35.0.2\n"} if with_adb else {},
            windows_pnp_devices=None,
            server_state=None,
            devices_l_output="List of devices attached\n\n",
        ),
        adb_path,
    )


def test_windows_pnp_live_probe_fails_on_code_28_and_redacts_serial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env, _ = _make_win_doctor_env(tmp_path)
    fake_instance = "USB\\VID_04E8&PID_6860\\FAKE_SERIAL_28"

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[0] == "netstat":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "powershell":
            payload = [
                {
                    "InstanceId": fake_instance,
                    "FriendlyName": "SAMSUNG_Android",
                    "Class": "Unknown",
                    "Status": "Error",
                    "Problem": 28,
                }
            ]
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(doctor_mod.subprocess, "run", fake_run)

    report = run_doctor_checks(env)
    assert report["exit_code"] == EXIT_DEVICE_OR_PERMISSIONS

    pnp = next(c for c in report["checks"] if c["id"] == "windows_pnp")
    assert pnp["status"] == "fail"
    assert pnp["exit_code"] == EXIT_DEVICE_OR_PERMISSIONS
    assert pnp["details"]["probe_available"] is True
    assert pnp["details"]["inspected_count"] == 1
    assert pnp["details"]["problem_devices"] == [
        {
            "instance_id": redact_serial(fake_instance),
            "friendly_name": "SAMSUNG_Android",
            "class_name": "Unknown",
            "status": "Error",
            "problem_code": 28,
        }
    ]
    assert "FAKE_SERIAL_28" not in json.dumps(report)


def test_windows_pnp_live_probe_pnputil_fallback_detects_code_28(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env, _ = _make_win_doctor_env(tmp_path)
    pnputil_out = (
        "Instance ID:                USB\\VID_18D1&PID_4EE7\\FAKE_SERIAL_OK\n"
        "Device Description:         Android Composite ADB Interface\n"
        "Class Name:                 AndroidDevice\n"
        "Status:                     Started\n\n"
        "Instance ID:                USB\\VID_04E8&PID_6860\\FAKE_SERIAL_BAD\n"
        "Device Description:         SAMSUNG_Android\n"
        "Class Name:                 Unknown\n"
        "Status:                     Problem\n"
        "Problem Code:               28 (0x1c)\n"
    )

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[0] == "netstat":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "powershell":
            raise FileNotFoundError("powershell missing")
        if cmd[0] == "pnputil":
            return subprocess.CompletedProcess(cmd, 0, stdout=pnputil_out, stderr="")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(doctor_mod.subprocess, "run", fake_run)

    report = run_doctor_checks(env)
    assert report["exit_code"] == EXIT_DEVICE_OR_PERMISSIONS

    pnp = next(c for c in report["checks"] if c["id"] == "windows_pnp")
    assert pnp["status"] == "fail"
    assert pnp["details"]["inspected_count"] == 2
    assert len(pnp["details"]["problem_devices"]) == 1
    assert pnp["details"]["problem_devices"][0]["problem_code"] == 28
    assert pnp["details"]["problem_devices"][0]["friendly_name"] == "SAMSUNG_Android"


def test_windows_pnp_warns_when_probe_unavailable_and_passes_when_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_missing, _ = _make_win_doctor_env(tmp_path)

    def fake_run_missing(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[0] == "netstat":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "powershell":
            raise FileNotFoundError("powershell unavailable")
        if cmd[0] == "pnputil":
            raise subprocess.TimeoutExpired(cmd, 6)
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(doctor_mod.subprocess, "run", fake_run_missing)
    report_missing = run_doctor_checks(env_missing)
    pnp_missing = next(c for c in report_missing["checks"] if c["id"] == "windows_pnp")
    assert pnp_missing["status"] == "warn"
    assert pnp_missing["exit_code"] == EXIT_OK
    assert pnp_missing["details"]["probe_available"] is False
    assert "reason" in pnp_missing["details"] and pnp_missing["details"]["reason"]

    env_healthy, _ = _make_win_doctor_env(tmp_path)

    def fake_run_healthy(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[0] == "netstat":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "powershell":
            payload = [
                {
                    "InstanceId": "USB\\VID_18D1&PID_4EE7\\FAKE_SERIAL_HEALTHY",
                    "FriendlyName": "Android Composite ADB Interface",
                    "Class": "AndroidUsbDeviceClass",
                    "Status": "OK",
                    "Problem": "CM_PROB_NONE",
                }
            ]
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(doctor_mod.subprocess, "run", fake_run_healthy)
    report_healthy = run_doctor_checks(env_healthy)
    pnp_healthy = next(c for c in report_healthy["checks"] if c["id"] == "windows_pnp")
    assert pnp_healthy["status"] == "pass"
    assert pnp_healthy["exit_code"] == EXIT_OK
    assert pnp_healthy["details"]["probe_available"] is True
    assert pnp_healthy["details"]["inspected_count"] == 1
    assert pnp_healthy["details"]["problem_devices"] == []


def test_live_env_none_windows_5037_version_mismatch_exits_3_and_skips_devices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pt_dir = tmp_path / "AppData" / "Local" / "Android" / "platform-tools"
    pt_dir.mkdir(parents=True)
    client_adb = pt_dir / "adb.exe"
    client_adb.write_bytes(b"fake")
    (pt_dir / "source.properties").write_text("Pkg.Revision=35.0.2\n", encoding="utf-8")

    server_adb = tmp_path / "Android" / "Sdk" / "platform-tools" / "adb.exe"
    server_adb.parent.mkdir(parents=True)
    server_adb.write_bytes(b"fake-old")

    monkeypatch.setattr(doctor_mod.sys, "platform", "win32")
    monkeypatch.setattr(doctor_mod.platform, "system", lambda: "Windows")
    monkeypatch.setattr(doctor_mod.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(doctor_mod.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("PATH", str(pt_dir))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    monkeypatch.setenv("USERNAME", "devuser")
    monkeypatch.delenv("ANDROID_HOME", raising=False)
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name, path=None: str(client_adb) if "adb" in name.lower() else None,
    )

    seen_cmds: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen_cmds.append(list(cmd))
        assert "kill-server" not in cmd
        assert "devices" not in cmd, "adb devices -l must not run when 5037 fails"
        if cmd[0] == "netstat":
            out = "  TCP    127.0.0.1:5037         0.0.0.0:0              LISTENING       4321\n"
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
        if cmd[0] == "powershell" and "Win32_Process" in cmd[-1]:
            info = {
                "ExecutablePath": str(server_adb),
                "Name": "adb.exe",
                "Domain": "WORKGROUP",
                "User": "devuser",
                "ReturnValue": 0,
            }
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(info), stderr="")
        if cmd[0] == "powershell" and "Get-PnpDevice" in cmd[-1]:
            return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
        if cmd == [str(client_adb), "version"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=FAKE_CLIENT_VERSION_35, stderr="")
        if cmd == [str(server_adb), "version"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=FAKE_SERVER_VERSION_34, stderr="")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(doctor_mod.subprocess, "run", fake_run)

    report = run_doctor_checks(None)
    assert report["exit_code"] == EXIT_UNSAFE_SHARED_SERVER
    assert report["checks"][0]["id"] == "adb_server_5037"
    srv = report["checks"][0]
    assert srv["status"] == "fail"
    assert srv["exit_code"] == EXIT_UNSAFE_SHARED_SERVER
    assert srv["details"]["version_mismatch"] is False
    assert srv["details"]["server_exe"] == str(server_adb)
    assert srv["details"]["server_version"] is None
    assert [str(server_adb), "version"] not in seen_cmds

    assert report["checks"][-1]["id"] == "devices_state"
    assert report["checks"][-1]["status"] == "skip"
    assert not any("devices" in c for c in seen_cmds)


@pytest.mark.parametrize(
    ("domain", "user", "ret_val"),
    [
        ("NT AUTHORITY", "SYSTEM", 0),
        ("WORKGROUP", "Administrator", 0),
        ("WORKGROUP", "otheruser", 0),
        ("", "", 2),
    ],
)
def test_live_windows_5037_foreign_owner_exits_3_and_skips_devices(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    domain: str,
    user: str,
    ret_val: int,
) -> None:
    env, client_adb = _make_win_doctor_env(tmp_path)
    monkeypatch.setattr(doctor_mod, "_is_windows_admin", lambda: False)

    seen_cmds: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen_cmds.append(list(cmd))
        assert "kill-server" not in cmd
        assert "devices" not in cmd
        if cmd[0] == "netstat":
            out = "  TCP    127.0.0.1:5037         0.0.0.0:0              LISTENING       7777\n"
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
        if cmd[0] == "powershell" and "Win32_Process" in cmd[-1]:
            info = {
                "ExecutablePath": str(client_adb) if ret_val == 0 else None,
                "Name": "adb.exe",
                "Domain": domain,
                "User": user,
                "ReturnValue": ret_val,
            }
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(info), stderr="")
        if cmd[0] == "powershell" and "Get-PnpDevice" in cmd[-1]:
            return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(doctor_mod.subprocess, "run", fake_run)

    report = run_doctor_checks(env)
    assert report["exit_code"] == EXIT_UNSAFE_SHARED_SERVER
    srv = report["checks"][0]
    assert srv["id"] == "adb_server_5037"
    assert srv["status"] == "fail"
    assert srv["exit_code"] == EXIT_UNSAFE_SHARED_SERVER
    assert srv["details"]["owner_matches_current_user"] is False

    devices_chk = report["checks"][-1]
    assert devices_chk["id"] == "devices_state"
    assert devices_chk["status"] == "skip"
    assert not any("devices" in c for c in seen_cmds)


def test_live_linux_5037_version_mismatch_exits_3_and_skips_devices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_proc = tmp_path / "proc"
    (fake_proc / "net").mkdir(parents=True)
    (fake_proc / "5555" / "fd").mkdir(parents=True)
    (fake_proc / "5555" / "fd" / "3").write_text("", encoding="utf-8")
    (fake_proc / "5555" / "status").write_text(
        "Name:\tadb\nUid:\t1000\t1000\t1000\t1000\n", encoding="utf-8"
    )
    (fake_proc / "net" / "tcp").write_text(
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
        "   0: 0100007F:13AD 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 98765 1 00000000 100 0 0 10 0\n",
        encoding="utf-8",
    )

    home = tmp_path / "home"
    client_adb = home / ".local" / "share" / "android" / "platform-tools" / "adb"
    server_adb = tmp_path / "usr" / "lib" / "android-sdk" / "platform-tools" / "adb"

    monkeypatch.setattr(doctor_mod, "_PROC_ROOT", fake_proc)
    monkeypatch.setattr(doctor_mod, "_TMP_5037_PATH", tmp_path / "tmp_5037_absent")
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)

    orig_readlink = os.readlink

    def fake_readlink(path: Any) -> str:
        p_str = str(path)
        if p_str.endswith(os.path.join("5555", "fd", "3")):
            return "socket:[98765]"
        if p_str.endswith(os.path.join("5555", "exe")):
            return str(server_adb)
        return orig_readlink(path)

    monkeypatch.setattr(os, "readlink", fake_readlink)

    seen_cmds: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen_cmds.append(list(cmd))
        assert "kill-server" not in cmd
        assert "devices" not in cmd
        if cmd == [str(client_adb), "version"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=FAKE_CLIENT_VERSION_35, stderr="")
        if cmd == [str(server_adb), "version"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=FAKE_SERVER_VERSION_LEGACY, stderr="")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(doctor_mod.subprocess, "run", fake_run)

    env = DoctorEnv(
        host_os="debian",
        arch="x86_64",
        home_dir=home,
        environ={"PATH": f"{client_adb.parent}:/usr/bin"},
        which_adb=client_adb,
        which_set=True,
        path_executables=[client_adb],
        existing_paths={client_adb.parent, client_adb, client_adb.parent / "source.properties"},
        dpkg_owners={client_adb: None},
        version_outputs=None,
        source_properties={client_adb.parent / "source.properties": "Pkg.Revision=35.0.2\n"},
        server_state=None,
        devices_l_output="List of devices attached\n\n",
    )

    report = run_doctor_checks(env)
    assert report["exit_code"] == EXIT_UNSAFE_SHARED_SERVER
    srv = report["checks"][0]
    assert srv["id"] == "adb_server_5037"
    assert srv["status"] == "fail"
    assert srv["details"]["pid"] == 5555
    assert srv["details"]["server_exe"] == str(server_adb)
    assert srv["details"]["version_mismatch"] is False
    assert srv["details"]["server_version"] is None
    assert [str(server_adb), "version"] not in seen_cmds

    assert report["checks"][-1]["id"] == "devices_state"
    assert report["checks"][-1]["status"] == "skip"
    assert not any("devices" in c for c in seen_cmds)


def test_live_linux_5037_root_owner_exits_3_and_skips_devices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_proc = tmp_path / "proc"
    (fake_proc / "net").mkdir(parents=True)
    (fake_proc / "net" / "tcp").write_text(
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
        "   0: 0100007F:13AD 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 11111 1 00000000 100 0 0 10 0\n",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    client_adb = home / ".local" / "share" / "android" / "platform-tools" / "adb"

    monkeypatch.setattr(doctor_mod, "_PROC_ROOT", fake_proc)
    monkeypatch.setattr(doctor_mod, "_TMP_5037_PATH", tmp_path / "tmp_5037_absent")
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(
        doctor_mod.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a[0], 1, stdout="", stderr=""),
    )

    env = DoctorEnv(
        host_os="debian",
        arch="x86_64",
        home_dir=home,
        environ={"PATH": f"{client_adb.parent}:/usr/bin"},
        which_adb=client_adb,
        which_set=True,
        path_executables=[client_adb],
        existing_paths={client_adb.parent, client_adb, client_adb.parent / "source.properties"},
        version_outputs={client_adb: FAKE_CLIENT_VERSION_35},
        source_properties={client_adb.parent / "source.properties": "Pkg.Revision=35.0.2\n"},
        server_state=None,
        devices_l_output="List of devices attached\n\n",
    )

    report = run_doctor_checks(env)
    assert report["exit_code"] == EXIT_UNSAFE_SHARED_SERVER
    srv = report["checks"][0]
    assert srv["status"] == "fail"
    assert srv["details"]["owner_uid"] == 0
    assert srv["details"]["owner_matches_current_user"] is False
    assert report["checks"][-1]["id"] == "devices_state"
    assert report["checks"][-1]["status"] == "skip"


def test_setup_forwards_expected_sha256_and_supports_legacy_2arg_downloader(
    tmp_path: Path,
) -> None:
    def _missing_env() -> DoctorEnv:
        return DoctorEnv(
            host_os="windows",
            arch="amd64",
            home_dir=tmp_path,
            environ={
                "PATH": "C:\\Windows\\System32",
                "LOCALAPPDATA": str(tmp_path / "Local"),
            },
            which_adb=None,
            which_set=True,
            path_executables=[],
            existing_paths=set(),
            version_outputs={},
            source_properties={},
            windows_pnp_devices=[],
                server_state=Server5037State(listening=False),
        )

    expected_digest = "a" * 64
    received: list[tuple[str, Path, str | None]] = []

    def kw_downloader(url: str, target_dir: Path, expected_sha256: str | None = None) -> str:
        received.append((url, target_dir, expected_sha256))
        return expected_sha256 or ("b" * 64)

    report = run_setup_workflow(
        _missing_env(),
        yes=True,
        expected_sha256=expected_digest,
        downloader=kw_downloader,
    )
    assert report["setup"]["action"] == "installed"
    assert report["setup"]["sha256"] == expected_digest
    assert len(received) == 1
    assert received[0][2] == expected_digest

    # Legacy 2-arg downloader continues working when expected_sha256 is None
    legacy_calls: list[tuple[str, Path]] = []
    report_legacy = run_setup_workflow(
        _missing_env(),
        yes=True,
        downloader=lambda url, dest: legacy_calls.append((url, dest)),
    )
    assert report_legacy["setup"]["action"] == "installed"
    assert len(legacy_calls) == 1

    # Passing expected_sha256 to a 2-arg downloader that cannot verify it must fail closed
    with pytest.raises(TypeError, match="expected_sha256"):
        run_setup_workflow(
            _missing_env(),
            yes=True,
            expected_sha256=expected_digest,
            downloader=lambda url, dest: None,
        )


def _make_missing_adb_env(tmp_path: Path) -> DoctorEnv:
    return DoctorEnv(
        host_os="windows",
        arch="amd64",
        home_dir=tmp_path,
        environ={
            "PATH": "C:\\Windows\\System32",
            "LOCALAPPDATA": str(tmp_path / "Local"),
        },
        which_adb=None,
        which_set=True,
        path_executables=[],
        existing_paths=set(),
        version_outputs={},
        source_properties={},
        windows_pnp_devices=[],
        server_state=Server5037State(listening=False),
    )


def test_setup_tty_declined_aborts_without_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _make_missing_adb_env(tmp_path)
    original_path = env.environ["PATH"]
    downloader = MagicMock(return_value="a" * 64)
    input_mock = MagicMock(return_value="n")

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(builtins, "input", input_mock)

    report = run_setup_workflow(env, yes=False, downloader=downloader)

    assert report["setup"]["action"] == "aborted_unconfirmed"
    assert report["setup"]["downloaded"] is False
    assert report["setup"]["path_updated"] is False
    assert report["setup"]["target_dir"] == str(tmp_path / "Local" / "Android" / "platform-tools")
    assert report["setup"]["url"].startswith("https://dl.google.com/")
    assert env.environ["PATH"] == original_path
    downloader.assert_not_called()
    input_mock.assert_called_once_with()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Google" in captured.err
    assert str(tmp_path / "Local" / "Android" / "platform-tools") in captured.err


def test_setup_tty_confirmed_yes_invokes_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _make_missing_adb_env(tmp_path)
    downloader = MagicMock(return_value="a" * 64)
    input_mock = MagicMock(return_value="yes")

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(builtins, "input", input_mock)

    report = run_setup_workflow(env, yes=False, downloader=downloader)

    assert report["setup"]["action"] == "installed"
    assert report["setup"]["downloaded"] is True
    downloader.assert_called_once()
    input_mock.assert_called_once_with()


def test_setup_tty_with_yes_flag_skips_prompt_and_downloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _make_missing_adb_env(tmp_path)
    downloader = MagicMock(return_value="a" * 64)
    input_mock = MagicMock(side_effect=AssertionError("input() must not be called"))

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setattr(builtins, "input", input_mock)

    report = run_setup_workflow(env, yes=True, downloader=downloader)

    assert report["setup"]["action"] == "installed"
    assert report["setup"]["downloaded"] is True
    downloader.assert_called_once()
    input_mock.assert_not_called()


@pytest.mark.parametrize("stdin_tty,stderr_tty", [(False, False), (False, True), (True, False)])
def test_setup_non_tty_without_yes_aborts_without_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdin_tty: bool, stderr_tty: bool
) -> None:
    env = _make_missing_adb_env(tmp_path)
    original_path = env.environ["PATH"]
    original_paths = set(env.existing_paths)
    downloader = MagicMock(return_value="a" * 64)
    input_mock = MagicMock(side_effect=AssertionError("input() must not be called"))

    monkeypatch.setattr(sys.stdin, "isatty", lambda: stdin_tty)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: stderr_tty)
    monkeypatch.setattr(builtins, "input", input_mock)

    report = run_setup_workflow(env, yes=False, downloader=downloader)

    assert report["setup"]["action"] == "aborted_needs_yes"
    assert report["setup"]["downloaded"] is False
    downloader.assert_not_called()
    assert report["setup"]["path_updated"] is False
    input_mock.assert_not_called()
    assert env.environ["PATH"] == original_path
    assert env.existing_paths == original_paths


def test_setup_live_env_none_updates_os_environ_path_and_post_doctor_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty_bin = tmp_path / "empty_bin"
    empty_bin.mkdir()
    local_app = tmp_path / "AppData" / "Local"
    expected_target = local_app / "Android" / "platform-tools"
    binary_name = "adb.exe" if sys.platform == "win32" else "adb"
    expected_adb = (
        expected_target / binary_name
        if sys.platform == "win32"
        else tmp_path / ".local" / "share" / "android" / "platform-tools" / binary_name
    )

    monkeypatch.setattr(doctor_mod.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(local_app))
    monkeypatch.setenv("PATH", str(empty_bin))
    monkeypatch.delenv("ANDROID_HOME", raising=False)
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)

    monkeypatch.setattr(doctor_mod, "add_to_windows_user_path", lambda target: True)
    monkeypatch.setattr(doctor_mod, "ensure_debian_user_path", lambda target, home, env=None: True)
    monkeypatch.setattr(
        doctor_mod,
        "_probe_server_5037",
        lambda *args, **kwargs: doctor_mod.Server5037State(listening=False),
    )

    def fake_downloader(url: str, target_dir: Path, expected_sha256: str | None = None) -> str:
        target_dir.mkdir(parents=True, exist_ok=True)
        adb_file = target_dir / binary_name
        adb_file.write_bytes(b"#!/bin/sh\necho fake\n")
        if sys.platform != "win32":
            adb_file.chmod(0o755)
        (target_dir / "source.properties").write_text("Pkg.Revision=35.0.2\n", encoding="utf-8")
        return "c" * 64

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd == [str(expected_adb), "version"]:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=(
                    "Android Debug Bridge version 1.0.41\n"
                    "Version 35.0.2-12147458\n"
                    f"Installed as {expected_adb}\n"
                ),
                stderr="",
            )
        if cmd == [str(expected_adb), "devices", "-l"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="List of devices attached\n\n", stderr="")
        if cmd[0] == "powershell" and "Get-PnpDevice" in cmd[-1]:
            return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
        if cmd[0] == "dpkg":
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no path found")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(doctor_mod.subprocess, "run", fake_run)

    report = run_setup_workflow(None, yes=True, downloader=fake_downloader)

    assert report["setup"]["action"] == "installed"
    assert report["setup"]["downloaded"] is True
    assert report["setup"]["path_updated"] is True
    assert os.environ["PATH"].split(os.pathsep)[0] == str(expected_adb.parent)

    adb_path_check = next(c for c in report["checks"] if c["id"] == "adb_path")
    assert adb_path_check["status"] == "pass"
    assert Path(adb_path_check["details"]["resolved_adb"]) == expected_adb
    assert Path(report["toolchain"]["adb_path"]) == expected_adb




@pytest.mark.parametrize("endpoint", [
    {"ADB_SERVER_SOCKET": "tcp:remote.example:5038"},
    {"ANDROID_ADB_SERVER_PORT": "5038"},
    {"ADB_SERVER_PORT": "5038"},
])
def test_doctor_nondefault_endpoint_skips_devices(tmp_path, endpoint):
    env, _ = _make_win_doctor_env(tmp_path)
    env.environ.update(endpoint)
    env.server_state = Server5037State(listening=False)
    env.windows_pnp_devices = []
    report = run_doctor_checks(env)
    assert report["exit_code"] == EXIT_UNSAFE_SHARED_SERVER
    assert report["checks"][0]["status"] == "fail"
    assert "non-default" in report["checks"][-1]["summary"]
    assert not env.devices_l_invoked


@pytest.mark.parametrize(("domain", "user", "current", "matches", "system"), [
    ("HOST", "alice", "HOST\\alice", True, False),
    ("host", "ALICE", "HOST\\alice", True, False),
    ("OTHERDOMAIN", "alice", "HOST\\alice", False, False),
    ("", "alice", "HOST\\alice", False, False),
    ("HOST", "alice", "", False, False),
    ("HOST", "Administrator", "HOST\\alice", False, True),
    ("NT AUTHORITY", "SYSTEM", "HOST\\alice", False, True),
    ("HOST", "Administrator", "HOST\\Administrator", True, False),
    ("OTHERHOST", "Administrator", "HOST\\Administrator", False, True),
])
def test_windows_owner_requires_exact_account(monkeypatch, domain, user, current, matches, system):
    monkeypatch.setattr(doctor_mod, "_is_windows_admin", lambda: True)
    state = Server5037State()
    doctor_mod._apply_windows_owner(state, domain, user, current)
    assert state.owner_matches_current_user is matches
    assert state.owner_is_root_or_system is system


@pytest.mark.parametrize("local,remote", [
    ("127.0.0.1:5037", "0.0.0.0:0"),
    ("[::1]:5037", "[::]:0"),
    ("[::]:5037", "*:*"),
])
def test_windows_localized_listener(tmp_path, monkeypatch, local, remote):
    env, adb = _make_win_doctor_env(tmp_path)
    env.windows_current_account = "HOST\\alice"
    def fake_run(cmd, **kwargs):
        assert cmd == ["netstat", "-ano"]
        assert kwargs["errors"] == "replace"
        return subprocess.CompletedProcess(cmd, 0, stdout=f"TCP {local} {remote} ABH\u00d6REN 42\n")
    monkeypatch.setattr(doctor_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(doctor_mod, "_probe_windows_pid_info", lambda *a: (adb, "HOST", "alice", 0, True))
    state = doctor_mod._probe_server_5037("windows", tmp_path, env, trusted_adb=adb)
    assert state.listening and state.pid == 42
    assert state.owner_matches_current_user
    assert state.server_version_raw


@pytest.mark.parametrize("row", [
    "TCP 127.0.0.1:5037 malformed",
    "TCP [::1]:5037 [::]:0 ABH\u00d6REN unknown",
    "TCP [::1]:5037 unknown ABH\u00d6REN 42",
])
def test_windows_unclassifiable_listener_fails_closed(tmp_path, monkeypatch, row):
    monkeypatch.setattr(doctor_mod.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=row))
    state = doctor_mod._probe_server_5037("windows", tmp_path, DoctorEnv(environ={}))
    assert state.probe_error


def test_linux_deleted_executable_is_unverified(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    (proc / "net").mkdir(parents=True)
    (proc / "net" / "tcp").write_text(
        "header\n0: 0100007F:13AD 00000000:0000 0A 0 0 0 1000 0 123\n", encoding="utf-8",
    )
    monkeypatch.setattr(doctor_mod, "_PROC_ROOT", proc)
    monkeypatch.setattr(doctor_mod, "_TMP_5037_PATH", tmp_path / "absent")
    monkeypatch.setattr(doctor_mod, "_find_linux_pid_for_inode", lambda *a: 42)
    monkeypatch.setattr(doctor_mod, "_read_linux_proc_uid", lambda *a: 1000)
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    adb = tmp_path / "adb"
    monkeypatch.setattr(os, "readlink", lambda *a: str(adb) + " (deleted)")
    query = MagicMock(side_effect=AssertionError("Deleted daemon must not certify on-disk binary"))
    monkeypatch.setattr(doctor_mod, "_query_adb_version_raw", query)
    state = doctor_mod._probe_server_5037("linux", tmp_path, DoctorEnv(environ={}), trusted_adb=adb)
    assert state.listening
    assert state.server_exe is None
    assert state.server_version_raw is None
    with pytest.raises(doctor_mod.UnsafeAdbServerError, match="ownership/version is unverified"):
        doctor_mod.preflight_adb_server(adb, ["devices"], environ={}, state=state)
    query.assert_not_called()
