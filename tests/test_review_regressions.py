"""Focused offline regressions for Astra review findings F1-F12."""

from __future__ import annotations

import json
import io
import shlex
import subprocess
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

import adapt_adb.cli as cli_mod
import adapt_adb.doctor as doctor_mod
from adapt_adb.adb import (
    AdbProbeError,
    GoogleRedirectHandler,
    _validate_google_https_url,
    enrich_devices_long,
    get_connected_devices,
    parse_devices_short,
    split_adb_prefix,
)
from adapt_adb.cli import app, run_connect_command, run_pair_command
from adapt_adb.doctor import (
    DoctorEnv,
    Server5037State,
    UnsafeAdbServerError,
    ensure_debian_user_path,
    preflight_adb_server,
    run_doctor_checks,
)
from adapt_adb.mcp.server import dispatch_tool_call
from adapt_adb.mcp.tools import escape_adb_input_text, tool_device_info, tool_type
from adapt_adb.render.ui_summary import capture_ui_summary
from adapt_adb.safety import ForbiddenArgv, validate_adb_args


def _payload(result: object) -> dict[str, object]:
    return json.loads(result.content[0].text)  # type: ignore[attr-defined]


def test_f1_type_rejects_controls_before_any_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    resolve = MagicMock()
    monkeypatch.setattr("adapt_adb.mcp.tools.resolve_adb", resolve)
    for text in ("line\nreboot", "carriage\rreturn", "tab\tdata", "nul\0data"):
        result = tool_type(text)
        assert result["error"] == "invalid_params"
    resolve.assert_not_called()


def test_f1_type_posix_quotes_one_remote_argument() -> None:
    for text in ("", "a b", "quote'back\\slash", "$HOME; reboot", "*?[abc]"):
        escaped = escape_adb_input_text(text)
        assert shlex.split(escaped, posix=True) == [text.replace(" ", "%s")]


def test_f2_tab_identity_preserves_spaced_mdns_and_enriches_exactly() -> None:
    serial = "adb-Pixel 8 Pro-device._adb-tls-connect._tcp"
    short = f"List of devices attached\n{serial}\tdevice\nemulator-5554\toffline\n"
    long = (
        "List of devices attached\n"
        f"{serial}\tdevice product:husky model:Pixel_8_Pro transport_id:7\n"
        "emulator-5554\toffline transport_id:2\n"
    )
    identities = parse_devices_short(short)
    assert identities[0] == {"serial": serial, "state": "device"}
    enriched = enrich_devices_long(identities, long)
    assert enriched[0]["serial"] == serial
    assert enriched[0]["model"] == "Pixel 8 Pro"


def test_f2_malformed_whitespace_listing_fails() -> None:
    with pytest.raises(ValueError, match="TAB-delimited"):
        parse_devices_short("List of devices attached\nSERIAL device\n")


def test_f3_global_parser_keeps_operands_opaque_and_normalizes_aliases() -> None:
    assert split_adb_prefix(["logcat", "-d", "-s", "MyTag"]).serial is None
    assert split_adb_prefix(["shell", "echo", "-something"]).serial is None
    parsed = split_adb_prefix(["--serial=Pixel 8 Pro", "shell", "getprop"])
    assert parsed.prefix == ["-s", "Pixel 8 Pro"]
    assert parsed.command == ["shell", "getprop"]
    for argv in (
        ["-s"],
        ["-s", "-e", "shell"],
        ["--serial="],
        ["-s", "A", "-e", "shell"],
        ["-d", "-e", "shell"],
    ):
        with pytest.raises(ValueError):
            split_adb_prefix(list(argv))


def test_f3_android_serial_unavailable_never_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    run = MagicMock()
    monkeypatch.setenv("ANDROID_SERIAL", "OFFLINE_B")
    monkeypatch.setattr(cli_mod, "resolve_adb", lambda: Path("adb"))
    monkeypatch.setattr(cli_mod, "preflight_adb_server", lambda *a, **k: None)
    monkeypatch.setattr(
        cli_mod,
        "get_connected_devices",
        lambda p: [
            {"serial": "READY_A", "state": "device"},
            {"serial": "OFFLINE_B", "state": "offline"},
        ],
    )
    monkeypatch.setattr(subprocess, "run", run)
    result = runner.invoke(app, ["adb", "shell", "getprop"])
    assert result.exit_code == 1
    run.assert_not_called()


def test_f4_dispatch_sanitizes_timeout_without_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "SYNTHETIC_SECRET"

    def timeout(*args: object, **kwargs: object) -> object:
        raise subprocess.TimeoutExpired(["adb", "shell", "input", "text", secret], 20)

    monkeypatch.setattr("adapt_adb.mcp.tools.tool_type", timeout)
    result = dispatch_tool_call("type", {"text": secret})
    encoded = json.dumps(_payload(result))
    assert _payload(result)["error"] == "adb_timeout"
    assert secret not in encoded
    assert "shell" not in encoded


def test_f4_f6_pair_code_is_stdin_only_and_success_is_textual(monkeypatch: pytest.MonkeyPatch) -> None:
    run = MagicMock(
        return_value=subprocess.CompletedProcess(
            ["adb", "pair", "192.0.2.1:37123"], 0, "Successfully paired to 192.0.2.1:37123\n", ""
        )
    )
    monkeypatch.setattr(cli_mod, "preflight_adb_server", lambda *a, **k: None)
    monkeypatch.setattr(subprocess, "run", run)
    assert run_pair_command(Path("adb"), "192.0.2.1:37123", "123456") == 0
    argv = run.call_args.args[0]
    assert "123456" not in argv
    assert run.call_args.kwargs["input"] == "123456\n"
    assert run.call_args.kwargs["timeout"] == 30


@pytest.mark.parametrize(
    ("stdout", "returncode", "expected"),
    [
        ("connected to 192.0.2.1:5555\n", 0, 0),
        ("already connected to 192.0.2.1:5555\n", 0, 0),
        ("failed to connect to 192.0.2.1:5555\n", 0, 1),
        ("", 0, 1),
        ("connected to 192.0.2.1:5555\n", 2, 2),
    ],
)
def test_f6_connect_requires_rc_and_success_text(
    monkeypatch: pytest.MonkeyPatch, stdout: str, returncode: int, expected: int
) -> None:
    monkeypatch.setattr(cli_mod, "preflight_adb_server", lambda *a, **k: None)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], returncode, stdout, ""),
    )
    assert run_connect_command(Path("adb"), "192.0.2.1") == expected


def test_f6_wireless_menu_stops_after_failed_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    connect = MagicMock()
    monkeypatch.setattr(cli_mod, "build_next_actions", lambda report: [
        {"key": "1", "id": "wireless"},
        {"key": "q", "id": "done"},
    ])
    monkeypatch.setattr(cli_mod, "resolve_adb", lambda: Path("adb"))
    monkeypatch.setattr(cli_mod, "run_pair_command", lambda *a, **k: 1)
    monkeypatch.setattr(cli_mod, "run_connect_command", connect)
    monkeypatch.setattr(cli_mod, "run_doctor_checks", lambda *a, **k: {})
    monkeypatch.setattr(cli_mod, "render_doctor_human", lambda *a, **k: "ok")
    monkeypatch.setattr(cli_mod.getpass, "getpass", lambda prompt: "123456")
    monkeypatch.setattr(
        cli_mod.sys,
        "stdin",
        io.StringIO("1\n192.0.2.1:37123\n192.0.2.1:5555\nq\n"),
    )
    cli_mod._run_next_menu({}, show_fix=False)
    connect.assert_not_called()


@pytest.mark.parametrize(
    "arguments",
    [
        {"apk": "x.apk", "grant": "false"},
        {"apk": "x.apk", "reinstall": 0},
        {"x": 1.5, "y": 2},
        {"x": True, "y": 2},
        {"device": None, "x": 1, "y": 2},
        {"x": 1, "y": 2, "surprise": 3},
    ],
)
def test_f5_invalid_schema_never_executes(monkeypatch: pytest.MonkeyPatch, arguments: dict[str, object]) -> None:
    tap = MagicMock()
    install = MagicMock()
    monkeypatch.setattr("adapt_adb.mcp.tools.tool_tap", tap)
    monkeypatch.setattr("adapt_adb.mcp.tools.tool_install_apk", install)
    name = "install_apk" if "apk" in arguments else "tap"
    result = dispatch_tool_call(name, arguments)
    assert _payload(result)["error"] == "invalid_params"
    tap.assert_not_called()
    install.assert_not_called()


def test_f7_unknown_listener_fails_without_running_listener_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    query = MagicMock()
    monkeypatch.setattr(doctor_mod, "_query_adb_version_raw", query)
    unknown = Server5037State(
        listening=True,
        server_exe=Path("untrusted-listener"),
        owner=None,
        server_version_raw=None,
    )
    with pytest.raises(UnsafeAdbServerError, match="unverified"):
        preflight_adb_server(Path("adb"), ["devices"], environ={}, state=unknown)
    query.assert_not_called()


def test_f7_matching_listener_and_custom_endpoint_handling(monkeypatch: pytest.MonkeyPatch) -> None:
    matching = Server5037State(
        listening=True,
        owner="current-user",
        owner_matches_current_user=True,
        server_version_raw="Version 35.0.2-123456",
    )
    monkeypatch.setattr(
        doctor_mod, "_query_adb_version_raw", lambda *a, **k: "Version 35.0.2-999999"
    )
    preflight_adb_server(Path("adb"), ["devices"], environ={}, state=matching)
    with pytest.raises(UnsafeAdbServerError, match="endpoint"):
        preflight_adb_server(Path("adb"), ["-H", "remote.example", "devices"], environ={})


def test_f7_doctor_unknown_listener_skips_devices() -> None:
    env = DoctorEnv(
        host_os="debian",
        which_set=True,
        which_adb=Path("/mock/adb"),
        server_state=Server5037State(listening=True),
    )
    report = run_doctor_checks(env)
    assert report["exit_code"] == 3
    assert env.devices_l_invoked == []


def test_f8_enumeration_failure_is_not_empty_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("adapt_adb.doctor.preflight_adb_server", lambda *a, **kw: None)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", "cannot connect"),
    )
    with pytest.raises(AdbProbeError):
        get_connected_devices(Path("adb"))


def test_f8_failed_device_probe_and_unknown_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("adapt_adb.mcp.tools.resolve_adb", lambda: Path("adb"))
    monkeypatch.setattr(
        "adapt_adb.mcp.tools.resolve_target_device",
        lambda *a, **k: ("SERIAL", None, [{"device": "SERIAL", "state": "device"}]),
    )
    monkeypatch.setattr(
        "adapt_adb.mcp.tools.run_adb",
        lambda *a, **k: subprocess.CompletedProcess(a[1], 1, "", "offline"),
    )
    assert tool_device_info()["error"] == "adb_probe_failed"

    outputs = iter(["Pixel\n", "34\n", "14\n", "1080x2400\n", "420\n", "unrecognized\n"])
    monkeypatch.setattr(
        "adapt_adb.mcp.tools.run_adb",
        lambda *a, **k: subprocess.CompletedProcess(a[1], 0, next(outputs), ""),
    )
    assert tool_device_info()["lock"] == "unknown"


def test_f9_failed_dump_never_cats_and_cleans_owned_path() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[-1] == "/dev/tty":
            return subprocess.CompletedProcess(argv, 1, "", "primary failed")
        if "uiautomator" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "fallback failed")
        return subprocess.CompletedProcess(argv, 0, "", "")

    with pytest.raises(RuntimeError, match="UI dump failed"):
        capture_ui_summary(Path("adb"), "SERIAL", runner=runner)
    assert not any("cat" in call for call in calls)
    cleanup = next(call for call in calls if "rm" in call)
    assert cleanup[-1].startswith("/sdcard/adapt-ui-")
    assert cleanup[-1] != "/sdcard/window_dump.xml"


def test_f10_profile_path_is_shell_quoted_literal() -> None:
    target = Path("/home/dev/Android $(touch marker) 'Tools'")
    home = Path("/home/dev")
    env = DoctorEnv(debian_profile_texts={})
    assert ensure_debian_user_path(target, home, env)
    profile = env.debian_profile_texts[home / ".profile"]
    line = next(item for item in profile.splitlines() if item.startswith("export PATH="))
    assignment = line.removeprefix("export PATH=")
    literal, suffix = assignment.rsplit(":", 1)
    assert shlex.split(literal, posix=True) == [str(target)]
    assert suffix == '"$PATH"'


def test_f11_url_and_redirect_policy_blocks_official_boundary_escape() -> None:
    for url in (
        "http://dl.google.com/archive.zip",
        "https://example.invalid/archive.zip",
        "https://user@dl.google.com/archive.zip",
        "https://dl.google.com:444/archive.zip",
    ):
        with pytest.raises(ValueError):
            _validate_google_https_url(url)
    handler = GoogleRedirectHandler(frozenset({"dl.google.com"}))
    req = urllib.request.Request("https://dl.google.com/a")
    with pytest.raises(ValueError):
        handler.redirect_request(req, None, 302, "Found", {}, "http://example.invalid/b")
    allowed = handler.redirect_request(req, None, 302, "Found", {}, "/b")
    assert allowed.full_url == "https://dl.google.com/b"


def test_f12_nested_quoted_clear_and_complex_shell_are_blocked_but_typed_word_is_not() -> None:
    with pytest.raises(ForbiddenArgv, match="pm clear"):
        validate_adb_args(["shell", "sh", "-c", "'pm \"clear\" com.example'"])
    with pytest.raises(ForbiddenArgv, match="Complex shell syntax"):
        validate_adb_args(["shell", "getprop", ";", "reboot"])
    assert validate_adb_args(["shell", "input", "text", "please-uninstall-later"])
    assert validate_adb_args(
        ["shell", "sh", "-c", "'pm \"clear\" com.example'"], allow_destructive=True
    )
