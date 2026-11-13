"""Android 10-17 capability planning and bounded read-only inspection.

The version matrix is deliberately about eligibility, not certification.  A
reported SDK integer never proves that an OEM build, a host tool, or a
particular runtime path implements a feature successfully.
"""

from __future__ import annotations

from adapt_adb.progress import operation

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping

import typer

from adapt_adb.doctor import UnsafeAdbServerError, preflight_adb_server


SCHEMA_VERSION = 1
SOURCE_CHECKED_DATE = "2026-11-12"
MIN_SUPPORTED_API = 29
MAX_VERIFIED_API = 37
ADB_TIMEOUT_SECONDS = 10
HOST_PROBE_TIMEOUT_SECONDS = 5

_SERIAL_RE = re.compile(r"^[^\x00-\x1f\x7f-\x9f]{1,1024}$")
_SDK_RE = re.compile(r"^[0-9]{1,3}$")
_RELEASE_MAJOR_RE = re.compile(r"^\s*([0-9]+)")
_SCRCPY_VERSION_RE = re.compile(r"\bscrcpy\s+v?([0-9]+)\.([0-9]+)(?:\.([0-9]+))?\b", re.I)
_DENIED_RE = re.compile(
    r"permission denial|securityexception|security exception|not allowed|"
    r"operation not permitted|access denied",
    re.IGNORECASE,
)
_ERROR_TEXT_RE = re.compile(
    r"(?:^|\n)\s*(?:adb:\s*)?(?:error|failure)\s*[:\[]|"
    r"(?:^|\n)\s*(?:device\s+)?(?:unauthorized|offline)\s*$",
    re.IGNORECASE,
)

# Android 12 has two API levels: 31 for the initial release and 32 for the
# 12L/12.1 platform update.  Android's release property is OEM-controlled, so
# the release/API consistency check is advisory only.
ANDROID_VERSIONS: tuple[dict[str, Any], ...] = (
    {"api": 29, "android": "10", "codename": "Q", "release_status": "released"},
    {"api": 30, "android": "11", "codename": "R", "release_status": "released"},
    {"api": 31, "android": "12", "codename": "S", "release_status": "released"},
    {"api": 32, "android": "12L", "codename": "S_V2", "release_status": "released"},
    {"api": 33, "android": "13", "codename": "TIRAMISU", "release_status": "released"},
    {"api": 34, "android": "14", "codename": "UPSIDE_DOWN_CAKE", "release_status": "released"},
    {"api": 35, "android": "15", "codename": "VANILLA_ICE_CREAM", "release_status": "released"},
    {"api": 36, "android": "16", "codename": "BAKLAVA", "release_status": "released"},
    {"api": 37, "android": "17", "codename": "CINNAMON_BUN", "release_status": "released"},
)
_VERSION_BY_API = {item["api"]: item for item in ANDROID_VERSIONS}
_EXPECTED_RELEASE_MAJOR = {
    29: 10,
    30: 11,
    31: 12,
    32: 12,
    33: 13,
    34: 14,
    35: 15,
    36: 16,
    37: 17,
}

_FEATURES: tuple[dict[str, Any], ...] = (
    {
        "id": "usb_adb",
        "label": "USB ADB",
        "min_api": 1,
        "default": "version_eligible",
        "boundary": "Requires user-enabled debugging, authorization, a working cable/port, and host driver or udev access; the explicit target probe does not reveal its transport.",
    },
    {
        "id": "wireless_pairing",
        "label": "Pairing-based wireless ADB",
        "min_api": 30,
        "default": "version_eligible",
        "boundary": "Phone support starts at Android 11; TV and Wear have later documented floors. Pairing, network, mDNS, and user consent remain runtime requirements.",
    },
    {
        "id": "adb_wifi_2",
        "label": "ADB Wi-Fi 2.0",
        "min_api": 37,
        "default": "version_eligible",
        "boundary": "Requires Android 17, adb 37.0.0 or newer, and runtime mDNS service-version evidence.",
    },
    {
        "id": "legacy_tcp_adb",
        "label": "Legacy USB-first TCP ADB",
        "min_api": 1,
        "default": "version_eligible",
        "boundary": "Android 10 and lower use the documented USB-first adb tcpip workflow; ADAPT inspection never enables TCP mode automatically.",
    },
    {
        "id": "screenshot",
        "label": "ADB screenshot",
        "min_api": 1,
        "default": "version_eligible",
        "boundary": "Secure/protected surfaces can be blank or omitted; eligibility does not prove a useful capture.",
    },
    {
        "id": "uiautomator",
        "label": "UI Automator hierarchy/interaction",
        "min_api": 18,
        "default": "version_eligible",
        "boundary": "UI Automator exposes visible accessibility-backed elements; non-accessible/custom-rendered content may have limited or no node data.",
    },
    {
        "id": "scrcpy_audio",
        "label": "scrcpy audio forwarding",
        "min_api": 30,
        "default": "version_eligible",
        "boundary": "scrcpy supports audio on Android 11+; Android 11 must be unlocked at startup, and encoder/runtime/tool regressions can still fail.",
    },
    {
        "id": "existing_display_mirroring",
        "label": "Mirror an existing secondary display",
        "min_api": 1,
        "default": "unknown_oem_dependent",
        "boundary": "Requires an existing enumerable display plus compatible capture behavior; this generic capability is not Samsung DeX certification.",
    },
    {
        "id": "new_virtual_display",
        "label": "Create and mirror a new virtual display",
        "min_api": 29,
        "default": "unknown_oem_dependent",
        "boundary": "Requires a compatible scrcpy 3.0+ host and device/OEM display behavior; launcher, decorations, focus, and app behavior vary.",
    },
    {
        "id": "input_routing",
        "label": "Route input to mirrored/virtual displays",
        "min_api": 29,
        "default": "unknown_oem_dependent",
        "boundary": "Injection permission, display focus, input mode, Android release, scrcpy release, and OEM behavior all matter; Android 15+ is the documented floor for the scrcpy 3.3 UHID pointer fix on new displays.",
    },
    {
        "id": "developer_options_keys",
        "label": "Developer Options settings-key availability",
        "min_api": 1,
        "default": "unknown_oem_dependent",
        "boundary": "Settings paths/names and hidden keys can differ by device and OEM; availability must be read on the target and does not prove writability.",
    },
)


class AndroidSupportError(RuntimeError):
    """A classified error whose message is safe to expose without raw ADB output."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def resolve_adb() -> Path | None:
    """Resolve adb lazily so tests and parent integrations can replace the helper."""
    from adapt_adb.adb import resolve_adb as project_resolve_adb

    return project_resolve_adb()


def _parse_sdk(sdk: int | str) -> int:
    if isinstance(sdk, bool):
        raise AndroidSupportError("malformed_sdk", "Device reported a malformed Android SDK level.")
    if isinstance(sdk, int):
        value = sdk
    elif isinstance(sdk, str) and _SDK_RE.fullmatch(sdk):
        value = int(sdk)
    else:
        raise AndroidSupportError("malformed_sdk", "Device reported a malformed Android SDK level.")
    if value < 1:
        raise AndroidSupportError("malformed_sdk", "Device reported a malformed Android SDK level.")
    return value


def _clean_optional_identity(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _observed(evidence: Mapping[str, Any] | None, feature_id: str) -> tuple[bool | None, str | None]:
    if not evidence or feature_id not in evidence:
        return None, None
    raw = evidence[feature_id]
    if isinstance(raw, bool):
        return raw, "caller-provided runtime evidence"
    if isinstance(raw, Mapping):
        observed = raw.get("observed")
        source = raw.get("source")
        return (
            observed if isinstance(observed, bool) else None,
            source if isinstance(source, str) and source.strip() else "caller-provided runtime evidence",
        )
    return None, None


def evaluate_capabilities(
    sdk: int | str,
    release: str | None = None,
    manufacturer: str | None = None,
    model: str | None = None,
    *,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Purely evaluate version eligibility and optional runtime observations.

    ``evidence`` is intentionally explicit.  A value of ``True`` (or a mapping
    with ``{"observed": True}``) records actual caller-supplied runtime
    evidence.  SDK thresholds alone never produce ``observed_runtime_evidence``.
    """
    api = _parse_sdk(sdk)
    release_value = _clean_optional_identity(release)
    manufacturer_value = _clean_optional_identity(manufacturer)
    model_value = _clean_optional_identity(model)

    if api < MIN_SUPPORTED_API:
        coverage = "unsupported_baseline"
    elif api > MAX_VERIFIED_API:
        coverage = "unknown_future_os"
    else:
        coverage = "covered_version"

    version_row = _VERSION_BY_API.get(api)
    release_major_match: bool | None = None
    warnings: list[str] = []
    if version_row is not None and release_value is not None:
        match = _RELEASE_MAJOR_RE.match(release_value)
        release_major_match = bool(
            match and int(match.group(1)) == _EXPECTED_RELEASE_MAJOR[api]
        )
        if not release_major_match:
            warnings.append(
                "Reported Android release does not match the authoritative API-level mapping; capability thresholds use the SDK level and the mismatch remains unresolved."
            )

    capabilities: dict[str, dict[str, Any]] = {}
    for spec in _FEATURES:
        feature_id = str(spec["id"])
        minimum = int(spec["min_api"])
        version_eligible: bool | None
        if api > MAX_VERIFIED_API:
            status = "unknown_future_os"
            version_eligible = None
        elif api < minimum:
            status = "unsupported_by_os"
            version_eligible = False
        elif api < MIN_SUPPORTED_API:
            status = "unsupported_by_baseline"
            version_eligible = True
        else:
            status = str(spec["default"])
            version_eligible = True

        runtime_observed, evidence_source = _observed(evidence, feature_id)
        evidence_conflict = False
        if runtime_observed is True:
            evidence_conflict = status in {"unsupported_by_os", "unsupported_by_baseline"}
            status = "observed_runtime_evidence"

        capabilities[feature_id] = {
            "label": spec["label"],
            "status": status,
            "version_eligible": version_eligible,
            "minimum_api": minimum,
            "runtime_observed": runtime_observed,
            "evidence_source": evidence_source,
            "evidence_conflict": evidence_conflict,
            "boundary": spec["boundary"],
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "source_checked": SOURCE_CHECKED_DATE,
        "mode": "offline_evaluation",
        "platform": {
            "sdk": api,
            "reported_release": release_value,
            "android_version": version_row["android"] if version_row else None,
            "codename": version_row["codename"] if version_row else None,
            "release_status": version_row["release_status"] if version_row else None,
            "coverage": coverage,
            "release_major_matches_api": release_major_match,
        },
        "device": {"manufacturer": manufacturer_value, "model": model_value},
        "capabilities": capabilities,
        "warnings": warnings,
        "claim_boundary": "Version eligibility and host availability are not native API-per-version runtime acceptance.",
    }


def version_matrix() -> dict[str, Any]:
    """Return the authoritative offline planning range without probing a host."""
    return {
        "schema_version": SCHEMA_VERSION,
        "source_checked": SOURCE_CHECKED_DATE,
        "minimum_supported_api": MIN_SUPPORTED_API,
        "maximum_verified_api": MAX_VERIFIED_API,
        "versions": [dict(item) for item in ANDROID_VERSIONS],
        "claim_boundary": "The matrix maps released platform versions to API levels; it does not certify ADAPT on every version or OEM build.",
    }


def _validate_serial(serial: str) -> str:
    if not isinstance(serial, str) or not serial.strip():
        raise AndroidSupportError("invalid_serial", "A nonblank explicit -s/--serial target is required.")
    if serial != serial.strip() or serial.startswith("-") or not _SERIAL_RE.fullmatch(serial):
        raise AndroidSupportError("invalid_serial", "The explicit ADB target contains unsupported characters.")
    return serial


def _run_process(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _checked_adb_property(adb_path: Path, serial: str, key: str) -> str:
    command = [str(adb_path), "-s", serial, "shell", "getprop", key]
    try:
        result = _run_process(command, ADB_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise AndroidSupportError("timeout", "ADB property inspection timed out.") from exc
    except FileNotFoundError as exc:
        raise AndroidSupportError("missing_adb", "adb binary was not found; run 'adapt setup'.") from exc
    except OSError as exc:
        raise AndroidSupportError("adb_error", "ADB property inspection could not start.") from exc

    stdout = result.stdout or ""
    stderr = result.stderr or ""
    combined = "\n".join(part for part in (stdout, stderr) if part).strip()
    if _DENIED_RE.search(combined):
        raise AndroidSupportError("denied", "ADB property inspection was denied.")
    if result.returncode != 0 or _ERROR_TEXT_RE.search(combined):
        raise AndroidSupportError("adb_error", "ADB property inspection failed.")

    value = stdout.strip()
    if len(value) > 512 or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
        raise AndroidSupportError("malformed_property", "Device reported a malformed Android property.")
    return value


def _parse_scrcpy_version(output: str) -> tuple[int, int, int] | None:
    match = _SCRCPY_VERSION_RE.search(output)
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))


def _probe_scrcpy() -> dict[str, Any]:
    from adapt_adb.scrcpy_install import resolve_scrcpy_binary
    executable = resolve_scrcpy_binary()
    if not executable:
        return {
            "available": False,
            "version": None,
            "version_parsed": False,
            "audio_client_eligible": False,
            "virtual_display_client_eligible": False,
        }
    try:
        result = _run_process([str(executable), "--version"], HOST_PROBE_TIMEOUT_SECONDS)
    except (subprocess.TimeoutExpired, OSError):
        return {
            "available": True,
            "version": None,
            "version_parsed": False,
            "audio_client_eligible": None,
            "virtual_display_client_eligible": None,
        }
    combined = "\n".join(part for part in (result.stdout, result.stderr) if part)
    parsed = _parse_scrcpy_version(combined) if result.returncode == 0 else None
    version_text = ".".join(str(part) for part in parsed) if parsed else None
    return {
        "available": True,
        "version": version_text,
        "version_parsed": parsed is not None,
        "audio_client_eligible": parsed >= (2, 0, 0) if parsed else None,
        "virtual_display_client_eligible": parsed >= (3, 0, 0) if parsed else None,
    }


@operation("Inspecting Android capabilities")
def inspect_capabilities(
    serial: str,
    *,
    adb_path: Path | str | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Inspect one explicit target using bounded read-only property queries.

    The target is never enumerated, guessed, or normalized.  It is passed as a
    single argv element immediately after ``-s`` for every device command.
    """
    target = _validate_serial(serial)
    resolved = Path(adb_path) if adb_path is not None else resolve_adb()
    if resolved is None:
        raise AndroidSupportError("missing_adb", "adb binary not found; run 'adapt setup'.")

    gate_args = ["-s", target, "shell", "getprop", "ro.build.version.sdk"]
    try:
        preflight_adb_server(Path(resolved), gate_args)
    except UnsafeAdbServerError as exc:
        raise AndroidSupportError(
            "unsafe_adb_server", "ADB server ownership/version could not be verified."
        ) from exc

    properties = {
        "sdk": _checked_adb_property(Path(resolved), target, "ro.build.version.sdk"),
        "release": _checked_adb_property(Path(resolved), target, "ro.build.version.release"),
        "manufacturer": _checked_adb_property(Path(resolved), target, "ro.product.manufacturer"),
        "model": _checked_adb_property(Path(resolved), target, "ro.product.model"),
    }
    report = evaluate_capabilities(
        properties["sdk"],
        properties["release"],
        properties["manufacturer"],
        properties["model"],
        evidence=evidence,
    )
    report["command"] = "android capabilities"
    report["mode"] = "read_only_inspection"
    report["inspection"] = {
        "target_selection": "explicit",
        "target_value_returned": False,
        "transport_observed": "adb_target_reachable",
        "transport_kind": "unknown",
        "properties_observed": [
            "ro.build.version.sdk",
            "ro.build.version.release",
            "ro.product.manufacturer",
            "ro.product.model",
        ],
        "mutations": [],
    }
    report["host"] = {"scrcpy": _probe_scrcpy()}
    return report


def _render_versions(report: dict[str, Any]) -> str:
    lines = [
        f"ADAPT Android planning baseline (checked {report['source_checked']})",
        "API  Android  Codename             Release status",
    ]
    for item in report["versions"]:
        lines.append(
            f"{item['api']:>3}  {item['android']:<7}  {item['codename']:<19}  {item['release_status']}"
        )
    lines.append(report["claim_boundary"])
    return "\n".join(lines)


def _render_capabilities(report: dict[str, Any]) -> str:
    platform = report["platform"]
    lines = [
        f"Android {platform['android_version'] or 'unknown'} / API {platform['sdk']}",
        f"Coverage: {platform['coverage']}",
    ]
    for feature_id, feature in report["capabilities"].items():
        lines.append(f"  {feature_id}: {feature['status']}")
    for warning in report["warnings"]:
        lines.append(f"Warning: {warning}")
    lines.append(report["claim_boundary"])
    return "\n".join(lines)


def _emit_error(exc: AndroidSupportError, *, json_output: bool) -> None:
    if json_output:
        typer.echo(
            json.dumps(
                {"ok": False, "error": exc.kind, "message": str(exc)},
                sort_keys=True,
            )
        )
    else:
        typer.echo(f"Error: {exc}", err=True)


android_app = typer.Typer(
    name="android",
    help="Plan and inspect Android 10-17 capability eligibility.",
    no_args_is_help=True,
)


@android_app.command("capabilities")
def capabilities_command(
    serial: str = typer.Option(..., "--serial", "-s", help="Explicit complete ADB target."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Read Android identity properties and evaluate capability boundaries."""
    try:
        report = inspect_capabilities(serial)
    except AndroidSupportError as exc:
        _emit_error(exc, json_output=json_output)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(report, sort_keys=True) if json_output else _render_capabilities(report))


@android_app.command("versions")
def versions_command(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show the offline Android/API planning matrix without contacting adb."""
    report = version_matrix()
    typer.echo(json.dumps(report, sort_keys=True) if json_output else _render_versions(report))


__all__ = [
    "ANDROID_VERSIONS",
    "AndroidSupportError",
    "MAX_VERIFIED_API",
    "MIN_SUPPORTED_API",
    "android_app",
    "evaluate_capabilities",
    "inspect_capabilities",
    "version_matrix",
]
