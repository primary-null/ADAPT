"""Bounded Samsung DeX inspection and existing-display mirroring.

This module never starts DeX, creates a virtual display, changes device state,
or chooses a secondary display for the caller.  It accepts only an explicit
ADB serial and an explicit, currently observed non-primary display ID.
"""

from __future__ import annotations

from adapt_adb.progress import operation

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import typer

from adapt_adb.adb import resolve_adb
from adapt_adb.doctor import UnsafeAdbServerError, preflight_adb_server
from adapt_adb.failure_report import FailureContext, attach_failure, classify, emit_failure


SCHEMA_VERSION = 1
MIN_WORKFLOW_API = 33
MAX_RESEARCHED_API = 37
ADB_TIMEOUT_SECONDS = 10
HOST_TIMEOUT_SECONDS = 5
MIN_SCRCPY_VERSION = (2, 0, 0)

_SERIAL_RE = re.compile(r"^[^\x00-\x1f\x7f-\x9f]{1,1024}$")
_DENIED_RE = re.compile(
    r"permission denial|securityexception|security exception|not allowed|"
    r"operation not permitted|access denied",
    re.IGNORECASE,
)
_UNKNOWN_SERVICE_RE = re.compile(
    r"(?:can't|cannot) find service|unknown service|service\s+desktopmode\s+not found",
    re.IGNORECASE,
)
_SCRCPY_VERSION_RE = re.compile(
    r"\bscrcpy\s+v?([0-9]+)\.([0-9]+)(?:\.([0-9]+))?\b", re.IGNORECASE
)
_LOGICAL_SECTION_RE = re.compile(r"(?im)^\s*Logical Displays\s*:[^\r\n]*$")
_LOGICAL_HEADER_RE = re.compile(r"(?m)^\s*Display\s+([0-9]+)\s*:\s*$")
_TOP_LEVEL_SECTION_RE = re.compile(r"(?m)^[A-Za-z][^:\r\n]*:[^\r\n]*$")

# Recognized ``dumpsys desktopmode`` shapes, reported as ``recognized_shape``.
#
# "synthetic_current_state" is the shape the parser was first written against.
# It comes from synthetic test fixtures and has never been seen in real device
# output.  "observed_statemanager_semdesktopmodestate" was observed on ONE
# Samsung phone (Android 13 / API 33) during a real wireless DeX session in
# dual mode, and again with DeX idle.  Nothing else was captured: standalone
# DeX and wired DeX are not recognized, and other One UI versions may print
# something different, which stays "unknown" rather than being guessed at.
RECOGNIZED_SHAPE_SYNTHETIC = "synthetic_current_state"
RECOGNIZED_SHAPE_OBSERVED = "observed_statemanager_semdesktopmodestate"

# Second, separate evidence source, used ONLY when the ``desktopmode`` service
# is absent ("Can't find service"), never when the service answered.  It reads
# the per-display flags that ``parse_logical_displays`` already extracts from
# ``dumpsys display``.  It was observed on ONE Samsung phone on Android 17
# (One UI 9) during ONE live wireless DeX session started from the phone's
# "Wireless DeX" tile to a Miracast-type receiver: the receiving display (id 6)
# carried FLAG_WIRELESS_DEX_DISPLAY, FLAG_EXTERNAL_DEX_HOSTING and
# FLAG_WIFI_DISPLAY, the DeX desktop was drawn directly on it (no separate
# virtual "Desktop" display), and with DeX idle only display 0 was listed.
# NOT observed: wired DeX, standalone DeX, and the Android 16 (One UI 8) tablet
# with DeX running.  On the Android 13 phone the layout differs (the wireless
# sink is not the desktop display), which is why the flag on its own is not
# trusted below.
RECOGNIZED_SHAPE_DISPLAY_FLAGS = "dumpsys_display_flag_wireless_dex_display"
CLASSIFICATION_DISPLAY_FLAGS_EVIDENCE = "active_wireless_dex_display_flag_evidence"
CLASSIFICATION_DISPLAY_FLAGS = "active_wireless_dex_display_flag"
EVIDENCE_SOURCE_DISPLAY_FLAGS = "dumpsys_display_flags"
FLAG_WIRELESS_DEX = "FLAG_WIRELESS_DEX_DISPLAY"
FLAG_EXTERNAL_DEX_HOSTING = "FLAG_EXTERNAL_DEX_HOSTING"
DISPLAY_FLAGS_OBSERVED_ON = (
    "one Samsung phone on Android 17 (One UI 9) and one Samsung tablet on Android 16 (One UI 8), "
    "each during one live wireless DeX session"
)
DISPLAY_FLAGS_NOT_OBSERVED = [
    "wired DeX",
    "standalone DeX",
    "a screen capture of the flagged display",
]
# Classifications that confirm a DeX desktop on an existing non-primary display.
_CONFIRMED_DUAL_CLASSIFICATIONS = frozenset({"active_dual", CLASSIFICATION_DISPLAY_FLAGS})
# A "not found" reply is a line or two.  A reply longer than this is a real
# dump that merely contains the phrase, so it never counts as "service absent".
_ABSENT_SERVICE_MAX_LINES = 3

_SYNTHETIC_STATE_LINE_RE = re.compile(r"(?m)^\s*mCurrentState=DesktopModeState\{")
_STATE_SECTION_RE = re.compile(r"^(\s*)Current ([A-Za-z0-9_]+) state:\s*$")
_SECTION_FLAG_RE = re.compile(r"^\s*(mDualModeEnabled|mStandaloneModeEnabled)=(\S+)\s*$")
_SECTION_DISPLAY_ID_RE = re.compile(r"^\s*mDesktopDisplayId=(\S+)\s*$")
_SECTION_STATE_RE = re.compile(r"^\s*mState=(.*?)\s*$")
_BOOL_VALUE_RE = re.compile(r"(?i)true|false")
_DISPLAY_ID_VALUE_RE = re.compile(r"-?[0-9]{1,9}")
_STATE_BODY_RE = re.compile(r"State\{([^{}]*)\}")
_SEM_STATE_RE = re.compile(r"SemDesktopModeState\(([^()]*)\)")
_DESKTOP_DISPLAY_TOKEN_RE = re.compile(r"(?<!\S)desktopDisplay\.([0-9]{1,9})(?!\S)")
_SAFE_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{1,40}")


class DexError(RuntimeError):
    """A bounded DeX operation could not be completed safely."""

    def __init__(self, kind: str, message: str, *, failure_id: str | None = None):
        super().__init__(message)
        self.kind = kind
        if failure_id is not None:
            # Names the specific failure where one kind covers several situations;
            # failure_report.classify prefers it over the kind map.
            self.failure_id = failure_id


@dataclass(frozen=True)
class ProbeResult:
    status: str
    stdout: str = ""
    reason: str | None = None
    # True only for an unknown-service reply that is just the short "not found"
    # message, i.e. the service really is absent rather than answering with text.
    bare_not_found: bool = False


ProcessRunner = Callable[[list[str], int], subprocess.CompletedProcess[str]]
Preflight = Callable[[Path, list[str]], None]
PopenFactory = Callable[..., Any]
ScrcpyResolver = Callable[[Path | str | None], Path]


def _run_process(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _popen_process(args: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
    return subprocess.Popen(args, **kwargs)


def _validate_serial(serial: str) -> str:
    # ADB treats -s as an opaque argv value.  Spaced mDNS names are valid and
    # must remain one exact element; only edge whitespace/control bytes/flags
    # are rejected.
    if not isinstance(serial, str) or not serial.strip():
        raise DexError("invalid_serial", "A nonblank explicit -s/--serial target is required.")
    if serial != serial.strip() or serial.startswith("-") or not _SERIAL_RE.fullmatch(serial):
        raise DexError("invalid_serial", "The explicit ADB target contains unsupported characters.")
    return serial


def _resolve_adb(adb_path: Path | str | None) -> Path:
    resolved = Path(adb_path) if adb_path is not None else resolve_adb()
    if resolved is None:
        raise DexError("missing_adb", "adb binary not found; run 'adapt setup'.")
    return Path(resolved)


def _probe_adb(
    adb_path: Path,
    serial: str,
    device_args: list[str],
    *,
    runner: ProcessRunner,
) -> ProbeResult:
    argv = [str(adb_path), "-s", serial, *device_args]
    try:
        result = runner(argv, ADB_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise DexError("timeout", "Read-only ADB inspection timed out.") from exc
    except FileNotFoundError as exc:
        raise DexError("missing_adb", "adb binary was not found.", failure_id="adb.exec_failed") from exc
    except OSError as exc:
        raise DexError("adb_error", f"adb could not start: {exc}") from exc

    stdout = result.stdout or ""
    stderr = result.stderr or ""
    combined = "\n".join(part for part in (stdout, stderr) if part).strip()
    if _DENIED_RE.search(combined):
        return ProbeResult("denied", reason="The read-only query was denied.")
    if _UNKNOWN_SERVICE_RE.search(combined):
        lines = [line for line in combined.splitlines() if line.strip()]
        return ProbeResult(
            "unknown_service",
            reason="The desktopmode service was not available by this name.",
            bare_not_found=len(lines) <= _ABSENT_SERVICE_MAX_LINES,
        )
    if result.returncode != 0:
        return ProbeResult("error", reason=f"adb exited with status {result.returncode}.")
    return ProbeResult("observed", stdout=stdout.strip())


def _one_value(text: str, pattern: str, field: str) -> tuple[str | None, str | None]:
    values = re.findall(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
    if len(values) != 1:
        if not values:
            return None, f"recognized field {field} was missing"
        unique = sorted(set(str(value).casefold() for value in values))
        qualifier = "conflicting" if len(unique) > 1 else "duplicated"
        return None, f"recognized field {field} was {qualifier}"
    return str(values[0]), None


def _parse_synthetic_current_state(text: str) -> dict[str, Any]:
    """Parse the original ``mCurrentState=DesktopModeState{...}`` shape.

    Synthetic fixtures can exercise this parser, but never certify a device.
    Missing, repeated, conflicting, or novel fields are intentionally unknown.
    """
    fields: dict[str, str] = {}
    issues: list[str] = []
    patterns = {
        "enabled": r"^\s*mCurrentState=DesktopModeState\{[^\r\n}]*\benabled=([0-9]+)[^\r\n}]*\}",
        "dual": r"^\s*mDualModeEnabled=(true|false)\s*$",
        "standalone": r"^\s*mStandaloneModeEnabled=(true|false)\s*$",
        "display_id": r"^\s*mDesktopDisplayId=(-?[0-9]+)\s*$",
    }
    for field, pattern in patterns.items():
        value, issue = _one_value(text, pattern, field)
        if issue:
            issues.append(issue)
        elif value is not None:
            fields[field] = value.casefold()

    if issues:
        return {
            "status": "unknown",
            "classification": "unknown",
            "desktop_display_id": None,
            "recognized_shape": None,
            "issues": issues,
        }

    enabled = int(fields["enabled"])
    dual = fields["dual"] == "true"
    standalone = fields["standalone"] == "true"
    display_id = int(fields["display_id"])

    if enabled == 4 and dual and not standalone and display_id > 0:
        classification = "active_dual_evidence"
    elif enabled == 4 and standalone and not dual and display_id == 0:
        classification = "active_standalone_evidence"
    elif enabled != 4 and not dual and not standalone and display_id == -1:
        classification = "inactive"
    else:
        return {
            "status": "unknown",
            "classification": "unknown",
            "desktop_display_id": None,
            "recognized_shape": None,
            "issues": ["recognized desktop-mode fields form an inconsistent or unrecognized state"],
        }

    return {
        "status": "observed",
        "classification": classification,
        "desktop_display_id": display_id,
        "recognized_shape": RECOGNIZED_SHAPE_SYNTHETIC,
        "recognized_fields": {
            "enabled": enabled,
            "dual_mode": dual,
            "standalone_mode": standalone,
            "desktop_display_id": display_id,
        },
        "issues": [],
    }


def _unknown_desktop_mode(issues: list[str]) -> dict[str, Any]:
    return {
        "status": "unknown",
        "classification": "unknown",
        "desktop_display_id": None,
        "recognized_shape": None,
        "issues": issues,
    }


def _state_sections(text: str) -> list[tuple[str, list[str]]]:
    """Split out the ``Current <Name> state:`` sections of the observed shape.

    A section holds only the lines indented deeper than its header.  The
    timestamped log history that precedes the sections (it contains
    ``enabled=`` and ``State=State{`` inside log messages), and anything printed
    after a section at the header's indentation or shallower, is never read as
    state.
    """

    def indent(line: str) -> int:
        expanded = line.expandtabs()
        return len(expanded) - len(expanded.lstrip())

    sections: list[tuple[str, list[str]]] = []
    current: list[str] | None = None
    header_indent = 0
    for line in text.splitlines():
        header = _STATE_SECTION_RE.match(line)
        if header:
            current = []
            header_indent = indent(line)
            sections.append((header.group(2), current))
            continue
        if current is None or not line.strip():
            continue
        if indent(line) <= header_indent:
            current = None
            continue
        current.append(line)
    return sections


def _single_state_value(
    name: str,
    occurrences: list[tuple[str, str]],
    valid: re.Pattern[str],
    issues: list[str],
) -> str | None:
    """Return the one value every ``(section, value)`` occurrence agrees on.

    The observed shape prints some fields in more than one section.  Repetition
    with identical values is evidence, not a conflict; a missing, malformed, or
    disagreeing field yields ``None`` and a specific issue.  Echoed values and
    section names are device output, so only validated or charset-limited text
    is repeated in an issue.
    """
    if not occurrences:
        issues.append(f"{name} was missing from the desktop-mode state sections")
        return None
    malformed = [section for section, value in occurrences if not valid.fullmatch(value)]
    if malformed:
        where = ", ".join(sorted(set(malformed)))
        issues.append(f"{name} had an unrecognized value in {where}")
        return None
    if len({value.casefold() for _, value in occurrences}) > 1:
        detail = ", ".join(f"{section}={value}" for section, value in occurrences)
        issues.append(f"{name} conflicted between state sections ({detail})")
        return None
    return occurrences[0][1].casefold()


def _safe_token(value: str, fallback: str) -> str:
    return value if _SAFE_TOKEN_RE.fullmatch(value) else fallback


def _read_state_manager_line(
    state_lines: list[str], issues: list[str]
) -> tuple[str | None, int | None]:
    """Classify the ``StateManager`` ``mState=State{...}`` line.

    Returns ``("active", N)`` for an enabled dual-mode ``SemDesktopModeState``
    naming ``desktopDisplay.N``, ``("inactive", None)`` for a ``State{...}``
    with no desktop-mode or display content, and ``(None, None)`` plus issues
    for anything else.  Standalone-looking and other states are not recognized.
    """
    if not state_lines:
        issues.append("StateManager mState line was missing")
        return None, None
    if len(set(state_lines)) > 1:
        issues.append("StateManager mState was reported more than once with different values")
        return None, None
    match = _STATE_BODY_RE.fullmatch(state_lines[0])
    if not match:
        issues.append("StateManager mState was not a recognized State{...} line")
        return None, None
    body = match.group(1)

    if "SemDesktopModeState" not in body:
        if "DisplayInfo(" in body or "desktopDisplay." in body:
            issues.append(
                "StateManager mState described a display without SemDesktopModeState, "
                "which is not a recognized inactive shape"
            )
            return None, None
        return "inactive", None

    blocks = _SEM_STATE_RE.findall(body)
    if len(blocks) != 1 or body.count("SemDesktopModeState") != 1:
        issues.append(
            "StateManager mState did not contain exactly one well-formed SemDesktopModeState(...)"
        )
        return None, None
    args = [part.strip() for part in blocks[0].split(",")]
    display_types = [part for part in args[1:] if part.startswith("DISPLAY_TYPE_")]
    tokens = sorted(set(_DESKTOP_DISPLAY_TOKEN_RE.findall(body)))
    found: list[str] = []
    if args[0] != "ENABLED":
        found.append(
            f"SemDesktopModeState reported {_safe_token(args[0], 'an unrecognized state')}, not ENABLED"
        )
    if len(display_types) != 1:
        found.append("SemDesktopModeState did not name exactly one DISPLAY_TYPE_ value")
    elif display_types[0] != "DISPLAY_TYPE_DUAL":
        found.append(
            f"SemDesktopModeState display type {_safe_token(display_types[0], 'value')} is not "
            "DISPLAY_TYPE_DUAL; only dual mode is recognized (standalone and wired DeX were not captured)"
        )
    if len(tokens) != 1:
        found.append("StateManager mState did not name exactly one desktopDisplay.N token")
    if found:
        issues.extend(found)
        return None, None
    return "active", int(tokens[0])


def _parse_observed_state_sections(text: str) -> dict[str, Any] | None:
    """Parse the shape observed on one Samsung phone (Android 13 / API 33).

    State comes only from the ``Current <Name> state:`` sections.  Evidence rule:

    * active dual: the ``StateManager`` ``mState=State{...}`` line holds
      ``SemDesktopModeState(ENABLED, ..., DISPLAY_TYPE_DUAL)`` and a
      ``desktopDisplay.N`` token; ``mDesktopDisplayId`` is that same N and
      positive; every ``mDualModeEnabled`` line says true and every
      ``mStandaloneModeEnabled`` line says false.
    * inactive: ``mState=State{...}`` holds no ``SemDesktopModeState`` and no
      display content; ``mDesktopDisplayId=-1``; every dual and standalone flag
      says false.
    * anything else (disagreeing flags, a mismatched id, an unrecognized
      ``mState``, a standalone-looking state) is unknown, with a specific issue.

    Evidence limits: this was observed on one phone, with a real wireless DeX
    session in dual mode and with DeX idle.  Standalone and wired DeX were not
    captured, and other One UI versions may print something else; those yield
    ``unknown``.  Returns ``None`` when the text has no state sections at all.
    """
    sections = _state_sections(text)
    if not sections:
        return None

    flags: dict[str, list[tuple[str, str]]] = {"mDualModeEnabled": [], "mStandaloneModeEnabled": []}
    display_ids: list[tuple[str, str]] = []
    state_lines: list[str] = []
    for name, lines in sections:
        for line in lines:
            flag = _SECTION_FLAG_RE.match(line)
            if flag:
                flags[flag.group(1)].append((name, flag.group(2)))
                continue
            display_id = _SECTION_DISPLAY_ID_RE.match(line)
            if display_id:
                display_ids.append((name, display_id.group(1)))
                continue
            # mState is a generic name; only the StateManager section's is read.
            state = _SECTION_STATE_RE.match(line)
            if state and name == "StateManager":
                state_lines.append(state.group(1))

    issues: list[str] = []
    dual_text = _single_state_value("mDualModeEnabled", flags["mDualModeEnabled"], _BOOL_VALUE_RE, issues)
    standalone_text = _single_state_value(
        "mStandaloneModeEnabled", flags["mStandaloneModeEnabled"], _BOOL_VALUE_RE, issues
    )
    id_text = _single_state_value("mDesktopDisplayId", display_ids, _DISPLAY_ID_VALUE_RE, issues)
    kind, token_id = _read_state_manager_line(state_lines, issues)
    if issues:
        return _unknown_desktop_mode(issues)

    dual = dual_text == "true"
    standalone = standalone_text == "true"
    display_id = int(str(id_text))
    conflicts: list[str] = []
    if kind == "active":
        if token_id != display_id:
            conflicts.append(
                f"desktopDisplay.{token_id} in StateManager mState does not match mDesktopDisplayId={display_id}"
            )
        elif display_id <= 0:
            conflicts.append(
                f"mState names desktop display {display_id}, which is not a positive non-primary display ID"
            )
        if not dual:
            conflicts.append("mState reports an enabled dual-mode desktop but mDualModeEnabled is false")
        if standalone:
            conflicts.append("mState reports an enabled dual-mode desktop but mStandaloneModeEnabled is true")
        classification = "active_dual_evidence"
    else:
        if display_id != -1:
            conflicts.append(f"mState shows no desktop mode but mDesktopDisplayId is {display_id}")
        if dual:
            conflicts.append("mState shows no desktop mode but mDualModeEnabled is true")
        if standalone:
            conflicts.append("mState shows no desktop mode but mStandaloneModeEnabled is true")
        classification = "inactive"
    if conflicts:
        return _unknown_desktop_mode(conflicts)

    return {
        "status": "observed",
        "classification": classification,
        "desktop_display_id": display_id,
        "recognized_shape": RECOGNIZED_SHAPE_OBSERVED,
        "recognized_fields": {
            "dual_mode": dual,
            "standalone_mode": standalone,
            "desktop_display_id": display_id,
            "sem_desktop_mode_state": "ENABLED" if kind == "active" else None,
            "sem_display_type": "DISPLAY_TYPE_DUAL" if kind == "active" else None,
        },
        "issues": [],
    }


def parse_desktop_mode(text: str) -> dict[str, Any]:
    """Parse ``dumpsys desktopmode`` into a conservative, recognized shape.

    Two shapes are recognized, reported as ``recognized_shape``: the original
    synthetic ``mCurrentState=DesktopModeState{...}`` shape, and the
    ``Current <Name> state:`` sections observed on one Samsung phone (see
    ``_parse_observed_state_sections`` for the evidence rule and its limits).
    Fixtures can exercise this parser but never certify a device.  Missing,
    repeated-with-different-values, conflicting, or novel fields are
    intentionally unknown.
    """
    if not _SYNTHETIC_STATE_LINE_RE.search(text):
        observed = _parse_observed_state_sections(text)
        if observed is not None:
            return observed
    return _parse_synthetic_current_state(text)


def parse_logical_displays(text: str) -> dict[str, Any]:
    """Parse only IDs in the ``Logical Displays`` section of dumpsys.

    Physical device mode IDs and supported-mode IDs are deliberately ignored.
    """
    section_match = _LOGICAL_SECTION_RE.search(text or "")
    if not section_match:
        return {
            "status": "unknown",
            "items": [],
            "reason": "Unrecognized dumpsys display format: Logical Displays section missing.",
        }
    section = text[section_match.end() :]
    # A dumpsys report often has later sections containing their own display
    # numbers.  Only the indented blocks before the next top-level heading are
    # part of Logical Displays.
    next_section = _TOP_LEVEL_SECTION_RE.search(section)
    if next_section:
        section = section[: next_section.start()]
    headers = list(_LOGICAL_HEADER_RE.finditer(section))
    if not headers:
        return {
            "status": "unknown",
            "items": [],
            "reason": "Unrecognized dumpsys display format: no logical display blocks found.",
        }

    items: list[dict[str, Any]] = []
    seen: set[int] = set()
    for index, header in enumerate(headers):
        block_end = headers[index + 1].start() if index + 1 < len(headers) else len(section)
        block = section[header.end() : block_end]
        header_id = int(header.group(1))
        raw_ids = re.findall(r"(?m)^\s*mDisplayId\s*=\s*(-?[0-9]+)\s*$", block)
        if len(raw_ids) != 1 or int(raw_ids[0]) != header_id or header_id in seen:
            return {
                "status": "unknown",
                "items": [],
                "reason": "Logical display IDs were missing, duplicated, conflicting, or repeated.",
            }
        seen.add(header_id)

        name_match = re.search(r'DisplayInfo\{"([^"]*)"', block)
        resolution_match = re.search(r"\breal\s+([0-9]+)\s*x\s*([0-9]+)\b", block)
        density_match = re.search(r"\bdensity\s+([0-9]+)\b", block)
        flags = sorted(set(re.findall(r"\bFLAG_[A-Z0-9_]+\b", block)))
        items.append(
            {
                "id": header_id,
                "primary": header_id == 0,
                "name": name_match.group(1) if name_match else None,
                "resolution": (
                    {"width": int(resolution_match.group(1)), "height": int(resolution_match.group(2))}
                    if resolution_match
                    else None
                ),
                "density_dpi": int(density_match.group(1)) if density_match else None,
                "flags": flags,
                "metadata_status": "observed" if name_match else "partial",
            }
        )

    return {"status": "observed", "items": items, "reason": None}


def _display_flag_evidence(displays: dict[str, Any]) -> dict[str, Any]:
    """Judge DeX from logical-display flags; call only when ``desktopmode`` is absent.

    Evidence rule (see ``RECOGNIZED_SHAPE_DISPLAY_FLAGS`` for what was observed):

    * matched: exactly one logical display carries FLAG_WIRELESS_DEX_DISPLAY, it
      is not display 0, it ALSO carries FLAG_EXTERNAL_DEX_HOSTING (as the observed
      Android 17 display did), and no other display carries either DeX flag.
      The desktop is that display.
    * anything else is ``unknown`` with a specific issue: more than one
      wireless-DeX display, the flag on display 0, the wireless flag without the
      hosting flag (on Android 13 the wireless sink was not the desktop display,
      so the desktop cannot be located), another display also hosting DeX, or
      FLAG_EXTERNAL_DEX_HOSTING without FLAG_WIRELESS_DEX_DISPLAY (possibly wired
      DeX, never observed).
    * no flagged display, or an unreadable display list: ``unknown``.  This never
      says DeX is inactive; it says no active DeX display was observed.

    Only flags already extracted by ``parse_logical_displays`` are used.  The
    manufacturer gate is the caller's.  Returns outcome, display_id, issues,
    reason and the displays carrying a DeX flag.
    """
    absent = "The desktopmode service is absent on this firmware"
    if displays.get("status") != "observed":
        reason = displays.get("reason") or "display service could not be inspected"
        return {
            "outcome": "displays_unknown",
            "display_id": None,
            "issues": [f"Display flags could not be read: {reason}"],
            "reason": (
                f"{absent} and 'dumpsys display' could not be read, so no display flags "
                "were evaluated and no active DeX display was observed."
            ),
            "flagged_displays": [],
        }

    carrying: list[dict[str, Any]] = []
    wireless: list[int] = []
    hosting: list[int] = []
    for item in displays["items"]:
        flags = item.get("flags") or []
        dex_flags = [flag for flag in (FLAG_WIRELESS_DEX, FLAG_EXTERNAL_DEX_HOSTING) if flag in flags]
        if dex_flags:
            carrying.append({"id": item["id"], "flags": dex_flags})
        if FLAG_WIRELESS_DEX in flags:
            wireless.append(item["id"])
        if FLAG_EXTERNAL_DEX_HOSTING in flags:
            hosting.append(item["id"])

    def unknown(outcome: str, issue: str) -> dict[str, Any]:
        return {
            "outcome": outcome,
            "display_id": None,
            "issues": [issue],
            "reason": f"{absent}, and the display flags do not identify one DeX display: {issue}",
            "flagged_displays": carrying,
        }

    if not wireless and not hosting:
        return {
            "outcome": "no_flagged_display",
            "display_id": None,
            "issues": [f"No logical display carries {FLAG_WIRELESS_DEX} or {FLAG_EXTERNAL_DEX_HOSTING}."],
            "reason": (
                f"{absent} and no logical display carries {FLAG_WIRELESS_DEX} or "
                f"{FLAG_EXTERNAL_DEX_HOSTING}, so no active DeX display was observed. "
                "This does not show that DeX is inactive."
            ),
            "flagged_displays": [],
        }
    if not wireless:
        ids = ", ".join(str(display_id) for display_id in hosting)
        return unknown(
            "external_hosting_only",
            f"{FLAG_EXTERNAL_DEX_HOSTING} is on logical display {ids} without "
            f"{FLAG_WIRELESS_DEX}; this combination (possibly wired DeX) has not been observed.",
        )
    if len(wireless) > 1:
        ids = ", ".join(str(display_id) for display_id in wireless)
        return unknown(
            "multiple_flagged_displays",
            f"More than one logical display carries {FLAG_WIRELESS_DEX} (displays {ids}).",
        )
    candidate = wireless[0]
    if candidate == 0:
        return unknown(
            "primary_display_flagged",
            f"{FLAG_WIRELESS_DEX} is on logical display 0, the built-in screen, not on an external display.",
        )
    if candidate not in hosting:
        return unknown(
            "wireless_flag_without_hosting",
            f"Logical display {candidate} carries {FLAG_WIRELESS_DEX} without {FLAG_EXTERNAL_DEX_HOSTING}; "
            "a wireless sink display is not always the display the desktop is drawn on.",
        )
    others = [display_id for display_id in hosting if display_id != candidate]
    if others:
        ids = ", ".join(str(display_id) for display_id in others)
        return unknown(
            "other_hosting_display",
            f"Logical display {ids} also carries {FLAG_EXTERNAL_DEX_HOSTING}, so the DeX display is ambiguous.",
        )
    return {
        "outcome": "matched",
        "display_id": candidate,
        "issues": [],
        "reason": (
            f"{absent}, so DeX was confirmed from display flags instead: logical display "
            f"{candidate} carries {FLAG_WIRELESS_DEX} and {FLAG_EXTERNAL_DEX_HOSTING} in 'dumpsys display'. "
            f"This evidence source was observed on {DISPLAY_FLAGS_OBSERVED_ON}; "
            f"{', '.join(DISPLAY_FLAGS_NOT_OBSERVED[:-1])} and {DISPLAY_FLAGS_NOT_OBSERVED[-1]} were not observed."
        ),
        "flagged_displays": carrying,
    }


def _platform_status(sdk_text: str | None) -> tuple[int | None, str]:
    if sdk_text is None or not re.fullmatch(r"[0-9]{1,3}", sdk_text.strip()):
        return None, "unknown"
    api = int(sdk_text)
    if api < MIN_WORKFLOW_API:
        return api, "below_minimum"
    if api > MAX_RESEARCHED_API:
        return api, "unverified_future"
    return api, "eligible"


@operation("Inspecting DeX and displays")
def inspect_device(
    serial: str,
    *,
    adb_path: Path | str | None = None,
    runner: ProcessRunner | None = None,
    preflight: Preflight | None = None,
) -> dict[str, Any]:
    """Run one bounded, read-only inspection against one exact target."""
    target = _validate_serial(serial)
    adb = _resolve_adb(adb_path)
    run = runner or _run_process
    gate = preflight or preflight_adb_server
    gate_args = ["-s", target, "shell", "getprop", "ro.build.version.sdk"]
    try:
        gate(adb, gate_args)
    except UnsafeAdbServerError as exc:
        raise DexError(
            "unsafe_adb_server", "ADB server ownership/version could not be verified."
        ) from exc

    property_keys = {
        "manufacturer": "ro.product.manufacturer",
        "model": "ro.product.model",
        "sdk": "ro.build.version.sdk",
    }
    properties: dict[str, str | None] = {}
    property_status: dict[str, str] = {}
    for label, key in property_keys.items():
        probe = _probe_adb(adb, target, ["shell", "getprop", key], runner=run)
        properties[label] = probe.stdout if probe.status == "observed" and probe.stdout else None
        property_status[label] = probe.status

    if all(status == "error" for status in property_status.values()):
        # Every identity query failed at the adb level (no answer, not even a
        # denial), so nothing about the device was observed.  Stop before the
        # dumpsys probes and do not report an "unknown" device as non-Samsung.
        raise DexError(
            "device_unreachable",
            "The device did not answer any read-only query; it may be disconnected, "
            "offline, unauthorized, or the serial may be wrong. "
            "Run 'adapt devices' to check its state.",
        )

    desktop_probe = _probe_adb(adb, target, ["shell", "dumpsys", "desktopmode"], runner=run)
    if desktop_probe.status == "observed":
        desktop = parse_desktop_mode(desktop_probe.stdout)
        desktop["service_status"] = "observed"
    else:
        desktop = {
            "status": "unknown",
            "service_status": "unknown",
            "classification": "unknown",
            "desktop_display_id": None,
            "recognized_shape": None,
            "issues": [desktop_probe.reason or "desktopmode service could not be inspected"],
        }

    display_probe = _probe_adb(adb, target, ["shell", "dumpsys", "display"], runner=run)
    if display_probe.status == "observed":
        displays = parse_logical_displays(display_probe.stdout)
    else:
        displays = {
            "status": "unknown",
            "items": [],
            "reason": display_probe.reason or "display service could not be inspected",
        }

    api, workflow_status = _platform_status(properties["sdk"])
    is_samsung = bool(
        properties["manufacturer"]
        and properties["manufacturer"].strip().casefold() == "samsung"
    )
    # Display-flag evidence applies only when the desktopmode service is absent
    # (a bare "not found" reply) and the manufacturer was read as Samsung.  A
    # service that answered, denied, errored or printed anything else never
    # reaches it, so the service-based results above are final.
    flag_evidence: dict[str, Any] | None = None
    if desktop_probe.status == "unknown_service" and desktop_probe.bare_not_found and is_samsung:
        flag_evidence = _display_flag_evidence(displays)
        if flag_evidence["outcome"] == "matched":
            desktop = {
                **desktop,
                "status": "observed",
                "classification": CLASSIFICATION_DISPLAY_FLAGS_EVIDENCE,
                "desktop_display_id": flag_evidence["display_id"],
                "recognized_shape": RECOGNIZED_SHAPE_DISPLAY_FLAGS,
                "recognized_fields": {
                    "desktop_display_id": flag_evidence["display_id"],
                    "wireless_dex_display_flag": True,
                    "external_dex_hosting_flag": True,
                },
                "issues": [],
            }
        else:
            desktop = {**desktop, "issues": [*desktop["issues"], *flag_evidence["issues"]]}
        desktop["display_flag_evidence"] = {
            "source": EVIDENCE_SOURCE_DISPLAY_FLAGS,
            "desktopmode_service": "absent",
            "flags_read_from": "dumpsys display (Logical Displays section)",
            "outcome": flag_evidence["outcome"],
            "flagged_displays": flag_evidence["flagged_displays"],
            "observed_on": DISPLAY_FLAGS_OBSERVED_ON,
            "not_observed": list(DISPLAY_FLAGS_NOT_OBSERVED),
        }

    classification = desktop["classification"]
    confirmed_display_id: int | None = None
    confirmation_reason: str
    observed_ids = {item["id"] for item in displays["items"]} if displays["status"] == "observed" else set()

    if not is_samsung:
        classification = "unknown"
        if properties["manufacturer"] is None:
            confirmation_reason = "The device manufacturer could not be read, so Samsung DeX cannot be confirmed."
        else:
            confirmation_reason = "Samsung DeX cannot be confirmed on a non-Samsung manufacturer identity."
    elif flag_evidence is not None:
        candidate_id = desktop["desktop_display_id"]
        if flag_evidence["outcome"] == "matched" and candidate_id in observed_ids and candidate_id != 0:
            classification = CLASSIFICATION_DISPLAY_FLAGS
            confirmed_display_id = candidate_id
        else:
            classification = "unknown"
        confirmation_reason = flag_evidence["reason"]
    elif classification == "active_dual_evidence":
        candidate_id = desktop["desktop_display_id"]
        if candidate_id in observed_ids and candidate_id != 0:
            classification = "active_dual"
            confirmed_display_id = candidate_id
            confirmation_reason = "Recognized active dual-mode state matches an existing non-primary logical display."
        else:
            classification = "unknown"
            confirmation_reason = "Desktop-mode display ID did not match an existing non-primary logical display."
    elif classification == "active_standalone_evidence":
        if 0 in observed_ids:
            classification = "active_standalone"
            confirmed_display_id = 0
            confirmation_reason = "Recognized standalone state uses display 0; it is not a distinct dual-mirror surface."
        else:
            classification = "unknown"
            confirmation_reason = "Standalone state referenced missing logical display 0."
    elif classification == "inactive":
        confirmation_reason = "Recognized desktop-mode fields report DeX inactive."
    else:
        confirmation_reason = "Runtime DeX state is unknown; secondary displays alone are not DeX proof."

    return {
        "schema_version": SCHEMA_VERSION,
        "command": "dex inspect",
        "serial": target,
        "device": {
            "manufacturer": properties["manufacturer"],
            "model": properties["model"],
            "api_level": api,
            "property_status": property_status,
        },
        "workflow": {
            "minimum_api": MIN_WORKFLOW_API,
            "maximum_researched_api": MAX_RESEARCHED_API,
            "status": workflow_status,
            "generic_secondary_display_control_minimum_api": 29,
        },
        "dex": {
            **desktop,
            "classification": classification,
            "confirmed": classification in _CONFIRMED_DUAL_CLASSIFICATIONS | {"active_standalone"},
            "confirmed_display_id": confirmed_display_id,
            "confirmation_reason": confirmation_reason,
        },
        "displays": displays,
        "mutations": [],
        "claim_boundary": (
            "This read-only snapshot is runtime evidence only for the queried state. "
            "It is not model-wide certification, and an external display alone does not prove Samsung DeX."
        ),
    }


def list_displays(
    serial: str,
    *,
    adb_path: Path | str | None = None,
    runner: ProcessRunner | None = None,
    preflight: Preflight | None = None,
) -> dict[str, Any]:
    report = inspect_device(
        serial, adb_path=adb_path, runner=runner, preflight=preflight
    )
    listing = {
        "schema_version": SCHEMA_VERSION,
        "command": "dex list-displays",
        "serial": report["serial"],
        "status": report["displays"]["status"],
        "displays": report["displays"]["items"],
        "reason": report["displays"].get("reason"),
        "confirmed_dex_display_id": (
            report["dex"]["confirmed_display_id"]
            if report["dex"]["classification"] in _CONFIRMED_DUAL_CLASSIFICATIONS
            else None
        ),
        "note": "Display IDs are runtime values. Select an observed nonzero ID explicitly.",
    }
    if report["dex"]["classification"] == CLASSIFICATION_DISPLAY_FLAGS:
        # Only present when the confirmation came from display flags (the
        # desktopmode service being absent), so the source stays visible.
        listing["confirmed_dex_evidence_source"] = EVIDENCE_SOURCE_DISPLAY_FLAGS
    return listing


def _build_plan(
    report: dict[str, Any],
    dex_display: int,
    *,
    scrcpy_executable: str,
    view_only: bool,
) -> dict[str, Any]:
    if report["workflow"]["status"] == "below_minimum":
        raise DexError(
            "unsupported_api",
            "The bounded DeX dual-mirror workflow requires Android 13 / API 33 or newer; "
            "scrcpy documents generic secondary-display control separately from Android 10.",
        )
    if dex_display == 0:
        raise DexError(
            "primary_duplicate",
            "Display 0 is the primary surface. Standalone DeX on display 0 cannot provide two distinct phone and DeX surfaces.",
        )
    if dex_display < 0:
        raise DexError("invalid_display", "--dex-display must be an observed nonnegative logical display ID.")
    if report["displays"]["status"] != "observed":
        raise DexError(
            "unknown_display_format",
            f"Display discovery is unknown: {report['displays'].get('reason') or 'inspect the current dumpsys format'}",
        )
    items = report["displays"]["items"]
    ids = [item["id"] for item in items]
    if len(ids) != len(set(ids)):
        raise DexError("duplicate_display", "Display discovery returned duplicate logical display IDs.")
    if 0 not in ids:
        raise DexError("missing_primary", "Logical display 0 was not observed; refusing an incomplete dual-mirror plan.")
    if dex_display not in ids:
        raise DexError(
            "missing_display",
            f"Logical display {dex_display} is not currently present; run 'adapt dex list-displays -s SERIAL'.",
        )

    common = ["-s", report["serial"]]
    phone_argv = [scrcpy_executable, *common, "--display-id=0", "--no-audio"]
    desktop_argv = [scrcpy_executable, *common, f"--display-id={dex_display}", "--no-audio"]
    if view_only:
        phone_argv.append("--no-control")
        desktop_argv.append("--no-control")

    # "Dual mirror" is the phone screen (display 0) plus the display the DeX
    # desktop is drawn on.  A display confirmed from the desktopmode service and
    # one confirmed from display flags are both exactly that: an existing,
    # distinct, non-primary display (0 is always refused above), so both are
    # accepted the same way.  The flag source is labelled separately and its
    # observation limits are repeated in the plan notes.
    confirmed = (
        report["dex"]["classification"] in _CONFIRMED_DUAL_CLASSIFICATIONS
        and report["dex"]["confirmed_display_id"] == dex_display
    )
    from_flags = confirmed and report["dex"]["classification"] == CLASSIFICATION_DISPLAY_FLAGS
    phone_argv.extend(["--window-title", "ADAPT Phone / display 0"])
    desktop_argv.extend(["--window-title", f"ADAPT {'DeX' if confirmed else 'External'} / display {dex_display}"])
    selected = next(item for item in items if item["id"] == dex_display)
    if from_flags:
        target_classification = "confirmed_active_samsung_dex_display_flag"
        confirmation_note = (
            f"The desktopmode service is absent on this firmware; the {FLAG_WIRELESS_DEX} and "
            f"{FLAG_EXTERNAL_DEX_HOSTING} flags in 'dumpsys display' identify the selected display as the DeX "
            f"desktop. That evidence was observed on {DISPLAY_FLAGS_OBSERVED_ON}; "
            "scrcpy capturing that display on that firmware was not observed, so launch is not a health claim."
        )
    elif confirmed:
        target_classification = "confirmed_active_samsung_dex"
        confirmation_note = "Runtime evidence confirms the selected existing display is the active DeX dual-mode display."
    else:
        target_classification = "external_display_candidate_unconfirmed"
        confirmation_note = (
            "The selected display is only an existing-display candidate; inspect must positively confirm active DeX before launch."
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "command": "dex plan-dual-mirror",
        "serial": report["serial"],
        "selected_display": selected,
        "target_classification": target_classification,
        "runtime_dex_confirmed": confirmed,
        "workflow_verification": report["workflow"]["status"],
        "view_only": view_only,
        "instances": [
            {"surface": "phone_primary", "display_id": 0, "argv": phone_argv},
            {"surface": "selected_existing_display", "display_id": dex_display, "argv": desktop_argv},
        ],
        "notes": [
            "Both instances disable audio as a conservative duplicate-capture policy, not as mandatory crash prevention.",
            "No fixed ports are assigned; scrcpy manages concurrent session transport.",
            "No virtual display is created and this plan does not launch a process.",
            confirmation_note,
        ],
    }


def plan_dual_mirror(
    serial: str,
    dex_display: int,
    *,
    adb_path: Path | str | None = None,
    runner: ProcessRunner | None = None,
    preflight: Preflight | None = None,
    scrcpy_executable: str = "scrcpy",
) -> dict[str, Any]:
    report = inspect_device(
        serial, adb_path=adb_path, runner=runner, preflight=preflight
    )
    return _build_plan(
        report,
        dex_display,
        scrcpy_executable=scrcpy_executable,
        view_only=False,
    )


def resolve_scrcpy(explicit: Path | str | None = None) -> Path:
    """Resolve an explicit scrcpy client or one from PATH; never install it."""
    if explicit is not None:
        candidate = Path(explicit).expanduser()
        if candidate.is_file():
            return candidate.resolve()
        located = shutil.which(str(explicit))
        if located:
            return Path(located).resolve()
        raise DexError("missing_scrcpy", f"scrcpy was not found at {explicit!s}.")
    from adapt_adb.scrcpy_install import resolve_scrcpy_binary
    located = resolve_scrcpy_binary()
    if not located:
        raise DexError("missing_scrcpy", "scrcpy was not found on PATH or in the managed installation; use tools install scrcpy or pass --scrcpy PATH.")
    return Path(located).resolve()


def _check_scrcpy_version(scrcpy: Path, runner: ProcessRunner) -> str:
    try:
        result = runner([str(scrcpy), "--version"], HOST_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise DexError("scrcpy_version", "scrcpy version check timed out.") from exc
    except OSError as exc:
        raise DexError("scrcpy_version", f"scrcpy version check could not start: {exc}") from exc
    output = "\n".join(part for part in (result.stdout or "", result.stderr or "") if part)
    match = _SCRCPY_VERSION_RE.search(output) if result.returncode == 0 else None
    if not match:
        raise DexError("scrcpy_version", "scrcpy version could not be verified.")
    parsed = tuple(int(part or 0) for part in match.groups())
    if parsed < MIN_SCRCPY_VERSION:
        raise DexError(
            "scrcpy_version",
            "This launch shape requires scrcpy 2.0+ for the explicit --no-audio policy.",
            failure_id="scrcpy.too_old",
        )
    return ".".join(str(part) for part in parsed)


def _stop_owned_process(process: Any) -> None:
    """Best-effort cleanup for a process created by this invocation only."""
    try:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    except (OSError, AttributeError, subprocess.SubprocessError):
        pass


def mirror_dual(
    serial: str,
    dex_display: int,
    *,
    yes: bool = False,
    view_only: bool = False,
    scrcpy_path: Path | str | None = None,
    adb_path: Path | str | None = None,
    runner: ProcessRunner | None = None,
    preflight: Preflight | None = None,
    popen_factory: PopenFactory | None = None,
    scrcpy_resolver: ScrcpyResolver | None = None,
) -> dict[str, Any]:
    """Launch two owned scrcpy processes only after positive matching evidence."""
    if not yes:
        raise DexError(
            "confirmation_required",
            "Mirror launch requires explicit --yes. Use plan-dual-mirror for a side-effect-free plan.",
        )

    run = runner or _run_process
    report = inspect_device(
        serial, adb_path=adb_path, runner=run, preflight=preflight
    )
    adb = _resolve_adb(adb_path)
    resolver = scrcpy_resolver or resolve_scrcpy
    scrcpy = resolver(scrcpy_path)
    plan = _build_plan(
        report,
        dex_display,
        scrcpy_executable=str(scrcpy),
        view_only=view_only,
    )
    if report["workflow"]["status"] != "eligible":
        raise DexError(
            "unverified_platform",
            "Launch is limited to the researched Android 13-17 / API 33-37 range; inspect and plan remain available.",
        )
    if not plan["runtime_dex_confirmed"]:
        raise DexError(
            "dex_unconfirmed",
            "Active Samsung DeX was not positively confirmed on the selected display; run inspect and plan-dual-mirror.",
        )
    version = _check_scrcpy_version(scrcpy, run)

    env = dict(os.environ)
    env["ADB"] = str(adb)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    start = popen_factory or _popen_process
    started: list[Any] = []
    try:
        for instance in plan["instances"]:
            process = start(
                instance["argv"],
                env=env,
                creationflags=creationflags,
                shell=False,
            )
            started.append(process)
    except (OSError, subprocess.SubprocessError) as exc:
        for process in reversed(started):
            _stop_owned_process(process)
        raise DexError(
            "launch_failed",
            f"scrcpy launch failed after starting {len(started)} owned process(es); only those processes were cleaned up.",
        ) from exc

    return {
        **plan,
        "command": "dex mirror",
        "status": "processes_started",
        "launched_processes": len(started),
        "process_ids": [getattr(process, "pid", None) for process in started],
        "scrcpy_version": version,
        "adb_override": str(adb),
        "healthy_streaming": "not_verified",
        "health_note": "Process creation succeeded; this command does not claim that video or input became healthy.",
    }


def _render_inspection(report: dict[str, Any]) -> str:
    device = report["device"]
    dex = report["dex"]
    lines = [
        f"{device['manufacturer'] or 'unknown'} {device['model'] or 'unknown'} (API {device['api_level'] or 'unknown'})",
        f"Workflow: {report['workflow']['status']}",
        f"DeX state: {dex['classification']}",
        dex["confirmation_reason"],
        f"Logical displays: {', '.join(str(item['id']) for item in report['displays']['items']) or 'unknown'}",
        report["claim_boundary"],
    ]
    return "\n".join(lines)


def _render_displays(report: dict[str, Any]) -> str:
    if report["status"] != "observed":
        return f"Display discovery: UNKNOWN - {report['reason']}"
    lines = ["ID  Primary  Resolution  Density  Name"]
    for item in report["displays"]:
        resolution = item["resolution"]
        size = f"{resolution['width']}x{resolution['height']}" if resolution else "unknown"
        label = (
            "confirmed active DeX, from display flags"
            if report.get("confirmed_dex_evidence_source") == EVIDENCE_SOURCE_DISPLAY_FLAGS
            else "confirmed active DeX"
        )
        confirmed = f" [{label}]" if item["id"] == report["confirmed_dex_display_id"] else ""
        lines.append(
            f"{item['id']:<3} {'yes' if item['primary'] else 'no':<8} {size:<11} "
            f"{str(item['density_dpi'] or 'unknown'):<7} {item['name'] or 'unknown'}{confirmed}"
        )
    lines.append(report["note"])
    return "\n".join(lines)


def _render_plan(plan: dict[str, Any]) -> str:
    lines = [
        f"Target: {plan['target_classification']}",
        f"Workflow verification: {plan['workflow_verification']}",
    ]
    for instance in plan["instances"]:
        lines.append(f"{instance['surface']}: {json.dumps(instance['argv'])}")
    lines.extend(plan["notes"])
    return "\n".join(lines)


def _emit_error(exc: DexError, *, json_output: bool, command: str = "dex") -> None:
    # The first line and the JSON keys are unchanged; failure guidance is added after
    # them (stderr in human mode, an additive `failure` key in JSON mode).
    failure_id = classify(exc, area="dex")
    context = FailureContext.from_runtime(command=command)
    if json_output:
        payload = {"ok": False, "error": exc.kind, "message": str(exc)}
        typer.echo(json.dumps(attach_failure(payload, failure_id, context), sort_keys=True))
    else:
        typer.echo(f"Error: {exc}", err=True)
        emit_failure(failure_id, context)


dex_app = typer.Typer(
    name="dex",
    help="Inspect Samsung DeX displays and explicitly launch selected phone/desktop mirrors.",
    no_args_is_help=True,
)


@dex_app.command("inspect")
def inspect_command(
    serial: str = typer.Option(..., "--serial", "-s", help="Explicit complete ADB target."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Read identity, desktop-mode state, and logical displays."""
    try:
        report = inspect_device(serial)
    except DexError as exc:
        _emit_error(exc, json_output=json_output, command="dex inspect")
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(report, sort_keys=True) if json_output else _render_inspection(report))


@dex_app.command("list-displays")
def list_displays_command(
    serial: str = typer.Option(..., "--serial", "-s", help="Explicit complete ADB target."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List currently observed logical display IDs and metadata."""
    try:
        report = list_displays(serial)
    except DexError as exc:
        _emit_error(exc, json_output=json_output, command="dex list-displays")
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(report, sort_keys=True) if json_output else _render_displays(report))


@dex_app.command("plan-dual-mirror")
def plan_dual_mirror_command(
    serial: str = typer.Option(..., "--serial", "-s", help="Explicit complete ADB target."),
    dex_display: int = typer.Option(..., "--dex-display", help="Existing nonzero logical display ID."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Return two exact scrcpy argv arrays without launching either."""
    try:
        plan = plan_dual_mirror(serial, dex_display)
    except DexError as exc:
        _emit_error(exc, json_output=json_output, command="dex plan-dual-mirror")
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(plan, sort_keys=True) if json_output else _render_plan(plan))


@dex_app.command("mirror")
def mirror_command(
    serial: str = typer.Option(..., "--serial", "-s", help="Explicit complete ADB target."),
    dex_display: int = typer.Option(..., "--dex-display", help="Confirmed active DeX logical display ID."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Explicitly approve launching two scrcpy windows."),
    view_only: bool = typer.Option(False, "--view-only", help="Disable control on both windows."),
    scrcpy: Path | None = typer.Option(None, "--scrcpy", help="Explicit scrcpy executable path."),
) -> None:
    """Launch phone and confirmed active dual-mode DeX mirrors."""
    try:
        result = mirror_dual(
            serial,
            dex_display,
            yes=yes,
            view_only=view_only,
            scrcpy_path=scrcpy,
        )
    except DexError as exc:
        _emit_error(exc, json_output=False, command="dex mirror")
        raise typer.Exit(1) from exc
    typer.echo(
        f"Started {result['launched_processes']} scrcpy processes; healthy streaming is {result['healthy_streaming']}."
    )
    typer.echo(result["health_note"])


__all__ = [
    "DexError",
    "MAX_RESEARCHED_API",
    "MIN_WORKFLOW_API",
    "dex_app",
    "inspect_device",
    "list_displays",
    "mirror_dual",
    "parse_desktop_mode",
    "parse_logical_displays",
    "plan_dual_mirror",
    "resolve_scrcpy",
]
