"""The ten new profiles, end to end against a fake shell: effects, gates, options and claim boundaries."""

from __future__ import annotations

import json

import pytest

from adapt_adb import dev_options, failure_guide
from adapt_adb import dev_profiles as prof
from adapt_adb import failure_report as fr
from adapt_adb.dev_options import DevOptionsError
from tests.fake_device import PKG, FakeDevice

SERIAL = "SERIAL1"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(dev_options, "preflight_adb_server", lambda *a, **k: None)
    monkeypatch.setattr(dev_options, "_LOCK_ROOT", tmp_path / "locks")


@pytest.fixture
def make(monkeypatch):
    def build(**kw) -> FakeDevice:
        device = FakeDevice(**kw)
        monkeypatch.setattr(dev_options, "_run_process", device)
        return device
    return build


@pytest.fixture
def dev(make) -> FakeDevice:
    return make()


def apply(profile, tmp_path, name="snap.json", **kw):
    return dev_options.apply_profile(profile, SERIAL, tmp_path / name, adb_path="adb", **kw)


def restore(tmp_path, name="snap.json"):
    return dev_options.restore_snapshot(SERIAL, tmp_path / name, adb_path="adb")


def saved(tmp_path, name="snap.json"):
    return json.loads((tmp_path / name).read_text(encoding="utf-8"))


def names(tmp_path):
    return [e["name"] for e in saved(tmp_path)["settings"]]


# --- 1 layout-debug -------------------------------------------------------------------------------


def test_layout_debug_sets_the_property_and_the_attribute_setting_then_restores(dev, tmp_path):
    report = apply("layout-debug", tmp_path)
    assert dev.props["debug.layout"] == "true" and dev.settings[("global", None, "debug_view_attributes")] == "1"
    assert report["needs_app_restart"] and report["needs_effect_check"]
    assert "restarts" in report["claim_boundary"] and "Effect check needed" in report["claim_boundary"]
    assert "Property changes take effect when the app restarts" in report["note"]
    result = restore(tmp_path)
    assert result["ok"] and dev.props["debug.layout"] == "" and ("global", None, "debug_view_attributes") not in dev.settings
    assert "restarts" in result["note"]


# --- 2 lifecycle-stress ---------------------------------------------------------------------------


def test_lifecycle_stress_writes_settings_only_and_never_kills_or_rotates(dev, tmp_path):
    report = apply("lifecycle-stress", tmp_path)
    assert dev.settings[("global", None, "always_finish_activities")] == "1"
    assert dev.settings[("secure", 10, "anr_show_background")] == "1"
    assert dev.settings[("secure", 10, "show_first_crash_dialog_dev_option")] == "1"
    flat = " ".join(" ".join(call) for call in dev.calls)
    assert "am kill" not in flat and "rotation" not in flat and "send-trim-memory" not in flat
    assert report["needs_effect_check"] and "recreated" in report["effect_check"] and not report["needs_app_restart"]
    assert restore(tmp_path)["ok"]
    assert ("global", None, "always_finish_activities") not in dev.settings


# --- 3 performance-capture ------------------------------------------------------------------------


def test_performance_capture_compiles_one_app_and_keeps_other_charging_bits(dev, tmp_path):
    dev.put("global", "stay_on_while_plugged_in", "1")
    report = apply("performance-capture", tmp_path, package=PKG)
    assert dev.dexopt[PKG] == "speed-profile" and dev.settings[("global", None, "stay_on_while_plugged_in")] == "3"
    assert ["shell", "cmd", "package", "compile", "-m", "speed-profile", "-f", PKG] in dev.calls
    assert report["restore_exactness"] == "approximate" and report["package"] == PKG
    assert "animation" in prof.PROFILE_BY_NAME["performance-capture"].notes
    result = restore(tmp_path)
    assert result["ok"] and dev.settings[("global", None, "stay_on_while_plugged_in")] == "1"


def test_performance_capture_needs_a_package(dev, tmp_path):
    with pytest.raises(DevOptionsError) as caught:
        apply("performance-capture", tmp_path)
    assert caught.value.kind == "package_required"


# --- 4 battery-doze -------------------------------------------------------------------------------


def test_battery_doze_with_a_package_also_moves_the_standby_bucket(dev, tmp_path):
    apply("battery-doze", tmp_path, package=PKG)
    assert dev.buckets[PKG] == 40 and dev.battery_stopped and dev.doze == "IDLE"
    assert names(tmp_path) == ["battery_state", "doze_state", "standby_bucket"]
    restore(tmp_path)
    assert dev.buckets[PKG] == 20 and not dev.battery_stopped and dev.doze == "ACTIVE"


def test_battery_doze_without_a_package_skips_the_per_app_step(dev, tmp_path):
    report = apply("battery-doze", tmp_path)
    assert names(tmp_path) == ["battery_state", "doze_state"]
    assert {"entry": "standby_bucket", "reason": "needs_package"} in report["skipped_steps"]
    assert report["restore_exactness"] == "approximate" and "Doze returns to normal scheduling" in report["claim_boundary"]


def test_battery_doze_refuses_an_exempted_standby_bucket(dev, tmp_path):
    dev.buckets[PKG] = 5
    with pytest.raises(DevOptionsError) as caught:
        apply("battery-doze", tmp_path, package=PKG)
    assert caught.value.kind == "unsupported_state"
    assert not dev.battery_stopped, "the unsupported state is found before any write"


# --- 5 rtl-large-font -----------------------------------------------------------------------------


def test_rtl_large_font_on_android_14_uses_the_larger_font_scale(make, tmp_path):
    dev = make(sdk="34")
    dev.put("system", "font_scale", "1.0", user=10)
    apply("rtl-large-font", tmp_path)
    assert dev.props["debug.force_rtl"] == "1" and dev.settings[("global", None, "debug.force_rtl")] == "1"
    assert dev.settings[("system", 10, "font_scale")] == "2.0" and dev.density_override == 483
    assert "app_locale" not in names(tmp_path)
    restore(tmp_path)
    assert dev.settings[("system", 10, "font_scale")] == "1.0" and dev.density_override is None and dev.props["debug.force_rtl"] == ""


def test_rtl_large_font_on_android_13_uses_1_3_and_sets_the_app_locale_with_a_package(make, tmp_path):
    dev = make(sdk="33")
    dev.locales[PKG] = ["fr-fr", "en-us"]
    apply("rtl-large-font", tmp_path, package=PKG)
    assert dev.settings[("system", 10, "font_scale")] == "1.3" and dev.locales[PKG] == ["ar"]
    restore(tmp_path)
    assert dev.locales[PKG] == ["fr-fr", "en-us"]


def test_rtl_large_font_on_android_12_has_no_app_locale_step(make, tmp_path):
    dev = make(sdk="32")
    report = apply("rtl-large-font", tmp_path, package=PKG)
    assert "app_locale" not in names(tmp_path) and {"entry": "app_locale", "reason": "needs_api_33"} in report["skipped_steps"]


def test_rtl_large_font_clears_an_app_locale_that_was_empty(make, tmp_path):
    dev = make(sdk="36")
    apply("rtl-large-font", tmp_path, package=PKG)
    assert dev.locales[PKG] == ["ar"]
    restore(tmp_path)
    assert dev.locales[PKG] == [] and ["shell", "cmd", "locale", "set-app-locales", PKG, "--user", "10"] in dev.calls


def test_rtl_large_font_density_with_an_unreadable_physical_value_is_refused(dev, tmp_path):
    dev.density_physical = 20
    with pytest.raises(DevOptionsError) as caught:
        apply("rtl-large-font", tmp_path)
    assert caught.value.kind == "unsupported_value" and dev.writes() == []


# --- 6 accessibility-display ----------------------------------------------------------------------


def test_accessibility_display_sets_night_mode_contrast_and_bold_text(dev, tmp_path):
    dev.night = "auto"
    apply("accessibility-display", tmp_path)
    assert dev.night == "yes" and dev.settings[("secure", 10, "high_text_contrast_enabled")] == "1"
    assert dev.settings[("secure", 10, "font_weight_adjustment")] == "300"
    assert "daltonizer_mode" not in names(tmp_path) and "debug.hwui.force_dark" not in names(tmp_path)
    restore(tmp_path)
    assert dev.night == "auto" and ("secure", 10, "high_text_contrast_enabled") not in dev.settings


def test_accessibility_display_bold_text_needs_android_12(make, tmp_path):
    dev = make(sdk="30")
    report = apply("accessibility-display", tmp_path)
    assert "bold_text" not in names(tmp_path) and {"entry": "bold_text", "reason": "needs_api_31"} in report["skipped_steps"]


def test_accessibility_display_options_add_colour_simulation_and_force_dark(dev, tmp_path):
    apply("accessibility-display", tmp_path, options=["daltonizer", "force-dark"])
    assert dev.settings[("secure", 10, "accessibility_display_daltonizer")] == "12"
    assert dev.settings[("secure", 10, "accessibility_display_daltonizer_enabled")] == "1"
    assert dev.props["debug.hwui.force_dark"] == "true"
    assert restore(tmp_path)["ok"] and dev.props["debug.hwui.force_dark"] == ""


# --- 7 input-visualization ------------------------------------------------------------------------


@pytest.mark.parametrize("sdk,keys", [("34", False), ("35", True), ("36", True)])
def test_input_visualization_shows_key_presses_only_from_android_15(make, tmp_path, sdk, keys):
    dev = make(sdk=sdk)
    apply("input-visualization", tmp_path)
    assert dev.settings[("system", 10, "show_touches")] == "1"
    assert (("system", 10, "show_key_presses") in dev.settings) is keys
    assert ("show_key_presses" in names(tmp_path)) is keys
    restore(tmp_path)
    assert ("system", 10, "show_key_presses") not in dev.settings


def test_input_visualization_pointer_location_is_an_option(dev, tmp_path):
    apply("input-visualization", tmp_path, options=["pointer-location"])
    assert dev.settings[("system", 10, "pointer_location")] == "1"


# --- 8 network-degraded ---------------------------------------------------------------------------


def test_network_degraded_limits_the_download_rate_and_restores_the_absent_key(dev, tmp_path):
    report = apply("network-degraded", tmp_path)
    assert dev.settings[("global", None, "ingress_rate_limit_bytes_per_second")] == "16000"
    assert "airplane_mode" not in names(tmp_path) and report["transport"] == "usb"
    restore(tmp_path)
    assert ("global", None, "ingress_rate_limit_bytes_per_second") not in dev.settings


def test_network_degraded_airplane_option_turns_it_on_and_off(dev, tmp_path):
    apply("network-degraded", tmp_path, options=["airplane-mode"])
    assert dev.settings[("global", None, "airplane_mode_on")] == "1"
    assert ["shell", "cmd", "connectivity", "airplane-mode", "enable"] in dev.calls
    restore(tmp_path)
    assert dev.settings[("global", None, "airplane_mode_on")] == "0"


def test_network_degraded_with_airplane_mode_already_on_does_not_touch_it(dev, tmp_path):
    dev.put("global", "airplane_mode_on", "1")
    report = apply("network-degraded", tmp_path, options=["airplane-mode"])
    assert "airplane_mode" in report["unchanged"]
    restore(tmp_path)
    assert dev.settings[("global", None, "airplane_mode_on")] == "1"


def test_network_degraded_before_android_13_has_only_the_airplane_option(make, tmp_path):
    dev = make(sdk="31")
    with pytest.raises(DevOptionsError) as caught:
        apply("network-degraded", tmp_path)
    assert caught.value.kind == "profile_api_range" and dev.writes() == []
    apply("network-degraded", tmp_path, options=["airplane-mode"])
    assert names(tmp_path) == ["airplane_mode"]


# --- 9 background-restricted ----------------------------------------------------------------------


def test_background_restricted_ignores_background_runs_and_turns_data_saver_on(dev, tmp_path):
    dev.appops[PKG] = "deny"
    report = apply("background-restricted", tmp_path, package=PKG)
    assert dev.appops[PKG] == "ignore" and dev.restrict_background
    assert report["restore_exactness"] == "approximate"
    restore(tmp_path)
    assert dev.appops[PKG] == "deny" and not dev.restrict_background


def test_background_restricted_leaves_data_saver_on_when_it_was_already_on(dev, tmp_path):
    dev.restrict_background = True
    report = apply("background-restricted", tmp_path, package=PKG)
    assert "data_saver" in report["unchanged"]
    restore(tmp_path)
    assert dev.restrict_background


# --- 10 debugger-attach ---------------------------------------------------------------------------


def test_debugger_attach_selects_the_app_persistently_and_says_to_restore(dev, tmp_path):
    report = apply("debugger-attach", tmp_path, package=PKG)
    assert dev.settings[("global", None, "debug_app")] == PKG
    assert report["needs_effect_check"] and "debuggable" in report["effect_check"]
    assert "every launch" in prof.PROFILE_BY_NAME["debugger-attach"].notes


# --- gates ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("sdk", ["28", "38", "unknown", ""])
@pytest.mark.parametrize("profile", ["layout-debug", "lifecycle-stress", "battery-doze", "rtl-large-font", "accessibility-display"])
def test_api_range_gate_refuses_before_any_write_or_snapshot(make, tmp_path, profile, sdk):
    dev = make(sdk=sdk)
    with pytest.raises(DevOptionsError) as caught:
        apply(profile, tmp_path)
    assert caught.value.kind == "profile_api_range" and "API 29" in str(caught.value)
    assert dev.writes() == [] and not (tmp_path / "snap.json").exists()


def test_the_legacy_profiles_that_never_read_the_api_still_do_not(make, tmp_path):
    dev = make(sdk="28")
    apply("fast-ui", tmp_path)
    assert ["shell", "getprop", "ro.build.version.sdk"] not in dev.calls
    assert dev.settings[("global", None, "window_animation_scale")] == "0"


@pytest.mark.parametrize("maker,brand,expected", [("Google", "google", "pixel"), ("samsung", "samsung", "samsung"), ("ExampleMaker", "other", "other")])
def test_every_new_profile_applies_on_every_maker_layer(make, tmp_path, maker, brand, expected):
    dev = make(manufacturer=maker, brand=brand)
    for index, name in enumerate(prof.PROFILE_NAMES[4:]):
        profile = prof.PROFILE_BY_NAME[name]
        assert expected in profile.oem
        result = apply(name, tmp_path, name=f"{index}.json", package=PKG if profile.package != "none" else None)
        assert result["ok"] and result["claim_boundary"].endswith("has not been run on a device.")


def test_claim_boundary_always_says_a_stored_value_is_not_proof_and_names_effect_checks(dev, tmp_path):
    for index, name in enumerate(prof.PROFILE_NAMES[4:]):
        profile = prof.PROFILE_BY_NAME[name]
        result = apply(name, tmp_path, name=f"{index}.json", package=PKG if profile.package != "none" else None)
        text = result["claim_boundary"]
        assert "not proof" in text and "Offline-tested only" in text
        assert ("Effect check needed" in text) == profile.needs_effect_check == result["needs_effect_check"]
        assert ("cannot reproduce the exact prior state" in text) == (profile.restore_exactness == "approximate")


def test_profiles_that_need_an_effect_check_are_the_ones_the_research_marks(dev):
    flagged = {p.name for p in prof.PROFILES if p.needs_effect_check}
    assert {"layout-debug", "lifecycle-stress", "rtl-large-font"} <= flagged
    assert not {"performance-capture", "battery-doze", "background-restricted"} & flagged


# --- Samsung Auto Blocker guidance on a failed write ---------------------------------------------


def _samsung(make, **props):
    dev = make(manufacturer="samsung", brand="samsung", sdk=props.pop("sdk", "34"))
    dev.props.update(props)
    return dev


def test_failed_write_on_one_ui_6_points_to_auto_blocker(make, tmp_path):
    dev = _samsung(make, **{"ro.build.version.oneui": "60101"})
    dev.fail = lambda shell: "boom" if shell[1:3] == ["setprop", "debug.layout"] else None
    with pytest.raises(DevOptionsError) as caught:
        apply("layout-debug", tmp_path)
    assert fr.classify(caught.value) == "devopts.samsung_auto_blocker"
    text = " ".join(step.text for step in failure_guide.GUIDES["devopts.samsung_auto_blocker"].steps)
    assert "Security and privacy" in text and "Auto Blocker" in text


def test_failed_write_on_one_ui_5_does_not_blame_auto_blocker(make, tmp_path):
    dev = _samsung(make, sdk="33", **{"ro.build.version.oneui": "50100"})
    dev.fail = lambda shell: "boom" if shell[1:3] == ["setprop", "debug.layout"] else None
    with pytest.raises(DevOptionsError) as caught:
        apply("layout-debug", tmp_path)
    assert fr.classify(caught.value) in ("devopts.apply_rolled_back", "devopts.apply_rollback_incomplete")


def test_one_ui_version_missing_falls_back_to_the_android_version(make, tmp_path):
    dev = _samsung(make, sdk="35")
    dev.fail = lambda shell: "boom" if shell[1:3] == ["setprop", "debug.layout"] else None
    with pytest.raises(DevOptionsError) as caught:
        apply("layout-debug", tmp_path)
    assert fr.classify(caught.value) == "devopts.samsung_auto_blocker"


def test_failed_write_on_a_pixel_does_not_blame_auto_blocker(make, tmp_path):
    dev = make(manufacturer="Google", brand="google")
    dev.fail = lambda shell: "boom" if shell[1:3] == ["setprop", "debug.layout"] else None
    with pytest.raises(DevOptionsError) as caught:
        apply("layout-debug", tmp_path)
    assert fr.classify(caught.value) != "devopts.samsung_auto_blocker"


def test_failed_restore_on_one_ui_6_points_to_auto_blocker(make, tmp_path):
    dev = _samsung(make, **{"ro.build.version.oneui": "60000"})
    apply("layout-debug", tmp_path)
    dev.fail = lambda shell: "boom" if shell[1:3] == ["setprop", "debug.layout"] else None
    with pytest.raises(DevOptionsError) as caught:
        restore(tmp_path)
    assert caught.value.kind == "restore_failed" and fr.classify(caught.value) == "devopts.samsung_auto_blocker"


# --- the command line ------------------------------------------------------------------------------


def test_apply_command_prints_the_note_and_the_claim_boundary(dev, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from adapt_adb.cli import app

    result = CliRunner().invoke(app, ["dev-options", "apply", "--profile", "layout-debug", "-s", SERIAL, "--snapshot", str(tmp_path / "cli.json")])
    assert result.exit_code == 0
    assert result.stdout.startswith("Applied layout-debug; recovery snapshot:")
    assert "Property changes take effect when the app restarts" in result.stdout and "Effect check needed" in result.stdout
    restored = CliRunner().invoke(app, ["dev-options", "restore", "-s", SERIAL, "--snapshot", str(tmp_path / "cli.json")])
    assert restored.exit_code == 0 and restored.stdout.startswith("Restored snapshot:")


def test_apply_command_takes_package_and_repeatable_options(dev, tmp_path):
    from typer.testing import CliRunner

    from adapt_adb.cli import app

    result = CliRunner().invoke(app, ["dev-options", "apply", "--profile", "accessibility-display", "-s", SERIAL, "--snapshot", str(tmp_path / "a.json"),
                                      "--option", "daltonizer", "--option", "force-dark"])
    assert result.exit_code == 0 and saved(tmp_path, "a.json")["options"] == ["daltonizer", "force-dark"]
    result = CliRunner().invoke(app, ["dev-options", "apply", "--profile", "debugger-attach", "-s", SERIAL, "--snapshot", str(tmp_path / "b.json"), "--package", PKG])
    assert result.exit_code == 0 and saved(tmp_path, "b.json")["package"] == PKG


@pytest.mark.parametrize("args,first_line,guide", [
    (["--profile", "nope"], "Error: Profile must be one of: ", "devopts.invalid_profile"),
    (["--profile", "debugger-attach"], "Error: Profile debugger-attach needs --package", "devopts.package_required"),
    (["--profile", "fast-ui", "--package", PKG], "Error: Profile fast-ui does not use a package.", "devopts.invalid_package"),
    (["--profile", "input-visualization", "--option", "bogus"], "Error: Unknown option for input-visualization", "devopts.invalid_option"),
])
def test_apply_command_refusals_keep_the_error_line_and_point_to_a_guide(dev, tmp_path, args, first_line, guide):
    from typer.testing import CliRunner

    from adapt_adb.cli import app

    result = CliRunner().invoke(app, ["dev-options", "apply", "-s", SERIAL, "--snapshot", str(tmp_path / "x.json"), *args])
    assert result.exit_code == 1 and result.stderr.startswith(first_line)
    assert f"adapt explain {guide}" in result.stderr
    assert dev.calls == [] and not (tmp_path / "x.json").exists()


def test_apply_command_refuses_a_network_cutting_profile_over_wireless_adb(dev, tmp_path):
    from typer.testing import CliRunner

    from adapt_adb.cli import app

    result = CliRunner().invoke(app, ["dev-options", "apply", "--profile", "network-degraded", "-s", "192.0.2.10:5555", "--snapshot", str(tmp_path / "w.json")])
    assert result.exit_code == 1 and "refused over a TCP or wireless-debugging transport" in result.stderr
    assert "adapt explain devopts.wireless_refused" in result.stderr and dev.calls == []
