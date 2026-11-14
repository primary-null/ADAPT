"""Bounded Android Developer Options inspection and reversible profiles.

Only allowlisted Settings Provider keys are read or changed.  Mutating profiles
write a device-bound recovery snapshot before their first device write.
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
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator

import typer

from adapt_adb.doctor import UnsafeAdbServerError, preflight_adb_server
from adapt_adb.failure_report import FailureContext, attach_failure, classify, emit_failure


SCHEMA_VERSION = 1
ADB_TIMEOUT_SECONDS = 10
MAX_SNAPSHOT_BYTES = 1_000_000
# ADB's -s value is an opaque host argv element, not a remote shell word.
# mDNS names may include spaces, parentheses, and Unicode.
_SERIAL_RE = re.compile(r"^[^\x00-\x1f\x7f-\x9f]{1,1024}$")
_NUMBER_RE = re.compile(r"^-?(?:\d+(?:\.\d*)?|\.\d+)$")
_DENIED_RE = re.compile(
    r"permission denial|securityexception|not allowed|operation not permitted|access denied",
    re.IGNORECASE,
)
_ERROR_RE = re.compile(r"(?:^|\n)\s*(?:error|failure)\s*[:\[]", re.IGNORECASE)
_LOCK_ROOT = Path(tempfile.gettempdir()) / "adapt-adb-dev-options-locks"


@dataclass(frozen=True)
class SettingSpec:
    name: str
    group: str
    table: str
    key: str
    user_scoped: bool = False


SETTINGS: tuple[SettingSpec, ...] = (
    SettingSpec("developer_mode", "debugging", "global", "development_settings_enabled"),
    SettingSpec("usb_debugging", "debugging", "global", "adb_enabled"),
    SettingSpec("wireless_debugging", "debugging", "global", "adb_wifi_enabled"),
    SettingSpec("stay_awake", "power", "global", "stay_on_while_plugged_in"),
    SettingSpec("window_animation_scale", "animations", "global", "window_animation_scale"),
    SettingSpec("transition_animation_scale", "animations", "global", "transition_animation_scale"),
    SettingSpec("animator_duration_scale", "animations", "global", "animator_duration_scale"),
    SettingSpec("show_touches", "input", "system", "show_touches", True),
    SettingSpec("pointer_location", "input", "system", "pointer_location", True),
    SettingSpec("dont_keep_activities", "lifecycle", "global", "always_finish_activities"),
    SettingSpec("show_background_anrs", "lifecycle", "secure", "anr_show_background", True),
    SettingSpec("freeform_windows", "desktop", "global", "enable_freeform_support"),
    SettingSpec("force_desktop", "desktop", "global", "force_desktop_mode_on_external_displays"),
)
_SETTING_BY_ID = {(item.table, item.key): item for item in SETTINGS}
_SETTING_BY_NAME = {item.name: item for item in SETTINGS}
_PROFILE_VALUES: dict[str, dict[str, str]] = {
    "fast-ui": {
        "window_animation_scale": "0",
        "transition_animation_scale": "0",
        "animator_duration_scale": "0",
    },
    "touches": {"show_touches": "1"},
    "desktop-testing": {"freeform_windows": "1", "force_desktop": "1"},
    "stay-awake": {"stay_awake": "2"},
}
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
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise DevOptionsError("malformed_value", f"{field} is not numeric.") from exc
    if not number.is_finite():
        raise DevOptionsError("malformed_value", f"{field} must be finite.")
    return value


def _numbers_equal(left: str, right: str) -> bool:
    try:
        return Decimal(_validate_number(left)) == Decimal(_validate_number(right))
    except DevOptionsError:
        return False


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
        raise AdbCommandError("denied", combined or "ADB permission denied.")
    if result.returncode != 0 or _ERROR_RE.search(combined):
        message = combined or f"adb exited with status {result.returncode}."
        raise AdbCommandError("adb_error", message)
    return stdout.strip()


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
            raise DevOptionsError("malformed_value", "A numeric settings value is required.")
        args.append(_validate_number(value))
    elif value is not None:
        raise DevOptionsError("untrusted_operation", "Unexpected settings value.")
    return args


def _read_setting(
    adb_path: Path, serial: str, spec: SettingSpec, android_user: int
) -> dict[str, Any]:
    try:
        raw = _run_adb(
            adb_path,
            serial,
            _setting_args("get", spec, android_user=android_user),
        )
    except AdbCommandError as exc:
        return {"status": exc.kind, "value": None, "detail": str(exc)}
    if raw == "null":
        return {"status": "missing", "value": None, "detail": "key is absent"}
    if not raw or "\n" in raw or not _NUMBER_RE.fullmatch(raw):
        return {
            "status": "malformed",
            "value": None,
            "detail": "settings returned a nonnumeric value",
        }
    return {"status": "observed", "value": raw, "detail": None}


def _require_observation(observation: dict[str, Any], spec: SettingSpec) -> tuple[bool, str | None]:
    status = observation["status"]
    if status == "missing":
        return False, None
    if status != "observed":
        detail = observation.get("detail") or status
        raise DevOptionsError(
            f"setting_{status}", f"Cannot read {spec.table}/{spec.key}: {detail}"
        )
    value = _validate_number(observation["value"], field=f"{spec.key} value")
    return True, value


def _read_identity(adb_path: Path, serial: str) -> DeviceIdentity:
    ro_serialno = _run_adb(adb_path, serial, ["shell", "getprop", "ro.serialno"])
    fingerprint = _run_adb(
        adb_path, serial, ["shell", "getprop", "ro.build.fingerprint"]
    )
    user_raw = _run_adb(adb_path, serial, ["shell", "am", "get-current-user"])
    unsafe_identity = re.compile(r"[\x00-\x1f\x7f]")
    if (
        not ro_serialno
        or ro_serialno.lower() in {"null", "unknown"}
        or len(ro_serialno) > 256
        or unsafe_identity.search(ro_serialno)
        or not fingerprint
        or fingerprint.lower() in {"null", "unknown"}
        or len(fingerprint) > 2048
        or unsafe_identity.search(fingerprint)
        or not re.fullmatch(r"\d+", user_raw)
    ):
        raise DevOptionsError(
            "identity_unavailable",
            "Device identity is unavailable; refusing to use the transport endpoint as identity.",
        )
    return DeviceIdentity(ro_serialno, fingerprint, int(user_raw))


@operation("Inspecting Developer Options")
def inspect_device(serial: str, *, adb_path: Path | str | None = None) -> dict[str, Any]:
    """Inspect only the bounded, allowlisted Developer Options observations."""
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
    return {
        "ok": True,
        "command": "dev-options inspect",
        "target_serial": serial,
        "device": asdict(identity),
        "settings": observations,
        "note": "A settings value is an observation, not proof that the device exposes or honors the option.",
    }


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
    _LOCK_ROOT.mkdir(parents=True, exist_ok=True)
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


def _restore_original_value(
    adb: Path,
    serial: str,
    spec: SettingSpec,
    user: int,
    original: dict[str, Any],
) -> None:
    if original["present"]:
        _run_adb(
            adb,
            serial,
            _setting_args("put", spec, android_user=user, value=original["value"]),
        )
    else:
        _run_adb(adb, serial, _setting_args("delete", spec, android_user=user))
    observed = _read_setting(adb, serial, spec, user)
    if original["present"]:
        if observed["status"] != "observed" or not _numbers_equal(
            observed["value"], original["value"]
        ):
            raise DevOptionsError("readback_failed", f"Restoration readback failed for {spec.key}.")
    elif observed["status"] != "missing":
        raise DevOptionsError("readback_failed", f"Deletion readback failed for {spec.key}.")


def _rollback_apply(
    document: dict[str, Any], path: Path, adb: Path, serial: str
) -> None:
    user = document["device"]["android_user"]
    for entry in reversed(document["settings"]):
        if not entry["attempted"]:
            continue
        spec = _SETTING_BY_ID[(entry["table"], entry["key"])]
        observed = _read_setting(adb, serial, spec, user)
        original = entry["original"]
        already_original = (
            original["present"]
            and observed["status"] == "observed"
            and _numbers_equal(observed["value"], original["value"])
        ) or (not original["present"] and observed["status"] == "missing")
        try:
            if already_original:
                pass
            elif observed["status"] == "observed" and _numbers_equal(
                observed["value"], entry["applied_value"]
            ):
                _restore_original_value(adb, serial, spec, user, original)
            else:
                raise DevOptionsError(
                    "rollback_conflict", f"Rollback refused for externally changed {spec.key}."
                )
            entry.update(applied=False, attempted=False, state="rolled_back")
        except DevOptionsError as exc:
            entry["state"] = "error"
            document["errors"].append(str(exc))
        try:
            _save_snapshot(path, document)
        except DevOptionsError as exc:
            document["errors"].append(str(exc))


def _profile_value(profile: str, name: str, original: str | None) -> str:
    if profile == "stay-awake":
        # Enable USB power only, preserving AC/wireless/dock bits already set.
        # Unknown future bits or non-integer OEM values must not be overwritten.
        if original is not None and (not re.fullmatch(r"[0-9]{1,2}", original) or int(original) > 15):
            raise DevOptionsError("unsupported_value", "Stay-awake requires a known charging bitmask (0-15); no setting was changed.")
        return str((int(original) if original is not None else 0) | 2)
    return _PROFILE_VALUES[profile][name]


@operation("Applying test profile")
def apply_profile(
    profile: str,
    serial: str,
    snapshot: Path | str,
    *,
    adb_path: Path | str | None = None,
) -> dict[str, Any]:
    """Apply one bounded profile after exclusively creating its recovery snapshot."""
    if profile not in _PROFILE_VALUES:
        raise DevOptionsError("invalid_profile", "Profile must be fast-ui, touches, desktop-testing or stay-awake.")
    serial = _validate_serial(serial)
    path = Path(snapshot)
    adb = _resolve_adb_path(adb_path)
    if profile == "desktop-testing":
        sdk = _run_adb(adb, serial, ["shell", "getprop", "ro.build.version.sdk"]).strip()
        if not re.fullmatch(r"[0-9]{1,3}", sdk) or not 29 <= int(sdk) <= 37:
            raise DevOptionsError("unsupported_api", "Desktop testing settings require observed Android 10–17 / API 29–37; future OEM behavior is unverified.")
    identity = _read_identity(adb, serial)
    with _device_lock(identity):
        specs = [_SETTING_BY_NAME[name] for name in _PROFILE_VALUES[profile]]
        entries: list[dict[str, Any]] = []
        for spec in specs:
            present, original_value = _require_observation(
                _read_setting(adb, serial, spec, identity.android_user), spec
            )
            applied_value = _profile_value(profile, spec.name, original_value)
            changed = not present or not _numbers_equal(original_value or "", applied_value)
            entries.append(
                {
                    "name": spec.name,
                    "table": spec.table,
                    "key": spec.key,
                    "user": identity.android_user if spec.user_scoped else None,
                    "original": {"present": present, "value": original_value},
                    "applied_value": applied_value,
                    "changed": changed,
                    "attempted": False,
                    "applied": False,
                    "state": "pending" if changed else "not_changed",
                }
            )
        now = _utc_now()
        document: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "created_at": now,
            "updated_at": now,
            "profile": profile,
            "status": "prepared",
            "target_serial": serial,
            "device": asdict(identity),
            "settings": entries,
            "errors": [],
            "conflicts": [],
        }
        _create_snapshot(path, document)

        for entry in entries:
            if not entry["changed"]:
                continue
            spec = _SETTING_BY_ID[(entry["table"], entry["key"])]
            try:
                document["status"] = "applying"
                entry.update(attempted=True, state="pending")
                _save_snapshot(path, document)
                _run_adb(
                    adb,
                    serial,
                    _setting_args(
                        "put",
                        spec,
                        android_user=identity.android_user,
                        value=entry["applied_value"],
                    ),
                )
                observed = _read_setting(adb, serial, spec, identity.android_user)
                if observed["status"] != "observed" or not _numbers_equal(
                    observed["value"], entry["applied_value"]
                ):
                    raise DevOptionsError("readback_failed", f"Apply readback failed for {spec.key}.")
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
                raise DevOptionsError(
                    "apply_failed",
                    "Profile apply failed; rollback was attempted and the snapshot was retained.",
                    document,
                    failure_id="devopts.apply_rolled_back" if rolled_back else None,
                ) from exc

        document["status"] = "applied"
        _save_snapshot(path, document)
        return {
            "ok": True,
            "command": "dev-options apply",
            "profile": profile,
            "snapshot": str(path),
            "changed": [entry["name"] for entry in entries if entry["applied"]],
            "unchanged": [entry["name"] for entry in entries if not entry["changed"]],
            "note": "Snapshot retained; restoration is best-effort and must be run explicitly." + (" Desktop settings may require a manual reboot and may be ignored by this OEM; no reboot was performed." if profile == "desktop-testing" else "") + (" Stay-awake requests USB-powered screen-on and preserves existing charging modes. Readback does not prove effective wakefulness; device policy can override it. Restore after testing." if profile == "stay-awake" else ""),
        }


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


def _validate_snapshot(document: Any) -> dict[str, Any]:
    top_keys = {
        "schema_version",
        "created_at",
        "updated_at",
        "profile",
        "status",
        "target_serial",
        "device",
        "settings",
        "errors",
        "conflicts",
    }
    if not isinstance(document, dict) or set(document) != top_keys:
        raise DevOptionsError("malformed_snapshot", "Snapshot fields are invalid.")
    if document["schema_version"] != SCHEMA_VERSION:
        raise DevOptionsError("malformed_snapshot", "Unsupported snapshot schema.")
    profile = document["profile"]
    if profile not in _PROFILE_VALUES or document["status"] not in _SNAPSHOT_STATUSES:
        raise DevOptionsError("malformed_snapshot", "Snapshot profile or status is invalid.")
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
    unsafe_identity = re.compile(r"[\x00-\x1f\x7f]")
    if (
        not isinstance(device["ro_serialno"], str)
        or not device["ro_serialno"]
        or len(device["ro_serialno"]) > 256
        or unsafe_identity.search(device["ro_serialno"])
        or not isinstance(device["build_fingerprint"], str)
        or not device["build_fingerprint"]
        or len(device["build_fingerprint"]) > 2048
        or unsafe_identity.search(device["build_fingerprint"])
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

    entries = document["settings"]
    expected_names = set(_PROFILE_VALUES[profile])
    entry_keys = {
        "name",
        "table",
        "key",
        "user",
        "original",
        "applied_value",
        "changed",
        "attempted",
        "applied",
        "state",
    }
    if not isinstance(entries, list) or len(entries) != len(expected_names):
        raise DevOptionsError("malformed_snapshot", "Snapshot settings are invalid.")
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != entry_keys:
            raise DevOptionsError("malformed_snapshot", "Snapshot setting fields are invalid.")
        name = entry["name"]
        if name not in expected_names or name in seen:
            raise DevOptionsError("untrusted_setting", "Snapshot contains an untrusted setting.",
                                  failure_id="devopts.snapshot_untrusted")
        seen.add(name)
        spec = _SETTING_BY_NAME[name]
        if entry["table"] != spec.table or entry["key"] != spec.key:
            raise DevOptionsError("untrusted_setting", "Snapshot table/key is not allowlisted.",
                                  failure_id="devopts.snapshot_untrusted")
        expected_user = device["android_user"] if spec.user_scoped else None
        if entry["user"] != expected_user:
            raise DevOptionsError("malformed_snapshot", "Snapshot setting user is invalid.")
        original = entry["original"]
        if not isinstance(original, dict) or set(original) != {"present", "value"}:
            raise DevOptionsError("malformed_snapshot", "Snapshot original value is invalid.")
        if type(original["present"]) is not bool:
            raise DevOptionsError("malformed_snapshot", "Snapshot presence flag is invalid.")
        if original["present"]:
            _validate_number(original["value"], field="snapshot original value")
        elif original["value"] is not None:
            raise DevOptionsError("malformed_snapshot", "Absent key must have a null value.")
        if entry["applied_value"] != _profile_value(profile, name, original["value"]):
            raise DevOptionsError("untrusted_value", "Snapshot applied value is not allowlisted.")
        for flag in ("changed", "attempted", "applied"):
            if type(entry[flag]) is not bool:
                raise DevOptionsError("malformed_snapshot", f"Snapshot {flag} flag is invalid.")
        expected_changed = not original["present"] or not _numbers_equal(
            original["value"], entry["applied_value"]
        )
        if entry["changed"] != expected_changed or entry["state"] not in _ENTRY_STATUSES:
            raise DevOptionsError("malformed_snapshot", "Snapshot transaction state is invalid.")
        if entry["applied"] and not entry["attempted"]:
            raise DevOptionsError("malformed_snapshot", "Applied setting was not marked attempted.")
    return document


def _matches_original(observed: dict[str, Any], original: dict[str, Any]) -> bool:
    return bool(
        (
            original["present"]
            and observed["status"] == "observed"
            and _numbers_equal(observed["value"], original["value"])
        )
        or (not original["present"] and observed["status"] == "missing")
    )


def _matches_applied(observed: dict[str, Any], entry: dict[str, Any]) -> bool:
    return observed["status"] == "observed" and _numbers_equal(
        observed["value"], entry["applied_value"]
    )


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
    currently holds exactly the value ADAPT intended to write.
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
        user = current_identity.android_user
        document["conflicts"] = []
        ready: list[tuple[dict[str, Any], SettingSpec, bool]] = []
        for entry in document["settings"]:
            if not entry["changed"] or (not entry["attempted"] and not entry["applied"]):
                continue
            spec = _SETTING_BY_ID[(entry["table"], entry["key"])]
            observed = _read_setting(adb, serial, spec, user)
            uncertain = entry["attempted"] and not entry["applied"]
            holds_applied = _matches_applied(observed, entry)
            if _matches_original(observed, entry["original"]):
                entry.update(applied=False, attempted=False, state="already_restored")
            elif holds_applied and (entry["applied"] or accept_uncertain):
                ready.append((entry, spec, uncertain))
            else:
                if uncertain:
                    if holds_applied:
                        reason = (
                            f"{spec.key}: outcome is uncertain; refusing to overwrite current state. "
                            "The current value equals the value ADAPT intended to write; rerun "
                            "restore with --accept-uncertain to restore the original value."
                        )
                    elif accept_uncertain:
                        reason = (
                            f"{spec.key}: outcome is uncertain and the current value matches neither "
                            "the original nor the value ADAPT intended to write; refusing to "
                            "overwrite current state even with --accept-uncertain"
                        )
                    else:
                        reason = (
                            f"{spec.key}: outcome is uncertain; refusing to overwrite current state. "
                            "--accept-uncertain applies only when the current value equals the "
                            "value ADAPT intended to write, which it does not."
                        )
                    entry["state"] = "uncertain"
                else:
                    reason = f"{spec.key}: current value differs from the applied value"
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

        restored: list[tuple[dict[str, Any], SettingSpec]] = []
        left: list[dict[str, Any]] = []
        try:
            for entry, spec, uncertain in ready:
                # Ownership is re-checked immediately before each write, so a
                # change another actor made after preflight is not overwritten.
                # Android offers no conditional (compare-and-set) settings
                # write, so a small window between this read and the write
                # below remains.
                observed = _read_setting(adb, serial, spec, user)
                if _matches_original(observed, entry["original"]):
                    entry.update(applied=False, attempted=False, state="already_restored")
                    _save_snapshot(path, document)
                    continue
                if not _matches_applied(observed, entry):
                    if observed["status"] == "observed":
                        reason = f"{spec.key}: changed during restore; left untouched"
                    else:
                        reason = (
                            f"{spec.key}: could not be re-read during restore "
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
                _restore_original_value(adb, serial, spec, user, entry["original"])
                entry.update(
                    applied=False,
                    attempted=False,
                    state="restored_uncertain" if uncertain else "restored",
                )
                restored.append((entry, spec))
                _save_snapshot(path, document)
        except DevOptionsError as exc:
            document["errors"].append(str(exc))
            # Return successfully restored entries to the applied profile when safe.
            # Entries skipped because they changed during restore were never
            # written, so the rollback leaves them alone and they keep their
            # conflict record in the snapshot.
            for prior, prior_spec in reversed(restored):
                observed = _read_setting(adb, serial, prior_spec, user)
                original = prior["original"]
                is_original = (
                    original["present"]
                    and observed["status"] == "observed"
                    and _numbers_equal(observed["value"], original["value"])
                ) or (not original["present"] and observed["status"] == "missing")
                if not is_original:
                    document["errors"].append(
                        f"Restore rollback refused for externally changed {prior_spec.key}."
                    )
                    continue
                try:
                    _run_adb(
                        adb,
                        serial,
                        _setting_args(
                            "put",
                            prior_spec,
                            android_user=user,
                            value=prior["applied_value"],
                        ),
                    )
                    check = _read_setting(adb, serial, prior_spec, user)
                    if check["status"] != "observed" or not _numbers_equal(
                        check["value"], prior["applied_value"]
                    ):
                        raise DevOptionsError("readback_failed", "Restore rollback readback failed.")
                    prior.update(applied=True, attempted=True, state="applied")
                except DevOptionsError as rollback_exc:
                    document["errors"].append(str(rollback_exc))
            document["status"] = "restore_failed"
            _save_snapshot(path, document)
            raise DevOptionsError(
                "restore_failed",
                "Restore failed; rollback to the applied profile was attempted and the snapshot was retained.",
                document,
            ) from exc

        restored_names = [entry["name"] for entry, _ in restored]
        accepted_names = [
            entry["name"] for entry, _ in restored if entry["state"] == "restored_uncertain"
        ]
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
            "note": "Readback matched the snapshot; the snapshot was retained as recovery evidence.",
        }


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
) -> None:
    """Read allowlisted Developer Options without changing the device."""
    try:
        report = inspect_device(serial)
    except DevOptionsError as exc:
        _emit_error(exc, json_output=json_output, command="dev-options inspect")
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(report, sort_keys=True) if json_output else _render_inspection(report))


@dev_options_app.command("apply")
def apply_command(
    profile: str = typer.Option(..., "--profile", help="fast-ui, touches, stay-awake (USB power), or desktop-testing (may need manual reboot)."),
    serial: str = typer.Option(..., "--serial", "-s", help="Explicit ADB target serial."),
    snapshot: Path = typer.Option(..., "--snapshot", help="New recovery snapshot path."),
) -> None:
    """Apply a bounded profile after persisting its original values."""
    try:
        report = apply_profile(profile, serial, snapshot)
    except DevOptionsError as exc:
        _emit_error(exc, command="dev-options apply")
        raise typer.Exit(1) from exc
    typer.echo(f"Applied {profile}; recovery snapshot: {report['snapshot']}")
    typer.echo(report["note"])


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
