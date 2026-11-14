"""App validation, bounded log evidence and developer terminology.

Inspection never selects a debug app, attaches a debugger or launches/stops apps.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer

from adapt_adb.adb import resolve_adb
from adapt_adb.device_state import validate_target
from adapt_adb.doctor import preflight_adb_server
from adapt_adb.failure_report import (FailureContext, UNANSWERED_STATUSES, attach_failure, cause_failure_id, classify,
                                     current_command, emit_failure, query_failure_id, tagged)
from adapt_adb.progress import operation

app_app = typer.Typer(help="Validate installed app identity, version and debug flags without launching it.")
logs_app = typer.Typer(help="Extract bounded app or focused system diagnostics; read guidance without changing logging switches.")
debug_app = typer.Typer(help="Inspect debug-app configuration and preview deliberate debugger setup.")
PACKAGE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+")
LOG_MAX_BYTES = 128 * 1024


class DevelopmentError(ValueError):
    pass


def validate_package(package: str) -> str:
    if not isinstance(package, str) or len(package) > 255 or not PACKAGE_RE.fullmatch(package):
        raise tagged(DevelopmentError("Use an exact Android application ID, not a component, wildcard or command."), "app.package_invalid")
    return package


class Session:
    def __init__(self, serial: str):
        self.serial = validate_target(serial)
        self.adb = resolve_adb()
        if self.adb is None:
            raise tagged(DevelopmentError("ADB is missing; run adapt setup."), "adb.not_found")
        try:
            preflight_adb_server(self.adb, ["-s", serial, "shell"])
        except Exception as exc:
            raise tagged(DevelopmentError("Shared ADB safety could not be verified; no device query was attempted."), cause_failure_id(exc, "adb.server.unverified")) from exc

    def query(self, args: list[str], *, log_data: bool = False) -> dict[str, Any]:
        try:
            result = subprocess.run([str(self.adb), "-s", self.serial, *args], capture_output=True,
                                    text=True, encoding="utf-8", errors="strict", check=False, timeout=10)
        except subprocess.TimeoutExpired:
            return {"status": "timeout", "text": None}
        except (OSError, UnicodeError):
            return {"status": "unavailable", "text": None}
        stdout, stderr = result.stdout or "", result.stderr or ""
        errors = stderr if log_data else stdout + "\n" + stderr
        if re.search(r"permission deni|securityexception|not permitted|access denied", errors, re.I):
            return {"status": "denied", "text": None}
        if result.returncode != 0 or re.search(r"(?:^|\n)\s*(?:error:|adb: error:|failure\s*\[)", errors, re.I):
            return {"status": "failed", "text": None}
        if len(stdout.encode("utf-8")) > 2 * 1024 * 1024:
            return {"status": "too_large", "text": None}
        return {"status": "observed", "text": stdout.strip() if not log_data else stdout}


def parse_package_dump(text: str, package: str) -> dict[str, Any]:
    """Only direct fields in one exact Package block may establish flags."""
    result = {"status": "unknown", "version_code": None, "version_name": None,
              "debuggable": None, "profileable_by_shell": None}
    headers = list(re.finditer(r"(?m)^( *)Package \[" + re.escape(package) + r"\] \([^\r\n)]*\):\s*$", text))
    if len(headers) != 1:
        return result
    header = headers[0]
    indent = len(header[1])
    lines = []
    for line in text[header.end():].splitlines():
        if line.strip() and len(line) - len(line.lstrip(" ")) <= indent:
            break
        lines.append(line)
    block = "\n".join(lines)
    prefix = r"(?m)^ {" + str(indent + 2) + r"}"
    versions = re.findall(prefix + r"versionCode=([0-9]{1,19})(?:\s|$)", block)
    names = re.findall(prefix + r"versionName=([^\r\n]*)$", block)
    flags = re.findall(prefix + r"(?:pkgFlags|flags)=\[([^\]\r\n]*)\]\s*$", block)
    private = re.findall(prefix + r"privateFlags=\[([^\]\r\n]*)\]\s*$", block)
    if len(versions) == 1:
        result["version_code"] = int(versions[0])
    if len(names) == 1 and names[0].strip() not in {"", "null"}:
        result["version_name"] = names[0].strip()
    if len(flags) == 1:
        result["debuggable"] = "DEBUGGABLE" in flags[0].split()
    if len(private) == 1:
        result["profileable_by_shell"] = bool({"PROFILEABLE_BY_SHELL", "PRIVATE_FLAG_PROFILEABLE_BY_SHELL"} & set(private[0].split()))
    result["status"] = "observed" if result["version_code"] is not None and result["debuggable"] is not None else "partial"
    return result


def _inspect(session: Session, package: str) -> dict[str, Any]:
    user_result = session.query(["shell", "am", "get-current-user"])
    user_text = user_result["text"]
    if user_result["status"] != "observed" or not re.fullmatch(r"[0-9]{1,5}", user_text or ""):
        # A device that did not answer is named as such, not as a user-detection problem.
        raise tagged(DevelopmentError("The current Android user could not be observed; no user-0 fallback was used."), query_failure_id(user_result["status"], "app.user_unobserved"))
    user = int(user_text)
    listing = session.query(["shell", "pm", "list", "packages", "-U", "--user", str(user), package])
    installation, uid = "unknown", None
    if listing["status"] == "observed":
        rows, malformed = [], False
        for line in listing["text"].splitlines():
            if not line.strip():
                continue
            match = re.fullmatch(r"package:([^\s]+)(?:\s+uid:([0-9]{1,10}))?", line.strip())
            if not match:
                malformed = True
            elif match[1] == package:
                rows.append(match)
        if not malformed and len(rows) <= 1:
            installation = "installed" if rows else "not_installed"
            uid = int(rows[0][2]) if rows and rows[0][2] is not None else None
    metadata = {"status": "not_queried", "version_code": None, "version_name": None, "debuggable": None, "profileable_by_shell": None}
    paths = {"status": "not_queried", "items": []}
    if installation == "installed":
        dump = session.query(["shell", "dumpsys", "package", package])
        metadata = parse_package_dump(dump["text"], package) if dump["status"] == "observed" else {**metadata, "status": dump["status"]}
        path_result = session.query(["shell", "pm", "path", "--user", str(user), package])
        if path_result["status"] == "observed":
            items = [line.removeprefix("package:") for line in path_result["text"].splitlines()]
            valid = bool(items) and all(p.startswith("/") and p.endswith(".apk") and not re.search(r"[\x00-\x1f\x7f-\x9f]", p) for p in items) and all(line.startswith("package:") for line in path_result["text"].splitlines())
            paths = {"status": "observed" if valid else "unknown", "items": items if valid else []}
        else:
            paths["status"] = path_result["status"]
    return {"schema_version": 1, "serial": session.serial, "package": package, "android_user": user,
            "installation": installation, "package_uid": uid, "listing_status": listing["status"],
            "metadata": metadata, "apk_paths": paths, "observed_at_utc": datetime.now(timezone.utc).isoformat(),
            "build_evidence": {"variant": "unknown_from_device", "r8_optimization": "not_verified", "signing_certificate": "not_verified", "exact_artifact_match": "not_verified", "production_readiness": "not_established"},
            "mutations": [], "note": "Package/version/flags are runtime observations for the current user, not proof of source revision, R8, signing or an exact APK. No app was started or stopped."}


@operation("Validating installed app")
def validate_app(serial: str, package: str, *, expected_version_code: int | None = None,
                 expected_debuggable: bool | None = None) -> dict[str, Any]:
    validate_package(package)
    validate_target(serial)
    if expected_version_code is not None and (type(expected_version_code) is not int or not 0 <= expected_version_code <= 2**63 - 1):
        raise tagged(DevelopmentError("Expected version code must be a nonnegative 64-bit integer."), "app.expectation_invalid")
    if expected_debuggable is not None and type(expected_debuggable) is not bool:
        raise tagged(DevelopmentError("Expected debuggable must be true or false."), "app.expectation_invalid")
    report = _inspect(Session(serial), package)
    checks = [{"check": "installed_for_current_user", "expected": True,
               "observed": True if report["installation"] == "installed" else False if report["installation"] == "not_installed" else None}]
    for name, expected in (("version_code", expected_version_code), ("debuggable", expected_debuggable)):
        if expected is not None:
            checks.append({"check": name, "expected": expected, "observed": report["metadata"][name]})
    for check in checks:
        check["status"] = "unknown" if check["observed"] is None else "passed" if check["observed"] == check["expected"] else "failed"
    state = "failed" if any(c["status"] == "failed" for c in checks) else "inconclusive" if any(c["status"] == "unknown" for c in checks) else "passed"
    return {**report, "status": state, "checks": checks, "exit_code": {"passed": 0, "failed": 1, "inconclusive": 2}[state],
            "claim_boundary": "Passed means only the listed requested checks passed. It does not imply all metadata was readable or the app works."}


@operation("Extracting app logs")
def capture_logs(serial: str, package: str, *, lines: int = 200, buffer: str = "main,crash", level: str = "V") -> dict[str, Any]:
    validate_package(package)
    validate_target(serial)
    if type(lines) is not int or not 1 <= lines <= 500 or buffer not in {"main", "crash", "main,crash"} or level not in {"V", "D", "I", "W", "E", "F"}:
        raise tagged(DevelopmentError("Use 1–500 lines, main/crash/main,crash buffers and V/D/I/W/E/F severity."), "logs.args_invalid")
    session = Session(serial)
    app = _inspect(session, package)
    uid = app["package_uid"]
    if app["installation"] != "installed" or uid is None:
        raise tagged(DevelopmentError("Installed app UID could not be observed; device-wide log fallback is disabled."),
                     "logs.app_not_installed" if app["installation"] == "not_installed" else "device.command_denied" if app["listing_status"] == "denied" else "logs.uid_unobserved")
    args = ["logcat", "-d", "-t", str(lines), "-v", "threadtime", f"--uid={uid}"]
    for item in buffer.split(","):
        args.extend(["-b", item])
    args.append(f"*:{level}")
    result = session.query(args, log_data=True)
    if result["status"] != "observed":
        raise tagged(DevelopmentError("Log extraction failed or UID filtering is unavailable; no broader query was attempted."),
                     {"timeout": "logs.capture_timeout", "unavailable": "adb.exec_failed", "denied": "device.command_denied", "too_large": "logs.budget_exceeded"}.get(result["status"], "logs.capture_failed"))
    raw = result["text"].encode("utf-8")
    truncated = len(raw) > LOG_MAX_BYTES
    text = raw[-LOG_MAX_BYTES:].decode("utf-8", errors="ignore")
    records = [line for line in text.splitlines() if not line.startswith("--------- beginning of ")]
    truncated = truncated or len(records) > lines
    records = records[-lines:]
    # A package uninstall/user switch during capture must not silently relabel
    # a UID snapshot as the original app's current identity.
    after = _inspect(session, package)
    if after["installation"] != "installed" or after["package_uid"] != uid or after["android_user"] != app["android_user"]:
        raise tagged(DevelopmentError("App UID or current user changed during capture; captured contents were discarded."), "logs.target_changed")
    return {"schema_version": 1, "status": "captured", "serial": serial, "package": package, "android_user": app["android_user"],
            "uid": uid, "buffers": buffer.split(","), "minimum_level": level, "requested_lines": lines,
            "lines": records, "truncated": truncated, "max_bytes": LOG_MAX_BYTES,
            "captured_at_utc": datetime.now(timezone.utc).isoformat(), "app_snapshot": app,
            "mutations": [], "privacy": "Logs can contain credentials or personal data. UID filtering can include shared-UID packages or historical reused-UID events; system-originated crash reports may be omitted.",
            "claim_boundary": "A bounded ring-buffer snapshot. Empty output does not prove no crashes, and successful capture is not a test pass. No buffers were cleared."}


def export_logs(report: dict[str, Any], path: Path) -> dict[str, Any]:
    data = (json.dumps(report, indent=2) + "\n").encode("utf-8")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise tagged(DevelopmentError("Log export failed or the path already exists. No existing file was overwritten; a partial new file may remain after a write error."), "logs.export_failed") from exc
    return {**report, "export": {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}}


@operation("Inspecting debug-app selection")
def inspect_debug(serial: str) -> dict[str, Any]:
    session = Session(serial)
    app = session.query(["shell", "settings", "get", "global", "debug_app"])
    wait = session.query(["shell", "settings", "get", "global", "wait_for_debugger"])
    selected = app["text"] if app["status"] == "observed" else None
    selected_status = "observed" if selected and PACKAGE_RE.fullmatch(selected) else "none" if selected in {"null", ""} and app["status"] == "observed" else "unknown"
    wait_value = {"0": False, "1": True}.get(wait["text"]) if wait["status"] == "observed" else None
    return {"schema_version": 1, "status": "observed" if selected_status != "unknown" and wait_value is not None else "partial",
            "serial": serial, "persisted_configuration": {"debug_app": selected if selected_status == "observed" else None,
                "selection_status": selected_status, "wait_for_debugger": wait_value, "app_query_status": app["status"], "wait_query_status": wait["status"]},
            "runtime_effective_selection": "not_verified", "debugger_attached": "not_verified", "mutations": [],
            "note": "Settings describe persisted configuration; transient ActivityManager selections can differ. Selection does not make a release app debuggable."}


def plan_debug(serial: str, package: str, *, wait: bool = False, persistent: bool = False) -> dict[str, Any]:
    validate_target(serial)
    validate_package(package)
    if type(wait) is not bool or type(persistent) is not bool:
        raise tagged(DevelopmentError("Wait and persistent choices must be boolean."), "debug.plan_invalid")
    argv = ["adb", "-s", serial, "shell", "am", "set-debug-app"]
    if wait:
        argv.append("-w")
    if persistent:
        argv.append("--persistent")
    return {"schema_version": 1, "status": "plan_only", "serial": serial, "package": package, "argv": [*argv, package],
            "device_access": False, "would_force_stop_selected_app": True, "would_wait_at_startup": wait, "persistent": persistent,
            "requirements": ["Observe the installed package and debuggable flag first", "Save the prior device-global debug selection", "Deliberately accept stopping the selected package, potentially across Android users; save in-app work", "Attach an IDE/JDWP debugger if wait is selected", "Restore prior selection after testing; clearing is not equivalent to restoring another app"],
            "note": "Preview only. ADAPT neither executes this selection nor automatically clears it. This does not change build flags, R8 optimization, signing, app data or other devices."}


def build_modes() -> dict[str, Any]:
    return {"schema_version": 1, "status": "offline_reference", "dimensions": [
        {"name": "debuggable", "meaning": "Runtime permission for debugger attachment; inspect the installed package flag. Not a claim about signing or R8."},
        {"name": "build_variant", "meaning": "Gradle build type plus product flavor; debug/release are conventional names, not device-verifiable provenance."},
        {"name": "r8_optimized", "meaning": "R8 can shrink, optimize and obfuscate. Verify exact build configuration/task evidence and retain its mapping file. Not inferable from debuggable=false."},
        {"name": "profileable", "meaning": "Profiling eligibility is separate from full debugger access. Prefer production-like performance builds; debug overhead changes measurements."},
        {"name": "signing", "meaning": "Verify the artifact signing certificate independently. A non-debuggable optimized APK may still use a development certificate."},
        {"name": "installed", "meaning": "Exact package and current-user installation/version are runtime observations, not proof that an exact local APK was installed."},
        {"name": "production_ready", "meaning": "Requires the intended signer, source/artifact provenance and appropriate functional, optimized-build and device acceptance. No single flag proves readiness."},
    ], "ai_workflow": ["Record exact device, package, source revision, variant, APK hash and signer", "Call app_validate with expected version/debuggable", "Inspect debug_app_inspect before debugging", "Reproduce one deliberate app test journey", "Call app_logs and inspect logs alongside fresh UI evidence", "For optimized crashes use the exact build's mapping.txt with R8 retrace", "Report observed facts, requested check results and remaining unknowns separately"],
        "sources": ["https://developer.android.com/build/build-variants", "https://developer.android.com/topic/performance/app-optimization/enable-app-optimization", "https://developer.android.com/tools/retrace", "https://developer.android.com/studio/profile"]}


def report_failure_id(report: dict[str, Any]) -> str | None:
    """Guide for a returned report that is not a clean result (validate failed or inconclusive, debug partial)."""
    status = report.get("status")
    if status == "failed" and "checks" in report:
        return "app.validation_failed"
    if status == "inconclusive":
        statuses = (report.get("listing_status"), (report.get("metadata") or {}).get("status"))
        return next((query_failure_id(s, "") for s in statuses if s in UNANSWERED_STATUSES | {"denied"}), "app.validation_inconclusive")
    if status == "partial" and isinstance(report.get("persisted_configuration"), dict):
        config = report["persisted_configuration"]
        statuses = (config.get("app_query_status"), config.get("wait_query_status"))
        return next((query_failure_id(s, "") for s in statuses if s in UNANSWERED_STATUSES | {"denied"}), "debug.inspect_partial")
    return None


def _emit(fn, json_output: bool, **kwargs):
    # The report and exit code are unchanged; failure guidance follows them (stderr in human
    # mode, an additive `failure` key with --json).
    try:
        report = fn(**kwargs)
        failure_id = report_failure_id(report)
    except ValueError as exc:
        report = {"status": "refused", "exit_code": 2, "error": str(exc)}
        failure_id = classify(exc)
    context = FailureContext.from_runtime(command=current_command())
    if json_output and failure_id:
        report = attach_failure(report, failure_id, context)
    typer.echo(json.dumps(report, indent=2) if json_output else "ADAPT / DEVELOPMENT\n" + json.dumps(report, indent=2))
    if failure_id and not json_output:
        emit_failure(failure_id, context)
    code = report.get("exit_code", 2 if report.get("status") == "partial" else 0)
    if code:
        raise typer.Exit(code)


@app_app.command("validate")
def validate_command(serial: str = typer.Option(..., "--serial", "-s"), package: str = typer.Option(..., "--package"), expected_version_code: int | None = typer.Option(None, "--expect-version-code"), expected_debuggable: str | None = typer.Option(None, "--expect-debuggable", help="true or false; independent of R8/signing."), json_output: bool = typer.Option(False, "--json")):
    """Check current-user installation and optional version/debuggable expectations."""
    def validate():
        if expected_debuggable is not None and expected_debuggable not in {"true", "false"}:
            raise tagged(DevelopmentError("--expect-debuggable must be true or false."), "app.expectation_invalid")
        return validate_app(serial, package, expected_version_code=expected_version_code,
                            expected_debuggable=None if expected_debuggable is None else expected_debuggable == "true")
    _emit(validate, json_output)


@app_app.command("build-modes")
def build_modes_command(json_output: bool = typer.Option(False, "--json")):
    """Explain debuggable, release, R8, profiling, signing and evidence for AI workflows."""
    _emit(build_modes, json_output)


@logs_app.command("capture")
def logs_command(serial: str = typer.Option(..., "--serial", "-s"), package: str = typer.Option(..., "--package"), lines: int = typer.Option(200, "--lines"), buffer: str = typer.Option("main,crash", "--buffer"), level: str = typer.Option("V", "--level"), output: Path | None = typer.Option(None, "--output"), json_output: bool = typer.Option(False, "--json")):
    """Capture a finite app-UID log snapshot; optionally export without overwriting."""
    def capture():
        report = capture_logs(serial, package, lines=lines, buffer=buffer, level=level)
        return export_logs(report, output) if output is not None else report
    _emit(capture, json_output)


@debug_app.command("inspect")
def debug_inspect_command(serial: str = typer.Option(..., "--serial", "-s"), json_output: bool = typer.Option(False, "--json")):
    """Read persisted debug-app/wait configuration; do not infer debugger attachment."""
    _emit(inspect_debug, json_output, serial=serial)


@debug_app.command("plan")
def debug_plan_command(serial: str = typer.Option(..., "--serial", "-s"), package: str = typer.Option(..., "--package"), wait: bool = typer.Option(False, "--wait"), persistent: bool = typer.Option(False, "--persistent"), json_output: bool = typer.Option(False, "--json")):
    """Offline setup preview; executing set-debug-app would force-stop the chosen app."""
    _emit(plan_debug, json_output, serial=serial, package=package, wait=wait, persistent=persistent)
