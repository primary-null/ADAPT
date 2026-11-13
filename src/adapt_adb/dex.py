"""Bounded Samsung DeX inspection and existing-display mirroring.

This module never starts DeX, creates a virtual display, changes device state,
or chooses a secondary display for the caller.  It accepts only an explicit
ADB serial and an explicit, currently observed non-primary display ID.
"""

from __future__ import annotations

from adapt_adb.progress import operation

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import typer

from adapt_adb.adb import resolve_adb
from adapt_adb.doctor import UnsafeAdbServerError, preflight_adb_server


SCHEMA_VERSION = 1
MIN_WORKFLOW_API = 33
MAX_RESEARCHED_API = 37
ADB_TIMEOUT_SECONDS = 10
HOST_TIMEOUT_SECONDS = 5
MIN_SCRCPY_VERSION = (2, 0, 0)

_SERIAL_RE = re.compile(r"^[^\x00-\x1f\x7f-\x9f]{1,1024}$")
_DENIED_RE = re.compile(
    r"permission denial|securityexception|security exception|not allowed|"
    r"operation not permitted|access denied",
    re.IGNORECASE,
)
_UNKNOWN_SERVICE_RE = re.compile(
    r"(?:can't|cannot) find service|unknown service|service\s+desktopmode\s+not found",
    re.IGNORECASE,
)
_SCRCPY_VERSION_RE = re.compile(
    r"\bscrcpy\s+v?([0-9]+)\.([0-9]+)(?:\.([0-9]+))?\b", re.IGNORECASE
)
_LOGICAL_SECTION_RE = re.compile(r"(?im)^\s*Logical Displays\s*:[^\r\n]*$")
_LOGICAL_HEADER_RE = re.compile(r"(?m)^\s*Display\s+([0-9]+)\s*:\s*$")
_TOP_LEVEL_SECTION_RE = re.compile(r"(?m)^[A-Za-z][^:\r\n]*:[^\r\n]*$")


class DexError(RuntimeError):
    """A bounded DeX operation could not be completed safely."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class ProbeResult:
    status: str
    stdout: str = ""
    reason: str | None = None


ProcessRunner = Callable[[list[str], int], subprocess.CompletedProcess[str]]
Preflight = Callable[[Path, list[str]], None]
PopenFactory = Callable[..., Any]
ScrcpyResolver = Callable[[Path | str | None], Path]


def _run_process(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _popen_process(args: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
    return subprocess.Popen(args, **kwargs)


def _validate_serial(serial: str) -> str:
    # ADB treats -s as an opaque argv value.  Spaced mDNS names are valid and
    # must remain one exact element; only edge whitespace/control bytes/flags
    # are rejected.
    if not isinstance(serial, str) or not serial.strip():
        raise DexError("invalid_serial", "A nonblank explicit -s/--serial target is required.")
    if serial != serial.strip() or serial.startswith("-") or not _SERIAL_RE.fullmatch(serial):
        raise DexError("invalid_serial", "The explicit ADB target contains unsupported characters.")
    return serial


def _resolve_adb(adb_path: Path | str | None) -> Path:
    resolved = Path(adb_path) if adb_path is not None else resolve_adb()
    if resolved is None:
        raise DexError("missing_adb", "adb binary not found; run 'adapt setup'.")
    return Path(resolved)


def _probe_adb(
    adb_path: Path,
    serial: str,
    device_args: list[str],
    *,
    runner: ProcessRunner,
) -> ProbeResult:
    argv = [str(adb_path), "-s", serial, *device_args]
    try:
        result = runner(argv, ADB_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise DexError("timeout", "Read-only ADB inspection timed out.") from exc
    except FileNotFoundError as exc:
        raise DexError("missing_adb", "adb binary was not found.") from exc
    except OSError as exc:
        raise DexError("adb_error", f"adb could not start: {exc}") from exc

    stdout = result.stdout or ""
    stderr = result.stderr or ""
    combined = "\n".join(part for part in (stdout, stderr) if part).strip()
    if _DENIED_RE.search(combined):
        return ProbeResult("denied", reason="The read-only query was denied.")
    if _UNKNOWN_SERVICE_RE.search(combined):
        return ProbeResult("unknown_service", reason="The desktopmode service was not available by this name.")
    if result.returncode != 0:
        return ProbeResult("error", reason=f"adb exited with status {result.returncode}.")
    return ProbeResult("observed", stdout=stdout.strip())


def _one_value(text: str, pattern: str, field: str) -> tuple[str | None, str | None]:
    values = re.findall(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
    if len(values) != 1:
        if not values:
            return None, f"recognized field {field} was missing"
        unique = sorted(set(str(value).casefold() for value in values))
        qualifier = "conflicting" if len(unique) > 1 else "duplicated"
        return None, f"recognized field {field} was {qualifier}"
    return str(values[0]), None


def parse_desktop_mode(text: str) -> dict[str, Any]:
    """Parse a conservative, explicitly recognized DesktopModeService shape.

    Synthetic fixtures can exercise this parser, but never certify a device.
    Missing, repeated, conflicting, or novel fields are intentionally unknown.
    """
    fields: dict[str, str] = {}
    issues: list[str] = []
    patterns = {
        "enabled": r"^\s*mCurrentState=DesktopModeState\{[^\r\n}]*\benabled=([0-9]+)[^\r\n}]*\}",
        "dual": r"^\s*mDualModeEnabled=(true|false)\s*$",
        "standalone": r"^\s*mStandaloneModeEnabled=(true|false)\s*$",
        "display_id": r"^\s*mDesktopDisplayId=(-?[0-9]+)\s*$",
    }
    for field, pattern in patterns.items():
        value, issue = _one_value(text, pattern, field)
        if issue:
            issues.append(issue)
        elif value is not None:
            fields[field] = value.casefold()

    if issues:
        return {
            "status": "unknown",
            "classification": "unknown",
            "desktop_display_id": None,
            "issues": issues,
        }

    enabled = int(fields["enabled"])
    dual = fields["dual"] == "true"
    standalone = fields["standalone"] == "true"
    display_id = int(fields["display_id"])

    if enabled == 4 and dual and not standalone and display_id > 0:
        classification = "active_dual_evidence"
    elif enabled == 4 and standalone and not dual and display_id == 0:
        classification = "active_standalone_evidence"
    elif enabled != 4 and not dual and not standalone and display_id == -1:
        classification = "inactive"
    else:
        return {
            "status": "unknown",
            "classification": "unknown",
            "desktop_display_id": None,
            "issues": ["recognized desktop-mode fields form an inconsistent or unrecognized state"],
        }

    return {
        "status": "observed",
        "classification": classification,
        "desktop_display_id": display_id,
        "recognized_fields": {
            "enabled": enabled,
            "dual_mode": dual,
            "standalone_mode": standalone,
            "desktop_display_id": display_id,
        },
        "issues": [],
    }


def parse_logical_displays(text: str) -> dict[str, Any]:
    """Parse only IDs in the ``Logical Displays`` section of dumpsys.

    Physical device mode IDs and supported-mode IDs are deliberately ignored.
    """
    section_match = _LOGICAL_SECTION_RE.search(text or "")
    if not section_match:
        return {
            "status": "unknown",
            "items": [],
            "reason": "Unrecognized dumpsys display format: Logical Displays section missing.",
        }
    section = text[section_match.end() :]
    # A dumpsys report often has later sections containing their own display
    # numbers.  Only the indented blocks before the next top-level heading are
    # part of Logical Displays.
    next_section = _TOP_LEVEL_SECTION_RE.search(section)
    if next_section:
        section = section[: next_section.start()]
    headers = list(_LOGICAL_HEADER_RE.finditer(section))
    if not headers:
        return {
            "status": "unknown",
            "items": [],
            "reason": "Unrecognized dumpsys display format: no logical display blocks found.",
        }

    items: list[dict[str, Any]] = []
    seen: set[int] = set()
    for index, header in enumerate(headers):
        block_end = headers[index + 1].start() if index + 1 < len(headers) else len(section)
        block = section[header.end() : block_end]
        header_id = int(header.group(1))
        raw_ids = re.findall(r"(?m)^\s*mDisplayId\s*=\s*(-?[0-9]+)\s*$", block)
        if len(raw_ids) != 1 or int(raw_ids[0]) != header_id or header_id in seen:
            return {
                "status": "unknown",
                "items": [],
                "reason": "Logical display IDs were missing, duplicated, conflicting, or repeated.",
            }
        seen.add(header_id)

        name_match = re.search(r'DisplayInfo\{"([^"]*)"', block)
        resolution_match = re.search(r"\breal\s+([0-9]+)\s*x\s*([0-9]+)\b", block)
        density_match = re.search(r"\bdensity\s+([0-9]+)\b", block)
        flags = sorted(set(re.findall(r"\bFLAG_[A-Z0-9_]+\b", block)))
        items.append(
            {
                "id": header_id,
                "primary": header_id == 0,
                "name": name_match.group(1) if name_match else None,
                "resolution": (
                    {"width": int(resolution_match.group(1)), "height": int(resolution_match.group(2))}
                    if resolution_match
                    else None
                ),
                "density_dpi": int(density_match.group(1)) if density_match else None,
                "flags": flags,
                "metadata_status": "observed" if name_match else "partial",
            }
        )

    return {"status": "observed", "items": items, "reason": None}


def _platform_status(sdk_text: str | None) -> tuple[int | None, str]:
    if sdk_text is None or not re.fullmatch(r"[0-9]{1,3}", sdk_text.strip()):
        return None, "unknown"
    api = int(sdk_text)
    if api < MIN_WORKFLOW_API:
        return api, "below_minimum"
    if api > MAX_RESEARCHED_API:
        return api, "unverified_future"
    return api, "eligible"


@operation("Inspecting DeX and displays")
def inspect_device(
    serial: str,
    *,
    adb_path: Path | str | None = None,
    runner: ProcessRunner | None = None,
    preflight: Preflight | None = None,
) -> dict[str, Any]:
    """Run one bounded, read-only inspection against one exact target."""
    target = _validate_serial(serial)
    adb = _resolve_adb(adb_path)
    run = runner or _run_process
    gate = preflight or preflight_adb_server
    gate_args = ["-s", target, "shell", "getprop", "ro.build.version.sdk"]
    try:
        gate(adb, gate_args)
    except UnsafeAdbServerError as exc:
        raise DexError(
            "unsafe_adb_server", "ADB server ownership/version could not be verified."
        ) from exc

    property_keys = {
        "manufacturer": "ro.product.manufacturer",
        "model": "ro.product.model",
        "sdk": "ro.build.version.sdk",
    }
    properties: dict[str, str | None] = {}
    property_status: dict[str, str] = {}
    for label, key in property_keys.items():
        probe = _probe_adb(adb, target, ["shell", "getprop", key], runner=run)
        properties[label] = probe.stdout if probe.status == "observed" and probe.stdout else None
        property_status[label] = probe.status

    desktop_probe = _probe_adb(adb, target, ["shell", "dumpsys", "desktopmode"], runner=run)
    if desktop_probe.status == "observed":
        desktop = parse_desktop_mode(desktop_probe.stdout)
        desktop["service_status"] = "observed"
    else:
        desktop = {
            "status": "unknown",
            "service_status": "unknown",
            "classification": "unknown",
            "desktop_display_id": None,
            "issues": [desktop_probe.reason or "desktopmode service could not be inspected"],
        }

    display_probe = _probe_adb(adb, target, ["shell", "dumpsys", "display"], runner=run)
    if display_probe.status == "observed":
        displays = parse_logical_displays(display_probe.stdout)
    else:
        displays = {
            "status": "unknown",
            "items": [],
            "reason": display_probe.reason or "display service could not be inspected",
        }

    api, workflow_status = _platform_status(properties["sdk"])
    is_samsung = bool(
        properties["manufacturer"]
        and properties["manufacturer"].strip().casefold() == "samsung"
    )
    classification = desktop["classification"]
    confirmed_display_id: int | None = None
    confirmation_reason: str
    observed_ids = {item["id"] for item in displays["items"]} if displays["status"] == "observed" else set()

    if not is_samsung:
        classification = "unknown"
        confirmation_reason = "Samsung DeX cannot be confirmed on a non-Samsung manufacturer identity."
    elif classification == "active_dual_evidence":
        candidate_id = desktop["desktop_display_id"]
        if candidate_id in observed_ids and candidate_id != 0:
            classification = "active_dual"
            confirmed_display_id = candidate_id
            confirmation_reason = "Recognized active dual-mode state matches an existing non-primary logical display."
        else:
            classification = "unknown"
            confirmation_reason = "Desktop-mode display ID did not match an existing non-primary logical display."
    elif classification == "active_standalone_evidence":
        if 0 in observed_ids:
            classification = "active_standalone"
            confirmed_display_id = 0
            confirmation_reason = "Recognized standalone state uses display 0; it is not a distinct dual-mirror surface."
        else:
            classification = "unknown"
            confirmation_reason = "Standalone state referenced missing logical display 0."
    elif classification == "inactive":
        confirmation_reason = "Recognized desktop-mode fields report DeX inactive."
    else:
        confirmation_reason = "Runtime DeX state is unknown; secondary displays alone are not DeX proof."

    return {
        "schema_version": SCHEMA_VERSION,
        "command": "dex inspect",
        "serial": target,
        "device": {
            "manufacturer": properties["manufacturer"],
            "model": properties["model"],
            "api_level": api,
            "property_status": property_status,
        },
        "workflow": {
            "minimum_api": MIN_WORKFLOW_API,
            "maximum_researched_api": MAX_RESEARCHED_API,
            "status": workflow_status,
            "generic_secondary_display_control_minimum_api": 29,
        },
        "dex": {
            **desktop,
            "classification": classification,
            "confirmed": classification in {"active_dual", "active_standalone"},
            "confirmed_display_id": confirmed_display_id,
            "confirmation_reason": confirmation_reason,
        },
        "displays": displays,
        "mutations": [],
        "claim_boundary": (
            "This read-only snapshot is runtime evidence only for the queried state. "
            "It is not model-wide certification, and an external display alone does not prove Samsung DeX."
        ),
    }


def list_displays(
    serial: str,
    *,
    adb_path: Path | str | None = None,
    runner: ProcessRunner | None = None,
    preflight: Preflight | None = None,
) -> dict[str, Any]:
    report = inspect_device(
        serial, adb_path=adb_path, runner=runner, preflight=preflight
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "command": "dex list-displays",
        "serial": report["serial"],
        "status": report["displays"]["status"],
        "displays": report["displays"]["items"],
        "reason": report["displays"].get("reason"),
        "confirmed_dex_display_id": (
            report["dex"]["confirmed_display_id"]
            if report["dex"]["classification"] == "active_dual"
            else None
        ),
        "note": "Display IDs are runtime values. Select an observed nonzero ID explicitly.",
    }


def _build_plan(
    report: dict[str, Any],
    dex_display: int,
    *,
    scrcpy_executable: str,
    view_only: bool,
) -> dict[str, Any]:
    if report["workflow"]["status"] == "below_minimum":
        raise DexError(
            "unsupported_api",
            "The bounded DeX dual-mirror workflow requires Android 13 / API 33 or newer; "
            "scrcpy documents generic secondary-display control separately from Android 10.",
        )
    if dex_display == 0:
        raise DexError(
            "primary_duplicate",
            "Display 0 is the primary surface. Standalone DeX on display 0 cannot provide two distinct phone and DeX surfaces.",
        )
    if dex_display < 0:
        raise DexError("invalid_display", "--dex-display must be an observed nonnegative logical display ID.")
    if report["displays"]["status"] != "observed":
        raise DexError(
            "unknown_display_format",
            f"Display discovery is unknown: {report['displays'].get('reason') or 'inspect the current dumpsys format'}",
        )
    items = report["displays"]["items"]
    ids = [item["id"] for item in items]
    if len(ids) != len(set(ids)):
        raise DexError("duplicate_display", "Display discovery returned duplicate logical display IDs.")
    if 0 not in ids:
        raise DexError("missing_primary", "Logical display 0 was not observed; refusing an incomplete dual-mirror plan.")
    if dex_display not in ids:
        raise DexError(
            "missing_display",
            f"Logical display {dex_display} is not currently present; run 'adapt dex list-displays -s SERIAL'.",
        )

    common = ["-s", report["serial"]]
    phone_argv = [scrcpy_executable, *common, "--display-id=0", "--no-audio"]
    desktop_argv = [scrcpy_executable, *common, f"--display-id={dex_display}", "--no-audio"]
    if view_only:
        phone_argv.append("--no-control")
        desktop_argv.append("--no-control")

    confirmed = (
        report["dex"]["classification"] == "active_dual"
        and report["dex"]["confirmed_display_id"] == dex_display
    )
    phone_argv.extend(["--window-title", "ADAPT Phone / display 0"])
    desktop_argv.extend(["--window-title", f"ADAPT {'DeX' if confirmed else 'External'} / display {dex_display}"])
    selected = next(item for item in items if item["id"] == dex_display)
    return {
        "schema_version": SCHEMA_VERSION,
        "command": "dex plan-dual-mirror",
        "serial": report["serial"],
        "selected_display": selected,
        "target_classification": (
            "confirmed_active_samsung_dex" if confirmed else "external_display_candidate_unconfirmed"
        ),
        "runtime_dex_confirmed": confirmed,
        "workflow_verification": report["workflow"]["status"],
        "view_only": view_only,
        "instances": [
            {"surface": "phone_primary", "display_id": 0, "argv": phone_argv},
            {"surface": "selected_existing_display", "display_id": dex_display, "argv": desktop_argv},
        ],
        "notes": [
            "Both instances disable audio as a conservative duplicate-capture policy, not as mandatory crash prevention.",
            "No fixed ports are assigned; scrcpy manages concurrent session transport.",
            "No virtual display is created and this plan does not launch a process.",
            (
                "Runtime evidence confirms the selected existing display is the active DeX dual-mode display."
                if confirmed
                else "The selected display is only an existing-display candidate; inspect must positively confirm active DeX before launch."
            ),
        ],
    }


def plan_dual_mirror(
    serial: str,
    dex_display: int,
    *,
    adb_path: Path | str | None = None,
    runner: ProcessRunner | None = None,
    preflight: Preflight | None = None,
    scrcpy_executable: str = "scrcpy",
) -> dict[str, Any]:
    report = inspect_device(
        serial, adb_path=adb_path, runner=runner, preflight=preflight
    )
    return _build_plan(
        report,
        dex_display,
        scrcpy_executable=scrcpy_executable,
        view_only=False,
    )


def resolve_scrcpy(explicit: Path | str | None = None) -> Path:
    """Resolve an explicit scrcpy client or one from PATH; never install it."""
    if explicit is not None:
        candidate = Path(explicit).expanduser()
        if candidate.is_file():
            return candidate.resolve()
        located = shutil.which(str(explicit))
        if located:
            return Path(located).resolve()
        raise DexError("missing_scrcpy", f"scrcpy was not found at {explicit!s}.")
    from adapt_adb.scrcpy_install import resolve_scrcpy_binary
    located = resolve_scrcpy_binary()
    if not located:
        raise DexError("missing_scrcpy", "scrcpy was not found on PATH or in the managed installation; use tools install scrcpy or pass --scrcpy PATH.")
    return Path(located).resolve()


def _check_scrcpy_version(scrcpy: Path, runner: ProcessRunner) -> str:
    try:
        result = runner([str(scrcpy), "--version"], HOST_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise DexError("scrcpy_version", "scrcpy version check timed out.") from exc
    except OSError as exc:
        raise DexError("scrcpy_version", f"scrcpy version check could not start: {exc}") from exc
    output = "\n".join(part for part in (result.stdout or "", result.stderr or "") if part)
    match = _SCRCPY_VERSION_RE.search(output) if result.returncode == 0 else None
    if not match:
        raise DexError("scrcpy_version", "scrcpy version could not be verified.")
    parsed = tuple(int(part or 0) for part in match.groups())
    if parsed < MIN_SCRCPY_VERSION:
        raise DexError(
            "scrcpy_version",
            "This launch shape requires scrcpy 2.0+ for the explicit --no-audio policy.",
        )
    return ".".join(str(part) for part in parsed)


def _stop_owned_process(process: Any) -> None:
    """Best-effort cleanup for a process created by this invocation only."""
    try:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    except (OSError, AttributeError, subprocess.SubprocessError):
        pass


def mirror_dual(
    serial: str,
    dex_display: int,
    *,
    yes: bool = False,
    view_only: bool = False,
    scrcpy_path: Path | str | None = None,
    adb_path: Path | str | None = None,
    runner: ProcessRunner | None = None,
    preflight: Preflight | None = None,
    popen_factory: PopenFactory | None = None,
    scrcpy_resolver: ScrcpyResolver | None = None,
) -> dict[str, Any]:
    """Launch two owned scrcpy processes only after positive matching evidence."""
    if not yes:
        raise DexError(
            "confirmation_required",
            "Mirror launch requires explicit --yes. Use plan-dual-mirror for a side-effect-free plan.",
        )

    run = runner or _run_process
    report = inspect_device(
        serial, adb_path=adb_path, runner=run, preflight=preflight
    )
    adb = _resolve_adb(adb_path)
    resolver = scrcpy_resolver or resolve_scrcpy
    scrcpy = resolver(scrcpy_path)
    plan = _build_plan(
        report,
        dex_display,
        scrcpy_executable=str(scrcpy),
        view_only=view_only,
    )
    if report["workflow"]["status"] != "eligible":
        raise DexError(
            "unverified_platform",
            "Launch is limited to the researched Android 13-17 / API 33-37 range; inspect and plan remain available.",
        )
    if not plan["runtime_dex_confirmed"]:
        raise DexError(
            "dex_unconfirmed",
            "Active Samsung DeX was not positively confirmed on the selected display; run inspect and plan-dual-mirror.",
        )
    version = _check_scrcpy_version(scrcpy, run)

    env = dict(os.environ)
    env["ADB"] = str(adb)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    start = popen_factory or _popen_process
    started: list[Any] = []
    try:
        for instance in plan["instances"]:
            process = start(
                instance["argv"],
                env=env,
                creationflags=creationflags,
                shell=False,
            )
            started.append(process)
    except (OSError, subprocess.SubprocessError) as exc:
        for process in reversed(started):
            _stop_owned_process(process)
        raise DexError(
            "launch_failed",
            f"scrcpy launch failed after starting {len(started)} owned process(es); only those processes were cleaned up.",
        ) from exc

    return {
        **plan,
        "command": "dex mirror",
        "status": "processes_started",
        "launched_processes": len(started),
        "process_ids": [getattr(process, "pid", None) for process in started],
        "scrcpy_version": version,
        "adb_override": str(adb),
        "healthy_streaming": "not_verified",
        "health_note": "Process creation succeeded; this command does not claim that video or input became healthy.",
    }


def _render_inspection(report: dict[str, Any]) -> str:
    device = report["device"]
    dex = report["dex"]
    lines = [
        f"{device['manufacturer'] or 'unknown'} {device['model'] or 'unknown'} (API {device['api_level'] or 'unknown'})",
        f"Workflow: {report['workflow']['status']}",
        f"DeX state: {dex['classification']}",
        dex["confirmation_reason"],
        f"Logical displays: {', '.join(str(item['id']) for item in report['displays']['items']) or 'unknown'}",
        report["claim_boundary"],
    ]
    return "\n".join(lines)


def _render_displays(report: dict[str, Any]) -> str:
    if report["status"] != "observed":
        return f"Display discovery: UNKNOWN - {report['reason']}"
    lines = ["ID  Primary  Resolution  Density  Name"]
    for item in report["displays"]:
        resolution = item["resolution"]
        size = f"{resolution['width']}x{resolution['height']}" if resolution else "unknown"
        confirmed = " [confirmed active DeX]" if item["id"] == report["confirmed_dex_display_id"] else ""
        lines.append(
            f"{item['id']:<3} {'yes' if item['primary'] else 'no':<8} {size:<11} "
            f"{str(item['density_dpi'] or 'unknown'):<7} {item['name'] or 'unknown'}{confirmed}"
        )
    lines.append(report["note"])
    return "\n".join(lines)


def _render_plan(plan: dict[str, Any]) -> str:
    lines = [
        f"Target: {plan['target_classification']}",
        f"Workflow verification: {plan['workflow_verification']}",
    ]
    for instance in plan["instances"]:
        lines.append(f"{instance['surface']}: {json.dumps(instance['argv'])}")
    lines.extend(plan["notes"])
    return "\n".join(lines)


def _emit_error(exc: DexError, *, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps({"ok": False, "error": exc.kind, "message": str(exc)}, sort_keys=True))
    else:
        typer.echo(f"Error: {exc}", err=True)


dex_app = typer.Typer(
    name="dex",
    help="Inspect Samsung DeX displays and explicitly launch selected phone/desktop mirrors.",
    no_args_is_help=True,
)


@dex_app.command("inspect")
def inspect_command(
    serial: str = typer.Option(..., "--serial", "-s", help="Explicit complete ADB target."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Read identity, desktop-mode state, and logical displays."""
    try:
        report = inspect_device(serial)
    except DexError as exc:
        _emit_error(exc, json_output=json_output)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(report, sort_keys=True) if json_output else _render_inspection(report))


@dex_app.command("list-displays")
def list_displays_command(
    serial: str = typer.Option(..., "--serial", "-s", help="Explicit complete ADB target."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List currently observed logical display IDs and metadata."""
    try:
        report = list_displays(serial)
    except DexError as exc:
        _emit_error(exc, json_output=json_output)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(report, sort_keys=True) if json_output else _render_displays(report))


@dex_app.command("plan-dual-mirror")
def plan_dual_mirror_command(
    serial: str = typer.Option(..., "--serial", "-s", help="Explicit complete ADB target."),
    dex_display: int = typer.Option(..., "--dex-display", help="Existing nonzero logical display ID."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Return two exact scrcpy argv arrays without launching either."""
    try:
        plan = plan_dual_mirror(serial, dex_display)
    except DexError as exc:
        _emit_error(exc, json_output=json_output)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(plan, sort_keys=True) if json_output else _render_plan(plan))


@dex_app.command("mirror")
def mirror_command(
    serial: str = typer.Option(..., "--serial", "-s", help="Explicit complete ADB target."),
    dex_display: int = typer.Option(..., "--dex-display", help="Confirmed active DeX logical display ID."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Explicitly approve launching two scrcpy windows."),
    view_only: bool = typer.Option(False, "--view-only", help="Disable control on both windows."),
    scrcpy: Path | None = typer.Option(None, "--scrcpy", help="Explicit scrcpy executable path."),
) -> None:
    """Launch phone and confirmed active dual-mode DeX mirrors."""
    try:
        result = mirror_dual(
            serial,
            dex_display,
            yes=yes,
            view_only=view_only,
            scrcpy_path=scrcpy,
        )
    except DexError as exc:
        _emit_error(exc, json_output=False)
        raise typer.Exit(1) from exc
    typer.echo(
        f"Started {result['launched_processes']} scrcpy processes; healthy streaming is {result['healthy_streaming']}."
    )
    typer.echo(result["health_note"])


__all__ = [
    "DexError",
    "MAX_RESEARCHED_API",
    "MIN_WORKFLOW_API",
    "dex_app",
    "inspect_device",
    "list_displays",
    "mirror_dual",
    "parse_desktop_mode",
    "parse_logical_displays",
    "plan_dual_mirror",
    "resolve_scrcpy",
]
