"""Failure guidance Phase 4: modules whose errors are plain messages.

app/logs/debug, rapid, detect, desktop, DeX PC sessions, SSH workers, Tailscale, tools/scrcpy,
everything, `adapt devices` with a failing adb, and the MCP development tools. Every wired site
keeps its existing output (first line, JSON report keys, exit code); guidance follows on stderr
(human) or as an additive `failure` key (JSON). CliRunner is not a terminal, so the default is the
one-line short form; "tty" tests patch the stderr TTY check. Offline only: adb, ssh, tailscale,
scrcpy and downloads are faked.
"""

from __future__ import annotations

import ast
import json
import subprocess
import urllib.error
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from adapt_adb import desktop, development, device_state, dex_pc, everything, scrcpy_install, ssh_worker, tailscale_mode
from adapt_adb import failure_guide as fg
from adapt_adb import failure_report as fr
from adapt_adb import tool_catalog
from adapt_adb.cli import app
from adapt_adb.dex import DexError

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "adapt_adb"
PHASE4_MODULES = ("development", "rapid", "device_state", "desktop", "dex_pc", "ssh_worker", "tailscale_mode",
                  "diagnostics", "tool_catalog", "scrcpy_install", "everything")


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("ADAPT_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("NO_COLOR", "1")
    for name in ("ADAPT_TIPS", "ADAPT_FAILURE_GUIDANCE", "ADAPT_DEBUG", "ADAPT_ADB", "ADB_PATH", "ANDROID_SERIAL",
                 "ADB_SERVER_SOCKET", "ANDROID_ADB_SERVER_PORT", "ADB_SERVER_PORT", "ADAPT_SSH", "ADAPT_TAILSCALE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def tty(monkeypatch):
    monkeypatch.setattr(fr, "_stderr_isatty", lambda: True)


def _more(stderr: str) -> list[str]:
    return [line for line in stderr.splitlines() if line.startswith("More: ")]


def _dump(stdout: str, title: str) -> dict:
    assert stdout.startswith(title + "\n"), stdout[:80]
    return json.loads(stdout[len(title) + 1:])


# ---------------------------------------------------------------------------
# Registry and source cross-checks for the Phase 4 modules
# ---------------------------------------------------------------------------

def _phase4_trees():
    for name in PHASE4_MODULES:
        path = SRC / f"{name}.py"
        yield name, ast.parse(path.read_text(encoding="utf-8"))


def test_no_untagged_raise_sites_remain_in_phase4_modules():
    from tests.test_failure_guide import untagged_raise_sites
    left = [s for s in untagged_raise_sites() if s.split(":", 1)[0].removesuffix(".py") in PHASE4_MODULES]
    assert left == []


_ID_ARGUMENT = {"tagged": 1, "emit_failure": 0, "emit_pointer": 0, "query_failure_id": 1, "cause_failure_id": 1}


def test_every_id_passed_to_the_runtime_in_phase4_modules_exists():
    # Literal IDs, including both branches of `"a" if cond else "b"` and dict lookups, at every
    # tagged()/emit_*()/query_failure_id()/cause_failure_id() call in the Phase 4 modules.
    found = []
    for name, tree in _phase4_trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                callee = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
                index = _ID_ARGUMENT.get(callee)
                if index is not None and len(node.args) > index:
                    found += [(name, n.value) for n in ast.walk(node.args[index])
                              if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value and "." in n.value
                              and n.value not in {"device", "denied"}]
    assert len(found) > 150
    for name, value in found:
        assert value in fg.GUIDES, (name, value)


def test_computed_ids_and_reason_maps_name_existing_guides():
    statuses = ("failed", "timeout", "unavailable", "denied", "observed", "missing", "malformed")
    assert {fr.query_failure_id(s, "app.user_unobserved") for s in statuses} <= set(fg.GUIDES)
    assert set(ssh_worker._REASON_GUIDES) == set(ssh_worker._SSH_FAILURE_TEXT)
    assert set(ssh_worker._RESPONSE_GUIDES) == set(ssh_worker._RESPONSE_TEXT)
    for mapping in (ssh_worker._REASON_GUIDES, ssh_worker._RESPONSE_GUIDES, tailscale_mode._AUTH_GUIDES,
                    device_state._SCAN_STATUS_GUIDES, tool_catalog._STATUS_GUIDES):
        assert set(mapping.values()) <= set(fg.GUIDES), mapping
    for state, (auth_state, _hint) in tailscale_mode._AUTH_STATE_HINTS.items():
        guide = tailscale_mode.report_failure_id({"status": "not_running", "auth_state": auth_state})
        assert guide in fg.GUIDES, state
    for args in (["push", "a", "b"], ["shell", "CLASSPATH=x", "app_process", "/"], ["shell", "rm", "-f", "x"],
                 ["shell", "settings", "put", "global", "k", "v"], ["shell", "settings", "delete", "global", "k"],
                 ["shell", "getprop", "x"], ["shell", "settings", "get", "global", "k"]):
        for output in ("error: closed", "java.lang.SecurityException: denied"):
            assert dex_pc._operation_failure_id(args, output) in fg.GUIDES


def test_dex_pc_device_operations_get_distinct_guides():
    op = dex_pc._operation_failure_id
    assert op(["push", "jar", "/data/local/tmp/x.jar"], "") == "dexpc.helper_push_failed"
    assert op(["shell", "CLASSPATH=x", "app_process", "/", "E", "probe"], "") == "dexpc.helper_failed"
    assert op(["shell", "rm", "-f", "x"], "") == "dexpc.helper_cleanup_failed"
    assert op(["shell", "settings", "put", "global", "overlay_display_devices", "v"], "") == "dexpc.overlay_write_failed"
    assert op(["shell", "getprop", "ro.product.manufacturer"], "error: device offline") == "device.unanswered"
    assert op(["shell", "settings", "get", "global", "k"], "SecurityException") == "device.command_denied"


def test_loopback_denied_guide_points_at_the_receiver_routes_and_rules_out_rooting():
    guide = fg.GUIDES["dexpc.loopback_denied"]
    commands = [s.command for s in guide.steps]
    assert commands[:2] == ["adapt dex receiver-plan", "adapt dex pc-receiver-plan"]
    assert any("root" in a.lower() and "bootloader" in a.lower() for a in guide.avoid)
    assert guide.variability and "not been observed succeeding" in guide.variability


def test_ssh_guides_never_advise_disabling_host_key_checks_outside_avoid():
    for gid in ("ssh.host_key_unknown", "ssh.host_key_changed", "ssh.connection_failed"):
        guide = fg.GUIDES[gid]
        texts = [guide.summary, *(c.text for c in guide.causes), *(s.text for s in guide.steps), *(s.command or "" for s in guide.steps)]
        assert not any("stricthostkeychecking" in t.lower() or "ssh-keygen -r" in t.lower() for t in texts), gid
        assert any("StrictHostKeyChecking=no" in a for a in guide.avoid), gid


# ---------------------------------------------------------------------------
# app validate / logs capture / debug inspect (development._emit)
# ---------------------------------------------------------------------------

USER_LINE = "The current Android user could not be observed; no user-0 fallback was used."


@pytest.fixture
def unreachable_adb(monkeypatch):
    monkeypatch.setattr(development, "resolve_adb", lambda: Path("adb"))
    monkeypatch.setattr(development, "preflight_adb_server", lambda *a, **k: None)
    monkeypatch.setattr(development.subprocess, "run", lambda argv, **k: subprocess.CompletedProcess(
        argv, 1, "", "error: device 'phone' not found"))


def test_app_validate_on_an_unreachable_device_names_the_device_not_user_detection(unreachable_adb):
    result = runner.invoke(app, ["app", "validate", "-s", "phone", "--package", "com.example.app"])
    assert result.exit_code == 2
    assert _dump(result.stdout, "ADAPT / DEVELOPMENT") == {"status": "refused", "exit_code": 2, "error": USER_LINE}
    assert result.stderr == "More: adapt explain device.unanswered\n"
    machine = runner.invoke(app, ["app", "validate", "-s", "phone", "--package", "com.example.app", "--json"])
    assert machine.exit_code == 2 and machine.stderr == ""
    payload = json.loads(machine.stdout)
    assert {k: payload[k] for k in ("status", "exit_code", "error")} == {"status": "refused", "exit_code": 2, "error": USER_LINE}
    assert payload["failure"]["id"] == "device.unanswered"


def test_app_validate_full_block_on_a_terminal(unreachable_adb, tty):
    result = runner.invoke(app, ["app", "validate", "-s", "phone", "--package", "com.example.app"])
    assert result.stderr.startswith("\nWhy: The device did not answer ADAPT's read-only query")
    assert "  1. See the device's state:\n       adapt devices   [read-only]" in result.stderr
    assert result.stderr.rstrip().endswith("More: adapt explain device.unanswered   |   adapt help-me devices")


def test_user_detection_with_an_answer_keeps_its_own_guide(monkeypatch):
    session = SimpleNamespace(serial="phone", query=lambda args, **k: {"status": "observed", "text": "not-a-number"})
    with pytest.raises(development.DevelopmentError) as raised:
        development._inspect(session, "com.example.app")
    assert fr.classify(raised.value) == "app.user_unobserved"
    session.query = lambda args, **k: {"status": "denied", "text": None}
    with pytest.raises(development.DevelopmentError) as raised:
        development._inspect(session, "com.example.app")
    assert fr.classify(raised.value) == "device.command_denied"


@pytest.mark.parametrize("report,expected", [
    ({"status": "failed", "checks": []}, "app.validation_failed"),
    ({"status": "inconclusive", "listing_status": "timeout", "metadata": {"status": "not_queried"}}, "device.unanswered"),
    ({"status": "inconclusive", "listing_status": "observed", "metadata": {"status": "denied"}}, "device.command_denied"),
    ({"status": "inconclusive", "listing_status": "observed", "metadata": {"status": "partial"}}, "app.validation_inconclusive"),
    ({"status": "partial", "persisted_configuration": {"app_query_status": "failed", "wait_query_status": "failed"}}, "device.unanswered"),
    ({"status": "partial", "persisted_configuration": {"app_query_status": "observed", "wait_query_status": "observed"}}, "debug.inspect_partial"),
    ({"status": "passed", "checks": []}, None),
    ({"status": "captured"}, None),
])
def test_development_report_statuses_map_to_guides(report, expected):
    assert development.report_failure_id(report) == expected


def test_logs_capture_for_a_package_that_is_not_installed(monkeypatch):
    monkeypatch.setattr(development, "_inspect", lambda session, package: {"installation": "not_installed", "package_uid": None,
                                                                          "listing_status": "observed", "android_user": 0})
    monkeypatch.setattr(development, "Session", lambda serial: SimpleNamespace(serial=serial))
    result = runner.invoke(app, ["logs", "capture", "-s", "phone", "--package", "com.example.app"])
    assert result.exit_code == 2
    assert _dump(result.stdout, "ADAPT / DEVELOPMENT")["error"].startswith("Installed app UID could not be observed")
    assert _more(result.stderr) == ["More: adapt explain logs.app_not_installed"]


def test_debug_inspect_partial_keeps_exit_code_and_adds_guidance(unreachable_adb):
    result = runner.invoke(app, ["debug", "inspect", "-s", "phone", "--json"])
    assert result.exit_code == 2 and result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["status"] == "partial" and payload["failure"]["id"] == "device.unanswered"
    human = runner.invoke(app, ["debug", "inspect", "-s", "phone"])
    assert human.exit_code == 2 and _more(human.stderr) == ["More: adapt explain device.unanswered"]


def test_successful_development_reports_are_unchanged_and_silent():
    result = runner.invoke(app, ["app", "build-modes", "--json"])
    assert result.exit_code == 0 and result.stderr == "" and "failure" not in json.loads(result.stdout)


def test_rapid_plan_human_refusal_names_the_serial_guide():
    result = runner.invoke(app, ["rapid", "plan", "-s", " bad"])
    assert result.exit_code == 2
    assert result.stderr.startswith("Supply one exact nonblank serial without control characters.\n")
    assert _more(result.stderr) == ["More: adapt explain input.serial_invalid"]


def test_logs_diagnostic_keeps_the_dev_options_guide_through_its_wrapper(monkeypatch):
    from adapt_adb import dev_options, diagnostics
    monkeypatch.setattr(dev_options, "_resolve_adb_path", lambda *a: Path("adb"))
    def unreachable(*a, **k):
        raise dev_options.AdbCommandError("adb_error", "error: device offline")
    monkeypatch.setattr(dev_options, "_read_identity", unreachable)
    with pytest.raises(development.DevelopmentError) as raised:
        diagnostics.capture_diagnostics("phone", topic="window")
    assert fr.classify(raised.value) == "device.command_failed"


# ---------------------------------------------------------------------------
# detect (device_state._emit)
# ---------------------------------------------------------------------------

def test_detect_inspect_unreachable_device_is_named(monkeypatch):
    monkeypatch.setattr(device_state, "resolve_adb", lambda: Path("adb"))
    monkeypatch.setattr(device_state, "preflight_adb_server", lambda *a, **k: None)
    monkeypatch.setattr(device_state, "run_process", lambda args, timeout=5: subprocess.CompletedProcess(args, 1, "", "error: device offline"))
    result = runner.invoke(app, ["detect", "inspect", "-s", "phone"])
    assert result.exit_code == 2 and result.stdout.startswith("ADAPT / ADB DEVICE STATE\n")
    assert _more(result.stderr) == ["More: adapt explain device.unanswered"]
    machine = runner.invoke(app, ["detect", "inspect", "-s", "phone", "--json"])
    assert machine.exit_code == 2 and machine.stderr == ""
    assert json.loads(machine.stdout)["failure"]["id"] == "device.unanswered"


def test_detect_inspect_observed_with_unanswered_property_keeps_exit_zero(monkeypatch):
    monkeypatch.setattr(device_state, "resolve_adb", lambda: Path("adb"))
    monkeypatch.setattr(device_state, "preflight_adb_server", lambda *a, **k: None)
    def run(args, timeout=5):
        if args[-1] == "ro.boot.flash.locked":
            raise subprocess.TimeoutExpired(args, timeout)
        return subprocess.CompletedProcess(args, 0, "2000" if args[-2:] == ["id", "-u"] else "x", "")
    monkeypatch.setattr(device_state, "run_process", run)
    result = runner.invoke(app, ["detect", "inspect", "-s", "phone"])
    assert result.exit_code == 0
    assert result.stderr == "Some values were not observed: adapt explain device.unanswered\n"
    machine = runner.invoke(app, ["detect", "inspect", "-s", "phone", "--json"])
    assert machine.exit_code == 0 and machine.stderr == ""
    payload = json.loads(machine.stdout)
    assert payload["status"] == "observed" and payload["failure"]["id"] == "device.unanswered"


def test_detect_scan_missing_fastboot_and_refusal(monkeypatch):
    monkeypatch.setattr(device_state, "resolve_adb", lambda: None)
    monkeypatch.setattr(device_state, "resolve_fastboot", lambda: None)
    result = runner.invoke(app, ["detect", "scan"])
    assert result.exit_code == 2 and result.stdout.startswith("ADAPT / TRANSPORT DETECTION\nadb: missing_tool")
    assert _more(result.stderr) == ["More: adapt explain adb.not_found"]
    refused = runner.invoke(app, ["detect", "scan", "--transport", "usb", "--json"])
    assert refused.exit_code == 2 and refused.stderr == ""
    payload = json.loads(refused.stdout)
    assert payload["error"] == "Transport must be all, adb or fastboot." and payload["failure"]["id"] == "detect.transport_invalid"


# ---------------------------------------------------------------------------
# desktop
# ---------------------------------------------------------------------------

def test_desktop_inspect_unreachable_and_start_pointer(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop, "_adb", lambda serial: Path("adb"))
    monkeypatch.setattr(desktop, "probe", lambda args, **k: {"status": "failed", "value": None})
    result = runner.invoke(app, ["desktop", "inspect", "-s", "phone"])
    assert result.exit_code == 2 and result.stdout.startswith("ADAPT / DESKTOP TEST\nStatus: partial")
    assert _more(result.stderr) == ["More: adapt explain device.unanswered"]
    monkeypatch.setattr(desktop, "probe", lambda args, **k: {"status": "observed", "value": "33" if "getprop" in args else "scrcpy 3.1.2"})
    monkeypatch.setattr(desktop, "resolve_scrcpy", lambda: Path("scrcpy"))
    monkeypatch.setattr(desktop.subprocess, "Popen", lambda *a, **k: SimpleNamespace(pid=4321))
    started = runner.invoke(app, ["desktop", "start", "-s", "phone", "--app", "com.example.app", "--yes"])
    assert started.exit_code == 0 and "Status: process_started" in started.stdout
    assert started.stderr == "If no window appears: adapt explain desktop.no_window\n"
    machine = runner.invoke(app, ["desktop", "start", "-s", "phone", "--app", "com.example.app", "--yes", "--json"])
    assert machine.exit_code == 0 and machine.stderr == "" and "failure" not in json.loads(machine.stdout)


def test_desktop_refusals_keep_their_line(monkeypatch):
    result = runner.invoke(app, ["desktop", "start", "-s", "phone", "--app", "com.example.app"])
    assert result.exit_code == 2
    assert result.stderr.startswith("Starting the app/display requires explicit --yes. Review desktop plan first.\n")
    assert _more(result.stderr) == ["More: adapt explain desktop.confirmation_required"]
    monkeypatch.setattr(desktop, "_adb", lambda serial: Path("adb"))
    monkeypatch.setattr(desktop, "probe", lambda args, **k: {"status": "timeout", "value": None})
    with pytest.raises(device_state.DetectionError) as raised:
        desktop.start("phone", "com.example.app", yes=True)
    assert fr.classify(raised.value) == "device.unanswered"


# ---------------------------------------------------------------------------
# DeX PC sessions
# ---------------------------------------------------------------------------

@pytest.fixture
def pc_session(tmp_path, monkeypatch):
    identity = dex_pc.dev_options.DeviceIdentity("synthetic-physical", "synthetic-build", 10)
    state = {"schema_version": 1, "token": "a" * 32, "serial": "exact target", "identity": asdict(identity),
             "method": "loopback", "size": "1920x1080", "dpi": 160, "view_only": False,
             "overlay_value": "1920x1080/160,hdmi", "original_overlay": "null", "status": "prepared", "events": []}
    path = tmp_path / "session.json"
    dex_pc._save(path, state)
    monkeypatch.setattr(dex_pc.dev_options, "_read_identity", lambda *a: identity)
    monkeypatch.setattr(dex_pc.dev_options, "_resolve_adb_path", lambda *a: Path("chosen-adb"))
    monkeypatch.setattr(dex_pc.dev_options, "_LOCK_ROOT", tmp_path / "locks")
    return path


def _deny_loopback(monkeypatch, message="CONFIGURE_WIFI_DISPLAY permission denied"):
    import io
    monkeypatch.setattr(dex_pc, "_adb", lambda b, s, a: "")
    monkeypatch.setattr(dex_pc, "resolve_scrcpy", lambda: Path("scrcpy"))
    monkeypatch.setattr(dex_pc, "_check_scrcpy_version", lambda *a: "3.1.2")
    baseline = {"displays": [{"id": 0, "type": 1}], "wifi_state": 0, "dex_enabled": 2, "dex_display_type": 0}
    monkeypatch.setattr(dex_pc, "_probe", lambda *a: baseline)
    denied = json.dumps({"event": "error", "message": message}) + "\n"
    process = SimpleNamespace(stdout=io.StringIO(denied), stdin=io.StringIO(), poll=lambda: 1)
    monkeypatch.setattr(dex_pc.subprocess, "Popen", lambda argv, **kwargs: process)


def test_loopback_denial_records_its_guide_and_pc_start_names_it(pc_session, monkeypatch):
    _deny_loopback(monkeypatch)
    dex_pc.worker(pc_session)
    state = dex_pc._load(pc_session)
    assert state["status"] == "failed" and state["failure_id"] == "dexpc.loopback_denied"
    monkeypatch.setattr(dex_pc, "start", lambda **k: dex_pc.public(dex_pc._load(pc_session), pc_session))
    result = runner.invoke(app, ["dex", "pc-start", "-s", "exact target", "--yes"])
    assert result.exit_code == 1
    report = _dump(result.stdout, "ADAPT / EXPERIMENTAL DeX PC")
    assert report["status"] == "failed" and report["error"].startswith("Samsung loopback activation refused: ")
    assert "failure" not in report
    assert _more(result.stderr) == ["More: adapt explain dexpc.loopback_denied"]
    status = runner.invoke(app, ["dex", "pc-status", "--snapshot", str(pc_session), "--json"])
    assert status.exit_code == 1 and status.stderr == ""
    assert json.loads(status.stdout)["failure"]["id"] == "dexpc.loopback_denied"


def test_loopback_denial_full_block_never_echoes_phone_text(pc_session, monkeypatch, tty):
    _deny_loopback(monkeypatch, "SENTINELPHONETEXT")
    dex_pc.worker(pc_session)
    monkeypatch.setattr(dex_pc, "start", lambda **k: dex_pc.public(dex_pc._load(pc_session), pc_session))
    result = runner.invoke(app, ["dex", "pc-start", "-s", "exact target", "--yes"])
    assert "SENTINELPHONETEXT" in result.stdout and "SENTINELPHONETEXT" not in result.stderr
    assert "adapt dex receiver-plan   [read-only]" in result.stderr


def test_unknown_recorded_id_falls_back_to_the_generic_session_guide(pc_session):
    state = dex_pc._load(pc_session)
    state.update(status="failed", error="x", failure_id="no such guide; SENTINEL")
    dex_pc._save(pc_session, state)
    result = runner.invoke(app, ["dex", "pc-status", "--snapshot", str(pc_session)])
    assert result.exit_code == 1 and _more(result.stderr) == ["More: adapt explain dexpc.session_failed"]


def test_pc_refusals_and_pending_stop(pc_session, monkeypatch):
    result = runner.invoke(app, ["dex", "pc-plan", "-s", "exact target", "--method", "usb", "--json"])
    assert result.exit_code == 1 and result.stderr == ""
    assert json.loads(result.stdout)["failure"]["id"] == "dexpc.request_invalid"
    monkeypatch.setattr(dex_pc, "stop", lambda path: {"status": "stop_pending"})
    pending = runner.invoke(app, ["dex", "pc-stop", "--snapshot", str(pc_session)])
    assert pending.exit_code == 2 and _more(pending.stderr) == ["More: adapt explain dexpc.stop_pending"]


def test_pc_attach_scrcpy_exit_and_started_pointer(monkeypatch):
    assert dex_pc.report_failure_id({"status": "failed", "display_evidence": "x"}) == "dexpc.scrcpy_exited"
    monkeypatch.setattr(dex_pc, "attach", lambda **k: {"status": "process_started", "display_evidence": "x"})
    result = runner.invoke(app, ["dex", "pc-attach", "-s", "phone", "--display", "3", "--yes"])
    assert result.exit_code == 0 and result.stderr == "If no window appears: adapt explain dexpc.no_window\n"


def test_unexpected_dex_error_gets_its_specific_guide_with_the_generic_first_line(monkeypatch):
    def boom(**kwargs):
        raise DexError("missing_scrcpy", "scrcpy was not found")
    monkeypatch.setattr(dex_pc, "attach", boom)
    result = runner.invoke(app, ["dex", "pc-attach", "-s", "phone", "--display", "3", "--yes"])
    assert result.exit_code == 1
    assert result.stderr.startswith("Error: ADAPT stopped unexpectedly while running 'adapt dex pc-attach' (DexError).\n")
    assert "More: adapt explain scrcpy.not_found" in result.stderr
    assert fr.specific_id(KeyError("x")) is None and fr.specific_id(OSError("x")) is None


# ---------------------------------------------------------------------------
# SSH workers
# ---------------------------------------------------------------------------

@pytest.fixture
def ssh_fake(monkeypatch):
    monkeypatch.setattr(ssh_worker, "resolve_ssh", lambda: "chosen-ssh")
    def fake(code, stdout="", stderr=""):
        monkeypatch.setattr(ssh_worker, "_execute", lambda argv, timeout: subprocess.CompletedProcess(argv, code, stdout, stderr))
    return fake


def test_ssh_host_key_failure_names_its_guide(ssh_fake):
    ssh_fake(255, stderr="Host key verification failed.\r\n")
    result = runner.invoke(app, ["ssh", "run", "worker"])
    assert result.exit_code == 1
    report = _dump(result.stdout, "ADAPT / SSH WORKER")
    assert report["ssh_failure_reason"] == "host_key_unknown" and "failure" not in report
    assert _more(result.stderr) == ["More: adapt explain ssh.host_key_unknown"]
    machine = runner.invoke(app, ["ssh", "run", "worker", "--json"])
    assert machine.stderr == "" and json.loads(machine.stdout)["failure"]["id"] == "ssh.host_key_unknown"


@pytest.mark.parametrize("reason", sorted(ssh_worker._REASON_GUIDES))
def test_every_ssh_reason_code_maps_to_its_own_guide(reason):
    report = {"status": "ssh_failed", "ssh_failure_reason": reason}
    assert ssh_worker.report_failure_id(report) == ssh_worker._REASON_GUIDES[reason]
    assert len(set(ssh_worker._REASON_GUIDES.values())) == len(ssh_worker._REASON_GUIDES)


@pytest.mark.parametrize("report,expected", [
    ({"status": "invalid_response", "response_reason": "remote_adapt_not_found"}, "ssh.remote_adapt_not_found"),
    ({"status": "invalid_response"}, "ssh.response_too_large"),
    ({"status": "timeout"}, "ssh.timeout"),
    ({"status": "ssh_failed"}, "ssh.exec_failed"),
    ({"status": "remote_reported_failure"}, "ssh.remote_reported_failure"),
    ({"status": "missing_client"}, "ssh.client_not_found"),
    ({"status": "remote_completed"}, None),
])
def test_ssh_report_statuses(report, expected):
    assert ssh_worker.report_failure_id(report) == expected


def test_ssh_status_without_client_keeps_exit_zero_and_points(monkeypatch):
    monkeypatch.setattr(ssh_worker, "resolve_ssh", lambda: None)
    result = runner.invoke(app, ["ssh", "status"])
    assert result.exit_code == 0 and result.stderr == "More: adapt explain ssh.client_not_found\n"


def test_ssh_refusal_keeps_report_and_names_guide():
    result = runner.invoke(app, ["ssh", "plan", "ssh://worker"])
    assert result.exit_code == 2 and _dump(result.stdout, "ADAPT / SSH WORKER")["status"] == "refused"
    assert _more(result.stderr) == ["More: adapt explain ssh.host_invalid"]


# ---------------------------------------------------------------------------
# Tailscale
# ---------------------------------------------------------------------------

def _tailscale(monkeypatch, backend: str, code: int = 1, peers: dict | None = None, self_node: dict | None = None):
    monkeypatch.setattr(tailscale_mode, "resolve_tailscale", lambda: "tailscale")
    status = {"BackendState": backend, "Peer": peers or {}, **({"Self": self_node} if self_node else {})}
    monkeypatch.setattr(tailscale_mode, "_execute", lambda argv, **k: subprocess.CompletedProcess(argv, code, json.dumps(status), ""))


def test_tailscale_connect_when_sign_in_is_needed(monkeypatch):
    _tailscale(monkeypatch, "NeedsLogin")
    result = runner.invoke(app, ["tailscale", "connect", "--peer", "phone", "--port", "37123"])
    assert result.exit_code == 2
    report = _dump(result.stdout, "ADAPT / TAILSCALE MODE")
    assert report["error"].startswith("Tailscale must be running and signed in on this PC before selecting a peer.")
    assert _more(result.stderr) == ["More: adapt explain tailscale.needs_login"]
    status = runner.invoke(app, ["tailscale", "status", "--json"])
    assert status.exit_code == 1 and status.stderr == ""
    payload = json.loads(status.stdout)
    assert payload["auth_state"] == "needs_login" and payload["failure"]["id"] == "tailscale.needs_login"


def _peer(peer_id, name, address, online=True, expired=False):
    return {"ID": peer_id, "HostName": name, "DNSName": f"{name}.example.ts.net.", "Online": online,
            "TailscaleIPs": [address], "Expired": expired}


@pytest.mark.parametrize("peers,query,expected", [
    ({}, "phone", "tailscale.peer_not_found"),
    ({"a": _peer("n1", "phone", "100.64.0.2"), "b": _peer("n2", "phone", "100.64.0.3")}, "phone", "tailscale.peer_ambiguous"),
    ({"a": _peer("n1", "phone", "100.64.0.2", online=False)}, "phone", "tailscale.peer_offline"),
    ({"a": _peer("n1", "phone", "100.64.0.2", expired=True)}, "phone", "tailscale.peer_expired"),
    ({}, "this-pc", "tailscale.peer_is_self"),
])
def test_peer_selection_failures_are_distinguished(monkeypatch, peers, query, expected):
    _tailscale(monkeypatch, "Running", code=0, peers=peers, self_node={"HostName": "this-pc", "TailscaleIPs": ["100.64.0.9"]})
    with pytest.raises(tailscale_mode.TailscaleError) as raised:
        tailscale_mode._select(query)
    assert fr.classify(raised.value) == expected


def test_tailscale_tls_port_empty_and_missing_client(monkeypatch):
    monkeypatch.setattr(tailscale_mode, "resolve_tailscale", lambda: None)
    result = runner.invoke(app, ["tailscale", "status"])
    assert result.exit_code == 1 and _more(result.stderr) == ["More: adapt explain tailscale.not_found"]
    from adapt_adb.dev_options import DeviceIdentity
    monkeypatch.setattr(tailscale_mode, "_adb_query", lambda adb, endpoint, args: "device" if args == ["get-state"] else "33" if args[-1] == "ro.build.version.sdk" else "")
    monkeypatch.setattr(tailscale_mode.dev_options, "_read_identity", lambda *a: DeviceIdentity("p", "b", 0))
    with pytest.raises(tailscale_mode.TailscaleError) as raised:
        tailscale_mode._check({"address": "100.64.0.2"}, 37123, Path("adb"), "tls")
    assert fr.classify(raised.value) == "tailscale.tls_port_empty"


def test_tailscale_worker_run_reuses_ssh_guides(monkeypatch):
    monkeypatch.setattr(tailscale_mode, "worker", lambda **k: {"status": "ssh_failed", "exit_code": 1, "ssh_failure_reason": "authentication_failed"})
    result = runner.invoke(app, ["tailscale", "worker-run", "--peer", "phone"])
    assert result.exit_code == 1 and _more(result.stderr) == ["More: adapt explain ssh.auth_failed"]


# ---------------------------------------------------------------------------
# tools / scrcpy
# ---------------------------------------------------------------------------

def test_tools_install_scrcpy_download_failure(monkeypatch, tmp_path):
    real = scrcpy_install.install_scrcpy
    def offline(_artifact):
        raise urllib.error.URLError("offline")
    monkeypatch.setattr(scrcpy_install, "install_scrcpy", lambda **k: real(
        **k, platform_name="linux", machine="x86_64", install_root=tmp_path / "managed", which=lambda _n: None, open_asset=offline))
    result = runner.invoke(app, ["tools", "install", "scrcpy", "--yes"])
    assert result.exit_code == 1
    assert result.stderr.startswith("FAILED / scrcpy download failed without installing; signed redirect details were not retained.\n")
    assert _more(result.stderr) == ["More: adapt explain scrcpy.download_failed"]
    machine = runner.invoke(app, ["tools", "install", "scrcpy", "--yes", "--json"])
    assert machine.exit_code == 1 and machine.stderr == ""
    payload = json.loads(machine.stdout)
    assert payload["status"] == "failed" and payload["failure"]["id"] == "scrcpy.download_failed"


def test_scrcpy_class_defaults_and_conflict_tags():
    assert fr.classify(scrcpy_install.ScrcpyIntegrityError("x")) == "scrcpy.integrity_failed"
    assert fr.classify(scrcpy_install.ScrcpyArchiveSafetyError("x")) == "scrcpy.archive_unsafe"
    assert fr.classify(scrcpy_install.UnsupportedScrcpyHostError("x")) == "scrcpy.unsupported_host"
    assert fr.classify(scrcpy_install.ScrcpyConfirmationRequiredError("x")) == "scrcpy.needs_confirmation"
    assert fr.classify(scrcpy_install.ScrcpyInstallConflictError("x")) == "scrcpy.install_conflict"
    artifact = scrcpy_install.detect_scrcpy_artifact("linux", "x86_64")
    with pytest.raises(scrcpy_install.ScrcpyIntegrityError) as raised:
        scrcpy_install._validate_delivery_url("http://example.invalid/x", artifact, initial=True)
    assert fr.classify(raised.value) == "scrcpy.download_redirect_refused"


def test_tools_catalog_refusals(monkeypatch):
    result = runner.invoke(app, ["tools", "info", "no-such-tool"])
    assert result.exit_code == 2 and result.stderr.startswith("Error: Unknown tool 'no-such-tool'.")
    assert _more(result.stderr) == ["More: adapt explain tools.tool_unknown"]
    machine = runner.invoke(app, ["tools", "plan", "lineage-build", "--json"])
    assert machine.exit_code == 2 and machine.stderr == ""
    assert json.loads(machine.stdout)["failure"]["id"] == "tools.plan_incomplete"
    other = runner.invoke(app, ["tools", "install", "adb"])
    assert other.exit_code == 2 and _more(other.stderr) == ["More: adapt explain tools.install_unsupported"]


# ---------------------------------------------------------------------------
# everything batch results, adapt devices with a failing adb
# ---------------------------------------------------------------------------

def test_everything_batch_results_name_a_guide(monkeypatch):
    server = {"id": "adb_server_5037", "status": "fail", "failure_id": "adb.server.foreign_owner"}
    blocked = {"status": "blocked", "steps": [{"step": "host_check_before", "report": {"checks": [server]}}]}
    assert everything.report_failure_id(blocked) == "adb.server.foreign_owner"
    assert everything.report_failure_id({"status": "blocked", "steps": []}) == "everything.blocked"
    assert everything.report_failure_id({"status": "failed"}) == "everything.step_failed"
    assert everything.report_failure_id({"status": "completed"}) is None
    report = {"status": "completed_with_manual_steps", "exit_code": 2, "steps": [], "device_inspections": [],
              "transport_detection": {"transports": {"fastboot": {"status": "observed", "devices": []}}},
              "summary": {"manual_missing": []}, "claim_boundary": "c", "note": "n"}
    monkeypatch.setattr(everything, "run_everything", lambda **k: report)
    result = runner.invoke(app, ["everything", "--yes"])
    assert result.exit_code == 2 and result.stdout.startswith("ADAPT / GIVE ME EVERYTHING\nResult: completed_with_manual_steps (exit 2)")
    assert _more(result.stderr) == ["More: adapt explain everything.incomplete"]
    machine = runner.invoke(app, ["everything", "--yes", "--json"])
    assert machine.exit_code == 2 and machine.stderr == ""
    assert json.loads(machine.stdout)["failure"]["id"] == "everything.incomplete"


def test_devices_with_a_failing_adb_adds_guidance_after_adb_output(monkeypatch):
    import adapt_adb.cli as cli
    monkeypatch.setattr(cli, "resolve_adb", lambda: Path("adb"))
    monkeypatch.setattr(cli, "preflight_adb_server", lambda *a, **k: None)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=3))
    result = runner.invoke(app, ["devices"])
    assert result.exit_code == 3 and result.stderr == "More: adapt explain adb.devices_failed\n"


# ---------------------------------------------------------------------------
# MCP development tools
# ---------------------------------------------------------------------------

def test_mcp_development_tools_return_specific_failures(monkeypatch, unreachable_adb):
    pytest.importorskip("mcp")
    from adapt_adb.mcp.server import dispatch_tool_call
    def payload(name, args):
        return json.loads(dispatch_tool_call(name, args).content[-1].text)
    data = payload("app_validate", {"device": "phone", "package": "com.example.app"})
    assert data["error"] == "development_inspection_failed" and data["message"] == USER_LINE
    assert data["failure"]["id"] == "device.unanswered" and data["failure"]["next_tool"]["name"] == "list_devices"
    data = payload("debug_app_inspect", {"device": "phone"})
    assert data["error"] == "incomplete_evidence" and data["failure"]["id"] == "device.unanswered"
    monkeypatch.setattr(development, "_inspect", lambda session, package: {"installation": "not_installed", "metadata": {"version_code": None, "debuggable": None}})
    monkeypatch.setattr(development, "Session", lambda serial: None)
    data = payload("app_validate", {"device": "phone", "package": "com.example.app"})
    assert data["error"] == "checks_failed" and data["failure"]["id"] == "app.validation_failed"
    for code in ("development_inspection_failed", "checks_failed", "incomplete_evidence"):
        assert code in fr.MCP_ERROR_MAP


# ---------------------------------------------------------------------------
# Privacy: nothing from the device, worker or tailnet reaches the guidance
# ---------------------------------------------------------------------------

def test_no_sentinel_reaches_phase4_guidance(monkeypatch, tty):
    monkeypatch.setattr(ssh_worker, "resolve_ssh", lambda: "chosen-ssh")
    monkeypatch.setattr(ssh_worker, "_execute", lambda argv, timeout: subprocess.CompletedProcess(
        argv, 255, "", "ssh: Could not resolve hostname SENTINELHOST: Name or service not known"))
    ssh_result = runner.invoke(app, ["ssh", "run", "SENTINELHOST"])
    assert "SENTINELHOST" in ssh_result.stdout and "SENTINELHOST" not in ssh_result.stderr
    assert "ssh.name_resolution" in ssh_result.stderr
    _tailscale(monkeypatch, "Running", code=0, peers={"a": _peer("SENTINELID", "SENTINELPEER", "100.64.0.2", online=False)})
    ts_result = runner.invoke(app, ["tailscale", "probe", "--peer", "SENTINELPEER", "--port", "5555"])
    assert ts_result.exit_code == 2 and "SENTINEL" not in ts_result.stderr and "tailscale.peer_offline" in ts_result.stderr
    monkeypatch.setattr(development, "resolve_adb", lambda: Path("adb"))
    monkeypatch.setattr(development, "preflight_adb_server", lambda *a, **k: None)
    monkeypatch.setattr(development.subprocess, "run", lambda argv, **k: subprocess.CompletedProcess(argv, 0, "SENTINELUSER", ""))
    app_result = runner.invoke(app, ["app", "validate", "-s", "SENTINELSERIAL", "--package", "com.example.app"])
    assert "SENTINEL" not in app_result.stderr and "app.user_unobserved" in app_result.stderr
