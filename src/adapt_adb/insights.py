"""Official Google/AOSP tooling insights and best practices."""

from __future__ import annotations

from typing import Any

GUIDE_TOPICS: dict[str, dict[str, Any]] = {
    "testing": {
        "title": "Testing",
        "headline": "Official Google/AOSP Testing Stack",
        "summary": (
            "Espresso / UI Automator / instrumentation; adb shell am instrument; "
            "bounded logcat -d -t; monkey; dumpsys. CTS/VTS via Trade Federation "
            "(cts-tradefed, run cts) - ADAPT only supplies adb."
        ),
        "tools": [
            ("Espresso / UI Automator", "Official UI test frameworks for on-device app and system interaction"),
            ("Instrumentation (adb shell am instrument)", "Official Android test runner for JUnit instrumentation tests"),
            ("Bounded Logcat (adb logcat -d -t <N>)", "Bounded crash and log inspection; never run unbounded adb logcat in automated agents"),
            ("UI Monkey (adb shell monkey)", "Pseudo-random event stream stress testing"),
            ("dumpsys (adb shell dumpsys)", "System service state diagnostics (meminfo, cpuinfo, battery, gfxinfo)"),
            ("CTS / VTS via Trade Federation", "Official compatibility test suites run via 'cts-tradefed' ('run cts') - ADAPT only supplies adb"),
        ],
    },
    "rom": {
        "title": "ROM / Flashing",
        "headline": "Official Bootloader & Flashing Tooling",
        "summary": (
            "fastboot/flashing stay official fastboot in platform-tools; recovery sideload is "
            "adb sideload; compatibility is CTS/VTS/CTS-V; never wrap OEM unlock as a silent default."
        ),
        "tools": [
            ("fastboot", "Official bootloader flashing tool in platform-tools (fastboot flash, fastboot boot)"),
            ("Recovery Sideload (adb sideload)", "Official recovery update mechanism for signed OTA zips"),
            ("Compatibility Verification", "CTS / VTS / CTS-V verify AOSP specification compliance"),
            ("OEM Unlock Safety", "Never wrap OEM unlock as a silent default; requires explicit user device authorization"),
        ],
    },
    "ai": {
        "title": "On-Device AI",
        "headline": "Modern Android On-Device AI / GenAI",
        "summary": (
            "on-device GenAI -> ML Kit GenAI / Gemini Nano via AICore; custom models -> "
            "LiteRT (Play Services, GPU/NPU delegates). NNAPI is deprecated. Profile with Perfetto / dumpsys."
        ),
        "tools": [
            ("ML Kit GenAI / Gemini Nano via AICore", "System-level on-device foundation models and inference via Android AICore"),
            ("LiteRT (Play Services)", "Google's on-device runtime with GPU/NPU hardware acceleration delegates"),
            ("NNAPI Deprecation", "Android Neural Networks API (NNAPI) is deprecated; migrate to LiteRT with hardware delegates"),
            ("Profiling & Tracing", "Profile inference latency and memory with Perfetto and dumpsys"),
        ],
    },
    "wireless": {
        "title": "Wireless Debugging",
        "headline": "Wi-Fi Debugging on Android 11+",
        "summary": "pair then connect; two ports; same Wi-Fi; platform-tools >= 34.",
        "tools": [
            ("Pair then Connect", "Requires two separate steps: first pair using pairing code, then connect"),
            ("Two Different Ports", "Pairing port is temporary (in 'Pair using pairing code'); connect port is on main screen"),
            ("Same Wi-Fi Network", "Host workstation and Android device must reside on the same Wi-Fi network subnet"),
            ("Platform-Tools >= 34", "Official platform-tools version >= 34 recommended for stable TLS pairing and mDNS"),
        ],
    },
}


def get_guide_topics() -> list[str]:
    """Return list of supported guide topic names."""
    return list(GUIDE_TOPICS.keys())


def render_guide_table(topic: str | None = None) -> str:
    """Format and return a human-readable table of official tools."""
    lines: list[str] = []
    if topic is None or topic.strip().lower() == "all":
        lines.append("Official Google/AOSP Tooling Guides:")
        lines.append("")
        col_w = 10
        lines.append(f"{'Topic':<{col_w}}  {'Official Tools / Best Practices'}")
        lines.append(f"{'-' * col_w}  {'-' * 80}")
        for key, entry in GUIDE_TOPICS.items():
            summary = entry["summary"]
            lines.append(f"{key:<{col_w}}  {summary}")
        lines.append("")
        lines.append("Run 'adapt guide <topic>' for detailed guidance on a specific topic.")
        return "\n".join(lines)

    key = topic.strip().lower()
    entry = GUIDE_TOPICS.get(key)
    if entry is None:
        raise KeyError(
            f"Unknown guide topic '{topic}'. Available: {', '.join(GUIDE_TOPICS.keys())}"
        )

    lines.append(f"Guide: {entry['title']} ({entry['headline']})")
    lines.append(entry["summary"])
    lines.append("")
    tool_w = 42
    lines.append(f"{'Official Tool / Practice':<{tool_w}}  {'Usage / Role'}")
    lines.append(f"{'-' * tool_w}  {'-' * 60}")
    for tool_name, desc in entry["tools"]:
        lines.append(f"{tool_name:<{tool_w}}  {desc}")

    return "\n".join(lines)
