"""Strict parsers, value rules and the profile registry (pure, no device)."""

from __future__ import annotations

import pytest

from adapt_adb import dev_entries as ent
from adapt_adb import dev_profiles as prof
from adapt_adb import safety


def test_value_rules_number_enum_and_pattern():
    assert ent.NUMBER.accepts("-1") and ent.NUMBER.accepts("1.5") and not ent.NUMBER.accepts("1e3")
    assert not ent.NUMBER.accepts("") and not ent.NUMBER.accepts(" 1") and not ent.NUMBER.accepts(None)
    bounded = ent.ValueRule("number", minimum="0.85", maximum="2.0")
    assert bounded.accepts("1.3") and not bounded.accepts("2.5") and not bounded.accepts("0.5")
    assert bounded.equal("1.30", "1.3") and not bounded.equal("1.3", "x")
    flag = ent.enum("0", "1")
    assert flag.accepts("1") and not flag.accepts("2") and flag.equal("1", "1") and not flag.equal("1", "01")
    token = ent.pattern(r"[a-z]{2,3}")
    assert token.accepts("ar") and not token.accepts("ar;ls") and not token.accepts("a" * 400)


@pytest.mark.parametrize("package,ok", [
    ("com.example.app", True), ("a.b", True), ("com.Example_1.app", True),
    ("com", False), ("com.example app", False), ("com.example.app; ls", False), ("-w", False), ("1com.app", False), ("", False), (None, False),
])
def test_package_validator(package, ok):
    assert ent.valid_package(package) is ok


def test_density_parser_default_and_override():
    assert ent.parse_density(["Physical density: 420"]) == ent.Parsed("default", {"physical": 420})
    parsed = ent.parse_density(["Physical density: 420\nOverride density: 480"])
    assert parsed.value == "override:480" and parsed.extras == {"physical": 420}
    for bad in ("", "Override density: 480", "Physical density: abc", "Physical density: 420\nsurprise"):
        assert ent.parse_density([bad]) is None


def test_density_derivation_and_rules():
    assert ent.derive_density({"physical": 420}) == "override:483"
    assert ent.derive_density({"physical": 5}) is None and ent.derive_density({}) is None
    spec = ent.COMMAND_BY_NAME["wm_density"]
    assert spec.applied_rule.accepts("override:483") and not spec.applied_rule.accepts("override:90") and not spec.applied_rule.accepts("default")
    assert spec.original_rule.accepts("default") and spec.original_rule.accepts("override:1000")
    assert spec.restore(ent.CommandContext(None, 0), "default") == [["wm", "density", "reset"]]
    assert spec.restore(ent.CommandContext(None, 0), "override:480") == [["wm", "density", "480"]]


BATTERY = "Current Battery Service state:\n{stopped}  AC powered: {ac}\n  USB powered: {usb}\n  Wireless powered: {wl}\n  level: 80"


def _battery(stopped=False, ac="false", usb="true", wl="false"):
    return BATTERY.format(stopped="  (UPDATES STOPPED -- use 'reset' to restart)\n" if stopped else "", ac=ac, usb=usb, wl=wl)


def test_battery_parser_states():
    assert ent.parse_battery([_battery()]).value == "normal"
    assert ent.parse_battery([_battery(stopped=True, usb="false")]).value == "unplugged"
    assert ent.parse_battery([_battery(stopped=True, usb="true")]).value == "override-other"
    assert ent.parse_battery(["Current Battery Service state:\n  level: 1"]) is None
    assert ent.parse_battery([""]) is None


@pytest.mark.parametrize("text,value", [("ACTIVE", "normal"), ("INACTIVE", "normal"), ("IDLE_PENDING", "normal"), ("IDLE", "idle"), ("IDLE_MAINTENANCE", "maintenance")])
def test_doze_parser_states(text, value):
    assert ent.parse_doze([text]).value == value


@pytest.mark.parametrize("bad", ["", "SURPRISE", "ACTIVE\nIDLE"])
def test_doze_parser_refuses_unknown_text(bad):
    assert ent.parse_doze([bad]) is None


def test_night_mode_parser_and_restore_rule():
    for mode in ("yes", "no", "auto"):
        assert ent.parse_night([f"Night mode: {mode}"]).value == mode
    assert ent.parse_night(["Night mode: custom_schedule"]).value == "custom"
    assert ent.parse_night(["Night mode: sometimes"]) is None
    assert not ent.COMMAND_BY_NAME["night_mode"].original_rule.accepts("custom")


def test_debug_app_parser_and_restore_commands():
    spec = ent.COMMAND_BY_NAME["debug_app"]
    ctx = ent.CommandContext("com.example.app", 0)
    assert ent.parse_debug_app(["null", "null"]).value == "none"
    assert ent.parse_debug_app(["com.other.app", "0"]).value == "app=com.other.app;wait=0"
    assert ent.parse_debug_app(["com.other.app", "1"]).value == "app=com.other.app;wait=1"
    assert ent.parse_debug_app(["bad name", "0"]) is None and ent.parse_debug_app(["null", "9"]) is None and ent.parse_debug_app(["null"]) is None
    assert spec.apply(ctx, "app=com.example.app;wait=1") == [["am", "set-debug-app", "-w", "--persistent", "com.example.app"]]
    assert spec.restore(ctx, "none") == [["am", "clear-debug-app"]]
    assert spec.restore(ctx, "app=com.other.app;wait=0") == [["am", "set-debug-app", "--persistent", "com.other.app"]]
    assert ent.derive_debug_app("com.example.app") == "app=com.example.app;wait=1" and ent.derive_debug_app("x") is None
    assert spec.applied_rule.accepts("app=com.example.app;wait=1") and not spec.applied_rule.accepts("none")


def test_dexopt_parser_reads_the_first_status_after_the_header():
    text = "junk [status=ignored]\nDexopt state:\n  [com.example.app]\n    arm64: [status=speed-profile] [reason=bg-dexopt]\n"
    assert ent.parse_dexopt([text]).value == "speed-profile"
    assert ent.parse_dexopt(["no header [status=verify]"]) is None
    assert ent.parse_dexopt(["Dexopt state:\n nothing here"]) is None


@pytest.mark.parametrize("text,value", [("10", "active"), ("20", "working_set"), ("30", "frequent"), ("40", "rare"), ("45", "restricted"), ("5", "exempted"), ("50", "never")])
def test_bucket_parser(text, value):
    assert ent.parse_bucket([text]).value == value
    assert (value in ("exempted", "never")) != ent.COMMAND_BY_NAME["standby_bucket"].original_rule.accepts(value)


def test_bucket_parser_refuses_unknown_numbers():
    assert ent.parse_bucket(["99"]) is None and ent.parse_bucket(["rare"]) is None


def test_appop_parser_and_restore_mapping():
    assert ent.parse_appop(["No operations."]).value == "allow"
    assert ent.parse_appop(["RUN_ANY_IN_BACKGROUND: ignore; time=+2d3h ago"]).value == "ignore"
    assert ent.parse_appop(["RUN_ANY_IN_BACKGROUND: default"]).value == "allow"
    assert ent.parse_appop(["RUN_ANY_IN_BACKGROUND: errored"]) is None and ent.parse_appop(["OTHER_OP: allow"]) is None
    spec = ent.COMMAND_BY_NAME["app_background"]
    ctx = ent.CommandContext("com.example.app", 10)
    assert spec.restore(ctx, "allow")[0][-1] == "default" and spec.restore(ctx, "deny")[0][-1] == "deny"
    assert not spec.exact_restore


def test_locale_parser_normalises_and_refuses_odd_tags():
    line = "Locales for com.example.app for user 10 are [{}]"
    assert ent.parse_locales([line.format("")]).value == ""
    assert ent.parse_locales([line.format("ar_EG, en_US")]).value == "ar-eg,en-us"
    assert ent.parse_locales([line.format("ar-u-nu-latn")]) is None
    assert ent.parse_locales([line.format("ar#bad")]) is None
    assert ent.parse_locales(["something else"]) is None
    spec = ent.COMMAND_BY_NAME["app_locale"]
    ctx = ent.CommandContext("com.example.app", 10)
    assert spec.restore(ctx, "") == [["cmd", "locale", "set-app-locales", "com.example.app", "--user", "10"]]
    assert spec.restore(ctx, "fr,en-us")[0][-2:] == ["--locales", "fr,en-us"]
    assert spec.original_rule.accepts("") and spec.original_rule.accepts("fr,en-us") and not spec.original_rule.accepts("fr; ls")


def test_data_saver_and_airplane_parsers():
    assert ent.parse_netpolicy(["Restrict background status: enabled"]).value == "enabled"
    assert ent.parse_netpolicy(["Restrict background status: maybe"]) is None
    assert ent.parse_airplane(["0"]).value == "disabled" and ent.parse_airplane(["1"]).value == "enabled"
    assert ent.parse_airplane(["null"]).value == "disabled" and ent.parse_airplane(["2"]) is None


def test_stay_awake_and_font_scale_derivation():
    assert ent.derive_stay_awake_usb(None) == "2" and ent.derive_stay_awake_usb("1") == "3" and ent.derive_stay_awake_usb("7") == "7"
    assert ent.derive_stay_awake_usb("16") is None and ent.derive_stay_awake_usb("2.5") is None
    assert ent.derive_font_scale(33) == "1.3" and ent.derive_font_scale(34) == "2.0" and ent.derive_font_scale(None) == "1.3"


def test_property_allowlist_is_debug_only_and_values_are_tokens():
    assert all(name.startswith("debug.") for name in ent.PROPERTY_BY_NAME)
    assert ent.PROPERTY_TOKEN.accepts("true") and ent.PROPERTY_TOKEN.accepts("1") and not ent.PROPERTY_TOKEN.accepts("a b")
    assert not ent.PROPERTY_TOKEN.accepts("x;y") and not ent.PROPERTY_TOKEN.accepts("")


def test_every_entry_name_is_unique_across_kinds():
    names = [item.name for item in ent.ALL_SETTINGS] + list(ent.PROPERTY_BY_NAME) + list(ent.COMMAND_BY_NAME)
    assert len(names) == len(set(names))
    assert len(ent.SETTINGS) == 13  # the inspected settings did not change


# --- every argv the registry can produce is an allowlisted shape -----------------------------------


def _all_command_argvs():
    ctx = ent.CommandContext("com.example.app", 10)
    samples = {
        "wm_density": ("default", "override:480"), "battery_state": ("normal",), "doze_state": ("normal",),
        "night_mode": ("yes", "no", "auto"), "debug_app": ("none", "app=com.example.app;wait=1", "app=com.example.app;wait=0"),
        "package_compile": ("verify", "speed-profile"), "standby_bucket": ("active", "rare", "restricted"),
        "app_background": ("allow", "ignore", "deny", "foreground"), "app_locale": ("", "ar", "fr,en-us"),
        "data_saver": ("enabled", "disabled"), "airplane_mode": ("enabled", "disabled"),
    }
    for spec in ent.COMMANDS:
        yield from spec.read(ctx)
        for state in samples[spec.name]:
            yield from spec.restore(ctx, state)
            if spec.applied_rule.accepts(state) or spec.name in ("battery_state", "doze_state", "airplane_mode", "data_saver"):
                yield from spec.apply(ctx, state if spec.applied_rule.accepts(state) else {"battery_state": "unplugged", "doze_state": "idle", "airplane_mode": "enabled", "data_saver": "enabled"}[spec.name])


def test_every_command_argv_matches_an_allowlisted_shape():
    argvs = list(_all_command_argvs())
    assert len(argvs) > 40
    for argv in argvs:
        assert safety.match_dev_options_shell(["shell", *argv]) is not None, argv


def test_every_setting_and_property_argv_matches_an_allowlisted_shape():
    for spec in ent.ALL_SETTINGS:
        for user in ([], ["--user", "10"]) if spec.user_scoped else ([],):
            assert safety.match_dev_options_shell(["shell", "settings", *user, "get", spec.table, spec.key]) is not None
            assert safety.match_dev_options_shell(["shell", "settings", *user, "put", spec.table, spec.key, "1.5"]) is not None
            assert safety.match_dev_options_shell(["shell", "settings", *user, "delete", spec.table, spec.key]) is not None
    for spec in ent.PROPERTIES:
        assert safety.match_dev_options_shell(["shell", "getprop", spec.name]) is not None
        assert safety.match_dev_options_shell(["shell", "setprop", spec.name, "true"]) is not None
        assert safety.match_dev_options_shell(["shell", "setprop", spec.name, "''"]) is not None


# --- registry integrity ---------------------------------------------------------------------------


def test_registry_names_and_new_profiles():
    assert prof.PROFILE_NAMES[:4] == ("fast-ui", "touches", "stay-awake", "desktop-testing")
    assert set(prof.PROFILE_NAMES) >= {
        "layout-debug", "lifecycle-stress", "performance-capture", "battery-doze", "rtl-large-font",
        "accessibility-display", "input-visualization", "network-degraded", "background-restricted", "debugger-attach",
    }
    assert len(prof.PROFILE_NAMES) == 14 and len(set(prof.PROFILE_NAMES)) == 14


def test_registry_entries_are_json_shaped_and_consistent():
    import json

    for item in prof.profile_registry():
        json.dumps(item)
        assert set(item["oem"]) <= set(prof.OEM_LAYERS)
        assert item["package"] in ("none", "optional", "required")
        assert item["package_required"] == (item["package"] == "required")
        assert item["needs_reboot"] in ("no", "unknown")
        assert item["restore_exactness"] in ("exact", "approximate")
        assert (item["approximate_restore"] != []) == (item["restore_exactness"] == "approximate")
        assert item["needs_effect_check"] == bool(item["effect_check"])
        assert set(item["mechanisms"]) <= {"setting", "property", "command"}


def test_per_app_steps_require_a_package_profile():
    for profile in prof.PROFILES:
        if any(step.package for step in profile.steps):
            assert profile.package in ("optional", "required"), profile.name
        if profile.package == "required":
            assert any(step.package for step in profile.steps), profile.name


def test_wireless_refusal_is_set_for_the_network_cutting_profiles():
    refusing = {p.name for p in prof.PROFILES if p.refuse_wireless}
    assert refusing == {"battery-doze", "network-degraded"}


def test_select_steps_gates_by_api_option_and_package():
    profile = prof.PROFILE_BY_NAME["input-visualization"]
    assert [s.entry for s in prof.select_steps(profile, 34, (), None)] == ["show_touches"]
    assert [s.entry for s in prof.select_steps(profile, 35, (), None)] == ["show_touches", "show_key_presses"]
    assert [s.entry for s in prof.select_steps(profile, 36, ("pointer-location",), None)] == ["show_touches", "show_key_presses", "pointer_location"]
    rtl = prof.PROFILE_BY_NAME["rtl-large-font"]
    assert "app_locale" not in [s.entry for s in prof.select_steps(rtl, 36, (), None)]
    assert "app_locale" not in [s.entry for s in prof.select_steps(rtl, 32, (), "com.example.app")]
    assert "app_locale" in [s.entry for s in prof.select_steps(rtl, 33, (), "com.example.app")]


def test_evaluate_reports_reasons_and_warnings():
    net = prof.PROFILE_BY_NAME["network-degraded"]
    assert prof.evaluate(net, api=36, oem="other", transport="usb")["eligible"]
    wireless = prof.evaluate(net, api=36, oem="other", transport="tcp")
    assert not wireless["eligible"] and wireless["reasons"][0]["code"] == "wireless_transport"
    assert prof.evaluate(net, api=29, oem="other", transport="usb")["reasons"][0]["code"] == "api_too_low"
    assert prof.evaluate(net, api=32, oem="other", transport="usb")["reasons"][0]["code"] == "no_steps_apply"
    assert prof.evaluate(net, api=99, oem="other", transport="usb")["reasons"][0]["code"] == "api_not_researched"
    assert prof.evaluate(net, api=None, oem="other", transport="usb")["reasons"][0]["code"] == "api_unknown"
    debug = prof.PROFILE_BY_NAME["debugger-attach"]
    verdict = prof.evaluate(debug, api=36, oem="samsung", transport="usb")
    assert verdict["eligible"] and verdict["needs_package"]
    assert prof.evaluate(debug, api=36, oem="samsung", transport="usb", package="com.example.app")["needs_package"] is False
    rtl = prof.evaluate(prof.PROFILE_BY_NAME["rtl-large-font"], api=36, oem="samsung", transport="usb")
    assert any("clamp" in warning for warning in rtl["warnings"])
    fast = prof.evaluate(prof.PROFILE_BY_NAME["fast-ui"], api=None, oem=None, transport="usb")
    assert fast["eligible"], "legacy ungated profiles do not need the API level"
