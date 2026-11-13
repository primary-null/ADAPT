#!/usr/bin/env python3
"""Build the ADAPT public visual showcase (assets + standalone HTML).

Run from anywhere:

    python scripts/build_showcase.py            # capture, render, write, validate
    python scripts/build_showcase.py --verify   # validate existing outputs only

What it produces (all paths relative to the repository root):

    docs/assets/adapt-title.gif                       animated title (branding asset)
    docs/assets/adapt-title.png / adapt-title.svg     static fallbacks of the same title
    docs/assets/showcase-overview.png                 README contact sheet of the 10 views
    docs/assets/cli-android-versions.txt              ACTUAL stdout of an offline CLI command
    docs/assets/cli-android-versions.provenance.json  command / output hash / source hash
    docs/assets/cli-android-versions-rendered.png/.svg  rendered capture of that stdout
    docs/assets/showcase-manifest.json                size + sha256 of every generated asset
    docs/showcase/index.html                          self-contained interactive page

Honesty rules baked into this script:

* Exactly one of the ten views is real: the stdout of
  ``adapt --no-banner --no-tips android versions`` (offline, no device, no
  network), captured by running the CLI from this checkout's ``src`` directory.
* The other nine are generated, source-derived illustrations. They call the
  real rendering/planning functions in ``src/adapt_adb`` with synthetic inputs
  where possible, and no host, device or network is touched to make them.
* "9 of 10" is a count of equal showcase views, not a pixel measurement.
* The rendered capture images are drawn by this script; they are not
  Windows Terminal (or any terminal) screenshots.
* No Android screenshots are used anywhere.

Only Pillow (already an optional dependency of the ``mcp`` extra) and the
project's own dependencies are needed. Nothing is downloaded.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import html
import importlib.util
import json
import math
import os
import re
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
ASSETS = REPO / "docs" / "assets"
SHOWCASE = REPO / "docs" / "showcase"
sys.path.insert(0, str(SRC))

try:
    from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont
except ImportError as exc:  # pragma: no cover - environment guidance
    raise SystemExit("Pillow is required: pip install Pillow  (or: pip install -e '.[mcp]')") from exc

GENERATOR = "scripts/build_showcase.py"
CAPTURE_ARGV = ["adapt", "--no-banner", "--no-tips", "android", "versions"]
CAPTURE_DISPLAY = " ".join(CAPTURE_ARGV)
CAPTURE_STEM = "cli-android-versions"
COLS = 70  # terminal width used for every generated view
VERSION = "0.1.0"

# --------------------------------------------------------------------------
# Real source modules (public checkout only, imported via ./src)
# --------------------------------------------------------------------------
from adapt_adb import android_support, banner, console, dev_options, rapid, terminal_ui  # noqa: E402
from adapt_adb import tool_catalog  # noqa: E402


def _load_access_policy():
    """Load mcp/access.py without importing the optional ``mcp`` dependency."""

    path = SRC / "adapt_adb" / "mcp" / "access.py"
    spec = importlib.util.spec_from_file_location("adapt_showcase_mcp_access", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


mcp_access = _load_access_policy()


# --------------------------------------------------------------------------
# Palette (ANSI-equivalent colours used by the terminal views)
# --------------------------------------------------------------------------
PAL = {
    "t": "#c9d4ce",   # default terminal text
    "w": "#ffffff",   # emphasis
    "d": "#7f8c85",   # dim / SKIP
    "g": "#4be37a",   # READY
    "y": "#e8c14a",   # WAITING
    "r": "#f06a6a",   # BLOCKED
    "pg": "#87ff87",  # pale green headings (ANSI 120)
    "L0": "#00ff00",  # banner A  (ANSI 38;5;46)
    "L1": "#87ff87",  # banner D  (ANSI 38;5;120)
    "L2": "#ffffff",  # banner A  (ANSI 97)
    "L3": "#cccccc",  # banner P  (ANSI 37)
    "L4": "#808080",  # banner T  (ANSI 90)
}
T_BG = "#0b110e"
T_BAR = "#121b16"
ACCENT = "#1c8c4e"
SGR = {"31": "r", "32": "g", "33": "y", "90": "d", "37": "L3", "97": "L2",
       "38;5;46": "L0", "38;5;120": "L1"}


def rgb(hexstr: str) -> tuple[int, int, int]:
    h = hexstr.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


# --------------------------------------------------------------------------
# Row model: a terminal line = coloured segments + highlight tags
# --------------------------------------------------------------------------
@dataclass
class Row:
    segs: list[tuple[str, str]]
    tags: tuple[str, ...] = ()

    @property
    def plain(self) -> str:
        return "".join(text for text, _ in self.segs)


_MARK = re.compile(r"@(\w+)\{([^}]*)\}")


def M(markup: str, *tags: str) -> Row:
    """Build a Row from ``text @g{coloured} text`` markup."""

    segs: list[tuple[str, str]] = []
    pos = 0
    for m in _MARK.finditer(markup):
        if m.start() > pos:
            segs.append((markup[pos:m.start()], "t"))
        segs.append((m.group(2), m.group(1)))
        pos = m.end()
    if pos < len(markup):
        segs.append((markup[pos:], "t"))
    return Row(segs, tuple(tags))


def T(text: str, cls: str = "t", *tags: str) -> Row:
    return Row([(text, cls)] if text else [], tuple(tags))


BLANK = T("")


def ansi_row(line: str, *tags: str) -> Row:
    segs: list[tuple[str, str]] = []
    cls, pos = "t", 0
    for m in re.finditer(r"\x1b\[([0-9;]*)m", line):
        if m.start() > pos:
            segs.append((line[pos:m.start()], cls))
        code = m.group(1)
        cls = "t" if code in ("", "0") else SGR.get(code, cls)
        pos = m.end()
    if pos < len(line):
        segs.append((line[pos:], cls))
    return Row(segs, tuple(tags))


@contextlib.contextmanager
def color_env():
    """Let the real renderers emit ANSI colour (they honour NO_COLOR / TERM=dumb)."""

    saved = {k: os.environ.pop(k, None) for k in ("NO_COLOR", "TERM", "ADAPT_NO_BANNER", "ADAPT_BANNER")}
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value


def tag_rows(rows: list[Row], rules: list[tuple[str, str]]) -> list[Row]:
    """Attach tags to rows whose plain text matches a regex."""

    out = []
    for row in rows:
        tags = list(row.tags)
        for pattern, tag in rules:
            if re.search(pattern, row.plain) and tag not in tags:
                tags.append(tag)
        out.append(Row(row.segs, tuple(tags)))
    return out


# --------------------------------------------------------------------------
# View model
# --------------------------------------------------------------------------
@dataclass
class View:
    n: int
    key: str
    short: str
    title: str
    kind: str  # "generated" | "actual"
    command: str
    blurb: str
    rows: list[Row]
    highlights: list[tuple[str, str, str]]
    sources: list[str]
    boundary: str
    extra: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# Banner data (single source of truth: src/adapt_adb/banner.py)
# --------------------------------------------------------------------------
MASK = banner._LOGO_MASK
SHADES = banner._SHADES
SUBTITLE = banner._SUBTITLE
LETTER_CLASSES = ("L0", "L1", "L2", "L3", "L4")
LETTER_RGB = [rgb(PAL[c]) for c in LETTER_CLASSES]
LOGO_COLS = 5 * 5 + 4 * 2
FILLED = sum(row[i].count("#") for row in MASK for i in range(5))
CELLS = 5 * 5 * 5
assert (FILLED, CELLS) == (85, 125), "banner mask changed; update showcase copy"
FILL_PERCENT = round(100 * FILLED / CELLS)
assert FILL_PERCENT == 68
assert [c for c in banner._LETTER_COLORS] == [
    "\x1b[38;5;46m", "\x1b[38;5;120m", "\x1b[97m", "\x1b[37m", "\x1b[90m"
], "banner letter colours changed; update PAL / SGR"
assert tuple(SHADES) == ("█", "▓", "█", "▓", "▒")


def logo_plain_lines() -> list[str]:
    lines = []
    for row in MASK:
        parts = [seg.replace("#", SHADES[i]) for i, seg in enumerate(row)]
        lines.append("  ".join(parts))
    return lines


assert logo_plain_lines() == banner._render_logo(color=False, use_unicode=True), "logo drifted from banner.py"


# --------------------------------------------------------------------------
# Views 1-9: generated, source-derived illustrations (synthetic values)
# --------------------------------------------------------------------------
DEMO_SERIAL = "DEMO-PIXEL-7A2F"


def view_home() -> View:
    with color_env():
        art = banner.render_banner(width=COLS, color=True, encoding="utf-8")
    rows = [ansi_row(line, *(("mask", "fade") if i < 5 else ())) for i, line in enumerate(art.splitlines())]
    plain = [r.plain for r in rows]
    with color_env():
        reference = banner.render_banner(width=COLS, color=False, encoding="utf-8").splitlines()
    assert plain == reference, "banner rows drifted from render_banner()"
    rows += [BLANK, T("ADAPT / MAIN MENU", "w"), BLANK,
             T("Choose an area. Supply a device explicitly when running its command.", "t", "nav")]
    for i, (name, _cmds) in enumerate(terminal_ui.AREAS, 1):
        rows.append(M(f"@g{{[{i}]}} {name}", "areas", *(("rapid",) if name == "Rapid ADAPT" else ())))
    rows += [BLANK, T("Page 1/1", "d"), M("[H] Home   [B] Back   [N/P] Page   [Q] Exit", "nav"),
             M("ADAPT > @w{_}")]
    return View(
        1, "home", "Home", "Home screen with the original banner", "generated", "adapt console",
        "The console's main menu under the real green-white-gray banner (static in the terminal).",
        rows,
        [
            ("mask", f"{FILL_PERCENT}% fill mask",
             f"{FILLED} of {CELLS} cells are filled across five 5x5 letter masks. Computed from the mask in banner.py when this page is built."),
            ("fade", "Green, white, gray",
             "Letter colors are ANSI 46, 120, 97, 37 and 90 with shades \u2588 \u2593 \u2588 \u2593 \u2592. The terminal banner is static; only this page animates the title."),
            ("areas", "Nine areas",
             "The list is read from AREAS in terminal_ui.py. Rapid ADAPT is area 7 of the same application."),
            ("nav", "Nothing runs on open",
             "Opening menus performs no host or device check and installs nothing (docs/console.md)."),
        ],
        ["src/adapt_adb/banner.py", "src/adapt_adb/terminal_ui.py", "docs/console.md"],
        "Illustration of one screen. Real terminals size the banner to their width and honor NO_COLOR, TERM=dumb and --no-banner.",
    )


def view_doctor() -> View:
    report = {
        "status": "device_unauthorized_or_no_permissions",
        "exit_code": 2,
        "host": {"os": "Windows 11", "arch": "x86_64"},
        "toolchain": {
            "platform_tools_version": "35.0.2",
            "adb_path": r"%LOCALAPPDATA%\Android\platform-tools\adb.exe",
            "origin": "google_official",
        },
        "checks": [
            {"id": "adb_server_5037", "status": "pass", "summary": "port 5037 free or owned by this user; versions match"},
            {"id": "adb_path", "status": "pass", "summary": "adb found on PATH"},
            {"id": "adb_version", "status": "pass", "summary": "platform-tools 34.0.0 or newer"},
            {"id": "adb_origin", "status": "pass", "summary": "Google official distribution"},
            {"id": "path_config", "status": "pass", "summary": "no shadowing adb earlier on PATH"},
            {"id": "android_home", "status": "pass", "summary": "left untouched"},
            {"id": "windows_pnp", "status": "pass", "summary": "no USB driver problem reported"},
            {"id": "debian_usb_permissions", "status": "skip", "summary": "Linux-only check"},
            {"id": "devices_state", "status": "warn", "summary": "1 transport is unauthorized"},
        ],
        "devices": {"by_state": {"device": 0, "unauthorized": 1, "offline": 0, "no_permissions": 0, "other": 0}},
    }
    actions = [{"id": "recheck", "key": "1", "label": "Run checks again"},
               {"id": "done", "key": "q", "label": "Exit"}]
    with color_env():
        text = console.render_console_report(report, actions, width=COLS, color=True)
    rows = [ansi_row(line) for line in text.splitlines()]
    rows = tag_rows(rows, [
        (r"^\s+Status: ", "ladder"),
        (r"^\s+(READY|WAITING|BLOCKED|SKIP)\s", "states"),
        (r"ADB daemon", "daemon"),
        (r"^Next:|^\s+\[[1q]\]", "next"),
    ])
    return View(
        2, "doctor", "Host doctor", "Host check and preflight (illustrative)", "generated", "adapt doctor",
        "The real console report renderer fed a synthetic report, so the layout and state words are the product's own.",
        rows,
        [
            ("ladder", "Exit-code ladder",
             "3 unsafe shared server, then 1 missing toolchain, then 2 device unauthorized, then 0 ok. The highest-precedence failure wins (docs/doctor.md)."),
            ("states", "Four states",
             "READY, WAITING, BLOCKED and SKIP. Status is always text; color only adds emphasis (console.py)."),
            ("daemon", "Daemon checked first",
             "Port 5037 ownership and version are evaluated before adb devices -l, so a mismatch can never trigger a client auto-kill."),
            ("next", "Next actions",
             "Interactive terminals get a numbered Next menu. --json and pipes never prompt."),
        ],
        ["src/adapt_adb/console.py", "src/adapt_adb/doctor.py", "docs/doctor.md"],
        "Synthetic report. No host check ran; paths are the documented %LOCALAPPDATA% location, not a real machine.",
    )


def view_dev_options() -> View:
    values = dev_options._PROFILE_VALUES
    by_name = dev_options._SETTING_BY_NAME
    assert values["fast-ui"] == {k: "0" for k in ("window_animation_scale", "transition_animation_scale", "animator_duration_scale")}
    assert values["touches"] == {"show_touches": "1"}
    assert values["stay-awake"] == {"stay_awake": "2"}
    assert values["desktop-testing"] == {"freeform_windows": "1", "force_desktop": "1"}
    assert by_name["stay_awake"].key == "stay_on_while_plugged_in"
    assert by_name["show_touches"].key == "show_touches"
    assert by_name["freeform_windows"].key == "enable_freeform_support"
    assert by_name["force_desktop"].key == "force_desktop_mode_on_external_displays"
    n_keys = len(dev_options.SETTINGS)
    rows = [
        M(f"@w{{ADAPT / DEV-OPTIONS}}  |  4 bounded profiles"),
        T("-" * 64, "d"),
        M(f"Target  -s {DEMO_SERIAL}  @d{{(synthetic; serial always explicit)}}", "identity"),
        M("Bound   ro.serialno + build fingerprint + current Android user", "identity"),
        BLANK,
        M(f"@pg{{PROFILE}}          @pg{{WRITES}}                 @d{{(inspect reads {n_keys} keys)}}"),
        M("fast-ui          3 animation scales -> 0", "profiles"),
        M("touches          system show_touches -> 1", "profiles"),
        M("stay-awake       stay_on_while_plugged_in |= 2 (keeps AC bits)", "profiles"),
        M("desktop-testing  enable_freeform_support -> 1", "profiles"),
        M("                 force_desktop_mode_on_external_displays -> 1", "profiles"),
        BLANK,
        M("@pg{RECOVERY RECORD}  new file, created exclusively before 1st write", "snapshot"),
        M("  holds: identity, each original value (or 'absent'), intent", "snapshot"),
        M("  existing path / dangling symlink is never overwritten", "snapshot"),
        BLANK,
        M("@pg{LIFECYCLE}", "restore"),
        M("  prepared -> applying -> applied   (every write read back)", "restore"),
        M("  applying -> partial_failure       (rollback keeps the record)", "restore"),
        M("  applied -> restoring -> restored", "restore"),
        M("  changed meanwhile -> @y{restore_conflict}, not overwritten", "restore"),
        BLANK,
        M(f"$ adapt dev-options apply --profile stay-awake -s {DEMO_SERIAL} \\", "snapshot"),
        M("      --snapshot snapshots/stay-awake.json", "snapshot"),
    ]
    return View(
        3, "dev-options", "Developer profiles", "Developer Options profiles with recovery (illustrative)",
        "generated", "adapt dev-options apply --profile ...",
        "Four fixed profiles and the snapshot lifecycle that makes each one reversible.",
        rows,
        [
            ("profiles", "Fixed profiles only",
             "Profile values are asserted against dev_options._PROFILE_VALUES at build time. ADAPT writes no other keys and no properties."),
            ("identity", "Exact target",
             "A serial is required and physical identity is read separately; a USB serial or wireless endpoint is never treated as identity."),
            ("snapshot", "Snapshot first",
             "The recovery file is created exclusively with the originals before the first device write."),
            ("restore", "Conflict-safe restore",
             "Restore refuses to overwrite a value changed by something else and retains the record (docs/dev-options.md)."),
        ],
        ["src/adapt_adb/dev_options.py", "docs/dev-options.md", "docs/rapid-adapt.md"],
        "Illustration of the contract. OEM firmware may ignore a setting; readback shows the requested value, not effective behavior.",
    )


def view_tools() -> View:
    catalog = tool_catalog.TOOL_CATALOG
    goals = tool_catalog.GOALS
    rows = [
        M(f"@w{{ADAPT / TOOLS}}  |  {len(catalog)} records, {len(goals)} goals"),
        T("-" * 64, "d"),
        T("Detection is a PATH lookup only; no catalogued tool is executed.", "t", "detect"),
        BLANK,
        M("@pg{GOAL}              @pg{TOOLS}", "goals"),
    ]
    for goal, ids in goals.items():
        wrapped = textwrap.wrap(", ".join(ids), width=COLS - 18)
        for i, chunk in enumerate(wrapped):
            rows.append(T(f"{goal:<18}{chunk}" if i == 0 else f"{'':<18}{chunk}", "t", "goals"))
    hosts = [
        ("windows-native", "x86_64", "Windows native"),
        ("wsl", "x86_64", "WSL"),
        ("linux-native", "x86_64", "Linux"),
        ("linux-native", "arm64", "Linux ARM64"),
    ]
    rows += [BLANK, M("@pg{BUILD HOST CHECK}  goal aosp-build, synthetic hosts", "hosts")]
    for kind, arch, label in hosts:
        env = tool_catalog.HostEnvironment(os="synthetic", kind=kind, arch=arch, kernel_release="", label=label, is_wsl=(kind == "wsl"))
        cls = tool_catalog._host_assessment("aosp-build", env)["classification"]
        color = "y" if cls in {"qualified_unverified", "requires_upstream_review"} else "r"
        rows.append(M(f"  {kind:<15}{arch:<8}@{color}{{{cls}}}", "hosts"))
    rows += [
        BLANK,
        M("@pg{ACQUISITION}  automatic: platform-tools, scrcpy (pinned)", "acq"),
        T(f"  the other {len(catalog) - 2} records: official link, you install", "t", "acq"),
        BLANK,
        M("$ adapt tools plan lineage-build", "lineage"),
        M("  @y{# incomplete, exit 2: --branch and --device are both required}", "lineage"),
    ]
    return View(
        4, "tools", "Tool catalog and ROM plans", "Tool catalog and build-host plans (illustrative)",
        "generated", "adapt tools list | plan GOAL",
        "Goals map to catalogued tools; build goals classify the host and fail closed.",
        rows,
        [
            ("goals", "Goals from the catalog",
             "Goal and tool lists are read from tool_catalog.GOALS. ADAPT is not a universal ROM builder or flasher."),
            ("detect", "Presence is not trust",
             "Detection never runs a discovered program, so 'found' proves neither version, publisher nor safety."),
            ("hosts", "Hosts fail closed",
             "Classifications come from the real _host_assessment() for four synthetic hosts. Native Windows is unsupported for AOSP builds; Linux needs upstream review."),
            ("lineage", "Context required",
             "A lineage-build plan without both a branch and a device is incomplete; supplied values stay user_supplied_unverified."),
        ],
        ["src/adapt_adb/tool_catalog.py", "docs/tools.md"],
        "Synthetic hosts only. Nothing was looked up on this machine and no tool was downloaded.",
    )


def view_android_matrix() -> View:
    versions = android_support.ANDROID_VERSIONS
    apis = [v["api"] for v in versions]
    short = {
        "usb_adb": "USB ADB",
        "wireless_pairing": "Pairing wireless ADB",
        "adb_wifi_2": "ADB Wi-Fi 2.0",
        "legacy_tcp_adb": "Legacy USB-first TCP",
        "screenshot": "ADB screenshot",
        "uiautomator": "UI Automator",
        "scrcpy_audio": "scrcpy audio",
        "existing_display_mirroring": "Mirror existing display",
        "new_virtual_display": "New virtual display",
        "input_routing": "Input to other displays",
        "developer_options_keys": "Developer Options keys",
    }
    evals = {api: android_support.evaluate_capabilities(api)["capabilities"] for api in apis}
    glyph = {"version_eligible": ("E", "g"), "unknown_oem_dependent": ("?", "y"),
             "unsupported_by_os": ("-", "d")}
    head_api = "".join(f"{a:>4}" for a in apis)
    head_and = "".join(f"{v['android']:>4}" for v in versions)
    rows = [
        M(f"@w{{ADAPT / ANDROID}}  |  eligibility by API level (offline)"),
        T("-" * 64, "d"),
        M(f"@pg{{{'API':<28}}}{head_api}", "axis"),
        M(f"@d{{{'Android':<28}}}{head_and}", "axis"),
    ]
    for feat in android_support._FEATURES:
        fid = feat["id"]
        cells = ""
        for api in apis:
            ch, cls = glyph[evals[api][fid]["status"]]
            cells += f"@{cls}{{{ch:>4}}}"
        tag = "oem" if feat["default"] == "unknown_oem_dependent" else "floor"
        rows.append(M(f"{short[fid]:<28}{cells}", tag))
    rows += [
        BLANK,
        M("@g{E} version-eligible   @y{?} OEM / runtime dependent   @d{-} below API floor"),
        T("No device observed: eligibility is not acceptance on any build.", "t", "caveat"),
        T("Android 12 has two API levels (31 and 32 = 12L).", "d", "caveat"),
    ]
    return View(
        5, "matrix", "Android matrix", "Android capability matrix (illustrative)", "generated",
        "adapt android capabilities -s SERIAL",
        "Each cell is computed by the real evaluate_capabilities() for an API level with no device evidence.",
        rows,
        [
            ("floor", "API floors",
             "Pairing-based wireless ADB starts at API 30 and ADB Wi-Fi 2.0 at API 37 (Android 17), from the feature table in android_support.py."),
            ("oem", "OEM-dependent rows",
             "Display, input and Developer Options rows stay '?' because OEM build, permissions and runtime state decide the result."),
            ("caveat", "Not certification",
             "The matrix never produces observed_runtime_evidence from an SDK number alone; only caller-supplied evidence can."),
        ],
        ["src/adapt_adb/android_support.py", "docs/android-support.md"],
        "Version eligibility only. ADAPT has not run native acceptance tests on every Android version or OEM build.",
    )


def view_tailscale() -> View:
    rows = [
        M("@w{ADAPT / TAILSCALE}  |  direct Android ADB, by stage"),
        T("-" * 64, "d"),
        T("Placeholders only: PHONE, PAIR_PORT, CONNECT_PORT.", "d"),
        BLANK,
        M("@g{1 status}   tailscale status | peers | guide", "stages"),
        T("           cached local daemon view; 'online' is not a service check", "d", "proves"),
        M("@g{2 plan}     tailscale plan --peer PHONE --port CONNECT_PORT", "stages"),
        T("           offline; requires no credential", "d", "proves"),
        M("@g{3 pair}     tailscale pair --peer PHONE --port PAIR_PORT", "stages", "ports"),
        T("           code at a hidden prompt or --code-stdin, never an argument", "d", "proves"),
        M("@g{4 connect}  tailscale connect --peer PHONE --port CONNECT_PORT", "stages", "ports"),
        T("           'verified' only after get-state, serial/build/user ids,", "d", "proves"),
        T("           model, SDK and a TLS listener on the requested port", "d", "proves"),
        M("@g{5 check}    tailscale check --peer PHONE --port CONNECT_PORT", "stages"),
        M("@g{6 use}      tailscale adb ... -- <one guarded device command>", "stages"),
        BLANK,
        M("@pg{PORTS}   PAIR_PORT is temporary; CONNECT_PORT is on the main", "ports"),
        T("        Wireless debugging screen. Re-read both before each attempt.", "t", "ports"),
        M("@y{NOT PROVEN}  peer online | tcp_open | paired == connected", "proves"),
        T("             pairing authorizes a key; it is not a transport", "d", "proves"),
        BLANK,
        M("Direct tailnet ADB: @y{NOT CONFIRMED} end to end", "gap"),
        T("  (the current live verifier has a gap; this is the documented flow)", "d", "gap"),
    ]
    return View(
        6, "tailscale", "Tailscale stages", "Tailscale connection stages (documented, not proven)", "generated",
        "adapt tailscale plan | pair | connect | check",
        "The six documented stages, what each one proves, and what it deliberately does not.",
        rows,
        [
            ("stages", "Six stages",
             "Commands and order follow docs/tailscale.md. ADAPT uses an existing Tailscale client and never edits VPN or tailnet policy."),
            ("ports", "Two ports",
             "Pairing and connection use different ports. Both change, so ADAPT never reuses or guesses them."),
            ("proves", "What a result proves",
             "Each status is an observation: peer online, TCP open, key paired and transport verified are four separate facts."),
            ("gap", "Not confirmed",
             "Direct tailnet ADB has not been confirmed end to end because the current live verifier has a gap. This view shows the plan, not a pass."),
        ],
        ["src/adapt_adb/tailscale_mode.py", "docs/tailscale.md"],
        "Illustration of the documented flow with placeholders. No peer, port or pairing code exists here; nothing was probed.",
    )


def view_dex() -> View:
    rows = [
        M("@w{ADAPT / DEX PC}  |  @y{EXPERIMENTAL}  Samsung Android 13-17 planning"),
        T("-" * 64, "d"),
        M("@g{1 pc-plan}     offline plan for --method loopback | overlay", "flow"),
        M("@g{2 pc-start}    needs --yes, shared-ADB preflight, NEW snapshot", "flow"),
        M("@g{3 gates}       scrcpy starts only when all of these hold:", "gates"),
        T("             - Samsung desktop state enabled / dual display", "t", "gates"),
        T("             - a newly created non-zero external display matches", "t", "gates"),
        T("             - overlay method: an overlay: display identity", "t", "gates"),
        T("             display 0, blank or unconfirmed = failed activation", "d", "gates"),
        M("@g{4 pc-status}   observe the supervised session", "flow"),
        M("@g{5 pc-stop}     supervised cleanup", "flow"),
        M("@g{6 pc-restore}  identity, current value and readback checked", "flow"),
        BLANK,
        M("@pg{METHODS}", "methods"),
        T("  loopback  source-bundled helper, compiled locally", "t", "methods"),
        T("            (scripts/build_dex_bridge.py, manual build)", "d", "methods"),
        T("  overlay   overlay_display_devices=WxH/DPI,hdmi", "t", "methods"),
        BLANK,
        M("@pg{NATIVE WINDOWS RECEIVER}  native/windows-receiver (Rust)", "native"),
        T("  source included, manual build; 15 unit tests in source", "t", "native"),
        T("  history: one Android 13 reconnect observed, not universal", "y", "native"),
        T("  experimental source: manual launch; no CLI backend", "d", "native"),
    ]
    return View(
        7, "dex", "DeX native (experimental)", "Samsung DeX for PC, native receiver (experimental)", "generated",
        "adapt dex pc-plan | pc-start | pc-stop",
        "The conservative activation flow and the evidence gates that decide whether a desktop is real.",
        rows,
        [
            ("flow", "Plan, start, restore",
             "pc-start refuses without --yes, a shared-ADB preflight and an exclusively created snapshot bound to the device identity."),
            ("gates", "Evidence gates",
             "A generic or blank display is a failed activation, never a working DeX desktop. Display 0 and manufacturer text prove nothing (docs/dex-pc.md)."),
            ("methods", "Two methods",
             "Loopback needs Samsung API availability and vendor permission, which ADAPT never grants. Overlay requests a simulated display."),
            ("native", "Experimental receiver",
             "15 native tests exist in source. One Android 13 reconnect was observed historically; this is not support for every Android version or device."),
        ],
        ["src/adapt_adb/dex_pc.py", "native/windows-receiver/src", "scripts/build_dex_bridge.py", "docs/dex-pc.md", "docs/dex.md"],
        "Experimental. No DeX session ran to make this view and Windows receiver launch is not part of the shipped CLI.",
    )


def view_rapid() -> View:
    steps = rapid.plan("DEMO", package="com.example.app", session_dir=PurePosixPath("s"), fast_ui=False)
    assert steps["device_access"] is False and steps["commands_executed"] == [] and steps["files_created"] == []
    rows = [M("@w{ADAPT / RAPID ADAPT OPTION / TEST LOOP / PLAN ONLY}", "noexec")]
    for step in steps["steps"]:
        head = f"{step['stage'].upper()} / {step['purpose']}"
        wrapped = textwrap.wrap(head, width=COLS, subsequent_indent="  ")
        rows.append(BLANK)
        for i, chunk in enumerate(wrapped):
            if i == 0:
                stage = step["stage"].upper()
                rows.append(Row([(stage, "g"), (chunk[len(stage):], "t")], ("stages",)))
            else:
                rows.append(T(chunk, "t", "stages"))
        for argv in step["commands"]:
            rendered = " ".join(argv)
            wrapped = textwrap.wrap(rendered, width=COLS - 2, subsequent_indent="    ", break_long_words=False)
            for i, chunk in enumerate(wrapped):
                rows.append(T("  " + chunk + (" \\" if i < len(wrapped) - 1 else ""), "t", "cmds"))
    rows += [BLANK, T("Nothing is executed or created by the plan. Each step is yours.", "y", "noexec")]
    return View(
        8, "rapid", "Rapid ADAPT plan", "Rapid ADAPT integrated test plan (illustrative)", "generated",
        "adapt rapid plan -s SERIAL --package PKG",
        "The check, prepare, reproduce, capture, restore loop, produced by the real plan() with a synthetic target.",
        rows,
        [
            ("stages", "Five stages",
             "check, prepare, reproduce, capture, restore. Rapid ADAPT is an option inside ADAPT, not a separate tool."),
            ("cmds", "Real command shapes",
             "The commands come from rapid.plan() with a synthetic serial; copy them only after replacing it with your own."),
            ("noexec", "Plan only",
             "The plan reports device_access false, no commands executed and no files created. It is not a session supervisor."),
        ],
        ["src/adapt_adb/rapid.py", "docs/rapid-adapt.md"],
        "Synthetic serial DEMO and package com.example.app. Fast UI is opt-in and omitted here because it can hide animation bugs.",
    )


def view_mcp_ssh() -> View:
    access = mcp_access
    observation = sorted(access.OBSERVATION_TOOLS)
    mutating = sorted(access.MUTATING_TOOLS)
    assert len(observation) == 19 and len(mutating) == 5
    rows = [
        M(f"@w{{$}} adapt mcp --read-only --device {DEMO_SERIAL}", "stdio"),
        BLANK,
        M("AI client  @g{--stdio JSON-RPC-->}  adapt mcp   local, no network port", "stdio"),
        T("                                  |", "d", "stdio"),
        M("                       @pg{AccessPolicy} (server side)", "policy"),
        M(f"  --read-only   exposes {len(observation)} observation tools,", "policy"),
        T(f"                hides the {len(mutating)} that can change a device", "t", "policy"),
        T("  --device ID   device tools accept only that exact transport;", "t", "policy"),
        T("                host-wide doctor is hidden", "t", "policy"),
        BLANK,
        M("@pg{hidden when read-only}  " + " ".join(mutating), "policy"),
        M("@pg{observation tools}", "tools"),
    ]
    for chunk in textwrap.wrap(" ".join(observation), width=COLS - 4):
        rows.append(T("  " + chunk, "t", "tools"))
    rows += [
        BLANK,
        M("@pg{SSH WORKER}  adapt ssh run android-worker --operation OP", "ssh"),
        T("  OP: doctor scan inspect android desktop dex tools", "t", "ssh"),
        T("  no remote shell, install or device mutation is exposed", "t", "ssh"),
        T("  the worker owns ADB and USB; this PC needs only OpenSSH", "d", "ssh"),
    ]
    return View(
        9, "mcp-ssh", "MCP and SSH access", "MCP and SSH worker access layers (illustrative)", "generated",
        "adapt mcp --read-only --device SERIAL",
        "Where the server-side restrictions sit between an AI client and the tools.",
        rows,
        [
            ("stdio", "Local stdio only",
             "An AI client launches adapt mcp as a subprocess. There is no HTTP service, and no banner or tips are written to protocol output."),
            ("policy", "Server-side policy",
             "Restrictions live in AccessPolicy, independent of a client's own prompts. Tool sets are read from mcp/access.py at build time."),
            ("tools", "19 observation tools",
             "Counted from OBSERVATION_TOOLS. Resources and prompts are not implemented yet (docs/clients.md)."),
            ("ssh", "Allowlisted SSH operations",
             "Seven read-only operations run on a chosen worker through existing OpenSSH configuration; there is no generic remote command."),
        ],
        ["src/adapt_adb/mcp/access.py", "src/adapt_adb/ssh_worker.py", "docs/clients.md", "docs/ssh-workers.md"],
        "Synthetic serial. No MCP server or SSH connection was started to make this view.",
    )


# --------------------------------------------------------------------------
# View 10: the ONE actual capture
# --------------------------------------------------------------------------
def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def rel(path: Path) -> str:
    return path.resolve().relative_to(REPO).as_posix()


def source_tree_hash() -> tuple[str, list[dict]]:
    files = sorted((SRC / "adapt_adb").rglob("*.py"))
    digest = hashlib.sha256()
    for path in files:
        digest.update(rel(path).encode() + b"\0" + sha256_file(path).encode() + b"\n")
    key_files = ["src/adapt_adb/android_support.py", "src/adapt_adb/cli.py", "src/adapt_adb/__main__.py", "pyproject.toml"]
    listing = [{"path": p, "sha256": sha256_file(REPO / p)} for p in key_files]
    return digest.hexdigest(), listing


def run_capture() -> dict:
    """Run the real offline CLI and return stdout/stderr/exit code (no mocks)."""

    env = os.environ.copy()
    for key in [k for k in env if k.startswith("ADAPT_")]:
        env.pop(key)
    env.update({
        "PYTHONPATH": "src",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "NO_COLOR": "1",
        "ADAPT_NO_BANNER": "1",
        "ADAPT_TIPS": "0",
        "TERM": "dumb",
    })
    argv = [sys.executable, "-B", "-m", "adapt_adb", *CAPTURE_ARGV[1:]]
    proc = subprocess.run(argv, cwd=REPO, env=env, stdin=subprocess.DEVNULL,
                          capture_output=True, timeout=120, check=False)
    return {"stdout": proc.stdout, "stderr": proc.stderr, "exit_code": proc.returncode}


def view_cli(stdout_text: str, sha: str, exit_code: int) -> View:
    lines = stdout_text.rstrip("\n").split("\n")
    rows = []
    for i, line in enumerate(lines):
        tags = ["real"]
        if 1 <= i <= 9:
            tags.append("table")
        if i == len(lines) - 1:
            tags.append("caveat")
        rows.append(T(line, "t", *tags))
    return View(
        10, "cli", "Actual CLI output", "Actual CLI output: android versions", "actual", CAPTURE_DISPLAY,
        "The only real capture: offline command stdout with normalized line endings, rendered below.",
        rows,
        [
            ("real", "Real stdout",
             f"Captured by running the CLI from this checkout (exit {exit_code}, sha256 {sha[:12]}...). Not edited, not colored."),
            ("table", "API 29 to 37",
             "Nine released platform versions mapped to API levels. Android 12 appears twice: API 31 and 32 (12L)."),
            ("caveat", "Planning data",
             "The matrix does not certify ADAPT on every Android version or OEM build."),
        ],
        ["src/adapt_adb/android_support.py", "src/adapt_adb/cli.py", "docs/android-support.md"],
        "Offline and read-only: no device, host probe or network. The image is a rendered capture, not a Windows Terminal screenshot.",
        extra={"sha256": sha, "exit_code": exit_code},
    )


# --------------------------------------------------------------------------
# Fonts + native (code-drawn) block glyphs
# --------------------------------------------------------------------------
FONT_CANDIDATES = [
    os.environ.get("ADAPT_SHOWCASE_FONT", ""),
    "C:/Windows/Fonts/CascadiaMono.ttf",
    "C:/Windows/Fonts/consola.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
    "/System/Library/Fonts/Menlo.ttc",
    "/Library/Fonts/Courier New.ttf",
]
_FONT_CACHE: dict[int, tuple[ImageFont.FreeTypeFont, str]] = {}


def load_font(size: int):
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    for candidate in FONT_CANDIDATES:
        if candidate and Path(candidate).is_file():
            try:
                _FONT_CACHE[size] = (ImageFont.truetype(candidate, size), Path(candidate).name)
                return _FONT_CACHE[size]
            except OSError:
                continue
    _FONT_CACHE[size] = (ImageFont.load_default(size), "pillow-default")
    return _FONT_CACHE[size]


def font_metrics(size: int) -> tuple[int, int]:
    font, _ = load_font(size)
    return max(1, round(font.getlength("M"))), round(size * 1.5)


def draw_shade(draw: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int, glyph: str, color) -> None:
    """Draw full/dark/medium shade blocks natively so no font needs them."""

    if glyph == "█":
        draw.rectangle([x, y, x + w - 1, y + h - 1], fill=color)
        return
    p = max(1, w // 8)
    for j, py in enumerate(range(y, y + h, p)):
        for i, px in enumerate(range(x, x + w, p)):
            lit = ((i + j) % 2 == 0) if glyph == "▒" else not (i % 2 == 0 and j % 2 == 1)
            if lit:
                draw.rectangle([px, py, min(px + p, x + w) - 1, min(py + p, y + h) - 1], fill=color)


def draw_rows(img: Image.Image, rows: list[Row], x0: int, y0: int, size: int, nrows: int | None = None) -> None:
    font, _ = load_font(size)
    cw, ch = font_metrics(size)
    asc, desc = font.getmetrics()
    draw = ImageDraw.Draw(img)
    for r, row in enumerate(rows[:nrows] if nrows else rows):
        col = 0
        for text, cls in row.segs:
            color = rgb(PAL[cls])
            for glyph in text:
                x, y = x0 + col * cw, y0 + r * ch
                if glyph in "█▓▒":
                    draw_shade(draw, x, y, cw, ch, glyph, color)
                elif glyph != " ":
                    draw.text((x, y + (ch - (asc + desc)) // 2), glyph, font=font, fill=color)
                col += 1


def badge(draw: ImageDraw.ImageDraw, right: int, cy: int, text: str, kind: str, size: int) -> None:
    font, _ = load_font(size)
    pad = size // 2
    width = round(font.getlength(text)) + 2 * pad
    height = round(size * 1.7)
    box = [right - width, cy - height // 2, right, cy + height // 2]
    if kind == "actual":
        draw.rounded_rectangle(box, radius=height // 2, fill=rgb(ACCENT))
        draw.text((box[0] + pad, cy), text, font=font, fill=(255, 255, 255), anchor="lm")
    else:
        draw.rounded_rectangle(box, radius=height // 2, outline=rgb(PAL["d"]), width=1)
        draw.text((box[0] + pad, cy), text, font=font, fill=rgb(PAL["t"]), anchor="lm")


def render_terminal(rows: list[Row], cols: int, nrows: int, size: int, *, title: str, badge_text: str,
                    badge_kind: str, caption: list[str] | None = None) -> Image.Image:
    cw, ch = font_metrics(size)
    pad = max(10, size)
    bar = round(size * 2.4)
    cap_lines = caption or []
    cap_h = (len(cap_lines) * round(size * 1.4) + pad) if cap_lines else 0
    width = cols * cw + 2 * pad
    height = bar + nrows * ch + 2 * pad + cap_h
    img = Image.new("RGB", (width, height), rgb(T_BG))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, width, bar], fill=rgb(T_BAR))
    font, _ = load_font(size)
    draw.text((pad, bar // 2), title, font=font, fill=rgb(PAL["t"]), anchor="lm")
    badge(draw, width - pad, bar // 2, badge_text, badge_kind, max(9, size - 3))
    draw_rows(img, rows, pad, bar + pad, size, nrows)
    if cap_lines:
        top = bar + nrows * ch + 2 * pad
        draw.rectangle([0, top, width, height], fill=rgb(T_BAR))
        for i, line in enumerate(cap_lines):
            draw.text((pad, top + pad // 2 + i * round(size * 1.4)), line, font=load_font(max(9, size - 2))[0], fill=rgb(PAL["d"]))
    return img


# --------------------------------------------------------------------------
# Animated title (GIF) + static fallbacks (PNG, SVG)
# --------------------------------------------------------------------------
GIF_FRAMES = 36
GIF_MS = 60
LOGO_CELL = (16, 28)


def sheen(col: float, pos: float, sigma: float = 3.5) -> float:
    return math.exp(-((col - pos) ** 2) / (2 * sigma * sigma))


def tint(color, s: float):
    dim = 0.80
    c0 = [v * dim for v in color]
    c1 = [v + (255 - v) * 0.55 for v in color]
    return tuple(int(round(a + (b - a) * s)) for a, b in zip(c0, c1))


def logo_canvas(scale: int) -> tuple[int, int, dict]:
    cw, ch = LOGO_CELL[0] * scale, LOGO_CELL[1] * scale
    padx, pady = 40 * scale, 30 * scale
    gap = 22 * scale
    sub_h = 24 * scale
    width = LOGO_COLS * cw + 2 * padx
    height = pady + 5 * ch + gap + sub_h + pady
    return width, height, {"cw": cw, "ch": ch, "padx": padx, "pady": pady, "gap": gap, "sub_h": sub_h, "scale": scale}


def draw_logo(t: float | None, scale: int = 1) -> Image.Image:
    """Draw the banner mask. ``t`` in [0,1) moves a soft sheen; None = exact palette."""

    width, height, g = logo_canvas(scale)
    cw, ch = g["cw"], g["ch"]
    img = Image.new("RGB", (width, height), rgb(T_BG))
    draw = ImageDraw.Draw(img)
    pos = -14 + 61 * t if t is not None else 0.0
    for r, row in enumerate(MASK):
        for i, segment in enumerate(row):
            for k, mark in enumerate(segment):
                if mark != "#":
                    continue
                col = i * 7 + k
                color = LETTER_RGB[i] if t is None else tint(LETTER_RGB[i], sheen(col + 0.35 * r, pos))
                draw_shade(draw, g["padx"] + col * cw, g["pady"] + r * ch, cw, ch, SHADES[i], color)
    if t is not None:
        glow = img.filter(ImageFilter.GaussianBlur(cw * 0.55))
        glow = ImageEnhance.Brightness(glow).enhance(0.55)
        img = ImageChops.screen(img, glow)
        draw = ImageDraw.Draw(img)
    # fade rule: green -> white -> gray, the banner's own colour order
    rule_y = g["pady"] + 5 * ch + g["gap"] // 2
    span = LOGO_COLS * cw
    for x in range(span):
        f = x / max(1, span - 1) * 4
        a = min(3, int(f))
        c = tuple(int(round(p + (q - p) * (f - a))) for p, q in zip(LETTER_RGB[a], LETTER_RGB[a + 1]))
        draw.line([(g["padx"] + x, rule_y), (g["padx"] + x, rule_y + max(1, scale) - 1)], fill=tuple(int(v * 0.8) for v in c))
    # subtitle on a fixed pitch so it matches the logo width
    pitch = span / len(SUBTITLE)
    font, _ = load_font(max(8, int(pitch / 0.78)))
    for idx, glyph in enumerate(SUBTITLE):
        if glyph != " ":
            draw.text((g["padx"] + (idx + 0.5) * pitch, g["pady"] + 5 * ch + g["gap"] + g["sub_h"] // 2),
                      glyph, font=font, fill=rgb("#9aa7a0"), anchor="mm")
    return img


def build_title_assets() -> dict:
    ASSETS.mkdir(parents=True, exist_ok=True)
    frames = [draw_logo(i / GIF_FRAMES) for i in range(GIF_FRAMES)]
    sample = frames[::3]
    montage = Image.new("RGB", (frames[0].width, frames[0].height * len(sample)))
    for i, frame in enumerate(sample):
        montage.paste(frame, (0, i * frame.height))
    palette = montage.quantize(colors=255, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    paletted = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in frames]
    gif = ASSETS / "adapt-title.gif"
    paletted[0].save(gif, save_all=True, append_images=paletted[1:], duration=GIF_MS, loop=0,
                     disposal=1, optimize=False)
    static = draw_logo(None, scale=2)
    png = ASSETS / "adapt-title.png"
    static.save(png, format="PNG", compress_level=9)
    svg = ASSETS / "adapt-title.svg"
    svg.write_text(title_svg(), encoding="utf-8", newline="\n")
    return {"frames": GIF_FRAMES, "size": frames[0].size, "static_size": static.size}


def title_svg() -> str:
    width, height, g = logo_canvas(1)
    cw, ch = g["cw"], g["ch"]
    opacity = {"█": "1", "▓": "0.75", "▒": "0.5"}
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" aria-labelledby="t d">',
        "<title id=\"t\">ADAPT - Android Device Agent Provisioning Toolkit</title>",
        f"<desc id=\"d\">Static fallback of the ADAPT banner: five 5x5 letter masks, {FILLED} of {CELLS} cells filled ({FILL_PERCENT} percent), fading green to white to gray. Generated by {GENERATOR} from src/adapt_adb/banner.py.</desc>",
        f'<rect width="{width}" height="{height}" fill="{T_BG}"/>',
    ]
    for r, row in enumerate(MASK):
        for i, segment in enumerate(row):
            for k, mark in enumerate(segment):
                if mark == "#":
                    col = i * 7 + k
                    out.append(
                        f'<rect x="{g["padx"] + col * cw}" y="{g["pady"] + r * ch}" width="{cw}" height="{ch}" '
                        f'fill="{PAL[LETTER_CLASSES[i]]}" fill-opacity="{opacity[SHADES[i]]}"/>')
    span = LOGO_COLS * cw
    rule_y = g["pady"] + 5 * ch + g["gap"] // 2
    out += [
        '<defs><linearGradient id="f" x1="0" x2="1" y1="0" y2="0">'
        + "".join(f'<stop offset="{n * 25}%" stop-color="{PAL[c]}"/>' for n, c in enumerate(LETTER_CLASSES))
        + "</linearGradient></defs>",
        f'<rect x="{g["padx"]}" y="{rule_y}" width="{span}" height="1" fill="url(#f)" opacity="0.8"/>',
        f'<text x="{width / 2}" y="{g["pady"] + 5 * ch + g["gap"] + g["sub_h"] / 2 + 5}" text-anchor="middle" '
        f'font-family="Cascadia Mono, Consolas, DejaVu Sans Mono, Liberation Mono, monospace" font-size="15" '
        f'letter-spacing="2.2" fill="#9aa7a0">{html.escape(SUBTITLE)}</text>',
        "</svg>",
    ]
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------
# Rendered capture of the real CLI output (PNG + SVG), clearly labelled
# --------------------------------------------------------------------------
CAPTION = [
    "RENDERED CAPTURE: the real stdout above, drawn by scripts/build_showcase.py.",
    "It is not a Windows Terminal (or any terminal) screenshot.",
]


def render_capture_assets(stdout_text: str, sha: str, exit_code: int) -> dict:
    lines = stdout_text.rstrip("\n").split("\n")
    rows = [T(line) for line in lines]
    cols = max(COLS - 6, max(len(line) for line in lines) + 2)
    caption = [*CAPTION, f"command: {CAPTURE_DISPLAY}   exit: {exit_code}   stdout sha256: {sha[:16]}..."]
    img = render_terminal(rows, cols, len(rows), 17, title=f"$ {CAPTURE_DISPLAY}", badge_text="ACTUAL CLI OUTPUT",
                          badge_kind="actual", caption=caption)
    png = ASSETS / f"{CAPTURE_STEM}-rendered.png"
    img.save(png, format="PNG", compress_level=9)
    svg = ASSETS / f"{CAPTURE_STEM}-rendered.svg"
    svg.write_text(capture_svg(lines, cols, caption), encoding="utf-8", newline="\n")
    return {"png_size": img.size}


def capture_svg(lines: list[str], cols: int, caption: list[str]) -> str:
    cw, lh, pad, bar = 9.0, 22, 18, 40
    width = round(cols * cw + 2 * pad)
    body = len(lines) * lh + 2 * pad
    cap = len(caption) * 20 + pad
    height = bar + body + cap
    font = "Cascadia Mono, Consolas, DejaVu Sans Mono, Liberation Mono, monospace"
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" aria-labelledby="t d">',
        f"<title id=\"t\">Rendered capture: {html.escape(CAPTURE_DISPLAY)}</title>",
        "<desc id=\"d\">Rendered capture of the actual stdout of an offline ADAPT CLI command. Text is the real output; the drawing is not a terminal screenshot.</desc>",
        f'<rect width="{width}" height="{height}" fill="{T_BG}"/>',
        f'<rect width="{width}" height="{bar}" fill="{T_BAR}"/>',
        f'<text x="{pad}" y="{bar / 2 + 5}" font-family="{font}" font-size="14" fill="{PAL["t"]}">$ {html.escape(CAPTURE_DISPLAY)}</text>',
        f'<rect x="{width - pad - 150}" y="{bar / 2 - 12}" width="150" height="24" rx="12" fill="{ACCENT}"/>',
        f'<text x="{width - pad - 75}" y="{bar / 2 + 4}" text-anchor="middle" font-family="{font}" font-size="11" font-weight="700" fill="#ffffff">ACTUAL CLI OUTPUT</text>',
    ]
    for i, line in enumerate(lines):
        out.append(f'<text xml:space="preserve" x="{pad}" y="{bar + pad + i * lh + 15}" font-family="{font}" font-size="15" fill="{PAL["t"]}">{html.escape(line)}</text>')
    top = bar + body
    out.append(f'<rect y="{top}" width="{width}" height="{cap}" fill="{T_BAR}"/>')
    for i, line in enumerate(caption):
        out.append(f'<text x="{pad}" y="{top + 18 + i * 20}" font-family="{font}" font-size="12" fill="{PAL["d"]}">{html.escape(line)}</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"


def write_provenance(capture: dict, stdout_text: str, norm_sha: str) -> dict:
    tree_sha, key_files = source_tree_hash()
    stdout_path = ASSETS / f"{CAPTURE_STEM}.txt"
    doc = {
        "schema": "adapt.showcase.capture-provenance/1",
        "kind": "actual_offline_cli_capture",
        "project": {"name": "adapt-adb", "version": VERSION, "license": "MIT"},
        "command": {
            "display": CAPTURE_DISPLAY,
            "argv": CAPTURE_ARGV,
            "executed_as": ["python", "-B", "-m", "adapt_adb", *CAPTURE_ARGV[1:]],
            "working_directory": ".",
            "env_overrides": {"PYTHONPATH": "src", "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1",
                              "ADAPT_NO_BANNER": "1", "ADAPT_TIPS": "0", "TERM": "dumb", "PYTHONDONTWRITEBYTECODE": "1"},
            "stdin": "closed (devnull)",
            "offline": True,
            "device_access": False,
            "network_access": False,
        },
        "exit_code": capture["exit_code"],
        "stdout": {
            "file": rel(stdout_path),
            "sha256": norm_sha,
            "bytes": len(stdout_text.encode("utf-8")),
            "lines": stdout_text.count("\n"),
            "raw_sha256": sha256_bytes(capture["stdout"]),
            "normalization": "CRLF converted to LF; no other change",
        },
        "stderr": {"bytes": len(capture["stderr"]), "sha256": sha256_bytes(capture["stderr"])},
        "source": {
            "tree": "src/adapt_adb/**/*.py",
            "tree_sha256": tree_sha,
            "key_files": key_files,
        },
        "rendered_capture": {
            "label": "Rendered capture drawn by scripts/build_showcase.py. Not a Windows Terminal screenshot.",
            "files": [f"docs/assets/{CAPTURE_STEM}-rendered.png", f"docs/assets/{CAPTURE_STEM}-rendered.svg"],
        },
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "generator": GENERATOR,
        "notes": [
            "Output was not edited; the .txt is exactly this stdout with LF line endings.",
            "The release date in the first line is part of the CLI's own data, not a capture timestamp.",
        ],
    }
    path = ASSETS / f"{CAPTURE_STEM}.provenance.json"
    path.write_text(json.dumps(doc, indent=2, sort_keys=False) + "\n", encoding="utf-8", newline="\n")
    return doc


# --------------------------------------------------------------------------
# Overview PNG for the README
# --------------------------------------------------------------------------
TILE_TITLES = {
    1: "Home: original banner", 2: "Host doctor", 3: "Developer profiles", 4: "Tool catalog and ROM plans",
    5: "Android capability matrix", 6: "Tailscale connection stages", 7: "Samsung DeX, native (experimental)",
    8: "Rapid ADAPT test plan", 9: "MCP and SSH access", 10: "Actual CLI output: android versions",
}


def build_overview(views: list[View], nrows: int) -> Image.Image:
    size = 13
    tiles = []
    for v in views:
        actual = v.kind == "actual"
        rows, caption = v.rows, ["illustrative: synthetic values, source-derived"]
        if actual:
            rows, wrapped_any = [], False
            for row in v.rows:
                chunks = textwrap.wrap(row.plain, width=COLS, subsequent_indent="    ", drop_whitespace=False) or [""]
                wrapped_any |= len(chunks) > 1
                rows += [T(chunk.rstrip()) for chunk in chunks]
            caption = ["real stdout, rendered capture" + (" (long line wrapped to fit)" if wrapped_any else "")]
        tiles.append(render_terminal(
            rows, COLS, nrows, size, title=f"{v.n:02d}  {TILE_TITLES[v.n]}",
            badge_text="ACTUAL" if actual else "GENERATED", badge_kind="actual" if actual else "generated",
            caption=caption))
    tw, th = tiles[0].size
    margin, gutter = 36, 28
    head_h = 400
    width = margin * 2 + tw * 2 + gutter
    height = head_h + margin + 5 * th + 4 * gutter + margin
    img = Image.new("RGB", (width, height), rgb("#080d0a"))
    draw = ImageDraw.Draw(img)
    logo = draw_logo(None, scale=1)
    img.paste(logo, (margin, 24))
    tx = margin + logo.width + 40
    big, _ = load_font(30)
    mid, _ = load_font(17)
    small, _ = load_font(14)
    draw.text((tx, 56), "ADAPT public visual showcase", font=big, fill=rgb("#ffffff"))
    draw.text((tx, 108), f"Version {VERSION}  |  MIT  |  10 equal views", font=mid, fill=rgb(PAL["pg"]))
    note = [
        "9 generated, source-derived illustrations (synthetic values; nothing ran on a host or device).",
        "1 actual capture: stdout of an offline CLI command, shown as a rendered capture.",
        "9 of 10 counts equal views. It is not a pixel measurement.",
        "No Android screenshots. The rendered capture is not a Windows Terminal screenshot.",
    ]
    avail = max(20, int((width - margin - tx) / max(1, font_metrics(14)[0])))
    y = 150
    for line in note:
        for chunk in textwrap.wrap(line, width=avail):
            draw.text((tx, y), chunk, font=small, fill=rgb(PAL["t"]))
            y += 22
        y += 4
    my = y + 8
    for i in range(10):
        x = tx + i * 46
        box = [x, my, x + 36, my + 20]
        if i < 9:
            draw.rounded_rectangle(box, radius=4, fill=rgb("#16221b"), outline=rgb(PAL["d"]), width=2)
            for k in range(-20, 40, 8):
                draw.line([(x + 4 + k, my + 18), (x + 4 + k + 18, my + 2)], fill=rgb("#33433a"), width=1)
        else:
            draw.rounded_rectangle(box, radius=4, fill=rgb(ACCENT))
    draw.text((tx, my + 34), "9 generated views + 1 actual capture", font=small, fill=rgb(PAL["t"]))
    for idx, tile in enumerate(tiles):
        col, row = idx % 2, idx // 2
        img.paste(tile, (margin + col * (tw + gutter), head_h + margin + row * (th + gutter)))
    return img


# --------------------------------------------------------------------------
# Standalone HTML
# --------------------------------------------------------------------------
def esc(text: str) -> str:
    return html.escape(text, quote=True)


def row_html(row: Row) -> str:
    inner = "".join(f'<span class="c-{cls}">{esc(text)}</span>' for text, cls in row.segs)
    tags = f' data-h="{" ".join(row.tags)}"' if row.tags else ""
    return f'<span class="ln"{tags}>{inner}</span>'


def logo_html() -> str:
    out = []
    for r, row in enumerate(MASK):
        parts = []
        for i, segment in enumerate(row):
            cells = []
            for k, mark in enumerate(segment):
                if mark == "#":
                    cells.append(f'<span class="c l{i}" style="--col:{i * 7 + k}">{SHADES[i]}</span>')
                else:
                    cells.append(" ")
            parts.append("".join(cells))
        out.append("  ".join(parts))
    return "\n".join(out)


def panel_html(v: View, nrows: int, prov: dict | None) -> str:
    actual = v.kind == "actual"
    kind_label = "Actual" if actual else "Generated"
    kind_note = (
        "Real output of an offline CLI command, rendered below. Not a Windows Terminal screenshot."
        if actual else
        "Source-derived illustration with synthetic values. Nothing was run on a host, network or device."
    )
    hl = "".join(
        f'<li><button type="button" class="hl" data-h="{esc(tag)}" aria-pressed="false">{esc(label)}</button>'
        f'<span>{esc(detail)}</span></li>' for tag, label, detail in v.highlights)
    sources = "".join(f"<li><code>{esc(s)}</code></li>" for s in v.sources)
    extra = ""
    if actual and prov:
        extra = (
            '<div class="prov"><h3>Provenance</h3><dl>'
            f'<dt>Command</dt><dd><code>{esc(CAPTURE_DISPLAY)}</code></dd>'
            f'<dt>Exit code</dt><dd>{prov["exit_code"]}</dd>'
            f'<dt>stdout sha256</dt><dd><code>{esc(prov["stdout"]["sha256"])}</code></dd>'
            f'<dt>Source tree sha256</dt><dd><code>{esc(prov["source"]["tree_sha256"])}</code></dd>'
            "</dl>"
            '<p class="links">Files: '
            f'<a href="../assets/{CAPTURE_STEM}.txt">stdout .txt</a> &middot; '
            f'<a href="../assets/{CAPTURE_STEM}.provenance.json">provenance .json</a> &middot; '
            f'<a href="../assets/{CAPTURE_STEM}-rendered.png">rendered .png</a> &middot; '
            f'<a href="../assets/{CAPTURE_STEM}-rendered.svg">rendered .svg</a></p></div>')
    return f"""
<article class="panel card" id="view-{v.n}" data-kind="{v.kind}" aria-labelledby="h-{v.n}">
  <header class="phead">
    <span class="num" aria-hidden="true">{v.n}</span>
    <h2 id="h-{v.n}">{esc(v.title)}</h2>
    <span class="badge {'act' if actual else 'gen'}">{kind_label}</span>
  </header>
  <p class="blurb">{esc(v.blurb)}</p>
  <div class="pbody">
    <div class="term">
      <div class="term-bar"><span class="cmd">$ {esc(v.command)}</span><span class="tag">{'real stdout' if actual else 'illustrative'}</span></div>
      <pre class="term-body" tabindex="0" role="region" aria-label="Terminal output for view {v.n}: {esc(v.short)}">{''.join(row_html(r) for r in v.rows)}</pre>
    </div>
    <div class="side">
      <p class="kind"><strong>{kind_label}.</strong> {esc(kind_note)}</p>
      <h3>Highlights <span class="hint">(press one to light its lines)</span></h3>
      <ul class="hls">{hl}</ul>
      {extra}
      <details class="src"><summary>Sources and boundary</summary>
        <ul>{sources}</ul>
        <p>{esc(v.boundary)}</p>
      </details>
    </div>
  </div>
</article>"""


CSS = r"""
:root{
  --fb-fg:#14201a; --fb-muted:#4d5d55; --fb-card:#ffffff; --fb-border:#c9d5ce; --fb-soft:#eef3f0;
  --t-bg:#0b110e; --t-bar:#121b16; --t-fg:#c9d4ce; --t-dim:#7f8c85;
  --acc:#1c8c4e; --acc-soft:rgba(75,227,122,.16);
  color-scheme:light dark;
}
@media (prefers-color-scheme:dark){
  :root{--fb-fg:#e4ece7; --fb-muted:#9fb0a7; --fb-card:#151c18; --fb-border:#2a362f; --fb-soft:#1b241f; --acc:#2fa965;}
}
:root{
  --c-fg:var(--foreground,var(--fb-fg));
  --c-muted:var(--muted-foreground,var(--fb-muted));
  --c-card:var(--card,var(--fb-card));
  --c-border:var(--border,var(--fb-border));
  --c-soft:var(--fb-soft);
  --r:var(--radius,12px);
  --mono:var(--font-mono,ui-monospace,"Cascadia Mono","SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace);
  --sans:var(--font-sans,system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif);
}
*,*::before,*::after{box-sizing:border-box}
html,body{margin:0;padding:0;background:transparent;color:var(--c-fg);font-family:var(--sans);line-height:1.5}
body{font-size:15px}
main{max-width:78rem;margin:0 auto;display:grid;gap:12px}
.sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.card{background:var(--c-card);border:1px solid var(--c-border);border-radius:var(--r);padding:16px}
h1,h2,h3,p{margin:0}
h2{font-size:1.05rem;line-height:1.3}
h3{font-size:.8rem;text-transform:uppercase;letter-spacing:.06em;color:var(--c-muted);margin:14px 0 6px}
h3 .hint{text-transform:none;letter-spacing:0;font-weight:400}
code{font-family:var(--mono);font-size:.85em;background:var(--c-soft);padding:1px 5px;border-radius:5px;overflow-wrap:anywhere}
a{color:inherit;text-underline-offset:3px}
button{font:inherit;color:inherit;cursor:pointer}
:focus-visible{outline:2px solid var(--acc);outline-offset:2px}

/* hero */
.hero{display:grid;grid-template-columns:auto minmax(0,1fr);gap:20px;align-items:center}
.logo-box{background:var(--t-bg);border-radius:calc(var(--r) - 2px);padding:18px 22px;overflow:hidden;max-width:100%}
.logo,.subtitle{margin:0;font-family:var(--mono);color:var(--t-fg);white-space:pre}
.logo{font-size:clamp(11px,3.4vw,19px);line-height:1.04}
.subtitle{font-size:clamp(7px,1.9vw,11.5px);letter-spacing:.02em;margin-top:.9em;color:#9aa7a0;text-align:center}
.l0{color:#00ff00}.l1{color:#87ff87}.l2{color:#fff}.l3{color:#ccc}.l4{color:#808080}
.logo .c{display:inline-block;opacity:1}
.logo.anim .c{animation:sheen 4.2s ease-in-out infinite;animation-delay:calc((var(--col) - 33) * 55ms)}
@keyframes sheen{0%,62%,100%{opacity:.82;filter:brightness(.92)}14%{opacity:1;filter:brightness(1.5)}30%{opacity:.92;filter:brightness(1)}}
.lede{margin-top:6px;color:var(--c-muted)}
.lede strong{color:var(--c-fg)}
.controls{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px;align-items:center}
.btn{background:var(--c-soft);border:1px solid var(--c-border);border-radius:calc(var(--r) - 4px);padding:7px 12px;min-height:36px}
.btn:hover{border-color:var(--acc)}
.btn[aria-pressed="true"]{background:var(--acc);border-color:var(--acc);color:#fff}
.meter{display:flex;gap:4px;align-items:center;margin-top:14px;flex-wrap:wrap}
.meter i{display:block;width:26px;height:14px;border-radius:4px;border:1px solid var(--c-muted);
  background:repeating-linear-gradient(135deg,transparent 0 3px,var(--c-border) 3px 6px)}
.meter i.real{background:var(--acc);border-color:var(--acc)}
.meter span{margin-left:8px;font-size:.88rem}
.fine{font-size:.82rem;color:var(--c-muted);margin-top:6px}

/* legend */
.legend{display:grid;gap:10px}
.legend-row{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:center}
.legend h2{font-size:.8rem;text-transform:uppercase;letter-spacing:.06em;color:var(--c-muted)}
.chip{font:12px/1 var(--mono);background:var(--t-bg);color:var(--t-fg);padding:5px 8px;border-radius:6px;display:inline-flex;gap:6px;align-items:center}
.legend p{font-size:.88rem}

/* picker */
.pickbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.picker{display:flex;flex-wrap:wrap;gap:6px;margin:0;padding:0;list-style:none;flex:1 1 30rem}
.picker button{display:inline-flex;gap:7px;align-items:center;background:var(--c-soft);border:1px solid var(--c-border);
  border-radius:calc(var(--r) - 4px);padding:7px 11px;min-height:38px;font-size:.88rem}
.picker button:hover{border-color:var(--acc)}
.picker button[aria-current="true"]{background:var(--acc);border-color:var(--acc);color:#fff}
.picker .n{font:700 .78rem var(--mono);opacity:.85}
.picker .k{width:8px;height:8px;border-radius:50%;border:1.5px solid currentColor;display:inline-block}
.picker .k.real{background:currentColor}
.navs{display:flex;gap:6px;align-items:center}
.pos{font:.82rem var(--mono);color:var(--c-muted);min-width:5.5ch;text-align:center}

/* panels */
.panels{display:grid;gap:12px}
.panel[hidden]{display:none}
.all-views .panels{grid-template-columns:repeat(auto-fit,minmax(min(100%,40rem),1fr))}
.panel{container-type:inline-size;display:flex;flex-direction:column;gap:10px;min-width:0}
.phead{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.phead h2{flex:1 1 12rem}
.num{display:inline-grid;place-items:center;width:28px;height:28px;border-radius:50%;background:var(--c-soft);
  border:1px solid var(--c-border);font:700 .8rem var(--mono)}
.badge{font:700 .68rem/1 var(--mono);letter-spacing:.07em;text-transform:uppercase;padding:6px 10px;border-radius:999px}
.badge.gen{border:1px dashed var(--c-muted)}
.badge.act{background:var(--acc);color:#fff;border:1px solid var(--acc)}
.blurb{color:var(--c-muted);font-size:.92rem}
.pbody{display:grid;gap:14px;grid-template-columns:minmax(0,1fr);align-items:start}
@container (min-width:56rem){.pbody{grid-template-columns:minmax(0,max-content) minmax(0,1fr)}}
.term{background:var(--t-bg);border:1px solid #223028;border-radius:calc(var(--r) - 2px);overflow:hidden;min-width:0;max-width:100%}
.term-bar{display:flex;justify-content:space-between;gap:10px;padding:7px 12px;background:var(--t-bar);color:var(--t-dim);font:12px var(--mono)}
.term-bar .cmd{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.term-bar .tag{white-space:nowrap;border:1px solid #2c3b33;border-radius:999px;padding:1px 8px}
.term-body{margin:0;padding:12px;height:calc(var(--rows) * 1.45em + 26px);overflow:auto;font:13px/1.45 var(--mono);
  color:var(--t-fg);white-space:pre;tab-size:4}
.ln{display:block;min-height:1.45em;border-left:2px solid transparent;padding-left:6px;margin-left:-8px}
.term-body[data-active] .ln{opacity:.34}
.term-body[data-active] .ln.on{opacity:1;background:var(--acc-soft);border-left-color:#4be37a}
.c-t{color:#c9d4ce}.c-w{color:#fff;font-weight:700}.c-d{color:#7f8c85}.c-g{color:#4be37a}.c-y{color:#e8c14a}
.c-r{color:#f06a6a}.c-pg{color:#87ff87}.c-L0{color:#00ff00}.c-L1{color:#87ff87}.c-L2{color:#fff}.c-L3{color:#ccc}.c-L4{color:#808080}
.side{min-width:0}
.kind{font-size:.88rem}
.hls{list-style:none;margin:0;padding:0;display:grid;gap:8px}
.hls li{display:grid;gap:3px;font-size:.88rem}
.hl{justify-self:start;text-align:left;background:var(--c-soft);border:1px solid var(--c-border);
  border-radius:calc(var(--r) - 4px);padding:5px 10px;font-weight:600;font-size:.85rem}
.hl:hover{border-color:var(--acc)}
.hl[aria-pressed="true"]{background:var(--acc);border-color:var(--acc);color:#fff}
.hls span{color:var(--c-muted)}
.src{margin-top:12px;font-size:.86rem}
.src summary{cursor:pointer;font-weight:600}
.src ul{margin:8px 0;padding-left:1.1rem;display:grid;gap:3px}
.src p{color:var(--c-muted)}
.prov{margin-top:12px;font-size:.86rem}
.prov dl{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:4px 12px;margin:6px 0}
.prov dt{color:var(--c-muted)}
.prov dd{margin:0;overflow-wrap:anywhere}
.links{margin-top:6px}

/* notes */
.notes ul{margin:8px 0 0;padding-left:1.15rem;display:grid;gap:6px;font-size:.9rem}
.notes h2{font-size:1rem}
@media (max-width:40rem){
  .hero{grid-template-columns:minmax(0,1fr)}
  .logo-box{padding:14px}
}
@media (prefers-reduced-motion:reduce){
  .logo.anim .c{animation:none}
  html{scroll-behavior:auto}
}
"""

JS = r"""
(function () {
  var doc = document, root = doc.documentElement;
  root.classList.add('js');
  var panels = [].slice.call(doc.querySelectorAll('.panel'));
  var picks = [].slice.call(doc.querySelectorAll('.picker button'));
  var modeBtn = doc.getElementById('mode');
  var pos = doc.getElementById('pos');
  var cur = 0, all = false;

  function store() { try { history.replaceState(null, '', all ? '#all' : '#view-' + (cur + 1)); } catch (e) {} }
  function show(i) {
    cur = (i + panels.length) % panels.length;
    panels.forEach(function (p, k) { p.hidden = !all && k !== cur; });
    picks.forEach(function (b, k) { if (k === cur) b.setAttribute('aria-current', 'true'); else b.removeAttribute('aria-current'); });
    pos.textContent = all ? 'all' : (cur + 1) + ' / ' + panels.length;
    store();
  }
  function setAll(v) {
    all = v; root.classList.toggle('all-views', v);
    modeBtn.setAttribute('aria-pressed', String(v));
    modeBtn.textContent = v ? 'Show one view' : 'Show all 10 views';
    show(cur);
  }
  picks.forEach(function (b, k) {
    b.addEventListener('click', function () {
      show(k);
      if (all) { panels[k].scrollIntoView({ block: 'start' }); }
    });
    b.addEventListener('keydown', function (e) {
      var d = e.key === 'ArrowRight' ? 1 : e.key === 'ArrowLeft' ? -1 : 0;
      if (e.key === 'Home') d = -cur; if (e.key === 'End') d = panels.length - 1 - cur;
      if (!d) return;
      e.preventDefault(); show(cur + d); picks[cur].focus();
    });
  });
  doc.getElementById('prev').addEventListener('click', function () { show(cur - 1); });
  doc.getElementById('next').addEventListener('click', function () { show(cur + 1); });
  modeBtn.addEventListener('click', function () { setAll(!all); });

  panels.forEach(function (p) {
    var term = p.querySelector('.term-body');
    var btns = [].slice.call(p.querySelectorAll('button.hl'));
    function clear() {
      term.removeAttribute('data-active');
      [].forEach.call(term.querySelectorAll('.ln.on'), function (l) { l.classList.remove('on'); });
      btns.forEach(function (b) { b.setAttribute('aria-pressed', 'false'); });
    }
    btns.forEach(function (b) {
      b.addEventListener('click', function () {
        var turnOn = b.getAttribute('aria-pressed') !== 'true';
        clear();
        if (!turnOn) return;
        var key = b.getAttribute('data-h');
        b.setAttribute('aria-pressed', 'true');
        term.setAttribute('data-active', key);
        var first = null;
        [].forEach.call(term.querySelectorAll('.ln'), function (l) {
          if ((' ' + (l.getAttribute('data-h') || '') + ' ').indexOf(' ' + key + ' ') > -1) {
            l.classList.add('on'); if (!first) first = l;
          }
        });
        if (first && first.offsetTop < term.scrollTop + 4) term.scrollTop = Math.max(0, first.offsetTop - 12);
        if (first && first.offsetTop > term.scrollTop + term.clientHeight - 24) term.scrollTop = first.offsetTop - 12;
      });
    });
    term.addEventListener('keydown', function (e) { if (e.key === 'Escape') clear(); });
  });

  var logo = doc.querySelector('.logo'), mb = doc.getElementById('motion');
  var mq = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
  function motion(on) {
    logo.classList.toggle('anim', on);
    mb.textContent = on ? 'Pause title animation' : 'Play title animation';
    mb.setAttribute('aria-pressed', String(!on));
  }
  motion(!(mq && mq.matches));
  mb.addEventListener('click', function () { motion(!logo.classList.contains('anim')); });
  if (mq && mq.addEventListener) mq.addEventListener('change', function (e) { motion(!e.matches); });

  var h = (location.hash || '').replace('#', '');
  var m = /^view-(\d+)$/.exec(h);
  var start = m ? Math.min(panels.length, Math.max(1, +m[1])) - 1 : 0;
  cur = start;
  setAll(h === 'all');
})();
"""


def build_html(views: list[View], nrows: int, prov: dict) -> str:
    picker = "".join(
        f'<li><button type="button" data-view="{v.n}" aria-controls="view-{v.n}">'
        f'<span class="n">{v.n:02d}</span>{esc(v.short)}'
        f'<span class="k{" real" if v.kind == "actual" else ""}" aria-hidden="true"></span>'
        f'<span class="sr-only">({"actual capture" if v.kind == "actual" else "generated illustration"})</span></button></li>'
        for v in views)
    meter = "".join(f'<i class="{"real" if v.kind == "actual" else ""}"></i>' for v in views)
    status_chips = "".join(
        f'<span class="chip"><span class="c-{cls}">{label}</span></span>'
        for cls, label in (("g", "READY"), ("y", "WAITING"), ("r", "BLOCKED"), ("d", "SKIP")))
    letters = "".join(f'<span class="c-{c}">{ch}</span>' for c, ch in zip(LETTER_CLASSES, "ADAPT"))
    body = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>ADAPT Showcase</title>
<meta name="description" content="Interactive visual tour of ADAPT {VERSION}: nine generated, source-derived terminal illustrations and one actual offline CLI capture.">
<style>{CSS}</style>
</head>
<body style="--rows:{nrows}">
<main id="top">
  <h1 class="sr-only">ADAPT, the Android Device Agent Provisioning Toolkit: visual showcase</h1>

  <section class="card hero" aria-label="ADAPT title">
    <div class="logo-box" aria-hidden="true">
      <pre class="logo">{logo_html()}</pre>
      <p class="subtitle">{esc(SUBTITLE)}</p>
    </div>
    <div>
      <p class="lede"><strong>ADAPT {VERSION}</strong> &middot; MIT &middot; Python 3.11+. A visual tour of ten terminal views.
        Nine are <strong>generated illustrations</strong> built from the source with synthetic values; nothing was run on a host or device to make them.
        One is the <strong>actual output</strong> of an offline CLI command, rendered to an image.</p>
      <div class="meter" role="img" aria-label="Nine of ten views generated, one actual">{meter}<span>9 generated + 1 actual</span></div>
      <p class="fine">&ldquo;9 of 10&rdquo; counts equal showcase views. It is not a pixel measurement of how much is generated or real.</p>
      <div class="controls">
        <button type="button" class="btn" id="motion" aria-pressed="false">Pause title animation</button>
        <button type="button" class="btn" id="mode" aria-pressed="false">Show all 10 views</button>
      </div>
      <p class="fine">The title animation is a branding effect for this page and the README only. The terminal banner itself stays static.</p>
    </div>
  </section>

  <section class="card legend" aria-labelledby="legend-h">
    <h2 id="legend-h">How to read this page</h2>
    <div class="legend-row">
      <span class="badge gen">Generated</span><span>Illustration derived from source, synthetic values, not a run.</span>
    </div>
    <div class="legend-row">
      <span class="badge act">Actual</span><span>Real offline stdout, normalized line endings, verified by hash.</span>
    </div>
    <div class="legend-row"><span>Status words:</span>{status_chips}
      <span>Banner letters:</span><span class="chip">{letters}</span></div>
    <p>Pick a view, then press a <em>highlight</em> to light the matching terminal lines. Arrow keys move along the picker. Press Esc inside a terminal to clear highlights.</p>
  </section>

  <section class="card" aria-label="View picker">
    <div class="pickbar">
      <ul class="picker">{picker}</ul>
      <div class="navs">
        <button type="button" class="btn" id="prev" aria-label="Previous view">&larr;</button>
        <span class="pos" id="pos" aria-live="polite">1 / 10</span>
        <button type="button" class="btn" id="next" aria-label="Next view">&rarr;</button>
      </div>
    </div>
  </section>

  <div class="panels">{"".join(panel_html(v, nrows, prov if v.kind == "actual" else None) for v in views)}
  </div>

  <section class="card notes" aria-labelledby="notes-h">
    <h2 id="notes-h">What this showcase does not claim</h2>
    <ul>
      <li>Generated views show the shape of ADAPT output, not results. No host check, device, network or pairing ran, and every serial, package and peer name is a placeholder.</li>
      <li>There are no Android screenshots here. The rendered capture image is drawn by <code>scripts/build_showcase.py</code>; it is not a Windows Terminal screenshot.</li>
      <li>Samsung DeX for PC is experimental. Fifteen native unit tests exist in source, one Android 13 reconnect was observed historically, and that is not support for every device or Android version. Receiver launch is not a shipped CLI backend.</li>
      <li>Direct Tailnet ADB is not confirmed end to end because the current live verifier has a gap; view 6 shows the documented stages only.</li>
      <li>Android 10 to 17 rows are planning data from the CLI's own matrix, not certification on every OEM build.</li>
    </ul>
  </section>
</main>
<script>{JS}</script>
</body>
</html>
"""
    return body


# --------------------------------------------------------------------------
# Manifest, validation, secret scan
# --------------------------------------------------------------------------
TEXT_OUTPUTS = [
    "docs/showcase/index.html", "docs/showcase/README.md",
    "docs/assets/adapt-title.svg", f"docs/assets/{CAPTURE_STEM}-rendered.svg",
    f"docs/assets/{CAPTURE_STEM}.txt", f"docs/assets/{CAPTURE_STEM}.provenance.json",
    "docs/assets/showcase-manifest.json",
]
BINARY_OUTPUTS = [
    "docs/assets/adapt-title.gif", "docs/assets/adapt-title.png",
    "docs/assets/showcase-overview.png", f"docs/assets/{CAPTURE_STEM}-rendered.png",
]
SECRET_PATTERNS = [
    ("absolute windows path", re.compile(r"[A-Za-z]:[\\/]+(?:Users|Documents and Settings|Projects)[\\/]", re.I)),
    ("unix home path", re.compile(r"/(?:home|Users)/[A-Za-z0-9._-]+")),
    ("email address", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    ("ipv4 address", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("mac address", re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b")),
    ("credential word", re.compile(r"(?i)\b(?:api[_-]?key|secret|token|passw(?:or)?d|private key)\b|BEGIN [A-Z ]*PRIVATE KEY|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}")),
    ("remote url", re.compile(r"https?://", re.I)),
]


def private_markers() -> list[str]:
    import getpass
    markers = {str(REPO), REPO.as_posix(), str(Path.home()), Path.home().as_posix()}
    for name in (getpass.getuser(), os.environ.get("USERNAME", ""), os.environ.get("USER", "")):
        if name and len(name) >= 3:
            markers.add(name)
    return sorted(m for m in markers if m)


def scan_text_outputs() -> list[str]:
    problems: list[str] = []
    markers = private_markers()
    for relpath in TEXT_OUTPUTS:
        path = REPO / relpath
        if not path.is_file():
            problems.append(f"missing text output {relpath}")
            continue
        text = path.read_text(encoding="utf-8").replace("http://www.w3.org/2000/svg", "")
        for label, pattern in SECRET_PATTERNS:
            for m in pattern.finditer(text):
                problems.append(f"{relpath}: {label}: {m.group(0)[:40]!r}")
        for marker in markers:
            if marker.lower() in text.lower():
                problems.append(f"{relpath}: private marker present")
    return problems


def png_info(path: Path) -> tuple[int, int]:
    with Image.open(path) as im:
        return im.size


def write_manifest(info: dict) -> dict:
    entries = []
    for relpath in [*BINARY_OUTPUTS, *[t for t in TEXT_OUTPUTS if not t.endswith("showcase-manifest.json")]]:
        path = REPO / relpath
        entry = {"path": relpath, "bytes": path.stat().st_size, "sha256": sha256_file(path)}
        if relpath.endswith((".png", ".gif")):
            w, h = png_info(path)
            entry["width"], entry["height"] = w, h
        if relpath.endswith(".gif"):
            with Image.open(path) as im:
                entry["frames"] = getattr(im, "n_frames", 1)
                entry["frame_ms"] = GIF_MS
        entries.append(entry)
    doc = {
        "schema": "adapt.showcase.manifest/1",
        "generator": GENERATOR,
        "project_version": VERSION,
        "views": {"total": 10, "generated": 9, "actual": 1,
                  "note": "9 of 10 counts equal showcase views; it is not a pixel measurement."},
        "android_screenshots": 0,
        "font": info["font"],
        "pillow": Image.__version__ if hasattr(Image, "__version__") else "unknown",
        "determinism": "Outputs are reproducible for the same source, Pillow version and monospace font; "
                       "image hashes can differ across machines with different fonts.",
        "assets": entries,
    }
    path = ASSETS / "showcase-manifest.json"
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8", newline="\n")
    return doc


class _Audit(__import__("html.parser", fromlist=["HTMLParser"]).HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids, self.dupes, self.badges, self.panels, self.urls, self.imgs = set(), [], [], 0, [], 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "id" in a:
            if a["id"] in self.ids:
                self.dupes.append(a["id"])
            self.ids.add(a["id"])
        if tag == "article" and "panel" in (a.get("class") or ""):
            self.panels += 1
            self.badges.append(a.get("data-kind"))
        for key in ("src", "href"):
            if key in a and a[key] and not a[key].startswith(("#", "../assets/")):
                self.urls.append(a[key])
        if tag == "img":
            self.imgs += 1


def validate(info: dict | None = None) -> list[str]:
    problems: list[str] = []
    manifest_path = ASSETS / "showcase-manifest.json"
    if not manifest_path.is_file():
        return ["showcase-manifest.json is missing; run without --verify first"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_dims = {
        "docs/assets/adapt-title.png": logo_canvas(2)[:2],
        "docs/assets/adapt-title.gif": logo_canvas(1)[:2],
    }
    for entry in manifest["assets"]:
        path = REPO / entry["path"]
        if not path.is_file():
            problems.append(f"missing asset {entry['path']}")
            continue
        if sha256_file(path) != entry["sha256"]:
            problems.append(f"hash mismatch {entry['path']}")
        if entry["path"] in expected_dims and (entry["width"], entry["height"]) != tuple(expected_dims[entry["path"]]):
            problems.append(f"unexpected dimensions {entry['path']}: {entry['width']}x{entry['height']}")
        if entry["path"].endswith(".gif"):
            with Image.open(path) as im:
                total = 0
                for index in range(getattr(im, "n_frames", 1)):
                    im.seek(index)
                    total += im.info.get("duration", 0)
            # Pillow may merge identical consecutive frames, adding their durations.
            if total != GIF_FRAMES * GIF_MS:
                problems.append(f"gif loop length {total} ms != {GIF_FRAMES * GIF_MS} ms")
        if entry["path"] == "docs/assets/showcase-overview.png" and entry["width"] < 1000:
            problems.append("overview too small")
    # provenance consistency
    prov = json.loads((ASSETS / f"{CAPTURE_STEM}.provenance.json").read_text(encoding="utf-8"))
    stdout_file = REPO / prov["stdout"]["file"]
    if sha256_file(stdout_file) != prov["stdout"]["sha256"]:
        problems.append("captured stdout hash does not match provenance")
    tree_sha, _ = source_tree_hash()
    if tree_sha != prov["source"]["tree_sha256"]:
        problems.append("source tree changed since the capture; re-run build_showcase.py")
    if prov["exit_code"] != 0 or prov["stderr"]["bytes"] != 0:
        problems.append("capture exit/stderr not clean")
    if prov["command"]["argv"] != CAPTURE_ARGV:
        problems.append("provenance argv mismatch")
    for needle in ("\\", ":/"):
        if needle in json.dumps(prov["command"]) or needle in json.dumps(prov["source"]):
            problems.append("provenance contains path-like text")
    # HTML structure
    page = (SHOWCASE / "index.html").read_text(encoding="utf-8")
    audit = _Audit()
    audit.feed(page)
    if audit.panels != 10:
        problems.append(f"expected 10 panels, found {audit.panels}")
    if audit.dupes:
        problems.append(f"duplicate ids: {audit.dupes}")
    gen = audit.badges.count("generated")
    act = audit.badges.count("actual")
    if (gen, act) != (9, 1):
        problems.append(f"badge counts generated={gen} actual={act}, expected 9/1")
    if audit.urls:
        problems.append(f"non-local references: {audit.urls}")
    if audit.imgs:
        problems.append("page unexpectedly contains <img> elements")
    for needle in ("prefers-reduced-motion", 'id="motion"', "aria-pressed", "100vh", "height:100%"):
        present = needle in page
        if needle in ("100vh", "height:100%"):
            if present:
                problems.append(f"HTML contains forbidden layout token {needle!r}")
        elif not present:
            problems.append(f"HTML missing {needle!r}")
    # the actual panel's text must equal the captured file
    actual_text = stdout_file.read_text(encoding="utf-8").rstrip("\n").split("\n")
    for line in actual_text:
        if html.escape(line, quote=True) not in page:
            problems.append("captured line missing from HTML panel 10")
            break
    problems += scan_text_outputs()
    return problems


# --------------------------------------------------------------------------
# README for the showcase folder
# --------------------------------------------------------------------------
SHOWCASE_README = f"""# ADAPT visual showcase

`index.html` is a self-contained, responsive page (inline CSS and JavaScript, no network
requests) with a view picker, an all-views mode, clickable highlights and a pausable
title animation. It honors `prefers-reduced-motion`: the title starts static for those users.

Ten equal views:

| # | View | Kind |
|---|------|------|
| 1 | Home screen with the original banner | generated |
| 2 | Host doctor | generated |
| 3 | Developer profiles | generated |
| 4 | Tool catalog and ROM plans | generated |
| 5 | Android capability matrix | generated |
| 6 | Tailscale connection stages (documented, not proven) | generated |
| 7 | Samsung DeX, native receiver (experimental) | generated |
| 8 | Rapid ADAPT test plan | generated |
| 9 | MCP and SSH access | generated |
| 10 | `{CAPTURE_DISPLAY}` | **actual** |

* **Generated** views are source-derived illustrations. They call the real rendering and
  planning code in `src/adapt_adb` with synthetic values. No host, device or network was used.
* **Actual** view 10 is the real stdout of an offline command, with normalized line endings in
  `../assets/{CAPTURE_STEM}.txt` with a hash in `../assets/{CAPTURE_STEM}.provenance.json`.
  The PNG/SVG next to it are a *rendered capture*, not a Windows Terminal screenshot.
* "9 of 10" counts equal views. It is not a pixel measurement.
* There are no Android screenshots. The animated title is a branding asset for this page and the
  README; the terminal banner is unchanged and static.

Rebuild everything (needs Pillow; nothing is downloaded):

```text
python scripts/build_showcase.py
python scripts/build_showcase.py --verify
```
"""


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def build() -> int:
    ASSETS.mkdir(parents=True, exist_ok=True)
    SHOWCASE.mkdir(parents=True, exist_ok=True)

    print("capturing: " + CAPTURE_DISPLAY)
    capture = run_capture()
    if capture["exit_code"] != 0 or capture["stderr"]:
        print(capture["stderr"].decode("utf-8", "replace"), file=sys.stderr)
        raise SystemExit(f"capture failed (exit {capture['exit_code']}); nothing was fabricated")
    stdout_text = capture["stdout"].decode("utf-8").replace("\r\n", "\n")
    if "ADAPT Android planning baseline" not in stdout_text:
        raise SystemExit("unexpected CLI output; refusing to publish it as a capture")
    norm_sha = sha256_bytes(stdout_text.encode("utf-8"))
    (ASSETS / f"{CAPTURE_STEM}.txt").write_text(stdout_text, encoding="utf-8", newline="\n")

    views = [view_home(), view_doctor(), view_dev_options(), view_tools(), view_android_matrix(),
             view_tailscale(), view_dex(), view_rapid(), view_mcp_ssh(),
             view_cli(stdout_text, norm_sha, capture["exit_code"])]
    nrows = max(len(v.rows) for v in views)
    wide = [f"view {v.n} ({len(r.plain)}): {r.plain!r}" for v in views if v.kind == "generated"
            for r in v.rows if len(r.plain) > COLS]
    if wide:
        raise SystemExit(f"rows exceed {COLS} columns:\n  " + "\n  ".join(wide))
    for v in views:
        keys = {tag for row in v.rows for tag in row.tags}
        for tag, _label, _detail in v.highlights:
            if tag not in keys:
                raise SystemExit(f"view {v.n}: highlight {tag!r} matches no row")
        for source in v.sources:
            if not (REPO / source).exists():
                raise SystemExit(f"view {v.n}: source reference missing: {source}")
    if nrows > 30:
        raise SystemExit(f"views too tall ({nrows} rows)")
    assert sum(v.kind == "generated" for v in views) == 9 and sum(v.kind == "actual" for v in views) == 1

    print("rendering title assets")
    title = build_title_assets()
    print("rendering capture assets")
    render_capture_assets(stdout_text, norm_sha, capture["exit_code"])
    prov = write_provenance(capture, stdout_text, norm_sha)
    print("rendering overview")
    build_overview(views, nrows).save(ASSETS / "showcase-overview.png", format="PNG", compress_level=9)
    print("writing html")
    (SHOWCASE / "index.html").write_text(build_html(views, nrows, prov), encoding="utf-8", newline="\n")
    (SHOWCASE / "README.md").write_text(SHOWCASE_README, encoding="utf-8", newline="\n")
    manifest = write_manifest({"font": load_font(13)[1]})

    problems = validate()
    print(f"views: {len(views)} (9 generated, 1 actual); terminal rows per view: {nrows}")
    print(f"title gif: {title['frames']} frames {title['size'][0]}x{title['size'][1]}; static png {title['static_size'][0]}x{title['static_size'][1]}")
    print(f"font: {manifest['font']}")
    for entry in manifest["assets"]:
        dims = f" {entry['width']}x{entry['height']}" if "width" in entry else ""
        print(f"  {entry['path']}  {entry['bytes']} bytes{dims}  sha256={entry['sha256'][:16]}")
    print(f"stdout sha256: {norm_sha}")
    print(f"source tree sha256: {prov['source']['tree_sha256']}")
    if problems:
        print("VALIDATION FAILED:", file=sys.stderr)
        for p in problems:
            print("  - " + p, file=sys.stderr)
        return 1
    print("validation: ok (hashes, dimensions, provenance, html structure, secret scan)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--verify", action="store_true", help="validate existing outputs without rebuilding")
    args = parser.parse_args()
    if args.verify:
        problems = validate()
        for p in problems:
            print("  - " + p, file=sys.stderr)
        print("validation: " + ("FAILED" if problems else "ok"))
        return 1 if problems else 0
    return build()


if __name__ == "__main__":
    raise SystemExit(main())
