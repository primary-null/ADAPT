"""MCP resources and the `help` tool: static text, the same through both, never device data (offline)."""

from __future__ import annotations

import json
import re

import anyio
import pytest
from typer.testing import CliRunner

from adapt_adb import agent_guide, failure_report
from adapt_adb.cli import app
from adapt_adb.failure_guide import GUIDES
from adapt_adb.mcp import access, resources
from adapt_adb.mcp import tools as mcp_tools
from adapt_adb.mcp.access import AccessPolicy
from adapt_adb.mcp.server import create_server, dispatch_tool_call, get_mcp_tools

STATIC_TOPICS = [t for t in resources.HELP_TOPICS if t != "failure"]
TOOL_NAMES = {s["name"] for s in mcp_tools.TOOL_SCHEMAS}
POLICIES = {
    "full": AccessPolicy(),
    "read-only": AccessPolicy(read_only=True),
    "device": AccessPolicy(allowed_devices=("SERIAL_SECRET",)),
}


def _call(name, arguments, policy=None):
    result = dispatch_tool_call(name, arguments, policy=policy or AccessPolicy())
    return result, json.loads(result.content[0].text)


def _exercise(policy, coro):
    from mcp import Client

    async def run():
        async with Client(create_server(policy)) as client:
            return await coro(client)

    return anyio.run(run)


# --- the server serves them ------------------------------------------------------------------


def test_every_resource_is_listed_and_readable_through_a_real_client():
    async def go(client):
        listed = (await client.list_resources()).resources
        templates = (await client.list_resource_templates()).resource_templates
        texts = {}
        for item in listed:
            result = await client.read_resource(item.uri)
            assert len(result.contents) == 1 and result.contents[0].mime_type == "text/markdown"
            texts[str(item.uri)] = result.contents[0].text
        return [str(r.uri) for r in listed], [t.uri_template for t in templates], texts

    uris, templates, texts = _exercise(AccessPolicy(), go)
    assert "adapt://primer" in uris and "adapt://session" in uris and "adapt://safety" in uris
    assert "adapt://workflows/build-install-test" in uris and "adapt://workflows/ui-testing" in uris
    assert "adapt://reference/errors" in uris
    for guide in resources.GUIDE_TOPICS:
        assert f"adapt://guides/{guide}" in uris
    assert templates == ["adapt://guides/{topic}", "adapt://failures/{id}"]
    assert all(text.strip() for text in texts.values())


def test_a_failure_template_instance_and_unknown_uris_through_a_real_client():
    from mcp import MCPError

    async def go(client):
        good = await client.read_resource("adapt://failures/device.unauthorized")
        errors = []
        for uri in ("adapt://nope", "adapt://failures/not.a.guide", "adapt://failures/..%2Fx", "file:///etc/passwd",
                    "adapt://failures/", "adapt://guides/unknown"):
            try:
                await client.read_resource(uri)
            except MCPError as exc:
                errors.append(exc.error.code)
        return good.contents[0].text, errors

    text, errors = _exercise(AccessPolicy(), go)
    assert "device.unauthorized" in text
    assert len(errors) == 6 and set(errors) == {-32602}


def test_resources_do_not_depend_on_the_session_except_adapt_session():
    for topic in STATIC_TOPICS:
        if topic == "session":
            continue
        assert resources.read_topic(topic, POLICIES["full"]) == resources.read_topic(topic, POLICIES["read-only"])


def test_session_resource_follows_the_policy_and_names_no_serial():
    full = resources.read_topic("session", POLICIES["full"])["text"]
    read_only = resources.read_topic("session", POLICIES["read-only"])["text"]
    restricted = resources.read_topic("session", POLICIES["device"])["text"]
    assert "Mode: full" in full and "`tap`" in full and "Acting:" in full
    assert "Mode: read-only" in read_only and "`tap`" not in read_only and "Acting:" not in read_only
    assert "restricted to 1 exact device" in restricted and "`doctor`" not in restricted
    assert "may omit `device`" in restricted
    assert "SERIAL_SECRET" not in restricted
    for policy in POLICIES.values():
        text = resources.read_topic("session", policy)["text"]
        offered = {s["name"] for s in mcp_tools.TOOL_SCHEMAS if policy.exposes(s["name"])}
        assert set(re.findall(r"`([a-z_]+)`", text)) & TOOL_NAMES == offered


# --- the content ------------------------------------------------------------------------------


def test_resource_text_holds_no_device_data_no_process_and_no_private_values(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a resource must not start a process or open a connection")

    monkeypatch.setattr("subprocess.run", refuse)
    monkeypatch.setattr("subprocess.Popen", refuse)
    monkeypatch.setattr("socket.socket.connect", refuse)
    for topic in STATIC_TOPICS:
        text = resources.read_topic(topic, POLICIES["device"])["text"]
        assert text.isascii(), topic
        assert not re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text), topic
        assert not re.search(r"[A-Za-z]:\\|/home/|/Users/|@", text), topic
        assert "SERIAL_SECRET" not in text, topic


def test_workflow_texts_name_only_real_tools_and_real_commands():
    allowed_words = {"apk", "device", "expected_debuggable", "expected_version_code", "package", "ok"}
    for topic in ("build-install-test", "ui-testing", "session"):
        text = resources.read_topic(topic)["text"]
        assert set(re.findall(r"`([a-z_]+)`", text)) - TOOL_NAMES <= allowed_words, topic
    from typer.main import get_command

    root = get_command(app)
    for topic in STATIC_TOPICS:
        for quoted in re.findall(r"`(adapt [a-z][a-z0-9 -]*)", resources.read_topic(topic)["text"]):
            command = root
            for token in quoted.split()[1:]:
                if token.startswith("-") or not hasattr(command, "list_commands"):
                    break
                command = command.get_command(None, token)
                assert command is not None, (topic, quoted)
            assert not hasattr(command, "list_commands"), (topic, quoted)  # ends on a real leaf command


def test_primer_resource_is_the_cli_primer():
    assert resources.read_topic("primer")["text"] == agent_guide.render_markdown(agent_guide.build_primer())
    result = CliRunner().invoke(app, ["agent", "primer"])
    assert result.stdout.strip() == resources.read_topic("primer")["text"].strip()


def test_guides_reuse_the_existing_offline_text():
    from adapt_adb import development, diagnostics
    from adapt_adb.android_support import version_matrix

    def block(topic):
        text = resources.read_topic(topic)["text"]
        return json.loads(text[text.index("```json") + 7:text.rindex("```")])

    assert block("android-versions") == version_matrix()
    assert block("build-modes") == development.build_modes()
    assert block("logging") == diagnostics.logging_guide()
    catalog = block("tool-catalog")["tools"]
    assert {t["id"] for t in catalog} >= {"openssh", "platform-tools", "scrcpy"}
    assert "detection" not in json.dumps(catalog)  # the curated records only; nothing was looked up on PATH
    assert "stay-awake" in resources.read_topic("dev-options")["text"]


def test_failure_resource_is_the_explain_text(monkeypatch):
    monkeypatch.setenv("COLUMNS", "80")
    for failure_id in ("device.unauthorized", "adb.not_found", "mcp.access_denied"):
        text = resources.read_topic("failure", failure_id=failure_id)["text"]
        assert text == failure_report.render_explain(failure_report.resolve(failure_id), width=80)
        cli = CliRunner().invoke(app, ["explain", failure_id])
        assert cli.exit_code == 0 and cli.stdout.strip() == text.strip()
    assert resources.read_uri("adapt://failures/device.unauthorized")["uri"] == "adapt://failures/device.unauthorized"


def test_failure_ids_are_validated_before_any_lookup():
    for bad in (None, "", "Device.Unauthorized", "../x", "a" * 65, "x y", "device.unauthorized/extra"):
        report = resources.read_topic("failure", failure_id=bad)
        assert report["error"] == "invalid_params", bad
    assert resources.read_topic("failure", failure_id="no.such_guide")["error"] == "unknown_failure"
    assert resources.read_topic("nope")["error"] == "invalid_params"
    assert resources.read_uri("adapt://primer/extra")["error"] == "unknown_resource"


def test_every_known_failure_guide_can_be_read():
    for failure_id in sorted(GUIDES)[:40]:
        assert resources.read_topic("failure", failure_id=failure_id)["text"].strip()


# --- the help tool ----------------------------------------------------------------------------


def test_help_tool_schema_and_classification():
    schema = next(s for s in mcp_tools.TOOL_SCHEMAS if s["name"] == "help")
    assert schema["inputSchema"]["properties"]["topic"]["enum"] == list(resources.HELP_TOPICS)
    assert schema["inputSchema"]["required"] == ["topic"]
    assert schema["inputSchema"]["additionalProperties"] is False
    assert "help" in access.OBSERVATION_TOOLS and "help" not in access.DEVICE_TOOLS
    assert "help" not in access.MUTATING_TOOLS and "help" not in access.OPEN_WORLD_TOOLS
    for policy in POLICIES.values():
        assert policy.exposes("help")
        hints = next(t for t in get_mcp_tools(policy) if t.name == "help").annotations
        assert hints.read_only_hint is True and hints.open_world_hint is False and hints.idempotent_hint is True
        advertised = next(t for t in get_mcp_tools(policy) if t.name == "help")
        assert "device" not in advertised.input_schema["properties"] and advertised.input_schema["required"] == ["topic"]


@pytest.mark.parametrize("label", list(POLICIES))
def test_help_returns_the_resource_text_for_every_topic(label, monkeypatch):
    policy = POLICIES[label]

    def refuse(*args, **kwargs):
        raise AssertionError("help must not start a process")

    monkeypatch.setattr("subprocess.run", refuse)
    for topic in STATIC_TOPICS:
        result, payload = _call("help", {"topic": topic}, policy)
        assert not result.is_error, topic
        assert payload["text"] == resources.read_topic(topic, policy)["text"]
        assert payload["uri"] == resources.TOPIC_RESOURCES[topic][0]
        assert "content_trust" not in payload  # ADAPT-authored text, not device text
    result, payload = _call("help", {"topic": "failure", "id": "device.unauthorized"}, policy)
    assert payload["text"] == resources.read_topic("failure", failure_id="device.unauthorized")["text"]


def test_help_errors_carry_a_failure_object():
    for arguments in ({}, {"topic": "nope"}, {"topic": "failure"}, {"topic": "failure", "id": "no.such_guide"},
                      {"topic": "primer", "extra": 1}):
        result, payload = _call("help", arguments)
        assert result.is_error and payload["error"] in {"invalid_params", "unknown_failure"}
        assert payload["failure"]["id"]
    assert "ui_summary" in resources.read_topic("session")["text"]


def test_help_works_over_a_real_client_in_a_read_only_one_device_session():
    async def go(client):
        names = [t.name for t in (await client.list_tools()).tools]
        result = await client.call_tool("help", {"topic": "safety"})
        return names, json.loads(result.content[0].text)

    names, payload = _exercise(AccessPolicy(read_only=True, allowed_devices=("SERIAL_SECRET",)), go)
    assert "help" in names and "tap" not in names
    assert payload["text"] == resources.read_topic("safety")["text"]
