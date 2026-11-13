"""Transport separation and conservative evidence without hardware access."""
import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from adapt_adb import device_state as state
from adapt_adb.cli import app


def observed(value):
    return {"status": "observed", "value": value}


def forbid(*args, **kwargs):
    pytest.fail("Unexpected process/device access")


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(state, "resolve_adb", lambda: Path("chosen-adb"))
    monkeypatch.setattr(state, "resolve_fastboot", lambda: Path("chosen-fastboot"))
    monkeypatch.setattr(state, "preflight_adb_server", lambda *a: None)
    monkeypatch.setattr(state, "run_process", forbid)


def test_mixed_transport_scan_keeps_exact_identities_and_does_not_query_devices(monkeypatch):
    calls = []
    def run(args, timeout=5):
        calls.append(args)
        output = "List of devices attached\nadb-Phone (2)._adb-tls-connect._tcp\tdevice\n" if args[0] == "chosen-adb" else "boot-phone\tfastboot\n"
        return subprocess.CompletedProcess(args, 0, output, "")
    monkeypatch.setattr(state, "run_process", run)
    report = state.scan()
    assert report["transports"]["adb"]["devices"][0]["serial"] == "adb-Phone (2)._adb-tls-connect._tcp"
    assert report["transports"]["fastboot"]["devices"][0]["serial"] == "boot-phone"
    assert calls == [["chosen-adb", "devices"], ["chosen-fastboot", "devices"]]
    assert report["identity_correlation"] == "not_attempted"


def test_fastboot_detection_continues_when_adb_gate_is_blocked(monkeypatch):
    monkeypatch.setattr(state, "preflight_adb_server", lambda *a: (_ for _ in ()).throw(RuntimeError()))
    calls = []
    monkeypatch.setattr(state, "run_process", lambda args: calls.append(args) or subprocess.CompletedProcess(args, 0, "phone\tfastboot\n", ""))
    report = state.scan()
    assert report["transports"]["adb"]["status"] == "blocked"
    assert report["transports"]["fastboot"]["status"] == "observed"
    assert calls == [["chosen-fastboot", "devices"]]


def test_fastboot_only_never_resolves_or_preflights_adb(monkeypatch):
    monkeypatch.setattr(state, "resolve_adb", forbid)
    monkeypatch.setattr(state, "preflight_adb_server", forbid)
    monkeypatch.setattr(state, "run_process", lambda args: subprocess.CompletedProcess(args, 0, "", ""))
    assert state.scan(transport="fastboot")["transports"]["fastboot"] == {"status": "observed", "devices": []}


def test_empty_adb_response_is_not_a_valid_empty_listing(monkeypatch):
    monkeypatch.setattr(state, "run_process", lambda args: subprocess.CompletedProcess(args, 0, "", ""))
    report = state.scan(transport="adb")
    assert report["status"] == "partial" and report["transports"]["adb"]["status"] == "malformed"


@pytest.mark.parametrize("text", ["unrecognized", "phone\tfastboot\nphone\tfastboot\n", "bad\x1bserial\tfastboot"])
def test_invalid_listing_is_not_reported_as_no_devices(text):
    with pytest.raises(state.DetectionError):
        state.parse_fastboot_devices(text)


@pytest.mark.parametrize("values,expected", [
    ({}, "unknown"),
    ({"ro.boot.flash.locked": "0"}, "unlocked"),
    ({"ro.boot.verifiedbootstate": "yellow"}, "locked"),
    ({"ro.boot.flash.locked": "1", "ro.boot.vbmeta.device_state": "unlocked"}, "conflicting"),
    ({"sys.oem_unlock_allowed": "1", "ro.oem_unlock_supported": "1"}, "unknown"),
])
def test_lock_state_uses_matching_evidence_not_unlock_permission(values, expected):
    report = state.evaluate({k: observed(v) for k, v in values.items()}, observed("2000"), {"status": "missing", "value": None})
    assert report["bootloader"]["status"] == expected
    assert report["root"]["status"] == "unknown"


def test_custom_rom_and_root_indicators_are_separate():
    props = {"ro.lineage.version": observed("23-test"), "ro.build.tags": observed("test-keys")}
    report = state.evaluate(props, observed("2000"), observed("/system/bin/su"))
    assert report["rom"]["status"] == "custom_rom_indicators"
    assert report["root"]["status"] == "su_candidate_observed"
    assert state.evaluate({}, observed("0"), {"status": "missing"})["root"]["status"] == "root_shell_observed"
    assert state.evaluate({"ro.build.tags": observed("test-keys")}, observed("2000"), {"status": "denied"})["rom"]["status"] == "unknown"


def test_adb_inspection_does_not_request_root_or_execute_su(monkeypatch):
    calls = []
    def run(args):
        calls.append(args)
        text = "2000" if args[-2:] == ["id", "-u"] else "" if "getprop" in args else "/system/bin/su"
        return subprocess.CompletedProcess(args, 0, text, "")
    monkeypatch.setattr(state, "run_process", run)
    target = "adb-Phone (2)._adb-tls-connect._tcp"
    report = state.inspect_device(target)
    assert all(a[1:4] == ["-s", target, "shell"] for a in calls)
    assert calls[-1][4:] == ["command", "-v", "su"]
    assert report["root"]["status"] == "su_candidate_observed"
    assert report["bootloader"]["status"] == "unknown"


@pytest.mark.parametrize("userspace,mode", [("yes", "fastbootd"), ("no", "bootloader_fastboot")])
def test_fastboot_inspection_reads_stderr_and_never_touches_adb(userspace, mode, monkeypatch):
    monkeypatch.setattr(state, "resolve_adb", forbid)
    monkeypatch.setattr(state, "preflight_adb_server", forbid)
    calls = []
    def run(args):
        calls.append(args)
        key = args[-1]
        value = {"unlocked": "yes", "is-userspace": userspace, "product": "test"}[key]
        return subprocess.CompletedProcess(args, 0, "", f"(bootloader) {key}: {value}\nFinished. Total time: 0.001s\n")
    monkeypatch.setattr(state, "run_process", run)
    report = state.inspect_device("phone", transport="fastboot")
    assert report["mode"] == mode and report["bootloader"]["status"] == "unlocked"
    assert all(a[:4] == ["chosen-fastboot", "-s", "phone", "getvar"] for a in calls)
    assert report["root"]["status"] == "not_available_in_this_transport"


@pytest.mark.parametrize("response", ["", "unlocked: no\nunlocked: yes", "FAILED (remote: unknown variable)"])
def test_unsupported_and_conflicting_fastboot_responses_stay_unknown(response, monkeypatch):
    monkeypatch.setattr(state, "run_process", lambda args: subprocess.CompletedProcess(args, 0, "", response))
    assert state.inspect_device("phone", transport="fastboot")["bootloader"]["status"] == "unknown"


def test_timeout_is_unknown_not_locked(monkeypatch):
    monkeypatch.setattr(state, "run_process", lambda args: (_ for _ in ()).throw(subprocess.TimeoutExpired(args, 5)))
    report = state.inspect_device("phone", transport="fastboot")
    assert report["status"] == "partial" and report["bootloader"]["status"] == "unknown"


def test_invalid_target_and_cli_error_are_clean(monkeypatch):
    monkeypatch.setattr(state, "resolve_adb", forbid)
    result = CliRunner().invoke(app, ["detect", "inspect", "-s", "bad\ntarget", "--json"])
    assert result.exit_code == 2 and result.stderr == ""
    assert json.loads(result.stdout)["status"] == "refused"
