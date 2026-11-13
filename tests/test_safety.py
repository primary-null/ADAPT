"""Unit tests for adapt_adb.safety (no real adb calls, placeholder serials only)."""

import pytest

from adapt_adb.safety import (
    FORBIDDEN_ADB_ARGS,
    ForbiddenArgv,
    NeedSerial,
    SafetyError,
    check_no_destructive_package_ops,
    check_no_kill_server,
    check_no_sudo,
    is_version_mismatch_stderr,
    never_kill_server,
    require_serial_if_many,
    validate_adb_args,
)


def test_forbidden_adb_args_constant_and_never_kill_server():
    """Verify FORBIDDEN_ADB_ARGS constant and never_kill_server() documentation."""
    assert "kill-server" in FORBIDDEN_ADB_ARGS
    assert "sudo" in FORBIDDEN_ADB_ARGS
    assert "pm clear" in FORBIDDEN_ADB_ARGS
    assert "uninstall" in FORBIDDEN_ADB_ARGS

    doc_text = never_kill_server()
    assert "kill-server" in doc_text
    assert "5037" in doc_text
    assert never_kill_server.__doc__ is not None
    assert "FORBIDDEN_ADB_ARGS" in never_kill_server.__doc__


def test_forbid_kill_server():
    """Verify `adb kill-server` is blocked unconditionally, even after a version mismatch."""
    with pytest.raises(ForbiddenArgv, match="kill-server"):
        validate_adb_args(["kill-server"])

    with pytest.raises(SafetyError):
        check_no_kill_server(["-s", "placeholder-001", "kill-server"])

    # The former version-mismatch exception is gone: the opt-in parameters no
    # longer exist, and a genuine mismatch message does not unlock kill-server.
    mismatch_msg = "adb server version (39) doesn't match this client (41); killing..."
    assert is_version_mismatch_stderr(mismatch_msg) is True
    assert is_version_mismatch_stderr("normal output") is False
    with pytest.raises(TypeError):
        validate_adb_args(  # type: ignore[call-arg]
            ["kill-server"],
            allow_kill_server_on_mismatch=True,
            mismatch_stderr=mismatch_msg,
        )
    with pytest.raises(TypeError):
        check_no_kill_server(  # type: ignore[call-arg]
            ["kill-server"], allow_on_mismatch=True, mismatch_stderr=mismatch_msg
        )
    with pytest.raises(ForbiddenArgv, match="kill-server"):
        validate_adb_args(["-s", "placeholder-001", "kill-server"], allow_destructive=True)


def test_forbid_sudo():
    """Verify `sudo` is always rejected in or around adb arguments."""
    with pytest.raises(ForbiddenArgv, match="sudo"):
        validate_adb_args(["sudo", "adb", "devices"])

    with pytest.raises(ForbiddenArgv, match="sudo"):
        validate_adb_args(["shell", "sudo", "id"])

    with pytest.raises(ForbiddenArgv, match="sudo"):
        check_no_sudo(["shell", "sudo rm -rf /data/local/tmp/test"])


def test_forbid_pm_clear_without_flag():
    """Verify `pm clear` is rejected unless allow_destructive=True is passed."""
    with pytest.raises(ForbiddenArgv, match="pm clear"):
        validate_adb_args(["shell", "pm", "clear", "com.example.app"])

    with pytest.raises(ForbiddenArgv, match="pm clear"):
        validate_adb_args(["shell", "pm clear com.example.app"])

    # Allowed when explicit flag is provided
    allowed = validate_adb_args(
        ["shell", "pm", "clear", "com.example.app"],
        allow_destructive=True,
    )
    assert allowed == ["shell", "pm", "clear", "com.example.app"]


def test_forbid_uninstall_without_flag():
    """Verify `uninstall` is rejected unless allow_destructive=True is passed."""
    with pytest.raises(ForbiddenArgv, match="uninstall"):
        validate_adb_args(["uninstall", "com.example.app"])

    with pytest.raises(ForbiddenArgv, match="uninstall"):
        validate_adb_args(["shell", "pm", "uninstall", "com.example.app"])

    with pytest.raises(ForbiddenArgv, match="uninstall"):
        check_no_destructive_package_ops(["shell", "pm uninstall -k com.example.app"])

    # Allowed when explicit flag is provided
    allowed = validate_adb_args(
        ["uninstall", "com.example.app"],
        allow_destructive=True,
    )
    assert allowed == ["uninstall", "com.example.app"]

    # Token-based check: substring containing 'uninstall' is not falsely rejected
    benign_sub = ["shell", "getprop", "ro.my_uninstall_flag"]
    assert validate_adb_args(benign_sub) == benign_sub


def test_forbid_interactive_shell():
    """Verify bare `adb shell` with no following args is refused, while `shell getprop` is allowed."""
    with pytest.raises(ForbiddenArgv, match="shell"):
        validate_adb_args(["shell"])

    with pytest.raises(ForbiddenArgv, match="shell"):
        validate_adb_args(["-s", "placeholder-serial-1", "shell"])

    with pytest.raises(ForbiddenArgv, match="shell"):
        validate_adb_args(["shell", "   "])

    allowed = validate_adb_args(["shell", "getprop"])
    assert allowed == ["shell", "getprop"]


def test_forbid_unbounded_logcat():
    """Verify `logcat` without `-d` and without `-t` is refused, while `-d` or `-t` is allowed."""
    with pytest.raises(ForbiddenArgv, match="logcat"):
        validate_adb_args(["logcat"])

    with pytest.raises(ForbiddenArgv, match="logcat"):
        validate_adb_args(["-s", "placeholder-serial-1", "logcat", "*:E"])

    with pytest.raises(ForbiddenArgv, match="logcat"):
        validate_adb_args(["shell", "logcat"])

    assert validate_adb_args(["logcat", "-d"]) == ["logcat", "-d"]
    assert validate_adb_args(["logcat", "-t", "50"]) == ["logcat", "-t", "50"]
    assert validate_adb_args(["logcat", "-t50"]) == ["logcat", "-t50"]
    assert validate_adb_args(["shell", "logcat", "-d"]) == ["shell", "logcat", "-d"]


def test_safe_adb_args_allowed():
    """Verify benign adb commands pass validation unchanged."""
    args = ["-s", "placeholder-serial-1", "shell", "getprop", "ro.build.version.release"]
    assert validate_adb_args(args) == args


_UI_DUMP = "/sdcard/adapt-ui-0123456789abcdef.xml"


@pytest.mark.parametrize(
    "argv",
    [
        # CORE-01 audit reproductions.
        ["-s", "SERIAL", "shell", "/system/bin/pm", "clear", "com.example.app"],
        ["-s", "SERIAL", "shell", "-T", "pm", "clear", "com.example.app"],
        ["-s", "SERIAL", "shell", "env", "pm", "uninstall", "com.example.app"],
        ["-s", "SERIAL", "shell", "exec", "cmd", "package", "clear", "com.example.app"],
        ["-s", "SERIAL", "shell", "/system/bin/sh", "-c", "'pm clear com.example.app'"],
        ["-s", "SERIAL", "exec-out", "pm clear com.example.app; reboot"],
        # exec-out / exec-in get the same inspection as shell.
        ["-s", "SERIAL", "exec-out", "pm", "clear", "com.example.app"],
        ["-s", "SERIAL", "exec-out", "/system/bin/cmd package uninstall com.example.app"],
        ["-s", "SERIAL", "exec-in", "pm", "clear", "com.example.app"],
        ["-s", "SERIAL", "exec-out", "getprop; reboot"],
        ["-s", "SERIAL", "exec-out", "-T", "pm", "clear", "com.example.app"],
        # adb shell options, combined flags, -e with a value, and `--`.
        ["-s", "SERIAL", "shell", "-n", "-tt", "pm", "clear", "com.example.app"],
        ["-s", "SERIAL", "shell", "-nT", "pm", "clear", "com.example.app"],
        ["-s", "SERIAL", "shell", "-x", "pm", "clear", "com.example.app"],
        ["-s", "SERIAL", "shell", "-e", "none", "pm", "clear", "com.example.app"],
        ["-s", "SERIAL", "shell", "-e~", "pm", "clear", "com.example.app"],
        ["-s", "SERIAL", "shell", "--", "pm", "clear", "com.example.app"],
        ["-s", "SERIAL", "shell", "-c", "pm clear com.example.app"],
        ["-s", "SERIAL", "shell", "-e"],
        # Paths, case, and nested shells.
        ["shell", "/system/bin/../bin/pm", "uninstall", "com.example.app"],
        ["shell", "PM", "CLEAR", "com.example.app"],
        ["shell", "/system/bin/mksh", "-c", "'/system/bin/pm clear com.example.app'"],
        ["shell", "sh", "-c", "\"sh -c 'pm clear com.example.app'\""],
        # Wrappers that hide the real command.
        ["shell", "/system/bin/env", "pm", "clear", "com.example.app"],
        ["shell", "command", "pm", "clear", "com.example.app"],
        ["shell", "builtin", "command", "pm", "clear", "com.example.app"],
        ["shell", "nohup", "pm", "clear", "com.example.app"],
        ["shell", "time", "pm", "clear", "com.example.app"],
        ["shell", "nice", "-n", "5", "pm", "clear", "com.example.app"],
        ["shell", "timeout", "5", "pm", "clear", "com.example.app"],
        ["shell", "xargs", "pm", "clear"],
        ["shell", "su", "0", "pm", "clear", "com.example.app"],
        ["shell", "/system/bin/toybox", "xargs", "pm", "clear"],
        ["shell", "busybox", "sh", "-c", "'pm clear com.example.app'"],
        ["shell", "run-as", "com.example.app", "pm", "clear", "com.example.app"],
        ["shell", "find", "/sdcard", "-maxdepth", "0", "-exec", "pm", "clear", "com.example.app", "\\;"],
        # Shell indirection and evaluating builtins.
        ["shell", "eval", "'pm clear com.example.app'"],
        ["shell", ".", "/data/local/tmp/script.sh"],
        ["shell", "source", "/data/local/tmp/script.sh"],
        ["shell", "bash", "-c", "'pm clear com.example.app'"],
        ["shell", "trap", "'pm clear com.example.app'", "EXIT"],
        ["shell", "let", "'x[$(pm clear com.example.app)]'"],
        ["shell", "test", "'x[$(pm clear com.example.app)]'", "-eq", "0"],
        # Command words the shell would rewrite before running them.
        ["shell", "FOO=1", "pm", "clear", "com.example.app"],
        ["shell", "(pm", "clear", "com.example.app)"],
        ["shell", "!", "pm", "clear", "com.example.app"],
        ["shell", "/system/bin/p?", "clear", "com.example.app"],
        ["shell", "/s*/bin/pm", "clear", "com.example.app"],
        ["shell", "p[m]", "clear", "com.example.app"],
        ["shell", "{pm,}", "clear", "com.example.app"],
        ["shell", "'pm'", "clear", "com.example.app"],
        ["shell", "p\\m", "clear", "com.example.app"],
        ["shell", "pm\x0bclear", "com.example.app"],
        # Other destructive package spellings.
        ["shell", "pm", "uninstall", "-k", "--user", "0", "com.example.app"],
        ["shell", "pm", "clear", "--user", "0", "com.example.app"],
        ["shell", "pm", "uninstall-system-updates", "com.example.app"],
        ["shell", "cmd", "package", "uninstall", "com.example.app"],
        ["shell", "cmd", "-w", "package", "clear", "com.example.app"],
        ["shell", "pm", "'clear'", "com.example.app"],
        ["shell", "pm", "cl\\ear", "com.example.app"],
        ["shell", "service", "call", "package", "1", "s16", "com.example.app"],
        ["shell", "service", "call", "activity", "1", "s16", "com.example.app"],
        # Interactive exec-out / shell-with-options and streaming logcat via exec-out.
        ["exec-out"],
        ["shell", "-T"],
        ["exec-out", "logcat"],
        ["shell", "/system/bin/logcat"],
    ],
)
def test_remote_command_bypasses_rejected(argv):
    """CORE-01: destructive or uninspectable device commands are refused in every spelling."""
    with pytest.raises(ForbiddenArgv):
        validate_adb_args(argv)


@pytest.mark.parametrize(
    "argv",
    [
        # Commands ADAPT itself sends through the guard.
        ["shell", "getprop", "ro.build.version.sdk"],
        ["shell", "input", "tap", "10", "20"],
        ["shell", "input", "swipe", "1", "2", "3", "4", "300"],
        ["shell", "input", "keyevent", "KEYCODE_BACK"],
        ["shell", "input", "text", "'hello%sworld'"],
        ["shell", "input", "text", "hello%sworld"],
        ["shell", "input", "text", "'$5 & more'"],
        ["shell", "wm", "size"],
        ["shell", "wm", "density"],
        ["shell", "dumpsys", "window"],
        ["shell", "dumpsys", "display"],
        ["shell", "dumpsys", "desktopmode"],
        ["shell", "settings", "get", "global", "window_animation_scale"],
        ["shell", "settings", "put", "global", "window_animation_scale", "0"],
        ["shell", "settings", "--user", "0", "get", "secure", "show_touches"],
        ["shell", "am", "get-current-user"],
        ["shell", "uiautomator", "dump", _UI_DUMP],
        ["shell", "rm", "-f", _UI_DUMP],
        ["shell", "date", "+%s"],
        ["exec-out", "screencap", "-p"],
        ["exec-out", "uiautomator", "dump", "/dev/tty"],
        ["exec-out", "cat", _UI_DUMP],
        ["logcat", "-d", "-t", "100"],
        ["logcat", "-d", "-t", "100", "-s", "MyTag:V"],
        ["install", "-r", "app.apk"],
        ["devices", "-l"],
        ["version"],
        # Ordinary user commands through the raw `adapt adb` passthrough.
        ["shell", "ls", "/sdcard"],
        ["shell", "pm", "list", "packages"],
        ["shell", "pm", "path", "com.example.app"],
        ["shell", "am", "start", "-n", "com.example.app/.MainActivity"],
        ["shell", "cmd", "package", "list", "packages"],
        ["shell", "/system/bin/getprop", "ro.product.model"],
        ["shell", "-T", "getprop", "ro.serialno"],
        ["shell", "-t", "-e", "none", "getprop", "ro.serialno"],
        ["shell", "logcat", "-d", "*:E"],
        ["shell", "find", "/sdcard", "-name", "x.png"],
        ["shell", "test", "-e", "/sdcard/x.png"],
        ["shell", "service", "list"],
        ["shell", "sh", "-c", "'getprop ro.serialno'"],
        ["shell", "getprop ro.build.version.sdk"],
    ],
)
def test_legitimate_commands_still_pass(argv):
    """CORE-01: the commands ADAPT and its users rely on keep passing the guard."""
    full = ["-s", "placeholder-serial-1", *argv]
    assert validate_adb_args(full) == full


def test_require_serial_if_many():
    """Verify require_serial_if_many behavior for 0, 1, and multiple placeholder devices."""
    # 0 devices -> returns None when no serial specified
    assert require_serial_if_many([], None) is None
    assert require_serial_if_many([], "placeholder-serial-1") == "placeholder-serial-1"

    # 1 device -> returns implicit serial when none specified
    single = [{"serial": "placeholder-serial-1", "state": "device", "model": "Fake_Phone_A"}]
    assert require_serial_if_many(single, None) == "placeholder-serial-1"
    assert require_serial_if_many(single, "placeholder-serial-override") == "placeholder-serial-override"

    # >1 devices without serial -> raises NeedSerial listing connected devices
    many = [
        {"serial": "placeholder-serial-1", "state": "device", "model": "Fake_Phone_A"},
        {"serial": "placeholder-serial-2", "state": "device", "model": "Fake_Phone_B"},
    ]
    with pytest.raises(NeedSerial) as exc_info:
        require_serial_if_many(many, None)

    err_msg = str(exc_info.value)
    assert "placeholder-serial-1" in err_msg
    assert "placeholder-serial-2" in err_msg
    assert "Fake_Phone_A" in err_msg
    assert "Fake_Phone_B" in err_msg

    # >1 devices with explicit serial -> returns explicit serial
    assert require_serial_if_many(many, "placeholder-serial-2") == "placeholder-serial-2"

    # >1 devices with -s in args or ANDROID_SERIAL in env -> resolves cleanly
    assert (
        require_serial_if_many(many, None, args=["-s", "placeholder-serial-1", "shell"])
        == "placeholder-serial-1"
    )
    assert (
        require_serial_if_many(many, None, env={"ANDROID_SERIAL": "placeholder-serial-2"})
        == "placeholder-serial-2"
    )


@pytest.mark.parametrize(
    "command",
    [
        ["shell", "run-as", "com.example.app", "ls", "files"],
        ["exec-out", "run-as", "com.example.app", "cat", "databases/app.db"],
        ["shell", "run-as", "com.example.app", "--user", "0", "ls"],
        ["shell", "timeout", "5", "ping", "-c", "1", "192.0.2.1"],
        ["shell", "su", "-c", "id"],
        ["shell", "su", "0", "id"],
        ["shell", "su", "root", "getprop", "ro.debuggable"],
        ["shell", "su", "0", "logcat", "-d", "-t", "5"],
    ],
)
def test_fixed_layout_wrappers_are_inspected_not_refused(command):
    assert validate_adb_args(["-s", "SERIAL", *command])


@pytest.mark.parametrize(
    "command",
    [
        ["shell", "run-as", "com.example.app", "pm", "clear", "com.example.app"],
        ["shell", "run-as", "com.example.app", "--user", "0", "pm", "clear", "com.example.app"],
        ["exec-out", "run-as", "com.example.app", "/system/bin/pm", "clear", "com.example.app"],
        ["shell", "timeout", "5", "pm", "uninstall", "com.example.app"],
        ["shell", "timeout", "-s", "KILL", "5", "pm", "clear", "com.example.app"],
        ["shell", "su", "-c", "'pm clear com.example.app'"],
        ["shell", "su", "0", "pm", "uninstall", "com.example.app"],
        ["shell", "su", "0", "-c", "'pm clear com.example.app'"],
        ["shell", "su", "0", "sh", "-c", "'pm clear com.example.app'"],
        ["shell", "su", "--mount-master", "-c", "'pm clear com.example.app'"],
        ["shell", "su", "0", "timeout", "5", "run-as", "com.example.app", "pm", "clear", "com.example.app"],
        ["shell", "su", "0", "logcat"],
        ["shell", "run-as", "com.example.app", "env", "pm", "clear", "com.example.app"],
        ["shell", "timeout", "5", "sh", "-c", "'pm clear x; reboot'"],
    ],
)
def test_fixed_layout_wrappers_cannot_hide_refused_commands(command):
    with pytest.raises(ForbiddenArgv):
        validate_adb_args(["-s", "SERIAL", *command])
