"""MCP tools for the agent build-test loop: launch, stop, state, links, log marks, tests,
Developer Options profiles, runtime permissions and UI waits.

Every device command here is an exact, allowlisted argv shape (`safety.AGENT_SHELL_SHAPES`)
whose values passed a strict pattern first; a shape that does not match is refused before adb
runs. Every tool is bounded in time and in output (`bound_payload`, about 20 KB). Nothing here
uses root or `kill-server`, clears data or uninstalls, and nothing chooses a device the caller
did not name. Tools return payload dicts (an `error` key marks a failure) and do not raise
project exceptions; the dispatcher turns an unexpected exception into its generic error.

All results are offline-tested only: no tool in this module has been run against a device.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import secrets
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from adapt_adb import dev_options, safety
from adapt_adb.failure_report import classify, tag_payload
from adapt_adb.mcp import tools as _t
from adapt_adb.mcp.access import mark_untrusted_device_text
from adapt_adb.render.ui_summary import UiDumpUnavailableError, capture_ui_summary

# Result budget: any text result is cut to about this many characters, with a marker and a hint.
MAX_RESULT_CHARS = 20000
LAUNCH_WAIT_SECONDS = 30
# `test_run` waits this long by default, then returns a run id (clients cut a call at about 60 s).
DEFAULT_TEST_WAIT_SECONDS = 50
MAX_TEST_WAIT_SECONDS = 55
MIN_TEST_WAIT_SECONDS = 1
# No instrumentation run, however slow, lives longer than this.
MAX_TEST_RUN_SECONDS = 600
KEEP_FINISHED_RUNS = 8
MAX_FAILURES_RETURNED = 20
MAX_UI_NODES = 500
DEFAULT_UI_WAIT_SECONDS = 10
MAX_UI_WAIT_SECONDS = 55
UI_POLL_SECONDS = 1.0
EXIT_INFO_MIN_SDK = 30

_RUN_ID_RE = re.compile(r"[0-9a-f]{12}")
_RESOURCE_ID_RE = re.compile(r"[A-Za-z0-9_.:/\-]{1,200}")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_MONOTONIC = time.monotonic
_SLEEP = time.sleep

NO_ADB = {"error": "no_adb", "message": "adb binary not found. Run 'adapt setup' or 'adapt doctor'.", "devices": []}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _error(error: str, message: str, guide: str | None = None, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"error": error, "message": message, "devices": [], **extra}
    return tag_payload(payload, guide) if guide else payload


def _untrusted_error(error: str, message: str, guide: str, **extra: Any) -> dict[str, Any]:
    """An error whose payload carries labels read from the screen: marked as device data, then tagged."""
    return tag_payload(mark_untrusted_device_text({"error": error, "message": message, "devices": [], **extra}), guide)


def _clean(text: Any, limit: int = 300) -> str:
    """Device text made safe to return: control characters replaced, length bounded."""
    return _t.truncate_text(_CONTROL_RE.sub(" ", str(text or "")), limit)


def _target(device: str | None) -> tuple[Path, str] | dict[str, Any]:
    """Resolve adb and the named device, or return the error payload.

    Every tool here, reading or acting, needs the device named: a blank `device` is `need_device`,
    never "the only ready phone".
    """
    adb_path = _t.resolve_adb()
    if adb_path is None:
        return dict(NO_ADB)
    serial, err, _ = _t.resolve_action_target(adb_path, device)
    if err is not None or serial is None:
        return err or {"error": "no_device", "message": "No target device.", "devices": []}
    return adb_path, serial


def _bad_package(package: Any) -> dict[str, Any] | None:
    if safety.is_package_name(package):
        return None
    return _error("invalid_package", "`package` must be an exact application ID such as com.example.app.", "app.package_invalid")


def _shell(adb_path: Path, serial: str, args: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str] | dict[str, Any]:
    """Run one allowlisted device command; an unlisted shape is refused before adb runs."""
    if safety.match_agent_shell_shape(args) is None:
        return _t.refused_command_error()
    return _t.run_adb(adb_path, args, serial=serial, timeout=timeout)


def _failed(proc: subprocess.CompletedProcess[Any]) -> bool:
    return proc.returncode != 0


# ---------------------------------------------------------------------------
# Output budget
# ---------------------------------------------------------------------------

_LIST_KEYS_TAIL = ("lines",)  # newest entries are the useful ones: keep the end
_LIST_KEYS_HEAD = ("failures", "candidates", "matches", "pending", "pending_before", "pending_after", "results")


def _size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload))


def bound_payload(payload: dict[str, Any], limit: int = MAX_RESULT_CHARS) -> dict[str, Any]:
    """Cut a result to about `limit` characters; say what was dropped and how to narrow the request."""
    if not isinstance(payload, dict) or _size(payload) <= limit:
        return payload
    result = dict(payload)
    omitted: dict[str, int] = {}
    room = limit - 600  # the `truncated`, `omitted` and `narrow_hint` fields are added afterwards
    for key in (*_LIST_KEYS_TAIL, *_LIST_KEYS_HEAD):
        items = result.get(key)
        if not isinstance(items, list):
            continue
        items = list(items)
        dropped = 0
        while items and _size({**result, key: items}) > room:
            items.pop(0 if key in _LIST_KEYS_TAIL else -1)
            dropped += 1
        result[key] = items
        if dropped:
            omitted[key] = dropped
        if _size(result) <= room:
            break
    if _size(result) > room:
        # Long strings are the rest: shorten each one.
        result = {k: (_t.truncate_text(v, 1000) if isinstance(v, str) else v) for k, v in result.items()}
        omitted["long_text"] = 1
    result["truncated"] = True
    result["omitted"] = omitted
    result["narrow_hint"] = (
        "Result cut to about 20 KB. Narrow the request: fewer `lines`, a `tag` or `level`, a later `since` "
        "mark from `log_mark`, or a single test `class` and `method`."
    )
    guide = getattr(payload, "failure_id", None)
    return tag_payload(result, guide) if guide else result


# ---------------------------------------------------------------------------
# Parsers (pure)
# ---------------------------------------------------------------------------

_AM_FIELD_RE = re.compile(r"^(Status|LaunchState|Activity|TotalTime|WaitTime|ThisTime):\s*(.*)$", re.MULTILINE)
_RESUMED_RE = re.compile(
    r"(?:topResumedActivity|mResumedActivity|ResumedActivity)[=:]\s*ActivityRecord\{[0-9a-f]+ u\d+ "
    r"([A-Za-z0-9_.]+)/([A-Za-z0-9_.$]+)"
)
_EXIT_BLOCK_RE = re.compile(r"ApplicationExitInfo #\d+:")
_EXIT_STAMP_RE = re.compile(r"timestamp=(\d{4})-(\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})")
_EXIT_PID_RE = re.compile(r"\bpid=(\d+)")
_EXIT_REASON_RE = re.compile(r"\breason=(\d+) \(([A-Z_]+)\)")
_EXIT_DESC_RE = re.compile(r"description=(.*?)(?: state=\S*| trace=\S*|$)", re.MULTILINE)
_MARK_OUTPUT_RE = re.compile(r"(\d{2}-\d{2})_(\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}|%N|N)?)?")
CRASH_REASONS = frozenset({"CRASH", "CRASH_NATIVE"})


def parse_am_start(text: str) -> dict[str, Any]:
    """Fields of `am start -W` output; `ok` is False when it reported an error."""
    fields = {key: value.strip() for key, value in _AM_FIELD_RE.findall(text or "")}
    error = re.search(r"^(?:Error\b|[\w.]*Exception\b)[^\n]*$", text or "", re.MULTILINE)
    status = fields.get("Status")
    ok = error is None and status in (None, "ok")
    out: dict[str, Any] = {"ok": ok, "status": status, "error_text": _clean(error.group(0), 200) if error else None}
    out["launch_state"] = fields.get("LaunchState")
    out["component"] = fields.get("Activity") if safety.is_component_name(fields.get("Activity")) else None
    for key, name in (("TotalTime", "total_time_ms"), ("WaitTime", "wait_time_ms")):
        value = fields.get(key, "")
        out[name] = int(value) if value.isdigit() and len(value) <= 9 else None
    return out


def launch_failure_reason(text: str) -> str:
    low = (text or "").lower()
    if "error type 3" in low or "does not exist" in low or "unable to resolve" in low or "unknown package" in low:
        return "activity_or_package_not_found"
    if "permission denial" in low or "not exported" in low or "securityexception" in low:
        return "not_exported_or_denied"
    if "timeout" in low:
        return "launch_timeout"
    return "other"


def parse_resumed_activity(dump: str) -> dict[str, str] | None:
    """The resumed (focused) activity from `dumpsys activity activities`, or None."""
    match = _RESUMED_RE.search(dump or "")
    return {"package": match.group(1), "activity": match.group(2)} if match else None


def parse_pids(text: str) -> list[int]:
    return [int(p) for p in (text or "").split() if p.isdigit() and len(p) <= 10][:20]


def parse_exit_info(dump: str, package: str) -> dict[str, Any] | None:
    """The most recent exit record for `package` from `dumpsys activity exit-info`, or None."""
    chunks = _EXIT_BLOCK_RE.split(dump or "")[1:]
    best: dict[str, Any] | None = None
    for chunk in chunks:
        head = chunk[:1500]
        owner = re.search(r"process=(\S+)", head)
        if owner and owner.group(1) != package and not owner.group(1).startswith(package + ":"):
            continue
        reason = _EXIT_REASON_RE.search(head)
        stamp = _EXIT_STAMP_RE.search(head)
        if not reason or not stamp:
            continue
        pid = _EXIT_PID_RE.search(head)
        desc = _EXIT_DESC_RE.search(head)
        record = {
            "at": stamp.group(2),
            "year": int(stamp.group(1)),
            "pid": int(pid.group(1)) if pid else None,
            "reason": reason.group(2),
            "crash": reason.group(2) in CRASH_REASONS,
            "anr": reason.group(2) == "ANR",
            "description": _clean(desc.group(1), 160) if desc else "",
        }
        if best is None or (record["year"], record["at"]) > (best["year"], best["at"]):
            best = record
    return best


def parse_device_time(text: str) -> tuple[str, str] | None:
    """(`MM-DD HH:MM:SS.mmm`, precision) from `date +%m-%d_%H:%M:%S.%N` output, else None."""
    match = _MARK_OUTPUT_RE.search((text or "").strip())
    if not match:
        return None
    day, clock, frac = match.group(1), match.group(2), match.group(3)
    if frac and frac.isdigit():
        return f"{day} {clock}.{(frac + '000')[:3]}", "ms"
    return f"{day} {clock}.000", "s"


class InstrumentationParser:
    """Incremental parser for `am instrument -r` raw output."""

    def __init__(self) -> None:
        self.passed = self.failed = self.errors = self.ignored = self.skipped = 0
        self.numtests: int | None = None
        self.current_test: str | None = None
        self.failures: list[dict[str, str]] = []
        self.finished = False
        self.result_code: int | None = None
        self.instrumentation_failed: str | None = None
        self.result_message = ""
        self.other_tail: list[str] = []
        self._block: dict[str, str] = {}
        self._result: dict[str, str] = {}
        self._target: dict[str, str] | None = None
        self._key: str | None = None

    def feed(self, raw: str) -> None:
        line = raw.rstrip("\r\n")
        if line.startswith("INSTRUMENTATION_STATUS_CODE:"):
            code = line.split(":", 1)[1].strip()
            self._finish_status(int(code) if re.fullmatch(r"-?\d{1,3}", code) else 0)
        elif line.startswith("INSTRUMENTATION_STATUS:"):
            self._begin(self._block, line.split(":", 1)[1])
        elif line.startswith("INSTRUMENTATION_RESULT:"):
            self._begin(self._result, line.split(":", 1)[1])
        elif line.startswith("INSTRUMENTATION_CODE:"):
            self.finished = True
            code = line.split(":", 1)[1].strip()
            self.result_code = int(code) if re.fullmatch(r"-?\d{1,3}", code) else None
            self._target = None
            self.result_message = _clean(" ".join(self._result.get(k, "") for k in ("shortMsg", "longMsg")), 300)
        elif line.startswith("INSTRUMENTATION_FAILED:"):
            self.instrumentation_failed = _clean(line.split(":", 1)[1], 300) or "instrumentation failed"
        elif self._target is not None and self._key is not None:
            current = self._target.get(self._key, "")
            if len(current) < 4000:
                self._target[self._key] = current + "\n" + line
        elif line.strip():
            self.other_tail = [*self.other_tail[-4:], _clean(line, 200)]

    def _begin(self, target: dict[str, str], rest: str) -> None:
        key, _, value = rest.strip().partition("=")
        self._target, self._key = target, key.strip()
        target[self._key] = value

    def _finish_status(self, code: int) -> None:
        block, self._block = self._block, {}
        self._target = self._key = None
        test, cls = block.get("test", ""), block.get("class", "")
        name = f"{cls}#{test}" if cls and test else test or cls or "unknown"
        if block.get("numtests", "").isdigit():
            self.numtests = int(block["numtests"])
        if code == 1:
            self.current_test = _clean(name, 200)
        elif code == 0:
            self.passed += 1
        elif code in (-1, -2):
            self.failed += code == -2
            self.errors += code == -1
            stack = block.get("stack", "")
            first = next((ln.strip() for ln in stack.splitlines() if ln.strip()), "")
            self.failures.append({
                "test": _clean(name, 200),
                "kind": "failure" if code == -2 else "error",
                "message": _clean(first, 300),
                "stack": _t.truncate_text(_CONTROL_RE.sub(" ", stack.replace("\t", "  ").replace("\n", "\\n")), 1200),
            })
        elif code == -3:
            self.ignored += 1
        elif code == -4:
            self.skipped += 1

    @property
    def tests_run(self) -> int:
        return self.passed + self.failed + self.errors

    def verdict(self) -> tuple[str, str | None]:
        """(status, reason) once the process has ended."""
        if self.instrumentation_failed:
            return "error", f"Instrumentation failed: {self.instrumentation_failed}"
        if self.failed or self.errors:
            return "failed", None
        if self.result_message and re.search(r"crash|died|killed", self.result_message, re.IGNORECASE):
            return "error", f"The instrumentation process ended abnormally: {self.result_message}"
        if not self.finished:
            return "error", "The run ended without a result (no INSTRUMENTATION_CODE); the device may have disconnected."
        if self.tests_run == 0:
            return "error", "The runner finished but reported no tests."
        return "passed", None


# ---------------------------------------------------------------------------
# App lifecycle tools
# ---------------------------------------------------------------------------

def _activity_component(package: str, activity: str) -> str | None:
    """`package/class` for an `activity` argument (class, `.Class` or `package/class`), else None."""
    if "/" in activity:
        pkg, _, cls = activity.partition("/")
        return f"{pkg}/{cls}" if pkg == package and safety.is_class_name(cls) else None
    return f"{package}/{activity}" if safety.is_class_name(activity) else None


def _resolve_launcher(adb_path: Path, serial: str, package: str) -> str | dict[str, Any] | None:
    proc = _shell(adb_path, serial, ["shell", "cmd", "package", "resolve-activity", "--brief", "-a",
                                     "android.intent.action.MAIN", "-c", "android.intent.category.LAUNCHER", package])
    if isinstance(proc, dict):
        return proc
    if _failed(proc):
        return _error("adb_error", _t._adb_error_message(proc, "launcher lookup failed"))
    for line in reversed((proc.stdout or "").splitlines()):
        found = safety.COMPONENT_RE.fullmatch(line.strip())
        if found and found.group(1) == package:
            return line.strip()
    return None


def _resumed_activity(adb_path: Path, serial: str) -> dict[str, str] | None | dict[str, Any]:
    proc = _shell(adb_path, serial, ["shell", "dumpsys", "activity", "activities"], timeout=20)
    if isinstance(proc, dict) or _failed(proc):
        return None
    return parse_resumed_activity(proc.stdout or "")


def tool_app_launch(device: str | None, package: str, activity: str | None = None, wait: bool = True) -> dict[str, Any]:
    """Start an app's launcher (or the named) activity with `am start -W`; report launch time and the resumed activity."""
    bad = _bad_package(package)
    if bad:
        return bad
    component = None
    if activity is not None:
        component = _activity_component(package, activity)
        if component is None:
            return _error("invalid_params", "`activity` must be a class name such as .MainActivity or com.example.app.MainActivity "
                          "(or `package/class` for this package); no `$` or other characters.")
    target = _target(device)
    if isinstance(target, dict):
        return target
    adb_path, serial = target
    if component is None:
        resolved = _resolve_launcher(adb_path, serial, package)
        if isinstance(resolved, dict):
            return resolved
        if resolved is None:
            return _error("no_launcher_activity", "No launcher activity was found for this package: it may not be installed, or it has no "
                          "launcher entry. Check with app_validate, or pass `activity`.", "app.no_launcher_activity")
        component = resolved
    argv = ["shell", "am", "start", *(["-W"] if wait else []), "-n", component]
    proc = _shell(adb_path, serial, argv, timeout=LAUNCH_WAIT_SECONDS if wait else 15)
    if isinstance(proc, dict):
        return proc
    text = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    parsed = parse_am_start(text)
    if _failed(proc) or not parsed["ok"]:
        reason = launch_failure_reason(text)
        return _error("app_launch_failed", f"The app did not start ({reason}). Device said: {parsed['error_text'] or _clean(text, 200)}",
                      "app.launch_failed", reason=reason)
    result: dict[str, Any] = {
        "ok": True, "package": package, "component": parsed["component"] or component, "waited": bool(wait),
        "launch_state": parsed["launch_state"], "total_time_ms": parsed["total_time_ms"], "wait_time_ms": parsed["wait_time_ms"],
    }
    resumed = _resumed_activity(adb_path, serial)
    resumed = resumed if isinstance(resumed, dict) and "error" not in resumed else None
    result["resumed_activity"] = resumed
    result["foreground"] = None if resumed is None else resumed["package"] == package
    result["note"] = ("A successful start is not proof the app works. Call ui_summary to see the screen and app_state to check "
                      "for a crash.")
    return result


def tool_app_stop(device: str | None, package: str) -> dict[str, Any]:
    """`am force-stop`: ends the app's processes; app data and the installed build are untouched."""
    bad = _bad_package(package)
    if bad:
        return bad
    target = _target(device)
    if isinstance(target, dict):
        return target
    adb_path, serial = target
    proc = _shell(adb_path, serial, ["shell", "am", "force-stop", package])
    if isinstance(proc, dict):
        return proc
    if _failed(proc):
        return _error("adb_error", _t._adb_error_message(proc, "force-stop failed"))
    check = _shell(adb_path, serial, ["shell", "pidof", package])
    still = None if isinstance(check, dict) else (None if check.returncode not in (0, 1) else bool(parse_pids(check.stdout or "")))
    return {"ok": True, "package": package, "still_running": still,
            "note": "Only the app's processes were stopped; its data and installed build are unchanged. Relaunch with app_launch."}


def tool_app_state(device: str | None, package: str) -> dict[str, Any]:
    """Process id, resumed activity and last exit reason: did the app crash, without reading logs."""
    bad = _bad_package(package)
    if bad:
        return bad
    target = _target(device)
    if isinstance(target, dict):
        return target
    adb_path, serial = target
    sources: dict[str, str] = {}

    pid_proc = _shell(adb_path, serial, ["shell", "pidof", package])
    pids: list[int] | None = None
    if not isinstance(pid_proc, dict):
        if pid_proc.returncode in (0, 1):
            pids = parse_pids(pid_proc.stdout or "")
        sources["pidof"] = "observed" if pids is not None else "unavailable"

    resumed = _resumed_activity(adb_path, serial)
    resumed = resumed if isinstance(resumed, dict) and "error" not in resumed else None
    sources["resumed_activity"] = "observed" if resumed else "not_found"

    sdk_proc = _shell(adb_path, serial, ["shell", "getprop", "ro.build.version.sdk"])
    sdk = int(sdk_proc.stdout.strip()) if not isinstance(sdk_proc, dict) and (sdk_proc.stdout or "").strip().isdigit() else None
    last_exit: dict[str, Any] | None = None
    if sdk is not None and sdk >= EXIT_INFO_MIN_SDK:
        info = _shell(adb_path, serial, ["shell", "dumpsys", "activity", "exit-info", package], timeout=20)
        if not isinstance(info, dict) and not _failed(info):
            last_exit = parse_exit_info(info.stdout or "", package)
            sources["exit_info"] = "observed" if last_exit else "no_record"
        else:
            sources["exit_info"] = "unavailable"
    else:
        sources["exit_info"] = "needs_api_30" if sdk is not None else "unknown_api"

    clock = _shell(adb_path, serial, ["shell", "date", "+%m-%d_%H:%M:%S.%N"])
    now = parse_device_time(clock.stdout or "") if not isinstance(clock, dict) and not _failed(clock) else None
    if last_exit is not None:
        last_exit = {k: v for k, v in last_exit.items() if k != "year"}
    return {
        "package": package,
        "running": None if pids is None else bool(pids),
        "pids": pids or [],
        "resumed_activity": resumed,
        "foreground": None if resumed is None else resumed["package"] == package,
        "api_level": sdk,
        "last_exit": last_exit,
        "device_time": now[0] if now else None,
        "sources": sources,
        "note": ("`last_exit` is the most recent recorded process exit (Android 11+), which can predate your launch: compare "
                 "`last_exit.at` with `device_time` or a `log_mark`. `crash` is true for a Java or native crash. Running processes "
                 "named `package:suffix` are not counted. An empty result is not proof the app works."),
    }


def tool_app_open_link(device: str | None, uri: str, package: str | None = None) -> dict[str, Any]:
    """`am start -a android.intent.action.VIEW -d URI`: open a link or deep link on the device."""
    if not safety.is_safe_uri(uri):
        return _error("invalid_uri", "`uri` must be an ASCII URI of at most 2000 characters with a scheme (https://, a deep-link scheme), "
                      "no spaces or control characters, none of ' \" ` $ \\ < > | ^ { }, and not javascript:, data:, vbscript: or intent:. "
                      "Percent-encode anything else.", "app.link_invalid")
    if package is not None:
        bad = _bad_package(package)
        if bad:
            return bad
    target = _target(device)
    if isinstance(target, dict):
        return target
    adb_path, serial = target
    argv = ["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", safety.quote_safe_uri(uri)]
    if package is not None:
        argv += ["-p", package]
    proc = _shell(adb_path, serial, argv, timeout=15)
    if isinstance(proc, dict):
        return proc
    text = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    if _failed(proc) or re.search(r"^(?:Error|Exception)", text, re.MULTILINE):
        return _error("link_not_handled", f"No app opened the link. Device said: {_clean(text, 200)}", "app.link_unhandled")
    return {"ok": True, "uri": uri, "package": package,
            "note": "The system accepted the intent. Call ui_summary to see what opened; a browser or chooser may need a further tap."}


def tool_log_mark(device: str | None) -> dict[str, Any]:
    """The device's current time as a mark for `since` on `app_logs` and `logcat_tail`."""
    target = _target(device)
    if isinstance(target, dict):
        return target
    adb_path, serial = target
    proc = _shell(adb_path, serial, ["shell", "date", "+%m-%d_%H:%M:%S.%N"])
    if isinstance(proc, dict):
        return proc
    parsed = None if _failed(proc) else parse_device_time(proc.stdout or "")
    if parsed is None:
        return _error("adb_error", "The device did not report a usable time.")
    return {"mark": parsed[0], "precision": parsed[1],
            "note": "Pass `mark` as `since` to app_logs or logcat_tail to read only what was logged after now. Buffers are not cleared. "
                    "Seconds precision can include a little earlier output from the same second."}


# ---------------------------------------------------------------------------
# Runtime permissions
# ---------------------------------------------------------------------------

def _permission_tool(action: str, device: str | None, package: str, permission: str) -> dict[str, Any]:
    bad = _bad_package(package)
    if bad:
        return bad
    if not safety.is_runtime_permission(permission, package):
        return _error("invalid_permission", "`permission` must be a runtime permission such as android.permission.CAMERA, or one this app "
                      "defines under its own package. Special-access permissions (overlay, write settings, all-files access) cannot be "
                      "changed with pm grant.", "app.permission_refused")
    target = _target(device)
    if isinstance(target, dict):
        return target
    adb_path, serial = target
    proc = _shell(adb_path, serial, ["shell", "pm", action, package, permission])
    if isinstance(proc, dict):
        return proc
    text = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    if _failed(proc) or re.search(r"exception|error|not allowed|unknown package", text, re.IGNORECASE):
        low = text.lower()
        reason = ("not_requested_by_app" if "has not requested" in low else "not_a_changeable_runtime_permission"
                  if "not a changeable" in low or "not a runtime" in low else "unknown_package" if "unknown package" in low else "other")
        return _error("permission_failed", f"pm {action} was refused ({reason}): {_clean(text, 200)}", "app.permission_refused", reason=reason)
    return {"ok": True, "package": package, "permission": permission, "action": action,
            "note": "pm accepted the command; the new state was not read back. Some permission changes restart the app."}


def tool_permission_grant(device: str | None, package: str, permission: str) -> dict[str, Any]:
    return _permission_tool("grant", device, package, permission)


def tool_permission_revoke(device: str | None, package: str, permission: str) -> dict[str, Any]:
    return _permission_tool("revoke", device, package, permission)


# ---------------------------------------------------------------------------
# UI tools
# ---------------------------------------------------------------------------

def _validate_selector(text: Any, resource_id: Any) -> dict[str, Any] | None:
    if (text is None) == (resource_id is None):
        return _error("invalid_params", "Pass exactly one of `text` or `resource_id`.")
    if text is not None and (not isinstance(text, str) or not 0 < len(text) <= 200 or _CONTROL_RE.search(text)):
        return _error("invalid_params", "`text` must be 1-200 characters without control characters.")
    if resource_id is not None and not (isinstance(resource_id, str) and _RESOURCE_ID_RE.fullmatch(resource_id)):
        return _error("invalid_params", "`resource_id` must look like com.example.app:id/button (letters, digits, `_ . : / -`).")
    return None


def _ui_nodes(adb_path: Path, serial: str, *, allow_device_file: bool, deadline: float | None = None) -> list[dict[str, Any]] | dict[str, Any]:
    guarded = _t.guarded_runner(adb_path)

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        if deadline is not None and "timeout" in kwargs:
            kwargs["timeout"] = max(1, min(kwargs["timeout"], int(deadline - _MONOTONIC()) + 1))
        return guarded(argv, **kwargs)

    try:
        return capture_ui_summary(adb_path, serial, max_nodes=MAX_UI_NODES, runner=runner, allow_device_file=allow_device_file)["nodes"]
    except UiDumpUnavailableError:
        return _error("ui_dump_failed", "The direct UI dump failed and the file-based fallback is disabled in read-only sessions.", "app.ui_dump_failed")
    except (safety.SafetyError, _t.UnsafeAdbServerError):
        raise
    except (subprocess.TimeoutExpired, RuntimeError, ValueError, OSError):
        return _error("ui_dump_failed", "The UI hierarchy could not be dumped (screen off or locked, or another UI automation is running).",
                      "app.ui_dump_failed")


def _select(nodes: list[dict[str, Any]], text: str | None, resource_id: str | None) -> list[dict[str, Any]]:
    if text is not None:
        return [n for n in nodes if n["text"] == text or n["content_desc"] == text]
    return [n for n in nodes if n["resource_id"] == resource_id]


def _describe(node: dict[str, Any]) -> dict[str, Any]:
    return {"text": _clean(node["text"], 80), "content_desc": _clean(node["content_desc"], 80),
            "resource_id": _clean(node["resource_id"], 120), "bounds": node["bounds"], "center": node["center"]}


def _labels(nodes: list[dict[str, Any]], limit: int = 8) -> list[str]:
    seen: list[str] = []
    for node in nodes:
        label = _clean(node["text"] or node["content_desc"], 60)
        if label and label not in seen:
            seen.append(label)
        if len(seen) >= limit:
            break
    return seen


def tool_ui_tap_text(device: str | None, text: str | None = None, resource_id: str | None = None, index: int | None = None) -> dict[str, Any]:
    """A fresh UI dump, then a tap at the centre of the one element matching `text` or `resource_id`."""
    bad = _validate_selector(text, resource_id)
    if bad:
        return bad
    target = _target(device)
    if isinstance(target, dict):
        return target
    adb_path, serial = target
    nodes = _ui_nodes(adb_path, serial, allow_device_file=True)
    if isinstance(nodes, dict):
        return nodes
    matches = _select(nodes, text, resource_id)
    wanted = {"text": text} if text is not None else {"resource_id": resource_id}
    if not matches:
        return _untrusted_error(
            "ui_no_match", "No element on the current screen matches. `available` lists some visible labels; call ui_summary for the rest.",
            "app.ui_no_match", selector=wanted, available=_labels(nodes))
    if index is None and len(matches) > 1:
        return _untrusted_error(
            "ui_ambiguous", f"{len(matches)} elements match; pass `index` (0-based, in screen order) to choose one.",
            "app.ui_ambiguous", selector=wanted, match_count=len(matches), matches=[_describe(n) for n in matches[:10]])
    chosen_index = 0 if index is None else index
    if not 0 <= chosen_index < len(matches):
        return _untrusted_error(
            "ui_no_match", f"`index` {chosen_index} is out of range: {len(matches)} element(s) match.", "app.ui_no_match",
            selector=wanted, match_count=len(matches))
    node = matches[chosen_index]
    x, y = node["center"]
    if not (0 <= x <= _t.MAX_COORDINATE and 0 <= y <= _t.MAX_COORDINATE):
        return _error("ui_no_match", "The matching element is off-screen; scroll it into view with swipe, then try again.", "app.ui_no_match")
    proc = _shell(adb_path, serial, ["shell", "input", "tap", str(x), str(y)])
    if isinstance(proc, dict):
        return proc
    if _failed(proc):
        return _error("adb_error", _t._adb_error_message(proc, "tap failed"))
    return {"ok": True, "tapped": [x, y], "match_count": len(matches), "matched": _describe(node),
            "note": "Coordinates are from a UI dump taken just now. Call ui_summary to see the result of the tap."}


def tool_ui_wait(device: str | None, text: str | None = None, resource_id: str | None = None, gone: bool = False,
                 timeout_s: int = DEFAULT_UI_WAIT_SECONDS, *, allow_device_file: bool = True) -> dict[str, Any]:
    """Poll bounded UI dumps until an element appears (or, with `gone`, disappears), up to `timeout_s`."""
    bad = _validate_selector(text, resource_id)
    if bad:
        return bad
    limit = max(1, min(int(timeout_s), MAX_UI_WAIT_SECONDS))
    target = _target(device)
    if isinstance(target, dict):
        return target
    adb_path, serial = target
    started = _MONOTONIC()
    deadline = started + limit
    polls = good = 0
    matches: list[dict[str, Any]] = []
    last_error: dict[str, Any] | None = None
    met = False
    while True:
        polls += 1
        nodes = _ui_nodes(adb_path, serial, allow_device_file=allow_device_file, deadline=deadline)
        if isinstance(nodes, dict):
            last_error = nodes
        else:
            good += 1
            matches = _select(nodes, text, resource_id)
            met = (not matches) if gone else bool(matches)
            if met:
                break
        if _MONOTONIC() + UI_POLL_SECONDS >= deadline:
            break
        _SLEEP(UI_POLL_SECONDS)
    if good == 0 and last_error is not None:
        return last_error
    return {
        "condition": "gone" if gone else "present", "met": met, "timed_out": not met, "waited_s": round(_MONOTONIC() - started, 1),
        "timeout_s": limit, "polls": polls, "match_count": len(matches), "first_match": _describe(matches[0]) if matches else None,
        "selector": {"text": text} if text is not None else {"resource_id": resource_id},
        "note": "Call ui_summary for the current screen. A timeout is a result, not an error: the element did not reach the condition in time.",
    }


# ---------------------------------------------------------------------------
# Test runs (am instrument), at most one per device, with a hard time cap
# ---------------------------------------------------------------------------

class _TestRun:
    def __init__(self, run_id: str, serial: str, test_package: str, runner: str, adb_path: Path) -> None:
        self.run_id, self.serial, self.test_package, self.runner, self.adb_path = run_id, serial, test_package, runner, adb_path
        self.parser = InstrumentationParser()
        self.done = threading.Event()
        self.started = _MONOTONIC()
        self.ended: float | None = None
        self.status = "running"
        self.reason: str | None = None
        self.exit_code: int | None = None
        self.cleanup: str | None = None
        self.proc: Any = None
        self.timer: threading.Timer | None = None

    def elapsed(self) -> float:
        return round((self.ended or _MONOTONIC()) - self.started, 1)


_LOCK = threading.Lock()
_RUNS: dict[str, _TestRun] = {}
_ATEXIT_REGISTERED = False


def _stop_on_device(run: _TestRun) -> str:
    """Best effort: end the test package's processes so the instrumentation does not outlive its client."""
    try:
        proc = _t.run_adb(run.adb_path, ["shell", "am", "force-stop", run.test_package], serial=run.serial, timeout=10) \
            if safety.match_agent_shell_shape(["shell", "am", "force-stop", run.test_package]) else None
    except Exception:  # noqa: BLE001 - cleanup must never raise
        return "force_stop_failed"
    return "force_stopped" if proc is not None and proc.returncode == 0 else "force_stop_failed"


def _kill(proc: Any) -> None:
    for action in ("terminate", "kill"):
        try:
            getattr(proc, action)()
            proc.wait(timeout=3)
            return
        except Exception:  # noqa: BLE001
            continue


def _finish(run: _TestRun, status: str | None = None, reason: str | None = None) -> None:
    with _LOCK:
        if run.done.is_set():
            return
        if status is None:
            status, reason = run.parser.verdict()
        run.status, run.reason = status, reason
        run.ended = _MONOTONIC()
        run.done.set()
    if run.timer is not None:
        run.timer.cancel()


def _reader(run: _TestRun) -> None:
    try:
        for line in run.proc.stdout:
            run.parser.feed(line)
        run.exit_code = run.proc.wait()
    except Exception:  # noqa: BLE001 - a broken pipe ends the run as an error, not a crash of the server
        run.exit_code = None
    if not run.done.is_set():
        status, reason = run.parser.verdict()
        if run.exit_code not in (0, None) and status == "passed":
            status, reason = "error", f"adb exited with status {run.exit_code}."
        if status == "error" and not run.parser.finished and run.parser.other_tail:
            reason = f"{reason} Last output: {run.parser.other_tail[-1]}"
        _finish(run, status, reason)


def _expire(run: _TestRun) -> None:
    if run.done.is_set():
        return
    _finish(run, "timed_out", f"The run exceeded the {MAX_TEST_RUN_SECONDS} s hard limit and was stopped.")
    _kill(run.proc)
    run.cleanup = _stop_on_device(run)


def shutdown_test_runs() -> None:
    """Stop every active run (server exit): kill the adb client and force-stop the test package."""
    with _LOCK:
        active = [r for r in _RUNS.values() if not r.done.is_set()]
    for run in active:
        _finish(run, "cancelled", "The ADAPT server stopped while this run was active.")
        _kill(run.proc)
        run.cleanup = _stop_on_device(run)


def _evict() -> None:
    finished = [r for r in _RUNS.values() if r.done.is_set()]
    for run in sorted(finished, key=lambda r: r.ended or 0)[:-KEEP_FINISHED_RUNS]:
        _RUNS.pop(run.run_id, None)


def _progress(run: _TestRun) -> dict[str, Any]:
    p = run.parser
    return {"tests_run": p.tests_run, "passed": p.passed, "failed": p.failed + p.errors, "ignored": p.ignored,
            "assumption_failed": p.skipped, "total": p.numtests, "current": p.current_test}


def _run_payload(run: _TestRun) -> dict[str, Any]:
    base: dict[str, Any] = {"run_id": run.run_id, "status": run.status, "elapsed_s": run.elapsed(), "device": run.serial,
                            "test_package": run.test_package, "progress": _progress(run)}
    if run.status == "running":
        base["next"] = f"Still running. Call test_status with run_id {run.run_id} to check, or wait and call it again."
        return base
    failures = run.parser.failures
    base.update({
        "passed": run.status == "passed", "counts": _progress(run), "failures": failures[:MAX_FAILURES_RETURNED],
        "failures_omitted": max(0, len(failures) - MAX_FAILURES_RETURNED), "reason": run.reason,
        "cleanup": run.cleanup,
        "claim_boundary": "Counts come from the instrumentation runner's own report. `passed` means the runner reported no failures; it does "
                          "not show the app works beyond what the tests check.",
    })
    return base


def _test_args_valid(test_package: Any, runner: Any, class_name: Any, method: Any) -> dict[str, Any] | None:
    if not safety.is_package_name(test_package):
        return _error("invalid_package", "`test_package` must be the test APK's application ID such as com.example.app.test.", "app.package_invalid")
    if not safety.is_class_name(runner) or ("." not in runner):
        return _error("invalid_params", "`runner` must be the instrumentation runner class, for example androidx.test.runner.AndroidJUnitRunner.")
    if class_name is not None and not safety.is_qualified_class_name(class_name):
        return _error("invalid_params", "`class` must be a fully qualified test class name such as com.example.app.LoginTest.")
    if method is not None and (class_name is None or not safety.is_method_name(method)):
        return _error("invalid_params", "`method` needs `class` and must be a plain method name.")
    return None


def tool_test_run(device: str | None, test_package: str, runner: str, class_name: str | None = None, method: str | None = None,
                  timeout_s: int = DEFAULT_TEST_WAIT_SECONDS) -> dict[str, Any]:
    """Start `am instrument -w -r`; wait up to `timeout_s`, then return the result or a run id for test_status."""
    bad = _test_args_valid(test_package, runner, class_name, method)
    if bad:
        return bad
    target = _target(device)
    if isinstance(target, dict):
        return target
    adb_path, serial = target
    component = f"{test_package}/{runner}"
    argv = ["shell", "am", "instrument", "-w", "-r"]
    if class_name is not None:
        argv += ["-e", "class", f"{class_name}#{method}" if method else class_name]
    argv.append(component)
    if safety.match_agent_shell_shape(argv) is None:
        return _t.refused_command_error()
    full = [str(adb_path), "-s", serial, *argv]
    _t._guard_adb_command(adb_path, full[1:])

    global _ATEXIT_REGISTERED
    run = _TestRun(secrets.token_hex(6), serial, test_package, runner, adb_path)
    with _LOCK:
        busy = next((r for r in _RUNS.values() if r.serial == serial and not r.done.is_set()), None)
        if busy is not None:
            return _error("test_run_busy", f"A test run is already active on this device (run_id {busy.run_id}); only one run per device. "
                          "Poll it with test_status.", "app.test_busy", run_id=busy.run_id)
        try:
            run.proc = subprocess.Popen(full, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                                        encoding="utf-8", errors="replace", bufsize=1)
        except OSError:
            return {"error": "adb_launch_failed", "message": "Could not execute ADB.", "devices": []}
        _RUNS[run.run_id] = run
        if not _ATEXIT_REGISTERED:
            atexit.register(shutdown_test_runs)
            _ATEXIT_REGISTERED = True
        _evict()
    run.timer = threading.Timer(MAX_TEST_RUN_SECONDS, _expire, args=(run,))
    run.timer.daemon = True
    run.timer.start()
    threading.Thread(target=_reader, args=(run,), name=f"adapt-test-run-{run.run_id}", daemon=True).start()
    run.done.wait(max(MIN_TEST_WAIT_SECONDS, min(int(timeout_s), MAX_TEST_WAIT_SECONDS)))
    return _run_payload(run)


def tool_test_status(run_id: str, *, allowed_devices: tuple[str, ...] | None = None) -> dict[str, Any]:
    """Progress or the final parsed result of a `test_run`."""
    if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
        return _error("invalid_params", "`run_id` is the 12-character id test_run returned.")
    with _LOCK:
        run = _RUNS.get(run_id)
    if run is None or (allowed_devices is not None and run.serial not in allowed_devices):
        return _error("unknown_run", "No test run with that id: it was never started by this server session, or it was evicted "
                      f"(the {KEEP_FINISHED_RUNS} most recent finished runs are kept). Start one with test_run.", "app.test_run_unknown")
    return _run_payload(run)


# ---------------------------------------------------------------------------
# Developer Options profiles with ADAPT-owned snapshots
# ---------------------------------------------------------------------------

PROFILES = _t.PROFILE_NAMES


def state_dir() -> Path:
    """Per-user folder for the snapshots `dev_options_apply` writes (never a path the caller chooses)."""
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    else:
        root = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
    return root / "adapt" / "dev-options-snapshots"


def _identity_key(identity: Any) -> str:
    text = f"{identity.ro_serialno}\0{identity.build_fingerprint}\0{identity.android_user}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


_SNAPSHOT_NAME_RE = re.compile("(" + "|".join(map(re.escape, PROFILES)) + r")-(\d{8}T\d{6}Z)-([0-9a-f]{8})\.json")


def _snapshot_status(path: Path) -> str:
    """The snapshot's recorded status, or `unreadable`; never raises."""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > dev_options.MAX_SNAPSHOT_BYTES:
            return "unreadable"
        status = json.loads(path.read_text(encoding="utf-8")).get("status")
    except (OSError, ValueError, AttributeError):
        return "unreadable"
    return status if isinstance(status, str) else "unreadable"


def list_pending_snapshots(directory: Path, profile: str | None = None) -> list[dict[str, Any]]:
    """Snapshots in `directory` that are not yet `restored`, oldest first."""
    pending = []
    try:
        names = sorted(p for p in directory.iterdir() if p.is_file() or p.is_symlink())
    except OSError:
        return []
    for path in names:
        match = _SNAPSHOT_NAME_RE.fullmatch(path.name)
        if not match or (profile is not None and match.group(1) != profile):
            continue
        status = _snapshot_status(path)
        if status != "restored":
            pending.append({"snapshot_id": path.stem, "profile": match.group(1), "status": status, "created": match.group(2), "path": path})
    return sorted(pending, key=lambda e: (e["created"], e["snapshot_id"]))


def _public(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in e.items() if k != "path"} for e in entries]


def _dev_error(exc: dev_options.DevOptionsError, **extra: Any) -> dict[str, Any]:
    payload = {"error": exc.kind, "message": _clean(exc, 300), "devices": [], **extra}
    return tag_payload(payload, classify(exc, area="dev_options"))


def tool_dev_options_apply(device: str | None, profile: str, package: str | None = None,
                           options: list[str] | None = None) -> dict[str, Any]:
    """Apply one Developer Options profile with a recovery snapshot ADAPT stores and names; restore it before finishing."""
    if profile not in PROFILES:
        return _error("invalid_params", f"`profile` must be one of: {', '.join(PROFILES)}.")
    target = _target(device)
    if isinstance(target, dict):
        return target
    adb_path, serial = target
    try:
        adb = dev_options._resolve_adb_path(adb_path)
        identity = dev_options._read_identity(adb, serial)
        root = state_dir()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = root / _identity_key(identity)
        pending = list_pending_snapshots(directory, profile)
        if pending:
            return _error("snapshot_pending", f"A `{profile}` snapshot for this device is still pending restore. Call dev_options_restore first.",
                          "devopts.snapshot_pending", pending=_public(pending))
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        snapshot_id = f"{profile}-{stamp}-{secrets.token_hex(4)}"
        directory.mkdir(mode=0o700, exist_ok=True)
        report = dev_options.apply_profile(profile, serial, directory / f"{snapshot_id}.json", adb_path=adb,
                                           package=package, options=tuple(options or ()))
    except dev_options.DevOptionsError as exc:
        return _dev_error(exc)
    except OSError:
        return _error("snapshot_io", "ADAPT could not create its snapshot folder in the per-user state directory.", "devopts.snapshot_io")
    result = {"ok": True, "profile": profile, "snapshot_id": snapshot_id, "changed": report["changed"], "unchanged": report["unchanged"],
              "note": f"{report['note']} Call dev_options_restore before you finish."}
    # What the agent must still do or know: restart the app, check the effect, how exact restore is.
    for key in ("needs_app_restart", "needs_effect_check", "effect_check", "restore_exactness", "skipped_steps", "claim_boundary"):
        if key in report:
            result[key] = report[key]
    return result


def tool_dev_options_restore(device: str | None, profile: str | None = None) -> dict[str, Any]:
    """Restore pending snapshots for this device (newest first); lists what was pending. Nothing pending is a success."""
    if profile is not None and profile not in PROFILES:
        return _error("invalid_params", f"`profile` must be one of: {', '.join(PROFILES)}.")
    target = _target(device)
    if isinstance(target, dict):
        return target
    adb_path, serial = target
    try:
        adb = dev_options._resolve_adb_path(adb_path)
        identity = dev_options._read_identity(adb, serial)
        directory = state_dir() / _identity_key(identity)
        before = list_pending_snapshots(directory, profile)
        results: list[dict[str, Any]] = []
        for entry in reversed(before):
            try:
                outcome = dev_options.restore_snapshot(serial, entry["path"], adb_path=adb)
            except dev_options.DevOptionsError as exc:
                failure = _dev_error(exc, snapshot_id=entry["snapshot_id"], restored=[r["snapshot_id"] for r in results if r.get("ok")],
                                     pending=_public(list_pending_snapshots(directory)))
                return failure
            results.append({"snapshot_id": entry["snapshot_id"], "profile": entry["profile"], "ok": bool(outcome.get("ok")),
                            "status": outcome.get("status"), "restored": outcome.get("restored", []),
                            "conflicts": [_clean(c, 200) for c in outcome.get("conflicts", [])[:5]]})
            if not outcome.get("ok"):
                after = list_pending_snapshots(directory)
                guide = "devopts.restore_conflict" if outcome.get("status") == "restore_conflict" else "devopts.restore_partial"
                return _error("restore_incomplete", "A setting no longer holds the value ADAPT applied, so it was left alone "
                              "and the snapshot was kept. Tell the user; do not overwrite it.", guide, results=results,
                              pending=_public(after))
        after = list_pending_snapshots(directory)
    except dev_options.DevOptionsError as exc:
        return _dev_error(exc)
    except OSError:
        return _error("snapshot_io", "ADAPT could not read its snapshot folder in the per-user state directory.", "devopts.snapshot_unreadable")
    still = [e for e in after if profile is None or e["profile"] == profile]
    return {"ok": not still, "pending_before": _public(before), "results": results, "pending": _public(after),
            "note": "Nothing was pending for this device." if not before else
                    "Snapshots are kept as recovery evidence after a restore; only ones not yet `restored` count as pending."}


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

AGENT_TOOLS = frozenset({
    "app_launch", "app_stop", "app_state", "app_open_link", "log_mark", "test_run", "test_status", "dev_options_apply",
    "dev_options_restore", "permission_grant", "permission_revoke", "ui_tap_text", "ui_wait",
})


def call_tool(name: str, args: dict[str, Any], *, allow_device_file: bool = True, allowed_devices: tuple[str, ...] | None = None) -> dict[str, Any]:
    """Run one agent-loop tool with already schema-validated arguments; the result is within the output budget."""
    device = args.get("device")
    if name == "app_launch":
        payload = tool_app_launch(device, args["package"], args.get("activity"), bool(args.get("wait", True)))
    elif name == "app_stop":
        payload = tool_app_stop(device, args["package"])
    elif name == "app_state":
        payload = tool_app_state(device, args["package"])
    elif name == "app_open_link":
        payload = tool_app_open_link(device, args["uri"], args.get("package"))
    elif name == "log_mark":
        payload = tool_log_mark(device)
    elif name == "test_run":
        payload = tool_test_run(device, args["test_package"], args["runner"], args.get("class"), args.get("method"),
                                int(args.get("timeout_s", DEFAULT_TEST_WAIT_SECONDS)))
    elif name == "test_status":
        payload = tool_test_status(args["run_id"], allowed_devices=allowed_devices)
    elif name == "dev_options_apply":
        payload = tool_dev_options_apply(device, args["profile"], args.get("package"), args.get("options"))
    elif name == "dev_options_restore":
        payload = tool_dev_options_restore(device, args.get("profile"))
    elif name == "permission_grant":
        payload = tool_permission_grant(device, args["package"], args["permission"])
    elif name == "permission_revoke":
        payload = tool_permission_revoke(device, args["package"], args["permission"])
    elif name == "ui_tap_text":
        payload = tool_ui_tap_text(device, args.get("text"), args.get("resource_id"), args.get("index"))
    elif name == "ui_wait":
        payload = tool_ui_wait(device, args.get("text"), args.get("resource_id"), bool(args.get("gone", False)),
                               int(args.get("timeout_s", DEFAULT_UI_WAIT_SECONDS)), allow_device_file=allow_device_file)
    else:
        return {"error": "unknown_tool", "message": f"Unknown tool: {name}", "devices": []}
    return bound_payload(payload)
