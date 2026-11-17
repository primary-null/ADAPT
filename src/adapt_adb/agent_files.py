"""File primitives shared by `adapt agent install`, `skill install` and `agents-md --write`.

Everything that changes a file on this host goes through here, so the rules are in one place:
show a unified diff first, copy the original to `<file>.adapt-backup-<UTC>`, record that backup
in a manifest, and replace the file atomically. Nothing here raises: a failure comes back as an
error string for the caller to report.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

MANIFEST_SCHEMA = 1
BACKUP_MARK = ".adapt-backup-"
_SECRET_JSON = re.compile(r'("[^"\n]*(?:token|key|secret|password|passwd|authorization|credential)[^"\n]*"\s*:\s*)"[^"\n]*"', re.I)
_SECRET_TOML = re.compile(r"^(\s*[\w.\-\"]*(?:token|key|secret|password|passwd|authorization|credential)[\w.\-\"]*\s*=\s*).+$", re.I)


def utc_stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")


def decode(data: bytes) -> tuple[str | None, bool]:
    """(text, had_bom); text is None when the bytes are not UTF-8."""
    bom = data.startswith(b"\xef\xbb\xbf")
    try:
        return data[3:].decode("utf-8") if bom else data.decode("utf-8"), bom
    except UnicodeDecodeError:
        return None, bom


def encode(text: str, bom: bool = False) -> bytes:
    return (b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8")


def newline_of(text: str) -> str:
    """CRLF when the file uses it, else LF: a changed file keeps its own line endings."""
    return "\r\n" if "\r\n" in text else "\n"


def to_newline(text: str, newline: str) -> str:
    return text.replace("\r\n", "\n").replace("\n", newline)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def redact_line(line: str) -> str:
    """Hide the value of a secret-looking key in a diff line, so a diff never prints a token."""
    return _SECRET_TOML.sub(r"\1<redacted>", _SECRET_JSON.sub(r'\1"<redacted>"', line))


def _lines(text: str | None) -> list[str]:
    if text is None:
        return []
    lines = text.replace("\r\n", "\n").split("\n")
    return lines[:-1] if lines and lines[-1] == "" else lines


def unified_diff(old: str | None, new: str | None, label: str) -> str:
    """A unified diff of two texts (None means the file does not exist), secret values hidden."""
    lines = difflib.unified_diff(_lines(old), _lines(new), fromfile="/dev/null" if old is None else f"a/{label}",
                                 tofile="/dev/null" if new is None else f"b/{label}", lineterm="", n=1)
    return "\n".join(redact_line(line) for line in lines)


def write_atomic(path: Path, data: bytes) -> str | None:
    """Replace `path` with `data` through a temporary file in the same folder. Returns an error text or None."""
    target = path.resolve() if path.is_symlink() else path
    temp_name = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".adapt-tmp", dir=str(target.parent))
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if target.exists():
            shutil.copymode(target, temp_name)
        os.replace(temp_name, target)
        temp_name = None
        return None
    except OSError as exc:
        return f"could not write the file ({exc.__class__.__name__}); nothing was changed"
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass


def make_backup(path: Path, stamp: str) -> tuple[Path | None, str | None]:
    """Copy an existing file to `<file>.adapt-backup-<stamp>`; (None, None) when there is nothing to back up."""
    if not path.exists():
        return None, None
    base = path.parent / (path.name + BACKUP_MARK + stamp)
    candidate, counter = base, 1
    while candidate.exists():
        counter += 1
        candidate = path.parent / f"{base.name}-{counter}"
    try:
        shutil.copy2(path, candidate)
    except OSError as exc:
        return None, f"could not back up the file ({exc.__class__.__name__}); nothing was changed"
    return candidate, None


# --- manifest --------------------------------------------------------------------------------


def manifest_path(environ: Mapping[str, str], windows: bool) -> Path | None:
    """`<adapt config folder>/agent-installs.json`; the same folder as the preferences file."""
    if environ.get("ADAPT_CONFIG_DIR"):
        return Path(environ["ADAPT_CONFIG_DIR"]) / "agent-installs.json"
    home = environ.get("USERPROFILE") or environ.get("HOME") if windows else environ.get("HOME") or environ.get("USERPROFILE")
    if windows:
        root = environ.get("LOCALAPPDATA") or (f"{home}/AppData/Local" if home else None)
    else:
        root = environ.get("XDG_CONFIG_HOME") or (f"{home}/.config" if home else None)
    return Path(root) / "adapt" / "agent-installs.json" if root else None


def read_manifest(path: Path | None) -> tuple[dict[str, Any], str | None]:
    """(manifest, problem). A missing file is an empty manifest; an unreadable one is a problem."""
    empty: dict[str, Any] = {"schema": MANIFEST_SCHEMA, "entries": []}
    if path is None:
        return empty, "no folder for ADAPT's install manifest: HOME, USERPROFILE and ADAPT_CONFIG_DIR are all unset"
    if not path.exists():
        return empty, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty, "ADAPT's install manifest could not be read; it was left untouched"
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        return empty, "ADAPT's install manifest has an unexpected shape; it was left untouched"
    return data, None


def append_manifest(path: Path | None, record: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Add one record (the manifest assigns its `id`); returns (record, error)."""
    manifest, problem = read_manifest(path)
    if problem or path is None:
        return None, problem
    numbers = [int(str(e.get("id", "0")).rsplit("-", 1)[-1]) for e in manifest["entries"]
               if str(e.get("id", "")).rsplit("-", 1)[-1].isdigit()]
    record = {**record, "id": f"{record['time']}-{max(numbers, default=0) + 1}"}
    manifest["entries"].append(record)
    error = write_atomic(path, (json.dumps(manifest, indent=2) + "\n").encode("utf-8"))
    return (None, error) if error else (record, None)
