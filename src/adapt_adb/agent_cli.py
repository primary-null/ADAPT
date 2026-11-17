"""The `adapt agent` command family beyond the primer: clients, config, install, uninstall, restore, skill, agents-md, verify.

Commands register on `agent_app` (defined in `agent_guide.py`) when this module is imported by
`cli.py`. Results go to stdout (Markdown-ish text, or one JSON document with `--json`); prompts
and notes go to stderr. Exit codes: 0 done or nothing to do, 1 failed, 2 refused or not confirmed.

Writing a client's settings happens only in `install`, `uninstall`, `restore`, `skill install` and
`agents-md --write`; each shows the change first, backs the file up and needs `--yes` unless it is
run in a terminal and confirmed there. `clients`, `config`, `skill print`, `agents-md print` and
`verify` write nothing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import typer

from adapt_adb import agent_setup as setup
from adapt_adb import agent_skill as skill
from adapt_adb.agent_guide import agent_app
from adapt_adb.agent_setup import Context

skill_app = typer.Typer(help="Print or install the Agent Skill that teaches an agent to use ADAPT.", no_args_is_help=True)
agent_app.add_typer(skill_app, name="skill")

_JSON = typer.Option(False, "--json", help="Emit one JSON document on stdout.")
_PROJECT = typer.Option(None, "--project-dir", help="Project directory for project scope (default: the current directory).")
_ADAPT_PATH = typer.Option(None, "--adapt-path", help="Absolute path of the adapt executable to write at user scope (default: found on PATH).")
_SCOPE = typer.Option("user", "--scope", help="`user` (the user's own config) or `project` (a file in the project directory).")
_DEVICE = typer.Option(None, "--device", help="Restrict the registered server to this exact transport; repeat for several. No serial is written unless given.")
_ALLOW_ACTIONS = typer.Option(False, "--allow-actions", help="Register without --read-only, so the tools that act on the phone are offered.")
_DRY_RUN = typer.Option(False, "--dry-run", help="Show the change and the command; write and run nothing.")
_YES = typer.Option(False, "--yes", help="Approve the change shown. Required when the run is not interactive.")
EXIT_FAILED, EXIT_REFUSED = 1, 2


def _context(project_dir: str | None) -> Context:
    return Context(project_dir=Path(project_dir).resolve() if project_dir else Path.cwd().resolve())


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stderr.isatty()


def _confirm(question: str) -> bool:
    return typer.confirm(question, default=False, err=True)


def _emit(payload: dict[str, Any], json_output: bool, text: str) -> None:
    typer.echo(json.dumps(payload, indent=2) if json_output else text.rstrip("\n"))


def _exit_for(statuses: list[str]) -> int:
    if any(s == "failed" for s in statuses):
        return EXIT_FAILED
    if any(s in {"refused", "declined", "not_registered"} for s in statuses):
        return EXIT_REFUSED
    return 0


# --- clients ---------------------------------------------------------------------------------


@agent_app.command("clients")
def clients_command(json_output: bool = _JSON, project_dir: str = _PROJECT) -> None:
    """Detect installed AI clients and show, per client, its config path and whether adapt is registered."""
    report = setup.clients_report(_context(project_dir))
    lines = ["# AI clients on this computer", "", f"Platform: {report['platform']}. adapt executable: {report['adapt_command'] or 'not found'}.", ""]
    for record in report["clients"]:
        if not record["installed"]:
            continue
        lines.append(f"- **{record['name']}** (`{record['key']}`, {record['status']}): found ({'; '.join(record['detected'])})")
        for scope, item in record["scopes"].items():
            if item["path"] is None and "problem" not in item:
                continue
            if item.get("registered"):
                state = "registered, read-only" if item.get("read_only") else "registered, actions allowed"
            else:
                state = item.get("problem") or "not registered"
            lines.append(f"  - {scope}: `{item['path']}` ({state})")
    absent = [r["key"] for r in report["clients"] if not r["installed"]]
    lines += ["", "Not detected: " + (", ".join(absent) or "none") + ".", "",
              "Print an entry with `adapt agent config CLIENT`; register with `adapt agent install CLIENT --dry-run` first.",
              "", report["claim_boundary"]]
    _emit(report, json_output, "\n".join(lines))


# --- config ----------------------------------------------------------------------------------


@agent_app.command("config")
def config_command(
    client: str = typer.Argument(..., help="Client key from `adapt agent clients`, e.g. claude-code, codex, cursor."),
    scope: str = _SCOPE, device: list[str] | None = _DEVICE, allow_actions: bool = _ALLOW_ACTIONS,
    json_output: bool = _JSON, project_dir: str = _PROJECT, adapt_path: str = _ADAPT_PATH,
) -> None:
    """Print the exact entry (or the client's own command) that registers ADAPT. Writes nothing; read-only by default."""
    report = setup.config_report(client, scope, tuple(device or ()), allow_actions, _context(project_dir), adapt_path)
    if report["status"] != "ok":
        _emit(report, json_output, f"Refused ({report['reason']}): {report['message']}")
        raise typer.Exit(EXIT_REFUSED)
    lines = [f"# {report['name']}: register ADAPT ({scope} scope, {'actions allowed' if allow_actions else 'read-only'})", ""]
    if report["own_command"]:
        lines += ["The client's own command:", "", f"    {report['own_command']}", ""]
    if report["config_path"]:
        lines += [f"Config file: {report['config_path']}", ""]
    if report["install"] == "print-only":
        lines += ["ADAPT never writes this client's settings (print-only).", ""]
    lines += [f"Entry ({report['format']}):", "", *("    " + line for line in report["snippet"].rstrip("\n").split("\n")), ""]
    lines += [f"- {note}" for note in report["notes"]] + ["", report["claim_boundary"]]
    _emit(report, json_output, "\n".join(lines))


# --- install / uninstall / restore -----------------------------------------------------------


def _show_plan(public: dict[str, Any]) -> str:
    lines = [f"## {public['name']} ({public['scope']} scope): {public['status']}"]
    if public.get("method"):
        lines.append(f"Method: {public['method']}" + (f" (`{public['own_command']}`)" if public["method"] == "cli" else ""))
    if public.get("config_path"):
        lines.append(f"Config file: {public['config_path']}")
    if public.get("server_command"):
        lines.append(f"The client will run: {public['server_command']}")
    if public.get("message"):
        lines.append(public["message"])
    if public.get("diff"):
        lines += ["", "```diff", public["diff"], "```"]
    elif public.get("status") == "refused" and public.get("snippet"):
        lines += ["", "Entry to add yourself:", "", *("    " + line for line in public["snippet"].rstrip("\n").split("\n"))]
    lines += [f"- {note}" for note in public.get("notes", [])]
    if public.get("backup"):
        lines.append(f"Backup: {public['backup']}")
    if public.get("manifest_id"):
        lines.append(f"Recorded as {public['manifest_id']} (undo with `adapt agent restore {public['manifest_id']} --yes`).")
    if public.get("warning"):
        lines.append(f"Warning: {public['warning']}")
    return "\n".join(lines)


@agent_app.command("install")
def install_command(
    clients: list[str] = typer.Argument(..., help="One or more client keys; see `adapt agent clients`."),
    scope: str = _SCOPE, device: list[str] | None = _DEVICE, allow_actions: bool = _ALLOW_ACTIONS,
    dry_run: bool = _DRY_RUN, yes: bool = _YES,
    replace: bool = typer.Option(False, "--replace", help="Overwrite a different existing `adapt` entry (the file is backed up first)."),
    method: str = typer.Option("auto", "--method", help="auto (the client's own command when it can express everything), cli, or file."),
    json_output: bool = _JSON, project_dir: str = _PROJECT, adapt_path: str = _ADAPT_PATH,
) -> None:
    """Register ADAPT's MCP server with AI clients. Shows a diff first, backs the file up, and replaces it atomically.

    Never starts, restarts or stops a client and never touches a device. Print-only clients, files that do not parse,
    files with comments and a different existing `adapt` entry are refused with the entry printed for you to add.
    """
    if method not in {"auto", "cli", "file"}:
        typer.echo("Error: --method must be auto, cli or file.", err=True)
        raise typer.Exit(EXIT_REFUSED)
    ctx = _context(project_dir)
    results: list[dict[str, Any]] = []

    def show(text: str) -> None:
        if not json_output:
            typer.echo(text)

    for key in clients:
        planned = setup.plan_install(key, scope=scope, devices=tuple(device or ()), allow_actions=allow_actions,
                                     replace=replace, method=method, ctx=ctx, adapt_path=adapt_path)
        if isinstance(planned, dict):
            results.append(planned)
            show(f"## {key}: refused ({planned['reason']})\n{planned['message']}\n")
            continue
        public = planned.public()
        if dry_run or planned.status != "planned":
            results.append(public)
            show(_show_plan(public) + "\n")
            continue
        show(_show_plan(public) + "\n")  # the whole change is on screen before anyone is asked
        blocked = setup.gate(yes, _interactive(), _confirm, f"Apply this change to {public['name']}?")
        outcome = {**public, **blocked} if blocked else setup.apply_plan(planned, ctx)
        results.append(outcome)
        show((_result_line(outcome) if outcome["status"] == "installed" else _show_plan({**outcome, "diff": ""})) + "\n")
    if json_output:
        typer.echo(json.dumps({"results": results, "claim_boundary": setup.CLAIM_BOUNDARY}, indent=2))
    code = _exit_for([r["status"] for r in results])
    if code:
        raise typer.Exit(code)


def _result_line(outcome: dict[str, Any]) -> str:
    lines = [f"Done: {outcome['name']} ({outcome['scope']} scope) registered."]
    if outcome.get("backup"):
        lines.append(f"Backup: {outcome['backup']}")
    if outcome.get("manifest_id"):
        lines.append(f"Recorded as {outcome['manifest_id']}; undo with `adapt agent restore {outcome['manifest_id']} --yes`.")
    if outcome.get("warning"):
        lines.append(f"Warning: {outcome['warning']}")
    lines.append("Restart or reload the client yourself: ADAPT never starts or stops one.")
    return "\n".join(lines)


@agent_app.command("uninstall")
def uninstall_command(
    clients: list[str] = typer.Argument(..., help="One or more client keys."),
    scope: str = _SCOPE, dry_run: bool = _DRY_RUN, yes: bool = _YES, json_output: bool = _JSON, project_dir: str = _PROJECT,
) -> None:
    """Remove only the `adapt` entry that `adapt agent install` added (the file is backed up first)."""
    ctx = _context(project_dir)
    results = [setup.uninstall_client(key, scope=scope, dry_run=dry_run, yes=yes, interactive=_interactive(),
                                      confirm=_confirm, ctx=ctx) for key in clients]
    lines = []
    for r in results:
        lines.append(f"## {r['client']} ({r['scope']} scope): {r['status']}" + (f" ({r['reason']})" if r.get("reason") else ""))
        for field_name in ("message", "config_path"):
            if r.get(field_name):
                lines.append(str(r[field_name]))
        if r.get("diff"):
            lines += ["", "```diff", r["diff"], "```"]
        if r.get("backup"):
            lines.append(f"Backup: {r['backup']}")
    _emit({"results": results, "claim_boundary": setup.CLAIM_BOUNDARY}, json_output, "\n".join(lines))
    code = _exit_for([r["status"] for r in results])
    if code:
        raise typer.Exit(code)


@agent_app.command("restore")
def restore_command(
    entry_id: str | None = typer.Argument(None, help="Record id from `adapt agent restore --list`."),
    list_all: bool = typer.Option(False, "--list", help="List the changes ADAPT recorded, with their backups."),
    force: bool = typer.Option(False, "--force", help="Restore even though the file changed after ADAPT wrote it."),
    dry_run: bool = _DRY_RUN, yes: bool = _YES, json_output: bool = _JSON,
) -> None:
    """Restore a file from the backup ADAPT recorded (the file as it is now is backed up first)."""
    ctx = Context()
    if list_all or entry_id is None:
        report = setup.restore_list(ctx)
        lines = ["# Changes ADAPT recorded", ""]
        lines += [f"- `{r['id']}`: {r['action']} {r['client']} ({r['scope']}) {r['file']}"
                  + (" [backup present]" if r["backup_exists"] else " [created the file]" if r["created_file"] else " [no backup]")
                  for r in report["entries"]] or ["Nothing recorded yet."]
        if entry_id is None and not list_all:
            lines += ["", "Pass a record id to restore it: `adapt agent restore ID --yes`."]
        _emit(report, json_output, "\n".join(lines))
        raise typer.Exit(0 if report["status"] == "ok" else EXIT_REFUSED)
    result = setup.restore_entry(entry_id, force=force, dry_run=dry_run, yes=yes, interactive=_interactive(),
                                 confirm=_confirm, ctx=ctx)
    lines = [f"Restore {entry_id}: {result['status']}" + (f" ({result['reason']})" if result.get("reason") else "")]
    if result.get("message"):
        lines.append(result["message"])
    if result.get("diff"):
        lines += ["", "```diff", result["diff"], "```"]
    if result.get("backup"):
        lines.append(f"The file as it was is saved at {result['backup']}.")
    _emit(result, json_output, "\n".join(lines))
    code = _exit_for([result["status"]])
    if code:
        raise typer.Exit(code)


# --- skill -----------------------------------------------------------------------------------


@skill_app.command("print")
def skill_print_command(json_output: bool = _JSON) -> None:
    """Print the packaged SKILL.md (what `skill install` copies)."""
    text = skill.skill_text()
    if text is None:
        typer.echo("The skill files are missing from this installation of ADAPT.", err=True)
        raise typer.Exit(EXIT_FAILED)
    if json_output:
        typer.echo(json.dumps({"name": skill.SKILL_NAME, "files": sorted(skill.skill_files()), "skill_md": text}, indent=2))
    else:
        typer.echo(text.rstrip("\n"))


@skill_app.command("install")
def skill_install_command(
    scope: str = typer.Option("project", "--scope", help="`project` (folders in the project directory) or `user` (folders in the home directory)."),
    target: str = typer.Option(",".join(skill.DEFAULT_SKILL_TARGETS), "--target",
                               help="Comma-separated: " + ", ".join(skill.SKILL_TARGETS) + ". agents -> .agents/skills/adapt, claude -> .claude/skills/adapt."),
    replace: bool = typer.Option(False, "--replace", help="Overwrite a different existing skill file (backed up first)."),
    dry_run: bool = _DRY_RUN, yes: bool = _YES, json_output: bool = _JSON, project_dir: str = _PROJECT,
) -> None:
    """Copy the Agent Skill to .agents/skills/adapt and .claude/skills/adapt (or the user-scope equivalents)."""
    targets = tuple(t.strip() for t in target.split(",") if t.strip())
    report = skill.install_skill(scope, targets, replace=replace, dry_run=dry_run, yes=yes, interactive=_interactive(),
                                 confirm=_confirm, ctx=_context(project_dir))
    lines = [f"Skill install ({scope} scope): {report['status']}" + (f" ({report['reason']})" if report.get("reason") else "")]
    if report.get("message"):
        lines.append(report["message"])
    done = report["status"] == "installed"
    for item in report.get("items", []):
        state = "written" if done and item["status"] == "planned" else item["status"]
        lines.append(f"- {state}: {item.get('path', item['target'])}" + (f" ({item['reason']})" if item.get("reason") else ""))
        if not done and item.get("diff") and item["status"] in {"planned", "refused"} and item["file"] == "SKILL.md":
            lines += ["", "```diff", item["diff"], "```"]
    for written in report.get("written", []):
        lines.append(f"Wrote {written['path']}" + (f" (backup {written['backup']})" if written["backup"] else ""))
    _emit(report, json_output, "\n".join(lines))
    code = _exit_for([report["status"]])
    if code:
        raise typer.Exit(code)


# --- AGENTS.md -------------------------------------------------------------------------------


@agent_app.command("agents-md")
def agents_md_command(
    action: str = typer.Argument("print", help="`print` shows the block; `write` inserts or replaces it (same as --write)."),
    write: bool = typer.Option(False, "--write", help="Insert or replace the block in AGENTS.md. Needs --yes unless run in a terminal."),
    file: str = typer.Option(None, "--file", help="File to change (default: AGENTS.md in the project directory)."),
    dry_run: bool = _DRY_RUN, yes: bool = _YES, json_output: bool = _JSON, project_dir: str = _PROJECT,
) -> None:
    """Print, or write, the short block between <!-- adapt:begin --> and <!-- adapt:end --> that points at `adapt agent primer`."""
    if action not in {"print", "write"}:
        typer.echo("Error: the action is `print` or `write`.", err=True)
        raise typer.Exit(EXIT_REFUSED)
    ctx = _context(project_dir)
    path = Path(file).resolve() if file else (ctx.project_dir / "AGENTS.md" if ctx.project_dir else None)
    report = skill.agents_md_report(path, write=write or action == "write", dry_run=dry_run, yes=yes,
                                    interactive=_interactive(), confirm=_confirm, ctx=ctx)
    lines = [report["block"]] if "diff" not in report and report["status"] == "ok" else [f"AGENTS.md: {report['status']}"]
    if report.get("message"):
        lines.append(report["message"])
    if report.get("diff"):
        lines += ["", "```diff", report["diff"], "```"]
    if report.get("backup"):
        lines.append(f"Backup: {report['backup']}")
    _emit(report, json_output, "\n".join(lines))
    code = _exit_for([report["status"]])
    if code:
        raise typer.Exit(code)


# --- verify ----------------------------------------------------------------------------------


@agent_app.command("verify")
def verify_command(
    client: str | None = typer.Argument(None, help="Client key: verify the `adapt` entry registered there. Omit to verify this installation."),
    timeout: float = typer.Option(setup.VERIFY_TIMEOUT_S, "--timeout", min=1.0, max=120.0, help="Seconds before the child process is given up on."),
    json_output: bool = _JSON, project_dir: str = _PROJECT, adapt_path: str = _ADAPT_PATH,
) -> None:
    """Start `adapt mcp` as a child process, run the MCP handshake and tools/list, and report. Contacts no device."""
    report = setup.verify_report(client, _context(project_dir), adapt_path, timeout)
    lines = [f"verify: {report['status']}"]
    if report.get("message"):
        lines.append(report["message"])
    for check in report.get("checks", []):
        head = f"- {check.get('scope', 'this installation')}: {check['status']}"
        if check["status"] == "ok":
            head += (f", server {check['server_name']} {check['server_version']}, {check['tool_count']} tools, "
                     f"{check['resource_count']} resources, instructions {check['instructions_chars']} characters")
        lines.append(head)
        if check.get("message"):
            lines.append("  " + check["message"])
        lines.append("  " + check.get("command", ""))
    lines.append("")
    lines.append("No device was contacted and no AI client was involved.")
    _emit(report, json_output, "\n".join(lines))
    code = {"ok": 0, "failed": EXIT_FAILED}.get(report["status"], EXIT_REFUSED)
    if code:
        raise typer.Exit(code)
