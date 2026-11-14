"""Failure guidance at the Phase 1 CLI sites, `adapt explain`, help integration and the
unexpected-exception handler.

Every site keeps its first error line byte-identical and its exit code; guidance follows on
stderr. CliRunner is not a terminal, so the default is the one-line short form; "fake TTY"
tests patch the stderr TTY check. Offline: adb, preflight and downloads are faked.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

import adapt_adb.cli as cli
from adapt_adb import failure_report as fr
from adapt_adb import help_system as help_mod
from adapt_adb.cli import app
from adapt_adb.doctor import DoctorEnv, Server5037State, UnsafeAdbServerError, render_doctor_human
from adapt_adb.console import render_console_report

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("ADAPT_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("NO_COLOR", "1")
    for name in ("ADAPT_TIPS", "ADAPT_FAILURE_GUIDANCE", "ADAPT_DEBUG", "ADAPT_ADB", "ADB_PATH", "ANDROID_SERIAL",
                 "ADB_SERVER_SOCKET", "ANDROID_ADB_SERVER_PORT", "ADB_SERVER_PORT"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def tty(monkeypatch):
    monkeypatch.setattr(fr, "_stderr_isatty", lambda: True)


@pytest.fixture
def fake_adb(monkeypatch):
    monkeypatch.setattr(cli, "resolve_adb", lambda: Path("adb"))
    monkeypatch.setattr(cli, "preflight_adb_server", lambda *a, **k: None)


def _more_lines(stderr: str) -> list[str]:
    return [line for line in stderr.splitlines() if line.startswith("More: ")]


def _short(result, failure_id: str) -> None:
    assert _more_lines(result.stderr) == [f"More: adapt explain {failure_id}"], result.stderr
    assert "Why:" not in result.stderr


def _full(result, failure_id: str) -> None:
    assert "\nWhy: " in result.stderr and "\nTry:\n" in result.stderr, result.stderr
    assert f"More: adapt explain {failure_id}" in result.stderr


def _refuse(failure_id: str, message: str = "Listening ADB server ownership/version is unverified or unsafe."):
    def refuse(*a, **k):
        raise fr.tagged(UnsafeAdbServerError(message), failure_id)
    return refuse


# ---------------------------------------------------------------------------
# adb missing, preflight, timeouts (devices / adb / pair / connect)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [["devices"], ["adb", "--", "devices"], ["pair", "192.0.2.1:37000"], ["connect", "192.0.2.1:5555"]])
def test_adb_not_found_keeps_lines_and_adds_pointer(argv, monkeypatch):
    monkeypatch.setattr(cli, "resolve_adb", lambda: None)
    result = runner.invoke(app, argv)
    assert result.exit_code == 1
    assert result.stderr.startswith("Error: adb binary not found.\nRun 'adapt setup' to install adb.\n")
    _short(result, "adb.not_found")


def test_adb_not_found_full_block_on_terminal(monkeypatch, tty):
    monkeypatch.setattr(cli, "resolve_adb", lambda: None)
    result = runner.invoke(app, ["devices"])
    assert result.exit_code == 1
    assert result.stderr.startswith("Error: adb binary not found.\nRun 'adapt setup' to install adb.\n\nWhy: ")
    _full(result, "adb.not_found")
    assert "adapt setup --dry-run   [read-only]" in result.stderr
    assert result.stdout == ""


def test_adb_not_found_promotes_override_cause(monkeypatch, tty):
    monkeypatch.setattr(cli, "resolve_adb", lambda: None)
    monkeypatch.setenv("ADAPT_ADB", "SENTINEL_NOT_A_FILE")
    result = runner.invoke(app, ["devices"])
    why = result.stderr.split("Possible causes, most likely first:\n", 1)[1]
    assert why.splitlines()[0].startswith("  - ADAPT_ADB or ADB_PATH is set")
    assert "SENTINEL_NOT_A_FILE" not in result.stderr


@pytest.mark.parametrize("failure_id", ["adb.server.foreign_owner", "adb.server.unverified", "adb.server.probe_failed",
                                        "adb.server.version_mismatch"])
def test_devices_preflight_keeps_reason_and_exit_3(failure_id, monkeypatch):
    monkeypatch.setattr(cli, "resolve_adb", lambda: Path("adb"))
    monkeypatch.setattr(cli, "preflight_adb_server", _refuse(failure_id))
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: pytest.fail("adb spawned"))
    result = runner.invoke(app, ["devices"])
    assert result.exit_code == 3
    assert result.stderr.splitlines()[0] == "Error: Listening ADB server ownership/version is unverified or unsafe."
    _short(result, failure_id)


def test_foreign_owner_full_block_never_suggests_killing(monkeypatch, tty):
    monkeypatch.setattr(cli, "resolve_adb", lambda: Path("adb"))
    monkeypatch.setattr(cli, "preflight_adb_server", _refuse("adb.server.foreign_owner"))
    result = runner.invoke(app, ["devices"])
    assert result.exit_code == 3
    _full(result, "adb.server.foreign_owner")
    try_part = result.stderr.split("\nTry:\n", 1)[1].split("  Avoid:", 1)[0]
    assert "kill" not in try_part.lower() and "sudo" not in try_part.lower()


def test_real_preflight_endpoint_env_is_named_without_value(monkeypatch, tty):
    monkeypatch.setattr(cli, "resolve_adb", lambda: Path("adb"))
    monkeypatch.setenv("ADB_SERVER_SOCKET", "tcp:SENTINELHOST:5038")
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: pytest.fail("adb spawned"))
    result = runner.invoke(app, ["devices"])
    assert result.exit_code == 3
    assert result.stderr.splitlines()[0] == "Error: Selected ADB server endpoint cannot be verified safely; refusing to contact it."
    _full(result, "adb.server.endpoint_unverified")
    causes = result.stderr.split("Possible causes, most likely first:\n", 1)[1]
    assert causes.splitlines()[0].startswith("  - An environment variable selects another server: ADB_SERVER_SOCKET")
    assert "SENTINELHOST" not in result.stderr


def test_adb_server_option_on_command_line_is_named(monkeypatch, tty):
    monkeypatch.setattr(cli, "resolve_adb", lambda: Path("adb"))
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: pytest.fail("adb spawned"))
    result = runner.invoke(app, ["adb", "--", "-H", "192.0.2.9", "devices"])
    assert result.exit_code == 3
    causes = result.stderr.split("Possible causes, most likely first:\n", 1)[1]
    assert causes.splitlines()[0].startswith("  - The command line passes a server option (-H, -P or -L)")
    assert "192.0.2.9" not in result.stderr


@pytest.mark.parametrize("exc,first,failure_id", [
    (subprocess.TimeoutExpired(["adb"], 10), "Error: ADB device listing timed out.", "adb.timeout"),
    (OSError("denied"), "Error: Could not execute ADB.", "adb.exec_failed"),
])
def test_devices_timeout_and_exec_failure(exc, first, failure_id, monkeypatch, fake_adb):
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(exc))
    result = runner.invoke(app, ["devices"])
    assert result.exit_code == 1 and result.stderr.splitlines()[0] == first
    _short(result, failure_id)


# ---------------------------------------------------------------------------
# adapt adb: input, enumeration, target states
# ---------------------------------------------------------------------------

def test_adb_missing_command_and_refused_args(fake_adb):
    result = runner.invoke(app, ["adb", "--", "-s", "X"])
    assert result.exit_code == 1 and result.stderr.splitlines()[0] == "Error: Missing ADB command"
    _short(result, "input.adb_command_missing")
    result = runner.invoke(app, ["adb", "--", "kill-server"])
    assert result.exit_code == 1 and result.stderr.startswith("Error: ")
    _short(result, "input.adb_args_refused")


def test_adb_enumeration_failure(monkeypatch, fake_adb):
    monkeypatch.setattr(cli, "get_connected_devices", lambda *a, **k: (_ for _ in ()).throw(cli.AdbProbeError("x")))
    result = runner.invoke(app, ["adb", "--", "shell", "true"])
    assert result.exit_code == 1
    assert result.stderr.splitlines()[0] == "Error: ADB device enumeration failed; target state is unknown."
    _short(result, "adb.enumeration_failed")


@pytest.mark.parametrize("state,failure_id", [("unauthorized", "device.unauthorized"), ("offline", "device.offline"),
                                              ("no permissions", "device.no_permissions"), ("recovery", "device.not_ready")])
def test_explicit_target_state_picks_specific_guide(state, failure_id, monkeypatch, fake_adb):
    monkeypatch.setattr(cli, "get_connected_devices", lambda *a, **k: [{"serial": "SENTINELSERIAL", "state": state}])
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: pytest.fail("adb spawned"))
    result = runner.invoke(app, ["adb", "--", "-s", "SENTINELSERIAL", "shell", "getprop"])
    assert result.exit_code == 1
    assert result.stderr.splitlines()[0] == "Error: Explicit target is unavailable or not in state 'device'."
    _short(result, failure_id)


def test_unauthorized_target_full_block_without_serial(monkeypatch, fake_adb, tty):
    monkeypatch.setattr(cli, "get_connected_devices", lambda *a, **k: [{"serial": "SENTINELSERIAL", "state": "unauthorized"}])
    result = runner.invoke(app, ["adb", "--", "-s", "SENTINELSERIAL", "shell", "getprop"])
    assert result.exit_code == 1
    assert result.stderr.startswith("Error: Explicit target is unavailable or not in state 'device'.\n\nWhy: ADB can see this device, but it is in state 'unauthorized'")
    _full(result, "device.unauthorized")
    assert "[on the phone]" in result.stderr and "SENTINELSERIAL" not in result.stderr


def test_missing_target_from_android_serial_is_called_out(monkeypatch, fake_adb, tty):
    monkeypatch.setenv("ANDROID_SERIAL", "SENTINELSTALE")
    monkeypatch.setattr(cli, "get_connected_devices", lambda *a, **k: [{"serial": "OTHER", "state": "device"}])
    result = runner.invoke(app, ["adb", "--", "shell", "true"])
    assert result.exit_code == 1
    _full(result, "device.not_found")
    causes = result.stderr.split("Possible causes, most likely first:\n", 1)[1]
    assert causes.splitlines()[0].startswith("  - The target came from the ANDROID_SERIAL environment variable")
    assert "SENTINELSTALE" not in result.stderr


@pytest.mark.parametrize("devices,failure_id", [
    ([], "device.none_ready"),
    ([{"serial": "A", "state": "unauthorized"}], "device.unauthorized"),
    ([{"serial": "A", "state": "unauthorized"}, {"serial": "B", "state": "unauthorized"}], "device.unauthorized"),
    ([{"serial": "A", "state": "unauthorized"}, {"serial": "B", "state": "offline"}], "device.none_ready"),
    ([{"serial": "A", "state": "offline"}], "device.offline"),
])
def test_no_ready_device_uses_states_in_memory(devices, failure_id, monkeypatch, fake_adb):
    monkeypatch.setattr(cli, "get_connected_devices", lambda *a, **k: list(devices))
    result = runner.invoke(app, ["adb", "--", "shell", "true"])
    assert result.exit_code == 1
    assert result.stderr.splitlines()[0] == "Error: No ready Android devices connected (state 'device'). Run 'adapt doctor' to diagnose."
    _short(result, failure_id)


def test_mixed_states_promote_unauthorized_cause(monkeypatch, fake_adb, tty):
    monkeypatch.setattr(cli, "get_connected_devices", lambda *a, **k: [{"serial": "A", "state": "offline"}, {"serial": "B", "state": "unauthorized"}])
    result = runner.invoke(app, ["adb", "--", "shell", "true"])
    causes = result.stderr.split("Possible causes, most likely first:\n", 1)[1]
    assert causes.splitlines()[0].startswith("  - A phone is connected but has not approved")


def test_multiple_devices_keeps_list_and_adds_guide(monkeypatch, fake_adb, tty):
    monkeypatch.setattr(cli, "get_connected_devices", lambda *a, **k: [{"serial": "A", "state": "device"}, {"serial": "B", "state": "device"}])
    result = runner.invoke(app, ["adb", "--", "shell", "true"])
    assert result.exit_code == 1
    lines = result.stderr.splitlines()
    assert lines[:4] == ["Error: Multiple devices connected (2). Specify a target device with -s or --serial.",
                         "Connected devices:", "  A\t(device)", "  B\t(device)"]
    _full(result, "device.multiple")
    assert "ADB lists 2 ready devices" in result.stderr


@pytest.mark.parametrize("exc,first,failure_id", [
    (subprocess.TimeoutExpired(["adb"], 60), "Error: ADB command timed out.", "adb.timeout"),
    (OSError("x"), "Error: Could not execute ADB.", "adb.exec_failed"),
])
def test_adb_passthrough_timeout_and_exec(exc, first, failure_id, monkeypatch, fake_adb):
    monkeypatch.setattr(cli, "get_connected_devices", lambda *a, **k: [{"serial": "A", "state": "device"}])
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(exc))
    result = runner.invoke(app, ["adb", "--", "shell", "true"])
    assert result.exit_code == 1 and result.stderr.splitlines()[0] == first
    _short(result, failure_id)


def test_successful_adb_command_prints_no_guidance(monkeypatch, fake_adb):
    monkeypatch.setattr(cli, "get_connected_devices", lambda *a, **k: [{"serial": "A", "state": "device"}])
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0))
    result = runner.invoke(app, ["adb", "--", "shell", "true"])
    assert result.exit_code == 0 and "More:" not in result.stderr


# ---------------------------------------------------------------------------
# Wireless pair / connect
# ---------------------------------------------------------------------------

def test_pair_invalid_endpoint_and_missing_code(fake_adb, monkeypatch):
    monkeypatch.setattr(cli, "_is_stdin_tty", lambda: False)
    result = runner.invoke(app, ["pair", "not an endpoint"])
    assert result.exit_code == 1 and result.stderr.splitlines()[0] == "Error: pairing requires a valid host:port endpoint."
    _short(result, "wireless.endpoint_invalid")
    result = runner.invoke(app, ["pair", "192.0.2.1:37000"])
    assert result.exit_code == 1
    assert result.stderr.splitlines()[0] == "Error: pairing requires a valid endpoint and pairing code."
    _short(result, "wireless.pair_code_missing")


def test_pair_and_connect_preflight_keep_line_but_explain_reason(monkeypatch, tty):
    monkeypatch.setattr(cli, "resolve_adb", lambda: Path("adb"))
    monkeypatch.setattr(cli, "preflight_adb_server", _refuse("adb.server.unverified"))
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: pytest.fail("adb spawned"))
    for argv in (["pair", "192.0.2.1:37000", "123456"], ["connect", "192.0.2.1:41000"]):
        result = runner.invoke(app, argv)
        assert result.exit_code == 3
        assert result.stderr.startswith("Error: ADB server preflight failed.\n\nWhy: An ADB server is already running on port 5037")
        assert "adb_server_5037" in result.stderr and "adapt doctor --json" in result.stderr


@pytest.mark.parametrize("cmd,first,failure_id", [
    ("pair", "Error: ADB pairing timed out.", "wireless.pair_timeout"),
    ("connect", "Error: ADB connect timed out.", "wireless.connect_timeout"),
])
def test_wireless_timeouts(cmd, first, failure_id, monkeypatch, fake_adb):
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired(["adb"], 30)))
    argv = [cmd, "192.0.2.1:37000"] + (["123456"] if cmd == "pair" else [])
    result = runner.invoke(app, argv)
    assert result.exit_code == 1 and result.stderr.splitlines()[0] == first
    _short(result, failure_id)


@pytest.mark.parametrize("cmd,first", [("pair", "Error: Could not execute ADB pairing."), ("connect", "Error: Could not execute ADB connect.")])
def test_wireless_exec_failure(cmd, first, monkeypatch, fake_adb):
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    argv = [cmd, "192.0.2.1:37000"] + (["123456"] if cmd == "pair" else [])
    result = runner.invoke(app, argv)
    assert result.exit_code == 1 and result.stderr.splitlines()[0] == first
    _short(result, "adb.exec_failed")


def test_connect_failure_relays_adb_text_then_classifies_symptom(monkeypatch, fake_adb, tty):
    out = "failed to connect to '203.0.113.77:41000': Connection refused\n"
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 1, out, ""))
    result = runner.invoke(app, ["connect", "203.0.113.77:41000"])
    assert result.exit_code == 1
    assert result.stdout == out  # adb's own text relayed unchanged
    _full(result, "wireless.connect_failed")
    assert "203.0.113.77" not in result.stderr  # never re-quoted
    causes = result.stderr.split("Possible causes, most likely first:\n", 1)[1]
    assert causes.splitlines()[0].startswith("  - The port is not the current connection port")


def test_pair_failure_with_wrong_code_symptom(monkeypatch, fake_adb, tty):
    err = "Failed: Wrong password or connection was dropped.\n"
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", err))
    result = runner.invoke(app, ["pair", "192.0.2.1:37000", "123456"])
    assert result.exit_code == 1
    assert result.stderr.startswith(err)
    _full(result, "wireless.pair_failed")
    causes = result.stderr.split("Possible causes, most likely first:\n", 1)[1]
    assert causes.splitlines()[0].startswith("  - ADB reported a wrong pairing code")
    assert "123456" not in result.stderr


def test_successful_connect_prints_no_guidance(monkeypatch, fake_adb):
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "connected to 192.0.2.1:41000\n", ""))
    result = runner.invoke(app, ["connect", "192.0.2.1:41000"])
    assert result.exit_code == 0 and "More:" not in result.stderr


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

def _windows_env(tmp_path: Path, *, server: Server5037State | None = None) -> DoctorEnv:
    home = tmp_path / "Users" / "dev"
    return DoctorEnv(
        host_os="windows", arch="AMD64", uid_is_root=False, home_dir=home,
        environ={"LOCALAPPDATA": str(home / "AppData" / "Local"), "PATH": r"C:\Windows\System32"},
        which_adb=None, which_set=True, path_executables=[], existing_paths=set(), source_properties={},
        version_outputs={}, server_state=server or Server5037State(listening=False), windows_pnp_devices=[],
        devices_l_output="List of devices attached\n\n",
    )


def _digest_unavailable(url, target_dir, expected_sha256=None):
    raise fr.tagged(RuntimeError("SHA-256 or SHA-1 digest unavailable for platform-tools download."), "setup.digest_unavailable")


def test_setup_download_failure_is_classified_human_and_json(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_ACTIVE_DOCTOR_ENV", _windows_env(tmp_path))
    monkeypatch.setattr(cli, "download_and_extract_platform_tools", _digest_unavailable)
    result = runner.invoke(app, ["setup", "--yes"])
    assert result.exit_code == 1
    assert result.stderr.splitlines()[0] == "Error: SHA-256 or SHA-1 digest unavailable for platform-tools download."
    _short(result, "setup.digest_unavailable")
    result = runner.invoke(app, ["setup", "--yes", "--json"])
    assert result.exit_code == 1 and result.stderr == ""
    report = json.loads(result.stdout)
    assert report["setup"]["action"] == "failed" and report["failure"]["id"] == "setup.digest_unavailable"
    assert report["failure"]["schema"] == 1 and report["failure"]["explain"] == "adapt explain setup.digest_unavailable"


def test_setup_untagged_downloader_error_falls_back_to_setup_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_ACTIVE_DOCTOR_ENV", _windows_env(tmp_path))
    monkeypatch.setattr(cli, "download_and_extract_platform_tools", lambda url, target_dir, expected_sha256=None: (_ for _ in ()).throw(ValueError("boom")))
    result = runner.invoke(app, ["setup", "--yes", "--json"])
    assert json.loads(result.stdout)["failure"]["id"] == "setup.failed"


def test_setup_needs_confirmation_human_and_json(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_ACTIVE_DOCTOR_ENV", _windows_env(tmp_path))
    monkeypatch.setattr(cli, "download_and_extract_platform_tools", lambda *a, **k: pytest.fail("downloaded"))
    result = runner.invoke(app, ["setup"])
    assert result.exit_code == 1
    assert "pass --yes" in result.stderr
    assert result.stderr.splitlines()[0] == "Aborted setup: Confirmation is unavailable; pass --yes to install."
    _short(result, "setup.needs_confirmation")
    result = runner.invoke(app, ["setup", "--json"])
    assert result.stderr == "" and json.loads(result.stdout)["failure"]["id"] == "setup.needs_confirmation"


def test_setup_unsafe_server_names_specific_guide(tmp_path, monkeypatch):
    server = Server5037State(listening=True, owner="other", owner_matches_current_user=False, pid=4)
    monkeypatch.setattr(cli, "_ACTIVE_DOCTOR_ENV", _windows_env(tmp_path, server=server))
    result = runner.invoke(app, ["setup", "--yes"])
    assert result.exit_code == 3
    assert result.stderr.splitlines()[0] == "Aborted setup: Port 5037 has an unsafe or mismatched shared ADB server (exit code 3)."
    _short(result, "adb.server.foreign_owner")
    data = json.loads(runner.invoke(app, ["setup", "--yes", "--json"]).stdout)
    assert data["setup"]["action"] == "aborted_unsafe_server" and data["failure"]["id"] == "adb.server.foreign_owner"


def test_setup_exception_json_payload_keeps_keys_and_adds_failure(monkeypatch):
    monkeypatch.setattr(cli, "run_setup_workflow", lambda *a, **k: (_ for _ in ()).throw(PermissionError("C:/SENTINELPATH")))
    result = runner.invoke(app, ["setup", "--json"])
    assert result.exit_code == 1 and result.stderr == ""
    payload = json.loads(result.stdout)
    assert {k: payload[k] for k in ("schema_version", "command", "exit_code", "status", "error")} == {
        "schema_version": "1", "command": "setup", "exit_code": 1, "status": "missing_toolchain", "error": "C:/SENTINELPATH"}
    assert payload["failure"]["id"] == "host.os_error"
    assert "SENTINELPATH" not in json.dumps(payload["failure"])
    human = runner.invoke(app, ["setup"])
    assert human.stderr.splitlines()[0] == "Error: C:/SENTINELPATH"
    _short(human, "host.os_error")


def test_setup_success_tells_user_to_reopen_terminals(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_ACTIVE_DOCTOR_ENV", _windows_env(tmp_path))
    monkeypatch.setattr(cli, "download_and_extract_platform_tools", lambda url, target_dir, expected_sha256=None: "a" * 64)
    result = runner.invoke(app, ["setup", "--yes"])
    assert "reopen any terminal that was already open" in result.stdout
    assert "More:" not in result.stderr
    monkeypatch.setattr(cli, "_ACTIVE_DOCTOR_ENV", _windows_env(tmp_path / "second"))
    data = json.loads(runner.invoke(app, ["setup", "--yes", "--json"]).stdout)
    assert "failure" not in data and data["setup"]["path_updated"] is True
    monkeypatch.setattr(cli, "_ACTIVE_DOCTOR_ENV", _windows_env(tmp_path / "third"))
    no_path = runner.invoke(app, ["setup", "--yes", "--no-path"])
    assert "reopen any terminal" not in no_path.stdout


def test_capturing_downloader_keeps_signature_for_legacy_downloaders():
    import inspect

    def legacy(url, target_dir):
        return "x"
    cli_downloader = cli.download_and_extract_platform_tools
    try:
        cli.download_and_extract_platform_tools = legacy
        wrapped = cli._capturing_downloader({})
    finally:
        cli.download_and_extract_platform_tools = cli_downloader
    assert list(inspect.signature(wrapped).parameters) == ["url", "target_dir"]


# ---------------------------------------------------------------------------
# MCP SDK missing
# ---------------------------------------------------------------------------

def test_mcp_sdk_missing_keeps_message(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "adapt_adb.mcp.server":
            raise ImportError("no mcp")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    result = runner.invoke(app, ["mcp"])
    assert result.exit_code == 2
    assert result.stderr.startswith("Error: Python MCP SDK is ")  # installed-or-not depends on this environment
    assert "lands in v1" not in result.stderr
    _short(result, "mcp.sdk_missing")


# ---------------------------------------------------------------------------
# Suppression controls at the CLI
# ---------------------------------------------------------------------------

def test_no_tips_and_env_controls(monkeypatch, tty):
    monkeypatch.setattr(cli, "resolve_adb", lambda: None)
    _short(runner.invoke(app, ["--no-tips", "devices"]), "adb.not_found")
    monkeypatch.setenv("ADAPT_TIPS", "0")
    _short(runner.invoke(app, ["devices"]), "adb.not_found")
    monkeypatch.delenv("ADAPT_TIPS")
    monkeypatch.setenv("ADAPT_FAILURE_GUIDANCE", "off")
    result = runner.invoke(app, ["devices"])
    assert result.stderr == "Error: adb binary not found.\nRun 'adapt setup' to install adb.\n"
    monkeypatch.setenv("ADAPT_FAILURE_GUIDANCE", "full")
    _full(runner.invoke(app, ["--no-tips", "devices"]), "adb.not_found")
    assert not help_mod.preferences_path().exists()


# ---------------------------------------------------------------------------
# adapt explain and help-me
# ---------------------------------------------------------------------------

def test_explain_list_id_search_and_unknown():
    listed = runner.invoke(app, ["explain"])
    assert listed.exit_code == 0 and "adb.not_found" in listed.stdout and listed.stderr == ""
    assert runner.invoke(app, ["explain", "--list"]).stdout == listed.stdout
    one = runner.invoke(app, ["explain", "device.unauthorized"])
    assert one.exit_code == 0 and one.stdout.startswith("ADAPT / EXPLAIN / device.unauthorized")
    pasted = runner.invoke(app, ["explain", "error:", "failed", "to", "authenticate", "to", "192.0.2.1:37000"])
    assert pasted.exit_code == 0 and "wireless.pair_failed" in pasted.stdout and "wireless.connect_failed" in pasted.stdout
    keyword = runner.invoke(app, ["explain", "Allow USB debugging"])
    assert keyword.stdout.startswith("ADAPT / EXPLAIN / device.unauthorized")
    none = runner.invoke(app, ["explain", "qqqq", "zzzz"])
    assert none.exit_code == 2 and "adapt explain --list" in none.stderr and none.stdout == ""


def test_explain_json_shapes_keep_stderr_empty():
    data = json.loads(runner.invoke(app, ["explain", "--list", "--json"]).stdout)
    assert {"id", "summary"} == set(data["failures"][0])
    one = runner.invoke(app, ["explain", "adb.not_found", "--json"])
    assert one.stderr == "" and json.loads(one.stdout)["failure"]["id"] == "adb.not_found"
    full = json.loads(one.stdout)["failure"]
    assert {"variability", "diagnose", "commands", "aliases"} <= set(full)
    none = runner.invoke(app, ["explain", "qqqq", "--json"])
    assert none.exit_code == 2 and none.stderr == ""
    assert json.loads(none.stdout)["error"] == "unknown_failure"


def test_explain_is_listed_in_help_and_inventory():
    assert "explain" in runner.invoke(app, ["--help"]).stdout
    data = json.loads(runner.invoke(app, ["help-me", "--all", "--json"]).stdout)
    record = next(r for r in data["commands"] if r["command"] == "adapt explain")
    assert record["how_to"] and "adapt explain --list" in record["how_to"][1]


def test_help_me_accepts_failure_ids_and_lists_linked_failures():
    result = runner.invoke(app, ["help-me", "adb.not_found"])
    assert result.exit_code == 0 and result.stdout.startswith("ADAPT / EXPLAIN / adb.not_found")
    data = json.loads(runner.invoke(app, ["help-me", "adb.not_found", "--json"]).stdout)
    assert data["failure"]["id"] == "adb.not_found"
    topic = runner.invoke(app, ["help-me", "wireless"])
    assert "Common failures:" in topic.stdout and "wireless.connect_failed" in topic.stdout
    data = json.loads(runner.invoke(app, ["help-me", "devices", "--json"]).stdout)
    assert any(f["id"] == "device.unauthorized" for f in data["failures"])
    unknown = runner.invoke(app, ["help-me", "not-a-tool", "--json"])
    assert unknown.exit_code == 2 and unknown.stderr == ""


# ---------------------------------------------------------------------------
# Unexpected exceptions (design 2.7)
# ---------------------------------------------------------------------------

def test_unexpected_exception_human_block_no_traceback(monkeypatch, tty):
    monkeypatch.setattr(cli, "resolve_adb", lambda: (_ for _ in ()).throw(KeyError("C:/SENTINELPATH")))
    result = runner.invoke(app, ["devices"])
    assert result.exit_code == 1
    assert result.stderr.startswith("Error: ADAPT stopped unexpectedly while listing devices (KeyError).\n\nWhy: ADAPT stopped while listing devices")
    assert "Traceback" not in result.stderr and "SENTINELPATH" not in result.stderr
    assert "ADAPT_DEBUG=1" in result.stderr and result.stderr.rstrip().endswith("More: adapt explain --list")


def test_unexpected_exception_short_form_and_nested_activity(monkeypatch):
    monkeypatch.setattr(cli, "resolve_adb", lambda: (_ for _ in ()).throw(KeyError("x")))
    result = runner.invoke(app, ["devices"])
    assert result.exit_code == 1
    assert result.stderr.splitlines() == ["Error: ADAPT stopped unexpectedly while listing devices (KeyError).",
                                          "More: adapt explain internal.unclassified   |   ADAPT_DEBUG=1 shows technical details"]


def test_unexpected_exception_json_is_parseable_and_stderr_empty(monkeypatch):
    monkeypatch.setattr(fr, "search", lambda q: (_ for _ in ()).throw(KeyError("C:/SENTINELPATH")))
    result = runner.invoke(app, ["explain", "anything", "--json"])
    assert result.exit_code == 1 and result.stderr == ""
    data = json.loads(result.stdout)
    assert data["status"] == "error" and data["error"] == "internal_error" and data["exit_code"] == 1
    assert data["failure"]["id"] == "internal.unclassified" and "SENTINELPATH" not in result.stdout


def test_adapt_debug_restores_the_exception(monkeypatch):
    monkeypatch.setenv("ADAPT_DEBUG", "1")
    monkeypatch.setattr(cli, "resolve_adb", lambda: (_ for _ in ()).throw(KeyError("x")))
    result = runner.invoke(app, ["devices"])
    assert result.exit_code == 1 and isinstance(result.exception, KeyError)


def test_exits_usage_errors_and_aborts_pass_through(monkeypatch):
    assert runner.invoke(app, ["no-such-command"]).exit_code == 2
    monkeypatch.setattr(cli, "resolve_adb", lambda: None)
    assert runner.invoke(app, ["devices"]).exit_code == 1  # typer.Exit is not "unexpected"
    assert "stopped unexpectedly" not in runner.invoke(app, ["devices"]).stderr


# ---------------------------------------------------------------------------
# Doctor --fix pointer (small fix b)
# ---------------------------------------------------------------------------

def _report(status: str, remediation: str | None) -> dict:
    return {"host": {"os": "windows", "arch": "AMD64"},
            "toolchain": {"adb_path": "adb", "platform_tools_version": "35.0.2", "origin": "google_official"},
            "status": "ok", "exit_code": 0,
            "checks": [{"id": "devices_state", "status": status, "summary": "s", "remediation": remediation,
                        "details": {"total": 0, "by_state": {}}}]}


def test_doctor_renderers_point_to_fix_only_when_useful(monkeypatch):
    monkeypatch.setattr("adapt_adb.doctor._is_stdout_tty", lambda: False)
    pointer = "Run 'adapt doctor --fix' to see how to fix the items above."
    assert render_doctor_human(_report("fail", "Do X."), show_fix=False).endswith("\n\n" + pointer)
    assert pointer not in render_doctor_human(_report("fail", "Do X."), show_fix=True)
    assert pointer not in render_doctor_human(_report("pass", None), show_fix=False)
    assert pointer not in render_doctor_human(_report("warn", None), show_fix=False)
    assert render_console_report(_report("warn", "Do Y."), [], show_fix=False, width=100).endswith(pointer)
    assert pointer not in render_console_report(_report("warn", "Do Y."), [], show_fix=True, width=100)
    assert pointer not in render_console_report(_report("pass", None), [], show_fix=False, width=100)
