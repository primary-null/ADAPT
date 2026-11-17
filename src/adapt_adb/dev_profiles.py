"""The Developer Options profile registry: one place for names, ranges and gates.

Pure data.  ``dev_options`` applies and restores profiles from this registry, the
CLI lists it, and the MCP schema derives its profile enum from ``PROFILE_NAMES``.

A profile is a short list of steps over the allowlisted entries in
``dev_entries``.  The registry says what a profile needs (an API range, a
package, a USB transport), what it leaves for the person to do (an app restart,
an effect check) and how exactly restore can reproduce the earlier state.
Nothing here proves that an OEM build honours a profile: that is for a device run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from adapt_adb.dev_entries import COMMAND_BY_NAME, PROPERTY_BY_NAME, SETTING_BY_NAME

OEM_LAYERS = ("pixel", "samsung", "other")
MAX_VERIFIED_API = 37
MIN_API = 29


@dataclass(frozen=True)
class StepSpec:
    entry: str                       # name in dev_entries
    kind: str                        # setting | property | command
    value: str | None = None         # fixed applied value; None means derived (see derive)
    derive: str | None = None        # stay_awake_usb | font_scale | density | debug_app
    min_api: int = MIN_API
    max_api: int | None = None
    option: str | None = None        # None: always; otherwise only when this option is chosen
    package: bool = False            # per-app step: needs the package


@dataclass(frozen=True)
class ProfileSpec:
    name: str
    description: str
    steps: tuple[StepSpec, ...]
    min_api: int = MIN_API
    max_api: int | None = MAX_VERIFIED_API
    api_gate: bool = True            # False: legacy profile that never read the API level
    oem: tuple[str, ...] = OEM_LAYERS
    package: str = "none"            # none | optional | required
    options: tuple[str, ...] = ()
    needs_reboot: str = "no"         # no | unknown
    needs_app_restart: bool = False
    needs_effect_check: bool = False
    effect_check: str = ""
    refuse_wireless: bool = False
    oem_caveats: tuple[tuple[str, str], ...] = ()
    notes: str = ""
    omitted: str = ""                # research steps left out, and why

    @property
    def restore_exactness(self) -> str:
        return "approximate" if self.inexact_entries() else "exact"

    def inexact_entries(self) -> tuple[str, ...]:
        return tuple(step.entry for step in self.steps if step.kind == "command" and not COMMAND_BY_NAME[step.entry].exact_restore)


def _s(entry: str, value: str | None = None, **kw: Any) -> StepSpec:
    return StepSpec(entry, "setting", value, **kw)


def _p(entry: str, value: str, **kw: Any) -> StepSpec:
    return StepSpec(entry, "property", value, **kw)


def _c(entry: str, value: str | None = None, **kw: Any) -> StepSpec:
    return StepSpec(entry, "command", value, **kw)


PROFILES: tuple[ProfileSpec, ...] = (
    ProfileSpec(
        "fast-ui", "Turn the three animation scales off.",
        (_s("window_animation_scale", "0"), _s("transition_animation_scale", "0"), _s("animator_duration_scale", "0")),
        api_gate=False, min_api=MIN_API, max_api=None,
        notes="Animations off can hide races and changes what performance numbers mean.",
    ),
    ProfileSpec(
        "touches", "Show touches on the screen.",
        (_s("show_touches", "1"),), api_gate=False, max_api=None,
    ),
    ProfileSpec(
        "stay-awake", "Keep the screen on while USB-powered, preserving other charging modes.",
        (_s("stay_awake", derive="stay_awake_usb"),), api_gate=False, max_api=None,
        notes="Requests screen-on while USB-powered; it does not unlock the phone or beat a device policy.",
    ),
    ProfileSpec(
        "desktop-testing", "Request freeform windows and desktop mode on external displays.",
        (_s("freeform_windows", "1"), _s("force_desktop", "1")),
        needs_reboot="unknown",
        notes="A manual reboot may be needed and the OEM may ignore the settings; no reboot is performed.",
    ),
    ProfileSpec(
        "layout-debug", "Show layout bounds and enable view attribute inspection.",
        (_p("debug.layout", "true"), _s("view_attribute_inspection", "1")),
        needs_app_restart=True, needs_effect_check=True,
        effect_check="Restart the app, then take a screenshot and look for layout bounds.",
        notes="The layout-bounds property reaches a running app only after the app restarts.",
    ),
    ProfileSpec(
        "lifecycle-stress", "Don't keep activities, show background ANRs and the first-crash dialog.",
        (_s("dont_keep_activities", "1"), _s("show_background_anrs", "1"), _s("show_first_crash_dialog", "1")),
        needs_effect_check=True,
        effect_check="Leave the app and come back to it, then look for the activity being recreated in the lifecycle log; "
                     "whether a shell write of the setting takes effect without a reboot is not verified.",
        notes="Settings only: killing the app process and rotating the screen stay separate steps you run yourself.",
        omitted="am kill, send-trim-memory and rotation are one-time actions, not restorable state.",
    ),
    ProfileSpec(
        "performance-capture", "Compile one app with speed-profile and keep the screen on, for comparable numbers.",
        (_c("package_compile", "speed-profile", package=True), _s("stay_awake", derive="stay_awake_usb")),
        package="required",
        oem_caveats=(("samsung", "Game Optimizing Service and Device Care can change performance numbers; ADAPT does not read or change them."),),
        notes="Leave animation scales at 1: fast-ui invalidates frame numbers. Reset frame stats yourself before the run "
              "(dumpsys gfxinfo PACKAGE reset).",
        omitted="Thermal status and Game Optimizing Service reads (low confidence in the research).",
    ),
    ProfileSpec(
        "battery-doze", "Unplug the battery state, force Doze and, with a package, put the app in the rare standby bucket.",
        (_c("battery_state", "unplugged"), _c("doze_state", "idle"), _c("standby_bucket", "rare", package=True)),
        package="optional", refuse_wireless=True,
        oem_caveats=(("samsung", "Sleeping apps and Device Care add background limits ADAPT cannot read."),),
        notes="Turn the screen off first: Doze only holds with the screen off. Use a USB cable; Doze can drop wireless adb.",
        omitted="Battery saver (optional step) is not applied.",
    ),
    ProfileSpec(
        "rtl-large-font", "Force right-to-left layout, the largest font and a larger display density; per-app Arabic locale with a package.",
        (_p("debug.force_rtl", "1"), _s("force_rtl_setting", "1"), _s("font_scale", derive="font_scale"),
         _c("wm_density", derive="density"), _c("app_locale", "ar", package=True, min_api=33)),
        package="optional", needs_app_restart=True, needs_effect_check=True,
        effect_check="Restart the app, then take a screenshot and check that the layout is mirrored and the text is large; "
                     "whether the shell writes flip layouts without the Settings app is not verified.",
        oem_caveats=(("samsung", "Samsung may clamp font_scale below the value ADAPT writes."),),
        notes="The system language cannot be changed from the shell; only the per-app locale (Android 13 and later) can.",
    ),
    ProfileSpec(
        "accessibility-display", "Dark theme, high-contrast text and bold text.",
        (_c("night_mode", "yes"), _s("high_text_contrast", "1"), _s("bold_text", "300", min_api=31),
         _s("daltonizer_mode", "12", option="daltonizer"), _s("daltonizer_enabled", "1", option="daltonizer"),
         _p("debug.hwui.force_dark", "true", option="force-dark")),
        options=("daltonizer", "force-dark"), needs_effect_check=True,
        effect_check="Look at a screenshot: dark theme and bold text show; colour-blind simulation does not appear in screenshots.",
        notes="Bold text needs Android 12 (API 31). The force-dark property reaches a running app only after it restarts.",
    ),
    ProfileSpec(
        "input-visualization", "Show touches and, on Android 15 and later, key presses; optionally pointer location.",
        (_s("show_touches", "1"), _s("show_key_presses", "1", min_api=35), _s("pointer_location", "1", option="pointer-location")),
        options=("pointer-location",), needs_effect_check=True,
        effect_check="Record or screenshot the screen and look for the touch and key indicators.",
    ),
    ProfileSpec(
        "network-degraded", "Limit the download rate (Android 13 and later); optionally turn airplane mode on (Android 11 and later).",
        (_s("ingress_rate_limit", "16000", min_api=33), _c("airplane_mode", "enabled", option="airplane-mode", min_api=30)),
        min_api=30, options=("airplane-mode",), refuse_wireless=True, needs_effect_check=True,
        effect_check="Measure a download: the kernel may ignore the rate limit on some devices.",
        notes="Refused over wireless adb. The rate limit is for downloads only.",
        omitted="Turning Wi-Fi and mobile data off with svc (medium confidence, wireless-cutting).",
    ),
    ProfileSpec(
        "background-restricted", "Restrict one app's background activity and turn Data Saver on.",
        (_c("app_background", "ignore", package=True), _c("data_saver", "enabled")),
        package="required",
        oem_caveats=(("samsung", "Sleeping-app lists add restrictions ADAPT cannot read."),),
        omitted="Revoking runtime permissions (optional step; per-permission state is not saved).",
    ),
    ProfileSpec(
        "debugger-attach", "Select the app as the persistent debug app and make launches wait for a debugger.",
        (_c("debug_app", derive="debug_app", package=True),),
        package="required", needs_effect_check=True,
        effect_check="Launch the app: it should wait for a debugger. The app must be debuggable.",
        notes="Without a restore, the app waits for a debugger at every launch, including after a reboot.",
    ),
)

PROFILE_BY_NAME = {item.name: item for item in PROFILES}
PROFILE_NAMES: tuple[str, ...] = tuple(item.name for item in PROFILES)
LEGACY_PROFILE_NAMES = ("fast-ui", "touches", "stay-awake", "desktop-testing")


def _check_registry() -> None:
    names = [item.name for item in PROFILES]
    assert len(names) == len(set(names))
    for profile in PROFILES:
        for step in profile.steps:
            table = {"setting": SETTING_BY_NAME, "property": PROPERTY_BY_NAME, "command": COMMAND_BY_NAME}[step.kind]
            assert step.entry in table, (profile.name, step.entry)
            assert step.option is None or step.option in profile.options


_check_registry()


def step_applies(step: StepSpec, api: int | None, options: tuple[str, ...], package: str | None) -> str | None:
    """None when the step applies, otherwise why it is skipped."""
    if step.option is not None and step.option not in options:
        return "option_not_selected"
    if api is not None and api < step.min_api:
        return f"needs_api_{step.min_api}"
    if api is not None and step.max_api is not None and api > step.max_api:
        return f"not_after_api_{step.max_api}"
    if step.package and package is None:
        return "needs_package"
    return None


def select_steps(profile: ProfileSpec, api: int | None, options: tuple[str, ...], package: str | None) -> list[StepSpec]:
    return [step for step in profile.steps if step_applies(step, api, options, package) is None]


def registry_entry(profile: ProfileSpec) -> dict[str, Any]:
    """The JSON-able description of one profile: what a schema, a CLI or an agent needs."""
    return {
        "name": profile.name,
        "description": profile.description,
        "min_api": profile.min_api if profile.api_gate else None,
        "max_api": profile.max_api if profile.api_gate else None,
        "api_gated": profile.api_gate,
        "oem": list(profile.oem),
        "oem_caveats": {oem: text for oem, text in profile.oem_caveats},
        "package": profile.package,
        "package_required": profile.package == "required",
        "options": list(profile.options),
        "mechanisms": sorted({step.kind for step in profile.steps}),
        "steps": [
            {"entry": s.entry, "kind": s.kind, "min_api": s.min_api, "option": s.option, "package": s.package}
            for s in profile.steps
        ],
        "needs_reboot": profile.needs_reboot,
        "needs_app_restart": profile.needs_app_restart,
        "needs_effect_check": profile.needs_effect_check,
        "effect_check": profile.effect_check or None,
        "refuses_wireless_adb": profile.refuse_wireless,
        "restore_exactness": profile.restore_exactness,
        "approximate_restore": list(profile.inexact_entries()),
        "notes": profile.notes or None,
        "omitted_steps": profile.omitted or None,
    }


def profile_registry() -> list[dict[str, Any]]:
    return [registry_entry(item) for item in PROFILES]


def evaluate(
    profile: ProfileSpec,
    *,
    api: int | None,
    oem: str | None,
    transport: str,
    package: str | None = None,
    options: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Eligibility of one profile on one device, from facts already read.

    ``reasons`` hold hard blockers; ``warnings`` hold caveats that do not block.
    A missing package is reported through ``needs_package`` so a listing can say
    what to pass, instead of marking the profile ineligible.
    """
    reasons: list[dict[str, str]] = []
    warnings: list[str] = []
    if profile.api_gate:
        if api is None:
            reasons.append({"code": "api_unknown", "text": "The Android API level could not be read."})
        elif api < profile.min_api:
            reasons.append({"code": "api_too_low", "text": f"Needs Android API {profile.min_api} or later; the device reports {api}."})
        elif profile.max_api is not None and api > profile.max_api:
            reasons.append({"code": "api_not_researched", "text": f"ADAPT has researched this only up to API {profile.max_api}; the device reports {api}."})
    if oem is not None and oem not in profile.oem:
        reasons.append({"code": "oem_not_supported", "text": f"Not applicable to the detected maker layer ({oem})."})
    if profile.refuse_wireless and transport == "tcp":
        reasons.append({"code": "wireless_transport", "text": "This profile can cut the network; it is refused over a TCP or wireless-debugging transport. Use USB."})
    unknown = [name for name in options if name not in profile.options]
    if unknown:
        reasons.append({"code": "unknown_option", "text": f"Unknown option: {', '.join(unknown)}."})
    if package is not None and profile.package == "none":
        reasons.append({"code": "package_not_used", "text": "This profile does not use a package."})
    probe_package = package or ("com.example.app" if profile.package != "none" else None)
    if not reasons and profile.steps and not select_steps(profile, api, tuple(options), probe_package):
        reasons.append({"code": "no_steps_apply", "text": "No step of this profile applies on this device with these options."})
    if oem is not None:
        caveat = dict(profile.oem_caveats).get(oem)
        if caveat:
            warnings.append(caveat)
    skipped = [
        {"entry": step.entry, "reason": why}
        for step in profile.steps
        if (why := step_applies(step, api, tuple(options), package)) is not None and why != "option_not_selected"
    ]
    return {
        "name": profile.name,
        "eligible": not reasons,
        "reasons": reasons,
        "warnings": warnings,
        "needs_package": profile.package == "required" and package is None,
        "package": profile.package,
        "skipped_steps": skipped,
    }
