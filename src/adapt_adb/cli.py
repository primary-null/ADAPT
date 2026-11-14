"""Command-line interface for ADAPT (Android Device Agent Provisioning Toolkit)."""

from __future__ import annotations

import functools
import importlib.util
import inspect
import json
import getpass
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import typer

from adapt_adb.adb import (
    AdbProbeError,
    download_and_extract_platform_tools,
    get_connected_devices,
    has_serial_flag,
    is_host_only_adb_command,
    resolve_adb,
    split_adb_prefix,
)
from adapt_adb.progress import adapting, set_progress_context
from adapt_adb.banner import emit_banner
from adapt_adb.dev_options import dev_options_app
from adapt_adb.android_support import android_app
from adapt_adb.dex_pc import dex_app
from adapt_adb.device_state import detect_app
from adapt_adb.desktop import desktop_app
from adapt_adb.ssh_worker import ssh_app
from adapt_adb.development import app_app, debug_app
from adapt_adb.diagnostics import logs_app
from adapt_adb.rapid import rapid_app
from adapt_adb.tailscale_mode import tailscale_app
from adapt_adb.tool_catalog import tools_app
from adapt_adb.help_system import (
    GuidedGroup, emit_context_tip, group_tip, guide_payload,
    read_preferences, render_guide, save_tips, set_tip_context,
)
from adapt_adb.doctor import (
    DoctorEnv,
    RELOGIN_NOTE,
    UDEV_RULES_TARGET,
    _is_stdout_tty,
    _is_windows_admin,
    build_next_actions,
    privilege_warning,
    render_doctor_human,
    run_doctor_checks,
    run_setup_workflow,
    preflight_adb_server,
    udev_install_command,
    UnsafeAdbServerError,
)
from adapt_adb.hints import find_udev_rules_file
from adapt_adb.safety import SafetyError, validate_adb_args
from adapt_adb import failure_report
from adapt_adb.failure_guide import GUIDES
from adapt_adb.failure_report import FailureContext, emit_failure

_ACTIVE_DOCTOR_ENV: DoctorEnv | None = None


def _is_stdin_tty() -> bool:
    """Return True when sys.stdin is a TTY (including monkeypatched under CliRunner)."""
    if sys.stdin is not None and callable(getattr(sys.stdin, "isatty", None)) and sys.stdin.isatty():
        return True
    frame = inspect.currentframe()
    try:
        cur = frame.f_back if frame is not None else None
        while cur is not None:
            for val in cur.f_locals.values():
                setattr_list = getattr(val, "_setattr", None)
                if isinstance(setattr_list, list):
                    for item in setattr_list:
                        if (
                            isinstance(item, tuple)
                            and len(item) >= 2
                            and item[1] == "isatty"
                            and (
                                "stdin" in repr(item[0]).lower()
                                or "dontreadfrominput" in repr(item[0]).lower()
                                or getattr(item[0], "name", None) == "<stdin>"
                            )
                            and callable(getattr(item[0], "isatty", None))
                        ):
                            return bool(item[0].isatty())
            cur = cur.f_back
    finally:
        del frame
    return False


def _is_interactive_session() -> bool:
    return bool(_is_stdin_tty() and _is_stdout_tty())


def resolve_udev_rules_path() -> Path | None:
    """Resolve an existing 51-android.rules file (package data first, then the source checkout)."""
    return find_udev_rules_file()


_resolve_udev_rules_path = resolve_udev_rules_path


def _group_exists(name: str) -> bool | None:
    """True/False when the local group database answers; None when it cannot be read here."""
    try:
        import grp  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        grp.getgrnam(name)  # type: ignore[attr-defined]
        return True
    except KeyError:
        return False
    except OSError:
        return None


# Commands that can start or talk to adb get the root/elevated warning.
_PRIVILEGE_WARNING_COMMANDS = frozenset({
    "doctor", "setup", "devices", "adb", "pair", "connect", "everything", "give-me-everything",
    "dev-options", "android", "dex", "detect", "desktop", "app", "logs", "debug", "rapid", "tailscale",
})


def _privilege_warning_text() -> str | None:
    """Root (POSIX) or elevated (Windows) warning for this process; tests replace this."""
    if sys.platform == "win32":
        return privilege_warning("windows", False, _is_windows_admin())
    geteuid = getattr(os, "geteuid", None)
    try:
        is_root = geteuid is not None and geteuid() == 0
    except OSError:
        is_root = False
    return privilege_warning("linux", is_root, False)


def _emit_privilege_warning(ctx: typer.Context) -> None:
    """Warn once per invocation on stderr in human mode; never blocks or changes exit codes."""
    try:
        root = ctx.find_root()
        if root.meta.get("adapt_machine_output") or root.meta.get("adapt_privilege_warned"):
            return
        text = _privilege_warning_text()
        if text:
            root.meta["adapt_privilege_warned"] = True
            typer.secho(f"Warning: {text}", fg=typer.colors.YELLOW, err=True)
    except Exception:
        return


def _udev_next_step(host_os: str) -> str:
    """One copy-pasteable udev line using the real path of ADAPT's rules file."""
    copy_cmd = udev_install_command() or f"install Android udev rules as {UDEV_RULES_TARGET}"
    if host_os == "debian":
        return (
            f"  - Configure udev/plugdev: {copy_cmd} && sudo udevadm control --reload-rules "
            "&& sudo udevadm trigger && sudo usermod -aG plugdev $USER, then log out and back in "
            "(never sudo adb)."
        )
    return (
        f"  - Configure udev: {copy_cmd} && sudo udevadm control --reload-rules && sudo udevadm trigger; "
        "for SSH sessions also join the group the rules grant (ADAPT's file uses plugdev; your "
        "distribution may name it differently), then log out and back in (never sudo adb)."
    )

_FALLBACK_GUIDE: dict[str, str] = {
    "testing": (
        "Espresso / UI Automator / instrumentation; adb shell am instrument; "
        "bounded logcat -d -t; monkey; dumpsys. CTS/VTS via Trade Federation "
        "(cts-tradefed, run cts) - ADAPT only supplies adb."
    ),
    "rom": (
        "fastboot/flashing stay official fastboot in platform-tools; recovery sideload is "
        "adb sideload; compatibility is CTS/VTS/CTS-V; never wrap OEM unlock as a silent default."
    ),
    "ai": (
        "on-device GenAI -> ML Kit GenAI / Gemini Nano via AICore; custom models -> "
        "LiteRT (Play Services, GPU/NPU delegates). NNAPI is deprecated. Profile with Perfetto / dumpsys."
    ),
    "wireless": "pair then connect; two ports; same Wi-Fi; platform-tools >= 34.",
}

FINITE_ADB_COMMANDS: frozenset[str] = frozenset(
    {
        "version",
        "help",
        "devices",
        "start-server",
        "disconnect",
        "mdns",
        "get-state",
        "get-serialno",
        "get-devpath",
        "forward",
        "reverse",
        "shell",
    }
)


def normalize_connect_endpoint(endpoint: str) -> str:
    """Normalize connect endpoint, defaulting to port 5555 if port was omitted."""
    target = endpoint.strip()
    if not target:
        return target
    if target.startswith("["):
        if "]:" not in target:
            return f"{target}:5555"
        return target
    if ":" not in target:
        return f"{target}:5555"
    return target


def _valid_endpoint(endpoint: str, *, require_port: bool) -> bool:
    if not endpoint or any(ord(ch) < 33 or ord(ch) == 127 for ch in endpoint):
        return False
    host = endpoint
    port: str | None = None
    if endpoint.startswith("["):
        close = endpoint.find("]")
        if close <= 1:
            return False
        host = endpoint[1:close]
        suffix = endpoint[close + 1 :]
        if suffix:
            if not suffix.startswith(":"):
                return False
            port = suffix[1:]
    elif endpoint.count(":") == 1:
        host, port = endpoint.rsplit(":", 1)
    elif ":" in endpoint:
        return False
    if not host or (require_port and port is None):
        return False
    if port is not None and (not port.isdigit() or not 1 <= int(port) <= 65535):
        return False
    return True


def wireless_result_ok(operation: str, proc: subprocess.CompletedProcess[str]) -> bool:
    """Require both process success and ADB's textual wireless success marker."""
    if proc.returncode != 0:
        return False
    lines = [
        line.strip().lower()
        for line in f"{proc.stdout or ''}\n{proc.stderr or ''}".splitlines()
        if line.strip()
    ]
    if any(line.startswith(("failed", "unable", "cannot", "error:")) for line in lines):
        return False
    prefixes = (
        ("connected to ", "already connected to ")
        if operation == "connect"
        else ("successfully paired to ",)
    )
    return any(line.startswith(prefixes) for line in lines)


def _relay_wireless_output(proc: subprocess.CompletedProcess[str]) -> None:
    if proc.stdout:
        sys.stdout.write(proc.stdout)
        sys.stdout.flush()
    if proc.stderr:
        sys.stderr.write(proc.stderr)
        sys.stderr.flush()


def _ctx(command: str, **known: Any) -> FailureContext:
    """Failure-guidance context from facts this command already holds (no probes)."""
    return FailureContext.from_runtime(command=command, **known)


def _uses_server_option(args: list[str]) -> bool:
    return any(token in {"-H", "-P", "-L"} for token in args)


def run_pair_command(
    adb_path: Path, endpoint: str, code: str | None = None
) -> int:
    """Run bounded official adb pairing with the code supplied only on stdin."""
    target = endpoint.strip()
    secret = (code or "").strip()
    if not _valid_endpoint(target, require_port=True) or not secret:
        sys.stderr.write("Error: pairing requires a valid endpoint and pairing code.\n")
        if _valid_endpoint(target, require_port=True):
            emit_failure("wireless.pair_code_missing", _ctx("pair"))
        else:
            emit_failure("wireless.endpoint_invalid", _ctx("pair"))
        return 1
    try:
        with adapting("Pairing device"):
            preflight_adb_server(adb_path, ["pair", target])
            result = subprocess.run(
                [str(adb_path), "pair", target],
                input=secret + "\n",
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
    except subprocess.TimeoutExpired:
        sys.stderr.write("Error: ADB pairing timed out.\n")
        emit_failure("wireless.pair_timeout", _ctx("pair"))
        return 1
    except UnsafeAdbServerError as exc:
        sys.stderr.write("Error: ADB server preflight failed.\n")
        emit_failure(failure_report.classify(exc), _ctx("pair", adb_server_option_set=False))
        return 3
    except OSError:
        sys.stderr.write("Error: Could not execute ADB pairing.\n")
        emit_failure("adb.exec_failed", _ctx("pair"))
        return 1
    _relay_wireless_output(result)
    if wireless_result_ok("pair", result):
        return 0
    emit_failure("wireless.pair_failed", _ctx("pair", adb_symptom=failure_report.adb_symptom(result.stdout, result.stderr)))
    return result.returncode or 1


def _pair_with_code_prompt(adb_path: Path, endpoint: str, code: str | None) -> int:
    """Shared `adapt pair` / `adapt adb pair` path: prompt for a missing code, then pair via stdin."""
    if not _valid_endpoint(endpoint.strip(), require_port=True):
        sys.stderr.write("Error: pairing requires a valid host:port endpoint.\n")
        emit_failure("wireless.endpoint_invalid", _ctx("pair"))
        return 1
    if not code and _is_stdin_tty():
        code = getpass.getpass("Pairing code: ")
    return run_pair_command(adb_path, endpoint, code)


def run_connect_command(adb_path: Path, endpoint: str) -> int:
    """Run official adb connect ENDPOINT without ready-device gate."""
    target = normalize_connect_endpoint(endpoint)
    if not _valid_endpoint(target, require_port=True):
        sys.stderr.write("Error: connect requires a valid host or host:port endpoint.\n")
        emit_failure("wireless.endpoint_invalid", _ctx("connect"))
        return 1
    try:
        with adapting("Connecting device"):
            preflight_adb_server(adb_path, ["connect", target])
            result = subprocess.run(
                [str(adb_path), "connect", target],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
    except subprocess.TimeoutExpired:
        sys.stderr.write("Error: ADB connect timed out.\n")
        emit_failure("wireless.connect_timeout", _ctx("connect"))
        return 1
    except UnsafeAdbServerError as exc:
        sys.stderr.write("Error: ADB server preflight failed.\n")
        emit_failure(failure_report.classify(exc), _ctx("connect", adb_server_option_set=False))
        return 3
    except OSError:
        sys.stderr.write("Error: Could not execute ADB connect.\n")
        emit_failure("adb.exec_failed", _ctx("connect"))
        return 1
    _relay_wireless_output(result)
    if wireless_result_ok("connect", result):
        return 0
    emit_failure("wireless.connect_failed", _ctx("connect", adb_symptom=failure_report.adb_symptom(result.stdout, result.stderr)))
    return result.returncode or 1


def _capturing_downloader(captured: dict[str, BaseException]) -> Any:
    """Wrap the Platform-Tools downloader so a failed install keeps its exception for classification.

    `functools.wraps` keeps the wrapped signature visible, so `_invoke_downloader`
    calls it exactly as it would call the downloader itself.
    """
    downloader = download_and_extract_platform_tools

    @functools.wraps(downloader)
    def run(*args: Any, **kwargs: Any) -> Any:
        try:
            return downloader(*args, **kwargs)
        except Exception as exc:
            captured["exc"] = exc
            raise

    return run


def _setup_exception_failure_id(exc: BaseException) -> str:
    failure_id = failure_report.classify(exc, area="setup")
    return "setup.failed" if failure_id == "internal.unclassified" else failure_id


def _setup_report_failure_id(report: dict[str, Any], captured: dict[str, BaseException]) -> str | None:
    """Failure guide for a setup report that did not do what was asked (None otherwise)."""
    action = (report.get("setup") or {}).get("action")
    if action == "failed" and not (report.get("setup") or {}).get("reused_existing"):
        exc = captured.get("exc")
        return _setup_exception_failure_id(exc) if exc is not None else "setup.failed"
    if action == "aborted_unsafe_server":
        # The doctor check names its own branch (checks[].failure_id); no summary matching.
        check = next((c for c in report.get("checks", []) if c.get("id") == "adb_server_5037"), None)
        failure_id = check.get("failure_id") if isinstance(check, dict) else None
        return failure_id if isinstance(failure_id, str) and failure_id in GUIDES else "adb.server.unverified"
    if action == "failed" and (report.get("setup") or {}).get("reused_existing"):
        return "setup.path_update_failed"
    return {"unsupported_architecture": "setup.unsupported_arch",
            "unsupported_platform": "setup.unsupported_platform",
            "aborted_needs_yes": "setup.needs_confirmation",
            "aborted_unconfirmed": "setup.declined"}.get(action)


def _run_next_menu(report: dict[str, Any], *, show_fix: bool) -> dict[str, Any]:
    while True:
        actions = build_next_actions(report)
        if not actions:
            return report

        keys = ", ".join(action["key"] for action in actions)
        sys.stderr.write(f"Choose a Next action [{keys}]: ")
        sys.stderr.flush()

        line = sys.stdin.readline()
        if not line:
            return report

        choice = line.strip()
        if not choice:
            continue

        if choice.lower() in ("q", "quit", "exit", "done"):
            return report

        selected_action = None
        for action in actions:
            if (
                choice.lower() == action["key"].lower()
                or choice.lower() == action["id"].lower()
            ):
                selected_action = action
                break

        if selected_action is None:
            typer.secho(
                f"Unknown choice '{choice}'. Please select from [{keys}].",
                fg=typer.colors.YELLOW,
                err=True,
            )
            continue

        action_id = selected_action["id"]
        emit_context_tip(action_id)
        if action_id == "done":
            return report
        elif action_id == "everything":
            from adapt_adb.everything import EverythingError, render_result, run_everything
            try:
                typer.echo(render_result(run_everything()))
            except EverythingError as exc:
                typer.echo(str(exc), err=True)
            report = run_doctor_checks(_ACTIVE_DOCTOR_ENV, command_name="doctor")
            typer.echo(render_doctor_human(report, show_fix=show_fix))
        elif action_id == "setup":
            setup_report = None
            captured: dict[str, BaseException] = {}
            try:
                setup_report = run_setup_workflow(
                    _ACTIVE_DOCTOR_ENV,
                    yes=True,
                    downloader=_capturing_downloader(captured),
                )
            except Exception as exc:
                typer.secho(f"Error: setup failed: {exc}", fg=typer.colors.RED, err=True)
                emit_failure(_setup_exception_failure_id(exc), _ctx("setup"))
            else:
                setup_meta = setup_report.get("setup", {}) if isinstance(setup_report, dict) else {}
                action = setup_meta.get("action")
                if action == "aborted_unsafe_server":
                    typer.secho(
                        "Aborted setup: Port 5037 has an unsafe or mismatched shared ADB server (exit code 3).",
                        fg=typer.colors.RED,
                        err=True,
                    )
                    emit_failure(_setup_report_failure_id(setup_report, captured) or "adb.server.unverified", _ctx("setup"))
                elif setup_meta.get("error"):
                    typer.secho(f"Error: {setup_meta['error']}", fg=typer.colors.RED, err=True)
                    emit_failure(_setup_report_failure_id(setup_report, captured) or "setup.failed", _ctx("setup"))
            report = run_doctor_checks(_ACTIVE_DOCTOR_ENV, command_name="doctor")
            typer.echo(render_doctor_human(report, show_fix=show_fix))
        elif action_id == "device_manager":
            try:
                os.startfile("devmgmt.msc")
            except Exception:
                subprocess.Popen(["devmgmt.msc"], shell=True)
            typer.echo("Device Manager opened.")
            sys.stderr.write("Press Enter when finished in Device Manager to recheck... ")
            sys.stderr.flush()
            sys.stdin.readline()
            report = run_doctor_checks(_ACTIVE_DOCTOR_ENV, command_name="doctor")
            typer.echo(render_doctor_human(report, show_fix=show_fix))
        elif action_id == "udev":
            rules_path = resolve_udev_rules_path()
            if rules_path is None or not rules_path.is_file():
                typer.secho(
                    "Error: 51-android.rules not found in packaging or package data.",
                    fg=typer.colors.RED,
                    err=True,
                )
                emit_failure("host.udev_rules_file_missing", _ctx("doctor"))
            else:
                host_os = str((report.get("host") or {}).get("os") or "")
                # Debian always has plugdev; elsewhere join it only when the group exists.
                join_plugdev = host_os == "debian" or _group_exists("plugdev") is not False
                try:
                    commands = [
                        ["sudo", "cp", "--", str(rules_path.resolve()), UDEV_RULES_TARGET],
                        ["sudo", "udevadm", "control", "--reload-rules"],
                        ["sudo", "udevadm", "trigger"],
                    ]
                    if join_plugdev:
                        commands.append(["sudo", "usermod", "-aG", "plugdev", getpass.getuser()])
                    for command in commands:
                        subprocess.run(command, check=True)
                except (subprocess.CalledProcessError, OSError):
                    typer.secho(
                        "Warning: Failed to execute udev rules setup.",
                        fg=typer.colors.YELLOW,
                        err=True,
                    )
                    emit_failure("host.udev_setup_failed", _ctx("doctor"))
                else:
                    if join_plugdev:
                        typer.echo(RELOGIN_NOTE)
                    else:
                        typer.echo(
                            "This system has no 'plugdev' group, so ADAPT did not change your groups. "
                            "For SSH sessions, join your distribution's equivalent group; " + RELOGIN_NOTE
                        )
            report = run_doctor_checks(_ACTIVE_DOCTOR_ENV, command_name="doctor")
            typer.echo(render_doctor_human(report, show_fix=show_fix))
        elif action_id == "recheck":
            report = run_doctor_checks(_ACTIVE_DOCTOR_ENV, command_name="doctor")
            typer.echo(render_doctor_human(report, show_fix=show_fix))
        elif action_id == "wireless":
            adb_path = resolve_adb()
            if adb_path is None:
                typer.secho(
                    "Error: adb binary not found. Run 'adapt setup' to install adb.",
                    fg=typer.colors.RED,
                    err=True,
                )
                emit_failure("adb.not_found", _ctx("pair", adb_resolved=False))
            else:
                typer.echo("")
                typer.echo("Wireless debugging requires Android 11+ on the same Wi-Fi network.")
                typer.echo("Note: The pairing port is NOT the connect port (pairing port != connect port).")
                typer.echo("1. Under Developer options -> Wireless debugging -> 'Pair device with pairing code', note IP:port and code.")
                typer.echo("2. Under Wireless debugging (main screen), note IP:port for connecting.")
                typer.echo("")
                sys.stderr.write("Pairing endpoint (ip:port): ")
                sys.stderr.flush()
                pairing_endpoint = sys.stdin.readline().strip()

                # Ask for the secret only when there is a usable pairing endpoint.
                pair_rc = 0
                pairing_code: str | None = None
                if pairing_endpoint:
                    if not _valid_endpoint(pairing_endpoint, require_port=True):
                        typer.secho(
                            "Error: pairing requires a valid host:port endpoint.",
                            fg=typer.colors.RED,
                            err=True,
                        )
                        emit_failure("wireless.endpoint_invalid", _ctx("pair"))
                        pair_rc = 1
                    else:
                        pairing_code = getpass.getpass("Pairing code: ")

                sys.stderr.write("Connect endpoint (ip:port): ")
                sys.stderr.flush()
                connect_endpoint = sys.stdin.readline().strip()

                if pairing_endpoint and pair_rc == 0:
                    pair_rc = run_pair_command(adb_path, pairing_endpoint, pairing_code or None)
                if connect_endpoint and pair_rc == 0:
                    run_connect_command(adb_path, connect_endpoint)
            report = run_doctor_checks(_ACTIVE_DOCTOR_ENV, command_name="doctor")
            typer.echo(render_doctor_human(report, show_fix=show_fix))
        else:
            return report

app = typer.Typer(
    cls=GuidedGroup,
    name="adapt",
    help="ADAPT - Android Device Agent Provisioning Toolkit",
    no_args_is_help=False,
    invoke_without_command=True,
    add_completion=False,
)


app.add_typer(dev_options_app, name="dev-options", callback=group_tip)
app.add_typer(tools_app, name="tools", callback=group_tip)
app.add_typer(android_app, name="android", callback=group_tip)
app.add_typer(dex_app, name="dex", callback=group_tip)
app.add_typer(detect_app, name="detect", callback=group_tip)
app.add_typer(desktop_app, name="desktop", callback=group_tip)
app.add_typer(ssh_app, name="ssh", callback=group_tip)
app.add_typer(app_app, name="app", callback=group_tip)
app.add_typer(logs_app, name="logs", callback=group_tip)
app.add_typer(debug_app, name="debug", callback=group_tip)
app.add_typer(rapid_app, name="rapid", callback=group_tip)
app.add_typer(tailscale_app, name="tailscale", callback=group_tip)


@app.callback()
def _root(
    ctx: typer.Context,
    no_banner: bool = typer.Option(
        False,
        "--no-banner",
        help="Hide the ADAPT ASCII name art (or set ADAPT_NO_BANNER=1).",
    ),
    no_tips: bool = typer.Option(False, "--no-tips", help="Hide contextual usage tips for this invocation."),
    no_progress: bool = typer.Option(False, "--no-progress", help="Disable the adapting activity indicator."),
) -> None:
    set_tip_context(ctx)
    set_progress_context(ctx)
    if ctx.invoked_subcommand in _PRIVILEGE_WARNING_COMMANDS:
        _emit_privilege_warning(ctx)
    if ctx.invoked_subcommand is None:
        if _is_interactive_session():
            _launch_console(ctx)
        else:
            typer.echo(ctx.get_help())
            raise typer.Exit(2)
        return
    if ctx.invoked_subcommand not in {"console", "home"}:
        emit_banner(ctx, no_banner=no_banner)
    if ctx.invoked_subcommand not in {"tools", "dev-options", "android", "dex", "detect", "desktop", "ssh", "app", "logs", "debug", "rapid", "tailscale", "console", "home"}:
        emit_context_tip(ctx.invoked_subcommand or "", ctx)


def _launch_console(ctx: typer.Context, *, no_redraw: bool = False) -> None:
    from typer.main import get_command
    from adapt_adb.terminal_ui import run_console
    flags = ctx.find_root().params
    try:
        run_console(get_command(app), no_redraw=no_redraw, no_banner=flags.get("no_banner", False),
                    no_tips=flags.get("no_tips", False), no_progress=flags.get("no_progress", False))
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc


@app.command("console")
def console_command(ctx: typer.Context, no_redraw: bool = typer.Option(False, "--no-redraw", help="Use an append-only console for screen readers or basic terminals.")) -> None:
    """Open ADAPT's main screen, with Home/Back navigation and paged command screens."""
    _launch_console(ctx, no_redraw=no_redraw)


app.command("home", hidden=True)(console_command)


@app.command("everything")
def everything(
    yes: bool = typer.Option(False, "--yes", "-y", help="Approve the supported batch setup steps."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show scope and tool detection without downloads, writes, or device access."),
    devices: list[str] | None = typer.Option(None, "--device", "-s", help="Also inspect this exact device, read-only; repeat for several."),
    no_path: bool = typer.Option(False, "--no-path", help="Do not update the user PATH during Platform-Tools setup."),
    json_output: bool = typer.Option(False, "--json", help="Emit one JSON report. New batch execution requires --yes."),
) -> None:
    """Give Me Everything: detect all catalog tools and run supported confirmed setup/checks."""
    from adapt_adb.everything import EverythingError, render_result, report_failure_id, run_everything

    def refuse_json_prompt(_plan: str) -> bool:
        raise failure_report.tagged(
            EverythingError("JSON execution requires --yes. Use --dry-run to preview without changes."),
            "everything.needs_confirmation",
        )

    refusal_id: str | None = None
    try:
        report = run_everything(yes=yes, dry_run=dry_run, devices=devices, no_path=no_path,
                                confirm=refuse_json_prompt if json_output and not yes else None)
    except EverythingError as exc:
        report = {"status": "refused", "exit_code": 2, "error": str(exc)}
        refusal_id = failure_report.classify(exc, area="everything")
        if refusal_id == "internal.unclassified":
            refusal_id = "everything.refused"
    # A batch that ran but is blocked, failed or incomplete names its guide too (exit code unchanged).
    result_id = None if refusal_id or "error" in report else report_failure_id(report)
    if json_output:
        if refusal_id or result_id:
            report = failure_report.attach_failure(report, refusal_id or result_id, _ctx("everything"))
        typer.echo(json.dumps(report, indent=2))
    elif "error" in report:
        typer.echo(report["error"], err=True)
        if refusal_id:
            emit_failure(refusal_id, _ctx("everything"))
    else:
        typer.echo(render_result(report))
        if result_id:
            emit_failure(result_id, _ctx("everything"))
    if report["exit_code"]:
        raise typer.Exit(report["exit_code"])


app.command("give-me-everything", hidden=True)(everything)


@app.command("help-me")
def help_me(
    topic: str | None = typer.Argument(None, help='Command or topic, e.g. wireless or "dev-options apply".'),
    all_commands: bool = typer.Option(False, "--all", help="Show every registered command and option."),
    json_output: bool = typer.Option(False, "--json", help="Emit the guide as JSON."),
) -> None:
    """Learn workflows or explore everything this installed ADAPT can do."""
    from typer.main import get_command

    try:
        payload = guide_payload(get_command(app), topic, all_commands)
    except ValueError as exc:
        if json_output:
            payload = {"error": "unknown_topic", "message": str(exc)}
            typer.echo(json.dumps(failure_report.attach_failure(payload, "input.help_topic_unknown", _ctx("help-me"))))
        else:
            typer.echo(str(exc), err=True)
            emit_failure("input.help_topic_unknown", _ctx("help-me"))
        raise typer.Exit(2) from None
    typer.echo(json.dumps(payload, indent=2) if json_output else render_guide(payload))


@app.command("explain")
def explain(
    query: list[str] | None = typer.Argument(None, help='Failure ID, keyword, or pasted error text, e.g. device.unauthorized or "failed to authenticate".'),
    list_all: bool = typer.Option(False, "--list", help="List every known failure ID with a one-line summary."),
    json_output: bool = typer.Option(False, "--json", help="Emit the explanation or list as JSON."),
) -> None:
    """Explain a failure: what happened, possible causes and what to try next. Runs nothing."""
    text = " ".join(query or []).strip()
    matches = [] if list_all or not text else failure_report.search(text)
    if text and not list_all and not matches:
        message = "No known failure matches that text. Run 'adapt explain --list' for every failure ID."
        if json_output:
            typer.echo(json.dumps({"error": "unknown_failure", "message": message}))
        else:
            typer.echo(message, err=True)
        raise typer.Exit(2)
    if len(matches) == 1:
        resolved = failure_report.resolve(matches[0])
        if json_output:
            typer.echo(json.dumps({"failure": failure_report.failure_payload(resolved, full=True)}, indent=2))
        else:
            typer.echo(failure_report.render_explain(resolved))
        return
    entries = failure_report.list_entries(matches or None)
    if json_output:
        typer.echo(json.dumps({"failures": entries}, indent=2))
    else:
        typer.echo(failure_report.render_list(entries, title="matching failures" if matches else "known failures"))


@app.command("tips")
def tips(
    mode: str = typer.Argument("status", help="on, off, or status; on/off persist for this OS user."),
    json_output: bool = typer.Option(False, "--json", help="Emit preference status as JSON."),
) -> None:
    """Enable or disable instructional tips without disabling errors or confirmations."""
    mode = mode.lower()
    try:
        if mode not in {"on", "off", "status"}:
            raise ValueError("Choose on, off, or status.")
        if mode != "status":
            save_tips(mode == "on")
        enabled = read_preferences().get("tips", True)
    except (OSError, ValueError):
        payload = {"error": "tips_preference", "message": "Could not use tips preferences. Choose on/off/status; --no-tips remains available."}
        if mode not in {"on", "off", "status"}:
            failure_id = "input.tips_mode_invalid"
        else:
            failure_id = "prefs.unreadable"
        if json_output:
            typer.echo(json.dumps(failure_report.attach_failure(payload, failure_id, _ctx("tips"))))
        else:
            typer.echo(payload["message"], err=True)
            emit_failure(failure_id, _ctx("tips"))
        raise typer.Exit(2) from None
    payload = {"tips": enabled, "scope": "saved user preference", "override": "--no-tips or ADAPT_TIPS=0 can suppress tips for an invocation"}
    typer.echo(json.dumps(payload) if json_output else f"Tips {'on' if enabled else 'off'} (saved user preference). {payload['override']}.")


@app.command()
def doctor(
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit machine-checkable JSON diagnostic report on stdout.",
    ),
    fix: bool = typer.Option(
        False,
        "--fix",
        help="Print safe remediation commands only (never kills server, runs sudo, or wipes adbkey).",
    ),
) -> None:
    """Locate adb, inspect port 5037 safety, OS prerequisites, and device states."""
    report = run_doctor_checks(_ACTIVE_DOCTOR_ENV, command_name="doctor")
    if json_output:
        sys.stdout.write(json.dumps(report, indent=2) + "\n")
        sys.stdout.flush()
    else:
        typer.echo(render_doctor_human(report, show_fix=fix))
        if _is_interactive_session():
            report = _run_next_menu(report, show_fix=fix)

    raise typer.Exit(code=report["exit_code"])


@app.command()
def devices() -> None:
    """List connected devices via 'adb devices -l'."""
    adb_path = resolve_adb()
    if adb_path is None:
        typer.secho("Error: adb binary not found.", fg=typer.colors.RED, err=True)
        typer.echo("Run 'adapt setup' to install adb.", err=True)
        emit_failure("adb.not_found", _ctx("devices", adb_resolved=False))
        raise typer.Exit(code=1)

    try:
        preflight_adb_server(adb_path, ["devices", "-l"])
    except UnsafeAdbServerError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        emit_failure(failure_report.classify(exc), _ctx("devices", adb_server_option_set=False))
        raise typer.Exit(code=3)
    try:
        result = subprocess.run([str(adb_path), "devices", "-l"], timeout=10)
    except subprocess.TimeoutExpired:
        typer.secho("Error: ADB device listing timed out.", fg=typer.colors.RED, err=True)
        emit_failure("adb.timeout", _ctx("devices"))
        raise typer.Exit(code=1)
    except OSError:
        typer.secho("Error: Could not execute ADB.", fg=typer.colors.RED, err=True)
        emit_failure("adb.exec_failed", _ctx("devices"))
        raise typer.Exit(code=1)
    if result.returncode != 0:
        # adb printed its own message (if any) on the inherited stderr; guidance follows it.
        emit_failure("adb.devices_failed", _ctx("devices"))
        raise typer.Exit(code=result.returncode)


@app.command()
def setup(
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Run non-interactively without confirmation prompts.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Report planned download and PATH actions without mutating host state.",
    ),
    no_path: bool = typer.Option(
        False,
        "--no-path",
        help="Unpack platform-tools without modifying user PATH.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit machine-checkable JSON setup report on stdout.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Force download and unpack even if adb is already usable.",
    ),
    isolated: bool = typer.Option(
        False,
        "--isolated",
        help="Unpack into isolated ~/.android/platform-tools directory.",
    ),
    sha256: str | None = typer.Option(
        None,
        "--sha256",
        help="Expected SHA-256 hex digest for the platform-tools zip archive.",
    ),
) -> None:
    """Download and configure official Google platform-tools in user scope."""
    captured: dict[str, BaseException] = {}
    try:
        report = run_setup_workflow(
            _ACTIVE_DOCTOR_ENV,
            force=force,
            dry_run=dry_run,
            no_path=no_path,
            yes=yes,
            isolated=isolated,
            expected_sha256=sha256,
            downloader=_capturing_downloader(captured),
        )
    except Exception as exc:
        failure_id = _setup_exception_failure_id(exc)
        if json_output:
            err_payload = {
                "schema_version": "1",
                "command": "setup",
                "exit_code": 1,
                "status": "missing_toolchain",
                "error": str(exc),
            }
            err_payload = failure_report.attach_failure(err_payload, failure_id, _ctx("setup"))
            sys.stdout.write(json.dumps(err_payload, indent=2) + "\n")
            sys.stdout.flush()
        else:
            typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
            emit_failure(failure_id, _ctx("setup"))
        raise typer.Exit(code=1)

    report_failure_id = _setup_report_failure_id(report, captured)
    if json_output:
        if report_failure_id:
            report = failure_report.attach_failure(report, report_failure_id, _ctx("setup"))
        sys.stdout.write(json.dumps(report, indent=2) + "\n")
        sys.stdout.flush()
    else:
        setup_meta = report.get("setup", {})
        action = setup_meta.get("action")
        if action in ("failed", "unsupported_architecture", "unsupported_platform"):
            message = setup_meta.get("error") or report.get("error") or "Setup failed."
            typer.secho(f"Error: {message}", fg=typer.colors.RED, err=True)
            emit_failure(report_failure_id or "setup.failed", _ctx("setup"))
            raise typer.Exit(code=report["exit_code"])
        if action == "aborted_unsafe_server":
            typer.secho(
                "Aborted setup: Port 5037 has an unsafe or mismatched shared ADB server (exit code 3).",
                fg=typer.colors.RED,
                err=True,
            )
            emit_failure(report_failure_id or "adb.server.unverified", _ctx("setup"))
        elif action == "aborted_needs_yes":
            typer.secho(
                "Aborted setup: Confirmation is unavailable; pass --yes to add the existing "
                "platform-tools folder to PATH."
                if setup_meta.get("reused_existing")
                else "Aborted setup: Confirmation is unavailable; pass --yes to install.",
                fg=typer.colors.RED,
                err=True,
            )
            emit_failure("setup.needs_confirmation", _ctx("setup"))
        elif action == "aborted_unconfirmed":
            typer.secho(
                "Aborted setup: User declined confirmation prompt.",
                fg=typer.colors.RED,
                err=True,
            )
            emit_failure("setup.declined", _ctx("setup"))
        elif action == "noop_already_healthy":
            typer.echo(f"ADB is already usable at: {report['toolchain']['adb_path']}")
            typer.echo(f"Version: {report['toolchain']['platform_tools_version']}")
            typer.echo("Skipping download.")
        elif action == "noop_already_installed":
            typer.echo(f"Healthy platform-tools are already installed at: {setup_meta.get('target_dir')}")
            typer.echo("Skipping download; PATH not changed (--no-path).")
        elif action == "path_registered":
            typer.echo(f"Healthy platform-tools are already installed at: {setup_meta.get('target_dir')}")
            typer.echo("Skipping download; added that folder to your user PATH.")
            typer.echo("PATH updated for new terminals: reopen any terminal that was already open (including this one) before running adb or adapt there.")
        elif action == "dry_run":
            if setup_meta.get("would_download") is False:
                typer.echo("[DRY-RUN] Would not download: healthy platform-tools are already installed.")
            else:
                typer.echo(f"[DRY-RUN] Would download: {setup_meta.get('url')}")
            typer.echo(f"[DRY-RUN] Target directory: {setup_meta.get('target_dir')}")
            typer.echo(f"[DRY-RUN] Would update user PATH: {setup_meta.get('would_update_path')}")
        else:
            typer.echo(f"Downloaded official platform-tools from: {setup_meta.get('url')}")
            typer.echo(f"Target directory: {setup_meta.get('target_dir')}")
            typer.echo("Download and extraction completed successfully.")
            if no_path:
                typer.echo("Skipped PATH modification (--no-path).")
            elif setup_meta.get("path_updated"):
                typer.echo("PATH updated for new terminals: reopen any terminal that was already open (including this one) before running adb or adapt there.")

        typer.echo("")
        typer.echo(render_doctor_human(report, show_fix=False))
        if _is_interactive_session():
            report = _run_next_menu(report, show_fix=False)
        else:
            typer.echo("")
            typer.echo("Next:")
            if report.get("host", {}).get("os") == "windows":
                typer.echo(
                    "  - Install Google USB Driver or Samsung OEM USB driver via Device Manager."
                )
                typer.echo("  - Run 'adapt doctor' to recheck.")
            else:
                typer.echo(_udev_next_step(str(report.get("host", {}).get("os") or "")))
                typer.echo("  - Run 'adapt doctor' to recheck.")

    raise typer.Exit(code=report["exit_code"])


@app.command()
def mcp(
    read_only: bool = typer.Option(False, "--read-only", help="Expose inspection tools and deny interaction/install calls."),
    device: list[str] | None = typer.Option(None, "--device", help="Restrict calls to this exact transport; repeat to allow several."),
) -> None:
    """Start the ADAPT stdio MCP server."""
    try:
        from adapt_adb.mcp.server import run_stdio

        if read_only or device:
            from adapt_adb.mcp.access import AccessPolicy
            try:
                policy = AccessPolicy(read_only=read_only, allowed_devices=tuple(device) if device else None)
            except ValueError:
                typer.echo("Error: Device restrictions require complete nonblank targets.", err=True)
                raise typer.Exit(2) from None
            run_stdio(policy=policy)
        else:
            run_stdio()
    except ImportError as exc:
        sys.stderr.write(_mcp_import_error_line(exc))
        sys.stderr.flush()
        emit_failure("mcp.sdk_missing", _ctx("mcp"))
        raise typer.Exit(code=2)


def _mcp_sdk_installed() -> bool:
    try:
        return importlib.util.find_spec("mcp") is not None
    except (ImportError, ValueError):
        return False


def _mcp_import_error_line(exc: ImportError) -> str:
    """Tell "not installed" apart from "installed but failed to import"; names a module, never a path."""
    failing = exc.name if isinstance(exc.name, str) and exc.name else None
    if failing == "mcp" or not _mcp_sdk_installed():
        return "Error: Python MCP SDK is not installed (install with 'adapt-adb[mcp]').\n"
    return (
        "Error: Python MCP SDK is installed but could not be imported "
        f"(failing module: {failing or 'unknown'}). Reinstall 'adapt-adb[mcp]' in this Python environment.\n"
    )


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True}
)
def adb(ctx: typer.Context) -> None:
    """Pass remaining arguments through to the resolved adb binary."""
    adb_path = resolve_adb()
    if adb_path is None:
        typer.secho("Error: adb binary not found.", fg=typer.colors.RED, err=True)
        typer.echo("Run 'adapt setup' to install adb.", err=True)
        emit_failure("adb.not_found", _ctx("adb", adb_resolved=False))
        raise typer.Exit(code=1)

    raw_args = list(ctx.args)
    try:
        parsed = split_adb_prefix(raw_args)
        if not parsed.command:
            raise failure_report.tagged(ValueError("Missing ADB command"), "input.adb_command_missing")
        args = parsed.prefix + parsed.command
        validate_adb_args(args)
    except (SafetyError, ValueError) as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        emit_failure(failure_report.explicit_id(exc) or "input.adb_args_refused", _ctx("adb"))
        raise typer.Exit(code=1)

    if parsed.command[0] == "pair":
        # Never place a pairing code in adb's argv (visible in process listings):
        # use the protected `adapt pair` path, which sends the code on stdin.
        operands = parsed.command[1:]
        if parsed.prefix or not 1 <= len(operands) <= 2:
            typer.secho(
                "Error: use 'adapt adb pair ENDPOINT [CODE]' with no other ADB options or operands.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)
        rc = _pair_with_code_prompt(adb_path, operands[0], operands[1] if len(operands) == 2 else None)
        raise typer.Exit(code=rc)

    if parsed.command[0] == "connect":
        operands = parsed.command[1:]
        if parsed.prefix or len(operands) != 1:
            typer.secho(
                "Error: use 'adapt adb connect ENDPOINT' with no other ADB options or operands.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)
        raise typer.Exit(code=run_connect_command(adb_path, parsed.command[1]))

    try:
        preflight_adb_server(adb_path, args)
    except UnsafeAdbServerError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        emit_failure(failure_report.classify(exc), _ctx("adb", adb_server_option_set=_uses_server_option(parsed.prefix)))
        raise typer.Exit(code=3)

    host_only = is_host_only_adb_command(args)
    env_serial = os.environ.get("ANDROID_SERIAL", "").strip()
    explicit_serial = parsed.serial or (env_serial if not parsed.selector else None)
    if not host_only and parsed.selector in (None, "-s"):
        try:
            # The shared-server preflight ran just above for this same invocation.
            devices_list = get_connected_devices(adb_path, server_checked=True)
        except AdbProbeError as exc:
            typer.secho(
                "Error: ADB device enumeration failed; target state is unknown.",
                fg=typer.colors.RED,
                err=True,
            )
            emit_failure(failure_report.classify(exc), _ctx("adb"))
            raise typer.Exit(code=1)
        ready_devices = [d for d in devices_list if d.get("state") == "device"]
        if explicit_serial:
            match = next((d for d in devices_list if d.get("serial") == explicit_serial), None)
            if match is None or match.get("state") != "device":
                typer.secho(
                    "Error: Explicit target is unavailable or not in state 'device'.",
                    fg=typer.colors.RED,
                    err=True,
                )
                from_env = parsed.serial is None
                if match is None:
                    emit_failure("device.not_found", _ctx("adb", device_state="absent", target_from_env=from_env))
                else:
                    state = failure_report.device_state_key(match.get("state"))
                    emit_failure(failure_report.device_state_failure_id(state),
                                 _ctx("adb", device_state=state, target_from_env=from_env))
                raise typer.Exit(code=1)
            if parsed.serial is None:
                args = ["-s", explicit_serial] + args
        elif parsed.selector is None:
            if len(ready_devices) == 0:
                typer.secho(
                    "Error: No ready Android devices connected (state 'device'). Run 'adapt doctor' to diagnose.",
                    fg=typer.colors.RED,
                    err=True,
                )
                _emit_no_ready_device(devices_list)
                raise typer.Exit(code=1)
            if len(ready_devices) > 1:
                typer.secho(
                    f"Error: Multiple devices connected ({len(ready_devices)}). Specify a target device with -s or --serial.",
                    fg=typer.colors.RED,
                    err=True,
                )
                typer.echo("Connected devices:", err=True)
                for dev in ready_devices:
                    typer.echo(f"  {dev['serial']}\t({dev['state']})", err=True)
                emit_failure("device.multiple", _ctx("adb", ready_count=len(ready_devices),
                                                     not_ready_count=len(devices_list) - len(ready_devices)))
                raise typer.Exit(code=1)
            args = ["-s", str(ready_devices[0]["serial"])] + args

    cmd_name = parsed.command[0] if parsed.command else ""
    cmd_timeout = 60 if cmd_name in FINITE_ADB_COMMANDS else None
    try:
        result = subprocess.run(
            [str(adb_path)] + args,
            timeout=cmd_timeout,
        )
    except subprocess.TimeoutExpired:
        typer.secho("Error: ADB command timed out.", fg=typer.colors.RED, err=True)
        emit_failure("adb.timeout", _ctx("adb"))
        raise typer.Exit(code=1)
    except OSError:
        typer.secho("Error: Could not execute ADB.", fg=typer.colors.RED, err=True)
        emit_failure("adb.exec_failed", _ctx("adb"))
        raise typer.Exit(code=1)
    raise typer.Exit(code=result.returncode)


def _emit_no_ready_device(devices_list: list[dict[str, str]]) -> None:
    """Pick the most specific guide from the device list already in memory (design 2.3)."""
    states = [failure_report.device_state_key(d.get("state")) for d in devices_list]
    if not states:
        emit_failure("device.none_ready", _ctx("adb", device_state="absent", ready_count=0, not_ready_count=0))
        return
    if len(set(states)) == 1:
        emit_failure(failure_report.device_state_failure_id(states[0]),
                     _ctx("adb", device_state=states[0], ready_count=0, not_ready_count=len(states)))
        return
    leading = next(s for s in ("unauthorized", "no_permissions", "offline", "other") if s in states)
    emit_failure("device.none_ready", _ctx("adb", device_state=leading, ready_count=0, not_ready_count=len(states)))


@app.command()
def pair(
    endpoint: str = typer.Argument(
        ...,
        help="ip:pairing-port from Developer options -> Wireless debugging -> Pair with pairing code.",
    ),
    code: str | None = typer.Argument(
        None,
        help="Pairing code from Wireless debugging.",
    ),
) -> None:
    """Pair with an Android device over Wi-Fi (official adb pair)."""
    adb_path = resolve_adb()
    if adb_path is None:
        typer.secho("Error: adb binary not found.", fg=typer.colors.RED, err=True)
        typer.echo("Run 'adapt setup' to install adb.", err=True)
        emit_failure("adb.not_found", _ctx("pair", adb_resolved=False))
        raise typer.Exit(code=1)

    rc = _pair_with_code_prompt(adb_path, endpoint, code)
    raise typer.Exit(code=rc)


@app.command()
def connect(
    endpoint: str = typer.Argument(
        ...,
        help="ip:connect-port from Wireless debugging screen (default port 5555 if :port omitted).",
    ),
) -> None:
    """Connect to an Android device over Wi-Fi (official adb connect)."""
    adb_path = resolve_adb()
    if adb_path is None:
        typer.secho("Error: adb binary not found.", fg=typer.colors.RED, err=True)
        typer.echo("Run 'adapt setup' to install adb.", err=True)
        emit_failure("adb.not_found", _ctx("connect", adb_resolved=False))
        raise typer.Exit(code=1)

    rc = run_connect_command(adb_path, endpoint)
    raise typer.Exit(code=rc)


@app.command()
def guide(
    topic: str | None = typer.Argument(
        None,
        help="Topic to inspect: testing, rom, ai, wireless (default: list topics)",
    ),
) -> None:
    """Print official Google/AOSP tool recommendations and best practices."""
    try:
        from adapt_adb.insights import render_guide_table

        output = render_guide_table(topic)
        typer.echo(output)
    except KeyError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    except ImportError:
        key = topic.strip().lower() if topic else None
        if key is None or key == "all":
            typer.echo("Official Google/AOSP Tooling Guides:")
            typer.echo("")
            for k, text in _FALLBACK_GUIDE.items():
                typer.echo(f"  [{k}] {text}")
            typer.echo("")
            typer.echo("Run 'adapt guide <topic>' for details.")
        elif key in _FALLBACK_GUIDE:
            typer.echo(f"Guide: {key}")
            typer.echo(_FALLBACK_GUIDE[key])
        else:
            typer.secho(
                f"Error: Unknown guide topic '{topic}'. Available: testing, rom, ai, wireless",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
