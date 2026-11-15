"""Supervise the experimental native Windows receiver (``dexdesk.exe``) from ADAPT.

The receiver (``native/windows-receiver``) is a Windows Wi-Fi Display sink that a
Samsung phone projects wireless DeX to. ADAPT never builds, bundles, downloads or
searches for it: the user builds it, and ``receiver-start`` runs the one
``dexdesk.exe`` the user names. Everything ADAPT learns comes from the receiver's
own ``status.json`` (schema version 1) and from a process-list check; the
receiver's log file is never read and the phone's name, peer ID and addresses
never appear here.

Conventions of this module:

- Operations return report dictionaries instead of raising. A refusal is
  ``{"status": "refused", "reason": ..., "error": ...}``. Nothing here raises, so
  nothing adds to the failure-guidance ratchet in ``tests/test_failure_guide.py``.
  ``report_failure_id`` names the failure guide for a report that is not a success
  (``dexpc.native_receiver.*`` for what ADAPT observed or refused,
  ``dexpc.receiver.<code>`` for a reason code the receiver recorded); the commands
  print it after the report, or add it as a ``failure`` object under ``--json``.
- Processes are never killed: no ``os.kill`` (on Windows it terminates), no
  ``terminate`` or ``kill``. A stop is a ``stop.request`` file the receiver reads.
- Exception text of ``OSError`` is not printed (it embeds paths and so the
  account name); only ``strerror`` is.
- Text read from ``status.json`` is never echoed. Only validated tokens and
  numbers are copied into a report; the receiver's ``message`` is withheld
  because it can echo values the phone sent.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import socket
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import typer

from adapt_adb.dex import dex_app
from adapt_adb.failure_guide import RECEIVER_GUIDES, Step
from adapt_adb.failure_report import FailureContext, attach_failure, current_command, emit_failure, emit_pointer

SCHEMA_VERSION = 1           # this module's report schema
STATUS_SCHEMA_VERSION = 1    # the receiver's status.json schema it understands
RECEIVER_EXE = "dexdesk.exe"
RECEIVER_PORT = 7236
STATUS_FILE = "status.json"
STOP_FILE = "stop.request"
MAX_STATUS_BYTES = 256 * 1024
STALE_AFTER_SECONDS = 10.0   # the receiver rewrites status.json at least once a second
START_WAIT_SECONDS = 30.0
STOP_WAIT_SECONDS = 20.0
START_POLL_SECONDS = 0.25
STOP_POLL_SECONDS = 0.5
NAME_LIMIT_BYTES = 256       # ADAPT_DEX_EXPECTED_NAME, as validated by the receiver
ID_LIMIT_BYTES = 1024        # ADAPT_DEX_EXPECTED_ID
HEADER = "ADAPT / EXPERIMENTAL DeX NATIVE RECEIVER"

ENV_NAME = "ADAPT_DEX_EXPECTED_NAME"
ENV_ID = "ADAPT_DEX_EXPECTED_ID"
ENV_AUDIO = "ADAPT_DEX_AUDIO"
ENV_DATA_DIR = "ADAPT_DEX_DATA_DIR"
ENV_LOG_PEER = "ADAPT_DEX_LOG_PEER_DETAILS"

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000

# Observation stages in the order the receiver reaches them (README, "Observation stages").
PRE_ATTEMPT_STAGES = ("starting", "advertising")
ATTEMPT_STAGES = ("connection_request", "peer_admitted", "wifi_direct_group", "rtsp_connected", "video_negotiated",
                  "playback_accepted", "input_channel_connected", "first_frame_decoded")
STAGE_ORDER = ("starting", "advertising", "connection_request", "peer_rejected", "peer_admitted", "wifi_direct_group",
               "rtsp_connected", "video_negotiated", "playback_accepted", "input_channel_connected", "first_frame_decoded")

STATE_LABELS = {
    "not_running": "not running", "starting": "starting", "ready": "ready for a phone",
    "connecting": "a phone is connecting", "streaming": "a phone is streaming", "stopping": "shutting down",
    "not_working": "not working", "stale": "status out of date", "unverified": "unverified",
    "unreadable": "status unreadable",
}

NOT_AUTHENTICATION = ("These restrictions are not authentication: a device can announce any name, and a peer ID is only as "
                      "secret as the radio traffic that carries it. A refused phone is simply never connected.")
OPEN_ADMISSION = ("Open admission is in effect: any Wi-Fi Direct device in radio range that requests a connection is admitted. "
                  "There is no prompt and no pairing code from ADAPT or the receiver.")
LISTENER_NOTE = ("The receiver listens on TCP 7236 on every IPv4 interface while it runs; ADAPT creates no firewall rule, so "
                 "Windows Defender Firewall alone decides who can reach it.")
STATUS_BOUNDARY = ("Read from the receiver's own status file and a process-list check; ADAPT did not contact the phone or the "
                   "Wi-Fi radio. An advertising receiver is not a working desktop, and a decoded frame does not certify "
                   "input, audio or Samsung DeX state.")


# ---------------------------------------------------------------------------
# Small helpers (module-level so tests can replace them)
# ---------------------------------------------------------------------------

def _is_windows() -> bool:
    return sys.platform == "win32"


def _now() -> float:
    return time.time()


def _clock() -> float:
    return time.monotonic()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


_TOKEN = re.compile(r"[a-z][a-z0-9_]{0,63}")
_VERSION = re.compile(r"[0-9A-Za-z.+_-]{1,32}")


def _token(value: object) -> str | None:
    return value if isinstance(value, str) and _TOKEN.fullmatch(value) else None


def _oserror_text(exc: OSError) -> str:
    return exc.strerror or type(exc).__name__


def _truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}


def _refuse(reason: str, error: str, **extra: Any) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "status": "refused", "reason": reason, "error": error,
            "changes_performed": False, **extra}


# ---------------------------------------------------------------------------
# Reason-code guidance lives in the failure-guide registry (IDs "dexpc.receiver.<code>"),
# so `adapt explain` can show and search it; RECEIVER_GUIDES is re-exported here.
# ---------------------------------------------------------------------------

# Scope of each code as the receiver documents it: "receiver", "session" or "either".
RECEIVER_REASON_SCOPES: dict[str, str] = {
    **dict.fromkeys(("invalid_configuration", "port_in_use", "wifi_direct_init_failed", "miracast_provider_unavailable",
                     "miracast_provider_failed", "wifi_radio_unavailable", "wifi_direct_resource_in_use",
                     "advertisement_aborted", "advertisement_timeout"), "receiver"),
    **dict.fromkeys(("peer_rejected_name", "peer_rejected_id", "wifi_direct_connect_failed", "wifi_direct_connect_timeout",
                     "no_ipv4_endpoint", "rtsp_connect_failed", "rtp_port_unavailable", "control_connection_failed",
                     "rtsp_protocol_error", "rtsp_request_rejected", "unsupported_video_format",
                     "input_channel_unavailable", "input_channel_failed", "decoder_init_failed", "decoder_failed",
                     "media_stream_error", "handshake_timeout", "partial_message_timeout", "control_idle_timeout",
                     "peer_disconnected", "peer_teardown", "user_stopped"), "session"),
    **dict.fromkeys(("internal_error", "internal_panic"), "either"),
}
# Session ends the receiver records that are orderly, not failures.
ORDERLY_ENDS = frozenset({"peer_teardown", "user_stopped"})


def _step_dict(step: Step) -> dict[str, str]:
    item = {"text": step.text, "effect": step.effect}
    if step.command:
        item["command"] = step.command
    return item


def explain_code(code: str, scope: str, stage: str, attempt: int, *, current: bool) -> dict[str, Any]:
    """Plain explanation of one recorded reason code. The receiver's message text is never included."""
    guide = RECEIVER_GUIDES.get(code)
    block: dict[str, Any] = {"code": code, "scope": scope, "stage": stage, "attempt": attempt, "current": current,
                             "known_code": guide is not None, "message": "withheld"}
    if guide is None:
        block.update(summary="This version of ADAPT does not know this reason code; newer receivers can add codes.",
                     possible_causes=[], next_steps=[{"text": "Update ADAPT, or read the receiver README's reason-code table.", "effect": "manual"}])
    else:
        block.update(summary=guide.summary, possible_causes=[cause.text for cause in guide.causes],
                     next_steps=[_step_dict(step) for step in guide.steps],
                     help="Causes are possibilities, not findings: ADAPT has not examined this PC's radio, driver or phone.")
    return block


# ---------------------------------------------------------------------------
# Data directory, status file and processes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DataDir:
    path: Path | None
    display: str            # what may be shown: what the user supplied, or the symbolic default
    source: str             # "option", "environment" or "default"
    problem: str | None = None


def resolve_data_dir(option: str | None, environ: Mapping[str, str] | None = None) -> DataDir:
    """The receiver's data directory: --data-dir, else ADAPT_DEX_DATA_DIR, else %LOCALAPPDATA%\\DeXDesk.

    The default is displayed symbolically so the account name is not printed.
    """
    env = os.environ if environ is None else environ
    if option is not None:
        supplied, source = option, "option"
    elif env.get(ENV_DATA_DIR):
        supplied, source = env[ENV_DATA_DIR], "environment"
    else:
        local = env.get("LOCALAPPDATA")
        if not local:
            return DataDir(None, "%LOCALAPPDATA%\\DeXDesk", "default", "Windows local app data (LOCALAPPDATA) is not set on this host.")
        return DataDir(Path(local) / "DeXDesk", "%LOCALAPPDATA%\\DeXDesk", "default")
    if not supplied or "\x00" in supplied or any(unicodedata.category(c) == "Cc" for c in supplied):
        return DataDir(None, "(invalid)", source, "The data directory is empty or contains control characters.")
    if source == "environment" and not os.path.isabs(supplied):
        return DataDir(None, supplied, source, "ADAPT_DEX_DATA_DIR is not an absolute path; the receiver refuses to use it.")
    return DataDir(Path(os.path.abspath(supplied)), supplied, source)


@dataclass(frozen=True)
class StatusRead:
    kind: str                         # ok | no_directory | no_file | unreadable | invalid | unsupported_schema
    observed: dict[str, Any] | None = None
    detail: str | None = None


def _end_record(value: object) -> tuple[dict[str, Any] | None, bool]:
    """(record, valid). Only validated tokens and integers are copied; the message is dropped."""
    if value is None:
        return None, True
    if not isinstance(value, dict):
        return None, False
    code, scope, stage = _token(value.get("code")), _token(value.get("scope")), _token(value.get("stage"))
    attempt, at = value.get("attempt"), value.get("at_epoch_ms")
    if not code or scope not in {"receiver", "session"} or not stage or not _is_int(attempt) or attempt < 0:
        return None, False
    return {"code": code, "scope": scope, "stage": stage, "attempt": attempt, "at_epoch_ms": at if _is_int(at) else None}, True


def _normalize(document: object) -> StatusRead:
    if not isinstance(document, dict) or not _is_int(document.get("schema_version")):
        return StatusRead("invalid", detail="not a status document")
    version = document["schema_version"]
    if version != STATUS_SCHEMA_VERSION:
        return StatusRead("unsupported_schema", detail=str(version))
    trace = document.get("trace")
    if not isinstance(trace, dict):
        return StatusRead("invalid", detail="no trace")
    attempt, active, last_stage, stages = trace.get("attempt"), trace.get("attempt_active"), _token(trace.get("last_stage")), trace.get("observed_stages")
    flags = [document.get(key) for key in ("running", "advertising", "session_active")]
    pid, updated, frames, phase = document.get("process_id"), document.get("updated_at_epoch_ms"), document.get("frames"), _token(document.get("phase_code"))
    if (not _is_int(attempt) or attempt < 0 or not isinstance(active, bool) or not last_stage or not isinstance(stages, list)
            or len(stages) > 64 or not all(isinstance(flag, bool) for flag in flags) or not _is_int(pid) or pid <= 0
            or not _is_int(updated) or updated < 0 or not _is_int(frames) or frames < 0 or not phase):
        return StatusRead("invalid", detail="missing or mistyped fields")
    failure, ok_failure = _end_record(document.get("last_failure"))
    ended, ok_end = _end_record(document.get("last_session_end"))
    if not ok_failure or not ok_end:
        return StatusRead("invalid", detail="malformed failure record")
    seen = {token for token in map(_token, stages) if token}
    ordered = [stage for stage in STAGE_ORDER if stage in seen] + sorted(seen - set(STAGE_ORDER))
    admission, peer = document.get("admission"), document.get("peer")
    version_text = document.get("receiver_version")
    started = document.get("started_at_epoch_ms")
    ignored = document.get("ignored_connection_requests")
    observed = {
        "receiver_version": version_text if isinstance(version_text, str) and _VERSION.fullmatch(version_text) else None,
        "process_id": pid, "started_at_epoch_ms": started if _is_int(started) else None, "updated_at_epoch_ms": updated,
        "running": flags[0], "advertising": flags[1], "session_active": flags[2], "phase_code": phase, "frames": frames,
        "trace": {"attempt": attempt, "attempt_active": active, "last_stage": last_stage, "observed_stages": ordered},
        "admission": ({key: admission.get(key) for key in ("open", "name_filter", "id_filter")}
                      if isinstance(admission, dict) and all(isinstance(admission.get(key), bool) for key in ("open", "name_filter", "id_filter")) else None),
        "peer": ({"present": peer.get("present"), "admitted": peer.get("admitted")}
                 if isinstance(peer, dict) and isinstance(peer.get("present"), bool) and peer.get("admitted") in (True, False, None) else None),
        "ignored_connection_requests": ignored if _is_int(ignored) and ignored >= 0 else 0,
        "last_failure": failure, "last_session_end": ended,
    }
    return StatusRead("ok", observed)


def read_status(directory: Path | None, *, attempts: int = 3) -> StatusRead:
    """Read ``status.json``. The receiver replaces it by rename, so a failed read is retried briefly."""
    if directory is None:
        return StatusRead("no_directory")
    try:
        if not directory.exists():
            return StatusRead("no_directory")
        if not directory.is_dir():
            return StatusRead("unreadable", detail="the data directory path is not a directory")
    except OSError:
        return StatusRead("unreadable", detail="the data directory could not be examined")
    path = directory / STATUS_FILE
    for attempt in range(attempts):
        try:
            if path.is_symlink():
                return StatusRead("unreadable", detail="status.json is a symbolic link")
            if not path.exists():
                return StatusRead("no_file")
            if not path.is_file() or path.stat().st_size > MAX_STATUS_BYTES:
                return StatusRead("unreadable", detail="status.json is not a regular file of plausible size")
            document = json.loads(path.read_bytes().decode("utf-8"))
        except FileNotFoundError:
            return StatusRead("no_file")
        except (OSError, ValueError, RecursionError):
            if attempt + 1 < attempts:
                _sleep(0.1)
                continue
            return StatusRead("unreadable", detail="status.json could not be read or is not valid JSON")
        return _normalize(document)
    return StatusRead("unreadable", detail="status.json could not be read")


def receiver_processes() -> list[int] | None:
    """Process IDs of running ``dexdesk.exe`` processes, or None when this host cannot tell.

    Uses the system ``tasklist`` (image name and PID only). Never ``os.kill``: on
    Windows that terminates the process.
    """
    if not _is_windows():
        return None
    root = os.environ.get("SystemRoot") or os.environ.get("windir")
    program = os.path.join(root, "System32", "tasklist.exe") if root else None
    if not program or not os.path.isfile(program):
        return None
    try:
        result = subprocess.run([program, "/FI", f"IMAGENAME eq {RECEIVER_EXE}", "/FO", "CSV", "/NH"], capture_output=True,
                                text=True, errors="replace", timeout=15, check=False, creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    pids: list[int] = []
    for row in csv.reader(io.StringIO(result.stdout or "")):
        if len(row) >= 2 and row[0].strip().lower() == RECEIVER_EXE and row[1].strip().isdigit():
            pids.append(int(row[1]))
    return pids


def port_state(port: int = RECEIVER_PORT) -> str:
    """``free``, ``in_use``, ``denied`` or ``unknown`` for TCP ``port`` on every IPv4 interface.

    Binds without listening and closes at once, which is the refusal the receiver
    itself would meet (it reports both "in use" and "access denied" as port_in_use).
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("0.0.0.0", port))
    except OSError as exc:
        code = getattr(exc, "winerror", None) or exc.errno
        if code in (10048, 98, 48):       # WSAEADDRINUSE, EADDRINUSE (Linux, macOS)
            return "in_use"
        if code in (10013, 13):           # WSAEACCES, EACCES
            return "denied"
        return "unknown"
    finally:
        probe.close()
    return "free"


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def _age_seconds(observed: dict[str, Any], now_ms: int) -> float:
    return max(0.0, (now_ms - observed["updated_at_epoch_ms"]) / 1000.0)


def assess(observed: dict[str, Any], *, alive: bool | None, now_ms: int) -> tuple[str, str | None]:
    """(state, stale_reason) following the receiver README's rules for supervisors."""
    age = _age_seconds(observed, now_ms)
    if alive is False:
        return "not_running", ("process_gone" if observed["running"] else None)
    if age > STALE_AFTER_SECONDS:
        return "stale", "not_updated"
    if not observed["running"]:
        return "stopping", None
    failure = observed["last_failure"]
    receiver_failed = failure is not None and failure["scope"] == "receiver"
    if observed["phase_code"] == "connected" and observed["frames"] > 0:
        return "streaming", None
    if observed["session_active"] or observed["phase_code"] in {"connecting", "connected"}:
        return "connecting", None
    if not observed["advertising"]:
        return ("not_working" if receiver_failed else "starting"), None
    return "ready", None


def _stage_view(observed: dict[str, Any]) -> dict[str, Any]:
    trace = observed["trace"]
    seen = trace["observed_stages"]
    if trace["attempt"] == 0:
        return {"scope": "before_any_phone", "observed": seen, "not_observed": [], "attempt": 0}
    missing = [stage for stage in ATTEMPT_STAGES if stage not in seen]
    return {"scope": "last_attempt", "observed": seen, "not_observed": missing, "attempt": trace["attempt"]}


def _state_steps(state: str, observed: dict[str, Any] | None, failure_block: dict[str, Any] | None) -> list[dict[str, str]]:
    if state == "streaming":
        steps = [
            {"text": "Observe the Samsung state and display IDs on the phone (this transfers and runs ADAPT's bundled helper temporarily; it needs --yes):",
             "command": "adapt dex pc-probe -s SERIAL --yes", "effect": "changes_device"},
            {"text": "Only if that probe positively identifies the desktop display, open a separate controllable scrcpy window on it; the receiver connection stays yours:",
             "command": "adapt dex pc-attach -s SERIAL --display DISPLAY_ID --yes", "effect": "changes_device"},
            {"text": "The receiver's own window input and a scrcpy window's input are different paths; test them separately.", "effect": "manual"},
        ]
        if observed is not None and "input_channel_connected" not in observed["trace"]["observed_stages"]:
            steps.insert(2, {"text": "The receiver has not reported its mouse and keyboard channel as connected in this session.", "effect": "manual"})
        return steps
    if state == "ready":
        return [{"text": "On the phone, open Samsung DeX's wireless receiver picker and choose this PC.", "effect": "on_phone"},
                {"text": "Read the state again after the phone connects:", "command": "adapt dex receiver-status", "effect": "read_only"},
                {"text": "Close the receiver when you are done:", "command": "adapt dex receiver-stop --yes", "effect": "changes_host"}]
    if state in {"starting", "connecting"}:
        return [{"text": "Read the state again in a few seconds:", "command": "adapt dex receiver-status", "effect": "read_only"}]
    if state == "stopping":
        return [{"text": "Read the state again in a few seconds; it should become not running:", "command": "adapt dex receiver-status", "effect": "read_only"}]
    if state == "not_working":
        steps = list(failure_block["next_steps"]) if failure_block else []
        return steps or [{"text": "Close the receiver and start it again:", "command": "adapt dex receiver-stop --yes", "effect": "changes_host"}]
    if state == "stale":
        return [{"text": "Close the receiver; if it does not close, close its window yourself (ADAPT never ends a process by ID):",
                 "command": "adapt dex receiver-stop --yes", "effect": "changes_host"}]
    if state == "unverified":
        return [{"text": "If you started the receiver with another data directory, point at it:", "command": "adapt dex receiver-status --data-dir DIRECTORY", "effect": "read_only"},
                {"text": "Otherwise wait a few seconds and read the state again:", "command": "adapt dex receiver-status", "effect": "read_only"}]
    if state == "unreadable":
        return [{"text": "Check the data directory and status file, or start the receiver again so it writes a fresh status file.", "effect": "manual"}]
    return [{"text": "Start it when you want a phone to connect (this advertises the PC over Wi-Fi Direct):",
             "command": "adapt dex receiver-start --exe PATH_TO_DEXDESK_EXE --yes", "effect": "changes_host"},
            {"text": "No built receiver yet? Read the plan:", "command": "adapt dex receiver-plan", "effect": "read_only"}]


STATE_SUMMARIES = {
    "not_running": "No receiver is running here.",
    "starting": "The receiver process is running but has not started advertising yet.",
    "ready": "The receiver is advertising and waiting for a phone. That is not a working desktop: no phone is connected.",
    "connecting": "A phone's connection attempt is in progress; no picture has been decoded yet.",
    "streaming": "A phone is connected and the receiver is decoding its video.",
    "stopping": "The receiver has begun shutting down.",
    "not_working": "The receiver is running but is not advertising, so no phone can find it.",
    "stale": "The status file is out of date: the receiver process is alive but has not updated it recently.",
    "unverified": "A dexdesk.exe process is running, but this data directory has no usable status file for it.",
    "unreadable": "The status file could not be used, so the receiver's state is not known.",
}


def _failure_context(observed: dict[str, Any], record: dict[str, Any] | None) -> dict[str, Any] | None:
    if record is None:
        return None
    trace = observed["trace"]
    current = record["scope"] == "receiver" or record["attempt"] == trace["attempt"]
    return explain_code(record["code"], record["scope"], record["stage"], record["attempt"], current=current)


def status(data_dir: str | None = None, *, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Read-only report of the receiver's recorded state."""
    location = resolve_data_dir(data_dir, environ)
    if location.problem:
        return _refuse("data_dir_invalid", location.problem, data_dir=location.display, data_dir_source=location.source)
    read = read_status(location.path)
    processes = receiver_processes()
    now_ms = int(_now() * 1000)
    report: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "status": "observed", "data_dir": location.display,
                              "data_dir_source": location.source, "claim_boundary": STATUS_BOUNDARY}
    report["running_dexdesk_processes"] = processes
    if read.kind != "ok":
        file_state = {"no_directory": "no_directory", "no_file": "missing", "unreadable": "unreadable",
                      "invalid": "invalid", "unsupported_schema": "unsupported_schema"}[read.kind]
        if read.kind not in {"no_directory", "no_file"}:
            state = "unreadable"          # a file exists but cannot be used: that is the more useful fact
        elif processes:
            state = "unverified"
        else:
            state = "not_running"
        summary = STATE_SUMMARIES[state]
        if state == "not_running":
            summary = ("No receiver is running here: the data directory does not exist." if read.kind == "no_directory"
                       else "No receiver is running here: the data directory has no status file.")
            if processes is None:
                summary += " This host could not check for a dexdesk.exe process."
        elif state == "unreadable" and read.kind == "unsupported_schema":
            shown = read.detail if read.detail and len(read.detail) <= 12 else "an unrecognized"
            summary = (f"The status file uses schema version {shown}, which this ADAPT does not understand (it reads version "
                       f"{STATUS_SCHEMA_VERSION}); a newer receiver may need a newer ADAPT.")
        report.update(state=state, state_label=STATE_LABELS[state], summary=summary, status_file=file_state,
                      process={"process_id": None, "alive": None, "checked": processes is not None},
                      stale=False, observed=None, last_failure=None, last_session_end=None,
                      next_steps=_state_steps(state, None, None))
        if read.kind == "unsupported_schema":
            report["status_schema_version"] = int(read.detail) if read.detail and len(read.detail) <= 12 else None
        report["note"] = ("The receiver writes status.json in its data directory; a file that is missing, unreadable or from a newer schema "
                          "says nothing about whether a receiver exists elsewhere.")
        return report
    observed = read.observed or {}
    alive = None if processes is None else observed["process_id"] in processes
    state, stale_reason = assess(observed, alive=alive, now_ms=now_ms)
    failure_block = _failure_context(observed, observed["last_failure"])
    end_block = _failure_context(observed, observed["last_session_end"])
    if end_block is not None:
        end_block["failure"] = observed["last_session_end"]["code"] not in ORDERLY_ENDS
    summary = STATE_SUMMARIES[state]
    if state == "not_running":
        summary = ("The last receiver exited without recording that it was shutting down; its process is gone, so this status file is stale. "
                   "It may have crashed or been ended from outside ADAPT." if stale_reason
                   else "The receiver was stopped; its status file records a completed shutdown.")
    elif state == "streaming":
        summary = f"A phone is streaming: the receiver reports {observed['frames']} decoded frames in the current session."
    elif state == "stale":
        summary += f" It was last updated about {int(_age_seconds(observed, now_ms))} seconds ago, so the receiver may be hung or the file may not be its own."
    warnings: list[str] = []
    if alive is None:
        warnings.append("This host cannot check whether the receiver's process is alive, so its state is as written in the status file.")
    if state in {"streaming", "connecting"} and failure_block and failure_block["scope"] == "receiver":
        warnings.append("The receiver has also recorded a receiver-level failure (see last_failure); a new phone may not be able to find this PC.")
    if observed["ignored_connection_requests"]:
        warnings.append(f"{observed['ignored_connection_requests']} connection request(s) were ignored while a session was active.")
    report.update(
        state=state, state_label=STATE_LABELS[state], summary=summary, status_file="present",
        receiver_version=observed["receiver_version"], stale=stale_reason is not None or state == "stale", stale_reason=stale_reason,
        process={"process_id": observed["process_id"], "alive": alive, "checked": processes is not None},
        updated_seconds_ago=round(_age_seconds(observed, now_ms), 1),
        observed={"running": observed["running"], "advertising": observed["advertising"], "session_active": observed["session_active"],
                  "phase_code": observed["phase_code"], "frames": observed["frames"], "attempt": observed["trace"]["attempt"],
                  "attempt_active": observed["trace"]["attempt_active"], "admission": observed["admission"], "peer": observed["peer"],
                  "ignored_connection_requests": observed["ignored_connection_requests"]},
        stages=_stage_view(observed), last_failure=failure_block, last_session_end=end_block, warnings=warnings,
        next_steps=_state_steps(state, observed, failure_block))
    if state == "not_running" and stale_reason and failure_block and (failure_block["scope"] == "receiver" or failure_block["code"] == "internal_panic"):
        report["next_steps"] = failure_block["next_steps"] + report["next_steps"]
    return report


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------

def _valid_value(variable: str, value: str, limit: int) -> str | None:
    """None when usable, else a reason. Mirrors the receiver's own check (bytes, no control characters)."""
    if value == "":
        return f"{variable} is empty; an empty value restricts nothing, so omit it instead."
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return f"{variable} is not valid Unicode."
    if size > limit:
        return f"{variable} is longer than {limit} bytes."
    if any(unicodedata.category(c) == "Cc" for c in value):
        return f"{variable} contains a control character."
    return None


def _start_failure(code: int | None, process_id: int, observed: dict[str, Any] | None, alive: bool) -> dict[str, Any]:
    if observed is not None and observed["last_failure"] is not None:
        block = _failure_context(observed, observed["last_failure"])
        return {"summary": "The receiver recorded a failure: " + block["summary"], "last_failure": block, "next_steps": block["next_steps"]}
    if not alive and code == 0:
        text = ("The receiver process exited at once with code 0 and recorded nothing. That is what it does when another instance already "
                "owns the window (it focuses that window and exits); the process list could not rule that out beforehand.")
    elif not alive:
        text = (f"The receiver process exited with code {code} before it recorded a state. Start-up errors that happen before it has any state "
                "(the data directory cannot be created, the log cannot be opened) go to its stderr and a message box; ADAPT does not capture either.")
    else:
        text = ("The receiver process is running but has written no status for itself within the wait. If a message box appeared, it describes a "
                "start-up problem that the receiver reports only there.")
    return {"summary": text, "last_failure": None,
            "next_steps": [{"text": "Read the state again:", "command": "adapt dex receiver-status", "effect": "read_only"},
                           {"text": "Close the receiver if it is still running:", "command": "adapt dex receiver-stop --yes", "effect": "changes_host"}]}


def start(exe: str, *, data_dir: str | None = None, expect_name: str | None = None, expect_id: str | None = None,
          audio: bool = False, yes: bool = False, environ: Mapping[str, str] | None = None,
          wait_seconds: float = START_WAIT_SECONDS) -> dict[str, Any]:
    """Start a user-built receiver detached and return once its status shows it advertising."""
    env = dict(os.environ if environ is None else environ)
    if not _is_windows():
        return _refuse("not_windows", "The native receiver is a Windows program; ADAPT starts it only on Windows.")
    executable = Path(exe)
    if executable.name.lower() != RECEIVER_EXE:
        return _refuse("exe_name", f"--exe must name a file called {RECEIVER_EXE}; ADAPT does not search for it or run any other program.", exe=exe)
    try:
        usable = executable.is_file()
    except OSError:
        usable = False
    if not usable:
        return _refuse("exe_missing", "No file exists at the --exe path; ADAPT does not search the disk or PATH. Build it first (adapt dex receiver-plan).", exe=exe)
    location = resolve_data_dir(data_dir, env)
    if location.problem:
        return _refuse("data_dir_invalid", location.problem, data_dir=location.display)
    try:
        if location.path is not None and location.path.exists() and not location.path.is_dir():
            return _refuse("data_dir_invalid", "The data directory path exists and is not a directory.", data_dir=location.display)
    except OSError:
        return _refuse("data_dir_invalid", "The data directory could not be examined.", data_dir=location.display)
    effective = {ENV_NAME: expect_name if expect_name is not None else env.get(ENV_NAME, ""),
                 ENV_ID: expect_id if expect_id is not None else env.get(ENV_ID, "")}
    for variable, option, limit in ((ENV_NAME, expect_name, NAME_LIMIT_BYTES), (ENV_ID, expect_id, ID_LIMIT_BYTES)):
        if option is not None or effective[variable]:
            problem = _valid_value(variable, effective[variable], limit)
            if problem:
                source = "the option" if option is not None else "this terminal's environment"
                return _refuse("restriction_invalid", f"{problem} (from {source}) The receiver would refuse to start with it.", restriction=variable)
    if not yes:
        return _refuse("yes_required", "Starting the receiver advertises this PC over Wi-Fi Direct, opens TCP 7236 on every IPv4 interface and "
                       "lets a phone connect to it. Review adapt dex receiver-plan, then repeat with --yes.")
    processes = receiver_processes()
    if processes:
        return _refuse("already_running", f"A {RECEIVER_EXE} process is already running (process ID {processes[0]}"
                       + (f" and {len(processes) - 1} more" if len(processes) > 1 else "")
                       + "). The receiver is single-instance: a second launch would only focus the running window and exit. "
                       "Read adapt dex receiver-status, or close it with adapt dex receiver-stop --yes.", process_ids=processes)
    existing = read_status(location.path)
    if processes is None and existing.kind == "ok" and existing.observed["running"] \
            and _age_seconds(existing.observed, int(_now() * 1000)) <= STALE_AFTER_SECONDS:
        return _refuse("already_running", "This host cannot list processes, and the data directory's status file was updated moments ago by a "
                       "receiver that says it is running; not starting a second one.")
    port = port_state()
    if port in {"in_use", "denied"}:
        return _refuse("port_in_use", f"TCP port {RECEIVER_PORT} is {'already in use' if port == 'in_use' else 'not available to this user (access denied, often a reserved range)'}, "
                       "and no receiver of this data directory is running. Another program holds it: for example Windows' own Wireless Display "
                       "receiver or another receiver. Close that program first.", port=port, port_number=RECEIVER_PORT)
    child_env = dict(env)
    if expect_name is not None:
        child_env[ENV_NAME] = expect_name
    if expect_id is not None:
        child_env[ENV_ID] = expect_id
    if audio:
        child_env[ENV_AUDIO] = "1"
    if data_dir is not None and location.path is not None:
        child_env[ENV_DATA_DIR] = str(location.path)
    audio_on = _truthy(child_env.get(ENV_AUDIO))
    audio_source = "option" if audio else ("environment" if ENV_AUDIO in env else "receiver_setting")
    restrictions = {"name": "option" if expect_name is not None else ("environment" if effective[ENV_NAME] else None),
                    "id": "option" if expect_id is not None else ("environment" if effective[ENV_ID] else None)}
    warnings: list[str] = []
    if _truthy(env.get(ENV_LOG_PEER)):
        warnings.append("ADAPT_DEX_LOG_PEER_DETAILS is set in this terminal, so the receiver will write the phone's name, peer ID and addresses to its own log file.")
    if processes is None:
        warnings.append("This host could not list processes, so a running receiver in another data directory was not ruled out.")
    if port == "unknown":
        warnings.append(f"TCP port {RECEIVER_PORT} could not be checked beforehand.")
    launched_ms = int(_now() * 1000)
    try:
        process = subprocess.Popen([os.path.abspath(executable)], env=child_env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, close_fds=True,
                                   creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
    except (OSError, ValueError) as exc:
        return _refuse("launch_failed", "Windows could not start the file named by --exe"
                       + (f" ({_oserror_text(exc)})" if isinstance(exc, OSError) else "") + "; nothing is running.", exe=exe)
    deadline = _clock() + max(0.0, wait_seconds)
    observed: dict[str, Any] | None = None
    state = "starting"
    exit_code: int | None = None
    while True:
        read = read_status(location.path, attempts=1)
        if read.kind == "ok" and read.observed["process_id"] == process.pid \
                and (read.observed["started_at_epoch_ms"] is None or read.observed["started_at_epoch_ms"] >= launched_ms - 5000):
            observed = read.observed
            state, _ = assess(observed, alive=True, now_ms=int(_now() * 1000))
            if state in {"ready", "connecting", "streaming", "not_working"}:
                break
        exit_code = process.poll()
        if exit_code is not None:
            break
        if _clock() >= deadline:
            break
        _sleep(START_POLL_SECONDS)
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION, "data_dir": location.display, "data_dir_source": location.source,
        "process_id": process.pid, "changes_performed": True,
        "request": {"restrictions": restrictions, "audio_requested": audio, "audio_enabled_for_receiver": audio_on, "audio_source": audio_source,
                    "settings_passed_through": "environment variables only; the command line holds only the executable path"},
        "warnings": warnings, "listener": LISTENER_NOTE,
    }
    if state in {"ready", "connecting", "streaming"} and exit_code is None and observed is not None:
        reported = observed["admission"]
        restricted = (not reported["open"]) if reported is not None else any(restrictions.values())
        report.update(status="started", state=state, state_label=STATE_LABELS[state], receiver_version=observed["receiver_version"],
                      admission_reported_by_receiver=reported,
                      admission_note=(NOT_AUTHENTICATION if restricted else OPEN_ADMISSION),
                      stages=_stage_view(observed),
                      next_steps=_state_steps(state, observed, None),
                      note="Process creation plus an advertising status are start-up facts only. The receiver is running detached with its own window; "
                           "nothing here shows a phone connecting, a decoded frame, input, audio or Samsung DeX.",
                      claim_boundary=STATUS_BOUNDARY)
        if audio_on:
            report["audio_note"] = "Audio is experimental and off by default; with it on, correct audible sound has not been established."
        return report
    detail = _start_failure(exit_code, process.pid, observed, exit_code is None)
    still_running = exit_code is None
    report.update(status="failed" if (exit_code is not None or state == "not_working") else "start_pending", state=state if still_running else "not_running",
                  summary=detail["summary"], last_failure=detail["last_failure"], next_steps=detail["next_steps"],
                  process_still_running=still_running, exit_code=exit_code,
                  admission_note=(NOT_AUTHENTICATION if any(restrictions.values()) else OPEN_ADMISSION),
                  note=("ADAPT does not end the receiver: it keeps running until it is closed with adapt dex receiver-stop --yes or its window."
                        if still_running else "The receiver is not running."),
                  claim_boundary=STATUS_BOUNDARY)
    return report


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------

def stop(data_dir: str | None = None, *, yes: bool = False, environ: Mapping[str, str] | None = None,
         wait_seconds: float = STOP_WAIT_SECONDS) -> dict[str, Any]:
    """Ask the receiver to stop through ``stop.request`` and wait a bounded time. Never ends a process."""
    if not _is_windows():
        return _refuse("not_windows", "The native receiver is a Windows program; ADAPT can check and stop it only on Windows.")
    location = resolve_data_dir(data_dir, environ)
    if location.problem:
        return _refuse("data_dir_invalid", location.problem, data_dir=location.display)
    if not yes:
        return _refuse("yes_required", "Stopping closes the receiver window and ends any phone session in progress. Repeat with --yes.")
    processes = receiver_processes()
    if processes is None:
        return _refuse("process_check_unavailable", "This host cannot list processes, so ADAPT cannot tell that the status file belongs to a live "
                       "receiver. No stop request was written (a request for a receiver that has not started is deleted by the next start).")
    base: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "data_dir": location.display, "data_dir_source": location.source,
                            "claim_boundary": "A stop request asks the receiver to close; the report says what its process and status file showed afterwards. ADAPT never ends a process by ID."}
    read = read_status(location.path)
    if read.kind != "ok":
        if processes:
            return _refuse("no_usable_status", f"A {RECEIVER_EXE} process is running (process ID {processes[0]}) but this data directory has no usable status file for it, "
                           "so no stop request was written: it may use another data directory (use --data-dir) or may still be starting.", process_ids=processes, data_dir=location.display, data_dir_source=location.source)
        return {**base, "status": "nothing_running", "changes_performed": False, "state": "not_running",
                "summary": "No receiver is running: no dexdesk.exe process was found."
                           + ("" if read.kind in {"no_directory", "no_file"} else " The status file in this data directory could not be used."),
                "stop_request_written": False}
    observed = read.observed or {}
    pid = observed["process_id"]
    alive = pid in processes
    others = [p for p in processes if p != pid]
    if not alive:
        report = {**base, "status": "nothing_running", "changes_performed": False, "state": "not_running", "stale": bool(observed["running"]),
                  "summary": "No receiver is running: the process named in the status file is gone"
                             + (", so the file is stale." if observed["running"] else "; the file records a completed shutdown."),
                  "stop_request_written": False}
        if others:
            report["other_dexdesk_processes"] = len(others)
            report["note"] = "Another dexdesk.exe process is running that this data directory's status file does not belong to; it was not touched."
        return report
    written = False
    existing = location.path / STOP_FILE
    if observed["running"]:
        try:
            with open(existing, "x", encoding="ascii") as stream:
                stream.write("stop\n")
            written = True
        except OSError as exc:
            # FileExistsError for a file already there; Windows reports PermissionError when a directory is in the way.
            if existing.is_file():
                pass
            elif existing.exists():
                return _refuse("stop_request_blocked", "A directory or other non-file object named stop.request is in the way, so the receiver would not see a request.")
            else:
                return _refuse("stop_request_failed", f"The stop request could not be written ({_oserror_text(exc)}).")
    deadline = _clock() + max(0.0, wait_seconds)
    exited = False
    while True:
        current = receiver_processes()
        if current is not None and pid not in current:
            exited = True
            break
        if _clock() >= deadline:
            break
        _sleep(STOP_POLL_SECONDS)
    after = read_status(location.path, attempts=1)
    final = after.observed if after.kind == "ok" and after.observed["process_id"] == pid else None
    report = {**base, "changes_performed": written, "process_id": pid, "stop_request_written": written,
              "stop_request_already_present": (not written and observed["running"]), "waited_seconds_at_most": wait_seconds}
    if exited:
        removed = False
        try:
            if existing.is_file():
                existing.unlink()
                removed = True
        except OSError:
            removed = False
        clean = final is not None and not final["running"]
        end = final["last_session_end"] if final else None
        report.update(status="stopped", state="not_running", stop_request_removed=removed,
                      shutdown_recorded=clean, final_status="recorded_stopped" if clean else "not_recorded",
                      last_session_end=(None if end is None else explain_code(end["code"], end["scope"], end["stage"], end["attempt"],
                                                                              current=end["attempt"] == final["trace"]["attempt"])),
                      summary=("The receiver process has exited"
                               + ("; its status file records a completed shutdown." if clean
                                  else ", but its status file does not record a completed shutdown (it may have been interrupted).")))
        return report
    report.update(status="stop_pending", state="stopping", process_still_running=True,
                  summary=f"The receiver process (ID {pid}) was still running after the wait; the stop request remains in the data directory.",
                  next_steps=[{"text": "Read the state again:", "command": "adapt dex receiver-status", "effect": "read_only"},
                              {"text": "The receiver checks for the request about once a second. If its window is stuck, close the window yourself or end dexdesk.exe from Task Manager; ADAPT never ends a process by ID.", "effect": "manual"}],
                  note="The request file was left in place so a slow receiver can still act on it; the next start deletes a stale one.")
    return report


# ---------------------------------------------------------------------------
# Plan (offline)
# ---------------------------------------------------------------------------

def receiver_plan() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION, "status": "plan_only", "windows_only": True, "changes_performed": False, "runs_nothing": True,
        "routes": [
            {"id": "microsoft_wireless_display", "maturity": "Microsoft's optional Windows feature", "plan_command": "adapt dex pc-receiver-plan",
             "summary": "Install Microsoft's Wireless Display feature (administrator approval) and use its receiver window. ADAPT only explains the steps."},
            {"id": "native_receiver", "maturity": "experimental source that you build yourself", "plan_command": "adapt dex receiver-plan",
             "summary": "ADAPT's own Windows receiver (package dexdesk, window title 'ADAPT Wireless DeX'), derived from the MIT-licensed DeXDesk. "
                        "Samsung DeX still runs on the phone; this PC shows and controls that desktop in the receiver's own window."},
        ],
        "native_receiver": {
            "source": "native/windows-receiver in an ADAPT source checkout; it is not in the Python wheel, and ADAPT never downloads or bundles it",
            "prerequisites": [
                "Windows x64",
                "A Wi-Fi adapter and driver that support Wi-Fi Direct (netsh wlan show drivers has a 'Wireless Display Supported' line, a hint and not proof); ADAPT has not checked yours",
                "The Rust toolchain pinned in native/windows-receiver/rust-toolchain.toml (1.99.1); rustup may download it, and cargo downloads the locked crates, on the first build",
                "TCP port 7236 free: close Windows' own Wireless Display receiver and any other receiver first",
                "A Samsung phone whose firmware offers wireless DeX; that is outside this program's control",
            ],
            "build": {"command": "powershell -NoProfile -File scripts\\build_receiver.ps1", "preview_without_building": "powershell -NoProfile -File scripts\\build_receiver.ps1 -DryRun",
                      "equivalent": "cd native\\windows-receiver; cargo build --release --locked",
                      "executable": "native\\windows-receiver\\target\\release\\dexdesk.exe"},
            "data_directory": "%LOCALAPPDATA%\\DeXDesk, or the absolute directory in ADAPT_DEX_DATA_DIR or --data-dir; it holds status.json, a log and settings",
            "security_posture": [
                "Admission is open by default: any Wi-Fi Direct device in radio range that requests a connection is admitted, with no prompt and no pairing code.",
                "--expect-name and --expect-id narrow that to one phone name or peer ID. They are not authentication: a device can announce any name, and a peer ID is only as secret as the radio traffic that carries it.",
                "It listens on TCP 7236 on every IPv4 interface while it runs and creates no firewall rule; Windows Defender Firewall alone decides who can reach it.",
                "It decodes H.264 video sent by the phone with Windows' media decoder, and sends the phone this PC's mouse and keyboard events.",
                "Its log withholds the phone's name, peer ID and addresses by default; ADAPT never reads the log.",
            ],
            "commands": [
                {"step": "Build it (locked; ADAPT does not run this for you)", "command": "powershell -NoProfile -File scripts\\build_receiver.ps1"},
                {"step": "Start it and wait for it to advertise (advertises this PC over Wi-Fi Direct)", "command": "adapt dex receiver-start --exe PATH_TO_DEXDESK_EXE --yes"},
                {"step": "Choose this PC in Samsung DeX's wireless receiver picker on the phone", "command": None},
                {"step": "Read what the receiver recorded", "command": "adapt dex receiver-status"},
                {"step": "Observe the Samsung desktop state and display IDs once a phone is streaming", "command": "adapt dex pc-probe -s SERIAL --yes"},
                {"step": "Optionally open a separate controllable scrcpy window on the observed desktop display", "command": "adapt dex pc-attach -s SERIAL --display DISPLAY_ID --yes"},
                {"step": "Close it gracefully", "command": "adapt dex receiver-stop --yes"},
            ],
            "observed_with": "One Android 13 Samsung phone and one Windows 11 PC, in short sessions (native/windows-receiver/README.md, 'Known boundaries'). Audio and real mouse and keyboard use are not established.",
        },
        "note": "Plan only: this command runs nothing and checks nothing on this PC. Whether your adapter supports Wi-Fi Direct, whether Rust is installed and whether port 7236 is free are not observed here.",
        "claim_boundary": "A built, running or advertising receiver is not a working desktop and not Samsung DeX. Compatibility with Android builds, Wi-Fi radios and Windows versions is not promised; the receiver relies on an undocumented Windows provider.",
    }


# ---------------------------------------------------------------------------
# Rendering and commands
# ---------------------------------------------------------------------------

def _step_lines(steps: list[dict[str, str]], indent: str = "  ") -> list[str]:
    lines: list[str] = []
    for number, step in enumerate(steps, 1):
        lines.append(f"{indent}{number}. {step['text']}")
        if step.get("command"):
            lines.append(f"{indent}   {step['command']}")
    return lines


def _failure_lines(title: str, block: dict[str, Any]) -> list[str]:
    scope = "receiver-wide" if block["scope"] == "receiver" else f"phone attempt {block['attempt']}"
    lines = ["", f"{title}: {block['code']} ({scope}, after stage {block['stage']}{'' if block['current'] else '; an earlier attempt'})", f"  {block['summary']}"]
    if block["possible_causes"]:
        lines.append("  Possible causes (not established):")
        lines += [f"    - {cause}" for cause in block["possible_causes"]]
    if block["next_steps"]:
        lines.append("  Next steps:")
        lines += _step_lines(block["next_steps"], "    ")
    return lines


def render_refused(report: dict[str, Any]) -> str:
    return f"{HEADER}\nRefused ({report['reason']}): {report['error']}\nNothing was changed."


def render_plan(report: dict[str, Any]) -> str:
    native = report["native_receiver"]
    lines = [HEADER, "Two ways to give a Samsung phone a Windows DeX receiver:"]
    for route in report["routes"]:
        lines += [f"  - {route['id']} ({route['maturity']})", f"    {route['summary']}", f"    More: {route['plan_command']}"]
    lines += ["", "The native receiver", f"  Source: {native['source']}", "  Prerequisites:"]
    lines += [f"    - {item}" for item in native["prerequisites"]]
    build = native["build"]
    lines += ["  Build:", f"    {build['command']}", f"    Preview only: {build['preview_without_building']}", f"    Equivalent: {build['equivalent']}",
              f"    Produces: {build['executable']}", f"  Data directory: {native['data_directory']}", "  Security posture:"]
    lines += [f"    - {item}" for item in native["security_posture"]]
    lines.append("  Commands, in order:")
    for number, item in enumerate(native["commands"], 1):
        lines.append(f"    {number}. {item['step']}" + (f"\n       {item['command']}" if item["command"] else ""))
    lines += [f"  Observed so far: {native['observed_with']}", "", report["note"], report["claim_boundary"]]
    return "\n".join(lines)


def render_status(report: dict[str, Any]) -> str:
    lines = [HEADER, f"State: {report['state_label']}", report["summary"], f"Data directory: {report['data_dir']} ({report['data_dir_source']})"]
    process = report["process"]
    if process["process_id"] is not None:
        alive = {True: "alive", False: "gone", None: "not checked"}[process["alive"]]
        lines.append(f"Receiver process: {process['process_id']} ({alive})"
                     + (f"; status last updated {report['updated_seconds_ago']} s ago" if "updated_seconds_ago" in report else ""))
    elif report.get("running_dexdesk_processes"):
        lines.append(f"dexdesk.exe processes found: {', '.join(map(str, report['running_dexdesk_processes']))}")
    if report.get("stale"):
        lines.append("This status is STALE" + (": the process that wrote it is gone." if report.get("stale_reason") == "process_gone" else "."))
    observed = report.get("observed")
    if observed:
        admission = observed["admission"]
        if admission is not None:
            restricted = [name for name, flag in (("phone name", admission["name_filter"]), ("peer ID", admission["id_filter"])) if flag]
            lines.append("Admission: open (any Wi-Fi Direct device in range is admitted)" if admission["open"]
                         else f"Admission: restricted by {' and '.join(restricted)} (not authentication)")
        stages = report["stages"]
        label = "Stages observed before any phone connected" if stages["scope"] == "before_any_phone" else f"Stages observed in the last phone attempt (#{stages['attempt']})"
        lines.append(f"{label}: {', '.join(stages['observed']) or 'none'}")
        if stages["not_observed"]:
            lines.append(f"Not observed in that attempt: {', '.join(stages['not_observed'])}")
        if observed["frames"]:
            lines.append(f"Decoded frames this session: {observed['frames']}")
    for warning in report.get("warnings", []):
        lines.append(f"Note: {warning}")
    if report.get("last_failure"):
        lines += _failure_lines("Last failure", report["last_failure"])
    end = report.get("last_session_end")
    if end and not (report.get("last_failure") and end["code"] == report["last_failure"]["code"] and end["attempt"] == report["last_failure"]["attempt"]):
        lines += _failure_lines("Last session ended" if end.get("failure") is False else "Last session end", end)
    if report["next_steps"] and report["state"] != "not_working":
        lines += ["", "Next steps:"] + _step_lines(report["next_steps"])
    lines += ["", report["claim_boundary"]]
    return "\n".join(lines)


def render_start(report: dict[str, Any]) -> str:
    started = report["status"] == "started"
    lines = [HEADER, f"Start: {report['status']}" + (f" ({report['state_label']})" if started else "")]
    lines.append(f"Receiver process: {report['process_id']}" + ("" if report.get("process_still_running", True) else " (has exited)"))
    lines.append(f"Data directory: {report['data_dir']} ({report['data_dir_source']})")
    lines.append(report["admission_note"])
    lines.append(report["listener"])
    request = report["request"]
    for key, label in (("name", "phone name"), ("id", "peer ID")):
        if request["restrictions"][key]:
            lines.append(f"Restriction by {label}: yes (from {request['restrictions'][key]}; the value is not shown)")
    lines.append(f"Audio: {'on' if request['audio_enabled_for_receiver'] else 'off or left to the receiver setting'} (experimental)")
    for warning in report["warnings"]:
        lines.append(f"Note: {warning}")
    if not started:
        lines += ["", report["summary"]]
        if report.get("last_failure"):
            lines += _failure_lines("Recorded failure", report["last_failure"])
    lines += ["", "Next steps:"] + _step_lines(report["next_steps"])
    lines += ["", report["note"], report["claim_boundary"]]
    return "\n".join(lines)


def render_stop(report: dict[str, Any]) -> str:
    lines = [HEADER, f"Stop: {report['status']}", report["summary"], f"Data directory: {report['data_dir']} ({report['data_dir_source']})"]
    if "stop_request_written" in report:
        lines.append("Stop request: " + ("written" if report["stop_request_written"] else ("already present" if report.get("stop_request_already_present") else "not written")))
    if report.get("stop_request_removed"):
        lines.append("The stop request file was removed after the receiver exited.")
    if report.get("last_session_end"):
        lines += _failure_lines("Last session ended", report["last_session_end"])
    if report.get("next_steps"):
        lines += ["", "Next steps:"] + _step_lines(report["next_steps"])
    if report.get("note"):
        lines += ["", report["note"]]
    lines += ["", report["claim_boundary"]]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Failure guidance: which guide explains a report that is not a success
# ---------------------------------------------------------------------------

_NATIVE = "dexpc.native_receiver."
# Every reason passed to _refuse in this module (tests/test_dex_receiver_guidance.py checks that).
REFUSAL_GUIDES: dict[str, str] = {
    "not_windows": _NATIVE + "not_windows",
    "exe_name": _NATIVE + "exe_not_found",
    "exe_missing": _NATIVE + "exe_not_found",
    "data_dir_invalid": _NATIVE + "data_dir_invalid",
    "restriction_invalid": _NATIVE + "restriction_invalid",
    "yes_required": _NATIVE + "confirmation_required",
    "already_running": _NATIVE + "already_running",
    "port_in_use": _NATIVE + "port_busy",
    "launch_failed": _NATIVE + "launch_failed",
    "process_check_unavailable": _NATIVE + "process_check_unavailable",
    "no_usable_status": _NATIVE + "status_missing",
    "stop_request_blocked": _NATIVE + "stop_request_failed",
    "stop_request_failed": _NATIVE + "stop_request_failed",
}
STATE_GUIDES: dict[str, str] = {"unverified": _NATIVE + "status_missing", "stale": _NATIVE + "status_stale"}


def _recorded_failure_id(block: Mapping[str, Any] | None) -> str | None:
    """Guide of a reason code the receiver recorded; None for none or an orderly end."""
    if not block or block.get("code") in ORDERLY_ENDS:
        return None
    return f"dexpc.receiver.{block['code']}" if block.get("known_code") else _NATIVE + "unknown_reason"


def report_failure_id(report: Mapping[str, Any]) -> str | None:
    """The failure guide for a receiver report that is not a success, or None.

    Reports with exit code 0 can still name one (a receiver that exited without recording a
    shutdown, or a phone attempt the running receiver recorded as failed); the commands then
    print a one-line pointer instead of the full guidance block.
    """
    outcome = report.get("status")
    if outcome == "refused":
        return REFUSAL_GUIDES.get(report.get("reason"), "internal.unclassified")
    if outcome == "failed":
        return _recorded_failure_id(report.get("last_failure")) or _NATIVE + "exited_early"
    if outcome == "start_pending":
        return _NATIVE + "start_timeout"
    if outcome == "stop_pending":
        return _NATIVE + "stop_unconfirmed"
    if outcome in {"nothing_running", "stopped"}:
        unrecorded = report.get("stale") is True or report.get("shutdown_recorded") is False
        return _NATIVE + "exited_unrecorded" if unrecorded else None
    if outcome != "observed":
        return None
    state, failure = report.get("state"), report.get("last_failure")
    if state == "unreadable":
        return _NATIVE + ("status_schema_unsupported" if report.get("status_file") == "unsupported_schema" else "status_unreadable")
    if state in STATE_GUIDES:
        return STATE_GUIDES[state]
    if state == "not_working":
        return _recorded_failure_id(failure) or _NATIVE + "unknown_reason"
    if state == "not_running" and report.get("stale"):
        crashed = failure and (failure["scope"] == "receiver" or failure["code"] == "internal_panic")
        return (_recorded_failure_id(failure) if crashed else None) or _NATIVE + "exited_unrecorded"
    return _recorded_failure_id(failure) if failure and failure.get("current") else None


_RENDERERS = {"plan_only": render_plan, "started": render_start, "failed": render_start, "start_pending": render_start,
              "stopped": render_stop, "nothing_running": render_stop, "stop_pending": render_stop}


def exit_code(report: dict[str, Any]) -> int:
    """0 done or observed normally; 1 refused, failed or unreadable; 2 pending or needs attention."""
    outcome = report["status"]
    if outcome in {"refused", "failed"}:
        return 1
    if outcome in {"start_pending", "stop_pending"}:
        return 2
    if outcome == "observed":
        return {"unreadable": 1, "not_working": 2, "stale": 2, "unverified": 2}.get(report["state"], 0)
    return 0


def _emit(report: dict[str, Any], json_output: bool) -> None:
    code = exit_code(report)
    failure_id = report_failure_id(report)
    context = FailureContext.from_runtime(command=current_command()) if failure_id else None
    if json_output:
        typer.echo(json.dumps(attach_failure(report, failure_id, context) if failure_id else report, indent=2))
    elif report["status"] == "refused":
        typer.echo(render_refused(report))
    elif report["status"] == "observed":
        typer.echo(render_status(report))
    else:
        typer.echo(_RENDERERS[report["status"]](report))
    if failure_id and not json_output:
        if code:
            emit_failure(failure_id, context)
        else:
            emit_pointer(failure_id)
    if code:
        raise typer.Exit(code)


@dex_app.command("receiver-plan")
def receiver_plan_command(json_output: bool = typer.Option(False, "--json")):
    """Explain both PC receiver routes, including the experimental native one; runs nothing."""
    _emit(receiver_plan(), json_output)


@dex_app.command("receiver-status")
def receiver_status_command(data_dir: str | None = typer.Option(None, "--data-dir"), json_output: bool = typer.Option(False, "--json")):
    """Read the native receiver's own status file and say what state it is in; read-only."""
    _emit(status(data_dir), json_output)


@dex_app.command("receiver-start")
def receiver_start_command(exe: str = typer.Option(..., "--exe"), data_dir: str | None = typer.Option(None, "--data-dir"),
                           expect_name: str | None = typer.Option(None, "--expect-name"), expect_id: str | None = typer.Option(None, "--expect-id"),
                           audio: bool = typer.Option(False, "--audio"), yes: bool = typer.Option(False, "--yes"), json_output: bool = typer.Option(False, "--json")):
    """Start a user-built dexdesk.exe detached and wait for it to advertise; advertises this PC over Wi-Fi Direct."""
    _emit(start(exe, data_dir=data_dir, expect_name=expect_name, expect_id=expect_id, audio=audio, yes=yes), json_output)


@dex_app.command("receiver-stop")
def receiver_stop_command(data_dir: str | None = typer.Option(None, "--data-dir"), yes: bool = typer.Option(False, "--yes"), json_output: bool = typer.Option(False, "--json")):
    """Ask the native receiver to stop through its stop.request file; never ends a process by ID."""
    _emit(stop(data_dir, yes=yes), json_output)
