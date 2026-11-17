"""The packaged Agent Skill, the AGENTS.md block, `adapt agent verify` and the primer's effect classes (offline)."""

from __future__ import annotations

import json
import re
import shlex
import sys
import time
from pathlib import Path

import pytest
from typer.main import get_command
from typer.testing import CliRunner

from adapt_adb import agent_guide
from adapt_adb import agent_setup as setup
from adapt_adb import agent_skill as skill
from adapt_adb.cli import app
from adapt_adb.failure_guide import GUIDES
from adapt_adb.mcp import tools as mcp_tools

from tests.agent_helpers import Sandbox, tree_snapshot

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "src" / "adapt_adb" / "data" / "skill" / "adapt"
TOOL_NAMES = {s["name"] for s in mcp_tools.TOOL_SCHEMAS}


@pytest.fixture
def box(tmp_path):
    return Sandbox(tmp_path)


def _resolve(root, tokens):
    command, rest = root, list(tokens)
    while hasattr(command, "list_commands") and rest and not rest[0].startswith("-"):
        child = command.get_command(None, rest[0])
        if child is None:
            return None, rest
        command, rest = child, rest[1:]
    return command, rest


def _option_names(command):
    return {n for p in command.params if getattr(p, "param_type_name", "") == "option" for n in [*p.opts, *p.secondary_opts]}


def _quoted_commands(text: str) -> list[str]:
    found = []
    for span in re.findall(r"`([^`\n]+)`", text):
        if span.startswith("adapt "):
            found.append(span)
    in_block = False
    for line in text.split("\n"):
        if line.startswith("```"):
            in_block = not in_block
        elif in_block and line.strip().startswith("adapt "):
            found.append(re.split(r"\s+#\s", line.strip())[0])
    return found


# --- the skill --------------------------------------------------------------------------------


def test_skill_frontmatter_follows_the_agent_skills_format():
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n") and "\r" not in text
    front, body = text[4:].split("\n---\n", 1)
    fields = dict(re.findall(r"^([a-z-]+): (.*)$", front, re.M))
    assert fields["name"] == "adapt" == SKILL_DIR.name
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", fields["name"]) and len(fields["name"]) <= 64
    assert 0 < len(fields["description"]) <= 1024
    assert "Use when" in fields["description"] and "Android" in fields["description"]
    assert 0 < len(fields["compatibility"]) <= 500
    assert set(fields) <= {"name", "description", "compatibility", "license", "metadata", "allowed-tools"}
    assert body.count("\n") < 500 and text.count("\n") < 500
    assert text.isascii()


def test_the_skill_files_are_packaged_and_read_back_from_the_package():
    files = skill.skill_files()
    assert sorted(files) == ["SKILL.md", "references/failures.md"]
    assert files["SKILL.md"] == (SKILL_DIR / "SKILL.md").read_bytes()
    assert skill.skill_text() == (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")


def test_every_command_the_skill_quotes_exists_with_real_flags():
    root = get_command(app)
    quoted = [c for name in ("SKILL.md", "references/failures.md") for c in _quoted_commands((SKILL_DIR / name).read_text(encoding="utf-8"))]
    assert len(quoted) >= 15
    for line in quoted:
        tokens = shlex.split(line)
        assert tokens[0] == "adapt", line
        command, rest = _resolve(root, tokens[1:])
        assert command is not None and not hasattr(command, "list_commands"), f"{line}: not a command"
        if tokens[1:2] == ["adb"]:
            continue  # everything after `--` belongs to adb
        flags = [t.split("=")[0] for t in rest if t.startswith("-")]
        assert set(flags) <= _option_names(command) | {"--help"}, f"{line}: unknown {set(flags) - _option_names(command)}"


def test_every_quoted_command_in_the_skill_is_in_the_primer_catalog_and_mcp_tools_exist():
    catalog = {r["command"] for r in agent_guide.command_catalog(get_command(app))}
    for name in ("SKILL.md", "references/failures.md"):
        text = (SKILL_DIR / name).read_text(encoding="utf-8")
        for line in _quoted_commands(text):
            path = [t for t in shlex.split(line)[1:] if not t.startswith("-")]
            assert any(" ".join(["adapt", *path[:n]]) in catalog for n in range(1, len(path) + 1)), line
        tokens = set(re.findall(r"`([a-z_]+)`", text))
        mcp_like = {t for t in tokens if "_" in t or t in TOOL_NAMES}
        assert mcp_like - TOOL_NAMES <= {"not_useful", "claim_boundary"}, mcp_like - TOOL_NAMES


def test_failure_ids_in_the_skill_references_exist():
    text = (SKILL_DIR / "references" / "failures.md").read_text(encoding="utf-8")
    ids = re.findall(r"`((?:device|adb|mcp|app|logs|debug)\.[a-z_.]+)`", text)
    assert len(ids) >= 8
    for failure_id in ids:
        assert failure_id in GUIDES, failure_id


def test_the_skill_agrees_with_the_primer_on_the_rules():
    text = " ".join((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").split())
    for needle in ("kill-server", "sudo", "exact device", "untrusted", "Restore every Developer Options profile",
                   "Ask the user before", "adapt agent primer", "stale", "exit code 3"):
        assert needle in text, needle
    assert "SERIAL" in text and not re.search(r"[A-Za-z]:\\|/home/|/Users/|@", text)


def test_skill_print_command():
    plain = runner.invoke(app, ["agent", "skill", "print"])
    assert plain.exit_code == 0 and plain.stderr == "" and plain.stdout.strip() == skill.skill_text().strip()
    data = json.loads(runner.invoke(app, ["agent", "skill", "print", "--json"]).stdout)
    assert data["name"] == "adapt" and data["files"] == ["SKILL.md", "references/failures.md"]


# --- skill install ----------------------------------------------------------------------------


def _install(box, scope="project", targets=("agents", "claude"), **kwargs):
    args = dict(replace=False, dry_run=False, yes=True, interactive=False, confirm=None)
    args.update(kwargs)
    return skill.install_skill(scope, targets, ctx=box.ctx(), **args)


def test_install_copies_the_skill_to_agents_and_claude_folders(box):
    report = _install(box)
    assert report["status"] == "installed"
    for folder in (".agents/skills/adapt", ".claude/skills/adapt"):
        assert (box.project / folder / "SKILL.md").read_bytes() == (SKILL_DIR / "SKILL.md").read_bytes()
        assert (box.project / folder / "references" / "failures.md").is_file()
    assert not (box.project / ".kiro").exists() and len(report["written"]) == 4
    records = json.loads(box.manifest.read_text())["entries"]
    assert [r["action"] for r in records] == ["skill"] * 4 and all(r["created_file"] for r in records)
    assert _install(box)["status"] == "unchanged"


def test_install_to_user_scope_uses_only_the_home_variables(box):
    report = _install(box, scope="user", targets=("agents", "claude", "kiro", "github"))
    assert report["status"] == "installed"
    assert (box.home / ".agents" / "skills" / "adapt" / "SKILL.md").is_file()
    assert (box.home / ".claude" / "skills" / "adapt" / "SKILL.md").is_file()
    assert (box.home / ".kiro" / "skills" / "adapt" / "SKILL.md").is_file()
    assert (box.home / ".copilot" / "skills" / "adapt" / "SKILL.md").is_file()
    assert not (box.project / ".agents").exists()


def test_install_needs_yes_and_dry_run_writes_nothing(box):
    before = tree_snapshot(box.root)
    assert _install(box, yes=False)["reason"] == "needs_yes"
    dry = _install(box, dry_run=True, yes=False)
    assert dry["status"] == "planned" and "+name: adapt" in dry["items"][0]["diff"]
    assert "bytes" not in json.dumps(dry["items"])
    assert str(box.root) not in dry["items"][0]["diff"] and "<project>/.agents/skills/adapt/SKILL.md" in dry["items"][0]["diff"]
    assert tree_snapshot(box.root) == before


def test_a_different_existing_skill_file_is_refused_then_replaced_with_a_backup(box):
    target = box.project / ".agents" / "skills" / "adapt" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("my own skill\n")
    before = tree_snapshot(box.root)
    refusal = _install(box)
    assert refusal["status"] == "refused" and refusal["items"][0]["reason"] == "different_file"
    assert tree_snapshot(box.root) == before  # all-or-nothing: the claude copy was not written either
    done = _install(box, replace=True)
    assert done["status"] == "installed"
    backups = [p for p in target.parent.iterdir() if ".adapt-backup-" in p.name]
    assert len(backups) == 1 and backups[0].read_text() == "my own skill\n"
    assert target.read_bytes() == (SKILL_DIR / "SKILL.md").read_bytes()


def test_bad_scope_target_and_missing_folders_are_refused(box):
    assert _install(box, scope="team")["reason"] == "bad_scope"
    assert _install(box, targets=("nope",))["reason"] == "bad_target" and _install(box, targets=())["reason"] == "bad_target"
    no_home = skill.install_skill("user", ("agents",), replace=False, dry_run=False, yes=True, interactive=False, confirm=None,
                                  ctx=box.ctx(environ={"PATH": ""}))
    assert no_home["status"] == "refused" and no_home["items"][0]["reason"] == "no_path"
    no_project = skill.install_skill("project", ("agents",), replace=False, dry_run=False, yes=True, interactive=False,
                                     confirm=None, ctx=box.ctx(project=False))
    assert no_project["status"] == "refused"


def test_skill_install_command(monkeypatch, box):
    for key, value in box.env.items():
        monkeypatch.setenv(key, value)
    blocked = runner.invoke(app, ["agent", "skill", "install", "--project-dir", str(box.project)])
    assert blocked.exit_code == 2 and not (box.project / ".agents").exists()
    done = runner.invoke(app, ["agent", "skill", "install", "--project-dir", str(box.project), "--yes", "--json"])
    assert done.exit_code == 0 and json.loads(done.stdout)["status"] == "installed" and done.stderr == ""
    bad = runner.invoke(app, ["agent", "skill", "install", "--project-dir", str(box.project), "--target", "x", "--yes"])
    assert bad.exit_code == 2


# --- AGENTS.md --------------------------------------------------------------------------------


def _agents(box, **kwargs):
    args = dict(write=True, dry_run=False, yes=True, interactive=False, confirm=None)
    args.update(kwargs)
    return skill.agents_md_report(box.project / "AGENTS.md", ctx=box.ctx(), **args)


def test_agents_block_is_short_and_points_at_the_primer():
    block = skill.agents_block()
    assert block.startswith(skill.AGENTS_BEGIN + "\n") and block.endswith(skill.AGENTS_END + "\n")
    assert len(block.splitlines()) <= 7 and "adapt agent primer" in block and block.isascii()
    printed = runner.invoke(app, ["agent", "agents-md", "print"])
    assert printed.exit_code == 0 and printed.stdout == block.rstrip("\n") + "\n"


def test_agents_md_is_created_updated_in_place_and_idempotent(box):
    path = box.project / "AGENTS.md"
    first = _agents(box)
    assert first["status"] == "written" and path.read_text() == skill.agents_block() and first["backup"] is None
    assert _agents(box)["status"] == "unchanged"
    path.write_text("# Project\n\nSome rules.\n\n" + skill.AGENTS_BEGIN + "\nold text\n" + skill.AGENTS_END + "\n\n## Later\nkeep\n")
    changed = _agents(box)
    text = path.read_text()
    assert changed["status"] == "written" and "old text" not in text and text.startswith("# Project\n\nSome rules.\n\n")
    assert text.endswith("\n\n## Later\nkeep\n") and text.count(skill.AGENTS_BEGIN) == 1
    assert Path(changed["backup"]).is_file() and "old text" in Path(changed["backup"]).read_text()


def test_agents_md_appends_to_a_file_without_markers_and_keeps_crlf(box):
    path = box.project / "AGENTS.md"
    path.write_bytes(b"# Rules\r\nBe kind.")
    report = _agents(box)
    data = path.read_bytes()
    assert report["status"] == "written" and data.startswith(b"# Rules\r\nBe kind.\r\n\r\n" + skill.AGENTS_BEGIN.encode())
    assert data.count(b"\r\n") == data.count(b"\n")


def test_agents_md_refuses_markers_it_cannot_trust_and_needs_yes(box):
    path = box.project / "AGENTS.md"
    for bad in (skill.AGENTS_BEGIN + "\nno end\n", skill.AGENTS_END + "\n" + skill.AGENTS_BEGIN + "\n",
                skill.AGENTS_BEGIN + skill.AGENTS_END + skill.AGENTS_BEGIN + skill.AGENTS_END):
        path.write_text(bad)
        assert _agents(box)["reason"] == "markers" and path.read_text() == bad
    path.unlink()
    assert _agents(box, yes=False)["reason"] == "needs_yes" and not path.exists()
    dry = _agents(box, dry_run=True, yes=False)
    assert dry["status"] == "planned" and "+<!-- adapt:begin -->" in dry["diff"] and not path.exists()


def test_agents_md_command(monkeypatch, box):
    for key, value in box.env.items():
        monkeypatch.setenv(key, value)
    base = ["agent", "agents-md", "--project-dir", str(box.project)]
    assert runner.invoke(app, [*base, "--write"]).exit_code == 2
    done = runner.invoke(app, [*base, "--write", "--yes"])
    assert done.exit_code == 0 and (box.project / "AGENTS.md").read_text() == skill.agents_block()
    other = box.project / "docs"
    other.mkdir()
    assert runner.invoke(app, [*base, "write", "--yes", "--file", str(other / "AI.md")]).exit_code == 0
    assert (other / "AI.md").is_file()
    assert runner.invoke(app, [*base, "bogus"]).exit_code == 2


# --- verify -----------------------------------------------------------------------------------


def test_verify_server_starts_a_child_and_lists_tools_and_resources():
    report = setup.verify_server(sys.executable, ["-m", "adapt_adb", "mcp", "--read-only"], timeout=60)
    assert report["status"] == "ok", report
    assert report["server_name"] == "adapt" and report["tool_count"] == 20 and "help" in report["tools"]
    assert "tap" not in report["tools"] and report["resource_count"] == 11 and report["instructions_chars"] > 500
    assert report["read_only_session"] is True and "No device was contacted" in report["claim_boundary"]


def test_verify_reports_a_timeout_and_a_child_that_is_not_an_mcp_server():
    started = time.monotonic()
    hung = setup.verify_server(sys.executable, ["-c", "import time; time.sleep(60)"], timeout=3)
    assert hung["status"] == "failed" and hung["reason"] in {"timeout", "handshake_failed"} and time.monotonic() - started < 30
    junk = setup.verify_server(sys.executable, ["-c", "print('not mcp')"], timeout=20)
    assert junk["status"] == "failed" and junk["reason"] in {"handshake_failed", "timeout"}
    missing = setup.verify_server(str(Path(sys.executable).parent / "no-such-binary"), ["mcp"], timeout=10)
    assert missing["status"] == "failed"


def test_verify_runs_only_a_registered_command_that_is_adapt_mcp(box, monkeypatch):
    assert setup.registered_command_is_adapt("/x/adapt", ["mcp", "--read-only"])
    assert setup.registered_command_is_adapt("C:/x/adapt.exe", ["mcp"])
    assert not setup.registered_command_is_adapt("/bin/sh", ["-c", "evil"])
    assert not setup.registered_command_is_adapt("/x/adapt", ["setup", "--yes"])
    assert not setup.registered_command_is_adapt("/x/adapt", [])
    box.touch(".cursor/mcp.json", json.dumps({"mcpServers": {"adapt": {"command": "/bin/sh", "args": ["-c", "evil"]}}}))
    ran = []
    monkeypatch.setattr(setup, "verify_server", lambda *a, **k: ran.append(a) or {"status": "ok"})
    report = setup.verify_report("cursor", box.ctx())
    assert report["status"] == "failed" and report["checks"][0]["reason"] == "not_adapt" and ran == []
    assert setup.verify_report("kiro", box.ctx())["status"] == "not_registered"
    assert setup.verify_report("nope", box.ctx())["reason"] == "unknown_client"


def test_verify_a_registered_entry_end_to_end(box, monkeypatch):
    box.touch(".cursor/mcp.json", json.dumps({"mcpServers": {"adapt": {
        "command": sys.executable, "args": ["-m", "adapt_adb", "mcp", "--read-only"]}}}))
    monkeypatch.setattr(setup, "registered_command_is_adapt", lambda command, args: args[-1] == "--read-only")
    report = setup.verify_report("cursor", box.ctx(), timeout=60)
    assert report["status"] == "ok" and report["checks"][0]["scope"] == "user" and report["checks"][0]["tool_count"] == 20


def test_verify_command_json_and_exit_codes(monkeypatch, box):
    for key, value in box.env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(setup, "find_adapt", lambda ctx, adapt_path=None: None)  # fall back to `python -m adapt_adb`
    ok = runner.invoke(app, ["agent", "verify", "--json", "--timeout", "60"])
    assert ok.exit_code == 0 and json.loads(ok.stdout)["checks"][0]["tool_count"] == 20 and ok.stderr == ""
    missing = runner.invoke(app, ["agent", "verify", "kiro", "--project-dir", str(box.project)])
    assert missing.exit_code == 2 and "No `adapt` entry" in missing.stdout


# --- effect classes ---------------------------------------------------------------------------


def test_every_command_has_one_effect_class_in_the_primer_json():
    primer = json.loads(runner.invoke(app, ["agent", "primer", "--json"]).stdout)
    for record in primer["commands"]:
        assert record["effect"] in agent_guide.EFFECT_CLASSES, record["command"]
    effects = {r["command"]: r["effect"] for r in primer["commands"]}
    assert effects["adapt doctor"] == "reads_device" and effects["adapt android versions"] == "reads_host"
    assert effects["adapt dev-options apply"] == "acts_on_device" and effects["adapt setup"] == "changes_host"
    assert effects["adapt agent install"] == "changes_host" and effects["adapt agent verify"] == "reads_host"
    assert effects["adapt agent config"] == "reads_host" and effects["adapt agent skill print"] == "reads_host"
    assert set(agent_guide.COMMAND_EFFECTS) == {c.removeprefix("adapt ") for c in effects}


def test_the_primer_tells_agents_how_to_register_and_the_setup_commands_exist():
    primer = agent_guide.build_primer()
    assert primer["mcp"]["setup_commands"] == list(agent_guide.AGENT_SETUP_COMMANDS)
    assert set(agent_guide.AGENT_SETUP_COMMANDS) <= set(primer["quoted_commands"])
    text = agent_guide.render_markdown(primer)
    assert "adapt agent clients" in text and "| Command | Flags | Effect |" in text
    assert "no client registration has been live-checked" in " ".join(text.split())
