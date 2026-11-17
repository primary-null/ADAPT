"""Bounded Android Developer Options inspection and reversible profiles.

Only allowlisted Settings Provider keys, ``debug.*`` properties and fixed
command pairs are read or changed, each through an argv shape allowlisted in
``safety``.  Mutating profiles write a device-bound recovery snapshot before
their first device write, read every write back, roll a partial apply back and
restore without overwriting an external change.

Property changes reach a running app only after the app restarts; ADAPT does not
restart apps, poke SystemProperties or use raw Binder calls.
"""

from __future__ import annotations

from adapt_adb.progress import operation

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import typer

from adapt_adb import dev_catalog, dev_detect
from adapt_adb import dev_entries as ent
from adapt_adb import dev_profiles as prof
from adapt_adb.adb import _transport_kind
from adapt_adb.dev_entries import SETTINGS, SettingSpec
from adapt_adb.doctor import UnsafeAdbServerError, preflight_adb_server
from adapt_adb.failure_report import FailureContext, attach_failure, classify, emit_failure
from adapt_adb.safety import match_dev_options_shell


SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 1
ADB_TIMEOUT_SECONDS = 10
MAX_SNAPSHOT_BYTES = 1_000_000
CATALOG_READ_SECONDS = 60
# ADB's -s value is an opaque host argv element, not a remote shell word.
# mDNS names may include spaces, parentheses, and Unicode.
_SERIAL_RE = re.compile(r"^[^\x00-\x1f\x7f-\x9f]{1,1024}$")
_NUMBER_RE = ent.NUMBER_RE
_DENIED_RE = re.compile(
    r"permission denial|securityexception|not allowed|operation not permitted|access denied",
    re.IGNORECASE,
)
_ERROR_RE = re.compile(r"(?:^|\n)\s*(?:error|failure)\s*[:\[]", re.IGNORECASE)
_UNSAFE_IDENTITY = re.compile(r"[\x00-\x1f\x7f]")


def _default_lock_root() -> Path | None:
    """Per-device lock folder in the system temporary folder; None when no temporary folder is usable."""
    try:
        return Path(tempfile.gettempdir()) / "adapt-adb-dev-options-locks"
    except OSError:
        return None


_LOCK_ROOT = _default_lock_root()
_LOCK_FOLDER_MESSAGE = "Cannot use the system temporary folder for the per-device transaction lock."

_SETTING_BY_ID = ent.SETTING_BY_ID
_SETTING_BY_NAME = ent.SETTING_BY_NAME
_SNAPSHOT_STATUSES = {
    "prepared",
    "applying",
    "applied",
    "partial_failure",
    "restore_conflict",
    "restore_partial",
    "restoring",
    "restore_failed",
    "restored",
}
_ENTRY_STATUSES = {
    "pending",
    "applied",
    "rolled_back",
    "not_changed",
    "restoring",
    "restored",
    "restored_uncertain",
    "already_restored",
    "conflict",
    "uncertain",
    "error",
}
_TRANSPORTS = {"usb", "tcp", "local"}
_V1_TOP_KEYS = {
    "schema_version", "created_at", "updated_at", "profile", "status", "target_serial", "device",
    "settings", "errors", "conflicts",
}
_V2_TOP_KEYS = _V1_TOP_KEYS | {"package", "options", "api_level", "transport"}
_V1_ENTRY_KEYS = {"name", "table", "key", "user", "original", "applied_value", "changed", "attempted", "applied", "state"}
_V2_ENTRY_KEYS = _V1_ENTRY_KEYS | {"kind", "scope"}


@dataclass(frozen=True)
class DeviceIdentity:
    ro_serialno: str
    build_fingerprint: str
    android_user: int


class DevOptionsError(RuntimeError):
    """A classified, user-actionable Developer Options failure."""

    def __init__(
        self,
        kind: str,
        message: str,
        report: dict[str, Any] | None = None,
        *,
        failure_id: str | None = None,
    ):
        super().__init__(message)
        self.kind = kind
        self.report = report
        if failure_id is not None:
            # Names the specific failure where one kind covers several situations;
            # failure_report.classify prefers it over the kind map.
            self.failure_id = failure_id


class AdbCommandError(DevOptionsError):
    pass


def resolve_adb() -> Path | None:
    """Resolve adb through the project helper without importing it during collection."""
    from adapt_adb.adb import resolve_adb as project_resolve_adb

    return project_resolve_adb()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_serial(serial: str) -> str:
    if not isinstance(serial, str) or not serial.strip():
        raise DevOptionsError("invalid_serial", "A nonblank -s/--serial is required.")
    if serial != serial.strip() or not _SERIAL_RE.fullmatch(serial) or serial.startswith("-"):
        raise DevOptionsError("invalid_serial", "Serial contains unsupported characters.")
    return serial


def _validate_number(value: Any, *, field: str = "value") -> str:
    if not isinstance(value, str) or not _NUMBER_RE.fullmatch(value):
        raise DevOptionsError("malformed_value", f"{field} must be a plain numeric string.")
    if ent.plain_number(value) is None:
        raise DevOptionsError("malformed_value", f"{field} is not numeric.")
    return value


def _numbers_equal(left: str, right: str) -> bool:
    return ent.NUMBER.equal(left, right)


def _resolve_adb_path(adb_path: Path | str | None) -> Path:
    resolved = Path(adb_path) if adb_path is not None else resolve_adb()
    if resolved is None:
        raise DevOptionsError("missing_adb", "adb binary not found; run 'adapt setup'.")
    try:
        preflight_adb_server(Path(resolved), ["shell"])
    except UnsafeAdbServerError as exc:
        raise DevOptionsError("unsafe_adb_server", "ADB server ownership/version could not be verified.") from exc
    return Path(resolved)


def _run_process(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _run_adb(adb_path: Path, serial: str, device_args: list[str]) -> str:
    command = [str(adb_path), "-s", _validate_serial(serial), *device_args]
    try:
        result = _run_process(command, ADB_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise AdbCommandError("timeout", "adb command timed out.") from exc
    except FileNotFoundError as exc:
        raise AdbCommandError("missing_adb", "adb binary was not found.", failure_id="adb.exec_failed") from exc
    except OSError as exc:
        raise AdbCommandError("adb_error", f"adb could not start: {exc}", failure_id="adb.exec_failed") from exc

    stdout = result.stdout or ""
    stderr = result.stderr or ""
    combined = "\n".join(part for part in (stdout, stderr) if part).strip()
    if _DENIED_RE.search(combined):
        raise AdbCommandError("denied", combined[:2000] or "ADB permission denied.")
    if result.returncode != 0 or _ERROR_RE.search(combined):
        message = combined[:2000] or f"adb exited with status {result.returncode}."
        raise AdbCommandError("adb_error", message)
    return stdout.strip()


def _run_checked(adb_path: Path, serial: str, device_args: list[str]) -> str:
    """Run a device command only when it matches an allowlisted argv shape."""
    if match_dev_options_shell(device_args) is None:
        raise DevOptionsError("untrusted_operation", "Device command is not an allowlisted shape.")
    return _run_adb(adb_path, serial, device_args)


def _reader(adb: Path, serial: str):
    """A never-raising reader for the detection and catalog modules: (ok, text or failure kind)."""

    def read(args: list[str]) -> tuple[bool, str]:
        try:
            return True, _run_checked(adb, serial, args)[: ent.MAX_READ_CHARS]
        except DevOptionsError as exc:
            return False, exc.kind

    return read


# --------------------------------------------------------------------------
# Settings, properties and command observations
# --------------------------------------------------------------------------


def _check_setting_value(spec: SettingSpec, value: Any, *, field: str = "value") -> str:
    if spec.rule.kind == "number":
        return _validate_number(value, field=field)
    if not spec.rule.accepts(value):
        raise DevOptionsError("malformed_value", f"{field} is not an allowed value for this setting.")
    return value


def _setting_args(
    action: str,
    spec: SettingSpec,
    *,
    android_user: int,
    value: str | None = None,
) -> list[str]:
    if _SETTING_BY_ID.get((spec.table, spec.key)) != spec:
        raise DevOptionsError("untrusted_setting", "Setting is not allowlisted.")
    if action not in {"get", "put", "delete"}:
        raise DevOptionsError("untrusted_operation", "Settings operation is not allowlisted.")
    if not isinstance(android_user, int) or isinstance(android_user, bool) or android_user < 0:
        raise DevOptionsError("invalid_identity", "Android user is invalid.")
    args = ["shell", "settings"]
    if spec.user_scoped:
        args.extend(["--user", str(android_user)])
    args.extend([action, spec.table, spec.key])
    if action == "put":
        if value is None:
            raise DevOptionsError("malformed_value", "A settings value is required.")
        args.append(_check_setting_value(spec, value))
    elif value is not None:
        raise DevOptionsError("untrusted_operation", "Unexpected settings value.")
    return args


def _read_setting(
    adb_path: Path, serial: str, spec: SettingSpec, android_user: int
) -> dict[str, Any]:
    try:
        raw = _run_checked(
            adb_path,
            serial,
            _setting_args("get", spec, android_user=android_user),
        )
    except AdbCommandError as exc:
        return {"status": exc.kind, "value": None, "detail": str(exc)}
    if raw == "null":
        return {"status": "missing", "value": None, "detail": "key is absent"}
    readable = _NUMBER_RE.fullmatch(raw) if spec.rule.kind == "number" else spec.rule.accepts(raw)
    if not raw or "\n" in raw or not readable:
        return {
            "status": "malformed",
            "value": None,
            "detail": "settings returned a nonnumeric value" if spec.rule.kind == "number"
            else "settings returned a value outside the allowed set",
        }
    return {"status": "observed", "value": raw, "detail": None}


def _read_property(adb_path: Path, serial: str, spec: ent.PropertySpec) -> dict[str, Any]:
    try:
        raw = _run_checked(adb_path, serial, ["shell", "getprop", spec.name])
    except AdbCommandError as exc:
        return {"status": exc.kind, "value": None, "detail": str(exc)}
    if raw == "":
        return {"status": "missing", "value": None, "detail": "property is unset"}
    if "\n" in raw or not ent.PROPERTY_TOKEN.accepts(raw):
        return {"status": "malformed", "value": None, "detail": "property holds a value outside the plain token form"}
    return {"status": "observed", "value": raw, "detail": None}


def _read_command(adb_path: Path, serial: str, spec: ent.CommandSpec, ctx: ent.CommandContext) -> dict[str, Any]:
    outputs: list[str] = []
    for argv in spec.read(ctx):
        try:
            outputs.append(_run_checked(adb_path, serial, ["shell", *argv])[: ent.MAX_READ_CHARS])
        except AdbCommandError as exc:
            return {"status": exc.kind, "value": None, "detail": str(exc)}
    parsed = spec.parse(outputs)
    if parsed is None:
        return {"status": "malformed", "value": None, "detail": "the command output was not in the expected form"}
    return {"status": "observed", "value": parsed.value, "detail": None, "extras": parsed.extras}


def _require_observation(observation: dict[str, Any], label: str) -> tuple[bool, str | None]:
    status = observation["status"]
    if status == "missing":
        return False, None
    if status != "observed":
        detail = observation.get("detail") or status
        raise DevOptionsError(
            f"setting_{status}", f"Cannot read {label}: {detail}"
        )
    return True, observation["value"]


class _Handler:
    """One entry's device operations: read, write, restore and compare, through allowlisted shapes."""

    kind = "setting"
    label = ""
    exact_restore = True
    verify_restore = True
    conflict_check = True
    restore_note = ""

    def __init__(self, adb: Path, serial: str, user: int):
        self.adb, self.serial, self.user = adb, serial, user

    def read(self) -> dict[str, Any]:
        raise NotImplementedError

    def original(self, observation: dict[str, Any]) -> tuple[bool, str | None]:
        raise NotImplementedError

    def write(self, value: str) -> None:
        raise NotImplementedError

    def write_original(self, original: dict[str, Any]) -> None:
        raise NotImplementedError

    def equal(self, left: str, right: str) -> bool:
        return left == right

    def matches_original(self, observed: dict[str, Any], original: dict[str, Any]) -> bool:
        return bool(
            (original["present"] and observed["status"] == "observed" and self.equal(observed["value"], original["value"]))
            or (not original["present"] and observed["status"] == "missing")
        )

    def matches_applied(self, observed: dict[str, Any], applied: str) -> bool:
        return observed["status"] == "observed" and self.equal(observed["value"], applied)

    def restore(self, original: dict[str, Any]) -> None:
        self.write_original(original)
        if not self.verify_restore:
            return
        observed = self.read()
        if original["present"]:
            if observed["status"] != "observed" or not self.equal(observed["value"], original["value"]):
                raise DevOptionsError("readback_failed", f"Restoration readback failed for {self.label}.")
        elif observed["status"] != "missing":
            raise DevOptionsError("readback_failed", f"Deletion readback failed for {self.label}.")


class _SettingHandler(_Handler):
    kind = "setting"

    def __init__(self, adb: Path, serial: str, user: int, spec: SettingSpec):
        super().__init__(adb, serial, user)
        self.spec = spec
        self.label = spec.key
        self.read_label = f"{spec.table}/{spec.key}"

    def read(self) -> dict[str, Any]:
        return _read_setting(self.adb, self.serial, self.spec, self.user)

    def original(self, observation: dict[str, Any]) -> tuple[bool, str | None]:
        present, value = _require_observation(observation, self.read_label)
        if present:
            _check_setting_value(self.spec, value, field=f"{self.spec.key} value")
        return present, value

    def write(self, value: str) -> None:
        _run_checked(self.adb, self.serial, _setting_args("put", self.spec, android_user=self.user, value=value))

    def write_original(self, original: dict[str, Any]) -> None:
        if original["present"]:
            self.write(original["value"])
        else:
            _run_checked(self.adb, self.serial, _setting_args("delete", self.spec, android_user=self.user))

    def equal(self, left: str, right: str) -> bool:
        return self.spec.rule.equal(left, right)


class _PropertyHandler(_Handler):
    kind = "property"

    def __init__(self, adb: Path, serial: str, user: int, spec: ent.PropertySpec):
        super().__init__(adb, serial, user)
        self.spec = spec
        self.label = self.read_label = spec.name

    def read(self) -> dict[str, Any]:
        return _read_property(self.adb, self.serial, self.spec)

    def original(self, observation: dict[str, Any]) -> tuple[bool, str | None]:
        return _require_observation(observation, self.read_label)

    def _set(self, value: str) -> None:
        _run_checked(self.adb, self.serial, ["shell", "setprop", self.spec.name, value or "''"])

    def write(self, value: str) -> None:
        if not self.spec.applied.accepts(value):
            raise DevOptionsError("malformed_value", "Property value is not allowlisted.")
        self._set(value)

    def write_original(self, original: dict[str, Any]) -> None:
        # An unset property is put back as an empty value; getprop cannot tell the two apart.
        self._set(original["value"] if original["present"] else "")


class _CommandHandler(_Handler):
    kind = "command"

    def __init__(self, adb: Path, serial: str, user: int, spec: ent.CommandSpec, package: str | None):
        super().__init__(adb, serial, user)
        self.spec = spec
        self.ctx = ent.CommandContext(package=package, user=user)
        self.label = self.read_label = spec.name
        self.exact_restore = spec.exact_restore
        self.verify_restore = spec.verify_restore
        self.conflict_check = spec.conflict_check
        self.restore_note = spec.restore_note

    def read(self) -> dict[str, Any]:
        return _read_command(self.adb, self.serial, self.spec, self.ctx)

    def original(self, observation: dict[str, Any]) -> tuple[bool, str | None]:
        present, value = _require_observation(observation, self.read_label)
        if value is None or not self.spec.original_rule.accepts(value):
            raise DevOptionsError(
                "unsupported_state",
                f"The current state of {self.spec.name} ({value}) cannot be put back exactly, so nothing was changed.",
            )
        return True, value

    def _run(self, argvs: list[list[str]]) -> None:
        for argv in argvs:
            _run_checked(self.adb, self.serial, ["shell", *argv])

    def write(self, value: str) -> None:
        if not self.spec.applied_rule.accepts(value):
            raise DevOptionsError("malformed_value", "Command value is not allowlisted.")
        self._run(self.spec.apply(self.ctx, value))

    def write_original(self, original: dict[str, Any]) -> None:
        self._run(self.spec.restore(self.ctx, original["value"]))

    def matches_original(self, observed: dict[str, Any], original: dict[str, Any]) -> bool:
        if not self.conflict_check:
            return False
        return super().matches_original(observed, original)

    def matches_applied(self, observed: dict[str, Any], applied: str) -> bool:
        if not self.conflict_check:
            return observed["status"] == "observed"
        return super().matches_applied(observed, applied)


def _handler_for(
    adb: Path, serial: str, user: int, kind: str, name: str, package: str | None
) -> _Handler:
    if kind == "setting" and name in _SETTING_BY_NAME:
        return _SettingHandler(adb, serial, user, _SETTING_BY_NAME[name])
    if kind == "property" and name in ent.PROPERTY_BY_NAME:
        return _PropertyHandler(adb, serial, user, ent.PROPERTY_BY_NAME[name])
    if kind == "command" and name in ent.COMMAND_BY_NAME:
        return _CommandHandler(adb, serial, user, ent.COMMAND_BY_NAME[name], package)
    raise DevOptionsError("untrusted_setting", "Entry is not allowlisted.", failure_id="devopts.snapshot_untrusted")


def _read_identity(adb_path: Path, serial: str) -> DeviceIdentity:
    ro_serialno = _run_checked(adb_path, serial, ["shell", "getprop", "ro.serialno"])
    fingerprint = _run_checked(
        adb_path, serial, ["shell", "getprop", "ro.build.fingerprint"]
    )
    user_raw = _run_checked(adb_path, serial, ["shell", "am", "get-current-user"])
    if (
        not ro_serialno
        or ro_serialno.lower() in {"null", "unknown"}
        or len(ro_serialno) > 256
        or _UNSAFE_IDENTITY.search(ro_serialno)
        or not fingerprint
        or fingerprint.lower() in {"null", "unknown"}
        or len(fingerprint) > 2048
        or _UNSAFE_IDENTITY.search(fingerprint)
        or not re.fullmatch(r"\d+", user_raw)
    ):
        raise DevOptionsError(
            "identity_unavailable",
            "Device identity is unavailable; refusing to use the transport endpoint as identity.",
        )
    return DeviceIdentity(ro_serialno, fingerprint, int(user_raw))


def _transport(serial: str) -> str:
    """Transport kind from the serial: usb, tcp (wireless debugging or host:port) or local (emulator).

    ADAPT cannot prove a transport is USB; a TCP transport under a USB-looking alias is not detectable.
    """
    kind = _transport_kind(serial)
    return kind if kind in _TRANSPORTS else "usb"


# --------------------------------------------------------------------------
# Inspection, detection and the profile listing
# --------------------------------------------------------------------------


def _catalog_reads(adb: Path, serial: str, identity: DeviceIdentity, detection: dict[str, Any]) -> dict[str, Any]:
    """Read every catalog entry that has an allowlisted read command for this device, within a time budget."""
    api, oem = detection.get("api_level"), detection.get("oem")
    plan = dev_catalog.plan_reads(api, oem, identity.android_user)
    read = _reader(adb, serial)
    deadline = time.monotonic() + CATALOG_READ_SECONDS
    results: list[dict[str, Any]] = []
    for item in plan:
        if item["skipped"] is not None:
            results.append({"id": item["id"], "status": "skipped", "reason": item["skipped"], "value": None})
            continue
        if time.monotonic() > deadline:
            results.append({"id": item["id"], "status": "not_read", "reason": "time_budget_reached", "value": None})
            continue
        texts: list[str] = []
        failed: str | None = None
        for argv in item["argvs"]:
            ok, text = read(["shell", *argv])
            if not ok:
                failed = text
                break
            texts.append(text)
        if failed is not None:
            results.append({"id": item["id"], "status": "error", "reason": failed, "value": None})
        else:
            results.append({"id": item["id"], "status": "observed", "reason": None, "value": dev_catalog.bound_value(texts)})
    counts = {name: sum(1 for r in results if r["status"] == name) for name in ("observed", "error", "skipped", "not_read")}
    return {
        "api_level": api,
        "oem": oem,
        "counts": counts,
        "entries": results,
        "bounds": {"seconds": CATALOG_READ_SECONDS, "value_chars": dev_catalog.MAX_VALUE_CHARS, "per_command_seconds": ADB_TIMEOUT_SECONDS},
        "note": "Only catalog entries whose read command has an allowlisted shape are read; a value is an observation, not proof the option is honoured.",
    }


@operation("Inspecting Developer Options")
def inspect_device(
    serial: str,
    *,
    adb_path: Path | str | None = None,
    detect: bool = True,
    include_all: bool = False,
) -> dict[str, Any]:
    """Inspect only the bounded, allowlisted Developer Options observations.

    The report also carries a read-only ``detection`` block (maker layer, One UI,
    Pixel build, page size, form factor hints, device administrator) unless
    ``detect`` is false; ``include_all`` adds every allowlisted catalog read.
    """
    serial = _validate_serial(serial)
    adb = _resolve_adb_path(adb_path)
    identity = _read_identity(adb, serial)
    observations: list[dict[str, Any]] = []
    for spec in SETTINGS:
        item = _read_setting(adb, serial, spec, identity.android_user)
        observations.append(
            {
                "name": spec.name,
                "group": spec.group,
                "table": spec.table,
                "key": spec.key,
                "status": item["status"],
                "value": item["value"],
                "detail": item["detail"],
                "support": "not_proven",
            }
        )
    report: dict[str, Any] = {
        "ok": True,
        "command": "dev-options inspect",
        "target_serial": serial,
        "device": asdict(identity),
        "settings": observations,
        "note": "A settings value is an observation, not proof that the device exposes or honors the option.",
    }
    if detect or include_all:
        report["detection"] = dev_detect.detect(
            _reader(adb, serial), transport=_transport(serial), emulator=serial.startswith("emulator-")
        )
    if include_all:
        report["catalog_reads"] = _catalog_reads(adb, serial, identity, report["detection"])
    return report


def _validate_package_text(package: Any) -> str:
    if not ent.valid_package(package):
        raise DevOptionsError("invalid_package", "The package must be a valid Android application id such as com.example.app.")
    return package


@operation("Listing Developer Options profiles")
def profiles_report(
    serial: str, *, adb_path: Path | str | None = None, package: str | None = None
) -> dict[str, Any]:
    """Every profile with its eligibility on this device, from read-only facts."""
    serial = _validate_serial(serial)
    if package is not None:
        _validate_package_text(package)
    adb = _resolve_adb_path(adb_path)
    _read_identity(adb, serial)  # an unreachable or unidentifiable device is an error, not an empty listing
    unread: list[dict[str, str]] = []
    transport = _transport(serial)
    detection = dev_detect.detect_oem(
        _reader(adb, serial), unread, transport=transport, emulator=serial.startswith("emulator-")
    )
    api, oem = detection["api_level"], detection["oem"]
    entries = []
    for profile in prof.PROFILES:
        verdict = prof.evaluate(profile, api=api, oem=oem, transport=transport, package=package if profile.package != "none" else None)
        entries.append({**prof.registry_entry(profile), **verdict})
    return {
        "ok": True,
        "command": "dev-options profiles",
        "target_serial": serial,
        "detection": {key: detection[key] for key in ("api_level", "oem", "transport", "emulator_hint", "samsung", "guidance")},
        "unread": unread,
        "profiles": entries,
        "note": "Eligibility comes from read-only facts; it does not prove the device honours a profile. No profile has been run on a device yet.",
    }


@operation("Checking the page size")
def pixel_16kb_check(serial: str, *, adb_path: Path | str | None = None) -> dict[str, Any]:
    """Read the page size and point to the safe ways to test 16 KB pages. Never changes the device."""
    serial = _validate_serial(serial)
    adb = _resolve_adb_path(adb_path)
    _read_identity(adb, serial)
    unread: list[dict[str, str]] = []
    read = _reader(adb, serial)
    detection = dev_detect.detect_oem(read, unread, transport=_transport(serial), emulator=serial.startswith("emulator-"))
    ok, text = read(["shell", "getconf", "PAGE_SIZE"])
    size = text.strip() if ok else ""
    page_size = int(size) if re.fullmatch(r"4096|16384|65536", size) else None
    if not ok:
        unread.append({"item": "page_size", "status": text})
    pixel = None
    if detection["oem"] == "pixel":
        ok_id, build = read(["shell", "getprop", "ro.build.id"])
        ok_soc, soc = read(["shell", "getprop", "ro.soc.model"])
        pixel = {"build_id": (build.strip() if ok_id else None) or None, "soc_model": (soc.strip() if ok_soc else None) or None}
    return {
        "ok": True,
        "command": "dev-options pixel-16kb-check",
        "target_serial": serial,
        "page_size": page_size,
        "uses_16kb_pages": None if page_size is None else page_size >= 16384,
        "oem": detection["oem"],
        "api_level": detection["api_level"],
        "pixel": pixel,
        "guidance": dev_catalog.page_size_guidance(page_size, detection["oem"]),
        "never_automated": [dev_catalog.BOOT_16KB_WARNING],
        "unread": unread,
        "claim_boundary": "The page size is what getconf reports for this boot; it says nothing about whether an app works on 16 KB pages.",
    }


# --------------------------------------------------------------------------
# Snapshot files and the per-device lock
# --------------------------------------------------------------------------


def _snapshot_json(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _ensure_snapshot_parent(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DevOptionsError("snapshot_io", f"Cannot create snapshot directory: {exc}") from exc


def _create_snapshot(path: Path, document: dict[str, Any]) -> None:
    _ensure_snapshot_parent(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY  # type: ignore[attr-defined]
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW  # type: ignore[attr-defined]
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise DevOptionsError(
            "snapshot_exists", "Recovery snapshot already exists; refusing to overwrite it."
        ) from exc
    except OSError as exc:
        raise DevOptionsError("snapshot_io", f"Cannot create recovery snapshot: {exc}") from exc
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(_snapshot_json(document))
            stream.flush()
            os.fsync(stream.fileno())
    except Exception as exc:
        try:
            path.unlink()
        except OSError:
            pass
        if isinstance(exc, DevOptionsError):
            raise
        raise DevOptionsError("snapshot_io", f"Cannot persist recovery snapshot: {exc}") from exc


def _assert_regular_snapshot(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise DevOptionsError(
            "snapshot_io", f"Cannot inspect snapshot: {exc}", failure_id="devopts.snapshot_unreadable"
        ) from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise DevOptionsError("unsafe_snapshot", "Snapshot must be a regular, non-symlink file.")


def _save_snapshot(path: Path, document: dict[str, Any]) -> None:
    _assert_regular_snapshot(path)
    document["updated_at"] = _utc_now()
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY  # type: ignore[attr-defined]
    try:
        fd = os.open(temp_path, flags, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(_snapshot_json(document))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    except OSError as exc:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise DevOptionsError("snapshot_io", f"Cannot update recovery snapshot: {exc}") from exc


@contextmanager
def _device_lock(identity: DeviceIdentity) -> Iterator[None]:
    try:
        if _LOCK_ROOT is None:
            raise FileNotFoundError("no usable temporary folder")
        _LOCK_ROOT.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DevOptionsError(
            "concurrent_operation", _LOCK_FOLDER_MESSAGE, failure_id="devopts.lock_unavailable",
        ) from exc
    # Global animation scales affect the whole device, across Android users.
    token = hashlib.sha256(identity.ro_serialno.encode("utf-8")).hexdigest()
    lock_path = _LOCK_ROOT / f"{token}.lock"
    try:
        handle = lock_path.open("a+b")
    except OSError as exc:
        raise DevOptionsError(
            "concurrent_operation", "Cannot open the per-device transaction lock.",
            failure_id="devopts.lock_unavailable",
        ) from exc
    acquired = False
    try:
        try:
            handle.seek(0)
            if handle.read(1) == b"":
                handle.seek(0)
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise DevOptionsError(
                "concurrent_operation", "Another Developer Options transaction is active for this device."
            ) from exc
        acquired = True
        yield
    finally:
        if acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


# --------------------------------------------------------------------------
# Apply
# --------------------------------------------------------------------------


def _handler_for_entry(
    adb: Path, serial: str, entry: dict[str, Any], document: dict[str, Any]
) -> _Handler:
    user = document["device"]["android_user"]
    return _handler_for(
        adb, serial, user, entry.get("kind", "setting"), entry["name"], document.get("package")
    )


def _rollback_apply(
    document: dict[str, Any], path: Path, adb: Path, serial: str
) -> None:
    for entry in reversed(document["settings"]):
        if not entry["attempted"]:
            continue
        try:
            handler = _handler_for_entry(adb, serial, entry, document)
            observed = handler.read()
            original = entry["original"]
            if handler.matches_original(observed, original):
                pass
            elif handler.matches_applied(observed, entry["applied_value"]):
                handler.restore(original)
            else:
                raise DevOptionsError(
                    "rollback_conflict", f"Rollback refused for externally changed {handler.label}."
                )
            entry.update(applied=False, attempted=False, state="rolled_back")
        except DevOptionsError as exc:
            entry["state"] = "error"
            document["errors"].append(str(exc))
        try:
            _save_snapshot(path, document)
        except DevOptionsError as exc:
            document["errors"].append(str(exc))


_LEGACY_API_MESSAGE = (
    "Desktop testing settings require observed Android 10–17 / API 29–37; future OEM behavior is unverified."
)


def _read_api(adb: Path, serial: str) -> int | None:
    sdk = _run_checked(adb, serial, ["shell", "getprop", "ro.build.version.sdk"]).strip()
    return int(sdk) if re.fullmatch(r"[0-9]{1,3}", sdk) else None


def _gate_api(profile: prof.ProfileSpec, api: int | None) -> None:
    low = api is None or api < profile.min_api
    high = api is not None and profile.max_api is not None and api > profile.max_api
    if not (low or high):
        return
    if profile.name == "desktop-testing":
        raise DevOptionsError("unsupported_api", _LEGACY_API_MESSAGE)
    reported = "none" if api is None else str(api)
    top = f"-{profile.max_api}" if profile.max_api is not None else " or later"
    raise DevOptionsError(
        "profile_api_range",
        f"Profile {profile.name} needs Android API {profile.min_api}{top}; the device reports {reported}, so nothing was changed.",
    )


def _validate_options(profile: prof.ProfileSpec, options: Any) -> tuple[str, ...]:
    if options is None:
        return ()
    if isinstance(options, str) or not isinstance(options, (list, tuple, set, frozenset)):
        raise DevOptionsError("invalid_option", "Options must be a list of option names.")
    chosen: list[str] = []
    for item in options:
        if not isinstance(item, str) or item not in profile.options:
            allowed = ", ".join(profile.options) or "none"
            raise DevOptionsError("invalid_option", f"Unknown option for {profile.name}; its options are: {allowed}.")
        if item not in chosen:
            chosen.append(item)
    return tuple(chosen)


def _validate_package_for(profile: prof.ProfileSpec, package: Any) -> str | None:
    if package is None:
        if profile.package == "required":
            raise DevOptionsError("package_required", f"Profile {profile.name} needs --package (an Android application id).")
        return None
    if profile.package == "none":
        raise DevOptionsError("invalid_package", f"Profile {profile.name} does not use a package.")
    return _validate_package_text(package)


def _planned_value(
    step: prof.StepSpec, original: str | None, extras: dict[str, Any], api: int | None, package: str | None
) -> str:
    if step.value is not None:
        return step.value
    if step.derive == "stay_awake_usb":
        # Enable USB power only, preserving AC/wireless/dock bits already set.
        # Unknown future bits or non-integer OEM values must not be overwritten.
        value = ent.derive_stay_awake_usb(original)
        if value is None:
            raise DevOptionsError("unsupported_value", "Stay-awake requires a known charging bitmask (0-15); no setting was changed.")
        return value
    if step.derive == "font_scale":
        return ent.derive_font_scale(api)
    if step.derive == "density":
        value = ent.derive_density(extras)
        if value is None:
            raise DevOptionsError("unsupported_value", "The physical display density could not be used; no setting was changed.")
        return value
    value = ent.derive_debug_app(package) if step.derive == "debug_app" else None
    if value is None:
        raise DevOptionsError("unsupported_value", "The applied value could not be derived; no setting was changed.")
    return value


def _claim_boundary(profile: prof.ProfileSpec, applied_kinds: set[str]) -> str:
    parts = ["A stored value or readback is not proof that the app or this OEM build honours the option."]
    if "property" in applied_kinds or profile.needs_app_restart:
        parts.append("Property changes reach an app only after it restarts; ADAPT does not restart apps or poke SystemProperties.")
    if profile.needs_effect_check:
        parts.append(f"Effect check needed: {profile.effect_check}")
    inexact = profile.inexact_entries()
    if inexact:
        notes = "; ".join(f"{name}: {ent.COMMAND_BY_NAME[name].restore_note}" for name in inexact)
        parts.append(f"Restore cannot reproduce the exact prior state ({notes}).")
    if profile.refuse_wireless:
        parts.append("The transport is classified from the serial form; a TCP transport under a USB-looking alias is not detected.")
    parts.append("Offline-tested only: this profile has not been run on a device.")
    return " ".join(parts)


def _samsung_auto_blocker(adb: Path, serial: str) -> bool:
    """Best effort after a failed write: does the phone report Samsung One UI 6 or later?"""
    block = dev_detect.detect_oem(_reader(adb, serial), [], transport=_transport(serial))
    samsung = block.get("samsung")
    return bool(isinstance(samsung, dict) and samsung.get("auto_blocker_applies"))


def _build_entry(handler: _Handler, step: prof.StepSpec, identity: DeviceIdentity, package: str | None, api: int | None) -> dict[str, Any]:
    observation = handler.read()
    present, original = handler.original(observation)
    applied = _planned_value(step, original, observation.get("extras", {}), api, package)
    rule = (
        handler.spec.rule if handler.kind == "setting"
        else handler.spec.applied if handler.kind == "property"
        else handler.spec.applied_rule
    )
    if not rule.accepts(applied):
        raise DevOptionsError("malformed_value", f"The value planned for {handler.label} is outside the allowed set.")
    changed = not present or not handler.equal(original or "", applied)
    if handler.kind == "setting":
        table, key = handler.spec.table, handler.spec.key
        user = identity.android_user if handler.spec.user_scoped else None
    elif handler.kind == "property":
        table, key, user = "property", handler.spec.name, None
    else:
        table, key = "command", handler.spec.name
        user = identity.android_user if handler.spec.user_scoped else None
    scope = package if handler.kind == "command" and handler.spec.scope == "package" else None
    return {
        "name": step.entry,
        "kind": handler.kind,
        "table": table,
        "key": key,
        "user": user,
        "scope": scope,
        "original": {"present": present, "value": original},
        "applied_value": applied,
        "changed": changed,
        "attempted": False,
        "applied": False,
        "state": "pending" if changed else "not_changed",
    }


@operation("Applying test profile")
def apply_profile(
    profile: str,
    serial: str,
    snapshot: Path | str,
    *,
    adb_path: Path | str | None = None,
    package: str | None = None,
    options: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Apply one bounded profile after exclusively creating its recovery snapshot.

    ``package`` scopes the per-app steps of profiles that take one; ``options``
    selects the optional steps a profile lists.  Both are validated and recorded
    in the snapshot.
    """
    spec = prof.PROFILE_BY_NAME.get(profile) if isinstance(profile, str) else None
    if spec is None:
        raise DevOptionsError("invalid_profile", "Profile must be one of: " + ", ".join(prof.PROFILE_NAMES) + ".")
    serial = _validate_serial(serial)
    path = Path(snapshot)
    chosen = _validate_options(spec, options)
    pkg = _validate_package_for(spec, package)
    transport = _transport(serial)
    if spec.refuse_wireless and transport == "tcp":
        raise DevOptionsError(
            "wireless_transport",
            f"Profile {spec.name} can cut the network and is refused over a TCP or wireless-debugging transport; use USB.",
        )
    adb = _resolve_adb_path(adb_path)
    api: int | None = None
    if spec.api_gate:
        api = _read_api(adb, serial)
        _gate_api(spec, api)
    steps = prof.select_steps(spec, api, chosen, pkg)
    if not steps:
        raise DevOptionsError(
            "profile_api_range",
            f"No step of {spec.name} applies at API {api} with the chosen options, so nothing was changed.",
        )
    identity = _read_identity(adb, serial)
    with _device_lock(identity):
        entries: list[dict[str, Any]] = []
        for step in steps:
            handler = _handler_for(adb, serial, identity.android_user, step.kind, step.entry, pkg)
            entries.append(_build_entry(handler, step, identity, pkg, api))
        now = _utc_now()
        document: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "created_at": now,
            "updated_at": now,
            "profile": spec.name,
            "status": "prepared",
            "target_serial": serial,
            "device": asdict(identity),
            "package": pkg,
            "options": list(chosen),
            "api_level": api,
            "transport": transport,
            "settings": entries,
            "errors": [],
            "conflicts": [],
        }
        _create_snapshot(path, document)

        for entry in entries:
            if not entry["changed"]:
                continue
            handler = _handler_for_entry(adb, serial, entry, document)
            try:
                document["status"] = "applying"
                entry.update(attempted=True, state="pending")
                _save_snapshot(path, document)
                handler.write(entry["applied_value"])
                observed = handler.read()
                if not handler.matches_applied(observed, entry["applied_value"]):
                    raise DevOptionsError("readback_failed", f"Apply readback failed for {handler.label}.")
                entry.update(applied=True, state="applied")
                _save_snapshot(path, document)
            except DevOptionsError as exc:
                document["errors"].append(str(exc))
                document["status"] = "partial_failure"
                _rollback_apply(document, path, adb, serial)
                try:
                    _save_snapshot(path, document)
                except DevOptionsError as save_exc:
                    document["errors"].append(str(save_exc))
                # "rolled_back" means a readback matched the original value; any "error"
                # entry leaves the device state unconfirmed.
                rolled_back = not any(item["state"] == "error" for item in entries)
                if _samsung_auto_blocker(adb, serial):
                    failure_id: str | None = "devopts.samsung_auto_blocker"
                else:
                    failure_id = "devopts.apply_rolled_back" if rolled_back else None
                raise DevOptionsError(
                    "apply_failed",
                    "Profile apply failed; rollback was attempted and the snapshot was retained.",
                    document,
                    failure_id=failure_id,
                ) from exc

        document["status"] = "applied"
        _save_snapshot(path, document)
        applied_kinds = {entry["kind"] for entry in entries if entry["applied"]}
        note = "Snapshot retained; restoration is best-effort and must be run explicitly."
        if spec.name == "desktop-testing":
            note += " Desktop settings may require a manual reboot and may be ignored by this OEM; no reboot was performed."
        if spec.name == "stay-awake":
            note += " Stay-awake requests USB-powered screen-on and preserves existing charging modes. Readback does not prove effective wakefulness; device policy can override it. Restore after testing."
        if "property" in applied_kinds:
            note += " Property changes take effect when the app restarts."
        return {
            "ok": True,
            "command": "dev-options apply",
            "profile": spec.name,
            "snapshot": str(path),
            "schema_version": SCHEMA_VERSION,
            "package": pkg,
            "options": list(chosen),
            "transport": transport,
            "changed": [entry["name"] for entry in entries if entry["applied"]],
            "unchanged": [entry["name"] for entry in entries if not entry["changed"]],
            "skipped_steps": [
                {"entry": step.entry, "reason": why}
                for step in spec.steps
                if (why := prof.step_applies(step, api, chosen, pkg)) is not None and why != "option_not_selected"
            ],
            "needs_app_restart": spec.needs_app_restart or "property" in applied_kinds,
            "needs_reboot": spec.needs_reboot,
            "needs_effect_check": spec.needs_effect_check,
            "effect_check": spec.effect_check or None,
            "restore_exactness": spec.restore_exactness,
            "approximate_restore": list(spec.inexact_entries()),
            "claim_boundary": _claim_boundary(spec, applied_kinds),
            "note": note,
        }


# --------------------------------------------------------------------------
# Snapshot validation (schema 1 and 2)
# --------------------------------------------------------------------------


def _load_snapshot(path: Path) -> dict[str, Any]:
    _assert_regular_snapshot(path)
    try:
        if path.stat().st_size > MAX_SNAPSHOT_BYTES:
            raise DevOptionsError("malformed_snapshot", "Snapshot is too large.")
        raw = path.read_text(encoding="utf-8")
        document = json.loads(raw)
    except DevOptionsError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DevOptionsError(
            "malformed_snapshot", f"Cannot read snapshot: {exc}",
            failure_id="devopts.snapshot_unreadable" if isinstance(exc, OSError) else None,
        ) from exc
    return _validate_snapshot(document)


def _validate_common(document: dict[str, Any], keys: set[str]) -> None:
    _validate_serial(document["target_serial"])
    for name in ("created_at", "updated_at"):
        if not isinstance(document[name], str) or len(document[name]) > 80:
            raise DevOptionsError("malformed_snapshot", f"Snapshot {name} is invalid.")
    device = document["device"]
    if not isinstance(device, dict) or set(device) != {
        "ro_serialno",
        "build_fingerprint",
        "android_user",
    }:
        raise DevOptionsError("malformed_snapshot", "Snapshot identity fields are invalid.")
    if (
        not isinstance(device["ro_serialno"], str)
        or not device["ro_serialno"]
        or len(device["ro_serialno"]) > 256
        or _UNSAFE_IDENTITY.search(device["ro_serialno"])
        or not isinstance(device["build_fingerprint"], str)
        or not device["build_fingerprint"]
        or len(device["build_fingerprint"]) > 2048
        or _UNSAFE_IDENTITY.search(device["build_fingerprint"])
        or not isinstance(device["android_user"], int)
        or isinstance(device["android_user"], bool)
        or device["android_user"] < 0
    ):
        raise DevOptionsError("malformed_snapshot", "Snapshot identity is invalid.")
    if not isinstance(document["errors"], list) or not all(
        isinstance(item, str) and len(item) <= 2000 for item in document["errors"]
    ):
        raise DevOptionsError("malformed_snapshot", "Snapshot errors are invalid.")
    if not isinstance(document["conflicts"], list) or not all(
        isinstance(item, str) and len(item) <= 2000 for item in document["conflicts"]
    ):
        raise DevOptionsError("malformed_snapshot", "Snapshot conflicts are invalid.")


def _validate_scope(document: dict[str, Any], profile: prof.ProfileSpec) -> tuple[int | None, tuple[str, ...], str | None]:
    """The schema 2 recording of api level, options and package; schema 1 has none of them."""
    if document["schema_version"] == LEGACY_SCHEMA_VERSION:
        return None, (), None
    api, options, package = document["api_level"], document["options"], document["package"]
    if api is not None and (type(api) is not int or not 1 <= api <= 999):
        raise DevOptionsError("malformed_snapshot", "Snapshot API level is invalid.")
    if not isinstance(options, list) or len(set(options)) != len(options) or any(
        not isinstance(item, str) or item not in profile.options for item in options
    ):
        raise DevOptionsError("malformed_snapshot", "Snapshot options are invalid.")
    if package is not None and not ent.valid_package(package):
        raise DevOptionsError("malformed_snapshot", "Snapshot package is invalid.")
    if (package is not None and profile.package == "none") or (package is None and profile.package == "required"):
        raise DevOptionsError("malformed_snapshot", "Snapshot package does not belong to this profile.")
    if document["transport"] not in _TRANSPORTS:
        raise DevOptionsError("malformed_snapshot", "Snapshot transport is invalid.")
    return api, tuple(options), package


def _same(left: Any, right: Any) -> bool:
    return left == right


def _untrusted(message: str) -> DevOptionsError:
    return DevOptionsError("untrusted_setting", message, failure_id="devopts.snapshot_untrusted")


def _validate_entry(
    entry: Any, step: prof.StepSpec | None, document: dict[str, Any], keys: set[str], api: int | None, package: str | None
) -> None:
    if not isinstance(entry, dict) or set(entry) != keys:
        raise DevOptionsError("malformed_snapshot", "Snapshot setting fields are invalid.")
    if step is None:
        raise _untrusted("Snapshot contains an untrusted setting.")
    kind = entry.get("kind", "setting")
    if kind != step.kind:
        raise _untrusted("Snapshot entry kind is not allowlisted.")
    android_user = document["device"]["android_user"]
    original = entry["original"]
    if not isinstance(original, dict) or set(original) != {"present", "value"}:
        raise DevOptionsError("malformed_snapshot", "Snapshot original value is invalid.")
    if type(original["present"]) is not bool:
        raise DevOptionsError("malformed_snapshot", "Snapshot presence flag is invalid.")
    present, value = original["present"], original["value"]
    if kind == "setting":
        spec = _SETTING_BY_NAME[step.entry]
        if entry["table"] != spec.table or entry["key"] != spec.key:
            raise _untrusted("Snapshot table/key is not allowlisted.")
        expected_user = android_user if spec.user_scoped else None
        if present:
            _check_setting_value(spec, value, field="snapshot original value")
        elif value is not None:
            raise DevOptionsError("malformed_snapshot", "Absent key must have a null value.")
        equal, rule = spec.rule.equal, spec.rule
        scope = None
        derived_ok = True
        if step.derive == "stay_awake_usb":
            expected = ent.derive_stay_awake_usb(value)
            if expected is None:
                raise DevOptionsError("unsupported_value", "Stay-awake requires a known charging bitmask (0-15); no setting was changed.")
            derived_ok = entry["applied_value"] == expected
        elif step.derive == "font_scale":
            derived_ok = entry["applied_value"] == ent.derive_font_scale(api)
    elif kind == "property":
        spec_p = ent.PROPERTY_BY_NAME[step.entry]
        if entry["table"] != "property" or entry["key"] != spec_p.name:
            raise _untrusted("Snapshot property is not allowlisted.")
        expected_user, scope = None, None
        if present:
            if not ent.PROPERTY_TOKEN.accepts(value):
                raise DevOptionsError("malformed_snapshot", "Snapshot property value is invalid.")
        elif value is not None:
            raise DevOptionsError("malformed_snapshot", "Absent property must have a null value.")
        rule = spec_p.applied
        equal = _same
        derived_ok = True
    else:
        spec_c = ent.COMMAND_BY_NAME[step.entry]
        if entry["table"] != "command" or entry["key"] != spec_c.name:
            raise _untrusted("Snapshot command is not allowlisted.")
        expected_user = android_user if spec_c.user_scoped else None
        scope = package if spec_c.scope == "package" else None
        if spec_c.scope == "package" and package is None:
            raise DevOptionsError("malformed_snapshot", "Snapshot per-app entry has no package.")
        if present is not True or not spec_c.original_rule.accepts(value):
            raise DevOptionsError("malformed_snapshot", "Snapshot command state is invalid.")
        rule = spec_c.applied_rule
        equal = _same
        derived_ok = True
        if step.derive == "debug_app":
            derived_ok = entry["applied_value"] == ent.derive_debug_app(package)
    if entry["user"] != expected_user:
        raise DevOptionsError("malformed_snapshot", "Snapshot setting user is invalid.")
    if entry.get("scope") != scope:
        raise DevOptionsError("malformed_snapshot", "Snapshot setting scope is invalid.")
    applied = entry["applied_value"]
    fixed_ok = step.value is None or applied == step.value
    if not isinstance(applied, str) or not rule.accepts(applied) or not fixed_ok or not derived_ok:
        raise DevOptionsError("untrusted_value", "Snapshot applied value is not allowlisted.")
    for flag in ("changed", "attempted", "applied"):
        if type(entry[flag]) is not bool:
            raise DevOptionsError("malformed_snapshot", f"Snapshot {flag} flag is invalid.")
    expected_changed = not present or not equal(value, applied)
    if entry["changed"] != expected_changed or entry["state"] not in _ENTRY_STATUSES:
        raise DevOptionsError("malformed_snapshot", "Snapshot transaction state is invalid.")
    if entry["applied"] and not entry["attempted"]:
        raise DevOptionsError("malformed_snapshot", "Applied setting was not marked attempted.")


def _validate_snapshot(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict) or set(document) not in (_V1_TOP_KEYS, _V2_TOP_KEYS):
        raise DevOptionsError("malformed_snapshot", "Snapshot fields are invalid.")
    version = document["schema_version"]
    keys = _V1_TOP_KEYS if version == LEGACY_SCHEMA_VERSION else _V2_TOP_KEYS if version == SCHEMA_VERSION else None
    if keys is None or set(document) != keys or type(version) is not int:
        raise DevOptionsError("malformed_snapshot", "Unsupported snapshot schema.")
    profile = prof.PROFILE_BY_NAME.get(document["profile"]) if isinstance(document["profile"], str) else None
    if profile is None or document["status"] not in _SNAPSHOT_STATUSES:
        raise DevOptionsError("malformed_snapshot", "Snapshot profile or status is invalid.")
    if version == LEGACY_SCHEMA_VERSION and profile.name not in prof.LEGACY_PROFILE_NAMES:
        raise DevOptionsError("malformed_snapshot", "Snapshot profile or status is invalid.")
    _validate_common(document, keys)
    api, options, package = _validate_scope(document, profile)

    entries = document["settings"]
    steps = prof.select_steps(profile, api, options, package)
    if version == LEGACY_SCHEMA_VERSION:
        steps = list(profile.steps)
    if not isinstance(entries, list) or len(entries) != len(steps):
        raise DevOptionsError("malformed_snapshot", "Snapshot settings are invalid.")
    by_name = {step.entry: step for step in steps}
    entry_keys = _V1_ENTRY_KEYS if version == LEGACY_SCHEMA_VERSION else _V2_ENTRY_KEYS
    seen: set[str] = set()
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or name in seen or name not in by_name:
            if isinstance(entry, dict) and set(entry) != entry_keys:
                raise DevOptionsError("malformed_snapshot", "Snapshot setting fields are invalid.")
            raise _untrusted("Snapshot contains an untrusted setting.")
        seen.add(name)
        _validate_entry(entry, by_name[name], document, entry_keys, api, package)
    return document


# --------------------------------------------------------------------------
# Restore
# --------------------------------------------------------------------------


@operation("Restoring test profile")
def restore_snapshot(
    serial: str,
    snapshot: Path | str,
    *,
    adb_path: Path | str | None = None,
    accept_uncertain: bool = False,
) -> dict[str, Any]:
    """Restore a snapshot only when identity and expected applied values still match.

    ``accept_uncertain`` lets an entry whose write was attempted but never
    acknowledged (for example after a host crash) be restored when the device
    currently holds exactly the value ADAPT intended to write.  Schema 1 and
    schema 2 snapshots are both accepted.
    """
    serial = _validate_serial(serial)
    path = Path(snapshot)
    document = _load_snapshot(path)
    adb = _resolve_adb_path(adb_path)
    current_identity = _read_identity(adb, serial)
    saved_identity = DeviceIdentity(**document["device"])
    if current_identity != saved_identity:
        raise DevOptionsError(
            "identity_mismatch", "Snapshot device/build/user identity does not match the target."
        )

    with _device_lock(current_identity):
        # Another restore can complete while this invocation waits for identity
        # probes. Revalidate the durable transaction after acquiring its lock.
        document = _load_snapshot(path)
        if DeviceIdentity(**document["device"]) != current_identity:
            raise DevOptionsError("identity_mismatch", "Snapshot identity changed before restore.")
        document["conflicts"] = []
        ready: list[tuple[dict[str, Any], _Handler, bool]] = []
        for entry in document["settings"]:
            if not entry["changed"] or (not entry["attempted"] and not entry["applied"]):
                continue
            handler = _handler_for_entry(adb, serial, entry, document)
            observed = handler.read()
            uncertain = entry["attempted"] and not entry["applied"]
            holds_applied = handler.matches_applied(observed, entry["applied_value"])
            if handler.matches_original(observed, entry["original"]):
                entry.update(applied=False, attempted=False, state="already_restored")
            elif holds_applied and (entry["applied"] or accept_uncertain):
                ready.append((entry, handler, uncertain))
            else:
                if uncertain:
                    if holds_applied:
                        reason = (
                            f"{handler.label}: outcome is uncertain; refusing to overwrite current state. "
                            "The current value equals the value ADAPT intended to write; rerun "
                            "restore with --accept-uncertain to restore the original value."
                        )
                    elif accept_uncertain:
                        reason = (
                            f"{handler.label}: outcome is uncertain and the current value matches neither "
                            "the original nor the value ADAPT intended to write; refusing to "
                            "overwrite current state even with --accept-uncertain"
                        )
                    else:
                        reason = (
                            f"{handler.label}: outcome is uncertain; refusing to overwrite current state. "
                            "--accept-uncertain applies only when the current value equals the "
                            "value ADAPT intended to write, which it does not."
                        )
                    entry["state"] = "uncertain"
                else:
                    reason = f"{handler.label}: current value differs from the applied value"
                    entry["state"] = "conflict"
                document["conflicts"].append(reason)

        if document["conflicts"]:
            document["status"] = "restore_conflict"
            _save_snapshot(path, document)
            return {
                "ok": False,
                "command": "dev-options restore",
                "snapshot": str(path),
                "status": document["status"],
                "conflicts": list(document["conflicts"]),
                "note": "No settings were changed because restore preflight found conflicts.",
            }

        restored: list[tuple[dict[str, Any], _Handler]] = []
        left: list[dict[str, Any]] = []
        try:
            for entry, handler, uncertain in ready:
                # Ownership is re-checked immediately before each write, so a
                # change another actor made after preflight is not overwritten.
                # Android offers no conditional (compare-and-set) settings
                # write, so a small window between this read and the write
                # below remains.
                observed = handler.read()
                if handler.matches_original(observed, entry["original"]):
                    entry.update(applied=False, attempted=False, state="already_restored")
                    _save_snapshot(path, document)
                    continue
                if not handler.matches_applied(observed, entry["applied_value"]):
                    if observed["status"] == "observed":
                        reason = f"{handler.label}: changed during restore; left untouched"
                    else:
                        reason = (
                            f"{handler.label}: could not be re-read during restore "
                            f"({observed['status']}); left untouched"
                        )
                    entry["state"] = "conflict"
                    document["conflicts"].append(reason)
                    left.append(entry)
                    _save_snapshot(path, document)
                    continue
                document["status"] = "restoring"
                entry["state"] = "restoring"
                _save_snapshot(path, document)
                handler.restore(entry["original"])
                entry.update(
                    applied=False,
                    attempted=False,
                    state="restored_uncertain" if uncertain else "restored",
                )
                restored.append((entry, handler))
                _save_snapshot(path, document)
        except DevOptionsError as exc:
            document["errors"].append(str(exc))
            # Return successfully restored entries to the applied profile when safe.
            # Entries skipped because they changed during restore were never
            # written, so the rollback leaves them alone and they keep their
            # conflict record in the snapshot.
            for prior, prior_handler in reversed(restored):
                observed = prior_handler.read()
                if not prior_handler.conflict_check:
                    document["errors"].append(
                        f"{prior_handler.label}: restore rollback skipped; this control can only be reset, not reapplied."
                    )
                    continue
                if not prior_handler.matches_original(observed, prior["original"]):
                    document["errors"].append(
                        f"Restore rollback refused for externally changed {prior_handler.label}."
                    )
                    continue
                try:
                    prior_handler.write(prior["applied_value"])
                    check = prior_handler.read()
                    if not prior_handler.matches_applied(check, prior["applied_value"]):
                        raise DevOptionsError("readback_failed", "Restore rollback readback failed.")
                    prior.update(applied=True, attempted=True, state="applied")
                except DevOptionsError as rollback_exc:
                    document["errors"].append(str(rollback_exc))
            document["status"] = "restore_failed"
            _save_snapshot(path, document)
            auto_blocker = _samsung_auto_blocker(adb, serial)
            raise DevOptionsError(
                "restore_failed",
                "Restore failed; rollback to the applied profile was attempted and the snapshot was retained.",
                document,
                failure_id="devopts.samsung_auto_blocker" if auto_blocker else None,
            ) from exc

        restored_names = [entry["name"] for entry, _ in restored]
        accepted_names = [
            entry["name"] for entry, _ in restored if entry["state"] == "restored_uncertain"
        ]
        approximate = [entry["name"] for entry, handler in restored if not handler.exact_restore]
        extra = ""
        if any(handler.kind == "property" for _, handler in restored):
            extra += " Property changes take effect when the app restarts."
        if approximate:
            extra += " Restore is approximate for: " + ", ".join(approximate) + "."
        if left:
            document["status"] = "restore_partial"
            _save_snapshot(path, document)
            return {
                "ok": False,
                "command": "dev-options restore",
                "snapshot": str(path),
                "status": document["status"],
                "restored": restored_names,
                "accepted_uncertain": accepted_names,
                "left_untouched": [entry["name"] for entry in left],
                "conflicts": list(document["conflicts"]),
                "note": (
                    "Partial restore: settings that changed during restore were left untouched; "
                    "restored settings matched readback. The snapshot was retained; resolve the "
                    "conflicts and run restore again to finish."
                ),
            }

        document["status"] = "restored"
        _save_snapshot(path, document)
        return {
            "ok": True,
            "command": "dev-options restore",
            "snapshot": str(path),
            "status": document["status"],
            "restored": restored_names,
            "accepted_uncertain": accepted_names,
            "approximate_restore": approximate,
            "note": "Readback matched the snapshot; the snapshot was retained as recovery evidence." + extra,
        }


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def _render_detection(detection: dict[str, Any]) -> list[str]:
    lines = [f"Detection (read-only): maker layer {detection['oem']}, API {detection['api_level']}, transport {detection['transport']}"]
    if detection.get("manufacturer"):
        lines.append(f"  manufacturer {detection['manufacturer']}, brand {detection.get('brand')}")
    samsung = detection.get("samsung")
    if samsung:
        version = samsung["version"] or (f"about {samsung['estimated_major']} (estimated from Android)" if samsung["estimated_major"] else "unknown")
        lines.append(f"  One UI {version} (undocumented by Samsung)")
    pixel = detection.get("pixel")
    if pixel:
        lines.append(f"  Pixel build {pixel['build_id']}, SoC {pixel['soc_model']}")
    lines.append(f"  build type {detection.get('build_type')}, page size {detection.get('page_size')}")
    form = detection.get("form_factor") or {}
    hints = [name for name, value in form.items() if value is True]
    lines.append("  form factor hints: " + (", ".join(hints) if hints else "none"))
    admin = detection.get("device_admin")
    if admin is not None:
        lines.append(f"  device administrator present: {admin['present']}")
    for text in detection.get("guidance", []):
        lines.append(f"  Note: {text}")
    if detection.get("unread"):
        lines.append("  not readable: " + ", ".join(f"{item['item']} ({item['status']})" for item in detection["unread"]))
    return lines


def _render_inspection(report: dict[str, Any]) -> str:
    device = report["device"]
    lines = [
        f"Device {device['ro_serialno']}  user {device['android_user']}",
        f"Build {device['build_fingerprint']}",
    ]
    current_group = None
    for item in report["settings"]:
        if item["group"] != current_group:
            current_group = item["group"]
            lines.append(f"{current_group}:")
        value = item["value"] if item["status"] == "observed" else item["status"]
        lines.append(f"  {item['name']}: {value}")
    if report.get("detection"):
        lines.extend(_render_detection(report["detection"]))
    reads = report.get("catalog_reads")
    if reads:
        counts = reads["counts"]
        lines.append(
            f"Catalog reads: {counts['observed']} observed, {counts['error']} failed, "
            f"{counts['skipped']} without an allowlisted read, {counts['not_read']} not read (time budget)."
        )
        for item in reads["entries"]:
            if item["status"] == "observed":
                lines.append(f"  {item['id']}: {item['value']}")
    lines.append(report["note"])
    return "\n".join(lines)


def _render_profiles(report: dict[str, Any]) -> str:
    detection = report["detection"]
    lines = [f"Profiles for maker layer {detection['oem']}, API {detection['api_level']}, transport {detection['transport']} (read-only):"]
    for item in report["profiles"]:
        low, high = item["min_api"], item["max_api"]
        span = "any API" if low is None else f"API {low}-{high}" if high else f"API {low}+"
        status = "eligible" if item["eligible"] else "not eligible"
        package = {"required": ", needs --package", "optional": ", optional --package", "none": ""}[item["package"]]
        lines.append(f"  {item['name']}: {status} ({span}{package}); {item['description']}")
        for reason in item["reasons"]:
            lines.append(f"      why not: {reason['text']}")
        for warning in item["warnings"]:
            lines.append(f"      note: {warning}")
    lines.append(report["note"])
    return "\n".join(lines)


def _emit_error(exc: DevOptionsError, *, json_output: bool = False, command: str = "dev-options") -> None:
    # The first line and the JSON keys are unchanged; failure guidance is added after
    # them (stderr in human mode, an additive `failure` key in JSON mode).
    failure_id = classify(exc, area="dev_options")
    context = FailureContext.from_runtime(command=command)
    if json_output:
        payload = {"ok": False, "error": exc.kind, "message": str(exc)}
        typer.echo(json.dumps(attach_failure(payload, failure_id, context)))
    else:
        typer.echo(f"Error: {exc}", err=True)
        emit_failure(failure_id, context)


dev_options_app = typer.Typer(
    name="dev-options",
    help="Inspect and reversibly apply bounded Developer Options profiles.",
    no_args_is_help=True,
)


@dev_options_app.command("inspect")
def inspect_command(
    serial: str = typer.Option(..., "--serial", "-s", help="Explicit ADB target serial."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
    all_entries: bool = typer.Option(
        False, "--all",
        help="Also read every catalog entry with an allowlisted read command for this device's API and maker layer (bounded time and output).",
    ),
) -> None:
    """Read allowlisted Developer Options and detect the device's maker layer without changing it."""
    try:
        report = inspect_device(serial, include_all=True) if all_entries else inspect_device(serial)
    except DevOptionsError as exc:
        _emit_error(exc, json_output=json_output, command="dev-options inspect")
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(report, sort_keys=True) if json_output else _render_inspection(report))


@dev_options_app.command("profiles")
def profiles_command(
    serial: str = typer.Option(..., "--serial", "-s", help="Explicit ADB target serial."),
    package: str | None = typer.Option(None, "--package", help="Optional app id, to see which per-app steps would apply."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List every profile and whether this device is eligible, from read-only facts."""
    try:
        report = profiles_report(serial, package=package)
    except DevOptionsError as exc:
        _emit_error(exc, json_output=json_output, command="dev-options profiles")
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(report, sort_keys=True) if json_output else _render_profiles(report))


@dev_options_app.command("catalog")
def catalog_command(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
    layer: str | None = typer.Option(None, "--layer", help="aosp, pixel, samsung or adb_control."),
    api: int | None = typer.Option(None, "--api", min=1, max=999, help="Only entries present at this Android API level."),
) -> None:
    """List the curated Developer Options catalog (offline, no device)."""
    report = dev_catalog.catalog_report(layer=layer, api=api)
    if not report["ok"]:
        typer.echo(json.dumps(report) if json_output else f"Error: {report['message']}", err=not json_output)
        raise typer.Exit(2)
    typer.echo(json.dumps(report, sort_keys=True) if json_output else dev_catalog.render_catalog(report))


@dev_options_app.command("pixel-16kb-check")
def pixel_16kb_command(
    serial: str = typer.Option(..., "--serial", "-s", help="Explicit ADB target serial."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Read the page size and show how to test 16 KB pages safely (changes nothing)."""
    try:
        report = pixel_16kb_check(serial)
    except DevOptionsError as exc:
        _emit_error(exc, json_output=json_output, command="dev-options pixel-16kb-check")
        raise typer.Exit(1) from exc
    if json_output:
        typer.echo(json.dumps(report, sort_keys=True))
        return
    size = report["page_size"]
    typer.echo(f"Page size: {size if size is not None else 'unknown'} bytes (maker layer {report['oem']}).")
    for line in report["guidance"]:
        typer.echo(line)
    for line in report["never_automated"]:
        typer.echo(f"Never automated: {line}")
    typer.echo(report["claim_boundary"])


@dev_options_app.command("apply")
def apply_command(
    profile: str = typer.Option(..., "--profile", help="A profile name; list them with 'adapt dev-options profiles'."),
    serial: str = typer.Option(..., "--serial", "-s", help="Explicit ADB target serial."),
    snapshot: Path = typer.Option(..., "--snapshot", help="New recovery snapshot path."),
    package: str | None = typer.Option(None, "--package", help="App id for profiles with per-app steps."),
    option: list[str] = typer.Option([], "--option", help="An optional step the profile lists; repeat for several."),
) -> None:
    """Apply a bounded profile after persisting its original values."""
    try:
        report = apply_profile(profile, serial, snapshot, package=package, options=option)
    except DevOptionsError as exc:
        _emit_error(exc, command="dev-options apply")
        raise typer.Exit(1) from exc
    typer.echo(f"Applied {profile}; recovery snapshot: {report['snapshot']}")
    typer.echo(report["note"])
    typer.echo(report["claim_boundary"])


@dev_options_app.command("restore")
def restore_command(
    serial: str = typer.Option(..., "--serial", "-s", help="Explicit ADB target serial."),
    snapshot: Path = typer.Option(..., "--snapshot", help="Recovery snapshot path."),
    accept_uncertain: bool = typer.Option(
        False,
        "--accept-uncertain",
        help=(
            "Also restore a setting whose write was interrupted before it was recorded, "
            "but only when the device currently holds exactly the value ADAPT intended to write."
        ),
    ),
) -> None:
    """Restore a matching snapshot without overwriting external changes."""
    try:
        report = restore_snapshot(serial, snapshot, accept_uncertain=accept_uncertain)
    except DevOptionsError as exc:
        _emit_error(exc, command="dev-options restore")
        raise typer.Exit(1) from exc
    if not report["ok"]:
        for conflict in report["conflicts"]:
            typer.echo(f"Conflict: {conflict}", err=True)
        typer.echo(report["note"], err=True)
        context = FailureContext.from_runtime(command="dev-options restore")
        if report.get("status") == "restore_partial":
            emit_failure("devopts.restore_partial", context)
        else:
            emit_failure("devopts.restore_conflict", context)
        raise typer.Exit(1)
    typer.echo(f"Restored snapshot: {report['snapshot']}")
    typer.echo(report["note"])
