"""Runtime evidence and app logs cannot silently imply build provenance."""
import hashlib
import json
import subprocess
from pathlib import Path

import pytest
import anyio
from typer.testing import CliRunner

from adapt_adb import development as dev
from adapt_adb.cli import app
from adapt_adb.mcp.access import AccessPolicy
from adapt_adb.mcp.server import create_server, dispatch_tool_call, get_mcp_tools

PACKAGE = "com.example.app"
DUMP = """Packages:
  Package [com.example.app] (abc):
    userId=10123
    versionCode=42 minSdk=29 targetSdk=37
    versionName=1.2.3
    flags=[ HAS_CODE DEBUGGABLE ]
    privateFlags=[ PROFILEABLE_BY_SHELL ]
    User 10: installed=true
Shared users:
  other flags=[ SYSTEM ]
"""


def forbid(*a, **k):
    pytest.fail("Unexpected device access or mutation")


class FakeSession:
    def __init__(self, serial):
        self.serial = serial
        self.calls = []
        self.listing = "package:com.example.app uid:1010123"
        self.dump = DUMP
        self.logs = "--------- beginning of main\n11-13 03:00:00 123 123 E AndroidRuntime: java.lang.SecurityException: test\n"
        self.user = "10"
        self.statuses = {}
        self.after_logs = None

    def query(self, args, **kwargs):
        self.calls.append(args)
        key = "logs" if args[0] == "logcat" else "listing" if "packages" in args else "dump" if "dumpsys" in args else "user" if "get-current-user" in args else args[-1] if "settings" in args else "paths"
        value = {"logs":self.logs,"listing":self.listing,"dump":self.dump,"user":self.user,
                 "paths":"package:/data/app/example/base.apk\npackage:/data/app/example/split_config.apk",
                 "debug_app":PACKAGE,"wait_for_debugger":"1"}[key]
        if key == "logs" and self.after_logs:
            self.after_logs()
        return {"status":self.statuses.get(key,"observed"),"text":value if self.statuses.get(key,"observed")=="observed" else None}


@pytest.fixture
def session(monkeypatch):
    fake = FakeSession("exact target")
    monkeypatch.setattr(dev, "Session", lambda serial: fake)
    return fake


def test_exact_current_user_install_version_debug_flags_and_unknown_r8(session):
    report = dev.validate_app("exact target", PACKAGE, expected_version_code=42, expected_debuggable=True)
    assert report["status"] == "passed" and report["android_user"] == 10
    assert report["metadata"]["profileable_by_shell"] is True
    assert report["apk_paths"]["items"][1].endswith("split_config.apk")
    assert report["build_evidence"]["r8_optimization"] == "not_verified"
    assert report["build_evidence"]["exact_artifact_match"] == "not_verified"
    assert session.calls[1] == ["shell","pm","list","packages","-U","--user","10",PACKAGE]


def test_non_debuggable_cannot_prove_r8_or_signing(session):
    session.dump = DUMP.replace(" DEBUGGABLE", "")
    report = dev.validate_app("exact target", PACKAGE, expected_debuggable=False)
    assert report["status"] == "passed"
    assert report["build_evidence"]["signing_certificate"] == "not_verified"
    assert report["build_evidence"]["production_readiness"] == "not_established"


@pytest.mark.parametrize("dump", [DUMP.replace(PACKAGE,"com.other.app"), DUMP+DUMP, "unknown OEM format", DUMP.replace("    flags=", "      flags=")])
def test_missing_ambiguous_or_nested_package_flags_are_not_guessed(dump):
    assert dev.parse_package_dump(dump, PACKAGE)["debuggable"] is None


def test_exact_package_is_not_confused_with_filter_substring(session):
    session.listing = "package:com.example.app.premium uid:1010124"
    report = dev.validate_app("exact target", PACKAGE)
    assert report["installation"] == "not_installed" and report["exit_code"] == 1
    assert len(session.calls) == 2


@pytest.mark.parametrize("listing", ["nonsense", "package:com.example.app uid:1010123\npackage:com.example.app uid:1010124"])
def test_invalid_or_duplicate_listing_stays_unknown(listing, session):
    session.listing = listing
    assert dev.validate_app("exact target", PACKAGE)["status"] == "inconclusive"


def test_denied_listing_is_not_a_missing_install(session):
    session.statuses["listing"] = "denied"
    report = dev.validate_app("exact target", PACKAGE)
    assert report["installation"] == "unknown" and report["exit_code"] == 2


def test_expected_version_mismatch_and_unreadable_flags_remain_distinct(session):
    assert dev.validate_app("exact target", PACKAGE, expected_version_code=43)["status"] == "failed"
    session.dump = "unknown"
    report = dev.validate_app("exact target", PACKAGE, expected_debuggable=False)
    assert report["status"] == "inconclusive"
    assert report["checks"][0]["status"] == "passed"


def test_unknown_current_user_never_falls_back_to_user_zero(session):
    session.user = "unknown"
    with pytest.raises(dev.DevelopmentError, match="user-0"):
        dev.validate_app("exact target", PACKAGE)
    assert len(session.calls) == 1


@pytest.mark.parametrize("kwargs", [{"package":"bad;reboot"},{"serial":"bad\nserial"},{"expected_version_code":True},{"expected_debuggable":"false"}])
def test_invalid_inputs_fail_before_session(monkeypatch, kwargs):
    monkeypatch.setattr(dev,"Session",forbid)
    with pytest.raises(ValueError):
        dev.validate_app(**{"serial":"phone","package":PACKAGE,**kwargs})


def test_logs_use_finite_uid_filter_preserve_app_exceptions_and_do_not_clear(session):
    report = dev.capture_logs("exact target", PACKAGE, lines=20)
    log_commands = [a for a in session.calls if a[0] == "logcat"]
    assert len(log_commands) == 1 and "--uid=1010123" in log_commands[0]
    assert "-d" in log_commands[0] and "-c" not in log_commands[0]
    assert report["lines"][0].endswith("SecurityException: test")
    assert not any("beginning of" in line for line in report["lines"])
    assert report["mutations"] == [] and "shared-UID" in report["privacy"]


def test_empty_logs_do_not_become_no_crashes_or_test_pass(session):
    session.logs = ""
    report = dev.capture_logs("exact target", PACKAGE)
    assert report["lines"] == [] and report["status"] == "captured"
    assert "does not prove no crashes" in report["claim_boundary"]


def test_uid_unavailable_or_filter_denied_never_runs_a_wider_capture(session):
    session.listing = "package:com.example.app"
    with pytest.raises(dev.DevelopmentError, match="fallback"):
        dev.capture_logs("exact target", PACKAGE)
    assert not any(a[0]=="logcat" for a in session.calls)
    session.listing = "package:com.example.app uid:1010123"
    session.statuses["logs"]="denied"
    with pytest.raises(dev.DevelopmentError, match="broader"):
        dev.capture_logs("exact target", PACKAGE)
    assert sum(a[0]=="logcat" for a in session.calls) == 1


def test_uid_reassignment_during_capture_discards_contents(session):
    session.after_logs=lambda:setattr(session,"listing","package:com.example.app uid:1010999")
    with pytest.raises(dev.DevelopmentError, match="discarded"):
        dev.capture_logs("exact target", PACKAGE)


def test_logs_keep_recent_tail_and_enforce_byte_and_line_caps(session,monkeypatch):
    session.logs="old entry\n"+"x"*200+"\nnewest entry\n"
    monkeypatch.setattr(dev,"LOG_MAX_BYTES",32)
    report=dev.capture_logs("exact target",PACKAGE,lines=1)
    assert report["truncated"] and report["lines"] == ["newest entry"]


def test_export_is_no_clobber_and_hash_matches_exact_bytes(session,tmp_path):
    target=tmp_path/"logs.json"
    report=dev.export_logs(dev.capture_logs("exact target",PACKAGE),target)
    assert hashlib.sha256(target.read_bytes()).hexdigest() == report["export"]["sha256"]
    before=target.read_bytes()
    with pytest.raises(dev.DevelopmentError,match="overwritten"):
        dev.export_logs({"changed":True},target)
    assert target.read_bytes()==before


def test_debug_inspection_does_not_conflate_saved_selection_with_runtime(session):
    report=dev.inspect_debug("exact target")
    assert report["persisted_configuration"]["debug_app"]==PACKAGE
    assert report["persisted_configuration"]["wait_for_debugger"] is True
    assert report["runtime_effective_selection"] == "not_verified"
    assert len(session.calls)==2 and all("settings" in a and "get" in a for a in session.calls)


def test_debug_plan_is_offline_and_discloses_force_stop_wait_persistence(monkeypatch):
    monkeypatch.setattr(dev,"Session",forbid)
    report=dev.plan_debug("phone",PACKAGE,wait=True)
    assert report["would_force_stop_selected_app"] and report["would_wait_at_startup"]
    assert "-w" in report["argv"] and "--persistent" not in report["argv"]
    assert report["device_access"] is False


def test_real_query_adapter_targets_exact_serial_and_keeps_log_data(monkeypatch):
    calls=[]
    monkeypatch.setattr(dev,"resolve_adb",lambda:Path("chosen-adb"))
    monkeypatch.setattr(dev,"preflight_adb_server",lambda *a:None)
    def run(args,**kwargs):
        calls.append((args,kwargs))
        return subprocess.CompletedProcess(args,0,"java.lang.SecurityException: app crash\n","")
    monkeypatch.setattr(dev.subprocess,"run",run)
    session=dev.Session("exact target")
    assert session.query(["logcat","-d"],log_data=True)["status"]=="observed"
    assert calls[0][0][:3]==["chosen-adb","-s","exact target"]
    assert calls[0][1]["timeout"]==10


def test_mcp_requires_exact_targets_and_read_only_policy_allows_observation(session):
    policy=AccessPolicy(read_only=True,allowed_devices=("exact target",))
    exposed={tool.name:tool for tool in get_mcp_tools(policy)}
    assert {"app_validate","app_logs","debug_app_inspect","build_modes"} <= set(exposed)
    for name in ("app_validate","app_logs","debug_app_inspect"):
        schema = exposed[name].model_dump(by_alias=True)["inputSchema"]
        assert schema["properties"]["device"]["enum"] == ["exact target"]
    denied=dispatch_tool_call("app_validate",{"device":"other","package":PACKAGE},policy=policy)
    assert denied.is_error and session.calls==[]
    result=dispatch_tool_call("app_validate",{"device":"exact target","package":PACKAGE,"expected_version_code":42},policy=policy)
    assert not result.is_error and json.loads(result.content[0].text)["status"] == "passed"
    invalid=dispatch_tool_call("app_validate",{"device":"exact target","package":PACKAGE,"r8":True},policy=policy)
    assert invalid.is_error


def test_cli_machine_guidance_previews_and_invalid_debug_option_stay_clean(monkeypatch):
    monkeypatch.setattr(dev,"Session",forbid)
    runner=CliRunner()
    for args in (["app","build-modes","--json"],["debug","plan","-s","phone","--package",PACKAGE,"--json"]):
        result=runner.invoke(app,args)
        assert result.exit_code==0 and result.stderr==""
        assert json.loads(result.stdout)["status"] in {"offline_reference","plan_only"}
    result=runner.invoke(app,["app","validate","-s","phone","--package",PACKAGE,"--expect-debuggable","maybe","--json"])
    assert result.exit_code==2 and result.stderr=="" and json.loads(result.stdout)["status"]=="refused"


def test_new_observation_tools_work_over_official_mcp_sdk_client(session):
    from mcp import Client
    server = create_server(AccessPolicy(read_only=True, allowed_devices=("exact target",)))
    async def exercise():
        async with Client(server) as client:
            for name, args in (
                ("app_validate", {"device":"exact target", "package":PACKAGE, "expected_debuggable":True}),
                ("app_logs", {"device":"exact target", "package":PACKAGE, "lines":10}),
                ("debug_app_inspect", {"device":"exact target"}),
                ("build_modes", {}),
            ):
                result = await client.call_tool(name, args)
                assert not result.is_error
                assert isinstance(json.loads(result.content[0].text), dict)
    anyio.run(exercise)
