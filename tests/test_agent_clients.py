"""The AI-client table, `adapt agent clients` and `config`, and the docs generated from the table (offline)."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from adapt_adb import agent_clients as ac
from adapt_adb import agent_guide, agent_setup as setup
from adapt_adb.cli import app

from tests.agent_helpers import Sandbox, tree_snapshot

ROOT = Path(__file__).resolve().parents[1]
runner = CliRunner()


@pytest.fixture
def box(tmp_path):
    return Sandbox(tmp_path)


def _sandbox_env(monkeypatch, box):
    for key, value in box.env.items():
        monkeypatch.setenv(key, value)


# --- the table --------------------------------------------------------------------------------


def test_table_is_consistent():
    assert len(ac.CLIENT_KEYS) == len(set(ac.CLIENT_KEYS)) == len(ac.CLIENTS)
    for client in ac.CLIENTS:
        assert client.fmt in {"json", "toml", "yaml", "jsonc"}
        assert set(client.file_scopes) <= set(client.paths), client.key
        assert set(client.cli) <= set(ac.SCOPES), client.key
        if client.install:
            assert client.file_scopes or client.cli, f"{client.key} claims install but has no method"
            assert client.fmt in {"json", "jsonc", "toml"} or client.cli
            assert client.evidence.startswith("official"), client.key
        if client.file_scopes:
            assert client.fmt in {"json", "toml"}, client.key  # ADAPT edits no JSONC and no YAML
        if client.timeout:
            assert client.timeout[1] == ac.LONG_TOOL_TIMEOUT_S == 180
        assert client.note, client.key


def test_print_only_clients_are_the_ones_with_unverified_facts():
    print_only = {c.key for c in ac.CLIENTS if not c.install}
    assert print_only == {"windsurf", "zed", "continue", "goose", "jetbrains", "junie", "grok"}
    for key in print_only:
        assert not ac.CLIENT_BY_KEY[key].file_scopes and not ac.CLIENT_BY_KEY[key].cli


def test_shape_per_client_matches_the_research():
    by = ac.CLIENT_BY_KEY
    assert by["cursor"].type_value == "stdio" and by["cursor"].type_required
    assert by["opencode"].command_style == "array" and by["opencode"].servers_path == ("mcp", "servers")
    assert by["amp"].servers_path == ("amp.mcpServers",)
    assert by["codex"].fmt == "toml" and by["codex"].servers_path == ("mcp_servers",)
    assert by["vscode-local"].servers_path == ("servers",) and by["zed"].servers_path == ("context_servers",)
    assert by["codex"].timeout == ("tool_timeout_sec", 180)
    assert by["roo-code"].timeout == ("timeout", 180) and by["pi"].timeout == ("timeout", 180)
    # Claude Code's own state file is never edited; Codex's and Pi's commands cannot carry the timeout.
    assert by["claude-code"].file_scopes == ("project",)
    assert not by["codex"].cli_sets_timeout and not by["pi"].cli_sets_timeout


def test_entry_shapes():
    args = ac.server_arguments(devices=("SERIAL",))
    assert args == ["mcp", "--read-only", "--device", "SERIAL"]
    assert ac.server_arguments(allow_actions=True) == ["mcp"]
    assert ac.build_entry(ac.CLIENT_BY_KEY["cursor"], "adapt", args) == {"type": "stdio", "command": "adapt", "args": args}
    assert ac.build_entry(ac.CLIENT_BY_KEY["opencode"], "adapt", args) == {"type": "local", "command": ["adapt", *args]}
    assert ac.build_entry(ac.CLIENT_BY_KEY["codex"], "adapt", args)["tool_timeout_sec"] == 180
    assert ac.build_entry(ac.CLIENT_BY_KEY["gemini-cli"], "adapt", args) == {"command": "adapt", "args": args}


def test_paths_come_only_from_environment_variables(box):
    tokens = ac.path_tokens(box.env, box.project, "linux")
    assert ac.config_path(ac.CLIENT_BY_KEY["codex"], "user", tokens, "linux") == box.home / ".codex" / "config.toml"
    assert ac.config_path(ac.CLIENT_BY_KEY["codex"], "project", tokens, "linux") == box.project / ".codex" / "config.toml"
    assert ac.config_path(ac.CLIENT_BY_KEY["claude-desktop"], "user", tokens, "linux") == box.home / ".config" / "Claude" / "claude_desktop_config.json"
    win = ac.path_tokens(box.env, box.project, "win32")
    assert ac.config_path(ac.CLIENT_BY_KEY["claude-desktop"], "user", win, "win32") == box.root / "appdata" / "Claude" / "claude_desktop_config.json"
    assert ac.config_path(ac.CLIENT_BY_KEY["vscode"], "user", tokens, "linux") == box.home / ".copilot" / "mcp-config.json"
    # No variable, no path: the real profile is never guessed.
    empty = ac.path_tokens({}, None, "linux")
    assert empty["home"] is None
    assert ac.config_path(ac.CLIENT_BY_KEY["codex"], "user", empty, "linux") is None
    assert ac.config_path(ac.CLIENT_BY_KEY["codex"], "project", empty, "linux") is None
    assert ac.config_path(ac.CLIENT_BY_KEY["claude-desktop"], "user", win, "darwin") is None


def test_user_profile_precedence_follows_the_platform():
    env = {"HOME": "/h", "USERPROFILE": "C:/u"}
    assert ac.path_tokens(env, None, "win32")["home"] == "C:/u"
    assert ac.path_tokens(env, None, "linux")["home"] == "/h"


# --- config -----------------------------------------------------------------------------------


@pytest.mark.parametrize("client", ac.CLIENT_KEYS)
@pytest.mark.parametrize("scope", ac.SCOPES)
def test_config_prints_a_parsable_entry_for_every_client(box, client, scope):
    report = setup.config_report(client, scope, ("SERIAL",), False, box.ctx(), box.adapt)
    assert report["status"] == "ok" and report["read_only"] is True
    spec = ac.CLIENT_BY_KEY[client]
    if spec.fmt == "toml":
        data = tomllib.loads(report["snippet"])
        node = data
        for key in spec.servers_path:
            node = node[key]
        assert node["adapt"] == report["entry"]
    elif spec.fmt == "yaml":
        assert "adapt" in report["snippet"] and "--read-only" in report["snippet"]
    else:
        node = json.loads(report["snippet"])
        for key in spec.servers_path:
            node = node[key]
        assert node["adapt"] == report["entry"]
    flat = json.dumps(report["entry"])
    assert "--read-only" in flat and "SERIAL" in flat
    expected_command = "adapt" if scope == "project" else box.adapt
    assert (report["entry"]["command"][0] if isinstance(report["entry"]["command"], list) else report["entry"]["command"]) == expected_command


def test_config_defaults_to_read_only_and_writes_no_serial_unless_given(box):
    plain = setup.config_report("codex", "user", (), False, box.ctx(), box.adapt)
    assert plain["entry"]["args"] == ["mcp", "--read-only"] and "SERIAL" not in plain["snippet"] + plain["server_command"]
    assert any("no serial is written" in note for note in plain["notes"])
    actions = setup.config_report("codex", "user", (), True, box.ctx(), box.adapt)
    assert actions["entry"]["args"] == ["mcp"] and any("--allow-actions" in n for n in actions["notes"])
    project = setup.config_report("codex", "project", ("SERIAL",), False, box.ctx(), box.adapt)
    assert any("serial you gave" in note for note in project["notes"])


def test_config_prints_the_clients_own_command_and_the_file_path(box):
    report = setup.config_report("claude-code", "user", (), False, box.ctx(), box.adapt)
    assert report["own_command"] == f"claude mcp add --scope user adapt -- {box.adapt} mcp --read-only"
    assert report["config_path"] == str(box.home / ".claude.json")
    assert setup.config_report("zed", "user", (), False, box.ctx(), box.adapt)["install"] == "print-only"
    assert setup.config_report("cursor", "user", (), False, box.ctx(), box.adapt)["own_command"] is None


def test_config_refuses_bad_input_without_raising(box):
    assert setup.config_report("nope", "user", (), False, box.ctx(), box.adapt)["reason"] == "unknown_client"
    assert setup.config_report("codex", "team", (), False, box.ctx(), box.adapt)["reason"] == "bad_scope"
    assert setup.config_report("codex", "user", ("-x",), False, box.ctx(), box.adapt)["reason"] == "bad_device"
    assert setup.config_report("codex", "project", (), False, box.ctx(project=False), box.adapt)["reason"] == "no_project"


def test_config_command_prints_text_and_json(monkeypatch, box):
    _sandbox_env(monkeypatch, box)
    text = runner.invoke(app, ["agent", "config", "codex", "--adapt-path", box.adapt, "--project-dir", str(box.project)])
    assert text.exit_code == 0 and text.stderr == ""
    assert "[mcp_servers.adapt]" in text.stdout and "tool_timeout_sec = 180" in text.stdout and "read-only" in text.stdout
    as_json = runner.invoke(app, ["agent", "config", "cursor", "--json", "--scope", "project", "--device", "SERIAL"])
    assert as_json.exit_code == 0
    assert json.loads(as_json.stdout)["entry"]["command"] == "adapt"
    bad = runner.invoke(app, ["agent", "config", "nope"])
    assert bad.exit_code == 2 and "Unknown client" in bad.stdout


# --- clients ----------------------------------------------------------------------------------


def test_clients_detects_by_path_and_folder_and_reports_registration(box):
    box.on_path = {"codex"}
    box.touch(".cursor/mcp.json", json.dumps({"mcpServers": {"adapt": {"command": "/x/adapt",
                                                                      "args": ["mcp", "--read-only", "--device", "SERIAL_SECRET"]}}}))
    report = setup.clients_report(box.ctx())
    by = {r["key"]: r for r in report["clients"]}
    assert by["codex"]["installed"] and by["codex"]["detected"] == ["codex on PATH"]
    assert by["cursor"]["installed"] and by["cursor"]["detected"] == ["found {home}/.cursor"]
    assert not by["kiro"]["installed"] and not by["zed"]["installed"]
    registered = by["cursor"]["scopes"]["user"]
    assert registered["registered"] is True and registered["read_only"] is True and registered["devices"] == 1
    assert registered["command"] == "absolute" and "SERIAL_SECRET" not in json.dumps(report)
    assert by["codex"]["scopes"]["user"]["registered"] is False
    assert by["codex"]["own_command"].startswith("codex mcp add adapt --")
    assert by["zed"]["status"] == "print-only" and by["codex"]["status"] == "install"


def test_clients_reports_files_that_do_not_parse_and_reads_nothing_else(box):
    box.touch(".cursor/mcp.json", "{ not json")
    box.touch(".codex/config.toml", "token = [")
    secret = box.touch(".cursor/other.json", '{"api_key": "hunter2"}')
    report = setup.clients_report(box.ctx())
    by = {r["key"]: r for r in report["clients"]}
    assert "does not parse" in by["cursor"]["scopes"]["user"]["problem"]
    assert "does not parse" in by["codex"]["scopes"]["user"]["problem"]
    assert "hunter2" not in json.dumps(report) and secret.read_text() == '{"api_key": "hunter2"}'


def test_clients_without_any_home_variable_reports_unavailable_paths(box):
    report = setup.clients_report(box.ctx(environ={"PATH": ""}, project=False))
    assert all(not r["installed"] for r in report["clients"])
    codex = next(r for r in report["clients"] if r["key"] == "codex")
    assert codex["scopes"]["user"]["path"] is None and "variable is unset" in codex["scopes"]["user"]["problem"]


def test_clients_command_changes_nothing(monkeypatch, box):
    _sandbox_env(monkeypatch, box)
    box.touch(".codex/config.toml", "model = 'x'\n")
    before = tree_snapshot(box.root)
    result = runner.invoke(app, ["agent", "clients", "--project-dir", str(box.project)])
    as_json = runner.invoke(app, ["agent", "clients", "--json", "--project-dir", str(box.project)])
    assert result.exit_code == 0 and as_json.exit_code == 0 and as_json.stderr == ""
    assert "OpenAI Codex" in result.stdout and json.loads(as_json.stdout)["clients"]
    assert tree_snapshot(box.root) == before


# --- generated documentation ------------------------------------------------------------------


def test_clients_doc_section_matches_the_table():
    text = (ROOT / "docs" / "clients.md").read_bytes().decode("utf-8").replace("\r\n", "\n")
    assert ac.render_clients_doc() in text, "docs/clients.md drifted from the client table; regenerate its generated section"
    assert text.count(ac.CLIENTS_DOC_BEGIN) == 1 and text.count(ac.CLIENTS_DOC_END) == 1
    for client in ac.CLIENTS:
        assert f"(`{client.key}`)" in text


def test_example_files_match_the_table():
    examples = {
        "cursor.mcp.json": "cursor", "vscode.mcp.json": "vscode", "opencode.jsonc": "opencode",
        "zed.settings.json": "zed", "gemini.settings.json": "gemini-cli", "goose.config.yaml": "goose",
    }
    for name, key in examples.items():
        client = ac.CLIENT_BY_KEY[key]
        expected = ac.render_snippet(client, ac.build_entry(client, "adapt", ac.server_arguments(devices=("SERIAL",))))
        text = (ROOT / "examples" / name).read_text(encoding="utf-8")
        body = "\n".join(line for line in text.split("\n") if not line.startswith(("//", "#")))
        assert body.strip() == expected.strip(), name
        assert "\r" not in text
    json.loads((ROOT / "examples" / "cursor.mcp.json").read_text())
    for existing in ("mcp.json", "claude.mcp.json"):
        assert json.loads((ROOT / "examples" / existing).read_text())["mcpServers"]["adapt"]["command"] == "adapt"
    assert tomllib.loads((ROOT / "examples" / "codex.toml").read_text())["mcp_servers"]["adapt"]["command"] == "adapt"


def test_the_old_promise_is_gone_everywhere_and_the_new_one_is_stated():
    old = re.compile(r"(does not|never) (rewrite|write)s? (an AI |a )?client'?s? settings|ADAPT never writes a client", re.I)
    new = "ADAPT changes a client's settings only when you run `adapt agent install`, shows the change first, and keeps a backup."
    hits = []
    for path in [*ROOT.glob("*.md"), *ROOT.glob("docs/*.md"), *ROOT.glob("examples/*.md"), *ROOT.glob("src/adapt_adb/**/*.py"),
                 *ROOT.glob("src/adapt_adb/data/**/*.md")]:
        text = path.read_bytes().decode("utf-8", "replace")
        if old.search(text):
            hits.append(path.relative_to(ROOT).as_posix())
    assert hits == []
    for rel in ("docs/mcp-access.md", "docs/clients.md"):
        flat = " ".join((ROOT / rel).read_bytes().decode("utf-8").split())
        assert new in flat, rel
    assert new in " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
    primer = agent_guide.render_markdown(agent_guide.build_primer())
    assert "changes a client's settings only when you run `adapt agent install`" in " ".join(primer.split())
