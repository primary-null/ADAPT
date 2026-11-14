"""Pinned, user-scope scrcpy provisioner for ADAPT.

This module is intentionally independent from the Google-only Platform-Tools
downloader.  It never changes PATH, invokes scrcpy, contacts a device, or
replaces an existing directory.  The public integration surface is
``plan_scrcpy()`` and ``install_scrcpy()``.
"""

from __future__ import annotations

from adapt_adb.failure_report import tagged
from adapt_adb.progress import operation

import ctypes
import errno
import hashlib
import hmac
import json
import os
import platform
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, BinaryIO, Callable


SCRCPY_VERSION = "5.0.1"
SCRCPY_RELEASE_TAG = f"v{SCRCPY_VERSION}"
SCRCPY_RELEASED_AT = "2026-10-08T06:44:27Z"
SCRCPY_TAG_COMMIT = "a60891aea193d92e7e5c3942700eca63f9d19a5f"
SCRCPY_LICENSE_URL = (
    "https://github.com/Genymobile/scrcpy/blob/v5.0.1/LICENSE"
)
SCRCPY_RELEASE_URL = (
    "https://github.com/Genymobile/scrcpy/releases/tag/v5.0.1"
)

_GITHUB_HOST = "github.com"
_ASSET_DELIVERY_HOST = "release-assets.githubusercontent.com"
_MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024
_MAX_EXPANDED_BYTES = 1024 * 1024 * 1024
_MAX_MEMBER_BYTES = 256 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 4096
_MAX_COMPRESSION_RATIO = 200
_MAX_REDIRECTS = 5
_DOWNLOAD_CHUNK_BYTES = 64 * 1024
_HTTP_TIMEOUT_SECONDS = 30
_RECEIPT_NAME = "adapt-install-receipt.json"
_RECEIPT_SCHEMA = "1"
_SERVER_SHA256 = "764eb6f79811d5211fe9df341120882ba9994c7a61b897d7bf3fb662e53bc536"

_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul", "clock$"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
    | {f"com{i}" for i in "¹²³"}
    | {f"lpt{i}" for i in "¹²³"}
)


class ScrcpyInstallError(RuntimeError):
    """Base error for a refused or failed scrcpy provision operation."""


class UnsupportedScrcpyHostError(ScrcpyInstallError):
    """Raised when upstream publishes no approved asset for this host."""

    failure_id = "scrcpy.unsupported_host"


class ScrcpyConfirmationRequiredError(ScrcpyInstallError):
    """Raised when a non-interactive caller omitted explicit ``yes=True``."""

    failure_id = "scrcpy.needs_confirmation"


class ScrcpyInstallConflictError(ScrcpyInstallError):
    """Raised when safe placement would clobber an existing path or install."""

    failure_id = "scrcpy.install_conflict"


class ScrcpyIntegrityError(ScrcpyInstallError):
    """Raised when downloaded or extracted bytes do not match pinned evidence."""

    failure_id = "scrcpy.integrity_failed"


class ScrcpyArchiveSafetyError(ScrcpyInstallError):
    """Raised when an archive violates bounded safe-extraction policy."""

    failure_id = "scrcpy.archive_unsafe"


@dataclass(frozen=True)
class ScrcpyArtifact:
    """One approved upstream artifact for a precise OS and architecture."""

    host_os: str
    host_arch: str
    asset_name: str
    archive_kind: str
    sha256: str
    size: int
    package_dir: str
    executable_name: str
    license_name: str

    @property
    def source_url(self) -> str:
        return (
            "https://github.com/Genymobile/scrcpy/releases/download/"
            f"{SCRCPY_RELEASE_TAG}/{self.asset_name}"
        )

    @property
    def required_files(self) -> tuple[str, str, str]:
        return (self.executable_name, "scrcpy-server", self.license_name)


_ARTIFACTS: dict[tuple[str, str], ScrcpyArtifact] = {}
for _artifact in (
    ScrcpyArtifact(
        "windows",
        "x86_64",
        "scrcpy-win64-v5.0.1.zip",
        "zip",
        "b12a2c4ee8be317422451fc7dcf8ee20a71b5ea7ef9ad73ddd825a25316227a5",
        11_264_634,
        "scrcpy-win64-v5.0.1",
        "scrcpy.exe",
        "LICENSE.txt",
    ),
    ScrcpyArtifact(
        "windows",
        "x86",
        "scrcpy-win32-v5.0.1.zip",
        "zip",
        "0cb65ebf1bce892fa4d1502d699af0c7a1a7f24d7febcb9ef72894e3f23fc2ad",
        10_133_753,
        "scrcpy-win32-v5.0.1",
        "scrcpy.exe",
        "LICENSE.txt",
    ),
    ScrcpyArtifact(
        "windows",
        "arm64",
        "scrcpy-winarm64-v5.0.1.zip",
        "zip",
        "16c3fc2068df64946f670e3504b4752073ebae47a416a45b46e79a5ab19c689b",
        9_825_739,
        "scrcpy-winarm64-v5.0.1",
        "scrcpy.exe",
        "LICENSE.txt",
    ),
    ScrcpyArtifact(
        "linux",
        "x86_64",
        "scrcpy-linux-x86_64-v5.0.1.tar.gz",
        "tar.gz",
        "9f969d30cc574816077edecda65719c1c70b0aee1df1ba5fa00779ac07b8fd6e",
        17_883_742,
        "scrcpy-linux-x86_64-v5.0.1",
        "scrcpy",
        "LICENSE",
    ),
):
    _ARTIFACTS[(_artifact.host_os, _artifact.host_arch)] = _artifact


def _normalize_host(platform_name: str, machine: str) -> tuple[str, str]:
    platform_key = platform_name.strip().lower()
    if platform_key in {"win32", "windows"}:
        host_os = "windows"
    elif platform_key.startswith("linux"):
        host_os = "linux"
    else:
        raise UnsupportedScrcpyHostError(
            "The managed scrcpy provisioner supports only native Windows and Linux; "
            f"host platform {platform_name!r} requires a manual upstream-reviewed path."
        )

    machine_key = machine.strip().lower().replace("-", "_")
    aliases = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "x86_64": "x86_64",
        "i386": "x86",
        "i486": "x86",
        "i586": "x86",
        "i686": "x86",
        "x86": "x86",
        "arm64": "arm64",
        "aarch64": "arm64",
    }
    host_arch = aliases.get(machine_key, machine_key or "unknown")
    if (host_os, host_arch) not in _ARTIFACTS:
        supported = ", ".join(
            sorted(arch for os_name, arch in _ARTIFACTS if os_name == host_os)
        )
        raise UnsupportedScrcpyHostError(
            f"No pinned scrcpy {SCRCPY_VERSION} asset is approved for "
            f"{host_os}/{host_arch}. Supported {host_os} architectures: {supported}. "
            "Refusing to install an asset for a different architecture."
        )
    return host_os, host_arch


def detect_scrcpy_artifact(
    platform_name: str | None = None,
    machine: str | None = None,
) -> ScrcpyArtifact:
    """Select the exact pinned artifact for this native host, or fail closed."""
    host_os, host_arch = _normalize_host(
        platform_name if platform_name is not None else sys.platform,
        machine if machine is not None else platform.machine(),
    )
    return _ARTIFACTS[(host_os, host_arch)]


def get_default_scrcpy_root(platform_name: str | None = None) -> Path:
    """Return ADAPT's user-scope, version-parent directory for managed scrcpy."""
    current = (platform_name if platform_name is not None else sys.platform).lower()
    if current in {"win32", "windows"}:
        local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
        return base / "ADAPT" / "tools" / "scrcpy"
    if current.startswith("linux"):
        xdg_data = os.environ.get("XDG_DATA_HOME", "").strip()
        base = Path(xdg_data) if xdg_data else Path.home() / ".local" / "share"
        return base / "adapt" / "tools" / "scrcpy"
    raise UnsupportedScrcpyHostError(
        "The managed scrcpy provisioner supports only native Windows and Linux."
    )


def _is_link_or_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    return bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _managed_paths(artifact: ScrcpyArtifact, install_root: Path) -> tuple[Path, Path]:
    version_parent = install_root / SCRCPY_VERSION
    return version_parent, version_parent / artifact.package_dir


def _normalized_install_root(
    install_root: Path | None,
    platform_name: str | None,
) -> Path:
    root = (
        Path(install_root)
        if install_root is not None
        else get_default_scrcpy_root(platform_name)
    ).expanduser()
    if not root.is_absolute():
        raise tagged(ScrcpyInstallError(
            "Managed scrcpy install root must be absolute; refusing a cwd-relative write."
        ), "scrcpy.install_failed")
    return root


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_DOWNLOAD_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_managed_receipt(target_dir: Path, artifact: ScrcpyArtifact) -> dict[str, Any] | None:
    receipt_path = target_dir / _RECEIPT_NAME
    if _is_link_or_reparse(target_dir) or not target_dir.is_dir():
        return None
    try:
        if _is_link_or_reparse(receipt_path) or receipt_path.stat().st_size > 4 * 1024 * 1024:
            return None
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(receipt, dict):
        return None
    asset_record = receipt.get("asset")
    if (
        receipt.get("schema_version") != _RECEIPT_SCHEMA
        or receipt.get("tool") != "scrcpy"
        or receipt.get("version") != SCRCPY_VERSION
        or not isinstance(asset_record, dict)
        or asset_record.get("name") != artifact.asset_name
        or asset_record.get("sha256") != artifact.sha256
    ):
        return None
    file_records = receipt.get("files")
    if not isinstance(file_records, list) or len(file_records) > _MAX_ARCHIVE_MEMBERS:
        return None

    by_path: dict[str, dict[str, Any]] = {}
    normalized_paths: set[str] = set()
    for item in file_records:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            return None
        relative = item["path"]
        try:
            parts = _safe_member_parts(relative)
        except ScrcpyArchiveSafetyError:
            return None
        normalized = "/".join(part.casefold() for part in parts)
        if normalized in normalized_paths or relative == _RECEIPT_NAME:
            return None
        normalized_paths.add(normalized)
        by_path[relative] = item

    actual_paths: set[str] = set()
    try:
        for candidate in target_dir.rglob("*"):
            if _is_link_or_reparse(candidate):
                return None
            if candidate.is_file():
                relative = candidate.relative_to(target_dir).as_posix()
                if relative != _RECEIPT_NAME:
                    actual_paths.add(relative)
    except OSError:
        return None
    if actual_paths != set(by_path):
        return None

    for relative, item in by_path.items():
        candidate = target_dir.joinpath(*relative.split("/"))
        try:
            expected_size = int(item.get("bytes"))
        except (TypeError, ValueError):
            return None
        if (
            not candidate.is_file()
            or _is_link_or_reparse(candidate)
            or candidate.stat().st_size != expected_size
            or item.get("sha256") != _hash_file(candidate)
        ):
            return None
    for rel_name in artifact.required_files:
        candidate = target_dir / rel_name
        if (
            rel_name not in by_path
            or not candidate.is_file()
            or _is_link_or_reparse(candidate)
        ):
            return None
    if not hmac.compare_digest(_hash_file(target_dir / "scrcpy-server"), _SERVER_SHA256):
        return None
    return receipt


def plan_scrcpy(
    *,
    platform_name: str | None = None,
    machine: str | None = None,
    install_root: Path | None = None,
    which: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    """Return a JSON-serializable, offline plan without writes or execution.

    An existing ``scrcpy`` on PATH is reused by default and is deliberately not
    executed or assigned trust/version claims by this planner.
    """
    finder = which if which is not None else shutil.which
    found = finder("scrcpy")
    if found:
        return {
            "status": "reuse_existing",
            "action": "reuse",
            "version": "not_probed",
            "trusted": False,
            "binary_path": str(Path(found)),
            "writes": False,
            "downloads": False,
            "path_modified": False,
            "note": "Existing PATH candidate is reused without executing or modifying it.",
        }

    artifact = detect_scrcpy_artifact(platform_name, machine)
    root = _normalized_install_root(install_root, platform_name)
    _, target_dir = _managed_paths(artifact, root)
    managed = _read_managed_receipt(target_dir, artifact) if target_dir.exists() else None
    if managed is not None:
        return {
            "status": "already_installed",
            "action": "reuse_managed",
            "version": SCRCPY_VERSION,
            "trusted": True,
            "binary_path": str(target_dir / artifact.executable_name),
            "receipt_path": str(target_dir / _RECEIPT_NAME),
            "writes": False,
            "downloads": False,
            "path_modified": False,
        }
    if target_dir.exists() or target_dir.is_symlink():
        return {
            "status": "conflict",
            "action": "refuse",
            "version": SCRCPY_VERSION,
            "target_dir": str(target_dir),
            "writes": False,
            "downloads": False,
            "path_modified": False,
            "reason": "Target exists but is not this exact verified managed install.",
        }
    return {
        "status": "ready",
        "action": "install",
        "version": SCRCPY_VERSION,
        "release_tag": SCRCPY_RELEASE_TAG,
        "released_at": SCRCPY_RELEASED_AT,
        "tag_commit": SCRCPY_TAG_COMMIT,
        "host": {"os": artifact.host_os, "arch": artifact.host_arch},
        "asset": {
            "name": artifact.asset_name,
            "url": artifact.source_url,
            "sha256": artifact.sha256,
            "bytes": artifact.size,
        },
        "target_dir": str(target_dir),
        "binary_path": str(target_dir / artifact.executable_name),
        "confirmation_required": True,
        "writes": True,
        "downloads": True,
        "path_modified": False,
        "launches_scrcpy": False,
        "device_access": False,
    }


def resolve_scrcpy_binary() -> Path | None:
    """Locate an existing PATH or intact managed client, without installation."""
    try:
        plan = plan_scrcpy()
    except (ScrcpyInstallError, OSError):
        return None
    if plan["status"] in {"reuse_existing", "already_installed"}:
        return Path(plan["binary_path"])
    return None


def _validate_delivery_url(url: str, artifact: ScrcpyArtifact, *, initial: bool) -> None:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise tagged(ScrcpyIntegrityError("Release URL is malformed; refusing download."), "scrcpy.download_redirect_refused") from exc
    if parsed.scheme.lower() != "https":
        raise tagged(ScrcpyIntegrityError("Release delivery must remain HTTPS on every redirect hop."), "scrcpy.download_redirect_refused")
    if parsed.username is not None or parsed.password is not None:
        raise tagged(ScrcpyIntegrityError("Release URL must not contain credentials."), "scrcpy.download_redirect_refused")
    if port not in (None, 443):
        raise tagged(ScrcpyIntegrityError("Release URL must use the default HTTPS port."), "scrcpy.download_redirect_refused")
    if parsed.fragment:
        raise tagged(ScrcpyIntegrityError("Release URL must not contain a fragment."), "scrcpy.download_redirect_refused")

    host = (parsed.hostname or "").lower()
    expected_path = urllib.parse.urlsplit(artifact.source_url).path
    if host == _GITHUB_HOST:
        if parsed.path != expected_path or parsed.query:
            raise tagged(ScrcpyIntegrityError(
                "GitHub release URL did not remain pinned to the approved tag and asset."
            ), "scrcpy.download_redirect_refused")
    elif not initial and host == _ASSET_DELIVERY_HOST:
        if not parsed.path.startswith("/") or "\x00" in parsed.path:
            raise tagged(ScrcpyIntegrityError("GitHub asset-delivery path is malformed."), "scrcpy.download_redirect_refused")
        # Signed delivery queries are accepted in transit but never logged or persisted.
    else:
        raise tagged(ScrcpyIntegrityError(
            "Release redirect left the narrowly approved GitHub asset-delivery hosts."
        ), "scrcpy.download_redirect_refused")


def _sanitize_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


class _ScrcpyRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, artifact: ScrcpyArtifact):
        super().__init__()
        self.artifact = artifact
        self.chain: list[str] = []

    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Any:
        if len(self.chain) >= _MAX_REDIRECTS:
            raise tagged(ScrcpyIntegrityError("Release download exceeded the redirect limit."), "scrcpy.download_redirect_refused")
        target = urllib.parse.urljoin(req.full_url, newurl)
        _validate_delivery_url(target, self.artifact, initial=False)
        self.chain.append(_sanitize_url(target))
        return super().redirect_request(req, fp, code, msg, headers, target)


def _open_release_asset(artifact: ScrcpyArtifact) -> tuple[Any, list[str]]:
    _validate_delivery_url(artifact.source_url, artifact, initial=True)
    handler = _ScrcpyRedirectHandler(artifact)
    opener = urllib.request.build_opener(handler)
    request = urllib.request.Request(
        artifact.source_url,
        headers={"User-Agent": f"ADAPT/{SCRCPY_VERSION} scrcpy-provisioner"},
    )
    response = opener.open(request, timeout=_HTTP_TIMEOUT_SECONDS)
    return response, handler.chain


@operation("Downloading scrcpy")
def _download_artifact(
    artifact: ScrcpyArtifact,
    destination: Path,
    *,
    open_asset: Callable[[ScrcpyArtifact], tuple[Any, list[str]]] | None = None,
) -> dict[str, Any]:
    opener = open_asset if open_asset is not None else _open_release_asset
    digest = hashlib.sha256()
    total = 0
    try:
        response, redirect_chain = opener(artifact)
        with response:
            final_url = response.geturl() if hasattr(response, "geturl") else artifact.source_url
            _validate_delivery_url(final_url, artifact, initial=(not redirect_chain))
            raw_length = response.headers.get("Content-Length") if hasattr(response, "headers") else None
            if raw_length:
                try:
                    content_length = int(raw_length)
                except ValueError as exc:
                    raise ScrcpyIntegrityError("Release response has an invalid Content-Length.") from exc
                if content_length != artifact.size or content_length > _MAX_DOWNLOAD_BYTES:
                    raise ScrcpyIntegrityError(
                        "Release response size does not match pinned version metadata."
                    )
            with destination.open("xb") as output:
                while True:
                    chunk = response.read(_DOWNLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _MAX_DOWNLOAD_BYTES or total > artifact.size:
                        raise ScrcpyIntegrityError("Release archive exceeded its pinned byte bound.")
                    digest.update(chunk)
                    output.write(chunk)
    except ScrcpyInstallError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError):
        raise tagged(ScrcpyInstallError(
            "scrcpy download failed without installing; signed redirect details were not retained."
        ), "scrcpy.download_failed") from None

    actual = digest.hexdigest()
    if total != artifact.size:
        raise ScrcpyIntegrityError(
            f"Release archive size mismatch: expected {artifact.size} bytes, received {total}."
        )
    if not hmac.compare_digest(actual, artifact.sha256):
        raise ScrcpyIntegrityError("Release archive SHA-256 does not match pinned upstream metadata.")
    return {
        "bytes": total,
        "sha256": actual,
        "canonical_url": _sanitize_url(final_url),
        "redirect_count": len(redirect_chain),
    }


def _safe_member_parts(raw_name: str) -> tuple[str, ...]:
    if not raw_name or "\x00" in raw_name or len(raw_name) > 1024:
        raise ScrcpyArchiveSafetyError("Archive contains an empty, NUL, or overlong path.")
    if raw_name.startswith(("/", "\\")) or PureWindowsPath(raw_name).is_absolute():
        raise ScrcpyArchiveSafetyError("Archive contains an absolute path.")
    trimmed = raw_name.rstrip("/\\")
    if not trimmed:
        raise ScrcpyArchiveSafetyError("Archive contains an empty root path.")
    raw_parts = re.split(r"[/\\]", trimmed)
    parts: list[str] = []
    for raw_part in raw_parts:
        part = unicodedata.normalize("NFC", raw_part)
        if not part or part in {".", ".."}:
            raise ScrcpyArchiveSafetyError("Archive contains path traversal or ambiguous separators.")
        if len(part) > 255 or any(ord(char) < 32 for char in part):
            raise ScrcpyArchiveSafetyError("Archive contains an overlong or control-character path.")
        if ":" in part:
            raise ScrcpyArchiveSafetyError("Archive contains a Windows ADS or drive-like path.")
        if part.endswith((" ", ".")):
            raise ScrcpyArchiveSafetyError("Archive contains a Windows-ambiguous trailing name.")
        reserved_base = part.split(".", 1)[0].casefold()
        if reserved_base in _WINDOWS_RESERVED:
            raise ScrcpyArchiveSafetyError("Archive contains a Windows reserved device name.")
        parts.append(part)
    return tuple(parts)


def _validate_member_set(
    members: list[tuple[tuple[str, ...], bool, int, int]],
    expected_root: str,
) -> None:
    if not members or len(members) > _MAX_ARCHIVE_MEMBERS:
        raise ScrcpyArchiveSafetyError("Archive member count is empty or exceeds the safety bound.")
    expanded = sum(size for _, is_dir, size, _ in members if not is_dir)
    compressed = sum(size for _, is_dir, _, size in members if not is_dir)
    if expanded > _MAX_EXPANDED_BYTES:
        raise ScrcpyArchiveSafetyError("Archive expanded size exceeds the safety bound.")
    if expanded and compressed == 0:
        raise ScrcpyArchiveSafetyError("Archive has an invalid zero-byte compressed representation.")
    if compressed and expanded > compressed * _MAX_COMPRESSION_RATIO:
        raise ScrcpyArchiveSafetyError("Archive compression ratio exceeds the safety bound.")

    kinds: dict[str, bool] = {}
    for parts, is_dir, size, _ in members:
        if parts[0] != expected_root:
            raise ScrcpyArchiveSafetyError("Archive contains content outside its pinned package root.")
        if size < 0 or size > _MAX_MEMBER_BYTES:
            raise ScrcpyArchiveSafetyError("Archive member size exceeds the safety bound.")
        key = "/".join(part.casefold() for part in parts)
        if key in kinds:
            raise ScrcpyArchiveSafetyError("Archive contains duplicate or case-colliding paths.")
        kinds[key] = is_dir

    for key, is_dir in kinds.items():
        components = key.split("/")
        for index in range(1, len(components)):
            parent = "/".join(components[:index])
            if parent in kinds and not kinds[parent]:
                raise ScrcpyArchiveSafetyError("Archive has a file/directory prefix collision.")
        if not is_dir and any(other.startswith(f"{key}/") for other in kinds):
            raise ScrcpyArchiveSafetyError("Archive has a file/directory descendant collision.")


def _copy_exact(source: BinaryIO, destination: BinaryIO, expected: int) -> None:
    copied = 0
    while True:
        chunk = source.read(min(_DOWNLOAD_CHUNK_BYTES, expected - copied + 1))
        if not chunk:
            break
        copied += len(chunk)
        if copied > expected or copied > _MAX_MEMBER_BYTES:
            raise ScrcpyArchiveSafetyError("Archive member expanded beyond its declared size.")
        destination.write(chunk)
    if copied != expected:
        raise ScrcpyArchiveSafetyError("Archive member did not match its declared size.")


def _extract_zip(archive: Path, destination: Path, artifact: ScrcpyArtifact) -> None:
    try:
        with zipfile.ZipFile(archive, "r") as zipped:
            records: list[tuple[zipfile.ZipInfo, tuple[str, ...], bool, int, int]] = []
            for member in zipped.infolist():
                if member.flag_bits & 0x1:
                    raise ScrcpyArchiveSafetyError("Encrypted archive members are not accepted.")
                parts = _safe_member_parts(member.filename)
                unix_type = (member.external_attr >> 16) & 0o170000
                is_dir = member.is_dir()
                if unix_type not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise ScrcpyArchiveSafetyError("Archive links and special files are not accepted.")
                records.append(
                    (member, parts, is_dir, member.file_size, member.compress_size)
                )
            _validate_member_set(
                [(parts, is_dir, size, compressed) for _, parts, is_dir, size, compressed in records],
                artifact.package_dir,
            )
            for member, parts, is_dir, size, _ in records:
                output_path = destination.joinpath(*parts)
                if is_dir:
                    output_path.mkdir(parents=True, exist_ok=True)
                    continue
                output_path.parent.mkdir(parents=True, exist_ok=True)
                with zipped.open(member, "r") as source, output_path.open("xb") as output:
                    _copy_exact(source, output, size)
    except (zipfile.BadZipFile, EOFError) as exc:
        raise ScrcpyArchiveSafetyError("Release ZIP is malformed or truncated.") from exc


def _extract_tar_gz(archive: Path, destination: Path, artifact: ScrcpyArtifact) -> None:
    try:
        with tarfile.open(archive, "r:gz") as tarred:
            records: list[tuple[tarfile.TarInfo, tuple[str, ...], bool, int, int]] = []
            for member in tarred.getmembers():
                if not (member.isdir() or member.isreg()):
                    raise ScrcpyArchiveSafetyError("Archive links and special files are not accepted.")
                parts = _safe_member_parts(member.name)
                records.append((member, parts, member.isdir(), member.size, member.size))
            _validate_member_set(
                [(parts, is_dir, size, compressed) for _, parts, is_dir, size, compressed in records],
                artifact.package_dir,
            )
            compressed_size = archive.stat().st_size
            expanded_size = sum(size for _, _, is_dir, size, _ in records if not is_dir)
            if compressed_size == 0 or expanded_size > compressed_size * _MAX_COMPRESSION_RATIO:
                raise ScrcpyArchiveSafetyError("Archive compression ratio exceeds the safety bound.")
            for member, parts, is_dir, size, _ in records:
                output_path = destination.joinpath(*parts)
                if is_dir:
                    output_path.mkdir(parents=True, exist_ok=True)
                    continue
                output_path.parent.mkdir(parents=True, exist_ok=True)
                source = tarred.extractfile(member)
                if source is None:
                    raise ScrcpyArchiveSafetyError("Archive regular file could not be read.")
                with source, output_path.open("xb") as output:
                    _copy_exact(source, output, size)
                output_path.chmod(0o755 if member.mode & 0o111 else 0o644)
    except (tarfile.TarError, EOFError) as exc:
        raise ScrcpyArchiveSafetyError("Release tar.gz is malformed or truncated.") from exc


@operation("Preparing scrcpy")
def _extract_archive(archive: Path, destination: Path, artifact: ScrcpyArtifact) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    if artifact.archive_kind == "zip":
        _extract_zip(archive, destination, artifact)
    elif artifact.archive_kind == "tar.gz":
        _extract_tar_gz(archive, destination, artifact)
    else:
        raise ScrcpyArchiveSafetyError("Pinned artifact has an unsupported archive format.")


def _validate_payload(package_dir: Path, artifact: ScrcpyArtifact) -> list[dict[str, Any]]:
    for required in artifact.required_files:
        candidate = package_dir / required
        if not candidate.is_file() or _is_link_or_reparse(candidate) or candidate.stat().st_size == 0:
            raise ScrcpyIntegrityError(f"Verified archive is missing required file {required!r}.")
    server_digest = _hash_file(package_dir / "scrcpy-server")
    if not hmac.compare_digest(server_digest, _SERVER_SHA256):
        raise ScrcpyIntegrityError("Bundled scrcpy-server does not match the pinned version hash.")
    try:
        license_text = (package_dir / artifact.license_name).read_text(
            encoding="utf-8", errors="strict"
        )
    except (OSError, UnicodeError) as exc:
        raise ScrcpyIntegrityError("Bundled scrcpy license is not readable UTF-8 text.") from exc
    if "Apache License" not in license_text or "Copyright (C) 2018 Genymobile" not in license_text:
        raise ScrcpyIntegrityError("Bundled scrcpy license text is not the expected upstream notice.")

    manifest: list[dict[str, Any]] = []
    for path in sorted(package_dir.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if _is_link_or_reparse(path):
            raise ScrcpyArchiveSafetyError("Extracted payload contains a link or reparse point.")
        if path.is_file():
            relative = path.relative_to(package_dir).as_posix()
            manifest.append(
                {"path": relative, "bytes": path.stat().st_size, "sha256": _hash_file(path)}
            )
    if not manifest or len(manifest) > _MAX_ARCHIVE_MEMBERS:
        raise ScrcpyArchiveSafetyError("Extracted file count exceeds the safety bound.")
    return manifest


def _build_receipt(
    artifact: ScrcpyArtifact,
    download: dict[str, Any],
    files: list[dict[str, Any]],
) -> dict[str, Any]:
    notice_paths = [
        item["path"]
        for item in files
        if Path(item["path"]).name.casefold().startswith(("license", "notice", "copying"))
    ]
    return {
        "schema_version": _RECEIPT_SCHEMA,
        "tool": "scrcpy",
        "version": SCRCPY_VERSION,
        "release_tag": SCRCPY_RELEASE_TAG,
        "released_at": SCRCPY_RELEASED_AT,
        "tag_commit": SCRCPY_TAG_COMMIT,
        "installed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "host": {"os": artifact.host_os, "arch": artifact.host_arch},
        "asset": {
            "name": artifact.asset_name,
            "source_url": artifact.source_url,
            "canonical_url": download["canonical_url"],
            "bytes": download["bytes"],
            "sha256": download["sha256"],
            "redirect_count": download["redirect_count"],
        },
        "license": {
            "scrcpy": "Apache-2.0",
            "url": SCRCPY_LICENSE_URL,
            "retained_notices": notice_paths,
            "bundle_note": (
                "Bundled dependencies retain their own licenses; this receipt does not claim "
                "that the entire release bundle is Apache-2.0."
            ),
        },
        "files": files,
        "effects": {
            "path_modified": False,
            "binary_executed": False,
            "downloaded_shell_executed": False,
            "device_access": False,
        },
    }


def _atomic_place_no_clobber(source: Path, target: Path, host_os: str) -> None:
    if target.exists() or target.is_symlink():
        raise ScrcpyInstallConflictError(
            "Managed scrcpy target appeared during installation; nothing was overwritten."
        )
    if host_os == "windows":
        try:
            os.rename(source, target)
        except FileExistsError as exc:
            raise ScrcpyInstallConflictError(
                "Managed scrcpy target appeared during installation; nothing was overwritten."
            ) from exc
        return

    # Linux renameat2(RENAME_NOREPLACE) provides an atomic no-clobber placement.
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise tagged(ScrcpyInstallError(
            "This Linux runtime lacks atomic no-clobber rename support; refusing unsafe placement."
        ), "scrcpy.install_failed")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(-100, os.fsencode(source), -100, os.fsencode(target), 1)
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise ScrcpyInstallConflictError(
            "Managed scrcpy target appeared during installation; nothing was overwritten."
        )
    raise OSError(error_number, os.strerror(error_number), str(target))


def _confirm_on_stderr(message: str) -> bool:
    sys.stderr.write(f"{message} [y/N]: ")
    sys.stderr.flush()
    answer = sys.stdin.readline()
    return answer.strip().casefold() in {"y", "yes"}


def install_scrcpy(
    *,
    yes: bool = False,
    dry_run: bool = False,
    platform_name: str | None = None,
    machine: str | None = None,
    install_root: Path | None = None,
    which: Callable[[str], str | None] | None = None,
    confirm: Callable[[str], bool] | None = None,
    open_asset: Callable[[ScrcpyArtifact], tuple[Any, list[str]]] | None = None,
) -> dict[str, Any]:
    """Reuse or safely install the pinned scrcpy release.

    ``yes=True`` is the only non-interactive opt-in.  ``dry_run=True`` performs
    no download and no filesystem write.  No code path modifies PATH, launches
    scrcpy, probes a device, or invokes any downloaded executable/script.
    """
    plan = plan_scrcpy(
        platform_name=platform_name,
        machine=machine,
        install_root=install_root,
        which=which,
    )
    if plan["status"] in {"reuse_existing", "already_installed"}:
        return plan
    artifact = detect_scrcpy_artifact(platform_name, machine)
    if plan["status"] == "conflict":
        raise ScrcpyInstallConflictError(
            "Managed scrcpy target already exists but is not this exact verified install; "
            "refusing to overwrite or adopt it."
        )

    if dry_run:
        return {**plan, "status": "dry_run", "writes": False, "downloads": False}

    prompt = (
        f"scrcpy {SCRCPY_VERSION} / Genymobile ({artifact.host_os}/{artifact.host_arch})\n"
        f"Source: {artifact.source_url}\nDestination: {plan['target_dir']}\n"
        f"License: Apache-2.0 for scrcpy; bundled dependencies retain separate licenses.\n"
        f"License text: {SCRCPY_LICENSE_URL}\n"
        "No PATH changes or device/mirror launch. Install this release?"
    )

    if not yes:
        if confirm is not None:
            approved = bool(confirm(prompt))
        elif not sys.stdin.isatty():
            raise ScrcpyConfirmationRequiredError(
                "Non-interactive scrcpy installation requires explicit yes=True/--yes."
            )
        else:
            approved = _confirm_on_stderr(prompt)
        if not approved:
            return {**plan, "status": "cancelled", "writes": False, "downloads": False}

    root = _normalized_install_root(install_root, platform_name)
    if root.exists() and _is_link_or_reparse(root):
        raise ScrcpyInstallConflictError("Managed scrcpy root is a link/reparse point; refusing write.")
    root.mkdir(parents=True, exist_ok=True)
    version_parent, target_dir = _managed_paths(artifact, root)
    if version_parent.exists() and _is_link_or_reparse(version_parent):
        raise ScrcpyInstallConflictError("Managed scrcpy version root is a link/reparse point.")
    version_parent.mkdir(parents=True, exist_ok=True)

    lock_dir = version_parent / f".{artifact.package_dir}.install-lock"
    try:
        lock_dir.mkdir()
    except FileExistsError as exc:
        raise tagged(ScrcpyInstallConflictError(
            "Another scrcpy installation is active or left a lock; refusing concurrent placement."
        ), "scrcpy.install_locked") from exc

    archive_path: Path | None = None
    staging_dir: Path | None = None
    try:
        if target_dir.exists() or target_dir.is_symlink():
            raise ScrcpyInstallConflictError(
                "Managed scrcpy target appeared before download; nothing was overwritten."
            )
        file_descriptor, archive_name = tempfile.mkstemp(
            dir=version_parent,
            prefix=f".{artifact.package_dir}-download-",
            suffix=".archive",
        )
        os.close(file_descriptor)
        archive_path = Path(archive_name)
        archive_path.unlink()  # _download_artifact uses exclusive creation.

        staging_dir = version_parent / f".{artifact.package_dir}-staging-{uuid.uuid4().hex}"
        download_evidence = _download_artifact(
            artifact, archive_path, open_asset=open_asset
        )
        extraction_root = staging_dir / "payload"
        _extract_archive(archive_path, extraction_root, artifact)
        package_dir = extraction_root / artifact.package_dir
        files = _validate_payload(package_dir, artifact)
        receipt = _build_receipt(artifact, download_evidence, files)
        receipt_path = package_dir / _RECEIPT_NAME
        try:
            with receipt_path.open("x", encoding="utf-8") as receipt_stream:
                receipt_stream.write(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        except FileExistsError as exc:
            raise ScrcpyArchiveSafetyError(
                "Upstream archive collides with ADAPT's managed receipt name."
            ) from exc
        _atomic_place_no_clobber(package_dir, target_dir, artifact.host_os)
        return {
            "status": "installed",
            "version": SCRCPY_VERSION,
            "binary_path": str(target_dir / artifact.executable_name),
            "receipt_path": str(target_dir / _RECEIPT_NAME),
            "asset_sha256": artifact.sha256,
            "path_modified": False,
            "binary_executed": False,
            "device_access": False,
        }
    finally:
        if archive_path is not None:
            try:
                archive_path.unlink(missing_ok=True)
            except OSError:
                pass
        if staging_dir is not None:
            shutil.rmtree(staging_dir, ignore_errors=True)
        try:
            lock_dir.rmdir()
        except OSError:
            pass


__all__ = [
    "resolve_scrcpy_binary",
    "SCRCPY_VERSION",
    "ScrcpyArtifact",
    "ScrcpyArchiveSafetyError",
    "ScrcpyConfirmationRequiredError",
    "ScrcpyInstallConflictError",
    "ScrcpyInstallError",
    "ScrcpyIntegrityError",
    "UnsupportedScrcpyHostError",
    "detect_scrcpy_artifact",
    "get_default_scrcpy_root",
    "install_scrcpy",
    "plan_scrcpy",
]
