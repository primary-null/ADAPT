"""Offline tests for the ``dumpsys desktopmode`` shape observed on real firmware.

The fixtures below reproduce the *structure* of output observed on one Samsung
phone (Android 13 / API 33): once during a real wireless DeX session in dual
mode and once with DeX idle.  They are abridged reconstructions, not verbatim
captures.  Standalone and wired DeX were never captured, so there is no fixture
that certifies them, and other One UI versions may print something else.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from adapt_adb import dex
from tests.test_dex import (
    ACTIVE_DUAL,
    ACTIVE_STANDALONE,
    INACTIVE,
    FakeProcess,
    FakeRunner,
    inspect,
    no_op_preflight,
)
from tests.test_dex_unreachable import (
    ADB_FAIL,
    ALL_IDENTITY_FAIL,
    NON_SAMSUNG_SENTENCE,
    UNREAD_MANUFACTURER_SENTENCE,
    ScriptedRunner,
)

ADB = Path("C:/adapt/adb.exe")
OBSERVED = "observed_statemanager_semdesktopmodestate"

# Timestamped history that precedes the state sections on real firmware.  It
# contains ``enabled=``, ``State=State{`` and flag names inside log messages,
# and some lines contradict the current state on purpose: none of it may be
# read as state.
HISTORY = """\
11-13 01:16:12.333  1000  1494  2546 D [DMS]State: (enter, successful) State=State{s25 user0 DisplayInfo(id=10, WIRELESS_DEX, (1920x1080), ROTATION_0) navBarGestureEnabled}
11-13 01:16:12.101  1000  1494  2546 D [DMS]State: (exit, successful) State=State{s24 user0 SemDesktopModeState(DISABLED, STATE_UNDEFINED, DISPLAY_TYPE_STANDALONE) navBarGestureEnabled}
11-13 01:16:11.870  1000  1494  2546 D [DMS]StandaloneModeChanger: setStandaloneModeEnabled mStandaloneModeEnabled=true enabled=1
11-13 01:16:11.842  1000  1494  2546 D [DMS]DualModeChanger: apply mDualModeEnabled=false mDesktopDisplayId=-1 enabled=0
11-06 03:37:32.540  1000  1494  2546 D [DMS]DesktopModeStateManager: mCurrentState=DesktopModeState{enabled=1, disabled=0}
11-06 03:37:32.506  1000  1494  2546 D [DMS]DisplayPortStateManager: setHighResolutionsForExternalEnabledLocked(), enabled=false
11-06 03:37:32.411  1000  1494  2546 D [DMS]State: (enter, successful) State=State{s12 user0 SemDesktopModeState(ENABLED, STATE_UNDEFINED, DISPLAY_TYPE_DUAL) desktopDisplay.7 touchpadAvailable}
"""

ACTIVE_SECTIONS = """\
  Current StandaloneModeChanger state:
    mStandaloneModeEnabled=false
  Current DualModeChanger state:
    mDesktopDisplayId=2
    mDualModeEnabled=true
  Current StateManager state:
    mState=State{s34 user0 DisplayInfo(id=10, WIRELESS_DEX, (1920x1080), ROTATION_0) SemDesktopModeState(ENABLED, STATE_UNDEFINED, DISPLAY_TYPE_DUAL) desktopDisplay.2 touchpadAvailable navBarGestureEnabled}
  Current HardwareManager state:
    mConnectedDisplay=DisplayInfo(id=10, WIRELESS_DEX, (1920x1080), ROTATION_0)
    mDisplays={0=DisplayInfo(id=0, INTERNAL, (1768x2208), ROTATION_0), 2=DisplayInfo(id=2, VIRTUAL, (1920x1080), ROTATION_0), 10=DisplayInfo(id=10, WIRELESS_DEX, (1920x1080), ROTATION_0)}
  Current DisplayPortStateManager state:
    mDualModeEnabled=true
    mStandaloneModeEnabled=false
  Current WirelessDexManager state:
"""

IDLE_SECTIONS = """\
  Current StandaloneModeChanger state:
    mStandaloneModeEnabled=false
  Current DualModeChanger state:
    mDesktopDisplayId=-1
    mDualModeEnabled=false
  Current StateManager state:
    mState=State{s64 user0 navBarGestureEnabled}
  Current HardwareManager state:
    mConnectedDisplay=null
    mDisplays={0=DisplayInfo(id=0, INTERNAL, (1768x2208), ROTATION_0), 2=DisplayInfo(id=2, VIRTUAL, (1920x1080), ROTATION_0)}
  Current DisplayPortStateManager state:
    mDualModeEnabled=false
    mStandaloneModeEnabled=false
  Current WirelessDexManager state:
"""

HEADER = "DesktopModeService (dumpsys desktopmode):\n"
ACTIVE_STATE_LINE = (
    "mState=State{s34 user0 DisplayInfo(id=10, WIRELESS_DEX, (1920x1080), ROTATION_0) "
    "SemDesktopModeState(ENABLED, STATE_UNDEFINED, DISPLAY_TYPE_DUAL) desktopDisplay.2 "
    "touchpadAvailable navBarGestureEnabled}"
)
ACTIVE_REAL = HEADER + HISTORY + ACTIVE_SECTIONS
IDLE_REAL = HEADER + HISTORY + IDLE_SECTIONS


def display_block(display_id: int, name: str, flags: str, size: str = "real 1920 x 1080") -> str:
    return (
        f"  Display {display_id}:\n"
        f"    mDisplayId={display_id}\n"
        f'    mOverrideDisplayInfo=DisplayInfo{{"{name}", displayId {display_id}, {flags}, {size}, '
        "density 160 (160.0 x 160.0) dpi}\n"
    )


def displays(*blocks: str) -> str:
    return f"Display Devices: size=1\nLogical Displays: size={len(blocks)}\n" + "".join(blocks)


BUILT_IN = display_block(
    0, "Built-in Screen", "FLAG_ALLOWED_TO_BE_DEFAULT_DISPLAY", "real 1768 x 2208"
)
DESKTOP = display_block(2, "Desktop", "FLAG_PRESENTATION")
WIRELESS = display_block(10, "Wireless Display", "FLAG_WIRELESS_DEX_DISPLAY")
OTHER = display_block(3, "Other", "FLAG_PRESENTATION")

DISPLAYS_WITH_DESKTOP = displays(BUILT_IN, DESKTOP, WIRELESS)
DISPLAYS_WITHOUT_DESKTOP = displays(BUILT_IN, WIRELESS)
DISPLAYS_IDLE = displays(BUILT_IN, DESKTOP)


def swap(text: str, old: str, new: str) -> str:
    """Replace exactly one occurrence, so a fixture edit can never silently miss."""
    assert text.count(old) == 1, f"fixture anchor is not unique: {old!r}"
    return text.replace(old, new)


def edited(sections: str, *pairs: tuple[str, str]) -> str:
    """A full dump whose sections were edited; the history is never touched."""
    for old, new in pairs:
        sections = swap(sections, old, new)
    return HEADER + HISTORY + sections


def active(old: str, new: str) -> str:
    return edited(ACTIVE_SECTIONS, (old, new))


def idle(old: str, new: str) -> str:
    return edited(IDLE_SECTIONS, (old, new))


def run_inspect(desktop: str, display: str = DISPLAYS_WITH_DESKTOP, **kwargs) -> dict:
    return inspect(FakeRunner(desktop=desktop, display=display, **kwargs))


def assert_unknown(parsed: dict, *fragments: str) -> None:
    assert parsed["status"] == "unknown"
    assert parsed["classification"] == "unknown"
    assert parsed["desktop_display_id"] is None
    assert parsed["recognized_shape"] is None
    text = " | ".join(parsed["issues"])
    assert parsed["issues"]
    for fragment in fragments:
        assert fragment in text, f"{fragment!r} not in {text!r}"


# --- the observed active dual shape ---------------------------------------------


def test_active_dual_shape_is_dual_evidence_and_history_is_ignored():
    parsed = dex.parse_desktop_mode(ACTIVE_REAL)

    assert parsed["status"] == "observed"
    assert parsed["classification"] == "active_dual_evidence"
    assert parsed["desktop_display_id"] == 2  # not the 7 in a history line
    assert parsed["issues"] == []
    assert parsed["recognized_shape"] == OBSERVED == dex.RECOGNIZED_SHAPE_OBSERVED
    assert parsed["recognized_fields"] == {
        "dual_mode": True,
        "standalone_mode": False,
        "desktop_display_id": 2,
        "sem_desktop_mode_state": "ENABLED",
        "sem_display_type": "DISPLAY_TYPE_DUAL",
    }


def test_repeated_flags_with_identical_values_are_not_a_conflict():
    parsed = dex.parse_desktop_mode(ACTIVE_REAL)

    assert ACTIVE_REAL.count("mDualModeEnabled=true") == 2
    assert ACTIVE_REAL.count("mStandaloneModeEnabled=false") == 2
    assert parsed["classification"] == "active_dual_evidence"
    assert not any("duplicated" in issue or "conflict" in issue for issue in parsed["issues"])


def test_inspect_confirms_active_dual_on_display_2():
    report = run_inspect(ACTIVE_REAL)

    assert [item["id"] for item in report["displays"]["items"]] == [0, 2, 10]
    assert report["dex"]["classification"] == "active_dual"
    assert report["dex"]["confirmed"] is True
    assert report["dex"]["confirmed_display_id"] == 2
    assert report["dex"]["desktop_display_id"] == 2
    assert report["dex"]["recognized_shape"] == OBSERVED
    assert report["dex"]["confirmation_reason"] == (
        "Recognized active dual-mode state matches an existing non-primary logical display."
    )
    assert report["mutations"] == []


def test_list_displays_plan_and_mirror_proceed_past_confirmation():
    fake = FakeRunner(desktop=ACTIVE_REAL, display=DISPLAYS_WITH_DESKTOP)
    listing = dex.list_displays("phone", adb_path=ADB, runner=fake, preflight=no_op_preflight)
    assert listing["status"] == "observed"
    assert listing["confirmed_dex_display_id"] == 2
    assert [item["id"] for item in listing["displays"]] == [0, 2, 10]
    assert listing["displays"][1]["name"] == "Desktop"
    assert listing["displays"][1]["resolution"] == {"width": 1920, "height": 1080}

    plan = dex.plan_dual_mirror("phone", 2, adb_path=ADB, runner=fake, preflight=no_op_preflight)
    assert plan["target_classification"] == "confirmed_active_samsung_dex"
    assert plan["runtime_dex_confirmed"] is True
    assert plan["instances"][0]["argv"] == [
        "scrcpy", "-s", "phone", "--display-id=0", "--no-audio",
        "--window-title", "ADAPT Phone / display 0",
    ]
    assert plan["instances"][1]["argv"] == [
        "scrcpy", "-s", "phone", "--display-id=2", "--no-audio",
        "--window-title", "ADAPT DeX / display 2",
    ]

    processes = [FakeProcess(301), FakeProcess(302)]
    launches: list[list[str]] = []

    def popen(argv, **kwargs):
        launches.append(argv)
        return processes[len(launches) - 1]

    result = dex.mirror_dual(
        "phone",
        2,
        yes=True,
        adb_path=ADB,
        runner=fake,
        preflight=no_op_preflight,
        scrcpy_resolver=lambda _: Path("C:/tools/scrcpy.exe"),
        popen_factory=popen,
    )
    assert result["status"] == "processes_started"
    assert result["process_ids"] == [301, 302]
    assert [argv[3] for argv in launches] == ["--display-id=0", "--display-id=2"]


def test_a_different_existing_display_is_still_only_an_unconfirmed_candidate():
    fake = FakeRunner(desktop=ACTIVE_REAL, display=DISPLAYS_WITH_DESKTOP)

    plan = dex.plan_dual_mirror("phone", 10, adb_path=ADB, runner=fake, preflight=no_op_preflight)
    assert plan["target_classification"] == "external_display_candidate_unconfirmed"
    assert plan["runtime_dex_confirmed"] is False

    launched: list = []
    with pytest.raises(dex.DexError) as caught:
        dex.mirror_dual(
            "phone",
            10,
            yes=True,
            adb_path=ADB,
            runner=fake,
            preflight=no_op_preflight,
            scrcpy_resolver=lambda _: Path("C:/tools/scrcpy.exe"),
            popen_factory=lambda *args, **kwargs: launched.append(args),
        )
    assert caught.value.kind == "dex_unconfirmed"
    assert launched == []


def test_active_evidence_without_the_display_in_the_list_is_not_confirmed():
    parsed = dex.parse_desktop_mode(ACTIVE_REAL)
    assert parsed["classification"] == "active_dual_evidence"  # evidence alone is not confirmation

    fake = FakeRunner(desktop=ACTIVE_REAL, display=DISPLAYS_WITHOUT_DESKTOP)
    report = inspect(fake)
    assert report["dex"]["classification"] == "unknown"
    assert report["dex"]["confirmed"] is False
    assert report["dex"]["confirmed_display_id"] is None
    assert report["dex"]["confirmation_reason"] == (
        "Desktop-mode display ID did not match an existing non-primary logical display."
    )

    listing = dex.list_displays("phone", adb_path=ADB, runner=fake, preflight=no_op_preflight)
    assert listing["confirmed_dex_display_id"] is None
    with pytest.raises(dex.DexError) as caught:
        dex.mirror_dual(
            "phone",
            10,
            yes=True,
            adb_path=ADB,
            runner=fake,
            preflight=no_op_preflight,
            scrcpy_resolver=lambda _: Path("C:/tools/scrcpy.exe"),
            popen_factory=lambda *args, **kwargs: pytest.fail("launched"),
        )
    assert caught.value.kind == "dex_unconfirmed"


def test_unparseable_display_list_leaves_active_evidence_unconfirmed():
    report = run_inspect(ACTIVE_REAL, display="Display Devices: nothing recognizable")

    assert report["displays"]["status"] == "unknown"
    assert report["dex"]["classification"] == "unknown"
    assert report["dex"]["confirmed"] is False


# --- the observed idle shape ---------------------------------------------------


def test_idle_shape_is_inactive_and_history_is_ignored():
    parsed = dex.parse_desktop_mode(IDLE_REAL)

    assert parsed["status"] == "observed"
    assert parsed["classification"] == "inactive"
    assert parsed["desktop_display_id"] == -1
    assert parsed["recognized_shape"] == OBSERVED
    assert parsed["recognized_fields"]["sem_desktop_mode_state"] is None
    assert parsed["recognized_fields"]["dual_mode"] is False
    assert parsed["recognized_fields"]["standalone_mode"] is False
    assert parsed["issues"] == []


def test_inspect_reports_idle_as_inactive_and_confirms_nothing():
    fake = FakeRunner(desktop=IDLE_REAL, display=DISPLAYS_IDLE)
    report = inspect(fake)

    assert report["dex"]["classification"] == "inactive"
    assert report["dex"]["confirmed"] is False
    assert report["dex"]["confirmed_display_id"] is None
    assert report["dex"]["recognized_shape"] == OBSERVED
    assert report["dex"]["confirmation_reason"] == "Recognized desktop-mode fields report DeX inactive."
    listing = dex.list_displays("phone", adb_path=ADB, runner=fake, preflight=no_op_preflight)
    assert listing["confirmed_dex_display_id"] is None


# --- anything else stays unknown, with a specific issue -------------------------


@pytest.mark.parametrize(
    ("old", "new", "fragment"),
    [
        (
            "mDualModeEnabled=true\n    mStandaloneModeEnabled=false",
            "mDualModeEnabled=false\n    mStandaloneModeEnabled=false",
            "mDualModeEnabled conflicted between state sections "
            "(DualModeChanger=true, DisplayPortStateManager=false)",
        ),
        (
            "mDualModeEnabled=true\n    mStandaloneModeEnabled=false",
            "mDualModeEnabled=true\n    mStandaloneModeEnabled=true",
            "mStandaloneModeEnabled conflicted between state sections "
            "(StandaloneModeChanger=false, DisplayPortStateManager=true)",
        ),
        (
            "mDualModeEnabled=true\n    mStandaloneModeEnabled=false",
            "mDualModeEnabled=true\n    mStandaloneModeEnabled=false\n    mDesktopDisplayId=3",
            "mDesktopDisplayId conflicted between state sections "
            "(DualModeChanger=2, DisplayPortStateManager=3)",
        ),
        (
            "mDualModeEnabled=true\n    mStandaloneModeEnabled=false",
            "mDualModeEnabled=maybe\n    mStandaloneModeEnabled=false",
            "mDualModeEnabled had an unrecognized value in DisplayPortStateManager",
        ),
        (
            "  Current WirelessDexManager state:\n",
            "  Current WirelessDexManager state:\n    mDualModeEnabled=false\n",
            "mDualModeEnabled conflicted between state sections "
            "(DualModeChanger=true, DisplayPortStateManager=true, WirelessDexManager=false)",
        ),
    ],
)
def test_disagreeing_or_malformed_flags_are_unknown(old: str, new: str, fragment: str):
    desktop = active(old, new)
    parsed = dex.parse_desktop_mode(desktop)

    assert_unknown(parsed, fragment)
    report = run_inspect(desktop)
    assert report["dex"]["classification"] == "unknown"
    assert report["dex"]["confirmed"] is False
    assert report["dex"]["confirmed_display_id"] is None


def test_conflicting_state_lines_in_the_state_manager_are_unknown():
    desktop = active(
        "navBarGestureEnabled}\n  Current HardwareManager state:",
        "navBarGestureEnabled}\n    mState=State{s35 user0}\n  Current HardwareManager state:",
    )
    assert_unknown(
        dex.parse_desktop_mode(desktop),
        "StateManager mState was reported more than once with different values",
    )


def test_desktop_display_token_that_does_not_match_the_display_id_is_unknown():
    desktop = active("desktopDisplay.2 ", "desktopDisplay.3 ")
    assert_unknown(
        dex.parse_desktop_mode(desktop),
        "desktopDisplay.3 in StateManager mState does not match mDesktopDisplayId=2",
    )

    # Even with both displays present nothing is confirmed.
    report = run_inspect(desktop, display=displays(BUILT_IN, DESKTOP, OTHER))
    assert report["dex"]["classification"] == "unknown"
    assert report["dex"]["confirmed"] is False
    assert report["dex"]["confirmed_display_id"] is None


def test_desktop_display_token_missing_or_repeated_with_a_different_id_is_unknown():
    assert_unknown(
        dex.parse_desktop_mode(active("desktopDisplay.2 ", "")),
        "did not name exactly one desktopDisplay.N token",
    )
    assert_unknown(
        dex.parse_desktop_mode(active("desktopDisplay.2 ", "desktopDisplay.2 desktopDisplay.4 ")),
        "did not name exactly one desktopDisplay.N token",
    )


def test_primary_display_zero_is_never_a_dual_desktop():
    desktop = edited(
        ACTIVE_SECTIONS,
        ("desktopDisplay.2 ", "desktopDisplay.0 "),
        ("mDesktopDisplayId=2", "mDesktopDisplayId=0"),
    )
    assert_unknown(
        dex.parse_desktop_mode(desktop),
        "mState names desktop display 0, which is not a positive non-primary display ID",
    )


@pytest.mark.parametrize(
    ("old", "new", "fragment"),
    [
        (
            "DISPLAY_TYPE_DUAL",
            "DISPLAY_TYPE_STANDALONE",
            "SemDesktopModeState display type DISPLAY_TYPE_STANDALONE is not DISPLAY_TYPE_DUAL",
        ),
        (
            "DISPLAY_TYPE_DUAL",
            "DISPLAY_TYPE_WIRED",
            "SemDesktopModeState display type DISPLAY_TYPE_WIRED is not DISPLAY_TYPE_DUAL",
        ),
        (
            "(ENABLED, STATE_UNDEFINED, DISPLAY_TYPE_DUAL)",
            "(ENABLED, STATE_UNDEFINED)",
            "SemDesktopModeState did not name exactly one DISPLAY_TYPE_ value",
        ),
        (
            "(ENABLED,",
            "(ENABLING,",
            "SemDesktopModeState reported ENABLING, not ENABLED",
        ),
        (
            "(ENABLED,",
            "(DISABLED,",
            "SemDesktopModeState reported DISABLED, not ENABLED",
        ),
        (
            "DISPLAY_TYPE_DUAL)",
            "DISPLAY_TYPE_DUAL) SemDesktopModeState(ENABLED, X, DISPLAY_TYPE_DUAL)",
            "did not contain exactly one well-formed SemDesktopModeState(...)",
        ),
        (
            "SemDesktopModeState(ENABLED, STATE_UNDEFINED, DISPLAY_TYPE_DUAL)",
            "SemDesktopModeState(ENABLED, STATE_UNDEFINED, DISPLAY_TYPE_DUAL",
            "did not contain exactly one well-formed SemDesktopModeState(...)",
        ),
    ],
)
def test_enabled_state_that_is_not_the_recognized_dual_shape_is_unknown(
    old: str, new: str, fragment: str
):
    desktop = active(old, new)

    assert_unknown(dex.parse_desktop_mode(desktop), fragment)
    report = run_inspect(desktop)
    assert report["dex"]["classification"] == "unknown"
    assert report["dex"]["confirmed"] is False


def test_device_text_is_not_echoed_into_issues_unfiltered():
    desktop = active("DISPLAY_TYPE_DUAL)", "DISPLAY_TYPE_\x1b[31mX)")
    parsed = dex.parse_desktop_mode(desktop)

    assert_unknown(parsed, "SemDesktopModeState display type value is not DISPLAY_TYPE_DUAL")
    assert "\x1b" not in " ".join(parsed["issues"])


def test_standalone_looking_state_stays_unknown_even_though_the_synthetic_shape_has_one():
    desktop = edited(
        ACTIVE_SECTIONS,
        ("mStandaloneModeEnabled=false\n  Current DualModeChanger", "mStandaloneModeEnabled=true\n  Current DualModeChanger"),
        ("mDesktopDisplayId=2\n    mDualModeEnabled=true", "mDesktopDisplayId=0\n    mDualModeEnabled=false"),
        ("mDualModeEnabled=true\n    mStandaloneModeEnabled=false", "mDualModeEnabled=false\n    mStandaloneModeEnabled=true"),
        ("DISPLAY_TYPE_DUAL) desktopDisplay.2", "DISPLAY_TYPE_STANDALONE) desktopDisplay.0"),
    )

    assert_unknown(dex.parse_desktop_mode(desktop), "DISPLAY_TYPE_STANDALONE")
    report = run_inspect(desktop, display=displays(BUILT_IN))
    assert report["dex"]["classification"] == "unknown"
    assert report["dex"]["confirmed"] is False
    assert report["dex"]["confirmed_display_id"] is None


def test_active_state_with_consistent_but_wrong_flags_is_unknown():
    dual_false = edited(
        ACTIVE_SECTIONS,
        ("mDesktopDisplayId=2\n    mDualModeEnabled=true", "mDesktopDisplayId=2\n    mDualModeEnabled=false"),
        ("mDualModeEnabled=true\n    mStandaloneModeEnabled=false", "mDualModeEnabled=false\n    mStandaloneModeEnabled=false"),
    )
    assert_unknown(
        dex.parse_desktop_mode(dual_false),
        "mState reports an enabled dual-mode desktop but mDualModeEnabled is false",
    )

    standalone_true = edited(
        ACTIVE_SECTIONS,
        ("mStandaloneModeEnabled=false\n  Current DualModeChanger", "mStandaloneModeEnabled=true\n  Current DualModeChanger"),
        ("mDualModeEnabled=true\n    mStandaloneModeEnabled=false", "mDualModeEnabled=true\n    mStandaloneModeEnabled=true"),
    )
    assert_unknown(
        dex.parse_desktop_mode(standalone_true),
        "mState reports an enabled dual-mode desktop but mStandaloneModeEnabled is true",
    )


def test_idle_state_contradicted_by_consistent_flags_or_display_id_is_unknown():
    id_set = idle("mDesktopDisplayId=-1", "mDesktopDisplayId=2")
    assert_unknown(
        dex.parse_desktop_mode(id_set),
        "mState shows no desktop mode but mDesktopDisplayId is 2",
    )

    dual_all = edited(
        IDLE_SECTIONS,
        ("mDesktopDisplayId=-1\n    mDualModeEnabled=false", "mDesktopDisplayId=-1\n    mDualModeEnabled=true"),
        ("mDualModeEnabled=false\n    mStandaloneModeEnabled=false", "mDualModeEnabled=true\n    mStandaloneModeEnabled=false"),
    )
    assert_unknown(
        dex.parse_desktop_mode(dual_all),
        "mState shows no desktop mode but mDualModeEnabled is true",
    )

    one_flag = idle("mDualModeEnabled=false\n    mStandaloneModeEnabled=false", "mDualModeEnabled=true\n    mStandaloneModeEnabled=false")
    assert_unknown(dex.parse_desktop_mode(one_flag), "mDualModeEnabled conflicted between state sections")


@pytest.mark.parametrize(
    ("replacement", "fragment"),
    [
        ("mState=IDLE", "StateManager mState was not a recognized State{...} line"),
        (
            "mState=State{s64 user0 DisplayInfo(id=10, WIRELESS_DEX, (1920x1080), ROTATION_0)}",
            "described a display without SemDesktopModeState",
        ),
        (
            "mState=State{s64 user0 desktopDisplay.2}",
            "described a display without SemDesktopModeState",
        ),
        (
            "mState=State{s64 user0 SemDesktopModeState(DISABLED, STATE_UNDEFINED, DISPLAY_TYPE_NONE)}",
            "SemDesktopModeState reported DISABLED, not ENABLED",
        ),
        (
            "mState=State{s64 user0 SemDesktopModeState(ENABLED, STATE_UNDEFINED, DISPLAY_TYPE_DUAL) desktopDisplay.2",
            "StateManager mState was not a recognized State{...} line",
        ),
    ],
)
def test_unrecognized_state_manager_shapes_are_unknown(replacement: str, fragment: str):
    desktop = idle("mState=State{s64 user0 navBarGestureEnabled}", replacement)
    assert_unknown(dex.parse_desktop_mode(desktop), fragment)


def test_a_generic_mstate_outside_the_state_manager_section_is_not_read():
    # The StateManager section has no mState line; an active-looking one in
    # another section must not stand in for it.
    desktop = edited(
        ACTIVE_SECTIONS,
        ("    " + ACTIVE_STATE_LINE + "\n", ""),
        ("  Current HardwareManager state:\n", "  Current HardwareManager state:\n    " + ACTIVE_STATE_LINE + "\n"),
    )
    assert_unknown(dex.parse_desktop_mode(desktop), "StateManager mState line was missing")


def test_missing_fields_are_named():
    no_dual = edited(
        ACTIVE_SECTIONS,
        ("    mDualModeEnabled=true\n  Current StateManager", "  Current StateManager"),
        ("    mDualModeEnabled=true\n    mStandaloneModeEnabled=false", "    mStandaloneModeEnabled=false"),
    )
    assert_unknown(dex.parse_desktop_mode(no_dual), "mDualModeEnabled was missing")

    no_id = active("    mDesktopDisplayId=2\n", "")
    assert_unknown(dex.parse_desktop_mode(no_id), "mDesktopDisplayId was missing")


def test_a_flag_printed_in_only_one_section_is_still_evidence_when_it_agrees():
    # Deliberately looser than "both sections": other firmware may print each
    # flag once; one agreeing line, with every other condition, is enough.
    one_each = active(
        "  Current DisplayPortStateManager state:\n    mDualModeEnabled=true\n    mStandaloneModeEnabled=false\n",
        "  Current DisplayPortStateManager state:\n",
    )
    parsed = dex.parse_desktop_mode(one_each)
    assert parsed["classification"] == "active_dual_evidence"
    assert parsed["desktop_display_id"] == 2


# --- history and other text outside the state sections --------------------------


def test_history_lines_alone_are_unknown():
    parsed = dex.parse_desktop_mode(HEADER + HISTORY)

    assert parsed["classification"] == "unknown"
    assert parsed["status"] == "unknown"
    assert parsed["desktop_display_id"] is None
    assert parsed["recognized_shape"] is None
    assert parsed["issues"]
    report = run_inspect(HEADER + HISTORY)
    assert report["dex"]["classification"] == "unknown"
    assert report["dex"]["confirmed"] is False


def test_state_looking_lines_outside_state_sections_are_not_read():
    stray = "    mDualModeEnabled=true\n    mStandaloneModeEnabled=true\n    mDesktopDisplayId=7\n"
    parsed = dex.parse_desktop_mode(HEADER + HISTORY + stray + IDLE_SECTIONS)
    assert parsed["classification"] == "inactive"
    assert parsed["desktop_display_id"] == -1

    # With no section header at all there is nothing recognized to read.
    bare = dex.parse_desktop_mode("mDualModeEnabled=true\nmStandaloneModeEnabled=false\nmDesktopDisplayId=2\n")
    assert bare["classification"] == "unknown"


@pytest.mark.parametrize("newline", ["\r\n", "\r\r\n"])
def test_crlf_output_from_adb_is_recognized(newline: str):
    # Some adb shells return CRLF (or a doubled CR) line endings.
    for text, expected in [(ACTIVE_REAL, "active_dual_evidence"), (IDLE_REAL, "inactive")]:
        parsed = dex.parse_desktop_mode(text.replace("\n", newline))
        assert parsed["classification"] == expected
        assert parsed["recognized_shape"] == OBSERVED


def test_lines_after_a_section_at_header_indentation_or_shallower_are_not_state():
    trailing = ACTIVE_REAL + "mDualModeEnabled=false\n  mStandaloneModeEnabled=true\n  mDesktopDisplayId=9\n"
    parsed = dex.parse_desktop_mode(trailing)

    assert parsed["classification"] == "active_dual_evidence"
    assert parsed["desktop_display_id"] == 2


# --- non-Samsung, unreadable identity, unreachable ------------------------------


def test_non_samsung_identity_never_confirms_the_observed_active_shape():
    fake = FakeRunner(
        manufacturer="Google", model="Pixel", desktop=ACTIVE_REAL, display=DISPLAYS_WITH_DESKTOP
    )
    report = inspect(fake)

    assert report["dex"]["classification"] == "unknown"
    assert report["dex"]["confirmed"] is False
    assert report["dex"]["confirmed_display_id"] is None
    assert report["dex"]["confirmation_reason"] == NON_SAMSUNG_SENTENCE
    listing = dex.list_displays("phone", adb_path=ADB, runner=fake, preflight=no_op_preflight)
    assert listing["confirmed_dex_display_id"] is None
    with pytest.raises(dex.DexError) as caught:
        dex.mirror_dual(
            "phone",
            2,
            yes=True,
            adb_path=ADB,
            runner=fake,
            preflight=no_op_preflight,
            scrcpy_resolver=lambda _: Path("C:/tools/scrcpy.exe"),
            popen_factory=lambda *args, **kwargs: pytest.fail("launched"),
        )
    assert caught.value.kind == "dex_unconfirmed"


def test_unreadable_manufacturer_never_confirms_the_observed_active_shape():
    runner = ScriptedRunner(
        manufacturer=ADB_FAIL,
        desktop=(0, ACTIVE_REAL, ""),
        display=(0, DISPLAYS_WITH_DESKTOP, ""),
    )
    report = dex.inspect_device("phone", adb_path=ADB, runner=runner, preflight=no_op_preflight)

    assert report["dex"]["confirmation_reason"] == UNREAD_MANUFACTURER_SENTENCE
    assert report["dex"]["classification"] == "unknown"
    assert report["dex"]["confirmed"] is False
    assert report["dex"]["confirmed_display_id"] is None


def test_unreachable_device_still_raises_before_any_dumpsys_with_the_observed_dump():
    runner = ScriptedRunner(
        **ALL_IDENTITY_FAIL,
        desktop=(0, ACTIVE_REAL, ""),
        display=(0, DISPLAYS_WITH_DESKTOP, ""),
    )
    with pytest.raises(dex.DexError) as caught:
        dex.inspect_device("phone", adb_path=ADB, runner=runner, preflight=no_op_preflight)

    assert caught.value.kind == "device_unreachable"
    assert runner.dumpsys_calls() == []


def test_unobserved_desktop_service_reports_no_recognized_shape():
    report = inspect(FakeRunner(desktop="", desktop_returncode=1, desktop_stderr="Permission Denial: x"))

    assert report["dex"]["classification"] == "unknown"
    assert report["dex"]["recognized_shape"] is None


# --- the original synthetic shape is unchanged ----------------------------------


@pytest.mark.parametrize(
    ("desktop", "classification", "display_id", "enabled"),
    [
        (ACTIVE_DUAL, "active_dual_evidence", 17, 4),
        (ACTIVE_STANDALONE, "active_standalone_evidence", 0, 4),
        (INACTIVE, "inactive", -1, 1),
    ],
)
def test_original_synthetic_shape_still_parses_and_says_which_shape_matched(
    desktop: str, classification: str, display_id: int, enabled: int
):
    parsed = dex.parse_desktop_mode(desktop)

    assert parsed["status"] == "observed"
    assert parsed["classification"] == classification
    assert parsed["desktop_display_id"] == display_id
    assert parsed["recognized_shape"] == "synthetic_current_state" == dex.RECOGNIZED_SHAPE_SYNTHETIC
    assert parsed["recognized_fields"]["enabled"] == enabled
    assert parsed["issues"] == []


def test_original_synthetic_shape_still_confirms_through_inspect():
    report = inspect(FakeRunner())

    assert report["dex"]["classification"] == "active_dual"
    assert report["dex"]["confirmed_display_id"] == 17
    assert report["dex"]["recognized_shape"] == "synthetic_current_state"


@pytest.mark.parametrize(
    "desktop",
    [
        ACTIVE_DUAL + "  mDualModeEnabled=true\n",
        ACTIVE_DUAL.replace("mDesktopDisplayId=17", "mDesktopDisplayId=0"),
    ],
)
def test_original_shape_rejections_are_unchanged(desktop: str):
    parsed = dex.parse_desktop_mode(desktop)

    assert parsed["classification"] == "unknown"
    assert parsed["recognized_shape"] is None
    assert parsed["issues"]


def test_a_dump_mixing_both_shapes_is_unknown():
    mixed = HEADER + HISTORY + "  mCurrentState=DesktopModeState{enabled=4, disabled=0}\n" + ACTIVE_SECTIONS
    parsed = dex.parse_desktop_mode(mixed)

    assert parsed["classification"] == "unknown"
    assert parsed["recognized_shape"] is None


# --- empty and truncated dumps never raise --------------------------------------


@pytest.mark.parametrize("text", ["", "\n", "   \r\n  ", "DesktopModeService (dumpsys desktopmode):", HEADER])
def test_empty_dump_is_unknown_without_raising(text: str):
    parsed = dex.parse_desktop_mode(text)

    assert parsed["status"] == "unknown"
    assert parsed["classification"] == "unknown"
    assert parsed["issues"]
    assert parsed["recognized_shape"] is None


def test_truncated_state_sections_are_unknown_without_raising():
    cut = ACTIVE_REAL.index("  Current DualModeChanger state:")
    parsed = dex.parse_desktop_mode(ACTIVE_REAL[:cut])

    assert_unknown(parsed, "mDualModeEnabled was missing")


@pytest.mark.parametrize(
    ("full", "allowed", "required"),
    [
        (
            ACTIVE_REAL,
            {"unknown", "active_dual_evidence"},
            [ACTIVE_STATE_LINE, "mDualModeEnabled=true", "mStandaloneModeEnabled=false", "mDesktopDisplayId=2"],
        ),
        (
            IDLE_REAL,
            {"unknown", "inactive"},
            [
                "mState=State{s64 user0 navBarGestureEnabled}",
                "mDualModeEnabled=false",
                "mStandaloneModeEnabled=false",
                "mDesktopDisplayId=-1",
            ],
        ),
    ],
)
def test_every_truncation_point_is_safe(full: str, allowed: set[str], required: list[str]):
    # A prefix of the dump may be recognized only when it still holds every line
    # the evidence rule needs, complete; otherwise it must be unknown.
    for length in range(len(full) + 1):
        prefix = full[:length]
        parsed = dex.parse_desktop_mode(prefix)
        assert parsed["classification"] in allowed
        if parsed["classification"] != "unknown":
            for line in required:
                assert line in prefix, f"recognized a prefix of length {length} missing {line!r}"


# --- the same behavior through the real CLI commands ----------------------------


def test_cli_json_reports_confirmed_display_and_recognized_shape(monkeypatch):
    fake = FakeRunner(desktop=ACTIVE_REAL, display=DISPLAYS_WITH_DESKTOP)
    monkeypatch.setattr(dex, "resolve_adb", lambda: ADB)
    monkeypatch.setattr(dex, "preflight_adb_server", lambda adb, args: None)
    monkeypatch.setattr(dex, "_run_process", fake)
    monkeypatch.setattr(
        dex.subprocess, "run", lambda *a, **k: pytest.fail("real subprocess.run reached")
    )
    runner = CliRunner()

    listing = runner.invoke(dex.dex_app, ["list-displays", "-s", "phone", "--json"])
    assert listing.exit_code == 0
    assert json.loads(listing.stdout)["confirmed_dex_display_id"] == 2

    inspected = runner.invoke(dex.dex_app, ["inspect", "-s", "phone", "--json"])
    assert inspected.exit_code == 0
    payload = json.loads(inspected.stdout)
    assert payload["dex"]["classification"] == "active_dual"
    assert payload["dex"]["confirmed_display_id"] == 2
    assert payload["dex"]["recognized_shape"] == OBSERVED

    planned = runner.invoke(
        dex.dex_app, ["plan-dual-mirror", "-s", "phone", "--dex-display", "2", "--json"]
    )
    assert planned.exit_code == 0
    assert json.loads(planned.stdout)["runtime_dex_confirmed"] is True
    assert all(call[0] == str(ADB) for call in fake.calls)
