"""Independent, non-transitioning ADB and fastboot observations.

No root request, mode switch, unlock, flash, server restart or target inference.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import typer

from adapt_adb.adb import get_search_locations, parse_devices_short, resolve_adb
from adapt_adb.doctor import preflight_adb_server
from adapt_adb.progress import operation

detect_app = typer.Typer(help="Observe ADB/fastboot transports, root indicators, ROM and bootloader state.")
PROPERTIES = (
    "ro.build.version.sdk", "ro.product.manufacturer", "ro.build.type", "ro.build.tags",
    "ro.boot.flash.locked", "ro.boot.vbmeta.device_state", "ro.boot.verifiedbootstate",
    "ro.oem_unlock_supported", "sys.oem_unlock_allowed", "ro.lineage.version",
    "ro.modversion", "ro.crdroid.version", "ro.bootmode",
)


class DetectionError(ValueError):
    pass


def validate_target(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or value.startswith("-") or len(value) > 1024 or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value):
        raise DetectionError("Supply one exact nonblank serial without control characters.")
    return value


def resolve_fastboot() -> Path | None:
    override = os.environ.get("ADAPT_FASTBOOT", "").strip()
    if override:
        candidate = Path(override).expanduser()
        return candidate.resolve() if candidate.is_file() else None
    located = shutil.which("fastboot")
    if located:
        return Path(located).resolve()
    name = "fastboot.exe" if os.name == "nt" else "fastboot"
    # PATH and standard SDK directories are inspected without executing ADB.
    for _, adb in get_search_locations():
        if adb is not None and (candidate := adb.parent / name).is_file():
            return candidate.resolve()
    return None


def run_process(args: list[str], timeout: int = 5) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False, timeout=timeout)


def probe(args: list[str], *, empty_missing: bool = True) -> dict[str, Any]:
    try:
        result = run_process(args)
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "value": None}
    except OSError:
        return {"status": "unavailable", "value": None}
    text = (result.stdout or "").strip()
    errors = (result.stderr or "") + "\n" + text
    if re.search(r"permission deni|securityexception|not permitted|access denied", errors, re.I):
        return {"status": "denied", "value": None}
    if result.returncode != 0 or re.search(r"(?:^|\n)\s*(?:error:|adb: error:|FAILED|< waiting)", errors, re.I):
        return {"status": "failed", "value": None}
    if len(text) > 32768 or any(ord(c) < 32 and c not in "\t\r\n" or 127 <= ord(c) <= 159 for c in text):
        return {"status": "malformed", "value": None}
    return {"status": "missing" if empty_missing and not text else "observed", "value": text}


def parse_fastboot_devices(text: str) -> list[dict[str, str]]:
    records = []
    for line in text.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"(.+?)\s+fastboot\s*", line)
        if not match:
            raise DetectionError("Unrecognized fastboot device listing; presence is unknown.")
        serial = validate_target(match[1])
        if any(r["serial"] == serial for r in records):
            raise DetectionError("Duplicate fastboot identity; listing is ambiguous.")
        records.append({"serial": serial, "state": "fastboot_transport", "mode": "unknown_until_inspected"})
    return records


@operation("Detecting Android transports")
def scan(*, transport: str = "all") -> dict[str, Any]:
    if transport not in {"all", "adb", "fastboot"}:
        raise DetectionError("Transport must be all, adb or fastboot.")
    reports = {}
    for kind in ("adb", "fastboot"):
        if transport not in {"all", kind}:
            continue
        binary = resolve_adb() if kind == "adb" else resolve_fastboot()
        if binary is None:
            reports[kind] = {"status": "missing_tool", "devices": []}
            continue
        if kind == "adb":
            try:
                preflight_adb_server(binary, ["devices"])
            except Exception:
                reports[kind] = {"status": "blocked", "devices": []}
                continue  # Fastboot inspection stays independent of this failure.
        result = probe([str(binary), "devices"], empty_missing=False)
        if result["status"] != "observed":
            reports[kind] = {"status": result["status"], "devices": []}
            continue
        try:
            if kind == "adb" and not any(line.startswith("List of devices attached") for line in result["value"].splitlines()):
                raise DetectionError("ADB listing header was missing; device presence is unknown.")
            devices = parse_devices_short(result["value"]) if kind == "adb" else parse_fastboot_devices(result["value"])
            reports[kind] = {"status": "observed", "devices": devices}
        except ValueError:
            reports[kind] = {"status": "malformed", "devices": []}
    return {"schema_version": 1, "transports": reports, "status": "observed" if all(r["status"] == "observed" for r in reports.values()) else "partial",
            "identity_correlation": "not_attempted", "mutations": [],
            "note": "Independent transport snapshots; matching serial text does not prove physical identity. No device changes mode."}


def evaluate(properties: dict[str, dict[str, Any]], uid: dict[str, Any], su: dict[str, Any]) -> dict[str, Any]:
    def value(key):
        item = properties.get(key, {})
        return item.get("value") if item.get("status") == "observed" else None

    lock_evidence = []
    for key, mapping in (
        ("ro.boot.flash.locked", {"1": "locked", "0": "unlocked"}),
        ("ro.boot.vbmeta.device_state", {"locked": "locked", "unlocked": "unlocked"}),
        ("ro.boot.verifiedbootstate", {"green": "locked", "yellow": "locked", "orange": "unlocked"}),
    ):
        state = mapping.get(value(key))
        if state:
            lock_evidence.append({"source": key, "state": state})
    states = {e["state"] for e in lock_evidence}
    lock = next(iter(states)) if len(states) == 1 else "conflicting" if states else "unknown"
    shell_uid = uid.get("value") if uid.get("status") == "observed" else None
    markers = {k: value(k) for k in ("ro.lineage.version", "ro.modversion", "ro.crdroid.version") if value(k)}
    return {
        "root": {"status": "root_shell_observed" if shell_uid == "0" else "su_candidate_observed" if su.get("status") == "observed" else "unknown",
                 "shell_uid": shell_uid if shell_uid and shell_uid.isdigit() else None,
                 "su_lookup": su, "note": "No su execution or adb root request. Missing indicators cannot prove the device is unrooted."},
        "rom": {"status": "custom_rom_indicators" if markers else "unknown", "markers": markers,
                "build_type": value("ro.build.type"), "build_tags": value("ro.build.tags"),
                "note": "Self-reported properties are heuristic. test-keys, root or unlocked alone do not prove a custom ROM; absence does not prove stock."},
        "bootloader": {"status": lock, "evidence": lock_evidence, "source": "android_reported_properties",
                       "unlock_supported": value("ro.oem_unlock_supported"), "oem_unlock_allowed": value("sys.oem_unlock_allowed"),
                       "note": "Unlock capability/permission is separate from current lock state. Properties are not hardware attestation."},
    }


def getvar(binary: Path, serial: str, key: str) -> dict[str, Any]:
    try:
        proc = run_process([str(binary), "-s", serial, "getvar", key])
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "value": None}
    except OSError:
        return {"status": "unavailable", "value": None}
    text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if proc.returncode != 0 or re.search(r"FAILED|< waiting", text, re.I):
        return {"status": "unsupported_or_failed", "value": None}
    values = re.findall(r"^\s*(?:\(bootloader\)\s*)?" + re.escape(key) + r":\s*([^\r\n]*)$", text, re.M)
    if len(values) != 1 or not values[0].strip() or len(values[0]) > 256 or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in values[0]):
        return {"status": "unknown", "value": None}
    return {"status": "observed", "value": values[0].strip()}


@operation("Inspecting device state")
def inspect_device(serial: str, *, transport: str = "adb") -> dict[str, Any]:
    serial = validate_target(serial)
    if transport not in {"adb", "fastboot"}:
        raise DetectionError("Inspection transport must be adb or fastboot; no automatic switching.")
    binary = resolve_adb() if transport == "adb" else resolve_fastboot()
    if binary is None:
        raise DetectionError(f"{transport} is missing. Install Platform-Tools or configure its binary path.")
    base = {"schema_version": 1, "serial": serial, "transport": transport, "mutations": []}
    if transport == "fastboot":
        data = {key: getvar(binary, serial, key) for key in ("unlocked", "is-userspace", "product")}
        unlocked = data["unlocked"]["value"]
        userspace = data["is-userspace"]["value"]
        return {**base, "status": "observed" if all(r["status"] == "observed" for r in data.values()) else "partial",
                "mode": {"yes": "fastbootd", "no": "bootloader_fastboot"}.get(userspace, "unknown"),
                "bootloader": {"status": {"yes": "unlocked", "no": "locked"}.get(unlocked, "unknown"), "source": "fastboot_getvar_unlocked"},
                "root": {"status": "not_available_in_this_transport"}, "rom": {"status": "not_available_in_this_transport"}, "observations": data}
    try:
        preflight_adb_server(binary, ["-s", serial, "shell", "id", "-u"])
    except Exception as exc:
        raise DetectionError("ADB shared-server safety could not be verified; no device queries attempted.") from exc
    prefix = [str(binary), "-s", serial, "shell"]
    properties = {key: probe([*prefix, "getprop", key]) for key in PROPERTIES}
    uid = probe([*prefix, "id", "-u"])
    su = probe([*prefix, "command", "-v", "su"])
    evaluated = evaluate(properties, uid, su)
    return {**base, "status": "observed" if uid["status"] == "observed" else "partial", **evaluated,
            "observations": properties, "shell_identity": uid,
            "note": "ADB/recovery shell observations are not a universal root/ROM detector. Samsung Download/Odin mode is not standard fastboot."}


def render(report: dict[str, Any]) -> str:
    if "transports" in report:
        lines = ["ADAPT / TRANSPORT DETECTION"]
        for kind, group in report["transports"].items():
            lines.append(f"{kind}: {group['status']}")
            lines.extend(f"  {d['serial']}: {d['state']}" for d in group["devices"])
        return "\n".join(lines)
    return "\n".join([f"ADAPT / {report['transport'].upper()} DEVICE STATE", f"Mode: {report.get('mode', 'ADB shell')}",
                       *(f"{key}: {report[key]['status']}" for key in ("root", "rom", "bootloader"))])


def _emit(fn, json_output: bool, **kwargs):
    try:
        report = fn(**kwargs)
    except DetectionError as exc:
        typer.echo(json.dumps({"status": "refused", "error": str(exc)}) if json_output else str(exc), err=not json_output)
        raise typer.Exit(2)
    typer.echo(json.dumps(report, indent=2) if json_output else render(report))
    if report["status"] == "partial":
        raise typer.Exit(2)


@detect_app.command("scan")
def scan_command(transport: str = typer.Option("all", "--transport"), json_output: bool = typer.Option(False, "--json")):
    """List independent ADB and fastboot transports without mode changes."""
    _emit(scan, json_output, transport=transport)


@detect_app.command("inspect")
def inspect_command(serial: str = typer.Option(..., "--serial", "-s"), transport: str = typer.Option("adb", "--transport"), json_output: bool = typer.Option(False, "--json")):
    """Inspect one exact target using only the explicitly selected transport."""
    _emit(inspect_device, json_output, serial=serial, transport=transport)
