"""Test CLI help output."""

from typer.testing import CliRunner

from adapt_adb.cli import app

runner = CliRunner()


def test_cli_help():
    """Verify that `adapt --help` lists doctor, setup, devices, mcp, and adb."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ["doctor", "setup", "devices", "mcp", "adb"]:
        assert cmd in result.output
