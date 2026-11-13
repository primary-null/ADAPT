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
    """Verify `adb kill-server` is blocked unless explicit version mismatch recovery is active."""
    with pytest.raises(ForbiddenArgv, match="kill-server"):
        validate_adb_args(["kill-server"])

    with pytest.raises(SafetyError):
        check_no_kill_server(["-s", "placeholder-001", "kill-server"])

    # Even with allow_kill_server_on_mismatch=True, missing/non-matching stderr still raises
    with pytest.raises(ForbiddenArgv):
        validate_adb_args(
            ["kill-server"],
            allow_kill_server_on_mismatch=True,
            mismatch_stderr="error: device not found",
        )

    # Explicit version mismatch stderr allows recovery when opted in
    mismatch_msg = "adb server version (39) doesn't match this client (41); killing..."
    assert is_version_mismatch_stderr(mismatch_msg) is True
    assert is_version_mismatch_stderr("normal output") is False
    assert (
        validate_adb_args(
            ["kill-server"],
            allow_kill_server_on_mismatch=True,
            mismatch_stderr=mismatch_msg,
        )
        == ["kill-server"]
    )


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
