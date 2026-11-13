"""Desktop sessions are explicit, bounded and never inferred to be DeX."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from adapt_adb import desktop
from adapt_adb.cli import app


def forbid(*a, **k):
    pytest.fail("Unexpected process/device access")


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(desktop, "resolve_adb", lambda: Path("chosen-adb"))
    monkeypatch.setattr(desktop, "preflight_adb_server", lambda *a: None)
    monkeypatch.setattr(desktop, "probe", forbid)
    monkeypatch.setattr(desktop.subprocess, "Popen", forbid)
    monkeypatch.setattr(desktop.dev_options, "apply_profile", forbid)


def test_plan_is_offline_and_json_clean(monkeypatch):
    monkeypatch.setattr(desktop, "resolve_adb", forbid)
    result = CliRunner().invoke(app, ["desktop", "plan", "-s", "exact target", "--app", "com.example.app", "--json"])
    assert result.exit_code == 0 and result.stderr == ""
    report = json.loads(result.stdout)
    assert report["device_access"] is False and report["samsung_dex"] == "not_inferred"
    assert report["argv"][1:3] == ["-s", "exact target"]
    assert "--no-vd-destroy-content" in report["argv"]


@pytest.mark.parametrize("kwargs", [{"app": "bad;reboot"}, {"app": "-com.app"}, {"size": "9000x1080"}, {"dpi": 0}, {"size": "0x0"}])
def test_invalid_launch_parameters_fail_before_device_work(kwargs):
    values = {"serial": "phone", "app": "com.example.app", **kwargs}
    with pytest.raises(desktop.DetectionError):
        desktop.start(**values, yes=True)


def test_confirmation_before_any_probe(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop, "resolve_adb", forbid)
    with pytest.raises(desktop.DetectionError, match="--yes"):
        desktop.start("phone", "com.example.app")
    with pytest.raises(desktop.DetectionError, match="--yes"):
        desktop.enable("phone", tmp_path / "settings.json")


@pytest.mark.parametrize("sdk", ["28", "38", "", "Permission denial"])
def test_unsupported_or_unknown_sdk_never_starts(sdk, monkeypatch):
    monkeypatch.setattr(desktop, "probe", lambda args: {"status": "observed", "value": sdk})
    with pytest.raises(desktop.DetectionError, match="API 29"):
        desktop.start("phone", "com.example.app", yes=True)


def test_start_uses_chosen_adb_exact_target_and_preserves_other_sessions(monkeypatch):
    calls = []
    monkeypatch.setattr(desktop, "resolve_scrcpy", lambda: Path("chosen-scrcpy"))
    monkeypatch.setattr(desktop, "probe", lambda args: {"status": "observed", "value": "37" if "getprop" in args else "scrcpy 5.0.1"})
    monkeypatch.setattr(desktop.subprocess, "Popen", lambda args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace(pid=101))
    report = desktop.start("exact target", "com.example.app", yes=True, view_only=True)
    assert report["status"] == "process_started" and report["stream_health"] == "not_verified"
    assert len(calls) == 1 and calls[0][0][:3] == ["chosen-scrcpy", "-s", "exact target"]
    assert "--no-control" in calls[0][0] and "--no-vd-destroy-content" in calls[0][0]
    assert calls[0][1]["env"]["ADB"] == "chosen-adb"


@pytest.mark.parametrize("version", ["2.7", "3.0"])
def test_old_scrcpy_is_rejected(monkeypatch, version):
    monkeypatch.setattr(desktop, "resolve_scrcpy", lambda: Path("chosen-scrcpy"))
    monkeypatch.setattr(desktop, "probe", lambda args: {"status": "observed", "value": "36" if "getprop" in args else f"scrcpy {version}"})
    with pytest.raises(desktop.DetectionError, match="scrcpy 3.1"):
        desktop.start("phone", "com.example.app", yes=True)


def test_inspection_does_not_claim_desktop_from_features_or_settings(monkeypatch):
    def probe(args):
        value = "36" if "getprop" in args else "feature:android.software.freeform_window_management" if "features" in args else "1" if "settings" in args else "Unknown OEM dump"
        return {"status": "observed", "value": value}
    monkeypatch.setattr(desktop, "probe", probe)
    report = desktop.inspect_device("phone")
    assert report["desktop_shell"] == "not_verified" and report["samsung_dex"] == "not_inferred"
    assert report["displays"]["status"] == "unknown"
    assert report["advertised_features"]["android.software.freeform_window_management"] == "advertised"


def test_inspection_handles_probe_failures_cleanly(monkeypatch):
    monkeypatch.setattr(desktop, "probe", lambda args: {"status": "failed", "value": None})
    try:
        report = desktop.inspect_device("phone")
    except desktop.DetectionError:
        pass
    else:
        assert isinstance(report, dict)

