"""Allowlisted Developer Options entries: settings, properties and command pairs.

Pure data and strict parsers.  Nothing here talks to a device and nothing raises
a project exception: ``dev_options`` runs the commands, reads them back and turns
a refusal into a classified error.

Three entry types share one snapshot model:

``setting``   a Settings Provider key (``settings get|put|delete``); the value is
              checked by a per-entry rule (number with bounds, enum or pattern).
``property``  a ``debug.*`` system property the shell user can set; restore uses
              ``setprop`` with the prior value, or an empty value when it was unset.
``command``   a control with its own read and reset commands (``wm density``,
              ``cmd battery``, ``am set-debug-app``...).  The read output is parsed
              strictly into a short canonical state; restore puts that observed
              state back instead of a guessed default.

Properties changed here reach a running app only after the app restarts.  ADAPT
never pokes SystemProperties and never uses raw Binder calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

PACKAGE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+")
NUMBER_RE = re.compile(r"^-?(?:\d+(?:\.\d*)?|\.\d+)$")
MAX_READ_CHARS = 2_000_000
MAX_VALUE_CHARS = 256


def valid_package(value: Any) -> bool:
    """True for an Android application id; the only per-app scope value ADAPT accepts."""
    return isinstance(value, str) and len(value) <= 255 and bool(PACKAGE_RE.fullmatch(value))


def plain_number(value: Any) -> Decimal | None:
    """A finite decimal for a plain numeric string, else None (no exponents, no spaces)."""
    if not isinstance(value, str) or not NUMBER_RE.fullmatch(value):
        return None
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


@dataclass(frozen=True)
class ValueRule:
    """How one entry's value is validated: a bounded number, an enum or a strict pattern."""

    kind: str = "number"                 # number | enum | pattern
    values: tuple[str, ...] = ()
    pattern: str = ""
    minimum: str | None = None
    maximum: str | None = None

    def accepts(self, value: Any) -> bool:
        if not isinstance(value, str) or len(value) > MAX_VALUE_CHARS:
            return False
        if self.kind == "enum":
            return value in self.values
        if self.kind == "pattern":
            return bool(re.fullmatch(self.pattern, value))
        number = plain_number(value)
        if number is None:
            return False
        if self.minimum is not None and number < Decimal(self.minimum):
            return False
        return not (self.maximum is not None and number > Decimal(self.maximum))

    def equal(self, left: Any, right: Any) -> bool:
        if self.kind == "number":
            a, b = plain_number(left), plain_number(right)
            return a is not None and b is not None and a == b
        return isinstance(left, str) and left == right


NUMBER = ValueRule()


def enum(*values: str) -> ValueRule:
    return ValueRule("enum", values=tuple(values))


def pattern(regex: str) -> ValueRule:
    return ValueRule("pattern", pattern=regex)


# ---------------------------------------------------------------------------
# Settings Provider keys
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SettingSpec:
    name: str
    group: str
    table: str
    key: str
    user_scoped: bool = False
    rule: ValueRule = NUMBER


# The 13 settings `dev-options inspect` has always read.
SETTINGS: tuple[SettingSpec, ...] = (
    SettingSpec("developer_mode", "debugging", "global", "development_settings_enabled"),
    SettingSpec("usb_debugging", "debugging", "global", "adb_enabled"),
    SettingSpec("wireless_debugging", "debugging", "global", "adb_wifi_enabled"),
    SettingSpec("stay_awake", "power", "global", "stay_on_while_plugged_in"),
    SettingSpec("window_animation_scale", "animations", "global", "window_animation_scale"),
    SettingSpec("transition_animation_scale", "animations", "global", "transition_animation_scale"),
    SettingSpec("animator_duration_scale", "animations", "global", "animator_duration_scale"),
    SettingSpec("show_touches", "input", "system", "show_touches", True),
    SettingSpec("pointer_location", "input", "system", "pointer_location", True),
    SettingSpec("dont_keep_activities", "lifecycle", "global", "always_finish_activities"),
    SettingSpec("show_background_anrs", "lifecycle", "secure", "anr_show_background", True),
    SettingSpec("freeform_windows", "desktop", "global", "enable_freeform_support"),
    SettingSpec("force_desktop", "desktop", "global", "force_desktop_mode_on_external_displays"),
)

_ZERO_ONE = enum("0", "1")

# Settings only the newer profiles write.  They are not part of the 13 inspected settings.
PROFILE_SETTINGS: tuple[SettingSpec, ...] = (
    SettingSpec("view_attribute_inspection", "layout", "global", "debug_view_attributes", False, _ZERO_ONE),
    SettingSpec("force_rtl_setting", "layout", "global", "debug.force_rtl", False, _ZERO_ONE),
    SettingSpec("show_first_crash_dialog", "lifecycle", "secure", "show_first_crash_dialog_dev_option", True, _ZERO_ONE),
    SettingSpec("font_scale", "accessibility", "system", "font_scale", True, ValueRule("number", minimum="0.85", maximum="2.0")),
    SettingSpec("high_text_contrast", "accessibility", "secure", "high_text_contrast_enabled", True, _ZERO_ONE),
    SettingSpec("bold_text", "accessibility", "secure", "font_weight_adjustment", True, ValueRule("number", minimum="0", maximum="500")),
    SettingSpec("daltonizer_enabled", "accessibility", "secure", "accessibility_display_daltonizer_enabled", True, _ZERO_ONE),
    SettingSpec("daltonizer_mode", "accessibility", "secure", "accessibility_display_daltonizer", True, ValueRule("number", minimum="-1", maximum="20")),
    SettingSpec("show_key_presses", "input", "system", "show_key_presses", True, _ZERO_ONE),
    SettingSpec("ingress_rate_limit", "network", "global", "ingress_rate_limit_bytes_per_second", False, ValueRule("number", minimum="-1", maximum="1000000000")),
)

ALL_SETTINGS: tuple[SettingSpec, ...] = SETTINGS + PROFILE_SETTINGS
SETTING_BY_ID = {(item.table, item.key): item for item in ALL_SETTINGS}
SETTING_BY_NAME = {item.name: item for item in ALL_SETTINGS}


# ---------------------------------------------------------------------------
# debug.* system properties
# ---------------------------------------------------------------------------

# A prior value is restored verbatim only when it is a plain token; an unset
# property is restored as an empty value.
PROPERTY_TOKEN = pattern(r"[A-Za-z0-9_.-]{1,64}")


@dataclass(frozen=True)
class PropertySpec:
    name: str
    group: str
    applied: ValueRule


PROPERTIES: tuple[PropertySpec, ...] = (
    PropertySpec("debug.layout", "layout", enum("true")),
    PropertySpec("debug.force_rtl", "layout", enum("1")),
    PropertySpec("debug.hwui.force_dark", "accessibility", enum("true")),
)
PROPERTY_BY_NAME = {item.name: item for item in PROPERTIES}


# ---------------------------------------------------------------------------
# Command pairs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandContext:
    package: str | None
    user: int


@dataclass(frozen=True)
class Parsed:
    value: str
    extras: dict[str, Any] = field(default_factory=dict)


Argv = list[str]


@dataclass(frozen=True)
class CommandSpec:
    name: str
    group: str
    scope: str                                   # "none" | "package"
    user_scoped: bool
    read: Callable[[CommandContext], list[Argv]]
    parse: Callable[[list[str]], Parsed | None]
    apply: Callable[[CommandContext, str], list[Argv]]
    restore: Callable[[CommandContext, str], list[Argv]]
    original_rule: ValueRule
    applied_rule: ValueRule
    exact_restore: bool = True
    verify_restore: bool = True                  # False: reset only, nothing to compare to
    conflict_check: bool = True                  # False: reset is always safe to run
    restore_note: str = ""
    apply_note: str = ""


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _first(outputs: list[str]) -> str:
    return outputs[0][:MAX_READ_CHARS] if outputs else ""


# --- wm density ------------------------------------------------------------

_DENSITY_PHYSICAL = re.compile(r"Physical density: ([0-9]{2,4})")
_DENSITY_OVERRIDE = re.compile(r"Override density: ([0-9]{2,4})")


def parse_density(outputs: list[str]) -> Parsed | None:
    physical = override = None
    for line in _lines(_first(outputs)):
        if match := _DENSITY_PHYSICAL.fullmatch(line):
            physical = int(match.group(1))
        elif match := _DENSITY_OVERRIDE.fullmatch(line):
            override = int(match.group(1))
        else:
            return None
    if physical is None:
        return None
    return Parsed("default" if override is None else f"override:{override}", {"physical": physical})


def _density_value(state: str) -> str:
    return state.split(":", 1)[1]


# --- cmd battery -----------------------------------------------------------

_BATTERY_POWER = re.compile(r"(AC|USB|Wireless) powered: (true|false)")


def parse_battery(outputs: list[str]) -> Parsed | None:
    text = _first(outputs)
    power: dict[str, bool] = {}
    for line in _lines(text):
        if match := _BATTERY_POWER.fullmatch(line):
            power[match.group(1)] = match.group(2) == "true"
    if set(power) != {"AC", "USB", "Wireless"}:
        return None
    if "UPDATES STOPPED" not in text:
        return Parsed("normal")
    return Parsed("override-other" if any(power.values()) else "unplugged")


# --- dumpsys deviceidle ----------------------------------------------------

_DOZE_NORMAL = frozenset({"ACTIVE", "INACTIVE", "IDLE_PENDING", "SENSING", "LOCATING", "QUICK_DOZE_DELAY"})


def parse_doze(outputs: list[str]) -> Parsed | None:
    lines = _lines(_first(outputs))
    if len(lines) != 1:
        return None
    state = lines[0]
    if state in _DOZE_NORMAL:
        return Parsed("normal", {"state": state})
    if state == "IDLE":
        return Parsed("idle", {"state": state})
    if state == "IDLE_MAINTENANCE":
        return Parsed("maintenance", {"state": state})
    return None


# --- cmd uimode night ------------------------------------------------------

_NIGHT = re.compile(r"Night mode: (yes|no|auto|custom|custom_schedule|custom_bedtime)")


def parse_night(outputs: list[str]) -> Parsed | None:
    lines = _lines(_first(outputs))
    if len(lines) != 1 or not (match := _NIGHT.fullmatch(lines[0])):
        return None
    mode = match.group(1)
    return Parsed("custom" if mode.startswith("custom") else mode)


# --- debug app (am set-debug-app) ------------------------------------------

_DEBUG_STATE = re.compile(r"app=(" + PACKAGE_RE.pattern + r");wait=([01])")


def parse_debug_app(outputs: list[str]) -> Parsed | None:
    if len(outputs) != 2:
        return None
    app, wait = outputs[0].strip(), outputs[1].strip()
    if wait not in {"", "null", "0", "1"}:
        return None
    if app in {"", "null"}:
        return Parsed("none")
    if not valid_package(app):
        return None
    return Parsed(f"app={app};wait={1 if wait == '1' else 0}")


def _debug_parts(state: str) -> tuple[str, str] | None:
    match = _DEBUG_STATE.fullmatch(state)
    return (match.group(1), match.group(2)) if match else None


# --- cmd package compile (dexopt) ------------------------------------------

_DEXOPT_STATUS = re.compile(r"\[status=([a-z][a-z0-9-]{1,30})\]")


def parse_dexopt(outputs: list[str]) -> Parsed | None:
    text = _first(outputs)
    start = text.find("Dexopt state:")
    if start < 0:
        return None
    match = _DEXOPT_STATUS.search(text, start)
    return Parsed(match.group(1)) if match else None


# --- app standby bucket ----------------------------------------------------

_BUCKET_BY_NUMBER = {"10": "active", "20": "working_set", "30": "frequent", "40": "rare", "45": "restricted",
                     "5": "exempted", "50": "never"}


def parse_bucket(outputs: list[str]) -> Parsed | None:
    lines = _lines(_first(outputs))
    if len(lines) != 1:
        return None
    name = _BUCKET_BY_NUMBER.get(lines[0])
    return Parsed(name) if name else None


# --- appops RUN_ANY_IN_BACKGROUND ------------------------------------------

_APPOP = re.compile(r"RUN_ANY_IN_BACKGROUND: (allow|ignore|deny|foreground|default)(?:;.*)?")


def parse_appop(outputs: list[str]) -> Parsed | None:
    lines = _lines(_first(outputs))
    if lines == ["No operations."]:
        return Parsed("allow")
    if len(lines) != 1 or not (match := _APPOP.fullmatch(lines[0])):
        return None
    mode = match.group(1)
    return Parsed("allow" if mode == "default" else mode)


# --- per-app locale --------------------------------------------------------

_LOCALE_TAG = r"[a-z]{2,3}(?:-[a-z0-9]{2,8}){0,3}"
_LOCALE_LIST = re.compile(r"Locales for (\S+) for user ([0-9]+) are \[([^\]]*)\]")
_LOCALE_RAW_TAG = re.compile(r"[A-Za-z]{2,3}(?:[_-][A-Za-z0-9]{2,8}){0,3}")


def parse_locales(outputs: list[str]) -> Parsed | None:
    lines = _lines(_first(outputs))
    if len(lines) != 1 or not (match := _LOCALE_LIST.fullmatch(lines[0])):
        return None
    tags: list[str] = []
    for raw in (part.strip() for part in match.group(3).split(",") if part.strip()):
        if not _LOCALE_RAW_TAG.fullmatch(raw):
            return None
        tags.append(raw.replace("_", "-").lower())
    if len(tags) > 8:
        return None
    return Parsed(",".join(tags), {"package": match.group(1), "user": int(match.group(2))})


# --- data saver ------------------------------------------------------------

_NETPOLICY = re.compile(r"Restrict background status: (enabled|disabled)")


def parse_netpolicy(outputs: list[str]) -> Parsed | None:
    lines = _lines(_first(outputs))
    if len(lines) != 1 or not (match := _NETPOLICY.fullmatch(lines[0])):
        return None
    return Parsed(match.group(1))


# --- airplane mode (read through the global setting) -----------------------


def parse_airplane(outputs: list[str]) -> Parsed | None:
    lines = _lines(_first(outputs))
    # An unset key reads as "null" and means airplane mode is off.
    if len(lines) != 1 or lines[0] not in {"0", "1", "null"}:
        return None
    return Parsed("enabled" if lines[0] == "1" else "disabled")


def _user_args(ctx: CommandContext) -> Argv:
    return ["--user", str(ctx.user)]


def _pkg(ctx: CommandContext) -> str:
    return ctx.package or ""


COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec(
        "wm_density", "layout", "none", False,
        read=lambda ctx: [["wm", "density"]],
        parse=parse_density,
        apply=lambda ctx, value: [["wm", "density", _density_value(value)]],
        restore=lambda ctx, value: [["wm", "density", "reset"] if value == "default" else ["wm", "density", _density_value(value)]],
        original_rule=pattern(r"default|override:[0-9]{2,4}"),
        applied_rule=pattern(r"override:(?:1[2-9][0-9]|[2-9][0-9]{2})"),
    ),
    CommandSpec(
        "battery_state", "power", "none", False,
        read=lambda ctx: [["dumpsys", "battery"]],
        parse=parse_battery,
        apply=lambda ctx, value: [["cmd", "battery", "unplug"]],
        restore=lambda ctx, value: [["cmd", "battery", "reset"]],
        original_rule=enum("normal"),
        applied_rule=enum("unplugged"),
        restore_note="battery reporting is returned to the real hardware state with 'cmd battery reset'",
    ),
    CommandSpec(
        "doze_state", "power", "none", False,
        read=lambda ctx: [["dumpsys", "deviceidle", "get", "deep"]],
        parse=parse_doze,
        apply=lambda ctx, value: [["dumpsys", "deviceidle", "force-idle"]],
        restore=lambda ctx, value: [["dumpsys", "deviceidle", "unforce"]],
        original_rule=enum("normal"),
        applied_rule=enum("idle"),
        exact_restore=False,
        restore_note="Doze returns to normal scheduling, not necessarily to the same step it was in before",
    ),
    CommandSpec(
        "night_mode", "accessibility", "none", False,
        read=lambda ctx: [["cmd", "uimode", "night"]],
        parse=parse_night,
        apply=lambda ctx, value: [["cmd", "uimode", "night", value]],
        restore=lambda ctx, value: [["cmd", "uimode", "night", value]],
        original_rule=enum("yes", "no", "auto"),
        applied_rule=enum("yes"),
    ),
    CommandSpec(
        "debug_app", "debugging", "package", False,
        read=lambda ctx: [["settings", "get", "global", "debug_app"], ["settings", "get", "global", "wait_for_debugger"]],
        parse=parse_debug_app,
        apply=lambda ctx, value: [_set_debug_app(value)],
        restore=lambda ctx, value: [["am", "clear-debug-app"] if value == "none" else _set_debug_app(value)],
        original_rule=pattern(r"none|" + _DEBUG_STATE.pattern),
        applied_rule=pattern(_DEBUG_STATE.pattern),
        apply_note="the debug app is persistent and makes every launch of the app wait for a debugger until it is restored",
    ),
    CommandSpec(
        "package_compile", "performance", "package", False,
        read=lambda ctx: [["dumpsys", "package", _pkg(ctx)]],
        parse=parse_dexopt,
        apply=lambda ctx, value: [["cmd", "package", "compile", "-m", value, "-f", _pkg(ctx)]],
        restore=lambda ctx, value: [["cmd", "package", "compile", "--reset", _pkg(ctx)]],
        original_rule=pattern(r"[a-z][a-z0-9-]{1,30}"),
        applied_rule=enum("speed-profile"),
        exact_restore=False,
        verify_restore=False,
        conflict_check=False,
        restore_note="'cmd package compile --reset' returns the app to the install-time compiler filter, which can differ from the filter it had before",
    ),
    CommandSpec(
        "standby_bucket", "power", "package", True,
        read=lambda ctx: [["am", "get-standby-bucket", *_user_args(ctx), _pkg(ctx)]],
        parse=parse_bucket,
        apply=lambda ctx, value: [["am", "set-standby-bucket", *_user_args(ctx), _pkg(ctx), value]],
        restore=lambda ctx, value: [["am", "set-standby-bucket", *_user_args(ctx), _pkg(ctx), value]],
        original_rule=enum("active", "working_set", "frequent", "rare", "restricted"),
        applied_rule=enum("rare"),
        restore_note="the system may move the app to another bucket on its own, which is reported as a conflict",
    ),
    CommandSpec(
        "app_background", "power", "package", True,
        read=lambda ctx: [["cmd", "appops", "get", *_user_args(ctx), _pkg(ctx), "RUN_ANY_IN_BACKGROUND"]],
        parse=parse_appop,
        apply=lambda ctx, value: [["cmd", "appops", "set", *_user_args(ctx), _pkg(ctx), "RUN_ANY_IN_BACKGROUND", value]],
        restore=lambda ctx, value: [["cmd", "appops", "set", *_user_args(ctx), _pkg(ctx), "RUN_ANY_IN_BACKGROUND", "default" if value == "allow" else value]],
        original_rule=enum("allow", "ignore", "deny", "foreground"),
        applied_rule=enum("ignore"),
        exact_restore=False,
        restore_note="an app operation that was allowed is put back to the platform default, which cannot be told apart from an explicit allow",
    ),
    CommandSpec(
        "app_locale", "locale", "package", True,
        read=lambda ctx: [["cmd", "locale", "get-app-locales", _pkg(ctx), *_user_args(ctx)]],
        parse=parse_locales,
        apply=lambda ctx, value: [["cmd", "locale", "set-app-locales", _pkg(ctx), *_user_args(ctx), "--locales", value]],
        restore=lambda ctx, value: [["cmd", "locale", "set-app-locales", _pkg(ctx), *_user_args(ctx), *(["--locales", value] if value else [])]],
        original_rule=pattern(r"(?:" + _LOCALE_TAG + r"(?:," + _LOCALE_TAG + r"){0,7})?"),
        applied_rule=enum("ar"),
    ),
    CommandSpec(
        "data_saver", "network", "none", False,
        read=lambda ctx: [["cmd", "netpolicy", "get", "restrict-background"]],
        parse=parse_netpolicy,
        apply=lambda ctx, value: [["cmd", "netpolicy", "set", "restrict-background", "true"]],
        restore=lambda ctx, value: [["cmd", "netpolicy", "set", "restrict-background", "true" if value == "enabled" else "false"]],
        original_rule=enum("enabled", "disabled"),
        applied_rule=enum("enabled"),
    ),
    CommandSpec(
        "airplane_mode", "network", "none", False,
        read=lambda ctx: [["settings", "get", "global", "airplane_mode_on"]],
        parse=parse_airplane,
        apply=lambda ctx, value: [["cmd", "connectivity", "airplane-mode", "enable"]],
        restore=lambda ctx, value: [["cmd", "connectivity", "airplane-mode", "enable" if value == "enabled" else "disable"]],
        original_rule=enum("enabled", "disabled"),
        applied_rule=enum("enabled"),
    ),
)
COMMAND_BY_NAME = {item.name: item for item in COMMANDS}


def _set_debug_app(state: str) -> Argv:
    parts = _debug_parts(state)
    if parts is None:
        return ["am", "clear-debug-app"]
    package, wait = parts
    return ["am", "set-debug-app", *(["-w"] if wait == "1" else []), "--persistent", package]


# ---------------------------------------------------------------------------
# Derived applied values
# ---------------------------------------------------------------------------


def derive_stay_awake_usb(original: str | None) -> str | None:
    """Add the USB bit to the charging bitmask; None when the stored mask is not understood."""
    if original is not None and (not re.fullmatch(r"[0-9]{1,2}", original) or int(original) > 15):
        return None
    return str((int(original) if original is not None else 0) | 2)


def derive_font_scale(api: int | None) -> str:
    return "2.0" if api is not None and api >= 34 else "1.3"


def derive_density(extras: dict[str, Any]) -> str | None:
    physical = extras.get("physical")
    if not isinstance(physical, int) or physical <= 0:
        return None
    target = round(physical * 1.15)
    state = f"override:{target}"
    return state if COMMAND_BY_NAME["wm_density"].applied_rule.accepts(state) else None


def derive_debug_app(package: str | None) -> str | None:
    return f"app={package};wait=1" if valid_package(package) else None
