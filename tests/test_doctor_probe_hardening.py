"""Regression tests for hardened `adapt doctor` host probes.

Covers the Linux port-5037 listener scan, the Windows PnP Android/other split and
the Debian udev rule matching. Offline only: `/proc`, udev directories, `grp` and
`subprocess.run` are faked; no adb, PowerShell, pnputil or device is touched.
"""

from __future__ import annotations

import os
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

import adapt_adb.doctor as doctor_mod
from adapt_adb.doctor import (
    EXIT_DEVICE_OR_PERMISSIONS,
    EXIT_OK,
    EXIT_UNSAFE_SHARED_SERVER,
    DebianUsbState,
    DoctorEnv,
    PnpDeviceRecord,
    Server5037State,
    run_doctor_checks,
)

FAKE_VERSION_35 = (
    "Android Debug Bridge version 1.0.41\n"
    "Version 35.0.2-12147458\n"
    "Installed as /home/dev/.local/share/android/platform-tools/adb\n"
)

MULTIPLE_OWNERS = "Multiple local TCP 5037 listener owners."
TCP_HEADER = (
    "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
)
V4_LOOPBACK = "0100007F"
V6_ANY = "00000000000000000000000000000000"


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _check(report: dict[str, Any], check_id: str) -> dict[str, Any]:
    return next(c for c in report["checks"] if c["id"] == check_id)


def _debian_env(tmp_path: Path, **overrides: Any) -> DoctorEnv:
    home = tmp_path / "home" / "dev"
    pt_dir = home / ".local" / "share" / "android" / "platform-tools"
    adb_bin = pt_dir / "adb"
    values: dict[str, Any] = dict(
        host_os="debian",
        arch="x86_64",
        uid_is_root=False,
        home_dir=home,
        environ={"PATH": f"{pt_dir}:/usr/bin"},
        which_adb=adb_bin,
        which_set=True,
        path_executables=[adb_bin],
        existing_paths={adb_bin, pt_dir},
        dpkg_owners={adb_bin: None},
        source_properties={pt_dir / "source.properties": "Pkg.Revision=35.0.2\n"},
        version_outputs={adb_bin: FAKE_VERSION_35},
        server_state=Server5037State(listening=False),
        debian_usb_state=DebianUsbState(),
        devices_l_output="List of devices attached\n\n",
    )
    values.update(overrides)
    return DoctorEnv(**values)


def _windows_env(tmp_path: Path, devices: list[PnpDeviceRecord]) -> DoctorEnv:
    home = tmp_path / "Users" / "dev"
    local_app = home / "AppData" / "Local"
    pt_dir = local_app / "Android" / "platform-tools"
    adb_exe = pt_dir / "adb.exe"
    return DoctorEnv(
        host_os="windows",
        arch="AMD64",
        uid_is_root=False,
        home_dir=home,
        environ={"LOCALAPPDATA": str(local_app), "PATH": str(pt_dir)},
        which_adb=adb_exe,
        which_set=True,
        path_executables=[adb_exe],
        existing_paths={adb_exe, pt_dir},
        source_properties={pt_dir / "source.properties": "Pkg.Revision=35.0.2\n"},
        version_outputs={adb_exe: FAKE_VERSION_35},
        server_state=Server5037State(listening=False),
        windows_pnp_devices=devices,
        devices_l_output="List of devices attached\n\n",
    )


# ---------------------------------------------------------------------------
# DOC-01: Linux port-5037 listener scan
# ---------------------------------------------------------------------------


def _tcp_row(index: int, addr: str, uid: int, inode: str) -> str:
    return (
        f"   {index}: {addr}:13AD 00000000:0000 0A 00000000:00000000 00:00000000 00000000 "
        f"{uid:5d}        0 {inode} 1 0000000000000000 100 0 0 10 0\n"
    )


def _fake_proc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    tcp: list[str],
    tcp6: list[str],
    pid_inodes: dict[int, list[str]] | None = None,
    exe: dict[int, str] | None = None,
) -> Path:
    proc = tmp_path / "proc"
    (proc / "net").mkdir(parents=True)
    (proc / "net" / "tcp").write_text(TCP_HEADER + "".join(tcp), encoding="utf-8")
    (proc / "net" / "tcp6").write_text(TCP_HEADER + "".join(tcp6), encoding="utf-8")
    links: dict[str, str] = {}
    for pid, inodes in (pid_inodes or {}).items():
        fd_dir = proc / str(pid) / "fd"
        fd_dir.mkdir(parents=True)
        for fd, inode in enumerate(inodes, start=3):
            (fd_dir / str(fd)).write_text("", encoding="utf-8")
            links[str(fd_dir / str(fd))] = f"socket:[{inode}]"
    for pid, target in (exe or {}).items():
        links[str(proc / str(pid) / "exe")] = target

    def fake_readlink(path: Any) -> str:
        key = str(path)
        if key in links:
            return links[key]
        raise OSError("not a link")

    monkeypatch.setattr(os, "readlink", fake_readlink)
    monkeypatch.setattr(doctor_mod, "_PROC_ROOT", proc)
    monkeypatch.setattr(doctor_mod, "_TMP_5037_PATH", tmp_path / "tmp_5037_absent")
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    return proc


def _no_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(doctor_mod.subprocess, "run", fake_run)


def test_linux_root_listener_only_in_tcp6_beside_own_tcp_listener_exits_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_proc(
        tmp_path,
        monkeypatch,
        tcp=[_tcp_row(0, V4_LOOPBACK, 1000, "111")],
        tcp6=[_tcp_row(0, V6_ANY, 0, "222")],
        pid_inodes={500: ["111"]},
    )
    _no_subprocess(monkeypatch)

    state = doctor_mod._probe_server_5037("linux", tmp_path, DoctorEnv(environ={}))
    assert state.listening
    assert state.owner_is_root_or_system is True
    assert state.owner_matches_current_user is False
    assert state.owner_uid == 0
    assert state.probe_error == MULTIPLE_OWNERS
    assert state.server_version_raw is None

    report = run_doctor_checks(_debian_env(tmp_path, server_state=None))
    srv = _check(report, "adb_server_5037")
    assert srv["status"] == "fail"
    assert srv["exit_code"] == EXIT_UNSAFE_SHARED_SERVER
    assert srv["details"]["owner_uid"] == 0
    assert report["exit_code"] == EXIT_UNSAFE_SHARED_SERVER
    assert _check(report, "devices_state")["status"] == "skip"


def test_linux_two_tcp_listeners_with_different_users_are_multiple_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_proc(
        tmp_path,
        monkeypatch,
        tcp=[_tcp_row(0, V4_LOOPBACK, 1000, "111"), _tcp_row(1, "00000000", 1001, "333")],
        tcp6=[],
        pid_inodes={500: ["111"]},
    )
    _no_subprocess(monkeypatch)

    state = doctor_mod._probe_server_5037("linux", tmp_path, DoctorEnv(environ={}))
    assert state.probe_error == MULTIPLE_OWNERS
    assert state.owner_uid == 1001
    assert state.owner_matches_current_user is False
    assert state.owner_is_root_or_system is False


def test_linux_same_user_listeners_in_two_processes_are_multiple_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_proc(
        tmp_path,
        monkeypatch,
        tcp=[_tcp_row(0, V4_LOOPBACK, 1000, "111"), _tcp_row(1, "00000000", 1000, "444")],
        tcp6=[],
        pid_inodes={500: ["111"], 600: ["444"]},
    )
    _no_subprocess(monkeypatch)

    state = doctor_mod._probe_server_5037("linux", tmp_path, DoctorEnv(environ={}))
    assert state.probe_error == MULTIPLE_OWNERS
    assert state.owner_matches_current_user is True
    assert state.server_version_raw is None


def test_linux_dual_stack_listener_of_one_process_is_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adb = tmp_path / "home" / "dev" / ".local" / "share" / "android" / "platform-tools" / "adb"
    _fake_proc(
        tmp_path,
        monkeypatch,
        tcp=[_tcp_row(0, V4_LOOPBACK, 1000, "111")],
        tcp6=[_tcp_row(0, V6_ANY, 1000, "112")],
        pid_inodes={500: ["111", "112"]},
        exe={500: str(adb)},
    )
    _no_subprocess(monkeypatch)
    monkeypatch.setattr(doctor_mod, "_query_adb_version_raw", lambda *a, **kw: FAKE_VERSION_35)

    state = doctor_mod._probe_server_5037("linux", tmp_path, DoctorEnv(environ={}), trusted_adb=adb)
    assert state.probe_error is None
    assert state.listening is True
    assert state.pid == 500
    assert state.owner_uid == 1000
    assert state.owner_matches_current_user is True
    assert state.server_exe == adb
    assert state.server_version_raw == FAKE_VERSION_35


def test_linux_foreign_listener_only_in_tcp6_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_proc(tmp_path, monkeypatch, tcp=[], tcp6=[_tcp_row(0, V6_ANY, 1001, "555")])
    monkeypatch.setattr(
        doctor_mod.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )

    state = doctor_mod._probe_server_5037("linux", tmp_path, DoctorEnv(environ={}))
    assert state.listening is True
    assert state.owner_uid == 1001
    assert state.owner_matches_current_user is False


def test_linux_partly_unattributed_listeners_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_proc(
        tmp_path,
        monkeypatch,
        tcp=[_tcp_row(0, V4_LOOPBACK, 1000, "111")],
        tcp6=[_tcp_row(0, V6_ANY, 1000, "999")],
        pid_inodes={500: ["111"]},
    )
    _no_subprocess(monkeypatch)

    state = doctor_mod._probe_server_5037("linux", tmp_path, DoctorEnv(environ={}))
    assert state.probe_error is not None
    assert state.server_version_raw is None


def test_linux_ss_fallback_with_two_pids_is_multiple_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_proc(
        tmp_path,
        monkeypatch,
        tcp=[_tcp_row(0, V4_LOOPBACK, 1000, "111")],
        tcp6=[_tcp_row(0, V6_ANY, 1000, "112")],
    )
    ss_out = (
        'LISTEN 0 4096 127.0.0.1:5037 0.0.0.0:* users:(("adb",pid=700,fd=7))\n'
        'LISTEN 0 4096 [::]:5037 [::]:* users:(("other",pid=701,fd=5))\n'
    )
    monkeypatch.setattr(
        doctor_mod.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=ss_out, stderr=""),
    )

    state = doctor_mod._probe_server_5037("linux", tmp_path, DoctorEnv(environ={}))
    assert state.probe_error == MULTIPLE_OWNERS
    assert state.server_version_raw is None


def test_linux_malformed_row_does_not_hide_later_root_listener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad_row = "   0: 0100007F:13AD 00000000:0000 0A 00000000:00000000 00:00000000 00000000 notauid 0 1\n"
    _fake_proc(
        tmp_path,
        monkeypatch,
        tcp=[bad_row, _tcp_row(1, "00000000", 0, "222")],
        tcp6=[],
    )
    monkeypatch.setattr(
        doctor_mod.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr=""),
    )

    state = doctor_mod._probe_server_5037("linux", tmp_path, DoctorEnv(environ={}))
    assert state.probe_error == "Cannot inspect local TCP listeners."
    assert state.listening is True
    assert state.owner_is_root_or_system is True


# ---------------------------------------------------------------------------
# DOC-03: Windows PnP Android/other split
# ---------------------------------------------------------------------------

PIXEL_OK = PnpDeviceRecord(
    instance_id="USB\\VID_18D1&PID_4EE7\\FAKE_PIXEL",
    friendly_name="Android Composite ADB Interface",
    class_name="AndroidUsbDeviceClass",
    status="OK",
    problem_code=0,
)
WEBCAM_DISABLED = PnpDeviceRecord(
    instance_id="USB\\VID_046D&PID_0825\\FAKE_WEBCAM",
    friendly_name="USB Video Device",
    class_name="USBDevice",
    status="Error",
    problem_code=22,
)


def test_windows_pnp_disabled_webcam_beside_healthy_pixel_warns(tmp_path: Path) -> None:
    report = run_doctor_checks(_windows_env(tmp_path, [PIXEL_OK, WEBCAM_DISABLED]))
    pnp = _check(report, "windows_pnp")
    assert pnp["status"] == "warn"
    assert pnp["exit_code"] == EXIT_OK
    assert report["exit_code"] == EXIT_OK
    assert pnp["details"]["problem_devices"] == []
    assert pnp["details"]["inspected_count"] == 2
    assert pnp["details"]["probe_available"] is True
    others = pnp["details"]["other_usb_problem_devices"]
    assert len(others) == 1
    assert others[0]["problem_code"] == 22
    assert others[0]["instance_id"] == doctor_mod.redact_serial(WEBCAM_DISABLED.instance_id)
    assert "FAKE_WEBCAM" not in str(pnp)
    assert "1 non-Android USB device(s)" in pnp["summary"]
    assert "no Android" in pnp["summary"]
    assert "Device Manager" in (pnp["remediation"] or "")


def test_windows_pnp_samsung_vendor_with_generic_name_fails(tmp_path: Path) -> None:
    samsung = PnpDeviceRecord(
        instance_id="USB\\VID_04E8&PID_6860\\FAKE_SAMSUNG",
        friendly_name="USB Composite Device",
        class_name="USB",
        status="Error",
        problem_code=28,
    )
    report = run_doctor_checks(_windows_env(tmp_path, [samsung, WEBCAM_DISABLED]))
    pnp = _check(report, "windows_pnp")
    assert pnp["status"] == "fail"
    assert pnp["exit_code"] == EXIT_DEVICE_OR_PERMISSIONS
    assert [d["problem_code"] for d in pnp["details"]["problem_devices"]] == [28]
    assert [d["problem_code"] for d in pnp["details"]["other_usb_problem_devices"]] == [22]


def test_windows_pnp_unknown_vendor_adb_interface_fails(tmp_path: Path) -> None:
    adb_iface = PnpDeviceRecord(
        instance_id="USB\\VID_ABCD&PID_0001&MI_01\\FAKE_ADB",
        friendly_name="ADB Interface",
        class_name="",
        status="Error",
        problem_code=28,
    )
    report = run_doctor_checks(_windows_env(tmp_path, [adb_iface]))
    pnp = _check(report, "windows_pnp")
    assert pnp["status"] == "fail"
    assert pnp["exit_code"] == EXIT_DEVICE_OR_PERMISSIONS
    assert len(pnp["details"]["problem_devices"]) == 1


def test_windows_pnp_healthy_pass_includes_empty_other_list(tmp_path: Path) -> None:
    pnp = _check(run_doctor_checks(_windows_env(tmp_path, [PIXEL_OK])), "windows_pnp")
    assert pnp["status"] == "pass"
    assert pnp["details"]["other_usb_problem_devices"] == []


@pytest.mark.parametrize(
    ("instance_id", "friendly_name", "class_name", "expected"),
    [
        ("USB\\VID_046D&PID_0825\\X", "USB Video Device", "AndroidDevice", True),
        ("USB\\VID_046D&PID_0825\\X", "USB Video Device", "AndroidUsbDeviceClass", True),
        ("USB\\VID_ABCD&PID_0001\\X", "Pixel Phone", "USB", True),
        ("USB\\VID_2717&PID_FF48\\X", "USB Composite Device", "USB", True),
        ("USB\\VID_0e8d&PID_201C\\X", "MT65xx Preloader", "Ports", True),
        ("USB\\VID_046D&PID_0825\\X", "USB Video Device", "USBDevice", False),
        ("USB\\VID_046D&PID_0825\\X", "USB Video Device", "Camera", False),
    ],
)
def test_is_android_pnp_device(
    instance_id: str, friendly_name: str, class_name: str, expected: bool
) -> None:
    assert doctor_mod._is_android_pnp_device(instance_id, friendly_name, class_name) is expected


def test_parse_pnputil_reads_problem_line() -> None:
    out = (
        "Instance ID:                USB\\VID_04E8&PID_6860\\FAKE_SERIAL\n"
        "Device Description:         USB Composite Device\n"
        "Class Name:                 USB\n"
        "Status:                     Problem\n"
        "Problem:                    28 (0x1C)\n"
    )
    records = doctor_mod._parse_pnputil_devices(out)
    assert len(records) == 1
    assert records[0].problem_code == 28


def test_powershell_and_pnputil_paths_collect_the_same_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ps_items = (
        '[{"InstanceId":"USB\\\\VID_046D&PID_0825\\\\W","FriendlyName":"USB Video Device",'
        '"Class":"Camera","Status":"Error","Problem":22},'
        '{"InstanceId":"HID\\\\VID_1234&PID_0001\\\\M","FriendlyName":"HID Mouse",'
        '"Class":"Mouse","Status":"OK","Problem":0}]'
    )
    pnputil_out = (
        "Instance ID:                USB\\VID_046D&PID_0825\\W\n"
        "Device Description:         USB Video Device\n"
        "Class Name:                 Camera\n"
        "Status:                     Problem\n"
        "Problem Code:               22\n\n"
        "Instance ID:                HID\\VID_1234&PID_0001\\M\n"
        "Device Description:         HID Mouse\n"
        "Class Name:                 Mouse\n"
        "Status:                     Started\n"
    )

    def ps_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        assert cmd[0] == "powershell"
        return subprocess.CompletedProcess(cmd, 0, stdout=ps_items, stderr="")

    monkeypatch.setattr(doctor_mod.subprocess, "run", ps_run)
    ps_records, ps_err = doctor_mod._probe_windows_pnp()

    def pnputil_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        if cmd[0] == "powershell":
            raise FileNotFoundError("powershell missing")
        return subprocess.CompletedProcess(cmd, 0, stdout=pnputil_out, stderr="")

    monkeypatch.setattr(doctor_mod.subprocess, "run", pnputil_run)
    pnp_records, pnp_err = doctor_mod._probe_windows_pnp()

    assert ps_err is None and pnp_err is None
    assert ps_records is not None and pnp_records is not None
    assert [(r.instance_id, r.problem_code) for r in ps_records] == [("USB\\VID_046D&PID_0825\\W", 22)]
    assert [(r.instance_id, r.problem_code) for r in pnp_records] == [("USB\\VID_046D&PID_0825\\W", 22)]


# ---------------------------------------------------------------------------
# DOC-04: Debian udev rule matching and effective group access
# ---------------------------------------------------------------------------

DEBIAN_ANDROID_RULES = (
    "# Android Debug Bridge udev rules\n"
    'ACTION!="add", GOTO="android_usb_rules_end"\n'
    'ATTR{idVendor}=="18d1", ENV{adb_user}="yes"\n'
    'ENV{adb_user}=="yes", MODE="0660", GROUP="plugdev", TAG+="uaccess"\n'
    'LABEL="android_usb_rules_end"\n'
)
STOCK_LIBMTP_RULES = (
    "# libmtp rules\n"
    'ATTR{idVendor}=="04e8", ATTR{idProduct}=="6860", SYMLINK+="libmtp-%k", ENV{ID_MTP_DEVICE}="1"\n'
    'ATTR{bInterfaceClass}=="06", ENV{ID_MTP_DEVICE}="1"\n'
    'ENV{ID_MTP_DEVICE}=="1", TAG+="uaccess"\n'
)

PLUGDEV_GID = 46
ADBUSERS_GID = 120


def _fake_groups(
    monkeypatch: pytest.MonkeyPatch, *, groups: list[int], egid: int = 1000
) -> None:
    gids = {"plugdev": PLUGDEV_GID, "adbusers": ADBUSERS_GID}

    def getgrnam(name: str) -> Any:
        if name not in gids:
            raise KeyError(name)
        return types.SimpleNamespace(gr_name=name, gr_gid=gids[name])

    monkeypatch.setitem(sys.modules, "grp", types.SimpleNamespace(getgrnam=getgrnam))
    monkeypatch.setattr(os, "getgroups", lambda: list(groups), raising=False)
    monkeypatch.setattr(os, "getegid", lambda: egid, raising=False)


def _rule_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, files: dict[str, str]) -> Path:
    etc = tmp_path / "etc" / "udev" / "rules.d"
    lib = tmp_path / "lib" / "udev" / "rules.d"
    etc.mkdir(parents=True)
    lib.mkdir(parents=True)
    for rel, text in files.items():
        (tmp_path / rel).write_text(text, encoding="utf-8")
    monkeypatch.setattr(doctor_mod, "_UDEV_RULE_DIRS", (etc, lib))
    return tmp_path


def test_debian_stock_mtp_rules_are_not_android_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _rule_dirs(tmp_path, monkeypatch, {"lib/udev/rules.d/69-libmtp.rules": STOCK_LIBMTP_RULES})
    _fake_groups(monkeypatch, groups=[PLUGDEV_GID])

    state = doctor_mod._probe_debian_usb_permissions({})
    assert state.has_udev_rules is False
    assert state.has_uaccess_tag is False
    assert state.rule_files == []

    report = run_doctor_checks(_debian_env(tmp_path, debian_usb_state=None))
    deb = _check(report, "debian_usb_permissions")
    assert deb["status"] == "fail"
    assert deb["exit_code"] == EXIT_DEVICE_OR_PERMISSIONS
    assert "Missing Android USB udev rules" in deb["summary"]


def test_debian_android_rules_file_is_relevant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _rule_dirs(
        tmp_path,
        monkeypatch,
        {
            "lib/udev/rules.d/51-android.rules": DEBIAN_ANDROID_RULES,
            "lib/udev/rules.d/69-libmtp.rules": STOCK_LIBMTP_RULES,
        },
    )
    _fake_groups(monkeypatch, groups=[PLUGDEV_GID])

    state = doctor_mod._probe_debian_usb_permissions({})
    assert state.has_udev_rules is True
    assert [Path(p).name for p in state.rule_files] == ["51-android.rules"]
    assert state.has_uaccess_tag is True
    assert state.has_plugdev_rule is True
    assert state.has_adbusers_rule is False
    assert state.in_plugdev_group is True


def test_debian_commented_uaccess_line_does_not_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = (
        'SUBSYSTEM=="usb", ATTR{bInterfaceClass}=="ff", ATTR{bInterfaceSubClass}=="42", GROUP="plugdev"\n'
        '  # SUBSYSTEM=="usb", TAG+="uaccess"\n'
    )
    _rule_dirs(tmp_path, monkeypatch, {"etc/udev/rules.d/99-phones.rules": text})
    _fake_groups(monkeypatch, groups=[])

    state = doctor_mod._probe_debian_usb_permissions({})
    assert state.has_udev_rules is True
    assert state.has_uaccess_tag is False
    assert state.has_plugdev_rule is True


def test_debian_commented_android_interface_does_not_qualify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = (
        '# ATTR{bInterfaceClass}=="ff", ATTR{bInterfaceSubClass}=="42", TAG+="uaccess"\n'
        'ATTR{idVendor}=="1234", TAG+="uaccess"\n'
    )
    _rule_dirs(tmp_path, monkeypatch, {"etc/udev/rules.d/60-misc.rules": text})
    _fake_groups(monkeypatch, groups=[])

    assert doctor_mod._probe_debian_usb_permissions({}).has_udev_rules is False


@pytest.mark.parametrize(
    "text",
    [
        'SUBSYSTEM=="usb", ENV{ID_USB_INTERFACES}=="*:ff4201:*", TAG+="uaccess"\n',
        'ENV{ID_DEBUG_APPLIANCE}=="?*", TAG+="uaccess"\n',
        'ENV{adb_user}=="yes", GROUP="plugdev"\n',
    ],
)
def test_debian_android_rule_detected_by_content(text: str) -> None:
    assert doctor_mod._is_android_udev_rule("70-misc.rules", text) is True


def test_debian_etc_rule_overrides_same_named_lib_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _rule_dirs(
        tmp_path,
        monkeypatch,
        {
            "etc/udev/rules.d/51-android.rules": 'ENV{adb_user}=="yes", GROUP="plugdev"\n',
            "lib/udev/rules.d/51-android.rules": DEBIAN_ANDROID_RULES,
        },
    )
    _fake_groups(monkeypatch, groups=[])

    state = doctor_mod._probe_debian_usb_permissions({})
    assert len(state.rule_files) == 1
    assert "etc" in Path(state.rule_files[0]).parts
    assert state.has_uaccess_tag is False


def test_debian_group_membership_via_egid_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _rule_dirs(tmp_path, monkeypatch, {"lib/udev/rules.d/51-android.rules": DEBIAN_ANDROID_RULES})
    _fake_groups(monkeypatch, groups=[], egid=PLUGDEV_GID)

    state = doctor_mod._probe_debian_usb_permissions({})
    assert state.in_plugdev_group is True


def test_debian_adbusers_membership_with_adbusers_rule_passes_headless(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = 'SUBSYSTEM=="usb", ENV{ID_USB_INTERFACES}=="*:ff4201:*", MODE="0660", GROUP="adbusers"\n'
    _rule_dirs(tmp_path, monkeypatch, {"lib/udev/rules.d/51-android.rules": text})
    _fake_groups(monkeypatch, groups=[ADBUSERS_GID])

    env = _debian_env(tmp_path, debian_usb_state=None)
    env.environ["SSH_CONNECTION"] = "192.0.2.1 50000 192.0.2.2 22"
    deb = _check(run_doctor_checks(env), "debian_usb_permissions")
    assert deb["details"]["seat"] == "headless"
    assert deb["details"]["in_plugdev_group"] is False
    assert deb["details"]["in_adbusers_group"] is True
    assert deb["details"]["has_adbusers_rule"] is True
    assert deb["details"]["effective_group_access"] is True
    assert deb["status"] == "pass"
    assert deb["exit_code"] == EXIT_OK


def test_debian_plugdev_membership_without_plugdev_rule_or_uaccess_fails(tmp_path: Path) -> None:
    env = _debian_env(
        tmp_path,
        debian_usb_state=DebianUsbState(
            has_udev_rules=True,
            has_uaccess_tag=False,
            has_plugdev_rule=False,
            rule_files=["/etc/udev/rules.d/51-android.rules"],
            in_plugdev_group=True,
            seat="local",
        ),
    )
    deb = _check(run_doctor_checks(env), "debian_usb_permissions")
    assert deb["status"] == "fail"
    assert deb["exit_code"] == EXIT_DEVICE_OR_PERMISSIONS
    assert deb["details"]["effective_group_access"] is False


def test_debian_local_uaccess_without_granted_group_warns(tmp_path: Path) -> None:
    env = _debian_env(
        tmp_path,
        debian_usb_state=DebianUsbState(
            has_udev_rules=True,
            has_uaccess_tag=True,
            has_plugdev_rule=False,
            in_plugdev_group=True,
            seat="local",
        ),
    )
    deb = _check(run_doctor_checks(env), "debian_usb_permissions")
    assert deb["status"] == "warn"
    assert deb["exit_code"] == EXIT_OK
