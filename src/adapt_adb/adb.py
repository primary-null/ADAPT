"""ADB resolution, device enumeration, and subprocess management for ADAPT."""

from adapt_adb.progress import operation

import hashlib
import hmac
import logging
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
import warnings
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

logger = logging.getLogger(__name__)

PLATFORM_TOOLS_URL_WINDOWS = (
    "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"
)
PLATFORM_TOOLS_URL_LINUX = (
    "https://dl.google.com/android/repository/platform-tools-latest-linux.zip"
)
PLATFORM_TOOLS_REPOSITORY_XML_URL = (
    "https://dl.google.com/android/repository/repository2-3.xml"
)
_ALLOWED_DOWNLOAD_HOSTS = frozenset({"dl.google.com"})
_ALLOWED_MANIFEST_HOSTS = frozenset({"dl.google.com", "developer.android.com"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_WIN_ENV_VAR_RE = re.compile(r"%([^%]+)%")
_MIN_PLATFORM_TOOLS_VERSION = (34, 0, 0)
_PLATFORM_TOOLS_VERSION_RE = re.compile(
    r"^Version\s+(\d+)\.(\d+)\.(\d+)(?:-[0-9A-Za-z._-]+)?(?:\s|$)",
    re.IGNORECASE,
)
_MAX_PLATFORM_TOOLS_ZIP_BYTES = 256 * 1024 * 1024
_MAX_REPOSITORY_XML_BYTES = 16 * 1024 * 1024
_DOWNLOAD_CHUNK_SIZE = 65536
_HTTP_TIMEOUT_SECONDS = 30


class AdbProbeError(RuntimeError):
    """Raised when a read-only ADB probe did not complete successfully."""


@dataclass(frozen=True)
class AdbPrefix:
    """Parsed ADB global options and opaque command operands."""

    prefix: list[str]
    command: list[str]
    serial: str | None = None
    selector: str | None = None


def get_default_platform_tools_dir() -> Path:
    """Return default platform-tools directory based on current OS."""
    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA")
        base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
        return base / "Android" / "platform-tools"
    return Path.home() / ".local" / "share" / "android" / "platform-tools"


def get_search_locations() -> list[tuple[str, Path | None]]:
    """Return ordered list of (description, candidate_path) to check for adb."""
    binary_name = "adb.exe" if sys.platform == "win32" else "adb"
    locations: list[tuple[str, Path | None]] = []

    # 1. PATH
    path_bin = shutil.which("adb")
    locations.append(("PATH", Path(path_bin).resolve() if path_bin else None))

    # 2. ANDROID_HOME / ANDROID_SDK_ROOT
    android_home = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if android_home:
        candidate = Path(android_home) / "platform-tools" / binary_name
        locations.append(("ANDROID_HOME/platform-tools", candidate))
    else:
        locations.append(("ANDROID_HOME/platform-tools", None))

    # 3. Windows LOCALAPPDATA Android Sdk / platform-tools
    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA")
        base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
        locations.append(("LOCALAPPDATA Android platform-tools", base / "Android" / "platform-tools" / binary_name))
        locations.append(("LOCALAPPDATA Android Sdk", base / "Android" / "Sdk" / "platform-tools" / binary_name))

    # 4. Linux ~/.local/share/android/platform-tools
    linux_default = Path.home() / ".local" / "share" / "android" / "platform-tools" / binary_name
    locations.append(("~/.local/share/android/platform-tools", linux_default))

    return locations


def _candidate_meets_min_version(adb_path: Path) -> bool:
    """Return True if candidate adb binary reports platform-tools >= 34.0.0."""
    try:
        proc = subprocess.run(
            [str(adb_path), "version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        output = f"{proc.stdout or ''}\n{proc.stderr or ''}"
        for line in output.splitlines():
            match = _PLATFORM_TOOLS_VERSION_RE.match(line.strip())
            if match:
                ver_tuple = (
                    int(match.group(1)),
                    int(match.group(2)),
                    int(match.group(3)),
                )
                return ver_tuple >= _MIN_PLATFORM_TOOLS_VERSION
    except (subprocess.SubprocessError, OSError):
        return False
    return False


def resolve_adb() -> Path | None:
    """Resolve the adb executable from known locations.

    Checks ADAPT_ADB / ADB_PATH overrides first, then prefers the first candidate
    whose version is >= 34.0.0; falls back to the first found executable if none meet 34.0.0.
    Never downloads and never kills server.
    """
    for env_var in ("ADAPT_ADB", "ADB_PATH"):
        val = os.environ.get(env_var, "").strip()
        if val:
            cand = Path(val).expanduser()
            if cand.is_file():
                return cand.resolve()

    valid_candidates: list[Path] = []
    seen: set[Path] = set()
    for _, candidate in get_search_locations():
        if candidate and candidate.is_file() and os.access(candidate, os.X_OK):
            resolved = candidate.resolve()
            if resolved not in seen:
                seen.add(resolved)
                valid_candidates.append(resolved)

    for cand in valid_candidates:
        if _candidate_meets_min_version(cand):
            return cand

    return valid_candidates[0] if valid_candidates else None


def get_adb_version(adb_path: Path) -> str | None:
    """Run `adb version` and return the first line of output."""
    try:
        proc = subprocess.run(
            [str(adb_path), "version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        for line in proc.stdout.splitlines():
            stripped = line.strip()
            if stripped:
                return stripped
        for line in proc.stderr.splitlines():
            stripped = line.strip()
            if stripped:
                return stripped
    except (subprocess.SubprocessError, OSError):
        return None
    return None


def parse_devices_short(output: str) -> list[dict[str, str]]:
    """Parse the TAB-delimited identity records emitted by ``adb devices``.

    Serial identity is everything before the first tab.  This is deliberately
    stricter than whitespace splitting because wireless mDNS serials can
    contain spaces.
    """
    devices: list[dict[str, str]] = []
    for raw_line in (output or "").splitlines():
        if not raw_line or raw_line.startswith("*") or raw_line.startswith("List of devices"):
            continue
        serial, separator, status = raw_line.partition("\t")
        if not separator or not serial:
            raise ValueError("Malformed adb device record: expected TAB-delimited serial and state")
        status = status.strip()
        if not status:
            raise ValueError("Malformed adb device record: missing state")
        state = "no permissions" if status.startswith("no permissions") else status.split(" ", 1)[0]
        devices.append({"serial": serial, "state": state})
    return devices


def enrich_devices_long(
    identities: list[dict[str, str]], output: str
) -> list[dict[str, str]]:
    """Add ``devices -l`` metadata without allowing it to redefine identity."""
    result = [dict(item) for item in identities]
    by_serial = {item["serial"]: item for item in result}
    for raw_line in (output or "").splitlines():
        if not raw_line or raw_line.startswith("*") or raw_line.startswith("List of devices"):
            continue
        serial, separator, status_and_meta = raw_line.partition("\t")
        if not separator:
            # Official `devices -l` may space-pad its columns. Identities still
            # come ONLY from the authoritative short TAB-delimited listing.
            # Match a known full serial and its already-observed state; never
            # split an opaque serial on spaces or add an identity from metadata.
            matches = []
            for known, device in by_serial.items():
                if raw_line.startswith(known + " "):
                    rest = raw_line[len(known):].lstrip(" ")
                    state = device["state"]
                    if rest == state or rest.startswith(state + " "):
                        matches.append((known, rest))
            if len(matches) != 1:
                continue
            serial, status_and_meta = matches[0]
            separator = " "
        if not separator or serial not in by_serial:
            continue
        fields = status_and_meta.strip().split()
        if not fields:
            continue
        meta_start = 2 if fields[:2] == ["no", "permissions"] else 1
        meta: dict[str, str] = {}
        for token in fields[meta_start:]:
            if ":" in token:
                key, value = token.split(":", 1)
                meta[key] = value
        item = by_serial[serial]
        item["model"] = meta.get("model", "").replace("_", " ")
        item["product"] = meta.get("product", "")
        item["transport"] = (
            meta.get("transport")
            or ("local" if serial.startswith("emulator-") else "usb")
        )
    return result


def get_connected_devices(adb_path: Path) -> list[dict[str, str]]:
    """Return a list of attached devices parsed from `adb devices`.

    Never kills server.
    """
    try:
        proc = subprocess.run(
            [str(adb_path), "devices"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except subprocess.TimeoutExpired as exc:
        raise AdbProbeError("ADB device enumeration timed out") from exc
    except OSError as exc:
        raise AdbProbeError("Could not execute ADB device enumeration") from exc
    if proc.returncode != 0:
        raise AdbProbeError(f"ADB device enumeration failed (exit {proc.returncode})")
    try:
        return parse_devices_short(proc.stdout or "")
    except ValueError as exc:
        raise AdbProbeError("ADB returned a malformed device listing") from exc


def has_serial_flag(args: list[str]) -> bool:
    """Check whether the ADB *global prefix* specifies a target serial."""
    return split_adb_prefix(args).serial is not None


_HOST_ONLY_ADB_COMMANDS = frozenset(
    {
        "version",
        "help",
        "start-server",
        "devices",
        "connect",
        "disconnect",
        "pair",
        "mdns",
    }
)
_ADB_VALUE_FLAGS = frozenset({"-H", "-P", "-L", "-s", "-t", "--serial"})


def split_adb_prefix(args: list[str]) -> AdbPrefix:
    """Parse and normalize ADB global options before the command.

    Command operands remain opaque, so flags belonging to ``shell`` or
    ``logcat`` cannot be mistaken for device selectors.
    """
    prefix: list[str] = []
    serial: str | None = None
    selector: str | None = None
    i = 0
    while i < len(args) and str(args[i]).startswith("-"):
        flag = str(args[i])
        value: str | None = None
        normalized = flag
        if flag.startswith("--serial="):
            normalized = "--serial"
            value = flag.split("=", 1)[1]
            i += 1
        elif flag.startswith("-s") and flag != "-s":
            normalized = "-s"
            value = flag[2:]
            i += 1
        elif flag in _ADB_VALUE_FLAGS:
            if (
                i + 1 >= len(args)
                or not str(args[i + 1])
                or str(args[i + 1]).startswith("-")
            ):
                raise ValueError(f"Missing value for ADB global option {flag}")
            value = str(args[i + 1])
            i += 2
        elif flag in {"-d", "-e", "-a", "--exit-on-write-error"}:
            if flag in {"-d", "-e"}:
                if selector is not None:
                    raise ValueError("Conflicting ADB target selectors")
                selector = flag
            prefix.append(flag)
            i += 1
            continue
        else:
            raise ValueError(f"Unsupported ADB global option: {flag}")

        assert value is not None
        if normalized in {"-s", "--serial"}:
            if selector is not None or serial is not None or not value:
                raise ValueError("Empty, duplicate, or conflicting serial selector")
            serial = value
            selector = "-s"
            prefix.extend(["-s", value])
        elif normalized == "-t":
            if selector is not None:
                raise ValueError("Conflicting ADB target selectors")
            selector = "-t"
            prefix.extend([normalized, value])
        else:
            prefix.extend([normalized, value])

    return AdbPrefix(prefix=prefix, command=[str(a) for a in args[i:]], serial=serial, selector=selector)


def is_host_only_adb_command(args: list[str]) -> bool:
    """Return True when the primary adb subcommand is host-only and needs no device serial."""
    parsed = split_adb_prefix(args)
    return bool(parsed.command and parsed.command[0] in _HOST_ONLY_ADB_COMMANDS)


def _expand_windows_env_vars(value: str) -> str:
    """Expand both %VAR% and standard environment variables across platforms."""

    def _replace_percent(match: re.Match[str]) -> str:
        var_name = match.group(1)
        if var_name in os.environ:
            return os.environ[var_name]
        for k, v in os.environ.items():
            if k.upper() == var_name.upper():
                return v
        return match.group(0)

    expanded = _WIN_ENV_VAR_RE.sub(_replace_percent, value)
    return os.path.expandvars(expanded)


def _same_windows_path_entry(entry: str, target_str: str) -> bool:
    """Compare two Windows PATH entries case-insensitively with env-var expansion."""
    exp_entry = _expand_windows_env_vars(entry.strip())
    exp_target = _expand_windows_env_vars(target_str.strip())
    norm_entry = exp_entry.replace("\\", "/").rstrip("/").lower()
    norm_target = exp_target.replace("\\", "/").rstrip("/").lower()
    if norm_entry == norm_target:
        return True
    return os.path.normcase(os.path.normpath(exp_entry)).lower() == os.path.normcase(
        os.path.normpath(exp_target)
    ).lower()


def _dir_contains_adb_exe(raw_entry: str) -> bool:
    """Return True if an expanded PATH directory entry contains adb.exe."""
    expanded = _expand_windows_env_vars(raw_entry.strip())
    if not expanded:
        return False
    try:
        return (Path(expanded) / "adb.exe").is_file()
    except OSError:
        return False


def add_to_windows_user_path(target_dir: Path) -> bool:
    """Add target_dir to HKCU\\Environment\\Path, prepending if shadowed by another adb.exe."""
    if sys.platform != "win32":
        return False
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Environment",
            0,
            winreg.KEY_READ | winreg.KEY_SET_VALUE,
        )
    except OSError:
        return False

    try:
        try:
            current_path, reg_type = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current_path, reg_type = "", winreg.REG_EXPAND_SZ

        current_path = current_path or ""
        parts = [p.strip() for p in current_path.split(";") if p.strip()]
        target_str = str(target_dir)

        existing_idx = next(
            (idx for idx, p in enumerate(parts) if _same_windows_path_entry(p, target_str)),
            None,
        )

        if existing_idx is not None:
            shadowed_earlier = any(
                _dir_contains_adb_exe(p) for p in parts[:existing_idx]
            )
            if not shadowed_earlier:
                return True
            remaining = [p for p in parts if not _same_windows_path_entry(p, target_str)]
            new_path = ";".join([target_str, *remaining])
        else:
            has_shadowing_adb = any(_dir_contains_adb_exe(p) for p in parts)
            if has_shadowing_adb:
                new_path = ";".join([target_str, *parts])
            else:
                new_path = ";".join([*parts, target_str]) if parts else target_str

        winreg.SetValueEx(key, "Path", 0, reg_type, new_path)

        # Broadcast WM_SETTINGCHANGE if possible so open apps/shells can notice
        try:
            import ctypes

            hwnd_broadcast = 0xFFFF
            wm_settingchange = 0x001A
            smto_abortifhung = 0x0002
            res = ctypes.c_ulong()
            ctypes.windll.user32.SendMessageTimeoutW(
                hwnd_broadcast,
                wm_settingchange,
                0,
                "Environment",
                smto_abortifhung,
                5000,
                ctypes.byref(res),
            )
        except Exception:
            pass

        return True
    finally:
        winreg.CloseKey(key)


def _validate_google_https_url(
    url: str,
    allowed_hosts: frozenset[str] = _ALLOWED_DOWNLOAD_HOSTS,
) -> None:
    """Ensure URL uses HTTPS and points to an allowed Google host."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() != "https":
        raise ValueError(f"Platform-tools URL must use HTTPS, got: {url!r}")
    host = (parsed.hostname or "").lower()
    if host not in allowed_hosts:
        raise ValueError(
            f"Platform-tools URL host {host!r} is not in allowed hosts {sorted(allowed_hosts)}."
        )
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Platform-tools URL must not contain credentials.")
    if parsed.port not in (None, 443):
        raise ValueError("Platform-tools URL must use the default HTTPS port.")


class GoogleRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject any redirect hop that leaves the configured HTTPS host allowlist."""

    def __init__(self, hosts: frozenset[str]):
        super().__init__()
        self.hosts = hosts

    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> Any:
        target = urllib.parse.urljoin(req.full_url, newurl)
        _validate_google_https_url(target, self.hosts)
        return super().redirect_request(req, fp, code, msg, headers, target)


def _open_google_url(url: str, hosts: frozenset[str]) -> Any:
    _validate_google_https_url(url, hosts)
    opener = urllib.request.build_opener(GoogleRedirectHandler(hosts))
    return opener.open(url, timeout=_HTTP_TIMEOUT_SECONDS)


def normalize_sha256(digest: str) -> str:
    """Validate and normalize a SHA-256 hex digest string."""
    cleaned = digest.strip().lower()
    if cleaned.startswith("sha256:"):
        cleaned = cleaned[len("sha256:") :].strip()
    if not _SHA256_RE.fullmatch(cleaned):
        raise ValueError(f"Invalid SHA-256 digest: {digest!r}")
    return cleaned


def normalize_sha1(digest: str) -> str:
    """Validate and normalize a SHA-1 hex digest string."""
    cleaned = digest.strip().lower()
    if cleaned.startswith("sha1:"):
        cleaned = cleaned[len("sha1:") :].strip()
    if not _SHA1_RE.fullmatch(cleaned):
        raise ValueError(f"Invalid SHA-1 digest: {digest!r}")
    return cleaned


def _ensure_not_in_android_home(target_dir: Path) -> None:
    """Refuse to write into or replace ANDROID_HOME or ANDROID_SDK_ROOT."""
    target_resolved = target_dir.expanduser().resolve()
    for var_name in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        raw_val = os.environ.get(var_name, "").strip()
        if not raw_val:
            continue
        sdk_resolved = Path(raw_val).expanduser().resolve()
        if (
            target_resolved == sdk_resolved
            or target_resolved.is_relative_to(sdk_resolved)
            or sdk_resolved.is_relative_to(target_resolved)
        ):
            raise ValueError(
                f"Refusing to write into {var_name} ({sdk_resolved}); "
                f"target directory {target_resolved} overlaps with configured Android SDK."
            )


def _xml_local_name(tag: str) -> str:
    """Return local XML element name without namespace prefix."""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _infer_host_os_from_url(url: str) -> str | None:
    """Infer Android repository host-os tag from a platform-tools zip URL."""
    filename = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1].lower()
    if "windows" in filename:
        return "windows"
    if "linux" in filename:
        return "linux"
    if "darwin" in filename or "macosx" in filename or "mac" in filename:
        return "macosx"
    return None


def _extract_digest_from_repo_xml(xml_bytes: bytes, url: str) -> tuple[str, str] | None:
    """Parse Android SDK repository XML for a platform-tools SHA-256 (preferred) or SHA-1 digest."""
    if b"<!DOCTYPE" in xml_bytes or b"<!ENTITY" in xml_bytes:
        raise ValueError("Repository XML contains forbidden DOCTYPE or ENTITY declaration.")

    root = ET.fromstring(xml_bytes)
    target_filename = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1].lower()
    target_host_os = _infer_host_os_from_url(url)

    packages: list[ET.Element] = []
    for elem in root.iter():
        if _xml_local_name(elem.tag) == "remotePackage" and elem.attrib.get("path") == "platform-tools":
            packages.append(elem)

    def _is_stable_package(pkg: ET.Element) -> bool:
        channel_refs = [
            child.attrib.get("ref", "").strip()
            for child in pkg
            if _xml_local_name(child.tag) == "channelRef"
        ]
        return not channel_refs or all(ref == "channel-0" for ref in channel_refs)

    packages.sort(key=lambda pkg: 0 if _is_stable_package(pkg) else 1)

    parsed_archives: list[tuple[bool, str | None, str, str | None, str | None]] = []
    for pkg in packages:
        is_stable = _is_stable_package(pkg)
        for archive in pkg.iter():
            if _xml_local_name(archive.tag) != "archive":
                continue

            host_os_val: str | None = None
            complete_elem: ET.Element | None = None
            for child in archive:
                lname = _xml_local_name(child.tag)
                if lname == "host-os" and child.text:
                    host_os_val = child.text.strip().lower()
                elif lname == "complete":
                    complete_elem = child

            if complete_elem is None:
                continue

            archive_url = ""
            sha256_candidate: str | None = None
            sha1_candidate: str | None = None
            for child in complete_elem:
                lname = _xml_local_name(child.tag)
                if lname == "url" and child.text:
                    archive_url = child.text.strip().rsplit("/", 1)[-1].lower()
                elif lname == "checksum" and child.text:
                    csum_type = child.attrib.get("type", "").strip().lower()
                    if csum_type == "sha256":
                        sha256_candidate = child.text.strip()
                    elif csum_type == "sha1":
                        sha1_candidate = child.text.strip()
                elif lname == "sha256" and child.text:
                    sha256_candidate = child.text.strip()
                elif lname == "sha1" and child.text:
                    sha1_candidate = child.text.strip()

            parsed_archives.append(
                (is_stable, host_os_val, archive_url, sha256_candidate, sha1_candidate)
            )

    # Pass A — exact filename match (channel does not matter)
    if target_filename:
        exact_sha1_fallback: str | None = None
        for _, _, archive_url, sha256_candidate, sha1_candidate in parsed_archives:
            if archive_url == target_filename:
                if sha256_candidate:
                    return ("sha256", normalize_sha256(sha256_candidate))
                if sha1_candidate and exact_sha1_fallback is None:
                    exact_sha1_fallback = normalize_sha1(sha1_candidate)
        if exact_sha1_fallback is not None:
            return ("sha1", exact_sha1_fallback)

    # Pass B — stable channel only (channel-0 or no channelRef)
    stable_sha1_fallback: str | None = None
    for is_stable, host_os_val, archive_url, sha256_candidate, sha1_candidate in parsed_archives:
        if not is_stable:
            continue
        matches_host = (
            (target_host_os is not None and host_os_val == target_host_os)
            or (target_host_os is not None and archive_url.endswith(f"-{target_host_os}.zip"))
        )
        if matches_host:
            if sha256_candidate:
                return ("sha256", normalize_sha256(sha256_candidate))
            if sha1_candidate and stable_sha1_fallback is None:
                stable_sha1_fallback = normalize_sha1(sha1_candidate)

    if stable_sha1_fallback is not None:
        return ("sha1", stable_sha1_fallback)
    return None


def _extract_sha256_from_repo_xml(xml_bytes: bytes, url: str) -> str | None:
    """Parse Android SDK repository XML for a platform-tools SHA-256 digest."""
    res = _extract_digest_from_repo_xml(xml_bytes, url)
    if res is not None and res[0] == "sha256":
        return res[1]
    return None


def fetch_platform_tools_digest(
    url: str,
    manifest_url: str | None = PLATFORM_TOOLS_REPOSITORY_XML_URL,
) -> tuple[str, str] | None:
    """Fetch a documented ('sha256', hex) or ('sha1', hex) digest from Google's SDK repository XML."""
    if not manifest_url:
        return None
    _validate_google_https_url(manifest_url, _ALLOWED_MANIFEST_HOSTS)

    try:
        with _open_google_url(manifest_url, _ALLOWED_MANIFEST_HOSTS) as resp:
            xml_bytes = resp.read(_MAX_REPOSITORY_XML_BYTES + 1)
        if len(xml_bytes) > _MAX_REPOSITORY_XML_BYTES:
            logger.warning("Repository manifest exceeded size limit; ignoring.")
            return None
        return _extract_digest_from_repo_xml(xml_bytes, url)
    except (urllib.error.URLError, OSError, TimeoutError, ET.ParseError, ValueError) as exc:
        logger.debug("Could not fetch digest from %s: %s", manifest_url, exc)
        return None


def fetch_platform_tools_sha256(
    url: str,
    manifest_url: str | None = PLATFORM_TOOLS_REPOSITORY_XML_URL,
) -> str | None:
    """Attempt to fetch a documented SHA-256 digest from Google's SDK repository XML.

    Returns the normalized SHA-256 hex digest if available, or None if unavailable.
    """
    res = fetch_platform_tools_digest(url, manifest_url=manifest_url)
    if res is not None and res[0] == "sha256":
        return res[1]
    return None


def _download_to_path_with_digests(url: str, dest_path: Path) -> tuple[str, str]:
    """Stream URL to dest_path using urlopen and return (sha256_hex, sha1_hex)."""
    sha256_hasher = hashlib.sha256()
    sha1_hasher = hashlib.sha1()
    total_bytes = 0

    with _open_google_url(url, _ALLOWED_DOWNLOAD_HOSTS) as resp:
        with open(dest_path, "wb") as out_file:
            while True:
                chunk = resp.read(_DOWNLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > _MAX_PLATFORM_TOOLS_ZIP_BYTES:
                    raise ValueError(
                        f"Downloaded archive exceeded {_MAX_PLATFORM_TOOLS_ZIP_BYTES} byte limit."
                    )
                sha256_hasher.update(chunk)
                sha1_hasher.update(chunk)
                out_file.write(chunk)

    return sha256_hasher.hexdigest(), sha1_hasher.hexdigest()


def _download_to_path_with_sha256(url: str, dest_path: Path) -> str:
    """Stream URL to dest_path using urlopen and return its SHA-256 hex digest."""
    actual_sha256, _ = _download_to_path_with_digests(url, dest_path)
    return actual_sha256


def _validate_zip_member_path(raw_name: str, external_attr: int) -> list[str]:
    """Validate a zip member path against zip-slip and symlink attacks."""
    if not raw_name or "\x00" in raw_name:
        raise ValueError(f"Unsafe zip entry path: {raw_name!r}")

    if raw_name.startswith(("/", "\\")) or (len(raw_name) >= 2 and raw_name[1] == ":"):
        raise ValueError(f"Unsafe absolute zip entry path (zip-slip): {raw_name!r}")

    if PureWindowsPath(raw_name).is_absolute():
        raise ValueError(f"Unsafe absolute zip entry path (zip-slip): {raw_name!r}")

    unix_mode = (external_attr >> 16) & 0o170000
    if unix_mode == stat.S_IFLNK:
        raise ValueError(f"Refusing to extract symlink from zip archive: {raw_name!r}")

    parts = re.split(r"[/\\]+", raw_name)
    for part in parts:
        if part == "..":
            raise ValueError(f"Unsafe zip entry path traversal (zip-slip): {raw_name!r}")

    return [p for p in parts if p and p != "."]


def _safe_extract_zip(zip_path: Path, staging_dir: Path) -> None:
    """Validate all zip entries for zip-slip and unpack safely into staging_dir."""
    staging_resolved = staging_dir.resolve()
    with zipfile.ZipFile(zip_path, "r") as zf:
        infos = zf.infolist()
        validated: list[tuple[zipfile.ZipInfo, list[str]]] = [
            (member, _validate_zip_member_path(member.filename, member.external_attr))
            for member in infos
        ]

        non_empty_parts = [parts for _, parts in validated if parts]
        strip_prefix = bool(non_empty_parts) and all(
            parts[0] == "platform-tools" for parts in non_empty_parts
        )

        for member, parts in validated:
            rel_parts = parts[1:] if strip_prefix and parts else parts
            if not rel_parts:
                continue

            dest_path = staging_dir.joinpath(*rel_parts).resolve()
            if dest_path == staging_resolved or not dest_path.is_relative_to(staging_resolved):
                raise ValueError(
                    f"Unsafe zip entry resolves outside staging directory: {member.filename!r}"
                )

            if member.is_dir():
                dest_path.mkdir(parents=True, exist_ok=True)
                continue

            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member, "r") as src_file, open(dest_path, "wb") as dst_file:
                shutil.copyfileobj(src_file, dst_file)

            if sys.platform != "win32":
                perm_bits = (member.external_attr >> 16) & 0o777
                if perm_bits:
                    os.chmod(dest_path, perm_bits)

    binary_name = "adb.exe" if sys.platform == "win32" else "adb"
    bin_path = staging_dir / binary_name
    if bin_path.exists() and sys.platform != "win32":
        bin_path.chmod(0o755)


def _atomic_swap_dir(staging_dir: Path, target_dir: Path) -> None:
    """Atomically swap staging_dir into target_dir within the same parent directory."""
    if not target_dir.exists() and not target_dir.is_symlink():
        staging_dir.replace(target_dir)
        return

    backup_dir = target_dir.parent / f".{target_dir.name}.bak-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    target_dir.replace(backup_dir)
    try:
        staging_dir.replace(target_dir)
    except Exception:
        if backup_dir.exists() and not target_dir.exists():
            try:
                backup_dir.replace(target_dir)
            except OSError:
                pass
        raise
    else:
        shutil.rmtree(backup_dir, ignore_errors=True)


def _locate_staged_adb(staging_dir: Path) -> Path | None:
    """Locate the extracted adb executable in staging_dir (root or platform-tools/)."""
    names = ("adb.exe", "adb") if sys.platform == "win32" else ("adb", "adb.exe")
    candidates = [
        *(staging_dir / name for name in names),
        *(staging_dir / "platform-tools" / name for name in names),
    ]
    for candidate in candidates:
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    return None


def _verify_staged_adb_version(staging_dir: Path) -> tuple[int, int, int]:
    """Run `<staging>/adb version` and require platform-tools >= 34.0.0 before swap."""
    adb_bin = _locate_staged_adb(staging_dir)
    if adb_bin is None:
        raise RuntimeError(
            f"Extracted platform-tools archive in {staging_dir} does not contain an adb executable."
        )

    if sys.platform != "win32":
        try:
            adb_bin.chmod(adb_bin.stat().st_mode | 0o755)
        except OSError:
            pass

    try:
        proc = subprocess.run(
            [str(adb_bin), "version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise RuntimeError(
            f"Failed to execute staged adb binary {adb_bin} to verify version: {exc}"
        ) from exc

    if proc.returncode != 0:
        raise RuntimeError(
            f"Staged adb binary {adb_bin} exited with code {proc.returncode} during version check."
        )

    output = proc.stdout or ""
    for line in output.splitlines():
        match = _PLATFORM_TOOLS_VERSION_RE.match(line.strip())
        if match:
            version_tuple = (
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)),
            )
            if version_tuple < _MIN_PLATFORM_TOOLS_VERSION:
                raise RuntimeError(
                    f"Staged adb version {version_tuple[0]}.{version_tuple[1]}.{version_tuple[2]} "
                    "is below minimum required 34.0.0; refusing to replace target directory."
                )
            return version_tuple

    raise RuntimeError(
        f"Could not parse platform-tools version >= 34.0.0 from staged adb output: {output!r}"
    )


@operation("Installing Platform-Tools")
def download_and_extract_platform_tools(
    url: str,
    target_dir: Path | None = None,
    expected_sha256: str | None = None,
    allow_unverified: bool = False,
    manifest_url: str | None = PLATFORM_TOOLS_REPOSITORY_XML_URL,
) -> str:
    """Download platform-tools zip, verify digest and zip paths, and unpack atomically.

    Args:
        url: Official HTTPS Google platform-tools download URL.
        target_dir: Destination directory (defaults to user-scope default dir).
        expected_sha256: Optional SHA-256 hex digest to verify against.
        allow_unverified: If True, warn and continue when no digest is available.
            Note: checksum mismatches ALWAYS fail even when allow_unverified=True.
        manifest_url: Documented Google repository XML URL to query when expected_sha256 is None.

    Returns:
        The computed SHA-256 hex digest of the downloaded archive.
    """
    resolved_target = (
        get_default_platform_tools_dir() if target_dir is None else Path(target_dir)
    ).expanduser().resolve()

    _ensure_not_in_android_home(resolved_target)
    _validate_google_https_url(url, _ALLOWED_DOWNLOAD_HOSTS)

    digest_algo: str | None = None
    digest_to_verify: str | None = None
    if expected_sha256 is not None:
        digest_algo = "sha256"
        digest_to_verify = normalize_sha256(expected_sha256)
    else:
        fetched = fetch_platform_tools_digest(url, manifest_url=manifest_url)
        if fetched is not None:
            digest_algo, digest_to_verify = fetched
        else:
            if not allow_unverified:
                raise RuntimeError(
                    "SHA-256 or SHA-1 digest unavailable for platform-tools download; "
                    "pass expected_sha256 or allow_unverified=True to proceed."
                )
            msg = (
                "SHA-256 or SHA-1 digest unavailable for platform-tools download; "
                "proceeding without verification because allow_unverified=True."
            )
            warnings.warn(msg, RuntimeWarning, stacklevel=2)
            logger.warning(msg)

    dest_parent = resolved_target.parent
    dest_parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        dir=dest_parent,
        prefix=".platform-tools-",
        suffix=".zip",
        delete=False,
    ) as tmp_file:
        tmp_zip_path = Path(tmp_file.name)

    staging_dir = Path(
        tempfile.mkdtemp(dir=dest_parent, prefix=".platform-tools-staging-")
    )

    try:
        actual_sha256, actual_sha1 = _download_to_path_with_digests(url, tmp_zip_path)
        if digest_to_verify is not None:
            if digest_algo == "sha1":
                if not hmac.compare_digest(actual_sha1, digest_to_verify):
                    raise ValueError(
                        f"SHA-1 checksum mismatch for {url}: "
                        f"expected {digest_to_verify}, got {actual_sha1}"
                    )
            else:
                if not hmac.compare_digest(actual_sha256, digest_to_verify):
                    raise ValueError(
                        f"SHA-256 checksum mismatch for {url}: "
                        f"expected {digest_to_verify}, got {actual_sha256}"
                    )

        _safe_extract_zip(tmp_zip_path, staging_dir)
        _verify_staged_adb_version(staging_dir)
        _atomic_swap_dir(staging_dir, resolved_target)
        return actual_sha256
    finally:
        if tmp_zip_path.exists():
            try:
                tmp_zip_path.unlink()
            except OSError:
                pass
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
