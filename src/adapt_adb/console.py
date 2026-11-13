"""Pure human-console presentation; never probes a host or a device."""

from __future__ import annotations

import os
import re
import textwrap
from typing import Any

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_COLORS = {"READY": "32", "WAITING": "33", "BLOCKED": "31", "SKIP": "90"}
_LABELS = {"pass": "READY", "warn": "WAITING", "fail": "BLOCKED", "skip": "SKIP"}
_ACTION_LABELS = {
    "everything": "Give Me Everything (preview and confirm)",
    "setup": "Install Platform-Tools (adapt setup)",
    "device_manager": "Open Windows Device Manager",
    "udev": "Configure Linux USB permissions (requires sudo)",
    "wireless": "Pair or connect over Wi-Fi",
    "recheck": "Run checks again",
    "done": "Exit",
}
_CHECK_LABELS = {
    "adb_server_5037": "ADB daemon",
    "adb_path": "Platform-Tools",
    "adb_version": "ADB version",
    "adb_origin": "Distribution source",
    "path_config": "Command path",
    "android_home": "Android SDK",
    "windows_pnp": "Windows USB driver",
    "debian_usb_permissions": "Linux USB access",
    "devices_state": "Device transports",
}


def _safe(value: Any) -> str:
    # Reports include host filenames and tool output; never interpret their
    # control characters as terminal instructions.
    return _CONTROL.sub(lambda m: f"\\x{ord(m[0]):02x}", str(value))


def render_console_report(
    report: dict[str, Any],
    actions: list[dict[str, str]],
    *,
    show_fix: bool = False,
    width: int = 80,
    color: bool = False,
) -> str:
    """Render a compact, width-bounded view of an existing diagnostic report.

    The caller chooses interactive mode. Machine JSON and redirected diagnostic
    output continue to use their existing renderers. Status is always textual;
    colors add emphasis only. No device identity or state is inferred here.
    """
    width = max(12, width)
    color = color and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"
    lines: list[str] = []

    def add(value: str = "", indent: str = "") -> None:
        if not value:
            lines.append("")
            return
        lines.extend(textwrap.wrap(
            _safe(value), width=width, initial_indent=indent,
            subsequent_indent=indent, break_long_words=True,
            break_on_hyphens=False, replace_whitespace=False,
        ))

    def badge(status: str) -> str:
        label = _LABELS.get(status, "UNKNOWN")
        return f"\x1b[{_COLORS[label]}m{label}\x1b[0m" if color and label in _COLORS else label

    status = {"ok": "READY", "missing_toolchain": "BLOCKED",
              "unsafe_shared_server": "BLOCKED",
              "device_unauthorized_or_no_permissions": "WAITING"}.get(str(report.get("status", "")), "UNKNOWN")
    add(f"ADAPT / HOST CHECK  |  {status}")
    add("-" * min(width, 64))
    host, tools = report.get("host", {}), report.get("toolchain", {})
    for key, value in (
        ("OS", f"{host.get('os', 'unknown')} ({host.get('arch', 'unknown')})"),
        ("ADB Version", tools.get("platform_tools_version") or "Unavailable"),
        ("ADB Path", tools.get("adb_path") or "Not found"),
        ("Origin", tools.get("origin", "unknown")),
    ):
        add(f"{key}: {value}", "  ")
    add(f"Status: {report.get('status', 'unknown')} (exit code {report.get('exit_code', '?')})", "  ")
    add()
    add("CHECKS")
    for check in report.get("checks", []):
        # Wrap plain text first so ANSI bytes never count toward visible width.
        start = len(lines)
        label = _LABELS.get(check.get("status", ""), "UNKNOWN")
        name = _CHECK_LABELS.get(check.get("id", ""), check.get("id", "check"))
        add(f"{label:<7}  {name} / {check.get('summary', '')}", "  ")
        if color and len(lines) > start:
            lines[start] = lines[start].replace(label, badge(check.get("status", "")), 1)
    add()
    devices = report.get("devices")
    if not isinstance(devices, dict) or "by_state" not in devices:
        devices = next((c.get("details", {}) for c in report.get("checks", []) if c.get("id") == "devices_state"), {})
    counts = devices.get("by_state") if isinstance(devices, dict) else None
    if isinstance(counts, dict):
        add("TRANSPORTS / reported connection counts")
        add("  ".join(f"{key}={counts[key]}" for key in ("device", "unauthorized", "offline", "no_permissions", "other") if key in counts), "  ")
        add()
    add("Next: ACTIONS")
    # Action order/keys and enablement are owned by build_next_actions.
    for action in actions:
        label = _ACTION_LABELS.get(action.get("id", ""), action["label"])
        add(f"[{action['key']}] {label}", "  ")
    if not actions:
        add("No actions available.", "  ")
    if show_fix:
        add()
        add("Safe Remediation Commands (--fix, read-only preview):")
        fixes = [c for c in report.get("checks", []) if c.get("status") in {"fail", "warn"} and c.get("remediation")]
        for check in fixes:
            add(f"{check['id']}: {check['remediation']}", "  ")
        if not fixes:
            add("No remediation actions needed.", "  ")
    return "\n".join(lines)
