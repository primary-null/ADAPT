"""The curated Developer Options catalog: schema, filters, the offline CLI and bounded `inspect --all` reads."""

from __future__ import annotations

import json
import re
import subprocess

import pytest
from typer.testing import CliRunner

from adapt_adb import dev_catalog, dev_options
from adapt_adb import dev_profiles as prof
from adapt_adb.cli import app
from tests.fake_device import FakeDevice

runner = CliRunner()
SERIAL = "SERIAL1"


@pytest.fixture(scope="module")
def catalog():
    document = dev_catalog.load_catalog()
    assert document.get("ok") is not False
    return document


# --- schema of the packaged file -------------------------------------------------------------------


def test_catalog_top_level_and_counts(catalog):
    assert set(catalog) == {"schema_version", "researched", "scope", "source_note", "field_notes", "counts", "entries"}
    assert catalog["schema_version"] == 1 and catalog["researched"] == "2026-11-16"
    assert "Nothing in this catalog was run on a device" in catalog["source_note"]
    entries = catalog["entries"]
    assert len(entries) == catalog["counts"]["entries"] == 176
    by_class = {}
    for entry in entries:
        by_class[entry["adapt_class"]] = by_class.get(entry["adapt_class"], 0) + 1
    assert by_class == catalog["counts"]["by_class"] == {"inspect": 31, "profile": 95, "ui_only": 43, "dangerous": 7}


def test_every_entry_has_exactly_the_documented_fields_and_valid_enums(catalog):
    ids = set()
    for entry in catalog["entries"]:
        assert tuple(entry) == dev_catalog.ENTRY_FIELDS, entry["id"]
        assert entry["id"] not in ids and re.fullmatch(r"(aosp|pixel|samsung|adb)\.[a-z0-9_]+", entry["id"])
        ids.add(entry["id"])
        assert entry["layer"] in dev_catalog.LAYERS and entry["adapt_class"] in dev_catalog.CLASSES
        assert entry["confidence"] in dev_catalog.CONFIDENCE and entry["needs_reboot"] in dev_catalog.NEEDS_REBOOT
        assert entry["automation"] in dev_catalog.AUTOMATION
        assert isinstance(entry["min_api"], int) and 29 <= entry["min_api"] <= 37
        assert entry["max_api"] is None or (isinstance(entry["max_api"], int) and entry["min_api"] <= entry["max_api"] <= 37)
        assert isinstance(entry["sources"], list) and entry["sources"] and all(s.startswith("https://") for s in entry["sources"])
        assert isinstance(entry["testing_use"], list) and isinstance(entry["automated_by"], list)
        for field in ("ui_name", "section", "mechanism", "value_type"):
            assert isinstance(entry[field], str) and entry[field], (entry["id"], field)
        assert isinstance(entry["oem_notes"], str)
        for field in ("read_cmd", "write_cmd", "restore_cmd", "guidance"):
            assert entry[field] is None or isinstance(entry[field], str)


def test_automation_is_consistent_with_the_class(catalog):
    expected = {"inspect": {"read_only"}, "profile": {"adapt_profile", "reference_only"}, "ui_only": {"guidance_only"}, "dangerous": {"never_automate"}}
    for entry in catalog["entries"]:
        assert entry["automation"] in expected[entry["adapt_class"]], entry["id"]
        if entry["adapt_class"] in ("ui_only", "dangerous"):
            assert entry["write_cmd"] is None and entry["restore_cmd"] is None and entry["guidance"], entry["id"]
        if entry["adapt_class"] == "inspect":
            assert entry["write_cmd"] is None
        assert (entry["automation"] == "adapt_profile") == bool(entry["automated_by"]), entry["id"]
        if entry["automation"] == "reference_only":
            assert "not run by ADAPT" in entry["guidance"]


def test_privileged_entries_are_left_out(catalog):
    assert all(entry["adapt_class"] != "privileged" for entry in catalog["entries"])
    assert "pixel.aconfig_flags" not in {entry["id"] for entry in catalog["entries"]}


def test_automated_by_names_real_profiles_and_every_new_profile_is_documented(catalog):
    named = {name for entry in catalog["entries"] for name in entry["automated_by"]}
    assert named <= set(prof.PROFILE_NAMES)
    assert named >= set(prof.PROFILE_NAMES), "every profile appears on at least one catalog entry"


def test_catalog_contains_no_private_data(catalog):
    text = json.dumps(catalog)
    assert not re.search(r"[A-Za-z]:\\\\|/home/|/Users/", text)
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text)


def test_guidance_for_the_samsung_and_dangerous_entries(catalog):
    by_id = {entry["id"]: entry for entry in catalog["entries"]}
    blocker = by_id["samsung.auto_blocker"]
    assert blocker["automation"] == "guidance_only" and "Security and privacy" in blocker["guidance"] and "One UI 6" in blocker["guidance"]
    assert "Do this on the device" in by_id["aosp.background_process_limit"]["guidance"]
    for dangerous in ("aosp.oem_unlock", "aosp.revoke_usb_authorizations", "pixel.boot_16kb_page_size", "samsung.service_code_usb_settings",
                      "aosp.convert_to_fbe", "adb.pm_clear", "adb.lock_settings"):
        assert by_id[dangerous]["automation"] == "never_automate" and by_id[dangerous]["guidance"].startswith("Never automate"), dangerous
    assert "wipe" in by_id["pixel.boot_16kb_page_size"]["guidance"]
    for readonly in ("aosp.usb_debugging", "aosp.wireless_debugging"):
        assert by_id[readonly]["automation"] == "read_only" and "never changes" in by_id[readonly]["guidance"]


def test_unused_profile_commands_are_marked_reference_only(catalog):
    by_id = {entry["id"]: entry for entry in catalog["entries"]}
    assert by_id["aosp.show_refresh_rate"]["automation"] == "reference_only" and by_id["aosp.show_layout_bounds"]["automation"] == "adapt_profile"


# --- queries --------------------------------------------------------------------------------------


def test_report_filters_by_layer_and_api(catalog):
    report = dev_catalog.catalog_report(layer="samsung")
    assert report["ok"] and {e["layer"] for e in report["entries"]} == {"samsung"}
    assert report["counts"]["entries"] == len(report["entries"]) == 15
    low = dev_catalog.catalog_report(api=29)["entries"]
    high = dev_catalog.catalog_report(api=37)["entries"]
    assert all(e["min_api"] <= 29 for e in low) and all(e["max_api"] in (None,) or e["max_api"] >= 37 for e in high)
    assert len(low) != len(high)
    assert "aosp.show_key_presses" in {e["id"] for e in dev_catalog.catalog_report(api=36)["entries"]}
    assert "aosp.show_key_presses" not in {e["id"] for e in low}
    both = dev_catalog.catalog_report(layer="adb_control", api=34)
    assert {e["layer"] for e in both["entries"]} == {"adb_control"} and both["api"] == 34


def test_report_refuses_an_unknown_layer():
    report = dev_catalog.catalog_report(layer="oneplus")
    assert report["ok"] is False and report["error"] == "invalid_layer"


def test_catalog_is_offline(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("the catalog must not run a process"))
    monkeypatch.setattr(dev_options, "_run_process", lambda *a, **k: pytest.fail("the catalog must not reach a device"))
    assert dev_catalog.catalog_report()["ok"]
    result = runner.invoke(app, ["dev-options", "catalog", "--json"])
    assert result.exit_code == 0


def test_catalog_command_json_is_one_clean_document():
    result = runner.invoke(app, ["dev-options", "catalog", "--json", "--layer", "pixel", "--api", "36"])
    assert result.exit_code == 0 and "\x1b[" not in result.stdout
    payload = json.loads(result.stdout)
    assert payload["layer"] == "pixel" and payload["api"] == 36 and payload["command"] == "dev-options catalog"
    assert {e["layer"] for e in payload["entries"]} == {"pixel"}


def test_catalog_command_text_groups_by_class():
    result = runner.invoke(app, ["dev-options", "catalog", "--layer", "samsung"])
    assert result.exit_code == 0
    assert "Do this on the device (ADAPT cannot change it)" in result.stdout and "Never automated" in result.stdout
    assert "samsung.auto_blocker" in result.stdout and "Security and privacy" in result.stdout


def test_catalog_command_rejects_an_unknown_layer_in_both_modes():
    text = runner.invoke(app, ["dev-options", "catalog", "--layer", "oneplus"])
    assert text.exit_code == 2 and "Layer must be one of" in text.stderr and text.stdout == ""
    machine = runner.invoke(app, ["dev-options", "catalog", "--layer", "oneplus", "--json"])
    assert machine.exit_code == 2 and json.loads(machine.stdout)["error"] == "invalid_layer"


# --- planned reads --------------------------------------------------------------------------------


def test_plan_reads_only_plans_allowlisted_read_shapes():
    plan = dev_catalog.plan_reads(36, "other", 10)
    assert plan
    from adapt_adb import safety

    for item in plan:
        if item["skipped"] is None:
            assert item["argvs"]
            for argv in item["argvs"]:
                shape = safety.match_dev_options_shell(["shell", *argv])
                assert shape is not None and not safety.dev_options_shape_writes(shape), (item["id"], argv)
    reasons = {item["skipped"] for item in plan}
    assert {"no_read_command", "pipe_not_allowed", "needs_package", "no_allowlisted_read"} <= reasons
    by_id = {item["id"]: item for item in plan}
    assert by_id["aosp.show_taps"]["argvs"] == [["settings", "--user", "10", "get", "system", "show_touches"]]
    assert by_id["adb.page_size"]["argvs"] == [["getconf", "PAGE_SIZE"]]
    assert by_id["aosp.show_layout_bounds"]["argvs"] == [["getprop", "debug.layout"]]
    assert by_id["adb.app_locale"]["skipped"] == "needs_package" and by_id["aosp.feature_flags"]["skipped"] == "pipe_not_allowed"


def test_plan_reads_filters_by_layer_and_api():
    other = {item["id"] for item in dev_catalog.plan_reads(36, "other", 0)}
    samsung = {item["id"] for item in dev_catalog.plan_reads(36, "samsung", 0)}
    pixel = {item["id"] for item in dev_catalog.plan_reads(36, "pixel", 0)}
    assert not any(i.startswith(("samsung.", "pixel.")) for i in other)
    assert any(i.startswith("samsung.") for i in samsung) and not any(i.startswith("pixel.") for i in samsung)
    assert any(i.startswith("pixel.") for i in pixel)
    assert "aosp.show_key_presses" not in {item["id"] for item in dev_catalog.plan_reads(34, "other", 0)}
    assert len(dev_catalog.plan_reads(None, None, 0)) >= len(other)


def test_bound_value_collapses_whitespace_and_cuts_at_the_limit():
    assert dev_catalog.bound_value(["a  b\n c", "d"]) == "a b c ; d"
    assert len(dev_catalog.bound_value(["x" * 10_000])) == dev_catalog.MAX_VALUE_CHARS
    assert dev_catalog.bound_value(["a\x00b\x1b[0m"]) == "ab[0m"


# --- inspect --all through the fake shell --------------------------------------------------------


@pytest.fixture
def phone(monkeypatch, tmp_path):
    monkeypatch.setattr(dev_options, "preflight_adb_server", lambda *a, **k: None)
    monkeypatch.setattr(dev_options, "resolve_adb", lambda: __import__("pathlib").Path("adb"))
    monkeypatch.setattr(dev_options, "_LOCK_ROOT", tmp_path / "locks")

    def build(**kw):
        props = kw.pop("props", {})
        device = FakeDevice(**kw)
        device.props.update(props)
        monkeypatch.setattr(dev_options, "_run_process", device)
        return device
    return build


def test_inspect_all_reads_allowlisted_entries_only_and_writes_nothing(phone):
    device = phone()
    device.put("system", "show_touches", "1", user=10)
    device.props["debug.layout"] = "true"
    report = dev_options.inspect_device(SERIAL, adb_path="adb", include_all=True)
    reads = report["catalog_reads"]
    assert device.writes() == []
    assert reads["api_level"] == 36 and reads["oem"] == "other"
    assert reads["counts"]["observed"] > 25 and reads["counts"]["skipped"] > 50
    by_id = {item["id"]: item for item in reads["entries"]}
    assert by_id["aosp.show_taps"] == {"id": "aosp.show_taps", "status": "observed", "reason": None, "value": "1"}
    assert by_id["aosp.show_layout_bounds"]["value"] == "true" and by_id["adb.page_size"]["value"] == "4096"
    assert by_id["adb.wm_density"]["value"].startswith("Physical density: 420")
    assert by_id["aosp.feature_flags"]["status"] == "skipped" and by_id["aosp.feature_flags"]["reason"] == "pipe_not_allowed"
    assert all(len(item["value"] or "") <= dev_catalog.MAX_VALUE_CHARS for item in reads["entries"])
    assert reads["bounds"]["per_command_seconds"] == dev_options.ADB_TIMEOUT_SECONDS


def test_inspect_all_respects_api_and_maker_layer(phone):
    phone(sdk="30", manufacturer="samsung", brand="samsung")
    reads = dev_options.inspect_device(SERIAL, adb_path="adb", include_all=True)["catalog_reads"]
    ids = {item["id"] for item in reads["entries"]}
    assert "aosp.show_key_presses" not in ids and "samsung.identity" in ids and not any(i.startswith("pixel.") for i in ids)


def test_inspect_all_records_a_failed_read_and_keeps_going(phone):
    device = phone()
    device.fail = lambda shell: "Permission denial: no" if shell[1:3] == ["getprop", "debug.layout"] else None
    reads = dev_options.inspect_device(SERIAL, adb_path="adb", include_all=True)["catalog_reads"]
    by_id = {item["id"]: item for item in reads["entries"]}
    assert by_id["aosp.show_layout_bounds"]["status"] == "error" and by_id["aosp.show_layout_bounds"]["reason"] == "denied"
    assert by_id["adb.page_size"]["status"] == "observed"


def test_inspect_all_stops_reading_when_the_time_budget_is_spent(phone, monkeypatch):
    phone()
    monkeypatch.setattr(dev_options, "CATALOG_READ_SECONDS", -1)
    reads = dev_options.inspect_device(SERIAL, adb_path="adb", include_all=True)["catalog_reads"]
    assert reads["counts"]["observed"] == 0 and reads["counts"]["not_read"] > 25
    assert {item["reason"] for item in reads["entries"] if item["status"] == "not_read"} == {"time_budget_reached"}


def test_inspect_all_command_json_is_one_clean_document(phone):
    phone()
    result = runner.invoke(app, ["dev-options", "inspect", "-s", SERIAL, "--all", "--json"])
    assert result.exit_code == 0 and "\x1b[" not in result.stdout
    payload = json.loads(result.stdout)
    assert payload["catalog_reads"]["counts"]["observed"] > 25 and "detection" in payload


def test_inspect_all_text_summarises_the_reads(phone):
    phone()
    result = runner.invoke(app, ["dev-options", "inspect", "-s", SERIAL, "--all"])
    assert result.exit_code == 0 and "Catalog reads:" in result.stdout and "adb.page_size: 4096" in result.stdout
