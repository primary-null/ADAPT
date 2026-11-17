"""Safety policy enforcement for ADAPT (Android Device Agent Provisioning Toolkit).

Enforces ADAPT hard guardrails before spawning `adb` commands:
- Never run `adb kill-server`, under any circumstances.
- Never run `sudo adb` or elevate `adb` privileges.
- Never run `pm clear` or `uninstall` through the guarded passthrough; deliberate
  destructive work is done with plain adb outside ADAPT.
- Only run device commands (`shell`, `exec-out`, `exec-in`) that token
  inspection can establish: shell control syntax, shell indirection and
  command wrappers that would hide the real command are refused.
- Always require an explicit target serial (`-s` / `--serial`) when more than
  one device is connected.
"""

from __future__ import annotations

import posixpath
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

# adb subcommands whose operands are executed by the device shell.
_REMOTE_EXEC_COMMANDS = frozenset({"shell", "exec-out", "exec-in"})

# `adb shell` flag options (adb's getopt string is "+e:ntTx"; `-e` takes a value).
_ADB_SHELL_FLAG_OPTIONS = frozenset("ntTx")

# The command word ADAPT inspects must be a plain name or path: no quoting,
# escapes, globbing, brace expansion, subshell parentheses, `!` negation,
# tilde expansion, comments, or `NAME=value` assignment prefixes.
_PLAIN_COMMAND_WORD_RE = re.compile(r"[A-Za-z0-9_./:+@%-]+")

# Interpreters and builtins that evaluate their arguments as shell code, or as
# arithmetic that can expand a single-quoted `$(...)`.
_SHELL_INDIRECTIONS = frozenset(
    {
        "sh", "bash", "ash", "mksh", "dash", "zsh", "ksh", "csh", "tcsh",
        "eval", "source", ".", "trap", "let", "typeset", "declare", "local",
        "export", "readonly", "integer", "alias", "fc", "read", "unset",
    }
)

# Programs and builtins that run another command named in their arguments,
# hiding it from token inspection.
_COMMAND_WRAPPERS = frozenset(
    {
        "env", "exec", "command", "builtin", "nohup", "time", "nice", "ionice",
        "xargs", "su", "sudo", "toybox", "toolbox", "busybox", "timeout",
        "chroot", "taskset", "setsid", "chrt", "stdbuf", "watch", "strace",
        "ltrace", "nsenter", "unshare", "flock", "run-as", "runcon",
        "logwrapper",
    }
)

# Wrappers with a fixed argument layout: the wrapped command is inspected
# instead of refused, so `run-as PKG ls`, `timeout 5 ping ...` and `su 0 id`
# keep working while `run-as PKG pm clear PKG` is still caught.
_TIMEOUT_DURATION_RE = re.compile(r"\d+(?:\.\d+)?[smhd]?")
_SU_DIRECT_USER_RE = re.compile(r"(?:root|shell|\d{1,7})")

# `find` actions that execute a command for each match.
_FIND_EXEC_ACTIONS = frozenset({"-exec", "-execdir", "-ok", "-okdir"})

# Binder services whose raw `service call` transactions can delete packages or
# clear application data (IPackageManager / IActivityManager).
_RAW_BINDER_PACKAGE_SERVICES = frozenset({"package", "package_native", "activity"})

# `pm` / `cmd package` subcommands that destroy installed apps or app data.
_DESTRUCTIVE_PACKAGE_SUBCOMMANDS = frozenset(
    {"clear", "uninstall", "uninstall-system-updates"}
)


class AdaptError(ValueError):
    """Base exception for ADAPT errors."""


class SafetyError(AdaptError):
    """Raised when an ADB command or target violates ADAPT safety policy."""


class ForbiddenArgv(SafetyError):
    """Raised when an ADB invocation includes forbidden arguments."""


class NeedSerial(SafetyError):
    """Raised when multiple devices are present and no serial was specified."""

    failure_id = "device.multiple"  # failure guidance class default


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


def _command_name(word: str) -> str:
    """Return the comparable name of a command word (`/system/bin/PM` -> `pm`)."""
    return posixpath.basename(word).lower()


def _inspect_wrapped_command(name: str, tokens: Sequence[str], depth: int) -> list[str] | None:
    """Inspect the command inside a wrapper whose argument layout is fixed.

    Returns the inspected inner tokens for `run-as PKG [--user N] CMD...`,
    `timeout DURATION CMD...`, `su USER CMD...` and `su -c 'CMD'`, or ``None``
    for any other spelling, which the caller then refuses as an opaque wrapper.
    """
    if name == "su" and len(tokens) == 3 and tokens[1] == "-c":
        # The single string is parsed by a device shell, exactly like `sh -c`.
        return inspect_simple_shell(tokens[2], depth=depth + 1)
    inner: list[str] | None = None
    if name == "run-as" and len(tokens) >= 3 and not tokens[1].startswith("-"):
        inner = list(tokens[2:])
        if len(inner) >= 3 and inner[0] == "--user" and inner[1].isdigit():
            inner = inner[2:]
    elif name == "timeout" and len(tokens) >= 3 and _TIMEOUT_DURATION_RE.fullmatch(tokens[1]):
        inner = list(tokens[2:])
    elif name == "su" and len(tokens) >= 3 and _SU_DIRECT_USER_RE.fullmatch(tokens[1]):
        inner = list(tokens[2:])
    if inner is None:
        return None
    if inner[0].startswith("-"):
        # An option here would be read by the wrapper (for example `su 0 -c ...`).
        raise ForbiddenArgv(
            f"Unsupported command wrapper '{name}': options before the wrapped command "
            "cannot be safety-checked"
        )
    # These wrappers exec the remaining words directly, so each word is one literal argument.
    return inspect_simple_shell(shlex.join(inner), depth=depth + 1)


def inspect_simple_shell(payload: str, *, depth: int = 0) -> list[str]:
    """Parse the bounded shell grammar ADAPT can inspect honestly.

    Routine single commands and at most two nested ``sh -c`` wrappers are
    supported.  Redirections, separators, substitutions, other shell
    interpreters, evaluating builtins, and command wrappers (``env``,
    ``exec``, ``xargs``, ``toybox``, ``find -exec``...) are rejected because
    their effects cannot be established by token inspection.  The command
    word must be a plain name or path; the returned tokens carry it
    normalized to its lower-case basename so callers can compare it directly.
    """
    if depth > 2 or _contains_unquoted_shell_syntax(payload):
        raise ForbiddenArgv("Complex shell syntax cannot be safety-checked")
    try:
        tokens = shlex.split(payload, posix=True)
    except ValueError:
        raise ForbiddenArgv("Malformed shell quoting") from None
    if not tokens:
        return tokens
    raw_word = re.match(r"[ \t]*([^ \t]*)", payload)
    if raw_word is None or not _PLAIN_COMMAND_WORD_RE.fullmatch(raw_word.group(1)):
        raise ForbiddenArgv(
            "Complex shell syntax cannot be safety-checked: the command word must be "
            "a plain name or path"
        )
    name = _command_name(tokens[0])
    if len(tokens) == 3 and name in {"sh", "ash", "mksh"} and tokens[1] == "-c":
        return inspect_simple_shell(tokens[2], depth=depth + 1)
    if name in _SHELL_INDIRECTIONS:
        raise ForbiddenArgv("Unsupported shell indirection")
    if name in _COMMAND_WRAPPERS:
        wrapped = _inspect_wrapped_command(name, tokens, depth)
        if wrapped is not None:
            return wrapped
        raise ForbiddenArgv(
            f"Unsupported command wrapper '{name}': the wrapped command cannot be safety-checked"
        )
    if name == "find" and any(tok in _FIND_EXEC_ACTIONS for tok in tokens[1:]):
        raise ForbiddenArgv(
            "Unsupported command wrapper 'find -exec': the executed command cannot be safety-checked"
        )
    if (
        name == "service"
        and len(tokens) >= 3
        and tokens[1].lower() == "call"
        and tokens[2].lower() in _RAW_BINDER_PACKAGE_SERVICES
    ):
        raise ForbiddenArgv(
            "Raw binder transactions to package/activity services cannot be safety-checked"
        )
    if name == "test" and any("$" in tok or "`" in tok for tok in tokens[1:]):
        raise ForbiddenArgv("Unsupported shell indirection")
    return [name, *tokens[1:]]


def _parsed_command(args: Sequence[str]) -> tuple[list[str], list[str]]:
    """Return normalized ADB global prefix and opaque command arguments."""
    try:
        parsed = split_adb_prefix([str(a) for a in args])
    except ValueError as exc:
        raise SafetyError(str(exc)) from None
    return parsed.prefix, parsed.command


def remote_command_args(args: Sequence[str]) -> tuple[str, list[str]] | None:
    """Return the adb subcommand and the argv it executes on the device.

    Returns ``None`` unless the command is `shell`, `exec-out` or `exec-in`.
    For `shell`, adb's own leading options (`-n`, `-t`, `-tt`, `-T`, `-x`,
    `-e ESCAPE`, combined forms such as `-nT`, and `--`) are consumed the way
    adb's getopt does.  `exec-out` and `exec-in` take no options.  Raises
    `ForbiddenArgv` on an unrecognised leading option.
    """
    _, command = _parsed_command(args)
    if not command or command[0] not in _REMOTE_EXEC_COMMANDS:
        return None
    name = command[0]
    rest = [str(a) for a in command[1:]]
    if name != "shell":
        if rest and rest[0].strip().startswith("-"):
            raise ForbiddenArgv(f"Unsupported adb {name} option: {rest[0].strip()}")
        return name, rest
    i = 0
    while i < len(rest) and rest[i].startswith("-") and rest[i] != "-":
        option = rest[i]
        i += 1
        if option == "--":
            break
        for j, flag in enumerate(option[1:], start=1):
            if flag == "e":
                if j + 1 == len(option):
                    if i >= len(rest):
                        raise ForbiddenArgv("Missing value for adb shell option -e")
                    i += 1
                break
            if flag not in _ADB_SHELL_FLAG_OPTIONS:
                raise ForbiddenArgv(f"Unsupported adb shell option: {option}")
    return name, rest[i:]


def _inspect_remote_command(args: Sequence[str]) -> list[str] | None:
    """Return inspected device-side tokens for remote execution commands, else ``None``."""
    remote = remote_command_args(args)
    if remote is None:
        return None
    return inspect_simple_shell(" ".join(remote[1]))


def _skip_leading_flags(tokens: Sequence[str]) -> list[str]:
    """Drop leading `-flag` tokens (e.g. the `-w` in `cmd -w package ...`)."""
    i = 0
    while i < len(tokens) and tokens[i].startswith("-"):
        i += 1
    return list(tokens[i:])


def _destructive_package_subcommand(tokens: Sequence[str]) -> str | None:
    """Return the destructive `pm` / `cmd package` subcommand in inspected tokens, if any."""
    if not tokens:
        return None
    rest = list(tokens[1:])
    if tokens[0] == "cmd":
        rest = _skip_leading_flags(rest)
        if not rest or rest[0].lower() != "package":
            return None
        rest = rest[1:]
    elif tokens[0] != "pm":
        return None
    rest = _skip_leading_flags(rest)
    if rest and rest[0].lower() in _DESTRUCTIVE_PACKAGE_SUBCOMMANDS:
        return rest[0].lower()
    return None


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


def check_no_kill_server(args: Sequence[str]) -> None:
    """Forbid `adb kill-server` unconditionally."""
    _, command = _parsed_command(args)
    if command and command[0] == "kill-server":
        raise ForbiddenArgv(
            "Forbidden argument 'kill-server': ADAPT never kills the shared ADB server."
        )


def check_no_destructive_package_ops(
    args: Sequence[str],
    *,
    allow_destructive: bool = False,
) -> None:
    """Forbid `pm clear` and `uninstall` unless `allow_destructive=True` is set.

    `allow_destructive` is an internal parameter: no CLI option or MCP tool passes it.

    Device commands sent through `shell`, `exec-out` or `exec-in` are inspected
    with `inspect_simple_shell`, so absolute paths (`/system/bin/pm`), adb
    shell options, wrappers and `sh -c` nesting cannot hide `pm clear`,
    `pm uninstall`, `pm uninstall-system-updates` or their `cmd package` forms.
    """
    if allow_destructive:
        return

    _, command = _parsed_command(args)
    if not command:
        return
    if command[0] == "uninstall":
        raise ForbiddenArgv(
            "Forbidden command 'uninstall': ADAPT's guarded adb passthrough does not run this "
            "operation. If you intend it, run it with plain adb outside ADAPT."
        )
    shell_tokens = _inspect_remote_command(args)
    if shell_tokens is None:
        return
    subcommand = _destructive_package_subcommand(shell_tokens)
    if subcommand is not None:
        operation = "pm clear" if subcommand == "clear" else "uninstall"
        raise ForbiddenArgv(
            f"Forbidden command '{operation}': ADAPT's guarded adb passthrough does not run this "
            "operation. If you intend it, run it with plain adb outside ADAPT."
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
    """Refuse `adb shell` / `exec-out` / `exec-in` when no device command follows."""
    remote = remote_command_args(args)
    if remote is None:
        return
    name, remote_args = remote
    if not any(str(t).strip() for t in remote_args):
        raise ForbiddenArgv(
            f"Forbidden interactive '{name}': bare 'adb {name}' hangs non-interactive sessions; "
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
    shell_tokens = _inspect_remote_command(args)
    if shell_tokens is not None:
        if shell_tokens and shell_tokens[0] == "logcat" and not _has_bounded_logcat_flag(shell_tokens[1:]):
            raise ForbiddenArgv(
                "Forbidden streaming 'logcat': logcat requires '-d' or '-t' to prevent unbounded streaming."
            )


def validate_adb_args(
    args: Sequence[str],
    *,
    allow_destructive: bool = False,
) -> list[str]:
    """Validate an ADB argv sequence against ADAPT's safety policy.

    Raises `ForbiddenArgv` if the arguments contain:
    - `sudo`
    - `kill-server` (always)
    - `pm clear` or `uninstall` (unless the internal `allow_destructive=True` is passed)
    - a device command (`shell`, `exec-out`, `exec-in`) that cannot be
      safety-checked: shell control syntax, indirection or command wrappers
    - interactive `shell` / `exec-out` / `exec-in` with no device command
    - `logcat` without `-d` and without `-t`

    Returns a copy of `args` as a `list[str]` when valid.
    """
    arg_list = [str(a) for a in args]
    check_no_sudo(arg_list)
    check_no_kill_server(arg_list)
    check_no_destructive_package_ops(
        arg_list,
        allow_destructive=allow_destructive,
    )
    check_no_interactive_shell(arg_list)
    check_no_unbounded_logcat(arg_list)
    return arg_list


# ---------------------------------------------------------------------------
# Agent tool argv shapes (MCP tools for the build-test loop)
#
# The guard above is a denylist for the general passthrough. The MCP tools that launch,
# stop, inspect and test an app run only the exact device-command shapes listed here: every
# value that reaches `adb shell` first passes a strict pattern, so no value can carry a
# shell operator, a leading `-`, a control character or another command.
# ---------------------------------------------------------------------------

_PACKAGE_PATTERN = r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+"
_CLASS_PATTERN = r"\.?[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"
_QUALIFIED_CLASS_PATTERN = r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"
_METHOD_PATTERN = r"[A-Za-z_][A-Za-z0-9_]*"
# Characters that are safe inside a single-quoted shell word and in a URI (RFC 3986 minus
# quotes, backslash, backtick, `$`, `<`, `>`, `|`, `^`, braces and whitespace).
_URI_BODY_PATTERN = r"[A-Za-z0-9\-._~:/?#\[\]@!&()*+,;=%]"

PACKAGE_NAME_RE = re.compile(_PACKAGE_PATTERN)
CLASS_NAME_RE = re.compile(_CLASS_PATTERN)
QUALIFIED_CLASS_RE = re.compile(_QUALIFIED_CLASS_PATTERN)
METHOD_NAME_RE = re.compile(_METHOD_PATTERN)
COMPONENT_RE = re.compile(f"({_PACKAGE_PATTERN})/({_CLASS_PATTERN})")
LOG_MARK_RE = re.compile(r"\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}")
URI_SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]{0,31}")
MAX_URI_LENGTH = 2000
# Schemes that carry code or a nested intent: the VIEW action must not be a way around the guard.
REFUSED_URI_SCHEMES = frozenset({"javascript", "vbscript", "data", "intent"})
# Android's runtime ("dangerous") permissions that `pm grant` and `pm revoke` can change for an
# app. Special-access permissions (overlay, write settings, all-files access, install packages)
# are app-ops, not runtime permissions, and are not listed.
RUNTIME_PERMISSIONS = frozenset(
    f"android.permission.{name}"
    for name in (
        "READ_CALENDAR", "WRITE_CALENDAR", "READ_CALL_LOG", "WRITE_CALL_LOG", "PROCESS_OUTGOING_CALLS",
        "CAMERA", "READ_CONTACTS", "WRITE_CONTACTS", "GET_ACCOUNTS", "ACCESS_FINE_LOCATION",
        "ACCESS_COARSE_LOCATION", "ACCESS_BACKGROUND_LOCATION", "ACCESS_MEDIA_LOCATION", "RECORD_AUDIO",
        "READ_PHONE_STATE", "READ_PHONE_NUMBERS", "CALL_PHONE", "ANSWER_PHONE_CALLS", "ADD_VOICEMAIL",
        "USE_SIP", "ACCEPT_HANDOVER", "BODY_SENSORS", "BODY_SENSORS_BACKGROUND", "ACTIVITY_RECOGNITION",
        "SEND_SMS", "RECEIVE_SMS", "READ_SMS", "RECEIVE_WAP_PUSH", "RECEIVE_MMS", "READ_CELL_BROADCASTS",
        "READ_EXTERNAL_STORAGE", "WRITE_EXTERNAL_STORAGE", "READ_MEDIA_IMAGES", "READ_MEDIA_VIDEO",
        "READ_MEDIA_AUDIO", "READ_MEDIA_VISUAL_USER_SELECTED", "BLUETOOTH_CONNECT", "BLUETOOTH_SCAN",
        "BLUETOOTH_ADVERTISE", "NEARBY_WIFI_DEVICES", "UWB_RANGING", "POST_NOTIFICATIONS",
    )
) | {"com.android.voicemail.permission.ADD_VOICEMAIL"}
_HEALTH_PERMISSION_RE = re.compile(r"android\.permission\.health\.[A-Z][A-Z0-9_]{0,80}")


def _plain(value: Any, pattern: re.Pattern[str], limit: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit and pattern.fullmatch(value) is not None


def is_package_name(value: Any) -> bool:
    """An exact Android application ID such as ``com.example.app``."""
    return _plain(value, PACKAGE_NAME_RE, 255)


def is_class_name(value: Any) -> bool:
    """A Java/Kotlin class name, fully qualified or starting with ``.``; no ``$`` (a shell character)."""
    return _plain(value, CLASS_NAME_RE, 255)


def is_qualified_class_name(value: Any) -> bool:
    """A class name without a leading ``.`` (a test class named for `am instrument -e class`)."""
    return _plain(value, QUALIFIED_CLASS_RE, 255)


def is_method_name(value: Any) -> bool:
    return _plain(value, METHOD_NAME_RE, 128)


def is_component_name(value: Any) -> bool:
    """``package/class`` as `am start -n` and `am instrument` take it."""
    return _plain(value, COMPONENT_RE, 512)


def is_log_mark(value: Any) -> bool:
    """A device log time in logcat's ``MM-DD HH:MM:SS.mmm`` form, as `log_mark` returns it."""
    return _plain(value, LOG_MARK_RE, 32)


def is_runtime_permission(value: Any, package: str | None = None) -> bool:
    """A runtime permission: a known Android one, a health permission, or one the app itself defines."""
    if not isinstance(value, str) or not 0 < len(value) <= 255:
        return False
    if value in RUNTIME_PERMISSIONS or _HEALTH_PERMISSION_RE.fullmatch(value):
        return True
    return bool(
        package and is_package_name(package) and value.startswith(package + ".")
        and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", value)
    )


def is_safe_uri(value: Any) -> bool:
    """A bounded ASCII URI with a scheme, no whitespace or control characters and no shell characters."""
    if not isinstance(value, str) or not 0 < len(value) <= MAX_URI_LENGTH:
        return False
    scheme, separator, rest = value.partition(":")
    if not separator or not rest or not URI_SCHEME_RE.fullmatch(scheme) or scheme.lower() in REFUSED_URI_SCHEMES:
        return False
    return re.fullmatch(f"{_URI_BODY_PATTERN}+", value) is not None


def quote_safe_uri(value: str) -> str:
    """The device-shell word for an `is_safe_uri` value: single-quoted so `&`, `;` and `?` stay literal."""
    return f"'{value}'"


_URI_WORD = f"'{_URI_BODY_PATTERN}{{1,{MAX_URI_LENGTH}}}'"
_COORD = r"[0-9]{1,5}"
_INSTRUMENT = ("shell", "am", "instrument", "-w", "-r")
_START_VIEW = ("shell", "am", "start", "-a", "android.intent.action.VIEW", "-d")
_RESOLVE = (
    "shell", "cmd", "package", "resolve-activity", "--brief", "-a", "android.intent.action.MAIN",
    "-c", "android.intent.category.LAUNCHER",
)


def _token(spec: str | re.Pattern[str]) -> re.Pattern[str]:
    return spec if isinstance(spec, re.Pattern) else re.compile(re.escape(spec))


def _shape(*tokens: str | re.Pattern[str]) -> tuple[re.Pattern[str], ...]:
    return tuple(_token(t) for t in tokens)


_PKG = re.compile(_PACKAGE_PATTERN)
_COMPONENT = re.compile(f"{_PACKAGE_PATTERN}/{_CLASS_PATTERN}")
_PERMISSION_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+")

# name -> the exact tokens after `adb [-s SERIAL]`. A token that is a compiled pattern must match in full.
AGENT_SHELL_SHAPES: dict[str, tuple[re.Pattern[str], ...]] = {
    "launcher_resolve": _shape(*_RESOLVE, _PKG),
    "app_start": _shape("shell", "am", "start", "-n", _COMPONENT),
    "app_start_wait": _shape("shell", "am", "start", "-W", "-n", _COMPONENT),
    "app_force_stop": _shape("shell", "am", "force-stop", _PKG),
    "app_pid": _shape("shell", "pidof", _PKG),
    "activity_resumed": _shape("shell", "dumpsys", "activity", "activities"),
    "activity_exit_info": _shape("shell", "dumpsys", "activity", "exit-info", _PKG),
    "device_sdk": _shape("shell", "getprop", "ro.build.version.sdk"),
    "device_time": _shape("shell", "date", "+%m-%d_%H:%M:%S.%N"),
    "open_link": _shape(*_START_VIEW, re.compile(_URI_WORD)),
    "open_link_package": _shape(*_START_VIEW, re.compile(_URI_WORD), "-p", _PKG),
    "instrument": _shape(*_INSTRUMENT, _COMPONENT),
    "instrument_class": _shape(*_INSTRUMENT, "-e", "class", re.compile(_QUALIFIED_CLASS_PATTERN), _COMPONENT),
    "instrument_method": _shape(
        *_INSTRUMENT, "-e", "class",
        re.compile(rf"{_QUALIFIED_CLASS_PATTERN}#{_METHOD_PATTERN}"), _COMPONENT,
    ),
    "permission_grant": _shape("shell", "pm", "grant", _PKG, _PERMISSION_WORD),
    "permission_revoke": _shape("shell", "pm", "revoke", _PKG, _PERMISSION_WORD),
    "input_tap": _shape("shell", "input", "tap", re.compile(_COORD), re.compile(_COORD)),
}


def match_agent_shell_shape(args: Sequence[str]) -> str | None:
    """Name of the allowlisted agent shape these adb arguments match exactly, else ``None``.

    The global `-s SERIAL` prefix is ignored; the remaining tokens must equal a listed shape
    token for token, so an extra, missing or reordered token is not a match.
    """
    try:
        _, command = _parsed_command([str(a) for a in args])
    except SafetyError:
        return None
    for name, shape in AGENT_SHELL_SHAPES.items():
        if len(command) == len(shape) and all(p.fullmatch(t) for p, t in zip(shape, command)):
            return name
    return None


def match_agent_logcat(args: Sequence[str]) -> bool:
    """True for the bounded, time-windowed `logcat -d -T MARK` forms (the mark must be a `log_mark`)."""
    try:
        _, command = _parsed_command([str(a) for a in args])
    except SafetyError:
        return False
    if not command or command[0] != "logcat" or "-d" not in command:
        return False
    if "-T" not in command:
        return True
    at = command.index("-T")
    return at + 1 < len(command) and is_log_mark(command[at + 1])


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
    "remote_command_args",
    "AGENT_SHELL_SHAPES",
    "RUNTIME_PERMISSIONS",
    "match_agent_shell_shape",
    "match_agent_logcat",
    "is_package_name",
    "is_class_name",
    "is_qualified_class_name",
    "is_method_name",
    "is_component_name",
    "is_log_mark",
    "is_runtime_permission",
    "is_safe_uri",
    "quote_safe_uri",
    "validate_adb_args",
    "require_serial_if_many",
]
