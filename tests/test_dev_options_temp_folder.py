"""`adapt dev-options` names the problem when the system temporary folder cannot be used.

Offline: adb, the device identity and the lock folder are faked; nothing reaches a device.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from adapt_adb import dev_options
from adapt_adb import failure_report as fr
from adapt_adb.cli import app
from adapt_adb.dev_options import DevOptionsError, DeviceIdentity

runner = CliRunner()
FIRST_LINE = "Error: Cannot use the system temporary folder for the per-device transaction lock."


def _no_adb(*args, **kwargs):
    pytest.fail("no adb command may run once the lock folder is unusable")


@pytest.fixture
def phone(monkeypatch):
    monkeypatch.setattr(dev_options, "_resolve_adb_path", lambda adb_path: Path("adb"))
    monkeypatch.setattr(dev_options, "_read_identity", lambda adb, serial: DeviceIdentity("RS1", "brand/fp", 0))
    monkeypatch.setattr(dev_options, "_run_adb", _no_adb)


@pytest.fixture(params=["blocked", "none"])
def unusable_temp(request, monkeypatch, tmp_path):
    if request.param == "blocked":
        blocker = tmp_path / "not-a-folder"
        blocker.write_text("x", encoding="utf-8")
        monkeypatch.setattr(dev_options, "_LOCK_ROOT", blocker / "adapt-adb-dev-options-locks")
    else:  # no temporary folder could be found at all
        monkeypatch.setattr(dev_options, "_LOCK_ROOT", None)


def _apply(tmp_path, *extra):
    return runner.invoke(app, ["dev-options", "apply", "--profile", "fast-ui", "-s", "SERIAL1",
                               "--snapshot", str(tmp_path / "fast.json"), *extra])


def test_library_raises_a_classified_error(phone, unusable_temp, tmp_path):
    with pytest.raises(DevOptionsError) as caught:
        dev_options.apply_profile("fast-ui", "SERIAL1", tmp_path / "fast.json")
    assert str(caught.value) == FIRST_LINE.removeprefix("Error: ")
    assert fr.classify(caught.value, area="dev_options") == "devopts.lock_unavailable"
    assert not (tmp_path / "fast.json").exists()


def test_human_mode_names_the_temp_folder_and_points_to_the_guide(phone, unusable_temp, tmp_path):
    result = _apply(tmp_path)
    assert result.exit_code == 1
    assert result.stderr.splitlines()[0] == FIRST_LINE
    assert "stopped unexpectedly" not in result.stderr
    assert "adapt explain devopts.lock_unavailable" in result.stderr
    assert not (tmp_path / "fast.json").exists()


def test_json_mode_carries_the_error_and_the_guide(phone, unusable_temp, tmp_path, capsys):
    # `dev-options apply` and `restore` (the commands that take the lock) have no --json
    # option; this is the JSON error emitter dev-options uses for its --json output.
    with pytest.raises(DevOptionsError) as caught:
        dev_options.apply_profile("fast-ui", "SERIAL1", tmp_path / "fast.json")
    dev_options._emit_error(caught.value, json_output=True, command="dev-options apply")
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    assert doc["ok"] is False
    assert doc["error"] == "concurrent_operation"
    assert doc["message"] == FIRST_LINE.removeprefix("Error: ")
    assert doc["failure"]["id"] == "devopts.lock_unavailable"
    assert captured.err == ""


def test_guide_text_stays_within_the_safety_rules():
    text = fr.render_explain(fr.resolve("devopts.lock_unavailable")).lower()
    for banned in ("kill-server", "sudo adb", "adb root", "adbkey", "taskkill", "pkill"):
        assert banned not in text


def test_default_lock_root_is_none_when_no_temp_folder_is_usable(monkeypatch):
    def broken():
        raise FileNotFoundError("No usable temporary directory found")

    monkeypatch.setattr(dev_options.tempfile, "gettempdir", broken)
    assert dev_options._default_lock_root() is None
