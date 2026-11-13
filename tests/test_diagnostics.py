"""Focused system evidence is bounded, exact-targeted and never enables producers."""
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from adapt_adb import diagnostics as diag
from adapt_adb import rapid
from adapt_adb.cli import app
from adapt_adb.dev_options import DeviceIdentity
from adapt_adb.mcp.access import AccessPolicy
from adapt_adb.mcp.server import dispatch_tool_call


def forbid(*a, **k):
    pytest.fail("Unexpected device or process access")


@pytest.fixture
def target(monkeypatch):
    identity = DeviceIdentity("physical", "build", 10)
    monkeypatch.setattr(diag.dev_options, "_resolve_adb_path", lambda p: Path("chosen-adb"))
    monkeypatch.setattr(diag.dev_options, "_read_identity", lambda *a: identity)
    monkeypatch.setattr(diag.dev_options, "_run_adb", lambda *a: "1794543400")
    return identity


def test_time_window_and_fixed_tags_prevent_global_tail_from_hiding_subsystem_events(target, monkeypatch):
    calls = []
    def read(argv):
        calls.append(argv)
        return "--------- beginning of system\n11-12 22:18:58 1 1 E WifiP2pService: provision discovery failed status: 1\n"
    monkeypatch.setattr(diag, "_read_logs", read)
    report = diag.capture_diagnostics("exact target", topic="dex")
    argv = calls[0]
    assert argv[:4] == ["chosen-adb", "-s", "exact target", "logcat"]
    assert argv[argv.index("-T") + 1] == "1794543280.000" and "-t" not in argv
    assert argv[-1] == "*:S" and "WifiP2pService:V" in argv
    assert report["scope"] == "system_components_not_app_uid" and report["mutations"] == []
    assert len(report["lines"]) == 1 and "provision discovery" in report["lines"][0]
    assert "multiple apps/users" in report["privacy"]


@pytest.mark.parametrize("kwargs", [{"topic":"samsung"}, {"topic":"*"}, {"topic":"dex","minutes":0}, {"topic":"dex","lines":501}, {"topic":"window","level":"S"}])
def test_bad_capture_requests_stop_before_adb(monkeypatch, kwargs):
    monkeypatch.setattr(diag.dev_options, "_resolve_adb_path", forbid)
    with pytest.raises(ValueError):
        diag.capture_diagnostics("phone", **kwargs)


def test_clock_failure_and_identity_switch_discard_without_broad_fallback(target, monkeypatch):
    monkeypatch.setattr(diag.dev_options, "_run_adb", lambda *a:"unknown")
    monkeypatch.setattr(diag, "_read_logs", forbid)
    with pytest.raises(ValueError, match="clock"):
        diag.capture_diagnostics("phone", topic="window")
    monkeypatch.setattr(diag.dev_options, "_run_adb", lambda *a:"1794543400")
    monkeypatch.setattr(diag, "_read_logs", lambda *a:"private log")
    identities = iter([target, DeviceIdentity("other", "build", 10)])
    monkeypatch.setattr(diag.dev_options, "_read_identity", lambda *a:next(identities))
    with pytest.raises(ValueError, match="discarded"):
        diag.capture_diagnostics("phone", topic="window")


def test_record_and_byte_caps_and_empty_logs_remain_inconclusive(target, monkeypatch):
    monkeypatch.setattr(diag, "_read_logs", lambda *a:"x"*diag.LOG_MAX_BYTES + "\nlast\n")
    report = diag.capture_diagnostics("phone", topic="print", lines=1)
    assert report["truncated"] and report["lines"] == ["last"]
    monkeypatch.setattr(diag, "_read_logs", lambda *a:"")
    report = diag.capture_diagnostics("phone", topic="print")
    assert report["lines"] == [] and "Empty logs" in report["claim_boundary"]


def fake_process(monkeypatch, *, data=b"", errors=b"", code=0):
    class Process:
        returncode = code
        terminated = False
        killed = False
        def __init__(self, argv, **kwargs):
            kwargs["stdout"].write(data); kwargs["stdout"].flush()
            kwargs["stderr"].write(errors); kwargs["stderr"].flush()
        def poll(self): return self.returncode
        def terminate(self): self.terminated=True; self.returncode=-15
        def kill(self): self.killed=True; self.returncode=-9
        def wait(self, timeout): return self.returncode
    made = []
    def start(*a, **k):
        process=Process(*a, **k); made.append(process); return process
    monkeypatch.setattr(diag.subprocess, "Popen", start)
    return made


def test_capture_budget_terminates_only_its_owned_reader(monkeypatch):
    made = fake_process(monkeypatch, data=b"x"*(diag.READ_MAX_BYTES+1), code=None)
    with pytest.raises(ValueError, match="byte budget"):
        diag._read_logs(["adb", "-s", "phone", "logcat", "-d"])
    assert made[0].terminated and not made[0].killed


def test_capture_timeout_and_denial_do_not_return_private_contents(monkeypatch):
    made = fake_process(monkeypatch, code=None)
    times = iter([0,11]); monkeypatch.setattr(diag.time,"monotonic",lambda:next(times))
    with pytest.raises(ValueError, match="timed out"):
        diag._read_logs(["adb"])
    assert made[0].terminated
    monkeypatch.setattr(diag.time,"monotonic",lambda:0)
    fake_process(monkeypatch, data=b"private", errors=b"Permission denied", code=1)
    with pytest.raises(ValueError, match="no broader fallback"):
        diag._read_logs(["adb"])


def test_guidance_and_rapid_plan_are_offline_and_keep_heavy_modes_separate(monkeypatch, tmp_path):
    monkeypatch.setattr(diag.subprocess, "Popen", forbid)
    monkeypatch.setattr(diag.dev_options, "_resolve_adb_path", forbid)
    guide=diag.logging_guide("samsung")
    assert not guide["device_access"] and guide["topics"][0]["automation"] == "manual_unverified_mapping"
    report=rapid.plan("exact target", package="com.example.app", session_dir=tmp_path/"absent")
    assert report["label"] == "Rapid ADAPT" and not report["files_created"]
    assert not (tmp_path/"absent").exists()
    prepare=report["steps"][1]["commands"]
    assert len(prepare)==1 and "stay-awake" in prepare[0]
    assert "restore" in report["steps"][-1]["commands"][0]
    assert all("setprop" not in cmd for step in report["steps"] for cmd in step["commands"])


def test_cli_guide_and_rapid_json_are_silent_machine_output(monkeypatch):
    monkeypatch.setattr(diag.dev_options, "_resolve_adb_path", forbid)
    for argv in (["logs","guide","--topic","samsung","--json"], ["rapid","plan","-s","offline","--json"]):
        result=CliRunner().invoke(app,argv)
        assert result.exit_code==0, result.output
        report=json.loads(result.stdout)
        assert not report["device_access"] and "\x1b" not in result.stdout


def test_mcp_new_tools_are_read_only_but_system_capture_is_target_restricted(monkeypatch):
    policy=AccessPolicy(read_only=True,allowed_devices=("allowed",))
    assert policy.exposes("logging_guide") and policy.exposes("diagnostic_logs")
    assert not policy.permits("diagnostic_logs",{"device":"other"})
    monkeypatch.setattr(diag,"capture_diagnostics",forbid)
    result=dispatch_tool_call("diagnostic_logs",{"device":"other","topic":"dex"},policy=policy)
    assert result.is_error
    result=dispatch_tool_call("logging_guide",{"topic":"samsung"},policy=policy)
    assert not result.is_error and json.loads(result.content[0].text)["device_access"] is False
    result=dispatch_tool_call("diagnostic_logs",{"device":"allowed","topic":"dex","minutes":False},policy=policy)
    assert result.is_error
