"""Focused system log evidence and offline guidance; no logging switches changed."""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer

from adapt_adb import dev_options
from adapt_adb.development import DevelopmentError, LOG_MAX_BYTES, _emit, export_logs, logs_app
from adapt_adb.device_state import validate_target
from adapt_adb.progress import operation

TAGS = {
    "window": ("WindowManager", "WindowManagerShell", "ActivityTaskManager", "ShellTransitions"),
    "dex": ("DesktopModeService", "WifiDisplayController", "WifiDisplayAdapter", "WifiP2pService", "RemoteDisplay"),
    "print": ("PrintManagerService", "PrintSpooler", "RemotePrintSpooler", "RemotePrintService", "PrintService"),
}
READ_MAX_BYTES = 2 * 1024 * 1024
STDERR_MAX_BYTES = 64 * 1024

_GUIDES = {
    "samsung": {
        "title": "Samsung verbose debug logging",
        "purpose": "Firmware-specific Samsung diagnostics for reproducing vendor integration issues.",
        "automation": "manual_unverified_mapping",
        "steps": ["Record the model, Android and One UI versions and the original switch state", "Search Developer options for the exact Samsung label; availability can change by firmware", "Enable manually only for a short reproduction, then restore the original state", "Inspect relevant logs locally; the switch does not guarantee that vendor-private logs are readable over ADB"],
        "boundary": "No verified public Samsung control or universal Settings key is established here. This is separate from Select debug app, USB debugging, AOSP verbose vendor logging and SysDump debug level.",
        "sources": ["https://docs.samsungknox.com/admin/knox-manage/original-console/monitor/collect-bug-reports/"],
        "source_scope": "Samsung documents separate SysDump/support log workflows; it does not document this exact switch's automation mapping.",
    },
    "vendor": {
        "title": "Android verbose vendor logging",
        "purpose": "Additional OEM diagnostics through the dumpstate HAL, when the device implements it.",
        "automation": "manual_only",
        "boundary": "AOSP uses a vendor HAL service, not a universal settings put key. Availability and readable output vary. ADAPT does not issue raw Binder calls or change vendor properties.",
        "sources": ["https://android.googlesource.com/platform/packages/apps/Settings/+/refs/heads/main/src/com/android/settings/development/EnableVerboseVendorLoggingPreferenceController.java"],
    },
    "window": {
        "title": "Verbose window logging",
        "purpose": "Investigate focus, resizing, lifecycle, multi-window and display routing issues.",
        "capture": "adapt logs diagnostic -s SERIAL --topic window --minutes 2 --output window.json",
        "boundary": "Captures existing text logcat only. ProtoLog/Winscope/Perfetto traces are separate artifacts; commands and permissions change across Android versions. Enabling a window trace does not guarantee readable text logcat.",
        "sources": ["https://source.android.com/docs/core/graphics/winscope/capture/adb"],
    },
    "dex": {
        "title": "Samsung DeX and wireless display connection diagnostics",
        "purpose": "Distinguish Wi-Fi Direct provisioning, wireless display transport and Samsung desktop activation.",
        "capture": "adapt logs diagnostic -s SERIAL --topic dex --minutes 2 --output dex.json",
        "boundary": "Tag coverage is best-effort and was observed on the tested Fold3 firmware. No pairing, display activation or receiver reset occurs. A Wi-Fi provisioning error is not proof of an RTSP or scrcpy failure.",
        "sources": ["https://developer.android.com/tools/logcat"],
    },
    "print": {
        "title": "Verbose print logging",
        "purpose": "Investigate Android print framework, spooler and print-service jobs.",
        "capture": "adapt logs diagnostic -s SERIAL --topic print --minutes 2 --output print.json",
        "boundary": "Useful for printing problems; it is not a general app or DeX debugger. OEM/print-service tags may differ. Print logs can reveal document titles or printer/network details.",
        "sources": ["https://developer.android.com/reference/android/print/PrintManager", "https://developer.android.com/tools/logcat"],
    },
    "wifi": {
        "title": "Wi-Fi verbose logging",
        "purpose": "Additional Wi-Fi diagnostics, distinct from Samsung/vendor verbose logging.",
        "boundary": "Android documents extra wireless network and signal details. Restore the original switch after reproducing; this guide does not toggle it or change networks.",
        "sources": ["https://developer.android.com/studio/debug/dev-options"],
    },
    "app": {
        "title": "Application logs and debugger selection",
        "capture": "adapt logs capture -s SERIAL --package com.example.app --level V --output app.json",
        "boundary": "V is a reader severity filter, not a switch enabling every producer's debug output. App-UID logs can omit framework messages. Select debug app/Wait for debugger is a separate JDWP workflow, not Samsung verbose logging.",
        "sources": ["https://developer.android.com/tools/logcat", "https://developer.android.com/studio/debug/dev-options"],
    },
}


def logging_guide(topic: str = "all") -> dict[str, Any]:
    if topic != "all" and topic not in _GUIDES:
        raise DevelopmentError("Choose all, samsung, vendor, window, dex, print, wifi or app.")
    return {"schema_version": 1, "status": "offline_reference", "device_access": False,
            "topics": [{"topic": key, **value} for key, value in _GUIDES.items() if topic in {"all", key}],
            "ai_workflow": ["Observe device/build and select the failing subsystem", "Record original diagnostic switch states before manual changes", "Reproduce one bounded journey and capture its recent relevant logs", "Correlate timestamps with fresh UI evidence; distinguish a symptom from a verified cause", "Restore prior switch states; review sensitive data before sharing"],
            "effects": "No settings, properties, buffers, apps or device state changed.",
            "cost": "Verbose producers may increase power use, storage and diagnostic noise. Use short reproductions; do not enable every mode automatically. Logs may contain secrets or personal data."}


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


def _read_logs(argv: list[str]) -> str:
    # File-backed capture bounds memory while the owned child is running.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
        try:
            deadline = time.monotonic() + 10
            while process.poll() is None:
                if os.fstat(stdout.fileno()).st_size > READ_MAX_BYTES or os.fstat(stderr.fileno()).st_size > STDERR_MAX_BYTES:
                    raise DevelopmentError("Diagnostic log capture exceeded its byte budget; shorten --minutes. Captured contents were discarded.")
                if time.monotonic() >= deadline:
                    raise DevelopmentError("Diagnostic log capture timed out. Captured contents were discarded.")
                time.sleep(.02)
            stdout.seek(0); stderr.seek(0)
            data, errors = stdout.read(READ_MAX_BYTES + 1), stderr.read(STDERR_MAX_BYTES + 1)
            if len(data) > READ_MAX_BYTES or len(errors) > STDERR_MAX_BYTES:
                raise DevelopmentError("Diagnostic log capture exceeded its byte budget; shorten --minutes.")
            if process.returncode or re.search(rb"permission deni|securityexception|not permitted|access denied|(?:^|\n)\s*(?:error:|adb:)", errors, re.I):
                raise DevelopmentError("Focused logcat capture failed or was denied; no broader fallback was attempted.")
            return data.decode("utf-8", errors="strict")
        finally:
            _stop(process)


@operation("Extracting focused diagnostic logs")
def capture_diagnostics(serial: str, *, topic: str, minutes: int = 2, lines: int = 200, level: str = "V") -> dict[str, Any]:
    validate_target(serial)
    if topic not in TAGS or type(minutes) is not int or not 1 <= minutes <= 10 or type(lines) is not int or not 1 <= lines <= 500 or level not in {"V", "D", "I", "W", "E", "F"}:
        raise DevelopmentError("Choose window/dex/print, 1-10 minutes, 1-500 lines and V/D/I/W/E/F severity.")
    adb = dev_options._resolve_adb_path(None)
    identity = dev_options._read_identity(adb, serial)
    clock = dev_options._run_adb(adb, serial, ["shell", "date", "+%s"])
    if not re.fullmatch(r"[0-9]{9,11}", clock):
        raise DevelopmentError("Device clock could not be read; no host-clock or whole-buffer fallback was used.")
    since = str(int(clock) - minutes * 60) + ".000"
    argv = [str(adb), "-s", serial, "logcat", "-d", "-T", since, "-v", "threadtime", "-b", "main", "-b", "system", "-b", "crash", *[tag + ":" + level for tag in TAGS[topic]], "*:S"]
    try:
        text = _read_logs(argv)
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        raise DevelopmentError("Focused logcat capture was unavailable; captured contents were discarded.") from exc
    if dev_options._read_identity(adb, serial) != identity:
        raise DevelopmentError("Physical device/build/user changed during capture; captured contents were discarded.")
    raw = text.encode("utf-8")
    truncated = len(raw) > LOG_MAX_BYTES
    records = [line for line in raw[-LOG_MAX_BYTES:].decode("utf-8", errors="ignore").splitlines() if not line.startswith("--------- beginning of ")]
    truncated = truncated or len(records) > lines
    return {"schema_version": 1, "status": "captured", "serial": serial, "device": asdict(identity), "topic": topic,
            "tags": list(TAGS[topic]), "scope": "system_components_not_app_uid", "minutes": minutes,
            "since_device_epoch": since, "requested_lines": lines, "minimum_level": level,
            "lines": records[-lines:], "truncated": truncated, "max_bytes": LOG_MAX_BYTES,
            "captured_at_utc": datetime.now(timezone.utc).isoformat(), "mutations": [],
            "privacy": "Component logs may include multiple apps/users, network identifiers, document titles or secrets. They are not redacted automatically; review locally before sharing.",
            "claim_boundary": "Recent retained text logcat matching fixed tags, not full vendor dumps or binary window traces. Tags and permissions vary by firmware. Empty logs do not prove support or a passing test. No verbose switches or buffers were changed."}


@logs_app.command("guide")
def guide_command(topic: str = typer.Option("all", "--topic"), json_output: bool = typer.Option(False, "--json")):
    """Explain Samsung/vendor/window/print logging without contacting a device."""
    _emit(logging_guide, json_output, topic=topic)


@logs_app.command("diagnostic")
def diagnostic_command(serial: str = typer.Option(..., "--serial", "-s"), topic: str = typer.Option(..., "--topic", help="window, dex or print"), minutes: int = typer.Option(2, "--minutes"), lines: int = typer.Option(200, "--lines"), level: str = typer.Option("V", "--level"), output: Path | None = typer.Option(None, "--output"), json_output: bool = typer.Option(False, "--json")):
    """Capture recent fixed-tag system diagnostics; never enable verbose producers."""
    def capture():
        try:
            report = capture_diagnostics(serial, topic=topic, minutes=minutes, lines=lines, level=level)
        except dev_options.DevOptionsError as exc:
            raise DevelopmentError(str(exc)) from exc
        return export_logs(report, output) if output is not None else report
    _emit(capture, json_output)
