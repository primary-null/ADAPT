"""Unit tests for ADAPT v1 MCP server, tools, rendering, and safety rules (no physical phones)."""

from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import anyio
import pytest
from PIL import Image
from typer.testing import CliRunner

from adapt_adb.cli import app
from adapt_adb.mcp.server import (
    create_server,
    dispatch_tool_call,
    get_mcp_tools,
    run_stdio,
)
from adapt_adb.mcp.tools import (
    ForbiddenCommandError,
    escape_adb_input_text,
    parse_devices_l,
    resolve_target_device,
    tool_device_info,
    tool_doctor,
    tool_install_apk,
    tool_key,
    tool_list_devices,
    tool_logcat_tail,
    tool_screenshot,
    tool_setup_status,
    tool_swipe,
    tool_tap,
    tool_type,
    tool_ui_summary,
    validate_adb_args,
)
from adapt_adb.render.screenshot import scale_png_to_jpeg
from adapt_adb.render.ui_summary import parse_ui_summary

runner = CliRunner()


@pytest.fixture(autouse=True)
def _stub_server_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit fakes exercise tool commands; focused review tests cover the real preflight."""
    monkeypatch.setattr("adapt_adb.doctor.preflight_adb_server", lambda *a, **k: None)


def _make_synthetic_png(width: int = 1600, height: int = 2400) -> bytes:
    img = Image.new("RGBA", (width, height), color=(40, 120, 200, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


SAMPLE_DEVICES_TWO = (
    "List of devices attached\n"
    "SERIAL_A\tdevice usb:1-1 product:example model:Example_Phone device:example transport_id:1\n"
    "SERIAL_B\tdevice usb:1-2 product:example_tab model:Example_Tablet device:example_tab transport_id:2\n"
)

SAMPLE_DEVICES_ONE = (
    "List of devices attached\n"
    "SERIAL_A\tdevice usb:1-1 product:example model:Example_Phone device:example transport_id:1\n"
)

SAMPLE_UI_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="com.example.app"
        content-desc="" checkable="false" checked="false" clickable="false" enabled="true"
        focusable="false" focused="false" scrollable="false" long-clickable="false"
        password="false" selected="false" visible-to-user="true" bounds="[0,0][1080,2400]">
    <node index="0" text="Invisible" resource-id="com.example.app:id/hidden" class="android.widget.Button"
          content-desc="" clickable="true" visible-to-user="false" bounds="[10,10][100,100]" />
    <node index="1" text="ZeroSize" resource-id="com.example.app:id/zero" class="android.widget.Button"
          content-desc="" clickable="true" visible-to-user="true" bounds="[50,50][50,50]" />
    <node index="2" text="Checkout" resource-id="com.example.app:id/btn_checkout" class="android.widget.Button"
          content-desc="" clickable="true" visible-to-user="true" bounds="[138,900][400,980]" />
  </node>
</hierarchy>UI hierarchy dumped to: /dev/tty
"""


def test_mcp_command_no_longer_exits_2_when_sdk_present():
    """`adapt mcp` starts the stdio MCP server and exits 0 (not 2) when SDK is present."""
    result = runner.invoke(app, ["mcp"])
    assert result.exit_code == 0
    assert result.stdout == ""


def test_mcp_command_exits_2_when_sdk_missing():
    """`adapt mcp` writes to stderr and exits 2 if the `mcp` extra is missing."""
    with patch.dict(sys.modules, {"mcp": None}):
        result = runner.invoke(app, ["mcp"])
        assert result.exit_code == 2
        assert result.stdout == ""
        assert result.stderr != ""


def test_mcp_registered_tools_and_client_session(monkeypatch):
    """Verify the current MCP tools are registered with the official SDK client."""
    from mcp import Client

    tools = get_mcp_tools()
    names = [t.name for t in tools]
    expected = [
        "doctor",
        "setup_status",
        "list_devices",
        "device_info",
        "screenshot",
        "ui_summary",
        "tap",
        "swipe",
        "key",
        "type",
        "install_apk",
        "logcat_tail",
        "dex_inspect",
        "dex_displays",
        "android_versions",
        "android_capabilities",
        "developer_options_inspect",
        "tool_catalog",
        "app_validate",
        "app_logs",
        "debug_app_inspect",
        "diagnostic_logs",
        "logging_guide",
        "build_modes",
        "app_launch",
        "app_stop",
        "app_state",
        "app_open_link",
        "log_mark",
        "test_run",
        "test_status",
        "dev_options_apply",
        "dev_options_restore",
        "permission_grant",
        "permission_revoke",
        "ui_tap_text",
        "ui_wait",
        "help",
    ]
    assert names == expected

    fake_adb = Path("/mock/adb")
    monkeypatch.setattr("adapt_adb.mcp.tools.resolve_adb", lambda: fake_adb)

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=SAMPLE_DEVICES_ONE, stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    server = create_server()

    async def _exercise():
        async with Client(server) as client:
            listed = await client.list_tools()
            assert [t.name for t in listed.tools] == expected
            res = await client.call_tool("list_devices", {})
            assert not res.is_error
            data = json.loads(res.content[0].text)
            assert data["devices"][0]["device"] == "SERIAL_A"
            assert data["devices"][0]["model"] == "Example Phone"

    anyio.run(_exercise)


def test_list_devices_and_targeting_error_when_two_devices(monkeypatch):
    """When 2 devices are connected, list_devices lists both and device tools return need_device."""
    fake_adb = Path("/mock/adb")
    monkeypatch.setattr("adapt_adb.mcp.tools.resolve_adb", lambda: fake_adb)

    recorded_calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        recorded_calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout=SAMPLE_DEVICES_TWO, stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    # list_devices succeeds and lists both SERIAL_A and SERIAL_B
    listed = tool_list_devices()
    assert len(listed["devices"]) == 2
    assert listed["devices"][0]["device"] == "SERIAL_A"
    assert listed["devices"][0]["model"] == "Example Phone"
    assert listed["devices"][1]["device"] == "SERIAL_B"
    assert listed["devices"][1]["model"] == "Example Tablet"

    # Omitting `device` on a device-targeted tool returns `need_device` with both devices listed
    res = dispatch_tool_call("device_info", {})
    assert res.is_error is True
    err_payload = json.loads(res.content[0].text)
    assert err_payload["error"] == "need_device"
    assert len(err_payload["devices"]) == 2
    assert {d["device"] for d in err_payload["devices"]} == {"SERIAL_A", "SERIAL_B"}

    # Tap without device also returns `need_device` without invoking `shell input tap`
    tap_err = tool_tap(x=100, y=200)
    assert tap_err["error"] == "need_device"
    assert not any("tap" in call for call in recorded_calls)


def test_targeting_no_device_and_unknown_device(monkeypatch):
    """0 connected devices -> no_device; unknown serial -> unknown_device."""
    fake_adb = Path("/mock/adb")
    monkeypatch.setattr("adapt_adb.mcp.tools.resolve_adb", lambda: fake_adb)

    monkeypatch.setattr(
        "subprocess.run",
        lambda argv, **kw: subprocess.CompletedProcess(
            argv, 0, stdout="List of devices attached\n\n", stderr=""
        ),
    )
    assert tool_device_info()["error"] == "no_device"
    # A tool that acts on the phone never resolves an unnamed device, even with none ready.
    assert tool_tap(x=10, y=20)["error"] == "need_device"
    assert tool_tap(x=10, y=20, device="SERIAL_A")["error"] == "unknown_device"

    monkeypatch.setattr(
        "subprocess.run",
        lambda argv, **kw: subprocess.CompletedProcess(
            argv, 0, stdout=SAMPLE_DEVICES_ONE, stderr=""
        ),
    )
    unk = tool_tap(x=10, y=20, device="SERIAL_B")
    assert unk["error"] == "unknown_device"
    assert unk["devices"][0]["device"] == "SERIAL_A"


def test_logcat_uses_dash_d_and_truncates(monkeypatch):
    """`logcat_tail` MUST pass `-d` to adb logcat, always uses `-s <serial>`, and caps lines."""
    fake_adb = Path("/mock/adb")
    monkeypatch.setattr("adapt_adb.mcp.tools.resolve_adb", lambda: fake_adb)

    recorded_calls: list[list[str]] = []
    sample_logs = "\n".join(
        ["--------- beginning of main"]
        + [f"11-12 10:01:{i:02d}.123  1234  1256 E MyApp: line {i}" for i in range(15)]
    )

    def fake_run(argv, **kwargs):
        recorded_calls.append(list(argv))
        if "devices" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=SAMPLE_DEVICES_ONE, stderr="")
        if "logcat" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=sample_logs, stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    result = tool_logcat_tail(lines=5, tag="MyApp", level="E")
    assert len(result["lines"]) == 5
    assert result["lines"][-1].endswith("line 14")

    logcat_calls = [c for c in recorded_calls if "logcat" in c]
    assert len(logcat_calls) == 1
    cmd = logcat_calls[0]
    assert "-s" in cmd and "SERIAL_A" in cmd
    assert "-d" in cmd
    assert "MyApp:E" in cmd


def test_screenshot_binary_exec_out_and_jpeg_scale(monkeypatch):
    """`screenshot` uses `exec-out screencap -p` in binary mode and scales PNG down to JPEG <= 1024."""
    fake_adb = Path("/mock/adb")
    monkeypatch.setattr("adapt_adb.mcp.tools.resolve_adb", lambda: fake_adb)

    synthetic_png = _make_synthetic_png(width=1600, height=2400)
    recorded_calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(argv, **kwargs):
        recorded_calls.append((list(argv), dict(kwargs)))
        if "devices" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=SAMPLE_DEVICES_ONE, stderr="")
        if "screencap" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=synthetic_png, stderr=b"")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    res = dispatch_tool_call("screenshot", {"max_dimension": 1024, "quality": 80})
    assert res.is_error is False
    assert len(res.content) == 2

    img_block = res.content[0]
    txt_block = res.content[1]
    assert img_block.type == "image"
    assert img_block.mime_type == "image/jpeg"

    meta = json.loads(txt_block.text)
    assert meta["max_dimension"] == 1024
    assert meta["height"] == 1024
    assert meta["width"] == round(1600 * (1024 / 2400))

    raw_jpeg = base64.b64decode(img_block.data)
    with Image.open(io.BytesIO(raw_jpeg)) as decoded:
        assert decoded.format == "JPEG"
        assert max(decoded.size) == 1024

    screencap_calls = [(c, kw) for c, kw in recorded_calls if "screencap" in c]
    assert len(screencap_calls) == 1
    sc_argv, sc_kw = screencap_calls[0]
    assert sc_argv[1:3] == ["-s", "SERIAL_A"]
    assert sc_argv[3:] == ["exec-out", "screencap", "-p"]
    assert sc_kw.get("text") is False


def test_ui_summary_compacts_nodes_without_raw_xml(monkeypatch):
    """`ui_summary` drops invisible/zero-size/layout nodes and returns compact JSON with centers."""
    fake_adb = Path("/mock/adb")
    monkeypatch.setattr("adapt_adb.mcp.tools.resolve_adb", lambda: fake_adb)

    def fake_run(argv, **kwargs):
        if "devices" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=SAMPLE_DEVICES_ONE, stderr="")
        if "uiautomator" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=SAMPLE_UI_XML, stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    summary = tool_ui_summary()
    assert "nodes" in summary
    assert len(summary["nodes"]) == 1
    node = summary["nodes"][0]
    assert node == {
        "text": "Checkout",
        "content_desc": "",
        "resource_id": "com.example.app:id/btn_checkout",
        "class": "android.widget.Button",
        "clickable": True,
        "bounds": [138, 900, 400, 980],
        "center": [269, 940],
    }


def test_input_and_install_tools_always_pass_dash_s(monkeypatch):
    """Verify tap, swipe, key, type, install_apk, and device_info pass `-s SERIAL_A`."""
    fake_adb = Path("/mock/adb")
    monkeypatch.setattr("adapt_adb.mcp.tools.resolve_adb", lambda: fake_adb)

    recorded_calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        if "devices" not in argv and "version" not in argv:
            recorded_calls.append(list(argv))
        if "devices" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=SAMPLE_DEVICES_ONE, stderr="")
        if "ro.product.model" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="Example Phone\n", stderr="")
        if "ro.build.version.sdk" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="34\n", stderr="")
        if "ro.build.version.release" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="14\n", stderr="")
        if "size" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="Physical size: 1080x2400\n", stderr="")
        if "density" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="Physical density: 420\n", stderr="")
        if "dumpsys" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="mShowingLockscreen=false\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="Success\n", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    info = tool_device_info()
    assert info["device"] == "SERIAL_A"
    assert info["sdk"] == 34
    assert info["screen"] == {"width": 1080, "height": 2400, "density": 420}
    assert info["lock"] == "unlocked"

    assert tool_tap(269, 940, device="SERIAL_A") == {"ok": True, "tapped": [269, 940]}
    assert tool_swipe(100, 500, 100, 100, duration_ms=250, device="SERIAL_A") == {"ok": True}
    assert tool_key("BACK", device="SERIAL_A") == {"ok": True, "key": "BACK"}
    assert tool_type("hello world", device="SERIAL_A") == {"ok": True, "n": 11}
    assert tool_install_apk("/tmp/app-debug.apk", reinstall=True, device="SERIAL_A") == {
        "ok": True,
        "apk": "app-debug.apk",
        "reinstall": True,
    }

    for call in recorded_calls:
        assert call[1:3] == ["-s", "SERIAL_A"]

    install_calls = [c for c in recorded_calls if "install" in c]
    assert len(install_calls) == 1
    assert "-r" in install_calls[0]

    type_calls = [c for c in recorded_calls if "text" in c]
    assert len(type_calls) == 1
    assert "hello%sworld" in type_calls[0]


def test_fake_adb_script_integration(tmp_path, monkeypatch):
    """Test MCP tools against a temporary fake adb script on disk via ADAPT_ADB."""
    if sys.platform == "win32":
        script = tmp_path / "adb.cmd"
        script.write_text(
            "@echo off\r\n"
            "if \"%1\"==\"version\" (\r\n"
            "  echo Android Debug Bridge version 1.0.41\r\n"
            "  echo Version 35.0.2-123456\r\n"
            "  exit /b 0\r\n"
            ")\r\n"
            "if \"%1\"==\"devices\" (\r\n"
            "  echo List of devices attached\r\n"
                "  echo SERIAL_A\tdevice usb:1-1 product:example model:Example_Phone\r\n"
            "  exit /b 0\r\n"
            ")\r\n"
            "exit /b 0\r\n",
            encoding="utf-8",
        )
    else:
        script = tmp_path / "adb"
        script.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "version" ]; then\n'
            '  echo "Android Debug Bridge version 1.0.41"\n'
            '  echo "Version 35.0.2-123456"\n'
            "  exit 0\n"
            "fi\n"
            'if [ "$1" = "devices" ]; then\n'
            '  echo "List of devices attached"\n'
                '  printf "SERIAL_A\\tdevice usb:1-1 product:example model:Example_Phone\\n"\n'
            "  exit 0\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        script.chmod(0o755)

    monkeypatch.setenv("ADAPT_ADB", str(script))
    from adapt_adb.doctor import DoctorEnv, Server5037State

    monkeypatch.setattr(
        "adapt_adb.cli._ACTIVE_DOCTOR_ENV",
        DoctorEnv(
            host_os="windows" if sys.platform == "win32" else "debian",
            server_state=Server5037State(listening=False),
            windows_pnp_devices=[],
        ),
    )

    doc = tool_doctor()
    assert doc["adb_version"] == "35.0.2"
    assert doc["daemon"] == "ok"
    assert doc["devices"] == [{"device": "SERIAL_A", "model": "Example Phone", "state": "device"}]

    status = tool_setup_status()
    assert status["installed"] is True
    assert status["would_download"] is True


def test_doctor_exit_3_skips_device_listing_and_maps_daemon(tmp_path, monkeypatch):
    """When port 5037 check returns exit 3, tool_doctor() returns devices=[] and never calls devices -l."""
    from adapt_adb.doctor import DoctorEnv, Server5037State

    home = tmp_path / "home"
    pt_dir = home / ".local" / "share" / "android" / "platform-tools"
    adb_bin = pt_dir / "adb"

    def _forbid_list_devices(*args, **kwargs):
        raise AssertionError("list_connected_devices must not be called when 5037 is exit 3")

    monkeypatch.setattr("adapt_adb.mcp.tools.list_connected_devices", _forbid_list_devices)

    orig_run = subprocess.run

    def _guard_subprocess_run(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and "devices" in argv:
            raise AssertionError(f"subprocess.run for devices must not be called: {argv}")
        return orig_run(argv, *args, **kwargs)

    monkeypatch.setattr("subprocess.run", _guard_subprocess_run)

    # Case 1: root-owned /tmp/5037 -> daemon == "root_owned", devices == []
    root_env = DoctorEnv(
        host_os="debian",
        arch="x86_64",
        uid_is_root=False,
        home_dir=home,
        environ={"PATH": str(pt_dir)},
        which_adb=adb_bin,
        which_set=True,
        path_executables=[adb_bin],
        existing_paths={adb_bin, pt_dir},
        source_properties={pt_dir / "source.properties": "Pkg.Revision=35.0.2\n"},
        version_outputs={
            adb_bin: (
                "Android Debug Bridge version 1.0.41\n"
                "Version 35.0.2-12147458\n"
                f"Installed as {adb_bin}\n"
            )
        },
        server_state=Server5037State(
            listening=True,
            owner="root",
            owner_uid=0,
            owner_is_root_or_system=True,
            owner_matches_current_user=False,
            tmp_5037_exists=True,
            tmp_5037_owner_is_root=True,
        ),
        devices_l_output=SAMPLE_DEVICES_ONE,
    )
    monkeypatch.setattr("adapt_adb.cli._ACTIVE_DOCTOR_ENV", root_env)

    res_root = tool_doctor()
    assert res_root["daemon"] == "root_owned"
    assert res_root["devices"] == []
    assert root_env.devices_l_invoked == []

    # Case 2: version mismatch on 5037 -> daemon == "version_mismatch", devices == []
    mismatch_env = DoctorEnv(
        host_os="debian",
        arch="x86_64",
        uid_is_root=False,
        home_dir=home,
        environ={"PATH": str(pt_dir)},
        which_adb=adb_bin,
        which_set=True,
        path_executables=[adb_bin],
        existing_paths={adb_bin, pt_dir},
        source_properties={pt_dir / "source.properties": "Pkg.Revision=35.0.2\n"},
        version_outputs={
            adb_bin: (
                "Android Debug Bridge version 1.0.41\n"
                "Version 35.0.2-12147458\n"
                f"Installed as {adb_bin}\n"
            )
        },
        server_state=Server5037State(
            listening=True,
            owner_matches_current_user=True,
            server_version_raw=(
                "Android Debug Bridge version 1.0.41\n"
                "Version 33.0.3-8952118\n"
            ),
        ),
        devices_l_output=SAMPLE_DEVICES_ONE,
    )
    monkeypatch.setattr("adapt_adb.cli._ACTIVE_DOCTOR_ENV", mismatch_env)

    res_mismatch = tool_doctor()
    assert res_mismatch["daemon"] == "version_mismatch"
    assert res_mismatch["devices"] == []
    assert mismatch_env.devices_l_invoked == []


def test_forbidden_commands_rejected():
    """Verify safety policy rejects kill-server, sudo, uninstall, pm clear, and streaming logcat."""
    for bad_argv in [
        ["adb", "kill-server"],
        ["sudo", "adb", "devices"],
        ["adb", "-s", "SERIAL_A", "uninstall", "com.example.app"],
        ["adb", "-s", "SERIAL_A", "shell", "pm", "clear", "com.example.app"],
        ["adb", "-s", "SERIAL_A", "logcat"],
    ]:
        try:
            validate_adb_args(bad_argv)
        except ForbiddenCommandError:
            pass
        else:
            raise AssertionError(f"Expected ForbiddenCommandError for {bad_argv}")



@pytest.mark.parametrize("read_only", [False, True])
def test_ui_dispatch_read_only_fallback_commands(read_only, monkeypatch):
    from adapt_adb.mcp.access import AccessPolicy
    from adapt_adb.mcp import tools

    monkeypatch.setattr(tools, "resolve_adb", lambda: Path("adb"))
    monkeypatch.setattr(tools, "resolve_target_device", lambda *a: ("A", None, []))
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        assert kwargs["encoding"] == "utf-8" and kwargs["errors"] == "replace"
        return subprocess.CompletedProcess(argv, 1 if argv[-1] == "/dev/tty" else 0,
                                           SAMPLE_UI_XML if "cat" in argv else "", "")
    monkeypatch.setattr(subprocess, "run", run)
    result = dispatch_tool_call("ui_summary", {"device": "A"}, policy=AccessPolicy(read_only=read_only))
    data = json.loads(result.content[0].text)
    if read_only:
        assert result.is_error
        assert data["error"] == "ui_summary_unavailable_read_only"
        assert len(calls) == 1
        assert "content_trust" not in data
    else:
        assert not result.is_error and data["nodes"]
        assert len(calls) == 4
        assert calls[1][3:6] == ["shell", "uiautomator", "dump"]
        assert calls[2][3:5] == ["exec-out", "cat"]
        assert calls[3][3:6] == ["shell", "rm", "-f"]
        assert calls[1][-1] == calls[2][-1] == calls[3][-1]


@pytest.mark.parametrize("tool", ["ui_summary", "screenshot"])
@pytest.mark.parametrize("gate", ["guard", "preflight"])
def test_render_tools_refuse_before_spawn(tool, gate, monkeypatch):
    from adapt_adb.mcp import tools
    from adapt_adb.doctor import UnsafeAdbServerError

    monkeypatch.setattr(tools, "resolve_adb", lambda: Path("adb"))
    monkeypatch.setattr(tools, "resolve_target_device", lambda *a: ("A", None, []))
    seen = []
    def refuse(*a):
        seen.append(a)
        if gate == "guard":
            raise ForbiddenCommandError("refused")
        raise UnsafeAdbServerError("refused")
    if gate == "guard":
        monkeypatch.setattr(tools, "validate_adb_args", refuse)
        monkeypatch.setattr("adapt_adb.doctor.preflight_adb_server", lambda *a: pytest.fail("Preflight after guard refusal"))
    else:
        monkeypatch.setattr("adapt_adb.doctor.preflight_adb_server", refuse)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("Spawned despite refusal"))
    result = dispatch_tool_call(tool, {"device": "A"})
    assert result.is_error and len(seen) == 1
    args = seen[0][-1]
    assert args[:2] == ["-s", "A"]
    assert args[2:] == (["exec-out", "uiautomator", "dump", "/dev/tty"] if tool == "ui_summary" else ["exec-out", "screencap", "-p"])


def test_run_adb_utf8_and_binary_modes(monkeypatch):
    from adapt_adb.mcp.tools import run_adb

    def run(argv, **kwargs):
        if kwargs["text"]:
            assert kwargs["encoding"] == "utf-8" and kwargs["errors"] == "replace"
            value = b"caf\xc3\xa9\xff".decode(kwargs["encoding"], kwargs["errors"])
        else:
            assert "encoding" not in kwargs and "errors" not in kwargs
            value = b"\xff"
        return subprocess.CompletedProcess(argv, 0, value, "")
    monkeypatch.setattr(subprocess, "run", run)
    assert run_adb(Path("adb"), ["devices"]).stdout == "caf\u00e9\ufffd"
    assert run_adb(Path("adb"), ["exec-out", "screencap", "-p"], binary=True).stdout == b"\xff"


def test_screenshot_error_utf8_binary_capture():
    from adapt_adb.render.screenshot import capture_screenshot_jpeg

    def run(argv, **kwargs):
        assert kwargs["text"] is False
        return subprocess.CompletedProcess(argv, 1, b"", b"caf\xc3\xa9\xff")
    with pytest.raises(RuntimeError, match="caf\u00e9\ufffd"):
        capture_screenshot_jpeg(Path("adb"), "A", runner=run)


@pytest.mark.parametrize("name,kwargs", [
    ("tap", {"x": 1, "y": 2, "device": "A"}), ("swipe", {"x1": 1, "y1": 2, "x2": 3, "y2": 4, "device": "A"}),
    ("key", {"key": "BACK", "device": "A"}), ("install_apk", {"apk": "app.apk", "device": "A"}), ("logcat_tail", {}),
])
@pytest.mark.parametrize("stream", ["stderr", "stdout"])
def test_subprocess_errors_bounded(name, kwargs, stream, monkeypatch):
    from adapt_adb.mcp import tools

    monkeypatch.setattr(tools, "resolve_adb", lambda: Path("adb"))
    monkeypatch.setattr(tools, "resolve_target_device", lambda *a: ("A", None, []))
    def run(*a, **k):
        return subprocess.CompletedProcess([], 1, **{stream: " x" + "z" * 3000 + " ",
                                                     "stdout" if stream == "stderr" else "stderr": ""})
    monkeypatch.setattr(tools, "run_adb", run)
    data = getattr(tools, "tool_" + name)(**kwargs)
    assert data["message"] == "x" + "z" * 1999 + " [truncated 1001 chars]"


def test_long_logcat_line_bounded(monkeypatch):
    from adapt_adb.mcp import tools

    monkeypatch.setattr(tools, "resolve_adb", lambda: Path("adb"))
    monkeypatch.setattr(tools, "resolve_target_device", lambda *a: ("A", None, []))
    monkeypatch.setattr(tools, "run_adb", lambda *a, **k: subprocess.CompletedProcess([], 0, "z" * 3000 + "\nshort", ""))
    assert tools.tool_logcat_tail()["lines"] == ["z" * 2000 + " [truncated 1000 chars]", "short"]


@pytest.mark.parametrize("size,density,expected", [
    ("Physical size: 1080x2400\nOverride size: 720x1600", "Physical density: 420\nOverride density: 300", (720, 1600, 300)),
    ("Override size: 720x1600\nPhysical size: 1080x2400", "Override density: 300\nPhysical density: 420", (720, 1600, 300)),
    ("Physical size: 1080x2400", "Physical density: 420", (1080, 2400, 420)),
    ("720x1600", "300", (720, 1600, 300)),
])
def test_device_info_effective_override(size, density, expected, monkeypatch):
    from adapt_adb.mcp import tools

    monkeypatch.setattr(tools, "resolve_adb", lambda: Path("adb"))
    monkeypatch.setattr(tools, "resolve_target_device", lambda *a: ("A", None, []))
    def run(path, args, **kwargs):
        value = size if args[-1] == "size" else density if args[-1] == "density" else "34"
        return subprocess.CompletedProcess([], 0, value, "")
    monkeypatch.setattr(tools, "run_adb", run)
    screen = tools.tool_device_info()["screen"]
    assert (screen["width"], screen["height"], screen["density"]) == expected
