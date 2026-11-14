"""Test CLI wireless hardening, connect result checking, timeouts, and menu safety."""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from adapt_adb.cli import _run_next_menu, app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _stub_server_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate CLI command tests from actual ADB daemon checks."""
    monkeypatch.setattr("adapt_adb.cli.preflight_adb_server", lambda *a, **k: None)


# ---------------------------------------------------------------------------
# 03-R1: adapt adb connect result checking & validation
# ---------------------------------------------------------------------------


def test_adb_connect_failed_output_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    """`adapt adb connect` exits non-zero when adb returns code 0 but stdout says failed."""
    fake_path = Path("/mock/adb")
    monkeypatch.setattr("adapt_adb.cli.resolve_adb", lambda: fake_path)

    mock_run = MagicMock(
        return_value=subprocess.CompletedProcess(
            args=[str(fake_path), "connect", "192.0.2.1:5555"],
            returncode=0,
            stdout="failed to connect to 192.0.2.1:5555\n",
            stderr="",
        )
    )
    monkeypatch.setattr("subprocess.run", mock_run)

    result = runner.invoke(app, ["adb", "connect", "192.0.2.1:5555"])
    assert result.exit_code != 0


def test_adb_connect_connected_output_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """`adapt adb connect` exits 0 when adb outputs 'connected to ...'."""
    fake_path = Path("/mock/adb")
    monkeypatch.setattr("adapt_adb.cli.resolve_adb", lambda: fake_path)

    mock_run = MagicMock(
        return_value=subprocess.CompletedProcess(
            args=[str(fake_path), "connect", "192.0.2.1:5555"],
            returncode=0,
            stdout="connected to 192.0.2.1:5555\n",
            stderr="",
        )
    )
    monkeypatch.setattr("subprocess.run", mock_run)

    result = runner.invoke(app, ["adb", "connect", "192.0.2.1:5555"])
    assert result.exit_code == 0
    assert "connected to 192.0.2.1:5555" in (result.output + result.stdout)


def test_adb_connect_already_connected_output_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """`adapt adb connect` exits 0 when adb outputs 'already connected to ...'."""
    fake_path = Path("/mock/adb")
    monkeypatch.setattr("adapt_adb.cli.resolve_adb", lambda: fake_path)

    mock_run = MagicMock(
        return_value=subprocess.CompletedProcess(
            args=[str(fake_path), "connect", "192.0.2.1:5555"],
            returncode=0,
            stdout="already connected to 192.0.2.1:5555\n",
            stderr="",
        )
    )
    monkeypatch.setattr("subprocess.run", mock_run)

    result = runner.invoke(app, ["adb", "connect", "192.0.2.1:5555"])
    assert result.exit_code == 0


@pytest.mark.parametrize(
    "extra_args",
    [
        ["-s", "mock-serial", "connect", "192.0.2.1:5555"],
        ["connect", "192.0.2.1:5555", "extra-operand"],
        ["connect"],
    ],
)
def test_adb_connect_prefix_or_invalid_operands_rejected_without_spawning_adb(
    monkeypatch: pytest.MonkeyPatch, extra_args: list[str]
) -> None:
    """Global prefix or extra/missing operands on `adapt adb connect` are rejected with exit 1."""
    fake_path = Path("/mock/adb")
    monkeypatch.setattr("adapt_adb.cli.resolve_adb", lambda: fake_path)

    mock_run = MagicMock()
    monkeypatch.setattr("subprocess.run", mock_run)

    result = runner.invoke(app, ["adb"] + extra_args)
    assert result.exit_code == 1
    assert "Error: use 'adapt adb connect ENDPOINT' with no other ADB options or operands." in result.stderr
    mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# 03-R4: endpoint validation before pairing secret prompt
# ---------------------------------------------------------------------------


def test_adapt_pair_invalid_endpoint_no_port_exits_one_without_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`adapt pair` with endpoint missing a port exits 1 without prompting for secret."""
    fake_path = Path("/mock/adb")
    monkeypatch.setattr("adapt_adb.cli.resolve_adb", lambda: fake_path)
    monkeypatch.setattr("adapt_adb.cli._is_stdin_tty", lambda: True)

    mock_getpass = MagicMock()
    monkeypatch.setattr("getpass.getpass", mock_getpass)

    mock_run = MagicMock()
    monkeypatch.setattr("subprocess.run", mock_run)

    result = runner.invoke(app, ["pair", "192.0.2.1"])
    assert result.exit_code == 1
    mock_getpass.assert_not_called()
    mock_run.assert_not_called()
    assert "Error: pairing requires a valid host:port endpoint." in result.stderr


def test_adapt_pair_valid_endpoint_on_tty_prompts_for_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`adapt pair` with valid host:port on a TTY prompts for pairing code."""
    fake_path = Path("/mock/adb")
    monkeypatch.setattr("adapt_adb.cli.resolve_adb", lambda: fake_path)
    monkeypatch.setattr("adapt_adb.cli._is_stdin_tty", lambda: True)

    mock_getpass = MagicMock(return_value="123456")
    monkeypatch.setattr("getpass.getpass", mock_getpass)

    mock_run = MagicMock(
        return_value=subprocess.CompletedProcess(
            args=[str(fake_path), "pair", "192.0.2.1:37123"],
            returncode=0,
            stdout="successfully paired to 192.0.2.1:37123\n",
            stderr="",
        )
    )
    monkeypatch.setattr("subprocess.run", mock_run)

    result = runner.invoke(app, ["pair", "192.0.2.1:37123"])
    assert result.exit_code == 0
    mock_getpass.assert_called_once_with("Pairing code: ")
    assert mock_run.call_args.kwargs.get("input") == "123456\n"


# ---------------------------------------------------------------------------
# 03-R3: bounded subprocess calls
# ---------------------------------------------------------------------------


def test_devices_command_bounded_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """`adapt devices` passes timeout=10 to subprocess.run."""
    fake_path = Path("/mock/adb")
    monkeypatch.setattr("adapt_adb.cli.resolve_adb", lambda: fake_path)

    mock_run = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr("subprocess.run", mock_run)

    result = runner.invoke(app, ["devices"])
    assert result.exit_code == 0
    mock_run.assert_called_once_with([str(fake_path), "devices", "-l"], timeout=10)


def test_devices_command_timeout_expired_exits_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """`adapt devices` prints timeout error and exits 1 on TimeoutExpired."""
    fake_path = Path("/mock/adb")
    monkeypatch.setattr("adapt_adb.cli.resolve_adb", lambda: fake_path)

    mock_run = MagicMock(side_effect=subprocess.TimeoutExpired(cmd=["devices", "-l"], timeout=10))
    monkeypatch.setattr("subprocess.run", mock_run)

    result = runner.invoke(app, ["devices"])
    assert result.exit_code == 1
    assert "Error: ADB device listing timed out." in result.stderr


def test_devices_command_oserror_exits_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """`adapt devices` prints execute error and exits 1 on OSError."""
    fake_path = Path("/mock/adb")
    monkeypatch.setattr("adapt_adb.cli.resolve_adb", lambda: fake_path)

    mock_run = MagicMock(side_effect=OSError("Exec failed"))
    monkeypatch.setattr("subprocess.run", mock_run)

    result = runner.invoke(app, ["devices"])
    assert result.exit_code == 1
    assert "Error: Could not execute ADB." in result.stderr


def test_adb_passthrough_finite_and_unbounded_command_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finite commands get timeout=60; streaming/transfer commands keep timeout=None."""
    fake_path = Path("/mock/adb")
    monkeypatch.setattr("adapt_adb.cli.resolve_adb", lambda: fake_path)
    monkeypatch.setattr(
        "adapt_adb.cli.get_connected_devices",
        lambda p, **kw: [{"serial": "mock-dev-1", "state": "device"}],
    )

    mock_run = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr("subprocess.run", mock_run)

    # Finite command: version -> timeout=60
    res_version = runner.invoke(app, ["adb", "version"])
    assert res_version.exit_code == 0
    assert mock_run.call_args.kwargs.get("timeout") == 60

    # Finite command: disconnect -> timeout=60
    mock_run.reset_mock()
    res_disconnect = runner.invoke(app, ["adb", "disconnect"])
    assert res_disconnect.exit_code == 0
    assert mock_run.call_args.kwargs.get("timeout") == 60

    # Unbounded transfer command: push -> timeout=None
    mock_run.reset_mock()
    res_push = runner.invoke(app, ["adb", "push", "source", "dest"])
    assert res_push.exit_code == 0
    assert mock_run.call_args.kwargs.get("timeout") is None

    # Unbounded stream command: logcat -d -> timeout=None
    mock_run.reset_mock()
    res_logcat = runner.invoke(app, ["adb", "logcat", "-d"])
    assert res_logcat.exit_code == 0
    assert mock_run.call_args.kwargs.get("timeout") is None


def test_adb_passthrough_finite_command_timeout_expired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finite adb commands exit 1 and report 'Error: ADB command timed out.' on TimeoutExpired."""
    fake_path = Path("/mock/adb")
    monkeypatch.setattr("adapt_adb.cli.resolve_adb", lambda: fake_path)

    mock_run = MagicMock(side_effect=subprocess.TimeoutExpired(cmd=["version"], timeout=60))
    monkeypatch.setattr("subprocess.run", mock_run)

    result = runner.invoke(app, ["adb", "version"])
    assert result.exit_code == 1
    assert "Error: ADB command timed out." in result.stderr


# ---------------------------------------------------------------------------
# DOC-05: interactive Next-steps menu
# ---------------------------------------------------------------------------


def test_menu_setup_exception_does_not_propagate_and_rechecks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Setup raising an exception prints error in red on stderr and proceeds to recheck."""
    iteration = 0

    def mock_build_actions(report: dict[str, object]) -> list[dict[str, str]]:
        nonlocal iteration
        iteration += 1
        return [{"key": "1", "id": "setup", "label": "Setup"}] if iteration == 1 else []

    monkeypatch.setattr("adapt_adb.cli.build_next_actions", mock_build_actions)
    monkeypatch.setattr(sys, "stdin", io.StringIO("1\n"))
    monkeypatch.setattr(
        "adapt_adb.cli.run_setup_workflow",
        MagicMock(side_effect=OSError("platform-tools download failed")),
    )
    mock_recheck = MagicMock(return_value={"exit_code": 0, "status": "ok"})
    monkeypatch.setattr("adapt_adb.cli.run_doctor_checks", mock_recheck)
    monkeypatch.setattr("adapt_adb.cli.render_doctor_human", lambda *a, **k: "doctor-recheck-ok")

    _run_next_menu({}, show_fix=False)

    assert mock_recheck.called
    captured = capsys.readouterr()
    assert "Error: setup failed: platform-tools download failed" in captured.err


def test_menu_setup_aborted_unsafe_server_prints_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Setup returning aborted_unsafe_server prints the standard unsafe server error."""
    iteration = 0

    def mock_build_actions(report: dict[str, object]) -> list[dict[str, str]]:
        nonlocal iteration
        iteration += 1
        return [{"key": "1", "id": "setup", "label": "Setup"}] if iteration == 1 else []

    monkeypatch.setattr("adapt_adb.cli.build_next_actions", mock_build_actions)
    monkeypatch.setattr(sys, "stdin", io.StringIO("1\n"))
    monkeypatch.setattr(
        "adapt_adb.cli.run_setup_workflow",
        MagicMock(return_value={"setup": {"action": "aborted_unsafe_server"}}),
    )
    mock_recheck = MagicMock(return_value={"exit_code": 3, "status": "fail"})
    monkeypatch.setattr("adapt_adb.cli.run_doctor_checks", mock_recheck)
    monkeypatch.setattr("adapt_adb.cli.render_doctor_human", lambda *a, **k: "")

    _run_next_menu({}, show_fix=False)

    assert mock_recheck.called
    captured = capsys.readouterr()
    assert (
        "Aborted setup: Port 5037 has an unsafe or mismatched shared ADB server (exit code 3)."
        in captured.err
    )


def test_menu_setup_error_field_prints_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Setup report containing an error field prints 'Error: <error>' on stderr."""
    iteration = 0

    def mock_build_actions(report: dict[str, object]) -> list[dict[str, str]]:
        nonlocal iteration
        iteration += 1
        return [{"key": "1", "id": "setup", "label": "Setup"}] if iteration == 1 else []

    monkeypatch.setattr("adapt_adb.cli.build_next_actions", mock_build_actions)
    monkeypatch.setattr(sys, "stdin", io.StringIO("1\n"))
    monkeypatch.setattr(
        "adapt_adb.cli.run_setup_workflow",
        MagicMock(return_value={"setup": {"action": "failed", "error": "SHA-256 verification failed"}}),
    )
    monkeypatch.setattr("adapt_adb.cli.run_doctor_checks", lambda *a, **k: {"exit_code": 1})
    monkeypatch.setattr("adapt_adb.cli.render_doctor_human", lambda *a, **k: "")

    _run_next_menu({}, show_fix=False)

    captured = capsys.readouterr()
    assert "Error: SHA-256 verification failed" in captured.err


def test_menu_wireless_blank_pairing_endpoint_skips_pair_prompts_and_connects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blank pairing endpoint avoids getpass prompt and proceeds directly to connect."""
    iteration = 0

    def mock_build_actions(report: dict[str, object]) -> list[dict[str, str]]:
        nonlocal iteration
        iteration += 1
        return [{"key": "1", "id": "wireless", "label": "Wireless"}] if iteration == 1 else []

    fake_path = Path("/mock/adb")
    monkeypatch.setattr("adapt_adb.cli.resolve_adb", lambda: fake_path)
    monkeypatch.setattr("adapt_adb.cli.build_next_actions", mock_build_actions)
    # 1: choose wireless; empty line: blank pairing endpoint; connect endpoint
    monkeypatch.setattr(sys, "stdin", io.StringIO("1\n\n192.0.2.1:5555\n"))

    mock_getpass = MagicMock()
    monkeypatch.setattr("getpass.getpass", mock_getpass)

    mock_pair = MagicMock()
    monkeypatch.setattr("adapt_adb.cli.run_pair_command", mock_pair)

    mock_connect = MagicMock(return_value=0)
    monkeypatch.setattr("adapt_adb.cli.run_connect_command", mock_connect)

    monkeypatch.setattr("adapt_adb.cli.run_doctor_checks", lambda *a, **k: {"exit_code": 0})
    monkeypatch.setattr("adapt_adb.cli.render_doctor_human", lambda *a, **k: "")

    _run_next_menu({}, show_fix=False)

    mock_getpass.assert_not_called()
    mock_pair.assert_not_called()
    mock_connect.assert_called_once_with(fake_path, "192.0.2.1:5555")


def test_menu_wireless_invalid_pairing_endpoint_no_code_no_pair_no_connect(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Invalid pairing endpoint emits error without calling getpass, pair, or connect."""
    iteration = 0

    def mock_build_actions(report: dict[str, object]) -> list[dict[str, str]]:
        nonlocal iteration
        iteration += 1
        return [{"key": "1", "id": "wireless", "label": "Wireless"}] if iteration == 1 else []

    fake_path = Path("/mock/adb")
    monkeypatch.setattr("adapt_adb.cli.resolve_adb", lambda: fake_path)
    monkeypatch.setattr("adapt_adb.cli.build_next_actions", mock_build_actions)
    # 1: choose wireless; invalid pairing endpoint without port; connect endpoint
    monkeypatch.setattr(sys, "stdin", io.StringIO("1\n192.0.2.1\n192.0.2.1:5555\n"))

    mock_getpass = MagicMock()
    monkeypatch.setattr("getpass.getpass", mock_getpass)

    mock_pair = MagicMock()
    monkeypatch.setattr("adapt_adb.cli.run_pair_command", mock_pair)

    mock_connect = MagicMock()
    monkeypatch.setattr("adapt_adb.cli.run_connect_command", mock_connect)

    monkeypatch.setattr("adapt_adb.cli.run_doctor_checks", lambda *a, **k: {"exit_code": 0})
    monkeypatch.setattr("adapt_adb.cli.render_doctor_human", lambda *a, **k: "")

    _run_next_menu({}, show_fix=False)

    mock_getpass.assert_not_called()
    mock_pair.assert_not_called()
    mock_connect.assert_not_called()
    captured = capsys.readouterr()
    assert "Error: pairing requires a valid host:port endpoint." in captured.err
