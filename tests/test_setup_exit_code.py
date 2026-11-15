"""`adapt setup` exits 0 when its own work succeeded and the toolchain checks pass.

Remaining findings (USB permissions, device state, the shared server) stay in the
report and in `doctor_status` / `doctor_exit_code`. Offline: injected DoctorEnv only.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import adapt_adb.cli as cli_mod
from adapt_adb.cli import app
from adapt_adb.doctor import (
    DebianUsbState,
    DoctorEnv,
    EXIT_DEVICE_OR_PERMISSIONS,
    EXIT_MISSING_TOOLCHAIN,
    EXIT_OK,
    EXIT_UNSAFE_SHARED_SERVER,
    Server5037State,
    run_setup_workflow,
)

runner = CliRunner()

V35 = "Android Debug Bridge version 1.0.41\nVersion 35.0.2-12147458\n"
V29 = "Android Debug Bridge version 1.0.41\nVersion 29.0.6-debian\n"
USB_FAILING = dict(has_udev_rules=False, has_uaccess_tag=False, has_plugdev_rule=False,
                   in_plugdev_group=False, seat="headless")
PERMISSIONS_STATUS = "device_unauthorized_or_no_permissions"


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr("adapt_adb.cli.preflight_adb_server", lambda *a, **k: None)
    monkeypatch.setattr(cli_mod, "_is_interactive_session", lambda: False)


def _fake_download(url, dest):
    return None


def _no_download(*args, **kwargs):
    pytest.fail("setup downloaded although nothing needed downloading")


def _debian_without_adb(tmp_path: Path, *, usb_ok: bool = False) -> DoctorEnv:
    """No adb anywhere; the udev rules are missing (a USB-permission finding) unless usb_ok."""
    return DoctorEnv(
        host_os="debian", arch="x86_64", uid_is_root=False, home_dir=tmp_path / "home" / "dev",
        environ={"PATH": "/usr/bin"}, which_adb=None, which_set=True, path_executables=[],
        existing_paths=set(), source_properties={}, version_outputs={},
        server_state=Server5037State(listening=False),
        debian_usb_state=DebianUsbState(rule_files=["x"]) if usb_ok else DebianUsbState(**USB_FAILING),
        devices_l_output="List of devices attached\n\n", debian_profile_texts={},
    )


def _healthy_windows(tmp_path: Path, devices: str = "List of devices attached\n\n") -> DoctorEnv:
    home = tmp_path / "Users" / "dev"
    pt_dir = home / "AppData" / "Local" / "Android" / "platform-tools"
    adb_exe = pt_dir / "adb.exe"
    return DoctorEnv(
        host_os="windows", arch="AMD64", uid_is_root=False, home_dir=home,
        environ={"LOCALAPPDATA": str(home / "AppData" / "Local"), "PATH": str(pt_dir)},
        which_adb=adb_exe, which_set=True, path_executables=[adb_exe], existing_paths={adb_exe, pt_dir},
        source_properties={pt_dir / "source.properties": "Pkg.Revision=35.0.2\n"},
        version_outputs={adb_exe: V35}, server_state=Server5037State(listening=False),
        windows_pnp_devices=[], devices_l_output=devices,
    )


UNAUTHORIZED = "List of devices attached\nSERIAL0001\tunauthorized transport_id:1\n\n"


# --- library -----------------------------------------------------------------

def test_install_with_a_usb_permission_finding_exits_0(tmp_path):
    report = run_setup_workflow(_debian_without_adb(tmp_path), yes=True, downloader=_fake_download)
    assert report["setup"]["action"] == "installed"
    assert report["exit_code"] == EXIT_OK
    assert report["doctor_exit_code"] == EXIT_DEVICE_OR_PERMISSIONS
    assert report["doctor_status"] == PERMISSIONS_STATUS
    assert report["status"] == PERMISSIONS_STATUS  # status keeps describing host health
    usb = next(c for c in report["checks"] if c["id"] == "debian_usb_permissions")
    assert usb["status"] == "fail"


def test_clean_install_reports_matching_doctor_fields(tmp_path):
    report = run_setup_workflow(_debian_without_adb(tmp_path, usb_ok=True), yes=True, downloader=_fake_download)
    assert report["setup"]["action"] == "installed"
    assert (report["exit_code"], report["doctor_exit_code"], report["doctor_status"]) == (EXIT_OK, EXIT_OK, "ok")


def test_install_whose_adb_is_still_unusable_exits_non_zero(tmp_path):
    env = _debian_without_adb(tmp_path)
    managed = env.home_dir / ".local" / "share" / "android" / "platform-tools" / "adb"
    env.version_outputs[managed] = V29  # the unpacked adb reports a version below the minimum
    report = run_setup_workflow(env, yes=True, downloader=_fake_download)
    assert report["setup"]["action"] == "installed"
    assert report["exit_code"] == EXIT_MISSING_TOOLCHAIN
    assert report["doctor_exit_code"] == EXIT_MISSING_TOOLCHAIN


def test_failed_download_still_exits_non_zero(tmp_path):
    def broken(url, dest):
        raise OSError("connection reset")

    report = run_setup_workflow(_debian_without_adb(tmp_path), yes=True, downloader=broken)
    assert report["setup"]["action"] == "failed"
    assert report["exit_code"] == EXIT_MISSING_TOOLCHAIN
    assert report["doctor_exit_code"] == EXIT_MISSING_TOOLCHAIN


def test_noop_on_a_healthy_toolchain_with_an_unauthorized_device_exits_0(tmp_path):
    report = run_setup_workflow(_healthy_windows(tmp_path, UNAUTHORIZED), downloader=_no_download)
    assert report["setup"]["action"] == "noop_already_healthy"
    assert report["exit_code"] == EXIT_OK
    assert report["doctor_exit_code"] == EXIT_DEVICE_OR_PERMISSIONS


def test_dry_run_keeps_the_pre_flight_exit_code(tmp_path):
    report = run_setup_workflow(_healthy_windows(tmp_path, UNAUTHORIZED), force=True, dry_run=True,
                                downloader=_no_download)
    assert report["setup"]["action"] == "dry_run"
    assert report["exit_code"] == EXIT_DEVICE_OR_PERMISSIONS
    assert report["doctor_exit_code"] == EXIT_DEVICE_OR_PERMISSIONS


def test_aborted_setup_keeps_the_not_done_rule_and_records_the_doctor_result(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.stdin", None)
    report = run_setup_workflow(_healthy_windows(tmp_path), force=True, downloader=_no_download)
    assert report["setup"]["action"] == "aborted_needs_yes"
    assert report["exit_code"] == EXIT_MISSING_TOOLCHAIN
    assert (report["doctor_exit_code"], report["doctor_status"]) == (EXIT_OK, "ok")


def test_unsafe_server_abort_still_exits_3(tmp_path):
    env = _healthy_windows(tmp_path)
    env.server_state = Server5037State(listening=True, owner="SYSTEM", owner_is_root_or_system=True, owner_matches_current_user=False)
    report = run_setup_workflow(env, yes=True, downloader=_no_download)
    assert report["setup"]["action"] == "aborted_unsafe_server"
    assert report["exit_code"] == EXIT_UNSAFE_SHARED_SERVER


def test_install_that_no_longer_matches_a_running_server_still_exits_3(tmp_path):
    # Before setup the adb and the running server are the same version, so the pre-flight
    # passes. A forced reinstall then brings a newer adb than the server the user still runs.
    env = _healthy_windows(tmp_path)
    adb_exe = env.which_adb
    env.server_state = Server5037State(listening=True, owner="dev", server_exe=adb_exe, server_version_raw=V35)

    def newer_adb(url, dest):
        env.version_outputs[adb_exe] = "Android Debug Bridge version 1.0.41\nVersion 37.0.1-14910828\n"

    report = run_setup_workflow(env, force=True, yes=True, downloader=newer_adb)
    assert report["setup"]["action"] == "installed"
    assert report["doctor_exit_code"] == EXIT_UNSAFE_SHARED_SERVER
    assert report["exit_code"] == EXIT_UNSAFE_SHARED_SERVER
    server = next(c for c in report["checks"] if c["id"] == "adb_server_5037")
    assert server["failure_id"] == "adb.server.version_mismatch"


# --- CLI ---------------------------------------------------------------------

def test_cli_human_install_exits_0_and_still_prints_report_and_next_steps(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_mod, "_ACTIVE_DOCTOR_ENV", _debian_without_adb(tmp_path))
    monkeypatch.setattr(cli_mod, "download_and_extract_platform_tools", _fake_download)
    result = runner.invoke(app, ["setup", "--yes"])
    assert result.exit_code == EXIT_OK
    assert "Download and extraction completed successfully." in result.stdout
    assert f"Status: {PERMISSIONS_STATUS} (exit code {EXIT_DEVICE_OR_PERMISSIONS})" in result.stdout
    assert "Next:" in result.stdout and "Run 'adapt doctor' to recheck." in result.stdout


def test_cli_json_install_exposes_the_doctor_result(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_mod, "_ACTIVE_DOCTOR_ENV", _debian_without_adb(tmp_path))
    monkeypatch.setattr(cli_mod, "download_and_extract_platform_tools", _fake_download)
    result = runner.invoke(app, ["setup", "--yes", "--json"])
    doc = json.loads(result.stdout)
    assert result.exit_code == doc["exit_code"] == EXIT_OK
    assert doc["doctor_exit_code"] == EXIT_DEVICE_OR_PERMISSIONS
    assert doc["doctor_status"] == doc["status"] == PERMISSIONS_STATUS
    assert "failure" not in doc


def test_cli_doctor_json_has_no_setup_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_mod, "_ACTIVE_DOCTOR_ENV", _healthy_windows(tmp_path, UNAUTHORIZED))
    doc = json.loads(runner.invoke(app, ["doctor", "--json"]).stdout)
    assert "doctor_exit_code" not in doc and "doctor_status" not in doc
    assert doc["exit_code"] == EXIT_DEVICE_OR_PERMISSIONS


def test_everything_still_blocks_on_an_unsafe_server_found_after_setup(monkeypatch):
    from adapt_adb import everything as batch
    from adapt_adb.tool_catalog import HostEnvironment, build_list_report

    host = HostEnvironment("windows", "windows-native", "x86_64", "test", "Synthetic Windows", False)
    inventory = build_list_report(host=host, which=lambda _: None)
    monkeypatch.setattr(batch, "build_list_report", lambda: inventory)
    monkeypatch.setattr(batch, "plan_scrcpy", lambda: {"status": "ready", "target_dir": "synthetic-managed"})
    monkeypatch.setattr(batch.device_state, "scan", lambda **k: {"status": "observed", "transports": {}})
    monkeypatch.setattr(batch, "run_doctor_checks", lambda: {"exit_code": 0})
    monkeypatch.setattr(batch, "run_setup_workflow", lambda **k: {
        "exit_code": 0, "doctor_exit_code": EXIT_UNSAFE_SHARED_SERVER, "setup": {"action": "installed"},
    })
    monkeypatch.setattr(batch, "install_scrcpy", lambda **k: pytest.fail("scrcpy must not install"))
    result = batch.run_everything(yes=True, devices=[])
    scrcpy = next(s for s in result["steps"] if s["step"] == "scrcpy")
    assert scrcpy["status"] == "skipped"
