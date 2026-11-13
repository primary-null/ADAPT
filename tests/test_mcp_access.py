"""Session restrictions are enforced at dispatch, not just discovery."""

import json

import pytest
from typer.testing import CliRunner

from adapt_adb.cli import app
from adapt_adb.mcp.access import AccessPolicy
from adapt_adb.mcp.server import create_server, dispatch_tool_call, get_mcp_tools


def payload(result):
    return json.loads(result.content[0].text)


def test_read_only_hides_and_blocks_mutating_call_before_execution(monkeypatch):
    policy = AccessPolicy(read_only=True)
    assert not {"tap", "swipe", "key", "type", "install_apk"} & {t.name for t in get_mcp_tools(policy)}
    monkeypatch.setattr("adapt_adb.mcp.tools.tool_tap", lambda **k: pytest.fail("Mutation executed"))
    result = dispatch_tool_call("tap", {"x": 1, "y": 2}, policy=policy)
    assert result.is_error and payload(result)["error"] == "access_denied"


def test_target_restriction_requires_explicit_exact_transport(monkeypatch):
    allowed = "adb-Phone (2)._adb-tls-connect._tcp"
    policy = AccessPolicy(allowed_devices=(allowed,))
    calls = []
    monkeypatch.setattr("adapt_adb.mcp.tools.tool_android_capabilities", lambda **k: calls.append(k) or {"ok": True})
    for target in ("other", "", allowed + " "):
        assert payload(dispatch_tool_call("android_capabilities", {"device": target}, policy=policy))["error"] == "access_denied"
    assert not calls
    assert payload(dispatch_tool_call("android_capabilities", {"device": allowed}, policy=policy))["ok"]
    assert calls == [{"device": allowed}]


def test_restricted_discovery_and_results_do_not_change_default_schema(monkeypatch):
    policy = AccessPolicy(allowed_devices=("A",))
    schema = next(t.model_dump(by_alias=True)["inputSchema"] for t in get_mcp_tools(policy) if t.name == "screenshot")
    assert "device" in schema["required"] and schema["properties"]["device"]["enum"] == ["A"]
    default = next(t.model_dump(by_alias=True)["inputSchema"] for t in get_mcp_tools() if t.name == "screenshot")
    assert "enum" not in default["properties"]["device"]
    monkeypatch.setattr("adapt_adb.mcp.tools.tool_list_devices", lambda: {"devices": [{"device": "A"}, {"device": "B"}]})
    assert payload(dispatch_tool_call("list_devices", {}, policy=policy))["devices"] == [{"device": "A"}]
    assert payload(dispatch_tool_call("doctor", {}, policy=policy))["error"] == "access_denied"


def test_offline_tools_do_not_resolve_adb(monkeypatch):
    monkeypatch.setattr("adapt_adb.mcp.tools.resolve_adb", lambda: pytest.fail("ADB resolved"))
    assert "versions" in payload(dispatch_tool_call("android_versions", {}))
    assert payload(dispatch_tool_call("tool_catalog", {"goal": "mirroring"}))["goal"] == "mirroring"


def test_cli_starts_server_with_restrictions_without_banner(monkeypatch):
    calls = []
    monkeypatch.setattr("adapt_adb.mcp.server.run_stdio", lambda **k: calls.append(k))
    result = CliRunner().invoke(app, ["mcp", "--read-only", "--device", "A"])
    assert result.exit_code == 0 and result.stdout == "" and result.stderr == ""
    assert calls[0]["policy"] == AccessPolicy(read_only=True, allowed_devices=("A",))


def test_policy_enforced_over_official_protocol_client(monkeypatch):
    import anyio
    from mcp import Client

    monkeypatch.setattr("adapt_adb.mcp.tools.tool_tap", lambda **k: pytest.fail("Mutation executed"))
    server = create_server(AccessPolicy(read_only=True))

    async def run():
        async with Client(server) as client:
            result = await client.call_tool("tap", {"x": 1, "y": 2})
            assert result.is_error and payload(result)["error"] == "access_denied"
    anyio.run(run)


@pytest.mark.parametrize("tool", ["dex_inspect", "dex_displays"])
def test_dex_inspection_respects_read_only_and_exact_target(tool, monkeypatch):
    calls = []
    monkeypatch.setattr("adapt_adb.mcp.tools.tool_dex_inspect", lambda **k: calls.append(k) or {"observed": True})
    policy = AccessPolicy(read_only=True, allowed_devices=("allowed",))
    assert payload(dispatch_tool_call(tool, {"device": "other"}, policy=policy))["error"] == "access_denied"
    assert calls == []
    assert payload(dispatch_tool_call(tool, {"device": "allowed"}, policy=policy))["observed"]
    assert calls[0]["device"] == "allowed"
