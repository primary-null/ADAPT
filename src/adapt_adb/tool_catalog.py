"""Read-only, goal-based tool catalog for Android development workflows.

This module intentionally does not execute discovered programs.  A PATH hit is
an observation, not a version check, provenance receipt, or trust decision.
"""

from __future__ import annotations

import json
import platform
import re
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import quote

import typer


SCHEMA_VERSION = "1"
CHECKED_AT = "2026-11-12"

GOALS: dict[str, tuple[str, ...]] = {
    "testing": ("platform-tools", "android-sdk-command-line", "perfetto"),
    "mirroring": ("platform-tools", "scrcpy"),
    "remote-worker": ("openssh",),
    "aosp-build": ("git", "repo", "gpg", "java", "python", "ccache"),
    "lineage-build": ("git", "repo", "gpg", "java", "python", "ccache"),
    "image-inspection": (
        "python",
        "avbtool",
        "unpack_bootimg",
        "simg2img",
        "lpunpack",
        "payload-dumper-go",
    ),
}

GOAL_DESCRIPTIONS = {
    "remote-worker": "Prepare an SSH client for an existing ADAPT worker; Platform-Tools and device access belong on that worker.",
    "testing": "Test Android apps and systems with official SDK tooling and traces.",
    "mirroring": "Mirror and control a user-authorized Android device with adb and scrcpy.",
    "aosp-build": "Prepare for a branch-specific generic AOSP build on an assessed Linux host.",
    "lineage-build": "Prepare for an official, device- and branch-specific LineageOS build.",
    "image-inspection": "Inspect user-supplied Android firmware artifacts without a device.",
}


@dataclass(frozen=True)
class ToolRecord:
    """Curated provenance and acquisition metadata for one tool family."""

    id: str
    name: str
    purpose: str
    executable_candidates: tuple[str, ...]
    dependency_hints: tuple[str, ...]
    supported_hosts: tuple[str, ...]
    source_url: str
    license_name: str
    license_url: str
    acquisition: str
    acquisition_url: str
    action_class: int
    optional: bool = False
    checked_at: str = CHECKED_AT
    research_scope: str = "Static upstream/source review; no live metadata or version verification."


_AOSP_LICENSE = "https://source.android.com/docs/setup/contribute/licenses"
_SDK_TERMS = "https://developer.android.com/studio/terms"

TOOL_CATALOG: dict[str, ToolRecord] = {
    "openssh": ToolRecord(
        id="openssh", name="OpenSSH client",
        purpose="Explicit SSH worker inspection and optional MCP stdio transport; not an automatic ADB fallback.",
        executable_candidates=("ssh",),
        dependency_hints=("The worker needs its own SSH server, verified host key, authorized account, ADAPT and Android device access.",),
        supported_hosts=("windows-native", "linux-native", "wsl"),
        source_url="https://www.openssh.com/", license_name="BSD-style and bundled third-party licenses",
        license_url="https://www.openssh.com/license.html",
        acquisition="Windows: enable the OpenSSH Client optional feature; Debian: use the openssh-client OS package. ADAPT detects only; no OS feature/package is installed automatically.",
        acquisition_url="https://www.openssh.com/portable.html", action_class=2, optional=True,
    ),
    "platform-tools": ToolRecord(
        id="platform-tools",
        name="Android SDK Platform-Tools (adb and fastboot)",
        purpose="Host/device communication for testing and mirroring; presence never authorizes device mutation.",
        executable_candidates=("adb", "fastboot"),
        dependency_hints=("USB drivers or udev rules are separate privileged/manual concerns.",),
        supported_hosts=("windows-native", "linux-native", "wsl"),
        source_url="https://developer.android.com/tools/releases/platform-tools",
        license_name="Android SDK License Agreement",
        license_url=_SDK_TERMS,
        acquisition="Use ADAPT's existing Google-only verified path: adapt setup. Review USB setup separately.",
        acquisition_url="https://developer.android.com/tools/releases/platform-tools#downloads",
        action_class=1,
    ),
    "scrcpy": ToolRecord(
        id="scrcpy",
        name="scrcpy",
        purpose="Mirror and control a user-authorized Android device through adb.",
        executable_candidates=("scrcpy",),
        dependency_hints=("Requires adb; use the project's official release or a reviewed OS package.",),
        supported_hosts=("windows-native", "linux-native", "wsl"),
        source_url="https://github.com/Genymobile/scrcpy",
        license_name="Apache-2.0",
        license_url="https://github.com/Genymobile/scrcpy/blob/master/LICENSE",
        acquisition="Preview `adapt tools install scrcpy --dry-run`, then confirm installation. Managed assets: Windows x64/x86/ARM64 or Linux x86-64; other hosts require upstream review.",
        acquisition_url="https://github.com/Genymobile/scrcpy/releases",
        action_class=1,
    ),
    "git": ToolRecord(
        id="git",
        name="Git",
        purpose="Version-control client required underneath repo and Android source trees.",
        executable_candidates=("git",),
        dependency_hints=("Windows Git does not satisfy a separate WSL or Linux build environment.",),
        supported_hosts=("windows-native", "linux-native", "wsl"),
        source_url="https://git-scm.com/downloads",
        license_name="GPL-2.0",
        license_url="https://git-scm.com/about/free-and-open-source",
        acquisition="Review and install the package for the exact host environment that will own the source tree.",
        acquisition_url="https://git-scm.com/downloads",
        action_class=2,
    ),
    "repo": ToolRecord(
        id="repo",
        name="Repo launcher",
        purpose="Coordinate the many Git repositories in AOSP and LineageOS manifests.",
        executable_candidates=("repo",),
        dependency_hints=("Requires Git and GPG; pin manifest branch/tag and record resolved revisions.",),
        supported_hosts=("linux-native", "wsl"),
        source_url="https://gerrit.googlesource.com/git-repo/+/HEAD/README.md",
        license_name="Apache-2.0",
        license_url="https://gerrit.googlesource.com/git-repo/+/HEAD/LICENSE",
        acquisition="Follow the selected AOSP or LineageOS guide; review the distro package or official launcher instructions manually.",
        acquisition_url="https://source.android.com/docs/setup/download#installing-repo",
        action_class=2,
    ),
    "gpg": ToolRecord(
        id="gpg",
        name="GnuPG",
        purpose="Verify signed source tags and release evidence where upstream provides it.",
        executable_candidates=("gpg", "gpg2"),
        dependency_hints=("Signature verification still requires an authenticated signer/key policy.",),
        supported_hosts=("windows-native", "linux-native", "wsl"),
        source_url="https://gnupg.org/download/",
        license_name="GPL-3.0-or-later",
        license_url="https://gnupg.org/copying.html",
        acquisition="Review the operating-system package or GnuPG's official distribution for this host.",
        acquisition_url="https://gnupg.org/download/",
        action_class=2,
    ),
    "java": ToolRecord(
        id="java",
        name="Java / JDK",
        purpose="Provide Java only when the selected Android or LineageOS branch does not supply its required JDK.",
        executable_candidates=("java", "javac"),
        dependency_hints=(
            "There is no universal JDK recommendation across Android branches.",
            "Prefer source-provided prebuilts when the selected official guide says they are authoritative.",
        ),
        supported_hosts=("windows-native", "linux-native", "wsl"),
        source_url="https://source.android.com/docs/setup/start/requirements",
        license_name="Branch/provider specific; OpenJDK is GPL-2.0 with Classpath Exception",
        license_url="https://openjdk.org/legal/gplv2+ce.html",
        acquisition="Select the branch first, then follow that branch's official guide; do not replace a source-provided JDK globally.",
        acquisition_url="https://source.android.com/docs/setup/start/older-versions",
        action_class=2,
    ),
    "python": ToolRecord(
        id="python",
        name="Python",
        purpose="Run branch-specific build helpers and source-tree image inspection scripts.",
        executable_candidates=("python3", "python"),
        dependency_hints=("Current sources generally use Python 3; historical branches may require isolated legacy environments."),
        supported_hosts=("windows-native", "linux-native", "wsl"),
        source_url="https://www.python.org/downloads/",
        license_name="Python Software Foundation License",
        license_url="https://docs.python.org/3/license.html",
        acquisition="Use the selected branch's official requirement; review a host package or isolated environment rather than changing global Python blindly.",
        acquisition_url="https://www.python.org/downloads/",
        action_class=2,
    ),
    "ccache": ToolRecord(
        id="ccache",
        name="ccache",
        purpose="Optionally cache compiler output for repeated source builds.",
        executable_candidates=("ccache",),
        dependency_hints=("Requires separately planned storage and branch/build-system configuration."),
        supported_hosts=("linux-native", "wsl"),
        source_url="https://ccache.dev/",
        license_name="GPL-3.0-or-later",
        license_url="https://github.com/ccache/ccache/blob/master/LICENSE.adoc",
        acquisition="Optional: review the Linux distribution package and allocate cache storage explicitly.",
        acquisition_url="https://ccache.dev/download.html",
        action_class=2,
        optional=True,
    ),
    "perfetto": ToolRecord(
        id="perfetto",
        name="Perfetto",
        purpose="Capture and analyze Android performance traces using official Perfetto tooling.",
        executable_candidates=("perfetto", "trace_processor_shell"),
        dependency_hints=("Host tools and on-device tracing have different acquisition and authorization paths."),
        supported_hosts=("windows-native", "linux-native", "wsl"),
        source_url="https://perfetto.dev/docs/",
        license_name="Apache-2.0",
        license_url="https://github.com/google/perfetto/blob/main/LICENSE",
        acquisition="Use the official quickstart/download guidance for the chosen trace workflow and host.",
        acquisition_url="https://perfetto.dev/docs/quickstart/trace-analysis",
        action_class=2,
        optional=True,
    ),
    "android-sdk-command-line": ToolRecord(
        id="android-sdk-command-line",
        name="Android SDK Command-Line Tools",
        purpose="Manage SDK packages for Android app testing and build workflows.",
        executable_candidates=("sdkmanager", "avdmanager"),
        dependency_hints=("SDK packages have their own licenses and versioned package identifiers."),
        supported_hosts=("windows-native", "linux-native", "wsl"),
        source_url="https://developer.android.com/tools",
        license_name="Android SDK License Agreement",
        license_url=_SDK_TERMS,
        acquisition="Download command-line tools only from Android Developers, then review each requested SDK package and license.",
        acquisition_url="https://developer.android.com/studio#command-tools",
        action_class=2,
    ),
    "avbtool": ToolRecord(
        id="avbtool",
        name="AOSP avbtool",
        purpose="Inspect AVB footers and vbmeta descriptors; inspection does not establish image-set trust or compatibility.",
        executable_candidates=("avbtool", "avbtool.py"),
        dependency_hints=("Record the supplying AOSP tag/commit and required libavb version."),
        supported_hosts=("windows-native", "linux-native", "wsl"),
        source_url="https://android.googlesource.com/platform/external/avb/+/refs/heads/main/avbtool.py",
        license_name="Apache-2.0",
        license_url="https://android.googlesource.com/platform/external/avb/+/refs/heads/main/LICENSE",
        acquisition="Prefer the tool from an existing, pinned AOSP source tree; do not execute an unreviewed raw download.",
        acquisition_url="https://android.googlesource.com/platform/external/avb/+/refs/heads/main/README.md",
        action_class=3,
    ),
    "unpack_bootimg": ToolRecord(
        id="unpack_bootimg",
        name="AOSP unpack_bootimg",
        purpose="Inspect boot, recovery, and vendor_boot image structure without selecting a flash target.",
        executable_candidates=("unpack_bootimg", "unpack_bootimg.py"),
        dependency_hints=("Boot, init_boot, vendor_boot, and recovery layouts vary by Android/product generation."),
        supported_hosts=("windows-native", "linux-native", "wsl"),
        source_url="https://android.googlesource.com/platform/system/tools/mkbootimg/+/refs/heads/main/unpack_bootimg.py",
        license_name="Apache-2.0",
        license_url="https://android.googlesource.com/platform/system/tools/mkbootimg/+/refs/heads/main/LICENSE",
        acquisition="Prefer the script/tool from a pinned AOSP source tree and review it before execution.",
        acquisition_url="https://android.googlesource.com/platform/system/tools/mkbootimg/",
        action_class=3,
    ),
    "simg2img": ToolRecord(
        id="simg2img",
        name="AOSP simg2img",
        purpose="Convert an Android sparse image to a new raw-image destination for inspection.",
        executable_candidates=("simg2img",),
        dependency_hints=("Normally built as a host tool from AOSP libsparse; never overwrite the input image."),
        supported_hosts=("linux-native", "wsl"),
        source_url="https://android.googlesource.com/platform/system/core/+/refs/heads/main/libsparse/Android.bp",
        license_name="Apache-2.0",
        license_url=_AOSP_LICENSE,
        acquisition="Build/use the host tool from a pinned AOSP tree or review a signed distribution package manually.",
        acquisition_url="https://android.googlesource.com/platform/system/core/+/refs/heads/main/libsparse/",
        action_class=3,
    ),
    "lpunpack": ToolRecord(
        id="lpunpack",
        name="AOSP lpunpack",
        purpose="Extract selected logical partitions from a user-supplied super image into a new destination.",
        executable_candidates=("lpunpack",),
        dependency_hints=("Normally built from AOSP partition_tools; dynamic-partition metadata is product-specific."),
        supported_hosts=("linux-native", "wsl"),
        source_url="https://android.googlesource.com/platform/system/extras/+/refs/heads/main/partition_tools/Android.bp",
        license_name="Apache-2.0",
        license_url=_AOSP_LICENSE,
        acquisition="Build/use the host tool from a pinned AOSP tree; do not substitute an arbitrary forum binary.",
        acquisition_url="https://android.googlesource.com/platform/system/extras/+/refs/heads/main/partition_tools/",
        action_class=3,
    ),
    "payload-dumper-go": ToolRecord(
        id="payload-dumper-go",
        name="payload-dumper-go (optional third-party upstream)",
        purpose="Optionally list or extract partitions from Android OTA payloads supplied by the user.",
        executable_candidates=("payload-dumper-go",),
        dependency_hints=("Not an Android or LineageOS official tool; ADAPT has no provisioning allowlist for it."),
        supported_hosts=("windows-native", "linux-native", "wsl"),
        source_url="https://github.com/ssut/payload-dumper-go",
        license_name="Apache-2.0",
        license_url="https://github.com/ssut/payload-dumper-go/blob/master/LICENSE",
        acquisition="Optional: manually review the named upstream release, asset, architecture, published hash, and license. ADAPT does not download it.",
        acquisition_url="https://github.com/ssut/payload-dumper-go/releases",
        action_class=2,
        optional=True,
    ),
}

_TOOL_ALIASES = {
    "ssh": "openssh",
    "adb": "platform-tools",
    "fastboot": "platform-tools",
    "sdkmanager": "android-sdk-command-line",
    "python3": "python",
    "javac": "java",
}


@dataclass(frozen=True)
class HostEnvironment:
    os: str
    kind: str
    arch: str
    kernel_release: str
    label: str
    is_wsl: bool


def _read_kernel_marker(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError:
        return ""


def _normalize_arch(machine: str) -> str:
    value = machine.strip().lower()
    if value in {"x86_64", "amd64", "x64"}:
        return "x86_64"
    if value in {"aarch64", "arm64"}:
        return "arm64"
    return value or "unknown"


def detect_host(
    *,
    system: str | None = None,
    machine: str | None = None,
    kernel_release: str | None = None,
    kernel_markers: Iterable[str] | None = None,
) -> HostEnvironment:
    """Classify only the current process environment, without starting WSL."""

    system_name = (system or platform.system()).strip().lower()
    arch = _normalize_arch(machine or platform.machine())
    release = kernel_release if kernel_release is not None else platform.release()

    if system_name == "windows":
        return HostEnvironment(
            os="windows",
            kind="windows-native",
            arch=arch,
            kernel_release=release,
            label="Windows native",
            is_wsl=False,
        )

    if system_name == "linux":
        markers = list(kernel_markers or ())
        if kernel_markers is None:
            markers.extend(
                (
                    platform.version(),
                    _read_kernel_marker(Path("/proc/sys/kernel/osrelease")),
                    _read_kernel_marker(Path("/proc/version")),
                )
            )
        is_wsl = any(
            token in " ".join([release, *markers]).lower()
            for token in ("microsoft", "wsl")
        )
        return HostEnvironment(
            os="linux",
            kind="wsl" if is_wsl else "linux-native",
            arch=arch,
            kernel_release=release,
            label="WSL Linux environment" if is_wsl else "Native Linux",
            is_wsl=is_wsl,
        )

    return HostEnvironment(
        os=system_name or "unknown",
        kind="unsupported",
        arch=arch,
        kernel_release=release,
        label=f"Unsupported host ({system_name or 'unknown'})",
        is_wsl=False,
    )


def _host_payload(host: HostEnvironment) -> dict[str, object]:
    payload: dict[str, object] = asdict(host)
    if host.kind == "windows-native":
        payload["wsl_assessment"] = {
            "status": "not_assessed",
            "reason": "This command does not run wsl.exe or start/probe any distribution; Windows PATH results are not Linux results.",
        }
    return payload


def _catalog_payload(record: ToolRecord) -> dict[str, object]:
    payload = asdict(record)
    for key in ("executable_candidates", "dependency_hints", "supported_hosts"):
        payload[key] = list(payload[key])
    payload["goals"] = [goal for goal, tool_ids in GOALS.items() if record.id in tool_ids]
    return payload


def locate_tool(
    record: ToolRecord,
    *,
    which: Callable[[str], str | None] = shutil.which,
) -> dict[str, object]:
    """Locate candidates on this environment's PATH without executing them."""

    located: list[dict[str, str]] = []
    for candidate in record.executable_candidates:
        try:
            path = which(candidate)
        except (OSError, TypeError, ValueError):
            path = None
        if path:
            located.append({"candidate": candidate, "path": str(path)})

    return {
        "status": "found" if located else "missing",
        "locations": located,
        "version": "not_probed",
        "trusted": False,
        "verification": (
            "Found on the current environment PATH only; origin, integrity, version, and compatibility were not verified."
            if located
            else "No executable candidate was found on the current environment PATH."
        ),
    }


def _canonical_tool_id(tool: str) -> str | None:
    value = tool.strip().lower()
    value = _TOOL_ALIASES.get(value, value)
    return value if value in TOOL_CATALOG else None


def _goal_error(goal: str) -> str | None:
    if goal in GOALS:
        return None
    return f"Unknown goal '{goal}'. Choose one of: {', '.join(GOALS)}."


def build_list_report(
    goal: str | None = None,
    *,
    host: HostEnvironment | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> dict[str, object]:
    """Build a deterministic inventory report for tests and CLI rendering."""

    active_host = host or detect_host()
    selected_goal = goal.strip().lower() if goal else None
    if selected_goal and (error := _goal_error(selected_goal)):
        return _error_report("tools list", "invalid_goal", error)

    ids = GOALS[selected_goal] if selected_goal else tuple(TOOL_CATALOG)
    tools = []
    for tool_id in ids:
        record = TOOL_CATALOG[tool_id]
        item = _catalog_payload(record)
        item["detection"] = locate_tool(record, which=which)
        item["host_catalog_status"] = (
            "catalogued" if active_host.kind in record.supported_hosts else "not_catalogued_for_host"
        )
        tools.append(item)

    return {
        "schema_version": SCHEMA_VERSION,
        "command": "tools list",
        "status": "ok",
        "exit_code": 0,
        "goal": selected_goal,
        "goal_description": GOAL_DESCRIPTIONS.get(selected_goal) if selected_goal else None,
        "host": _host_payload(active_host),
        "detection_policy": "PATH lookup only; no discovered executable was run.",
        "tools": tools,
    }


def build_info_report(
    tool: str,
    *,
    host: HostEnvironment | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> dict[str, object]:
    tool_id = _canonical_tool_id(tool)
    if tool_id is None:
        return _error_report(
            "tools info",
            "invalid_tool",
            f"Unknown tool '{tool}'. Choose one of: {', '.join(TOOL_CATALOG)}.",
        )
    active_host = host or detect_host()
    record = TOOL_CATALOG[tool_id]
    payload = _catalog_payload(record)
    payload["detection"] = locate_tool(record, which=which)
    payload["host_catalog_status"] = (
        "catalogued" if active_host.kind in record.supported_hosts else "not_catalogued_for_host"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "command": "tools info",
        "status": "ok",
        "exit_code": 0,
        "host": _host_payload(active_host),
        "tool": payload,
    }


_CONTEXT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+/-]{0,127}$")


def _validate_context(name: str, value: str | None) -> str | None:
    if value is None:
        return None
    if not _CONTEXT_PATTERN.fullmatch(value):
        return f"Invalid {name}: use a branch/device identifier, not whitespace, a URL, or shell syntax."
    return None


def _host_assessment(goal: str, host: HostEnvironment) -> dict[str, str]:
    build_goal = goal in {"aosp-build", "lineage-build"}
    if host.kind == "unsupported":
        return {
            "classification": "unsupported",
            "reason": "This host operating system is not covered by the curated catalog.",
        }
    if build_goal and host.arch != "x86_64":
        return {
            "classification": "unsupported_architecture",
            "reason": "AOSP build requirements are for 64-bit x86 Linux; this architecture is not certified by this catalog.",
        }
    if build_goal and host.kind == "windows-native":
        return {
            "classification": "unsupported",
            "reason": "Native Windows is not a supported current AOSP/LineageOS compilation host; assess a separate Linux environment.",
        }
    if build_goal and host.kind == "wsl":
        return {
            "classification": "qualified_unverified",
            "reason": "WSL is a separate Linux VM environment and needs branch-guide, distro, filesystem, resource, and package review.",
        }
    if build_goal and host.kind == "linux-native":
        return {
            "classification": "requires_upstream_review",
            "reason": "Generic Linux may qualify, but the selected upstream version guide and distribution packages must be reviewed; this is not readiness certification.",
        }
    if host.kind == "wsl":
        return {
            "classification": "qualified_unverified",
            "reason": "Tools detected inside WSL are separate from native Windows and remain unverified.",
        }
    return {
        "classification": "catalogued_unverified",
        "reason": "The goal is catalogued on this host, but PATH presence is not provenance or compatibility verification.",
    }


def _lineage_context(branch: str | None, device: str | None) -> dict[str, object]:
    missing = [name for name, value in (("branch", branch), ("device", device)) if not value]
    if missing:
        return {
            "status": "incomplete",
            "missing": missing,
            "message": "LineageOS planning requires both --branch and --device.",
            "official_url": "https://wiki.lineageos.org/devices/",
            "verification": "Select the device's official page and a branch listed there.",
        }
    device_url = quote(device or "", safe="")
    return {
        "status": "user_supplied_unverified",
        "branch": branch,
        "device": device,
        "official_url": f"https://wiki.lineageos.org/devices/{device_url}/build/",
        "verification": "The values came from the user and were not checked against the live LineageOS site. Confirm that this exact branch appears on this exact device page.",
    }


def _aosp_context(branch: str | None) -> dict[str, object]:
    return {
        "status": "user_supplied_unverified" if branch else "branch_not_selected",
        "branch": branch,
        "official_url": "https://source.android.com/docs/setup/reference/build-numbers#source-code-tags-and-builds",
        "verification": (
            "The branch/tag came from the user and was not checked live; prefer a release tag or record the resolved manifest revisions."
            if branch
            else "Choose an official branch/tag before selecting Java, Python, packages, storage, or source inputs."
        ),
    }


def _action_for(record: ToolRecord) -> dict[str, object]:
    if record.action_class == 1 and record.id == "platform-tools":
        next_step = "Review and, if desired, run the existing `adapt setup` workflow for official Platform-Tools."
    elif record.action_class == 3:
        next_step = "Select and pin the supplying AOSP source revision, then review the source-tree host-tool build/acquisition path."
    else:
        next_step = record.acquisition
    return {
        "action_class": record.action_class,
        "tool": record.id,
        "mode": "explain_only",
        "next_step": next_step,
        "source_url": record.source_url,
        "acquisition_url": record.acquisition_url,
        "requires_explicit_follow_up": True,
    }


def build_plan_report(
    goal: str,
    *,
    branch: str | None = None,
    device: str | None = None,
    host: HostEnvironment | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> dict[str, object]:
    """Create an explain-only plan; never installs, downloads, or touches a device."""

    selected_goal = goal.strip().lower()
    if error := _goal_error(selected_goal):
        return _error_report("tools plan", "invalid_goal", error)
    for name, value in (("branch", branch), ("device", device)):
        if error := _validate_context(name, value):
            return _error_report("tools plan", "invalid_context", error)

    if selected_goal == "lineage-build":
        context = _lineage_context(branch, device)
    elif selected_goal == "aosp-build":
        if device:
            return _error_report(
                "tools plan",
                "invalid_context",
                "--device applies to lineage-build; generic AOSP planning does not infer phone support.",
            )
        context = _aosp_context(branch)
    else:
        if branch or device:
            return _error_report(
                "tools plan",
                "invalid_context",
                f"--branch and --device do not apply to the {selected_goal} goal.",
            )
        context = {"status": "not_required"}

    active_host = host or detect_host()
    inventory = build_list_report(selected_goal, host=active_host, which=which)
    tool_items = inventory["tools"]
    assert isinstance(tool_items, list)
    missing = [
        item
        for item in tool_items
        if isinstance(item, dict)
        and isinstance(item.get("detection"), dict)
        and item["detection"].get("status") == "missing"
    ]

    incomplete = context.get("status") == "incomplete"
    status = "incomplete" if incomplete else "explain_only"
    exit_code = 2 if incomplete else 0
    actions = [
        {
            "action_class": 0,
            "mode": "completed_observation",
            "next_step": "Review PATH-only observations; do not treat them as version, origin, trust, or readiness evidence.",
        }
    ]
    actions.extend(_action_for(TOOL_CATALOG[str(item["id"])]) for item in missing)

    return {
        "schema_version": SCHEMA_VERSION,
        "command": "tools plan",
        "status": status,
        "exit_code": exit_code,
        "goal": selected_goal,
        "goal_description": GOAL_DESCRIPTIONS[selected_goal],
        "host": _host_payload(active_host),
        "host_assessment": _host_assessment(selected_goal, active_host),
        "target_context": context,
        "tools": tool_items,
        "actions": actions,
        "action_classes": {
            "0": "Observe only: current PATH and supplied context; no discovered binary executed.",
            "1": "User-scope provision: Platform-Tools via adapt setup or pinned scrcpy via tools install, after user choice.",
            "2": "Privileged or manual: OS packages, drivers, SDK components, or upstream releases; this plan stops.",
            "3": "Project/device mutation: source sync/build, blob extraction, image changes, or device work require a separate authorized workflow.",
            "4": "Prohibited automatic behavior: destructive device actions, arbitrary scripts/binaries, privilege escalation, or trust bypass.",
        },
        "non_actions": [
            "No installs, downloads, network requests, package-manager changes, root/admin actions, or external writes were performed.",
            "No WSL distribution was listed, started, stopped, configured, or probed.",
            "No device was accessed and no bootloader, partition, recovery, or data mutation was planned.",
            "No source tree was synchronized or built; no image was extracted, repacked, signed, booted, or installed.",
        ],
        "limitations": [
            "PATH locations are not trusted or version-verified.",
            "Live upstream support, branch availability, package names, licenses, and release metadata were not refreshed at runtime.",
            "Source inputs such as device trees, kernels, proprietary files, partition layout, and signing policy are outside binary-tool detection.",
        ],
    }


def _error_report(command: str, status: str, error: str) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "status": status,
        "exit_code": 2,
        "error": error,
    }


def _emit_json(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    sys.stdout.flush()


def _render_list(report: dict[str, object]) -> str:
    if "error" in report:
        return f"Error: {report['error']}"
    host = report["host"]
    assert isinstance(host, dict)
    lines = [f"Host environment: {host['label']} ({host['arch']})"]
    if report.get("goal"):
        lines.append(f"Goal: {report['goal']} - {report['goal_description']}")
    if "wsl_assessment" in host:
        lines.append("WSL: not assessed (no distribution was listed, started, or probed).")
    lines.append("Detection: PATH lookup only; found does not mean trusted, version-verified, or compatible.")
    lines.append("")
    tools = report["tools"]
    assert isinstance(tools, list)
    for item in tools:
        assert isinstance(item, dict)
        detection = item["detection"]
        assert isinstance(detection, dict)
        locations = detection["locations"]
        assert isinstance(locations, list)
        suffix = ", ".join(str(loc["path"]) for loc in locations) if locations else "not on PATH"
        lines.append(f"{item['id']}: {detection['status']} ({suffix})")
        lines.append(f"  {item['purpose']}")
        lines.append(f"  Get/review: {item['acquisition_url']}")
    return "\n".join(lines)


def _render_info(report: dict[str, object]) -> str:
    if "error" in report:
        return f"Error: {report['error']}"
    tool = report["tool"]
    assert isinstance(tool, dict)
    detection = tool["detection"]
    assert isinstance(detection, dict)
    lines = [
        f"{tool['name']} [{tool['id']}]",
        f"Status: {detection['status']} (PATH only; version not probed; trusted=false)",
        f"Purpose: {tool['purpose']}",
        f"Goals: {', '.join(tool['goals'])}",
        f"Hosts: {', '.join(tool['supported_hosts'])}",
        f"Dependencies: {'; '.join(tool['dependency_hints'])}",
        f"Official/upstream source: {tool['source_url']}",
        f"License: {tool['license_name']} - {tool['license_url']}",
        f"Acquisition: {tool['acquisition']}",
        f"Acquisition link: {tool['acquisition_url']}",
        f"Checked: {tool['checked_at']} ({tool['research_scope']})",
    ]
    return "\n".join(lines)


def _render_plan(report: dict[str, object]) -> str:
    if "error" in report:
        return f"Error: {report['error']}"
    assessment = report["host_assessment"]
    context = report["target_context"]
    assert isinstance(assessment, dict) and isinstance(context, dict)
    lines = [
        f"Goal plan: {report['goal']} ({report['status']})",
        f"Host: {report['host']['label']} / {assessment['classification']}",  # type: ignore[index]
        f"  {assessment['reason']}",
        f"Target context: {context['status']}",
    ]
    if context.get("message"):
        lines.append(f"  {context['message']}")
    if context.get("official_url"):
        lines.append(f"  Official context: {context['official_url']}")
    if context.get("verification"):
        lines.append(f"  {context['verification']}")
    lines.extend(("", "Explain-only next actions:"))
    actions = report["actions"]
    assert isinstance(actions, list)
    for action in actions:
        assert isinstance(action, dict)
        lines.append(f"  Class {action['action_class']}: {action['next_step']}")
        if action.get("acquisition_url"):
            lines.append(f"    {action['acquisition_url']}")
    lines.extend(("", "Boundaries:"))
    non_actions = report["non_actions"]
    assert isinstance(non_actions, list)
    lines.extend(f"  - {item}" for item in non_actions)
    return "\n".join(lines)


tools_app = typer.Typer(
    name="tools",
    help="Explore Android tools and install supported, verified optional tools.",
    no_args_is_help=True,
    add_completion=False,
)


@tools_app.command("list")
def list_tools(
    goal: str | None = typer.Option(None, "--goal", help=f"Filter by goal: {', '.join(GOALS)}."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON only."),
) -> None:
    """List curated tools and observe their candidates on the current PATH."""

    report = build_list_report(goal)
    if json_output:
        _emit_json(report)
    elif "error" in report:
        typer.echo(_render_list(report), err=True)
    else:
        typer.echo(_render_list(report))
    if report["exit_code"]:
        raise typer.Exit(code=int(report["exit_code"]))


@tools_app.command("info")
def info_tool(
    tool: str = typer.Argument(..., help="Catalog tool ID (or adb/fastboot alias)."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON only."),
) -> None:
    """Show provenance, license, acquisition, and PATH observation for a tool."""

    report = build_info_report(tool)
    if json_output:
        _emit_json(report)
    elif "error" in report:
        typer.echo(_render_info(report), err=True)
    else:
        typer.echo(_render_info(report))
    if report["exit_code"]:
        raise typer.Exit(code=int(report["exit_code"]))


@tools_app.command("plan")
def plan_tools(
    goal: str = typer.Argument(..., help=f"Goal: {', '.join(GOALS)}."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON only."),
    branch: str | None = typer.Option(None, "--branch", help="User-supplied AOSP/LineageOS branch or tag."),
    device: str | None = typer.Option(None, "--device", help="User-supplied LineageOS device codename."),
) -> None:
    """Explain missing tools and acquisition boundaries without changing the host."""

    report = build_plan_report(goal, branch=branch, device=device)
    if json_output:
        _emit_json(report)
    elif "error" in report:
        typer.echo(_render_plan(report), err=True)
    else:
        typer.echo(_render_plan(report))
    if report["exit_code"]:
        raise typer.Exit(code=int(report["exit_code"]))


@tools_app.command("install")
def install_tool(
    tool: str = typer.Argument(..., help="Optional tool to install (currently scrcpy)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Approve the pinned user-scope installation."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview only; no download, writes, or confirmation."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON; a new installation requires --yes."),
) -> None:
    """Reuse an existing scrcpy or install the verified pinned release after confirmation."""
    from adapt_adb.scrcpy_install import (
        ScrcpyConfirmationRequiredError, ScrcpyInstallConflictError,
        ScrcpyInstallError, UnsupportedScrcpyHostError, install_scrcpy,
    )

    def no_json_prompt(_message: str) -> bool:
        raise ScrcpyConfirmationRequiredError("JSON installation requires --yes; use --dry-run to preview.")

    if tool.strip().casefold() != "scrcpy":
        payload = {"status": "unsupported_tool", "exit_code": 2, "error": "Managed installation currently supports only scrcpy."}
    else:
        try:
            payload = install_scrcpy(yes=yes, dry_run=dry_run, confirm=no_json_prompt if json_output and not yes else None)
            payload["exit_code"] = 0
        except ScrcpyInstallError as exc:
            refused = isinstance(exc, (ScrcpyConfirmationRequiredError, ScrcpyInstallConflictError, UnsupportedScrcpyHostError))
            payload = {"status": "refused" if refused else "failed", "exit_code": 2 if refused else 1, "error": str(exc)}
        except OSError:
            payload = {"status": "failed", "exit_code": 1, "error": "Cannot access the managed tool directory; no existing installation was replaced."}
    if json_output:
        _emit_json(payload)
    elif payload["exit_code"]:
        typer.echo(f"FAILED / {payload['error']}", err=True)
    elif payload["status"] == "cancelled":
        typer.echo("CANCELLED / Nothing installed.")
    elif payload["status"] == "dry_run":
        typer.echo(f"PLAN / scrcpy {payload['version']}\n  Source: {payload['asset']['url']}\n  Destination: {payload['target_dir']}\n  No download or changes performed. Run without --dry-run to confirm installation.")
    else:
        typer.echo(f"{'DONE' if payload['status'] == 'installed' else 'READY'} / {payload['status']}: {payload['binary_path']}")
    if payload["exit_code"]:
        raise typer.Exit(int(payload["exit_code"]))


__all__ = [
    "CHECKED_AT",
    "GOALS",
    "HostEnvironment",
    "TOOL_CATALOG",
    "ToolRecord",
    "build_info_report",
    "build_list_report",
    "build_plan_report",
    "detect_host",
    "locate_tool",
    "tools_app",
]
