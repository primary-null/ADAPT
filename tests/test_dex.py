"""Offline tests for bounded Samsung DeX inspection and launch planning."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from adapt_adb import dex


# These are synthetic parser fixtures, not Galaxy captures or hardware proof.
ACTIVE_DUAL = """\
SERVICE desktopmode:
  mCurrentState=DesktopModeState{enabled=4, disabled=0}
  mDualModeEnabled=true
  mStandaloneModeEnabled=false
  mDesktopDisplayId=17
"""

ACTIVE_STANDALONE = """\
SERVICE desktopmode:
  mCurrentState=DesktopModeState{enabled=4, disabled=0}
  mDualModeEnabled=false
  mStandaloneModeEnabled=true
  mDesktopDisplayId=0
"""

INACTIVE = """\
SERVICE desktopmode:
  mCurrentState=DesktopModeState{enabled=1, disabled=1}
  mDualModeEnabled=false
  mStandaloneModeEnabled=false
  mDesktopDisplayId=-1
"""

DISPLAYS = """\
Display Devices: size=2
  DisplayDeviceInfo{"Built-in": 1080 x 2340, modeId 99, supportedModes [{id=98, width=1080}]}
  DisplayDeviceInfo{"External": 1920 x 1080, modeId 17, supportedModes [{id=700, width=1920}]}
Logical Displays: size=2
  Display 0:
    mDisplayId=0
    mOverrideDisplayInfo=DisplayInfo{"Built-in Screen", displayId 0, FLAG_ALLOWED_TO_BE_DEFAULT_DISPLAY, real 1080 x 2340, density 420 (420.0 x 420.0) dpi}
  Display 17:
    mDisplayId=17
    mOverrideDisplayInfo=DisplayInfo{"HDMI sink", displayId 17, FLAG_PRESENTATION, real 1920 x 1080, density 160 (160.0 x 160.0) dpi}
"""


class FakeRunner:
    def __init__(
        self,
        *,
        manufacturer: str = "samsung",
        model: str = "Synthetic Model",
        sdk: str = "34",
        desktop: str = ACTIVE_DUAL,
        display: str = DISPLAYS,
        desktop_returncode: int = 0,
        desktop_stderr: str = "",
    ) -> None:
        self.manufacturer = manufacturer
        self.model = model
        self.sdk = sdk
        self.desktop = desktop
        self.display = display
        self.desktop_returncode = desktop_returncode
        self.desktop_stderr = desktop_stderr
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(argv, 0, "scrcpy 5.0.1\n", "")
        if argv[-2:] == ["getprop", "ro.product.manufacturer"]:
            return subprocess.CompletedProcess(argv, 0, self.manufacturer + "\n", "")
        if argv[-2:] == ["getprop", "ro.product.model"]:
            return subprocess.CompletedProcess(argv, 0, self.model + "\n", "")
        if argv[-2:] == ["getprop", "ro.build.version.sdk"]:
            return subprocess.CompletedProcess(argv, 0, self.sdk + "\n", "")
        if argv[-2:] == ["dumpsys", "desktopmode"]:
            return subprocess.CompletedProcess(
                argv, self.desktop_returncode, self.desktop, self.desktop_stderr
            )
        if argv[-2:] == ["dumpsys", "display"]:
            return subprocess.CompletedProcess(argv, 0, self.display, "")
        raise AssertionError(f"unexpected command: {argv!r}")


def no_op_preflight(adb: Path, args: list[str]) -> None:
    assert adb == Path("C:/adapt/adb.exe")
    assert args[-3:] == ["shell", "getprop", "ro.build.version.sdk"]


def inspect(fake: FakeRunner, serial: str = "phone") -> dict:
    return dex.inspect_device(
        serial,
        adb_path=Path("C:/adapt/adb.exe"),
        runner=fake,
        preflight=no_op_preflight,
    )


def test_active_dual_requires_matching_runtime_display_and_dynamic_id():
    report = inspect(FakeRunner())

    assert report["dex"]["classification"] == "active_dual"
    assert report["dex"]["confirmed_display_id"] == 17
    assert [item["id"] for item in report["displays"]["items"]] == [0, 17]
    assert report["displays"]["items"][1]["resolution"] == {
        "width": 1920,
        "height": 1080,
    }


def test_physical_mode_ids_never_become_logical_displays():
    parsed = dex.parse_logical_displays(
        DISPLAYS
        + "\nDisplay Groups: size=1\n"
        + "  Display 700:\n    mDisplayId=700\n"
    )

    assert parsed["status"] == "observed"
    assert {item["id"] for item in parsed["items"]} == {0, 17}
    assert not ({98, 99, 700} & {item["id"] for item in parsed["items"]})


def test_shared_daemon_preflight_runs_before_any_adb_command():
    calls: list[str] = []

    def blocked(adb: Path, args: list[str]) -> None:
        calls.append("preflight")
        raise dex.UnsafeAdbServerError("synthetic unsafe daemon")

    with pytest.raises(dex.DexError) as caught:
        dex.inspect_device(
            "phone",
            adb_path=Path("C:/adapt/adb.exe"),
            runner=lambda *args: pytest.fail("ADB command reached"),
            preflight=blocked,
        )

    assert caught.value.kind == "unsafe_adb_server"
    assert calls == ["preflight"]


@pytest.mark.parametrize(
    ("desktop", "expected"),
    [(ACTIVE_STANDALONE, "active_standalone"), (INACTIVE, "inactive")],
)
def test_standalone_and_off_are_distinct(desktop: str, expected: str):
    report = inspect(FakeRunner(desktop=desktop))

    assert report["dex"]["classification"] == expected
    if expected == "active_standalone":
        assert report["dex"]["confirmed_display_id"] == 0
        assert "not a distinct dual-mirror" in report["dex"]["confirmation_reason"]
    else:
        assert report["dex"]["confirmed"] is False


def test_non_samsung_identity_never_confirms_dex_from_same_dump():
    report = inspect(FakeRunner(manufacturer="Google", model="Pixel"))

    assert report["dex"]["classification"] == "unknown"
    assert report["dex"]["confirmed"] is False
    assert "non-Samsung" in report["dex"]["confirmation_reason"]


@pytest.mark.parametrize(
    ("stderr", "returncode"),
    [
        ("Can't find service: desktopmode", 0),
        ("Permission Denial: dumpsys desktopmode", 1),
    ],
)
def test_unknown_or_denied_desktop_service_is_unknown(stderr: str, returncode: int):
    report = inspect(
        FakeRunner(desktop="", desktop_returncode=returncode, desktop_stderr=stderr)
    )

    assert report["dex"]["service_status"] == "unknown"
    assert report["dex"]["classification"] == "unknown"
    assert report["dex"]["confirmed"] is False


@pytest.mark.parametrize(
    "desktop",
    [
        ACTIVE_DUAL + "  mDesktopDisplayId=22\n",
        ACTIVE_DUAL + "  mDualModeEnabled=true\n",
        ACTIVE_DUAL.replace("mDualModeEnabled=true", "mDualModeEnabled=maybe"),
        ACTIVE_DUAL.replace("mDesktopDisplayId=17", "mDesktopDisplayId=0"),
    ],
)
def test_duplicate_conflicting_or_malformed_state_is_unknown(desktop: str):
    parsed = dex.parse_desktop_mode(desktop)

    assert parsed["classification"] == "unknown"
    assert parsed["issues"]


def test_unrecognized_display_format_is_actionable_unknown():
    parsed = dex.parse_logical_displays(
        "Display Devices: supportedModes [{id=2}] mDisplayId=99"
    )

    assert parsed["status"] == "unknown"
    assert "Logical Displays" in parsed["reason"]


@pytest.mark.parametrize(
    ("sdk", "expected"),
    [("32", "below_minimum"), ("33", "eligible"), ("37", "eligible"), ("38", "unverified_future")],
)
def test_android_version_boundaries_are_explicit(sdk: str, expected: str):
    report = inspect(FakeRunner(sdk=sdk))
    assert report["workflow"]["status"] == expected


def test_exact_spaced_mdns_serial_is_one_argv_element_every_time():
    serial = "adb-Example Phone (2)._adb-tls-connect._tcp"
    fake = FakeRunner()
    report = inspect(fake, serial=serial)

    assert report["serial"] == serial
    assert all(call[1:3] == ["-s", serial] for call in fake.calls)


def test_plan_uses_existing_displays_exact_serial_no_virtual_display_or_ports():
    serial = "adb-Example Phone (2)._adb-tls-connect._tcp"
    fake = FakeRunner()
    plan = dex.plan_dual_mirror(
        serial,
        17,
        adb_path=Path("C:/adapt/adb.exe"),
        runner=fake,
        preflight=no_op_preflight,
    )

    assert plan["target_classification"] == "confirmed_active_samsung_dex"
    assert plan["instances"][0]["argv"] == [
        "scrcpy",
        "-s",
        serial,
        "--display-id=0",
        "--no-audio",
        "--window-title",
        "ADAPT Phone / display 0",
    ]
    assert plan["instances"][1]["argv"] == [
        "scrcpy",
        "-s",
        serial,
        "--display-id=17",
        "--no-audio",
        "--window-title",
        "ADAPT DeX / display 17",
    ]
    flattened = [arg for instance in plan["instances"] for arg in instance["argv"]]
    assert not any(arg.startswith("--new-display") for arg in flattened)
    assert not any(arg.startswith("--port") for arg in flattened)
    assert "--no-control" not in flattened  # control is on by default


def test_unknown_dex_state_plans_only_unconfirmed_existing_display_candidate():
    fake = FakeRunner(desktop="unrecognized vendor format")
    plan = dex.plan_dual_mirror(
        "phone",
        17,
        adb_path=Path("C:/adapt/adb.exe"),
        runner=fake,
        preflight=no_op_preflight,
    )

    assert plan["target_classification"] == "external_display_candidate_unconfirmed"
    assert plan["runtime_dex_confirmed"] is False


@pytest.mark.parametrize(
    ("display_id", "kind"),
    [(0, "primary_duplicate"), (2, "missing_display")],
)
def test_plan_rejects_primary_duplicate_and_missing_selection(display_id: int, kind: str):
    with pytest.raises(dex.DexError) as caught:
        dex.plan_dual_mirror(
            "phone",
            display_id,
            adb_path=Path("C:/adapt/adb.exe"),
            runner=FakeRunner(),
            preflight=no_op_preflight,
        )
    assert caught.value.kind == kind


def test_plan_rejects_below_android_13_but_future_is_unverified_plan():
    with pytest.raises(dex.DexError) as caught:
        dex.plan_dual_mirror(
            "phone",
            17,
            adb_path=Path("C:/adapt/adb.exe"),
            runner=FakeRunner(sdk="32"),
            preflight=no_op_preflight,
        )
    assert caught.value.kind == "unsupported_api"

    future = dex.plan_dual_mirror(
        "phone",
        17,
        adb_path=Path("C:/adapt/adb.exe"),
        runner=FakeRunner(sdk="38"),
        preflight=no_op_preflight,
    )
    assert future["workflow_verification"] == "unverified_future"


def test_mirror_without_yes_has_no_adb_resolution_or_process_side_effect(monkeypatch):
    monkeypatch.setattr(dex, "resolve_adb", lambda: pytest.fail("resolved adb"))

    with pytest.raises(dex.DexError) as caught:
        dex.mirror_dual(
            "phone",
            17,
            yes=False,
            runner=lambda *args: pytest.fail("ran process"),
            popen_factory=lambda *args, **kwargs: pytest.fail("launched process"),
        )

    assert caught.value.kind == "confirmation_required"


def test_mirror_refuses_unconfirmed_display_before_scrcpy_launch():
    launched = []
    with pytest.raises(dex.DexError) as caught:
        dex.mirror_dual(
            "phone",
            17,
            yes=True,
            adb_path=Path("C:/adapt/adb.exe"),
            runner=FakeRunner(desktop="unrecognized vendor format"),
            preflight=no_op_preflight,
            scrcpy_resolver=lambda _: Path("C:/tools/scrcpy.exe"),
            popen_factory=lambda *args, **kwargs: launched.append(args),
        )

    assert caught.value.kind == "dex_unconfirmed"
    assert launched == []


class FakeProcess:
    def __init__(self, pid: int):
        self.pid = pid
        self.terminated = False
        self.killed = False
        self.waits: list[int] = []

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: int) -> int:
        self.waits.append(timeout)
        return 0


def test_second_launch_failure_cleans_only_process_started_by_this_invocation():
    owned = FakeProcess(101)
    calls = []

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        if len(calls) == 1:
            return owned
        raise OSError("synthetic second launch failure")

    with pytest.raises(dex.DexError) as caught:
        dex.mirror_dual(
            "phone",
            17,
            yes=True,
            adb_path=Path("C:/adapt/adb.exe"),
            runner=FakeRunner(),
            preflight=no_op_preflight,
            scrcpy_resolver=lambda _: Path("C:/tools/scrcpy.exe"),
            popen_factory=popen,
        )

    assert caught.value.kind == "launch_failed"
    assert owned.terminated is True
    assert owned.killed is False
    assert len(calls) == 2


def test_successful_launch_uses_adapt_adb_view_only_and_reports_unverified_health():
    processes = [FakeProcess(201), FakeProcess(202)]
    calls = []

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return processes[len(calls) - 1]

    result = dex.mirror_dual(
        "phone",
        17,
        yes=True,
        view_only=True,
        adb_path=Path("C:/adapt/adb.exe"),
        runner=FakeRunner(),
        preflight=no_op_preflight,
        scrcpy_resolver=lambda _: Path("C:/tools/scrcpy.exe"),
        popen_factory=popen,
    )

    assert result["status"] == "processes_started"
    assert result["healthy_streaming"] == "not_verified"
    assert result["process_ids"] == [201, 202]
    assert all("--no-control" in argv for argv, _ in calls)
    assert all(kwargs["env"]["ADB"] == str(Path("C:/adapt/adb.exe")) for _, kwargs in calls)
    assert all(kwargs["shell"] is False for _, kwargs in calls)
    assert all(kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0) for _, kwargs in calls)


def test_cli_json_contract_can_be_wired_as_a_typer_group(monkeypatch):
    monkeypatch.setattr(
        dex,
        "inspect_device",
        lambda serial: {
            "serial": serial,
            "device": {"manufacturer": "samsung", "model": "Synthetic", "api_level": 34},
            "workflow": {"status": "eligible"},
            "dex": {"classification": "unknown", "confirmation_reason": "unknown"},
            "displays": {"items": []},
            "claim_boundary": "not certified",
        },
    )
    result = CliRunner().invoke(dex.dex_app, ["inspect", "-s", "phone", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["serial"] == "phone"

