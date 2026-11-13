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
    return {"schema_version": 1, "status": "located_unverified" if binary else "missing_client",
            "binary": binary, "network_access": False,
            "note": "PATH/override observation only. No SSH configuration, keys or hosts were read or contacted.",
            "setup": "Install the OpenSSH client through Windows Optional Features or Debian's openssh-client package. Configure a verified host/key separately."}


def _validate_options(host: str, remote_shell: str, remote_adapt: str, port: int | None, identity: str | None):
    if not isinstance(host, str) or len(host) > 320 or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@:\[\]-]*", host) or host.count("@") > 1 or host.endswith("@"):
        raise SshWorkerError("Use an SSH host alias, hostname or IP, optionally user@host; no URLs, whitespace or command options.")
    if remote_shell not in {"posix", "powershell"}:
        raise SshWorkerError("Remote shell must be posix or powershell.")
    if not isinstance(remote_adapt, str) or not remote_adapt or len(remote_adapt) > 2048 or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in remote_adapt):
        raise SshWorkerError("Choose adapt or an absolute path to the remote adapt executable.")
    path = PureWindowsPath(remote_adapt) if remote_shell == "powershell" else PurePosixPath(remote_adapt)
    if path.name.lower() not in {"adapt", "adapt.exe"} or not (remote_adapt.lower() in {"adapt", "adapt.exe"} or path.is_absolute()):
        raise SshWorkerError("Remote executable must be adapt/adapt.exe or its absolute path, not a shell command.")
    if port is not None and (type(port) is not int or not 1 <= port <= 65535):
        raise SshWorkerError("SSH port must be between 1 and 65535.")
    if identity is not None and (not identity or len(identity) > 4096 or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in identity)):
        raise SshWorkerError("Identity must be a local key-file path without control characters.")


def _remote_command(args: list[str], remote_shell: str) -> str:
    if remote_shell == "posix":
        return shlex.join(args)
    # OpenSSH sends a shell command string, not a remote argv array. Encode the
    # PowerShell script so the Windows default cmd shell sees no user syntax.
    # Embedded double quotes in native arguments are unsupported by legacy
    # Windows PowerShell; refuse them rather than silently changing a target.
    if any('"' in arg for arg in args):
        raise SshWorkerError("PowerShell worker arguments cannot contain embedded double quotes; select a different exact transport identifier.")
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
        raise SshWorkerError("Choose a supported read-only operation and adb/fastboot/all transport.")
    if operation in DEVICE_OPERATIONS:
        if device is None:
            raise SshWorkerError("This operation requires an exact --device on the remote worker.")
        validate_target(device)
    elif device is not None:
        raise SshWorkerError("This host/list operation is not device-scoped. Choose inspect, android, desktop or dex for --device.")
    if operation not in {"scan", "inspect"} and transport != "adb" or operation == "inspect" and transport == "all":
        raise SshWorkerError("Only scan supports all transports; fastboot is available for scan and inspect only.")
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


@operation("Inspecting SSH worker")
def run(host: str, *, timeout: int = 180, **kwargs) -> dict[str, Any]:
    report = plan(host, **kwargs)
    if type(timeout) is not int or not 1 <= timeout <= 300:
        raise SshWorkerError("SSH operation timeout must be between 1 and 300 seconds.")
    binary = resolve_ssh()
    if binary is None:
        raise SshWorkerError("OpenSSH client not found. Run adapt ssh status for setup guidance.")
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
        return {**base, "status": "ssh_failed", "exit_code": 1, "ssh_exit_code": 255,
                "error": "Verify connectivity, existing host-key trust, key/agent authentication and worker configuration. No host keys were automatically accepted."}
    output = result.stdout or ""
    if len(output.encode("utf-8")) > MAX_RESPONSE_BYTES:
        return {**base, "status": "invalid_response", "exit_code": 1, "error": "Worker JSON response exceeded the accepted 4 MiB limit."}
    try:
        payload = json.loads(output)
        if not isinstance(payload, dict) or result.returncode not in {0, 1, 2, 3}:
            raise ValueError()
    except (ValueError, RecursionError):
        return {**base, "status": "invalid_response", "exit_code": 1, "ssh_exit_code": result.returncode,
                "error": "Expected one ADAPT JSON object. Check remote ADAPT version/path, selected shell and stdout from login scripts; no local fallback ran."}
    return {**base, "status": "remote_completed" if result.returncode == 0 else "remote_reported_failure",
            "exit_code": result.returncode, "ssh_exit_code": result.returncode, "remote_report": payload}


def mcp_config(host: str, *, devices: list[str] | None = None, remote_shell: str = "posix",
               remote_adapt: str = "adapt", port: int | None = None, identity: str | None = None) -> dict[str, Any]:
    _validate_options(host, remote_shell, remote_adapt, port, identity)
    if devices is not None and (not isinstance(devices, list) or not all(isinstance(d, str) for d in devices)):
        raise SshWorkerError("MCP devices must be a list of exact device strings.")
    remote_args = [remote_adapt, "--no-banner", "--no-tips", "--no-progress", "mcp", "--read-only"]
    for device in dict.fromkeys(devices or []):
        remote_args.extend(["--device", validate_target(device)])
    argv = _argv(host, remote_args, remote_shell=remote_shell, port=port, identity=identity)
    return {"schema_version": 1, "status": "configuration_only", "worker": {"host": host, "transport": "ssh", "remote_shell": remote_shell},
            "network_access": False, "configuration_written": False, "read_only": True,
            "client_config": {"mcpServers": {"adapt-worker": {"command": argv[0], "args": argv[1:]}}},
            "note": "The MCP client launches SSH directly. Rename adapt-worker for multiple workers; verify client-specific config syntax. No server was started."}


def _emit(fn, json_output: bool, **kwargs):
    try:
        report = fn(**kwargs)
    except ValueError as exc:
        report = {"status": "refused", "exit_code": 2, "error": str(exc)}
    if json_output:
        typer.echo(json.dumps(report, indent=2))
    else:
        typer.echo("ADAPT / SSH WORKER\n" + json.dumps(report, indent=2))
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
