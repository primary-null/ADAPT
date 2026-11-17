"""Read-only device detection, the profile listing and the page-size check (synthetic values only)."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from adapt_adb import dev_detect, dev_options
from adapt_adb.cli import app
from adapt_adb.dev_options import DevOptionsError
from tests.fake_device import FakeDevice

SERIAL = "SERIAL1"
runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(dev_options, "preflight_adb_server", lambda *a, **k: None)
    monkeypatch.setattr(dev_options, "resolve_adb", lambda: __import__("pathlib").Path("adb"))
    monkeypatch.setattr(dev_options, "_LOCK_ROOT", tmp_path / "locks")


@pytest.fixture
def make(monkeypatch):
    def build(**kw) -> FakeDevice:
        props = kw.pop("props", {})
        device = FakeDevice(**kw)
        device.props.update(props)
        monkeypatch.setattr(dev_options, "_run_process", device)
        return device
    return build


# --- pure parsers ---------------------------------------------------------------------------------


@pytest.mark.parametrize("maker,brand,expected", [
    ("Google", "google", "pixel"), ("google", "GOOGLE", "pixel"), ("samsung", "samsung", "samsung"), ("SAMSUNG", "other", "samsung"),
    ("other", "samsung", "samsung"), ("Google", "other", "other"), ("ExampleMaker", "examplebrand", "other"), ("", "", "other"),
])
def test_classify_oem(maker, brand, expected):
    assert dev_detect.classify_oem(maker, brand) == expected


@pytest.mark.parametrize("raw,version,major", [
    ("60000", "6.0", 6), ("60101", "6.1.1", 6), ("80500", "8.5", 8), ("70000", "7.0", 7), ("90000", "9.0", 9), ("100100", "10.1", 10),
])
def test_one_ui_property_decoding(raw, version, major):
    assert dev_detect.parse_oneui(raw) == {"version": version, "major": major}


@pytest.mark.parametrize("raw", ["", "abc", "600", "6.1", "-60000", "6000000", "60000\n1"])
def test_one_ui_property_rejects_other_shapes(raw):
    assert dev_detect.parse_oneui(raw) is None


def test_device_admin_keeps_counts_only():
    text = (
        "Device Owner: \n  admin=ComponentInfo{com.example.mdm/com.example.mdm.Admin}\n"
        "Profile Owner (User 10):\n  admin=ComponentInfo{com.example.work/com.example.work.Admin}\n"
        "Enabled Device Admins (User 0, provisioningState: 0):\n  com.example.mdm/com.example.mdm.Admin:\n    uid=1\n"
        "  com.example.other/com.example.other.Admin:\n    uid=2\nOther section:\n"
    )
    admin = dev_detect._device_admin(text)
    assert admin["device_owner"] is True and admin["profile_owner"] is True and admin["active_admins"] == 2 and admin["present"]
    assert "example" not in json.dumps(admin)
    empty = dev_detect._device_admin("Device policy state:\n")
    assert empty["present"] is False and empty["active_admins"] == 0


# --- detect() through the fake shell --------------------------------------------------------------


def _detect(device_args):
    return dev_options._reader(__import__("pathlib").Path("adb"), SERIAL)


def test_detect_pixel_reports_build_id_and_soc(make):
    make(manufacturer="Google", brand="google", props={"ro.build.id": "AP1A.000000.000", "ro.soc.model": "ExampleSoC"})
    detection = dev_detect.detect(_detect(None), transport="usb")
    assert detection["oem"] == "pixel" and detection["pixel"] == {
        "build_id": "AP1A.000000.000", "soc_model": "ExampleSoC", "note": detection["pixel"]["note"]}
    assert detection["samsung"] is None and detection["guidance"] == []
    assert detection["api_level"] == 36 and detection["build_type"] == "user" and detection["page_size"] == 4096


@pytest.mark.parametrize("oneui,version,blocker", [("60101", "6.1.1", True), ("80500", "8.5", True), ("50100", "5.1", False)])
def test_detect_samsung_one_ui_from_the_property(make, oneui, version, blocker):
    make(manufacturer="samsung", brand="samsung", props={"ro.build.version.oneui": oneui})
    detection = dev_detect.detect(_detect(None), transport="usb")
    samsung = detection["samsung"]
    assert detection["oem"] == "samsung" and samsung["version"] == version and samsung["source"] == "ro.build.version.oneui"
    assert samsung["auto_blocker_applies"] is blocker and "not documented by Samsung" in samsung["note"]
    assert bool(detection["guidance"]) is blocker
    assert detection["pixel"] is None


def test_detect_samsung_falls_back_to_sep_and_sem_then_to_the_android_version(make):
    make(manufacturer="samsung", brand="samsung", sdk="34", props={"ro.build.version.sep": "140100", "ro.build.version.sem": "3401"})
    samsung = dev_detect.detect(_detect(None), transport="usb")["samsung"]
    assert samsung["version"] is None and samsung["major"] is None and samsung["source"] is None
    assert samsung["fallback_sep"] == "140100" and samsung["fallback_sem"] == "3401"
    assert samsung["estimated_major"] == 6 and samsung["auto_blocker_applies"] is True


def test_detect_samsung_with_an_old_android_does_not_claim_auto_blocker(make):
    make(manufacturer="samsung", brand="samsung", sdk="33")
    samsung = dev_detect.detect(_detect(None), transport="usb")["samsung"]
    assert samsung["estimated_major"] == 5 and samsung["auto_blocker_applies"] is False


def test_detect_other_maker_has_no_vendor_blocks(make):
    make(props={"ro.build.characteristics": "tablet,nosdcard"})
    device = make()
    device.props["ro.build.characteristics"] = "tablet,nosdcard"
    device.page_size = "16384"
    detection = dev_detect.detect(_detect(None), transport="tcp", emulator=True)
    assert detection["oem"] == "other" and detection["samsung"] is None and detection["pixel"] is None
    assert detection["page_size"] == 16384 and detection["form_factor"]["tablet"] is True
    assert detection["form_factor"]["hinge_sensor"] is True and detection["form_factor"]["picture_in_picture"] is True
    assert detection["form_factor"]["tv"] is False
    assert detection["transport"] == "tcp" and detection["emulator_hint"] is True


def test_detect_lists_what_could_not_be_read(make):
    device = make()
    device.fail = lambda shell: "Permission denial: nope" if shell[1:3] in (["dumpsys", "device_policy"], ["getconf", "PAGE_SIZE"]) else None
    detection = dev_detect.detect(_detect(None), transport="usb")
    assert detection["device_admin"] is None and detection["page_size"] is None
    assert {item["item"] for item in detection["unread"]} == {"device_policy", "page_size"}
    assert all(item["status"] == "denied" for item in detection["unread"])


def test_detect_ignores_unexpected_property_text(make):
    make(props={"ro.build.type": "weird build", "ro.product.manufacturer": "bad;maker"})
    detection = dev_detect.detect(_detect(None), transport="usb")
    assert detection["build_type"] is None and detection["manufacturer"] is None and detection["oem"] == "other"


def test_detection_only_reads(make):
    device = make(manufacturer="samsung", brand="samsung")
    dev_detect.detect(_detect(None), transport="usb")
    assert device.writes() == [] and device.calls


# --- inspect --------------------------------------------------------------------------------------


def test_inspect_report_carries_the_detection_block(make):
    make(manufacturer="Google", brand="google", props={"ro.build.id": "AP1A.000000.000"})
    report = dev_options.inspect_device(SERIAL, adb_path="adb")
    assert report["detection"]["oem"] == "pixel" and report["detection"]["pixel"]["build_id"] == "AP1A.000000.000"
    assert len(report["settings"]) == 13 and "catalog_reads" not in report


def test_inspect_detection_can_be_switched_off(make):
    device = make()
    report = dev_options.inspect_device(SERIAL, adb_path="adb", detect=False)
    assert "detection" not in report and ["shell", "getconf", "PAGE_SIZE"] not in device.calls


def test_inspect_json_is_one_clean_document(make):
    make(manufacturer="samsung", brand="samsung", props={"ro.build.version.oneui": "60101"})
    result = runner.invoke(app, ["dev-options", "inspect", "-s", SERIAL, "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["detection"]["samsung"]["version"] == "6.1.1" and "\x1b[" not in result.stdout
    assert result.stdout.strip().startswith("{") and result.stdout.strip().endswith("}")


def test_inspect_text_shows_detection_and_the_auto_blocker_note(make):
    make(manufacturer="samsung", brand="samsung", props={"ro.build.version.oneui": "60101"})
    result = runner.invoke(app, ["dev-options", "inspect", "-s", SERIAL])
    assert result.exit_code == 0
    assert "maker layer samsung" in result.stdout and "One UI 6.1.1 (undocumented by Samsung)" in result.stdout
    assert "Security and privacy > Auto Blocker" in result.stdout


def test_inspect_error_json_stays_one_document(monkeypatch):
    def deny(args, timeout):
        raise dev_options.AdbCommandError("denied", "Permission denial")

    monkeypatch.setattr(dev_options, "_run_process", deny)
    result = runner.invoke(app, ["dev-options", "inspect", "-s", SERIAL, "--json"])
    assert result.exit_code == 1 and json.loads(result.stdout)["ok"] is False


# --- profiles listing -----------------------------------------------------------------------------


def test_profiles_report_lists_every_profile_with_eligibility(make):
    device = make(manufacturer="samsung", brand="samsung", sdk="31", props={"ro.build.version.oneui": "40100"})
    report = dev_options.profiles_report(SERIAL, adb_path="adb")
    assert device.writes() == []
    assert report["detection"]["oem"] == "samsung" and report["detection"]["api_level"] == 31
    by_name = {item["name"]: item for item in report["profiles"]}
    assert len(by_name) == 14
    assert by_name["layout-debug"]["eligible"] and by_name["fast-ui"]["eligible"]
    assert by_name["network-degraded"]["eligible"] is False
    assert by_name["network-degraded"]["reasons"][0]["code"] == "no_steps_apply"
    assert by_name["debugger-attach"]["needs_package"] is True and by_name["debugger-attach"]["package"] == "required"
    assert by_name["rtl-large-font"]["warnings"] and "clamp" in by_name["rtl-large-font"]["warnings"][0]
    assert {"entry": "app_locale", "reason": "needs_api_33"} in by_name["rtl-large-font"]["skipped_steps"] or by_name["rtl-large-font"]["package"] == "optional"
    json.dumps(report)


def test_profiles_report_refuses_network_cutting_profiles_over_wireless(make):
    make()
    report = dev_options.profiles_report("192.0.2.10:5555", adb_path="adb")
    by_name = {item["name"]: item for item in report["profiles"]}
    assert report["detection"]["transport"] == "tcp"
    for name in ("battery-doze", "network-degraded"):
        assert by_name[name]["eligible"] is False and by_name[name]["reasons"][0]["code"] == "wireless_transport"
    assert by_name["lifecycle-stress"]["eligible"]


def test_profiles_report_gates_by_api(make):
    make(sdk="28")
    by_name = {item["name"]: item for item in dev_options.profiles_report(SERIAL, adb_path="adb")["profiles"]}
    assert by_name["layout-debug"]["reasons"][0]["code"] == "api_too_low" and by_name["fast-ui"]["eligible"]
    make(sdk="40")
    by_name = {item["name"]: item for item in dev_options.profiles_report(SERIAL, adb_path="adb")["profiles"]}
    assert by_name["layout-debug"]["reasons"][0]["code"] == "api_not_researched"


def test_profiles_report_with_a_package_marks_per_app_steps(make):
    make()
    by_name = {item["name"]: item for item in dev_options.profiles_report(SERIAL, adb_path="adb", package="com.example.app")["profiles"]}
    assert by_name["debugger-attach"]["needs_package"] is False
    assert by_name["fast-ui"]["package"] == "none" and by_name["fast-ui"]["eligible"]


def test_profiles_report_rejects_an_invalid_package_before_contacting_the_device(monkeypatch):
    monkeypatch.setattr(dev_options, "_run_process", lambda *a, **k: pytest.fail("device reached"))
    with pytest.raises(DevOptionsError) as caught:
        dev_options.profiles_report(SERIAL, adb_path="adb", package="bad name")
    assert caught.value.kind == "invalid_package"


def test_profiles_command_json_and_text(make):
    make(manufacturer="Google", brand="google")
    result = runner.invoke(app, ["dev-options", "profiles", "-s", SERIAL, "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["command"] == "dev-options profiles" and len(payload["profiles"]) == 14
    for item in payload["profiles"]:
        assert {"name", "description", "min_api", "max_api", "oem", "package", "needs_reboot", "needs_app_restart", "needs_effect_check",
                "restore_exactness", "eligible", "reasons", "warnings", "options", "mechanisms"} <= set(item)
    text = runner.invoke(app, ["dev-options", "profiles", "-s", SERIAL])
    assert text.exit_code == 0 and "layout-debug: eligible" in text.stdout and "needs --package" in text.stdout


def test_profiles_command_error_is_json_when_asked(monkeypatch):
    def timeout(args, timeout):
        raise dev_options.AdbCommandError("timeout", "adb command timed out.")

    monkeypatch.setattr(dev_options, "_run_process", timeout)
    result = runner.invoke(app, ["dev-options", "profiles", "-s", SERIAL, "--json"])
    assert result.exit_code == 1 and json.loads(result.stdout)["error"] == "timeout"


# --- pixel-16kb-check -----------------------------------------------------------------------------


def test_page_size_check_on_a_4kb_pixel_points_to_the_emulator_and_warns_about_the_wipe(make):
    device = make(manufacturer="Google", brand="google", props={"ro.build.id": "AP1A.000000.000", "ro.soc.model": "ExampleSoC"})
    report = dev_options.pixel_16kb_check(SERIAL, adb_path="adb")
    assert device.writes() == []
    assert report["page_size"] == 4096 and report["uses_16kb_pages"] is False and report["oem"] == "pixel"
    assert report["pixel"]["build_id"] == "AP1A.000000.000"
    text = " ".join(report["guidance"])
    assert "emulator" in text and "developer option" in text and "page-sizes" in text
    assert "unlocked bootloader" in report["never_automated"][0] and "wipes the device" in report["never_automated"][0]
    assert "says nothing about whether an app works" in report["claim_boundary"]


def test_page_size_check_on_a_16kb_device_says_to_test_on_it(make):
    device = make()
    device.page_size = "16384"
    report = dev_options.pixel_16kb_check(SERIAL, adb_path="adb")
    assert report["uses_16kb_pages"] is True and "16 KB" in report["guidance"][0]
    assert any("not available on this maker layer" in line for line in report["guidance"])


def test_page_size_check_when_unreadable(make):
    device = make()
    device.page_size = "not a number"
    report = dev_options.pixel_16kb_check(SERIAL, adb_path="adb")
    assert report["page_size"] is None and report["uses_16kb_pages"] is None and "getconf PAGE_SIZE" in report["guidance"][0]


def test_page_size_check_command_json_and_text_are_clean(make):
    make(manufacturer="Google", brand="google")
    result = runner.invoke(app, ["dev-options", "pixel-16kb-check", "-s", SERIAL, "--json"])
    assert result.exit_code == 0 and json.loads(result.stdout)["command"] == "dev-options pixel-16kb-check"
    text = runner.invoke(app, ["dev-options", "pixel-16kb-check", "-s", SERIAL])
    assert text.exit_code == 0 and "Page size: 4096 bytes" in text.stdout and "Never automated:" in text.stdout


# --- MCP: the read-only inspect tool carries the detection block ---------------------------------


def test_mcp_developer_options_inspect_returns_detection_and_stays_read_only(make):
    from adapt_adb.mcp import access, tools

    device = make(manufacturer="samsung", brand="samsung", props={"ro.build.version.oneui": "70000"})
    payload = tools.tool_developer_options_inspect(SERIAL)
    assert payload["detection"]["oem"] == "samsung" and payload["detection"]["samsung"]["version"] == "7.0"
    assert payload["detection"]["guidance"] and "catalog_reads" not in payload
    assert device.writes() == []
    assert "developer_options_inspect" in access.OBSERVATION_TOOLS and "developer_options_inspect" not in access.MUTATING_TOOLS
