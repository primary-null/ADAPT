"""Failure guidance Phases 2 and 3: kind-carrying errors, doctor pointers, setup, console, leftovers.

Every wired site keeps its first error line byte-identical and its exit code; guidance follows
on stderr (human) or as an additive `failure` key (JSON, MCP). CliRunner is not a terminal, so
the default is the one-line short form; "tty" tests patch the stderr TTY check. Offline only:
adb, the shared-server preflight, devices and scrcpy are faked.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import subprocess
from pathlib import Path

import pytest
from typer.main import get_command
from typer.testing import CliRunner

import adapt_adb.cli as cli
from adapt_adb import android_support, dev_options, dex, dex_receiver
from adapt_adb import failure_guide as fg
from adapt_adb import failure_report as fr
from adapt_adb import terminal_ui as ui
from adapt_adb.android_support import AndroidSupportError
from adapt_adb.cli import app
from adapt_adb.console import render_console_report
from adapt_adb.dev_options import AdbCommandError, DeviceIdentity, DevOptionsError
from adapt_adb.dex import DexError
from adapt_adb.doctor import render_doctor_human

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[1]
APPLY_LINE = "Error: Profile apply failed; rollback was attempted and the snapshot was retained.\n"


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


def _more_lines(stderr: str) -> list[str]:
    return [line for line in stderr.splitlines() if line.startswith("More: ")]


# ---------------------------------------------------------------------------
# A fake phone for dev-options apply: settings get/put/delete only
# ---------------------------------------------------------------------------

class FakePhone:
    def __init__(self, values: dict[str, str], *, deny_put: str | None = None, offline_after_denial: bool = False):
        self.values = dict(values)
        self.deny_put = deny_put
        self.offline_after_denial = offline_after_denial
        self.denied = False

    def __call__(self, adb_path, serial, args):
        args = list(args)
        if args[:2] == ["shell", "getprop"]:  # the best-effort Samsung check after a failed write
            return ""
        assert args[:2] == ["shell", "settings"], args
        args = args[2:]
        if args[0] == "--user":
            args = args[2:]
        action, _table, key = args[:3]
        if self.denied and self.offline_after_denial:
            raise AdbCommandError("adb_error", "error: device offline")
        if action == "get":
            return self.values.get(key, "null")
        if action == "put":
            if key == self.deny_put:
                self.denied = True
                raise AdbCommandError("denied", "Permission denial: writing requires a permission")
            self.values[key] = args[3]
            return ""
        self.values.pop(key, None)
        return ""


ANIMATIONS = {"window_animation_scale": "1", "transition_animation_scale": "1", "animator_duration_scale": "1"}


@pytest.fixture
def phone(monkeypatch):
    def install(fake: FakePhone) -> FakePhone:
        monkeypatch.setattr(dev_options, "_resolve_adb_path", lambda adb_path: Path("adb"))
        monkeypatch.setattr(dev_options, "_read_identity", lambda adb, serial: DeviceIdentity("RS1", "brand/fp", 0))
        monkeypatch.setattr(dev_options, "_device_lock", lambda identity: contextlib.nullcontext())
        monkeypatch.setattr(dev_options, "_run_adb", fake)
        return fake
    return install


def _apply(tmp_path, *extra):
    return runner.invoke(app, ["dev-options", "apply", "--profile", "fast-ui", "-s", "SERIAL1",
                               "--snapshot", str(tmp_path / "fast.json"), *extra])


def test_apply_failure_with_a_confirmed_rollback_is_identified(phone, tmp_path):
    fake = phone(FakePhone(ANIMATIONS, deny_put="transition_animation_scale"))
    with pytest.raises(DevOptionsError) as caught:
        dev_options.apply_profile("fast-ui", "SERIAL1", tmp_path / "direct.json")
    assert caught.value.kind == "apply_failed"
    assert fr.classify(caught.value) == "devopts.apply_rolled_back"
    assert fake.values == ANIMATIONS
    states = {e["name"]: e["state"] for e in caught.value.report["settings"]}
    assert "error" not in states.values()


def test_apply_failure_with_an_unconfirmed_rollback_stays_conservative(phone, tmp_path):
    phone(FakePhone(ANIMATIONS, deny_put="transition_animation_scale", offline_after_denial=True))
    with pytest.raises(DevOptionsError) as caught:
        dev_options.apply_profile("fast-ui", "SERIAL1", tmp_path / "direct.json")
    assert "error" in {e["state"] for e in caught.value.report["settings"]}
    assert fr.classify(caught.value) == "devopts.apply_rollback_incomplete"


def test_apply_cli_keeps_the_first_line_and_adds_the_pointer(phone, tmp_path):
    phone(FakePhone(ANIMATIONS, deny_put="transition_animation_scale"))
    result = _apply(tmp_path)
    assert result.exit_code == 1
    assert result.stderr.startswith(APPLY_LINE)
    assert _more_lines(result.stderr) == ["More: adapt explain devopts.apply_rolled_back"]


def test_apply_guidance_says_how_to_check_the_phone_and_the_snapshot(phone, tmp_path, tty):
    phone(FakePhone(ANIMATIONS, deny_put="transition_animation_scale", offline_after_denial=True))
    result = _apply(tmp_path)
    assert result.exit_code == 1 and result.stderr.startswith(APPLY_LINE)
    text = result.stderr
    assert "\nWhy: " in text and "\nTry:\n" in text
    assert "adapt dev-options inspect -s SERIAL" in text
    assert "snapshot file" in text and "'rolled_back' or 'error'" in text
    assert "adapt dev-options restore -s SERIAL --snapshot SNAPSHOT_FILE" in text
    assert 'More: adapt explain devopts.apply_rollback_incomplete   |   adapt help-me "dev-options restore"' in text
    assert "SERIAL1" not in text.replace(APPLY_LINE, "")


@pytest.mark.parametrize("failure_id", ["devopts.apply_rolled_back", "devopts.apply_rollback_incomplete",
                                        "devopts.restore_failed", "devopts.snapshot_io"])
def test_state_changing_failures_name_the_inspect_command_and_the_snapshot(failure_id):
    guide = fg.GUIDES[failure_id]
    commands = [s.command for s in guide.steps if s.command]
    assert "adapt dev-options inspect -s SERIAL" in commands
    assert any("snapshot" in s.text.lower() or "snapshot" in (s.command or "").lower() for s in guide.steps)


def test_dev_options_inspect_json_carries_the_specific_failure(monkeypatch):
    def deny(serial):
        raise DevOptionsError("denied", "Permission denial")
    monkeypatch.setattr(dev_options, "inspect_device", deny)
    result = runner.invoke(app, ["dev-options", "inspect", "-s", "SERIAL1", "--json"])
    assert result.exit_code == 1 and result.stderr == ""
    payload = json.loads(result.stdout)
    assert list(payload)[:3] == ["ok", "error", "message"]  # original order kept, failure appended
    assert payload["failure"]["id"] == "device.command_denied"
    assert {k: payload[k] for k in ("ok", "error", "message")} == {"ok": False, "error": "denied", "message": "Permission denial"}


@pytest.mark.parametrize("status,failure_id", [("restore_conflict", "devopts.restore_conflict"),
                                               ("restore_partial", "devopts.restore_partial")])
def test_restore_conflicts_get_guidance_after_the_unchanged_lines(status, failure_id, monkeypatch):
    report = {"ok": False, "status": status, "conflicts": ["stay_on_while_plugged_in: current value differs"],
              "note": "No settings were changed because restore preflight found conflicts."}
    monkeypatch.setattr(dev_options, "restore_snapshot", lambda *a, **k: report)
    result = runner.invoke(app, ["dev-options", "restore", "-s", "SERIAL1", "--snapshot", "snap.json"])
    assert result.exit_code == 1
    assert result.stderr.startswith("Conflict: stay_on_while_plugged_in: current value differs\n"
                                    "No settings were changed because restore preflight found conflicts.\n")
    assert _more_lines(result.stderr) == [f"More: adapt explain {failure_id}"]


# ---------------------------------------------------------------------------
# Kind classification: every mapped kind and the per-site overrides
# ---------------------------------------------------------------------------

_CLASSES = {"dev_options": DevOptionsError, "dex": DexError, "android_support": AndroidSupportError}


@pytest.mark.parametrize("module,kind", sorted(fr.KIND_MAP))
def test_every_kind_classifies_to_its_mapped_guide(module, kind):
    exc = _CLASSES[module](kind, "message")
    assert fr.classify(exc) == fr.KIND_MAP[(module, kind)]
    assert fr.KIND_MAP[(module, kind)] in fg.GUIDES


def test_failure_id_keyword_overrides_the_kind_map():
    assert fr.classify(AdbCommandError("missing_adb", "adb binary was not found.", failure_id="adb.exec_failed")) == "adb.exec_failed"
    assert fr.classify(DexError("scrcpy_version", "x", failure_id="scrcpy.too_old")) == "scrcpy.too_old"
    assert fr.classify(AndroidSupportError("adb_error", "x", failure_id="adb.exec_failed")) == "adb.exec_failed"
    # backward compatible: positional use is unchanged
    assert DevOptionsError("snapshot_io", "x", {"a": 1}).report == {"a": 1}
    assert "failure_id" not in DexError("timeout", "x").__dict__


def test_unsafe_server_wrapped_by_a_kind_error_keeps_the_specific_server_guide():
    from adapt_adb.doctor import UnsafeAdbServerError
    try:
        try:
            raise fr.tagged(UnsafeAdbServerError("x"), "adb.server.version_mismatch")
        except UnsafeAdbServerError as inner:
            raise DexError("unsafe_adb_server", "ADB server ownership/version could not be verified.") from inner
    except DexError as exc:
        assert fr.classify(exc) == "adb.server.version_mismatch"


def test_scrcpy_version_sites_are_split(monkeypatch):
    old = lambda argv, timeout: subprocess.CompletedProcess(argv, 0, "scrcpy 1.25 <https://github.com/Genymobile/scrcpy>", "")
    with pytest.raises(DexError) as caught:
        dex._check_scrcpy_version(Path("scrcpy"), old)
    assert fr.classify(caught.value) == "scrcpy.too_old"
    garbled = lambda argv, timeout: subprocess.CompletedProcess(argv, 0, "something else", "")
    with pytest.raises(DexError) as caught:
        dex._check_scrcpy_version(Path("scrcpy"), garbled)
    assert fr.classify(caught.value) == "scrcpy.version_unverified"


def test_adb_vanishing_at_spawn_is_an_exec_failure(monkeypatch):
    def gone(argv, timeout):
        raise FileNotFoundError(2, "No such file")
    with pytest.raises(DexError) as caught:
        dex._probe_adb(Path("adb"), "SERIAL1", ["shell", "getprop", "x"], runner=gone)
    assert caught.value.kind == "missing_adb" and fr.classify(caught.value) == "adb.exec_failed"
    monkeypatch.setattr(android_support, "_run_process", lambda args, timeout: (_ for _ in ()).throw(PermissionError(13, "denied")))
    with pytest.raises(AndroidSupportError) as caught:
        android_support._checked_adb_property(Path("adb"), "SERIAL1", "ro.build.version.sdk")
    assert caught.value.kind == "adb_error" and fr.classify(caught.value) == "adb.exec_failed"


def test_missing_snapshot_file_is_unreadable_not_malformed(tmp_path):
    with pytest.raises(DevOptionsError) as caught:
        dev_options._load_snapshot(tmp_path / "missing.json")
    assert fr.classify(caught.value) == "devopts.snapshot_unreadable"
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(DevOptionsError) as caught:
        dev_options._load_snapshot(bad)
    assert caught.value.kind == "malformed_snapshot" and fr.classify(caught.value) == "devopts.snapshot_malformed"


# ---------------------------------------------------------------------------
# dex and android CLI surfaces
# ---------------------------------------------------------------------------

UNREACHABLE = ("The device did not answer any read-only query; it may be disconnected, offline, unauthorized, "
               "or the serial may be wrong. Run 'adapt devices' to check its state.")


def _unreachable(serial, **kwargs):
    raise DexError("device_unreachable", UNREACHABLE)


def test_dex_inspect_unreachable_human_short_and_full(monkeypatch, tty):
    monkeypatch.setattr(dex, "inspect_device", _unreachable)
    result = runner.invoke(app, ["dex", "inspect", "-s", "SERIAL1"])
    assert result.exit_code == 1
    assert result.stderr.startswith(f"Error: {UNREACHABLE}\n")
    assert "\nWhy: The device did not answer any of ADAPT's read-only queries" in result.stderr
    assert "adapt devices" in result.stderr and "More: adapt explain device.unreachable" in result.stderr


def test_dex_json_errors_stay_sorted_and_silent(monkeypatch):
    monkeypatch.setattr(dex, "inspect_device", _unreachable)
    result = runner.invoke(app, ["dex", "inspect", "-s", "SERIAL1", "--json"])
    assert result.exit_code == 1 and result.stderr == ""
    assert list(json.loads(result.stdout)) == ["error", "failure", "message", "ok"]
    assert json.loads(result.stdout)["failure"]["id"] == "device.unreachable"


def test_dex_mirror_confirmation_has_its_own_guide():
    result = runner.invoke(app, ["dex", "mirror", "-s", "SERIAL1", "--dex-display", "2"])
    assert result.exit_code == 1
    assert result.stderr.startswith("Error: Mirror launch requires explicit --yes.")
    assert _more_lines(result.stderr) == ["More: adapt explain dex.confirmation_required"]


def test_android_capabilities_human_and_json(monkeypatch):
    def deny(serial):
        raise AndroidSupportError("denied", "ADB property inspection was denied.")
    monkeypatch.setattr(android_support, "inspect_capabilities", deny)
    human = runner.invoke(app, ["android", "capabilities", "-s", "SERIAL1"])
    assert human.exit_code == 1 and human.stderr.startswith("Error: ADB property inspection was denied.\n")
    assert _more_lines(human.stderr) == ["More: adapt explain device.command_denied"]
    machine = runner.invoke(app, ["android", "capabilities", "-s", "SERIAL1", "--json"])
    assert machine.exit_code == 1 and machine.stderr == ""
    assert json.loads(machine.stdout)["failure"]["id"] == "device.command_denied"


# ---------------------------------------------------------------------------
# MCP: specific failure objects for the three inspection tools
# ---------------------------------------------------------------------------

def _mcp(tool, monkeypatch, target, exc):
    pytest.importorskip("mcp")
    from adapt_adb.mcp.server import dispatch_tool_call

    def boom(*args, **kwargs):
        raise exc
    monkeypatch.setattr(target, boom)
    result = dispatch_tool_call(tool, {"device": "SERIAL_A"})
    return result.is_error, json.loads(result.content[-1].text)


def test_mcp_dex_inspect_failure_is_specific_and_keeps_its_message(monkeypatch):
    is_error, payload = _mcp("dex_inspect", monkeypatch, "adapt_adb.dex.inspect_device",
                             DexError("device_unreachable", UNREACHABLE))
    assert is_error is True
    assert payload["error"] == "device_unreachable"
    assert payload["message"] == "DeX inspection failed; run the targeted CLI inspection for details."
    assert payload["failure"]["id"] == "device.unreachable"
    assert payload["failure"]["next_tool"] == {"name": "list_devices", "arguments": {}}
    assert payload["failure"]["user_action_required"] is True


def test_mcp_dex_displays_failure_is_specific(monkeypatch):
    pytest.importorskip("mcp")
    from adapt_adb.mcp.server import dispatch_tool_call
    monkeypatch.setattr("adapt_adb.dex.list_displays", lambda serial: (_ for _ in ()).throw(DexError("timeout", "x")))
    payload = json.loads(dispatch_tool_call("dex_displays", {"device": "SERIAL_A"}).content[-1].text)
    assert payload["failure"]["id"] == "adb.timeout"


def test_mcp_android_and_dev_options_failures_are_specific(monkeypatch):
    _, payload = _mcp("android_capabilities", monkeypatch, "adapt_adb.android_support.inspect_capabilities",
                      AndroidSupportError("malformed_sdk", "Device reported a malformed Android SDK level."))
    assert payload["error"] == "malformed_sdk" and payload["failure"]["id"] == "android.property_malformed"
    _, payload = _mcp("developer_options_inspect", monkeypatch, "adapt_adb.dev_options.inspect_device",
                      DevOptionsError("identity_unavailable", "Device identity is unavailable."))
    assert payload["error"] == "identity_unavailable" and payload["failure"]["id"] == "device.identity_unavailable"
    assert payload["message"] == "Developer Options inspection failed; run the targeted CLI inspection for details."


# ---------------------------------------------------------------------------
# Doctor renderers and setup
# ---------------------------------------------------------------------------

def _report() -> dict:
    return {"host": {"os": "windows", "arch": "AMD64"},
            "toolchain": {"adb_path": "adb", "platform_tools_version": "35.0.2", "origin": "google_official"},
            "status": "unsafe_shared_server", "exit_code": 3,
            "checks": [
                {"id": "adb_server_5037", "status": "fail", "summary": "Listening ADB server ownership/version is unverified or unsafe.",
                 "remediation": fg.doctor_remediation_text("adb.server.unverified"), "failure_id": "adb.server.unverified",
                 "details": {}},
                {"id": "adb_origin", "status": "warn", "summary": "origin unverified",
                 "remediation": fg.doctor_remediation_text("adb.origin_unverified"), "failure_id": "adb.origin_unverified",
                 "details": {}},
                {"id": "devices_state", "status": "skip", "summary": "skipped", "remediation": None, "failure_id": None,
                 "details": {"total": 0, "by_state": {}}},
            ]}


def test_plain_doctor_report_names_every_blocked_item_in_one_line(monkeypatch):
    monkeypatch.setattr("adapt_adb.doctor._is_stdout_tty", lambda: False)
    text = render_doctor_human(_report())
    assert text.endswith("\n\nMore: adapt explain adb.server.unverified  |  also: adb.origin_unverified\n"
                         "Run 'adapt doctor --fix' to see how to fix the items above.")
    fixed = render_doctor_human(_report(), show_fix=True)
    assert ("- (adb_server_5037) Verify the existing daemon owner and exact platform-tools version before using it. "
            "(adapt explain adb.server.unverified)") in fixed
    assert "More: adapt explain" not in fixed


def test_terminal_doctor_report_points_under_each_blocked_check():
    text = render_console_report(_report(), [], width=100)
    lines = text.splitlines()
    first = next(i for i, line in enumerate(lines) if "ADB daemon /" in line)
    assert lines[first + 1] == "           More: adapt explain adb.server.unverified"
    assert "           More: adapt explain adb.origin_unverified" in lines
    assert text.endswith("Run 'adapt doctor --fix' to see how to fix the items above.")
    assert "More: adapt explain" not in render_console_report(_report(), [], width=100, explain=False)


def test_failure_guidance_off_removes_doctor_pointers(monkeypatch):
    monkeypatch.setattr("adapt_adb.doctor._is_stdout_tty", lambda: False)
    monkeypatch.setenv("ADAPT_FAILURE_GUIDANCE", "off")
    text = render_doctor_human(_report())
    assert "adapt explain" not in text
    assert text.endswith("\n\nRun 'adapt doctor --fix' to see how to fix the items above.")
    assert "adapt explain" not in render_doctor_human(_report(), show_fix=True)


def test_doctor_json_checks_all_carry_failure_id(monkeypatch):
    from tests.test_doctor_failure_ids import win_env
    monkeypatch.setattr("adapt_adb.cli._ACTIVE_DOCTOR_ENV", win_env())
    result = runner.invoke(app, ["doctor", "--json"])
    report = json.loads(result.stdout)
    assert result.stderr == "" and all("failure_id" in check for check in report["checks"])
    assert "failure" not in report  # the report itself is the diagnosis


def test_setup_abort_takes_the_guide_from_the_check_not_its_summary():
    report = {"setup": {"action": "aborted_unsafe_server"},
              "checks": [{"id": "adb_server_5037", "status": "fail", "failure_id": "adb.server.version_mismatch",
                          "summary": "Selected non-default ADB server endpoint is unverified.", "details": {}}]}
    assert cli._setup_report_failure_id(report, {}) == "adb.server.version_mismatch"
    report["checks"][0]["failure_id"] = None
    assert cli._setup_report_failure_id(report, {}) == "adb.server.unverified"
    assert fr.server_check_failure_id({"failure_id": "adb.server.probe_failed", "summary": "", "details": {}}) == "adb.server.probe_failed"


# ---------------------------------------------------------------------------
# Interactive console
# ---------------------------------------------------------------------------

class Terminal(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def screen(monkeypatch):
    monkeypatch.setenv("ADAPT_NO_BANNER", "1")
    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.setattr(ui.shutil, "get_terminal_size", lambda **kwargs: os.terminal_size((120, 200)))
    monkeypatch.setattr(ui, "_enable_vt", lambda: (True, lambda: None))
    monkeypatch.setattr(ui.subprocess, "run", lambda *a, **k: pytest.fail("Opening a menu must not run any command"))


def _keys_to(path: str) -> str:
    records = ui.command_inventory(get_command(app))
    group, leaf = path.split(" ")[1], path
    area = next(i for i, (_, names) in enumerate(ui.AREAS) if group in names)
    top = [r for r in records if r["command"].count(" ") == 1 and r["command"] != "adapt console"
           and r["command"].split(" ")[1] in ui.AREAS[area][1]]
    keys = [str(area + 1), str(next(i for i, r in enumerate(top, 1) if r["command"] == f"adapt {group}"))]
    if leaf != f"adapt {group}":
        children = [r for r in records if r["command"].rsplit(" ", 1)[0] == f"adapt {group}"]
        keys.append(str(next(i for i, r in enumerate(children, 1) if r["command"] == leaf)))
    return "\n".join(keys) + "\n"


def test_command_detail_screen_lists_common_failures(screen):
    out = Terminal()
    ui.run_console(get_command(app), stdin=Terminal(_keys_to("adapt dev-options apply") + "q\n"), stdout=out, stderr=Terminal())
    text = out.getvalue()
    assert "COMMON FAILURES" in text and "devopts.apply_rolled_back  /  Applying the profile failed partway." in text
    assert "Causes and steps for one: adapt explain ID" in text


def test_common_failures_are_capped_and_hidden_without_tips(screen):
    lines = ui.common_failures("adapt dex receiver-status")
    assert sum(1 for line in lines if "  /  " in line) == ui.MAX_COMMON_FAILURES
    assert any(line.startswith("...and ") and line.endswith("adapt explain --list") for line in lines)
    assert ui.common_failures("adapt console") == []
    out = Terminal()
    ui.run_console(get_command(app), stdin=Terminal(_keys_to("adapt dev-options apply") + "q\n"), stdout=out,
                   stderr=Terminal(), no_tips=True)
    assert "COMMON FAILURES" not in out.getvalue()


def test_explain_is_in_the_help_area():
    assert "explain" in dict(ui.AREAS)["Logs, help & preferences"]


@pytest.mark.parametrize("code,expected", [(0, "Command finished with exit code 0. Enter"),
                                           (1, "Command finished with exit code 1. Its Why/Try notes are above; "
                                               "adapt explain lists all failures. Enter")])
def test_post_run_line_points_at_guidance_only_after_a_failure(screen, monkeypatch, code, expected):
    monkeypatch.setattr(ui.subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, code))
    out = Terminal()
    ui.run_console(get_command(app), stdin=Terminal(_keys_to("adapt doctor") + "r\n\n\nq\n"), stdout=out,
                   stderr=Terminal(), no_tips=True)
    assert expected in out.getvalue()


# ---------------------------------------------------------------------------
# Phase 1 leftovers: help-me, tips and everything error payloads
# ---------------------------------------------------------------------------

def test_help_me_unknown_topic_json_and_human():
    machine = runner.invoke(app, ["help-me", "not-a-tool", "--json"])
    assert machine.exit_code == 2 and machine.stderr == ""
    payload = json.loads(machine.stdout)
    assert payload["error"] == "unknown_topic" and payload["failure"]["id"] == "input.help_topic_unknown"
    human = runner.invoke(app, ["help-me", "not-a-tool"])
    assert human.exit_code == 2 and _more_lines(human.stderr) == ["More: adapt explain input.help_topic_unknown"]


def test_tips_errors_are_split_by_cause(tmp_path):
    bad = runner.invoke(app, ["tips", "maybe", "--json"])
    assert bad.exit_code == 2 and json.loads(bad.stdout)["failure"]["id"] == "input.tips_mode_invalid"
    config = tmp_path / "config"
    config.mkdir()
    (config / "preferences.json").write_text("not json", encoding="utf-8")
    broken = runner.invoke(app, ["tips", "status", "--json"])
    assert broken.exit_code == 2 and broken.stderr == ""
    payload = json.loads(broken.stdout)
    assert payload["error"] == "tips_preference" and payload["failure"]["id"] in {"prefs.unreadable", "input.tips_mode_invalid"}


def test_everything_refusals_carry_failure(monkeypatch):
    from adapt_adb import everything as batch
    monkeypatch.setattr(batch, "run_doctor_checks", lambda *a, **k: pytest.fail("no doctor run"))
    machine = runner.invoke(app, ["everything", "--json"])
    assert machine.exit_code == 2 and machine.stderr == ""
    payload = json.loads(machine.stdout)
    assert payload["status"] == "refused" and payload["failure"]["id"] == "everything.needs_confirmation"
    human = runner.invoke(app, ["everything", "--device", " bad"])
    assert human.exit_code == 2
    assert human.stderr.startswith("Use complete nonblank device targets; no implicit target is selected.\n")
    # Phase 4 tags the invalid --device refusal with its own guide (was the generic everything.refused).
    assert _more_lines(human.stderr) == ["More: adapt explain everything.target_invalid"]


# ---------------------------------------------------------------------------
# Receiver guidance registered for `adapt explain`
# ---------------------------------------------------------------------------

def test_receiver_guides_live_in_the_registry():
    assert dex_receiver.RECEIVER_GUIDES is fg.RECEIVER_GUIDES
    assert len(fg.RECEIVER_GUIDES) == 33
    for code, guide in fg.RECEIVER_GUIDES.items():
        assert fg.GUIDES[f"dexpc.receiver.{code}"] is guide


def test_explain_shows_and_finds_a_receiver_code():
    result = runner.invoke(app, ["explain", "dexpc.receiver.port_in_use"])
    assert result.exit_code == 0 and result.stderr == ""
    assert result.stdout.startswith("ADAPT / EXPLAIN / dexpc.receiver.port_in_use\n")
    assert "could not bind TCP port 7236" in result.stdout
    assert 'Help: adapt help-me "dex receiver-status"' in result.stdout
    assert fr.search("port_in_use") == ["dexpc.receiver.port_in_use"]
    listed = json.loads(runner.invoke(app, ["explain", "dexpc.receiver", "--json"]).stdout)["failures"]
    assert len(listed) == 33


def test_dex_receiver_imports_its_guides_without_defining_them():
    tree = ast.parse((ROOT / "src" / "adapt_adb" / "dex_receiver.py").read_text(encoding="utf-8"))
    assigned = {t.id for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AnnAssign))
                for t in (node.targets if isinstance(node, ast.Assign) else [node.target]) if isinstance(t, ast.Name)}
    assert "RECEIVER_GUIDES" not in assigned


# ---------------------------------------------------------------------------
# Coverage of the new guides by the shared registry checks
# ---------------------------------------------------------------------------

def test_new_guides_are_covered_by_the_privacy_and_forbidden_scans():
    # tests/test_failure_report.py::test_no_sentinel_reaches_any_surface and the forbidden scan in
    # tests/test_failure_guide.py iterate fg.GUIDES, so these IDs are part of them.
    for failure_id in ("devopts.apply_rolled_back", "device.unreachable", "scrcpy.too_old", "android.property_malformed",
                       "host.udev_missing", "adb.too_old", "dexpc.receiver.internal_panic", "everything.refused"):
        assert failure_id in fg.GUIDES


def test_doctor_pointer_lines_add_no_private_values(monkeypatch):
    monkeypatch.setattr("adapt_adb.doctor._is_stdout_tty", lambda: False)
    report = _report()
    before = render_doctor_human({**report, "checks": [{**c, "failure_id": None} for c in report["checks"]]})
    after = render_doctor_human(report)
    added = [line for line in after.splitlines() if line not in before.splitlines()]
    assert added == ["More: adapt explain adb.server.unverified  |  also: adb.origin_unverified"]


# ---------------------------------------------------------------------------
# Doctor Next menu: the udev action's own error paths
# ---------------------------------------------------------------------------

def _menu_report():
    return {"host": {"os": "debian", "arch": "x86_64", "supported": True}, "toolchain": {"adb_path": "/x/adb"},
            "checks": [], "devices": {"total": 0, "by_state": {}}, "exit_code": 0, "status": "ok"}


def _menu(monkeypatch):
    import sys
    monkeypatch.setattr(cli, "run_doctor_checks", lambda *a, **k: _menu_report())
    monkeypatch.setattr(cli, "render_doctor_human", lambda *a, **k: "ok")
    monkeypatch.setattr(sys, "stdin", io.StringIO("udev\nq\n"))


def test_menu_udev_missing_rules_file_adds_its_pointer(monkeypatch, capsys):
    _menu(monkeypatch)
    monkeypatch.setattr(cli, "find_udev_rules_file", lambda: None)
    cli._run_next_menu(_menu_report(), show_fix=False)
    err = capsys.readouterr().err
    assert "Error: 51-android.rules not found in packaging or package data.\n" in err
    assert "More: adapt explain host.udev_rules_file_missing" in err


def test_menu_udev_command_failure_adds_its_pointer(monkeypatch, capsys):
    _menu(monkeypatch)
    monkeypatch.setattr(cli, "_group_exists", lambda name: True)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(subprocess.CalledProcessError(1, "sudo")))
    cli._run_next_menu(_menu_report(), show_fix=False)
    err = capsys.readouterr().err
    assert "Warning: Failed to execute udev rules setup.\n" in err
    assert "More: adapt explain host.udev_setup_failed" in err
