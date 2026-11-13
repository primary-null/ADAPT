"""Offline tests for the read-only goal-based tool catalog."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from adapt_adb.tool_catalog import (
    GOALS,
    TOOL_CATALOG,
    HostEnvironment,
    build_info_report,
    build_list_report,
    build_plan_report,
    detect_host,
    tools_app,
)


runner = CliRunner()


def _which(mapping: dict[str, str]):
    return lambda candidate: mapping.get(candidate)


WINDOWS = HostEnvironment(
    os="windows",
    kind="windows-native",
    arch="x86_64",
    kernel_release="10.0.26100",
    label="Windows native",
    is_wsl=False,
)
LINUX = HostEnvironment(
    os="linux",
    kind="linux-native",
    arch="x86_64",
    kernel_release="6.12.0",
    label="Native Linux",
    is_wsl=False,
)
WSL = HostEnvironment(
    os="linux",
    kind="wsl",
    arch="x86_64",
    kernel_release="6.6.87.2-microsoft-standard-WSL2",
    label="WSL Linux environment",
    is_wsl=True,
)


def test_catalog_has_all_requested_goals_and_curated_tools():
    assert set(GOALS) == {
        "testing",
        "mirroring",
        "aosp-build",
        "lineage-build",
        "image-inspection",
        "remote-worker",
    }
    assert {
        "platform-tools",
        "scrcpy",
        "git",
        "repo",
        "gpg",
        "java",
        "python",
        "ccache",
        "perfetto",
        "android-sdk-command-line",
        "avbtool",
        "unpack_bootimg",
        "simg2img",
        "lpunpack",
        "payload-dumper-go",
    } <= set(TOOL_CATALOG)

    for record in TOOL_CATALOG.values():
        assert record.purpose
        assert record.executable_candidates
        assert record.dependency_hints
        assert record.supported_hosts
        assert record.source_url.startswith("https://")
        assert record.license_url.startswith("https://")
        assert record.acquisition_url.startswith("https://")
        assert record.checked_at == "2026-11-12"
        assert record.research_scope


def test_detect_host_keeps_windows_native_separate_from_wsl():
    host = detect_host(
        system="Windows",
        machine="AMD64",
        kernel_release="10.0.26100",
        kernel_markers=("microsoft text must not turn Windows into WSL",),
    )
    assert host.kind == "windows-native"
    assert host.arch == "x86_64"
    assert not host.is_wsl


def test_detect_host_distinguishes_native_linux_and_wsl():
    native = detect_host(
        system="Linux",
        machine="x86_64",
        kernel_release="6.12.0-generic",
        kernel_markers=("Linux version 6.12.0",),
    )
    wsl = detect_host(
        system="Linux",
        machine="x86_64",
        kernel_release="6.6.87.2-microsoft-standard-WSL2",
        kernel_markers=(),
    )
    assert native.kind == "linux-native"
    assert not native.is_wsl
    assert wsl.kind == "wsl"
    assert wsl.is_wsl


def test_list_reports_missing_and_located_without_running_tools():
    report = build_list_report(
        "mirroring",
        host=WINDOWS,
        which=_which({"adb": r"C:\Android\adb.exe", "scrcpy": r"C:\Tools\scrcpy.exe"}),
    )
    by_id = {item["id"]: item for item in report["tools"]}
    assert by_id["platform-tools"]["detection"]["status"] == "found"
    assert by_id["scrcpy"]["detection"]["status"] == "found"
    assert by_id["platform-tools"]["detection"]["version"] == "not_probed"
    assert by_id["platform-tools"]["detection"]["trusted"] is False
    assert "wsl_assessment" in report["host"]

    missing = build_list_report("mirroring", host=WINDOWS, which=_which({}))
    assert all(item["detection"]["status"] == "missing" for item in missing["tools"])


def test_windows_path_result_is_not_merged_into_wsl_or_linux_report():
    windows = build_list_report(
        "aosp-build", host=WINDOWS, which=_which({"git": r"C:\Program Files\Git\cmd\git.exe"})
    )
    wsl = build_list_report("aosp-build", host=WSL, which=_which({}))
    linux = build_list_report("aosp-build", host=LINUX, which=_which({}))
    assert next(item for item in windows["tools"] if item["id"] == "git")["detection"]["status"] == "found"
    assert next(item for item in wsl["tools"] if item["id"] == "git")["detection"]["status"] == "missing"
    assert next(item for item in linux["tools"] if item["id"] == "git")["detection"]["status"] == "missing"
    assert windows["host"]["kind"] == "windows-native"
    assert wsl["host"]["kind"] == "wsl"
    assert linux["host"]["kind"] == "linux-native"


def test_native_windows_build_plan_is_unsupported():
    report = build_plan_report("aosp-build", branch="android-16.0.0_r1", host=WINDOWS, which=_which({}))
    assert report["host_assessment"]["classification"] == "unsupported"
    assert "Native Windows" in report["host_assessment"]["reason"]
    assert report["host"]["wsl_assessment"]["status"] == "not_assessed"


def test_wsl_build_plan_is_qualified_not_native_linux_certified():
    report = build_plan_report(
        "lineage-build",
        branch="lineage-22.2",
        device="panther",
        host=WSL,
        which=_which({}),
    )
    assert report["host_assessment"]["classification"] == "qualified_unverified"
    assert report["target_context"]["status"] == "user_supplied_unverified"


def test_native_linux_build_plan_requires_upstream_review():
    report = build_plan_report("aosp-build", branch="android-latest-release", host=LINUX, which=_which({}))
    assert report["host_assessment"]["classification"] == "requires_upstream_review"
    assert "not readiness certification" in report["host_assessment"]["reason"]


def test_arm64_build_host_is_not_certified_as_supported():
    arm = HostEnvironment(
        os="linux",
        kind="linux-native",
        arch="arm64",
        kernel_release="6.12.0",
        label="Native Linux",
        is_wsl=False,
    )
    report = build_plan_report("aosp-build", branch="android-16.0.0_r1", host=arm, which=_which({}))
    assert report["host_assessment"]["classification"] == "unsupported_architecture"
    assert "64-bit x86 Linux" in report["host_assessment"]["reason"]


def test_lineage_plan_requires_branch_and_device_context():
    report = build_plan_report("lineage-build", host=LINUX, which=_which({}))
    assert report["status"] == "incomplete"
    assert report["exit_code"] == 2
    assert set(report["target_context"]["missing"]) == {"branch", "device"}
    assert report["target_context"]["official_url"] == "https://wiki.lineageos.org/devices/"


def test_lineage_supplied_context_stays_user_supplied_and_unverified():
    report = build_plan_report(
        "lineage-build",
        branch="lineage-22.2",
        device="panther",
        host=LINUX,
        which=_which({}),
    )
    context = report["target_context"]
    assert report["status"] == "explain_only"
    assert report["exit_code"] == 0
    assert context["status"] == "user_supplied_unverified"
    assert context["official_url"] == "https://wiki.lineageos.org/devices/panther/build/"
    assert "not checked" in context["verification"]


def test_invalid_goal_and_tool_fail_clearly():
    goal = build_list_report("everything", host=LINUX, which=_which({}))
    tool = build_info_report("mystery-flasher", host=LINUX, which=_which({}))
    assert goal["status"] == "invalid_goal"
    assert goal["exit_code"] == 2
    assert "Choose one of" in goal["error"]
    assert tool["status"] == "invalid_tool"
    assert tool["exit_code"] == 2
    assert "Choose one of" in tool["error"]


def test_invalid_context_fails_closed():
    report = build_plan_report(
        "lineage-build",
        branch="lineage-22.2; remove-things",
        device="panther",
        host=LINUX,
        which=_which({}),
    )
    assert report["status"] == "invalid_context"
    assert report["exit_code"] == 2


def test_info_alias_has_actionable_acquisition_and_path_disclaimer():
    report = build_info_report("adb", host=WINDOWS, which=_which({"adb": r"C:\Android\adb.exe"}))
    tool = report["tool"]
    assert tool["id"] == "platform-tools"
    assert set(tool["goals"]) == {"testing", "mirroring"}
    assert tool["acquisition_url"].startswith("https://")
    assert "adapt setup" in tool["acquisition"]
    assert tool["detection"]["trusted"] is False
    assert tool["detection"]["version"] == "not_probed"


def test_plan_is_explain_only_and_contains_no_executable_command_field():
    report = build_plan_report("mirroring", host=WINDOWS, which=_which({}))
    assert report["status"] == "explain_only"
    assert {action["action_class"] for action in report["actions"]} == {0, 1}
    assert all(action.get("mode") in {"completed_observation", "explain_only"} for action in report["actions"])
    assert all("command" not in action for action in report["actions"])
    assert any("No device was accessed" in item for item in report["non_actions"])


def test_java_and_python_guidance_is_branch_specific_not_universal():
    java = TOOL_CATALOG["java"]
    python = TOOL_CATALOG["python"]
    assert "no universal jdk" in " ".join(java.dependency_hints).lower()
    assert "branch" in java.acquisition.lower()
    assert "branch" in python.acquisition.lower()


def test_cli_list_json_is_clean_and_parseable():
    result = runner.invoke(tools_app, ["list", "--goal", "mirroring", "--json"])
    assert result.exit_code == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["command"] == "tools list"
    assert payload["goal"] == "mirroring"
    assert {item["id"] for item in payload["tools"]} == {"platform-tools", "scrcpy"}


def test_cli_invalid_goal_json_is_clean_and_machine_readable():
    result = runner.invoke(tools_app, ["list", "--goal", "invalid", "--json"])
    assert result.exit_code == 2
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["status"] == "invalid_goal"
    assert payload["exit_code"] == 2


def test_cli_invalid_tool_json_is_clean_and_machine_readable():
    result = runner.invoke(tools_app, ["info", "invalid", "--json"])
    assert result.exit_code == 2
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["status"] == "invalid_tool"


def test_cli_lineage_json_incomplete_vs_supplied_unverified():
    incomplete = runner.invoke(tools_app, ["plan", "lineage-build", "--json"])
    assert incomplete.exit_code == 2
    assert incomplete.stderr == ""
    incomplete_payload = json.loads(incomplete.stdout)
    assert incomplete_payload["status"] == "incomplete"

    supplied = runner.invoke(
        tools_app,
        ["plan", "lineage-build", "--branch", "lineage-22.2", "--device", "panther", "--json"],
    )
    assert supplied.exit_code == 0
    assert supplied.stderr == ""
    supplied_payload = json.loads(supplied.stdout)
    assert supplied_payload["target_context"]["status"] == "user_supplied_unverified"


def test_cli_human_info_contains_actionable_links():
    result = runner.invoke(tools_app, ["info", "payload-dumper-go"])
    assert result.exit_code == 0
    assert "Acquisition link: https://github.com/ssut/payload-dumper-go/releases" in result.stdout
    assert "optional third-party upstream" in result.stdout

