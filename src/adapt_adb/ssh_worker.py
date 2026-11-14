"""Explicit SSH worker transport; no local ADB fallback or arbitrary commands."""
from __future__ import annotations

import base64
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import typer

from adapt_adb.device_state import validate_target
from adapt_adb.failure_report import FailureContext, attach_failure, classify, current_command, emit_failure, emit_pointer, tagged
from adapt_adb.progress import operation

ssh_app = typer.Typer(help="Run read-only ADAPT checks on an explicitly selected SSH worker.")
OPERATIONS = ("doctor", "scan", "inspect", "android", "desktop", "dex", "tools")
DEVICE_OPERATIONS = frozenset({"inspect", "android", "desktop", "dex"})
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024
SSH_OPTIONS = (
    "BatchMode=yes", "StrictHostKeyChecking=yes", "UpdateHostKeys=no",
    "CheckHostIP=no", "VerifyHostKeyDNS=no",
    "ClearAllForwardings=yes", "ForwardAgent=no", "ForwardX11=no",
    "PermitLocalCommand=no", "RemoteCommand=none", "ControlPath=none",
    "ControlMaster=no", "ControlPersist=no",
    "ConnectTimeout=10", "ConnectionAttempts=1", "ServerAliveInterval=15",
    "ServerAliveCountMax=2",
)


class SshWorkerError(ValueError):
    pass


class _ResponseLimitError(SshWorkerError):
    pass


def resolve_ssh() -> str | None:
    override = os.environ.get("ADAPT_SSH", "").strip()
    if override:
        candidate = Path(override).expanduser()
        return str(candidate.resolve()) if candidate.is_file() else None
    return shutil.which("ssh")


def status() -> dict[str, Any]:
    binary = resolve_ssh()
    report = {"schema_version": 1, "status": "located_unverified" if binary else "missing_client",
            "binary": binary, "network_access": False,
            "note": "PATH/override observation only. No SSH configuration, keys or hosts were read or contacted.",
            "setup": "Install the OpenSSH client through Windows Optional Features or Debian's openssh-client package. Configure a verified host/key separately."}
    if not binary and os.environ.get("ADAPT_SSH", "").strip():
        report["override_error"] = "ADAPT_SSH is set but does not point to an existing file; no other SSH client was tried."
    return report


def _validate_options(host: str, remote_shell: str, remote_adapt: str, port: int | None, identity: str | None):
    if not isinstance(host, str) or len(host) > 320 or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@:\[\]-]*", host) or host.count("@") > 1 or host.endswith("@"):
        raise tagged(SshWorkerError("Use an SSH host alias, hostname or IP, optionally user@host; no URLs, whitespace or command options."), "ssh.host_invalid")
    if remote_shell not in {"posix", "powershell"}:
        raise tagged(SshWorkerError("Remote shell must be posix or powershell."), "ssh.option_invalid")
    if not isinstance(remote_adapt, str) or not remote_adapt or len(remote_adapt) > 2048 or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in remote_adapt):
        raise tagged(SshWorkerError("Choose adapt or an absolute path to the remote adapt executable."), "ssh.option_invalid")
    path = PureWindowsPath(remote_adapt) if remote_shell == "powershell" else PurePosixPath(remote_adapt)
    if path.name.lower() not in {"adapt", "adapt.exe"} or not (remote_adapt.lower() in {"adapt", "adapt.exe"} or path.is_absolute()):
        raise tagged(SshWorkerError("Remote executable must be adapt/adapt.exe or its absolute path, not a shell command."), "ssh.option_invalid")
    if port is not None and (type(port) is not int or not 1 <= port <= 65535):
        raise tagged(SshWorkerError("SSH port must be between 1 and 65535."), "ssh.option_invalid")
    if identity is not None and (not identity or len(identity) > 4096 or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in identity)):
        raise tagged(SshWorkerError("Identity must be a local key-file path without control characters."), "ssh.option_invalid")


def _remote_command(args: list[str], remote_shell: str) -> str:
    if remote_shell == "posix":
        return shlex.join(args)
    # OpenSSH sends a shell command string, not a remote argv array. Encode the
    # PowerShell script so the Windows default cmd shell sees no user syntax.
    # Embedded double quotes in native arguments are unsupported by legacy
    # Windows PowerShell; refuse them rather than silently changing a target.
    if any('"' in arg for arg in args):
        raise tagged(SshWorkerError("PowerShell worker arguments cannot contain embedded double quotes; select a different exact transport identifier."), "ssh.option_invalid")
    quote = lambda value: "'" + value.replace("'", "''") + "'"
    script = "& " + " ".join(quote(arg) for arg in args) + "; exit $LASTEXITCODE"
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    return "powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand " + encoded


def _argv(host: str, remote_args: list[str], *, remote_shell: str, port: int | None, identity: str | None) -> list[str]:
    argv = ["ssh", "-T"]
    for item in SSH_OPTIONS:
        argv.extend(["-o", item])
    if port is not None:
        argv.extend(["-p", str(port)])
    if identity is not None:
        argv.extend(["-i", identity])
    return [*argv, "--", host, _remote_command(remote_args, remote_shell)]


def plan(host: str, *, operation: str = "doctor", device: str | None = None, transport: str = "adb",
         remote_shell: str = "posix", remote_adapt: str = "adapt", port: int | None = None,
         identity: str | None = None) -> dict[str, Any]:
    _validate_options(host, remote_shell, remote_adapt, port, identity)
    if operation not in OPERATIONS or transport not in {"adb", "fastboot", "all"}:
        raise tagged(SshWorkerError("Choose a supported read-only operation and adb/fastboot/all transport."), "ssh.operation_invalid")
    if operation in DEVICE_OPERATIONS:
        if device is None:
            raise tagged(SshWorkerError("This operation requires an exact --device on the remote worker."), "ssh.operation_invalid")
        validate_target(device)
    elif device is not None:
        raise tagged(SshWorkerError("This host/list operation is not device-scoped. Choose inspect, android, desktop or dex for --device."), "ssh.operation_invalid")
    if operation not in {"scan", "inspect"} and transport != "adb" or operation == "inspect" and transport == "all":
        raise tagged(SshWorkerError("Only scan supports all transports; fastboot is available for scan and inspect only."), "ssh.operation_invalid")
    commands = {
        "doctor": ["doctor"], "scan": ["detect", "scan", "--transport", transport],
        "inspect": ["detect", "inspect", "--transport", transport],
        "android": ["android", "capabilities"], "desktop": ["desktop", "inspect"],
        "dex": ["dex", "inspect"], "tools": ["tools", "list"],
    }
    remote_args = [remote_adapt, "--no-banner", "--no-tips", "--no-progress", *commands[operation]]
    if device is not None:
        remote_args.extend(["-s", device])
    remote_args.append("--json")
    return {"schema_version": 1, "status": "planned", "worker": {"host": host, "transport": "ssh", "remote_shell": remote_shell},
            "operation": operation, "device": device, "remote_argv": remote_args,
            "ssh_argv": _argv(host, remote_args, remote_shell=remote_shell, port=port, identity=identity),
            "network_access": False, "local_adb_access": False, "automatic_fallback": False,
            "note": "The selected worker runs ADAPT and owns its ADB/fastboot access. This preview executes nothing and does not read SSH configuration."}


def _execute(argv: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
    stdout = bytearray(); stderr = bytearray(); failures: list[Exception] = []
    def kill():
        try: process.kill()
        except ProcessLookupError: pass
    def read(stream, output, limit, message):
        try:
            while chunk := stream.read1(min(64 * 1024, limit - len(output) + 1)):
                if len(output) + len(chunk) > limit:
                    failures.append(_ResponseLimitError(message)); kill(); return
                output.extend(chunk)
        except Exception as exc:
            failures.append(exc); kill()
    readers = [threading.Thread(target=read, args=(process.stdout, stdout, MAX_RESPONSE_BYTES, "Worker JSON response exceeded the accepted 4 MiB limit.")),
               threading.Thread(target=read, args=(process.stderr, stderr, MAX_STDERR_BYTES, "SSH stderr exceeded the accepted 64 KiB limit."))]
    started = []
    try:
        for reader in readers:
            reader.start(); started.append(reader)
        process.wait(timeout=timeout)
    finally:
        if process.poll() is None: kill()
        process.wait()
        for reader in started: reader.join()
        process.stdout.close(); process.stderr.close()
    if failures: raise failures[0]
    return subprocess.CompletedProcess(argv, process.returncode, stdout.decode("utf-8", errors="strict"), stderr.decode("utf-8", errors="strict"))


_SSH_GENERIC_ERROR = "Verify connectivity, existing host-key trust, key/agent authentication and worker configuration. No host keys were automatically accepted."
# First matching rule wins, so order encodes specificity: a changed key also prints
# "Host key verification failed", and connection-level errors can follow an identity
# warning. Matching is case-insensitive on whitespace-normalised stderr.
_SSH_FAILURE_RULES = tuple((code, re.compile(pattern, re.I)) for code, pattern in (
    ("host_key_changed", r"remote host identification has changed|offending (?:\S+ ){0,3}key|has changed and you have requested strict checking"),
    ("host_key_unknown", r"host key verification failed|no (?:\S+ )?host key is known"),
    ("name_resolution_failed", r"could not resolve hostname|name or service not known|temporary failure in name resolution|nodename nor servname|no such host is known"),
    ("connection_refused", r"connection refused"),
    ("connection_timed_out", r"timed out|timeout, server \S+ not responding"),
    ("network_unreachable", r"no route to host|network is unreachable"),
    ("connection_closed", r"connection closed by|connection reset by|kex_exchange_identification|closed by remote host|broken pipe"),
    ("identity_file_problem", r"bad permissions|unprotected private key file|are too open|no such identity|load key \"|identity file .{0,300}not accessible"),
    ("authentication_failed", r"(?<!\()permission denied|too many authentication failures|no more authentication methods"),
))
# Fixed (statement, hint) per reason; nothing here is ever derived from ssh output.
_SSH_FAILURE_TEXT = {
    "host_key_unknown": ("SSH reported that the host key is not trusted.",
                         "The worker's host key is not in known_hosts; connect once with ssh yourself and verify the key fingerprint out of band before trusting it, because ADAPT never accepts host keys automatically."),
    "host_key_changed": ("SSH reported that the worker's host key has changed.",
                         "The host key the worker presented no longer matches the one stored in known_hosts; verify the change with the worker's administrator through a trusted channel before altering known_hosts, because it can also mean the connection is being intercepted."),
    "authentication_failed": ("SSH reported that authentication failed.",
                              "Batch mode cannot prompt for a password or passphrase, so load a key for this account into your SSH agent or give it with the identity option, and confirm the worker accepts that key."),
    "name_resolution_failed": ("SSH could not resolve the worker host name.",
                               "Check the host name or alias spelling, your SSH configuration and DNS, or use the worker's IP address."),
    "connection_refused": ("The worker refused the SSH connection.",
                           "Check that the worker's SSH server is running and listening on the selected port and that no firewall rejects the connection."),
    "connection_timed_out": ("The SSH connection timed out.",
                             "Check that the worker is on and reachable from this PC, that the address and port are right, and that no firewall or VPN is dropping the connection."),
    "network_unreachable": ("SSH reported that the worker's network or route is unreachable.",
                            "Check this PC's network, VPN or tailnet connection and the route to the worker's address."),
    "connection_closed": ("The worker or a device on the path closed or reset the SSH connection.",
                          "Check the worker's SSH server logs and access limits such as allowed users or connection limits, and any proxy or jump host in your SSH configuration."),
    "identity_file_problem": ("SSH reported a problem with a private key (identity) file.",
                              "Check that the key file given with the identity option or your SSH configuration exists, is a valid private key and is readable only by you."),
    "unknown": ("", "Run the SSH command from adapt ssh plan in a terminal to see the full message; ADAPT never copies ssh's own output into its reports."),
}
_RESPONSE_TEXT = {
    "remote_adapt_not_found": "The remote ADAPT program was not found; install this ADAPT version on the worker or give the absolute path of its adapt executable with the remote-adapt option, because a non-interactive SSH session may have a different PATH than a login shell.",
    "remote_shell_mismatch": "The worker answered with a shell of the other family than the selected remote shell; choose powershell for a Windows worker (with an absolute remote-adapt path if adapt is not on its PATH) and posix for a Linux, macOS or other Unix-like worker.",
    "not_json": "The worker printed output that is not one ADAPT JSON object; check for login-script or banner output on stdout and for an incompatible ADAPT version.",
    "empty_response": "The worker printed no output; check that ADAPT runs on the worker and that its version matches, then run the remote command there to see why.",
    "unexpected_exit_code": "The worker printed a JSON object but exited outside ADAPT's 0-3 range; check that the remote adapt is this ADAPT and not a wrapper script.",
}
_POSIX_SHELL_MARKERS = re.compile(r"^-?(?:ba|z|da|k|a|c|tc|fi)?sh:|powershell\.exe[^\n]*not found|not found[^\n]*powershell\.exe", re.I | re.M)
_WINDOWS_SHELL_MARKERS = re.compile(r"is not recognized as|commandnotfoundexception|fullyqualifiederrorid|categoryinfo|#< clixml|the syntax of the command is incorrect", re.I)
_ADAPT_MISSING = re.compile(r"command not found|adapt(?:\.exe)?'?: not found|is not recognized as|no such file or directory", re.I)


def _classify_ssh_failure(returncode: int, stderr: str) -> str:
    """Map OpenSSH client stderr to a fixed reason code; only ssh's own exit 255 is classified."""
    if returncode != 255:
        return "unknown"
    text = " ".join((stderr or "").split())
    return next((code for code, pattern in _SSH_FAILURE_RULES if pattern.search(text)), "unknown")


def _classify_invalid_response(returncode: int, stdout: str, stderr: str, *, json_object: bool, remote_shell: str) -> str:
    """Explain why a worker reply was not one ADAPT JSON object, from fixed markers only."""
    stderr = stderr or ""
    if (_POSIX_SHELL_MARKERS if remote_shell == "powershell" else _WINDOWS_SHELL_MARKERS).search(stderr):
        return "remote_shell_mismatch"
    if returncode == 127 or ("adapt" in stderr.lower() and _ADAPT_MISSING.search(stderr)):
        return "remote_adapt_not_found"
    if not (stdout or "").strip():
        return "empty_response"
    return "unexpected_exit_code" if json_object else "not_json"


@operation("Inspecting SSH worker")
def run(host: str, *, timeout: int = 180, **kwargs) -> dict[str, Any]:
    report = plan(host, **kwargs)
    if type(timeout) is not int or not 1 <= timeout <= 300:
        raise tagged(SshWorkerError("SSH operation timeout must be between 1 and 300 seconds."), "ssh.option_invalid")
    binary = resolve_ssh()
    if binary is None:
        raise tagged(SshWorkerError("OpenSSH client not found. Run adapt ssh status for setup guidance."), "ssh.client_not_found")
    argv = [binary, *report["ssh_argv"][1:]]
    base = {"schema_version": 1, "worker": report["worker"], "operation": report["operation"], "device": report["device"],
            "network_access": True, "local_adb_access": False, "automatic_fallback": False}
    try:
        result = _execute(argv, timeout)
    except _ResponseLimitError as exc:
        return {**base, "status": "invalid_response", "exit_code": 1, "error": str(exc)}
    except subprocess.TimeoutExpired:
        return {**base, "status": "timeout", "exit_code": 1, "error": "SSH client timed out; no local fallback ran. A remote read-only process may briefly remain on the worker."}
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return {**base, "status": "ssh_failed", "exit_code": 1, "error": "SSH could not complete or its response encoding was invalid; no fallback ran."}
    if result.returncode == 255:
        reason = _classify_ssh_failure(255, result.stderr)
        statement, hint = _SSH_FAILURE_TEXT[reason]
        return {**base, "status": "ssh_failed", "exit_code": 1, "ssh_exit_code": 255,
                "error": f"{statement} {_SSH_GENERIC_ERROR}" if statement else _SSH_GENERIC_ERROR,
                "ssh_failure_reason": reason, "hint": hint}
    output = result.stdout or ""
    if len(output.encode("utf-8")) > MAX_RESPONSE_BYTES:
        return {**base, "status": "invalid_response", "exit_code": 1, "error": "Worker JSON response exceeded the accepted 4 MiB limit."}
    payload = None
    try:
        payload = json.loads(output)
        if not isinstance(payload, dict) or result.returncode not in {0, 1, 2, 3}:
            raise tagged(ValueError(), "ssh.response_not_json")
    except (ValueError, RecursionError):
        reason = _classify_invalid_response(result.returncode, output, result.stderr, json_object=isinstance(payload, dict), remote_shell=report["worker"]["remote_shell"])
        return {**base, "status": "invalid_response", "exit_code": 1, "ssh_exit_code": result.returncode,
                "error": "Expected one ADAPT JSON object. Check remote ADAPT version/path, selected shell and stdout from login scripts; no local fallback ran.",
                "response_reason": reason, "hint": _RESPONSE_TEXT[reason]}
    return {**base, "status": "remote_completed" if result.returncode == 0 else "remote_reported_failure",
            "exit_code": result.returncode, "ssh_exit_code": result.returncode, "remote_report": payload}


def mcp_config(host: str, *, devices: list[str] | None = None, remote_shell: str = "posix",
               remote_adapt: str = "adapt", port: int | None = None, identity: str | None = None) -> dict[str, Any]:
    _validate_options(host, remote_shell, remote_adapt, port, identity)
    if devices is not None and (not isinstance(devices, list) or not all(isinstance(d, str) for d in devices)):
        raise tagged(SshWorkerError("MCP devices must be a list of exact device strings."), "ssh.option_invalid")
    remote_args = [remote_adapt, "--no-banner", "--no-tips", "--no-progress", "mcp", "--read-only"]
    for device in dict.fromkeys(devices or []):
        remote_args.extend(["--device", validate_target(device)])
    argv = _argv(host, remote_args, remote_shell=remote_shell, port=port, identity=identity)
    return {"schema_version": 1, "status": "configuration_only", "worker": {"host": host, "transport": "ssh", "remote_shell": remote_shell},
            "network_access": False, "configuration_written": False, "read_only": True,
            "client_config": {"mcpServers": {"adapt-worker": {"command": argv[0], "args": argv[1:]}}},
            "note": "The MCP client launches SSH directly. Rename adapt-worker for multiple workers; verify client-specific config syntax. No server was started."}


# One guide per fixed reason code; the reports' own `hint` text stays as it is.
_REASON_GUIDES = {
    "host_key_unknown": "ssh.host_key_unknown", "host_key_changed": "ssh.host_key_changed",
    "authentication_failed": "ssh.auth_failed", "name_resolution_failed": "ssh.name_resolution",
    "connection_refused": "ssh.connection_refused", "connection_timed_out": "ssh.connection_timed_out",
    "network_unreachable": "ssh.network_unreachable", "connection_closed": "ssh.connection_closed",
    "identity_file_problem": "ssh.identity_file", "unknown": "ssh.connection_failed",
}
_RESPONSE_GUIDES = {
    "remote_adapt_not_found": "ssh.remote_adapt_not_found", "remote_shell_mismatch": "ssh.remote_shell_mismatch",
    "not_json": "ssh.response_not_json", "empty_response": "ssh.response_empty", "unexpected_exit_code": "ssh.unexpected_exit_code",
}


def report_failure_id(report: dict[str, Any]) -> str | None:
    """Guide for a status or run report that is not a completed result."""
    status = report.get("status")
    if status == "missing_client":
        return "ssh.client_not_found"
    if status == "ssh_failed":
        return _REASON_GUIDES.get(report.get("ssh_failure_reason"), "ssh.exec_failed")
    if status == "timeout":
        return "ssh.timeout"
    if status == "invalid_response":
        return _RESPONSE_GUIDES.get(report.get("response_reason"), "ssh.response_too_large")
    if status == "remote_reported_failure":
        return "ssh.remote_reported_failure"
    return None


def _emit(fn, json_output: bool, **kwargs):
    context = FailureContext.from_runtime(command=current_command())
    try:
        report = fn(**kwargs)
        failure_id = report_failure_id(report)
    except ValueError as exc:
        report = {"status": "refused", "exit_code": 2, "error": str(exc)}
        failure_id = classify(exc)
    if json_output and failure_id:
        report = attach_failure(report, failure_id, context)
    if json_output:
        typer.echo(json.dumps(report, indent=2))
    else:
        typer.echo("ADAPT / SSH WORKER\n" + json.dumps(report, indent=2))
        if failure_id and report.get("exit_code", 0):
            emit_failure(failure_id, context)
        elif failure_id:
            emit_pointer(failure_id)  # e.g. ssh status without a client: exit code 0 is kept
    if report.get("exit_code", 0):
        raise typer.Exit(report["exit_code"])


@ssh_app.command("status")
def status_command(json_output: bool = typer.Option(False, "--json")):
    """Locate the local OpenSSH client without execution or network access."""
    _emit(status, json_output)


@ssh_app.command("plan")
def plan_command(host: str, operation_name: str = typer.Option("doctor", "--operation"), device: str | None = typer.Option(None, "--device", "-s"), transport: str = typer.Option("adb", "--transport"), remote_shell: str = typer.Option("posix", "--remote-shell"), remote_adapt: str = typer.Option("adapt", "--remote-adapt"), port: int | None = typer.Option(None, "--port"), identity: str | None = typer.Option(None, "--identity"), json_output: bool = typer.Option(False, "--json")):
    """Preview one remote operation offline; no SSH config or key reads."""
    _emit(plan, json_output, host=host, operation=operation_name, device=device, transport=transport, remote_shell=remote_shell, remote_adapt=remote_adapt, port=port, identity=identity)


@ssh_app.command("run")
def run_command(host: str, operation_name: str = typer.Option("doctor", "--operation"), device: str | None = typer.Option(None, "--device", "-s"), transport: str = typer.Option("adb", "--transport"), remote_shell: str = typer.Option("posix", "--remote-shell"), remote_adapt: str = typer.Option("adapt", "--remote-adapt"), port: int | None = typer.Option(None, "--port"), identity: str | None = typer.Option(None, "--identity"), timeout: int = typer.Option(180, "--timeout"), json_output: bool = typer.Option(False, "--json")):
    """Run an allowlisted read-only check on this exact SSH worker."""
    _emit(run, json_output, host=host, operation=operation_name, device=device, transport=transport, remote_shell=remote_shell, remote_adapt=remote_adapt, port=port, identity=identity, timeout=timeout)


@ssh_app.command("mcp-config")
def mcp_config_command(host: str, devices: list[str] | None = typer.Option(None, "--device", "-s"), remote_shell: str = typer.Option("posix", "--remote-shell"), remote_adapt: str = typer.Option("adapt", "--remote-adapt"), port: int | None = typer.Option(None, "--port"), identity: str | None = typer.Option(None, "--identity"), json_output: bool = typer.Option(False, "--json")):
    """Generate an observation-only MCP-over-SSH client entry without launching."""
    _emit(mcp_config, json_output, host=host, devices=devices, remote_shell=remote_shell, remote_adapt=remote_adapt, port=port, identity=identity)
