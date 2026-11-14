"""Android 10-17 capability matrix and bounded-inspection tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from adapt_adb import android_support
from adapt_adb.android_support import AndroidSupportError, android_app
from adapt_adb.doctor import UnsafeAdbServerError


EXPECTED_VERSIONS = {
    29: "10",
    30: "11",
    31: "12",
    32: "12L",
    33: "13",
    34: "14",
    35: "15",
    36: "16",
    37: "17",
}


@pytest.mark.parametrize("api,android_version", EXPECTED_VERSIONS.items())
def test_api_29_through_37_authoritative_mapping(api: int, android_version: str):
    report = android_support.evaluate_capabilities(api, android_version)

    assert report["platform"]["sdk"] == api
    assert report["platform"]["android_version"] == android_version
    assert report["platform"]["coverage"] == "covered_version"
    assert report["platform"]["release_status"] == "released"
    assert report["platform"]["release_major_matches_api"] is True
    assert not any(
        item["status"] in {"supported", "fully_supported"}
        for item in report["capabilities"].values()
    )


def test_android_17_is_released_api_37_but_not_runtime_certified():
    matrix = android_support.version_matrix()
    android_17 = next(item for item in matrix["versions"] if item["api"] == 37)

    assert android_17 == {
        "api": 37,
        "android": "17",
        "codename": "CINNAMON_BUN",
        "release_status": "released",
    }
    assert matrix["maximum_verified_api"] == 37
    assert "does not certify" in matrix["claim_boundary"]


def test_version_thresholds_distinguish_os_eligibility_and_oem_unknowns():
    api_29 = android_support.evaluate_capabilities(29, "10")
    api_30 = android_support.evaluate_capabilities(30, "11")
    api_37 = android_support.evaluate_capabilities(37, "17")

    assert api_29["capabilities"]["wireless_pairing"]["status"] == "unsupported_by_os"
    assert api_29["capabilities"]["scrcpy_audio"]["status"] == "unsupported_by_os"
    assert api_29["capabilities"]["legacy_tcp_adb"]["status"] == "version_eligible"
    assert api_30["capabilities"]["wireless_pairing"]["status"] == "version_eligible"
    assert api_30["capabilities"]["scrcpy_audio"]["status"] == "version_eligible"
    assert api_30["capabilities"]["adb_wifi_2"]["status"] == "unsupported_by_os"
    assert api_37["capabilities"]["adb_wifi_2"]["status"] == "version_eligible"
    for feature in (
        "existing_display_mirroring",
        "new_virtual_display",
        "input_routing",
        "developer_options_keys",
    ):
        assert api_37["capabilities"][feature]["status"] == "unknown_oem_dependent"


def test_older_and_future_sdk_levels_fail_closed_without_guessing():
    older = android_support.evaluate_capabilities(28, "9")
    future = android_support.evaluate_capabilities(38, "18")

    assert older["platform"]["coverage"] == "unsupported_baseline"
    assert older["capabilities"]["wireless_pairing"]["status"] == "unsupported_by_os"
    assert older["capabilities"]["screenshot"]["status"] == "unsupported_by_baseline"
    assert future["platform"]["coverage"] == "unknown_future_os"
    assert future["platform"]["android_version"] is None
    assert future["platform"]["release_status"] is None
    assert {
        item["status"] for item in future["capabilities"].values()
    } == {"unknown_future_os"}


@pytest.mark.parametrize("bad_sdk", ["", "abc", "29.0", " 29", "29\n30", True, 0])
def test_malformed_sdk_is_classified_without_fabricated_value(bad_sdk):
    with pytest.raises(AndroidSupportError) as exc_info:
        android_support.evaluate_capabilities(bad_sdk)

    assert exc_info.value.kind == "malformed_sdk"
    if str(bad_sdk):
        assert str(bad_sdk) not in str(exc_info.value)


def test_release_mismatch_is_advisory_and_not_rewritten():
    report = android_support.evaluate_capabilities(34, "13", "Example", "Model")

    assert report["platform"]["reported_release"] == "13"
    assert report["platform"]["android_version"] == "14"
    assert report["platform"]["release_major_matches_api"] is False
    assert len(report["warnings"]) == 1


def test_optional_runtime_evidence_is_separate_from_version_eligibility():
    report = android_support.evaluate_capabilities(
        35,
        "15",
        evidence={
            "screenshot": {"observed": True, "source": "fixture capture"},
            "new_virtual_display": False,
        },
    )

    screenshot = report["capabilities"]["screenshot"]
    virtual = report["capabilities"]["new_virtual_display"]
    assert screenshot["status"] == "observed_runtime_evidence"
    assert screenshot["runtime_observed"] is True
    assert screenshot["evidence_source"] == "fixture capture"
    assert virtual["status"] == "unknown_oem_dependent"
    assert virtual["runtime_observed"] is False


def _fake_getprops(
    expected_serial: str,
    *,
    sdk: str = "37",
    release: str = "17",
    manufacturer: str = "Example OEM",
    model: str = "Example Model",
):
    values = {
        "ro.build.version.sdk": sdk,
        "ro.build.version.release": release,
        "ro.product.manufacturer": manufacturer,
        "ro.product.model": model,
    }
    calls: list[list[str]] = []

    def run(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        assert timeout == android_support.ADB_TIMEOUT_SECONDS
        assert args[1:3] == ["-s", expected_serial]
        assert args[3:5] == ["shell", "getprop"]
        return subprocess.CompletedProcess(args, 0, values[args[5]] + "\n", "")

    return run, calls


@pytest.mark.parametrize("api,android_version", EXPECTED_VERSIONS.items())
def test_inspection_with_all_adb_and_host_probes_mocked_for_api_29_through_37(
    monkeypatch, api: int, android_version: str
):
    serial = f"fixture-api-{api}"
    fake_run, calls = _fake_getprops(serial, sdk=str(api), release=android_version)
    monkeypatch.setattr(android_support, "_run_process", fake_run)
    monkeypatch.setattr(android_support, "preflight_adb_server", lambda *a, **k: None)
    monkeypatch.setattr(
        android_support,
        "_probe_scrcpy",
        lambda: {
            "available": True,
            "version": "4.1.0",
            "version_parsed": True,
            "audio_client_eligible": True,
            "virtual_display_client_eligible": True,
        },
    )

    report = android_support.inspect_capabilities(serial, adb_path=Path("mock-adb"))

    assert report["platform"]["sdk"] == api
    assert report["platform"]["android_version"] == android_version
    assert report["host"]["scrcpy"]["available"] is True
    assert len(calls) == 4


def test_inspection_preserves_spaced_mdns_target_and_never_enumerates(monkeypatch):
    serial = "adb-ABC123-My Pixel 9._adb-tls-connect._tcp"
    fake_run, calls = _fake_getprops(serial)
    gate_calls: list[tuple[Path, list[str]]] = []
    monkeypatch.setattr(android_support, "_run_process", fake_run)
    monkeypatch.setattr(
        android_support,
        "preflight_adb_server",
        lambda path, args: gate_calls.append((path, list(args))),
    )
    monkeypatch.setattr(android_support, "_probe_scrcpy", lambda: {"available": False})

    report = android_support.inspect_capabilities(serial, adb_path=Path("mock-adb"))

    assert len(calls) == 4
    assert all(call[2] == serial for call in calls)
    assert all("devices" not in call for call in calls)
    assert gate_calls[0][1][0:2] == ["-s", serial]
    assert report["inspection"]["target_selection"] == "explicit"
    assert report["inspection"]["target_value_returned"] is False
    assert report["inspection"]["transport_kind"] == "unknown"
    assert report["platform"]["sdk"] == 37


def test_inspection_stops_before_adb_when_daemon_gate_is_unsafe(monkeypatch):
    run_mock = MagicMock()
    monkeypatch.setattr(android_support, "_run_process", run_mock)

    def reject(*args, **kwargs):
        raise UnsafeAdbServerError("raw unsafe detail")

    monkeypatch.setattr(android_support, "preflight_adb_server", reject)

    with pytest.raises(AndroidSupportError) as exc_info:
        android_support.inspect_capabilities("explicit-target", adb_path=Path("mock-adb"))

    assert exc_info.value.kind == "unsafe_adb_server"
    assert "raw unsafe detail" not in str(exc_info.value)
    run_mock.assert_not_called()


@pytest.mark.parametrize(
    "result,kind",
    [
        (subprocess.CompletedProcess([], 0, "", "Permission denial: secret detail"), "denied"),
        (subprocess.CompletedProcess([], 0, "error: secret detail", ""), "adb_error"),
        (subprocess.CompletedProcess([], 1, "", "device serial-secret offline"), "adb_error"),
    ],
)
def test_exit_zero_error_text_and_failures_are_generic(monkeypatch, result, kind):
    monkeypatch.setattr(android_support, "preflight_adb_server", lambda *a, **k: None)
    monkeypatch.setattr(android_support, "_run_process", lambda *a, **k: result)

    with pytest.raises(AndroidSupportError) as exc_info:
        android_support.inspect_capabilities("serial-secret", adb_path=Path("mock-adb"))

    assert exc_info.value.kind == kind
    assert "secret" not in str(exc_info.value)


def test_timeout_is_generic_and_does_not_probe_further(monkeypatch):
    calls = 0

    def timeout(args, seconds):
        nonlocal calls
        calls += 1
        raise subprocess.TimeoutExpired(args, seconds, output="serial-secret")

    monkeypatch.setattr(android_support, "preflight_adb_server", lambda *a, **k: None)
    monkeypatch.setattr(android_support, "_run_process", timeout)

    with pytest.raises(AndroidSupportError) as exc_info:
        android_support.inspect_capabilities("serial-secret", adb_path=Path("mock-adb"))

    assert exc_info.value.kind == "timeout"
    assert "serial-secret" not in str(exc_info.value)
    assert calls == 1


def test_inspection_rejects_malformed_sdk_after_mocked_property_reads(monkeypatch):
    fake_run, calls = _fake_getprops("target", sdk="thirty-seven")
    monkeypatch.setattr(android_support, "preflight_adb_server", lambda *a, **k: None)
    monkeypatch.setattr(android_support, "_run_process", fake_run)
    monkeypatch.setattr(android_support, "_probe_scrcpy", lambda: {"available": False})

    with pytest.raises(AndroidSupportError) as exc_info:
        android_support.inspect_capabilities("target", adb_path=Path("mock-adb"))

    assert exc_info.value.kind == "malformed_sdk"
    assert len(calls) == 4


@pytest.mark.parametrize(
    "output,expected",
    [
        ("scrcpy 1.25\n", (False, False)),
        ("scrcpy 2.4\n", (True, False)),
        ("scrcpy 3.3.4\n", (True, True)),
    ],
)
def test_host_scrcpy_probe_reports_availability_not_device_acceptance(monkeypatch, output, expected):
    monkeypatch.setattr(android_support.shutil, "which", lambda name: "/mock/scrcpy")
    monkeypatch.setattr(
        android_support,
        "_run_process",
        lambda args, timeout: subprocess.CompletedProcess(args, 0, output, ""),
    )

    report = android_support._probe_scrcpy()

    assert report["available"] is True
    assert (report["audio_client_eligible"], report["virtual_display_client_eligible"]) == expected
    assert "supported" not in report


def test_capabilities_cli_json_is_clean_and_contains_no_target(monkeypatch):
    report = android_support.evaluate_capabilities(33, "13", "OEM", "Model")
    report["command"] = "android capabilities"
    monkeypatch.setattr(android_support, "inspect_capabilities", lambda serial: report)

    result = CliRunner().invoke(android_app, ["capabilities", "-s", "spaced target", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["platform"]["sdk"] == 33
    assert "spaced target" not in result.stdout
    assert "\x1b[" not in result.stdout


def test_capabilities_cli_json_error_is_machine_readable_and_redacted(monkeypatch):
    def fail(serial: str):
        raise AndroidSupportError("adb_error", "ADB property inspection failed.")

    monkeypatch.setattr(android_support, "inspect_capabilities", fail)
    result = CliRunner().invoke(android_app, ["capabilities", "-s", "private target", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    # Failure guidance Phase 2 adds one additive `failure` key; the original keys are unchanged.
    failure = payload.pop("failure")
    assert payload == {
        "error": "adb_error",
        "message": "ADB property inspection failed.",
        "ok": False,
    }
    assert failure["id"] == "device.command_failed"
    assert "private target" not in result.stdout


def test_versions_cli_is_offline_clean_json(monkeypatch):
    monkeypatch.setattr(
        android_support,
        "resolve_adb",
        lambda: pytest.fail("offline versions command must not resolve adb"),
    )
    result = CliRunner().invoke(android_app, ["versions", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["minimum_supported_api"] == 29
    assert payload["maximum_verified_api"] == 37
    assert [item["api"] for item in payload["versions"]] == list(range(29, 38))
    assert "\x1b[" not in result.stdout


def test_capabilities_cli_requires_explicit_complete_target():
    result = CliRunner().invoke(android_app, ["capabilities", "--json"])

    assert result.exit_code != 0
    assert "Missing option" in (result.stdout + result.stderr)

