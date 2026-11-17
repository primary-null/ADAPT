"""MCP tool definitions and implementations for ADAPT v1."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import adapt_adb.adb as _adb_mod
from adapt_adb.adb import AdbProbeError
from adapt_adb.doctor import UnsafeAdbServerError
from adapt_adb.failure_guide import GUIDES
from adapt_adb.failure_report import UNSAFE_SHARED_SERVER_MESSAGE, classify, tag_payload
from adapt_adb.mcp.access import MAX_DEVICE_LENGTH
from adapt_adb.render.screenshot import capture_screenshot_jpeg
from adapt_adb.render.ui_summary import UiDumpUnavailableError, capture_ui_summary
from adapt_adb.safety import is_log_mark

_ORIG_GET_CONNECTED_DEVICES = _adb_mod.get_connected_devices

ALLOWED_KEYS = ("BACK", "HOME", "ENTER", "APP_SWITCH", "POWER", "DEL")
ALLOWED_LOG_LEVELS = ("V", "D", "I", "W", "E", "F")
# Developer Options profiles `dev_options_apply` accepts (the same four the CLI knows).
PROFILE_NAMES = ("fast-ui", "touches", "desktop-testing", "stay-awake")

# Argument bounds advertised in TOOL_SCHEMAS and enforced by the server's validator.
MAX_COORDINATE = 20000
DEFAULT_MAX_STRING_LENGTH = 4096
# Cap for any error message or log line built from adb/device output.
MAX_OUTPUT_TEXT_CHARS = 2000


def _device_prop(*, explicit: bool = False) -> dict[str, Any]:
    prop: dict[str, Any] = {"type": "string", "minLength": 1, "maxLength": MAX_DEVICE_LENGTH}
    if explicit:
        prop["description"] = (
            "Exact transport name from list_devices. Required for this tool unless the server "
            "was started with --device naming exactly one target."
        )
    return prop


# Appended to the description of every tool that acts on the phone (see MUTATING_TOOLS).
ACTS_ON_PHONE_NOTE = (
    " Acts on the phone, so `device` must be named explicitly; ADAPT never picks a device for this tool."
)

# need_device message for a tool that acts on the phone and was called without `device`.
EXPLICIT_DEVICE_MESSAGE = (
    "This tool acts on the phone and requires an explicit `device`; ADAPT does not pick one, even "
    "when only one device is connected. Pass `device` with an exact transport from `devices[].device`, "
    "or start the server with `--device SERIAL`."
)
# Guide ID for that case. It takes effect once the failure-guide owner adds the guide; until then
# failure_report falls back to the generic need_device mapping (device.multiple).
EXPLICIT_DEVICE_FAILURE_ID = "device.explicit_required"

# Preferred guide ID for a refused command; see refused_command_error() for the fallback.
REFUSED_COMMAND_FAILURE_ID = "mcp.adb_command_refused"

REFUSED_COMMAND_MESSAGE = (
    "ADAPT's safety rules refused an adb command before it ran; it was not sent to the device."
)


def _coordinate_prop() -> dict[str, Any]:
    return {"type": "integer", "minimum": 0, "maximum": MAX_COORDINATE}


def _since_prop() -> dict[str, Any]:
    return {
        "type": "string", "minLength": 18, "maxLength": 18, "pattern": r"^\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}$",
        "description": "A mark returned by log_mark (device time, MM-DD HH:MM:SS.mmm): only lines logged since then are read.",
    }


def _package_prop(description: str = "Exact application ID, for example com.example.app.") -> dict[str, Any]:
    return {"type": "string", "minLength": 3, "maxLength": 255, "description": description}


def _selector_props() -> dict[str, Any]:
    return {
        "text": {"type": "string", "minLength": 1, "maxLength": 200,
                 "description": "Exact visible text or content description of the element (pass this or `resource_id`)."},
        "resource_id": {"type": "string", "minLength": 1, "maxLength": 200,
                        "description": "Exact resource id such as com.example.app:id/login (pass this or `text`)."},
    }


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "doctor",
        "description": "Inspect platform-tools path, version, daemon status, OS driver/udev hints, and connected devices.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "setup_status",
        "description": "Report whether platform-tools are installed, whether setup would download or update PATH, and OS hints.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "list_devices",
        "description": "List connected Android transports from `adb devices -l`.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "device_info",
        "description": "Return model, Android SDK/release, screen resolution/density, and lock state for a device.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "device": _device_prop(),
            },
        },
    },
    {
        "name": "screenshot",
        "description": "Capture a device screenshot via `exec-out screencap -p` and scale to JPEG (max_dimension <= 1280).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "device": _device_prop(),
                "max_dimension": {"type": "integer", "default": 1024, "minimum": 256, "maximum": 1280},
                "quality": {"type": "integer", "default": 80, "minimum": 40, "maximum": 90},
            },
        },
    },
    {
        "name": "ui_summary",
        "description": "Dump UI hierarchy and return compact interactive/labeled nodes with bounds and centers (no raw XML).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "device": _device_prop(),
            },
        },
    },
    {
        "name": "tap",
        "description": "Tap screen coordinates (x, y) on the target device." + ACTS_ON_PHONE_NOTE,
        "inputSchema": {
            "type": "object",
            "properties": {
                "device": _device_prop(explicit=True),
                "x": _coordinate_prop(),
                "y": _coordinate_prop(),
            },
            "required": ["x", "y"],
        },
    },
    {
        "name": "swipe",
        "description": "Swipe from (x1, y1) to (x2, y2) over duration_ms on the target device." + ACTS_ON_PHONE_NOTE,
        "inputSchema": {
            "type": "object",
            "properties": {
                "device": _device_prop(explicit=True),
                "x1": _coordinate_prop(),
                "y1": _coordinate_prop(),
                "x2": _coordinate_prop(),
                "y2": _coordinate_prop(),
                "duration_ms": {"type": "integer", "default": 300, "minimum": 1, "maximum": 10000},
            },
            "required": ["x1", "y1", "x2", "y2"],
        },
    },
    {
        "name": "key",
        "description": "Send a hardware/navigation key event (BACK, HOME, ENTER, APP_SWITCH, POWER, DEL)." + ACTS_ON_PHONE_NOTE,
        "inputSchema": {
            "type": "object",
            "properties": {
                "device": _device_prop(explicit=True),
                "key": {
                    "type": "string",
                    "enum": list(ALLOWED_KEYS),
                },
            },
            "required": ["key"],
        },
    },
    {
        "name": "type",
        "description": "Type text on the target device using escaped `adb shell input text`." + ACTS_ON_PHONE_NOTE,
        "inputSchema": {
            "type": "object",
            "properties": {
                "device": _device_prop(explicit=True),
                "text": {"type": "string", "maxLength": 2000},
            },
            "required": ["text"],
        },
    },
    {
        "name": "install_apk",
        "description": "Install an APK on the target device (`-r` by default to keep app data)." + ACTS_ON_PHONE_NOTE,
        "inputSchema": {
            "type": "object",
            "properties": {
                "device": _device_prop(explicit=True),
                "apk": {"type": "string", "minLength": 1, "maxLength": 1024},
                "reinstall": {"type": "boolean", "default": True},
                "grant": {"type": "boolean", "default": False},
            },
            "required": ["apk"],
        },
    },
    {
        "name": "logcat_tail",
        "description": (
            "Read recent device-wide logcat lines (finite, never streaming, default 100, max 500, result cut to about 20 KB). "
            "To read only what one step logged, call log_mark first and pass its mark as `since`; buffers are never cleared. "
            "Narrow with `tag` and `level`."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "device": _device_prop(),
                "lines": {"type": "integer", "default": 100, "minimum": 1, "maximum": 500},
                "tag": {"type": "string", "maxLength": 64},
                "level": {"type": "string", "enum": list(ALLOWED_LOG_LEVELS)},
                "since": _since_prop(),
            },
        },
    },
]

TOOL_SCHEMAS.extend([
    {"name": "dex_inspect", "description": "Inspect Samsung DeX state and logical displays on one explicit target; never starts DeX or mirrors.",
     "inputSchema": {"type": "object", "properties": {"device": _device_prop()}, "required": ["device"]}},
    {"name": "dex_displays", "description": "Read existing logical display IDs without creating a virtual display or launching mirrors.",
     "inputSchema": {"type": "object", "properties": {"device": _device_prop()}, "required": ["device"]}},
    {"name": "android_versions", "description": "Read the offline Android 10-17 planning matrix; not a device certification.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "android_capabilities", "description": "Inspect version-dependent capabilities for one explicit transport without changing settings.",
     "inputSchema": {"type": "object", "properties": {"device": _device_prop()}, "required": ["device"]}},
    {"name": "developer_options_inspect", "description": "Read allowlisted Developer Options settings for one explicit target; no profile changes.",
     "inputSchema": {"type": "object", "properties": {"device": _device_prop()}, "required": ["device"]}},
    {"name": "tool_catalog", "description": "Read official tool acquisition guidance and PATH observations; never installs or executes discovered tools.",
     "inputSchema": {"type": "object", "properties": {"goal": {"type": "string", "enum": ["testing", "mirroring", "aosp-build", "lineage-build", "image-inspection", "remote-worker"]}}}},
    {"name": "app_validate", "description": "Check exact current-user package installation and optional version/debuggable expectations. Does not infer R8/signing or launch the app.",
     "inputSchema": {"type": "object", "properties": {"device": _device_prop(), "package": {"type": "string", "minLength": 1, "maxLength": 255}, "expected_version_code": {"type": "integer", "minimum": 0, "maximum": 9223372036854775807}, "expected_debuggable": {"type": "boolean"}}, "required": ["device", "package"]}},
    {"name": "app_logs", "description": "Read an app's own logs (UID-filtered main and crash buffers; finite, result cut to about 20 KB). For one step's logs, call log_mark first and pass its mark as `since`; buffers are never cleared. Empty logs do not prove no crash: check app_state too.",
     "inputSchema": {"type": "object", "properties": {"device": _device_prop(), "package": {"type": "string", "minLength": 1, "maxLength": 255}, "lines": {"type": "integer", "minimum": 1, "maximum": 500}, "buffer": {"type": "string", "enum": ["main", "crash", "main,crash"]}, "level": {"type": "string", "enum": list(ALLOWED_LOG_LEVELS)}, "since": _since_prop()}, "required": ["device", "package"]}},
    {"name": "debug_app_inspect", "description": "Read persisted debug-app/wait settings on an exact device; transient runtime selection and debugger attachment remain unverified.",
     "inputSchema": {"type": "object", "properties": {"device": _device_prop()}, "required": ["device"]}},
    {"name": "diagnostic_logs", "description": "Read recent fixed-tag window/DeX/print system logs. May contain multiple apps/users or sensitive details. No verbose toggles, clearing, export or wider fallback; empty output is not a test pass.",
     "inputSchema": {"type": "object", "properties": {"device": _device_prop(), "topic": {"type": "string", "enum": ["window", "dex", "print"]}, "minutes": {"type": "integer", "minimum": 1, "maximum": 10}, "lines": {"type": "integer", "minimum": 1, "maximum": 500}, "level": {"type": "string", "enum": list(ALLOWED_LOG_LEVELS)}}, "required": ["device", "topic"]}},
    {"name": "logging_guide", "description": "Offline Samsung/vendor/window/print logging guidance; distinguishes producer verbosity, logcat filters and app debugging. Changes nothing.",
     "inputSchema": {"type": "object", "properties": {"topic": {"type": "string", "enum": ["all", "samsung", "vendor", "window", "dex", "print", "wifi", "app"]}}}},
    {"name": "build_modes", "description": "Offline developer/AI guidance distinguishing debuggable, variants, R8, profiling, signing and production evidence.",
     "inputSchema": {"type": "object", "properties": {}}},
])

TOOL_SCHEMAS.extend([
    {"name": "app_launch",
     "description": "Start an installed app so you can test it: opens its launcher activity (or the `activity` you name) and waits for the launch. "
                    "Returns launch time and the resumed activity. Next: ui_summary to see the screen, app_state to check it did not crash."
                    + ACTS_ON_PHONE_NOTE,
     "inputSchema": {"type": "object", "properties": {
         "device": _device_prop(explicit=True), "package": _package_prop(),
         "activity": {"type": "string", "minLength": 1, "maxLength": 255,
                      "description": "Optional activity class, such as .MainActivity or com.example.app.MainActivity. Default: the launcher activity."},
         "wait": {"type": "boolean", "default": True, "description": "Wait for the launch to complete (at most 30 s). False returns at once."},
     }, "required": ["package"]}},
    {"name": "app_stop",
     "description": "Stop an app's processes (force-stop) to test a cold start or end a hung app. App data and the installed build are untouched. "
                    "Next: app_launch." + ACTS_ON_PHONE_NOTE,
     "inputSchema": {"type": "object", "properties": {"device": _device_prop(explicit=True), "package": _package_prop()}, "required": ["package"]}},
    {"name": "app_state",
     "description": "Answer 'did the app crash?' without reading logs: whether the app is running (process ids), whether it is in the foreground, "
                    "and its last recorded exit reason (CRASH, ANR, killed, user stop; Android 11+). Next: app_logs with `since` for the stack trace.",
     "inputSchema": {"type": "object", "properties": {"device": _device_prop(), "package": _package_prop()}, "required": ["device", "package"]}},
    {"name": "app_open_link",
     "description": "Open a web link or deep link on the device (android.intent.action.VIEW) to test routing into your app. Optionally restrict it to "
                    "one app with `package`. Next: ui_summary. Only ASCII URIs with a scheme; javascript:, data: and intent: are refused."
                    + ACTS_ON_PHONE_NOTE,
     "inputSchema": {"type": "object", "properties": {
         "device": _device_prop(explicit=True),
         "uri": {"type": "string", "minLength": 1, "maxLength": 2000, "description": "For example https://example.com/path or myapp://item/42. Percent-encode special characters."},
         "package": _package_prop("Optional: only this app may handle the link."),
     }, "required": ["uri"]}},
    {"name": "log_mark",
     "description": "Take a log mark: the device's current time. Call it just before a step, then pass the mark as `since` to app_logs or logcat_tail "
                    "to read only that step's logs. Nothing is cleared or changed.",
     "inputSchema": {"type": "object", "properties": {"device": _device_prop()}, "required": ["device"]}},
    {"name": "test_run",
     "description": "Run an app's instrumented tests (am instrument) and get pass/fail counts and failures. Waits up to `timeout_s` (default 50); if the run is "
                    "not finished it returns status `running` and a `run_id`: poll test_status. One run per device; a run is stopped after 10 minutes. "
                    "Install the app and test APKs first (install_apk)." + ACTS_ON_PHONE_NOTE,
     "inputSchema": {"type": "object", "properties": {
         "device": _device_prop(explicit=True),
         "test_package": {**_package_prop("Application ID of the test APK, for example com.example.app.test."), "minLength": 1},
         "runner": {"type": "string", "minLength": 1, "maxLength": 255, "description": "Instrumentation runner class, for example androidx.test.runner.AndroidJUnitRunner."},
         "class": {"type": "string", "minLength": 1, "maxLength": 255, "description": "Optional fully qualified test class; narrows the run."},
         "method": {"type": "string", "minLength": 1, "maxLength": 128, "description": "Optional test method; needs `class`."},
         "timeout_s": {"type": "integer", "default": 50, "minimum": 1, "maximum": 55, "description": "How long to wait before returning `running`."},
     }, "required": ["test_package", "runner"]}},
    {"name": "test_status",
     "description": "Check a test run started by test_run: progress while it runs, the parsed result (counts and failures) when it has finished.",
     "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string", "minLength": 12, "maxLength": 12, "description": "The run_id test_run returned."}}, "required": ["run_id"]}},
    {"name": "dev_options_apply",
     "description": "Prepare the phone for testing with a reversible Developer Options profile (fast-ui animations off, touches, stay-awake, desktop-testing). "
                    "ADAPT stores the recovery snapshot itself. Refused while a snapshot for this device and profile is still pending. "
                    "Always call dev_options_restore before you finish." + ACTS_ON_PHONE_NOTE,
     "inputSchema": {"type": "object", "properties": {
         "device": _device_prop(explicit=True), "profile": {"type": "string", "enum": list(PROFILE_NAMES)},
     }, "required": ["profile"]}},
    {"name": "dev_options_restore",
     "description": "Undo what dev_options_apply changed: restores every pending snapshot for this device (newest first) and lists what was pending. "
                    "Call it before you finish; with nothing pending it changes nothing. A setting someone else changed since is left alone and reported."
                    + ACTS_ON_PHONE_NOTE,
     "inputSchema": {"type": "object", "properties": {
         "device": _device_prop(explicit=True), "profile": {"type": "string", "enum": list(PROFILE_NAMES), "description": "Optional: restore only this profile."},
     }}},
    {"name": "permission_grant",
     "description": "Grant a runtime permission (for example android.permission.CAMERA) to an app so a test does not stop at the permission dialog. "
                    "Runtime permissions only. Undo with permission_revoke." + ACTS_ON_PHONE_NOTE,
     "inputSchema": {"type": "object", "properties": {
         "device": _device_prop(explicit=True), "package": _package_prop(),
         "permission": {"type": "string", "minLength": 1, "maxLength": 255, "description": "For example android.permission.CAMERA."},
     }, "required": ["package", "permission"]}},
    {"name": "permission_revoke",
     "description": "Revoke a runtime permission from an app to test the permission-denied path. Runtime permissions only. Undo with permission_grant."
                    + ACTS_ON_PHONE_NOTE,
     "inputSchema": {"type": "object", "properties": {
         "device": _device_prop(explicit=True), "package": _package_prop(),
         "permission": {"type": "string", "minLength": 1, "maxLength": 255, "description": "For example android.permission.CAMERA."},
     }, "required": ["package", "permission"]}},
    {"name": "ui_tap_text",
     "description": "Tap an element by its visible text or resource id: takes a fresh UI dump, then taps the centre of the one match. Prefer this to `tap`: "
                    "no stale coordinates. Fails clearly on no match, or on several matches unless you pass `index`. Next: ui_summary or ui_wait."
                    + ACTS_ON_PHONE_NOTE,
     "inputSchema": {"type": "object", "properties": {
         "device": _device_prop(explicit=True), **_selector_props(),
         "index": {"type": "integer", "minimum": 0, "maximum": 499, "description": "Which match to tap (0-based, screen order) when several match."},
     }}},
    {"name": "ui_wait",
     "description": "Wait for a screen to settle instead of sleeping: polls the UI until an element with this text or resource id appears (or, with `gone`, "
                    "disappears), for at most `timeout_s`. A timeout is a result, not an error. Next: ui_summary.",
     "inputSchema": {"type": "object", "properties": {
         "device": _device_prop(), **_selector_props(),
         "gone": {"type": "boolean", "default": False, "description": "Wait for the element to disappear instead."},
         "timeout_s": {"type": "integer", "default": 10, "minimum": 1, "maximum": 55},
     }, "required": ["device"]}},
])

for _tool_schema in TOOL_SCHEMAS:
    _tool_schema["inputSchema"]["additionalProperties"] = False
    for _prop in _tool_schema["inputSchema"]["properties"].values():
        # Every string is length-bounded in the advertised schema, so what clients see
        # is exactly what the server enforces.
        if _prop.get("type") == "string":
            _prop.setdefault("maxLength", DEFAULT_MAX_STRING_LENGTH)


from adapt_adb.safety import ForbiddenArgv, SafetyError, validate_adb_args as _safety_validate_adb_args


class ForbiddenCommandError(ForbiddenArgv):
    """Raised when an ADB invocation violates ADAPT v1 safety rules."""


def tool_dex_inspect(device: str, *, displays_only: bool = False) -> dict[str, Any]:
    from adapt_adb.dex import DexError, inspect_device, list_displays
    try:
        return list_displays(device) if displays_only else inspect_device(device)
    except DexError as exc:
        # `error` and `message` are unchanged; the remembered guide ID makes the dispatcher's
        # `failure` object specific to this DexError (for example device.unreachable).
        payload = {"error": exc.kind, "message": "DeX inspection failed; run the targeted CLI inspection for details.", "devices": []}
        return tag_payload(payload, classify(exc, area="dex"))


def tool_android_capabilities(device: str) -> dict[str, Any]:
    from adapt_adb.android_support import AndroidSupportError, inspect_capabilities
    try:
        return inspect_capabilities(device)
    except AndroidSupportError as exc:
        return tag_payload({"error": exc.kind, "message": truncate_text(exc), "devices": []},
                           classify(exc, area="android_support"))


def tool_android_versions() -> dict[str, Any]:
    from adapt_adb.android_support import version_matrix
    return version_matrix()


def tool_developer_options_inspect(device: str) -> dict[str, Any]:
    from adapt_adb.dev_options import DevOptionsError, inspect_device
    try:
        return inspect_device(device)
    except DevOptionsError as exc:
        payload = {"error": exc.kind, "message": "Developer Options inspection failed; run the targeted CLI inspection for details.", "devices": []}
        return tag_payload(payload, classify(exc, area="dev_options"))


def tool_catalog(goal: str | None = None) -> dict[str, Any]:
    from adapt_adb.tool_catalog import build_list_report
    return build_list_report(goal)


def tool_development(name: str, **arguments: Any) -> dict[str, Any]:
    from adapt_adb import development
    from adapt_adb import diagnostics, dev_options
    operations = {"app_validate": development.validate_app, "app_logs": development.capture_logs,
                  "diagnostic_logs": diagnostics.capture_diagnostics, "logging_guide": diagnostics.logging_guide,
                  "debug_app_inspect": development.inspect_debug, "build_modes": development.build_modes}
    if "device" in arguments:
        arguments["serial"] = arguments.pop("device")
    try:
        report = operations[name](**arguments)
    except SafetyError:
        raise  # the dispatcher reports the refusal without echoing the argv
    except (ValueError, dev_options.DevOptionsError) as exc:
        # `error` and `message` are unchanged; the remembered guide ID makes the dispatcher's
        # `failure` object specific (for example device.unanswered for a phone that did not answer).
        return tag_payload({"error": "development_inspection_failed", "message": truncate_text(exc), "devices": []}, classify(exc))
    if report.get("status") in {"failed", "inconclusive", "partial"}:
        report = {**report, "error": "checks_failed" if report["status"] == "failed" else "incomplete_evidence"}
        return tag_payload(report, development.report_failure_id(report))
    return report


def validate_adb_args(argv: list[str]) -> None:
    """Enforce ADAPT v1 safety rules on ADB argv before spawning subprocess."""
    if not argv:
        raise ForbiddenCommandError("Empty command is not allowed.")

    first_name = Path(argv[0]).name.lower()
    if first_name in ("sudo", "sudo.exe"):
        raise ForbiddenCommandError("Forbidden: sudo is not allowed.")

    safety_args = argv
    if argv and Path(argv[0]).name.lower() in {"adb", "adb.exe"}:
        safety_args = argv[1:]
    try:
        _safety_validate_adb_args(safety_args)
    except ForbiddenArgv as exc:
        raise ForbiddenCommandError(str(exc)) from exc

    if safety_args and safety_args[0] == "logcat" and "-d" not in safety_args:
        raise ForbiddenCommandError("Forbidden: unbounded logcat without -d is not allowed.")


def resolve_adb() -> Path | None:
    """Resolve the adb binary, checking ADAPT_ADB / ADB_PATH first, then adapt_adb.adb.resolve_adb()."""
    for env_var in ("ADAPT_ADB", "ADB_PATH"):
        val = os.environ.get(env_var)
        if val:
            cand = Path(val)
            if cand.is_file():
                return cand.resolve()
    return _adb_mod.resolve_adb()


def truncate_text(text: Any, limit: int = MAX_OUTPUT_TEXT_CHARS, *, strip: bool = True) -> str:
    """Return text capped at `limit` characters, marking any cut with an explicit suffix."""
    value = str(text or "")
    if strip:
        value = value.strip()
    if len(value) <= limit:
        return value
    return f"{value[:limit]} [truncated {len(value) - limit} chars]"


def _adb_error_message(proc: subprocess.CompletedProcess[Any], fallback: str) -> str:
    """Bounded error text from a failed adb process (stderr, else stdout, else fallback)."""
    return truncate_text(proc.stderr or proc.stdout or fallback) or fallback


def unsafe_server_error(exc: BaseException) -> dict[str, Any]:
    """Fail closed for an unverified/unsafe shared ADB server; ADAPT did not contact it.

    The payload is a plain error dict that also remembers the specific guide ID
    (endpoint, listener, owner or version) for the dispatcher's `failure` object.
    """
    payload = {"error": "unsafe_shared_server", "message": UNSAFE_SHARED_SERVER_MESSAGE, "devices": []}
    return tag_payload(payload, classify(exc))


def refused_command_error() -> dict[str, Any]:
    """Payload for an adb argv the safety guard refused (`SafetyError`, `ForbiddenArgv`, `ForbiddenCommandError`).

    The refused argv, the exception text and any device text are deliberately not echoed.
    The MCP-specific guide is used once it exists; until then the CLI guide for refused
    arguments describes the same situation.
    """
    payload = {"error": "adb_command_refused", "message": REFUSED_COMMAND_MESSAGE, "devices": []}
    guide = REFUSED_COMMAND_FAILURE_ID if REFUSED_COMMAND_FAILURE_ID in GUIDES else "input.adb_args_refused"
    return tag_payload(payload, guide)


def _guard_adb_command(adb_path: Path, command_args: list[str]) -> None:
    """Apply the ADAPT argv guard and the port-5037 preflight before any adb spawn."""
    validate_adb_args(command_args)
    from adapt_adb.doctor import preflight_adb_server

    preflight_adb_server(adb_path, command_args)


def guarded_runner(adb_path: Path) -> Any:
    """Return a `subprocess.run`-compatible runner that guards every adb argv it is given.

    Used for render helpers that build their own adb argv, so they pass the same
    safety check and shared-server preflight as `run_adb`.
    """

    def _run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        argv = [str(item) for item in argv]
        if not argv or argv[0] != str(adb_path):
            raise ForbiddenCommandError("Forbidden: runner only executes the resolved adb binary.")
        _guard_adb_command(adb_path, argv[1:])
        return subprocess.run(argv, **kwargs)

    return _run


def run_adb(
    adb_path: Path,
    args: list[str],
    *,
    serial: str | None = None,
    binary: bool = False,
    timeout: int = 20,
) -> subprocess.CompletedProcess[Any]:
    """Run adb safely with optional `-s <serial>`, capturing output so stdout stays clean.

    Text output is decoded as UTF-8 (device text is UTF-8) with undecodable bytes replaced.
    """
    if serial is not None and not serial:
        raise ValueError("Device serial must not be empty when targeting a device.")
    command_args = (["-s", serial] if serial is not None else []) + list(args)
    full_argv = [str(adb_path)] + command_args
    _guard_adb_command(adb_path, command_args)
    decode: dict[str, Any] = {"text": False} if binary else {"text": True, "encoding": "utf-8", "errors": "replace"}
    return subprocess.run(
        full_argv,
        capture_output=True,
        check=False,
        timeout=timeout,
        **decode,
    )


def parse_devices_l(output: str) -> list[dict[str, str]]:
    """Parse TAB-delimited ``adb devices -l`` without redefining serial identity."""
    identities = _adb_mod.parse_devices_short(output)
    enriched = _adb_mod.enrich_devices_long(identities, output)
    return [
        {
            "device": item["serial"],
            "state": item["state"],
            "model": item.get("model", ""),
            "product": item.get("product", ""),
            "transport": item.get("transport", "usb"),
        }
        for item in enriched
    ]


def checked_stdout(proc: subprocess.CompletedProcess[Any], operation: str) -> str:
    """Return stdout only for a successful ADB probe."""
    if proc.returncode != 0:
        raise AdbProbeError(f"{operation} failed (exit {proc.returncode})")
    return proc.stdout or ""


def list_connected_devices(adb_path: Path) -> list[dict[str, str]]:
    """List connected transports via `adb devices -l` (or monkeypatched get_connected_devices)."""
    if _adb_mod.get_connected_devices is not _ORIG_GET_CONNECTED_DEVICES:
        raw_list = _adb_mod.get_connected_devices(adb_path)
        normalized: list[dict[str, str]] = []
        for item in raw_list:
            serial = item.get("device") or item.get("serial") or ""
            normalized.append(
                {
                    "device": serial,
                    "state": item.get("state", "device"),
                    "model": item.get("model", ""),
                    "product": item.get("product", ""),
                    "transport": item.get("transport", "usb"),
                }
            )
        return normalized

    short_proc = run_adb(adb_path, ["devices"], timeout=10)
    identities = _adb_mod.parse_devices_short(checked_stdout(short_proc, "device enumeration"))
    long_proc = run_adb(adb_path, ["devices", "-l"], timeout=10)
    long_output = checked_stdout(long_proc, "device metadata enumeration")
    enriched = _adb_mod.enrich_devices_long(identities, long_output)
    return [
        {
            "device": item["serial"],
            "state": item["state"],
            "model": item.get("model", ""),
            "product": item.get("product", ""),
            "transport": item.get("transport", "usb"),
        }
        for item in enriched
    ]


def _compact_device_list(devices: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        {
            "device": d["device"],
            "model": d.get("model", ""),
            "state": d.get("state", ""),
        }
        for d in devices
    ]


def resolve_target_device(
    adb_path: Path,
    requested_device: str | None = None,
    *,
    require_explicit: bool = False,
) -> tuple[str | None, dict[str, Any] | None, list[dict[str, str]]]:
    """Resolve target device serial according to MCP-V1 targeting rules.

    With `require_explicit=True` (tools that act on the phone) a call that names no device is
    answered with `need_device` and the candidate list, even if only one device is ready.

    Returns (serial, error_dict, all_devices).
    """
    try:
        devices = list_connected_devices(adb_path)
    except UnsafeAdbServerError as exc:
        return None, unsafe_server_error(exc), []
    except (AdbProbeError, subprocess.TimeoutExpired, OSError, ValueError):
        return (
            None,
            {
                "error": "adb_probe_failed",
                "message": "ADB device enumeration failed; target state is unknown.",
                "devices": [],
            },
            [],
        )
    compact = _compact_device_list(devices)
    ready = [d for d in devices if d.get("state") == "device"]

    req = (requested_device or "").strip()
    if not req and require_explicit:
        return (
            None,
            tag_payload(
                {"error": "need_device", "message": EXPLICIT_DEVICE_MESSAGE, "devices": compact},
                EXPLICIT_DEVICE_FAILURE_ID,
            ),
            devices,
        )
    if req:
        matched = next((d for d in devices if d["device"] == req), None)
        if matched is None:
            return (
                None,
                {
                    "error": "unknown_device",
                    "message": f"Device '{req}' not found among connected transports.",
                    "devices": compact,
                },
                devices,
            )
        if matched.get("state") != "device":
            return (
                None,
                {
                    "error": "no_device",
                    "message": (
                        f"Device '{req}' is in state '{matched.get('state')}'. "
                        "Check unauthorized RSA prompt, missing WinUSB/udev, or run 'adapt doctor'."
                    ),
                    "devices": compact,
                },
                devices,
            )
        return matched["device"], None, devices

    if len(ready) == 0:
        return (
            None,
            {
                "error": "no_device",
                "message": (
                    "No connected device in 'device' state. "
                    "Check unauthorized RSA prompt, missing WinUSB/udev, or run 'adapt doctor'."
                ),
                "devices": compact,
            },
            devices,
        )

    if len(ready) > 1:
        return (
            None,
            {
                "error": "need_device",
                "message": "Pass device; more than one connected.",
                "devices": compact,
            },
            devices,
        )

    return ready[0]["device"], None, devices


def resolve_action_target(
    adb_path: Path,
    requested_device: str | None = None,
) -> tuple[str | None, dict[str, Any] | None, list[dict[str, str]]]:
    """Resolve the target of a tool that acts on the phone: the device must be named explicitly."""
    if (requested_device or "").strip():
        return resolve_target_device(adb_path, requested_device)
    return resolve_target_device(adb_path, None, require_explicit=True)


def _extract_version_number(adb_path: Path) -> str | None:
    try:
        proc = run_adb(adb_path, ["version"], timeout=10)
        text = f"{proc.stdout or ''}\n{proc.stderr or ''}"
        m = re.search(r"Version\s+([0-9]+(?:\.[0-9]+)+)", text)
        if m:
            return m.group(1)
        m2 = re.search(r"version\s+([0-9]+(?:\.[0-9]+)+)", text, re.IGNORECASE)
        if m2:
            return m2.group(1)
        for line in text.splitlines():
            if line.strip():
                return line.strip()
    except (subprocess.SubprocessError, OSError):
        pass
    return _adb_mod.get_adb_version(adb_path)


def tool_doctor() -> dict[str, Any]:
    """Execute the `doctor` MCP tool."""
    import adapt_adb.cli as _cli_mod
    from adapt_adb.doctor import (
        EXIT_UNSAFE_SHARED_SERVER,
        parse_platform_tools_version,
        run_doctor_checks,
    )

    active_env = getattr(_cli_mod, "_ACTIVE_DOCTOR_ENV", None)
    report = run_doctor_checks(active_env, command_name="doctor")

    checks_by_id = {
        c["id"]: c for c in report.get("checks", []) if isinstance(c, dict) and "id" in c
    }
    srv_check = checks_by_id.get("adb_server_5037", {})
    srv_details = srv_check.get("details") or {}
    srv_exit_code = srv_check.get("exit_code", 0)

    toolchain = report.get("toolchain") or {}
    report_adb_str = toolchain.get("adb_path")
    report_adb_path = Path(report_adb_str) if report_adb_str else None

    has_env_override = bool(os.environ.get("ADAPT_ADB") or os.environ.get("ADB_PATH"))
    if has_env_override:
        adb_path = resolve_adb()
    elif report_adb_path is not None:
        adb_path = report_adb_path
    elif active_env is None:
        adb_path = resolve_adb()
    else:
        adb_path = None

    host_os = (report.get("host") or {}).get("os")
    if host_os == "windows":
        is_win = True
        os_name = "windows"
    elif host_os in ("debian", "linux"):
        is_win = False
        os_name = "linux"
    else:
        is_win = sys.platform == "win32"
        os_name = "windows" if is_win else ("linux" if sys.platform.startswith("linux") else sys.platform)

    default_dir = _adb_mod.get_default_platform_tools_dir()
    managed = False
    adb_version: str | None = None

    if adb_path is not None:
        try:
            managed = adb_path.resolve().parent == default_dir.resolve()
        except OSError:
            managed = False

        if report_adb_path is not None and adb_path == report_adb_path:
            raw_pt_ver = toolchain.get("platform_tools_version")
            if raw_pt_ver:
                m_ver = re.match(r"^(\d+(?:\.\d+)+)", str(raw_pt_ver))
                adb_version = m_ver.group(1) if m_ver else str(raw_pt_ver)
        if adb_version is None:
            adb_version = _extract_version_number(adb_path)

    unsafe_server = srv_exit_code == EXIT_UNSAFE_SHARED_SERVER
    daemon = "missing_adb"
    daemon_reason: str | None = None

    if unsafe_server:
        owner = srv_details.get("owner")
        owner_uid = srv_details.get("owner_uid")
        owner_matches = srv_details.get("owner_matches_current_user")
        tmp_5037_root = bool(srv_details.get("tmp_5037_owner_is_root"))

        # Doctor already decided whether the owner is root/SYSTEM; do not re-derive it here.
        is_root_or_system = tmp_5037_root or bool(srv_details.get("owner_is_root_or_system"))

        if srv_details.get("version_mismatch"):
            daemon = "version_mismatch"
        elif is_root_or_system:
            daemon = "root_owned"
        elif owner_matches is False and (owner is not None or owner_uid is not None):
            daemon = "foreign_owner"
        else:
            daemon = "unverified"

        daemon_reason = srv_check.get("summary") or "ADB server 5037 is in an unsafe state."
    elif adb_path is not None:
        if (
            srv_details.get("listening")
            and srv_details.get("server_version")
            and adb_version
        ):
            srv_ver = parse_platform_tools_version(
                f"Version {srv_details['server_version']}"
            ).get("version_tuple")
            cli_ver = parse_platform_tools_version(f"Version {adb_version}").get(
                "version_tuple"
            )
            if srv_ver is not None and cli_ver is not None and srv_ver != cli_ver:
                unsafe_server = True
                daemon = "version_mismatch"
                daemon_reason = (
                    srv_check.get("summary")
                    or f"Running ADB server version ({srv_details.get('server_version')}) mismatches resolved adb ({adb_version})."
                )

        if not unsafe_server:
            daemon = "ok"

    devices: list[dict[str, str]] = []
    if not unsafe_server and srv_exit_code != EXIT_UNSAFE_SHARED_SERVER and adb_path is not None:
        devices = list_connected_devices(adb_path)

    udev_status: str | None = None
    plugdev_status: bool | None = None
    if not is_win:
        deb_check = checks_by_id.get("debian_usb_permissions", {})
        deb_details = deb_check.get("details") or {}
        if "has_udev_rules" in deb_details:
            udev_status = "present" if deb_details.get("has_udev_rules") else "missing"
            plugdev_status = bool(deb_details.get("in_plugdev_group"))

    return {
        "adb_path": str(adb_path) if adb_path else None,
        "adb_version": adb_version,
        "managed": managed,
        "daemon": daemon,
        "daemon_reason": daemon_reason,
        "os": os_name,
        "driver_hint": "Google USB / WinUSB" if is_win else "udev 51-android.rules + plugdev",
        "udev": udev_status,
        "plugdev": plugdev_status,
        "devices": _compact_device_list(devices),
    }


def tool_setup_status() -> dict[str, Any]:
    """Execute the `setup_status` MCP tool."""
    from adapt_adb.doctor import parse_platform_tools_version

    adb_path = resolve_adb()
    default_dir = _adb_mod.get_default_platform_tools_dir()
    path_env = os.environ.get("PATH", "")
    path_parts = [p.strip().lower() for p in path_env.split(os.pathsep) if p.strip()]
    adb_dir_on_path = adb_path is not None and str(adb_path.parent).lower() in path_parts
    in_path = str(default_dir).lower() in path_parts or adb_dir_on_path

    if sys.platform == "win32":
        hints = ["Google USB driver if Device Manager shows Android"]
    else:
        hints = [
            "Install /etc/udev/rules.d/51-android.rules and add user to plugdev group"
        ]

    usable = False
    distribution_package = False
    if adb_path is not None:
        norm_path = str(adb_path).replace("\\", "/").lower()
        distribution_package = norm_path in ("/usr/bin/adb", "/bin/adb") or norm_path.startswith("/usr/lib/android-sdk")
        ver_str = _extract_version_number(adb_path)
        parsed = parse_platform_tools_version(f"Version {ver_str}" if ver_str else None)
        # A distribution-packaged adb is usable when it meets the minimum (doctor warns about its
        # origin instead of failing); an unparseable or older version is never usable.
        usable = bool(parsed.get("meets_minimum"))

    return {
        "installed": usable,
        "distribution_package": distribution_package,
        "adb_path": str(adb_path) if adb_path else str(default_dir / ("adb.exe" if sys.platform == "win32" else "adb")),
        "would_download": not (usable and adb_dir_on_path),
        "would_path": not in_path,
        "os_hints": hints,
    }


def tool_list_devices() -> dict[str, Any]:
    """Execute the `list_devices` MCP tool."""
    adb_path = resolve_adb()
    if adb_path is None:
        return {
            "error": "no_adb",
            "message": "adb binary not found. Run 'adapt setup' or 'adapt doctor'.",
            "devices": [],
        }
    try:
        devices = list_connected_devices(adb_path)
    except UnsafeAdbServerError as exc:
        return unsafe_server_error(exc)
    except (AdbProbeError, subprocess.TimeoutExpired, OSError, ValueError):
        return {
            "error": "adb_probe_failed",
            "message": "ADB device enumeration failed; device state is unknown.",
            "devices": [],
        }
    return {"devices": devices}


def tool_device_info(device: str | None = None) -> dict[str, Any]:
    """Execute the `device_info` MCP tool."""
    adb_path = resolve_adb()
    if adb_path is None:
        return {
            "error": "no_adb",
            "message": "adb binary not found. Run 'adapt setup' or 'adapt doctor'.",
            "devices": [],
        }

    serial, err, devices = resolve_target_device(adb_path, device)
    if err is not None or serial is None:
        return err or {"error": "no_device", "message": "No target device.", "devices": []}

    matched = next((d for d in devices if d["device"] == serial), {})
    try:
        model_prop = checked_stdout(
            run_adb(adb_path, ["shell", "getprop", "ro.product.model"], serial=serial),
            "model probe",
        )
        sdk_prop = checked_stdout(
            run_adb(adb_path, ["shell", "getprop", "ro.build.version.sdk"], serial=serial),
            "SDK probe",
        )
        rel_prop = checked_stdout(
            run_adb(adb_path, ["shell", "getprop", "ro.build.version.release"], serial=serial),
            "release probe",
        )
        wm_size = checked_stdout(
            run_adb(adb_path, ["shell", "wm", "size"], serial=serial), "screen-size probe"
        )
        wm_density = checked_stdout(
            run_adb(adb_path, ["shell", "wm", "density"], serial=serial),
            "screen-density probe",
        )
        dumpsys_win = checked_stdout(
            run_adb(adb_path, ["shell", "dumpsys", "window"], serial=serial),
            "lock-state probe",
        )
    except (AdbProbeError, subprocess.TimeoutExpired, OSError):
        return {
            "error": "adb_probe_failed",
            "message": "Required device information probe failed; device state is unknown.",
            "devices": [],
        }

    model = model_prop.strip() or matched.get("model") or "Unknown"
    try:
        sdk = int(sdk_prop.strip())
    except ValueError:
        sdk = 0
    release = rel_prop.strip() or ""

    # `wm` prints "Physical ..." and, when an override is active, "Override ...". The
    # override is what input coordinates are mapped against, so it wins.
    physical_size = _labelled_wm_value(wm_size, "Physical", "size", r"(\d+)x(\d+)")
    override_size = _labelled_wm_value(wm_size, "Override", "size", r"(\d+)x(\d+)")
    size_match = override_size or physical_size or re.search(r"(\d+)x(\d+)", wm_size)
    width, height = (int(size_match.group(1)), int(size_match.group(2))) if size_match else (0, 0)

    physical_density = _labelled_wm_value(wm_density, "Physical", "density", r"(\d+)")
    override_density = _labelled_wm_value(wm_density, "Override", "density", r"(\d+)")
    dens_match = override_density or physical_density or re.search(r"(\d+)", wm_density)
    density = int(dens_match.group(1)) if dens_match else 0

    screen: dict[str, Any] = {
        "width": width,
        "height": height,
        "density": density,
    }
    if override_size or override_density:
        screen["override_active"] = True
        screen["physical"] = {
            "width": int(physical_size.group(1)) if physical_size else 0,
            "height": int(physical_size.group(2)) if physical_size else 0,
            "density": int(physical_density.group(1)) if physical_density else 0,
        }

    lock = "unknown"
    if any(
        marker in dumpsys_win
        for marker in (
            "mDreamingLockscreen=true",
            "isStatusBarKeyguard=true",
            "mShowingLockscreen=true",
        )
    ):
        lock = "locked"
    elif any(
        marker in dumpsys_win
        for marker in (
            "mDreamingLockscreen=false",
            "isStatusBarKeyguard=false",
            "mShowingLockscreen=false",
        )
    ):
        lock = "unlocked"

    return {
        "device": serial,
        "model": model,
        "sdk": sdk,
        "release": release,
        "screen": screen,
        "lock": lock,
    }


def _labelled_wm_value(output: str, label: str, noun: str, value_pattern: str) -> re.Match[str] | None:
    """Match one labelled `wm size`/`wm density` line such as `Override size: 720x1600`."""
    return re.search(rf"^\s*{label}\s+{noun}\s*:\s*{value_pattern}", output, re.IGNORECASE | re.MULTILINE)


def tool_screenshot(
    device: str | None = None,
    max_dimension: int = 1024,
    quality: int = 80,
) -> tuple[str | None, dict[str, Any]]:
    """Execute the `screenshot` MCP tool.

    Returns (base64_jpeg_or_none, metadata_or_error_dict).
    """
    adb_path = resolve_adb()
    if adb_path is None:
        return None, {
            "error": "no_adb",
            "message": "adb binary not found. Run 'adapt setup' or 'adapt doctor'.",
            "devices": [],
        }

    serial, err, _ = resolve_target_device(adb_path, device)
    if err is not None or serial is None:
        return None, err or {"error": "no_device", "message": "No target device.", "devices": []}

    try:
        b64_jpeg, meta = capture_screenshot_jpeg(
            adb_path,
            serial,
            max_dimension=max_dimension,
            quality=quality,
            runner=guarded_runner(adb_path),
        )
        return b64_jpeg, meta
    except (SafetyError, UnsafeAdbServerError):
        raise  # the dispatcher reports these with their specific codes
    except Exception:
        return None, {
            "error": "screenshot_failed",
            "message": "Screenshot capture failed.",
            "devices": [],
        }


def tool_ui_summary(device: str | None = None, *, allow_device_file: bool = True) -> dict[str, Any]:
    """Execute the `ui_summary` MCP tool.

    With `allow_device_file=False` (read-only sessions) the temporary-file fallback,
    which writes and removes a file on the device, is never used.
    """
    adb_path = resolve_adb()
    if adb_path is None:
        return {
            "error": "no_adb",
            "message": "adb binary not found. Run 'adapt setup' or 'adapt doctor'.",
            "devices": [],
        }

    serial, err, _ = resolve_target_device(adb_path, device)
    if err is not None or serial is None:
        return err or {"error": "no_device", "message": "No target device.", "devices": []}

    try:
        return capture_ui_summary(
            adb_path,
            serial,
            runner=guarded_runner(adb_path),
            allow_device_file=allow_device_file,
        )
    except UiDumpUnavailableError:
        return {
            "error": "ui_summary_unavailable_read_only",
            "message": (
                "The direct UI dump failed, and the file-based fallback (which writes a "
                "temporary file on the device) is disabled in read-only sessions."
            ),
            "devices": [],
        }
    except (SafetyError, UnsafeAdbServerError):
        raise  # the dispatcher reports these with their specific codes
    except Exception:
        return {
            "error": "ui_summary_failed",
            "message": "UI summary capture failed.",
            "devices": [],
        }


def tool_tap(x: int, y: int, device: str | None = None) -> dict[str, Any]:
    """Execute the `tap` MCP tool."""
    adb_path = resolve_adb()
    if adb_path is None:
        return {
            "error": "no_adb",
            "message": "adb binary not found. Run 'adapt setup' or 'adapt doctor'.",
            "devices": [],
        }

    serial, err, _ = resolve_action_target(adb_path, device)
    if err is not None or serial is None:
        return err or {"error": "no_device", "message": "No target device.", "devices": []}

    ix, iy = int(x), int(y)
    proc = run_adb(adb_path, ["shell", "input", "tap", str(ix), str(iy)], serial=serial)
    if proc.returncode != 0:
        return {
            "error": "adb_error",
            "message": _adb_error_message(proc, "tap failed"),
            "devices": [],
        }
    return {"ok": True, "tapped": [ix, iy]}


def tool_swipe(
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    duration_ms: int = 300,
    device: str | None = None,
) -> dict[str, Any]:
    """Execute the `swipe` MCP tool."""
    adb_path = resolve_adb()
    if adb_path is None:
        return {
            "error": "no_adb",
            "message": "adb binary not found. Run 'adapt setup' or 'adapt doctor'.",
            "devices": [],
        }

    serial, err, _ = resolve_action_target(adb_path, device)
    if err is not None or serial is None:
        return err or {"error": "no_device", "message": "No target device.", "devices": []}

    args = [
        "shell",
        "input",
        "swipe",
        str(int(x1)),
        str(int(y1)),
        str(int(x2)),
        str(int(y2)),
        str(int(duration_ms)),
    ]
    proc = run_adb(adb_path, args, serial=serial)
    if proc.returncode != 0:
        return {
            "error": "adb_error",
            "message": _adb_error_message(proc, "swipe failed"),
            "devices": [],
        }
    return {"ok": True}


def tool_key(key: str, device: str | None = None) -> dict[str, Any]:
    """Execute the `key` MCP tool."""
    key_norm = (key or "").strip().upper()
    if key_norm not in ALLOWED_KEYS:
        return {
            "error": "invalid_key",
            "message": f"Unsupported key '{key}'. Allowed: {', '.join(ALLOWED_KEYS)}",
            "devices": [],
        }

    adb_path = resolve_adb()
    if adb_path is None:
        return {
            "error": "no_adb",
            "message": "adb binary not found. Run 'adapt setup' or 'adapt doctor'.",
            "devices": [],
        }

    serial, err, _ = resolve_action_target(adb_path, device)
    if err is not None or serial is None:
        return err or {"error": "no_device", "message": "No target device.", "devices": []}

    proc = run_adb(adb_path, ["shell", "input", "keyevent", f"KEYCODE_{key_norm}"], serial=serial)
    if proc.returncode != 0:
        return {
            "error": "adb_error",
            "message": _adb_error_message(proc, "keyevent failed"),
            "devices": [],
        }
    return {"ok": True, "key": key_norm}


def escape_adb_input_text(text: str) -> str:
    """POSIX-quote one control-free argument for Android ``input text``."""
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise ValueError("type accepts single-line text without control characters")
    return shlex.quote(text.replace(" ", "%s"))


def tool_type(text: str, device: str | None = None) -> dict[str, Any]:
    """Execute the `type` MCP tool."""
    try:
        escaped = escape_adb_input_text(text)
    except (TypeError, ValueError):
        return {
            "error": "invalid_params",
            "message": "type accepts single-line text without control characters.",
            "devices": [],
        }
    adb_path = resolve_adb()
    if adb_path is None:
        return {
            "error": "no_adb",
            "message": "adb binary not found. Run 'adapt setup' or 'adapt doctor'.",
            "devices": [],
        }

    serial, err, _ = resolve_action_target(adb_path, device)
    if err is not None or serial is None:
        return err or {"error": "no_device", "message": "No target device.", "devices": []}

    proc = run_adb(adb_path, ["shell", "input", "text", escaped], serial=serial)
    if proc.returncode != 0:
        return {
            "error": "adb_error",
            "message": "Input text operation failed.",
            "devices": [],
        }
    return {"ok": True, "n": len(text)}


def tool_install_apk(
    apk: str,
    reinstall: bool = True,
    grant: bool = False,
    device: str | None = None,
) -> dict[str, Any]:
    """Execute the `install_apk` MCP tool (`-r` when reinstall=True)."""
    adb_path = resolve_adb()
    if adb_path is None:
        return {
            "error": "no_adb",
            "message": "adb binary not found. Run 'adapt setup' or 'adapt doctor'.",
            "devices": [],
        }

    serial, err, _ = resolve_action_target(adb_path, device)
    if err is not None or serial is None:
        return err or {"error": "no_device", "message": "No target device.", "devices": []}

    args = ["install"]
    if reinstall:
        args.append("-r")
    if grant:
        args.append("-g")
    args.append(str(apk))

    proc = run_adb(adb_path, args, serial=serial, timeout=120)
    if proc.returncode != 0:
        return {
            "error": "install_failed",
            "message": _adb_error_message(proc, "install failed"),
            "devices": [],
        }
    return {
        "ok": True,
        "apk": Path(apk).name,
        "reinstall": bool(reinstall),
    }


def tool_logcat_tail(
    lines: int = 100,
    tag: str | None = None,
    level: str | None = None,
    device: str | None = None,
    since: str | None = None,
) -> dict[str, Any]:
    """Execute the `logcat_tail` MCP tool (`adb logcat -d`, default 100, max 500).

    With `since` (a `log_mark`) the window is `logcat -d -T MARK`: everything logged after the mark,
    newest `lines` kept; nothing is cleared.
    """
    if since is not None and not is_log_mark(since):
        return {
            "error": "invalid_params",
            "message": "`since` must be a mark returned by log_mark (MM-DD HH:MM:SS.mmm).",
            "devices": [],
        }
    adb_path = resolve_adb()
    if adb_path is None:
        return {
            "error": "no_adb",
            "message": "adb binary not found. Run 'adapt setup' or 'adapt doctor'.",
            "devices": [],
        }

    serial, err, _ = resolve_target_device(adb_path, device)
    if err is not None or serial is None:
        return err or {"error": "no_device", "message": "No target device.", "devices": []}

    clamped_lines = max(1, min(int(lines), 500))
    args = ["logcat", "-d", *(["-T", since] if since is not None else ["-t", str(clamped_lines)])]

    lvl = (level or "").strip().upper()
    if lvl and lvl not in ALLOWED_LOG_LEVELS:
        return {
            "error": "invalid_level",
            "message": f"Unsupported logcat level '{level}'. Allowed: {', '.join(ALLOWED_LOG_LEVELS)}",
            "devices": [],
        }

    tag_clean = (tag or "").strip()
    if tag_clean:
        args.extend(["-s", f"{tag_clean}:{lvl or 'V'}"])
    elif lvl:
        args.append(f"*:{lvl}")

    proc = run_adb(adb_path, args, serial=serial, timeout=20)
    if proc.returncode != 0:
        return {
            "error": "logcat_failed",
            "message": _adb_error_message(proc, "logcat failed"),
            "devices": [],
        }

    raw_lines = [
        truncate_text(ln.rstrip("\r"), MAX_OUTPUT_TEXT_CHARS, strip=False)
        for ln in (proc.stdout or "").splitlines()
        if ln.strip() and not ln.startswith("--------- beginning of ")
    ]
    return {"lines": raw_lines[-clamped_lines:]}
