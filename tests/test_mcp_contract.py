"""Contract tests for ADAPT v1 MCP server & tools using fakes (no physical phones)."""

from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest
from PIL import Image
from typer.testing import CliRunner

from adapt_adb.banner import BANNER
from adapt_adb.cli import app
from adapt_adb.mcp.server import create_server, dispatch_tool_call
from adapt_adb.mcp.tools import ForbiddenCommandError, tool_logcat_tail, validate_adb_args

runner = CliRunner()


@pytest.fixture(autouse=True)
def _stub_server_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tool contract tests inject ADB output; review regressions cover host preflight."""
    monkeypatch.setattr("adapt_adb.doctor.preflight_adb_server", lambda *a, **k: None)

TWO_DEVICES_OUTPUT = (
    "List of devices attached\n"
    "SERIAL_A\tdevice usb:1-1 product:example model:Example_Phone device:example transport_id:1\n"
    "SERIAL_B\tdevice usb:1-2 product:example_tab model:Example_Tablet device:example_tab transport_id:2\n"
)

ONE_DEVICE_OUTPUT = (
    "List of devices attached\n"
    "SERIAL_A\tdevice usb:1-1 product:example model:Example_Phone device:example transport_id:1\n"
)


def _make_png_bytes(width: int = 2000, height: int = 3000) -> bytes:
    img = Image.new("RGBA", (width, height), color=(30, 90, 180, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.parametrize(
    ("tool_name", "tool_args"),
    [
        ("device_info", {}),
        ("screenshot", {}),
        ("ui_summary", {}),
        ("tap", {"x": 100, "y": 200}),
        ("swipe", {"x1": 10, "y1": 20, "x2": 30, "y2": 40}),
        ("key", {"key": "BACK"}),
        ("type", {"text": "hello"}),
        ("install_apk", {"apk": "app-debug.apk"}),
        ("logcat_tail", {}),
    ],
)
def test_two_devices_without_serial_returns_need_device(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    tool_args: dict[str, Any],
) -> None:
    """When >1 `device`-state transports exist and `device` is omitted, return `need_device`."""
    fake_adb = Path("/mock/adb")
    monkeypatch.setattr("adapt_adb.mcp.tools.resolve_adb", lambda: fake_adb)

    recorded_calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        recorded_calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout=TWO_DEVICES_OUTPUT, stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    res = dispatch_tool_call(tool_name, tool_args)
    assert res.is_error is True

    payload = json.loads(res.content[0].text)
    assert payload["error"] == "need_device"
    assert payload["devices"] == [
        {"device": "SERIAL_A", "model": "Example Phone", "state": "device"},
        {"device": "SERIAL_B", "model": "Example Tablet", "state": "device"},
    ]
    # Short identity is authoritative; long output only enriches metadata.
    assert len(recorded_calls) == 2
    assert recorded_calls[0][1:] == ["devices"]
    assert recorded_calls[1][1:] == ["devices", "-l"]


def test_two_devices_need_device_over_mcp_client_and_explicit_serial_targets_b(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Over MCP Client: omitting `device` yields `need_device`; passing `SERIAL_B` uses `-s SERIAL_B`."""
    from mcp import Client

    fake_adb = Path("/mock/adb")
    monkeypatch.setattr("adapt_adb.mcp.tools.resolve_adb", lambda: fake_adb)

    recorded_calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        recorded_calls.append(list(argv))
        if "devices" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=TWO_DEVICES_OUTPUT, stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    server = create_server()

    async def _exercise() -> None:
        async with Client(server) as client:
            err_res = await client.call_tool("tap", {"x": 50, "y": 60})
            assert err_res.is_error is True
            err_payload = json.loads(err_res.content[0].text)
            assert err_payload["error"] == "need_device"
            assert [d["device"] for d in err_payload["devices"]] == ["SERIAL_A", "SERIAL_B"]

            ok_res = await client.call_tool("tap", {"x": 50, "y": 60, "device": "SERIAL_B"})
            assert ok_res.is_error is False
            assert json.loads(ok_res.content[0].text) == {"ok": True, "tapped": [50, 60]}

    anyio.run(_exercise)

    tap_calls = [c for c in recorded_calls if "tap" in c]
    assert len(tap_calls) == 1
    assert tap_calls[0][1:] == ["-s", "SERIAL_B", "shell", "input", "tap", "50", "60"]


def test_logcat_uses_dash_d_and_clamps_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    """`logcat_tail` always passes `-d` (never streams), clamps lines to [1, 500], and tails from end."""
    fake_adb = Path("/mock/adb")
    monkeypatch.setattr("adapt_adb.mcp.tools.resolve_adb", lambda: fake_adb)

    recorded_calls: list[list[str]] = []
    sample_stdout = "\n".join(
        ["--------- beginning of main"]
        + [f"11-12 10:00:{i:02d}.000  100  200 W Tag: msg {i}" for i in range(10)]
    )

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        recorded_calls.append(list(argv))
        if "devices" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=ONE_DEVICE_OUTPUT, stderr="")
        if "logcat" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=sample_stdout, stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    invalid = dispatch_tool_call("logcat_tail", {"lines": 999, "tag": "Tag", "level": "W"})
    assert invalid.is_error is True
    assert json.loads(invalid.content[0].text)["error"] == "invalid_params"

    res = dispatch_tool_call("logcat_tail", {"lines": 500, "tag": "Tag", "level": "W"})
    assert res.is_error is False
    payload = json.loads(res.content[0].text)
    assert len(payload["lines"]) == 10
    assert payload["lines"][0].endswith("msg 0")
    assert payload["lines"][-1].endswith("msg 9")

    tail_res = tool_logcat_tail(lines=3)
    assert len(tail_res["lines"]) == 3
    assert [ln.rsplit(" ", 1)[-1] for ln in tail_res["lines"]] == ["7", "8", "9"]

    logcat_calls = [c for c in recorded_calls if "logcat" in c]
    assert len(logcat_calls) == 2
    assert logcat_calls[0][1:] == ["-s", "SERIAL_A", "logcat", "-d", "-t", "500", "-s", "Tag:W"]
    assert logcat_calls[1][1:] == ["-s", "SERIAL_A", "logcat", "-d", "-t", "3"]

    with pytest.raises(ForbiddenCommandError):
        validate_adb_args(["adb", "-s", "SERIAL_A", "logcat"])


def test_screenshot_uses_exec_out_binary_and_returns_jpeg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`screenshot` invokes `exec-out screencap -p` in binary mode and scales PNG to JPEG."""
    fake_adb = Path("/mock/adb")
    monkeypatch.setattr("adapt_adb.mcp.tools.resolve_adb", lambda: fake_adb)

    raw_png = _make_png_bytes(width=2000, height=3000)
    recorded_calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        recorded_calls.append((list(argv), dict(kwargs)))
        if "devices" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=ONE_DEVICE_OUTPUT, stderr="")
        if "screencap" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=raw_png, stderr=b"")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    invalid = dispatch_tool_call("screenshot", {"max_dimension": 4096, "quality": 80})
    assert invalid.is_error is True
    assert json.loads(invalid.content[0].text)["error"] == "invalid_params"

    res = dispatch_tool_call("screenshot", {"max_dimension": 1280, "quality": 80})
    assert res.is_error is False
    assert len(res.content) == 2

    img_block, txt_block = res.content
    assert img_block.type == "image"
    assert img_block.mime_type == "image/jpeg"

    meta = json.loads(txt_block.text)
    assert meta["max_dimension"] == 1280
    assert meta["height"] == 1280
    assert meta["width"] == round(2000 * (1280 / 3000))

    decoded_bytes = base64.b64decode(img_block.data)
    with Image.open(io.BytesIO(decoded_bytes)) as out_img:
        assert out_img.format == "JPEG"
        assert max(out_img.size) == 1280

    screencap_calls = [(argv, kw) for argv, kw in recorded_calls if "screencap" in argv]
    assert len(screencap_calls) == 1
    sc_argv, sc_kw = screencap_calls[0]
    assert sc_argv[1:] == ["-s", "SERIAL_A", "exec-out", "screencap", "-p"]
    assert sc_kw.get("text") is False


def test_install_apk_uses_dash_r_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """`install_apk` defaults `reinstall=True` (`adb install -r`) and keeps data."""
    fake_adb = Path("/mock/adb")
    monkeypatch.setattr("adapt_adb.mcp.tools.resolve_adb", lambda: fake_adb)

    recorded_calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        recorded_calls.append(list(argv))
        if "devices" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=ONE_DEVICE_OUTPUT, stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="Success\n", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    res = dispatch_tool_call("install_apk", {"apk": "/tmp/build/app-debug.apk", "device": "SERIAL_A"})
    assert res.is_error is False
    assert json.loads(res.content[0].text) == {
        "ok": True,
        "apk": "app-debug.apk",
        "reinstall": True,
    }

    res_grant = dispatch_tool_call(
        "install_apk", {"apk": "/tmp/build/app-debug.apk", "grant": True, "device": "SERIAL_A"}
    )
    assert res_grant.is_error is False

    install_calls = [c for c in recorded_calls if "install" in c]
    assert len(install_calls) == 2
    assert install_calls[0][1:] == ["-s", "SERIAL_A", "install", "-r", "/tmp/build/app-debug.apk"]
    assert install_calls[1][1:] == ["-s", "SERIAL_A", "install", "-r", "-g", "/tmp/build/app-debug.apk"]

    with pytest.raises(ForbiddenCommandError):
        validate_adb_args(["adb", "-s", "SERIAL_A", "uninstall", "com.example.app"])
    with pytest.raises(ForbiddenCommandError):
        validate_adb_args(["adb", "-s", "SERIAL_A", "shell", "pm", "clear", "com.example.app"])


def test_doctor_returns_compact_mcp_v1_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """`dispatch_tool_call('doctor', {})` returns MCP-V1 compact keys, not the full doctor.md report."""
    fake_adb = Path("/mock/adb")
    monkeypatch.setattr("adapt_adb.mcp.tools.resolve_adb", lambda: fake_adb)

    def fake_run_doctor_checks(env: Any = None, *, command_name: str = "doctor") -> dict[str, Any]:
        return {
            "schema_version": "1",
            "command": command_name,
            "host": {"os": "debian", "arch": "x86_64", "uid_is_root": False, "seat": "local"},
            "exit_code": 0,
            "status": "pass",
            "toolchain": {
                "adb_path": str(fake_adb),
                "platform_tools_version": "35.0.2-12147458",
                "origin": "google_official",
                "android_home_present": False,
            },
            "checks": [
                {
                    "id": "adb_server_5037",
                    "status": "pass",
                    "exit_code": 0,
                    "summary": "Port 5037 is idle.",
                    "details": {"listening": False, "version_mismatch": False},
                    "remediation": None,
                },
                {
                    "id": "debian_usb_permissions",
                    "status": "pass",
                    "exit_code": 0,
                    "summary": "Debian udev rules and USB permissions are configured.",
                    "details": {"has_udev_rules": True, "in_plugdev_group": True},
                    "remediation": None,
                },
            ],
            "devices": {"total": 1, "by_state": {"device": 1}},
        }

    monkeypatch.setattr("adapt_adb.doctor.run_doctor_checks", fake_run_doctor_checks)
    monkeypatch.setattr(
        "adapt_adb.mcp.tools.list_connected_devices",
        lambda p: [{"device": "SERIAL_A", "state": "device", "model": "Example Phone"}],
    )

    res = dispatch_tool_call("doctor", {})
    assert res.is_error is False
    payload = json.loads(res.content[0].text)
    assert set(payload.keys()) == {
        "adb_path",
        "adb_version",
        "managed",
        "daemon",
        "daemon_reason",
        "os",
        "driver_hint",
        "udev",
        "plugdev",
        "devices",
    }
    assert "schema_version" not in payload
    assert "checks" not in payload
    assert payload["adb_version"] == "35.0.2"
    assert payload["daemon"] == "ok"
    assert payload["daemon_reason"] is None
    assert payload["os"] == "linux"
    assert payload["udev"] == "present"
    assert payload["plugdev"] is True
    assert payload["devices"] == [{"device": "SERIAL_A", "model": "Example Phone", "state": "device"}]


def test_mcp_stdout_has_no_banner_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """`adapt mcp` writes only MCP JSON-RPC to stdout and emits zero banner text even if TTY/env enabled."""
    monkeypatch.setenv("ADAPT_BANNER", "1")
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)

    cli_result = runner.invoke(app, ["mcp"])
    assert cli_result.exit_code == 0
    assert cli_result.stdout == ""
    assert "Android Device Agent Provisioning Toolkit" not in cli_result.stderr
    assert "hide this art" not in cli_result.stderr
    assert BANNER.strip() not in cli_result.stderr

    # Also verify a real stdio subprocess running `adapt_adb.cli mcp` emits strictly JSON-RPC on stdout.
    repo_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    src_dir = str(repo_root / "src")
    env["PYTHONPATH"] = src_dir + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["ADAPT_BANNER"] = "1"
    env.pop("ADAPT_NO_BANNER", None)

    init_req = (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "contract-test", "version": "0.1.0"},
                },
            }
        )
        + "\n"
    )

    proc = subprocess.run(
        [sys.executable, "-m", "adapt_adb.cli", "mcp"],
        input=init_req,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert proc.returncode == 0
    assert "Android Device Agent Provisioning Toolkit" not in proc.stdout
    assert "Android Device Agent Provisioning Toolkit" not in proc.stderr
    assert "hide this art" not in proc.stdout

    stdout_lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(stdout_lines) >= 1
    for line in stdout_lines:
        parsed = json.loads(line)
        assert parsed.get("jsonrpc") == "2.0"



def _bound_cases():
    from adapt_adb.mcp.tools import TOOL_SCHEMAS

    for tool in TOOL_SCHEMAS:
        schema = tool["inputSchema"]
        base = {}
        for key in schema.get("required", []):
            prop = schema["properties"][key]
            base[key] = prop.get("enum", [None])[0] if "enum" in prop else (
                "com.example.app" if key == "package" else "A" if prop["type"] == "string" else prop.get("minimum", 0)
            )
        for key, prop in schema["properties"].items():
            if prop["type"] == "integer":
                assert "minimum" in prop and "maximum" in prop
                for bound, delta in (("minimum", -1), ("maximum", 1)):
                    yield tool["name"], base, key, prop[bound], prop[bound] + delta
            elif prop["type"] == "string":
                assert "maxLength" in prop
                if "enum" in prop:
                    continue
                yield tool["name"], base, key, "a" * prop["maxLength"], "a" * (prop["maxLength"] + 1)
                if prop.get("minLength", 0):
                    yield tool["name"], base, key, "a" * prop["minLength"], ""


@pytest.mark.parametrize("name,base,key,valid,invalid", list(_bound_cases()))
def test_all_advertised_bounds_enforced(name, base, key, valid, invalid, monkeypatch):
    from adapt_adb.mcp import server, tools

    # Keep semantic/device validation out of this schema boundary test.
    for attr in dir(tools):
        if attr.startswith("tool_"):
            monkeypatch.setattr(tools, attr, lambda *a, **k: {"ok": True})
    monkeypatch.setattr(tools, "tool_screenshot", lambda **k: ("AA==", {}))
    args = {**base, key: invalid}
    result = dispatch_tool_call(name, args)
    assert result.is_error
    assert json.loads(result.content[0].text)["error"] == "invalid_params"
    args[key] = valid
    assert server._arguments_match_schema(name, args)
    assert not dispatch_tool_call(name, args).is_error


def test_default_string_limit_enforced_and_advertised(monkeypatch):
    from adapt_adb.mcp import server

    monkeypatch.setitem(server._TOOL_SCHEMAS, "type", {
        "properties": {"text": {"type": "string"}}, "required": ["text"]})
    assert server._arguments_match_schema("type", {"text": "a" * 4096})
    result = dispatch_tool_call("type", {"text": "a" * 4097})
    assert json.loads(result.content[0].text)["error"] == "invalid_params"


@pytest.mark.parametrize("name", [
    "ui_summary", "logcat_tail", "app_logs", "diagnostic_logs", "device_info",
    "dex_inspect", "dex_displays", "app_validate", "debug_app_inspect",
    "developer_options_inspect", "android_capabilities", "list_devices",
    "android_versions", "tool_catalog", "logging_guide", "build_modes", "setup_status", "doctor",
    "screenshot",
])
@pytest.mark.parametrize("failed", [False, True])
def test_content_trust_on_device_success_only(name, failed, monkeypatch):
    from adapt_adb.mcp import tools
    from adapt_adb.mcp.access import DEVICE_TEXT_TOOLS

    data = {"error": "mock_failure"} if failed else {"device": "A", "devices": [{"device": "A"}]}
    for attr in dir(tools):
        if attr.startswith("tool_"):
            monkeypatch.setattr(tools, attr, lambda *a, **k: data)
    monkeypatch.setattr(tools, "tool_screenshot", lambda **k: (None if failed else "AA==", data))
    args = {"device": "A", "package": "com.example.app", "topic": "window"}
    from adapt_adb.mcp.server import _TOOL_SCHEMAS
    args = {k: v for k, v in args.items() if k in _TOOL_SCHEMAS[name]["properties"]}
    result = dispatch_tool_call(name, args)
    payload = json.loads(result.content[-1].text)
    expected = not failed and (name in DEVICE_TEXT_TOOLS or name == "screenshot")
    assert ("content_trust" in payload) == expected
    assert ("content_notice" in payload) == expected
    if expected:
        assert payload["content_trust"] == "untrusted_device_data"
        assert payload["content_notice"]
    assert result.is_error == failed


def test_worker_dispatch_serializes_and_survives_new_loops(monkeypatch):
    import threading
    from adapt_adb.mcp import server
    from adapt_adb.mcp.access import AccessPolicy

    real_dispatch = server.dispatch_tool_call
    monkeypatch.setattr(server.mcp_tools, "tool_android_versions", lambda: {"versions": []})
    expected = real_dispatch("android_versions", {}).model_dump()
    main_thread = threading.get_ident()
    active = 0
    peak = 0
    entered = threading.Event()
    release = threading.Event()
    count = 0

    def dispatch(*a, **k):
        nonlocal active, peak, count
        assert threading.get_ident() != main_thread
        active += 1
        peak = max(peak, active)
        count += 1
        entered.set()
        assert release.wait(5)
        try:
            return real_dispatch(*a, **k)
        finally:
            active -= 1

    monkeypatch.setattr(server, "dispatch_tool_call", dispatch)

    async def exercise():
        entered.clear()
        release.clear()
        results = []
        async def call():
            results.append(await server._dispatch_in_worker("android_versions", {}, AccessPolicy()))
        async with anyio.create_task_group() as group:
            group.start_soon(call)
            group.start_soon(call)
            with anyio.fail_after(5):
                while not entered.is_set():
                    await anyio.sleep(0.001)
            # The loop remains responsive while the first worker is blocked.
            await anyio.sleep(0.02)
            assert active == 1
            release.set()
        assert [r.model_dump() for r in results] == [expected, expected]

    anyio.run(exercise)
    anyio.run(exercise)
    assert count == 4 and peak == 1


def test_modern_handler_uses_worker_and_preserves_errors(monkeypatch):
    from types import SimpleNamespace
    from adapt_adb.mcp import server
    import threading

    class ModernServer:
        def __init__(self, name, **kwargs):
            self.handler = kwargs["on_call_tool"]
    _, types, _ = server._require_mcp_sdk()
    monkeypatch.setattr(server, "_require_mcp_sdk", lambda: (anyio, types, SimpleNamespace(Server=ModernServer)))
    main_thread = threading.get_ident()
    original = server.dispatch_tool_call
    def checked(*a, **k):
        assert threading.get_ident() != main_thread
        return original(*a, **k)
    monkeypatch.setattr(server, "dispatch_tool_call", checked)
    instance = server.create_server()
    async def exercise():
        result = await instance.handler(None, SimpleNamespace(name="tap", arguments={}))
        assert result.is_error
        assert result.model_dump(by_alias=True)["isError"] is True
    anyio.run(exercise)
