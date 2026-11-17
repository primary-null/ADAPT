"""Read-only device detection for Developer Options: maker layer, One UI, Pixel build.

Everything here is derived from a handful of allowlisted property and feature
reads that the shell user can make.  The reader is injected: it takes a device
argv (after ``adb -s SERIAL``) and returns ``(ok, text)``, where ``text`` is the
output or, when ``ok`` is false, a short failure kind.  Nothing is written and no
project exception is raised here.

Detection is a hint for gating and for guidance.  It does not prove that an OEM
build honours any option, and the One UI version property is not documented by
Samsung.
"""

from __future__ import annotations

import re
from typing import Callable

Reader = Callable[[list[str]], tuple[bool, str]]

MAX_PROP_CHARS = 64
_ONEUI_RE = re.compile(r"[0-9]{5,6}")
_PROP_TEXT_RE = re.compile(r"[A-Za-z0-9 ._+/-]{1,64}")
_BUILD_TYPES = ("user", "userdebug", "eng")
_CHARACTERISTICS_RE = re.compile(r"[a-z0-9_]{1,24}(?:,[a-z0-9_]{1,24}){0,11}")

# One UI major version by Android API level, from the research notes (undocumented
# by Samsung); used only when the One UI property is missing.
ONEUI_BY_API = {29: 2, 30: 3, 31: 4, 32: 4, 33: 5, 34: 6, 35: 7, 36: 8, 37: 9}

_FEATURES = {
    "android.hardware.sensor.hinge_angle": "hinge_sensor",
    "android.software.freeform_window_management": "freeform_windows",
    "android.software.activities_on_secondary_displays": "secondary_displays",
    "android.software.picture_in_picture": "picture_in_picture",
    "android.software.leanback": "tv",
    "android.hardware.type.watch": "watch",
    "android.hardware.type.automotive": "automotive",
}

AUTO_BLOCKER_GUIDANCE = (
    "Samsung Auto Blocker (One UI 6 and later) can block commands sent over USB and turn USB debugging off. "
    "If a write fails or the device stops answering, check Settings > Security and privacy > Auto Blocker on the phone. "
    "ADAPT never changes it."
)


def classify_oem(manufacturer: str, brand: str) -> str:
    maker, label = manufacturer.strip().lower(), brand.strip().lower()
    if maker == "samsung" or label == "samsung":
        return "samsung"
    if maker == "google" and label == "google":
        return "pixel"
    return "other"


def _clean(text: str) -> str | None:
    value = text.strip()
    return value if _PROP_TEXT_RE.fullmatch(value) else None


def parse_oneui(raw: str) -> dict[str, object] | None:
    """Decode the five or six digit One UI property: 60101 is 6.1.1, 80500 is 8.5."""
    value = raw.strip()
    if not _ONEUI_RE.fullmatch(value):
        return None
    number = int(value)
    major, minor, patch = number // 10000, (number // 100) % 100, number % 100
    text = f"{major}.{minor}" + (f".{patch}" if patch else "")
    return {"version": text, "major": major}


def _one_ui(read: Reader, api: int | None, unread: list[dict[str, str]]) -> dict[str, object]:
    block: dict[str, object] = {
        "version": None, "major": None, "source": None, "raw": None, "estimated_major": None,
        "note": "The One UI version properties are not documented by Samsung; treat them as hints.",
    }
    for source in ("ro.build.version.oneui", "ro.build.version.sep", "ro.build.version.sem"):
        ok, text = read(["shell", "getprop", source])
        if not ok:
            unread.append({"item": source, "status": text})
            continue
        raw = text.strip()
        if not raw:
            continue
        if source.endswith("oneui"):
            parsed = parse_oneui(raw)
            if parsed:
                block.update(version=parsed["version"], major=parsed["major"], source=source, raw=raw)
                break
            block["raw"] = raw[:16] if raw.isdigit() else None
            continue
        block["fallback_" + source.rsplit(".", 1)[1]] = raw[:16] if raw.isdigit() else None
    if block["major"] is None and api in ONEUI_BY_API:
        block["estimated_major"] = ONEUI_BY_API[api]
    return block


def _features(text: str) -> dict[str, bool]:
    present = {line.removeprefix("feature:").split("=", 1)[0].strip() for line in text.splitlines() if line.startswith("feature:")}
    return {label: name in present for name, label in _FEATURES.items()}


def _device_admin(text: str) -> dict[str, object]:
    """Counts only: the output names organizations and apps, so none of it is kept."""
    body = text[:1_000_000]
    owner = bool(re.search(r"^\s*Device Owner:\s*\n\s+admin=", body, re.MULTILINE))
    profile_owner = bool(re.search(r"^\s*Profile Owner \(User [0-9]+\):\s*\n\s+admin=", body, re.MULTILINE))
    admins = 0
    in_block = False
    for line in body.splitlines():
        if line.lstrip().startswith("Enabled Device Admins"):
            in_block = True
            continue
        if in_block:
            if re.fullmatch(r"\s+[A-Za-z0-9_.]+/[A-Za-z0-9_.$]+:", line):
                admins += 1
            elif line.strip() and not line.startswith(" "):
                in_block = False
    return {
        "device_owner": owner,
        "profile_owner": profile_owner,
        "active_admins": admins,
        "present": owner or profile_owner or admins > 0,
        "note": "Read from dumpsys text whose layout varies; false means none was recognised, not that none exists.",
    }


def detect_oem(read: Reader, unread: list[dict[str, str]], *, transport: str = "usb", emulator: bool = False) -> dict[str, object]:
    """The maker layer and, for Samsung, the One UI block: the few reads gating and guidance need."""

    def prop(name: str) -> str | None:
        ok, text = read(["shell", "getprop", name])
        if not ok:
            unread.append({"item": name, "status": text})
            return None
        return text.strip()

    sdk_text = prop("ro.build.version.sdk")
    api = int(sdk_text) if sdk_text and re.fullmatch(r"[0-9]{1,3}", sdk_text) else None
    maker = _clean(prop("ro.product.manufacturer") or "") or ""
    brand = _clean(prop("ro.product.brand") or "") or ""
    oem = classify_oem(maker, brand)
    report: dict[str, object] = {
        "api_level": api,
        "oem": oem,
        "manufacturer": maker or None,
        "brand": brand or None,
        "transport": transport,
        "emulator_hint": emulator,
        "samsung": None,
        "pixel": None,
        "guidance": [],
    }
    if oem == "samsung":
        one_ui = _one_ui(read, api, unread)
        report["samsung"] = one_ui
        major = one_ui["major"] if one_ui["major"] is not None else one_ui["estimated_major"]
        blocker = isinstance(major, int) and major >= 6
        one_ui["auto_blocker_applies"] = blocker
        if blocker:
            report["guidance"] = [AUTO_BLOCKER_GUIDANCE]
    return report


def detect(read: Reader, *, transport: str, emulator: bool = False) -> dict[str, object]:
    """Collect the detection block with read-only calls; every failed read is listed in ``unread``."""
    unread: list[dict[str, str]] = []

    def prop(name: str) -> str | None:
        ok, text = read(["shell", "getprop", name])
        if not ok:
            unread.append({"item": name, "status": text})
            return None
        return text.strip()

    report = detect_oem(read, unread, transport=transport, emulator=emulator)
    oem = report["oem"]
    if oem == "pixel":
        report["pixel"] = {
            "build_id": _clean(prop("ro.build.id") or ""),
            "soc_model": _clean(prop("ro.soc.model") or ""),
            "note": "Pixel QPR releases share an API level; gate on what the device reports, not on the API number alone.",
        }
    build_type = (prop("ro.build.type") or "").strip()
    report["build_type"] = build_type if build_type in _BUILD_TYPES else None

    ok, text = read(["shell", "getconf", "PAGE_SIZE"])
    size = text.strip() if ok else ""
    report["page_size"] = int(size) if re.fullmatch(r"4096|16384|65536", size) else None
    if not ok:
        unread.append({"item": "page_size", "status": text})

    characteristics = (prop("ro.build.characteristics") or "").strip()
    chars = characteristics.split(",") if _CHARACTERISTICS_RE.fullmatch(characteristics) else []
    form: dict[str, object] = {"characteristics": chars, "tablet": "tablet" in chars}
    ok, text = read(["shell", "pm", "list", "features"])
    if ok:
        form.update(_features(text))
    else:
        unread.append({"item": "features", "status": text})
    report["form_factor"] = form

    ok, text = read(["shell", "dumpsys", "device_policy"])
    if ok:
        report["device_admin"] = _device_admin(text)
    else:
        report["device_admin"] = None
        unread.append({"item": "device_policy", "status": text})
    report["unread"] = unread
    report["note"] = "Detection is read-only and is a hint for gating and guidance, not proof that an option is honoured."
    return report
