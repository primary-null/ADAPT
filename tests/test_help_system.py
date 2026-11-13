"""Offline guide coverage, contextual selection, and preference boundaries."""

import io
import json

import pytest
from typer.testing import CliRunner

from adapt_adb.cli import app
from adapt_adb import help_system as help_mod


@pytest.fixture(autouse=True)
def isolated_preferences(monkeypatch, tmp_path):
    monkeypatch.setenv("ADAPT_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("ADAPT_TIPS", raising=False)


def test_full_guide_lists_registered_nested_commands_without_writes():
    result = CliRunner().invoke(app, ["help-me", "--all", "--json"])
    assert result.exit_code == 0 and result.stderr == ""
    data = json.loads(result.stdout)
    commands = {r["command"] for r in data["commands"]}
    assert {"adapt tools plan", "adapt dev-options apply", "adapt mcp", "adapt help-me"} <= commands
    assert not help_mod.preferences_path().exists()


def test_how_to_and_unknown_topic_are_actionable_and_offline():
    runner = CliRunner()
    result = runner.invoke(app, ["help-me", "wireless"])
    assert result.exit_code == 0
    assert "adapt pair IP:PAIR_PORT" in result.stdout and "adapt connect IP:CONNECT_PORT" in result.stdout
    result = runner.invoke(app, ["help-me", "not-a-tool", "--json"])
    assert result.exit_code == 2 and result.stderr == ""
    assert json.loads(result.stdout)["error"] == "unknown_topic"


def test_tips_setting_persists_and_preserves_other_preferences():
    path = help_mod.preferences_path()
    path.parent.mkdir()
    path.write_text('{"other_setting": 42}', encoding="utf-8")
    runner = CliRunner()
    assert runner.invoke(app, ["tips", "off"]).exit_code == 0
    assert help_mod.read_preferences() == {"other_setting": 42, "tips": False}
    assert json.loads(runner.invoke(app, ["tips", "status", "--json"]).stdout)["tips"] is False
    assert runner.invoke(app, ["tips", "on"]).exit_code == 0
    assert help_mod.tips_enabled()


def test_nested_command_selects_its_own_tip(monkeypatch):
    selected = []
    monkeypatch.setattr(help_mod, "emit_context_tip", lambda topic, ctx=None: selected.append(topic))
    result = CliRunner().invoke(app, ["tools", "info", "scrcpy"])
    assert result.exit_code == 0
    assert selected == ["tools info"]


def test_tips_are_human_only_and_do_not_override_machine_modes(monkeypatch):
    class Terminal(io.StringIO):
        def isatty(self):
            return True

    out = Terminal()
    for stream in ("stdin", "stdout", "stderr"):
        monkeypatch.setattr(help_mod.sys, stream, out)
    ctx = help_mod.click.Context(help_mod.GuidedGroup(name="adapt"))
    help_mod.emit_context_tip("tools plan", ctx)
    assert "HOW TO / tools plan" in out.getvalue()
    for mode in ("flag", "json", "env", "mcp"):
        out.seek(0); out.truncate()
        ctx.params["no_tips"] = mode == "flag"
        ctx.meta["adapt_machine_output"] = mode == "json"
        monkeypatch.setenv("ADAPT_TIPS", "0" if mode == "env" else "1")
        help_mod.emit_context_tip("mcp" if mode == "mcp" else "tools plan", ctx)
        assert out.getvalue() == ""


def test_invalid_preferences_cannot_break_normal_operation():
    path = help_mod.preferences_path()
    path.parent.mkdir()
    path.write_text("not json", encoding="utf-8")
    assert help_mod.tips_enabled() is False
    result = CliRunner().invoke(app, ["tips", "status", "--json"])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"] == "tips_preference"
