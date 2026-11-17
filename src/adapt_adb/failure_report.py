"""Failure guidance runtime: tagging, classification, context, rendering and emitting.

Guidance is output only: it never changes an exit code, never runs a step, and
never starts a device, network or daemon probe. Context comes from facts the
caller already holds plus ``sys.platform`` and the *presence* of a few
environment variables. Only ``failure_guide`` and ``console._safe`` are imported
at module level; everything else is imported lazily.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field, fields
from typing import Any, Callable, Literal, Mapping, TypeVar

from adapt_adb.console import _safe
from adapt_adb.failure_guide import (
    GUIDES,
    NAMESPACES,
    RENAMED,
    TEMPLATE_FIELDS,
    Cause,
    Condition,
    FailureGuide,
    Step,
)

E = TypeVar("E", bound=BaseException)
GuidanceMode = Literal["full", "short", "off"]

SCHEMA = 1
COMPACT_CAUSES = 3
COMPACT_STEPS = 4
JSON_CAUSES = 6
JSON_STEPS = 8
_MODES = ("full", "short", "off")

# (area module, kind) -> failure id, for DevOptionsError/AdbCommandError, DexError and
# AndroidSupportError. Where one kind covers several situations the raise site passes
# `failure_id=` (an instance tag), which wins over this map. A module listed in
# KIND_MAP_COVERED_MODULES must map every kind it raises (tests/test_failure_guide.py).
KIND_MAP: dict[tuple[str, str], str] = {
    # dev_options
    ("dev_options", "invalid_serial"): "input.serial_invalid",
    ("dev_options", "missing_adb"): "adb.not_found",
    ("dev_options", "unsafe_adb_server"): "adb.server.unverified",   # the cause chain refines it
    ("dev_options", "timeout"): "adb.timeout",
    ("dev_options", "adb_error"): "device.command_failed",
    ("dev_options", "denied"): "device.command_denied",
    ("dev_options", "identity_unavailable"): "device.identity_unavailable",
    ("dev_options", "malformed_value"): "devopts.internal_guard",
    ("dev_options", "untrusted_setting"): "devopts.internal_guard",
    ("dev_options", "untrusted_operation"): "devopts.internal_guard",
    ("dev_options", "invalid_identity"): "devopts.internal_guard",
    ("dev_options", "setting_timeout"): "devopts.setting_unreadable",
    ("dev_options", "setting_missing_adb"): "devopts.setting_unreadable",
    ("dev_options", "setting_adb_error"): "devopts.setting_unreadable",
    ("dev_options", "setting_denied"): "devopts.setting_unreadable",
    ("dev_options", "setting_malformed"): "devopts.setting_malformed",
    ("dev_options", "snapshot_io"): "devopts.snapshot_io",
    ("dev_options", "snapshot_exists"): "devopts.snapshot_exists",
    ("dev_options", "unsafe_snapshot"): "devopts.snapshot_unsafe",
    ("dev_options", "malformed_snapshot"): "devopts.snapshot_malformed",
    ("dev_options", "untrusted_value"): "devopts.snapshot_untrusted",
    ("dev_options", "concurrent_operation"): "devopts.concurrent_operation",
    ("dev_options", "readback_failed"): "devopts.readback_failed",
    ("dev_options", "rollback_conflict"): "devopts.apply_rollback_incomplete",
    ("dev_options", "unsupported_value"): "devopts.unsupported_value",
    ("dev_options", "invalid_profile"): "devopts.invalid_profile",
    ("dev_options", "unsupported_api"): "devopts.unsupported_api",
    ("dev_options", "apply_failed"): "devopts.apply_rollback_incomplete",  # the safe default; see apply_profile
    ("dev_options", "identity_mismatch"): "devopts.identity_mismatch",
    ("dev_options", "invalid_package"): "devopts.invalid_package",
    ("dev_options", "package_required"): "devopts.package_required",
    ("dev_options", "invalid_option"): "devopts.invalid_option",
    ("dev_options", "wireless_transport"): "devopts.wireless_refused",
    ("dev_options", "profile_api_range"): "devopts.profile_api_range",
    ("dev_options", "unsupported_state"): "devopts.unsupported_state",
    ("dev_options", "restore_failed"): "devopts.restore_failed",
    # dex
    ("dex", "invalid_serial"): "input.serial_invalid",
    ("dex", "missing_adb"): "adb.not_found",
    ("dex", "timeout"): "adb.timeout",
    ("dex", "adb_error"): "adb.exec_failed",
    ("dex", "unsafe_adb_server"): "adb.server.unverified",
    ("dex", "device_unreachable"): "device.unreachable",
    ("dex", "unsupported_api"): "dex.unsupported_api",
    ("dex", "primary_duplicate"): "dex.primary_display_selected",
    ("dex", "invalid_display"): "dex.invalid_display",
    ("dex", "unknown_display_format"): "dex.display_discovery_unknown",
    ("dex", "duplicate_display"): "dex.display_list_inconsistent",
    ("dex", "missing_primary"): "dex.display_list_inconsistent",
    ("dex", "missing_display"): "dex.display_missing",
    ("dex", "missing_scrcpy"): "scrcpy.not_found",
    ("dex", "scrcpy_version"): "scrcpy.version_unverified",
    ("dex", "confirmation_required"): "dex.confirmation_required",
    ("dex", "unverified_platform"): "dex.unverified_platform",
    ("dex", "dex_unconfirmed"): "dex.not_confirmed",
    ("dex", "launch_failed"): "dex.launch_failed",
    # android_support
    ("android_support", "invalid_serial"): "input.serial_invalid",
    ("android_support", "missing_adb"): "adb.not_found",
    ("android_support", "unsafe_adb_server"): "adb.server.unverified",
    ("android_support", "timeout"): "adb.timeout",
    ("android_support", "adb_error"): "device.command_failed",
    ("android_support", "denied"): "device.command_denied",
    ("android_support", "malformed_property"): "android.property_malformed",
    ("android_support", "malformed_sdk"): "android.property_malformed",
}
KIND_MAP_COVERED_MODULES: tuple[str, ...] = ("dev_options", "dex", "android_support")
# (area, compiled regex over an ADAPT-authored message) -> id. Transitional; may only shrink.
PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = ()

# Ordered (lowercase substring, symptom key) matched against adb output ADAPT already
# captured. The output itself is never re-printed; the key only reorders causes.
WIRELESS_SYMPTOMS: tuple[tuple[str, str], ...] = (
    ("connection refused", "refused"),
    ("connection timed out", "timed_out"),
    ("timed out", "timed_out"),
    ("no route to host", "no_route"),
    ("network is unreachable", "no_route"),
    ("failed to authenticate", "auth"),
    ("wrong password", "wrong_code"),
    ("wrong pairing code", "wrong_code"),
    ("protocol fault", "protocol"),
    ("unable to start pairing client", "pairing_client"),
)

ACTIVITY: dict[str, str] = {
    "devices": "listing devices",
    "adb": "running an ADB command",
    "pair": "pairing over Wi-Fi",
    "connect": "connecting over Wi-Fi",
    "setup": "installing Platform-Tools",
    "doctor": "checking this PC",
    "mcp": "starting the MCP server",
    "explain": "explaining a failure",
    "help-me": "showing help",
}

MCP_ERROR_MAP: dict[str, str] = {
    "no_adb": "adb.not_found",
    "adb_probe_failed": "adb.enumeration_failed",
    "unknown_device": "device.not_found",
    "need_device": "device.multiple",
    "no_device": "device.none_ready",
    "adb_timeout": "adb.timeout",
    "adb_launch_failed": "adb.exec_failed",
    "unsafe_shared_server": "adb.server.unverified",
    "access_denied": "mcp.access_denied",
    "invalid_params": "mcp.invalid_params",
    "unknown_tool": "mcp.unknown_tool",
    "internal_error": "internal.unclassified",
    # app_validate, app_logs, debug_app_inspect, diagnostic_logs (tools.tool_development). The tool
    # remembers a refined ID (tag_payload); these are the fallbacks when it has none.
    "development_inspection_failed": "internal.unclassified",
    "checks_failed": "app.validation_failed",
    "incomplete_evidence": "app.validation_inconclusive",
}
UNSAFE_SHARED_SERVER_MESSAGE = (
    "Shared ADB server on port 5037 is unverified or unsafe; ADAPT did not contact it."
)

_EFFECT_TAGS = {
    "read_only": "[read-only]",
    "changes_host": "[changes this PC]",
    "changes_device": "[changes the phone]",
    "on_phone": "[on the phone]",
    "manual": "[manual]",
}
_TEMPLATE_RE = re.compile(r"\{([a-z_]+)\}")
_TEMPLATE_UNKNOWN = {"device_state": "unknown", "ready_count": "several",
                     "not_ready_count": "several", "api_level": "unknown"}


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def tagged(exc: E, failure_id: str) -> E:
    """Attach a failure ID to any exception instance; the message is unchanged."""
    exc.failure_id = failure_id  # type: ignore[attr-defined]
    return exc


def explicit_id(exc: BaseException | None) -> str | None:
    """Return an instance-level tag (not a class default) when one was attached."""
    value = getattr(exc, "__dict__", {}).get("failure_id") if exc is not None else None
    return value if isinstance(value, str) else None


def _known(failure_id: str | None) -> str | None:
    if failure_id is None:
        return None
    failure_id = RENAMED.get(failure_id, failure_id)
    return failure_id if failure_id in GUIDES else None


def classify(exc: BaseException, *, area: str | None = None) -> str:
    """Map an exception to a guide ID without inspecting device or adb text."""
    # 1 explicit tag on the outer exception, 2 then on its cause/context chain.
    seen: set[int] = set()
    current: BaseException | None = exc
    for _ in range(6):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        found = _known(explicit_id(current))
        if found:
            return found
        current = current.__cause__ or current.__context__
    # 3 kind map
    kind = getattr(exc, "kind", None)
    if isinstance(kind, str):
        module = type(exc).__module__.rsplit(".", 1)[-1]
        found = _known(KIND_MAP.get((module, kind)))
        if found:
            return found
    # 4 class default through the MRO
    found = _known(getattr(type(exc), "failure_id", None))
    if found:
        return found
    # 5 type map
    if isinstance(exc, subprocess.TimeoutExpired):
        return "adb.timeout"
    if isinstance(exc, ImportError) and area == "mcp":
        return "mcp.sdk_missing"
    if isinstance(exc, OSError):
        return "host.os_error"
    # 6 message patterns (ADAPT-authored text only)
    text = str(exc)
    for pattern_area, pattern, failure_id in PATTERNS:
        if (area is None or pattern_area == area) and pattern.search(text):
            return failure_id
    return "internal.unclassified"


def adb_symptom(*texts: str | None) -> str:
    """Classify adb's own wireless output into a symptom key; never re-quoted."""
    blob = "\n".join(t for t in texts if t).lower()
    for needle, key in WIRELESS_SYMPTOMS:
        if needle in blob:
            return key
    return "unknown"


def device_state_key(raw_state: str | None) -> str:
    """Reduce an adb transport state to the FailureContext enum."""
    state = (raw_state or "").strip().lower()
    if state in {"device", "unauthorized", "offline"}:
        return state
    if state.startswith("no permissions") or state == "no_permissions":
        return "no_permissions"
    return "other"


_STATE_GUIDES = {"unauthorized": "device.unauthorized", "offline": "device.offline",
                 "no_permissions": "device.no_permissions", "other": "device.not_ready"}


def device_state_failure_id(state_key: str) -> str:
    return _STATE_GUIDES.get(state_key, "device.not_ready")


def server_check_failure_id(check: Mapping[str, Any] | None) -> str:
    """Pick the guide for a failed doctor `adb_server_5037` check.

    Since Phase 3 every doctor check carries `failure_id`, which is used when present.
    The summary and detail matching below remains only for reports without it (for
    example a report produced by an older ADAPT and replayed in a test).
    """
    if not isinstance(check, Mapping):
        return "adb.server.unverified"
    if _known(check.get("failure_id") if isinstance(check.get("failure_id"), str) else None):
        return check["failure_id"]
    summary = str(check.get("summary") or "")
    details = check.get("details") if isinstance(check.get("details"), Mapping) else {}
    if summary.startswith("Selected non-default ADB server endpoint"):
        return "adb.server.endpoint_unverified"
    if summary.startswith("ADB listener inspection failed"):
        return "adb.server.probe_failed"
    if (details.get("owner_is_root_or_system") or details.get("tmp_5037_owner_is_root")
            or details.get("owner_matches_current_user") is False):
        return "adb.server.foreign_owner"
    if details.get("version_mismatch"):
        return "adb.server.version_mismatch"
    return "adb.server.unverified"


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------

def _host_os() -> str:
    if sys.platform.startswith(("win32", "cygwin", "msys")):
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "macos"
    return "other"


def _env_present(*names: str) -> bool:
    return any(os.environ.get(name, "").strip() for name in names)


@dataclass(frozen=True)
class FailureContext:
    command: str | None = None                     # a command path only, e.g. "adb"
    surface: Literal["cli", "mcp", "console_child"] = "cli"
    host_os: Literal["windows", "linux", "macos", "other"] | None = None
    wsl: bool | None = None
    ssh_session: bool | None = None
    headless: bool | None = None
    adb_resolved: bool | None = None
    adb_override_set: bool | None = None           # ADAPT_ADB / ADB_PATH present (value never stored)
    adb_server_env_override: bool | None = None    # ADB_SERVER_SOCKET / ANDROID_ADB_SERVER_PORT / ADB_SERVER_PORT present
    adb_server_option_set: bool | None = None      # -H / -P / -L given on the adb command line
    target_from_env: bool | None = None            # target came from ANDROID_SERIAL, not -s
    adb_origin: Literal["google_official", "debian_apt", "unknown", "none"] | None = None
    doctor_checks: Mapping[str, Literal["pass", "warn", "fail", "skip"]] | None = None
    device_state: Literal["device", "unauthorized", "offline", "no_permissions", "other", "absent"] | None = None
    ready_count: int | None = None
    not_ready_count: int | None = None
    api_level: int | None = None
    adb_symptom: str | None = None
    interactive: bool = False

    @classmethod
    def from_runtime(cls, *, command: str | None = None, **known: Any) -> "FailureContext":
        """sys.platform + environment presence checks + caller-supplied facts. No I/O beyond that."""
        host_os = _host_os()
        base: dict[str, Any] = {
            "command": command,
            "host_os": host_os,
            "wsl": host_os == "linux" and _env_present("WSL_DISTRO_NAME", "WSL_INTEROP"),
            "ssh_session": _env_present("SSH_CONNECTION", "SSH_TTY"),
            "adb_override_set": _env_present("ADAPT_ADB", "ADB_PATH"),
            "adb_server_env_override": _env_present("ADB_SERVER_SOCKET", "ANDROID_ADB_SERVER_PORT", "ADB_SERVER_PORT"),
        }
        names = {f.name for f in fields(cls)}
        base.update({k: v for k, v in known.items() if k in names})
        return cls(**base)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ShownCause:
    text: str
    unknown: tuple[str, ...] = ()


@dataclass(frozen=True)
class ShownStep:
    step: Step
    text: str
    unknown: tuple[str, ...] = ()


@dataclass(frozen=True)
class Resolved:
    guide: FailureGuide
    summary: str
    causes: tuple[ShownCause, ...]
    steps: tuple[ShownStep, ...]
    context: FailureContext = field(default_factory=FailureContext)

    @property
    def id(self) -> str:
        return self.guide.id


def _evaluate(cond: Condition, ctx: FailureContext) -> tuple[bool, tuple[str, ...]]:
    unknown: list[str] = []
    for key, allowed in cond:
        value = getattr(ctx, key, None)
        if value is None:
            unknown.append(key)
        elif value not in allowed:
            return False, ()
    return True, tuple(unknown)


def _matches(cond: Condition, ctx: FailureContext) -> bool:
    return bool(cond) and all(getattr(ctx, k, None) is not None and getattr(ctx, k) in v for k, v in cond)


def _fill(text: str, ctx: FailureContext) -> str:
    def sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in TEMPLATE_FIELDS:
            return match.group(0)
        value = getattr(ctx, name, None)
        return _TEMPLATE_UNKNOWN[name] if value is None else str(value)
    return _TEMPLATE_RE.sub(sub, text)


def _label(text: str, label: str | None, unknown: tuple[str, ...]) -> str:
    return f"{label}: {text}" if unknown and label else text


def resolve(failure_id: str, context: FailureContext | None = None, *, activity: str | None = None) -> Resolved:
    ctx = context or FailureContext()
    guide = GUIDES.get(RENAMED.get(failure_id, failure_id)) or GUIDES["internal.unclassified"]
    summary = _fill(guide.summary, ctx)
    if guide.id == "internal.unclassified" and activity:
        summary = f"ADAPT stopped while {activity}, and it does not have a specific explanation for this error."

    def ordered(items: tuple[Any, ...]) -> list[tuple[Any, tuple[str, ...]]]:
        kept = []
        for item in items:
            keep, unknown = _evaluate(item.when, ctx)
            if keep:
                kept.append((item, unknown))
        return [x for x in kept if _matches(x[0].first_when, ctx)] + [x for x in kept if not _matches(x[0].first_when, ctx)]

    causes = tuple(ShownCause(_label(_fill(c.text, ctx), c.if_unknown, u), u) for c, u in ordered(guide.causes))
    steps = tuple(ShownStep(s, _label(_fill(s.text, ctx), s.if_unknown, u), u) for s, u in ordered(guide.steps))
    return Resolved(guide, summary, causes, steps, ctx)


def context_unknown(resolved: Resolved, *, max_causes: int | None = None, max_steps: int | None = None) -> list[str]:
    keys: list[str] = []
    for item in [*resolved.causes[:max_causes], *resolved.steps[:max_steps]]:
        keys.extend(k for k in item.unknown if k not in keys)
    return keys


# ---------------------------------------------------------------------------
# Human rendering
# ---------------------------------------------------------------------------

def _width() -> int:
    return max(40, min(shutil.get_terminal_size((80, 24)).columns, 100))


def _stderr_isatty() -> bool:
    stream = sys.stderr
    return bool(stream is not None and callable(getattr(stream, "isatty", None)) and stream.isatty())


def _color_allowed() -> bool:
    return (_stderr_isatty() and "NO_COLOR" not in os.environ
            and os.environ.get("TERM", "").strip().lower() != "dumb")


def _tag(step: Step, color: bool) -> str:
    tag = _EFFECT_TAGS.get(step.effect, "[manual]")
    admin = " [needs admin]" if step.needs_admin else ""
    if color and step.effect in {"changes_host", "changes_device"}:
        tag = f"\x1b[33m{tag}\x1b[0m"
    if color and admin:
        admin = f" \x1b[33m[needs admin]\x1b[0m"
    return tag + admin


def _bold(label: str, color: bool) -> str:
    return f"\x1b[1m{label}\x1b[0m" if color else label


def _wrap(text: str, width: int, first: str, rest: str) -> list[str]:
    return textwrap.wrap(_safe(text), width=width, initial_indent=first, subsequent_indent=rest,
                         break_long_words=False, break_on_hyphens=False) or [first.rstrip()]


def _diagnose_line(diagnose: str) -> str:
    if diagnose == "adapt doctor":
        return "Not sure which applies? adapt doctor checks this PC without changing anything."
    return f"Not sure which applies? {diagnose} shows more without changing anything."


def render_human(
    resolved: Resolved,
    *,
    width: int | None = None,
    color: bool = False,
    max_causes: int | None = COMPACT_CAUSES,
    max_steps: int | None = COMPACT_STEPS,
    max_avoid: int | None = 2,
    more: bool = True,
    extra_steps: tuple[Step, ...] = (),
) -> str:
    """Render the Why/Try/More block (no leading blank line)."""
    width = width or _width()
    guide = resolved.guide
    lines = _wrap(resolved.summary, width, _bold("Why:", color) + " ", "  ")
    causes = resolved.causes[:max_causes]
    if causes:
        lines.append("  Possible causes, most likely first:")
        for cause in causes:
            lines += _wrap(cause.text, width, "  - ", "    ")
    steps = [*resolved.steps[:max_steps], *(ShownStep(s, s.text) for s in extra_steps)]
    if steps:
        lines.append(_bold("Try:", color))
        for number, shown in enumerate(steps, 1):
            first = f"  {number}. "
            rest = " " * len(first)
            tag = _tag(shown.step, color)
            plain_tag = _tag(shown.step, False)
            if shown.step.command:
                lines += _wrap(shown.text, width, first, rest)
                lines.append(f"       {_safe(shown.step.command)}   {tag}")
            else:
                wrapped = _wrap(shown.text, width, first, rest)
                if len(wrapped[-1]) + 3 + len(plain_tag) <= width:
                    wrapped[-1] = f"{wrapped[-1]}   {tag}"
                else:
                    wrapped.append(f"{rest}{tag}")
                lines += wrapped
    if guide.diagnose and context_unknown(resolved, max_causes=max_causes, max_steps=max_steps):
        lines += _wrap(_diagnose_line(guide.diagnose), width, "  ", "  ")
    for note in guide.avoid[:max_avoid]:
        lines += _wrap(note, width, "  Avoid: ", "    ")
    if more:
        if guide.id == "internal.unclassified":
            lines.append(f"{_bold('More:', color)} adapt explain --list")
        else:
            topic = f"   |   adapt help-me {help_argument(guide.help_topic)}" if guide.help_topic else ""
            lines.append(f"{_bold('More:', color)} adapt explain {guide.id}{topic}")
    return "\n".join(lines)


def help_argument(topic: str) -> str:
    """A help topic as one copy-pasteable argument ("dev-options apply" needs quotes)."""
    return f'"{topic}"' if " " in topic else topic


def render_short(failure_id: str) -> str:
    return f"More: adapt explain {failure_id}"


def render_explain(resolved: Resolved, *, width: int | None = None) -> str:
    """Full on-demand view used by `adapt explain ID` and `adapt help-me ID` (stdout)."""
    width = width or _width()
    guide = resolved.guide
    lines = [f"ADAPT / EXPLAIN / {guide.id}", ""]
    lines.append(render_human(resolved, width=width, max_causes=None, max_steps=None, max_avoid=None, more=False))
    if guide.variability:
        lines += ["", *_wrap(guide.variability, width, "Varies by setup: ", "  ")]
    if guide.commands:
        lines += ["", *_wrap(", ".join(f"adapt {c}" for c in guide.commands), width, "Raised by: ", "  ")]
    if guide.help_topic:
        lines.append(f"Help: adapt help-me {help_argument(guide.help_topic)}")
    if guide.docs:
        lines.append(f"Docs: {guide.docs} (in an ADAPT source checkout)")
    lines.append("All failures: adapt explain --list")
    return "\n".join(lines)


def render_list(entries: list[dict[str, str]], *, title: str = "known failures") -> str:
    lines = [f"ADAPT / EXPLAIN / {title}", ""]
    namespace = None
    for entry in entries:
        current = entry["id"].split(".", 1)[0]
        if current != namespace:
            if namespace is not None:
                lines.append("")
            lines.append(current)
            namespace = current
        lines.append(f"  {entry['id']}  {_safe(entry['summary'])}")
    lines += ["", "Run 'adapt explain ID' for causes and steps, or 'adapt explain WORDS' to search."]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------

def list_entries(ids: list[str] | None = None) -> list[dict[str, str]]:
    chosen = ids if ids is not None else sorted(GUIDES)
    return [{"id": i, "summary": _fill(GUIDES[i].summary, FailureContext())} for i in chosen]


def search(query: str) -> list[str]:
    """IDs matching an exact ID, a keyword, or pasted error text (exact ID first)."""
    q = " ".join((query or "").strip().lower().split())
    if not q:
        return []
    exact = RENAMED.get(q, q)
    if exact in GUIDES:
        return [exact]
    hits: list[str] = []
    for guide in GUIDES.values():
        haystack = [guide.id, guide.summary.lower(), *(a.lower() for a in guide.aliases)]
        if any(q in item for item in haystack) or any(len(a) >= 4 and a.lower() in q for a in guide.aliases):
            hits.append(guide.id)
    if hits:
        return hits
    words = [w for w in re.findall(r"[a-z0-9_.:/-]+", q) if len(w) >= 4]
    scores: dict[str, int] = {}
    for guide in GUIDES.values():
        blob = " ".join([guide.id, guide.summary, *guide.aliases]).lower()
        score = sum(1 for w in words if w in blob)
        if score:
            scores[guide.id] = score
    if not scores:
        return []
    best = max(scores.values())
    return sorted(i for i, s in scores.items() if s == best)


# ---------------------------------------------------------------------------
# Machine output
# ---------------------------------------------------------------------------

def _recommendation(shown: ShownStep) -> dict[str, Any]:
    step = shown.step
    return {
        "text": shown.text,
        "command": step.command,
        "effect": step.effect,
        "changes_state": step.effect in {"changes_host", "changes_device"},
        "needs_admin": step.needs_admin,
        "applies": "unknown" if shown.unknown else "yes",
    }


def failure_payload(resolved: Resolved, *, full: bool = False) -> dict[str, Any]:
    guide = resolved.guide
    max_causes = None if full else JSON_CAUSES
    max_steps = None if full else JSON_STEPS
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "id": guide.id,
        "summary": resolved.summary,
        "certainty": guide.certainty,
        "causes": [c.text for c in resolved.causes[:max_causes]],
        "recommendations": [_recommendation(s) for s in resolved.steps[:max_steps]],
        "avoid": list(guide.avoid),
        "help_topic": guide.help_topic,
        "docs": guide.docs,
        "explain": f"adapt explain {guide.id}",
        "context_unknown": context_unknown(resolved, max_causes=max_causes, max_steps=max_steps),
    }
    if full:
        payload.update({"variability": guide.variability, "diagnose": guide.diagnose,
                        "commands": list(guide.commands), "aliases": list(guide.aliases)})
    return payload


def attach_failure(payload: Mapping[str, Any], failure_id: str, context: FailureContext | None = None) -> dict[str, Any]:
    """Return a new dict with an additive `failure` key; existing keys are unchanged."""
    return {**payload, "failure": failure_payload(resolve(failure_id, context))}


def refine_mcp_failure_id(error: str, payload: Mapping[str, Any], requested_device: str | None = None) -> str:
    failure_id = MCP_ERROR_MAP.get(error, "internal.unclassified")
    if failure_id != "device.none_ready":
        return failure_id
    devices = [d for d in payload.get("devices") or [] if isinstance(d, Mapping)]
    if requested_device:
        match = next((d for d in devices if d.get("device") == requested_device), None)
        if match is not None and device_state_key(match.get("state")) != "device":
            return device_state_failure_id(device_state_key(match.get("state")))
        return failure_id
    states = {device_state_key(d.get("state")) for d in devices}
    if len(states) == 1 and "device" not in states:
        return device_state_failure_id(states.pop())
    return failure_id


def mcp_failure(
    error: str,
    payload: Mapping[str, Any],
    *,
    exposes: Callable[[str], bool] = lambda _name: True,
    failure_id: str | None = None,
    requested_device: str | None = None,
    argument_hint: str | None = None,
) -> dict[str, Any]:
    """Agent-facing failure object for an MCP error payload (policy-aware next tool)."""
    fid = _known(failure_id) or refine_mcp_failure_id(error, payload, requested_device)
    devices = [d for d in payload.get("devices") or [] if isinstance(d, Mapping)]
    states = [device_state_key(d.get("state")) for d in devices]
    ctx = FailureContext.from_runtime(
        command=None, surface="mcp", target_from_env=False,
        ready_count=sum(1 for s in states if s == "device") if devices else None,
        not_ready_count=sum(1 for s in states if s != "device") if devices else None,
    )
    resolved = resolve(fid, ctx)
    guide = resolved.guide
    hint = guide.mcp
    next_tool = None
    if hint is not None:
        for name in ((hint.next_tool,) if hint.next_tool else ()) + hint.fallback_tools:
            if exposes(name):
                next_tool = {"name": name, "arguments": {}}
                break
    recommendations = []
    for shown in resolved.steps[:JSON_STEPS]:
        tool = shown.step.mcp_tool if shown.step.mcp_tool and exposes(shown.step.mcp_tool) else None
        recommendations.append({"text": shown.text, "command": shown.step.command,
                                "effect": shown.step.effect, "mcp_tool": tool})
    return {
        "schema": SCHEMA,
        "id": guide.id,
        "summary": resolved.summary,
        "certainty": guide.certainty,
        "causes": [c.text for c in resolved.causes[:JSON_CAUSES]],
        "recommendations": recommendations,
        "user_action_required": bool(hint and hint.user_action_required),
        "ask_user": hint.ask_user if hint else None,
        "next_tool": next_tool,
        "retry": hint.retry if hint else "not_useful",
        "argument_hint": argument_hint or (hint.argument_hint if hint else None),
    }


class TaggedPayload(dict):
    """A plain error payload dict that also remembers a refined failure ID."""

    failure_id: str | None = None


def tag_payload(payload: Mapping[str, Any], failure_id: str | None) -> TaggedPayload:
    result = TaggedPayload(payload)
    result.failure_id = failure_id
    return result


# ---------------------------------------------------------------------------
# Emitting (human, stderr)
# ---------------------------------------------------------------------------

def _click_context() -> Any:
    try:
        from typer._click.globals import get_current_context  # Typer versions with bundled Click.
    except ImportError:  # pragma: no cover - depends on the installed Typer
        from click import get_current_context
    return get_current_context(silent=True)


def _preference_mode() -> GuidanceMode | None:
    try:
        from adapt_adb.help_system import read_preferences
        value = read_preferences().get("failure_guidance")
    except (OSError, ValueError):
        return "short"  # A broken preference never blocks the command.
    if value is None:
        return None
    return value if value in _MODES else "short"


def _tips_disabled(root: Any) -> bool:
    if root is not None and root.params.get("no_tips"):
        return True
    if os.environ.get("ADAPT_TIPS", "").strip().lower() in {"0", "off", "false", "no"}:
        return True
    try:
        from adapt_adb.help_system import read_preferences
        return read_preferences().get("tips", True) is False
    except (OSError, ValueError):
        return True


def guidance_mode(click_ctx: Any = None) -> GuidanceMode:
    """How much human guidance to print on stderr for this invocation."""
    root = click_ctx.find_root() if click_ctx is not None else None
    if root is not None and root.meta.get("adapt_machine_output"):
        return "off"
    env = os.environ.get("ADAPT_FAILURE_GUIDANCE", "").strip().lower()
    if env in _MODES:
        return env  # type: ignore[return-value]
    preference = _preference_mode()
    if preference is not None:
        return preference
    if not _stderr_isatty():
        return "short"
    if _tips_disabled(root):
        return "short"
    return "full"


def pointers_enabled() -> bool:
    """Whether a report renderer (doctor) may add `adapt explain` pointer lines.

    Only `ADAPT_FAILURE_GUIDANCE=off` (or the saved preference "off") removes them; the
    pointers are one short line each, so terminal and redirected output both keep them.
    """
    env = os.environ.get("ADAPT_FAILURE_GUIDANCE", "").strip().lower()
    if env in _MODES:
        return env != "off"
    return _preference_mode() != "off"


def doctor_explain_ids(report: Mapping[str, Any]) -> list[str]:
    """Distinct failure IDs of the report's failing or warning checks, in check order."""
    ids: list[str] = []
    for check in report.get("checks") or []:
        failure_id = check.get("failure_id") if isinstance(check, Mapping) else None
        if check.get("status") in {"fail", "warn"} and isinstance(failure_id, str) and failure_id not in ids:
            ids.append(failure_id)
    return ids


def emit_failure(failure_id: str, context: FailureContext | None = None, *, ctx: Any = None) -> None:
    """Print guidance after an existing error line. Never raises, never changes exit codes."""
    try:
        ctx = ctx if ctx is not None else _click_context()
        mode = guidance_mode(ctx)
        if mode == "off":
            return
        failure_id = RENAMED.get(failure_id, failure_id)
        if ctx is not None:
            emitted = ctx.find_root().meta.setdefault("adapt_failures_emitted", set())
            if failure_id in emitted:
                return
            emitted.add(failure_id)
        stream = sys.stderr
        if mode == "short":
            stream.write(render_short(failure_id) + "\n")
        else:
            stream.write("\n" + render_human(resolve(failure_id, context), color=_color_allowed()) + "\n")
        stream.flush()
    except Exception:  # Guidance must never turn a reported failure into a crash.
        return


def emit_pointer(failure_id: str, *, label: str = "More:") -> None:
    """One `<label> adapt explain ID` line on stderr, for results that are not failures.

    Used where a command succeeds but could not observe something, or started a process
    it does not watch: the exit code stays as it is and no Why/Try block is printed.
    Silent under --json, the MCP server and ADAPT_FAILURE_GUIDANCE=off. Never raises.
    """
    try:
        ctx = _click_context()
        if guidance_mode(ctx) == "off":
            return
        failure_id = RENAMED.get(failure_id, failure_id)
        if ctx is not None:
            emitted = ctx.find_root().meta.setdefault("adapt_failures_emitted", set())
            if failure_id in emitted:
                return
            emitted.add(failure_id)
        sys.stderr.write(f"{label} adapt explain {failure_id}\n")
        sys.stderr.flush()
    except Exception:
        return


# ---------------------------------------------------------------------------
# Helpers for modules whose errors are plain messages (Phase 4)
# ---------------------------------------------------------------------------

# Query statuses that mean the device did not answer at all (adb error, timeout, could not start).
UNANSWERED_STATUSES = frozenset({"failed", "timeout", "unavailable"})


def query_failure_id(status: object, default: str) -> str:
    """Guide for a read-only device query that did not return an observation.

    A device that did not answer is reported as such instead of as the symptom the
    command was looking for (for example "user could not be observed").
    """
    if status in UNANSWERED_STATUSES:
        return "device.unanswered"
    if status == "denied":
        return "device.command_denied"
    return default


def cause_failure_id(exc: BaseException | None, default: str) -> str:
    """The specific guide of an exception a module wraps in its own message (`raise X(...) from exc`)."""
    if exc is None:
        return default
    found = classify(exc)
    return default if found == "internal.unclassified" else found


def current_command() -> str | None:
    """The running command path without the program name ("app validate"), for context only."""
    try:
        ctx = _click_context()
        parts = ctx.command_path.split()[1:] if ctx is not None else []
        return " ".join(parts) or None
    except Exception:
        return None


_DEBUG_STEP = Step("To see technical details, run the same command again with ADAPT_DEBUG=1.", effect="manual")


def specific_id(exc: BaseException) -> str | None:
    """A guide ID the exception itself carries (instance tag, its cause chain, kind or class default).

    Unlike classify(), no type map or message pattern is used: an exception that reaches the
    unexpected-error handler is described specifically only when ADAPT tagged it on purpose.
    """
    found = classify(exc)
    if found == "internal.unclassified":
        return None
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(seen) < 6:
        seen.add(id(current))
        if _known(explicit_id(current)):
            return found
        current = current.__cause__ or current.__context__
    kind = getattr(exc, "kind", None)
    if isinstance(kind, str) and _known(KIND_MAP.get((type(exc).__module__.rsplit(".", 1)[-1], kind))):
        return found
    return found if _known(getattr(type(exc), "failure_id", None)) else None


def report_unexpected(ctx: Any, exc: BaseException) -> None:
    """Replace a traceback with a generic, privacy-safe report (type name only)."""
    root = ctx.find_root() if ctx is not None else None
    path = (root.meta.get("adapt_command_path") if root is not None else None) or ""
    activity = ACTIVITY.get(path) or (f"running 'adapt {path}'" if path else "running ADAPT")
    # The first line stays generic; a tagged exception (for example a DexError that escaped a
    # module's handler) gets its specific Why/Try block instead of the generic one.
    resolved = resolve(specific_id(exc) or "internal.unclassified", FailureContext.from_runtime(command=path or None), activity=activity)
    if root is not None and root.meta.get("adapt_machine_output"):
        payload = {"status": "error", "exit_code": 1, "error": "internal_error",
                   "failure": failure_payload(resolved)}
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        sys.stdout.flush()
        return
    sys.stderr.write(f"Error: ADAPT stopped unexpectedly while {activity} ({type(exc).__name__}).\n")
    mode = guidance_mode(ctx)
    if mode == "full":
        sys.stderr.write("\n" + render_human(resolved, color=_color_allowed(), extra_steps=(_DEBUG_STEP,)) + "\n")
    elif mode == "short":
        sys.stderr.write(f"More: adapt explain {resolved.id}   |   ADAPT_DEBUG=1 shows technical details\n")
    sys.stderr.flush()


def namespace_of(failure_id: str) -> str:
    return failure_id.split(".", 1)[0]


__all__ = [
    "ACTIVITY", "KIND_MAP", "KIND_MAP_COVERED_MODULES", "MCP_ERROR_MAP", "NAMESPACES", "PATTERNS",
    "UNSAFE_SHARED_SERVER_MESSAGE", "WIRELESS_SYMPTOMS", "Cause", "FailureContext", "Resolved",
    "TaggedPayload", "UNANSWERED_STATUSES", "adb_symptom", "attach_failure", "cause_failure_id", "classify",
    "context_unknown", "current_command", "device_state_failure_id", "device_state_key", "emit_failure",
    "emit_pointer", "explicit_id", "failure_payload", "guidance_mode", "list_entries", "mcp_failure",
    "query_failure_id", "refine_mcp_failure_id", "render_explain", "render_human", "render_list",
    "render_short", "report_unexpected", "resolve", "search", "server_check_failure_id", "specific_id", "tag_payload", "tagged",
]
