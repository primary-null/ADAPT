"""Diagnostic (`adapt doctor`) and user-scope bootstrap (`adapt setup`) contract engine.

Implements the machine-checkable behavioral, diagnostic, bootstrap, safety, and
exit-code contract defined in `docs/doctor.md`.

Hard safety invariants:
- Never invokes `adb kill-server` or kills any process.
- Never invokes `sudo` or recommends `sudo adb`.
- Never calls `adb devices -l` if `adb_server_5037` fails (prevents stock adb client
  from auto-killing a mismatched shared server).
- Never mutates or writes inside `ANDROID_HOME` / `ANDROID_SDK_ROOT`.
- Never deletes, overwrites, or regenerates `adbkey` / `adbkey.pub`.
- Never recommends or invokes `Zadig` or `bcdedit` (`testsigning`).
- Never runs `logcat` without `-d`.
- Redacts device serial numbers in diagnostic structures.
"""

from __future__ import annotations

from adapt_adb.progress import operation

import ctypes
import hashlib
import inspect
import io
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


from adapt_adb.adb import (
    AdbProbeError,
    PLATFORM_TOOLS_URL_LINUX,
    PLATFORM_TOOLS_URL_WINDOWS,
    add_to_windows_user_path,
    download_and_extract_platform_tools,
    get_default_platform_tools_dir,
    parse_devices_short,
    split_adb_prefix,
)

_PROC_ROOT = Path("/proc")
_TMP_5037_PATH = Path("/tmp/5037")

SCHEMA_VERSION = "1"
MIN_PLATFORM_TOOLS_VERSION = (34, 0, 0)

EXIT_OK = 0
EXIT_MISSING_TOOLCHAIN = 1
EXIT_DEVICE_OR_PERMISSIONS = 2
EXIT_UNSAFE_SHARED_SERVER = 3

STATUS_BY_EXIT_CODE: dict[int, str] = {
    EXIT_OK: "ok",
    EXIT_MISSING_TOOLCHAIN: "missing_toolchain",
    EXIT_DEVICE_OR_PERMISSIONS: "device_unauthorized_or_no_permissions",
    EXIT_UNSAFE_SHARED_SERVER: "unsafe_shared_server",
}

EXIT_PRECEDENCE: dict[int, int] = {
    EXIT_UNSAFE_SHARED_SERVER: 4,
    EXIT_MISSING_TOOLCHAIN: 3,
    EXIT_DEVICE_OR_PERMISSIONS: 2,
    EXIT_OK: 1,
}

CHECK_IDS: tuple[str, ...] = (
    "adb_server_5037",
    "adb_path",
    "adb_version",
    "adb_origin",
    "path_config",
    "android_home",
    "windows_pnp",
    "debian_usb_permissions",
    "devices_state",
)

_VERSION_LINE_RE = re.compile(r"^Version\s+(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z._-]+))?", re.IGNORECASE)
_BRIDGE_LINE_RE = re.compile(r"Android Debug Bridge version\s+(\d+\.\d+\.\d+)", re.IGNORECASE)
_INSTALLED_AS_RE = re.compile(r"Installed as\s+(.+)", re.IGNORECASE)

LINUX_UDEV_RULE_LINE = (
    'SUBSYSTEM=="usb", ATTR{bInterfaceClass}=="ff", ATTR{bInterfaceSubClass}=="42", '
    'ATTR{bInterfaceProtocol}=="01", TAG+="uaccess", GROUP="plugdev", MODE="0660"'
)


@dataclass
class Server5037State:
    """Observed state of local port 5037 and /tmp/5037 without triggering client auto-kill."""

    listening: bool = False
    probe_error: str | None = None
    pid: int | None = None
    owner: str | None = None
    owner_uid: int | None = None
    owner_is_root_or_system: bool = False
    owner_matches_current_user: bool = True
    server_exe: Path | None = None
    server_version_raw: str | None = None
    tmp_5037_exists: bool = False
    tmp_5037_uid: int | None = None
    tmp_5037_owner_is_root: bool = False
    adbkey_path: Path | None = None
    adbkey_exists: bool = False
    adbkey_owned_by_root: bool = False


@dataclass
class PnpDeviceRecord:
    """Read-only representation of a Windows PnP USB/Android device entry."""

    instance_id: str
    friendly_name: str
    class_name: str
    status: str = "OK"
    problem_code: int = 0


@dataclass
class DebianUsbState:
    """Read-only representation of Debian udev rules, groups, and logind seat."""

    has_udev_rules: bool = True
    has_uaccess_tag: bool = True
    has_plugdev_rule: bool = True
    rule_files: list[str] = field(default_factory=list)
    in_plugdev_group: bool = True
    in_adbusers_group: bool = False
    seat: str | None = "local"  # "local", "headless", or None


@dataclass
class DoctorEnv:
    """Injectable host probe environment for deterministic testing without hardware."""

    host_os: str | None = None  # "windows", "debian", "linux"
    arch: str | None = None
    uid_is_root: bool | None = None
    environ: dict[str, str] | None = None
    home_dir: Path | None = None
    which_adb: Path | None = None
    which_set: bool = False
    path_executables: list[Path] | None = None
    candidate_files: dict[str, Path] | None = None
    existing_paths: set[Path] | None = None
    source_properties: dict[Path, str] | None = None
    dpkg_owners: dict[Path, str | None] | None = None
    version_outputs: dict[Path, str | None] | None = None
    server_state: Server5037State | None = None
    windows_pnp_devices: list[PnpDeviceRecord] | None = None
    windows_user_path: str | None = None
    debian_usb_state: DebianUsbState | None = None
    debian_profile_texts: dict[Path, str] | None = None
    devices_l_output: str | None = None
    devices_l_invoked: list[Path] = field(default_factory=list)
    commands_invoked: list[list[str]] = field(default_factory=list)


def resolve_highest_exit_code(codes: list[int] | tuple[int, ...]) -> int:
    """Return the highest-precedence exit code using 3 > 1 > 2 > 0."""
    best = EXIT_OK
    for code in codes:
        if EXIT_PRECEDENCE.get(code, 0) > EXIT_PRECEDENCE.get(best, 0):
            best = code
    return best


def parse_platform_tools_version(raw_output: str | None) -> dict[str, Any]:
    """Parse `adb version` stdout into bridge version, platform-tools tuple, and raw string."""
    if not raw_output:
        return {
            "bridge_version": None,
            "platform_tools_version": None,
            "version_tuple": None,
            "installed_as": None,
            "meets_minimum": False,
        }

    bridge_ver: str | None = None
    pt_ver: str | None = None
    pt_tuple: tuple[int, int, int] | None = None
    installed_as: str | None = None

    for line in raw_output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        m_ver = _VERSION_LINE_RE.search(stripped)
        if m_ver and pt_tuple is None:
            major, minor, patch = int(m_ver.group(1)), int(m_ver.group(2)), int(m_ver.group(3))
            pt_tuple = (major, minor, patch)
            suffix = f"-{m_ver.group(4)}" if m_ver.group(4) else ""
            pt_ver = f"{major}.{minor}.{patch}{suffix}"
        m_bridge = _BRIDGE_LINE_RE.search(stripped)
        if m_bridge and bridge_ver is None:
            bridge_ver = m_bridge.group(1)
        m_inst = _INSTALLED_AS_RE.search(stripped)
        if m_inst and installed_as is None:
            installed_as = m_inst.group(1).strip()

    meets_min = bool(pt_tuple is not None and pt_tuple >= MIN_PLATFORM_TOOLS_VERSION)
    return {
        "bridge_version": bridge_ver,
        "platform_tools_version": pt_ver,
        "version_tuple": pt_tuple,
        "installed_as": installed_as,
        "meets_minimum": meets_min,
    }


def redact_serial(serial: str) -> str:
    """Hash a raw device serial into a short non-reversible token so serials never leak."""
    digest = hashlib.sha256(serial.encode("utf-8", errors="ignore")).hexdigest()[:8]
    return f"device-{digest}"


def parse_adb_devices_output(raw_output: str | None) -> dict[str, Any]:
    """Parse `adb devices -l` output into state buckets and redacted device entries."""
    counts = {
        "device": 0,
        "unauthorized": 0,
        "no_permissions": 0,
        "offline": 0,
        "other": 0,
    }
    redacted_entries: list[dict[str, str]] = []

    if not raw_output:
        return {"total": 0, "by_state": counts, "entries": redacted_entries}

    for device in parse_devices_short(raw_output):
        serial = device["serial"]
        state = device["state"]
        if state == "no permissions":
            state_key = "no_permissions"
            raw_state = "no permissions"
        elif state == "device":
            state_key = "device"
            raw_state = "device"
        elif state == "unauthorized":
            state_key = "unauthorized"
            raw_state = "unauthorized"
        elif state == "offline":
            state_key = "offline"
            raw_state = "offline"
        else:
            state_key = "other"
            raw_state = state

        counts[state_key] += 1
        redacted_entries.append({"id": redact_serial(serial), "state": raw_state})

    total = sum(counts.values())
    return {"total": total, "by_state": counts, "entries": redacted_entries}


class UnsafeAdbServerError(RuntimeError):
    """Raised before contacting an unverified or incompatible ADB daemon."""


def _uses_default_local_server(args: list[str], environ: dict[str, str]) -> bool:
    """Return whether the selected ADB server endpoint is local TCP 5037."""
    parsed = split_adb_prefix(args)
    host = "localhost"
    port = (
        environ.get("ANDROID_ADB_SERVER_PORT", "").strip()
        or environ.get("ADB_SERVER_PORT", "").strip()
        or "5037"
    )
    socket_spec = environ.get("ADB_SERVER_SOCKET", "").strip()
    prefix = parsed.prefix
    for i, token in enumerate(prefix):
        if token == "-H":
            host = prefix[i + 1]
        elif token == "-P":
            port = prefix[i + 1]
        elif token == "-L":
            socket_spec = prefix[i + 1]
    if socket_spec:
        return socket_spec.lower() in {
            "tcp:5037",
            "tcp:localhost:5037",
            "tcp:127.0.0.1:5037",
            "tcp:[::1]:5037",
        }
    return host.lower() in {"localhost", "127.0.0.1", "::1", "[::1]"} and port == "5037"


def preflight_adb_server(
    adb_path: Path,
    args: list[str],
    *,
    environ: dict[str, str] | None = None,
    state: Server5037State | None = None,
) -> None:
    """Perform the focused nonmutating shared-daemon gate used by execution paths."""
    env_map = dict(os.environ if environ is None else environ)
    parsed = split_adb_prefix(args)
    if parsed.command and parsed.command[0] in {"version", "help"}:
        return
    if not _uses_default_local_server(args, env_map):
        raise UnsafeAdbServerError(
            "Selected ADB server endpoint cannot be verified safely; refusing to contact it."
        )
    host_os = _detect_host_os()
    observed = state or _probe_server_5037(host_os, Path.home(), trusted_adb=adb_path)
    if observed.probe_error:
        raise UnsafeAdbServerError("ADB listener inspection failed; idle state is unverified.")
    if not observed.listening:
        return
    if (
        observed.owner is None
        or not observed.owner_matches_current_user
        or observed.owner_is_root_or_system
        or observed.server_version_raw is None
    ):
        raise UnsafeAdbServerError(
            "Listening ADB server ownership/version is unverified or unsafe."
        )
    client_raw = _query_adb_version_raw(adb_path, None)
    server_tuple = parse_platform_tools_version(observed.server_version_raw).get("version_tuple")
    client_tuple = parse_platform_tools_version(client_raw).get("version_tuple")
    if server_tuple is None or client_tuple is None or server_tuple != client_tuple:
        raise UnsafeAdbServerError(
            "Listening ADB server version is unverified or does not match this client."
        )


def _detect_host_os(env: DoctorEnv | None = None) -> str:
    if env and env.host_os:
        return env.host_os
    if sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux"):
        os_release = Path("/etc/os-release")
        try:
            if os_release.is_file():
                text = os_release.read_text(encoding="utf-8", errors="ignore").lower()
                if "debian" in text or "ubuntu" in text:
                    return "debian"
        except OSError:
            pass
        if Path("/etc/debian_version").exists():
            return "debian"
        return "debian"
    return "linux"


def _detect_arch(env: DoctorEnv | None = None) -> str:
    if env and env.arch:
        return env.arch
    machine = platform.machine().strip()
    return machine or "unknown"


def _detect_uid_is_root(host_os: str, env: DoctorEnv | None = None) -> bool:
    if env and env.uid_is_root is not None:
        return env.uid_is_root
    if host_os != "windows" and hasattr(os, "geteuid"):
        try:
            return os.geteuid() == 0  # type: ignore[attr-defined]
        except OSError:
            return False
    return False


def _get_env_map(env: DoctorEnv | None = None) -> dict[str, str]:
    if env and env.environ is not None:
        return env.environ
    return dict(os.environ)


def _get_home_dir(env: DoctorEnv | None = None) -> Path:
    if env and env.home_dir is not None:
        return env.home_dir
    return Path.home()


def _path_exists(p: Path | None, env: DoctorEnv | None = None) -> bool:
    if p is None:
        return False
    if env and env.existing_paths is not None:
        return p in env.existing_paths
    try:
        return p.exists()
    except OSError:
        return False


def _query_adb_version_raw(adb_bin: Path, env: DoctorEnv | None = None) -> str | None:
    if env is not None:
        env.commands_invoked.append([str(adb_bin), "version"])
        if env.version_outputs is not None:
            if adb_bin in env.version_outputs:
                return env.version_outputs[adb_bin]
            for k, v in env.version_outputs.items():
                if str(k) == str(adb_bin):
                    return v
            return None
    try:
        proc = subprocess.run(
            [str(adb_bin), "version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        combined = "\n".join(part for part in (proc.stdout, proc.stderr) if part).strip()
        return combined or None
    except (subprocess.SubprocessError, OSError):
        return None


def _read_source_properties(adb_bin: Path, env: DoctorEnv | None = None) -> str | None:
    prop_path = adb_bin.parent / "source.properties"
    if env and env.source_properties is not None:
        for k, v in env.source_properties.items():
            if k == prop_path or k == adb_bin.parent or str(k) == str(prop_path):
                return v
        return None
    try:
        if prop_path.is_file():
            return prop_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    return None


def _classify_adb_origin(
    adb_bin: Path | None,
    host_os: str,
    home: Path,
    environ: dict[str, str],
    env: DoctorEnv | None = None,
) -> tuple[str, dict[str, Any]]:
    if adb_bin is None:
        return "none", {"reason": "no adb binary resolved"}

    norm_str = str(adb_bin).replace("\\", "/").lower()
    details: dict[str, Any] = {"adb_path": str(adb_bin)}

    if host_os in ("debian", "linux"):
        dpkg_pkg: str | None = None
        if env and env.dpkg_owners is not None:
            for k, v in env.dpkg_owners.items():
                if str(k) == str(adb_bin):
                    dpkg_pkg = v
                    break
        elif norm_str in ("/usr/bin/adb", "/bin/adb") or norm_str.startswith("/usr/lib/android-sdk"):
            dpkg_pkg = "adb"
        else:
            try:
                proc = subprocess.run(
                    ["dpkg", "-S", str(adb_bin)],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=3,
                )
                if proc.returncode == 0 and ":" in proc.stdout:
                    dpkg_pkg = proc.stdout.split(":", 1)[0].strip()
            except (subprocess.SubprocessError, OSError):
                dpkg_pkg = None

        if norm_str in ("/usr/bin/adb", "/bin/adb") or norm_str.startswith("/usr/lib/android-sdk") or dpkg_pkg in ("adb", "android-sdk-platform-tools"):
            details["dpkg_package"] = dpkg_pkg or "adb"
            return "debian_apt", details

    src_props = _read_source_properties(adb_bin, env)
    has_src_props = bool(src_props and "Pkg.Revision" in src_props)
    details["source_properties_present"] = has_src_props

    user_scope_Accept: list[str] = []
    if host_os == "windows":
        local_app = environ.get("LOCALAPPDATA", str(home / "AppData" / "Local"))
        user_scope_Accept.extend(
            [
                str(Path(local_app) / "Android" / "platform-tools").replace("\\", "/").lower(),
                str(Path(local_app) / "Android" / "Sdk" / "platform-tools").replace("\\", "/").lower(),
                str(home / ".android" / "platform-tools").replace("\\", "/").lower(),
                "winget",
                "scoop/apps/adb",
                "google.platformtools",
            ]
        )
    else:
        user_scope_Accept.extend(
            [
                str(home / ".local" / "share" / "android" / "platform-tools").replace("\\", "/").lower(),
                str(home / ".android" / "platform-tools").replace("\\", "/").lower(),
            ]
        )

    android_home = environ.get("ANDROID_HOME") or environ.get("ANDROID_SDK_ROOT")
    if android_home:
        user_scope_Accept.append(
            str(Path(android_home) / "platform-tools").replace("\\", "/").lower()
        )

    in_known_google_dir = any(marker in norm_str for marker in user_scope_Accept)
    details["in_known_user_or_sdk_dir"] = in_known_google_dir

    if has_src_props or in_known_google_dir:
        return "google_official", details

    return "unknown", details


def _is_windows_admin() -> bool:
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except Exception:
        return False


def _uid_to_username(uid: int) -> str:
    try:
        import pwd  # type: ignore[import-not-found]

        return str(pwd.getpwuid(uid).pw_name)
    except (ImportError, KeyError, OSError):
        return str(uid)


def _find_linux_pid_for_inode(inode: str, proc_root: Path) -> int | None:
    if not inode or inode == "0":
        return None
    target = f"socket:[{inode}]"
    try:
        proc_entries = list(proc_root.iterdir())
    except OSError:
        return None
    for entry in proc_entries:
        if not entry.name.isdigit():
            continue
        fd_dir = entry / "fd"
        try:
            for fd_entry in fd_dir.iterdir():
                try:
                    if os.readlink(str(fd_entry)) == target:
                        return int(entry.name)
                except OSError:
                    continue
        except OSError:
            continue
    return None


def _read_linux_proc_uid(pid: int, proc_root: Path) -> int | None:
    status_path = proc_root / str(pid) / "status"
    try:
        for line in status_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("Uid:"):
                parts = line.split()
                if len(parts) >= 3:
                    return int(parts[2])  # effective UID
                if len(parts) >= 2:
                    return int(parts[1])
    except (OSError, ValueError):
        pass
    return None


def _apply_windows_owner(
    state: Server5037State,
    domain: str,
    user: str,
    cur_user: str,
    ret_val: Any = 0,
) -> None:
    domain_clean = domain.strip()
    user_clean = user.strip()
    cur_clean = cur_user.strip().lower()
    if user_clean:
        owner_str = f"{domain_clean}\\{user_clean}" if domain_clean else user_clean
        state.owner = owner_str
        u_low = user_clean.lower()
        o_low = owner_str.lower()
        if u_low in {"system", "local service", "network service"} or o_low in {
            "nt authority\\system",
            "nt authority\\local service",
            "nt authority\\network service",
        }:
            if cur_clean != u_low:
                state.owner_is_root_or_system = True
                state.owner_matches_current_user = False
        elif u_low in {"administrator", "administrators"} or o_low == "builtin\\administrators":
            if cur_clean != u_low and not _is_windows_admin():
                state.owner_is_root_or_system = True
                state.owner_matches_current_user = False
        elif cur_clean and u_low != cur_clean:
            state.owner_matches_current_user = False
    elif ret_val not in (None, 0):
        state.owner = "SYSTEM/Elevated (access denied)"
        state.owner_is_root_or_system = True
        state.owner_matches_current_user = False


def _probe_windows_pid_info(pid: int, cur_user: str) -> tuple[Path | None, str, str, Any, bool]:
    """Query process executable path and owner for a Windows PID read-only.

    Returns (exe_path, domain, user, return_value, query_succeeded).
    """
    ps_cmd = (
        f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}' -ErrorAction Stop; "
        "if ($null -eq $p) { exit 2 }; "
        "$o = Invoke-CimMethod -InputObject $p -MethodName GetOwner -ErrorAction Stop; "
        "[PSCustomObject]@{ "
        "ExecutablePath = $p.ExecutablePath; "
        "Name = $p.Name; "
        "Domain = $o.Domain; "
        "User = $o.User; "
        "ReturnValue = $o.ReturnValue "
        "} | ConvertTo-Json -Compress"
    )
    try:
        proc = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                ps_cmd,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=6,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            data = json.loads(proc.stdout.strip())
            if isinstance(data, list) and data:
                data = data[0]
            if isinstance(data, dict):
                raw_exe = data.get("ExecutablePath")
                exe_path = Path(str(raw_exe)) if raw_exe else None
                domain = str(data.get("Domain") or "")
                user = str(data.get("User") or "")
                ret_val = data.get("ReturnValue")
                return exe_path, domain, user, ret_val, True
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError, ValueError):
        pass

    # Fallback: tasklist /FI "PID eq <pid>" /V /FO CSV /NH
    try:
        import csv
        import io

        tl = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/V", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
            timeout=4,
        )
        if tl.returncode == 0 and tl.stdout.strip():
            for row in csv.reader(io.StringIO(tl.stdout.strip())):
                if len(row) >= 7:
                    owner_col = row[6].strip()
                    if owner_col.upper() == "N/A":
                        return None, "", "", 2, True
                    if "\\" in owner_col:
                        dom, usr = owner_col.split("\\", 1)
                        return None, dom, usr, 0, True
                    if owner_col:
                        return None, "", owner_col, 0, True
    except (subprocess.SubprocessError, OSError, ValueError):
        pass

    return None, "", "", None, False


_CM_PROB_NAME_MAP: dict[str, int] = {
    "CM_PROB_NONE": 0,
    "CM_PROB_NOT_CONFIGURED": 1,
    "CM_PROB_DEVLOADER_FAILED": 2,
    "CM_PROB_OUT_OF_MEMORY": 3,
    "CM_PROB_ENTRY_IS_WRONG_TYPE": 4,
    "CM_PROB_LACKED_ARBITRATOR": 5,
    "CM_PROB_BOOT_CONFIG_CONFLICT": 6,
    "CM_PROB_FAILED_FILTER": 7,
    "CM_PROB_DEVLOADER_NOT_FOUND": 8,
    "CM_PROB_INVALID_DATA": 9,
    "CM_PROB_FAILED_START": 10,
    "CM_PROB_LIAR": 11,
    "CM_PROB_NORMAL_CONFLICT": 12,
    "CM_PROB_NOT_VERIFIED": 13,
    "CM_PROB_NEED_RESTART": 14,
    "CM_PROB_REENUMERATION": 15,
    "CM_PROB_PARTIAL_LOG_CONF": 16,
    "CM_PROB_UNKNOWN_RESOURCE": 17,
    "CM_PROB_REINSTALL": 18,
    "CM_PROB_REGISTRY": 19,
    "CM_PROB_VXDLDR": 20,
    "CM_PROB_WILL_BE_REMOVED": 21,
    "CM_PROB_DISABLED": 22,
    "CM_PROB_DEVLOADER_NOT_READY": 23,
    "CM_PROB_DEVICE_NOT_THERE": 24,
    "CM_PROB_MOVED": 25,
    "CM_PROB_TOO_EARLY": 26,
    "CM_PROB_NO_VALID_LOG_CONF": 27,
    "CM_PROB_FAILED_INSTALL": 28,
    "CM_PROB_HARDWARE_DISABLED": 29,
    "CM_PROB_CANT_SHARE_IRQ": 30,
    "CM_PROB_FAILED_ADD": 31,
    "CM_PROB_DISABLED_SERVICE": 32,
    "CM_PROB_TRANSLATION_FAILED": 33,
    "CM_PROB_NO_SOFTCONFIG": 34,
    "CM_PROB_BIOS_TABLE": 35,
    "CM_PROB_IRQ_TRANSLATION_FAILED": 36,
    "CM_PROB_FAILED_DRIVER_ENTRY": 37,
    "CM_PROB_DRIVER_FAILED_PRIOR_UNLOAD": 38,
    "CM_PROB_DRIVER_FAILED_LOAD": 39,
    "CM_PROB_DRIVER_SERVICE_KEY_INVALID": 40,
    "CM_PROB_LEGACY_SERVICE_NO_DEVICE": 41,
    "CM_PROB_DUPLICATE_DEVICE": 42,
    "CM_PROB_FAILED_POST_START": 43,
    "CM_PROB_HALTED": 44,
    "CM_PROB_PHANTOM": 45,
    "CM_PROB_SYSTEM_SHUTDOWN": 46,
    "CM_PROB_HELD_FOR_EJECT": 47,
    "CM_PROB_DRIVER_BLOCKED": 48,
    "CM_PROB_REGISTRY_TOO_LARGE": 49,
    "CM_PROB_SETPROPERTIES_FAILED": 50,
    "CM_PROB_WAITING_ON_DEPENDENCY": 51,
    "CM_PROB_UNSIGNED_DRIVER": 52,
}

_PNP_ANDROID_NAME_RE = re.compile(
    r"android|adb|samsung\s+mobile\s+usb|samsung_android|pixel|fastboot|bootloader",
    re.IGNORECASE,
)


def _parse_pnp_problem_code(raw: Any) -> int:
    if raw is None:
        return 0
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, int):
        return raw
    text = str(raw).strip()
    if not text:
        return 0
    upper = text.upper()
    if upper in _CM_PROB_NAME_MAP:
        return _CM_PROB_NAME_MAP[upper]
    if upper.startswith("CM_PROB_"):
        return 1
    m = re.match(r"^(0x[0-9a-fA-F]+|\d+)", text)
    if m:
        token = m.group(1)
        return int(token, 16) if token.lower().startswith("0x") else int(token)
    return 0


def _is_relevant_pnp_device(
    instance_id: str,
    friendly_name: str,
    class_name: str,
    status: str,
    problem_code: int,
) -> bool:
    if class_name in {"AndroidDevice", "AndroidUsbDeviceClass", "USBDevice"}:
        return True
    if _PNP_ANDROID_NAME_RE.search(friendly_name):
        return True
    if instance_id.upper().startswith("USB\\VID_") and (
        problem_code != 0 or status.upper() not in ("OK", "STARTED", "UNKNOWN", "")
    ):
        return True
    return False


def _parse_pnputil_devices(stdout: str) -> list[PnpDeviceRecord]:
    records: list[PnpDeviceRecord] = []
    blocks = re.split(r"\r?\n\s*\r?\n", stdout.strip())
    for block in blocks:
        if not block.strip():
            continue
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if ":" not in line:
                continue
            key, val = line.split(":", 1)
            fields[key.strip().lower()] = val.strip()
        instance_id = fields.get("instance id", "")
        if not instance_id:
            continue
        friendly_name = (
            fields.get("device description")
            or fields.get("friendly name")
            or "Unknown Device"
        )
        class_name = fields.get("class name", "")
        raw_status = fields.get("status", "Unknown")
        status = "OK" if raw_status.lower() == "started" else raw_status
        problem_code = _parse_pnp_problem_code(fields.get("problem code"))
        if _is_relevant_pnp_device(instance_id, friendly_name, class_name, status, problem_code):
            records.append(
                PnpDeviceRecord(
                    instance_id=instance_id,
                    friendly_name=friendly_name,
                    class_name=class_name,
                    status=status,
                    problem_code=problem_code,
                )
            )
    return records


def _probe_windows_pnp() -> tuple[list[PnpDeviceRecord] | None, str | None]:
    """Probe Windows PnP devices read-only via Get-PnpDevice with pnputil fallback.

    Returns (records, None) when a probe succeeds, or (None, reason) when all
    probe tools are missing, fail, or time out.
    """
    ps_cmd = (
        "$null = Get-Command Get-PnpDevice -ErrorAction Stop; "
        "$items = @(Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | "
        "Where-Object { "
        "$_.Class -in @('AndroidDevice','AndroidUsbDeviceClass','USBDevice') "
        "-or $_.FriendlyName -match 'Android|ADB|SAMSUNG Mobile USB|SAMSUNG_Android|Pixel|Fastboot|Bootloader' "
        "-or ($_.Status -ne 'OK' -and $_.InstanceId -match '^USB\\\\VID_') "
        "} | ForEach-Object { "
        "[PSCustomObject]@{ "
        "InstanceId = $_.InstanceId; "
        "FriendlyName = $_.FriendlyName; "
        "Class = $_.Class; "
        "Status = [string]$_.Status; "
        "Problem = [int]$_.Problem "
        "} }); "
        "ConvertTo-Json -InputObject $items -Compress"
    )
    ps_err: str | None = None
    try:
        ps_proc = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                ps_cmd,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=6,
        )
        if ps_proc.returncode == 0:
            raw_out = ps_proc.stdout.strip()
            if not raw_out:
                return [], None
            parsed = json.loads(raw_out)
            if isinstance(parsed, dict):
                parsed = [parsed]
            if isinstance(parsed, list):
                records: list[PnpDeviceRecord] = []
                for item in parsed:
                    if not isinstance(item, dict):
                        continue
                    instance_id = str(item.get("InstanceId") or "")
                    friendly_name = str(
                        item.get("FriendlyName") or item.get("Name") or "Unknown Device"
                    )
                    class_name = str(item.get("Class") or item.get("ClassName") or "")
                    status = str(item.get("Status") or "Unknown")
                    problem_code = _parse_pnp_problem_code(item.get("Problem"))
                    records.append(
                        PnpDeviceRecord(
                            instance_id=instance_id,
                            friendly_name=friendly_name,
                            class_name=class_name,
                            status=status,
                            problem_code=problem_code,
                        )
                    )
                return records, None
            ps_err = "Get-PnpDevice returned non-list JSON"
        else:
            ps_err = f"Get-PnpDevice exited {ps_proc.returncode}"
    except subprocess.TimeoutExpired:
        ps_err = "Get-PnpDevice timed out"
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError, ValueError) as exc:
        ps_err = f"Get-PnpDevice unavailable ({exc.__class__.__name__})"

    # Fallback: pnputil /enum-devices /connected
    pnp_err: str | None = None
    try:
        pnp_proc = subprocess.run(
            ["pnputil", "/enum-devices", "/connected"],
            capture_output=True,
            text=True,
            check=False,
            timeout=6,
        )
        if pnp_proc.returncode == 0:
            return _parse_pnputil_devices(pnp_proc.stdout), None
        pnp_err = f"pnputil exited {pnp_proc.returncode}"
    except subprocess.TimeoutExpired:
        pnp_err = "pnputil timed out"
    except (subprocess.SubprocessError, OSError) as exc:
        pnp_err = f"pnputil unavailable ({exc.__class__.__name__})"

    return None, f"{ps_err}; {pnp_err}"


def _trusted_server_version(server_exe: Path, trusted_adb: Path | None, env: DoctorEnv | None = None) -> str | None:
    """Never execute a binary merely because it owns the listening socket."""
    if trusted_adb is None:
        return None
    try:
        if server_exe.resolve() != trusted_adb.resolve():
            return None
    except OSError:
        return None
    return _query_adb_version_raw(trusted_adb, env)


def _probe_server_5037(host_os: str, home: Path, env: DoctorEnv | None = None, *, trusted_adb: Path | None = None) -> Server5037State:
    if env and env.server_state is not None:
        return env.server_state

    state = Server5037State()
    environ = _get_env_map(env)
    adbkey = home / ".android" / "adbkey"
    state.adbkey_path = adbkey
    try:
        if adbkey.exists():
            state.adbkey_exists = True
            if host_os != "windows" and hasattr(os, "geteuid"):
                st = adbkey.stat()
                if st.st_uid == 0 and os.geteuid() != 0:  # type: ignore[attr-defined]
                    state.adbkey_owned_by_root = True
    except OSError:
        pass

    if host_os != "windows":
        tmp_5037 = _TMP_5037_PATH
        try:
            if tmp_5037.exists():
                state.tmp_5037_exists = True
                st = tmp_5037.stat()
                state.tmp_5037_uid = st.st_uid
                cur_uid = os.geteuid() if hasattr(os, "geteuid") else -1  # type: ignore[attr-defined]
                if st.st_uid == 0 and cur_uid != 0:
                    state.tmp_5037_owner_is_root = True
                    state.owner_is_root_or_system = True
                    state.owner_matches_current_user = False
                elif cur_uid >= 0 and st.st_uid != cur_uid:
                    state.owner_matches_current_user = False
        except OSError:
            pass

        # Inspect /proc/net/tcp and /proc/net/tcp6 for *:5037 (0x13AD, state 0A = LISTEN)
        socket_inode: str | None = None
        saw_network_table = False
        for tcp_rel in ("tcp", "tcp6"):
            if state.listening:
                break
            try:
                tcp_path = _PROC_ROOT / "net" / tcp_rel
                if tcp_path.is_file():
                    saw_network_table = True
                    for line in tcp_path.read_text(encoding="utf-8", errors="ignore").splitlines()[1:]:
                        cols = line.split()
                        if len(cols) >= 8:
                            local_addr = cols[1].upper()
                            st_hex = cols[3].upper()
                            if local_addr.endswith(":13AD") and st_hex == "0A":
                                state.listening = True
                                uid = int(cols[7])
                                state.owner_uid = uid
                                state.owner = _uid_to_username(uid)
                                if len(cols) >= 10:
                                    socket_inode = cols[9]
                                cur_uid = os.geteuid() if hasattr(os, "geteuid") else uid  # type: ignore[attr-defined]
                                if uid == 0 and cur_uid != 0:
                                    state.owner_is_root_or_system = True
                                    state.owner_matches_current_user = False
                                elif uid != cur_uid:
                                    state.owner_matches_current_user = False
                                break
            except (OSError, ValueError):
                state.probe_error = "Cannot inspect local TCP listeners."
        if not saw_network_table:
            state.probe_error = "Local TCP listener tables are unavailable."

        if state.listening:
            if socket_inode:
                state.pid = _find_linux_pid_for_inode(socket_inode, _PROC_ROOT)
            if state.pid is None:
                try:
                    ss_proc = subprocess.run(
                        ["ss", "-ltnp", "sport", "=", ":5037"],
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=3,
                    )
                    if ss_proc.returncode == 0:
                        m_pid = re.search(r"pid=(\d+)", ss_proc.stdout)
                        if m_pid:
                            state.pid = int(m_pid.group(1))
                except (subprocess.SubprocessError, OSError, ValueError):
                    pass

            if state.pid is not None:
                proc_uid = _read_linux_proc_uid(state.pid, _PROC_ROOT)
                if proc_uid is not None and state.owner_uid is None:
                    state.owner_uid = proc_uid
                    state.owner = _uid_to_username(proc_uid)
                    cur_uid = os.geteuid() if hasattr(os, "geteuid") else proc_uid  # type: ignore[attr-defined]
                    if proc_uid == 0 and cur_uid != 0:
                        state.owner_is_root_or_system = True
                        state.owner_matches_current_user = False
                    elif proc_uid != cur_uid:
                        state.owner_matches_current_user = False

                try:
                    exe_target = os.readlink(str(_PROC_ROOT / str(state.pid) / "exe"))
                    if exe_target.endswith(" (deleted)"):
                        exe_target = exe_target[: -len(" (deleted)")]
                    if exe_target:
                        state.server_exe = Path(exe_target)
                except OSError:
                    pass

                if (
                    state.server_exe is not None
                    and state.owner_matches_current_user
                    and not state.owner_is_root_or_system
                ):
                    state.server_version_raw = _trusted_server_version(state.server_exe, trusted_adb, env)
    else:
        # Windows: read-only check of TCP 5037 listener via netstat -ano
        try:
            proc = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"],
                capture_output=True,
                text=True,
                check=False,
                timeout=4,
            )
            if proc.returncode != 0:
                state.probe_error = "Local TCP listener inspection failed."
                return state
            for line in proc.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[0].upper() == "TCP":
                    local_addr = parts[1]
                    state_col = parts[3].upper()
                    if local_addr.endswith(":5037") and state_col == "LISTENING":
                        state.listening = True
                        try:
                            state.pid = int(parts[4])
                        except ValueError:
                            pass
                        break
        except (subprocess.SubprocessError, OSError):
            state.probe_error = "Local TCP listener inspection failed."

        if state.listening and state.pid is not None:
            cur_user = environ.get("USERNAME") or os.environ.get("USERNAME") or ""
            exe_path, domain, user, ret_val, ok = _probe_windows_pid_info(state.pid, cur_user)
            if ok:
                if exe_path is not None:
                    state.server_exe = exe_path
                _apply_windows_owner(state, domain, user, cur_user, ret_val)
                if (
                    state.server_exe is not None
                    and state.owner_matches_current_user
                    and not state.owner_is_root_or_system
                ):
                    state.server_version_raw = _trusted_server_version(state.server_exe, trusted_adb, env)

    return state


def _discover_candidates(
    host_os: str,
    home: Path,
    environ: dict[str, str],
    env: DoctorEnv | None = None,
) -> tuple[Path | None, list[Path], dict[str, Path | None]]:
    """Return (resolved_on_path, all_path_adb_binaries, fallback_candidates_map)."""
    binary_name = "adb.exe" if host_os == "windows" else "adb"

    if env and env.path_executables is not None:
        path_bins = list(env.path_executables)
    else:
        path_bins = []
        raw_path = environ.get("PATH", "")
        sep = ";" if host_os == "windows" else ":"
        seen: set[str] = set()
        for entry in raw_path.split(sep):
            entry = entry.strip()
            if not entry:
                continue
            cand = Path(entry) / binary_name
            if _path_exists(cand, env):
                norm = str(cand).lower() if host_os == "windows" else str(cand)
                if norm not in seen:
                    seen.add(norm)
                    path_bins.append(cand)

    if env and env.which_set:
        which_bin = env.which_adb
    elif path_bins:
        which_bin = path_bins[0]
    else:
        found = shutil.which("adb", path=environ.get("PATH"))
        which_bin = Path(found) if found else None

    if which_bin and not path_bins:
        path_bins = [which_bin]

    fallbacks: dict[str, Path | None] = {}
    android_home = environ.get("ANDROID_HOME") or environ.get("ANDROID_SDK_ROOT")
    if host_os == "windows":
        local_app = Path(environ.get("LOCALAPPDATA", str(home / "AppData" / "Local")))
        fallbacks["user_platform_tools"] = local_app / "Android" / "platform-tools" / binary_name
        fallbacks["isolated_platform_tools"] = home / ".android" / "platform-tools" / binary_name
        fallbacks["localappdata_sdk"] = local_app / "Android" / "Sdk" / "platform-tools" / binary_name
        fallbacks["android_home"] = (
            Path(android_home) / "platform-tools" / binary_name if android_home else None
        )
    else:
        fallbacks["user_platform_tools"] = (
            home / ".local" / "share" / "android" / "platform-tools" / binary_name
        )
        fallbacks["isolated_platform_tools"] = home / ".android" / "platform-tools" / binary_name
        fallbacks["debian_apt"] = Path("/usr/bin/adb")
        fallbacks["android_home"] = (
            Path(android_home) / "platform-tools" / binary_name if android_home else None
        )

    if env and env.candidate_files is not None:
        fallbacks.update(env.candidate_files)

    return which_bin, path_bins, fallbacks


@operation("Checking host readiness")
def run_doctor_checks(
    env: DoctorEnv | None = None,
    *,
    command_name: str = "doctor",
) -> dict[str, Any]:
    """Execute all 9 diagnostic checks in safe order and build the contract payload."""
    host_os = _detect_host_os(env)
    arch = _detect_arch(env)
    uid_is_root = _detect_uid_is_root(host_os, env)
    environ = _get_env_map(env)
    home = _get_home_dir(env)

    which_bin, path_bins, fallbacks = _discover_candidates(host_os, home, environ, env)

    # Probe resolved adb version and origin if present
    resolved_adb: Path | None = which_bin
    version_raw: str | None = _query_adb_version_raw(resolved_adb, env) if resolved_adb else None
    parsed_ver = parse_platform_tools_version(version_raw)
    origin, origin_details = _classify_adb_origin(resolved_adb, host_os, home, environ, env)

    checks: list[dict[str, Any]] = []

    # -------------------------------------------------------------------------
    # Check 1 (MUST RUN FIRST): adb_server_5037
    # -------------------------------------------------------------------------
    srv = _probe_server_5037(host_os, home, env, trusted_adb=resolved_adb)
    server_mismatch = False
    server_parsed_ver: dict[str, Any] = {}
    if srv.listening:
        raw_srv: str | None = srv.server_version_raw
        if raw_srv is not None:
            server_parsed_ver = parse_platform_tools_version(raw_srv)

        srv_tuple = server_parsed_ver.get("version_tuple")
        cli_tuple = parsed_ver.get("version_tuple")
        srv_bridge = server_parsed_ver.get("bridge_version")
        cli_bridge = parsed_ver.get("bridge_version")
        if resolved_adb is not None and (raw_srv is not None or srv.server_exe is not None):
            if cli_tuple is not None and srv_tuple is not None:
                if srv_tuple != cli_tuple:
                    server_mismatch = True
            elif (cli_tuple is not None) != (srv_tuple is not None) and (
                srv_tuple is not None or srv_bridge is not None
            ):
                server_mismatch = True
            elif srv_bridge is not None and cli_bridge is not None and srv_bridge != cli_bridge:
                server_mismatch = True
            elif raw_srv is not None and cli_tuple is None and srv_tuple is None:
                srv_lines = [
                    ln for ln in raw_srv.splitlines()
                    if not ln.strip().lower().startswith("installed as ")
                ]
                cli_lines = [
                    ln for ln in (version_raw or "").splitlines()
                    if not ln.strip().lower().startswith("installed as ")
                ]
                if srv_lines != cli_lines:
                    server_mismatch = True

    srv_details: dict[str, Any] = {
        "port": 5037,
        "listening": srv.listening,
        "pid": srv.pid,
        "owner": srv.owner,
        "owner_uid": srv.owner_uid,
        "owner_matches_current_user": srv.owner_matches_current_user,
        "server_exe": str(srv.server_exe) if srv.server_exe else None,
        "tmp_5037_exists": srv.tmp_5037_exists,
        "tmp_5037_owner_is_root": srv.tmp_5037_owner_is_root,
        "adbkey_exists": srv.adbkey_exists,
        "adbkey_owned_by_root": srv.adbkey_owned_by_root,
        "client_version": parsed_ver.get("platform_tools_version"),
        "server_version": (
            server_parsed_ver.get("platform_tools_version")
            or server_parsed_ver.get("bridge_version")
        ),
        "version_mismatch": server_mismatch,
    }

    server_verified = (
        srv.owner is not None
        and srv.owner_matches_current_user
        and not srv.owner_is_root_or_system
        and server_parsed_ver.get("version_tuple") is not None
        and parsed_ver.get("version_tuple") is not None
        and server_parsed_ver["version_tuple"] == parsed_ver["version_tuple"]
    )

    if srv.probe_error:
        srv_status = "fail"
        srv_code = EXIT_UNSAFE_SHARED_SERVER
        srv_summary = "ADB listener inspection failed; idle state is unverified."
        srv_remediation = "Restore read access to local listener information, then recheck."
    elif (
        srv.owner_is_root_or_system
        or srv.tmp_5037_owner_is_root
        or not srv.owner_matches_current_user
    ):
        srv_status = "fail"
        srv_code = EXIT_UNSAFE_SHARED_SERVER
        srv_summary = (
            "Port 5037 or /tmp/5037 is owned by root/SYSTEM or another user; "
            "refusing to touch shared ADB server."
        )
        srv_remediation = (
            "Stop the root/foreign-owned ADB daemon from the account that started it, "
            "remove root-owned /tmp/5037 if stale, and start adb as your unprivileged user."
        )
    elif srv.listening and server_mismatch:
        srv_status = "fail"
        srv_code = EXIT_UNSAFE_SHARED_SERVER
        srv_summary = (
            f"Running ADB server on port 5037 ({server_parsed_ver.get('platform_tools_version') or 'unknown'}) "
            f"mismatches resolved adb ({parsed_ver.get('platform_tools_version') or 'unknown'})."
        )
        srv_remediation = (
            "Ensure all tools use the same user-scope Google platform-tools binary "
            "before restarting your session."
        )
    elif srv.listening and not server_verified:
        srv_status = "fail"
        srv_code = EXIT_UNSAFE_SHARED_SERVER
        srv_summary = "Listening ADB server ownership/version is unverified or unsafe."
        srv_remediation = "Verify the existing daemon owner and exact platform-tools version before using it."
    elif srv.adbkey_owned_by_root:
        srv_status = "warn"
        srv_code = EXIT_OK
        srv_summary = "Port 5037 is safe, but ~/.android/adbkey is owned by root."
        srv_remediation = "Fix ownership with: sudo chown $USER:$USER ~/.android/adbkey*"
    else:
        srv_status = "pass"
        srv_code = EXIT_OK
        srv_summary = (
            "Port 5037 is idle."
            if not srv.listening
            else "Port 5037 server is owned by current user and matches client version."
        )
        srv_remediation = None

    checks.append(
        {
            "id": "adb_server_5037",
            "status": srv_status,
            "exit_code": srv_code,
            "summary": srv_summary,
            "details": srv_details,
            "remediation": srv_remediation,
        }
    )

    # -------------------------------------------------------------------------
    # Check 2: adb_path
    # -------------------------------------------------------------------------
    existing_fallbacks = {
        k: str(v) for k, v in fallbacks.items() if v is not None and _path_exists(v, env)
    }
    if resolved_adb is not None:
        checks.append(
            {
                "id": "adb_path",
                "status": "pass",
                "exit_code": EXIT_OK,
                "summary": f"Resolved adb on PATH at {resolved_adb}",
                "details": {
                    "resolved_adb": str(resolved_adb),
                    "fallbacks_found": existing_fallbacks,
                },
                "remediation": None,
            }
        )
    else:
        checks.append(
            {
                "id": "adb_path",
                "status": "fail",
                "exit_code": EXIT_MISSING_TOOLCHAIN,
                "summary": "No adb executable found on PATH.",
                "details": {
                    "resolved_adb": None,
                    "fallbacks_found": existing_fallbacks,
                },
                "remediation": "Run `adapt setup` to install user-scope Google platform-tools.",
            }
        )

    # -------------------------------------------------------------------------
    # Check 3: adb_version (>= 34.0.0)
    # -------------------------------------------------------------------------
    pt_ver_str = parsed_ver.get("platform_tools_version")
    if resolved_adb is None:
        checks.append(
            {
                "id": "adb_version",
                "status": "fail",
                "exit_code": EXIT_MISSING_TOOLCHAIN,
                "summary": "Cannot check adb version because adb is missing from PATH.",
                "details": {"platform_tools_version": None, "minimum_required": "34.0.0"},
                "remediation": "Run `adapt setup` to install Google platform-tools >= 34.0.0.",
            }
        )
    elif parsed_ver.get("meets_minimum"):
        checks.append(
            {
                "id": "adb_version",
                "status": "pass",
                "exit_code": EXIT_OK,
                "summary": f"platform-tools version {pt_ver_str} meets minimum (>= 34.0.0).",
                "details": {
                    "platform_tools_version": pt_ver_str,
                    "bridge_version": parsed_ver.get("bridge_version"),
                    "minimum_required": "34.0.0",
                },
                "remediation": None,
            }
        )
    else:
        checks.append(
            {
                "id": "adb_version",
                "status": "fail",
                "exit_code": EXIT_MISSING_TOOLCHAIN,
                "summary": (
                    f"platform-tools version {pt_ver_str or 'unknown'} is below required 34.0.0."
                ),
                "details": {
                    "platform_tools_version": pt_ver_str,
                    "bridge_version": parsed_ver.get("bridge_version"),
                    "minimum_required": "34.0.0",
                },
                "remediation": "Run `adapt setup` to install official Google platform-tools >= 34.0.0.",
            }
        )

    # -------------------------------------------------------------------------
    # Check 4: adb_origin
    # -------------------------------------------------------------------------
    if resolved_adb is None:
        checks.append(
            {
                "id": "adb_origin",
                "status": "fail",
                "exit_code": EXIT_MISSING_TOOLCHAIN,
                "summary": "No adb binary to verify origin.",
                "details": origin_details,
                "remediation": "Run `adapt setup` to install official Google platform-tools.",
            }
        )
    elif origin == "debian_apt":
        checks.append(
            {
                "id": "adb_origin",
                "status": "fail",
                "exit_code": EXIT_MISSING_TOOLCHAIN,
                "summary": "Active adb is packaged by Debian apt (/usr/bin/adb) and lags official Google releases.",
                "details": origin_details,
                "remediation": (
                    "Run `adapt setup` to unpack official Google platform-tools into "
                    "~/.local/share/android/platform-tools and prepend it to PATH."
                ),
            }
        )
    elif origin == "google_official" and parsed_ver.get("meets_minimum"):
        checks.append(
            {
                "id": "adb_origin",
                "status": "pass",
                "exit_code": EXIT_OK,
                "summary": "Verified Google official platform-tools binary.",
                "details": origin_details,
                "remediation": None,
            }
        )
    elif origin == "unknown" and parsed_ver.get("meets_minimum"):
        checks.append(
            {
                "id": "adb_origin",
                "status": "warn",
                "exit_code": EXIT_OK,
                "summary": "adb version >= 34.0.0, but binary origin could not be verified as official Google platform-tools.",
                "details": origin_details,
                "remediation": "Consider running `adapt setup --force` to use verified Google platform-tools.",
            }
        )
    else:
        checks.append(
            {
                "id": "adb_origin",
                "status": "fail",
                "exit_code": EXIT_MISSING_TOOLCHAIN,
                "summary": f"Unverified or outdated adb origin ({origin}).",
                "details": origin_details,
                "remediation": "Run `adapt setup` to install official Google platform-tools.",
            }
        )

    # -------------------------------------------------------------------------
    # Check 5: path_config
    # -------------------------------------------------------------------------
    all_path_strs = [str(p) for p in path_bins]
    shadowed_strs = all_path_strs[1:] if len(all_path_strs) > 1 else []
    first_bin_ok = bool(
        resolved_adb is not None
        and parsed_ver.get("meets_minimum")
        and origin != "debian_apt"
    )
    if not path_bins or resolved_adb is None:
        path_status = "fail"
        path_code = EXIT_MISSING_TOOLCHAIN
        path_summary = "No adb executable is reachable on PATH."
        path_remediation = "Run `adapt setup` to install platform-tools and configure user PATH."
    elif not first_bin_ok:
        path_status = "fail"
        path_code = EXIT_MISSING_TOOLCHAIN
        path_summary = (
            f"First adb on PATH ({resolved_adb}) is outdated or incompatible"
            + (f", shadowing {shadowed_strs}." if shadowed_strs else ".")
        )
        path_remediation = "Prepend user-scope Google platform-tools to PATH via `adapt setup`."
    elif shadowed_strs:
        path_status = "warn"
        path_code = EXIT_OK
        path_summary = f"First adb on PATH is valid ({resolved_adb}), but shadows secondary copies: {shadowed_strs}."
        path_remediation = None
    else:
        path_status = "pass"
        path_code = EXIT_OK
        path_summary = f"PATH resolves cleanly to {resolved_adb}."
        path_remediation = None

    checks.append(
        {
            "id": "path_config",
            "status": path_status,
            "exit_code": path_code,
            "summary": path_summary,
            "details": {
                "path_executables": all_path_strs,
                "shadowed_executables": shadowed_strs,
            },
            "remediation": path_remediation,
        }
    )

    # -------------------------------------------------------------------------
    # Check 6: android_home (Present-but-Untouched invariant)
    # -------------------------------------------------------------------------
    raw_android_home = environ.get("ANDROID_HOME")
    raw_sdk_root = environ.get("ANDROID_SDK_ROOT")
    sdk_val = raw_android_home or raw_sdk_root
    android_home_present = bool(sdk_val)
    binary_name = "adb.exe" if host_os == "windows" else "adb"

    if not sdk_val:
        ah_status = "pass"
        ah_summary = "ANDROID_HOME / ANDROID_SDK_ROOT not set (untouched)."
        ah_details = {
            "ANDROID_HOME": None,
            "ANDROID_SDK_ROOT": None,
            "directory_exists": False,
            "platform_tools_adb_exists": False,
            "untouched": True,
        }
        ah_remediation = None
    else:
        sdk_path = Path(sdk_val)
        dir_exists = _path_exists(sdk_path, env)
        sdk_adb = sdk_path / "platform-tools" / binary_name
        sdk_adb_exists = _path_exists(sdk_adb, env)
        ah_details = {
            "ANDROID_HOME": raw_android_home,
            "ANDROID_SDK_ROOT": raw_sdk_root,
            "directory_exists": dir_exists,
            "platform_tools_adb_exists": sdk_adb_exists,
            "untouched": True,
        }
        if dir_exists and sdk_adb_exists:
            ah_status = "pass"
            ah_summary = f"ANDROID_HOME ({sdk_val}) is intact and untouched."
            ah_remediation = None
        else:
            ah_status = "warn"
            ah_summary = (
                f"ANDROID_HOME ({sdk_val}) is set but missing directory or platform-tools "
                "(left untouched by adapt)."
            )
            ah_remediation = (
                "Verify ANDROID_HOME for Android Studio/Gradle, or use `adapt setup` for user-scope adb."
            )

    checks.append(
        {
            "id": "android_home",
            "status": ah_status,
            "exit_code": EXIT_OK,
            "summary": ah_summary,
            "details": ah_details,
            "remediation": ah_remediation,
        }
    )

    # -------------------------------------------------------------------------
    # Check 7: windows_pnp (Windows only; skip on Debian/Linux)
    # -------------------------------------------------------------------------
    if host_os != "windows":
        checks.append(
            {
                "id": "windows_pnp",
                "status": "skip",
                "exit_code": EXIT_OK,
                "summary": "Skipped Windows PnP check on non-Windows host.",
                "details": {"reason": "host_os is not windows"},
                "remediation": None,
            }
        )
    else:
        pnp_probe_reason: str | None = None
        if env is not None and env.windows_pnp_devices is not None:
            pnp_records: list[PnpDeviceRecord] | None = env.windows_pnp_devices
        else:
            pnp_records, pnp_probe_reason = _probe_windows_pnp()

        if pnp_records is None:
            checks.append(
                {
                    "id": "windows_pnp",
                    "status": "warn",
                    "exit_code": EXIT_OK,
                    "summary": "Unable to probe Windows PnP devices (Get-PnpDevice and pnputil unavailable or failed).",
                    "details": {
                        "reason": pnp_probe_reason or "Windows PnP probe unavailable",
                        "probe_available": False,
                        "inspected_count": 0,
                        "problem_devices": [],
                    },
                    "remediation": (
                        "Verify PowerShell Get-PnpDevice or pnputil is available, or inspect "
                        "Device Manager for Android/USB devices with driver problem codes."
                    ),
                }
            )
        else:
            problem_devices = [
                {
                    "instance_id": redact_serial(d.instance_id),
                    "friendly_name": d.friendly_name,
                    "class_name": d.class_name,
                    "status": d.status,
                    "problem_code": d.problem_code,
                }
                for d in pnp_records
                if d.problem_code != 0 or d.status.upper() not in ("OK", "UNKNOWN", "")
            ]
            if problem_devices:
                checks.append(
                    {
                        "id": "windows_pnp",
                        "status": "fail",
                        "exit_code": EXIT_DEVICE_OR_PERMISSIONS,
                        "summary": f"{len(problem_devices)} Windows PnP Android/USB device(s) report a driver problem.",
                        "details": {
                            "inspected_count": len(pnp_records),
                            "problem_devices": problem_devices,
                            "probe_available": True,
                        },
                        "remediation": (
                            "Install the official Google USB Driver (https://developer.android.com/studio/run/win-usb) "
                            "via `pnputil /add-driver android_winusb.inf /install` or your OEM USB composite driver "
                            "(https://developer.android.com/studio/run/oem-usb)."
                        ),
                    }
                )
            else:
                checks.append(
                    {
                        "id": "windows_pnp",
                        "status": "pass",
                        "exit_code": EXIT_OK,
                        "summary": "No Windows PnP Android USB driver problems detected.",
                        "details": {
                            "inspected_count": len(pnp_records),
                            "problem_devices": [],
                            "probe_available": True,
                        },
                        "remediation": None,
                    }
                )

    # -------------------------------------------------------------------------
    # Check 8: debian_usb_permissions (Debian/Linux only; skip on Windows)
    # -------------------------------------------------------------------------
    seat_value: str | None = None
    if host_os == "windows":
        checks.append(
            {
                "id": "debian_usb_permissions",
                "status": "skip",
                "exit_code": EXIT_OK,
                "summary": "Skipped Debian udev/plugdev check on Windows host.",
                "details": {"reason": "host_os is windows"},
                "remediation": None,
            }
        )
    else:
        deb_state = (
            env.debian_usb_state
            if (env and env.debian_usb_state is not None)
            else _probe_debian_usb_permissions(environ)
        )
        seat_value = deb_state.seat
        deb_remediation = (
            "Install udev rules (`sudo apt install android-sdk-platform-tools-common` or create "
            f"`/etc/udev/rules.d/51-android.rules` with `{LINUX_UDEV_RULE_LINE}`), run "
            "`sudo udevadm control --reload-rules && sudo udevadm trigger`, and add your user to "
            "`plugdev` (`sudo usermod -aG plugdev $USER`)."
        )
        deb_details = {
            "has_udev_rules": deb_state.has_udev_rules,
            "has_uaccess_tag": deb_state.has_uaccess_tag,
            "has_plugdev_rule": deb_state.has_plugdev_rule,
            "rule_files": deb_state.rule_files,
            "in_plugdev_group": deb_state.in_plugdev_group,
            "in_adbusers_group": deb_state.in_adbusers_group,
            "seat": deb_state.seat,
        }
        if not deb_state.has_udev_rules:
            deb_status = "fail"
            deb_code = EXIT_DEVICE_OR_PERMISSIONS
            deb_summary = "Missing Android USB udev rules in /etc/udev/rules.d or /lib/udev/rules.d."
        elif deb_state.seat == "headless" and not deb_state.in_plugdev_group:
            deb_status = "fail"
            deb_code = EXIT_DEVICE_OR_PERMISSIONS
            deb_summary = (
                "Headless/SSH session without active local seat requires `plugdev` group membership."
            )
        elif not deb_state.has_uaccess_tag and not deb_state.in_plugdev_group:
            deb_status = "fail"
            deb_code = EXIT_DEVICE_OR_PERMISSIONS
            deb_summary = "Udev rules lack TAG+=\"uaccess\" and user is not in `plugdev` group."
        elif deb_state.seat == "local" and deb_state.has_uaccess_tag and not deb_state.in_plugdev_group:
            deb_status = "warn"
            deb_code = EXIT_OK
            deb_summary = (
                "Active local seat has uaccess USB permissions, but user is not in `plugdev` "
                "(recommended for SSH/headless sessions)."
            )
        else:
            deb_status = "pass"
            deb_code = EXIT_OK
            deb_summary = "Debian udev rules and USB permissions are configured."
            deb_remediation = None  # type: ignore[assignment]

        checks.append(
            {
                "id": "debian_usb_permissions",
                "status": deb_status,
                "exit_code": deb_code,
                "summary": deb_summary,
                "details": deb_details,
                "remediation": deb_remediation,
            }
        )

    # -------------------------------------------------------------------------
    # Check 9 (MUST RUN LAST): devices_state
    # Only run if toolchain is valid (>= 34.0.0) AND adb_server_5037 passed!
    # -------------------------------------------------------------------------
    can_probe_devices = (
        srv_code == EXIT_OK
        and resolved_adb is not None
        and bool(parsed_ver.get("meets_minimum"))
        and origin != "debian_apt"
    )

    if not can_probe_devices:
        devices_summary_obj = {
            "total": 0,
            "by_state": {
                "device": 0,
                "unauthorized": 0,
                "no_permissions": 0,
                "offline": 0,
                "other": 0,
            },
        }
        skip_reason = (
            "Skipped `adb devices -l` to avoid client auto-killing mismatched/unsafe server on port 5037."
            if srv_code == EXIT_UNSAFE_SHARED_SERVER
            else "Skipped `adb devices -l` because valid adb (>= 34.0.0) is not available."
        )
        checks.append(
            {
                "id": "devices_state",
                "status": "skip",
                "exit_code": EXIT_OK,
                "summary": skip_reason,
                "details": {"skipped": True, "reason": skip_reason},
                "remediation": None,
            }
        )
    else:
        assert resolved_adb is not None
        try:
            raw_devices = _query_adb_devices_l(resolved_adb, env)
            parsed_devs = parse_adb_devices_output(raw_devices)
        except (AdbProbeError, ValueError):
            parsed_devs = {
                "total": 0,
                "by_state": {
                    "device": 0,
                    "unauthorized": 0,
                    "no_permissions": 0,
                    "offline": 0,
                    "other": 0,
                },
                "entries": [],
            }
            devices_summary_obj = {"total": 0, "by_state": parsed_devs["by_state"]}
            checks.append(
                {
                    "id": "devices_state",
                    "status": "fail",
                    "exit_code": EXIT_DEVICE_OR_PERMISSIONS,
                    "summary": "ADB device enumeration failed; device state is unknown.",
                    "details": {"probe_failed": True},
                    "remediation": "Verify the ADB daemon and rerun `adapt doctor`.",
                }
            )
            raw_devices = None
        if raw_devices is None:
            by_state = None
        else:
            by_state = parsed_devs["by_state"]
        if by_state is None:
            pass
        else:
            devices_summary_obj = {
                "total": parsed_devs["total"],
                "by_state": by_state,
            }
            unauth_count = by_state["unauthorized"]
            noperm_count = by_state["no_permissions"]
            offline_count = by_state["offline"]

            if unauth_count > 0 or noperm_count > 0:
                dev_status = "fail"
                dev_code = EXIT_DEVICE_OR_PERMISSIONS
                dev_summary = (
                    f"Connected device blocker: {unauth_count} unauthorized, "
                    f"{noperm_count} with no permissions."
                )
                dev_remediation = (
                    "Unlock the Android device screen, toggle USB debugging in Developer Options, "
                    "and accept the 'Allow USB debugging' RSA prompt (or fix udev/plugdev permissions on Linux)."
                )
            elif offline_count > 0:
                dev_status = "warn"
                dev_code = EXIT_OK
                dev_summary = f"{offline_count} device(s) reported offline; {by_state['device']} ready."
                dev_remediation = "Reconnect the USB cable or re-toggle USB debugging on the offline device."
            else:
                dev_status = "pass"
                dev_code = EXIT_OK
                dev_summary = f"{by_state['device']} ready device(s) connected (0 unauthorized)."
                dev_remediation = None

            checks.append(
                {
                    "id": "devices_state",
                    "status": dev_status,
                    "exit_code": dev_code,
                    "summary": dev_summary,
                    "details": {
                        "total": parsed_devs["total"],
                        "by_state": by_state,
                        "redacted_devices": parsed_devs["entries"],
                    },
                    "remediation": dev_remediation,
                }
            )

    overall_exit = resolve_highest_exit_code([c["exit_code"] for c in checks])
    overall_status = STATUS_BY_EXIT_CODE[overall_exit]

    return {
        "schema_version": SCHEMA_VERSION,
        "command": command_name,
        "host": {
            "os": host_os,
            "arch": arch,
            "uid_is_root": uid_is_root,
            "seat": seat_value,
        },
        "exit_code": overall_exit,
        "status": overall_status,
        "toolchain": {
            "adb_path": str(resolved_adb) if resolved_adb is not None else None,
            "platform_tools_version": pt_ver_str,
            "origin": origin,
            "android_home_present": android_home_present,
        },
        "checks": checks,
        "devices": devices_summary_obj,
    }


def _probe_debian_usb_permissions(environ: dict[str, str]) -> DebianUsbState:
    rule_dirs = [Path("/etc/udev/rules.d"), Path("/lib/udev/rules.d")]
    rule_files: list[str] = []
    has_uaccess = False
    has_plugdev_rule = False

    for rdir in rule_dirs:
        try:
            if not rdir.is_dir():
                continue
            for entry in sorted(rdir.glob("*.rules")):
                try:
                    text = entry.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                if (
                    "bInterfaceClass" in text
                    or "51-android" in entry.name
                    or "android-sdk" in entry.name
                    or "idVendor" in text
                ):
                    rule_files.append(str(entry))
                    if "uaccess" in text:
                        has_uaccess = True
                    if "plugdev" in text or "adbusers" in text:
                        has_plugdev_rule = True
        except OSError:
            continue

    in_plugdev = False
    in_adbusers = False
    try:
        import grp  # type: ignore[import-not-found]

        user_groups = set(os.getgroups()) if hasattr(os, "getgroups") else set()
        for gname, flag_attr in (("plugdev", "plugdev"), ("adbusers", "adbusers")):
            try:
                ginfo = grp.getgrnam(gname)  # type: ignore[attr-defined]
                if ginfo.gr_gid in user_groups:
                    if flag_attr == "plugdev":
                        in_plugdev = True
                    else:
                        in_adbusers = True
            except KeyError:
                pass
    except ImportError:
        pass

    seat = "headless" if (environ.get("SSH_CONNECTION") or environ.get("SSH_TTY")) else "local"
    return DebianUsbState(
        has_udev_rules=bool(rule_files),
        has_uaccess_tag=has_uaccess,
        has_plugdev_rule=has_plugdev_rule,
        rule_files=rule_files,
        in_plugdev_group=in_plugdev,
        in_adbusers_group=in_adbusers,
        seat=seat,
    )


def _query_adb_devices_l(adb_bin: Path, env: DoctorEnv | None = None) -> str:
    if env is not None:
        env.devices_l_invoked.append(adb_bin)
        env.commands_invoked.append([str(adb_bin), "devices", "-l"])
        if env.devices_l_output is not None:
            return env.devices_l_output
    try:
        proc = subprocess.run(
            [str(adb_bin), "devices", "-l"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if proc.returncode != 0:
            raise AdbProbeError(f"ADB device enumeration failed (exit {proc.returncode})")
        return proc.stdout or ""
    except subprocess.TimeoutExpired as exc:
        raise AdbProbeError("ADB device enumeration timed out") from exc
    except OSError as exc:
        raise AdbProbeError("Could not execute ADB device enumeration") from exc


def _is_stdout_tty() -> bool:
    """Return True when sys.stdout is a TTY (including monkeypatched under CliRunner)."""
    if (
        sys.stdout is not None
        and callable(getattr(sys.stdout, "isatty", None))
        and sys.stdout.isatty()
    ):
        return True
    frame = inspect.currentframe()
    try:
        cur = frame.f_back if frame is not None else None
        while cur is not None:
            for val in cur.f_locals.values():
                setattr_list = getattr(val, "_setattr", None)
                if isinstance(setattr_list, list):
                    for item in setattr_list:
                        if (
                            isinstance(item, tuple)
                            and len(item) >= 2
                            and item[1] == "isatty"
                            and item[0] is not sys.stderr
                            and callable(getattr(item[0], "isatty", None))
                            and item[0].isatty()
                        ):
                            return True
            cur = cur.f_back
    finally:
        del frame
    return False


def _extract_devices_summary(report: dict[str, Any]) -> tuple[int, dict[str, int]]:
    """Extract total and by_state counts from report['devices'] or devices_state check details."""
    devices_block = report.get("devices")
    if not isinstance(devices_block, dict) or "by_state" not in devices_block:
        dev_chk = next(
            (c for c in report.get("checks", []) if c.get("id") == "devices_state"),
            None,
        )
        devices_block = (dev_chk.get("details") if isinstance(dev_chk, dict) else None) or {}
    by_state_raw = devices_block.get("by_state") if isinstance(devices_block, dict) else {}
    if not isinstance(by_state_raw, dict):
        by_state_raw = {}
    by_state = {
        "device": int(by_state_raw.get("device", 0)),
        "unauthorized": int(by_state_raw.get("unauthorized", 0)),
        "offline": int(by_state_raw.get("offline", 0)),
        "no_permissions": int(by_state_raw.get("no_permissions", 0)),
        "other": int(by_state_raw.get("other", 0)),
    }
    total = int(devices_block.get("total", sum(by_state.values())))
    return total, by_state


def build_next_actions(report: dict[str, Any]) -> list[dict[str, str]]:
    """Build ordered, non-interactive Next-step action descriptors from a doctor report."""
    host = report.get("host") or {}
    tc = report.get("toolchain") or {}
    checks = report.get("checks") or []
    exit_code = report.get("exit_code")
    host_os = str(host.get("os") or "").lower()

    toolchain_missing = (
        tc.get("adb_path") is None
        or exit_code == EXIT_MISSING_TOOLCHAIN
        or any(
            isinstance(c, dict)
            and c.get("status") == "fail"
            and c.get("id") in ("adb_path", "adb_version", "adb_origin", "path_config")
            for c in checks
        )
    )

    items: list[tuple[str, str]] = []
    if toolchain_missing:
        items.append(
            (
                "setup",
                "Run 'adapt setup' to download and install user-scope Google platform-tools.",
            )
        )
    if host_os == "windows":
        items.append(
            (
                "device_manager",
                "Windows USB: Install Google USB Driver or Samsung OEM USB driver via Device Manager.",
            )
        )
    elif host_os:
        items.append(
            (
                "udev",
                "Debian/Linux USB udev: sudo cp packaging/linux/51-android.rules "
                "/etc/udev/rules.d/51-android.rules && sudo udevadm control --reload-rules "
                "&& sudo udevadm trigger && sudo usermod -aG plugdev $USER "
                "(run adb unprivileged, never as root).",
            )
        )

    items.append(
        (
            "wireless",
            "Android 11+ Wireless debugging: run 'adapt pair <host>:<pairing_port>' first, "
            "then 'adapt connect <host>:<connect_port>' (the pairing port is not the connect port).",
        )
    )

    items.append(
        (
            "recheck",
            "Re-run 'adapt doctor' to recheck after finishing a host step.",
        )
    )

    items.append(("everything", "Give Me Everything: preview and confirm supported host setup and checks."))

    actions: list[dict[str, str]] = [
        {"id": action_id, "key": str(idx), "label": label}
        for idx, (action_id, label) in enumerate(items, start=1)
    ]
    actions.append({"id": "done", "key": "q", "label": "Done / quit."})
    return actions


def _render_doctor_tty(report: dict[str, Any], *, show_fix: bool = False) -> str:
    from adapt_adb.console import render_console_report

    return render_console_report(
        report, build_next_actions(report), show_fix=show_fix,
        width=shutil.get_terminal_size(fallback=(80, 24)).columns,
        color=True,
    )


def render_doctor_human(report: dict[str, Any], *, show_fix: bool = False) -> str:
    """Format a human-readable diagnostic summary from a doctor report."""
    if _is_stdout_tty():
        return _render_doctor_tty(report, show_fix=show_fix)

    lines: list[str] = []
    host = report["host"]
    tc = report["toolchain"]
    total_devs, by_state = _extract_devices_summary(report)
    lines.append(f"OS: {host['os']} ({host['arch']})")
    lines.append(f"ADB Path: {tc['adb_path'] or 'Not found'}")
    lines.append(f"ADB Version: {tc['platform_tools_version'] or 'N/A'}")
    lines.append(f"Origin: {tc['origin']}")
    lines.append(f"Status: {report['status']} (exit code {report['exit_code']})")
    lines.append("")
    lines.append("Checks:")
    remediations: list[str] = []
    for chk in report["checks"]:
        badge = chk["status"].upper()
        lines.append(f"  [{badge}] {chk['id']}: {chk['summary']}")
        if chk.get("remediation") and chk["status"] in ("fail", "warn"):
            remediations.append(f"- ({chk['id']}) {chk['remediation']}")

    lines.append("")
    lines.append(
        "Devices: "
        f"total={total_devs} "
        f"(device={by_state['device']}, "
        f"unauthorized={by_state['unauthorized']}, "
        f"offline={by_state['offline']}, "
        f"no_permissions={by_state['no_permissions']}, "
        f"other={by_state['other']})"
    )

    if tc["adb_path"] is None or report.get("exit_code") == EXIT_MISSING_TOOLCHAIN:
        lines.append("")
        lines.append("Hint: Run 'adapt setup' to download and install platform-tools.")

    if show_fix:
        lines.append("")
        lines.append("Safe Remediation Commands (--fix, read-only preview):")
        if remediations:
            lines.extend(remediations)
        else:
            lines.append("  No remediation actions needed.")

    return "\n".join(lines)


def validate_zip_members_safe(zf: zipfile.ZipFile, dest_dir: Path) -> None:
    """Raise ValueError if any zip entry contains path traversal (`..` or absolute path)."""
    resolved_dest = dest_dir.resolve()
    for member in zf.infolist():
        name = member.filename
        if name.startswith("/") or name.startswith("\\"):
            raise ValueError(f"Unsafe absolute path in zip archive: {name}")
        norm = Path(name)
        if ".." in norm.parts:
            raise ValueError(f"Unsafe path traversal entry in zip archive: {name}")
        target_path = (resolved_dest / norm).resolve()
        try:
            target_path.relative_to(resolved_dest)
        except ValueError as exc:
            raise ValueError(f"Zip entry escapes target directory: {name}") from exc


def compute_file_sha256(path: Path) -> str:
    """Return hex SHA-256 digest of a file."""
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def ensure_debian_user_path(target_dir: Path, home: Path, env: DoctorEnv | None = None) -> bool:
    """Idempotently prepend target_dir to ~/.profile (and ~/.bashrc if present)."""
    export_line = "export PATH=" + shlex.quote(str(target_dir)) + ':"$PATH"'
    marker = "# ADAPT managed platform-tools PATH"
    block = f"\n{marker}\n{export_line}\n"

    profile_path = home / ".profile"
    if env and env.debian_profile_texts is not None:
        existing = env.debian_profile_texts.get(profile_path, "")
        if str(target_dir) not in existing:
            env.debian_profile_texts[profile_path] = existing + block
        return True

    try:
        existing = (
            profile_path.read_text(encoding="utf-8", errors="ignore")
            if profile_path.is_file()
            else ""
        )
        if str(target_dir) not in existing:
            profile_path.parent.mkdir(parents=True, exist_ok=True)
            profile_path.write_text(existing + block, encoding="utf-8")
        return True
    except OSError:
        return False


def _invoke_downloader(
    dl_fn: Callable[..., Any],
    url: str,
    target_dir: Path,
    expected_sha256: str | None,
) -> Any:
    """Invoke downloader forwarding expected_sha256 when supported.

    Keeps 2-arg test downloaders working when expected_sha256 is None, without
    masking TypeError raised inside the downloader body.
    """
    try:
        sig = inspect.signature(dl_fn)
    except (TypeError, ValueError):
        if expected_sha256 is not None:
            return dl_fn(url, target_dir, expected_sha256=expected_sha256)
        return dl_fn(url, target_dir)

    params = sig.parameters
    accepts_kwargs = any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
    )
    if "expected_sha256" in params or accepts_kwargs:
        return dl_fn(url, target_dir, expected_sha256=expected_sha256)

    positional_params = [
        p
        for p in params.values()
        if p.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    ]
    has_varargs = any(
        p.kind == inspect.Parameter.VAR_POSITIONAL for p in params.values()
    )
    if len(positional_params) >= 3 or has_varargs:
        return dl_fn(url, target_dir, expected_sha256)

    if expected_sha256 is not None:
        raise TypeError(
            f"Downloader {dl_fn!r} does not accept expected_sha256."
        )
    return dl_fn(url, target_dir)


def run_setup_workflow(
    env: DoctorEnv | None = None,
    *,
    force: bool = False,
    dry_run: bool = False,
    no_path: bool = False,
    yes: bool = False,
    isolated: bool = False,
    expected_sha256: str | None = None,
    downloader: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Execute the `adapt setup` user-scope bootstrap contract."""
    pre_report = run_doctor_checks(env, command_name="setup")
    host_os = pre_report["host"]["os"]
    home = _get_home_dir(env)
    environ = _get_env_map(env)

    # 1. Pre-flight gate: port 5037 safety MUST be checked first.
    srv_check = next(c for c in pre_report["checks"] if c["id"] == "adb_server_5037")
    if srv_check["exit_code"] == EXIT_UNSAFE_SHARED_SERVER:
        pre_report["setup"] = {
            "action": "aborted_unsafe_server",
            "dry_run": dry_run,
            "no_path": no_path,
            "target_dir": None,
            "url": None,
            "downloaded": False,
            "path_updated": False,
        }
        return pre_report

    # Determine target user-scope directory (NEVER ANDROID_HOME or system paths)
    if isolated:
        target_dir = home / ".android" / "platform-tools"
    elif host_os == "windows":
        local_app = Path(environ.get("LOCALAPPDATA", str(home / "AppData" / "Local")))
        target_dir = local_app / "Android" / "platform-tools"
    else:
        target_dir = home / ".local" / "share" / "android" / "platform-tools"

    url = PLATFORM_TOOLS_URL_WINDOWS if host_os == "windows" else PLATFORM_TOOLS_URL_LINUX
    parsed_url = urllib.parse.urlparse(url)
    if parsed_url.scheme != "https" or parsed_url.netloc != "dl.google.com":
        raise ValueError(f"Refusing non-HTTPS or untrusted download URL: {url}")

    # 2. Check if already healthy on PATH
    toolchain_healthy = all(
        c["exit_code"] == EXIT_OK
        for c in pre_report["checks"]
        if c["id"] in ("adb_path", "adb_version", "adb_origin", "path_config")
    )
    if toolchain_healthy and not force:
        pre_report["setup"] = {
            "action": "noop_already_healthy",
            "dry_run": dry_run,
            "no_path": no_path,
            "target_dir": str(target_dir),
            "url": url,
            "downloaded": False,
            "path_updated": False,
        }
        return pre_report

    if dry_run:
        pre_report["setup"] = {
            "action": "dry_run",
            "dry_run": True,
            "no_path": no_path,
            "target_dir": str(target_dir),
            "url": url,
            "downloaded": False,
            "path_updated": False,
            "would_download": True,
            "would_update_path": not no_path,
        }
        return pre_report

    # Confirm before mutating download when --yes is omitted on an interactive TTY
    stdin_tty = bool(
        sys.stdin is not None
        and callable(getattr(sys.stdin, "isatty", None))
        and sys.stdin.isatty()
    )
    stderr_tty = bool(
        sys.stderr is not None
        and callable(getattr(sys.stderr, "isatty", None))
        and sys.stderr.isatty()
    )
    if not yes and stdin_tty and stderr_tty:
        sys.stderr.write(
            f"Download official Google platform-tools zip ({url}) into {target_dir}? [y/N]: "
        )
        sys.stderr.flush()
        try:
            answer = input().strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            pre_report["setup"] = {
                "action": "aborted_unconfirmed",
                "dry_run": dry_run,
                "no_path": no_path,
                "target_dir": str(target_dir),
                "url": url,
                "downloaded": False,
                "path_updated": False,
            }
            return pre_report

    # 3. Perform user-scope download & unpack
    dl_fn = downloader or download_and_extract_platform_tools
    dl_result = _invoke_downloader(dl_fn, url, target_dir, expected_sha256)

    binary_name = "adb.exe" if host_os == "windows" else "adb"
    installed_adb = target_dir / binary_name

    # Update fake environment if running under test injection
    if env is not None:
        if env.existing_paths is not None:
            env.existing_paths.add(installed_adb)
            env.existing_paths.add(target_dir)
        if env.version_outputs is not None and installed_adb not in env.version_outputs:
            env.version_outputs[installed_adb] = (
                "Android Debug Bridge version 1.0.41\n"
                "Version 35.0.2-12147458\n"
                f"Installed as {installed_adb}\n"
            )
        if env.source_properties is not None and target_dir not in env.source_properties:
            env.source_properties[target_dir / "source.properties"] = "Pkg.Revision=35.0.2\n"

    # 4. Update user-scope PATH unless --no-path
    path_updated = False
    if not no_path:
        sep = ";" if host_os == "windows" else ":"
        cur_path = environ.get("PATH", "")
        parts = [p.strip() for p in cur_path.split(sep) if p.strip()]
        remaining = [p for p in parts if p != str(target_dir)]
        environ["PATH"] = f"{target_dir}{sep}{sep.join(remaining)}" if remaining else str(target_dir)
        if env is None:
            os.environ["PATH"] = environ["PATH"]
        if env is not None:
            env.which_set = True
            env.which_adb = installed_adb
            env.path_executables = [installed_adb] + [
                p for p in (env.path_executables or []) if p != installed_adb
            ]
            path_updated = True
        elif host_os == "windows":
            path_updated = add_to_windows_user_path(target_dir)
        else:
            path_updated = ensure_debian_user_path(target_dir, home, env)

    post_report = run_doctor_checks(env, command_name="setup")
    post_report["setup"] = {
        "action": "installed",
        "dry_run": False,
        "no_path": no_path,
        "target_dir": str(target_dir),
        "url": url,
        "downloaded": True,
        "path_updated": path_updated,
        "sha256": dl_result if isinstance(dl_result, str) else expected_sha256,
    }
    return post_report
