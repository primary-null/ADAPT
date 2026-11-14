"""Generic Android desktop/application testing, distinct from Samsung DeX."""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import typer

from adapt_adb import dev_options
from adapt_adb.adb import resolve_adb
from adapt_adb.device_state import DetectionError, probe, validate_target
from adapt_adb.dex import DexError, parse_logical_displays, resolve_scrcpy
from adapt_adb.doctor import preflight_adb_server
from adapt_adb.failure_report import (FailureContext, UNANSWERED_STATUSES, attach_failure, cause_failure_id, classify,
                                     current_command, emit_failure, emit_pointer, query_failure_id, tagged)
from adapt_adb.progress import operation

desktop_app = typer.Typer(help="Inspect desktop eligibility, enable recoverable test settings, and open a separate app display.")
PROFILE = "desktop-testing"


def _adb(serial: str) -> Path:
    validate_target(serial)
    adb = resolve_adb()
    if adb is None:
        raise tagged(DetectionError("ADB is missing; run adapt setup."), "adb.not_found")
    try:
        preflight_adb_server(adb, ["-s", serial, "shell", "getprop", "ro.build.version.sdk"])
    except Exception as exc:
        raise tagged(DetectionError("ADB server safety could not be verified; no device operation attempted."), cause_failure_id(exc, "adb.server.unverified")) from exc
    return adb


@operation("Inspecting desktop test support")
def inspect_device(serial: str) -> dict[str, Any]:
    adb = _adb(serial)
    prefix = [str(adb), "-s", serial, "shell"]
    sdk = probe([*prefix, "getprop", "ro.build.version.sdk"])
    features = probe([*prefix, "pm", "list", "features"])
    settings = {key: probe([*prefix, "settings", "get", "global", key]) for key in ("enable_freeform_support", "force_desktop_mode_on_external_displays")}
    displays = probe([*prefix, "dumpsys", "display"])
    api = int(sdk["value"]) if sdk["status"] == "observed" and re.fullmatch(r"[0-9]{1,3}", sdk["value"]) else None
    flags = set((features["value"] or "").splitlines()) if features["status"] == "observed" else set()
    return {"schema_version": 1, "serial": serial, "api": api,
            "status": "observed" if api is not None else "partial",
            "virtual_display_eligibility": "version_eligible_oem_unverified" if api is not None and 29 <= api <= 37 else "below_minimum" if api is not None and api < 29 else "unknown",
            "desktop_shell": "not_verified", "samsung_dex": "not_inferred",
            "advertised_features": {key: "advertised" if "feature:" + key in flags else "not_advertised" if features["status"] == "observed" else "unknown" for key in ("android.software.freeform_window_management", "android.software.activities_on_secondary_displays")},
            "settings": settings, "displays": parse_logical_displays(displays["value"]) if displays["status"] == "observed" else {"status": displays["status"], "items": []},
            "mutations": [], "note": "Android version, features or written settings alone cannot prove an OEM desktop shell, physical video output or functioning freeform windows."}


def plan(serial: str, app: str, *, size: str = "1920x1080", dpi: int = 160, view_only: bool = False) -> dict[str, Any]:
    serial = validate_target(serial)
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", app):
        raise tagged(DetectionError("--app must be an exact Android package name, not a command or component."), "app.package_invalid")
    match = re.fullmatch(r"([0-9]{3,4})x([0-9]{3,4})", size)
    if not match or not all(320 <= int(v) <= 4096 for v in match.groups()) or type(dpi) is not int or not 80 <= dpi <= 640:
        raise tagged(DetectionError("Use a 320–4096 pixel size (WIDTHxHEIGHT) and DPI 80–640."), "desktop.geometry_invalid")
    argv = ["scrcpy", "-s", serial, f"--new-display={size}/{dpi}", f"--start-app={app}", "--no-audio", "--no-vd-destroy-content", "--window-title", "ADAPT Desktop Test"]
    if view_only:
        argv.append("--no-control")
    return {"schema_version": 1, "status": "planned", "serial": serial, "app": app, "size": size, "dpi": dpi, "argv": argv,
            "desktop_shell": "not_verified", "samsung_dex": "not_inferred", "device_access": False,
            "note": "Creates a separate app test display when started; desktop decorations are OEM-dependent. On normal close scrcpy removes its display and requests moving its apps to the main display."}


@operation("Starting desktop test display")
def start(serial: str, app: str, *, yes: bool = False, size: str = "1920x1080", dpi: int = 160, view_only: bool = False) -> dict[str, Any]:
    report = plan(serial, app, size=size, dpi=dpi, view_only=view_only)
    if not yes:
        raise tagged(DetectionError("Starting the app/display requires explicit --yes. Review desktop plan first."), "desktop.confirmation_required")
    adb = _adb(serial)
    sdk = probe([str(adb), "-s", serial, "shell", "getprop", "ro.build.version.sdk"])
    if sdk["status"] != "observed" or not re.fullmatch(r"[0-9]{1,3}", sdk["value"]) or not 29 <= int(sdk["value"]) <= 37:
        api_seen = sdk["status"] == "observed" and re.fullmatch(r"[0-9]{1,3}", sdk["value"])
        raise tagged(DetectionError("An observed Android 10–17 / API 29–37 target is required for this test workflow."),
                     "desktop.api_unsupported" if api_seen else query_failure_id(sdk["status"], "desktop.inspect_partial"))
    client = resolve_scrcpy()
    version = probe([str(client), "--version"])
    match = re.search(r"\bscrcpy\s+v?(\d+)\.(\d+)(?:\.(\d+))?\b", version["value"] or "", re.I)
    if version["status"] != "observed" or not match or tuple(int(v or 0) for v in match.groups()) < (3, 1, 0):
        raise tagged(DetectionError("A verified scrcpy 3.1+ client is required for new displays with app-content preservation."), "desktop.scrcpy_unverified")
    argv = [str(client), *report["argv"][1:]]
    env = os.environ.copy()
    env["ADB"] = str(adb)
    try:
        child = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
    except OSError as exc:
        raise tagged(DetectionError("The desktop test client could not start."), "desktop.client_start_failed") from exc
    return {**report, "argv": argv, "status": "process_started", "pid": child.pid, "device_access": True,
            "stream_health": "not_verified", "note": report["note"] + " No existing mirrors, device settings, transports or ADB servers were changed."}


def enable(serial: str, snapshot: Path, *, yes: bool = False) -> dict[str, Any]:
    validate_target(serial)
    if not yes:
        raise tagged(DetectionError("Enabling desktop test settings requires --yes and a new recovery snapshot."), "desktop.confirmation_required")
    result = dev_options.apply_profile(PROFILE, serial, snapshot)
    return {**result, "status": "settings_applied", "desktop_shell": "not_verified", "manual_reboot_may_be_required": True,
            "note": "Settings were requested, not certified supported. No reboot is performed. Restore using dev-options restore with this snapshot."}


def report_failure_id(report: dict[str, Any]) -> tuple[str | None, bool]:
    """(guide, is_failure) for an inspection report: partial, or observed with unanswered queries."""
    if "advertised_features" not in report:
        return None, False
    displays = report.get("displays") if isinstance(report.get("displays"), dict) else {}
    statuses = [item.get("status") for item in (report.get("settings") or {}).values() if isinstance(item, dict)]
    statuses += [displays["status"]] if displays.get("status") in UNANSWERED_STATUSES | {"denied"} else []
    missing = [s for s in statuses if s in UNANSWERED_STATUSES | {"denied"}]
    if report.get("status") == "partial":
        # The API-level query's own status is not in the report; when no later query answered
        # either, the device did not answer at all.
        if missing and len(missing) == len(statuses):
            return query_failure_id(missing[0], "desktop.inspect_partial"), True
        return "desktop.inspect_partial", True
    return (query_failure_id(missing[0], ""), False) if missing else (None, False)


def _emit(fn, json_output: bool, **kwargs):
    context = FailureContext.from_runtime(command=current_command())
    try:
        report = fn(**kwargs)
    except (DetectionError, DexError, dev_options.DevOptionsError) as exc:
        failure_id = classify(exc)
        typer.echo(json.dumps(attach_failure({"status": "refused", "error": str(exc)}, failure_id, context)) if json_output else str(exc), err=not json_output)
        if not json_output:
            emit_failure(failure_id, context)
        raise typer.Exit(2)
    failure_id, is_failure = report_failure_id(report)
    if json_output and failure_id:
        report = attach_failure(report, failure_id, context)
    if json_output:
        typer.echo(json.dumps(report, indent=2))
    else:
        typer.echo("ADAPT / DESKTOP TEST\nStatus: " + str(report.get("status", "unknown")))
        if "argv" in report:
            typer.echo("Process arguments: " + json.dumps(report["argv"]))
        if "settings" in report:
            typer.echo("Settings: " + json.dumps(report["settings"], indent=2))
        typer.echo(str(report.get("note", "")))
        if failure_id:
            emit_failure(failure_id, context) if is_failure else emit_pointer(failure_id, label="Some values were not observed:")
        elif report.get("status") == "process_started":
            # ADAPT does not watch scrcpy after starting it; name the guide for a missing window.
            emit_pointer("desktop.no_window", label="If no window appears:")
    if report.get("status") == "partial":
        raise typer.Exit(2)


@desktop_app.command("inspect")
def inspect_command(serial: str = typer.Option(..., "--serial", "-s"), json_output: bool = typer.Option(False, "--json")):
    """Observe desktop settings, advertised features and current displays."""
    _emit(inspect_device, json_output, serial=serial)


@desktop_app.command("plan")
def plan_command(serial: str = typer.Option(..., "--serial", "-s"), app: str = typer.Option(..., "--app"), size: str = typer.Option("1920x1080", "--size"), dpi: int = typer.Option(160, "--dpi"), view_only: bool = typer.Option(False, "--view-only"), json_output: bool = typer.Option(False, "--json")):
    """Offline preview of a separate app display; no device access or writes."""
    _emit(plan, json_output, serial=serial, app=app, size=size, dpi=dpi, view_only=view_only)


@desktop_app.command("start")
def start_command(serial: str = typer.Option(..., "--serial", "-s"), app: str = typer.Option(..., "--app"), size: str = typer.Option("1920x1080", "--size"), dpi: int = typer.Option(160, "--dpi"), view_only: bool = typer.Option(False, "--view-only"), yes: bool = typer.Option(False, "--yes"), json_output: bool = typer.Option(False, "--json")):
    """Start one confirmed app/display session; leave existing mirrors open."""
    _emit(start, json_output, serial=serial, app=app, size=size, dpi=dpi, view_only=view_only, yes=yes)


@desktop_app.command("enable")
def enable_command(serial: str = typer.Option(..., "--serial", "-s"), snapshot: Path = typer.Option(..., "--snapshot"), yes: bool = typer.Option(False, "--yes"), json_output: bool = typer.Option(False, "--json")):
    """Request desktop/freeform developer settings with a recovery snapshot."""
    _emit(enable, json_output, serial=serial, snapshot=snapshot, yes=yes)
