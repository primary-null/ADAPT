"""Hardening tests for terminal UI argument parsing, cancel escapes, and console reporting."""

from __future__ import annotations

import io
import os
import subprocess

import pytest
from typer.main import get_command

from adapt_adb import terminal_ui as ui
from adapt_adb.cli import app
from adapt_adb.console import render_console_report


class Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture
def terminal(monkeypatch):
    monkeypatch.delenv("ADAPT_NO_BANNER", raising=False)
    monkeypatch.delenv("ADAPT_BANNER", raising=False)
    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(ui.shutil, "get_terminal_size", lambda **kwargs: os.terminal_size((100, 32)))
    restored = []
    monkeypatch.setattr(ui, "_enable_vt", lambda: (True, lambda: restored.append(True)))
    monkeypatch.setattr(ui.subprocess, "run", lambda *a, **k: pytest.fail("Opening a menu must not run any command"))
    return Terminal(), Terminal(), restored


def run_keys(keys: str, terminal, **kwargs):
    out, err, restored = terminal
    ui.run_console(get_command(app), stdin=Terminal(keys), stdout=out, stderr=err, **kwargs)
    return out.getvalue(), err.getvalue(), restored


def tips_command_index() -> int:
    root = get_command(app)
    records = ui.command_inventory(root)
    area = [r for r in records if r["command"].count(" ") == 1 and r["command"].split()[1] in ui.AREAS[7][1]]
    return next(i for i, r in enumerate(area, 1) if r["command"] == "adapt tips")


def fixture_report() -> dict:
    return {
        "status": "unsafe_shared_server",
        "exit_code": 3,
        "host": {"os": "windows", "arch": "AMD64"},
        "toolchain": {"adb_path": "C:/Android Tools/adb.exe", "platform_tools_version": "36.0.0", "origin": "user-scope"},
        "checks": [{"id": "adb_server_5037", "status": "fail", "summary": "Owner unavailable", "remediation": "Verify the listening daemon."}],
        "devices": {"by_state": {"device": 0, "offline": 1}},
    }


# =====================================================================
# 04-R1: Windows argument parsing and unclosed quote detection
# =====================================================================


def test_split_arguments_windows_sentinel_parsing_os_independent(monkeypatch):
    monkeypatch.setattr(ui.os, "name", "nt")

    # Sentinel present as last element -> returns arguments without sentinel
    monkeypatch.setattr(ui, "_win_command_line_to_argv", lambda cmd: ["adapt", "alpha", "beta", ui._ARGV_SENTINEL])
    assert ui.split_arguments("alpha beta") == ["alpha", "beta"]

    # Sentinel merged into last element (unclosed quote) -> raises ValueError
    monkeypatch.setattr(ui, "_win_command_line_to_argv", lambda cmd: ["adapt", "--apk", f"C:\\Temp\\My App {ui._ARGV_SENTINEL}"])
    with pytest.raises(ValueError, match="Arguments could not be parsed. Use double quotes around paths with spaces."):
        ui.split_arguments('--apk "C:\\Temp\\My App')

    # Null / empty return from parser
    monkeypatch.setattr(ui, "_win_command_line_to_argv", lambda cmd: ["adapt"])
    with pytest.raises(ValueError, match="Arguments could not be parsed"):
        ui.split_arguments("")


@pytest.mark.skipif(os.name != "nt", reason="CommandLineToArgvW tests require Windows host")
def test_split_arguments_windows_live():
    # Unclosed quote raises ValueError
    with pytest.raises(ValueError, match="Arguments could not be parsed. Use double quotes around paths with spaces."):
        ui.split_arguments('--apk "C:\\Temp\\My App')

    # Closed quoted path with spaces parses properly
    assert ui.split_arguments('--apk "C:\\Temp\\My App\\app.apk" --serial X') == [
        "--apk",
        "C:\\Temp\\My App\\app.apk",
        "--serial",
        "X",
    ]

    # Empty string returns empty list
    assert ui.split_arguments("") == []

    # Argument ending in backslash parses
    assert ui.split_arguments("C:\\Temp\\") == ["C:\\Temp\\"]

    # Escaped quote parses without error
    assert ui.split_arguments('say \\"hi\\"') == ["say", '"hi"']


# =====================================================================
# 04-R3: Cancel escapes at Arguments > prompt (:b, :back, :q, :quit, :exit, :h, :home)
# =====================================================================


def test_arguments_prompt_escape_colon_b_returns_to_detail_without_subprocess(terminal):
    idx = tips_command_index()
    # Press 'r', then ':b' to cancel back to detail screen, then 'q' to exit
    out, err, _ = run_keys(f"8\n{idx}\nr\n:b\nq\n", terminal)
    # Subprocess.run was NOT called (terminal fixture would fail if called)
    # The screen drawn after Arguments prompt was the TIPS detail screen
    assert "Enter arguments for this command. :b goes back, H returns home, :q exits." in out
    after_prompt = out.split("Arguments > ")[1]
    assert "ADAPT / TIPS" in after_prompt


def test_arguments_prompt_escape_colon_back_returns_to_detail(terminal):
    idx = tips_command_index()
    out, err, _ = run_keys(f"8\n{idx}\nr\n:back\nq\n", terminal)
    after_prompt = out.split("Arguments > ")[1]
    assert "ADAPT / TIPS" in after_prompt


def test_arguments_prompt_escape_colon_q_exits_console(terminal):
    idx = tips_command_index()
    # Press 'r', then ':q' to exit immediately without running subprocess
    out, err, _ = run_keys(f"8\n{idx}\nr\n:q\n", terminal)
    # Console exited directly after Arguments > prompt
    assert "Arguments > " in out
    # Detail screen was NOT redrawn after ':q'
    after_prompt = out.split("Arguments > ")[1]
    assert "ADAPT / TIPS" not in after_prompt


def test_arguments_prompt_escape_colon_quit_and_exit(terminal):
    idx = tips_command_index()
    out1, _, _ = run_keys(f"8\n{idx}\nr\n:quit\n", terminal)
    assert "ADAPT / TIPS" not in out1.split("Arguments > ")[1]

    terminal[0].seek(0)
    terminal[0].truncate()
    terminal[1].seek(0)
    terminal[1].truncate()

    out2, _, _ = run_keys(f"8\n{idx}\nr\n:exit\n", terminal)
    assert "ADAPT / TIPS" not in out2.split("Arguments > ")[1]


def test_arguments_prompt_escape_colon_h_and_home(terminal):
    idx = tips_command_index()
    out, _, _ = run_keys(f"8\n{idx}\nr\n:h\nq\n", terminal)
    after_prompt = out.split("Arguments > ")[1]
    assert "ADAPT / MAIN MENU" in after_prompt


def test_arguments_prompt_literal_b_passes_to_subprocess(terminal, monkeypatch):
    calls = []
    monkeypatch.setattr(ui.subprocess, "run", lambda argv, **kwargs: calls.append((argv, kwargs)) or subprocess.CompletedProcess(argv, 0))
    idx = tips_command_index()
    # Press 'r', pass literal argument 'b', then Enter to dismiss completion status, then 'q' to exit
    out, err, _ = run_keys(f"8\n{idx}\nr\nb\n\nq\n", terminal)
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[-1] == "b"
    assert "Command finished with exit code 0." in out


# =====================================================================
# 04-R4: TERM=dumb normalization and empty transport counts
# =====================================================================


def test_render_console_report_term_dumb_variations(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)

    monkeypatch.setenv("TERM", "DUMB")
    result_upper = render_console_report(fixture_report(), [], color=True)
    assert "\x1b[" not in result_upper

    monkeypatch.setenv("TERM", "dumb ")
    result_space = render_console_report(fixture_report(), [], color=True)
    assert "\x1b[" not in result_space


def test_render_console_report_empty_transport_counts():
    report = fixture_report()

    # Empty dict -> no TRANSPORTS header
    report["devices"] = {"by_state": {}}
    res_empty = render_console_report(report, [])
    assert "TRANSPORTS" not in res_empty

    # Dict with unknown keys only -> no TRANSPORTS header
    report["devices"] = {"by_state": {"custom_unknown": 3}}
    res_unknown = render_console_report(report, [])
    assert "TRANSPORTS" not in res_unknown

    # Normal by_state -> prints header and counts
    report["devices"] = {"by_state": {"device": 2, "offline": 1}}
    res_normal = render_console_report(report, [])
    assert "TRANSPORTS / reported connection counts" in res_normal
    assert "device=2  offline=1" in res_normal


# =====================================================================
# 04-R5: Recovery paths and error handling
# =====================================================================


def test_run_console_split_arguments_value_error_shows_message_and_keeps_running(terminal):
    idx = tips_command_index()
    # Send control character \x01 in arguments -> split_arguments raises ValueError -> message displayed -> 'q' to exit
    out, err, _ = run_keys(f"8\n{idx}\nr\nbad\x01cmd\nq\n", terminal)
    assert "Arguments cannot contain terminal control characters." in out


def test_run_console_subprocess_oserror_shows_status_and_recovers(terminal, monkeypatch):
    monkeypatch.setattr(ui.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("mock oserror")))
    idx = tips_command_index()
    # Enter runnable -> empty args -> subprocess raises OSError -> prompt shows error -> Enter (Back) -> 'q' exits
    out, err, _ = run_keys(f"8\n{idx}\nr\n\n\nq\n", terminal)
    assert "Could not start the selected ADAPT command." in out
    # Console did not crash and returned to detail screen
    assert "ADAPT / TIPS" in out.split("Could not start the selected ADAPT command.")[1]


def test_run_console_subprocess_keyboard_interrupt_shows_status(terminal, monkeypatch):
    monkeypatch.setattr(ui.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    idx = tips_command_index()
    # Enter runnable -> empty args -> subprocess raises KeyboardInterrupt -> prompt shows error -> 'q' exits
    out, err, _ = run_keys(f"8\n{idx}\nr\n\nq\n", terminal)
    assert "Command interrupted." in out


def test_render_console_report_no_color_emits_no_ansi(monkeypatch):
    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.setenv("NO_COLOR", "")
    plain = render_console_report(fixture_report(), [], color=True)
    assert "\x1b" not in plain


def test_render_console_report_fallback_to_devices_state_check():
    # Top-level 'devices' key absent, fallback to check id 'devices_state'
    report = {
        "status": "ok",
        "exit_code": 0,
        "host": {"os": "windows", "arch": "AMD64"},
        "toolchain": {"adb_path": "adb.exe", "platform_tools_version": "36.0.0", "origin": "user-scope"},
        "checks": [
            {
                "id": "devices_state",
                "status": "pass",
                "summary": "1 connected",
                "details": {"by_state": {"device": 1, "offline": 0}},
            }
        ],
    }
    rendered = render_console_report(report, [])
    assert "TRANSPORTS / reported connection counts" in rendered
    assert "device=1  offline=0" in rendered


def test_render_console_report_shows_the_adb_commands_use_when_none_is_on_path():
    report = {
        "status": "ok", "exit_code": 0, "host": {"os": "windows", "arch": "x86_64"},
        "toolchain": {"adb_path": None, "platform_tools_version": None, "origin": "none",
                      "command_adb_path": "D:/tools/platform-tools/adb.exe",
                      "command_adb_version": "35.0.2", "command_adb_origin": "google_official"},
        "checks": [],
    }
    text = render_console_report(report, [], width=120, color=False)
    assert "Not found" not in text
    assert "D:/tools/platform-tools/adb.exe (used by ADAPT; not on PATH)" in text
    assert "ADB Version: 35.0.2" in text and "Origin: google_official" in text

    report["toolchain"].update(command_adb_path=None, command_adb_version=None, command_adb_origin=None)
    assert "ADB Path: Not found" in render_console_report(report, [], width=120, color=False)
