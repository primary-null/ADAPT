"""Offline tests: an unreachable device is an error, not a non-Samsung device."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from adapt_adb import dex
from tests.test_dex import ACTIVE_DUAL, DISPLAYS, FakeRunner, inspect, no_op_preflight

ADB = Path("C:/adapt/adb.exe")

UNREACHABLE_MESSAGE = (
    "The device did not answer any read-only query; it may be disconnected, "
    "offline, unauthorized, or the serial may be wrong. "
    "Run 'adapt devices' to check its state."
)
NON_SAMSUNG_SENTENCE = "Samsung DeX cannot be confirmed on a non-Samsung manufacturer identity."
UNREAD_MANUFACTURER_SENTENCE = (
    "The device manufacturer could not be read, so Samsung DeX cannot be confirmed."
)

# (returncode, stdout, stderr) outcomes for one adb probe.
OK = "ok"
ADB_FAIL = (1, "", "error: device offline")
DENIED = (1, "", "Permission Denial: getprop")
EMPTY = (0, "\n", "")

PROBE_NAMES = {
    ("getprop", "ro.product.manufacturer"): "manufacturer",
    ("getprop", "ro.product.model"): "model",
    ("getprop", "ro.build.version.sdk"): "sdk",
    ("dumpsys", "desktopmode"): "desktop",
    ("dumpsys", "display"): "display",
}


class ScriptedRunner:
    """Healthy Samsung device by default; any probe can be scripted to fail."""

    def __init__(self, **outcomes) -> None:
        self.healthy = {
            "manufacturer": (0, "samsung\n", ""),
            "model": (0, "Synthetic Model\n", ""),
            "sdk": (0, "34\n", ""),
            "desktop": (0, ACTIVE_DUAL, ""),
            "display": (0, DISPLAYS, ""),
        }
        self.outcomes = {**self.healthy, **outcomes}
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        name = PROBE_NAMES[tuple(argv[-2:])]
        returncode, stdout, stderr = self.outcomes[name]
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    def dumpsys_calls(self) -> list[list[str]]:
        return [call for call in self.calls if "dumpsys" in call]


ALL_IDENTITY_FAIL = {"manufacturer": ADB_FAIL, "model": ADB_FAIL, "sdk": ADB_FAIL}


def run_inspect(runner, serial: str = "phone") -> dict:
    return dex.inspect_device(serial, adb_path=ADB, runner=runner, preflight=no_op_preflight)


def run_list(runner, serial: str = "phone") -> dict:
    return dex.list_displays(serial, adb_path=ADB, runner=runner, preflight=no_op_preflight)


@pytest.fixture
def offline_cli(monkeypatch):
    """Route the real Typer commands through a fake runner; no adb ever starts."""

    def install(runner) -> CliRunner:
        monkeypatch.setattr(dex, "resolve_adb", lambda: ADB)
        monkeypatch.setattr(dex, "preflight_adb_server", lambda adb, args: None)
        monkeypatch.setattr(dex, "_run_process", runner)
        monkeypatch.setattr(
            dex.subprocess, "run", lambda *a, **k: pytest.fail("real subprocess.run reached")
        )
        return CliRunner()

    return install


# --- unreachable device -------------------------------------------------------


@pytest.mark.parametrize(
    "stderr",
    [
        "error: device 'phone' not found",
        "error: device offline",
        "error: device unauthorized.",
        "error: closed",
    ],
)
def test_every_identity_probe_failing_is_device_unreachable(stderr: str):
    failure = (1, "", stderr)
    runner = ScriptedRunner(manufacturer=failure, model=failure, sdk=failure)

    with pytest.raises(dex.DexError) as caught:
        run_inspect(runner)

    assert caught.value.kind == "device_unreachable"
    assert str(caught.value) == UNREACHABLE_MESSAGE
    assert "non-Samsung" not in str(caught.value)


def test_unreachable_check_stops_before_the_dumpsys_probes():
    runner = ScriptedRunner(**ALL_IDENTITY_FAIL)

    with pytest.raises(dex.DexError):
        run_inspect(runner)

    assert [call[-1] for call in runner.calls] == [
        "ro.product.manufacturer",
        "ro.product.model",
        "ro.build.version.sdk",
    ]
    assert runner.dumpsys_calls() == []


def test_unreachable_message_never_carries_serial_or_device_text():
    serial = "stale-serial-123"
    leaky = (1, "device says SECRET-TOKEN", f"error: device '{serial}' not found SECRET-TOKEN")
    runner = ScriptedRunner(manufacturer=leaky, model=leaky, sdk=leaky)

    with pytest.raises(dex.DexError) as caught:
        run_inspect(runner, serial=serial)

    assert str(caught.value) == UNREACHABLE_MESSAGE
    assert serial not in str(caught.value)
    assert "SECRET-TOKEN" not in str(caught.value)


def test_cli_inspect_unreachable_exits_one_without_a_non_samsung_claim(offline_cli):
    cli = offline_cli(ScriptedRunner(**ALL_IDENTITY_FAIL))

    result = cli.invoke(dex.dex_app, ["inspect", "-s", "stale"])

    assert result.exit_code == 1
    assert f"Error: {UNREACHABLE_MESSAGE}" in result.output
    assert "non-Samsung" not in result.output
    assert "cannot be confirmed" not in result.output
    assert "Logical displays" not in result.output


def test_cli_inspect_unreachable_json_uses_the_emit_error_shape(offline_cli):
    cli = offline_cli(ScriptedRunner(**ALL_IDENTITY_FAIL))

    result = cli.invoke(dex.dex_app, ["inspect", "-s", "stale", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    # Failure guidance Phase 2 adds one additive `failure` key; the original keys are unchanged.
    failure = payload.pop("failure")
    assert payload == {
        "ok": False,
        "error": "device_unreachable",
        "message": UNREACHABLE_MESSAGE,
    }
    assert failure["id"] == "device.unreachable"


@pytest.mark.parametrize("json_flag", [[], ["--json"]])
def test_cli_list_displays_unreachable_exits_one(offline_cli, json_flag):
    cli = offline_cli(ScriptedRunner(**ALL_IDENTITY_FAIL))

    result = cli.invoke(dex.dex_app, ["list-displays", "-s", "stale", *json_flag])

    assert result.exit_code == 1
    assert "device_unreachable" in result.output or UNREACHABLE_MESSAGE in result.output
    assert "non-Samsung" not in result.output


def test_cli_plan_dual_mirror_unreachable_exits_one(offline_cli):
    cli = offline_cli(ScriptedRunner(**ALL_IDENTITY_FAIL))

    result = cli.invoke(
        dex.dex_app, ["plan-dual-mirror", "-s", "stale", "--dex-display", "17", "--json"]
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == "device_unreachable"


def test_mcp_dex_inspect_reports_device_unreachable_as_an_error(offline_cli):
    from adapt_adb.mcp import tools

    offline_cli(ScriptedRunner(**ALL_IDENTITY_FAIL))

    for displays_only in (False, True):
        payload = tools.tool_dex_inspect("stale", displays_only=displays_only)
        assert payload["error"] == "device_unreachable"
        assert "non-Samsung" not in json.dumps(payload)


def test_mirror_and_plan_surface_device_unreachable_before_any_other_step():
    with pytest.raises(dex.DexError) as planned:
        dex.plan_dual_mirror(
            "stale",
            17,
            adb_path=ADB,
            runner=ScriptedRunner(**ALL_IDENTITY_FAIL),
            preflight=no_op_preflight,
        )
    assert planned.value.kind == "device_unreachable"

    with pytest.raises(dex.DexError) as mirrored:
        dex.mirror_dual(
            "stale",
            17,
            yes=True,
            adb_path=ADB,
            runner=ScriptedRunner(**ALL_IDENTITY_FAIL),
            preflight=no_op_preflight,
            scrcpy_resolver=lambda _: pytest.fail("scrcpy resolved for an unreachable device"),
            popen_factory=lambda *a, **k: pytest.fail("process launched"),
        )
    assert mirrored.value.kind == "device_unreachable"


# --- partially reachable device -----------------------------------------------


def test_unreadable_manufacturer_is_reported_as_unread_not_non_samsung():
    runner = ScriptedRunner(manufacturer=ADB_FAIL)

    report = run_inspect(runner)

    assert report["device"]["manufacturer"] is None
    assert report["device"]["model"] == "Synthetic Model"
    assert report["device"]["api_level"] == 34
    assert report["device"]["property_status"]["manufacturer"] == "error"
    assert report["dex"]["confirmation_reason"] == UNREAD_MANUFACTURER_SENTENCE
    assert "non-Samsung" not in report["dex"]["confirmation_reason"]
    assert report["dex"]["classification"] == "unknown"
    assert report["dex"]["confirmed"] is False
    assert len(runner.dumpsys_calls()) == 2


def test_empty_manufacturer_value_counts_as_unread():
    report = run_inspect(ScriptedRunner(manufacturer=EMPTY))

    assert report["device"]["manufacturer"] is None
    assert report["dex"]["confirmation_reason"] == UNREAD_MANUFACTURER_SENTENCE


def test_cli_inspect_partial_device_exits_zero_with_unread_wording(offline_cli):
    cli = offline_cli(ScriptedRunner(manufacturer=ADB_FAIL))

    result = cli.invoke(dex.dex_app, ["inspect", "-s", "phone"])

    assert result.exit_code == 0
    assert UNREAD_MANUFACTURER_SENTENCE in result.output
    assert "non-Samsung" not in result.output


def test_observed_non_samsung_manufacturer_keeps_the_existing_sentence():
    report = run_inspect(ScriptedRunner(manufacturer=(0, "Google\n", "")))

    assert report["device"]["manufacturer"] == "Google"
    assert report["dex"]["confirmation_reason"] == NON_SAMSUNG_SENTENCE
    assert report["dex"]["classification"] == "unknown"
    assert report["dex"]["confirmed"] is False


# --- denied probes are reachable ----------------------------------------------


def test_all_identity_probes_denied_is_a_report_not_unreachable():
    runner = ScriptedRunner(manufacturer=DENIED, model=DENIED, sdk=DENIED)

    report = run_inspect(runner)

    assert report["device"]["property_status"] == {
        "manufacturer": "denied",
        "model": "denied",
        "sdk": "denied",
    }
    assert report["device"]["manufacturer"] is None
    assert len(runner.dumpsys_calls()) == 2
    assert "non-Samsung" not in report["dex"]["confirmation_reason"]


@pytest.mark.parametrize(
    "outcomes",
    [
        {"manufacturer": ADB_FAIL, "model": DENIED, "sdk": ADB_FAIL},
        {"manufacturer": DENIED, "model": ADB_FAIL, "sdk": ADB_FAIL},
        {"manufacturer": ADB_FAIL, "model": ADB_FAIL, "sdk": DENIED},
    ],
)
def test_one_denied_identity_probe_among_failures_is_not_unreachable(outcomes):
    report = run_inspect(ScriptedRunner(**outcomes))

    assert report["serial"] == "phone"
    assert "denied" in report["device"]["property_status"].values()


# --- list_displays ------------------------------------------------------------


def test_list_displays_unreachable_raises_device_unreachable():
    runner = ScriptedRunner(**ALL_IDENTITY_FAIL)

    with pytest.raises(dex.DexError) as caught:
        run_list(runner)

    assert caught.value.kind == "device_unreachable"
    assert runner.dumpsys_calls() == []


def test_list_displays_healthy_device_still_lists_displays():
    report = run_list(ScriptedRunner())

    assert report["status"] == "observed"
    assert [item["id"] for item in report["displays"]] == [0, 17]
    assert report["confirmed_dex_display_id"] == 17


@pytest.mark.parametrize(
    "display_outcome",
    [
        (1, "", "Permission Denial: dumpsys display"),
        (0, "", "Can't find service: display"),
        (1, "", "error: closed"),
    ],
)
def test_list_displays_reachable_device_with_failed_display_probe_is_unknown(display_outcome):
    report = run_list(ScriptedRunner(display=display_outcome))

    assert report["status"] == "unknown"
    assert report["displays"] == []
    assert report["reason"]


# --- healthy device and unchanged error paths ---------------------------------


def test_healthy_samsung_fixture_report_is_unchanged():
    fake = FakeRunner()
    report = inspect(fake)

    assert report["device"] == {
        "manufacturer": "samsung",
        "model": "Synthetic Model",
        "api_level": 34,
        "property_status": {
            "manufacturer": "observed",
            "model": "observed",
            "sdk": "observed",
        },
    }
    assert report["workflow"]["status"] == "eligible"
    assert report["dex"]["classification"] == "active_dual"
    assert report["dex"]["confirmed_display_id"] == 17
    assert report["dex"]["confirmation_reason"] == (
        "Recognized active dual-mode state matches an existing non-primary logical display."
    )
    assert [item["id"] for item in report["displays"]["items"]] == [0, 17]
    assert len(fake.calls) == 5


def test_cli_healthy_inspect_output_is_unchanged(offline_cli):
    cli = offline_cli(FakeRunner())

    result = cli.invoke(dex.dex_app, ["inspect", "-s", "phone"])

    assert result.exit_code == 0
    assert result.output.splitlines()[:4] == [
        "samsung Synthetic Model (API 34)",
        "Workflow: eligible",
        "DeX state: active_dual",
        "Recognized active dual-mode state matches an existing non-primary logical display.",
    ]


def test_preflight_and_adb_level_errors_keep_their_own_kinds():
    def blocked(adb: Path, args: list[str]) -> None:
        raise dex.UnsafeAdbServerError("synthetic unsafe daemon")

    with pytest.raises(dex.DexError) as unsafe:
        dex.inspect_device(
            "phone", adb_path=ADB, runner=lambda *a: pytest.fail("adb reached"), preflight=blocked
        )
    assert unsafe.value.kind == "unsafe_adb_server"

    def raising(exc: BaseException):
        def runner(argv: list[str], timeout: int):
            raise exc

        return runner

    cases = [
        (raising(FileNotFoundError("adb")), "missing_adb"),
        (raising(subprocess.TimeoutExpired("adb", 10)), "timeout"),
        (raising(PermissionError("denied")), "adb_error"),
    ]
    for runner, kind in cases:
        with pytest.raises(dex.DexError) as caught:
            run_inspect(runner)
        assert caught.value.kind == kind

    with pytest.raises(dex.DexError) as invalid:
        run_inspect(ScriptedRunner(), serial="  ")
    assert invalid.value.kind == "invalid_serial"
