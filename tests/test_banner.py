"""Startup banner: rich for TTY humans, silent for machine-facing modes."""

import re
import sys

from typer.testing import CliRunner

from adapt_adb.banner import render_banner, should_show_banner
from adapt_adb.cli import app

runner = CliRunner()
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_BLOCKS = set("█▓▒░")


def _plain(text: str) -> str:
    return _ANSI.sub("", text)


def _combined(result) -> str:
    return _plain(f"{result.output}{result.stderr or ''}{result.stdout or ''}")


def test_plain_banner_is_filled_readable_and_quiet(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    text = render_banner(width=80, color=False, encoding="ascii")
    lines = text.splitlines()

    assert "Android Device Agent Provisioning Toolkit" not in text
    assert "ANDROID DEVICE AGENT PROVISIONING TOOLKIT" in text
    assert sum(line.count("#") for line in lines[:5]) == 85
    assert not (_BLOCKS & set(text))
    assert "--no-banner" not in text
    assert "ADAPT_NO_BANNER" not in text
    assert text.endswith("\n")


def test_rich_banner_has_blocks_and_green_to_gray_gradient(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("TERM", raising=False)
    text = render_banner(width=80, color=True, encoding="utf-8")

    assert "█" in text and "▓" in text and "▒" in text
    assert "\x1b[38;5;46m" in text
    assert "\x1b[38;5;120m" in text
    assert "\x1b[97m" in text
    assert "\x1b[37m" in text
    assert "\x1b[90m" in text
    assert _plain(text).endswith("ANDROID DEVICE AGENT PROVISIONING TOOLKIT\n")


def test_ascii_encoding_forces_readable_fallback(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    text = render_banner(width=41, color=False, encoding="ascii")
    assert "#" in text
    assert not (_BLOCKS & set(text))


def test_no_color_wins_over_explicit_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    text = render_banner(width=80, color=True, encoding="utf-8")
    assert "\x1b[" not in text


def test_limited_widths_never_overflow(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    for width in (1, 5, 13, 20, 32, 33, 40, 41, 80):
        text = _plain(render_banner(width=width, color=True, encoding="ascii"))
        assert all(len(line) <= width for line in text.splitlines())
    assert "ADAPT" in render_banner(width=20, color=False, encoding="ascii")
    assert "DEVICE AGENT TOOLKIT" in render_banner(
        width=32, color=False, encoding="ascii"
    )


def test_help_lists_no_banner_flag():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "--no-banner" in _combined(result)


def test_non_tty_help_has_no_art():
    result = runner.invoke(app, ["--help"])
    assert "ANDROID DEVICE AGENT PROVISIONING TOOLKIT" not in _combined(result)


def test_doctor_prints_art_when_enabled(monkeypatch):
    monkeypatch.setattr(
        "adapt_adb.banner.should_show_banner",
        lambda ctx, no_banner=False: True,
    )
    result = runner.invoke(app, ["doctor"])
    assert "ANDROID DEVICE AGENT PROVISIONING TOOLKIT" in _combined(result)
    assert "ANDROID DEVICE AGENT PROVISIONING TOOLKIT" in (result.stderr or "")
    assert "ANDROID DEVICE AGENT PROVISIONING TOOLKIT" not in (result.stdout or "")
    assert "--no-banner" not in _combined(result)


def test_no_banner_flag_hides_art(monkeypatch):
    monkeypatch.setattr(
        "adapt_adb.banner.should_show_banner",
        lambda ctx, no_banner=False: not no_banner,
    )
    result = runner.invoke(app, ["--no-banner", "doctor"])
    assert "ANDROID DEVICE AGENT PROVISIONING TOOLKIT" not in _combined(result)


def test_env_hides_art_on_tty(monkeypatch):
    monkeypatch.setenv("ADAPT_NO_BANNER", "1")
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    ctx = type("Ctx", (), {"invoked_subcommand": "doctor"})()
    assert should_show_banner(ctx, no_banner=False) is False


def test_legacy_adapt_banner_zero_hides_art(monkeypatch):
    monkeypatch.setenv("ADAPT_BANNER", "0")
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    ctx = type("Ctx", (), {"invoked_subcommand": "doctor"})()
    assert should_show_banner(ctx, no_banner=False) is False


def test_term_dumb_hides_art(monkeypatch):
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    ctx = type("Ctx", (), {"invoked_subcommand": "doctor"})()
    assert should_show_banner(ctx, no_banner=False) is False


def test_non_tty_hides_art(monkeypatch):
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False)
    ctx = type("Ctx", (), {"invoked_subcommand": "doctor"})()
    assert should_show_banner(ctx, no_banner=False) is False


def test_mcp_never_prints_art_even_when_policy_is_forced(monkeypatch):
    monkeypatch.setattr(
        "adapt_adb.banner.should_show_banner",
        lambda ctx, no_banner=False: True,
    )
    result = runner.invoke(app, ["mcp"])
    assert "ANDROID DEVICE AGENT PROVISIONING TOOLKIT" not in _combined(result)
    assert "ANDROID DEVICE AGENT PROVISIONING TOOLKIT" not in (result.stdout or "")


def test_should_show_skips_json(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["adapt", "doctor", "--json"])
    ctx = type("Ctx", (), {"invoked_subcommand": "doctor"})()
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    assert should_show_banner(ctx, no_banner=False) is False


def test_meta_adapt_machine_output_hides_banner(monkeypatch):
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    ctx = type("Ctx", (), {"invoked_subcommand": "doctor", "meta": {"adapt_machine_output": True}})()
    assert should_show_banner(ctx, no_banner=False) is False

