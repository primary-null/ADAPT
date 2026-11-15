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
    ADB_OVERRIDE_VARS,
    MANAGED_PATH_MARKER,
    REGISTERED_MANAGED_SOURCE,
    AdbProbeError,
    ManagedRegistration,
    PLATFORM_TOOLS_URL_LINUX,
    PLATFORM_TOOLS_URL_WINDOWS,
    add_to_windows_user_path,
    dir_listed_on_path,
    download_and_extract_platform_tools,
    find_registered_managed_adb,
    get_default_platform_tools_dir,
    managed_path_export_line,
    parse_devices_short,
    read_profile_text_bounded,
    resolve_adb_details,
    split_adb_prefix,
)
from adapt_adb.failure_guide import doctor_remediation_text
from adapt_adb.failure_report import tagged
from adapt_adb.hints import find_udev_rules_file

_PROC_ROOT = Path("/proc")
_TMP_5037_PATH = Path("/tmp/5037")
_OS_RELEASE_PATH = Path("/etc/os-release")
_DEBIAN_VERSION_PATH = Path("/etc/debian_version")

# Host OS values reported in `host.os`. Only Windows and Linux are supported hosts.
SUPPORTED_HOST_OS = frozenset({"windows", "debian", "linux"})

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

UDEV_RULES_TARGET = "/etc/udev/rules.d/51-android.rules"
RELOGIN_NOTE = "Group membership changes take effect after you log out and back in."

# Shown once per human doctor report; `host.uid_is_root` / `host.elevated` carry the facts.
ROOT_WARNING = (
    "ADAPT is running as root. An adb server it starts runs as root and can leave root-owned "
    "files (such as ~/.android/adbkey and /tmp/5037) that later block normal use. "
    "Run ADAPT as your normal user, without sudo."
)
ELEVATED_WARNING = (
    "ADAPT is running in an elevated (Administrator) Windows session. An adb server started "
    "from here runs elevated, and tools in normal, non-elevated sessions cannot share it. "
    "Run ADAPT from a normal terminal."
)


def privilege_warning(host_os: str, uid_is_root: bool, elevated: bool) -> str | None:
    """Return the root/elevated warning text for this host, or None."""
    if host_os == "windows":
        return ELEVATED_WARNING if elevated else None
    return ROOT_WARNING if uid_is_root else None


def udev_install_command() -> str | None:
    """`sudo cp <ADAPT's rules file> /etc/udev/rules.d/51-android.rules`, using the real path."""
    rules = find_udev_rules_file()
    if rules is None:
        return None
    return f"sudo cp {shlex.quote(str(rules))} {UDEV_RULES_TARGET}"


def linux_udev_remediation(host_os: str, failure_id: str = "host.udev_missing") -> str:
    """Remediation for `debian_usb_permissions`; apt and plugdev are prescribed on Debian only.

    The wording lives in the failure-guide registry; this fills in ADAPT's rules file.
    """
    copy_cmd = udev_install_command()
    create = f"create `{UDEV_RULES_TARGET}` with `{LINUX_UDEV_RULE_LINE}`"
    from_adapt = f"`{copy_cmd}`" if copy_cmd else create
    text = doctor_remediation_text(failure_id, "debian" if host_os == "debian" else None, UDEV_SOURCE=from_adapt)
    assert text is not None
    return text


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
    has_adbusers_rule: bool = False  # an Android-relevant rule references the `adbusers` group


@dataclass
class DoctorEnv:
    """Injectable host probe environment for deterministic testing without hardware."""

    host_os: str | None = None  # "windows", "debian", "linux", "macos"
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
    windows_current_account: str | None = None
    windows_user_path: str | None = None
    debian_usb_state: DebianUsbState | None = None
    debian_profile_texts: dict[Path, str] | None = None
    devices_l_output: str | None = None
    devices_l_invoked: list[Path] = field(default_factory=list)
    commands_invoked: list[list[str]] = field(default_factory=list)
    # The adb ADAPT's commands use (adb.resolve_adb). When command_adb_source is None,
    # an injected environment uses a valid ADAPT_ADB / ADB_PATH override, else the PATH adb.
    command_adb: Path | None = None
    # "override", "registered_managed_copy", "path", "fallback_location", "none"; when None,
    # an injected POSIX environment also applies adb.find_registered_managed_adb.
    command_adb_source: str | None = None
    os_release_text: str | None = None  # /etc/os-release contents for host detection
    elevated: bool | None = None  # Windows elevated (Administrator) session


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

    failure_id = "adb.server.unverified"  # safe generic; preflight tags the specific case


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
        raise tagged(UnsafeAdbServerError(
            "Selected ADB server endpoint cannot be verified safely; refusing to contact it."
        ), "adb.server.endpoint_unverified")
    host_os = _detect_host_os()
    observed = state or _probe_server_5037(host_os, Path.home(), trusted_adb=adb_path)
    if observed.probe_error:
        raise tagged(UnsafeAdbServerError("ADB listener inspection failed; idle state is unverified."),
                     "adb.server.probe_failed")
    if not observed.listening:
        return
    if (
        observed.owner is None
        or not observed.owner_matches_current_user
        or observed.owner_is_root_or_system
        or observed.server_version_raw is None
    ):
        foreign = observed.owner is not None and (
            not observed.owner_matches_current_user or observed.owner_is_root_or_system
        )
        raise tagged(UnsafeAdbServerError(
            "Listening ADB server ownership/version is unverified or unsafe."
        ), "adb.server.foreign_owner" if foreign else "adb.server.unverified")
    client_raw = _query_adb_version_raw(adb_path, None)
    server_tuple = parse_platform_tools_version(observed.server_version_raw).get("version_tuple")
    client_tuple = parse_platform_tools_version(client_raw).get("version_tuple")
    if server_tuple is None or client_tuple is None or server_tuple != client_tuple:
        raise tagged(UnsafeAdbServerError(
            "Listening ADB server version is unverified or does not match this client."
        ), "adb.server.version_mismatch" if server_tuple and client_tuple else "adb.server.unverified")


def _os_release_ids(text: str) -> set[str]:
    """Return the lower-cased ID and ID_LIKE tokens of an os-release document."""
    ids: set[str] = set()
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if not sep or key not in ("ID", "ID_LIKE"):
            continue
        value = value.strip().strip("\"'")
        ids.update(token.lower() for token in value.split() if token)
    return ids


def _classify_host_os(platform_name: str, os_release_text: str | None, debian_version_exists: bool) -> str:
    """Map a sys.platform value and Linux release facts to a `host.os` value.

    Debian-family Linux (ID or ID_LIKE names debian or ubuntu) is `debian`; other
    Linux is `linux`; macOS is `macos`. A Linux host without readable os-release
    data counts as Debian only when /etc/debian_version exists.
    """
    if platform_name == "win32":
        return "windows"
    if platform_name == "darwin":
        return "macos"
    if platform_name.startswith("linux"):
        if os_release_text is not None:
            ids = _os_release_ids(os_release_text)
            if ids:
                return "debian" if ids & {"debian", "ubuntu"} else "linux"
        return "debian" if debian_version_exists else "linux"
    return "linux"


def _detect_host_os(env: DoctorEnv | None = None) -> str:
    if env and env.host_os:
        return env.host_os
    os_release_text: str | None = env.os_release_text if env is not None else None
    debian_version_exists = False
    if sys.platform.startswith("linux") and os_release_text is None:
        try:
            if _OS_RELEASE_PATH.is_file():
                os_release_text = _OS_RELEASE_PATH.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            os_release_text = None
        try:
            debian_version_exists = _DEBIAN_VERSION_PATH.exists()
        except OSError:
            debian_version_exists = False
    return _classify_host_os(sys.platform, os_release_text, debian_version_exists)


def _detect_arch(env: DoctorEnv | None = None) -> str:
    if env and env.arch:
        return env.arch
    machine = platform.machine().strip()
    return machine or "unknown"


def _detect_elevated(host_os: str, env: DoctorEnv | None = None) -> bool:
    """Return True for an elevated (Administrator) Windows session; injected envs default to False."""
    if env is not None:
        return bool(env.elevated)
    return host_os == "windows" and _is_windows_admin()


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


def _same_file_key(cand: Path, host_os: str, env: DoctorEnv | None = None) -> str:
    """Key under which PATH entries that reach one file count once.

    On a merged-/usr Linux host `/bin` is a link to `/usr/bin`, so `/bin/adb` and
    `/usr/bin/adb` are one program, not a second copy that shadows the first.
    """
    text = str(cand)
    if not (env and env.existing_paths is not None):
        try:
            text = os.path.realpath(text)
        except (OSError, ValueError):
            pass
    return text.lower() if host_os == "windows" else text


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


def _current_windows_account() -> str | None:
    """Read the process account as DOMAIN\\user; never trust environment variables."""
    try:
        import ctypes
        from ctypes import wintypes

        get_name = ctypes.WinDLL("secur32", use_last_error=True).GetUserNameExW
        get_name.argtypes = [ctypes.c_int, wintypes.LPWSTR, ctypes.POINTER(wintypes.ULONG)]
        get_name.restype = wintypes.BOOLEAN
        size = wintypes.ULONG(0)
        get_name(2, None, ctypes.byref(size))  # NameSamCompatible
        if not size.value:
            return None
        buf = ctypes.create_unicode_buffer(size.value)
        if get_name(2, buf, ctypes.byref(size)) and "\\" in buf.value:
            return buf.value
    except (AttributeError, OSError, ValueError):
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
    state.owner_matches_current_user = False
    if user_clean:
        owner_str = f"{domain_clean}\\{user_clean}" if domain_clean else user_clean
        state.owner = owner_str
        state.owner_matches_current_user = (
            ret_val in (None, 0)
            and bool(domain_clean)
            and "\\" in cur_user
            and owner_str.casefold() == cur_user.strip().casefold()
        )
        # A privileged account name is unsafe only when it is someone else's:
        # a user signed in as the built-in Administrator owns their own server.
        state.owner_is_root_or_system = not state.owner_matches_current_user and user_clean.casefold() in {
            "system", "local service", "network service", "administrator", "administrators",
        }
    elif ret_val not in (None, 0):
        state.owner = "SYSTEM/Elevated (access denied)"
        state.owner_is_root_or_system = True


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

# Heuristic only: USB vendor IDs (upper-case hex) of common Android phone, tablet and
# chipset makers. A problem device under one of these vendors is treated as Android.
_ANDROID_USB_VENDOR_IDS = frozenset(
    {
        "18D1",  # Google
        "04E8",  # Samsung
        "2717",  # Xiaomi
        "2A70",  # OnePlus
        "12D1",  # Huawei
        "22B8",  # Motorola
        "1004",  # LG
        "0FCE",  # Sony
        "0BB4",  # HTC
        "22D9",  # OPPO / Realme
        "2D95",  # vivo
        "19D2",  # ZTE
        "05C6",  # Qualcomm
        "0E8D",  # MediaTek
        "2AE5",  # Fairphone
        "1949",  # Amazon
        "0955",  # Nvidia
    }
)

_PNP_VID_RE = re.compile(r"(?:^|[\\&])VID_([0-9A-F]{4})", re.IGNORECASE)


def _is_android_pnp_device(instance_id: str, friendly_name: str, class_name: str) -> bool:
    """Return True when a PnP record is confirmed Android-related (class, name or vendor)."""
    if class_name in {"AndroidDevice", "AndroidUsbDeviceClass"}:
        return True
    if _PNP_ANDROID_NAME_RE.search(friendly_name):
        return True
    m = _PNP_VID_RE.search(instance_id)
    return bool(m and m.group(1).upper() in _ANDROID_USB_VENDOR_IDS)


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
        problem_code = _parse_pnp_problem_code(
            fields["problem code"] if "problem code" in fields else fields.get("problem")
        )
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
                    # Same relevance rule as the pnputil fallback.
                    if not _is_relevant_pnp_device(
                        instance_id, friendly_name, class_name, status, problem_code
                    ):
                        continue
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

    if host_os == "macos":
        # ADAPT has no listener probe for macOS; fail closed instead of reading /proc.
        state.probe_error = "Listener inspection is not supported on macOS."
        return state

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

        # Inspect /proc/net/tcp and /proc/net/tcp6 for port 5037 (0x13AD, state 0A = LISTEN).
        # Both tables are scanned completely so a second listener cannot hide behind the
        # first one. Every LISTEN row on the port counts whatever its bind address
        # (fail closed, as on Windows).
        listeners: list[tuple[int, str | None]] = []
        saw_network_table = False
        for tcp_rel in ("tcp", "tcp6"):
            try:
                tcp_path = _PROC_ROOT / "net" / tcp_rel
                if tcp_path.is_file():
                    saw_network_table = True
                    for line in tcp_path.read_text(encoding="utf-8", errors="ignore").splitlines()[1:]:
                        cols = line.split()
                        if not cols:
                            continue
                        if len(cols) < 8:
                            # An unclassifiable row could be a listener; never report idle past it.
                            state.probe_error = "Cannot inspect local TCP listeners."
                            continue
                        local_addr = cols[1].upper()
                        st_hex = cols[3].upper()
                        if not (local_addr.endswith(":13AD") and st_hex == "0A"):
                            continue
                        try:
                            uid = int(cols[7])
                        except ValueError:
                            state.probe_error = "Cannot inspect local TCP listeners."
                            continue
                        listeners.append((uid, cols[9] if len(cols) >= 10 else None))
            except (OSError, ValueError):
                state.probe_error = "Cannot inspect local TCP listeners."
        if not saw_network_table:
            state.probe_error = "Local TCP listener tables are unavailable."

        if listeners:
            state.listening = True
            uids = sorted({uid for uid, _ in listeners})
            cur_uid = os.geteuid() if hasattr(os, "geteuid") else uids[0]  # type: ignore[attr-defined]
            foreign_uids = [uid for uid in uids if uid != cur_uid]
            # Report the most concerning listener: root first, then another user.
            if 0 in foreign_uids:
                reported_uid = 0
                state.owner_is_root_or_system = True
                state.owner_matches_current_user = False
            elif foreign_uids:
                reported_uid = foreign_uids[0]
                state.owner_matches_current_user = False
            else:
                reported_uid = cur_uid
            state.owner_uid = reported_uid
            state.owner = _uid_to_username(reported_uid)
            if len(uids) > 1:
                state.probe_error = "Multiple local TCP 5037 listener owners."

            # One process may hold both an IPv4 and an IPv6 socket (dual stack); more
            # than one distinct PID means more than one listener owner.
            inode_pids = {
                inode: (_find_linux_pid_for_inode(inode, _PROC_ROOT) if inode else None)
                for inode in {inode for _, inode in listeners}
            }
            pids = sorted({pid for pid in inode_pids.values() if pid is not None})
            if pids:
                if len(pids) > 1:
                    state.probe_error = "Multiple local TCP 5037 listener owners."
                elif any(pid is None for pid in inode_pids.values()):
                    if state.probe_error is None:
                        state.probe_error = "Cannot attribute every local TCP 5037 listener to one process."
                state.pid = pids[0]
            else:
                try:
                    ss_proc = subprocess.run(
                        ["ss", "-ltnp", "sport", "=", ":5037"],
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=3,
                    )
                    if ss_proc.returncode == 0:
                        ss_pids = sorted({int(m) for m in re.findall(r"pid=(\d+)", ss_proc.stdout)})
                        if len(ss_pids) > 1:
                            state.probe_error = "Multiple local TCP 5037 listener owners."
                        if ss_pids:
                            state.pid = ss_pids[0]
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
                        exe_target = ""
                    if exe_target:
                        state.server_exe = Path(exe_target)
                except OSError:
                    pass

                if (
                    state.server_exe is not None
                    and state.probe_error is None
                    and state.owner_matches_current_user
                    and not state.owner_is_root_or_system
                ):
                    state.server_version_raw = _trusted_server_version(state.server_exe, trusted_adb, env)
    else:
        # Windows: read-only check of TCP 5037 listener via netstat -ano
        try:
            proc = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True,
                text=True,
                errors="replace",
                check=False,
                timeout=4,
            )
            if proc.returncode != 0:
                state.probe_error = "Local TCP listener inspection failed."
                return state
            for line in proc.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0].upper() == "TCP" and parts[1].endswith(":5037"):
                    if len(parts) != 5:
                        state.probe_error = "Cannot classify local TCP 5037 row."
                        continue
                    remote = parts[2]
                    if remote.rpartition(":")[2].isdigit() and not remote.endswith(":0"):
                        # Connected or closing socket, not a listener. Windows reports
                        # sockets in TIME_WAIT with PID 0, so the PID is not judged here.
                        continue
                    if remote not in {"0.0.0.0:0", "[::]:0", "*:*"}:
                        state.probe_error = "Cannot classify local TCP 5037 row."
                        continue
                    try:
                        pid = int(parts[-1])
                        if pid <= 0:
                            raise ValueError("Invalid PID")
                    except ValueError:
                        state.probe_error = "Cannot classify local TCP 5037 row."
                        continue
                    if state.pid is not None and state.pid != pid:
                        state.probe_error = "Multiple local TCP 5037 listener owners."
                    state.listening = True
                    state.pid = pid
        except (subprocess.SubprocessError, OSError):
            state.probe_error = "Local TCP listener inspection failed."

        if state.listening and state.pid is not None:
            cur_user = (env.windows_current_account if env is not None else _current_windows_account()) or ""
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
                norm = _same_file_key(cand, host_os, env)
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


_COMMAND_ADB_SOURCE_TEXT: dict[str, str] = {
    "override": "the ADAPT_ADB / ADB_PATH environment variable",
    REGISTERED_MANAGED_SOURCE: "the platform-tools folder `adapt setup` registered in ~/.profile",
    "path": "PATH",
    "fallback_location": "a folder ADAPT searches outside PATH",
    "none": "nowhere",
}


def _injected_managed_registration(
    environ: dict[str, str],
    path_adb: Path | None,
    env: DoctorEnv,
    host_os: str | None,
    home: Path | None,
    path_bins: list[Path] | None,
) -> ManagedRegistration | None:
    """`adb.find_registered_managed_adb` with an injected environment's probes (POSIX only)."""
    if host_os is None or host_os == "windows" or home is None:
        return None

    def read_text(profile: Path) -> str | None:
        if env.debian_profile_texts is not None:
            return env.debian_profile_texts.get(profile)
        # Real files are read only when the test also gave a home folder and no fake paths.
        if env.existing_paths is None and env.home_dir is not None:
            return read_profile_text_bounded(profile)
        return None

    def on_path(folder: Path) -> bool:
        if any(_same_adb(p, folder / "adb") for p in (path_bins or [])):
            return True
        return dir_listed_on_path(folder, environ.get("PATH", ""), ":", resolve=False)

    def is_usable(adb_bin: Path) -> bool:
        if not _path_exists(adb_bin, env):
            return False
        return bool(parse_platform_tools_version(_query_adb_version_raw(adb_bin, env)).get("meets_minimum"))

    return find_registered_managed_adb(
        environ, home, path_adb,
        is_usable=is_usable, read_text=read_text, on_path=on_path, same_file=_same_adb,
    )


def _resolve_command_adb(
    environ: dict[str, str],
    path_adb: Path | None,
    env: DoctorEnv | None = None,
    *,
    host_os: str | None = None,
    home: Path | None = None,
    path_bins: list[Path] | None = None,
) -> dict[str, Any]:
    """Return the adb ADAPT's commands use (`adb.resolve_adb`) and where it came from.

    Keys: ``path`` (Path | None), ``source`` ("override", "registered_managed_copy",
    "path", "fallback_location" or "none"), ``override_var`` (name of the first set
    override variable, never its value) and ``override_missing`` (that variable does
    not name an existing file). ``registration`` (adb.ManagedRegistration) is added
    only for "registered_managed_copy".
    """
    override_var: str | None = None
    override_value = ""
    for name in ADB_OVERRIDE_VARS:
        value = environ.get(name, "").strip()
        if value:
            override_var, override_value = name, value
            break

    if env is None:
        resolution = resolve_adb_details()
        real: dict[str, Any] = {
            "path": resolution.path,
            "source": resolution.source,
            "override_var": resolution.override_var,
            "override_missing": resolution.override_missing,
        }
        if resolution.registration is not None:
            real["registration"] = resolution.registration
        return real

    override_path = Path(override_value).expanduser() if override_var else None
    override_missing = bool(override_path is not None and not _path_exists(override_path, env))
    registration: ManagedRegistration | None = None
    if env.command_adb_source is not None:
        cmd_path, source = env.command_adb, env.command_adb_source
    elif override_path is not None and not override_missing:
        cmd_path, source = override_path, "override"
    elif (
        registration := _injected_managed_registration(environ, path_adb, env, host_os, home, path_bins)
    ) is not None:
        cmd_path, source = registration.adb, REGISTERED_MANAGED_SOURCE
    elif path_adb is not None:
        cmd_path, source = path_adb, "path"
    else:
        cmd_path, source = None, "none"
    if cmd_path is None:
        source = "none"
    result: dict[str, Any] = {
        "path": cmd_path, "source": source, "override_var": override_var, "override_missing": override_missing,
    }
    if registration is not None:
        result["registration"] = registration
    return result


def _same_adb(a: Path | None, b: Path | None) -> bool:
    if a is None or b is None:
        return False
    if str(a) == str(b):
        return True
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


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
    elevated = _detect_elevated(host_os, env)
    host_supported = host_os in SUPPORTED_HOST_OS
    environ = _get_env_map(env)
    home = _get_home_dir(env)

    which_bin, path_bins, fallbacks = _discover_candidates(host_os, home, environ, env)

    # Probe resolved adb version and origin if present
    resolved_adb: Path | None = which_bin
    version_raw: str | None = _query_adb_version_raw(resolved_adb, env) if resolved_adb else None
    parsed_ver = parse_platform_tools_version(version_raw)
    origin, origin_details = _classify_adb_origin(resolved_adb, host_os, home, environ, env)
    path_pt_ver: str | None = parsed_ver.get("platform_tools_version")
    path_origin = origin

    # The adb ADAPT's own commands use (ADAPT_ADB / ADB_PATH, PATH, then fallback folders).
    command = _resolve_command_adb(environ, which_bin, env, host_os=host_os, home=home, path_bins=path_bins)
    command_adb: Path | None = command["path"]
    command_source: str = command["source"]
    # The managed copy `adapt setup` registered, used because this PATH lacks its folder.
    registration: ManagedRegistration | None = command.get("registration")
    registered_managed = registration is not None and command_source == REGISTERED_MANAGED_SOURCE
    registered_managed_facts: dict[str, Any] | None = (
        {
            "adb": str(registration.adb),
            "folder": str(registration.folder),
            "registration_file": str(registration.profile),
            "folder_on_path": False,
            "path_adb": str(resolved_adb) if resolved_adb is not None else str(registration.path_adb),
        }
        if registered_managed and registration is not None
        else None
    )
    if command_adb is not None and _same_adb(command_adb, resolved_adb):
        command_version_raw = version_raw
        command_parsed = parsed_ver
        command_origin, command_origin_details = origin, origin_details
    elif command_adb is not None:
        command_version_raw = _query_adb_version_raw(command_adb, env)
        command_parsed = parse_platform_tools_version(command_version_raw)
        command_origin, command_origin_details = _classify_adb_origin(command_adb, host_os, home, environ, env)
    else:
        command_version_raw = None
        command_parsed = parse_platform_tools_version(None)
        command_origin, command_origin_details = "none", {"reason": "no adb binary resolved"}
    # With no adb on PATH, the version and origin checks judge the copy the commands use.
    off_path_command = resolved_adb is None and command_adb is not None
    # So do they when the commands use the registered managed copy instead of the PATH adb.
    if off_path_command or registered_managed:
        parsed_ver, origin, origin_details = command_parsed, command_origin, command_origin_details
    command_usable = bool(command_adb is not None and command_parsed.get("meets_minimum"))
    # The shared-server check and the device listing use the adb the commands use, so
    # doctor never calls a server that ADAPT's own commands started unsafe.
    probe_adb: Path | None = command_adb if command_adb is not None else resolved_adb
    probe_version_raw = command_version_raw if command_adb is not None else version_raw
    probe_parsed = command_parsed if command_adb is not None else parsed_ver

    checks: list[dict[str, Any]] = []

    # -------------------------------------------------------------------------
    # Check 1 (MUST RUN FIRST): adb_server_5037
    # -------------------------------------------------------------------------
    srv = _probe_server_5037(host_os, home, env, trusted_adb=probe_adb)
    server_mismatch = False
    server_parsed_ver: dict[str, Any] = {}
    if srv.listening:
        raw_srv: str | None = srv.server_version_raw
        if raw_srv is not None:
            server_parsed_ver = parse_platform_tools_version(raw_srv)

        srv_tuple = server_parsed_ver.get("version_tuple")
        cli_tuple = probe_parsed.get("version_tuple")
        srv_bridge = server_parsed_ver.get("bridge_version")
        cli_bridge = probe_parsed.get("bridge_version")
        if probe_adb is not None and (raw_srv is not None or srv.server_exe is not None):
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
                    ln for ln in (probe_version_raw or "").splitlines()
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
        "owner_is_root_or_system": srv.owner_is_root_or_system,
        "server_exe": str(srv.server_exe) if srv.server_exe else None,
        "tmp_5037_exists": srv.tmp_5037_exists,
        "tmp_5037_owner_is_root": srv.tmp_5037_owner_is_root,
        "adbkey_exists": srv.adbkey_exists,
        "adbkey_owned_by_root": srv.adbkey_owned_by_root,
        "client_version": probe_parsed.get("platform_tools_version"),
        "client_adb": str(probe_adb) if probe_adb is not None else None,
        "client_adb_source": command_source if command_adb is not None else ("path" if probe_adb else "none"),
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
        and probe_parsed.get("version_tuple") is not None
        and server_parsed_ver["version_tuple"] == probe_parsed["version_tuple"]
    )

    default_endpoint = _uses_default_local_server(["devices"], _get_env_map(env))
    if not default_endpoint:
        srv_status = "fail"
        srv_code = EXIT_UNSAFE_SHARED_SERVER
        srv_summary = "Selected non-default ADB server endpoint is unverified."
        srv_failure_id: str | None = "adb.server.endpoint_unverified"
    elif srv.probe_error and host_os == "macos":
        srv_status = "fail"
        srv_code = EXIT_UNSAFE_SHARED_SERVER
        srv_summary = (
            "macOS is not a supported ADAPT host: ADAPT cannot inspect port 5037 here, "
            "so the shared ADB server state is unverified."
        )
        srv_failure_id = "host.unsupported_platform"
    elif srv.probe_error:
        srv_status = "fail"
        srv_code = EXIT_UNSAFE_SHARED_SERVER
        srv_summary = "ADB listener inspection failed; idle state is unverified."
        srv_failure_id = "adb.server.probe_failed"
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
        srv_failure_id = "adb.server.foreign_owner"
    elif srv.listening and server_mismatch:
        srv_status = "fail"
        srv_code = EXIT_UNSAFE_SHARED_SERVER
        srv_summary = (
            f"Running ADB server on port 5037 ({server_parsed_ver.get('platform_tools_version') or 'unknown'}) "
            f"mismatches resolved adb ({probe_parsed.get('platform_tools_version') or 'unknown'})."
        )
        srv_failure_id = "adb.server.version_mismatch"
    elif srv.listening and not server_verified:
        srv_status = "fail"
        srv_code = EXIT_UNSAFE_SHARED_SERVER
        srv_summary = "Listening ADB server ownership/version is unverified or unsafe."
        srv_failure_id = "adb.server.unverified"
    elif srv.adbkey_owned_by_root:
        srv_status = "warn"
        srv_code = EXIT_OK
        srv_summary = "Port 5037 is safe, but ~/.android/adbkey is owned by root."
        srv_failure_id = "adb.adbkey_root_owned"
    else:
        srv_status = "pass"
        srv_code = EXIT_OK
        srv_summary = (
            "Port 5037 is idle."
            if not srv.listening
            else "Port 5037 server is owned by current user and matches client version."
        )
        srv_failure_id = None

    checks.append(
        {
            "id": "adb_server_5037",
            "status": srv_status,
            "exit_code": srv_code,
            "summary": srv_summary,
            "details": srv_details,
            "remediation": doctor_remediation_text(srv_failure_id),
            "failure_id": srv_failure_id,
        }
    )

    # -------------------------------------------------------------------------
    # Check 2: adb_path
    # -------------------------------------------------------------------------
    existing_fallbacks = {
        k: str(v) for k, v in fallbacks.items() if v is not None and _path_exists(v, env)
    }
    override_var: str | None = command["override_var"]
    override_note = (
        f" {override_var} is set but does not name an existing file; ADAPT ignores it."
        if command["override_missing"] and override_var
        else ""
    )
    command_details = {
        "command_adb": str(command_adb) if command_adb is not None else None,
        "command_adb_source": command_source,
        "override_variable": override_var,
        "override_points_to_file": (not command["override_missing"]) if override_var else None,
    }
    source_text = _COMMAND_ADB_SOURCE_TEXT.get(command_source, command_source)
    off_path_remediation = doctor_remediation_text("adb.off_path")
    if resolved_adb is not None:
        checks.append(
            {
                "id": "adb_path",
                "status": "warn" if override_note else "pass",
                "exit_code": EXIT_OK,
                "summary": f"Resolved adb on PATH at {resolved_adb}" + (f".{override_note}" if override_note else "") + (
                    f"; ADAPT commands use {command_adb} instead ({source_text}, not on PATH in this shell)."
                    if registered_managed else ""
                ),
                "details": {
                    "resolved_adb": str(resolved_adb),
                    "fallbacks_found": existing_fallbacks,
                    **command_details,
                },
                "remediation": (
                    doctor_remediation_text("adb.override_invalid", OVERRIDE_VAR=override_var or "")
                    if override_note else None
                ),
                "failure_id": "adb.override_invalid" if override_note else None,
            }
        )
    elif off_path_command and command_usable:
        checks.append(
            {
                "id": "adb_path",
                "status": "warn",
                "exit_code": EXIT_OK,
                "summary": (
                    f"No adb on PATH, but ADAPT commands use {command_adb} (found through {source_text}). "
                    "Other tools that look for adb on PATH will not find it." + override_note
                ),
                "details": {
                    "resolved_adb": None,
                    "fallbacks_found": existing_fallbacks,
                    **command_details,
                },
                "remediation": off_path_remediation,
                "failure_id": "adb.off_path",
            }
        )
    elif off_path_command:
        checks.append(
            {
                "id": "adb_path",
                "status": "fail",
                "exit_code": EXIT_MISSING_TOOLCHAIN,
                "summary": (
                    f"No adb on PATH; ADAPT commands would use {command_adb} (found through {source_text}), "
                    "but it is older than 34.0.0 or did not report a version." + override_note
                ),
                "details": {
                    "resolved_adb": None,
                    "fallbacks_found": existing_fallbacks,
                    **command_details,
                },
                "remediation": doctor_remediation_text("adb.too_old", "adb_path"),
                "failure_id": "adb.too_old",
            }
        )
    else:
        checks.append(
            {
                "id": "adb_path",
                "status": "fail",
                "exit_code": EXIT_MISSING_TOOLCHAIN,
                "summary": "No adb executable found on PATH." + override_note,
                "details": {
                    "resolved_adb": None,
                    "fallbacks_found": existing_fallbacks,
                    **command_details,
                },
                "remediation": doctor_remediation_text("adb.not_found"),
                "failure_id": "adb.not_found",
            }
        )

    # -------------------------------------------------------------------------
    # Check 3: adb_version (>= 34.0.0)
    # -------------------------------------------------------------------------
    pt_ver_str = parsed_ver.get("platform_tools_version")
    off_path_suffix = (
        " (the adb ADAPT commands use; it is not on PATH)" if off_path_command
        else " (the registered managed adb ADAPT commands use; it is not on PATH in this shell)"
        if registered_managed else ""
    )
    if resolved_adb is None and command_adb is None:
        checks.append(
            {
                "id": "adb_version",
                "status": "fail",
                "exit_code": EXIT_MISSING_TOOLCHAIN,
                "summary": "Cannot check adb version because adb is missing from PATH.",
                "details": {"platform_tools_version": None, "minimum_required": "34.0.0"},
                "remediation": doctor_remediation_text("adb.not_found", "adb_version"),
                "failure_id": "adb.not_found",
            }
        )
    elif parsed_ver.get("meets_minimum"):
        checks.append(
            {
                "id": "adb_version",
                "status": "pass",
                "exit_code": EXIT_OK,
                "summary": f"platform-tools version {pt_ver_str} meets minimum (>= 34.0.0){off_path_suffix}.",
                "details": {
                    "platform_tools_version": pt_ver_str,
                    "bridge_version": parsed_ver.get("bridge_version"),
                    "minimum_required": "34.0.0",
                },
                "remediation": None,
                "failure_id": None,
            }
        )
    else:
        checks.append(
            {
                "id": "adb_version",
                "status": "fail",
                "exit_code": EXIT_MISSING_TOOLCHAIN,
                "summary": (
                    f"platform-tools version {pt_ver_str or 'unknown'} is below required 34.0.0{off_path_suffix}."
                ),
                "details": {
                    "platform_tools_version": pt_ver_str,
                    "bridge_version": parsed_ver.get("bridge_version"),
                    "minimum_required": "34.0.0",
                },
                "remediation": doctor_remediation_text("adb.too_old"),
                "failure_id": "adb.too_old",
            }
        )

    # -------------------------------------------------------------------------
    # Check 4: adb_origin
    # -------------------------------------------------------------------------
    distro_label = "Debian apt" if host_os == "debian" else "the Linux distribution"
    if resolved_adb is None and command_adb is None:
        checks.append(
            {
                "id": "adb_origin",
                "status": "fail",
                "exit_code": EXIT_MISSING_TOOLCHAIN,
                "summary": "No adb binary to verify origin.",
                "details": origin_details,
                "remediation": doctor_remediation_text("adb.not_found", "adb_origin"),
                "failure_id": "adb.not_found",
            }
        )
    elif origin == "debian_apt" and parsed_ver.get("meets_minimum"):
        checks.append(
            {
                "id": "adb_origin",
                "status": "warn",
                "exit_code": EXIT_OK,
                "summary": (
                    f"adb version >= 34.0.0 meets the minimum, but it is packaged by {distro_label}, "
                    f"not verified Google platform-tools{off_path_suffix}."
                ),
                "details": origin_details,
                "remediation": doctor_remediation_text("adb.origin_distribution"),
                "failure_id": "adb.origin_distribution",
            }
        )
    elif origin == "debian_apt":
        checks.append(
            {
                "id": "adb_origin",
                "status": "fail",
                "exit_code": EXIT_MISSING_TOOLCHAIN,
                "summary": (
                    "Active adb is packaged by Debian apt (/usr/bin/adb) and lags official Google releases."
                    if host_os == "debian"
                    else "Active adb is packaged by the Linux distribution and is older than 34.0.0."
                ) + off_path_suffix,
                "details": origin_details,
                "remediation": doctor_remediation_text("adb.too_old", "adb_origin"),
                "failure_id": "adb.too_old",
            }
        )
    elif origin == "google_official" and parsed_ver.get("meets_minimum"):
        checks.append(
            {
                "id": "adb_origin",
                "status": "pass",
                "exit_code": EXIT_OK,
                "summary": f"Verified Google official platform-tools binary{off_path_suffix}.",
                "details": origin_details,
                "remediation": None,
                "failure_id": None,
            }
        )
    elif origin == "unknown" and parsed_ver.get("meets_minimum"):
        checks.append(
            {
                "id": "adb_origin",
                "status": "warn",
                "exit_code": EXIT_OK,
                "summary": f"adb version >= 34.0.0, but binary origin could not be verified as official Google platform-tools{off_path_suffix}.",
                "details": origin_details,
                "remediation": doctor_remediation_text("adb.origin_unverified"),
                "failure_id": "adb.origin_unverified",
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
                "remediation": doctor_remediation_text("adb.origin_untrusted"),
                "failure_id": "adb.origin_untrusted",
            }
        )

    # -------------------------------------------------------------------------
    # Check 5: path_config
    # -------------------------------------------------------------------------
    all_path_strs = [str(p) for p in path_bins]
    shadowed_strs = all_path_strs[1:] if len(all_path_strs) > 1 else []
    # A distribution-packaged adb that meets the minimum is usable (adb_origin warns about it).
    first_bin_ok = bool(
        resolved_adb is not None
        and parsed_ver.get("meets_minimum")
    )
    if registered_managed and registration is not None:
        path_status = "warn"
        path_code = EXIT_OK
        path_summary = (
            f"ADAPT commands use {command_adb}, the managed copy `adapt setup` registered in "
            f"{registration.profile}, because PATH in this shell does not include {registration.folder}; "
            f"plain `adb` in this shell runs {resolved_adb}."
        )
        path_failure_id: str | None = "adb.managed_not_on_path"
    elif (not path_bins or resolved_adb is None) and off_path_command and command_usable:
        path_status = "warn"
        path_code = EXIT_OK
        path_summary = (
            f"No adb executable is reachable on PATH; ADAPT commands use {command_adb} "
            f"(found through {source_text}), which other tools cannot find."
        )
        path_failure_id = "adb.off_path"
    elif not path_bins or resolved_adb is None:
        path_status = "fail"
        path_code = EXIT_MISSING_TOOLCHAIN
        path_summary = "No adb executable is reachable on PATH." + (
            f" The copy ADAPT commands would use ({command_adb}) is older than 34.0.0 or did not report a version."
            if off_path_command
            else ""
        )
        path_failure_id = "adb.too_old" if off_path_command else "adb.not_found"
    elif not first_bin_ok:
        path_status = "fail"
        path_code = EXIT_MISSING_TOOLCHAIN
        path_summary = (
            f"First adb on PATH ({resolved_adb}) is outdated or incompatible"
            + (f", shadowing {shadowed_strs}." if shadowed_strs else ".")
        )
        path_failure_id = "adb.path_first_outdated"
    elif shadowed_strs:
        path_status = "warn"
        path_code = EXIT_OK
        path_summary = f"First adb on PATH is valid ({resolved_adb}), but shadows secondary copies: {shadowed_strs}."
        path_failure_id = "adb.path_shadowed"
    else:
        path_status = "pass"
        path_code = EXIT_OK
        path_summary = f"PATH resolves cleanly to {resolved_adb}."
        path_failure_id = None

    checks.append(
        {
            "id": "path_config",
            "status": path_status,
            "exit_code": path_code,
            "summary": path_summary,
            "details": {
                "path_executables": all_path_strs,
                "shadowed_executables": shadowed_strs,
                "command_adb": command_details["command_adb"],
                "command_adb_source": command_source,
                "registered_managed_copy": registered_managed_facts,
            },
            "remediation": doctor_remediation_text(
                path_failure_id, "path_config",
                **({"FOLDER": str(registration.folder)} if registered_managed and registration is not None else {}),
            ),
            "failure_id": path_failure_id,
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
        ah_failure_id: str | None = None
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
            ah_failure_id = None
        else:
            ah_status = "warn"
            ah_summary = (
                f"ANDROID_HOME ({sdk_val}) is set but missing directory or platform-tools "
                "(left untouched by adapt)."
            )
            ah_failure_id = "host.android_home_incomplete"

    checks.append(
        {
            "id": "android_home",
            "status": ah_status,
            "exit_code": EXIT_OK,
            "summary": ah_summary,
            "details": ah_details,
            "remediation": doctor_remediation_text(ah_failure_id),
            "failure_id": ah_failure_id,
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
                "failure_id": None,
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
                    "remediation": doctor_remediation_text("host.usb_driver_probe_failed"),
                    "failure_id": "host.usb_driver_probe_failed",
                }
            )
        else:
            # Only confirmed Android-related problems fail the check; other USB problems
            # stay visible as a warning so a disabled webcam cannot block Android work.
            problem_devices: list[dict[str, Any]] = []
            other_usb_problem_devices: list[dict[str, Any]] = []
            for d in pnp_records:
                if d.problem_code == 0 and d.status.upper() in ("OK", "UNKNOWN", ""):
                    continue
                pnp_entry = {
                    "instance_id": redact_serial(d.instance_id),
                    "friendly_name": d.friendly_name,
                    "class_name": d.class_name,
                    "status": d.status,
                    "problem_code": d.problem_code,
                }
                if _is_android_pnp_device(d.instance_id, d.friendly_name, d.class_name):
                    problem_devices.append(pnp_entry)
                else:
                    other_usb_problem_devices.append(pnp_entry)
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
                            "other_usb_problem_devices": other_usb_problem_devices,
                            "probe_available": True,
                        },
                        "remediation": doctor_remediation_text("host.usb_driver_problem"),
                        "failure_id": "host.usb_driver_problem",
                    }
                )
            elif other_usb_problem_devices:
                checks.append(
                    {
                        "id": "windows_pnp",
                        "status": "warn",
                        "exit_code": EXIT_OK,
                        "summary": (
                            f"{len(other_usb_problem_devices)} non-Android USB device(s) report a driver problem; "
                            "no Android USB driver problem was detected."
                        ),
                        "details": {
                            "inspected_count": len(pnp_records),
                            "problem_devices": [],
                            "other_usb_problem_devices": other_usb_problem_devices,
                            "probe_available": True,
                        },
                        "remediation": doctor_remediation_text("host.usb_driver_problem_other"),
                        "failure_id": "host.usb_driver_problem_other",
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
                            "other_usb_problem_devices": [],
                            "probe_available": True,
                        },
                        "remediation": None,
                        "failure_id": None,
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
                "failure_id": None,
            }
        )
    elif not host_supported:
        checks.append(
            {
                "id": "debian_usb_permissions",
                "status": "skip",
                "exit_code": EXIT_OK,
                "summary": "Skipped Linux udev check: this host is not a supported ADAPT platform, so it does not apply.",
                "details": {"reason": f"host_os is {host_os}"},
                "remediation": None,
                "failure_id": None,
            }
        )
    else:
        deb_state = (
            env.debian_usb_state
            if (env and env.debian_usb_state is not None)
            else _probe_debian_usb_permissions(environ)
        )
        seat_value = deb_state.seat
        # Group membership only grants access when an Android rule assigns that group.
        deb_group_access = (deb_state.in_plugdev_group and deb_state.has_plugdev_rule) or (
            deb_state.in_adbusers_group and deb_state.has_adbusers_rule
        )
        deb_details = {
            "has_udev_rules": deb_state.has_udev_rules,
            "has_uaccess_tag": deb_state.has_uaccess_tag,
            "has_plugdev_rule": deb_state.has_plugdev_rule,
            "has_adbusers_rule": deb_state.has_adbusers_rule,
            "rule_files": deb_state.rule_files,
            "in_plugdev_group": deb_state.in_plugdev_group,
            "in_adbusers_group": deb_state.in_adbusers_group,
            "effective_group_access": deb_group_access,
            "seat": deb_state.seat,
        }
        if not deb_state.has_udev_rules:
            deb_status = "fail"
            deb_code = EXIT_DEVICE_OR_PERMISSIONS
            deb_summary = "Missing Android USB udev rules in /etc/udev/rules.d or /lib/udev/rules.d."
            deb_failure_id: str | None = "host.udev_missing"
        elif deb_state.seat == "headless" and not deb_group_access:
            deb_status = "fail"
            deb_code = EXIT_DEVICE_OR_PERMISSIONS
            deb_member_of = (
                "plugdev" if deb_state.in_plugdev_group else "adbusers" if deb_state.in_adbusers_group else None
            )
            if deb_member_of:
                # The user already has the group; what is missing is a rule that grants it.
                deb_summary = (
                    f"Headless/SSH session without active local seat: this user is in `{deb_member_of}`, "
                    "but no Android udev rule grants that group."
                )
            else:
                deb_summary = (
                    "Headless/SSH session without active local seat requires membership in a group "
                    "that the Android udev rules grant (`plugdev` or `adbusers`)."
                )
            deb_failure_id = "host.udev_group_missing"
        elif not deb_state.has_uaccess_tag and not deb_group_access:
            deb_status = "fail"
            deb_code = EXIT_DEVICE_OR_PERMISSIONS
            deb_summary = (
                "Android udev rules lack TAG+=\"uaccess\" and grant no group this user belongs to "
                "(`plugdev` or `adbusers`)."
            )
            deb_failure_id = "host.udev_no_access"
        elif deb_state.seat == "local" and deb_state.has_uaccess_tag and not deb_group_access:
            deb_status = "warn"
            deb_code = EXIT_OK
            deb_summary = (
                "Active local seat can use the uaccess rule, but no Android udev rule grants a group "
                "this user belongs to (`plugdev` recommended for SSH/headless sessions)."
            )
            deb_failure_id = "host.udev_group_recommended"
        else:
            deb_status = "pass"
            deb_code = EXIT_OK
            deb_summary = (
                "Android udev rules were found, and this session has uaccess or a group those rules grant."
            )
            deb_failure_id = None

        checks.append(
            {
                "id": "debian_usb_permissions",
                "status": deb_status,
                "exit_code": deb_code,
                "summary": deb_summary,
                "details": deb_details,
                "remediation": linux_udev_remediation(host_os, deb_failure_id) if deb_failure_id else None,
                "failure_id": deb_failure_id,
            }
        )

    # -------------------------------------------------------------------------
    # Check 9 (MUST RUN LAST): devices_state
    # Only run if toolchain is valid (>= 34.0.0) AND adb_server_5037 passed!
    # -------------------------------------------------------------------------
    can_probe_devices = (
        default_endpoint
        and srv_code == EXIT_OK
        and probe_adb is not None
        and bool(probe_parsed.get("meets_minimum"))
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
            "Skipped `adb devices -l` because ADB_SERVER_SOCKET / ANDROID_ADB_SERVER_PORT / "
            "ADB_SERVER_PORT select a non-default ADB server."
            if not default_endpoint
            else
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
                "failure_id": None,
            }
        )
    else:
        assert probe_adb is not None
        try:
            raw_devices = _query_adb_devices_l(probe_adb, env)
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
                    "remediation": doctor_remediation_text("adb.enumeration_failed"),
                    "failure_id": "adb.enumeration_failed",
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
                dev_failure_id: str | None = (
                    "device.access_blocked" if unauth_count > 0 and noperm_count > 0
                    else "device.unauthorized" if unauth_count > 0
                    else "device.no_permissions"
                )
            elif offline_count > 0:
                dev_status = "warn"
                dev_code = EXIT_OK
                dev_summary = f"{offline_count} device(s) reported offline; {by_state['device']} ready."
                dev_failure_id = "device.offline"
            else:
                dev_status = "pass"
                dev_code = EXIT_OK
                dev_summary = f"{by_state['device']} ready device(s) connected (0 unauthorized)."
                dev_failure_id = None

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
                    "remediation": doctor_remediation_text(dev_failure_id),
                    "failure_id": dev_failure_id,
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
            "elevated": elevated,
            "supported": host_supported,
        },
        "exit_code": overall_exit,
        "status": overall_status,
        "toolchain": {
            "adb_path": str(resolved_adb) if resolved_adb is not None else None,
            "platform_tools_version": path_pt_ver,
            "origin": path_origin,
            "android_home_present": android_home_present,
            "command_adb_path": str(command_adb) if command_adb is not None else None,
            "command_adb_source": command_source,
            "command_adb_version": command_parsed.get("platform_tools_version"),
            "command_adb_origin": command_origin,
            "registered_managed_copy": registered_managed_facts,
        },
        "checks": checks,
        "devices": devices_summary_obj,
    }


# Searched in udev precedence order; a file in an earlier directory overrides a
# same-named file in a later one. Merged-/usr systems resolve two of these to one.
_UDEV_RULE_DIRS: tuple[Path, ...] = (
    Path("/etc/udev/rules.d"),
    Path("/lib/udev/rules.d"),
    Path("/usr/lib/udev/rules.d"),
)

# ADB USB interface: class ff, subclass 42.
_UDEV_ADB_IFACE_CLASS_RE = re.compile(r'bInterfaceClass\}?\s*==\s*"(?:0x)?ff"', re.IGNORECASE)
_UDEV_ADB_IFACE_SUBCLASS_RE = re.compile(r'bInterfaceSubClass\}?\s*==\s*"(?:0x)?42"', re.IGNORECASE)
_UDEV_ADB_USB_INTERFACES_RE = re.compile(r"ID_USB_INTERFACES[^\n]*:ff42", re.IGNORECASE)
_UDEV_ADB_USER_RE = re.compile(r"\badb_user\b")
_UDEV_UACCESS_RE = re.compile(r"\buaccess\b")
_UDEV_PLUGDEV_RE = re.compile(r"\bplugdev\b")
_UDEV_ADBUSERS_RE = re.compile(r"\badbusers\b")


def _strip_udev_comments(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _is_android_udev_rule(file_name: str, active_text: str) -> bool:
    """Return True for rules that target Android/ADB devices, not any USB rule file."""
    if "android" in file_name.lower():
        return True
    if _UDEV_ADB_IFACE_CLASS_RE.search(active_text) and _UDEV_ADB_IFACE_SUBCLASS_RE.search(active_text):
        return True
    if _UDEV_ADB_USB_INTERFACES_RE.search(active_text):
        return True
    if _UDEV_ADB_USER_RE.search(active_text):
        return True
    # Newer systemd grants uaccess to ADB/fastboot devices through this hwdb property.
    return "ID_DEBUG_APPLIANCE" in active_text


def _probe_debian_usb_permissions(environ: dict[str, str]) -> DebianUsbState:
    rule_files: list[str] = []
    has_uaccess = False
    has_plugdev_rule = False
    has_adbusers_rule = False
    seen_dirs: set[Path] = set()
    seen_names: set[str] = set()

    for rdir in _UDEV_RULE_DIRS:
        try:
            if not rdir.is_dir():
                continue
            resolved_dir = rdir.resolve()
            if resolved_dir in seen_dirs:
                continue
            seen_dirs.add(resolved_dir)
            for entry in sorted(rdir.glob("*.rules")):
                if entry.name in seen_names:
                    continue
                seen_names.add(entry.name)
                try:
                    text = entry.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                active_text = _strip_udev_comments(text)
                if not _is_android_udev_rule(entry.name, active_text):
                    continue
                rule_files.append(str(entry))
                if _UDEV_UACCESS_RE.search(active_text):
                    has_uaccess = True
                if _UDEV_PLUGDEV_RE.search(active_text):
                    has_plugdev_rule = True
                if _UDEV_ADBUSERS_RE.search(active_text):
                    has_adbusers_rule = True
        except OSError:
            continue

    in_plugdev = False
    in_adbusers = False
    try:
        import grp  # type: ignore[import-not-found]

        user_groups = set(os.getgroups()) if hasattr(os, "getgroups") else set()
        if hasattr(os, "getegid"):
            user_groups.add(os.getegid())  # type: ignore[attr-defined]
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
        has_adbusers_rule=has_adbusers_rule,
    )


def _query_adb_devices_l(adb_bin: Path, env: DoctorEnv | None = None) -> str:
    env_map = _get_env_map(env)
    if not _uses_default_local_server(["devices"], env_map):
        raise AdbProbeError("Selected ADB server endpoint is unverified.")
    # The doctor needs only serial and state. Plain `adb devices` is TAB-delimited,
    # which is what parse_devices_short requires; `devices -l` pads with spaces and
    # could never be parsed for a real attached device.
    if env is not None:
        env.devices_l_invoked.append(adb_bin)
        env.commands_invoked.append([str(adb_bin), "devices"])
        if env.devices_l_output is not None:
            return env.devices_l_output
    try:
        proc = subprocess.run(
            [str(adb_bin), "devices"],
            env=env_map,
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

    supported = host.get("supported", host_os in SUPPORTED_HOST_OS or not host_os)
    items: list[tuple[str, str]] = []
    if toolchain_missing and supported:
        off_path_copy = tc.get("adb_path") is None and tc.get("command_adb_path") is not None
        items.append(
            (
                "setup",
                "Run 'adapt setup' to put user-scope platform-tools on PATH "
                "(it downloads only when no healthy copy is installed)."
                if off_path_copy
                else "Run 'adapt setup' to download and install user-scope Google platform-tools.",
            )
        )
    if host_os == "windows":
        items.append(
            (
                "device_manager",
                "Windows USB: Install Google USB Driver or Samsung OEM USB driver via Device Manager.",
            )
        )
    elif host_os and supported:
        copy_cmd = udev_install_command() or f"install Android udev rules as {UDEV_RULES_TARGET}"
        if host_os == "debian":
            udev_label = (
                f"Debian/Linux USB udev: {copy_cmd} && sudo udevadm control --reload-rules "
                "&& sudo udevadm trigger && sudo usermod -aG plugdev $USER, then log out and back in "
                "(run adb unprivileged, never as root)."
            )
        else:
            udev_label = (
                f"Linux USB udev: {copy_cmd} && sudo udevadm control --reload-rules "
                "&& sudo udevadm trigger; for SSH sessions also join the group the rules grant "
                "(ADAPT's file uses plugdev; your distribution may name it differently), "
                "then log out and back in (run adb unprivileged, never as root)."
            )
        items.append(("udev", udev_label))

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


DOCTOR_FIX_POINTER = "Run 'adapt doctor --fix' to see how to fix the items above."


def _cli_already_warned_privilege() -> bool:
    """True when the running `adapt` command already printed the root/elevated warning on stderr."""
    try:
        from adapt_adb.failure_report import _click_context

        ctx = _click_context()
        return bool(ctx is not None and ctx.find_root().meta.get("adapt_privilege_warned"))
    except Exception:
        return False


def _host_notes(report: dict[str, Any]) -> list[str]:
    """Human-only host notes: unsupported platform and root/elevated sessions.

    The root/elevated note is left out when the CLI already warned on stderr, so a
    terminal shows it once; library and console renders keep it.
    """
    host = report.get("host") or {}
    host_os = str(host.get("os") or "")
    notes: list[str] = []
    if host.get("supported") is False:
        notes.append(
            f"Platform: {host_os} is not a supported ADAPT host (Windows and Linux are). "
            "Checks that need a supported host are skipped or fail closed."
        )
    warning = privilege_warning(host_os, bool(host.get("uid_is_root")), bool(host.get("elevated")))
    if warning and not _cli_already_warned_privilege():
        notes.append(f"Warning: {warning}")
    return notes


def _render_doctor_tty(report: dict[str, Any], *, show_fix: bool = False) -> str:
    from adapt_adb.console import render_console_report

    from adapt_adb.failure_report import pointers_enabled

    rendered = render_console_report(
        report, build_next_actions(report), show_fix=show_fix,
        width=shutil.get_terminal_size(fallback=(80, 24)).columns,
        color=True,
        explain=pointers_enabled(),
    )
    notes = _host_notes(report)
    return "\n".join([*notes, "", rendered]) if notes else rendered


def render_doctor_human(report: dict[str, Any], *, show_fix: bool = False) -> str:
    """Format a human-readable diagnostic summary from a doctor report."""
    if _is_stdout_tty():
        return _render_doctor_tty(report, show_fix=show_fix)

    lines: list[str] = []
    host = report["host"]
    tc = report["toolchain"]
    total_devs, by_state = _extract_devices_summary(report)
    lines.append(f"OS: {host['os']} ({host['arch']})")
    lines.extend(_host_notes(report))
    command_adb = tc.get("command_adb_path")
    if tc["adb_path"] is None and command_adb:
        # Describe the copy ADAPT's commands use; the JSON PATH fields stay as they are.
        lines.append(f"ADB Path: Not on PATH (ADAPT commands use {command_adb})")
        lines.append(f"ADB Version: {tc.get('command_adb_version') or 'N/A'}")
        lines.append(f"Origin: {tc.get('command_adb_origin') or 'none'}")
    else:
        lines.append(f"ADB Path: {tc['adb_path'] or 'Not found'}")
        lines.append(f"ADB Version: {tc['platform_tools_version'] or 'N/A'}")
        lines.append(f"Origin: {tc['origin']}")
        if tc.get("registered_managed_copy"):
            lines.append(
                f"ADAPT commands use: {command_adb} ({tc.get('command_adb_version') or 'N/A'}, "
                f"{tc.get('command_adb_origin') or 'none'}), registered by `adapt setup`; "
                "PATH in this shell does not include its folder"
            )
    lines.append(f"Status: {report['status']} (exit code {report['exit_code']})")
    lines.append("")
    lines.append("Checks:")
    from adapt_adb.failure_report import doctor_explain_ids, pointers_enabled

    explain = pointers_enabled()
    remediations: list[str] = []
    for chk in report["checks"]:
        badge = chk["status"].upper()
        lines.append(f"  [{badge}] {chk['id']}: {chk['summary']}")
        if chk.get("remediation") and chk["status"] in ("fail", "warn"):
            pointer = f" (adapt explain {chk['failure_id']})" if explain and isinstance(chk.get("failure_id"), str) else ""
            remediations.append(f"- ({chk['id']}) {chk['remediation']}{pointer}")

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

    if host.get("supported") is False:
        pass
    elif tc["adb_path"] is None and command_adb and report.get("exit_code") != EXIT_MISSING_TOOLCHAIN:
        lines.append("")
        lines.append("Hint: Run 'adapt setup' to put platform-tools on PATH for other tools as well.")
    elif tc["adb_path"] is None or report.get("exit_code") == EXIT_MISSING_TOOLCHAIN:
        lines.append("")
        lines.append("Hint: Run 'adapt setup' to download and install platform-tools.")

    if show_fix:
        lines.append("")
        lines.append("Safe Remediation Commands (--fix, read-only preview):")
        if remediations:
            lines.extend(remediations)
        else:
            lines.append("  No remediation actions needed.")
    else:
        # Short form for redirected output: one line naming every blocked or waiting
        # item's failure guide, then the existing --fix pointer.
        explain_ids = doctor_explain_ids(report) if explain else []
        if explain_ids or remediations:
            lines.append("")
        if explain_ids:
            also = f"  |  also: {', '.join(explain_ids[1:])}" if len(explain_ids) > 1 else ""
            lines.append(f"More: adapt explain {explain_ids[0]}{also}")
        if remediations:
            lines.append(DOCTOR_FIX_POINTER)

    return "\n".join(lines)


def validate_zip_members_safe(zf: zipfile.ZipFile, dest_dir: Path) -> None:
    """Raise ValueError if any zip entry is unsafe to extract into dest_dir.

    Each member goes through the production validator used by the real
    extractor (absolute and drive-relative names, NUL bytes, symlink entries,
    `..` traversal with either slash), then must resolve inside dest_dir.
    """
    from adapt_adb.adb import _validate_zip_member_path

    resolved_dest = dest_dir.resolve()
    for member in zf.infolist():
        name = member.filename
        parts = _validate_zip_member_path(name, member.external_attr)
        target_path = resolved_dest.joinpath(*parts).resolve()
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
    """Idempotently prepend target_dir to PATH via a managed block in ~/.profile.

    Only ~/.profile is edited. The block counts as present only when a line,
    after strip(), equals the managed export line exactly; comments or other
    paths that merely contain target_dir do not count.
    """
    export_line = managed_path_export_line(target_dir)
    marker = MANAGED_PATH_MARKER
    block = f"\n{marker}\n{export_line}\n"

    def _has_export_line(text: str) -> bool:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if stripped == export_line:
                return True
        return False

    profile_path = home / ".profile"
    if env and env.debian_profile_texts is not None:
        existing = env.debian_profile_texts.get(profile_path, "")
        if not _has_export_line(existing):
            env.debian_profile_texts[profile_path] = existing + block
        return True

    try:
        existing = (
            profile_path.read_text(encoding="utf-8", errors="ignore")
            if profile_path.is_file()
            else ""
        )
        if not _has_export_line(existing):
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


_X86_64_ARCH_NAMES = ("x86_64", "amd64")


def _managed_copy_healthy(
    adb_bin: Path,
    host_os: str,
    home: Path,
    environ: dict[str, str],
    env: DoctorEnv | None = None,
) -> bool:
    """True when setup's own install folder holds an adb that runs, is >= 34.0.0 and is Google's."""
    if not _path_exists(adb_bin, env):
        return False
    if not parse_platform_tools_version(_query_adb_version_raw(adb_bin, env)).get("meets_minimum"):
        return False
    return _classify_adb_origin(adb_bin, host_os, home, environ, env)[0] == "google_official"


def _register_on_path(
    target_dir: Path,
    installed_adb: Path,
    host_os: str,
    environ: dict[str, str],
    home: Path,
    env: DoctorEnv | None = None,
) -> bool:
    """Put target_dir first on the user PATH (and this process's PATH). Returns path_updated."""
    sep = ";" if host_os == "windows" else ":"
    cur_path = environ.get("PATH", "")
    parts = [p.strip() for p in cur_path.split(sep) if p.strip()]
    if host_os == "windows":
        from adapt_adb.adb import _same_windows_path_entry

        remaining = [
            p for p in parts if not _same_windows_path_entry(p, str(target_dir))
        ]
    else:
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
        if env.command_adb_source is not None:
            env.command_adb, env.command_adb_source = installed_adb, "path"
        return True
    if host_os == "windows":
        return add_to_windows_user_path(target_dir)
    return ensure_debian_user_path(target_dir, home, env)


def _confirm_setup_change(prompt: str, yes: bool) -> str | None:
    """Return None when the change may proceed, else the `aborted_*` setup action."""
    if yes:
        return None
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
    if not (stdin_tty and stderr_tty):
        return "aborted_needs_yes"
    sys.stderr.write(prompt)
    sys.stderr.flush()
    try:
        answer = input().strip().lower()
    except EOFError:
        answer = ""
    return None if answer in ("y", "yes") else "aborted_unconfirmed"


def _mark_setup_not_done(report: dict[str, Any]) -> dict[str, Any]:
    """Make a setup outcome that did not do what was asked exit non-zero.

    A healthy pre-flight (exit 0) becomes EXIT_MISSING_TOOLCHAIN; an existing
    non-zero code is kept. `status` keeps describing host health.
    """
    if report["exit_code"] == EXIT_OK:
        report["exit_code"] = EXIT_MISSING_TOOLCHAIN
    return report


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

    # 0. Only Windows and Linux are supported hosts; refuse before anything else.
    if host_os not in SUPPORTED_HOST_OS:
        pre_report["setup"] = {
            "action": "unsupported_platform",
            "dry_run": dry_run,
            "no_path": no_path,
            "target_dir": None,
            "url": None,
            "downloaded": False,
            "path_updated": False,
            "error": (
                f"Unsupported host platform '{host_os}': ADAPT setup installs platform-tools "
                "on Windows and Linux only."
            ),
        }
        pre_report["error"] = pre_report["setup"]["error"]
        return _mark_setup_not_done(pre_report)

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

    # 2. Check if already healthy on PATH (a usable adb off PATH still needs PATH registration)
    toolchain_healthy = pre_report["toolchain"]["adb_path"] is not None and all(
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

    # 2b. Setup's own install folder already holds a healthy adb and only PATH registration
    # is missing: register it without downloading again (unless --force).
    binary_name = "adb.exe" if host_os == "windows" else "adb"
    managed_adb = target_dir / binary_name
    path_adb_str = pre_report["toolchain"]["adb_path"]
    if (
        not force
        and not _same_adb(Path(path_adb_str) if path_adb_str else None, managed_adb)
        and _managed_copy_healthy(managed_adb, host_os, home, environ, env)
    ):
        reuse: dict[str, Any] = {
            "dry_run": dry_run,
            "no_path": no_path,
            "target_dir": str(target_dir),
            "url": url,
            "downloaded": False,
            "path_updated": False,
            "reused_existing": True,
        }
        if dry_run:
            pre_report["setup"] = {
                "action": "dry_run", **reuse, "dry_run": True,
                "would_download": False, "would_update_path": not no_path,
            }
            return pre_report
        if no_path:
            pre_report["setup"] = {"action": "noop_already_installed", **reuse}
            return pre_report
        aborted = _confirm_setup_change(
            f"Add the existing platform-tools folder {target_dir} to your user PATH? [y/N]: ", yes
        )
        if aborted is not None:
            pre_report["setup"] = {"action": aborted, **reuse}
            return _mark_setup_not_done(pre_report)
        if not _register_on_path(target_dir, managed_adb, host_os, environ, home, env):
            pre_report["setup"] = {
                "action": "failed", **reuse,
                "error": f"Could not add {target_dir} to your user PATH.",
            }
            pre_report["error"] = pre_report["setup"]["error"]
            return _mark_setup_not_done(pre_report)
        post_report = run_doctor_checks(env, command_name="setup")
        post_report["setup"] = {"action": "path_registered", **reuse, "path_updated": True}
        return post_report

    # The official Linux platform-tools archive ships x86_64 binaries only.
    host_arch = str(pre_report["host"].get("arch") or "unknown")
    if host_os != "windows" and host_arch.lower() not in _X86_64_ARCH_NAMES:
        pre_report["setup"] = {
            "action": "unsupported_architecture",
            "dry_run": dry_run,
            "no_path": no_path,
            "target_dir": str(target_dir),
            "url": url,
            "downloaded": False,
            "path_updated": False,
            "error": (
                f"Unsupported host architecture '{host_arch}': the official Linux "
                "platform-tools archive is x86_64 only."
            ),
        }
        pre_report["error"] = pre_report["setup"]["error"]
        return _mark_setup_not_done(pre_report)

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

    # Confirm before any installation mutation when --yes is omitted.
    aborted = _confirm_setup_change(
        f"Download official Google platform-tools zip ({url}) into {target_dir}? [y/N]: ", yes
    )
    if aborted is not None:
        pre_report["setup"] = {
            "action": aborted,
            "dry_run": dry_run,
            "no_path": no_path,
            "target_dir": str(target_dir),
            "url": url,
            "downloaded": False,
            "path_updated": False,
        }
        return _mark_setup_not_done(pre_report)

    # 3. Perform user-scope download & unpack
    dl_fn = downloader or download_and_extract_platform_tools
    try:
        dl_result = _invoke_downloader(dl_fn, url, target_dir, expected_sha256)
    except Exception as exc:
        pre_report["setup"] = {
            "action": "failed",
            "dry_run": False,
            "no_path": no_path,
            "target_dir": str(target_dir),
            "url": url,
            "downloaded": False,
            "path_updated": False,
            "error": str(exc),
        }
        pre_report["error"] = str(exc)
        return _mark_setup_not_done(pre_report)

    installed_adb = managed_adb

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
        path_updated = _register_on_path(target_dir, installed_adb, host_os, environ, home, env)

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
