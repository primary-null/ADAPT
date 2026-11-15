"""`adapt --version`: an eager root option, offline, with no banner, tips, progress or adb access."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

import adapt_adb.cli as cli_mod
from adapt_adb import __version__
from adapt_adb.cli import app

runner = CliRunner()


@pytest.fixture
def no_adb(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("--version must not touch adb")

    for name in ("resolve_adb", "run_doctor_checks", "preflight_adb_server", "emit_banner", "emit_context_tip"):
        monkeypatch.setattr(cli_mod, name, boom)
    monkeypatch.setattr(cli_mod, "_privilege_warning_text", lambda: "should not be printed")


def test_version_prints_name_and_version_and_exits_0(no_adb):
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout == f"adapt {__version__}\n"
    assert result.stderr == ""


def test_version_wins_over_a_following_command(no_adb):
    result = runner.invoke(app, ["--no-banner", "--version", "doctor"])
    assert result.exit_code == 0
    assert result.stdout == f"adapt {__version__}\n"


def test_version_is_listed_as_a_root_option_not_a_command():
    help_text = runner.invoke(app, ["--help"]).stdout
    assert "--version" in help_text
    assert "version" not in {c.name for c in app.registered_commands}
