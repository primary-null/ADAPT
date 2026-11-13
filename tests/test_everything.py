"""Batch setup exercises mocked independent steps, never real hosts/devices."""

import json

import pytest
from typer.testing import CliRunner

from adapt_adb import everything as batch
from adapt_adb.cli import app
from adapt_adb.tool_catalog import HostEnvironment, build_list_report


@pytest.fixture
def environment(monkeypatch):
    host = HostEnvironment("windows", "windows-native", "x86_64", "test", "Synthetic Windows", False)
    inventory = build_list_report(host=host, which=lambda _: None)
    monkeypatch.setattr(batch, "build_list_report", lambda: inventory)
    monkeypatch.setattr(batch, "plan_scrcpy", lambda: {"status": "ready", "target_dir": "synthetic-managed"})
    monkeypatch.setattr(batch.device_state, "scan", lambda **k: {"status": "observed", "transports": {"fastboot": {"status": "observed", "devices": []}}})
    monkeypatch.setattr(batch.device_state, "inspect_device", lambda target: {"status": "observed"})
    monkeypatch.setattr(batch.desktop, "inspect_device", lambda target: {"status": "observed"})
    monkeypatch.setattr(batch.development, "inspect_debug", lambda target: {"status": "observed"})
    return inventory


def forbid(*args, **kwargs):
    pytest.fail("Forbidden side effect was reached")


def test_dry_plan_covers_all_tools_without_execution(environment, monkeypatch):
    for name in ("run_doctor_checks", "run_setup_workflow", "install_scrcpy"):
        monkeypatch.setattr(batch, name, forbid)
    report = batch.run_everything(dry_run=True, devices=["exact target"], confirm=forbid)
    assert report["status"] == "dry_run" and report["writes"] is False and report["downloads"] is False
    assert {a["tool"] for a in report["automatic"]} | {m["tool"] for m in report["manual_tools"]} == {t["id"] for t in environment["tools"]}
    assert len(report["android_versions"]["versions"]) == 9


def test_decline_does_not_inspect_or_install(environment, monkeypatch):
    monkeypatch.setattr(batch, "run_doctor_checks", forbid)
    result = batch.run_everything(confirm=lambda text: False)
    assert result["status"] == "cancelled"


def test_unsafe_listener_blocks_installers_and_device_queries(environment, monkeypatch):
    monkeypatch.setattr(batch, "run_doctor_checks", lambda: {"exit_code": 3})
    monkeypatch.setattr(batch, "run_setup_workflow", forbid)
    monkeypatch.setattr(batch, "install_scrcpy", forbid)
    monkeypatch.setattr(batch.android_support, "inspect_capabilities", forbid)
    result = batch.run_everything(yes=True, devices=["phone"])
    assert result["status"] == "blocked" and result["exit_code"] == 3
    assert result["device_inspections"][0]["status"] == "skipped"


def test_batch_installs_supported_tools_and_preserves_exact_device_target(environment, monkeypatch):
    calls = []
    monkeypatch.setattr(batch, "run_doctor_checks", lambda: {"exit_code": 0})
    monkeypatch.setattr(batch, "run_setup_workflow", lambda **k: calls.append(("setup", k)) or {"exit_code": 0, "setup": {"action": "installed"}})
    monkeypatch.setattr(batch, "install_scrcpy", lambda **k: calls.append(("scrcpy", k)) or {"status": "reuse_existing"})
    for name, module, fn in (("android", batch.android_support, "inspect_capabilities"), ("options", batch.dev_options, "inspect_device"), ("dex", batch.dex, "inspect_device")):
        monkeypatch.setattr(module, fn, lambda target, name=name: calls.append((name, target)) or {"observed": True})
    monkeypatch.setattr(batch.dev_options, "apply_profile", forbid)
    monkeypatch.setattr(batch.dex, "mirror_dual", forbid)
    target = "adb-Phone (2)._adb-tls-connect._tcp"
    result = batch.run_everything(yes=True, devices=[target, target], no_path=True)
    assert result["status"] == "completed_with_manual_steps" and result["exit_code"] == 2
    assert result["summary"]["installed"] == ["platform-tools"] and result["summary"]["reused"] == ["scrcpy"]
    assert calls[:2] == [("setup", {"yes": True, "force": False, "no_path": True}), ("scrcpy", {"yes": True})]
    assert calls[2:] == [("android", target), ("options", target), ("dex", target)]


def test_independent_failure_keeps_success_and_reports_partial(environment, monkeypatch):
    monkeypatch.setattr(batch, "run_doctor_checks", lambda: {"exit_code": 0})
    monkeypatch.setattr(batch, "run_setup_workflow", lambda **k: {"exit_code": 0, "setup": {"action": "installed"}})
    monkeypatch.setattr(batch, "install_scrcpy", lambda **k: (_ for _ in ()).throw(batch.ScrcpyInstallError("synthetic")))
    result = batch.run_everything(yes=True)
    assert result["status"] == "failed" and result["exit_code"] == 1
    assert result["summary"]["installed"] == ["platform-tools"]


def test_json_requires_confirmation_and_dry_run_remains_clean(environment, monkeypatch):
    monkeypatch.setattr(batch, "run_doctor_checks", forbid)
    runner = CliRunner()
    result = runner.invoke(app, ["everything", "--json"])
    assert result.exit_code == 2 and result.stderr == ""
    assert json.loads(result.stdout)["status"] == "refused"
    result = runner.invoke(app, ["--no-progress", "everything", "--dry-run", "--json"])
    assert result.exit_code == 0 and result.stderr == ""
    assert json.loads(result.stdout)["status"] == "dry_run"


def test_invalid_device_is_rejected_before_detection(monkeypatch):
    monkeypatch.setattr(batch, "build_list_report", forbid)
    with pytest.raises(batch.EverythingError):
        batch.run_everything(yes=True, devices=["bad\ntarget"])


@pytest.mark.parametrize("final_code", [3, None, True, -1])
def test_final_safety_failure_blocks_device_probes(environment, monkeypatch, final_code):
    reports = iter([{"exit_code": 0}, {"exit_code": final_code}])
    monkeypatch.setattr(batch, "run_doctor_checks", lambda: next(reports))
    monkeypatch.setattr(batch, "run_setup_workflow", lambda **k: {"exit_code": 0, "setup": {"action": "noop_already_healthy"}})
    monkeypatch.setattr(batch, "install_scrcpy", lambda **k: {"status": "reuse_existing"})
    for module, name in ((batch.android_support, "inspect_capabilities"), (batch.dev_options, "inspect_device"), (batch.dex, "inspect_device")):
        monkeypatch.setattr(module, name, forbid)
    result = batch.run_everything(yes=True, devices=["phone"])
    assert result["exit_code"] == 3
    assert result["summary"]["reused"] == ["platform-tools", "scrcpy"]
    assert result["device_inspections"][0]["status"] == "skipped"


def test_unsupported_host_leaves_platform_acquisition_manual(environment, monkeypatch):
    environment["host"]["arch"] = "arm64"
    monkeypatch.setattr(batch, "run_doctor_checks", lambda: {"exit_code": 1})
    monkeypatch.setattr(batch, "run_setup_workflow", forbid)
    monkeypatch.setattr(batch, "install_scrcpy", lambda **k: {"status": "installed"})
    result = batch.run_everything(yes=True)
    assert result["exit_code"] == 2
    assert any(s["step"] == "platform-tools" and s["status"] == "manual_required" for s in result["steps"])
    assert result["summary"]["installed"] == ["scrcpy"]


def test_complete_reuse_is_distinct_from_new_install(environment, monkeypatch):
    for tool in environment["tools"]:
        tool["detection"]["status"] = "found"
    monkeypatch.setattr(batch, "run_doctor_checks", lambda: {"exit_code": 0})
    monkeypatch.setattr(batch, "run_setup_workflow", lambda **k: {"exit_code": 0, "setup": {"action": "noop_already_healthy"}})
    monkeypatch.setattr(batch, "install_scrcpy", lambda **k: {"status": "already_installed"})
    result = batch.run_everything(yes=True)
    assert result["status"] == "completed" and result["exit_code"] == 0
    assert result["summary"]["installed"] == []
    assert result["summary"]["reused"] == ["platform-tools", "scrcpy"]
