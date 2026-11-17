"""Register ADAPT's MCP server with AI clients: `clients`, `config`, `install`, `uninstall`, `restore`, `verify`.

Plan first, then act. `plan_install` reads files and builds a report (a diff, the command the
client will run, a refusal reason); only `apply_plan` writes, and only after the caller has the
user's confirmation. A write is: back the file up to `<file>.adapt-backup-<UTC>`, replace it
atomically, record the backup in ADAPT's manifest (`agent-installs.json`). ADAPT never starts,
restarts or stops a client and never touches a device.

Nothing here raises. Every outcome is a report dict with a `status` (`planned`, `installed`,
`unchanged`, `refused`, `failed`, ...) and, when it did not go ahead, a `reason` code and a
`message`. All paths come from the `Context` (environment variables and an explicit project
directory), so a test can aim the whole module at a temporary folder.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from adapt_adb import agent_clients as ac
from adapt_adb import agent_files as af
from adapt_adb.agent_clients import ENTRY_NAME, Client

RUN_TIMEOUT_S = 60
OUTPUT_LIMIT = 600
VERIFY_TIMEOUT_S = 20.0
CLAIM_BOUNDARY = (
    "Built offline from the installed ADAPT and each client's documented stdio shape. No client "
    "registration has been live-checked, and no client was started or stopped."
)


def _default_runner(argv: list[str], cwd: str | None, timeout: float) -> tuple[int, str]:
    try:
        done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, shell=False)
    except subprocess.TimeoutExpired:
        return 124, "the client's command did not finish in time"
    except OSError as exc:
        return 127, f"could not run the client's command ({exc.__class__.__name__})"
    return done.returncode, (done.stdout + done.stderr).strip()[:OUTPUT_LIMIT]


@dataclass
class Context:
    """Everything outside input: environment, project folder, PATH lookup, command runner, clock."""

    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    project_dir: Path | None = None
    platform: str = sys.platform
    which: Callable[[str], str | None] = shutil.which
    runner: Callable[[list[str], str | None, float], tuple[int, str]] = _default_runner
    now: Callable[[], datetime] | None = None

    @property
    def tokens(self) -> dict[str, str | None]:
        return ac.path_tokens(self.environ, self.project_dir, self.platform)

    @property
    def windows(self) -> bool:
        return ac.platform_key(self.platform) == "windows"

    def stamp(self) -> str:
        return af.utc_stamp(self.now() if self.now else None)

    def manifest(self) -> Path | None:
        return af.manifest_path(self.environ, self.windows)


# --- JSON and TOML text helpers --------------------------------------------------------------


def strip_jsonc(text: str) -> tuple[str, bool]:
    """Remove // and /* */ comments (outside strings) and trailing commas; also say whether a comment was seen."""
    out: list[str] = []
    i, n, had = 0, len(text), False
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
            out.append(ch)
        elif text.startswith("//", i):
            had = True
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            had = True
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 1
        else:
            out.append(ch)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out)), had


def parse_json_document(text: str) -> tuple[dict[str, Any] | None, str | None]:
    """(object, None) for plain JSON, ({}, None) for a blank file, else (None, reason code)."""
    if not text.strip():
        return {}, None
    try:
        data = json.loads(text)
    except ValueError:
        try:
            json.loads(strip_jsonc(text)[0])
        except ValueError:
            return None, "unparseable"
        return None, "comments" if strip_jsonc(text)[1] else "unparseable"
    return (data, None) if isinstance(data, dict) else (None, "shape")


def _indent_of(text: str) -> str:
    match = re.search(r'^([ \t]+)"', text, re.M)
    return match.group(1) if match else "  "


def _dump_json(data: dict[str, Any], original: str | None) -> str:
    text = json.dumps(data, indent=_indent_of(original or ""), ensure_ascii=False)
    newline = af.newline_of(original or "")
    return af.to_newline(text, newline) + (newline if original is None or original.endswith("\n") or not original.strip() else "")


def _navigate(data: dict[str, Any], path: tuple[str, ...], create: bool) -> dict[str, Any] | None:
    node = data
    for key in path:
        nxt = node.get(key)
        if nxt is None and create:
            nxt = node[key] = {}
        if not isinstance(nxt, dict):
            return None
        node = nxt
    return node


@dataclass
class Merge:
    status: str                   # changed | unchanged | refused
    text: str | None = None       # the new file text when changed
    reason: str | None = None
    message: str | None = None
    existing: Any = None          # the entry that was there before, if any


_REFUSALS = {
    "unparseable": "the file does not parse, so ADAPT will not touch it",
    "comments": "the file has comments that rewriting it would lose",
    "shape": "the file is not shaped the way this client documents (the server list is not an object)",
    "different_entry": "an `adapt` entry that differs from this one already exists (add --replace to overwrite it)",
    "layout": "the existing `adapt` entry is written in a layout ADAPT cannot edit safely",
    "verify": "the edited file did not read back as expected, so it was not written",
    "modified": "the `adapt` entry was changed after ADAPT added it, so ADAPT will not remove it",
    "not_ours": "ADAPT did not add this entry (it has no record of it), so it will not remove it",
    "not_utf8": "the file is not UTF-8 text",
}


def _refuse(reason: str, existing: Any = None) -> Merge:
    return Merge("refused", reason=reason, message=_REFUSALS[reason], existing=existing)


def _merge_json(text: str | None, client: Client, entry: dict[str, Any], replace: bool) -> Merge:
    data, problem = parse_json_document(text or "")
    if problem or data is None:
        return _refuse(problem or "unparseable")
    node = _navigate(data, client.servers_path, create=True)
    if node is None:
        return _refuse("shape")
    existing = node.get(ENTRY_NAME)
    if existing == entry:
        return Merge("unchanged", existing=existing)
    if existing is not None and not replace:
        return _refuse("different_entry", existing)
    node[ENTRY_NAME] = entry
    return Merge("changed", _dump_json(data, text), existing=existing)


def _remove_json(text: str, client: Client, expected: dict[str, Any], delete_if_empty: bool) -> Merge:
    data, problem = parse_json_document(text)
    if problem or data is None:
        return _refuse(problem or "unparseable")
    node = _navigate(data, client.servers_path, create=False)
    existing = None if node is None else node.get(ENTRY_NAME)
    if existing is None:
        return Merge("unchanged")
    if existing != expected:
        return _refuse("modified", existing)
    del node[ENTRY_NAME]
    if delete_if_empty:
        for depth in range(len(client.servers_path), 0, -1):
            parent = _navigate(data, client.servers_path[: depth - 1], create=False)
            child = None if parent is None else parent.get(client.servers_path[depth - 1])
            if parent is not None and child == {}:
                del parent[client.servers_path[depth - 1]]
    return Merge("changed", _dump_json(data, text), existing=existing)


_TOML_HEADER = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(?:#.*)?$")


def _toml_spans(lines: list[str], client: Client) -> list[tuple[int, int]]:
    """Line spans [start, end) of the `[<servers>.adapt]` table and its sub-tables."""
    base = ".".join([*client.servers_path, ENTRY_NAME])
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for index, line in enumerate(lines):
        header = _TOML_HEADER.match(line)
        if not header:
            continue
        name = header.group(1).replace('"', "").replace("'", "").replace(" ", "")
        if start is not None:
            spans.append((start, index))
            start = None
        if name == base or name.startswith(base + "."):
            start = index
    if start is not None:
        spans.append((start, len(lines)))
    return spans


def _toml_get(data: dict[str, Any], client: Client) -> Any:
    node: Any = data
    for key in client.servers_path:
        node = node.get(key) if isinstance(node, dict) else None
    return node.get(ENTRY_NAME) if isinstance(node, dict) else None


def _toml_without_adapt(data: dict[str, Any], client: Client) -> dict[str, Any]:
    """The document minus `adapt`, with a server table that only held `adapt` dropped (so add/remove compare equal)."""
    clone = json.loads(json.dumps(data, default=str))
    chain: list[Any] = [clone]
    for key in client.servers_path:
        nxt = chain[-1].get(key) if isinstance(chain[-1], dict) else None
        chain.append(nxt)
    if isinstance(chain[-1], dict):
        chain[-1].pop(ENTRY_NAME, None)
    for depth in range(len(client.servers_path), 0, -1):
        if chain[depth] == {} and isinstance(chain[depth - 1], dict):
            chain[depth - 1].pop(client.servers_path[depth - 1], None)
    return clone


def _merge_toml(text: str | None, client: Client, entry: dict[str, Any], replace: bool, *, remove: bool = False) -> Merge:
    source = text or ""
    try:
        before = tomllib.loads(source)
    except tomllib.TOMLDecodeError:
        return _refuse("unparseable")
    existing = _toml_get(before, client)
    if remove:
        if existing is None:
            return Merge("unchanged")
        if existing != entry:
            return _refuse("modified", existing)
    elif existing == entry:
        return Merge("unchanged", existing=existing)
    elif existing is not None and not replace:
        return _refuse("different_entry", existing)
    newline = af.newline_of(source)
    lines = source.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    spans = _toml_spans(lines, client)
    if existing is not None:
        if not spans:
            return _refuse("layout", existing)
        if any(re.match(r"^\s*#", line) for start, end in spans for line in lines[start:end]):
            return _refuse("comments", existing)
    block = ac.toml_block(client, entry).rstrip("\n").split("\n")
    kept: list[str] = []
    cursor, inserted = 0, False
    for start, end in spans:
        kept += lines[cursor:start]
        if not remove and not inserted:
            kept += block + [""]
            inserted = True
        cursor = end
    kept += lines[cursor:]
    if not remove and not inserted:
        if kept and kept[-1].strip():
            kept.append("")
        kept += block
    while remove and kept and not kept[-1].strip() and spans:
        kept.pop()
    new_text = newline.join(kept) + (newline if kept else "")
    try:
        after = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError:
        return _refuse("verify")
    wanted = None if remove else entry
    if _toml_get(after, client) != wanted or _toml_without_adapt(after, client) != _toml_without_adapt(before, client):
        return _refuse("verify")
    return Merge("changed", new_text, existing=existing)


def read_registered(text: str, client: Client) -> tuple[Any, str | None]:
    """The `adapt` entry in a config text and a problem code. Read-only, so JSONC comments are tolerated."""
    if client.fmt == "toml":
        try:
            return _toml_get(tomllib.loads(text), client), None
        except tomllib.TOMLDecodeError:
            return None, "unparseable"
    if client.fmt == "yaml":
        return None, "unsupported"
    data, problem = parse_json_document(text)
    if problem == "comments":
        try:
            data, problem = json.loads(strip_jsonc(text)[0]), None
        except ValueError:
            return None, "unparseable"
        if not isinstance(data, dict):
            return None, "shape"
    if problem or data is None:
        return None, problem
    node = _navigate(data, client.servers_path, create=False)
    return (None if node is None else node.get(ENTRY_NAME)), None


def describe_entry(client: Client, entry: Any) -> dict[str, Any]:
    """Flags of a registered entry, without any serial: read-only or not, and how many devices."""
    if not isinstance(entry, dict):
        return {"registered": False}
    command = entry.get("command")
    args = entry.get("args") if isinstance(entry.get("args"), list) else []
    if isinstance(command, list):
        command, args = (command[0] if command else None), command[1:]
    flags = sorted({a for a in args if isinstance(a, str) and a.startswith("--")})
    kind = None
    if isinstance(command, str):
        kind = "absolute" if re.match(r"^(?:[A-Za-z]:[\\/]|/)", command) else "bare" if command.lower() in {"adapt", "adapt.exe"} else "other"
    return {"registered": True, "command": kind, "flags": flags, "read_only": "--read-only" in args,
            "devices": sum(1 for a in args if a == "--device"), "mcp_subcommand": bool(args) and args[0] == "mcp"}


# --- the adapt executable --------------------------------------------------------------------


def find_adapt(ctx: Context, adapt_path: str | None = None) -> str | None:
    """Absolute path of the `adapt` executable: the explicit one, PATH, or beside this Python."""
    if adapt_path:
        return os.path.abspath(adapt_path)
    found = ctx.which("adapt")
    if found:
        return os.path.abspath(found)
    names = ("adapt.exe", "adapt") if ctx.windows else ("adapt",)
    folder = Path(sys.executable).parent
    for base in (folder, folder / "Scripts", folder / "bin"):
        for name in names:
            if (base / name).is_file():
                return str(base / name)
    return None


def server_command(ctx: Context, scope: str, adapt_path: str | None = None) -> tuple[str | None, str | None]:
    """(command, problem): the absolute path at user scope, a bare `adapt` at project scope."""
    if scope == "project":
        return "adapt", None
    found = find_adapt(ctx, adapt_path)
    if found is None:
        return None, "Could not find the adapt executable to write. Pass --adapt-path, or put adapt on PATH."
    return found, None


# --- reports ---------------------------------------------------------------------------------


def clients_report(ctx: Context) -> dict[str, Any]:
    """Which clients are installed, and whether `adapt` is registered with each (read-only)."""
    tokens = ctx.tokens
    records = []
    for client in ac.CLIENTS:
        detected = ac.detect(client, tokens, ctx.which)
        scopes: dict[str, Any] = {}
        for scope in ac.SCOPES:
            path = ac.config_path(client, scope, tokens, ctx.platform)
            item: dict[str, Any] = {"path": None if path is None else str(path), "exists": bool(path and path.is_file()),
                                    "registered": None}
            if path is None:
                item["problem"] = "no config path for this scope" if scope not in client.paths else "a folder variable is unset"
            elif path.is_file():
                text = af.decode(path.read_bytes())[0] if path.stat().st_size < 5_000_000 else None
                entry, problem = read_registered(text, client) if text is not None else (None, "not_utf8")
                if problem:
                    item["problem"] = _REFUSALS.get(problem, problem)
                else:
                    item.update(describe_entry(client, entry))
            else:
                item["registered"] = False
            scopes[scope] = item
        argv = ac.cli_argv(client, "user", "adapt", ac.server_arguments())
        records.append({
            "key": client.key, "name": client.name, "status": "install" if client.install else "print-only",
            "detected": detected, "installed": bool(detected), "scopes": scopes,
            "own_command": ac.quote_command(argv) if argv else None, "note": client.note,
            "evidence": client.evidence,
        })
    return {"schema_version": "1", "platform": ac.platform_key(ctx.platform), "adapt_command": find_adapt(ctx),
            "project_dir": None if ctx.project_dir is None else str(ctx.project_dir),
            "clients": records, "claim_boundary": CLAIM_BOUNDARY}


def _resolve_inputs(client_key: str, scope: str, devices: tuple[str, ...], ctx: Context,
                    adapt_path: str | None) -> tuple[Client | None, dict[str, Any] | None, str | None]:
    """(client, refusal, command) after the checks every command shares."""
    client = ac.CLIENT_BY_KEY.get(client_key)
    if client is None:
        return None, _refused(client_key, scope, "unknown_client", "Unknown client. Known: " + ", ".join(ac.CLIENT_KEYS) + "."), None
    if scope not in ac.SCOPES:
        return client, _refused(client_key, scope, "bad_scope", "Scope must be `user` or `project`."), None
    if devices:
        from adapt_adb.mcp.access import AccessPolicy

        try:
            AccessPolicy(allowed_devices=tuple(devices))
        except ValueError:
            return client, _refused(client_key, scope, "bad_device", "A --device value is not a complete exact target."), None
    if scope == "project" and ctx.project_dir is None:
        return client, _refused(client_key, scope, "no_project", "Project scope needs a project directory."), None
    command, problem = server_command(ctx, scope, adapt_path)
    if command is None:
        return client, _refused(client_key, scope, "no_adapt", problem or ""), None
    return client, None, command


def _refused(key: str, scope: str, reason: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"client": key, "scope": scope, "status": "refused", "reason": reason, "message": message, **extra}


def config_report(client_key: str, scope: str, devices: tuple[str, ...], allow_actions: bool, ctx: Context,
                  adapt_path: str | None = None) -> dict[str, Any]:
    """The exact entry (and the client's own command) to register ADAPT. Prints only; changes nothing."""
    client, refusal, command = _resolve_inputs(client_key, scope, devices, ctx, adapt_path)
    if refusal is not None:
        # A user-scope snippet without a resolved executable still helps: fall back to a bare `adapt`.
        if refusal["reason"] != "no_adapt" or client is None:
            return refusal
        command = "adapt"
    if client is None or command is None:
        return refusal or _refused(client_key, scope, "unknown_client", "Unknown client.")
    args = ac.server_arguments(allow_actions=allow_actions, devices=devices)
    entry = ac.build_entry(client, command, args)
    path = ac.config_path(client, scope, ctx.tokens, ctx.platform)
    argv = ac.cli_argv(client, scope, command, args)
    notes = _entry_notes(client, scope, devices, allow_actions, refusal)
    return {
        "client": client.key, "name": client.name, "scope": scope, "status": "ok",
        "install": "supported" if client.install else "print-only", "format": client.fmt,
        "config_path": None if path is None else str(path), "entry": entry,
        "snippet": ac.render_snippet(client, entry), "own_command": ac.quote_command(argv) if argv else None,
        "server_command": ac.quote_command([command, *args]), "read_only": not allow_actions,
        "notes": notes, "claim_boundary": CLAIM_BOUNDARY,
    }


def _entry_notes(client: Client, scope: str, devices: tuple[str, ...], allow_actions: bool, refusal: Any) -> list[str]:
    notes = [client.note] if client.note else []
    if refusal is not None:
        notes.append("The adapt executable was not found, so a bare `adapt` is shown; use its absolute path if the client lacks PATH.")
    if not devices:
        notes.append("No --device was given, so no serial is written; add `--device SERIAL` to restrict the session.")
    if devices and scope == "project":
        notes.append("This project file will hold the device serial you gave; do not commit a private serial.")
    if allow_actions:
        notes.append("--allow-actions drops --read-only: the tools that act on the phone (tap, swipe, key, type, install_apk) are offered.")
    return notes


# --- install ---------------------------------------------------------------------------------


@dataclass
class Plan:
    client: Client
    scope: str
    status: str = "planned"
    method: str | None = None
    reason: str | None = None
    message: str | None = None
    path: Path | None = None
    entry: dict[str, Any] = field(default_factory=dict)
    server_command: str = ""
    cli_argv: list[str] | None = None
    diff: str = ""
    snippet: str = ""
    notes: list[str] = field(default_factory=list)
    old_text: str | None = None
    old_bom: bool = False
    new_text: str | None = None
    existing: Any = None
    cwd: str | None = None
    result: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        report: dict[str, Any] = {
            "client": self.client.key, "name": self.client.name, "scope": self.scope, "status": self.status,
            "method": self.method, "config_path": None if self.path is None else str(self.path),
            "server_command": self.server_command, "own_command": ac.quote_command(self.cli_argv) if self.cli_argv else None,
            "diff": self.diff, "snippet": self.snippet, "notes": self.notes,
        }
        if self.reason:
            report["reason"] = self.reason
            report["message"] = self.message
        report.update(self.result)
        return report


def _refuse_plan(plan: Plan, reason: str, message: str) -> Plan:
    plan.status, plan.reason, plan.message = "refused", reason, message
    return plan


def plan_install(client_key: str, *, scope: str, devices: tuple[str, ...], allow_actions: bool, replace: bool,
                 method: str, ctx: Context, adapt_path: str | None = None) -> Plan | dict[str, Any]:
    """Decide how to register ADAPT with one client. Reads files, writes nothing.

    Returns a `Plan`, or a refusal dict when the request itself is unusable (unknown client, bad scope).
    """
    client, refusal, command = _resolve_inputs(client_key, scope, devices, ctx, adapt_path)
    if client is None or command is None:
        return refusal or _refused(client_key, scope, "unknown_client", "Unknown client.")
    args = ac.server_arguments(allow_actions=allow_actions, devices=devices)
    plan = Plan(client=client, scope=scope, entry=ac.build_entry(client, command, args),
                server_command=ac.quote_command([command, *args]),
                cwd=str(ctx.project_dir) if ctx.project_dir else None)
    plan.snippet = ac.render_snippet(client, plan.entry)
    plan.notes = _entry_notes(client, scope, devices, allow_actions, None)
    plan.path = ac.config_path(client, scope, ctx.tokens, ctx.platform)
    plan.cli_argv = ac.cli_argv(client, scope, command, args)
    if not client.install:
        return _refuse_plan(plan, "print_only", f"{client.name} is print-only: ADAPT never writes its settings. Use the snippet above.")
    if scope not in client.paths:
        return _refuse_plan(plan, "no_scope", f"{client.name} has no {scope}-scope config that ADAPT knows of.")
    if not ac.detect(client, ctx.tokens, ctx.which):
        return _refuse_plan(plan, "not_detected", f"{client.name} was not found on this computer (no executable on PATH and no config folder), so nothing was registered.")
    cli_ready = plan.cli_argv is not None and bool(ctx.which(plan.cli_argv[0]))
    file_ok = client.fmt in ("json", "toml") and scope in client.file_scopes
    needs_file_for_timeout = client.timeout is not None and not client.cli_sets_timeout and file_ok
    if method == "auto":
        chosen = "cli" if cli_ready and not needs_file_for_timeout else "file" if file_ok else "cli"
    else:
        chosen = method
    if chosen == "cli":
        if plan.cli_argv is None:
            return _refuse_plan(plan, "no_method", f"{client.name} has no registration command for {scope} scope; use the snippet above.")
        if not cli_ready:
            return _refuse_plan(plan, "no_method", f"`{plan.cli_argv[0]}` is not on PATH, so ADAPT cannot register {client.name} for you. "
                                                   "Run the command above where it is installed.")
        plan.method = "cli"
        if client.timeout:
            plan.notes.append(f"{client.name}'s own command cannot set `{client.timeout[0]}`; add `{client.timeout[0]} = {client.timeout[1]}` "
                              "to the entry yourself if installs or test runs are cut off at 60 s.")
        return plan
    if not file_ok:
        return _refuse_plan(plan, "no_method", f"ADAPT does not edit {client.name}'s {scope} config file"
                                               + (" (its own command is the supported way)" if plan.cli_argv else "") + "; use the snippet above.")
    if plan.path is None:
        return _refuse_plan(plan, "no_path", "The config path needs a folder variable that is not set (HOME, USERPROFILE, APPDATA or XDG_CONFIG_HOME).")
    plan.method = "file"
    old_text = None
    if plan.path.exists():
        if not plan.path.is_file():
            return _refuse_plan(plan, "shape", "The config path is not a regular file.")
        try:
            raw = plan.path.read_bytes()
        except OSError:
            return _refuse_plan(plan, "unreadable", "The config file could not be read.")
        old_text, plan.old_bom = af.decode(raw)
        if old_text is None:
            return _refuse_plan(plan, "not_utf8", _REFUSALS["not_utf8"] + ".")
    plan.old_text = old_text
    merge = _merge_toml(old_text, client, plan.entry, replace) if client.fmt == "toml" else _merge_json(old_text, client, plan.entry, replace)
    plan.existing = merge.existing
    if merge.status == "refused":
        return _refuse_plan(plan, merge.reason or "verify", (merge.message or "") + ". Nothing was changed; use the snippet above.")
    if merge.status == "unchanged":
        plan.status = "unchanged"
        plan.message = "ADAPT is already registered with exactly this entry."
        return plan
    plan.new_text = merge.text
    label = ac.display_label(client, scope)
    plan.diff = af.unified_diff(old_text, merge.text, label)
    if client.fmt == "json" and old_text and merge.text is not None:
        original_data, _ = parse_json_document(old_text)
        if original_data is not None and _dump_json(original_data, old_text) != old_text.replace("\r\n", "\n").replace("\n", af.newline_of(old_text)):
            plan.notes.append("The file's formatting is normalised when it is rewritten; the diff shows every changed line.")
    return plan


def apply_plan(plan: Plan, ctx: Context) -> dict[str, Any]:
    """Carry out a confirmed plan: the client's own command, or backup + atomic write + manifest."""
    if plan.status != "planned":
        return plan.public()
    manifest_file = ctx.manifest()
    _, problem = af.read_manifest(manifest_file)
    if problem:
        return {**_fail(plan, "manifest", problem), "diff": plan.diff}
    stamp = ctx.stamp()
    if plan.method == "cli" and plan.cli_argv:
        code, output = ctx.runner(plan.cli_argv, plan.cwd if plan.scope == "project" else None, RUN_TIMEOUT_S)
        if code != 0:
            plan.result = {"output": output}
            return _fail(plan, "cli_failed", f"The client's own command failed (exit {code}); nothing was recorded.", output=output)
        record, error = af.append_manifest(manifest_file, {
            "time": stamp, "action": "install", "client": plan.client.key, "scope": plan.scope, "method": "cli",
            "file": None, "backup": None, "created_file": False, "entry": plan.entry,
            "command": ac.quote_command(plan.cli_argv)})
        plan.status = "installed"
        plan.result = {"output": output, "manifest_id": record["id"] if record else None, "backup": None,
                       "warning": error}
        return plan.public()
    if plan.path is None or plan.new_text is None:
        return _fail(plan, "internal", "Nothing to write.")
    backup, error = af.make_backup(plan.path, stamp)
    if error:
        return _fail(plan, "backup_failed", error)
    data = af.encode(plan.new_text, plan.old_bom)
    error = af.write_atomic(plan.path, data)
    if error:
        return _fail(plan, "write_failed", error)
    record, manifest_error = af.append_manifest(manifest_file, {
        "time": stamp, "action": "install", "client": plan.client.key, "scope": plan.scope, "method": "file",
        "file": str(plan.path), "backup": None if backup is None else str(backup),
        "created_file": plan.old_text is None, "entry": plan.entry, "previous_entry": plan.existing,
        "sha256_after": af.sha256_bytes(data)})
    plan.status = "installed"
    plan.result = {"backup": None if backup is None else str(backup), "manifest_id": record["id"] if record else None,
                   "warning": manifest_error and f"{manifest_error}; the backup is still at {backup}"}
    return plan.public()


def _fail(plan: Plan, reason: str, message: str, **extra: Any) -> dict[str, Any]:
    plan.status, plan.reason, plan.message = "failed", reason, message
    return {**plan.public(), **extra}


# --- manifest views, uninstall, restore ------------------------------------------------------


def manifest_entries(ctx: Context) -> tuple[list[dict[str, Any]], str | None]:
    manifest, problem = af.read_manifest(ctx.manifest())
    return [e for e in manifest["entries"] if isinstance(e, dict)], problem


def active_installs(entries: list[dict[str, Any]]) -> dict[tuple[str, str, str | None], dict[str, Any]]:
    """The newest install per (client, scope, file) that no later uninstall or restore has undone."""
    active: dict[tuple[str, str, str | None], dict[str, Any]] = {}
    for entry in entries:
        key = (str(entry.get("client")), str(entry.get("scope")), entry.get("file"))
        if entry.get("action") == "install":
            active[key] = entry
        elif entry.get("action") in ("uninstall", "restore"):
            active = {k: v for k, v in active.items() if not (k[2] and k[2] == entry.get("file")) and k != key}
    return active


def uninstall_client(client_key: str, *, scope: str, dry_run: bool, yes: bool, interactive: bool,
                     confirm: Callable[[str], bool] | None, ctx: Context) -> dict[str, Any]:
    """Remove only the `adapt` entry ADAPT added (and recorded). A CLI-added entry is removed with the client."""
    client = ac.CLIENT_BY_KEY.get(client_key)
    if client is None:
        return _refused(client_key, scope, "unknown_client", "Unknown client. Known: " + ", ".join(ac.CLIENT_KEYS) + ".")
    entries, problem = manifest_entries(ctx)
    if problem:
        return _refused(client_key, scope, "manifest", problem)
    active = active_installs(entries)
    record = next((r for (c, s, _f), r in active.items() if c == client.key and s == scope), None)
    if record is None:
        return _refused(client_key, scope, "not_ours", "ADAPT has no record of registering itself with this client at this scope, "
                                                      "so it will not remove anything.")
    if record.get("method") == "cli":
        return {"client": client.key, "scope": scope, "status": "manual", "reason": "cli_install",
                "message": f"ADAPT registered this through {client.name}'s own command, so remove the `{ENTRY_NAME}` server with the "
                           f"client itself (its removal command is not verified here). Recorded command: {record.get('command')}"}
    path = Path(str(record.get("file")))
    if not path.is_file():
        return _refused(client_key, scope, "missing", "The config file ADAPT changed no longer exists.")
    raw = path.read_bytes()
    text, bom = af.decode(raw)
    if text is None:
        return _refused(client_key, scope, "not_utf8", _REFUSALS["not_utf8"] + ".")
    expected = record.get("entry") or {}
    created = bool(record.get("created_file"))
    merge = _merge_toml(text, client, expected, False, remove=True) if client.fmt == "toml" else _remove_json(text, client, expected, created)
    if merge.status == "refused":
        return _refused(client_key, scope, merge.reason or "verify", (merge.message or "") + ". Nothing was changed.", config_path=str(path))
    if merge.status == "unchanged":
        return {"client": client.key, "scope": scope, "status": "unchanged", "message": "The `adapt` entry is already gone.",
                "config_path": str(path)}
    delete_file = created and _is_empty_document(merge.text or "", client)
    label = ac.display_label(client, scope)
    diff = af.unified_diff(text, None if delete_file else merge.text, label)
    report = {"client": client.key, "scope": scope, "status": "planned", "config_path": str(path), "diff": diff,
              "removes_file": delete_file, "claim_boundary": CLAIM_BOUNDARY}
    if dry_run:
        return report
    blocked = gate(yes, interactive, confirm, f"Remove the `{ENTRY_NAME}` entry from {path}?")
    if blocked:
        return {**report, **blocked}
    stamp = ctx.stamp()
    backup, error = af.make_backup(path, stamp)
    if error:
        return {**report, "status": "failed", "reason": "backup_failed", "message": error}
    if delete_file:
        try:
            path.unlink()
        except OSError as exc:
            return {**report, "status": "failed", "reason": "write_failed", "message": f"could not remove the file ({exc.__class__.__name__})"}
        after = None
    else:
        data = af.encode(merge.text or "", bom)
        error = af.write_atomic(path, data)
        if error:
            return {**report, "status": "failed", "reason": "write_failed", "message": error}
        after = af.sha256_bytes(data)
    saved, manifest_error = af.append_manifest(ctx.manifest(), {
        "time": stamp, "action": "uninstall", "client": client.key, "scope": scope, "method": "file", "file": str(path),
        "backup": None if backup is None else str(backup), "created_file": False, "entry": expected, "sha256_after": after})
    return {**report, "status": "removed", "backup": None if backup is None else str(backup),
            "manifest_id": saved["id"] if saved else None, "warning": manifest_error}


def _is_empty_document(text: str, client: Client) -> bool:
    if client.fmt == "toml":
        try:
            return not tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return False
    data, problem = parse_json_document(text)
    return problem is None and data == {}


def gate(yes: bool, interactive: bool, confirm: Callable[[str], bool] | None, question: str) -> dict[str, Any] | None:
    """None when the user has approved; else a refusal telling them how to approve."""
    if yes:
        return None
    if interactive and confirm is not None:
        if confirm(question):
            return None
        return {"status": "declined", "reason": "declined", "message": "Not confirmed; nothing was changed."}
    return {"status": "refused", "reason": "needs_yes", "message": "This run is not interactive: review the diff above and run again with --yes."}


def restore_list(ctx: Context) -> dict[str, Any]:
    entries, problem = manifest_entries(ctx)
    rows = [{"id": e.get("id"), "time": e.get("time"), "action": e.get("action"), "client": e.get("client"),
             "scope": e.get("scope"), "file": e.get("file"), "backup": e.get("backup"),
             "backup_exists": bool(e.get("backup") and Path(str(e["backup"])).is_file()),
             "created_file": bool(e.get("created_file"))} for e in entries if e.get("file")]
    return {"status": "ok" if not problem else "refused", "message": problem, "entries": rows,
            "manifest": None if ctx.manifest() is None else str(ctx.manifest())}


def restore_entry(entry_id: str, *, force: bool, dry_run: bool, yes: bool, interactive: bool,
                  confirm: Callable[[str], bool] | None, ctx: Context) -> dict[str, Any]:
    """Put a file back as it was before one recorded change (the current file is backed up first)."""
    entries, problem = manifest_entries(ctx)
    if problem:
        return {"status": "refused", "reason": "manifest", "message": problem}
    record = next((e for e in entries if e.get("id") == entry_id), None)
    if record is None or not record.get("file"):
        return {"status": "refused", "reason": "unknown_entry", "message": "No recorded change has that id. Run `adapt agent restore --list`."}
    path = Path(str(record["file"]))
    backup = Path(str(record["backup"])) if record.get("backup") else None
    created = bool(record.get("created_file")) and backup is None
    if backup is None and not created:
        return {"status": "refused", "reason": "no_backup", "message": "That change has no backup to restore."}
    if backup is not None and not backup.is_file():
        return {"status": "refused", "reason": "backup_missing", "message": "The backup file no longer exists."}
    current = path.read_bytes() if path.is_file() else None
    expected = record.get("sha256_after")
    if current is not None and expected and af.sha256_bytes(current) != expected and not force:
        return {"status": "refused", "reason": "changed_since",
                "message": "The file changed after ADAPT wrote it, so restoring would lose those changes. Review it, or add --force."}
    old_text = None if current is None else (af.decode(current)[0] or "")
    new_bytes = None if backup is None else backup.read_bytes()
    new_text = None if new_bytes is None else (af.decode(new_bytes)[0] or "")
    report = {"status": "planned", "id": entry_id, "config_path": str(path), "diff": af.unified_diff(old_text, new_text, path.name),
              "removes_file": new_bytes is None}
    if dry_run:
        return report
    blocked = gate(yes, interactive, confirm, f"Restore {path}?")
    if blocked:
        return {**report, **blocked}
    stamp = ctx.stamp()
    safety, error = af.make_backup(path, stamp)
    if error:
        return {**report, "status": "failed", "reason": "backup_failed", "message": error}
    if new_bytes is None:
        try:
            path.unlink()
        except OSError as exc:
            return {**report, "status": "failed", "reason": "write_failed", "message": f"could not remove the file ({exc.__class__.__name__})"}
    else:
        error = af.write_atomic(path, new_bytes)
        if error:
            return {**report, "status": "failed", "reason": "write_failed", "message": error}
    saved, manifest_error = af.append_manifest(ctx.manifest(), {
        "time": stamp, "action": "restore", "client": record.get("client"), "scope": record.get("scope"), "method": "file",
        "file": str(path), "backup": None if safety is None else str(safety), "created_file": False,
        "restored_from": entry_id, "sha256_after": None if new_bytes is None else af.sha256_bytes(new_bytes)})
    return {**report, "status": "restored", "backup": None if safety is None else str(safety),
            "manifest_id": saved["id"] if saved else None, "warning": manifest_error}


# --- verify ----------------------------------------------------------------------------------


def registered_command_is_adapt(command: str, args: list[str]) -> bool:
    """True only for an `adapt mcp ...` command line: `verify` never runs anything else a config names."""
    return Path(command).stem.lower() == "adapt" and bool(args) and args[0] == "mcp"


def verify_server(command: str, args: list[str], *, timeout: float = VERIFY_TIMEOUT_S) -> dict[str, Any]:
    """Start the server as a child, run the MCP handshake and `tools/list`, and report. Contacts no device."""
    import anyio

    result: dict[str, Any] = {"status": "failed", "command": ac.quote_command([command, *args])}

    async def run() -> None:
        from mcp import Client
        from mcp.client.stdio import StdioServerParameters

        with anyio.fail_after(timeout):
            async with Client(StdioServerParameters(command=command, args=list(args))) as client:
                tools = (await client.list_tools()).tools
                names = [t.name for t in tools]
                resources = len((await client.list_resources()).resources)
                result.update(
                    status="ok", server_name=client.server_info.name, server_version=client.server_info.version,
                    instructions_chars=len(client.instructions or ""), tool_count=len(names), tools=names,
                    resource_count=resources, read_only_session="--read-only" in args)

    try:
        anyio.run(run)
    except TimeoutError:
        result.update(reason="timeout", message=f"The server did not answer within {timeout:g} s.")
    except ImportError:
        result.update(reason="sdk_missing", message="The MCP SDK is not installed (install adapt-adb[mcp]).")
    except Exception as exc:  # a child that exits or speaks garbage must come back as a report
        result.update(reason="handshake_failed", message=f"The server did not complete the MCP handshake ({exc.__class__.__name__}).")
    result["claim_boundary"] = ("Started a local child process and listed its tools. No device was contacted and no AI client "
                                "was involved, so this does not show that a client accepts the registration.")
    return result


def verify_report(client_key: str | None, ctx: Context, adapt_path: str | None = None,
                  timeout: float = VERIFY_TIMEOUT_S) -> dict[str, Any]:
    """Verify this installation of `adapt mcp`, or every `adapt` entry registered with one client."""
    if client_key is None:
        command = find_adapt(ctx, adapt_path)
        if command is None:
            command, args = sys.executable, ["-m", "adapt_adb", "mcp", "--read-only"]
        else:
            args = ["mcp", "--read-only"]
        check = verify_server(command, args, timeout=timeout)
        return {"status": check["status"], "checks": [check]}
    client = ac.CLIENT_BY_KEY.get(client_key)
    if client is None:
        return {"status": "refused", "reason": "unknown_client", "message": "Unknown client. Known: " + ", ".join(ac.CLIENT_KEYS) + "."}
    tokens = ctx.tokens
    checks: list[dict[str, Any]] = []
    for scope in ac.SCOPES:
        path = ac.config_path(client, scope, tokens, ctx.platform)
        if path is None or not path.is_file():
            continue
        text, _ = af.decode(path.read_bytes())
        entry, problem = read_registered(text or "", client) if text is not None else (None, "not_utf8")
        if entry is None:
            continue
        command, args = _command_and_args(entry)
        if command is None or not registered_command_is_adapt(command, args):
            checks.append({"scope": scope, "status": "refused", "reason": "not_adapt",
                           "message": "The registered command is not `adapt mcp`, so verify will not run it."})
            continue
        checks.append({"scope": scope, **verify_server(command, args, timeout=timeout)})
    if not checks:
        return {"status": "not_registered", "message": f"No `{ENTRY_NAME}` entry was found in {client.name}'s config files "
                "(entries added by the client's own command may live where ADAPT does not read)."}
    return {"status": "ok" if all(c.get("status") == "ok" for c in checks) else "failed", "client": client.key, "checks": checks}


def _command_and_args(entry: Any) -> tuple[str | None, list[str]]:
    if not isinstance(entry, dict):
        return None, []
    command = entry.get("command")
    args = entry.get("args") if isinstance(entry.get("args"), list) else []
    if isinstance(command, list):
        command, args = (command[0] if command else None), command[1:]
    return (command if isinstance(command, str) else None), [str(a) for a in args]
