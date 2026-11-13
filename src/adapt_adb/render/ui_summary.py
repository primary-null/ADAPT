"""Parse uiautomator XML hierarchy into compact interactive JSON nodes."""

from __future__ import annotations

import re
import subprocess
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")

_CONTAINER_CLASSES = {
    "android.view.ViewGroup",
    "android.widget.FrameLayout",
    "android.widget.LinearLayout",
    "android.widget.RelativeLayout",
    "android.widget.ScrollView",
    "android.widget.HorizontalScrollView",
    "androidx.recyclerview.widget.RecyclerView",
    "androidx.constraintlayout.widget.ConstraintLayout",
    "android.view.View",
}

# Device output is UTF-8; decode it as such on every host (not the Windows locale codec).
_TEXT_DECODE: dict[str, Any] = {"text": True, "encoding": "utf-8", "errors": "replace"}


class UiDumpUnavailableError(RuntimeError):
    """The direct UI dump failed and the on-device file fallback was not permitted."""


def _parse_bounds(bounds_str: str) -> tuple[int, int, int, int] | None:
    match = _BOUNDS_RE.search(bounds_str or "")
    if not match:
        return None
    x1, y1, x2, y2 = (int(match.group(i)) for i in range(1, 5))
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


def parse_ui_summary(xml_text: str, max_nodes: int = 80) -> dict[str, list[dict[str, Any]]]:
    """Parse uiautomator XML dump into compact interactive node objects.

    Drops invisible, zero-size, and non-interactive container nodes.
    Never returns raw XML.
    """
    start_idx = xml_text.find("<hierarchy")
    end_idx = xml_text.rfind("</hierarchy>")
    if start_idx != -1 or end_idx != -1:
        if start_idx == -1 or end_idx <= start_idx:
            raise ValueError("uiautomator XML has an unterminated or out-of-order <hierarchy> element")
        xml_slice = xml_text[start_idx : end_idx + len("</hierarchy>")]
    else:
        xml_slice = xml_text.strip()

    if not xml_slice:
        return {"nodes": []}

    try:
        root = ET.fromstring(xml_slice)
    except ET.ParseError as exc:
        raise ValueError(f"Failed to parse uiautomator XML: {exc}") from exc

    nodes: list[dict[str, Any]] = []
    for elem in root.iter("node"):
        if len(nodes) >= max_nodes:
            break

        attrib = elem.attrib
        if attrib.get("visible-to-user", "true").lower() == "false":
            continue

        parsed_bounds = _parse_bounds(attrib.get("bounds", ""))
        if parsed_bounds is None:
            continue
        x1, y1, x2, y2 = parsed_bounds

        text = (attrib.get("text") or "").strip()
        content_desc = (attrib.get("content-desc") or "").strip()
        resource_id = (attrib.get("resource-id") or "").strip()
        cls_name = (attrib.get("class") or "").strip()

        clickable = attrib.get("clickable", "false").lower() == "true"
        long_clickable = attrib.get("long-clickable", "false").lower() == "true"
        checkable = attrib.get("checkable", "false").lower() == "true"
        editable = "EditText" in cls_name or "AutoCompleteTextView" in cls_name

        is_interactive = clickable or long_clickable or checkable or editable
        has_label = bool(text or content_desc)

        if not is_interactive and not has_label:
            continue
        if not is_interactive and cls_name in _CONTAINER_CLASSES:
            continue

        center_x = (x1 + x2) // 2
        center_y = (y1 + y2) // 2

        nodes.append(
            {
                "text": text,
                "content_desc": content_desc,
                "resource_id": resource_id,
                "class": cls_name,
                "clickable": clickable,
                "bounds": [x1, y1, x2, y2],
                "center": [center_x, center_y],
            }
        )

    return {"nodes": nodes}


def capture_ui_summary(
    adb_path: Path,
    serial: str,
    max_nodes: int = 80,
    runner: Any = subprocess.run,
    *,
    allow_device_file: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    """Dump UI hierarchy from device via `adb -s <serial>` and return compact JSON summary.

    The direct `/dev/tty` dump writes nothing on the device. If it fails, the fallback
    dumps to a temporary `/sdcard` file, reads it and removes it; with
    `allow_device_file=False` that fallback is refused with `UiDumpUnavailableError`.
    """
    if not serial:
        raise ValueError("Target device serial is required for ui_summary.")

    proc = runner(
        [str(adb_path), "-s", serial, "exec-out", "uiautomator", "dump", "/dev/tty"],
        capture_output=True,
        **_TEXT_DECODE,
        check=False,
        timeout=25,
    )
    xml_out = proc.stdout or ""
    if proc.returncode != 0 or "<hierarchy" not in xml_out:
        if not allow_device_file:
            raise UiDumpUnavailableError(
                "Direct UI dump failed and the on-device file fallback is not permitted."
            )
        remote_path = f"/sdcard/adapt-ui-{uuid.uuid4().hex}.xml"
        try:
            dump_proc = runner(
                [str(adb_path), "-s", serial, "shell", "uiautomator", "dump", remote_path],
                capture_output=True,
                **_TEXT_DECODE,
                check=False,
                timeout=25,
            )
            if dump_proc.returncode != 0:
                raise RuntimeError("UI dump failed")
            cat_proc = runner(
                [str(adb_path), "-s", serial, "exec-out", "cat", remote_path],
                capture_output=True,
                **_TEXT_DECODE,
                check=False,
                timeout=15,
            )
            if cat_proc.returncode != 0:
                raise RuntimeError("UI dump read failed")
            xml_out = cat_proc.stdout or ""
        finally:
            try:
                runner(
                    [str(adb_path), "-s", serial, "shell", "rm", "-f", remote_path],
                    capture_output=True,
                    **_TEXT_DECODE,
                    check=False,
                    timeout=10,
                )
            except (OSError, subprocess.SubprocessError):
                pass

    if "<hierarchy" not in xml_out:
        err_msg = (proc.stderr or "").strip() or "uiautomator dump returned no hierarchy XML"
        raise RuntimeError(err_msg)

    return parse_ui_summary(xml_out, max_nodes=max_nodes)
