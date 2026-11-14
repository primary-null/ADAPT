"""Host fixes in the CLI: root/elevated warning, udev instructions, MCP import errors.

Offline only: privilege checks, subprocess and imports are injected or monkeypatched.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

import adapt_adb.cli as cli_mod
import adapt_adb.doctor as doctor_mod
from adapt_adb.cli import app
from adapt_adb.doctor import DoctorEnv, ROOT_WARNING, ELEVATED_WARNING, Server5037State, build_next_actions
from adapt_adb.hints import find_udev_rules_file

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_server_preflight(monkeypatch):
    monkeypatch.setattr("adapt_adb.cli.preflight_adb_server", lambda *a, **k: None)


def _healthy_env(tmp_path: Path) -> DoctorEnv:
    home = tmp_path / "Users" / "dev"
    pt_dir = home / "AppData" / "Local" / "Android" / "platform-tools"
    adb_exe = pt_dir / "adb.exe"
    return DoctorEnv(
        host_os="windows", arch="AMD64", uid_is_root=False, home_dir=home,
        environ={"LOCALAPPDATA": str(home / "AppData" / "Local"), "PATH": str(pt_dir)},
        which_adb=adb_exe, which_set=True, path_executables=[adb_exe], existing_paths={adb_exe, pt_dir},
        source_properties={pt_dir / "source.properties": "Pkg.Revision=35.0.2\n"},
        version_outputs={adb_exe: "Version 35.0.2-12147458\n"},
        server_state=Server5037State(listening=False), windows_pnp_devices=[],
        devices_l_output="List of devices attached\n\n",
    )


# ---------------------------------------------------------------------------
# Item 3: root / Administrator warning
# ---------------------------------------------------------------------------

@pytest.fixture
def as_root(monkeypatch):
    monkeypatch.setattr(cli_mod, "_privilege_warning_text", lambda: ROOT_WARNING)


def test_root_warning_is_printed_once_on_stderr_and_keeps_the_exit_code(as_root, monkeypatch):
    monkeypatch.setattr("adapt_adb.cli.resolve_adb", lambda: None)
    result = runner.invoke(app, ["devices"])
    assert result.exit_code == 1
    assert result.stderr.count("Warning: ADAPT is running as root.") == 1
    assert "Run ADAPT as your normal user, without sudo." in result.stderr
    assert "Warning:" not in result.stdout


@pytest.mark.parametrize("argv", [["adb", "version"], ["pair", "bad"], ["connect", "bad"], ["dev-options", "--help"]])
def test_root_warning_covers_adb_commands_and_device_groups(as_root, monkeypatch, argv):
    monkeypatch.setattr("adapt_adb.cli.resolve_adb", lambda: None)
    result = runner.invoke(app, argv)
    assert result.stderr.count("Warning: ADAPT is running as root.") == 1


def test_root_warning_never_appears_in_json_mode(as_root, tmp_path, monkeypatch):
    env = _healthy_env(tmp_path)
    monkeypatch.setattr(cli_mod, "_ACTIVE_DOCTOR_ENV", env)
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.stderr == ""
    json.loads(result.stdout)
    assert result.exit_code == 0


def test_root_warning_skips_commands_that_never_touch_adb(as_root):
    for argv in (["explain", "--list"], ["tips", "status"]):
        assert "Warning:" not in runner.invoke(app, argv).stderr


def test_doctor_human_run_has_one_stderr_warning_and_one_report_line(as_root, tmp_path, monkeypatch):
    env = _healthy_env(tmp_path)
    env.host_os, env.uid_is_root = "debian", True
    env.debian_usb_state = doctor_mod.DebianUsbState(rule_files=["x"])
    monkeypatch.setattr(cli_mod, "_ACTIVE_DOCTOR_ENV", env)
    result = runner.invoke(app, ["doctor"])
    assert result.stderr.count("Warning: ADAPT is running as root.") == 1
    assert result.stdout.count("Warning: ADAPT is running as root.") == 1


def test_a_failing_privilege_check_never_breaks_the_command(monkeypatch):
    def boom():
        raise RuntimeError("probe failed")

    monkeypatch.setattr(cli_mod, "_privilege_warning_text", boom)
    monkeypatch.setattr("adapt_adb.cli.resolve_adb", lambda: None)
    result = runner.invoke(app, ["devices"])
    assert result.exit_code == 1 and "Warning:" not in result.stderr


def test_privilege_check_detects_posix_root(monkeypatch):
    monkeypatch.setattr(cli_mod.sys, "platform", "linux")
    monkeypatch.setattr(cli_mod.os, "geteuid", lambda: 0, raising=False)
    assert cli_mod._privilege_warning_text() == ROOT_WARNING
    monkeypatch.setattr(cli_mod.os, "geteuid", lambda: 1000, raising=False)
    assert cli_mod._privilege_warning_text() is None


def test_privilege_check_detects_an_elevated_windows_session(monkeypatch):
    monkeypatch.setattr(cli_mod.sys, "platform", "win32")
    monkeypatch.setattr(cli_mod, "_is_windows_admin", lambda: True)
    assert cli_mod._privilege_warning_text() == ELEVATED_WARNING
    monkeypatch.setattr(cli_mod, "_is_windows_admin", lambda: False)
    assert cli_mod._privilege_warning_text() is None


def test_warning_texts_say_what_happens_and_what_to_do():
    assert "root-owned" in ROOT_WARNING and "normal user" in ROOT_WARNING
    assert "non-elevated" in ELEVATED_WARNING and "cannot share" in ELEVATED_WARNING
    for text in (ROOT_WARNING, ELEVATED_WARNING):
        assert "kill-server" not in text and "sudo adb" not in text


# ---------------------------------------------------------------------------
# Item 5: printed udev instructions use the real rules path
# ---------------------------------------------------------------------------

def test_cli_resolver_is_the_single_hints_resolver():
    assert cli_mod.resolve_udev_rules_path() == find_udev_rules_file()
    assert cli_mod._resolve_udev_rules_path() == find_udev_rules_file()


@pytest.mark.parametrize("host_os", ["debian", "linux"])
def test_next_action_udev_label_uses_the_resolved_path_and_relogin(host_os):
    report = {"host": {"os": host_os, "supported": True}, "toolchain": {"adb_path": "/x/adb"}, "checks": [], "exit_code": 0}
    label = next(a["label"] for a in build_next_actions(report) if a["id"] == "udev")
    assert str(find_udev_rules_file()) in label
    assert "packaging/linux/51-android.rules /etc" not in label
    assert "log out and back in" in label
    assert ("usermod -aG plugdev" in label) == (host_os == "debian")


def test_setup_next_steps_print_the_resolved_path(tmp_path, monkeypatch):
    env = _healthy_env(tmp_path)
    monkeypatch.setattr(cli_mod, "_ACTIVE_DOCTOR_ENV", env)
    monkeypatch.setattr(cli_mod, "run_setup_workflow", lambda *a, **k: {
        "host": {"os": "linux", "arch": "x86_64", "supported": True},
        "toolchain": {"adb_path": "/x/adb", "platform_tools_version": "35.0.2", "origin": "google_official"},
        "checks": [], "devices": {"total": 0, "by_state": {}}, "exit_code": 0, "status": "ok",
        "setup": {"action": "noop_already_healthy"},
    })
    monkeypatch.setattr(cli_mod, "render_doctor_human", lambda *a, **k: "report")
    result = runner.invoke(app, ["setup"])
    assert str(find_udev_rules_file()) in result.stdout
    assert "sudo cp packaging/linux" not in result.stdout
    assert "log out and back in" in result.stdout
    assert "usermod -aG plugdev" not in result.stdout  # non-Debian: no plugdev assumption


def _menu_report(host_os):
    return {"host": {"os": host_os, "arch": "x86_64", "supported": True}, "toolchain": {"adb_path": "/x/adb"},
            "checks": [], "devices": {"total": 0, "by_state": {}}, "exit_code": 0, "status": "ok"}


@pytest.mark.parametrize("host_os, group, expected_calls", [
    ("debian", False, 4), ("linux", True, 4), ("linux", False, 3),
])
def test_menu_udev_action_joins_plugdev_only_where_it_exists(monkeypatch, capsys, host_os, group, expected_calls):
    import subprocess as sp

    monkeypatch.setattr(cli_mod, "_group_exists", lambda name: group)
    monkeypatch.setattr(cli_mod, "run_doctor_checks", lambda *a, **k: _menu_report(host_os))
    monkeypatch.setattr(cli_mod, "render_doctor_human", lambda *a, **k: "ok")
    monkeypatch.setattr(sys, "stdin", io.StringIO("udev\nq\n"))
    run_mock = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr(sp, "run", run_mock)
    cli_mod._run_next_menu(_menu_report(host_os), show_fix=False)
    assert run_mock.call_count == expected_calls
    first = run_mock.call_args_list[0].args[0]
    assert first[:3] == ["sudo", "cp", "--"] and first[3] == str(find_udev_rules_file().resolve())
    out = capsys.readouterr().out
    assert "log out and back in" in out
    if expected_calls == 3:
        assert "no 'plugdev' group" in out


def test_menu_udev_action_reports_a_missing_rules_file(monkeypatch, capsys):
    monkeypatch.setattr(cli_mod, "find_udev_rules_file", lambda: None)
    monkeypatch.setattr(cli_mod, "run_doctor_checks", lambda *a, **k: _menu_report("debian"))
    monkeypatch.setattr(cli_mod, "render_doctor_human", lambda *a, **k: "ok")
    monkeypatch.setattr(sys, "stdin", io.StringIO("udev\nq\n"))
    cli_mod._run_next_menu(_menu_report("debian"), show_fix=False)
    assert "51-android.rules not found" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Item 6: `adapt mcp` import failures
# ---------------------------------------------------------------------------

def _raise_from_run_stdio(monkeypatch, exc):
    import adapt_adb.mcp.server as server

    def run_stdio(*a, **k):
        raise exc

    monkeypatch.setattr(server, "run_stdio", run_stdio)


def test_mcp_sdk_not_installed_message(monkeypatch):
    _raise_from_run_stdio(monkeypatch, ModuleNotFoundError("No module named 'mcp'", name="mcp"))
    result = runner.invoke(app, ["mcp"])
    assert result.exit_code == 2
    assert result.stderr.startswith("Error: Python MCP SDK is not installed (install with 'adapt-adb[mcp]').\n")
    assert "lands in v1" not in result.stderr
    assert "mcp.sdk_missing" in result.stderr
    assert result.stdout == ""


def test_mcp_sdk_installed_but_broken_names_the_module_not_a_path(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_mod, "_mcp_sdk_installed", lambda: True)
    exc = ModuleNotFoundError("No module named 'pydantic_core'", name="pydantic_core", path=str(tmp_path / "SECRET"))
    _raise_from_run_stdio(monkeypatch, exc)
    result = runner.invoke(app, ["mcp"])
    assert result.exit_code == 2
    first = result.stderr.splitlines()[0]
    assert first == ("Error: Python MCP SDK is installed but could not be imported (failing module: pydantic_core). "
                     "Reinstall 'adapt-adb[mcp]' in this Python environment.")
    assert "SECRET" not in result.stderr and "not installed" not in result.stderr
    assert "mcp.sdk_missing" in result.stderr


def test_mcp_sdk_absent_is_detected_even_without_a_module_name(monkeypatch):
    monkeypatch.setattr(cli_mod, "_mcp_sdk_installed", lambda: False)
    _raise_from_run_stdio(monkeypatch, ImportError("cannot import name"))
    assert "is not installed" in runner.invoke(app, ["mcp"]).stderr


def test_mcp_sdk_installed_check_handles_odd_module_state(monkeypatch):
    monkeypatch.setitem(sys.modules, "mcp", None)
    assert cli_mod._mcp_sdk_installed() is False
