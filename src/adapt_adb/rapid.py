"""The Rapid ADAPT testing option within the ADAPT application."""
from __future__ import annotations

from pathlib import Path
from typing import Any
import os
import shlex

import typer

from adapt_adb.development import _emit, validate_package
from adapt_adb.device_state import validate_target
from adapt_adb.failure_report import FailureContext, classify, emit_failure

rapid_app = typer.Typer(help="Rapid ADAPT option within ADAPT: check, prepare, reproduce, capture and restore. Start with a plan.", no_args_is_help=True)


def plan(serial: str, *, package: str | None = None, session_dir: Path = Path("rapid-session"), fast_ui: bool = False) -> dict[str, Any]:
    validate_target(serial)
    if package is not None:
        validate_package(package)
    awake = str(session_dir / "stay-awake.json")
    animations = str(session_dir / "fast-ui.json")
    steps = [
        {"stage": "check", "purpose": "Observe Android capabilities and original testing settings", "commands": [
            ["adapt", "android", "capabilities", "-s", serial, "--json"],
            ["adapt", "dev-options", "inspect", "-s", serial, "--json"]]},
    ]
    if package is not None:
        steps[0]["commands"].append(["adapt", "app", "validate", "-s", serial, "--package", package, "--json"])
    steps.append({"stage": "prepare", "purpose": "Keep the screen on while USB-powered; preserve existing charging modes", "commands": [
        ["adapt", "dev-options", "apply", "--profile", "stay-awake", "-s", serial, "--snapshot", awake]]})
    if fast_ui:
        steps[-1]["commands"].append(["adapt", "dev-options", "apply", "--profile", "fast-ui", "-s", serial, "--snapshot", animations])
    steps.append({"stage": "reproduce", "purpose": "Run one deliberate app journey and record UI/timing evidence", "commands": []})
    capture = (["adapt", "logs", "capture", "-s", serial, "--package", package, "--lines", "200", "--output", str(session_dir / "app-logs.json")]
               if package else ["adapt", "logs", "diagnostic", "-s", serial, "--topic", "window", "--minutes", "2", "--output", str(session_dir / "window-logs.json")])
    steps.append({"stage": "capture", "purpose": "Collect focused existing logs; empty output is not a passing test", "commands": [capture]})
    recovery = []
    if fast_ui:
        recovery.append(["adapt", "dev-options", "restore", "-s", serial, "--snapshot", animations])
    recovery.append(["adapt", "dev-options", "restore", "-s", serial, "--snapshot", awake])
    steps.append({"stage": "restore", "purpose": "Restore only profiles actually applied; retain snapshots and resolve conflicts", "commands": recovery})
    return {"schema_version": 1, "application": "ADAPT", "command_group": "rapid", "label": "Rapid ADAPT", "status": "plan_only", "serial": serial, "package": package,
            "device_access": False, "commands_executed": [], "files_created": [], "steps": steps,
            "deeper_diagnostics": "adapt logs guide --topic all",
            "notes": ["Execute each selected command deliberately; this plan runs none and is not an automatic session supervisor",
                      "Choose new snapshot paths. Apply refuses overwriting existing snapshots; restore is explicit and best-effort",
                      "Stay-awake needs USB power; it neither unlocks the phone nor overrides device policy",
                      "Fast UI is opt-in and can hide animation-dependent bugs; omit it for animation/performance acceptance",
                      "Samsung/vendor verbose logging, full bug reports, debugger selection, desktop mode and installations are outside the rapid path",
                      "Logs and recovery snapshots may contain private data; review before sharing"]}


@rapid_app.command("plan")
def plan_command(serial: str = typer.Option(..., "--serial", "-s"), package: str | None = typer.Option(None, "--package"), session_dir: Path = typer.Option(Path("rapid-session"), "--session-dir"), fast_ui: bool = typer.Option(False, "--fast-ui"), json_output: bool = typer.Option(False, "--json")):
    """Preview the rapid test loop, with exact target and explicit recovery commands."""
    if json_output:
        _emit(plan, True, serial=serial, package=package, session_dir=session_dir, fast_ui=fast_ui)
        return
    try:
        report = plan(serial, package=package, session_dir=session_dir, fast_ui=fast_ui)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        emit_failure(classify(exc), FailureContext.from_runtime(command="rapid plan"))
        raise typer.Exit(2) from exc
    typer.echo("ADAPT / RAPID ADAPT OPTION / TEST LOOP / PLAN ONLY")
    for step in report["steps"]:
        typer.echo(f"\n{step['stage'].upper()} / {step['purpose']}")
        for argv in step["commands"]:
            command = "& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in argv) if os.name == "nt" else shlex.join(argv)
            typer.echo("  " + command)
    for note in report["notes"]:
        typer.echo("\n" + note)
    typer.echo("\nDeeper diagnostics: " + report["deeper_diagnostics"])
