"""Test CLI commands: doctor, setup, devices, mcp, and adb."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from adapt_adb.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _stub_server_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI command tests isolate targeting; review regressions cover daemon preflight."""
    monkeypatch.setattr("adapt_adb.cli.preflight_adb_server", lambda *a, **k: None)


def test_mcp_command():
    """`adapt mcp` starts stdio server (exit 0) when SDK is installed, or exits 2 on stderr when missing."""
    result_ok = runner.invoke(app, ["mcp"])
    assert result_ok.exit_code == 0
    assert result_ok.stdout == ""

    import sys

    with patch.dict(sys.modules, {"mcp": None}):
        result_missing = runner.invoke(app, ["mcp"])
        assert result_missing.exit_code == 2
        assert "Python MCP SDK is not installed" in result_missing.stderr
        assert "lands in v1" not in result_missing.stderr
        assert result_missing.stdout == ""


def test_devices_command_missing_adb(monkeypatch):
    """`adapt devices` exits with 1 and prints hint if adb is missing."""
    monkeypatch.setattr("adapt_adb.cli.resolve_adb", lambda: None)
    result = runner.invoke(app, ["devices"])
    assert result.exit_code == 1
    assert "adapt setup" in (result.output + result.stderr)


def test_devices_command_found_adb(monkeypatch):
    """`adapt devices` invokes adb devices -l when adb is found."""
    fake_path = Path("/mock/adb")
    monkeypatch.setattr("adapt_adb.cli.resolve_adb", lambda: fake_path)

    mock_run = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr("subprocess.run", mock_run)

    result = runner.invoke(app, ["devices"])
    assert result.exit_code == 0
    mock_run.assert_called_once_with([str(fake_path), "devices", "-l"], timeout=10)


from adapt_adb.doctor import DoctorEnv, Server5037State


def _make_doctor_env(tmp_path: Path, *, has_adb: bool) -> DoctorEnv:
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
            "PATH": str(pt_dir) if has_adb else "",
        },
        which_adb=adb_exe if has_adb else None,
        which_set=True,
        path_executables=[adb_exe] if has_adb else [],
        existing_paths={adb_exe, pt_dir} if has_adb else set(),
        source_properties={pt_dir / "source.properties": "Pkg.Revision=35.0.2\n"} if has_adb else {},
        version_outputs={
            adb_exe: (
                "Android Debug Bridge version 1.0.41\n"
                "Version 35.0.2-12147458\n"
                f"Installed as {adb_exe}\n"
            )
        }
        if has_adb
        else {},
        server_state=Server5037State(listening=False),
        windows_pnp_devices=[],
        devices_l_output="List of devices attached\n\n",
    )


def test_doctor_command_found(monkeypatch, tmp_path):
    """`adapt doctor` reports OS, path, and version and exits 0 when healthy."""
    env = _make_doctor_env(tmp_path, has_adb=True)
    monkeypatch.setattr("adapt_adb.cli._ACTIVE_DOCTOR_ENV", env)

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "OS:" in result.output
    assert "ADB Path:" in result.output
    assert str(env.which_adb) in result.output
    assert "35.0.2-12147458" in result.output


def test_doctor_command_missing(monkeypatch, tmp_path):
    """`adapt doctor` reports Not found and N/A and exits non-zero (1) when adb is missing."""
    env = _make_doctor_env(tmp_path, has_adb=False)
    monkeypatch.setattr("adapt_adb.cli._ACTIVE_DOCTOR_ENV", env)

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "ADB Path: Not found" in result.output
    assert "ADB Version: N/A" in result.output
    assert "adapt setup" in result.output


def test_doctor_command_tty_glyphs_and_redacts_serial(monkeypatch, tmp_path):
    """TTY `adapt doctor` renders check glyphs and device counts without raw FAKE_SERIAL."""
    env = _make_doctor_env(tmp_path, has_adb=True)
    env.environ["ANDROID_HOME"] = str(tmp_path / "missing-sdk")
    env.devices_l_output = (
        "List of devices attached\n"
        "FAKE_SERIAL          unauthorized usb:1-1 transport_id:1\n\n"
    )
    monkeypatch.setattr("adapt_adb.cli._ACTIVE_DOCTOR_ENV", env)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    result = runner.invoke(app, ["doctor", "--fix"])
    assert result.exit_code == 2
    for state in ("READY", "WAITING", "BLOCKED", "SKIP"):
        assert state in result.output
    assert "unauthorized" in result.output
    assert "Safe Remediation Commands" in result.output
    assert "Device Manager" in result.output
    assert "Zadig" not in result.output
    assert "FAKE_SERIAL" not in result.output


def test_doctor_non_interactive_no_stdin_tty_does_not_hang_and_shows_next(
    monkeypatch, tmp_path
):
    """Non-interactive `adapt doctor` (no stdin TTY) returns without hanging and shows Next labels."""
    import json

    env = _make_doctor_env(tmp_path, has_adb=False)
    monkeypatch.setattr("adapt_adb.cli._ACTIVE_DOCTOR_ENV", env)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    result_tty_out = runner.invoke(app, ["doctor"])
    assert result_tty_out.exit_code == 1
    assert "Next:" in result_tty_out.output
    assert "adapt setup" in result_tty_out.output
    assert "Device Manager" in result_tty_out.output
    assert "Zadig" not in result_tty_out.output
    assert "choose" not in (result_tty_out.output + result_tty_out.stderr).lower()

    # Plain non-TTY stdout + stdin also returns without hanging
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    result_plain = runner.invoke(app, ["doctor"])
    assert result_plain.exit_code == 1
    assert "adapt setup" in result_plain.output
    assert "choose" not in (result_plain.output + result_plain.stderr).lower()

    # --json never prompts even if both stdin and stdout are TTYs
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    result_json = runner.invoke(app, ["doctor", "--json"])
    assert result_json.exit_code == 1
    payload = json.loads(result_json.stdout)
    assert payload["command"] == "doctor"
    assert payload["exit_code"] == 1


def test_doctor_interactive_tty_prompts_and_quits_with_q(monkeypatch, tmp_path):
    """Interactive TTY `adapt doctor` displays numbered Next menu, prompts, and exits on `q`."""
    env = _make_doctor_env(tmp_path, has_adb=True)
    monkeypatch.setattr("adapt_adb.cli._ACTIVE_DOCTOR_ENV", env)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    result = runner.invoke(app, ["doctor"], input="q\n")
    combined = result.output + result.stderr
    assert result.exit_code == 0
    assert "Next:" in result.output
    assert "[1]" in result.output
    assert "[q]" in result.output
    assert "choose" in combined.lower() or "next action" in combined.lower()
    assert "Zadig" not in combined

    # `--fix` remains a read-only preview panel plus interactive Next menu on TTY
    result_fix = runner.invoke(app, ["doctor", "--fix"], input="q\n")
    combined_fix = result_fix.output + result_fix.stderr
    assert result_fix.exit_code == 0
    assert "Safe Remediation Commands" in result_fix.output
    assert "[q]" in result_fix.output
    assert "choose" in combined_fix.lower() or "next action" in combined_fix.lower()
    assert "Zadig" not in combined_fix


def test_doctor_interactive_tty_setup_choice_runs_setup(monkeypatch, tmp_path):
    """Interactive TTY `adapt doctor` runs mocked setup when `1` is chosen, then exits on `q`."""
    from adapt_adb.doctor import run_doctor_checks

    env = _make_doctor_env(tmp_path, has_adb=False)
    healthy_env = _make_doctor_env(tmp_path, has_adb=True)
    monkeypatch.setattr("adapt_adb.cli._ACTIVE_DOCTOR_ENV", env)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    def fake_setup(*args, **kwargs):
        env.which_adb = healthy_env.which_adb
        env.path_executables = healthy_env.path_executables
        env.existing_paths = healthy_env.existing_paths
        env.version_outputs = healthy_env.version_outputs
        env.source_properties = healthy_env.source_properties
        env.environ["PATH"] = healthy_env.environ["PATH"]
        rep = run_doctor_checks(env, command_name="setup")
        rep["setup"] = {
            "action": "installed",
            "downloaded": True,
            "path_updated": True,
        }
        return rep

    setup_mock = MagicMock(side_effect=fake_setup)
    monkeypatch.setattr("adapt_adb.cli.run_setup_workflow", setup_mock)

    result = runner.invoke(app, ["doctor"], input="1\nq\n")
    setup_mock.assert_called_once()
    assert result.exit_code == 0
    assert "Zadig" not in (result.output + result.stderr)


def test_doctor_interactive_tty_device_manager_choice(monkeypatch, tmp_path):
    """Interactive TTY `adapt doctor` opens Device Manager on Windows choice, then exits on `q`."""
    import os
    import subprocess

    env = _make_doctor_env(tmp_path, has_adb=True)
    monkeypatch.setattr("adapt_adb.cli._ACTIVE_DOCTOR_ENV", env)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    startfile_mock = MagicMock()
    popen_mock = MagicMock()
    monkeypatch.setattr(os, "startfile", startfile_mock, raising=False)
    monkeypatch.setattr(subprocess, "Popen", popen_mock)

    result = runner.invoke(app, ["doctor"], input="1\n\nq\n")
    combined = result.output + result.stderr
    assert result.exit_code == 0
    assert "Device Manager" in result.output
    assert "zadig" not in combined.lower()
    assert startfile_mock.called or popen_mock.called
    if startfile_mock.called:
        assert "devmgmt.msc" in str(startfile_mock.call_args)
    if popen_mock.called:
        assert "devmgmt.msc" in str(popen_mock.call_args)


def test_setup_skips_when_usable(monkeypatch, tmp_path):
    """`adapt setup` skips download if adb is already usable."""
    env = _make_doctor_env(tmp_path, has_adb=True)
    monkeypatch.setattr("adapt_adb.cli._ACTIVE_DOCTOR_ENV", env)

    download_mock = MagicMock()
    monkeypatch.setattr("adapt_adb.cli.download_and_extract_platform_tools", download_mock)

    result = runner.invoke(app, ["setup"])
    assert result.exit_code == 0
    assert "already usable" in result.output
    assert "Skipping download" in result.output
    download_mock.assert_not_called()


def test_setup_downloads_when_missing(monkeypatch, tmp_path):
    """`adapt setup` downloads and unpacks when adb is not found."""
    env = _make_doctor_env(tmp_path, has_adb=False)
    monkeypatch.setattr("adapt_adb.cli._ACTIVE_DOCTOR_ENV", env)

    download_mock = MagicMock()
    monkeypatch.setattr("adapt_adb.cli.download_and_extract_platform_tools", download_mock)

    result = runner.invoke(app, ["setup", "--yes"])
    assert result.exit_code == 0
    download_mock.assert_called_once()
    assert "completed successfully" in result.output


def test_adb_missing(monkeypatch):
    """`adapt adb` exits with 1 when adb is missing."""
    monkeypatch.setattr("adapt_adb.cli.resolve_adb", lambda: None)
    result = runner.invoke(app, ["adb", "devices"])
    assert result.exit_code == 1
    assert "adapt setup" in (result.output + result.stderr)


def test_adb_zero_ready_devices_refusal(monkeypatch):
    """`adapt adb` exits 1 with `adapt doctor` hint on stderr when 0 ready (`state == 'device'`) devices exist."""
    fake_path = Path("/mock/adb")
    monkeypatch.setattr("adapt_adb.cli.resolve_adb", lambda: fake_path)
    monkeypatch.setattr(
        "adapt_adb.cli.get_connected_devices",
        lambda p, **kwargs: [{"serial": "mock-dev-1", "state": "unauthorized"}],
    )

    mock_run = MagicMock()
    monkeypatch.setattr("subprocess.run", mock_run)

    result = runner.invoke(app, ["adb", "shell", "getprop"])
    assert result.exit_code == 1
    assert "adapt doctor" in result.stderr
    mock_run.assert_not_called()


def test_adb_multiple_devices_refusal(monkeypatch):
    """`adapt adb` refuses execution without -s/--serial if >1 device is connected."""
    fake_path = Path("/mock/adb")
    monkeypatch.setattr("adapt_adb.cli.resolve_adb", lambda: fake_path)
    monkeypatch.setattr(
        "adapt_adb.cli.get_connected_devices",
        lambda p, **kwargs: [
            {"serial": "mock-dev-1", "state": "device"},
            {"serial": "mock-dev-2", "state": "device"},
        ],
    )

    mock_run = MagicMock()
    monkeypatch.setattr("subprocess.run", mock_run)

    result = runner.invoke(app, ["adb", "shell", "getprop"])
    assert result.exit_code == 1
    assert "Multiple devices connected (2)" in (result.output + result.stderr)
    assert "mock-dev-1" in result.output
    assert "mock-dev-2" in result.output
    mock_run.assert_not_called()


def test_adb_multiple_devices_allowed_with_dash_s(monkeypatch):
    """`adapt adb` allows execution when -s is provided even with multiple devices."""
    fake_path = Path("/mock/adb")
    monkeypatch.setattr("adapt_adb.cli.resolve_adb", lambda: fake_path)
    monkeypatch.setattr(
        "adapt_adb.cli.get_connected_devices",
        lambda p, **kwargs: [
            {"serial": "mock-dev-1", "state": "device"},
            {"serial": "mock-dev-2", "state": "device"},
        ],
    )

    mock_run = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr("subprocess.run", mock_run)

    result = runner.invoke(app, ["adb", "-s", "mock-dev-1", "shell", "getprop"])
    assert result.exit_code == 0
    mock_run.assert_called_once_with([str(fake_path), "-s", "mock-dev-1", "shell", "getprop"], timeout=60)


def test_adb_multiple_devices_allowed_with_long_serial(monkeypatch):
    """`adapt adb` allows execution when --serial is provided."""
    fake_path = Path("/mock/adb")
    monkeypatch.setattr("adapt_adb.cli.resolve_adb", lambda: fake_path)
    monkeypatch.setattr(
        "adapt_adb.cli.get_connected_devices",
        lambda p, **kwargs: [
            {"serial": "mock-dev-1", "state": "device"},
            {"serial": "mock-dev-2", "state": "device"},
        ],
    )

    mock_run = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr("subprocess.run", mock_run)

    result = runner.invoke(app, ["adb", "--serial=mock-dev-2", "shell", "getprop"])
    assert result.exit_code == 0
    mock_run.assert_called_once_with([str(fake_path), "-s", "mock-dev-2", "shell", "getprop"], timeout=60)


def test_adb_single_device_pass_through(monkeypatch):
    """`adapt adb` injects `-s mock-dev-1` when 1 ready (`state == 'device'`) device is connected."""
    fake_path = Path("/mock/adb")
    monkeypatch.setattr("adapt_adb.cli.resolve_adb", lambda: fake_path)
    monkeypatch.setattr(
        "adapt_adb.cli.get_connected_devices",
        lambda p, **kwargs: [
            {"serial": "mock-dev-1", "state": "device"},
            {"serial": "mock-dev-offline", "state": "offline"},
        ],
    )

    mock_run = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr("subprocess.run", mock_run)

    result = runner.invoke(app, ["adb", "shell", "getprop"])
    assert result.exit_code == 0
    mock_run.assert_called_once_with([str(fake_path), "-s", "mock-dev-1", "shell", "getprop"], timeout=60)


def test_adb_rejects_forbidden_args_via_safety(monkeypatch):
    """`adapt adb` blocks forbidden args (kill-server, sudo, pm clear, uninstall, bare shell, streaming logcat) before spawning."""
    fake_path = Path("/mock/adb")
    monkeypatch.setattr("adapt_adb.cli.resolve_adb", lambda: fake_path)
    monkeypatch.setattr(
        "adapt_adb.cli.get_connected_devices",
        lambda p, **kwargs: [{"serial": "mock-dev-1", "state": "device"}],
    )

    mock_run = MagicMock()
    monkeypatch.setattr("subprocess.run", mock_run)

    for bad_args in (
        ["adb", "kill-server"],
        ["adb", "sudo", "devices"],
        ["adb", "uninstall", "com.example.app"],
        ["adb", "shell", "pm", "clear", "com.example.app"],
        ["adb", "logcat"],
        ["adb", "shell"],
    ):
        result = runner.invoke(app, bad_args)
        assert result.exit_code == 1
        assert "Error:" in result.stderr
    mock_run.assert_not_called()


def test_adb_version_with_zero_ready_devices_runs_without_serial(monkeypatch):
    """`adapt adb version` runs without -s and exits 0 even with 0 ready devices."""
    fake_path = Path("/mock/adb")
    monkeypatch.setattr("adapt_adb.cli.resolve_adb", lambda: fake_path)
    monkeypatch.setattr(
        "adapt_adb.cli.get_connected_devices",
        lambda p, **kwargs: [{"serial": "mock-dev-1", "state": "unauthorized"}],
    )

    mock_run = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr("subprocess.run", mock_run)

    result = runner.invoke(app, ["adb", "version"])
    assert result.exit_code == 0
    assert "adapt doctor" not in result.stderr
    mock_run.assert_called_once_with([str(fake_path), "version"], timeout=60)
    assert "-s" not in mock_run.call_args.args[0]


def test_adb_version_with_multiple_ready_devices_runs_without_serial(monkeypatch):
    """`adapt adb version` runs without -s and without multi-device refusal when 2 ready devices exist."""
    fake_path = Path("/mock/adb")
    monkeypatch.setattr("adapt_adb.cli.resolve_adb", lambda: fake_path)
    monkeypatch.setattr(
        "adapt_adb.cli.get_connected_devices",
        lambda p, **kwargs: [
            {"serial": "mock-dev-1", "state": "device"},
            {"serial": "mock-dev-2", "state": "device"},
        ],
    )

    mock_run = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr("subprocess.run", mock_run)

    result = runner.invoke(app, ["adb", "version"])
    assert result.exit_code == 0
    assert "Multiple devices connected" not in (result.output + result.stderr)
    mock_run.assert_called_once_with([str(fake_path), "version"], timeout=60)
    assert "-s" not in mock_run.call_args.args[0]


def test_doctor_interactive_tty_udev_choice_uses_absolute_rules_path(monkeypatch, tmp_path):
    """Interactive TTY `adapt doctor` udev action copies an absolute 51-android.rules path."""
    import io
    import shlex
    import subprocess
    from pathlib import PurePosixPath
    from adapt_adb.cli import _run_next_menu

    env = _make_doctor_env(tmp_path, has_adb=True)
    env.host_os = "linux"
    monkeypatch.setattr("adapt_adb.cli._ACTIVE_DOCTOR_ENV", env)
    monkeypatch.setattr(
        "adapt_adb.cli.run_doctor_checks",
        lambda *a, **kw: {
            "host": {"os": "linux", "arch": "x86_64"},
            "toolchain": {
                "adb_path": "/mock/adb",
                "platform_tools_version": "35.0.2",
                "origin": "PATH",
            },
            "checks": [],
            "devices": {"total": 0, "by_state": {}},
            "exit_code": 0,
            "status": "ok",
        },
    )
    monkeypatch.setattr("adapt_adb.cli.render_doctor_human", lambda *a, **kw: "ok")
    monkeypatch.setattr(sys, "stdin", io.StringIO("1\nq\n"))

    run_mock = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr(subprocess, "run", run_mock)

    report = {
        "host": {"os": "linux", "arch": "x86_64"},
        "toolchain": {"adb_path": "/mock/adb"},
        "checks": [],
        "exit_code": 0,
    }
    _run_next_menu(report, show_fix=False)

    assert run_mock.call_count == 4
    first = run_mock.call_args_list[0]
    assert first.kwargs == {"check": True}
    cmd = first.args[0]
    assert cmd[:3] == ["sudo", "cp", "--"]
    assert "zadig" not in " ".join(cmd).lower()
    src_path = cmd[3]
    assert src_path != "packaging/linux/51-android.rules"
    assert "51-android.rules" in src_path
    assert Path(src_path).is_absolute() or PurePosixPath(src_path).is_absolute()


def test_pair_command_without_serial_and_zero_ready_devices(monkeypatch):
    """`adapt pair 192.0.2.1:37123 123456` runs `adb pair` without `-s` even with 0 ready devices."""
    fake_path = Path("/mock/adb")
    monkeypatch.setattr("adapt_adb.cli.resolve_adb", lambda: fake_path)
    monkeypatch.setattr("adapt_adb.cli.get_connected_devices", lambda p: [], raising=False)

    mock_run = MagicMock(
        return_value=MagicMock(
            returncode=0,
            stdout="Successfully paired to 192.0.2.1:37123\n",
            stderr="",
        )
    )
    monkeypatch.setattr("subprocess.run", mock_run)

    result = runner.invoke(app, ["pair", "192.0.2.1:37123", "123456"])
    assert result.exit_code == 0
    mock_run.assert_called_once()
    cmd = (
        mock_run.call_args.args[0]
        if mock_run.call_args.args
        else mock_run.call_args.kwargs["args"]
    )
    assert cmd[0] == str(fake_path)
    assert "pair" in cmd
    assert "192.0.2.1:37123" in cmd
    assert "-s" not in cmd
    assert not any(
        str(arg).startswith("-s") or str(arg).startswith("--serial") for arg in cmd
    )


def test_connect_command_runs_without_serial(monkeypatch):
    """`adapt connect 192.0.2.1:5555` runs `adb connect` without `-s`."""
    fake_path = Path("/mock/adb")
    monkeypatch.setattr("adapt_adb.cli.resolve_adb", lambda: fake_path)
    monkeypatch.setattr("adapt_adb.cli.get_connected_devices", lambda p: [], raising=False)

    mock_run = MagicMock(
        return_value=MagicMock(
            returncode=0,
            stdout="connected to 192.0.2.1:5555\n",
            stderr="",
        )
    )
    monkeypatch.setattr("subprocess.run", mock_run)

    result = runner.invoke(app, ["connect", "192.0.2.1:5555"])
    assert result.exit_code == 0
    mock_run.assert_called_once()
    cmd = (
        mock_run.call_args.args[0]
        if mock_run.call_args.args
        else mock_run.call_args.kwargs["args"]
    )
    assert cmd[0] == str(fake_path)
    assert "connect" in cmd
    assert "192.0.2.1:5555" in cmd
    assert "-s" not in cmd
    assert not any(
        str(arg).startswith("-s") or str(arg).startswith("--serial") for arg in cmd
    )


def test_adb_pair_and_connect_host_only_without_serial(monkeypatch):
    """`adapt adb pair` and `adapt adb connect` run without `-s` even with 0 or 2 ready devices."""
    fake_path = Path("/mock/adb")
    monkeypatch.setattr("adapt_adb.cli.resolve_adb", lambda: fake_path)
    monkeypatch.setattr("adapt_adb.cli.get_connected_devices", lambda p: [])

    mock_run = MagicMock(
        return_value=MagicMock(returncode=0, stdout="Successfully paired to 192.0.2.1:37123\n", stderr="")
    )
    monkeypatch.setattr("subprocess.run", mock_run)

    res_pair = runner.invoke(app, ["adb", "pair", "192.0.2.1:37123", "123456"])
    assert res_pair.exit_code == 0
    # The pairing code goes to adb on stdin, never into the spawned argv.
    assert mock_run.call_args.args[0] == [str(fake_path), "pair", "192.0.2.1:37123"]
    assert "123456" not in mock_run.call_args.args[0]
    assert mock_run.call_args.kwargs["input"] == "123456\n"
    assert "-s" not in mock_run.call_args.args[0]

    mock_run.return_value = MagicMock(
        returncode=0, stdout="connected to 192.0.2.1:5555\n", stderr=""
    )

    monkeypatch.setattr(
        "adapt_adb.cli.get_connected_devices",
        lambda p, **kwargs: [
            {"serial": "mock-dev-1", "state": "device"},
            {"serial": "mock-dev-2", "state": "device"},
        ],
    )
    res_connect = runner.invoke(app, ["adb", "connect", "192.0.2.1:5555"])
    assert res_connect.exit_code == 0
    assert mock_run.call_args.args[0] == [str(fake_path), "connect", "192.0.2.1:5555"]
    assert "-s" not in mock_run.call_args.args[0]


def test_guide_and_guide_testing_commands():
    """`adapt guide` and `adapt guide testing` exit 0 and mention CTS, Espresso, or LiteRT."""
    help_result = runner.invoke(app, ["guide", "--help"])
    if (
        help_result.exit_code != 0
        and "no such command" in (help_result.output + help_result.stderr).lower()
    ):
        pytest.skip("adapt guide not landed yet")

    res_guide = runner.invoke(app, ["guide"])
    assert res_guide.exit_code == 0
    guide_text = (res_guide.output + res_guide.stderr).lower()
    assert guide_text.strip() != ""
    assert "zadig" not in guide_text

    res_testing = runner.invoke(app, ["guide", "testing"])
    assert res_testing.exit_code == 0
    testing_text = (res_testing.output + res_testing.stderr).lower()
    assert any(term in testing_text for term in ("cts", "espresso", "litert"))
    assert "zadig" not in testing_text





@pytest.mark.parametrize("prompt", [False, True])
def test_adb_pair_secret_stdin_and_preflight(prompt, monkeypatch):
    import subprocess
    from adapt_adb import cli

    monkeypatch.setattr(cli, "resolve_adb", lambda: Path("adb"))
    monkeypatch.setattr(cli, "_is_stdin_tty", lambda: prompt)
    monkeypatch.setattr(cli.getpass, "getpass", lambda *a: "654321")
    gates = []
    monkeypatch.setattr(cli, "preflight_adb_server", lambda *a: gates.append(a))
    calls = []
    def run(argv, **kwargs):
        assert gates == [(Path("adb"), ["pair", "192.0.2.1:37123"])]
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "Successfully paired to 192.0.2.1:37123", "")
    monkeypatch.setattr(subprocess, "run", run)
    args = ["adb", "pair", "192.0.2.1:37123"] + ([] if prompt else ["654321"])
    assert runner.invoke(app, args).exit_code == 0
    assert calls[0][0] == ["adb", "pair", "192.0.2.1:37123"]
    assert calls[0][1]["input"] == "654321\n"


def test_adb_pair_preflight_refusal_prevents_spawn(monkeypatch):
    from adapt_adb import cli
    from adapt_adb.doctor import UnsafeAdbServerError

    monkeypatch.setattr(cli, "resolve_adb", lambda: Path("adb"))
    def refuse(*a):
        raise UnsafeAdbServerError("refused")
    monkeypatch.setattr(cli, "preflight_adb_server", refuse)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: pytest.fail("Spawned"))
    assert runner.invoke(app, ["adb", "pair", "192.0.2.1:37123", "654321"]).exit_code == 3


@pytest.mark.parametrize("args", [
    ["pair", "192.0.2.1:37123", "654321", "extra"],
    ["-s", "A", "pair", "192.0.2.1:37123", "654321"],
    ["-H", "localhost", "pair", "192.0.2.1:37123", "654321"],
])
def test_adb_pair_rejects_extra_operands_and_prefix(args, monkeypatch):
    from adapt_adb import cli

    monkeypatch.setattr(cli, "resolve_adb", lambda: Path("adb"))
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: pytest.fail("Spawned"))
    assert runner.invoke(app, ["adb", *args]).exit_code != 0
