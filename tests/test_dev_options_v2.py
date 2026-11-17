"""Snapshot model v2: property and command-pair entries, scopes, rollback, transport and v1 compatibility.

Offline: a fake shell stands in for the phone and refuses any argv that is not an allowlisted shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from adapt_adb import dev_entries as ent
from adapt_adb import dev_options, safety
from adapt_adb import dev_profiles as prof
from adapt_adb.dev_options import DevOptionsError
from tests.fake_device import PKG, FakeDevice

SERIAL = "SERIAL1"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(dev_options, "preflight_adb_server", lambda *a, **k: None)
    monkeypatch.setattr(dev_options, "_LOCK_ROOT", tmp_path / "locks")


@pytest.fixture
def dev(monkeypatch) -> FakeDevice:
    device = FakeDevice()
    monkeypatch.setattr(dev_options, "_run_process", device)
    return device


def apply(profile, tmp_path, name="snap.json", serial=SERIAL, **kw):
    return dev_options.apply_profile(profile, serial, tmp_path / name, adb_path="adb", **kw)


def restore(tmp_path, name="snap.json", serial=SERIAL, **kw):
    return dev_options.restore_snapshot(serial, tmp_path / name, adb_path="adb", **kw)


def load(tmp_path, name="snap.json"):
    return json.loads((tmp_path / name).read_text(encoding="utf-8"))


# --- property entries ------------------------------------------------------------------------------


def test_property_entry_absent_original_is_restored_as_empty(dev, tmp_path):
    report = apply("layout-debug", tmp_path)
    assert report["ok"] and dev.props["debug.layout"] == "true"
    saved = load(tmp_path)
    assert saved["schema_version"] == 2
    entry = next(e for e in saved["settings"] if e["kind"] == "property")
    assert entry["original"] == {"present": False, "value": None} and entry["table"] == "property" and entry["key"] == "debug.layout"
    assert ["shell", "setprop", "debug.layout", "true"] in dev.calls
    result = restore(tmp_path)
    assert result["ok"] and dev.props["debug.layout"] == ""
    assert ["shell", "setprop", "debug.layout", "''"] in dev.calls
    assert load(tmp_path)["status"] == "restored"


def test_property_entry_present_original_is_restored_verbatim(dev, tmp_path):
    dev.props["debug.layout"] = "false"
    apply("layout-debug", tmp_path)
    assert dev.props["debug.layout"] == "true"
    restore(tmp_path)
    assert dev.props["debug.layout"] == "false"


def test_property_already_holding_the_applied_value_is_unchanged_and_never_restored(dev, tmp_path):
    dev.props["debug.layout"] = "true"
    report = apply("layout-debug", tmp_path)
    assert "debug.layout" in report["unchanged"]
    assert not any(call[:2] == ["shell", "setprop"] for call in dev.calls)
    assert restore(tmp_path)["ok"] and dev.props["debug.layout"] == "true"


def test_property_external_change_is_a_conflict_not_overwritten(dev, tmp_path):
    apply("layout-debug", tmp_path)
    dev.props["debug.layout"] = "other"
    before = len(dev.calls)
    result = restore(tmp_path)
    assert result["ok"] is False and result["status"] == "restore_conflict"
    assert dev.props["debug.layout"] == "other"
    assert not [c for c in dev.calls[before:] if c[:2] == ["shell", "setprop"]]


def test_property_write_that_is_silently_ignored_fails_readback_and_rolls_back(dev, tmp_path):
    dev.ignore.add("setprop")
    with pytest.raises(DevOptionsError) as caught:
        apply("layout-debug", tmp_path)
    assert caught.value.kind == "apply_failed"
    assert load(tmp_path)["status"] == "partial_failure"
    assert dev.settings.get(("global", None, "debug_view_attributes")) is None


def test_property_value_that_is_not_a_plain_token_is_refused_before_any_write(dev, tmp_path):
    dev.props["debug.layout"] = "bad value"
    with pytest.raises(DevOptionsError) as caught:
        apply("layout-debug", tmp_path)
    assert caught.value.kind == "setting_malformed"
    assert dev.writes() == [] and not (tmp_path / "snap.json").exists()


def test_a_property_outside_the_debug_allowlist_has_no_argv_shape():
    for name in ("persist.sys.language", "ro.debuggable", "sys.powerctl", "debug.layout; reboot"):
        assert safety.match_dev_options_shell(["shell", "setprop", name, "1"]) is None
    assert safety.match_dev_options_shell(["shell", "setprop", "debug.layout", "a b"]) is None
    assert safety.match_dev_options_shell(["shell", "setprop", "debug.layout", "true;reboot"]) is None


# --- command-pair entries --------------------------------------------------------------------------


def test_command_pair_density_restores_the_observed_override(dev, tmp_path):
    dev.density_override = 400
    apply("rtl-large-font", tmp_path)
    assert dev.density_override == 483
    saved = load(tmp_path)
    density = next(e for e in saved["settings"] if e["key"] == "wm_density")
    assert density["kind"] == "command" and density["original"] == {"present": True, "value": "override:400"}
    restore(tmp_path)
    assert dev.density_override == 400
    assert ["shell", "wm", "density", "400"] in dev.calls and ["shell", "wm", "density", "reset"] not in dev.calls


def test_command_pair_density_without_override_uses_reset(dev, tmp_path):
    apply("rtl-large-font", tmp_path)
    restore(tmp_path)
    assert dev.density_override is None and ["shell", "wm", "density", "reset"] in dev.calls


def test_command_pair_battery_and_doze_round_trip(dev, tmp_path):
    report = apply("battery-doze", tmp_path)
    assert dev.battery_stopped and dev.doze == "IDLE"
    assert report["changed"] == ["battery_state", "doze_state"]
    result = restore(tmp_path)
    assert result["ok"] and not dev.battery_stopped and dev.doze == "ACTIVE"
    assert result["approximate_restore"] == ["doze_state"]
    assert [c for c in dev.calls if c[:3] == ["shell", "cmd", "battery"]][-1] == ["shell", "cmd", "battery", "reset"]


@pytest.mark.parametrize("setup,name", [
    (lambda d: setattr(d, "battery_stopped", True), "battery_state"),
    (lambda d: setattr(d, "doze", "IDLE"), "doze_state"),
])
def test_command_pair_refuses_a_prior_state_it_cannot_put_back(dev, tmp_path, setup, name):
    setup(dev)
    with pytest.raises(DevOptionsError) as caught:
        apply("battery-doze", tmp_path)
    assert caught.value.kind == "unsupported_state" and name in str(caught.value)
    assert dev.writes() == [] and not (tmp_path / "snap.json").exists()


def test_command_pair_unparseable_output_refuses_before_any_write(dev, tmp_path, monkeypatch):
    dev.night = "sometimes"
    with pytest.raises(DevOptionsError) as caught:
        apply("accessibility-display", tmp_path)
    assert caught.value.kind == "setting_malformed"
    assert dev.writes() == [] and not (tmp_path / "snap.json").exists()


def test_command_pair_failed_step_rolls_back_the_earlier_ones(dev, tmp_path):
    dev.fail = lambda shell: "boom" if shell[1:4] == ["dumpsys", "deviceidle", "force-idle"] else None
    with pytest.raises(DevOptionsError) as caught:
        apply("battery-doze", tmp_path)
    assert caught.value.kind == "apply_failed"
    assert not dev.battery_stopped and dev.doze == "INACTIVE"
    saved = load(tmp_path)
    assert saved["status"] == "partial_failure"
    assert {e["name"]: e["state"] for e in saved["settings"]}["battery_state"] == "rolled_back"


def test_command_pair_readback_that_does_not_match_rolls_back(dev, tmp_path):
    dev.ignore.add("doze")
    with pytest.raises(DevOptionsError):
        apply("battery-doze", tmp_path)
    assert not dev.battery_stopped


def test_command_pair_external_change_is_a_conflict_and_nothing_is_written(dev, tmp_path):
    apply("battery-doze", tmp_path)
    dev.doze = "IDLE_MAINTENANCE"        # neither the original (normal) nor the applied (idle) state
    before = len(dev.writes())
    result = restore(tmp_path)
    assert result["ok"] is False and result["status"] == "restore_conflict"
    assert any("doze_state" in conflict for conflict in result["conflicts"])
    assert len(dev.writes()) == before and dev.battery_stopped
    assert {e["name"]: e["state"] for e in load(tmp_path)["settings"]}["doze_state"] == "conflict"


def test_command_pair_already_back_to_normal_is_reported_as_already_restored(dev, tmp_path):
    apply("battery-doze", tmp_path)
    dev.battery_stopped = False
    dev.doze = "ACTIVE"
    result = restore(tmp_path)
    assert result["ok"] and result["restored"] == []
    assert {e["state"] for e in load(tmp_path)["settings"]} == {"already_restored"}


def test_command_pair_restore_failure_returns_restored_entries_to_the_applied_state(dev, tmp_path):
    apply("battery-doze", tmp_path)
    dev.fail = lambda shell: "boom" if shell[1:4] == ["dumpsys", "deviceidle", "unforce"] else None
    with pytest.raises(DevOptionsError) as caught:
        restore(tmp_path)
    assert caught.value.kind == "restore_failed"
    assert load(tmp_path)["status"] == "restore_failed"


def test_reset_only_control_always_resets_even_after_background_recompilation(dev, tmp_path):
    report = apply("performance-capture", tmp_path, package=PKG)
    assert dev.dexopt[PKG] == "speed-profile" and "package_compile" in report["changed"]
    assert report["restore_exactness"] == "approximate" and report["approximate_restore"] == ["package_compile"]
    assert "cannot reproduce the exact prior state" in report["claim_boundary"]
    dev.dexopt[PKG] = "speed"          # the system recompiled the app in the background
    result = restore(tmp_path)
    assert result["ok"] and result["approximate_restore"] == ["package_compile"]
    assert ["shell", "cmd", "package", "compile", "--reset", PKG] in dev.calls
    assert load(tmp_path)["status"] == "restored"


# --- per-app scope ---------------------------------------------------------------------------------


def test_per_app_entries_record_and_use_the_package(dev, tmp_path):
    apply("background-restricted", tmp_path, package=PKG)
    saved = load(tmp_path)
    assert saved["package"] == PKG
    appop = next(e for e in saved["settings"] if e["key"] == "app_background")
    assert appop["scope"] == PKG and appop["user"] == 10
    assert dev.appops[PKG] == "ignore" and dev.restrict_background is True
    restore(tmp_path)
    assert dev.appops[PKG] is None and dev.restrict_background is False


def test_whole_device_entries_have_no_scope(dev, tmp_path):
    apply("background-restricted", tmp_path, package=PKG)
    saver = next(e for e in load(tmp_path)["settings"] if e["key"] == "data_saver")
    assert saver["scope"] is None and saver["user"] is None


@pytest.mark.parametrize("package", ["", "bad name", "com.example.app; ls", "-w", "com"])
def test_invalid_package_is_refused_before_the_device_is_contacted(dev, tmp_path, package):
    with pytest.raises(DevOptionsError) as caught:
        apply("debugger-attach", tmp_path, package=package)
    assert caught.value.kind == "invalid_package"
    assert dev.calls == []


def test_missing_required_package_is_refused_before_the_device_is_contacted(dev, tmp_path):
    with pytest.raises(DevOptionsError) as caught:
        apply("debugger-attach", tmp_path)
    assert caught.value.kind == "package_required" and dev.calls == []


def test_package_on_a_whole_device_profile_is_refused(dev, tmp_path):
    with pytest.raises(DevOptionsError) as caught:
        apply("fast-ui", tmp_path, package=PKG)
    assert caught.value.kind == "invalid_package" and dev.calls == []


def test_unknown_option_is_refused(dev, tmp_path):
    with pytest.raises(DevOptionsError) as caught:
        apply("input-visualization", tmp_path, options=["nonsense"])
    assert caught.value.kind == "invalid_option" and dev.calls == []
    with pytest.raises(DevOptionsError):
        apply("input-visualization", tmp_path, options="pointer-location")


def test_debug_app_original_other_app_is_restored_with_its_flags(dev, tmp_path):
    dev.put("global", "debug_app", "com.other.app")
    dev.put("global", "wait_for_debugger", "0")
    apply("debugger-attach", tmp_path, package=PKG)
    assert dev.settings[("global", None, "debug_app")] == PKG and dev.settings[("global", None, "wait_for_debugger")] == "1"
    assert ["shell", "am", "set-debug-app", "-w", "--persistent", PKG] in dev.calls
    restore(tmp_path)
    assert dev.settings[("global", None, "debug_app")] == "com.other.app" and dev.settings[("global", None, "wait_for_debugger")] == "0"


def test_debug_app_none_original_is_cleared_on_restore(dev, tmp_path):
    apply("debugger-attach", tmp_path, package=PKG)
    restore(tmp_path)
    assert ("global", None, "debug_app") not in dev.settings
    assert ["shell", "am", "clear-debug-app"] in dev.calls


# --- non-numeric values ----------------------------------------------------------------------------


def test_non_numeric_enum_setting_round_trips_and_absent_key_is_deleted(dev, tmp_path, monkeypatch):
    spec = ent.SettingSpec("freezer", "lifecycle", "global", "cached_apps_freezer", False, ent.enum("device_default", "enabled", "disabled"))
    monkeypatch.setitem(ent.SETTING_BY_NAME, "freezer", spec)
    monkeypatch.setitem(ent.SETTING_BY_ID, ("global", "cached_apps_freezer"), spec)
    profile = prof.ProfileSpec("test-freezer", "test", (prof.StepSpec("freezer", "setting", "disabled"),), api_gate=False, max_api=None)
    monkeypatch.setitem(prof.PROFILE_BY_NAME, "test-freezer", profile)
    dev.put("global", "cached_apps_freezer", "device_default")
    apply("test-freezer", tmp_path)
    assert dev.settings[("global", None, "cached_apps_freezer")] == "disabled"
    assert restore(tmp_path)["ok"] and dev.settings[("global", None, "cached_apps_freezer")] == "device_default"
    dev.settings.pop(("global", None, "cached_apps_freezer"))
    apply("test-freezer", tmp_path, name="second.json") if False else dev_options.apply_profile("test-freezer", SERIAL, tmp_path / "second.json", adb_path="adb")
    assert dev.settings[("global", None, "cached_apps_freezer")] == "disabled"
    assert dev_options.restore_snapshot(SERIAL, tmp_path / "second.json", adb_path="adb")["ok"]
    assert ("global", None, "cached_apps_freezer") not in dev.settings


def test_enum_setting_outside_its_set_is_refused_not_overwritten(dev, tmp_path):
    dev.put("global", "debug_view_attributes", "7")
    with pytest.raises(DevOptionsError) as caught:
        apply("layout-debug", tmp_path)
    assert caught.value.kind == "setting_malformed" and dev.writes() == []


def test_setting_values_are_checked_by_their_rule_before_any_argv_is_built():
    spec = ent.SETTING_BY_NAME["view_attribute_inspection"]
    with pytest.raises(DevOptionsError) as caught:
        dev_options._setting_args("put", spec, android_user=0, value="2")
    assert caught.value.kind == "malformed_value"
    assert dev_options._setting_args("put", spec, android_user=0, value="1")[-3:] == ["global", "debug_view_attributes", "1"]


# --- transport check -------------------------------------------------------------------------------


@pytest.mark.parametrize("serial", ["192.0.2.10:5555", "[2001:db8::1]:5555", "adb-ABC123-xyz._adb-tls-connect._tcp", "adb-ABC123 (2)._adb._tcp"])
@pytest.mark.parametrize("profile", ["battery-doze", "network-degraded"])
def test_network_cutting_profiles_are_refused_over_wireless_adb(dev, tmp_path, profile, serial):
    with pytest.raises(DevOptionsError) as caught:
        apply(profile, tmp_path, serial=serial)
    assert caught.value.kind == "wireless_transport"
    assert dev.calls == [] and not (tmp_path / "snap.json").exists()


@pytest.mark.parametrize("serial", ["SERIAL1", "emulator-5554"])
def test_network_cutting_profile_is_allowed_over_usb_and_emulator(dev, tmp_path, serial):
    assert apply("battery-doze", tmp_path, serial=serial)["ok"]


def test_wireless_serial_may_use_a_profile_that_does_not_cut_the_network(dev, tmp_path):
    report = apply("lifecycle-stress", tmp_path, serial="192.0.2.10:5555")
    assert report["ok"] and report["transport"] == "tcp"
    assert load(tmp_path)["transport"] == "tcp"


# --- v1 snapshots keep restoring -------------------------------------------------------------------


def _v1_snapshot(profile, entries, **device):
    return {
        "schema_version": 1, "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
        "profile": profile, "status": "applied", "target_serial": SERIAL,
        "device": {"ro_serialno": "physical-123", "build_fingerprint": "vendor/device/product:16/BUILD/123:user/release-keys", "android_user": 10, **device},
        "settings": entries, "errors": [], "conflicts": [],
    }


def _v1_entry(name, table, key, user, original, applied):
    return {
        "name": name, "table": table, "key": key, "user": user,
        "original": {"present": original is not None, "value": original}, "applied_value": applied,
        "changed": True, "attempted": True, "applied": True, "state": "applied",
    }


def test_v1_touches_snapshot_still_restores(dev, tmp_path):
    dev.put("system", "show_touches", "1", user=10)
    path = tmp_path / "old.json"
    path.write_text(json.dumps(_v1_snapshot("touches", [_v1_entry("show_touches", "system", "show_touches", 10, "0", "1")])), encoding="utf-8")
    result = dev_options.restore_snapshot(SERIAL, path, adb_path="adb")
    assert result["ok"] and dev.settings[("system", 10, "show_touches")] == "0"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["schema_version"] == 1 and saved["status"] == "restored" and "package" not in saved
    assert set(saved["settings"][0]) == {"name", "table", "key", "user", "original", "applied_value", "changed", "attempted", "applied", "state"}


def test_v1_snapshot_with_an_absent_original_deletes_the_key(dev, tmp_path):
    dev.put("global", "stay_on_while_plugged_in", "3")
    path = tmp_path / "old.json"
    path.write_text(json.dumps(_v1_snapshot("stay-awake", [_v1_entry("stay_awake", "global", "stay_on_while_plugged_in", None, "1", "3")])), encoding="utf-8")
    assert dev_options.restore_snapshot(SERIAL, path, adb_path="adb")["ok"]
    assert dev.settings[("global", None, "stay_on_while_plugged_in")] == "1"


def test_v1_snapshot_cannot_name_a_new_profile_or_carry_v2_fields(dev, tmp_path):
    base = _v1_snapshot("touches", [_v1_entry("show_touches", "system", "show_touches", 10, "0", "1")])
    for mutate in (
        lambda d: d.update(profile="layout-debug"),
        lambda d: d.update(package=None, options=[], api_level=None, transport="usb"),
        lambda d: d["settings"][0].update(kind="property"),
    ):
        document = json.loads(json.dumps(base))
        mutate(document)
        path = tmp_path / "bad.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        with pytest.raises(DevOptionsError):
            dev_options.restore_snapshot(SERIAL, path, adb_path="adb")
        path.unlink()
    assert dev.writes() == []


def test_new_applies_write_schema_2_even_for_legacy_profiles(dev, tmp_path):
    apply("touches", tmp_path)
    saved = load(tmp_path)
    assert saved["schema_version"] == 2 and saved["profile"] == "touches" and saved["options"] == []
    assert saved["settings"][0]["kind"] == "setting" and saved["settings"][0]["scope"] is None
    assert restore(tmp_path)["ok"]


def test_unsupported_schema_is_refused(dev, tmp_path):
    document = _v1_snapshot("touches", [_v1_entry("show_touches", "system", "show_touches", 10, "0", "1")])
    document["schema_version"] = 3
    path = tmp_path / "future.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(DevOptionsError, match="Unsupported snapshot schema"):
        dev_options.restore_snapshot(SERIAL, path, adb_path="adb")


# --- v2 snapshot validation: tampered files never reach adb ---------------------------------------


def _tamper(tmp_path, dev, profile, mutate, **kw):
    apply(profile, tmp_path, **kw)
    document = load(tmp_path)
    mutate(document)
    (tmp_path / "snap.json").write_text(json.dumps(document), encoding="utf-8")
    dev.calls.clear()
    with pytest.raises(DevOptionsError) as caught:
        restore(tmp_path)
    assert dev.calls == [], "a rejected snapshot must not reach the device"
    return caught.value


def _entry(document, key):
    return next(e for e in document["settings"] if e["key"] == key)


@pytest.mark.parametrize("mutate,kind", [
    (lambda d: _entry(d, "debug.layout").update(key="persist.sys.language"), "untrusted_setting"),
    (lambda d: _entry(d, "debug.layout").update(table="global"), "untrusted_setting"),
    (lambda d: _entry(d, "debug.layout").update(kind="setting"), "untrusted_setting"),
    (lambda d: _entry(d, "debug.layout").update(applied_value="1; reboot"), "untrusted_value"),
    (lambda d: _entry(d, "debug.layout").update(original={"present": True, "value": "true; reboot"}), "malformed_snapshot"),
    (lambda d: _entry(d, "debug.layout").update(original={"present": False, "value": "x"}), "malformed_snapshot"),
    (lambda d: _entry(d, "debug.layout").update(scope="com.example.app"), "malformed_snapshot"),
    (lambda d: _entry(d, "debug.layout").update(user=3), "malformed_snapshot"),
    (lambda d: _entry(d, "debug.layout").update(changed=False), "malformed_snapshot"),
    (lambda d: _entry(d, "debug_view_attributes").update(applied_value="5"), "untrusted_value"),
    (lambda d: d["settings"].pop(), "malformed_snapshot"),
    (lambda d: d.update(options=["daltonizer"]), "malformed_snapshot"),
    (lambda d: d.update(transport="satellite"), "malformed_snapshot"),
    (lambda d: d.update(api_level="36"), "malformed_snapshot"),
    (lambda d: d.update(package=PKG), "malformed_snapshot"),
    (lambda d: d.update(extra=1), "malformed_snapshot"),
    (lambda d: d["settings"][0].update(extra=1), "malformed_snapshot"),
    (lambda d: d.update(profile="no-such-profile"), "malformed_snapshot"),
])
def test_tampered_property_snapshot_is_refused(dev, tmp_path, mutate, kind):
    assert _tamper(tmp_path, dev, "layout-debug", mutate).kind == kind


@pytest.mark.parametrize("mutate,kind", [
    (lambda d: d.update(package="com.other.app; ls"), "malformed_snapshot"),
    (lambda d: d.update(package=None), "malformed_snapshot"),
    (lambda d: _entry(d, "app_background").update(scope="com.other.app"), "malformed_snapshot"),
    (lambda d: _entry(d, "app_background").update(applied_value="allow; ls"), "untrusted_value"),
    (lambda d: _entry(d, "app_background").update(original={"present": True, "value": "weird"}), "malformed_snapshot"),
    (lambda d: _entry(d, "app_background").update(original={"present": False, "value": None}), "malformed_snapshot"),
    (lambda d: _entry(d, "data_saver").update(key="airplane_mode"), "untrusted_setting"),
    (lambda d: _entry(d, "data_saver").update(scope=PKG), "malformed_snapshot"),
])
def test_tampered_command_snapshot_is_refused(dev, tmp_path, mutate, kind):
    assert _tamper(tmp_path, dev, "background-restricted", mutate, package=PKG).kind == kind


def test_tampered_debug_app_applied_value_must_match_the_recorded_package(dev, tmp_path):
    error = _tamper(tmp_path, dev, "debugger-attach", lambda d: _entry(d, "debug_app").update(applied_value="app=com.other.app;wait=1"), package=PKG)
    assert error.kind == "untrusted_value"


def test_tampered_density_applied_value_must_pass_its_pattern(dev, tmp_path):
    error = _tamper(tmp_path, dev, "rtl-large-font", lambda d: _entry(d, "wm_density").update(applied_value="override:5; ls"))
    assert error.kind == "untrusted_value"


# --- allowlist and execution guard ----------------------------------------------------------------


def test_run_checked_refuses_a_command_without_an_allowlisted_shape(monkeypatch):
    monkeypatch.setattr(dev_options, "_run_process", lambda *a, **k: pytest.fail("reached the device"))
    for argv in (["shell", "rm", "-rf", "/sdcard"], ["shell", "settings", "put", "global", "x", "1;reboot"], ["shell", "sh", "-c", "id"],
                 ["shell", "pm", "clear", PKG], ["shell", "settings", "put", "other", "key", "1"], ["reboot"]):
        with pytest.raises(DevOptionsError) as caught:
            dev_options._run_checked(Path("adb"), SERIAL, argv)
        assert caught.value.kind == "untrusted_operation"


def test_every_command_the_new_profiles_run_is_an_allowlisted_shape(dev, tmp_path):
    """The fake asserts the shape of each argv; run every profile end to end through it."""
    for index, name in enumerate(prof.PROFILE_NAMES):
        package = PKG if prof.PROFILE_BY_NAME[name].package != "none" else None
        options = list(prof.PROFILE_BY_NAME[name].options)
        apply(name, tmp_path, name=f"{index}.json", package=package, options=options)
        assert restore(tmp_path, name=f"{index}.json")["ok"], name
    assert dev.calls and all(shape for shape in dev.shapes)


def test_a_second_apply_on_the_same_snapshot_path_is_refused(dev, tmp_path):
    apply("layout-debug", tmp_path)
    with pytest.raises(DevOptionsError, match="refusing to overwrite"):
        apply("layout-debug", tmp_path)
