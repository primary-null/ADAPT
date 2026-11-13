"""Offline integration boundaries for new command groups and daemon checks."""

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from adapt_adb.cli import app
from adapt_adb import doctor as doctor_mod


def test_new_commands_registered_and_tool_json_is_clean():
    runner = CliRunner()
    help_result = runner.invoke(app, ["--help"])
    assert help_result.exit_code == 0
    for command in ("dev-options", "tools", "android", "dex", "help-me", "tips"):
        assert command in help_result.stdout
    result = runner.invoke(app, ["tools", "list", "--goal", "mirroring", "--json"])
    assert result.exit_code == 0
    assert result.stderr == ""
    report = json.loads(result.stdout)
    assert report["goal"] == "mirroring"
    assert {item["id"] for item in report["tools"]} == {"platform-tools", "scrcpy"}


def test_dev_options_require_target_before_execution():
    result = CliRunner().invoke(app, ["dev-options", "inspect", "--json"])
    assert result.exit_code != 0
    assert "serial" in result.stderr.lower()


def test_listener_binary_does_not_become_trusted_by_owning_socket(monkeypatch):
    calls = []
    monkeypatch.setattr(doctor_mod, "_query_adb_version_raw", lambda *a: calls.append(a) or "version")
    assert doctor_mod._trusted_server_version(Path("untrusted/adb"), Path("selected/adb")) is None
    assert calls == []
    assert doctor_mod._trusted_server_version(Path("selected/adb"), Path("selected/adb")) == "version"
    assert len(calls) == 1


def test_failed_listener_probe_cannot_certify_idle_port(monkeypatch, tmp_path):
    def failed(args, **kwargs):
        assert args[0] == "netstat"
        return subprocess.CompletedProcess(args, 1, "", "denied")

    monkeypatch.setattr(doctor_mod.subprocess, "run", failed)
    state = doctor_mod._probe_server_5037("windows", tmp_path)
    assert not state.listening and state.probe_error
    with pytest.raises(doctor_mod.UnsafeAdbServerError):
        doctor_mod.preflight_adb_server(Path("adb"), ["devices"], environ={}, state=state)


def test_scrcpy_cli_dry_run_needs_no_confirmation_or_network(monkeypatch, tmp_path):
    from adapt_adb import scrcpy_install as si

    monkeypatch.setattr(si.shutil, "which", lambda _: None)
    monkeypatch.setattr(si, "get_default_scrcpy_root", lambda *a: tmp_path / "managed")
    monkeypatch.setattr(si, "_open_release_asset", lambda *a: pytest.fail("Unexpected download"))
    result = CliRunner().invoke(app, ["tools", "install", "scrcpy", "--dry-run", "--json"])
    assert result.exit_code == 0 and result.stderr == ""
    data = json.loads(result.stdout)
    assert data["status"] == "dry_run" and data["downloads"] is False and data["writes"] is False
    assert not (tmp_path / "managed").exists()


def test_scrcpy_cli_json_install_requires_explicit_yes(monkeypatch, tmp_path):
    from adapt_adb import scrcpy_install as si

    monkeypatch.setattr(si.shutil, "which", lambda _: None)
    monkeypatch.setattr(si, "get_default_scrcpy_root", lambda *a: tmp_path / "managed")
    monkeypatch.setattr(si, "_open_release_asset", lambda *a: pytest.fail("Unexpected download"))
    result = CliRunner().invoke(app, ["tools", "install", "scrcpy", "--json"])
    assert result.exit_code == 2 and result.stderr == ""
    assert json.loads(result.stdout)["status"] == "refused"
    assert not (tmp_path / "managed").exists()


def test_scrcpy_install_prompt_describes_source_destination_and_licenses(tmp_path):
    from adapt_adb import scrcpy_install as si
    prompts = []

    def decline(message):
        prompts.append(message)
        return False

    report = si.install_scrcpy(platform_name="win32", machine="AMD64", install_root=tmp_path / "managed", which=lambda _: None, confirm=decline)
    assert report["status"] == "cancelled" and not (tmp_path / "managed").exists()
    assert "Genymobile" in prompts[0] and "Apache-2.0" in prompts[0] and "separate licenses" in prompts[0]
    assert report["target_dir"] in prompts[0] and "https://github.com/Genymobile/scrcpy/releases/download/" in prompts[0]
