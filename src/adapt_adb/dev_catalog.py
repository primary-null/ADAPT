"""The curated Developer Options catalog: an offline guide to what exists and what ADAPT does with it.

The data file is a curated subset of desk research (``data/dev-options-catalog.json``):
read-only observations, options a profile could change, options only the person at the
device can toggle, and options that must never be automated.  Nothing in it was run on a
device by ADAPT.  This module only reads that file and plans allowlisted reads; it
contacts no device, and it returns report dictionaries instead of raising.
"""

from __future__ import annotations

import json
import shlex
from importlib import resources
from typing import Any

from adapt_adb.safety import dev_options_shape_writes, match_dev_options_shell

LAYERS = ("aosp", "pixel", "samsung", "adb_control")
CLASSES = ("inspect", "profile", "ui_only", "dangerous")
AUTOMATION = ("read_only", "adapt_profile", "reference_only", "guidance_only", "never_automate")
CONFIDENCE = ("high", "medium", "low")
NEEDS_REBOOT = ("no", "yes", "poke", "app_restart", "unknown")
MAX_VALUE_CHARS = 256
ENTRY_FIELDS = (
    "id", "ui_name", "section", "layer", "mechanism", "read_cmd", "write_cmd", "restore_cmd", "value_type",
    "default", "min_api", "max_api", "needs_reboot", "adapt_class", "testing_use", "oem_notes", "sources",
    "confidence", "automated_by", "automation", "guidance",
)
PAGE_SIZE_GUIDE_URL = "https://developer.android.com/guide/practices/page-sizes"
BOOT_16KB_WARNING = (
    "The 'Boot with 16KB page size' developer option on supported Pixel models needs an unlocked bootloader "
    "(OEM unlocking) and wipes the device."
)

_CACHE: dict[str, Any] = {}


def load_catalog() -> dict[str, Any]:
    """The catalog document, or ``{"ok": False, ...}`` when the packaged file cannot be read."""
    if "document" in _CACHE:
        return _CACHE["document"]
    try:
        text = resources.files("adapt_adb").joinpath("data/dev-options-catalog.json").read_text(encoding="utf-8")
        document = json.loads(text)
    except (OSError, ValueError, ModuleNotFoundError):
        document = None
    if not isinstance(document, dict) or not isinstance(document.get("entries"), list):
        document = {"ok": False, "entries": [], "message": "The packaged Developer Options catalog could not be read."}
    _CACHE["document"] = document
    return document


def _in_api(entry: dict[str, Any], api: int | None) -> bool:
    if api is None:
        return True
    low, high = entry.get("min_api"), entry.get("max_api")
    return (low is None or api >= low) and (high is None or api <= high)


def filter_entries(entries: list[dict[str, Any]], *, layer: str | None = None, api: int | None = None) -> list[dict[str, Any]]:
    return [e for e in entries if (layer is None or e["layer"] == layer) and _in_api(e, api)]


def catalog_report(*, layer: str | None = None, api: int | None = None) -> dict[str, Any]:
    """The catalog filtered by layer and API level.  Offline; contacts no device."""
    if layer is not None and layer not in LAYERS:
        return {"ok": False, "error": "invalid_layer", "message": f"Layer must be one of: {', '.join(LAYERS)}."}
    document = load_catalog()
    if document.get("ok") is False:
        return {"ok": False, "error": "catalog_unreadable", "message": document["message"]}
    entries = filter_entries(document["entries"], layer=layer, api=api)
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry["adapt_class"]] = counts.get(entry["adapt_class"], 0) + 1
    return {
        "ok": True,
        "command": "dev-options catalog",
        "schema_version": document["schema_version"],
        "researched": document["researched"],
        "scope": document["scope"],
        "source_note": document["source_note"],
        "layer": layer,
        "api": api,
        "counts": {"entries": len(entries), "by_class": counts},
        "entries": entries,
        "note": "Offline guide: nothing here was run on a device by ADAPT. Guidance-only and never-automate entries are not changed by any ADAPT command.",
    }


def render_catalog(report: dict[str, Any]) -> str:
    lines = [
        f"Developer Options catalog (researched {report['researched']}; offline, nothing run on a device)",
        f"  {report['counts']['entries']} entries"
        + (f", layer {report['layer']}" if report["layer"] else "")
        + (f", API {report['api']}" if report["api"] else ""),
    ]
    titles = {
        "inspect": "Read only (inspect)",
        "profile": "Reversible shell changes (some used by an ADAPT profile)",
        "ui_only": "Do this on the device (ADAPT cannot change it)",
        "dangerous": "Never automated",
    }
    for cls in CLASSES:
        group = [e for e in report["entries"] if e["adapt_class"] == cls]
        if not group:
            continue
        lines.append(f"{titles[cls]}: {len(group)}")
        for entry in group:
            used = f"  [profile: {', '.join(entry['automated_by'])}]" if entry["automated_by"] else ""
            lines.append(f"  {entry['id']} ({entry['confidence']}): {entry['ui_name']}{used}")
            if cls in ("ui_only", "dangerous") and entry.get("guidance"):
                lines.append(f"      {entry['guidance'][:220]}")
    lines.append(report["note"])
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Allowlisted reads for `inspect --all`
# --------------------------------------------------------------------------


def _read_plan(read_cmd: str | None, user: int) -> tuple[list[list[str]] | None, str | None]:
    if not read_cmd:
        return None, "no_read_command"
    if read_cmd.startswith("(host)"):
        return None, "host_command"
    argvs: list[list[str]] = []
    for part in read_cmd.split(" ; "):
        if "|" in part:
            return None, "pipe_not_allowed"
        try:
            tokens = shlex.split(part)
        except ValueError:
            return None, "unparseable"
        tokens = [str(user) if token == "USER" else token for token in tokens]
        if any(token in ("PKG", "SERIAL") for token in tokens):
            return None, "needs_package"
        shape = match_dev_options_shell(["shell", *tokens])
        if shape is None or dev_options_shape_writes(shape):
            return None, "no_allowlisted_read"
        argvs.append(tokens)
    return argvs, None


def plan_reads(api: int | None, oem: str | None, user: int) -> list[dict[str, Any]]:
    """For this device's API and maker layer: the entries to read, with their argvs, or why one is skipped."""
    document = load_catalog()
    layers = {"aosp", "adb_control"} | ({oem} if oem in ("pixel", "samsung") else set())
    plan: list[dict[str, Any]] = []
    for entry in document.get("entries", []):
        if entry["layer"] not in layers or not _in_api(entry, api):
            continue
        argvs, skipped = _read_plan(entry.get("read_cmd"), user)
        plan.append({"id": entry["id"], "layer": entry["layer"], "argvs": argvs or [], "skipped": skipped})
    return plan


def bound_value(texts: list[str]) -> str:
    """One short printable line from command outputs: whitespace collapsed and cut at the bound."""
    joined = " ; ".join(" ".join(text.split()) for text in texts)
    return "".join(ch for ch in joined if ch.isprintable())[:MAX_VALUE_CHARS]


def page_size_guidance(page_size: int | None, oem: str) -> list[str]:
    """What to do about 16 KB page testing, from the page size this boot reports."""
    if page_size is None:
        lines = ["The page size could not be read; ask the device with getconf PAGE_SIZE through adb."]
    elif page_size >= 16384:
        lines = [f"This device reports {page_size}-byte pages (16 KB or larger): test 16 KB compatibility on it as it is."]
    else:
        lines = [
            f"This device reports {page_size}-byte pages (4 KB).",
            "To test 16 KB pages without risk, use an Android emulator system image that uses 16 KB pages.",
        ]
    if oem == "pixel":
        lines.append(f"Some Pixel models can boot with 16 KB pages through a developer option. {BOOT_16KB_WARNING} ADAPT never changes it.")
    else:
        lines.append("The Pixel 16 KB boot option is not available on this maker layer.")
    lines.append(f"Google's guide to 16 KB page sizes: {PAGE_SIZE_GUIDE_URL}")
    return lines
