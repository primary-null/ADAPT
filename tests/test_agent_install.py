"""`adapt agent install`, `uninstall` and `restore`: diff first, backup, atomic write, refusals (offline, tmp folders only)."""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from adapt_adb import agent_files as af
from adapt_adb import agent_setup as setup
from adapt_adb.cli import app

from tests.agent_helpers import Sandbox, tree_snapshot

runner = CliRunner()


@pytest.fixture
def box(tmp_path):
    sandbox = Sandbox(tmp_path)
    sandbox.on_path = set()
    return sandbox


def plan(box, key, *, scope="user", devices=(), allow_actions=False, replace=False, method="auto", **ctx_args):
    return setup.plan_install(key, scope=scope, devices=devices, allow_actions=allow_actions, replace=replace,
                              method=method, ctx=box.ctx(**ctx_args), adapt_path=box.adapt)


def install(box, key, **kwargs):
    planned = plan(box, key, **kwargs)
    assert isinstance(planned, setup.Plan), planned
    assert planned.status == "planned", planned.public()
    return setup.apply_plan(planned, box.ctx())


def refused(box, key, reason, **kwargs):
    before = tree_snapshot(box.root)
    planned = plan(box, key, **kwargs)
    public = planned if isinstance(planned, dict) else planned.public()
    assert public["status"] == "refused" and public["reason"] == reason, public
    assert tree_snapshot(box.root) == before, "a refused install must change nothing"
    return public


# --- writing a JSON file ----------------------------------------------------------------------


def test_new_project_file_is_created_with_a_bare_adapt_and_no_serial(box):
    box.touch(".cursor/mcp.json", "{}")  # Cursor is installed
    result = install(box, "cursor", scope="project")
    path = box.project / ".cursor" / "mcp.json"
    data = json.loads(path.read_text())
    assert data == {"mcpServers": {"adapt": {"type": "stdio", "command": "adapt", "args": ["mcp", "--read-only"]}}}
    assert "SERIAL" not in path.read_text() and str(box.root) not in path.read_text()
    assert result["status"] == "installed" and result["backup"] is None
    record = json.loads(box.manifest.read_text())["entries"][0]
    assert record["created_file"] is True and record["file"] == str(path) and record["client"] == "cursor"
    assert not [p for p in box.project.rglob("*") if p.name.endswith(".adapt-tmp")]


def test_user_scope_merges_keeps_other_servers_backs_up_and_records(box):
    original = '{\n  "theme": "dark",\n  "mcpServers": {\n    "other": {"command": "x"}\n  }\n}\n'
    path = box.touch(".cursor/mcp.json", original)
    planned = plan(box, "cursor")
    assert "+    \"adapt\": {" in planned.diff and "--- a/<home>/.cursor/mcp.json" in planned.diff
    # The diff names the file as <home>/...; only the command line carries the absolute adapt path,
    # by design at user scope (JSON escapes it on Windows, so compare the header lines).
    headers = [line for line in planned.diff.splitlines() if line.startswith(("--- ", "+++ "))]
    assert headers and all(str(box.root) not in line and "<home>" in line for line in headers)
    assert path.read_text() == original  # planning wrote nothing
    result = setup.apply_plan(planned, box.ctx())
    data = json.loads(path.read_text())
    assert data["theme"] == "dark" and data["mcpServers"]["other"] == {"command": "x"}
    assert data["mcpServers"]["adapt"]["command"] == box.adapt  # the absolute path at user scope
    assert data["mcpServers"]["adapt"]["args"] == ["mcp", "--read-only"]
    backup = Path(result["backup"])
    assert backup.name.startswith("mcp.json.adapt-backup-20261116T1200") and backup.read_text() == original
    record = json.loads(box.manifest.read_text())["entries"][0]
    assert record["backup"] == str(backup) and record["created_file"] is False
    assert record["sha256_after"] == af.sha256_bytes(path.read_bytes())
    assert path.read_text().endswith("\n") and "\r" not in path.read_text()


def test_installing_twice_is_unchanged_and_writes_nothing(box):
    box.touch(".cursor/mcp.json", "{}")
    install(box, "cursor")
    before = tree_snapshot(box.root)
    again = plan(box, "cursor")
    assert again.status == "unchanged" and "already registered" in again.message
    assert setup.apply_plan(again, box.ctx())["status"] == "unchanged"
    assert tree_snapshot(box.root) == before


def test_crlf_and_bom_and_tab_indent_are_preserved(box):
    original = b'\xef\xbb\xbf{\r\n\t"mcpServers": {\r\n\t\t"other": {"command": "x"}\r\n\t}\r\n}\r\n'
    path = box.touch(".cursor/mcp.json", original)
    install(box, "cursor")
    data = path.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf{") and b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")
    assert b'\t"adapt"' in data or b'\t\t"adapt"' in data
    assert json.loads(data.decode("utf-8-sig"))["mcpServers"]["adapt"]["command"] == box.adapt


def test_a_serial_is_written_only_when_given_and_actions_only_when_asked(box):
    box.touch(".cursor/mcp.json", "{}")
    install(box, "cursor", devices=("SERIAL_A", "SERIAL_B"), allow_actions=True)
    args = json.loads((box.home / ".cursor" / "mcp.json").read_text())["mcpServers"]["adapt"]["args"]
    assert args == ["mcp", "--device", "SERIAL_A", "--device", "SERIAL_B"]
    assert "--read-only" not in args


def test_bad_devices_are_refused_before_anything_is_read(box):
    box.touch(".cursor/mcp.json", "{}")
    for bad in ("-s", "", " x", "a\nb"):
        public = refused(box, "cursor", "bad_device", devices=(bad,))
        assert "complete exact target" in public["message"]


# --- refusals ---------------------------------------------------------------------------------


@pytest.mark.parametrize("content,reason", [
    ("{ not json", "unparseable"),
    ('{"mcpServers": {"a": 1,}}', "unparseable"),
    ('// my servers\n{"mcpServers": {}}', "comments"),
    ('{"mcpServers": {} /* keep */}', "comments"),
    ("[1, 2]", "shape"),
    ('{"mcpServers": ["x"]}', "shape"),
])
def test_a_file_that_cannot_be_edited_safely_is_refused_untouched(box, content, reason):
    box.touch(".cursor/mcp.json", content)
    public = refused(box, "cursor", reason)
    assert "Nothing was changed" in public["message"] and "mcpServers" in public["snippet"]


def test_a_non_utf8_file_is_refused(box):
    box.touch(".cursor/mcp.json", b"\xff\xfe{}")
    refused(box, "cursor", "not_utf8")


def test_a_different_adapt_entry_needs_replace_and_replace_backs_it_up(box):
    original = json.dumps({"mcpServers": {"adapt": {"command": "/old/adapt", "args": ["mcp"]}, "keep": {"command": "k"}}})
    path = box.touch(".cursor/mcp.json", original)
    public = refused(box, "cursor", "different_entry")
    assert "--replace" in public["message"]
    result = install(box, "cursor", replace=True)
    data = json.loads(path.read_text())
    assert data["mcpServers"]["adapt"]["command"] == box.adapt and data["mcpServers"]["keep"] == {"command": "k"}
    assert Path(result["backup"]).read_text() == original
    assert json.loads(box.manifest.read_text())["entries"][0]["previous_entry"] == {"command": "/old/adapt", "args": ["mcp"]}


def test_unknown_print_only_and_not_installed_clients_are_refused(box):
    assert refused(box, "nope", "unknown_client")["message"].startswith("Unknown client")
    for key in ("zed", "goose", "windsurf", "continue", "jetbrains", "junie", "grok"):
        box.on_path = {key}  # even when its executable exists
        public = refused(box, key, "print_only")
        assert public["snippet"] and "never writes" in public["message"]
    box.on_path = set()
    refused(box, "kiro", "not_detected")
    refused(box, "cursor", "not_detected", scope="project")  # no executable and no folder: never register a missing client


def test_a_missing_scope_or_path_is_refused(box):
    box.on_path = {"roo-code"}
    refused(box, "roo-code", "no_scope")  # Roo Code's global file path is not known
    box.on_path = {"codex"}
    public = plan(box, "codex", scope="project", project=False)
    assert public["reason"] == "no_project"


def test_with_no_home_variable_nothing_is_guessed_or_written(box):
    before = tree_snapshot(box.root)
    public = plan(box, "cursor", environ={"PATH": ""}, project=False)
    public = public if isinstance(public, dict) else public.public()
    assert public["status"] == "refused" and before == tree_snapshot(box.root)


def test_user_scope_without_an_adapt_executable_is_refused(box, monkeypatch):
    box.touch(".cursor/mcp.json", "{}")
    monkeypatch.setattr(setup, "find_adapt", lambda ctx, adapt_path=None: None)
    public = setup.plan_install("cursor", scope="user", devices=(), allow_actions=False, replace=False, method="auto", ctx=box.ctx())
    assert public["reason"] == "no_adapt" and "--adapt-path" in public["message"]
    project = setup.plan_install("cursor", scope="project", devices=(), allow_actions=False, replace=False, method="auto", ctx=box.ctx())
    assert isinstance(project, dict) or project.status != "refused" or project.reason != "no_adapt"


# --- TOML (Codex) -----------------------------------------------------------------------------


def test_codex_toml_keeps_comments_and_other_tables_and_sets_the_timeout(box):
    original = "# my settings\nmodel = \"x\"  # inline\n\n[mcp_servers.other]\ncommand = \"foo\"\n\n[profiles.fast]\nmodel = \"y\"\n"
    path = box.touch(".codex/config.toml", original)
    result = install(box, "codex")
    text = path.read_text()
    assert text.startswith("# my settings\nmodel = \"x\"  # inline\n")
    data = tomllib.loads(text)
    assert data["mcp_servers"]["other"] == {"command": "foo"} and data["profiles"]["fast"] == {"model": "y"}
    assert data["mcp_servers"]["adapt"] == {"command": box.adapt, "args": ["mcp", "--read-only"], "tool_timeout_sec": 180}
    assert Path(result["backup"]).read_text() == original
    assert box.commands == []  # auto: Codex's own command cannot set the timeout, so the file was edited


def test_codex_replace_edits_only_its_own_table_and_refuses_comments_inside_it(box):
    old = "[mcp_servers.adapt]\ncommand = \"old\"\n\n[mcp_servers.adapt.env]\nA = \"1\"\n\n[other]\nx = 1\n"
    path = box.touch(".codex/config.toml", "# top\n" + old)
    refused(box, "codex", "different_entry")
    install(box, "codex", replace=True)
    data = tomllib.loads(path.read_text())
    assert data["mcp_servers"]["adapt"]["command"] == box.adapt and "env" not in data["mcp_servers"]["adapt"]
    assert data["other"] == {"x": 1} and path.read_text().startswith("# top\n")
    path.write_text("[mcp_servers.adapt]\n# my note\ncommand = \"old\"\n")
    refused(box, "codex", "comments", replace=True)


def test_codex_unusual_layouts_and_bad_files_are_refused(box):
    box.touch(".codex/config.toml", "[mcp_servers]\nadapt = { command = \"old\" }\n")
    refused(box, "codex", "layout", replace=True)
    box.touch(".codex/config.toml", "model = [")
    refused(box, "codex", "unparseable")


def test_codex_crlf_is_kept(box):
    path = box.touch(".codex/config.toml", b"model = \"x\"\r\n")
    install(box, "codex")
    raw = path.read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n") >= 4 and tomllib.loads(raw.decode())["mcp_servers"]["adapt"]


def test_timeouts_for_roo_and_pi(box):
    box.touch(".pi/x", "")
    box.on_path = set()
    (box.project / ".roo").mkdir()
    install(box, "roo-code", scope="project")
    assert json.loads((box.project / ".roo" / "mcp.json").read_text())["mcpServers"]["adapt"]["timeout"] == 180
    install(box, "pi")
    assert json.loads((box.home / ".pi" / "agent" / "mcp.json").read_text())["mcpServers"]["adapt"]["timeout"] == 180


# --- the client's own command -----------------------------------------------------------------


def test_the_clients_own_command_is_preferred_and_the_config_file_is_untouched(box):
    box.on_path = {"claude"}
    before = tree_snapshot(box.root)
    result = install(box, "claude-code", devices=("SERIAL",))
    # The program PATH lookup found is run, so a Windows `name.cmd` shim works too.
    assert box.commands == [[box.which("claude"), "mcp", "add", "--scope", "user", "adapt", "--", box.adapt, "mcp", "--read-only", "--device", "SERIAL"]]
    assert result["status"] == "installed" and result["method"] == "cli"
    assert not (box.home / ".claude.json").exists()
    after = tree_snapshot(box.root)
    assert set(after) - set(before) == {"config/agent-installs.json"}
    assert json.loads(box.manifest.read_text())["entries"][0]["method"] == "cli"


def test_claude_code_user_scope_without_its_command_is_refused_not_edited(box):
    box.touch(".claude/settings.json", "{}")  # the folder exists, the command does not
    public = refused(box, "claude-code", "no_method")
    assert "not on PATH" in public["message"] and "claude mcp add" in public["message"] + public["snippet"] + str(public["own_command"])


def test_claude_code_project_scope_edits_mcp_json(box):
    box.on_path = {"claude"}
    result = install(box, "claude-code", scope="project", method="file")
    assert result["method"] == "file" and box.commands == []
    assert json.loads((box.project / ".mcp.json").read_text())["mcpServers"]["adapt"]["command"] == "adapt"


def test_method_cli_is_honoured_for_codex_and_pi_with_a_note_about_the_timeout(box):
    box.on_path = {"codex"}
    planned = plan(box, "codex", method="cli")
    assert planned.method == "cli" and any("tool_timeout_sec" in n for n in planned.notes)
    setup.apply_plan(planned, box.ctx())
    assert box.commands[0][:4] == [box.which("codex"), "mcp", "add", "adapt"]
    box.touch(".cursor/mcp.json", "{}")  # Cursor is installed, but has no registration command
    cursor = plan(box, "cursor", method="cli")
    assert cursor.reason == "no_method" and "no registration command" in cursor.message


def test_a_failed_client_command_records_nothing(box):
    box.on_path = {"claude"}
    box.run_code = 3
    planned = plan(box, "claude-code")
    result = setup.apply_plan(planned, box.ctx())
    assert result["status"] == "failed" and result["reason"] == "cli_failed" and not box.manifest.exists()


def test_cline_opencode_and_amp_user_scope_are_registered_through_their_own_command_only(box):
    for key, exe in (("cline", "cline"), ("opencode", "opencode"), ("amp", "amp")):
        box.on_path = {exe}
        planned = plan(box, key)
        assert planned.method == "cli" and planned.cli_argv[0] == exe, key
        by_file = plan(box, key, method="file")
        assert by_file.status == "refused" and by_file.reason == "no_method", key


# --- failures leave the original alone --------------------------------------------------------


def test_a_failed_atomic_replace_keeps_the_original_and_cleans_up(box, monkeypatch):
    original = '{"mcpServers": {}}\n'
    path = box.touch(".cursor/mcp.json", original)
    planned = plan(box, "cursor")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    result = setup.apply_plan(planned, box.ctx())
    assert result["status"] == "failed" and result["reason"] == "write_failed"
    assert path.read_text() == original and not box.manifest.exists()
    assert not [p for p in path.parent.iterdir() if p.name.endswith(".adapt-tmp")]


def test_a_broken_manifest_stops_the_install_before_any_write(box):
    path = box.touch(".cursor/mcp.json", "{}")
    box.manifest.write_text("{ broken")
    planned = plan(box, "cursor")
    result = setup.apply_plan(planned, box.ctx())
    assert result["status"] == "failed" and result["reason"] == "manifest"
    assert path.read_text() == "{}" and box.manifest.read_text() == "{ broken"


def test_install_by_file_runs_no_process_and_never_touches_the_real_profile(box, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("no process may start")

    monkeypatch.setattr("subprocess.run", refuse)
    monkeypatch.setattr("subprocess.Popen", refuse)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: (_ for _ in ()).throw(AssertionError("real home read"))))
    box.touch(".cursor/mcp.json", "{}")
    box.touch(".kiro/x", "")
    install(box, "cursor")
    install(box, "kiro")
    assert (box.home / ".kiro" / "settings" / "mcp.json").is_file()


def test_the_symlink_is_followed_and_kept(box):
    target = box.touch("dotfiles/mcp.json", "{}")
    link = box.home / ".cursor" / "mcp.json"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")
    install(box, "cursor")
    assert link.is_symlink() and json.loads(target.read_text())["mcpServers"]["adapt"]


def test_a_second_client_sharing_the_project_file_is_reported_unchanged(box):
    box.on_path = {"claude", "code"}
    install(box, "claude-code", scope="project", method="file")
    again = plan(box, "vscode", scope="project")
    # Claude Code writes `type: stdio` too, so VS Code's entry is the same and nothing is rewritten.
    assert again.status == "unchanged"


# --- uninstall and restore --------------------------------------------------------------------


def test_uninstall_removes_only_what_adapt_added_and_restore_undoes_it(box):
    original = '{\n  "mcpServers": {\n    "other": {"command": "x"}\n  }\n}\n'
    path = box.touch(".cursor/mcp.json", original)
    install(box, "cursor")
    ctx = box.ctx()
    dry = setup.uninstall_client("cursor", scope="user", dry_run=True, yes=False, interactive=False, confirm=None, ctx=ctx)
    assert dry["status"] == "planned" and "-    \"adapt\"" in dry["diff"] or "-      \"adapt\"" in dry["diff"]
    needs = setup.uninstall_client("cursor", scope="user", dry_run=False, yes=False, interactive=False, confirm=None, ctx=ctx)
    assert needs["reason"] == "needs_yes"
    done = setup.uninstall_client("cursor", scope="user", dry_run=False, yes=True, interactive=False, confirm=None, ctx=ctx)
    assert done["status"] == "removed"
    assert json.loads(path.read_text()) == {"mcpServers": {"other": {"command": "x"}}}
    again = setup.uninstall_client("cursor", scope="user", dry_run=False, yes=True, interactive=False, confirm=None, ctx=ctx)
    assert again["reason"] == "not_ours"
    listing = setup.restore_list(ctx)["entries"]
    assert [e["action"] for e in listing] == ["install", "uninstall"] and all(e["backup_exists"] for e in listing)
    # Restoring the install record puts the original back; the file changed since, so --force is needed.
    refusal = setup.restore_entry(listing[0]["id"], force=False, dry_run=False, yes=True, interactive=False, confirm=None, ctx=ctx)
    assert refusal["reason"] == "changed_since" and "adapt" not in path.read_text()
    restored = setup.restore_entry(listing[0]["id"], force=True, dry_run=False, yes=True, interactive=False, confirm=None, ctx=ctx)
    assert restored["status"] == "restored" and path.read_text() == original
    assert Path(restored["backup"]).is_file()  # the file as it was before the restore is kept too


def test_restore_without_force_succeeds_when_nothing_changed_and_deletes_a_created_file(box):
    box.touch(".cursor/x", "")
    install(box, "cursor", scope="project")
    created = box.project / ".cursor" / "mcp.json"
    assert created.is_file()
    ctx = box.ctx()
    record = setup.restore_list(ctx)["entries"][0]
    assert record["created_file"] is True
    dry = setup.restore_entry(record["id"], force=False, dry_run=True, yes=False, interactive=False, confirm=None, ctx=ctx)
    assert dry["status"] == "planned" and dry["removes_file"] and created.is_file()
    assert setup.restore_entry(record["id"], force=False, dry_run=False, yes=False, interactive=False, confirm=None, ctx=ctx)["reason"] == "needs_yes"
    done = setup.restore_entry(record["id"], force=False, dry_run=False, yes=True, interactive=False, confirm=None, ctx=ctx)
    assert done["status"] == "restored" and not created.exists()


def test_uninstall_of_a_created_file_removes_the_file_and_a_modified_entry_is_kept(box):
    box.touch(".cursor/x", "")
    install(box, "cursor", scope="project")
    created = box.project / ".cursor" / "mcp.json"
    ctx = box.ctx()
    data = json.loads(created.read_text())
    data["mcpServers"]["adapt"]["args"].append("--device")
    created.write_text(json.dumps(data))
    kept = setup.uninstall_client("cursor", scope="project", dry_run=False, yes=True, interactive=False, confirm=None, ctx=ctx)
    assert kept["reason"] == "modified" and created.is_file()
    data["mcpServers"]["adapt"]["args"].pop()
    created.write_text(json.dumps(data, indent=2) + "\n")
    done = setup.uninstall_client("cursor", scope="project", dry_run=False, yes=True, interactive=False, confirm=None, ctx=ctx)
    assert done["status"] == "removed" and done["removes_file"] and not created.exists()


def test_uninstall_of_a_cli_install_is_a_manual_step_and_unknown_clients_are_refused(box):
    box.on_path = {"claude"}
    install(box, "claude-code")
    done = setup.uninstall_client("claude-code", scope="user", dry_run=False, yes=True, interactive=False, confirm=None, ctx=box.ctx())
    assert done["status"] == "manual" and "client itself" in done["message"] and box.commands.__len__() == 1
    assert setup.uninstall_client("nope", scope="user", dry_run=False, yes=True, interactive=False, confirm=None, ctx=box.ctx())["reason"] == "unknown_client"


def test_toml_uninstall_round_trips(box):
    original = "# top\nmodel = \"x\"\n\n[mcp_servers.other]\ncommand = \"foo\"\n"
    path = box.touch(".codex/config.toml", original)
    install(box, "codex")
    done = setup.uninstall_client("codex", scope="user", dry_run=False, yes=True, interactive=False, confirm=None, ctx=box.ctx())
    assert done["status"] == "removed"
    assert tomllib.loads(path.read_text()) == tomllib.loads(original) and path.read_text().startswith("# top\n")


def test_restore_unknown_ids_and_missing_backups_are_refused(box):
    ctx = box.ctx()
    assert setup.restore_entry("nope", force=False, dry_run=False, yes=True, interactive=False, confirm=None, ctx=ctx)["reason"] == "unknown_entry"
    path = box.touch(".cursor/mcp.json", "{}")
    install(box, "cursor")
    record = setup.restore_list(ctx)["entries"][0]
    Path(record["backup"]).unlink()
    assert setup.restore_entry(record["id"], force=True, dry_run=False, yes=True, interactive=False, confirm=None, ctx=ctx)["reason"] == "backup_missing"
    assert "adapt" in path.read_text()


def test_confirmation_prompt_is_used_when_interactive(box):
    box.touch(".cursor/mcp.json", "{}")
    install(box, "cursor")
    ctx = box.ctx()
    asked = []
    declined = setup.uninstall_client("cursor", scope="user", dry_run=False, yes=False, interactive=True,
                                      confirm=lambda q: asked.append(q) or False, ctx=ctx)
    assert declined["status"] == "declined" and asked and "adapt" in (box.home / ".cursor" / "mcp.json").read_text()
    accepted = setup.uninstall_client("cursor", scope="user", dry_run=False, yes=False, interactive=True, confirm=lambda q: True, ctx=ctx)
    assert accepted["status"] == "removed"


# --- the command line -------------------------------------------------------------------------


def _env(monkeypatch, box):
    for key, value in box.env.items():
        monkeypatch.setenv(key, value)


def run(monkeypatch, box, *args, project=True):
    _env(monkeypatch, box)
    return runner.invoke(app, ["agent", *args, *(["--project-dir", str(box.project)] if project else [])])


def test_cli_dry_run_shows_the_diff_and_writes_nothing(monkeypatch, box):
    box.touch(".cursor/mcp.json", "{}")
    before = tree_snapshot(box.root)
    result = run(monkeypatch, box, "install", "cursor", "--dry-run", "--adapt-path", box.adapt)
    assert result.exit_code == 0 and result.stderr == ""
    assert "```diff" in result.stdout and "The client will run:" in result.stdout and box.adapt in result.stdout
    assert tree_snapshot(box.root) == before


def test_cli_needs_yes_when_not_interactive_then_writes_with_yes(monkeypatch, box):
    box.touch(".cursor/mcp.json", "{}")
    before = tree_snapshot(box.root)
    blocked = run(monkeypatch, box, "install", "cursor", "--adapt-path", box.adapt)
    assert blocked.exit_code == 2 and "--yes" in blocked.stdout and tree_snapshot(box.root) == before
    done = run(monkeypatch, box, "install", "cursor", "--yes", "--adapt-path", box.adapt)
    assert done.exit_code == 0 and "Done: Cursor" in done.stdout and "restore" in done.stdout
    assert "adapt" in json.loads((box.home / ".cursor" / "mcp.json").read_text())["mcpServers"]


def test_cli_json_is_one_clean_document_and_exit_codes_follow_the_result(monkeypatch, box):
    box.touch(".cursor/mcp.json", "{}")
    result = run(monkeypatch, box, "install", "cursor", "zed", "--dry-run", "--json", "--adapt-path", box.adapt)
    assert result.exit_code == 2 and result.stderr == ""  # zed is print-only
    payload = json.loads(result.stdout)
    assert [r["status"] for r in payload["results"]] == ["planned", "refused"]
    assert payload["results"][1]["reason"] == "print_only" and "context_servers" in payload["results"][1]["snippet"]
    assert run(monkeypatch, box, "install", "cursor", "--method", "bogus").exit_code == 2


def test_cli_restore_and_uninstall_round_trip(monkeypatch, box):
    path = box.touch(".cursor/mcp.json", '{"mcpServers": {}}\n')
    run(monkeypatch, box, "install", "cursor", "--yes", "--adapt-path", box.adapt)
    listing = run(monkeypatch, box, "restore", "--list", "--json", project=False)
    entries = json.loads(listing.stdout)["entries"]
    assert listing.exit_code == 0 and entries[0]["client"] == "cursor"
    no_yes = run(monkeypatch, box, "uninstall", "cursor")
    assert no_yes.exit_code == 2 and "adapt" in path.read_text()
    assert run(monkeypatch, box, "uninstall", "cursor", "--yes").exit_code == 0
    assert "adapt" not in json.loads(path.read_text())["mcpServers"]
    again = run(monkeypatch, box, "restore", entries[0]["id"], "--yes", "--force", project=False)
    assert again.exit_code == 0 and path.read_text() == '{"mcpServers": {}}\n'


def test_the_clients_own_command_runs_the_program_path_lookup_found(box):
    """A client installed as an npm `.cmd` shim on Windows is found by PATH lookup, not by its bare name."""
    box.on_path = {"opencode"}
    box.touch(".config/opencode/opencode.jsonc", "{}")
    result = install(box, "opencode")
    assert result["status"] == "installed"
    assert box.commands and box.commands[0][0] == box.which("opencode") and box.commands[0][1:3] == ["mcp", "add"]
