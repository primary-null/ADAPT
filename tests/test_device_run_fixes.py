"""Fixes for two findings from the 2026-11-14 runs on real devices."""

from __future__ import annotations

import pytest

from adapt_adb.adb import _transport_kind
from adapt_adb.dex_pc import _without_hardware_addresses


@pytest.mark.parametrize(
    "serial,expected",
    [
        ("R58N123ABCD", "usb"),
        ("emulator-5554", "local"),
        ("192.0.2.10:5555", "tcp"),
        ("phone.example.ts.net:37123", "tcp"),
        ("[2001:db8::1]:5555", "tcp"),
        ("adb-R58N123ABCD-AbCdEf._adb-tls-connect._tcp", "tcp"),
        ("adb-R58N123ABCD-AbCdEf._adb._tcp", "tcp"),
        ("serial:with:colons", "usb"),
        ("", "usb"),
    ],
)
def test_transport_kind_reads_network_serials(serial: str, expected: str) -> None:
    assert _transport_kind(serial) == expected


def test_probe_observations_withhold_hardware_addresses() -> None:
    observed = {
        "dex_enabled": 4,
        "displays": [
            {"id": 0, "type": 1, "unique_id": "local:4630947232161729154"},
            {"id": 2, "type": 5, "unique_id": "virtual:android,1000,Desktop,0"},
            {"id": 10, "type": 3, "unique_id": "wifi:02:00:5e:10:00:01"},
            {"id": 6, "type": 3, "unique_id": "wifi:desktop:02:00:5E:10:00:01"},
        ],
        "methods": ["public abstract void a()"],
    }
    cleaned = _without_hardware_addresses(observed)
    assert "02:00:5e" not in str(cleaned).lower()
    assert cleaned["displays"][2]["unique_id"] == "wifi:<hardware-address>"
    assert cleaned["displays"][3]["unique_id"] == "wifi:desktop:<hardware-address>"
    # Identifiers the workflow matches on are untouched, and the input is not mutated.
    assert cleaned["displays"][0] == observed["displays"][0]
    assert cleaned["displays"][1]["unique_id"] == "virtual:android,1000,Desktop,0"
    assert observed["displays"][2]["unique_id"] == "wifi:02:00:5e:10:00:01"
    assert cleaned["dex_enabled"] == 4 and cleaned["methods"] == observed["methods"]
