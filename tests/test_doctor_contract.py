"""Automated contract test suite for `adapt doctor` and `adapt setup` (`docs/doctor.md`).

Uses pure in-memory fakes (`DoctorEnv`) — no physical phones, no network, no serials.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

import adapt_adb.cli as cli_mod
from adapt_adb.cli import app
from adapt_adb.doctor import (
    CHECK_IDS,
    DebianUsbState,
    DoctorEnv,
    EXIT_DEVICE_OR_PERMISSIONS,
    EXIT_MISSING_TOOLCHAIN,
    EXIT_OK,
    EXIT_UNSAFE_SHARED_SERVER,
    PnpDeviceRecord,
    Server5037State,
    parse_adb_devices_output,
    parse_platform_tools_version,
    resolve_highest_exit_code,
    run_doctor_checks,
    run_setup_workflow,
    validate_zip_members_safe,
)

runner = CliRunner()

FAKE_VERSION_35 = (
    "Android Debug Bridge version 1.0.41\n"
    "Version 35.0.2-12147458\n"
    "Installed as /home/user/.local/share/android/platform-tools/adb\n"
)

FAKE_VERSION_29_DEBIAN = (
    "Android Debug Bridge version 1.0.41\n"
    "Version 29.0.6-debian\n"
    "Installed as /usr/bin/adb\n"
)


def _make_healthy_windows_env(tmp_path: Path) -> DoctorEnv:
    home = tmp_path / "Users" / "dev"
    local_app = home / "AppData" / "Local"
    pt_dir = local_app / "Android" / "platform-tools"
    adb_exe = pt_dir / "adb.exe"
    return DoctorEnv(
        host_os="windows",
        arch="AMD64",
        uid_is_root=False,
        home_dir=home,
        environ={
            "LOCALAPPDATA": str(local_app),
            "PATH": str(pt_dir),
        },
        which_adb=adb_exe,
        which_set=True,
        path_executables=[adb_exe],
        existing_paths={adb_exe, pt_dir},
        source_properties={pt_dir / "source.properties": "Pkg.Revision=35.0.2\n"},
        version_outputs={adb_exe: FAKE_VERSION_35},
        server_state=Server5037State(listening=False),
        windows_pnp_devices=[],
        devices_l_output="List of devices attached\n\n",
    )


def _make_healthy_debian_env(tmp_path: Path) -> DoctorEnv:
    home = tmp_path / "home" / "dev"
    pt_dir = home / ".local" / "share" / "android" / "platform-tools"
    adb_bin = pt_dir / "adb"
    return DoctorEnv(
        host_os="debian",
        arch="x86_64",
        uid_is_root=False,
        home_dir=home,
        environ={"PATH": f"{pt_dir}:/usr/bin"},
        which_adb=adb_bin,
        which_set=True,
        path_executables=[adb_bin],
        existing_paths={adb_bin, pt_dir},
        source_properties={pt_dir / "source.properties": "Pkg.Revision=35.0.2\n"},
        version_outputs={adb_bin: FAKE_VERSION_35},
        server_state=Server5037State(listening=False),
        debian_usb_state=DebianUsbState(
            has_udev_rules=True,
            has_uaccess_tag=True,
            has_plugdev_rule=True,
            rule_files=["/etc/udev/rules.d/51-android.rules"],
            in_plugdev_group=True,
            seat="local",
        ),
        devices_l_output="List of devices attached\n\n",
    )


def test_check_ordering_server_first_and_devices_last(tmp_path: Path) -> None:
    """`adb_server_5037` MUST run first and `devices_state` MUST run last."""
    env = _make_healthy_windows_env(tmp_path)
    report = run_doctor_checks(env)
    ids = [c["id"] for c in report["checks"]]
    assert ids == list(CHECK_IDS)
    assert ids[0] == "adb_server_5037"
    assert ids[-1] == "devices_state"
    assert report["exit_code"] == EXIT_OK
    assert report["status"] == "ok"


def test_server_mismatch_never_calls_adb_devices(tmp_path: Path) -> None:
    """When client/server versions mismatch on 5037, doctor exits 3 and skips `adb devices -l`."""
    env = _make_healthy_debian_env(tmp_path)
    env.server_state = Server5037State(
        listening=True,
        pid=4242,
        owner="dev",
        owner_uid=1000,
        owner_matches_current_user=True,
        server_version_raw=FAKE_VERSION_29_DEBIAN,
    )
    report = run_doctor_checks(env)

    assert report["exit_code"] == EXIT_UNSAFE_SHARED_SERVER
    assert report["status"] == "unsafe_shared_server"
    assert env.devices_l_invoked == []
    for cmd in env.commands_invoked:
        assert "devices" not in cmd

    checks_by_id = {c["id"]: c for c in report["checks"]}
    assert checks_by_id["adb_server_5037"]["status"] == "fail"
    assert checks_by_id["adb_server_5037"]["exit_code"] == 3
    assert checks_by_id["devices_state"]["status"] == "skip"


def test_root_owned_server_or_tmp_5037_exits_3_and_skips_devices(tmp_path: Path) -> None:
    """Root-owned /tmp/5037 or port 5037 daemon immediately triggers exit code 3."""
    env = _make_healthy_debian_env(tmp_path)
    env.server_state = Server5037State(
        listening=True,
        pid=999,
        owner="root",
        owner_uid=0,
        owner_is_root_or_system=True,
        owner_matches_current_user=False,
        tmp_5037_exists=True,
        tmp_5037_uid=0,
        tmp_5037_owner_is_root=True,
    )
    report = run_doctor_checks(env)
    assert report["exit_code"] == EXIT_UNSAFE_SHARED_SERVER
    assert report["status"] == "unsafe_shared_server"
    assert env.devices_l_invoked == []


def test_exit_code_precedence_3_over_1_over_2_over_0(tmp_path: Path) -> None:
    """Verify strict exit code precedence: 3 > 1 > 2 > 0."""
    assert resolve_highest_exit_code([0, 2, 1, 3]) == 3
    assert resolve_highest_exit_code([0, 2, 1]) == 1
    assert resolve_highest_exit_code([0, 2]) == 2
    assert resolve_highest_exit_code([0, 0]) == 0

    # Combine root server (3) + missing toolchain (1) + PnP error (2) -> must return 3
    env = _make_healthy_windows_env(tmp_path)
    env.which_adb = None
    env.path_executables = []
    env.server_state = Server5037State(
        listening=True,
        owner="SYSTEM",
        owner_is_root_or_system=True,
        owner_matches_current_user=False,
    )
    env.windows_pnp_devices = [
        PnpDeviceRecord(
            instance_id="USB\\VID_18D1&PID_4EE7\\TEST",
            friendly_name="Android Composite ADB Interface",
            class_name="AndroidDevice",
            status="Error",
            problem_code=28,
        )
    ]
    report = run_doctor_checks(env)
    assert report["exit_code"] == EXIT_UNSAFE_SHARED_SERVER

    # Fix server -> missing toolchain (1) + PnP error (2) -> must return 1
    env.server_state = Server5037State(listening=False)
    report2 = run_doctor_checks(env)
    assert report2["exit_code"] == EXIT_MISSING_TOOLCHAIN
    assert report2["status"] == "missing_toolchain"


def test_debian_apt_adb_29_fails_version_and_origin(tmp_path: Path) -> None:
    """Debian `/usr/bin/adb` (version 29.0.6) fails `adb_version` and `adb_origin` with exit 1."""
    env = _make_healthy_debian_env(tmp_path)
    apt_adb = Path("/usr/bin/adb")
    env.which_adb = apt_adb
    env.path_executables = [apt_adb]
    env.existing_paths = {apt_adb}
    env.version_outputs = {apt_adb: FAKE_VERSION_29_DEBIAN}
    env.dpkg_owners = {apt_adb: "adb"}

    report = run_doctor_checks(env)
    assert report["exit_code"] == EXIT_MISSING_TOOLCHAIN
    assert report["toolchain"]["origin"] == "debian_apt"
    checks_by_id = {c["id"]: c for c in report["checks"]}
    assert checks_by_id["adb_version"]["status"] == "fail"
    assert checks_by_id["adb_origin"]["status"] == "fail"
    assert checks_by_id["devices_state"]["status"] == "skip"
    assert env.devices_l_invoked == []


def test_android_home_present_but_untouched(tmp_path: Path) -> None:
    """`ANDROID_HOME` is inspected read-only and never mutated or written into."""
    env = _make_healthy_windows_env(tmp_path)
    sdk_dir = tmp_path / "CustomAndroidSdk"
    env.environ["ANDROID_HOME"] = str(sdk_dir)
    # Missing on disk -> warn (exit 0), never created or modified
    report = run_doctor_checks(env)
    assert report["toolchain"]["android_home_present"] is True
    ah_check = next(c for c in report["checks"] if c["id"] == "android_home")
    assert ah_check["status"] == "warn"
    assert ah_check["exit_code"] == EXIT_OK
    assert ah_check["details"]["untouched"] is True
    assert not sdk_dir.exists()


def test_windows_pnp_and_debian_usb_permissions_exit_2(tmp_path: Path) -> None:
    """Windows PnP driver error or Debian missing udev/plugdev on SSH exits with 2."""
    win_env = _make_healthy_windows_env(tmp_path)
    win_env.windows_pnp_devices = [
        PnpDeviceRecord(
            instance_id="USB\\VID_04E8&PID_6860\\FAKE_ID",
            friendly_name="SAMSUNG Android ADB Interface",
            class_name="USBDevice",
            status="Error",
            problem_code=52,
        )
    ]
    win_report = run_doctor_checks(win_env)
    assert win_report["exit_code"] == EXIT_DEVICE_OR_PERMISSIONS
    assert win_report["status"] == "device_unauthorized_or_no_permissions"
    pnp_check = next(c for c in win_report["checks"] if c["id"] == "windows_pnp")
    assert pnp_check["status"] == "fail"
    assert "developer.android.com/studio/run/win-usb" in (pnp_check["remediation"] or "")
    assert "zadig" not in (pnp_check["remediation"] or "").lower()
    assert "bcdedit" not in (pnp_check["remediation"] or "").lower()

    deb_env = _make_healthy_debian_env(tmp_path)
    deb_env.debian_usb_state = DebianUsbState(
        has_udev_rules=True,
        has_uaccess_tag=True,
        has_plugdev_rule=True,
        rule_files=["/etc/udev/rules.d/51-android.rules"],
        in_plugdev_group=False,
        seat="headless",
    )
    deb_report = run_doctor_checks(deb_env)
    assert deb_report["exit_code"] == EXIT_DEVICE_OR_PERMISSIONS
    deb_check = next(c for c in deb_report["checks"] if c["id"] == "debian_usb_permissions")
    assert deb_check["status"] == "fail"
    assert "plugdev" in (deb_check["remediation"] or "")
    assert "sudo adb" not in (deb_check["remediation"] or "").lower()


def test_devices_state_parsing_and_serial_redaction(tmp_path: Path) -> None:
    """`devices_state` fails with exit 2 on `unauthorized` or `no permissions` and redacts serials."""
    env = _make_healthy_debian_env(tmp_path)
    fake_token_a = "FAKE_DEV_TOKEN_ALPHA"
    fake_token_b = "FAKE_DEV_TOKEN_BETA"
    env.devices_l_output = (
        "List of devices attached\n"
        f"{fake_token_a}\tunauthorized usb:1-1 transport_id:1\n"
        f"{fake_token_b}\tno permissions (user in plugdev group; are your udev rules wrong?); see [http://developer.android.com/tools/device.html]\n"
    )
    report = run_doctor_checks(env)
    assert report["exit_code"] == EXIT_DEVICE_OR_PERMISSIONS
    assert report["devices"]["total"] == 2
    assert report["devices"]["by_state"]["unauthorized"] == 1
    assert report["devices"]["by_state"]["no_permissions"] == 1

    serialized = json.dumps(report)
    assert fake_token_a not in serialized
    assert fake_token_b not in serialized


def test_cli_doctor_json_and_fix_flags(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`adapt doctor --json` emits strictly one JSON document; `--fix` prints safe hints only."""
    env = _make_healthy_debian_env(tmp_path)
    env.which_adb = None
    env.path_executables = []
    monkeypatch.setattr(cli_mod, "_ACTIVE_DOCTOR_ENV", env)

    res_json = runner.invoke(app, ["doctor", "--json"])
    assert res_json.exit_code == EXIT_MISSING_TOOLCHAIN
    payload = json.loads(res_json.stdout)
    assert payload["schema_version"] == "1"
    assert payload["command"] == "doctor"
    assert payload["exit_code"] == EXIT_MISSING_TOOLCHAIN
    assert payload["status"] == "missing_toolchain"
    assert isinstance(payload["checks"], list) and len(payload["checks"]) == 9

    res_fix = runner.invoke(app, ["doctor", "--fix"])
    assert res_fix.exit_code == EXIT_MISSING_TOOLCHAIN
    assert "Safe Remediation Commands" in res_fix.output
    lower_out = res_fix.output.lower()
    for forbidden in ("kill-server", "sudo adb", "zadig", "bcdedit"):
        assert forbidden not in lower_out


def test_cli_setup_flags_yes_dry_run_no_path_json_and_code_3_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`adapt setup --yes --dry-run --no-path --json` honors pre-flight code 3 and user-scope install."""
    # 1. Code 3 pre-flight abort
    env_unsafe = _make_healthy_debian_env(tmp_path)
    env_unsafe.which_adb = None
    env_unsafe.path_executables = []
    env_unsafe.server_state = Server5037State(
        listening=True,
        owner="root",
        owner_uid=0,
        owner_is_root_or_system=True,
        owner_matches_current_user=False,
    )
    monkeypatch.setattr(cli_mod, "_ACTIVE_DOCTOR_ENV", env_unsafe)
    dl_calls: list[tuple[str, Path]] = []
    monkeypatch.setattr(
        cli_mod,
        "download_and_extract_platform_tools",
        lambda url, dest: dl_calls.append((url, dest)),
    )

    res_abort = runner.invoke(app, ["setup", "--yes", "--json"])
    assert res_abort.exit_code == EXIT_UNSAFE_SHARED_SERVER
    abort_doc = json.loads(res_abort.stdout)
    assert abort_doc["exit_code"] == EXIT_UNSAFE_SHARED_SERVER
    assert abort_doc["setup"]["action"] == "aborted_unsafe_server"
    assert dl_calls == []

    # 2. Dry-run when toolchain is missing -> does not call downloader
    env_missing = _make_healthy_windows_env(tmp_path)
    env_missing.which_adb = None
    env_missing.path_executables = []
    monkeypatch.setattr(cli_mod, "_ACTIVE_DOCTOR_ENV", env_missing)

    res_dry = runner.invoke(app, ["setup", "--yes", "--dry-run", "--no-path", "--json"])
    assert res_dry.exit_code == EXIT_MISSING_TOOLCHAIN
    dry_doc = json.loads(res_dry.stdout)
    assert dry_doc["setup"]["action"] == "dry_run"
    assert dry_doc["setup"]["dry_run"] is True
    assert dry_doc["setup"]["no_path"] is True
    assert dl_calls == []

    # 3. Full user-scope setup transitions from missing (1) -> healthy (0)
    res_install = runner.invoke(app, ["setup", "--yes", "--json"])
    assert res_install.exit_code == EXIT_OK
    inst_doc = json.loads(res_install.stdout)
    assert inst_doc["exit_code"] == EXIT_OK
    assert inst_doc["status"] == "ok"
    assert inst_doc["setup"]["action"] == "installed"
    assert inst_doc["setup"]["downloaded"] is True
    assert len(dl_calls) == 1
    assert dl_calls[0][0].startswith("https://dl.google.com/")


def test_zip_slip_traversal_rejected(tmp_path: Path) -> None:
    """`validate_zip_members_safe` rejects archives containing `..` traversal."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../evil.exe", b"nope")
    buf.seek(0)
    with zipfile.ZipFile(buf, "r") as zf:
        with pytest.raises(ValueError, match="Unsafe"):
            validate_zip_members_safe(zf, tmp_path)


def test_build_next_actions_and_tty_numbered_menu(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`build_next_actions` returns ordered runnable actions and TTY renderer numbers them `[1]`..`[q]`."""
    import sys

    from adapt_adb.doctor import build_next_actions, render_doctor_human

    # 1. Windows missing toolchain -> setup (1), device_manager (2), ..., recheck, done (q)
    win_missing = _make_healthy_windows_env(tmp_path)
    win_missing.which_adb = None
    win_missing.path_executables = []
    win_missing_report = run_doctor_checks(win_missing)
    win_missing_actions = build_next_actions(win_missing_report)
    assert [a["key"] for a in win_missing_actions] == [
        str(i) for i in range(1, len(win_missing_actions))
    ] + ["q"]
    win_missing_ids = [a["id"] for a in win_missing_actions]
    assert win_missing_ids[:2] == ["setup", "device_manager"]
    assert "recheck" in win_missing_ids
    assert win_missing_ids[-1] == "done"
    assert "adapt setup" in win_missing_actions[0]["label"]
    assert "Device Manager" in win_missing_actions[1]["label"]
    assert all("zadig" not in a["label"].lower() for a in win_missing_actions)

    # 2. Windows healthy -> device_manager (1), ..., recheck, done (q)
    win_ok_report = run_doctor_checks(_make_healthy_windows_env(tmp_path))
    win_ok_actions = build_next_actions(win_ok_report)
    assert [a["key"] for a in win_ok_actions] == [
        str(i) for i in range(1, len(win_ok_actions))
    ] + ["q"]
    win_ok_ids = [a["id"] for a in win_ok_actions]
    assert win_ok_ids[0] == "device_manager"
    assert "recheck" in win_ok_ids
    assert win_ok_ids[-1] == "done"

    # 3. Debian healthy -> udev (1), ..., recheck, done (q) and never sudo adb
    deb_ok_report = run_doctor_checks(_make_healthy_debian_env(tmp_path))
    deb_ok_actions = build_next_actions(deb_ok_report)
    assert [a["key"] for a in deb_ok_actions] == [
        str(i) for i in range(1, len(deb_ok_actions))
    ] + ["q"]
    deb_ok_ids = [a["id"] for a in deb_ok_actions]
    assert deb_ok_ids[0] == "udev"
    assert "recheck" in deb_ok_ids
    assert deb_ok_ids[-1] == "done"
    assert all("sudo adb" not in a["label"].lower() for a in deb_ok_actions)

    # 4. TTY renderer prints numbered Next menu
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    rendered = render_doctor_human(win_missing_report, show_fix=True)
    assert "Next:" in rendered
    assert "[1]" in rendered
    assert "[q]" in rendered
    assert "adapt setup" in rendered
    assert "Device Manager" in rendered
    assert "Safe Remediation Commands" in rendered
    assert "zadig" not in rendered.lower()


def test_build_next_actions_includes_wireless_on_windows_and_linux(tmp_path: Path) -> None:
    """`build_next_actions` includes `id='wireless'` on Windows and Linux."""
    from adapt_adb.doctor import build_next_actions

    win_report = run_doctor_checks(_make_healthy_windows_env(tmp_path))
    win_actions = build_next_actions(win_report)
    win_wireless = [a for a in win_actions if a["id"] == "wireless"]
    assert len(win_wireless) == 1
    assert win_wireless[0]["label"].strip()
    assert "zadig" not in win_wireless[0]["label"].lower()

    deb_report = run_doctor_checks(_make_healthy_debian_env(tmp_path))
    deb_actions = build_next_actions(deb_report)
    deb_wireless = [a for a in deb_actions if a["id"] == "wireless"]
    assert len(deb_wireless) == 1
    assert deb_wireless[0]["label"].strip()
    assert "zadig" not in deb_wireless[0]["label"].lower()

    linux_env = _make_healthy_debian_env(tmp_path)
    linux_env.host_os = "linux"
    linux_report = run_doctor_checks(linux_env)
    linux_actions = build_next_actions(linux_report)
    linux_wireless = [a for a in linux_actions if a["id"] == "wireless"]
    assert len(linux_wireless) == 1
    assert linux_wireless[0]["label"].strip()
    assert "zadig" not in linux_wireless[0]["label"].lower()

