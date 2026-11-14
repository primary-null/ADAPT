"""Host fixes: `adapt setup` registers an existing healthy copy on PATH instead of downloading,
and refuses unsupported platforms. Offline only: injected DoctorEnv fakes, no download."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

import adapt_adb.cli as cli_mod
import adapt_adb.doctor as doctor_mod
from adapt_adb.cli import app
from adapt_adb.doctor import (
    DebianUsbState,
    DoctorEnv,
    EXIT_MISSING_TOOLCHAIN,
    EXIT_OK,
    Server5037State,
    run_setup_workflow,
)

runner = CliRunner()

V35 = "Android Debug Bridge version 1.0.41\nVersion 35.0.2-12147458\n"
V33 = "Android Debug Bridge version 1.0.41\nVersion 33.0.3-8952118\n"
V29_DEBIAN = "Android Debug Bridge version 1.0.41\nVersion 29.0.6-debian\n"


def _no_download(*args, **kwargs):
    pytest.fail("setup downloaded although a healthy copy was installed")


def _win_managed_off_path(tmp_path: Path) -> tuple[DoctorEnv, Path]:
    """A healthy managed copy sits in setup's own folder; PATH has no adb."""
    home = tmp_path / "Users" / "dev"
    local_app = home / "AppData" / "Local"
    pt_dir = local_app / "Android" / "platform-tools"
    adb_exe = pt_dir / "adb.exe"
    env = DoctorEnv(
        host_os="windows",
        arch="AMD64",
        uid_is_root=False,
        home_dir=home,
        environ={"LOCALAPPDATA": str(local_app), "PATH": ""},
        which_adb=None,
        which_set=True,
        path_executables=[],
        existing_paths={adb_exe, pt_dir},
        source_properties={pt_dir / "source.properties": "Pkg.Revision=35.0.2\n"},
        version_outputs={adb_exe: V35},
        server_state=Server5037State(listening=False),
        windows_pnp_devices=[],
        devices_l_output="List of devices attached\n\n",
    )
    return env, adb_exe


def test_healthy_managed_copy_is_registered_on_path_without_downloading(tmp_path):
    env, adb_exe = _win_managed_off_path(tmp_path)
    report = run_setup_workflow(env, yes=True, downloader=_no_download)
    setup = report["setup"]
    assert setup["action"] == "path_registered"
    assert setup["downloaded"] is False and setup["path_updated"] is True
    assert setup["reused_existing"] is True
    assert setup["target_dir"] == str(adb_exe.parent)
    assert env.which_adb == adb_exe
    assert env.environ["PATH"].split(";")[0] == str(adb_exe.parent)
    assert report["toolchain"]["adb_path"] == str(adb_exe)
    assert report["exit_code"] == EXIT_OK


def test_path_registration_honours_dry_run_and_no_path(tmp_path):
    env, _ = _win_managed_off_path(tmp_path)
    dry = run_setup_workflow(env, dry_run=True, downloader=_no_download)["setup"]
    assert dry["action"] == "dry_run" and dry["would_download"] is False and dry["would_update_path"] is True
    assert dry["reused_existing"] is True
    dry_np = run_setup_workflow(env, dry_run=True, no_path=True, downloader=_no_download)["setup"]
    assert dry_np["would_update_path"] is False
    assert env.which_adb is None and env.environ["PATH"] == ""

    report = run_setup_workflow(env, yes=True, no_path=True, downloader=_no_download)
    assert report["setup"]["action"] == "noop_already_installed"
    assert report["setup"]["path_updated"] is False
    assert env.which_adb is None and env.environ["PATH"] == ""
    assert report["exit_code"] == EXIT_MISSING_TOOLCHAIN  # pre-flight code: the injected doctor sees no PATH adb


def test_force_still_downloads_again(tmp_path):
    env, _ = _win_managed_off_path(tmp_path)
    calls = []
    report = run_setup_workflow(env, yes=True, force=True, downloader=lambda url, dest: calls.append(dest))
    assert report["setup"]["action"] == "installed" and len(calls) == 1


def test_path_only_change_needs_confirmation_like_a_download(tmp_path, monkeypatch):
    env, _ = _win_managed_off_path(tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    report = run_setup_workflow(env, downloader=_no_download)
    assert report["setup"]["action"] == "aborted_needs_yes"
    assert report["setup"]["reused_existing"] is True
    assert report["exit_code"] == EXIT_MISSING_TOOLCHAIN
    assert env.which_adb is None


def test_declined_path_only_prompt_changes_nothing(tmp_path, monkeypatch):
    env, _ = _win_managed_off_path(tmp_path)

    class _Tty(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", _Tty("n\n"))
    monkeypatch.setattr(sys, "stderr", _Tty())
    report = run_setup_workflow(env, downloader=_no_download)
    assert report["setup"]["action"] == "aborted_unconfirmed"
    assert "Add the existing platform-tools folder" in sys.stderr.getvalue()
    assert env.which_adb is None


def test_failed_path_registration_reports_failed_with_guidance(tmp_path, monkeypatch):
    env, adb_exe = _win_managed_off_path(tmp_path)
    monkeypatch.setattr(doctor_mod, "_register_on_path", lambda *a, **k: False)
    monkeypatch.setattr(cli_mod, "_ACTIVE_DOCTOR_ENV", env)
    result = runner.invoke(app, ["setup", "--yes", "--json"])
    doc = json.loads(result.stdout)
    assert doc["setup"]["action"] == "failed" and doc["setup"]["reused_existing"] is True
    assert doc["error"] == f"Could not add {adb_exe.parent} to your user PATH."
    assert doc["failure"]["id"] == "setup.path_update_failed"
    assert result.exit_code == EXIT_MISSING_TOOLCHAIN


def test_unhealthy_managed_copy_is_replaced_by_a_download(tmp_path):
    env, adb_exe = _win_managed_off_path(tmp_path)
    env.version_outputs[adb_exe] = V33
    calls = []
    report = run_setup_workflow(env, yes=True, downloader=lambda url, dest: calls.append(dest))
    assert report["setup"]["action"] == "installed" and calls


def test_shadowing_distribution_adb_is_fixed_by_prepending_the_managed_copy(tmp_path):
    home = tmp_path / "home" / "dev"
    managed = home / ".local" / "share" / "android" / "platform-tools" / "adb"
    apt_adb = Path("/usr/bin/adb")
    env = DoctorEnv(
        host_os="debian", arch="x86_64", uid_is_root=False, home_dir=home,
        environ={"PATH": "/usr/bin"}, which_adb=apt_adb, which_set=True, path_executables=[apt_adb],
        existing_paths={apt_adb, managed}, dpkg_owners={apt_adb: "adb"},
        source_properties={managed.parent / "source.properties": "Pkg.Revision=35.0.2\n"},
        version_outputs={apt_adb: V29_DEBIAN, managed: V35},
        server_state=Server5037State(listening=False),
        debian_usb_state=DebianUsbState(rule_files=["/etc/udev/rules.d/51-android.rules"]),
        devices_l_output="List of devices attached\n\n",
        debian_profile_texts={},
    )
    report = run_setup_workflow(env, yes=True, downloader=_no_download)
    assert report["setup"]["action"] == "path_registered"
    assert env.environ["PATH"] == f"{managed.parent}:/usr/bin"
    assert env.path_executables[0] == managed
    assert report["exit_code"] == EXIT_OK


def test_cli_human_output_for_path_registration(tmp_path, monkeypatch):
    env, adb_exe = _win_managed_off_path(tmp_path)
    monkeypatch.setattr(cli_mod, "_ACTIVE_DOCTOR_ENV", env)
    monkeypatch.setattr(cli_mod, "download_and_extract_platform_tools", _no_download)
    result = runner.invoke(app, ["setup", "--yes"])
    assert result.exit_code == EXIT_OK
    assert f"Healthy platform-tools are already installed at: {adb_exe.parent}" in result.stdout
    assert "Skipping download; added that folder to your user PATH." in result.stdout
    assert "reopen any terminal" in result.stdout
    assert "Downloaded official platform-tools" not in result.stdout


def test_cli_human_output_for_dry_run_and_no_path(tmp_path, monkeypatch):
    env, _ = _win_managed_off_path(tmp_path)
    monkeypatch.setattr(cli_mod, "_ACTIVE_DOCTOR_ENV", env)
    dry = runner.invoke(app, ["setup", "--dry-run"])
    assert "[DRY-RUN] Would not download: healthy platform-tools are already installed." in dry.stdout
    assert "[DRY-RUN] Would download:" not in dry.stdout
    nopath = runner.invoke(app, ["setup", "--yes", "--no-path"])
    assert "Skipping download; PATH not changed (--no-path)." in nopath.stdout


def test_cli_needs_yes_line_names_the_path_change(tmp_path, monkeypatch):
    env, _ = _win_managed_off_path(tmp_path)
    monkeypatch.setattr(cli_mod, "_ACTIVE_DOCTOR_ENV", env)
    result = runner.invoke(app, ["setup"])
    assert "pass --yes to add the existing platform-tools folder to PATH" in result.stderr
    assert result.exit_code == EXIT_MISSING_TOOLCHAIN


# ---------------------------------------------------------------------------
# Item 4: unsupported platform (macOS)
# ---------------------------------------------------------------------------

def _mac_env(tmp_path: Path) -> DoctorEnv:
    return DoctorEnv(
        host_os="macos", arch="arm64", uid_is_root=False, home_dir=tmp_path / "Users" / "dev",
        environ={"PATH": ""}, which_adb=None, which_set=True, path_executables=[], existing_paths=set(),
        version_outputs={}, server_state=Server5037State(listening=False),
    )


def test_macos_setup_refuses_before_downloading(tmp_path):
    env = _mac_env(tmp_path)
    for kwargs in ({"yes": True}, {"dry_run": True}, {"yes": True, "force": True}):
        report = run_setup_workflow(env, downloader=_no_download, **kwargs)
        setup = report["setup"]
        assert setup["action"] == "unsupported_platform"
        assert set(setup) == {"action", "dry_run", "no_path", "target_dir", "url", "downloaded", "path_updated", "error"}
        assert setup["downloaded"] is False and setup["target_dir"] is None and setup["url"] is None
        assert report["error"] == setup["error"] and "'macos'" in setup["error"]
        assert report["exit_code"] != EXIT_OK  # not-done rule


def test_macos_setup_cli_prints_error_and_guidance(tmp_path, monkeypatch):
    env = _mac_env(tmp_path)
    monkeypatch.setattr(cli_mod, "_ACTIVE_DOCTOR_ENV", env)
    human = runner.invoke(app, ["setup", "--yes"])
    assert human.stderr.startswith("Error: Unsupported host platform 'macos'")
    assert "adapt explain setup.unsupported_platform" in human.stderr
    assert human.exit_code != 0
    machine = runner.invoke(app, ["setup", "--yes", "--json"])
    doc = json.loads(machine.stdout)
    assert doc["failure"]["id"] == "setup.unsupported_platform"
    assert machine.exit_code == doc["exit_code"]
