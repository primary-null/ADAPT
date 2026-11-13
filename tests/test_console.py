"""Human presentation keeps diagnostic evidence and terminal boundaries."""

import re

from adapt_adb.console import render_console_report


def fixture_report():
    return {"status": "unsafe_shared_server", "exit_code": 3,
            "host": {"os": "windows", "arch": "AMD64"},
            "toolchain": {"adb_path": "C:/Android Tools/adb.exe", "platform_tools_version": "36.0.0", "origin": "user-scope"},
            "checks": [{"id": "adb_server_5037", "status": "fail", "summary": "Owner unavailable", "remediation": "Verify the listening daemon."}],
            "devices": {"by_state": {"device": 0, "offline": 1}}}


def test_failure_evidence_and_actions_survive_narrow_render():
    report = fixture_report()
    actions = [{"key": "q", "label": "Done"}]
    result = render_console_report(report, actions, width=36, show_fix=True)
    readable = " ".join(result.split())
    assert "BLOCKED" in result
    assert "Owner unavailable" in readable
    assert "device=0" in result and "offline=1" in result
    assert "[q] Done" in result
    assert "Verify the listening daemon." in readable
    assert max(map(len, result.splitlines())) <= 36


def test_report_content_cannot_inject_terminal_controls():
    report = fixture_report()
    report["checks"][0]["summary"] = "bad\x1b[2J\rtitle"
    result = render_console_report(report, [], color=False)
    assert "\x1b" not in result and "\r" not in result
    assert r"\x1b[2J\x0dtitle" in result


def test_no_color_removes_styles_without_changing_evidence(monkeypatch):
    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    styled = render_console_report(fixture_report(), [], color=True)
    assert "\x1b[31mBLOCKED\x1b[0m" in styled
    monkeypatch.setenv("NO_COLOR", "1")
    plain = render_console_report(fixture_report(), [], color=True)
    assert "\x1b" not in plain
    assert re.sub(r"\x1b\[[0-9;]*m", "", styled) == plain
