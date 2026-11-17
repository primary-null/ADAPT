"""The AI-client table behind `adapt agent clients`, `config` and `install`.

One table (`CLIENTS`) holds everything that differs between clients: the config file format and
paths per OS and scope, the key under which servers live, whether `command` is a string or an
array, whether `type` is required, the timeout key, and the client's own registration command.
`docs/clients.md` is generated from the same table (`render_clients_doc`), and a test fails when
the two drift.

Every path is built from environment variables (HOME, USERPROFILE, APPDATA, LOCALAPPDATA,
XDG_CONFIG_HOME, COPILOT_HOME) and an explicit project directory, passed in as a mapping, so a
test can point all of it at a temporary folder. Nothing here writes a file, starts a process or
touches a device, and nothing raises: a problem comes back as data.

Evidence: each client's facts come from its own documentation as read on 2026-11-16. A client
whose facts rest on a third-party source, or whose file shape is not stated, is `print-only`:
ADAPT prints the entry and never writes that client's settings. No registration has been
live-checked against a real client.
"""

from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

ENTRY_NAME = "adapt"
SCOPES = ("user", "project")
# Seconds. Codex, Roo Code and Pi cut a tool call at 60 s by default; an APK install can take longer.
LONG_TOOL_TIMEOUT_S = 180
EVIDENCE_CHECKED = "2026-11-16"


@dataclass(frozen=True)
class Client:
    key: str
    name: str
    install: bool                    # False: print-only, ADAPT never writes this client's settings
    fmt: str                         # json | toml | yaml
    servers_path: tuple[str, ...]    # where the server map lives inside the file
    paths: Mapping[str, Mapping[str, str]]   # scope -> platform (windows | linux | any) -> template
    command_style: str = "string"    # "string" or "array" (command and args in one array)
    type_value: str | None = None    # value of a `type` key, when the client wants one
    type_required: bool = False
    timeout: tuple[str, int] | None = None   # (key, seconds) written so installs are not cut at 60 s
    cli: Mapping[str, tuple[str, ...]] = field(default_factory=dict)  # scope -> argv template
    cli_sets_timeout: bool = False   # can the client's own command carry the timeout key?
    file_scopes: tuple[str, ...] = ()  # scopes where ADAPT may merge the file itself
    detect_exes: tuple[str, ...] = ()
    detect_paths: tuple[str, ...] = ()
    evidence: str = "official"
    note: str = ""


def _both(user: str | None = None, project: str | None = None, **per_os: str) -> dict[str, dict[str, str]]:
    paths: dict[str, dict[str, str]] = {}
    if user:
        paths["user"] = {"any": user}
    if per_os:
        paths["user"] = dict(per_os)
    if project:
        paths["project"] = {"any": project}
    return paths


_SERVER = "{server}"  # expands to the command and its arguments in a registration command

CLIENTS: tuple[Client, ...] = (
    Client(
        "claude-code", "Claude Code", True, "json", ("mcpServers",),
        _both("{home}/.claude.json", "{project}/.mcp.json"),
        type_value="stdio",
        cli={"user": ("claude", "mcp", "add", "--scope", "user", ENTRY_NAME, "--", _SERVER),
             "project": ("claude", "mcp", "add", "--scope", "project", ENTRY_NAME, "--", _SERVER)},
        file_scopes=("project",),
        detect_exes=("claude",), detect_paths=("{home}/.claude",),
        note="User scope uses `claude mcp add` only: ~/.claude.json is Claude Code's own state file, "
             "which ADAPT never edits. No timeout key is written (its unit is not stated).",
    ),
    Client(
        "claude-desktop", "Claude Desktop", True, "json", ("mcpServers",),
        _both(windows="{appdata}/Claude/claude_desktop_config.json", linux="{xdg}/Claude/claude_desktop_config.json"),
        file_scopes=("user",),
        detect_paths=("{appdata}/Claude", "{xdg}/Claude"),
        note="No command-line registration and no project scope. Restart Claude Desktop yourself afterwards; "
             "ADAPT never starts or stops a client. macOS is not covered by the sources read.",
    ),
    Client(
        "codex", "OpenAI Codex (CLI)", True, "toml", ("mcp_servers",),
        _both("{home}/.codex/config.toml", "{project}/.codex/config.toml"),
        timeout=("tool_timeout_sec", LONG_TOOL_TIMEOUT_S),
        cli={"user": ("codex", "mcp", "add", ENTRY_NAME, "--", _SERVER)},
        file_scopes=("user", "project"),
        detect_exes=("codex",), detect_paths=("{home}/.codex",),
        note="`codex mcp add` cannot set `tool_timeout_sec` (default 60 s), so `install` edits the TOML file "
             "unless you pass --method cli. Project config applies in trusted projects only.",
    ),
    Client(
        "cursor", "Cursor", True, "json", ("mcpServers",),
        _both("{home}/.cursor/mcp.json", "{project}/.cursor/mcp.json"),
        type_value="stdio", type_required=True,
        file_scopes=("user", "project"),
        detect_exes=("cursor",), detect_paths=("{home}/.cursor",),
        note="`type: \"stdio\"` is required. No command-line registration is documented.",
    ),
    Client(
        "vscode", "VS Code (Copilot harness)", True, "json", ("mcpServers",),
        _both("{copilot_home}/mcp-config.json", "{project}/.mcp.json"),
        type_value="stdio",
        file_scopes=("user", "project"),
        detect_exes=("code",), detect_paths=("{copilot_home}",),
        note="User file is ~/.copilot/mcp-config.json (or $COPILOT_HOME). `code --add-mcp` is not used: which "
             "file it writes is not stated. Project scope shares .mcp.json with Claude Code.",
    ),
    Client(
        "vscode-local", "VS Code (Local harness, deprecated)", True, "json", ("servers",),
        _both(project="{project}/.vscode/mcp.json"),
        type_value="stdio",
        file_scopes=("project",),
        detect_exes=("code",), detect_paths=("{project}/.vscode",),
        note="Project scope only (the user-profile file's path is not stated). The Local harness is "
             "deprecated, with removal announced for February 2027.",
    ),
    Client(
        "gemini-cli", "Gemini CLI", True, "json", ("mcpServers",),
        _both("{home}/.gemini/settings.json", "{project}/.gemini/settings.json"),
        file_scopes=("user", "project"),
        detect_exes=("gemini",), detect_paths=("{home}/.gemini/settings.json",),
        note="`gemini mcp add` takes the command's arguments positionally, which makes `--read-only` ambiguous, "
             "so `install` edits the JSON file. No timeout key is written (its unit is not stated).",
    ),
    Client(
        "antigravity", "Google Antigravity", True, "json", ("mcpServers",),
        _both("{home}/.gemini/config/mcp_config.json", "{project}/.agents/mcp_config.json"),
        file_scopes=("user", "project"),
        detect_exes=("antigravity",), detect_paths=("{home}/.gemini/config",),
        note="No command-line registration is documented. Its per-call timeout is 180 s already.",
    ),
    Client(
        "roo-code", "Roo Code", True, "json", ("mcpServers",),
        _both(project="{project}/.roo/mcp.json"),
        timeout=("timeout", LONG_TOOL_TIMEOUT_S),
        file_scopes=("project",),
        detect_paths=("{project}/.roo",),
        note="Project scope only (the global file's path is not stated). Detected only when the project "
             "already has a .roo folder, because the extension itself cannot be detected from outside.",
    ),
    Client(
        "opencode", "OpenCode", True, "jsonc", ("mcp", "servers"),
        _both("{xdg}/opencode/opencode.jsonc", "{project}/opencode.jsonc"),
        command_style="array", type_value="local", type_required=True,
        cli={"user": ("opencode", "mcp", "add", ENTRY_NAME, "--global", "--", _SERVER),
             "project": ("opencode", "mcp", "add", ENTRY_NAME, "--", _SERVER)},
        detect_exes=("opencode",), detect_paths=("{xdg}/opencode",),
        evidence="official; user path third-party",
        note="Registered through `opencode mcp add` only: its config is JSONC and ADAPT does not edit JSONC. "
             "`command` is one array.",
    ),
    Client(
        "pi", "Pi coding agent", True, "json", ("mcpServers",),
        _both("{home}/.pi/agent/mcp.json", "{project}/.pi/mcp.json"),
        timeout=("timeout", LONG_TOOL_TIMEOUT_S),
        cli={"user": ("pi", "mcp", "add", ENTRY_NAME, "--", _SERVER),
             "project": ("pi", "mcp", "add", "-l", ENTRY_NAME, "--", _SERVER)},
        file_scopes=("user", "project"),
        detect_exes=("pi",), detect_paths=("{home}/.pi",),
        note="`pi mcp add` cannot set `timeout` (default 60 s), so `install` edits the JSON file unless you "
             "pass --method cli. Pi runs tools without asking by default: keep the session read-only.",
    ),
    Client(
        "cline", "Cline (CLI)", True, "json", ("mcpServers",),
        _both("{home}/.cline/data/settings/cline_mcp_settings.json"),
        cli={"user": ("cline", "mcp", "add", ENTRY_NAME, "--yes", "--", _SERVER)},
        detect_exes=("cline",), detect_paths=("{home}/.cline",),
        note="Registered through `cline mcp add` only. The Cline VS Code extension keeps its settings "
             "elsewhere (path not stated): use `adapt agent config cline` and paste it into the extension.",
    ),
    Client(
        "amp", "Amp", True, "json", ("amp.mcpServers",),
        _both("{home}/.config/amp/settings.json", "{project}/.amp/settings.json"),
        cli={"user": ("amp", "mcp", "add", ENTRY_NAME, "--", _SERVER)},
        file_scopes=("project",),
        detect_exes=("amp",), detect_paths=("{home}/.config/amp",),
        note="The key is the single name `amp.mcpServers`. Workspace servers need `amp mcp approve adapt`.",
    ),
    Client(
        "kiro", "Kiro", True, "json", ("mcpServers",),
        _both("{home}/.kiro/settings/mcp.json", "{project}/.kiro/settings/mcp.json"),
        file_scopes=("user", "project"),
        detect_exes=("kiro",), detect_paths=("{home}/.kiro",),
        note="No command-line registration is documented.",
    ),
    # Print-only clients: a fact ADAPT needs is third-party, unstated, or the file cannot be edited safely.
    Client(
        "windsurf", "Windsurf / Devin Desktop", False, "json", ("mcpServers",),
        _both("{xdg}/devin/mcp_config.json", "{project}/.devin/mcp_config.json"),
        evidence="unverified",
        note="Print-only: the documentation now redirects to Devin Desktop and the server key is not stated "
             "on the pages read. The legacy path ~/.codeium/windsurf/mcp_config.json is third-party.",
    ),
    Client(
        "zed", "Zed", False, "json", ("context_servers",),
        _both(project="{project}/.zed/settings.json"),
        evidence="official; project path third-party",
        note="Print-only: settings.json is JSONC that holds the editor's own settings, and its user path is "
             "shown without an OS label. Paste the snippet into Zed's settings yourself.",
    ),
    Client(
        "continue", "Continue", False, "yaml", ("mcpServers",),
        _both("{home}/.continue/config.yaml", "{project}/.continue/mcpServers/adapt.yaml"),
        type_value="stdio", type_required=True,
        note="Print-only: YAML is not edited by ADAPT. A project JSON file under .continue/mcpServers/ is the "
             "documented alternative.",
    ),
    Client(
        "goose", "Goose", False, "yaml", ("extensions",),
        _both(windows="{appdata}/Block/goose/config/config.yaml", linux="{xdg}/goose/config.yaml"),
        type_value="stdio", type_required=True,
        note="Print-only: YAML is not edited by ADAPT. Goose runs tools autonomously by default; set "
             "GOOSE_MODE=approve. `goose configure` is interactive.",
    ),
    Client(
        "jetbrains", "JetBrains AI Assistant", False, "json", ("mcpServers",), {},
        note="Print-only: configured in the IDE settings UI; paste the JSON there.",
    ),
    Client(
        "junie", "JetBrains Junie", False, "json", ("mcpServers",),
        _both("{home}/.junie/mcp/mcp.json", "{project}/.junie/mcp/mcp.json"),
        evidence="third-party",
        note="Print-only: its documentation could not be read; the paths come from a third-party table.",
    ),
    Client(
        "grok", "Grok (community CLI)", False, "toml", ("mcp_servers",),
        _both(project="{project}/.grok/config.toml"),
        evidence="third-party",
        note="Print-only: whether xAI ships an official CLI is unverified; two third-party listings name "
             "`.grok/config.toml`.",
    ),
)
CLIENT_BY_KEY = {client.key: client for client in CLIENTS}
CLIENT_KEYS = tuple(CLIENT_BY_KEY)


# --- environment and paths -------------------------------------------------------------------


def platform_key(platform: str | None = None) -> str:
    platform = platform or sys.platform
    return "windows" if platform == "win32" else "linux" if platform.startswith("linux") else "other"


def path_tokens(environ: Mapping[str, str], project_dir: str | Path | None, platform: str | None = None) -> dict[str, str | None]:
    """`{home}`, `{appdata}`, `{xdg}`, `{copilot_home}` and `{project}` as strings, or None when unknown.

    The real user profile is never guessed: with no HOME or USERPROFILE the tokens are None and the
    paths that need them are reported as unavailable.
    """
    windows = platform_key(platform) == "windows"
    order = ("USERPROFILE", "HOME") if windows else ("HOME", "USERPROFILE")
    home = next((environ[name] for name in order if environ.get(name)), None)
    xdg = environ.get("XDG_CONFIG_HOME") or (f"{home}/.config" if home else None)
    copilot = environ.get("COPILOT_HOME") or (f"{home}/.copilot" if home else None)
    return {
        "home": home,
        "appdata": environ.get("APPDATA") or None,
        "xdg": xdg,
        "copilot_home": copilot,
        "project": str(project_dir) if project_dir is not None else None,
    }


def _expand(template: str, tokens: Mapping[str, str | None]) -> Path | None:
    out = template
    for name, value in tokens.items():
        if "{" + name + "}" in out:
            if value is None:
                return None
            out = out.replace("{" + name + "}", value)
    return None if "{" in out else Path(out)


def config_path(client: Client, scope: str, tokens: Mapping[str, str | None], platform: str | None = None) -> Path | None:
    by_os = client.paths.get(scope, {})
    template = by_os.get(platform_key(platform)) or by_os.get("any")
    path = _expand(template, tokens) if template else None
    # A client that reads `name.jsonc` also reads `name.json` (OpenCode's own `mcp add` writes
    # `opencode.json` when neither exists): use the plain JSON file when only it is there.
    if path is not None and path.suffix == ".jsonc" and not path.exists():
        sibling = path.with_suffix(".json")
        if sibling.is_file():
            return sibling
    return path


# --- the entry ADAPT registers ---------------------------------------------------------------


def server_arguments(*, allow_actions: bool = False, devices: tuple[str, ...] = ()) -> list[str]:
    """Arguments after the `adapt` executable. Read-only unless actions were asked for; serials only if given."""
    args = ["mcp"]
    if not allow_actions:
        args.append("--read-only")
    for device in devices:
        args += ["--device", device]
    return args


def build_entry(client: Client, command: str, args: list[str]) -> dict[str, Any]:
    """The server entry in this client's shape."""
    entry: dict[str, Any] = {}
    if client.type_value:
        entry["type"] = client.type_value
    if client.command_style == "array":
        entry["command"] = [command, *args]
    else:
        entry["command"] = command
        entry["args"] = list(args)
    if client.timeout:
        entry[client.timeout[0]] = client.timeout[1]
    return entry


def nest(client: Client, entry: dict[str, Any]) -> dict[str, Any]:
    """The whole document fragment: the entry under its server map."""
    node: dict[str, Any] = {ENTRY_NAME: entry}
    for key in reversed(client.servers_path):
        node = {key: node}
    return node


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


def toml_block(client: Client, entry: dict[str, Any]) -> str:
    header = ".".join([*client.servers_path, ENTRY_NAME])
    return "\n".join([f"[{header}]", *(f"{key} = {_toml_value(value)}" for key, value in entry.items())]) + "\n"


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, (bool, int)):
        return json.dumps(value)
    return json.dumps(str(value), ensure_ascii=False)


def yaml_block(client: Client, entry: dict[str, Any]) -> str:
    args = "[" + ", ".join(_yaml_scalar(a) for a in entry.get("args", [])) + "]"
    if client.key == "goose":
        return (f"extensions:\n  {ENTRY_NAME}:\n    type: stdio\n    cmd: {_yaml_scalar(entry['command'])}\n"
                f"    args: {args}\n    enabled: true\n")
    return (f"mcpServers:\n  - name: {ENTRY_NAME}\n    type: stdio\n    command: {_yaml_scalar(entry['command'])}\n"
            f"    args: {args}\n")


def render_snippet(client: Client, entry: dict[str, Any]) -> str:
    if client.fmt == "toml":
        return toml_block(client, entry)
    if client.fmt == "yaml":
        return yaml_block(client, entry)
    return json.dumps(nest(client, entry), indent=2, ensure_ascii=False) + "\n"


def cli_argv(client: Client, scope: str, command: str, args: list[str]) -> list[str] | None:
    """The client's own registration command for a scope, or None when it has none for it."""
    template = client.cli.get(scope)
    if not template:
        return None
    argv: list[str] = []
    for token in template:
        argv += [command, *args] if token == _SERVER else [token]
    return argv


def quote_command(argv: list[str]) -> str:
    """One display line for a command. Arguments with spaces are double-quoted; nothing is executed from it."""
    return " ".join(f'"{a}"' if (" " in a or not a) else a for a in argv)


# --- detection -------------------------------------------------------------------------------


def detect(client: Client, tokens: Mapping[str, str | None], which: Any = shutil.which) -> list[str]:
    """What shows the client is installed here: an executable on PATH, or its own folder. Runs nothing."""
    found: list[str] = []
    for exe in client.detect_exes:
        if which(exe):
            found.append(f"{exe} on PATH")
    for template in client.detect_paths:
        path = _expand(template, tokens)
        if path is not None and path.exists():
            found.append(f"found {template}")
    return found


def display_label(client: Client, scope: str, platform: str | None = None) -> str:
    """A file label for diffs and reports with no user name in it: `<home>/.codex/config.toml`."""
    by_os = client.paths.get(scope, {})
    template = by_os.get(platform_key(platform)) or by_os.get("any") or client.key
    for token in ("home", "project", "appdata", "xdg", "copilot_home"):
        template = template.replace("{" + token + "}", "<" + token.replace("_", "-") + ">")
    return template


# --- generated documentation -----------------------------------------------------------------

CLIENTS_DOC_BEGIN = "<!-- adapt:clients-table:begin (generated by `adapt agent` from the client table; do not edit) -->"
CLIENTS_DOC_END = "<!-- adapt:clients-table:end -->"


def _template_text(client: Client, scope: str) -> str:
    by_os = client.paths.get(scope)
    if not by_os:
        return "-"
    parts = []
    for os_name, template in by_os.items():
        label = "" if os_name == "any" else f"{os_name}: "
        parts.append(f"{label}`{template}`")
    return "; ".join(parts)


def render_clients_doc() -> str:
    """The per-client section of docs/clients.md, generated from `CLIENTS`."""
    sample_command = "adapt"
    sample_args = ["mcp", "--read-only", "--device", "SERIAL"]
    out = [
        CLIENTS_DOC_BEGIN, "",
        "Shape of each client's entry, config paths and how `adapt agent install` treats it. `{home}`, "
        "`{project}`, `{appdata}`, `{xdg}` (XDG_CONFIG_HOME or `~/.config`) and `{copilot_home}` "
        "(COPILOT_HOME or `~/.copilot`) are resolved from environment variables at run time.", "",
        "| Client (key) | Status | Format and server key | User config | Project config | Timeout written | Own command |",
        "|---|---|---|---|---|---|---|",
    ]
    for client in CLIENTS:
        status = "install" if client.install else "print-only"
        shape = f"{client.fmt}: `{'.'.join(client.servers_path)}`"
        if client.command_style == "array":
            shape += ", `command` is one array"
        if client.type_value:
            shape += f", `type: \"{client.type_value}\"`" + (" (required)" if client.type_required else "")
        timeout = f"`{client.timeout[0]} = {client.timeout[1]}`" if client.timeout else "-"
        own = "<br>".join(f"`{quote_command(argv)}`" for argv in
                          (cli_argv(client, s, sample_command, sample_args) for s in SCOPES) if argv) or "-"
        out.append(f"| {client.name} (`{client.key}`) | {status} | {shape} | {_template_text(client, 'user')} | "
                   f"{_template_text(client, 'project')} | {timeout} | {own} |")
    out += ["", "Notes per client (evidence: each client's own documentation read on "
            f"{EVIDENCE_CHECKED}; third-party facts are marked):", ""]
    for client in CLIENTS:
        evidence = "" if client.evidence == "official" else f" Evidence: {client.evidence}."
        out.append(f"- **{client.name}** (`{client.key}`): {client.note}{evidence}".rstrip())
    out += ["", "Print-only means ADAPT prints the entry and never writes that client's settings. No client "
            "registration in this table has been live-checked.", "", CLIENTS_DOC_END]
    return "\n".join(out) + "\n"
