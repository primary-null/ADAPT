"""Allowlisted argv shapes for Developer Options device commands."""

from __future__ import annotations

import pytest

from adapt_adb import safety

PKG = "com.example.app"

ACCEPTED = {
    "getprop": ["shell", "getprop", "ro.build.version.sdk"],
    "setprop": ["shell", "setprop", "debug.layout", "true"],
    "current-user": ["shell", "am", "get-current-user"],
    "settings-get": ["shell", "settings", "get", "global", "always_finish_activities"],
    "settings-get-user": ["shell", "settings", "--user", "10", "get", "system", "show_touches"],
    "settings-put": ["shell", "settings", "put", "global", "window_animation_scale", "0.5"],
    "settings-put-user": ["shell", "settings", "--user", "10", "put", "system", "font_scale", "1.3"],
    "settings-delete": ["shell", "settings", "delete", "global", "debug_view_attributes"],
    "settings-delete-user": ["shell", "settings", "--user", "10", "delete", "secure", "anr_show_background"],
    "wm-density-read": ["shell", "wm", "density"],
    "wm-size-read": ["shell", "wm", "size"],
    "wm-density-set": ["shell", "wm", "density", "480"],
    "wm-density-reset": ["shell", "wm", "density", "reset"],
    "battery-read": ["shell", "dumpsys", "battery"],
    "battery-unplug": ["shell", "cmd", "battery", "unplug"],
    "battery-reset": ["shell", "cmd", "battery", "reset"],
    "deviceidle-read": ["shell", "dumpsys", "deviceidle", "get", "deep"],
    "deviceidle-force": ["shell", "dumpsys", "deviceidle", "force-idle"],
    "deviceidle-unforce": ["shell", "dumpsys", "deviceidle", "unforce"],
    "debug-app-set": ["shell", "am", "set-debug-app", "--persistent", PKG],
    "debug-app-set-wait": ["shell", "am", "set-debug-app", "-w", "--persistent", PKG],
    "debug-app-clear": ["shell", "am", "clear-debug-app"],
    "package-dump": ["shell", "dumpsys", "package", PKG],
    "package-compile": ["shell", "cmd", "package", "compile", "-m", "speed-profile", "-f", PKG],
    "package-compile-reset": ["shell", "cmd", "package", "compile", "--reset", PKG],
    "uimode-read": ["shell", "cmd", "uimode", "night"],
    "uimode-set": ["shell", "cmd", "uimode", "night", "yes"],
    "standby-bucket-read": ["shell", "am", "get-standby-bucket", "--user", "10", PKG],
    "standby-bucket-set": ["shell", "am", "set-standby-bucket", "--user", "10", PKG, "rare"],
    "appops-read": ["shell", "cmd", "appops", "get", "--user", "10", PKG, "RUN_ANY_IN_BACKGROUND"],
    "appops-set": ["shell", "cmd", "appops", "set", "--user", "10", PKG, "RUN_ANY_IN_BACKGROUND", "ignore"],
    "app-locales-read": ["shell", "cmd", "locale", "get-app-locales", PKG, "--user", "10"],
    "app-locales-set": ["shell", "cmd", "locale", "set-app-locales", PKG, "--user", "10", "--locales", "ar,fr-ca"],
    "app-locales-clear": ["shell", "cmd", "locale", "set-app-locales", PKG, "--user", "10"],
    "netpolicy-read": ["shell", "cmd", "netpolicy", "get", "restrict-background"],
    "netpolicy-set": ["shell", "cmd", "netpolicy", "set", "restrict-background", "true"],
    "airplane-set": ["shell", "cmd", "connectivity", "airplane-mode", "enable"],
    "page-size": ["shell", "getconf", "PAGE_SIZE"],
    "features-read": ["shell", "pm", "list", "features"],
    "device-policy-read": ["shell", "dumpsys", "device_policy"],
}

REFUSED = [
    ["shell"],
    ["shell", "rm", "-rf", "/sdcard"],
    ["shell", "sh", "-c", "id"],
    ["shell", "pm", "clear", PKG],
    ["shell", "pm", "uninstall", PKG],
    ["shell", "service", "call", "activity", "1599295570"],
    ["shell", "setprop", "persist.sys.language", "en"],
    ["shell", "setprop", "debug.layout", "true;reboot"],
    ["shell", "setprop", "debug.layout", "a b"],
    ["shell", "setprop", "debug.layout"],
    ["shell", "setprop", "ro.debuggable", "1"],
    ["shell", "getprop", "a;b"],
    ["shell", "getprop"],
    ["shell", "settings", "put", "other", "key", "1"],
    ["shell", "settings", "put", "global", "key", "1;reboot"],
    ["shell", "settings", "put", "global", "key", "$(id)"],
    ["shell", "settings", "put", "global", "-key", "1"],
    ["shell", "settings", "--user", "x", "get", "system", "show_touches"],
    ["shell", "settings", "list", "global"],
    ["shell", "wm", "density", "x"],
    ["shell", "wm", "size", "1080x2400"],
    ["shell", "cmd", "battery", "set", "level", "5"],
    ["shell", "dumpsys", "deviceidle", "whitelist", "+com.example.app"],
    ["shell", "am", "set-debug-app", PKG],
    ["shell", "am", "set-debug-app", "-w", PKG],
    ["shell", "am", "set-debug-app", "-w", "--persistent", "bad name"],
    ["shell", "am", "kill", PKG],
    ["shell", "am", "force-stop", PKG],
    ["shell", "cmd", "package", "compile", "-m", "speed-profile", "-f", "-a"],
    ["shell", "cmd", "package", "compile", "-m", "SPEED", "-f", PKG],
    ["shell", "cmd", "package", "uninstall", PKG],
    ["shell", "cmd", "uimode", "night", "maybe"],
    ["shell", "cmd", "appops", "set", "--user", "10", PKG, "RUN_ANY_IN_BACKGROUND", "chaos"],
    ["shell", "cmd", "appops", "set", PKG, "RUN_ANY_IN_BACKGROUND", "ignore"],
    ["shell", "cmd", "locale", "set-app-locales", PKG, "--user", "10", "--locales", "ar; ls"],
    ["shell", "cmd", "netpolicy", "set", "restrict-background", "maybe"],
    ["shell", "cmd", "connectivity", "airplane-mode", "toggle"],
    ["shell", "svc", "wifi", "disable"],
    ["shell", "svc", "usb", "setFunctions", "none"],
    ["shell", "settings", "put", "global", "adb_enabled", "0;"],
    ["shell", "getconf", "PAGE_SIZE", "extra"],
    ["shell", "pm", "list", "packages"],
    ["shell", "dumpsys", "package"],
    ["shell", "dumpsys", "activity"],
    ["exec-out", "getprop", "ro.build.type"],
    ["install", "app.apk"],
]


def test_every_shape_has_an_accepted_example():
    assert set(ACCEPTED) == {name for name, _ in safety._DEV_SHAPES}


@pytest.mark.parametrize("shape", sorted(ACCEPTED))
def test_accepted_examples_match_their_shape(shape):
    assert safety.match_dev_options_shell(ACCEPTED[shape]) == shape


@pytest.mark.parametrize("argv", REFUSED, ids=lambda argv: " ".join(argv)[:60])
def test_refused_examples_have_no_shape(argv):
    assert safety.match_dev_options_shell(argv) is None


@pytest.mark.parametrize("shape", sorted(ACCEPTED))
def test_accepted_commands_also_pass_the_general_adb_guard(shape):
    assert safety.validate_adb_args(["-s", "SERIAL", *ACCEPTED[shape]]) == ["-s", "SERIAL", *ACCEPTED[shape]]


def test_write_shapes_are_exactly_the_state_changing_ones():
    writes = {name for name in ACCEPTED if safety.dev_options_shape_writes(name)}
    reads = set(ACCEPTED) - writes
    assert all(name.endswith(("read", "get", "get-user", "dump")) or name in {"getprop", "current-user", "page-size"} or name == "wm-size-read" for name in reads)
    assert {"setprop", "settings-put", "settings-delete", "battery-unplug", "battery-reset", "deviceidle-force", "package-compile",
            "airplane-set", "debug-app-clear", "wm-density-set"} <= writes
    assert not writes & {"getprop", "settings-get", "battery-read", "package-dump", "page-size", "features-read", "device-policy-read"}


def test_the_matcher_never_raises_on_odd_input():
    for argv in ([], [""], ["shell", ""], ["shell", None], ["shell", "settings", "get", "global", "k" * 5000]):
        assert safety.match_dev_options_shell(argv) is None


def test_no_raw_binder_or_systemproperties_poke_is_allowlisted():
    flat = " ".join(" ".join(shape) for _, shape in safety._DEV_SHAPES)
    assert "service" not in flat and "call" not in flat and "1599295570" not in flat
