"""Offline tests for the display-flag evidence source of ``adapt dex inspect``.

When the ``desktopmode`` service does not exist (Android 16 / One UI 8 and
Android 17 / One UI 9), ADAPT falls back to the per-display flags of
``dumpsys display``.  That was observed on ONE Samsung phone on Android 17
during ONE live wireless DeX session.  Wired DeX, standalone DeX and the
Android 16 tablet with DeX running were not observed.

The fixtures below are built from the structure of those lines with invented
names and addresses; they are not verbatim captures.  The fallback must never
run when the service answered, whatever it said.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from adapt_adb import dex
from tests.test_dex import INACTIVE, FakeProcess, FakeRunner, no_op_preflight
from tests.test_dex_real_firmware import (
    ACTIVE_REAL,
    DISPLAYS_IDLE,
    DISPLAYS_WITH_DESKTOP,
    IDLE_REAL,
)
from tests.test_dex_unreachable import (
    ADB_FAIL,
    ALL_IDENTITY_FAIL,
    DENIED,
    EMPTY,
    NON_SAMSUNG_SENTENCE,
    UNREAD_MANUFACTURER_SENTENCE,
    ScriptedRunner,
    offline_cli,  # noqa: F401 - pytest fixture shared with the unreachable tests
)

ADB = Path("C:/adapt/adb.exe")
SHAPE = "dumpsys_display_flag_wireless_dex_display"
CLASSIFICATION = "active_wireless_dex_display_flag"
ABSENT_MESSAGE = "Can't find service: desktopmode"

WIRELESS_DEX = "FLAG_WIRELESS_DEX_DISPLAY"
EXTERNAL_HOSTING = "FLAG_EXTERNAL_DEX_HOSTING"
EXTERNAL_DEX_FLAGS = f"FLAG_TRUSTED, {EXTERNAL_HOSTING}, {WIRELESS_DEX}, FLAG_WIFI_DISPLAY, FLAG_ALLOWS_CONTENT_MODE_SWITCH"

NO_FLAG_SENTENCE = (
    "The desktopmode service is absent on this firmware and no logical display carries "
    "FLAG_WIRELESS_DEX_DISPLAY or FLAG_EXTERNAL_DEX_HOSTING, so no active DeX display was observed. "
    "This does not show that DeX is inactive."
)
SERVICE_RESULT_INACTIVE_SENTENCE = "Recognized desktop-mode fields report DeX inactive."
SERVICE_UNKNOWN_SENTENCE = "Runtime DeX state is unknown; secondary displays alone are not DeX proof."

ABSENT = (0, ABSENT_MESSAGE + "\n", "")


def display_block(display_id: int, name: str, flags: str, group: int = 0, size: str = "real 1920 x 1080") -> str:
    def info(group_id: int) -> str:
        return (
            f'DisplayInfo{{"{name}", displayId {display_id}, displayGroupId {group_id}, {flags}, '
            f"{size}, density 160 (160.0 x 160.0) dpi}}"
        )

    # Like the observed lines: the base info sits in the device's own display
    # group, the override info in group 0.  Both carry the same flags.
    return (
        f"  Display {display_id}:\n"
        f"    mDisplayId={display_id}\n"
        f"    mBaseDisplayInfo={info(group)}\n"
        f"    mOverrideDisplayInfo={info(0)}\n"
    )


def dump(*blocks: str) -> str:
    return (
        "Display Devices: size=2\n"
        '  DisplayDeviceInfo{"Built-in Screen": uniqueId="local:1000000000000000001", 1440 x 3120, modeId 3, supportedModes [{id=3, width=1440}]}\n'
        '  DisplayDeviceInfo{"TEST-RECEIVER": uniqueId="wifi:desktop:02:00:00:00:00:01", 1920 x 1080, modeId 22, renderFrameRate 0.0}\n'
        f"Logical Displays: size={len(blocks)}\n" + "".join(blocks)
    )


BUILT_IN = display_block(
    0, "Built-in Screen", "FLAG_ALLOWED_TO_BE_DEFAULT_DISPLAY, FLAG_TRUSTED", size="real 1440 x 3120"
)
RECEIVER = display_block(6, "TEST-RECEIVER", EXTERNAL_DEX_FLAGS, group=2)
SECOND_RECEIVER = display_block(7, "OTHER-RECEIVER", EXTERNAL_DEX_FLAGS, group=2)
PLAIN_EXTERNAL = display_block(7, "Plain External", "FLAG_PRESENTATION")

DISPLAYS_WIRELESS_DEX = dump(BUILT_IN, RECEIVER)
DISPLAYS_NO_FLAGS = dump(BUILT_IN, display_block(6, "Plain External", "FLAG_PRESENTATION"))
DISPLAYS_IDLE_ONLY_BUILT_IN = dump(BUILT_IN)


def absent_fake(display: str = DISPLAYS_WIRELESS_DEX, **kwargs) -> FakeRunner:
    kwargs.setdefault("sdk", "37")
    return FakeRunner(
        desktop="", desktop_returncode=0, desktop_stderr=ABSENT_MESSAGE, display=display, **kwargs
    )


def inspect_report(fake) -> dict:
    return dex.inspect_device("phone", adb_path=ADB, runner=fake, preflight=no_op_preflight)


def listing(fake) -> dict:
    return dex.list_displays("phone", adb_path=ADB, runner=fake, preflight=no_op_preflight)


def plan(fake, display_id: int) -> dict:
    return dex.plan_dual_mirror("phone", display_id, adb_path=ADB, runner=fake, preflight=no_op_preflight)


def mirror(fake, display_id: int, popen) -> dict:
    return dex.mirror_dual(
        "phone",
        display_id,
        yes=True,
        adb_path=ADB,
        runner=fake,
        preflight=no_op_preflight,
        scrcpy_resolver=lambda _: Path("C:/tools/scrcpy.exe"),
        popen_factory=popen,
    )


def refused_mirror(fake, display_id: int) -> dex.DexError:
    launched: list = []
    with pytest.raises(dex.DexError) as caught:
        mirror(fake, display_id, lambda *args, **kwargs: launched.append(args))
    assert launched == []
    return caught.value


def assert_not_confirmed(report: dict) -> None:
    assert report["dex"]["classification"] == "unknown"
    assert report["dex"]["confirmed"] is False
    assert report["dex"]["confirmed_display_id"] is None


# --- the fixtures are shaped like the real lines ---------------------------------


def test_fixture_flags_reach_the_logical_display_items():
    parsed = dex.parse_logical_displays(DISPLAYS_WIRELESS_DEX)

    assert parsed["status"] == "observed"
    assert [item["id"] for item in parsed["items"]] == [0, 6]
    receiver = parsed["items"][1]
    assert receiver["name"] == "TEST-RECEIVER"
    assert receiver["resolution"] == {"width": 1920, "height": 1080}
    assert receiver["flags"] == sorted(
        [
            "FLAG_TRUSTED",
            EXTERNAL_HOSTING,
            WIRELESS_DEX,
            "FLAG_WIFI_DISPLAY",
            "FLAG_ALLOWS_CONTENT_MODE_SWITCH",
        ]
    )
    assert WIRELESS_DEX not in parsed["items"][0]["flags"]


# --- service absent, one wireless-DeX display: confirmed ---------------------------


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr"),
    [
        (0, "", ABSENT_MESSAGE),
        (0, ABSENT_MESSAGE + "\n", ""),
        (1, "", ABSENT_MESSAGE),
    ],
)
def test_absent_service_with_one_wireless_dex_display_confirms_dex_on_it(
    returncode: int, stdout: str, stderr: str
):
    fake = FakeRunner(
        sdk="37",
        desktop=stdout,
        desktop_returncode=returncode,
        desktop_stderr=stderr,
        display=DISPLAYS_WIRELESS_DEX,
    )
    report = inspect_report(fake)
    desktop = report["dex"]

    assert desktop["status"] == "observed"
    assert desktop["classification"] == CLASSIFICATION
    assert desktop["confirmed"] is True
    assert desktop["confirmed_display_id"] == 6
    assert desktop["desktop_display_id"] == 6
    assert desktop["recognized_shape"] == SHAPE == dex.RECOGNIZED_SHAPE_DISPLAY_FLAGS
    assert desktop["recognized_shape"] not in {
        dex.RECOGNIZED_SHAPE_SYNTHETIC,
        dex.RECOGNIZED_SHAPE_OBSERVED,
    }
    assert desktop["service_status"] == "unknown"  # the service itself was never read
    assert desktop["issues"] == []
    assert desktop["recognized_fields"] == {
        "desktop_display_id": 6,
        "wireless_dex_display_flag": True,
        "external_dex_hosting_flag": True,
    }
    assert report["workflow"]["status"] == "eligible"
    assert report["mutations"] == []
    assert [item["id"] for item in report["displays"]["items"]] == [0, 6]


def test_confirmation_report_names_the_source_and_every_observation_limit():
    report = inspect_report(absent_fake())
    desktop = report["dex"]
    evidence = desktop["display_flag_evidence"]

    assert evidence["source"] == "dumpsys_display_flags" == dex.EVIDENCE_SOURCE_DISPLAY_FLAGS
    assert evidence["desktopmode_service"] == "absent"
    assert evidence["outcome"] == "matched"
    assert "dumpsys display" in evidence["flags_read_from"]
    assert evidence["flagged_displays"] == [{"id": 6, "flags": [WIRELESS_DEX, EXTERNAL_HOSTING]}]
    assert "one Samsung phone on Android 17" in evidence["observed_on"]
    assert "one Samsung tablet on Android 16" in evidence["observed_on"]
    assert "one live wireless DeX session" in evidence["observed_on"]
    assert evidence["not_observed"] == [
        "wired DeX",
        "standalone DeX",
        "a screen capture of the flagged display",
    ]

    reason = desktop["confirmation_reason"]
    assert reason.startswith("The desktopmode service is absent on this firmware")
    assert "logical display 6 carries FLAG_WIRELESS_DEX_DISPLAY and FLAG_EXTERNAL_DEX_HOSTING" in reason
    assert "'dumpsys display'" in reason
    assert "one Samsung phone on Android 17" in reason
    assert "one Samsung tablet on Android 16" in reason
    assert "wired DeX, standalone DeX and a screen capture of the flagged display were not observed" in reason


def test_the_receiving_pc_name_is_not_copied_into_the_dex_evidence():
    report = inspect_report(absent_fake())

    assert "TEST-RECEIVER" not in json.dumps(report["dex"])
    assert json.dumps(report, sort_keys=True)  # fully serializable


def test_list_displays_confirms_the_same_display_and_names_the_source():
    result = listing(absent_fake())

    assert result["status"] == "observed"
    assert [item["id"] for item in result["displays"]] == [0, 6]
    assert result["displays"][1]["flags"] == sorted(
        ["FLAG_TRUSTED", EXTERNAL_HOSTING, WIRELESS_DEX, "FLAG_WIFI_DISPLAY", "FLAG_ALLOWS_CONTENT_MODE_SWITCH"]
    )
    assert result["confirmed_dex_display_id"] == 6
    assert result["confirmed_dex_evidence_source"] == "dumpsys_display_flags"
    assert "[confirmed active DeX, from display flags]" in dex._render_displays(result)


def test_service_based_listing_has_no_evidence_source_key():
    result = listing(FakeRunner())

    assert result["confirmed_dex_display_id"] == 17
    assert "confirmed_dex_evidence_source" not in result
    assert "[confirmed active DeX]" in dex._render_displays(result)


# --- dual mirroring on this layout ------------------------------------------------


def test_plan_dual_mirror_accepts_the_flag_confirmed_display_like_a_service_confirmed_one():
    # Dual mirror = the phone screen (display 0) plus the display carrying the DeX
    # desktop.  On this layout that is display 0 and the flagged external display.
    result = plan(absent_fake(), 6)

    assert result["runtime_dex_confirmed"] is True
    assert result["target_classification"] == "confirmed_active_samsung_dex_display_flag"
    assert result["workflow_verification"] == "eligible"
    assert result["selected_display"]["id"] == 6
    assert result["instances"][0]["argv"] == [
        "scrcpy", "-s", "phone", "--display-id=0", "--no-audio",
        "--window-title", "ADAPT Phone / display 0",
    ]
    assert result["instances"][1]["argv"] == [
        "scrcpy", "-s", "phone", "--display-id=6", "--no-audio",
        "--window-title", "ADAPT DeX / display 6",
    ]
    flattened = [arg for instance in result["instances"] for arg in instance["argv"]]
    assert not any(arg.startswith("--new-display") for arg in flattened)
    assert not any(arg.startswith("--port") for arg in flattened)
    notes = " ".join(result["notes"])
    assert "desktopmode service is absent" in notes
    assert "one Samsung phone on Android 17" in notes
    assert "scrcpy capturing that display on that firmware was not observed" in notes
    assert "No virtual display is created and this plan does not launch a process." in result["notes"]


def test_plan_view_only_adds_no_control_to_both_clients():
    result = dex._build_plan(
        inspect_report(absent_fake()), 6, scrcpy_executable="scrcpy", view_only=True
    )

    assert all("--no-control" in instance["argv"] for instance in result["instances"])


@pytest.mark.parametrize(
    ("display_id", "kind"),
    [(0, "primary_duplicate"), (9, "missing_display")],
)
def test_plan_still_refuses_the_primary_and_missing_displays(display_id: int, kind: str):
    with pytest.raises(dex.DexError) as caught:
        plan(absent_fake(), display_id)

    assert caught.value.kind == kind


def test_only_the_flagged_display_is_the_confirmed_one():
    fake = absent_fake(dump(BUILT_IN, RECEIVER, PLAIN_EXTERNAL))

    assert plan(fake, 6)["runtime_dex_confirmed"] is True
    other = plan(fake, 7)
    assert other["runtime_dex_confirmed"] is False
    assert other["target_classification"] == "external_display_candidate_unconfirmed"
    assert refused_mirror(fake, 7).kind == "dex_unconfirmed"


def test_mirror_starts_phone_and_flagged_display_clients_and_claims_no_health():
    processes = [FakeProcess(401), FakeProcess(402)]
    launches: list[list[str]] = []

    def popen(argv, **kwargs):
        launches.append(argv)
        return processes[len(launches) - 1]

    result = mirror(absent_fake(), 6, popen)

    assert result["status"] == "processes_started"
    assert result["runtime_dex_confirmed"] is True
    assert result["target_classification"] == "confirmed_active_samsung_dex_display_flag"
    assert result["process_ids"] == [401, 402]
    assert [argv[3] for argv in launches] == ["--display-id=0", "--display-id=6"]
    assert result["healthy_streaming"] == "not_verified"


def test_mirror_keeps_the_researched_api_gate_for_flag_confirmed_displays():
    # The flag source widens evidence, not the platform range: API 38 is unverified.
    error = refused_mirror(absent_fake(sdk="38"), 6)

    assert error.kind == "unverified_platform"


# --- service absent, nothing confirmed ------------------------------------------


@pytest.mark.parametrize("display", [DISPLAYS_NO_FLAGS, DISPLAYS_IDLE_ONLY_BUILT_IN])
def test_absent_service_and_no_flagged_display_is_unknown_not_inactive(display: str):
    fake = absent_fake(display, sdk="36")  # an Android 16 style tablet
    report = inspect_report(fake)
    desktop = report["dex"]

    assert_not_confirmed(report)
    assert desktop["classification"] != "inactive"
    assert desktop["status"] == "unknown"
    assert desktop["recognized_shape"] is None
    assert desktop["desktop_display_id"] is None
    assert desktop["confirmation_reason"] == NO_FLAG_SENTENCE
    assert desktop["issues"][0] == "The desktopmode service was not available by this name."
    assert len(desktop["issues"]) == 2
    assert desktop["display_flag_evidence"]["outcome"] == "no_flagged_display"
    assert desktop["display_flag_evidence"]["flagged_displays"] == []
    assert listing(fake)["confirmed_dex_display_id"] is None
    assert "confirmed_dex_evidence_source" not in listing(fake)


def test_an_unflagged_external_display_stays_an_unconfirmed_candidate():
    fake = absent_fake(DISPLAYS_NO_FLAGS)

    result = plan(fake, 6)
    assert result["runtime_dex_confirmed"] is False
    assert result["target_classification"] == "external_display_candidate_unconfirmed"
    assert refused_mirror(fake, 6).kind == "dex_unconfirmed"


def test_absent_service_and_unreadable_display_list_evaluates_no_flags():
    fake = absent_fake("Display Devices: nothing recognizable")
    report = inspect_report(fake)

    assert_not_confirmed(report)
    assert report["dex"]["display_flag_evidence"]["outcome"] == "displays_unknown"
    assert report["dex"]["confirmation_reason"] == (
        "The desktopmode service is absent on this firmware and 'dumpsys display' could not be read, "
        "so no display flags were evaluated and no active DeX display was observed."
    )
    assert any("Display flags could not be read" in issue for issue in report["dex"]["issues"])


def test_two_wireless_dex_displays_are_unknown():
    report = inspect_report(absent_fake(dump(BUILT_IN, RECEIVER, SECOND_RECEIVER)))
    desktop = report["dex"]

    assert_not_confirmed(report)
    assert desktop["display_flag_evidence"]["outcome"] == "multiple_flagged_displays"
    assert "More than one logical display carries FLAG_WIRELESS_DEX_DISPLAY (displays 6, 7)." in desktop["issues"]
    assert "do not identify one DeX display" in desktop["confirmation_reason"]
    assert [entry["id"] for entry in desktop["display_flag_evidence"]["flagged_displays"]] == [6, 7]
    assert desktop["recognized_shape"] is None


def test_the_wireless_dex_flag_on_display_zero_is_unknown():
    on_primary = display_block(
        0, "Built-in Screen", f"FLAG_ALLOWED_TO_BE_DEFAULT_DISPLAY, {EXTERNAL_DEX_FLAGS}", size="real 1440 x 3120"
    )
    fake = absent_fake(dump(on_primary, display_block(6, "Plain External", "FLAG_PRESENTATION")))
    report = inspect_report(fake)

    assert_not_confirmed(report)
    assert report["dex"]["display_flag_evidence"]["outcome"] == "primary_display_flagged"
    assert "FLAG_WIRELESS_DEX_DISPLAY is on logical display 0, the built-in screen" in " ".join(
        report["dex"]["issues"]
    )
    assert listing(fake)["confirmed_dex_display_id"] is None


def test_external_dex_hosting_without_the_wireless_flag_is_unknown_and_unobserved():
    hosting_only = display_block(6, "Wired Maybe", f"FLAG_TRUSTED, {EXTERNAL_HOSTING}", group=2)
    fake = absent_fake(dump(BUILT_IN, hosting_only))
    report = inspect_report(fake)

    assert_not_confirmed(report)
    assert report["dex"]["display_flag_evidence"]["outcome"] == "external_hosting_only"
    issue = " ".join(report["dex"]["issues"])
    assert "FLAG_EXTERNAL_DEX_HOSTING is on logical display 6 without FLAG_WIRELESS_DEX_DISPLAY" in issue
    assert "possibly wired DeX" in issue
    assert "has not been observed" in issue
    assert plan(fake, 6)["runtime_dex_confirmed"] is False
    assert refused_mirror(fake, 6).kind == "dex_unconfirmed"


def test_the_wireless_flag_without_the_hosting_flag_is_stricter_than_the_flag_alone():
    # On the Android 13 phone the wireless sink was NOT the display the desktop was
    # drawn on, so the wireless flag alone does not locate the desktop.
    sink_only = display_block(6, "TEST-RECEIVER", f"FLAG_TRUSTED, {WIRELESS_DEX}, FLAG_WIFI_DISPLAY", group=2)
    report = inspect_report(absent_fake(dump(BUILT_IN, sink_only)))

    assert_not_confirmed(report)
    assert report["dex"]["display_flag_evidence"]["outcome"] == "wireless_flag_without_hosting"
    assert "without FLAG_EXTERNAL_DEX_HOSTING" in " ".join(report["dex"]["issues"])


def test_another_display_also_hosting_dex_makes_the_choice_ambiguous():
    other = display_block(7, "Wired Maybe", f"FLAG_TRUSTED, {EXTERNAL_HOSTING}", group=3)
    report = inspect_report(absent_fake(dump(BUILT_IN, RECEIVER, other)))

    assert_not_confirmed(report)
    assert report["dex"]["display_flag_evidence"]["outcome"] == "other_hosting_display"
    assert "Logical display 7 also carries FLAG_EXTERNAL_DEX_HOSTING" in " ".join(report["dex"]["issues"])


# --- the service result always wins -------------------------------------------------


@pytest.mark.parametrize(
    ("desktop", "display", "classification", "reason"),
    [
        (INACTIVE, DISPLAYS_WIRELESS_DEX, "inactive", SERVICE_RESULT_INACTIVE_SENTENCE),
        (IDLE_REAL, DISPLAYS_WIRELESS_DEX, "inactive", SERVICE_RESULT_INACTIVE_SENTENCE),
        ("unrecognized vendor format", DISPLAYS_WIRELESS_DEX, "unknown", SERVICE_UNKNOWN_SENTENCE),
        ("", DISPLAYS_WIRELESS_DEX, "unknown", SERVICE_UNKNOWN_SENTENCE),
        # The service says active on display 2, which is absent: unknown, and the
        # flagged display 6 is not used instead.
        (
            ACTIVE_REAL,
            DISPLAYS_WIRELESS_DEX,
            "unknown",
            "Desktop-mode display ID did not match an existing non-primary logical display.",
        ),
    ],
)
def test_an_answering_service_wins_and_the_display_flag_is_not_used(
    desktop: str, display: str, classification: str, reason: str
):
    fake = FakeRunner(sdk="37", desktop=desktop, display=display)
    report = inspect_report(fake)

    assert report["dex"]["classification"] == classification
    assert report["dex"]["confirmed"] is False
    assert report["dex"]["confirmed_display_id"] is None
    assert report["dex"]["confirmation_reason"] == reason
    assert "display_flag_evidence" not in report["dex"]
    assert report["dex"]["recognized_shape"] != SHAPE
    assert listing(fake)["confirmed_dex_display_id"] is None
    assert plan(fake, 6)["runtime_dex_confirmed"] is False
    assert refused_mirror(fake, 6).kind == "dex_unconfirmed"


def test_a_long_dump_that_merely_contains_the_not_found_phrase_is_not_an_absent_service():
    dump_text = "\n".join(
        ["DesktopModeService (dumpsys desktopmode):"]
        + [f"  log line {number}" for number in range(6)]
        + [f"  earlier: {ABSENT_MESSAGE}", "  mState=State{s1 user0 navBarGestureEnabled}"]
    )
    report = inspect_report(FakeRunner(sdk="37", desktop=dump_text, display=DISPLAYS_WIRELESS_DEX))

    assert_not_confirmed(report)
    assert "display_flag_evidence" not in report["dex"]
    assert report["dex"]["confirmation_reason"] == SERVICE_UNKNOWN_SENTENCE


@pytest.mark.parametrize(
    "desktop",
    [
        (1, "", "Permission Denial: dumpsys desktopmode"),
        (1, "", "error: closed"),
        (0, "", ""),
    ],
)
def test_a_denied_or_failed_service_probe_never_uses_the_display_flag(desktop):
    report = inspect_report(
        ScriptedRunner(sdk=(0, "37\n", ""), desktop=desktop, display=(0, DISPLAYS_WIRELESS_DEX, ""))
    )

    assert_not_confirmed(report)
    assert "display_flag_evidence" not in report["dex"]
    assert report["dex"]["confirmation_reason"] == SERVICE_UNKNOWN_SENTENCE


def test_android_13_fixtures_keep_their_existing_results():
    active = FakeRunner(sdk="33", desktop=ACTIVE_REAL, display=DISPLAYS_WITH_DESKTOP)
    report = inspect_report(active)
    assert report["dex"]["classification"] == "active_dual"
    assert report["dex"]["confirmed_display_id"] == 2  # the desktop display, not the wireless sink 10
    assert report["dex"]["recognized_shape"] == "observed_statemanager_semdesktopmodestate"
    assert "display_flag_evidence" not in report["dex"]
    assert listing(active)["confirmed_dex_display_id"] == 2
    assert plan(active, 2)["target_classification"] == "confirmed_active_samsung_dex"
    assert plan(active, 10)["runtime_dex_confirmed"] is False

    idle = FakeRunner(sdk="33", desktop=IDLE_REAL, display=DISPLAYS_IDLE)
    idle_report = inspect_report(idle)
    assert idle_report["dex"]["classification"] == "inactive"
    assert idle_report["dex"]["confirmation_reason"] == SERVICE_RESULT_INACTIVE_SENTENCE
    assert "display_flag_evidence" not in idle_report["dex"]


# --- manufacturer and reachability gates --------------------------------------------


def test_a_non_samsung_manufacturer_never_uses_the_display_flag():
    fake = absent_fake(manufacturer="Google", model="Pixel")
    report = inspect_report(fake)

    assert_not_confirmed(report)
    assert report["dex"]["confirmation_reason"] == NON_SAMSUNG_SENTENCE
    assert "display_flag_evidence" not in report["dex"]
    assert report["dex"]["recognized_shape"] is None
    assert listing(fake)["confirmed_dex_display_id"] is None
    assert plan(fake, 6)["runtime_dex_confirmed"] is False
    assert refused_mirror(fake, 6).kind == "dex_unconfirmed"


@pytest.mark.parametrize("manufacturer", [ADB_FAIL, EMPTY, DENIED])
def test_an_unread_manufacturer_never_uses_the_display_flag(manufacturer):
    runner = ScriptedRunner(
        manufacturer=manufacturer,
        sdk=(0, "37\n", ""),
        desktop=ABSENT,
        display=(0, DISPLAYS_WIRELESS_DEX, ""),
    )
    report = inspect_report(runner)

    assert report["device"]["manufacturer"] is None
    assert_not_confirmed(report)
    assert report["dex"]["confirmation_reason"] == UNREAD_MANUFACTURER_SENTENCE
    assert "display_flag_evidence" not in report["dex"]
    assert report["dex"]["recognized_shape"] is None


def test_an_unreachable_device_is_still_device_unreachable_with_the_flagged_dump():
    runner = ScriptedRunner(**ALL_IDENTITY_FAIL, desktop=ABSENT, display=(0, DISPLAYS_WIRELESS_DEX, ""))

    with pytest.raises(dex.DexError) as caught:
        inspect_report(runner)

    assert caught.value.kind == "device_unreachable"
    assert runner.dumpsys_calls() == []


# --- the CLI surfaces ---------------------------------------------------------------


def test_cli_inspect_prints_the_state_and_the_evidence_limits(offline_cli):
    cli = offline_cli(absent_fake())

    result = cli.invoke(dex.dex_app, ["inspect", "-s", "phone"])

    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert lines[2] == f"DeX state: {CLASSIFICATION}"
    assert "confirmed from display flags" in result.output
    assert "one Samsung phone on Android 17" in result.output
    assert "were not observed" in result.output


def test_cli_inspect_json_carries_the_evidence_block(offline_cli):
    cli = offline_cli(absent_fake())

    result = cli.invoke(dex.dex_app, ["inspect", "-s", "phone", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["dex"]["recognized_shape"] == SHAPE
    assert payload["dex"]["confirmed_display_id"] == 6
    assert payload["dex"]["display_flag_evidence"]["not_observed"]


def test_cli_list_displays_marks_the_flag_confirmed_display(offline_cli):
    cli = offline_cli(absent_fake())

    result = cli.invoke(dex.dex_app, ["list-displays", "-s", "phone"])

    assert result.exit_code == 0
    assert "[confirmed active DeX, from display flags]" in result.output
