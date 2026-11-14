"""An explicit batch of supported host setup and optional read-only inspection."""

from __future__ import annotations

import sys
from typing import Any, Callable

from adapt_adb import android_support, dev_options, dex, device_state, desktop, development
from adapt_adb.doctor import run_doctor_checks, run_setup_workflow
from adapt_adb.failure_report import server_check_failure_id, tagged
from adapt_adb.scrcpy_install import ScrcpyInstallError, install_scrcpy, plan_scrcpy
from adapt_adb.tool_catalog import build_list_report


class EverythingError(ValueError):
    pass


def _unsafe_diagnostic(report: dict[str, Any]) -> bool:
    code = report.get("exit_code")
    return type(code) is not int or code not in {0, 1, 2}


def _targets(devices: list[str] | None) -> list[str]:
    result = []
    for value in devices or []:
        if not isinstance(value, str) or not value or value != value.strip() or value.startswith("-") or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value):
            raise tagged(EverythingError("Use complete nonblank device targets; no implicit target is selected."), "everything.target_invalid")
        if value not in result:
            result.append(value)
    return result


def plan_everything(*, devices: list[str] | None = None, no_path: bool = False) -> dict[str, Any]:
    """Plan using local PATH observations; no ADB, remote host, or network calls."""
    targets = _targets(devices)
    inventory = build_list_report()
    host = inventory["host"]
    platform_supported = host["os"] in {"windows", "linux"} and host["arch"] == "x86_64"
    try:
        scrcpy = plan_scrcpy()
    except (ScrcpyInstallError, OSError):
        scrcpy = {"status": "manual", "reason": "No approved managed scrcpy acquisition is available for this host, or its directory cannot be inspected."}
    manual = []
    for tool in inventory["tools"]:
        if tool["id"] in {"platform-tools", "scrcpy"}:
            continue
        located = tool["detection"]["status"] == "found"
        manual.append({"tool": tool["id"], "status": "located_unverified" if located else "manual_required",
                       "reason": "Version/branch compatibility needs review." if located else "No managed installer is implemented for this tool; follow its upstream or OS-package instructions.",
                       "source": tool["acquisition_url"], "guidance": tool["acquisition"]})
    return {
        "command": "everything", "title": "Give Me Everything", "status": "planned", "exit_code": 0,
        "host": host, "inventory": inventory, "devices": targets,
        "android_versions": android_support.version_matrix(),
        "automatic": [
            {"tool": "platform-tools", "status": "supported" if platform_supported else "manual",
             "source": "https://developer.android.com/tools/releases/platform-tools",
             "license": "https://developer.android.com/studio/terms",
             "action": "Reuse a healthy installation or run the existing verified user-scope setup without forcing replacement.",
             "user_path_update": not no_path},
            {"tool": "scrcpy", "status": scrcpy["status"], "plan": scrcpy,
             "source": "https://github.com/Genymobile/scrcpy/releases",
             "license": "Apache-2.0 for scrcpy; bundled components retain their own licenses."},
        ],
        "manual_tools": manual,
        "checks": ["Host/ADB/driver readiness before and after setup", "All catalog PATH observations", "Android version matrix",
                   "Independent fastboot transport detection (no mode transitions)",
                   "Android capabilities, root/ROM/bootloader evidence, Developer Options, saved debug selection and desktop/DeX inspection for explicitly supplied ADB targets only"],
        "excluded_actions": ["System package/driver installation and reboot", "ROM source sync, builds, unlock or flash",
                             "Device settings/profile changes and APK installation", "Pairing, transport reconfiguration and mirror launch",
                             "Hypervisor/VM changes, SSH connection and remote host installation"],
        "no_path": no_path,
        "claim_boundary": "Everything means every implemented, suitable setup/check step. Manual items are reported, never claimed installed or certified.",
    }


def render_plan(plan: dict[str, Any]) -> str:
    lines = ["ADAPT / GIVE ME EVERYTHING", "", f"Host: {plan['host']['label']} ({plan['host']['arch']})", "", "SUPPORTED ACQUISITIONS"]
    for item in plan["automatic"]:
        lines += [f"  {item['tool']}: {item['status']}", f"    Source: {item['source']}", f"    License: {item['license']}"]
        destination = item.get("plan", {}).get("target_dir", item.get("plan", {}).get("binary_path"))
        if destination:
            lines.append(f"    Destination/existing client: {destination}")
    lines += ["", "Platform-Tools may update user PATH." if not plan["no_path"] else "User PATH will not be changed.", "", "DETECTION", *("  " + s for s in plan["checks"]), "", "OTHER CATALOG TOOLS"]
    for item in plan["manual_tools"]:
        lines += [f"  {item['tool']}: {item['status']}", f"    {item['source']}"]
    lines += ["", "NOT AUTOMATIC", *("  " + s for s in plan["excluded_actions"]), "", plan["claim_boundary"]]
    return "\n".join(lines)


def run_everything(*, yes: bool = False, dry_run: bool = False, devices: list[str] | None = None,
                   no_path: bool = False, confirm: Callable[[str], bool] | None = None) -> dict[str, Any]:
    plan = plan_everything(devices=devices, no_path=no_path)
    if dry_run:
        return {**plan, "status": "dry_run", "writes": False, "downloads": False}
    if not yes:
        if confirm is None:
            if not sys.stdin.isatty() or not sys.stderr.isatty():
                raise tagged(EverythingError("Non-interactive execution requires --yes. Use --dry-run to review the complete plan."), "everything.noninteractive")
            sys.stderr.write(render_plan(plan) + "\n\nRun these supported setup and check steps? [y/N]: ")
            sys.stderr.flush()
            approved = sys.stdin.readline().strip().lower() in {"y", "yes"}
        else:
            approved = confirm(render_plan(plan))
        if not approved:
            return {**plan, "status": "cancelled", "writes": False, "downloads": False}

    steps: list[dict[str, Any]] = []
    inspections: list[dict[str, Any]] = []
    diagnostic: dict[str, Any] | None = None
    unsafe = False
    try:
        diagnostic = run_doctor_checks()
        unsafe = _unsafe_diagnostic(diagnostic)
        steps.append({"step": "host_check_before", "status": "blocked" if unsafe else "checked", "exit_code": diagnostic.get("exit_code"), "report": diagnostic})
    except Exception:
        unsafe = True
        steps.append({"step": "host_check_before", "status": "failed", "reason": "Host safety inspection failed; installations and device probes were not attempted."})

    platform_ready = False
    if unsafe:
        steps.extend({"step": tool, "status": "skipped", "reason": "Shared-daemon safety is unverified or unsafe."} for tool in ("platform-tools", "scrcpy"))
    else:
        if plan["automatic"][0]["status"] == "manual":
            steps.append({"step": "platform-tools", "status": "manual_required", "reason": "Managed Platform-Tools batch setup is limited to approved x86-64 Windows/Linux hosts."})
            platform_ready = diagnostic is not None and diagnostic.get("exit_code") in {0, 2}
        else:
            try:
                result = run_setup_workflow(yes=True, force=False, no_path=no_path)
                setup = result.get("setup", {})
                action = setup.get("action", "unknown")
                platform_ready = result.get("exit_code") in {0, 2} and action in {"installed", "noop_already_healthy", "path_registered", "noop_already_installed"}
                unsafe = result.get("exit_code") == 3
                steps.append({"step": "platform-tools", "status": "installed" if action == "installed" else "reused" if platform_ready else "blocked" if unsafe else "failed", "action": action, "report": result})
            except Exception:
                steps.append({"step": "platform-tools", "status": "failed", "reason": "Platform-Tools setup failed. Existing installations were not forcibly replaced."})

        if unsafe:
            steps.append({"step": "scrcpy", "status": "skipped", "reason": "Shared-daemon safety became unverified or unsafe."})
        elif plan["automatic"][1]["status"] in {"manual", "conflict"}:
            steps.append({"step": "scrcpy", "status": "manual_required", "reason": "No approved usable acquisition plan; inspect tools install scrcpy separately."})
        else:
            try:
                result = install_scrcpy(yes=True)
                status = result["status"]
                steps.append({"step": "scrcpy", "status": "installed" if status == "installed" else "reused" if status in {"reuse_existing", "already_installed"} else "manual_required", "report": result})
            except Exception:
                steps.append({"step": "scrcpy", "status": "failed", "reason": "scrcpy setup failed or has no approved artifact for this host; consult tools install scrcpy."})

    if not unsafe:
        try:
            diagnostic = run_doctor_checks()
            unsafe = _unsafe_diagnostic(diagnostic)
            steps.append({"step": "host_check_after", "status": "blocked" if unsafe else "checked", "exit_code": diagnostic.get("exit_code"), "report": diagnostic})
        except Exception:
            unsafe = True
            steps.append({"step": "host_check_after", "status": "failed", "reason": "Final host safety inspection failed; device probes were not attempted."})
    try:
        transport_detection = device_state.scan(transport="fastboot")
    except Exception:
        transport_detection = {"status": "partial", "transports": {"fastboot": {"status": "failed", "devices": []}}}
    for target in plan["devices"]:
        item: dict[str, Any] = {"device": target, "results": {}}
        if unsafe or not platform_ready:
            item.update(status="skipped", reason="A verified usable local ADB setup is required before device inspection.")
        else:
            for name, inspect in (("android", android_support.inspect_capabilities), ("developer_options", dev_options.inspect_device), ("dex", dex.inspect_device), ("device_state", device_state.inspect_device), ("desktop", desktop.inspect_device), ("debug_selection", development.inspect_debug)):
                try:
                    observation = inspect(target)
                    item["results"][name] = {"status": "partial" if observation.get("status") == "partial" else "checked", "report": observation}
                except Exception:
                    item["results"][name] = {"status": "failed", "reason": "Inspection failed; no mutation or fallback target was attempted."}
            item["status"] = "checked" if all(r["status"] == "checked" for r in item["results"].values()) else "partial"
        inspections.append(item)

    # Refresh all PATH observations, never treating a found executable as verified.
    try:
        inventory_after = build_list_report()
    except Exception:
        inventory_after = {"status": "failed", "tools": []}
    missing_manual = [m for m in plan["manual_tools"] if m["status"] == "manual_required"]
    failed = any(s["status"] == "failed" for s in steps) or inventory_after.get("status") == "failed"
    incomplete = transport_detection["status"] == "partial" or bool(missing_manual) or any(s["status"] in {"manual_required", "skipped"} for s in steps) or any(i["status"] != "checked" for i in inspections) or (diagnostic is not None and diagnostic.get("exit_code") != 0)
    exit_code = 3 if unsafe else 1 if failed else 2 if incomplete else 0
    return {**plan, "status": "blocked" if unsafe else "failed" if failed else "completed_with_manual_steps" if incomplete else "completed",
            "exit_code": exit_code, "steps": steps, "device_inspections": inspections, "transport_detection": transport_detection, "inventory_after": inventory_after,
            "summary": {"installed": [s["step"] for s in steps if s["status"] == "installed"],
                        "reused": [s["step"] for s in steps if s["status"] == "reused"],
                        "manual_missing": [m["tool"] for m in missing_manual]},
            "note": "Completed independent steps are retained on partial failure; this batch is not an atomic rollback transaction."}


def report_failure_id(report: dict[str, Any]) -> str | None:
    """Guide for a batch result that is not `completed` (blocked, failed, or with manual steps)."""
    status = report.get("status")
    if status == "blocked":
        # Name the specific shared-server problem when a host check recorded it.
        for step in report.get("steps") or []:
            checks = (step.get("report") or {}).get("checks") if isinstance(step.get("report"), dict) else None
            server = next((c for c in checks or [] if isinstance(c, dict) and c.get("id") == "adb_server_5037"), None)
            if server is not None and server.get("status") == "fail":
                return server_check_failure_id(server)
        return "everything.blocked"
    return {"failed": "everything.step_failed", "completed_with_manual_steps": "everything.incomplete"}.get(status)


def render_result(report: dict[str, Any]) -> str:
    if report["status"] in {"planned", "dry_run"}:
        return render_plan(report) + "\n\nPLAN ONLY / No downloads or changes performed."
    if report["status"] == "cancelled":
        return "CANCELLED / No setup steps performed."
    lines = ["ADAPT / GIVE ME EVERYTHING", f"Result: {report['status']} (exit {report['exit_code']})", ""]
    for step in report["steps"]:
        lines.append(f"  {step['step']}: {step['status']}")
        if step.get("reason"):
            lines.append("    " + step["reason"])
    for target in report["device_inspections"]:
        reason = f" ({target['reason']})" if target.get("reason") else ""
        lines.append(f"  {target.get('device', 'Selected device')}: {target['status']}{reason}")
        for name, result in target["results"].items():
            lines.append(f"    {name}: {result['status']}")
    fastboot = report["transport_detection"]["transports"]["fastboot"]
    lines.append(f"  fastboot detection: {fastboot['status']} ({len(fastboot['devices'])} transports)")
    if report["summary"]["manual_missing"]:
        lines += ["", "MANUAL ACQUISITIONS / These tools were not installed"]
        for item in report["manual_tools"]:
            if item["status"] == "manual_required":
                lines += [f"  {item['tool']}: {item['reason']}", "    " + item["source"]]
    lines += ["", report["claim_boundary"], report["note"]]
    return "\n".join(lines)
