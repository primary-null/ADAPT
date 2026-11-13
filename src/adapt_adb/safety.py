"""Safety policy enforcement for ADAPT (Android Device Agent Provisioning Toolkit).

Enforces ADAPT hard guardrails before spawning `adb` commands:
- Never run `adb kill-server` (unless an explicit server/client version mismatch
  is being recovered with explicit opt-in).
- Never run `sudo adb` or elevate `adb` privileges.
- Never run `pm clear` or `uninstall` without an explicit confirmation flag.
- Always require an explicit target serial (`-s` / `--serial`) when more than
  one device is connected.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping, Sequence
from typing import Any

from adapt_adb.adb import has_serial_flag, split_adb_prefix

# Constant documenting arguments/subcommands that ADAPT forbids by default.
FORBIDDEN_ADB_ARGS: tuple[str, ...] = (
    "kill-server",
    "sudo",
    "pm clear",
    "uninstall",
)

# Regex matching standard adb client/server version mismatch output.
_VERSION_MISMATCH_RE = re.compile(
    r"adb server version \(\d+\) doesn't match this client \(\d+\)",
    re.IGNORECASE,
)


class AdaptError(ValueError):
    """Base exception for ADAPT errors."""


class SafetyError(AdaptError):
    """Raised when an ADB command or target violates ADAPT safety policy."""


class ForbiddenArgv(SafetyError):
    """Raised when an ADB invocation includes forbidden arguments."""


class NeedSerial(SafetyError):
    """Raised when multiple devices are present and no serial was specified."""


class VersionMismatch(SafetyError):
    """Raised when the running ADB server version mismatches the client."""


def never_kill_server() -> str:
    """Document and return ADAPT's policy forbidding `adb kill-server`.

    Standard `adb kill-server` tears down the shared host ADB daemon on
    localhost:5037, disrupting concurrent agents, IDEs, test runners, and
    device sessions. See `FORBIDDEN_ADB_ARGS`.
    """
    forbidden = ", ".join(FORBIDDEN_ADB_ARGS)
    return (
        "ADAPT never runs 'adb kill-server' during normal operation, doctor, or setup "
        "because it destroys the shared host ADB server on port 5037. "
        f"Forbidden ADB arguments: {forbidden}."
    )


def is_version_mismatch_stderr(stderr: str | None) -> bool:
    """Return True if stderr explicitly indicates an ADB server/client version mismatch."""
    if not stderr:
        return False
    return bool(_VERSION_MISMATCH_RE.search(stderr))


def _normalize_tokens(args: Sequence[str]) -> list[str]:
    """Flatten argument tokens, expanding single-string `shell` payloads for inspection."""
    tokens: list[str] = []
    for i, raw in enumerate(args):
        arg = str(raw).strip()
        if not arg:
            continue
        tokens.append(arg)
        # If this token follows `shell` and contains whitespace, also split it
        # so `adb shell "pm clear com.example"` is caught identically to
        # `adb shell pm clear com.example`.
        if i > 0 and str(args[i - 1]).strip() == "shell" and (" " in arg or "\t" in arg):
            try:
                subtokens = shlex.split(arg)
            except ValueError:
                subtokens = arg.split()
            for sub in subtokens:
                if sub not in tokens:
                    tokens.append(sub)
    return tokens


def _contains_unquoted_shell_syntax(payload: str) -> bool:
    """Return True for shell control syntax outside protective single quotes."""
    quote: str | None = None
    escaped = False
    for char in payload:
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote != "'":
            escaped = True
            continue
        if char == "'" and quote != '"':
            quote = None if quote == "'" else "'"
            continue
        if char == '"' and quote != "'":
            quote = None if quote == '"' else '"'
            continue
        if char in "\n\r":
            return True
        if quote != "'" and char in ";|&<>$`":
            return True
    return False


def inspect_simple_shell(payload: str, *, depth: int = 0) -> list[str]:
    """Parse the bounded shell grammar ADAPT can inspect honestly.

    Routine single commands and at most two nested ``sh -c`` wrappers are
    supported.  Redirections, separators, substitutions, and other shell
    interpreters are rejected because their effects cannot be established by
    token inspection.
    """
    if depth > 2 or _contains_unquoted_shell_syntax(payload):
        raise ForbiddenArgv("Complex shell syntax cannot be safety-checked")
    try:
        tokens = shlex.split(payload, posix=True)
    except ValueError:
        raise ForbiddenArgv("Malformed shell quoting") from None
    if len(tokens) == 3 and tokens[0] in {"sh", "ash", "mksh"} and tokens[1] == "-c":
        return inspect_simple_shell(tokens[2], depth=depth + 1)
    if tokens and tokens[0] in {"sh", "bash", "ash", "mksh", "eval", "source", "."}:
        raise ForbiddenArgv("Unsupported shell indirection")
    return tokens


def _parsed_command(args: Sequence[str]) -> tuple[list[str], list[str]]:
    """Return normalized ADB global prefix and opaque command arguments."""
    try:
        parsed = split_adb_prefix([str(a) for a in args])
    except ValueError as exc:
        raise SafetyError(str(exc)) from None
    return parsed.prefix, parsed.command


def check_no_sudo(args: Sequence[str]) -> None:
    """Forbid invoking `sudo` in or around an `adb` command."""
    for raw in args:
        token = str(raw).strip()
        if token == "sudo" or token.startswith("sudo "):
            raise ForbiddenArgv(
                "Running 'sudo adb' is forbidden: ADB must run as the current unprivileged user."
            )
    _, command = _parsed_command(args)
    if command and command[0] == "sudo":
        raise ForbiddenArgv(
            "Running 'sudo' via ADB is forbidden: ADB must run as the current unprivileged user."
        )


def check_no_kill_server(
    args: Sequence[str],
    *,
    allow_on_mismatch: bool = False,
    mismatch_stderr: str | None = None,
) -> None:
    """Forbid `adb kill-server` unless an explicit version mismatch is proven."""
    _, command = _parsed_command(args)
    if command and command[0] == "kill-server":
        if allow_on_mismatch and is_version_mismatch_stderr(mismatch_stderr):
            return
        raise ForbiddenArgv(
            "Forbidden argument 'kill-server': ADAPT never kills the shared ADB server "
            "unless recovering from an explicit server/client version mismatch."
        )


def check_no_destructive_package_ops(
    args: Sequence[str],
    *,
    allow_destructive: bool = False,
) -> None:
    """Forbid `pm clear` and `uninstall` unless `allow_destructive=True` is set."""
    if allow_destructive:
        return

    _, command = _parsed_command(args)
    if not command:
        return
    if command[0] == "uninstall":
        raise ForbiddenArgv(
            "Forbidden command 'uninstall': uninstalling packages requires an explicit confirmation flag."
        )
    if command[0] != "shell":
        return
    shell_tokens = inspect_simple_shell(" ".join(command[1:]))
    destructive = (
        len(shell_tokens) >= 2
        and shell_tokens[0] == "pm"
        and shell_tokens[1] in {"clear", "uninstall"}
    ) or (
        len(shell_tokens) >= 3
        and shell_tokens[:2] == ["cmd", "package"]
        and shell_tokens[2] in {"clear", "uninstall"}
    )
    if destructive:
        operation = "pm clear" if "clear" in shell_tokens[:3] else "uninstall"
        raise ForbiddenArgv(
            f"Forbidden command '{operation}': destructive package operations require an explicit confirmation flag."
        )


def _has_bounded_logcat_flag(tokens_after_logcat: Sequence[str]) -> bool:
    """Return True if `-d` or `-t` is present in the arguments following `logcat`."""
    for tok in tokens_after_logcat:
        if tok in ("-d", "-t"):
            return True
        if tok.startswith("-t") and len(tok) > 2 and tok[2:].isdigit():
            return True
    return False


def check_no_interactive_shell(args: Sequence[str]) -> None:
    """Refuse interactive `adb shell` when no command arguments follow `shell`."""
    _, command = _parsed_command(args)
    if command and command[0] == "shell":
        if not any(str(t).strip() for t in command[1:]):
            raise ForbiddenArgv(
                "Forbidden interactive 'shell': bare 'adb shell' hangs non-interactive sessions; "
                "pass a command to execute (e.g. 'shell getprop')."
            )


def check_no_unbounded_logcat(args: Sequence[str]) -> None:
    """Refuse `logcat` unless bounded with `-d` or `-t`."""
    _, command = _parsed_command(args)
    if command and command[0] == "logcat":
        if not _has_bounded_logcat_flag(command[1:]):
            raise ForbiddenArgv(
                "Forbidden streaming 'logcat': logcat requires '-d' or '-t' to prevent unbounded streaming."
            )
    if command and command[0] == "shell":
        shell_tokens = inspect_simple_shell(" ".join(command[1:]))
        if shell_tokens and shell_tokens[0] == "logcat" and not _has_bounded_logcat_flag(shell_tokens[1:]):
            raise ForbiddenArgv(
                "Forbidden streaming 'logcat': logcat requires '-d' or '-t' to prevent unbounded streaming."
            )


def validate_adb_args(
    args: Sequence[str],
    *,
    allow_destructive: bool = False,
    allow_kill_server_on_mismatch: bool = False,
    mismatch_stderr: str | None = None,
) -> list[str]:
    """Validate an ADB argv sequence against ADAPT's safety policy.

    Raises `ForbiddenArgv` if the arguments contain:
    - `sudo`
    - `kill-server` (unless `allow_kill_server_on_mismatch=True` and `mismatch_stderr` matches)
    - `pm clear` or `uninstall` (unless `allow_destructive=True`)
    - interactive `shell` with no following arguments
    - `logcat` without `-d` and without `-t`

    Returns a copy of `args` as a `list[str]` when valid.
    """
    arg_list = [str(a) for a in args]
    check_no_sudo(arg_list)
    check_no_kill_server(
        arg_list,
        allow_on_mismatch=allow_kill_server_on_mismatch,
        mismatch_stderr=mismatch_stderr,
    )
    check_no_destructive_package_ops(
        arg_list,
        allow_destructive=allow_destructive,
    )
    check_no_interactive_shell(arg_list)
    check_no_unbounded_logcat(arg_list)
    return arg_list


def _extract_serial_from_args(args: Sequence[str] | None) -> str | None:
    """Extract the target serial from `-s` or `--serial` in args if present."""
    if not args:
        return None
    try:
        return split_adb_prefix([str(a) for a in args]).serial
    except ValueError as exc:
        raise SafetyError(str(exc)) from None


def _device_serial_and_label(dev: Any) -> tuple[str, str]:
    """Extract (serial, human_label) from a device dict, string, or object."""
    if isinstance(dev, str):
        return dev, dev
    if isinstance(dev, Mapping):
        serial = str(dev.get("serial", "unknown"))
        model = dev.get("model")
        state = dev.get("state")
        if model and state:
            return serial, f"{serial} ({model}, {state})"
        if model:
            return serial, f"{serial} ({model})"
        if state:
            return serial, f"{serial} ({state})"
        return serial, serial
    serial = str(getattr(dev, "serial", dev))
    model = getattr(dev, "model", None)
    if model:
        return serial, f"{serial} ({model})"
    return serial, serial


def require_serial_if_many(
    devices: Sequence[Any],
    serial: str | None = None,
    *,
    args: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Require an explicit device serial when more than one device is connected.

    Args:
        devices: Connected device entries (dicts with 'serial'/'state'/'model' or serial strings).
        serial: Explicit target serial if provided via CLI/API.
        args: Optional ADB argv list to inspect for `-s` / `--serial`.
        env: Optional environment mapping to inspect for `ANDROID_SERIAL`.

    Returns:
        The resolved target serial string, or `None` when 0 devices are present
        and no explicit serial was given.

    Raises:
        NeedSerial: When `len(devices) > 1` and no serial is specified.
    """
    explicit_serial = serial.strip() if isinstance(serial, str) and serial.strip() else None

    if explicit_serial is None and args is not None:
        from_args = _extract_serial_from_args(args)
        if from_args:
            explicit_serial = from_args

    if explicit_serial is None and env is not None:
        env_serial = env.get("ANDROID_SERIAL", "").strip()
        if env_serial:
            explicit_serial = env_serial

    if explicit_serial:
        return explicit_serial

    device_list = list(devices)
    if len(device_list) == 0:
        return None

    if len(device_list) == 1:
        only_serial, _ = _device_serial_and_label(device_list[0])
        return only_serial

    labels = [_device_serial_and_label(d)[1] for d in device_list]
    joined = ", ".join(labels)
    raise NeedSerial(
        f"Multiple devices connected ({len(device_list)}): {joined}. "
        "Specify a target device with -s / --serial."
    )


__all__ = [
    "FORBIDDEN_ADB_ARGS",
    "AdaptError",
    "SafetyError",
    "ForbiddenArgv",
    "NeedSerial",
    "VersionMismatch",
    "never_kill_server",
    "is_version_mismatch_stderr",
    "check_no_sudo",
    "check_no_kill_server",
    "check_no_destructive_package_ops",
    "inspect_simple_shell",
    "validate_adb_args",
    "require_serial_if_many",
]
