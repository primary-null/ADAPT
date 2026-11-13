"""Server-side session restrictions, independent of an AI client's UI prompts."""

from dataclasses import dataclass
from typing import Any

MUTATING_TOOLS = frozenset({"tap", "swipe", "key", "type", "install_apk"})
OBSERVATION_TOOLS = frozenset({"doctor", "setup_status", "list_devices", "device_info", "screenshot", "ui_summary", "logcat_tail", "android_versions", "android_capabilities", "developer_options_inspect", "tool_catalog", "dex_inspect", "dex_displays"})
DEVICE_TOOLS = frozenset({"device_info", "screenshot", "ui_summary", "tap", "swipe", "key", "type", "install_apk", "logcat_tail", "android_capabilities", "developer_options_inspect", "dex_inspect", "dex_displays"})
OBSERVATION_TOOLS |= frozenset({"app_validate", "app_logs", "debug_app_inspect", "build_modes", "diagnostic_logs", "logging_guide"})
DEVICE_TOOLS |= frozenset({"app_validate", "app_logs", "debug_app_inspect", "diagnostic_logs"})


@dataclass(frozen=True)
class AccessPolicy:
    read_only: bool = False
    allowed_devices: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.allowed_devices is not None and (
            not isinstance(self.allowed_devices, tuple) or not self.allowed_devices
            or any(not isinstance(s, str) or not s or s != s.strip() or s.startswith("-") or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in s) for s in self.allowed_devices)
        ):
            raise ValueError("Invalid transport restriction")

    def exposes(self, name: str) -> bool:
        if self.read_only and name not in OBSERVATION_TOOLS:
            return False
        # Doctor describes the entire host, including all transports. It is not
        # presented as device-scoped when a session has a target restriction.
        return not (self.allowed_devices is not None and name == "doctor")

    def permits(self, name: str, arguments: dict[str, Any]) -> bool:
        if not self.exposes(name):
            return False
        if self.allowed_devices is not None and name in DEVICE_TOOLS:
            return arguments.get("device") in self.allowed_devices
        return True

    def filter_result(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.allowed_devices is not None and isinstance(payload.get("devices"), list):
            payload = dict(payload)
            payload["devices"] = [d for d in payload["devices"] if isinstance(d, dict) and d.get("device", d.get("serial")) in self.allowed_devices]
        return payload
