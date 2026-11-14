"""Hardening tests for focused diagnostic capture, exception mapping, and byte truncation boundary handling."""
from pathlib import Path

import pytest

from adapt_adb import dev_options
from adapt_adb import diagnostics as diag
from adapt_adb.dev_options import DeviceIdentity


def test_dev_options_error_surfaces_as_development_error(monkeypatch):
    monkeypatch.setattr(dev_options, "_resolve_adb_path", lambda *a: Path("mock-adb"))

    # 1. DevOptionsError during initial _read_identity
    monkeypatch.setattr(dev_options, "_read_identity", lambda *a: (_ for _ in ()).throw(dev_options.DevOptionsError("adb_error", "adb identity inspection failed")))
    with pytest.raises(diag.DevelopmentError, match="adb identity inspection failed"):
        diag.capture_diagnostics("phone", topic="dex")

    # 2. DevOptionsError during _run_adb (clock read)
    monkeypatch.setattr(dev_options, "_read_identity", lambda *a: DeviceIdentity("phone", "build", 0))
    monkeypatch.setattr(dev_options, "_run_adb", lambda *a: (_ for _ in ()).throw(dev_options.DevOptionsError("adb_error", "adb shell failed")))
    with pytest.raises(diag.DevelopmentError, match="adb shell failed"):
        diag.capture_diagnostics("phone", topic="dex")

    # 3. DevOptionsError during post-capture _read_identity
    monkeypatch.setattr(dev_options, "_run_adb", lambda *a: "1700000000")
    monkeypatch.setattr(diag, "_read_logs", lambda *a: "log line 1\nlog line 2\n")
    first_call = True

    def read_identity_flaky(*a):
        nonlocal first_call
        if first_call:
            first_call = False
            return DeviceIdentity("phone", "build", 0)
        raise dev_options.DevOptionsError("adb_error", "device disconnected during log capture")

    monkeypatch.setattr(dev_options, "_read_identity", read_identity_flaky)
    with pytest.raises(diag.DevelopmentError, match="device disconnected during log capture"):
        diag.capture_diagnostics("phone", topic="dex")


def test_byte_truncation_drops_partial_leading_line(monkeypatch):
    monkeypatch.setattr(dev_options, "_resolve_adb_path", lambda *a: Path("mock-adb"))
    monkeypatch.setattr(dev_options, "_read_identity", lambda *a: DeviceIdentity("phone", "build", 0))
    monkeypatch.setattr(dev_options, "_run_adb", lambda *a: "1700000000")

    original_text = "line 1: header info\nline 2: second complete record\nline 3: third complete record\n"
    monkeypatch.setattr(diag, "_read_logs", lambda *a: original_text)

    # Patch LOG_MAX_BYTES so that the slice starts in the middle of line 2:
    # "line 1: header info\nline 2: second complete record\nline 3: third complete record\n"
    # Total bytes: 81
    # If LOG_MAX_BYTES is 45, the last 45 bytes will be:
    # "cond complete record\nline 3: third complete record\n"
    # Dropping up to and including the first '\n' leaves:
    # "line 3: third complete record\n"
    monkeypatch.setattr(diag, "LOG_MAX_BYTES", 45)

    report = diag.capture_diagnostics("phone", topic="dex", lines=10)
    assert report["truncated"] is True
    # The first returned line must be the complete line 3, NOT the fragmented "cond complete record"
    assert report["lines"] == ["line 3: third complete record"]


def test_untruncated_output_remains_unchanged(monkeypatch):
    monkeypatch.setattr(dev_options, "_resolve_adb_path", lambda *a: Path("mock-adb"))
    monkeypatch.setattr(dev_options, "_read_identity", lambda *a: DeviceIdentity("phone", "build", 0))
    monkeypatch.setattr(dev_options, "_run_adb", lambda *a: "1700000000")

    original_text = "line 1: first log line\nline 2: second log line\nline 3: third log line\n"
    monkeypatch.setattr(diag, "_read_logs", lambda *a: original_text)

    report = diag.capture_diagnostics("phone", topic="dex", lines=10)
    assert report["truncated"] is False
    assert report["lines"] == [
        "line 1: first log line",
        "line 2: second log line",
        "line 3: third log line",
    ]
