"""The root/elevated warning appears exactly once in a human CLI run (on stderr).

The doctor report keeps its own note when it is rendered without the CLI having
warned (library use, the interactive console). Offline: injected DoctorEnv only.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import adapt_adb.cli as cli_mod
import adapt_adb.doctor as doctor_mod
from adapt_adb.cli import app
from adapt_adb.doctor import DoctorEnv, ROOT_WARNING, Server5037State, render_doctor_human, run_doctor_checks

runner = CliRunner()
LINE = "Warning: ADAPT is running as root."


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr("adapt_adb.cli.preflight_adb_server", lambda *a, **k: None)
    monkeypatch.setattr(cli_mod, "_is_interactive_session", lambda: False)


def _root_linux_env(tmp_path: Path) -> DoctorEnv:
    home = tmp_path / "home" / "dev"
    pt_dir = home / ".local" / "share" / "android" / "platform-tools"
    adb = pt_dir / "adb"
    return DoctorEnv(
        host_os="debian", arch="x86_64", uid_is_root=True, home_dir=home,
        environ={"PATH": str(pt_dir)}, which_adb=adb, which_set=True, path_executables=[adb],
        existing_paths={adb, pt_dir},
        source_properties={pt_dir / "source.properties": "Pkg.Revision=35.0.2\n"},
        version_outputs={adb: "Version 35.0.2-12147458\n"},
        server_state=Server5037State(listening=False),
        debian_usb_state=doctor_mod.DebianUsbState(rule_files=["x"]),
        devices_l_output="List of devices attached\n\n",
    )


@pytest.fixture
def root_env(tmp_path, monkeypatch):
    env = _root_linux_env(tmp_path)
    monkeypatch.setattr(cli_mod, "_ACTIVE_DOCTOR_ENV", env)
    monkeypatch.setattr(cli_mod, "_privilege_warning_text", lambda: ROOT_WARNING)
    return env


@pytest.mark.parametrize("argv", [["doctor"], ["doctor", "--fix"], ["setup", "--yes"], ["setup", "--dry-run"]])
def test_human_cli_run_shows_the_warning_exactly_once(root_env, argv):
    result = runner.invoke(app, argv)
    assert (result.stdout + result.stderr).count(LINE) == 1
    assert result.stderr.count(LINE) == 1


def test_setup_still_renders_the_report(root_env):
    result = runner.invoke(app, ["setup", "--yes"])
    assert "Status:" in result.stdout and "Checks:" in result.stdout


@pytest.mark.parametrize("argv", [["doctor", "--json"], ["setup", "--yes", "--json"]])
def test_json_runs_carry_the_facts_and_no_warning_text(root_env, argv):
    result = runner.invoke(app, argv)
    assert LINE not in result.stdout and LINE not in result.stderr
    assert json.loads(result.stdout)["host"]["uid_is_root"] is True


def test_report_keeps_its_note_when_the_cli_did_not_warn(root_env, monkeypatch):
    # The CLI's own check sees no root, but the report's host does: the report says so once.
    monkeypatch.setattr(cli_mod, "_privilege_warning_text", lambda: None)
    result = runner.invoke(app, ["doctor"])
    assert result.stderr.count(LINE) == 0
    assert result.stdout.count(LINE) == 1


def test_library_render_keeps_the_note(tmp_path):
    report = run_doctor_checks(_root_linux_env(tmp_path))
    assert render_doctor_human(report).count(LINE) == 1


def test_library_tty_render_keeps_the_note(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor_mod, "_is_stdout_tty", lambda: True)
    report = run_doctor_checks(_root_linux_env(tmp_path))
    assert render_doctor_human(report).count(LINE) == 1
