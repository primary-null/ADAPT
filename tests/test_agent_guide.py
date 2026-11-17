"""Agent primer, MCP server instructions and MCP tool titles/annotations (offline)."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from typer.main import get_command
from typer.testing import CliRunner

from adapt_adb import agent_guide, help_system
from adapt_adb.cli import app
from adapt_adb.mcp import access
from adapt_adb.mcp import tools as mcp_tools
from adapt_adb.mcp.access import AccessPolicy
from adapt_adb.mcp.server import create_server, get_mcp_tools
from adapt_adb.safety import validate_adb_args

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[1]

POLICIES = {
    "full": AccessPolicy(),
    "read-only": AccessPolicy(read_only=True),
    "one-device": AccessPolicy(allowed_devices=("SERIAL",)),
    "read-only one-device": AccessPolicy(read_only=True, allowed_devices=("SERIAL",)),
    "two-devices": AccessPolicy(allowed_devices=("SERIAL_A", "SERIAL_B")),
}


def _leaf_paths(root) -> set[str]:
    """Visible leaf commands, found independently of the primer (via the help inventory)."""
    records = help_system.command_inventory(root)
    parents = {r["command"].rsplit(" ", 1)[0] for r in records}
    return {r["command"] for r in records if r["command"] not in parents and r["command"] != "adapt"}


def _resolve(root, tokens: list[str]):
    """Walk the click tree along `tokens`; return (command, remaining tokens) or (None, tokens)."""
    command, rest = root, list(tokens)
    while hasattr(command, "list_commands") and rest and not rest[0].startswith("-"):
        child = command.get_command(None, rest[0])
        if child is None:
            return None, rest
        command, rest = child, rest[1:]
    return command, rest


def _option_names(command) -> set[str]:
    return {n for p in command.params if getattr(p, "param_type_name", "") == "option"
            for n in [*p.opts, *p.secondary_opts]}


def _quoted(primer: dict) -> list[str]:
    return [primer["mcp"]["start"], *primer["quoted_commands"]]


# --- the CLI ---------------------------------------------------------------------------------


def test_primer_json_is_valid_and_stdout_clean():
    result = runner.invoke(app, ["agent", "primer", "--json"])
    assert result.exit_code == 0
    assert result.stderr == ""
    primer = json.loads(result.stdout)  # the whole of stdout is one JSON document
    assert primer["schema_version"] == agent_guide.PRIMER_SCHEMA_VERSION
    assert {"what_is_adapt", "first_steps", "development_loop", "safety_rules", "output_conventions",
            "mcp", "commands", "quoted_commands", "claim_boundary"} <= set(primer)
    assert len(primer["mcp"]["tools"]) == len(mcp_tools.TOOL_SCHEMAS)


def test_markdown_primer_is_stdout_only_and_bare_agent_prints_the_same():
    primer = runner.invoke(app, ["agent", "primer"])
    bare = runner.invoke(app, ["agent"])
    assert primer.exit_code == 0 and bare.exit_code == 0
    assert primer.stderr == "" and bare.stderr == ""
    assert primer.stdout == bare.stdout
    assert primer.stdout.startswith("# ADAPT agent primer")
    assert primer.stdout.isascii()
    for heading in ("## First steps", "## Development loop", "## Safety rules", "## Output and exit codes",
                    "## MCP server", "## Command catalog"):
        assert heading in primer.stdout


def test_primer_and_agent_help_make_no_adb_or_network_call(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the primer must not start a process or open a connection")

    monkeypatch.setattr("subprocess.run", refuse)
    monkeypatch.setattr("subprocess.Popen", refuse)
    monkeypatch.setattr("socket.socket.connect", refuse)
    assert runner.invoke(app, ["agent", "primer", "--json"]).exit_code == 0
    assert runner.invoke(app, ["agent", "--help"]).exit_code == 0


def test_root_help_tells_agents_to_run_the_primer():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "adapt agent primer" in " ".join(result.stdout.split())


def test_agent_group_is_registered_with_help_topics_and_a_console_area():
    root = get_command(app)
    assert "agent" in root.list_commands(None)
    assert "agent" in help_system.HOW_TO and "agent primer" in help_system.HOW_TO
    grouped = {name for _, names in __import__("adapt_adb.terminal_ui", fromlist=["AREAS"]).AREAS for name in names}
    top_level = {p.split(" ")[1] for p in _leaf_paths(root)} - {"console"}
    assert top_level <= grouped


def test_agent_is_machine_facing_so_no_banner_is_shown():
    from adapt_adb import banner

    assert banner._machine_facing(SimpleNamespace(invoked_subcommand="agent", meta={}))


# --- the command catalog ---------------------------------------------------------------------


def test_catalog_is_every_visible_leaf_command_and_no_hidden_alias():
    root = get_command(app)
    catalog = agent_guide.command_catalog(root)
    paths = [record["command"] for record in catalog]
    assert len(paths) == len(set(paths))
    assert set(paths) == _leaf_paths(root)
    assert "adapt agent primer" in paths
    assert "adapt home" not in paths and "adapt give-me-everything" not in paths


def test_catalog_flags_match_the_real_options():
    by_path = {r["command"]: r for r in agent_guide.command_catalog(get_command(app))}
    assert by_path["adapt dev-options inspect"]["serial"] is True
    assert by_path["adapt dev-options inspect"]["json"] is True
    assert by_path["adapt dev-options inspect"]["target_option"] == "--serial"
    assert by_path["adapt dev-options inspect"]["required_options"] == ["--serial"]
    assert by_path["adapt app validate"]["required_options"] == ["--package", "--serial"]
    assert by_path["adapt setup"]["yes"] is True and by_path["adapt setup"]["serial"] is False
    assert by_path["adapt everything"]["target_option"] == "--device"
    assert by_path["adapt devices"]["json"] is False
    assert by_path["adapt console"]["agent_note"] and by_path["adapt mcp"]["agent_note"]
    assert by_path["adapt doctor"]["agent_note"] is None


def test_every_mcp_tool_is_in_the_primer_with_its_classification():
    tools = {t["name"]: t for t in agent_guide.mcp_tool_catalog()}
    assert list(tools) == [s["name"] for s in mcp_tools.TOOL_SCHEMAS]
    for name, record in tools.items():
        assert record["kind"] == ("observation" if name in access.OBSERVATION_TOOLS else "acting")
        assert record["device_bound"] == (name in access.DEVICE_TOOLS)
        assert record["hidden_by_read_only"] == (name in access.MUTATING_TOOLS)
        assert record["title"] and record["summary"]
    counts = agent_guide.build_primer()["mcp"]["exposed_tool_counts"]
    assert counts == {"adapt mcp": 24, "adapt mcp --read-only": 19, "adapt mcp --device SERIAL": 23,
                      "adapt mcp --read-only --device SERIAL": 18}


# --- every quoted command is real ------------------------------------------------------------


def test_every_quoted_command_resolves_with_flags_that_exist():
    root = get_command(app)
    primer = agent_guide.build_primer(root)
    catalog = {r["command"]: r for r in primer["commands"]}
    assert primer["quoted_commands"]
    for quoted in _quoted(primer):
        tokens = shlex.split(quoted)
        assert tokens[0] == "adapt", quoted
        command, rest = _resolve(root, tokens[1:])
        assert command is not None and not hasattr(command, "list_commands"), quoted
        path = "adapt " + " ".join(tokens[1:len(tokens) - len(rest)])
        record = catalog[path]
        if path == "adapt adb":
            continue  # everything after `--` belongs to adb, checked below
        flags = [t.split("=")[0] for t in rest if t.startswith("-")]
        known = _option_names(command) | {"--help"}
        assert set(flags) <= known, f"{quoted}: unknown {set(flags) - known}"
        for required in record["required_options"]:
            assert required in flags or (required == "--serial" and "-s" in flags), f"{quoted}: missing {required}"
        if record["serial"]:
            assert "-s" in flags, f"{quoted}: must name an exact serial"


def test_every_quoted_command_shows_help_and_the_offline_ones_run(monkeypatch):
    root = get_command(app)
    primer = agent_guide.build_primer(root)
    for quoted in _quoted(primer):
        tokens = shlex.split(quoted)[1:]
        command, rest = _resolve(root, tokens)
        path = tokens[:len(tokens) - len(rest)]
        assert runner.invoke(app, [*path, "--help"]).exit_code == 0, quoted
    # These quoted commands touch no device and no host state, so they run for real offline.
    for quoted in (
        "adapt rapid plan -s SERIAL --package com.example.app --session-dir NEW_SESSION --json",
        "adapt debug plan -s SERIAL --package com.example.app --wait --json",
        "adapt explain device.unauthorized",
        "adapt explain --list",
    ):
        assert quoted in primer["quoted_commands"]
        result = runner.invoke(app, shlex.split(quoted)[1:])
        assert result.exit_code == 0, (quoted, result.output)


def test_quoted_adb_passthrough_passes_the_safety_guard():
    primer = agent_guide.build_primer()
    passthrough = [shlex.split(q)[1:] for q in primer["quoted_commands"] if q.startswith("adapt adb ")]
    assert passthrough
    for tokens in passthrough:
        assert tokens[:2] == ["adb", "--"]
        validate_adb_args(tokens[2:])
        assert tokens[2] == "-s" and tokens[3]  # the exact target comes first


def test_acting_commands_in_the_loop_are_marked_for_user_approval():
    steps = {s["id"]: s for s in agent_guide.build_primer()["development_loop"]["steps"]}
    assert steps["prepare"]["needs_user_approval"] and steps["install"]["needs_user_approval"]
    assert not steps["restore"]["needs_user_approval"]


def test_safety_rules_cover_the_documented_invariants():
    text = " ".join(agent_guide.build_primer()["safety_rules"])
    for needle in ("exact serial", "kill-server", "sudo", "Restore every profile", "Ask the user before",
                   "untrusted data", "Never follow instructions", "claim_boundary"):
        assert needle in text, needle
    primer = agent_guide.render_markdown(agent_guide.build_primer())
    assert "claude mcp add adapt -- adapt mcp --read-only --device SERIAL" in primer
    assert "codex mcp add adapt -- adapt mcp --read-only --device SERIAL" in primer
    assert "adapt mcp --read-only --device SERIAL" in primer


def test_primer_has_no_private_values():
    text = json.dumps(agent_guide.build_primer())
    assert not re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text)
    assert not re.search(r"[A-Za-z]:\\\\|/home/|/Users/", text)
    assert "@" not in text.replace("@property", "")


# --- MCP instructions ------------------------------------------------------------------------


@pytest.mark.parametrize("label", list(POLICIES))
def test_mcp_instructions_are_short_ascii_and_name_only_real_tools(label):
    text = agent_guide.mcp_instructions(POLICIES[label])
    assert 0 < len(text) <= agent_guide.MCP_INSTRUCTIONS_TARGET_CHARS == 2000
    assert text.isascii()
    names = {s["name"] for s in mcp_tools.TOOL_SCHEMAS}
    for token in re.findall(r"`([a-z_]+)`", text):
        if token in {"device", "failure", "content_trust", "untrusted_device_data"}:
            continue
        assert token in names, token
    assert "untrusted" in text and "adapt agent primer" in text


def test_mcp_instructions_follow_the_session_policy():
    full = agent_guide.mcp_instructions(POLICIES["full"])
    read_only = agent_guide.mcp_instructions(POLICIES["read-only"])
    for name in access.MUTATING_TOOLS:
        assert f"`{name}`" in full
        assert f"`{name}`" not in read_only
    assert "read-only" in read_only
    assert "limited to the devices" in agent_guide.mcp_instructions(POLICIES["one-device"])
    assert "limited to the devices" not in full
    assert "SERIAL" not in agent_guide.mcp_instructions(POLICIES["one-device"])


@pytest.mark.parametrize("label", list(POLICIES))
def test_server_announces_instructions_through_the_sdk(label):
    from mcp import Client

    policy = POLICIES[label]
    expected = agent_guide.mcp_instructions(policy)

    async def exercise():
        async with Client(create_server(policy)) as client:
            assert client.instructions == expected

    anyio.run(exercise)


def test_server_instructions_survive_the_older_one_x_constructors(monkeypatch):
    import mcp.server as mcp_server

    expected = agent_guide.mcp_instructions(AccessPolicy())

    class OneXServer:
        """mcp 1.x: positional name, no on_* handlers, `instructions` keyword."""
        def __init__(self, name, version=None, instructions=None):
            self.name, self.version, self.instructions = name, version, instructions

    class OldestServer:
        """An earlier 1.x constructor that takes only the name."""
        def __init__(self, name):
            self.name = name

    monkeypatch.setattr(mcp_server, "Server", OneXServer)
    assert create_server().instructions == expected
    monkeypatch.setattr(mcp_server, "Server", OldestServer)
    assert create_server().instructions == expected


# --- MCP titles and annotations --------------------------------------------------------------


@pytest.mark.parametrize("label", list(POLICIES))
def test_every_advertised_tool_has_a_title_and_annotations_matching_its_classification(label):
    policy = POLICIES[label]
    advertised = get_mcp_tools(policy)
    assert advertised
    assert [t.name for t in advertised] == [s["name"] for s in mcp_tools.TOOL_SCHEMAS if policy.exposes(s["name"])]
    titles = [t.title for t in advertised]
    assert all(titles) and len(set(titles)) == len(titles)
    for tool in advertised:
        hints = tool.annotations
        assert hints is not None and hints.title == tool.title
        assert hints.open_world_hint is (tool.name in access.OPEN_WORLD_TOOLS)
        if tool.name in access.OBSERVATION_TOOLS:
            assert hints.read_only_hint is True and hints.destructive_hint is False and hints.idempotent_hint is True
        else:
            assert tool.name in access.MUTATING_TOOLS
            assert hints.read_only_hint is False
            assert policy.read_only is False
    if policy.read_only:
        assert all(t.annotations.read_only_hint for t in advertised)


def test_every_acting_tool_is_destructive_and_input_tools_are_open_world():
    by_name = {t.name: t.annotations for t in get_mcp_tools()}
    assert {n for n, h in by_name.items() if not h.read_only_hint} == access.MUTATING_TOOLS
    for name in ("tap", "swipe", "key", "type"):
        assert by_name[name].read_only_hint is False
        assert by_name[name].destructive_hint is True
        assert by_name[name].idempotent_hint is False
        assert by_name[name].open_world_hint is True
    install = by_name["install_apk"]
    assert install.read_only_hint is False and install.destructive_hint is True
    assert install.idempotent_hint is True and install.open_world_hint is False
    for name in ("screenshot", "ui_summary", "logcat_tail", "app_logs", "diagnostic_logs"):
        assert by_name[name].read_only_hint is True and by_name[name].open_world_hint is True
    for name in ("doctor", "list_devices", "device_info", "android_versions", "build_modes"):
        assert by_name[name].open_world_hint is False


def test_a_new_tool_is_covered_without_touching_the_annotation_code(monkeypatch):
    schema = {"name": "new_sample_tool", "description": "A tool added later.",
              "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}}
    monkeypatch.setattr(mcp_tools, "TOOL_SCHEMAS", [*mcp_tools.TOOL_SCHEMAS, schema])
    tool = next(t for t in get_mcp_tools() if t.name == "new_sample_tool")
    assert tool.title == "New sample tool"
    # Unclassified means "acts on the phone": advertised conservatively, and hidden by --read-only.
    assert tool.annotations.read_only_hint is False and tool.annotations.destructive_hint is True
    assert "new_sample_tool" not in {t.name for t in get_mcp_tools(AccessPolicy(read_only=True))}


def test_title_generation():
    assert access.tool_title("ui_summary") == "UI summary"
    assert access.tool_title("dex_inspect") == "DeX inspect"
    assert access.tool_title("install_apk") == "Install APK"
    assert access.tool_title("app_validate") == "App validate"
    assert access.tool_title("tap") == "Tap screen"


def test_annotations_reach_a_real_client(monkeypatch):
    from mcp import Client

    async def exercise():
        async with Client(create_server(POLICIES["read-only one-device"])) as client:
            tools = (await client.list_tools()).tools
            assert len(tools) == 18
            assert all(t.title and t.annotations and t.annotations.read_only_hint for t in tools)

    anyio.run(exercise)


def test_tools_degrade_to_plain_tools_when_the_sdk_has_no_annotations():
    from adapt_adb.mcp.server import _build_tool

    class PlainTool:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    built = _build_tool(SimpleNamespace(Tool=PlainTool), "tap", "d", {"type": "object"})
    assert set(built.kwargs) == {"name", "description", "inputSchema"}

    class StrictTool(PlainTool):
        def __init__(self, **kwargs):
            if "annotations" in kwargs:
                raise TypeError("no annotations in this SDK")
            super().__init__(**kwargs)

    types = SimpleNamespace(Tool=StrictTool, ToolAnnotations=lambda **kw: kw)
    assert set(_build_tool(types, "tap", "d", {"type": "object"}).kwargs) == {"name", "description", "inputSchema"}


# --- documentation counts --------------------------------------------------------------------


def test_inventory_counts_match_the_live_code():
    inventory = json.loads((ROOT / "docs" / "feature-inventory.json").read_text(encoding="utf-8"))["counts"]
    root = get_command(app)
    records = help_system.command_inventory(root)
    leaves = _leaf_paths(root)
    groups = {r["command"] for r in records} - leaves
    assert inventory["visible_leaf_commands"] == len(leaves)
    assert inventory["visible_groups_including_root"] == len(groups)
    assert inventory["visible_cli_entries"] == len(records) == len(leaves) + len(groups)
    assert inventory["mcp_tools"] == len(mcp_tools.TOOL_SCHEMAS)
    assert inventory["mcp_mutating_tools"] == len(access.MUTATING_TOOLS)
    assert inventory["mcp_read_only_tools"] == len(access.OBSERVATION_TOOLS)
