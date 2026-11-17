# ADAPT and AI clients through MCP

ADAPT exposes a local stdio MCP server. An AI application starts `adapt mcp`,
asks for its tools, then sends named calls with structured arguments. ADAPT
validates those arguments, applies session restrictions, performs a bounded
operation, and returns data or an error. It does not embed a language model or
require an OpenAI/Anthropic API key to run the local server itself.

MCP also defines resources (readable context) and prompts (reusable workflows).
*(Development tree.)* ADAPT implements tools and static resources; prompts are
not implemented. The server sends short orientation `instructions` when a
client connects, every tool advertises a title and the standard annotations
(read-only, destructive, idempotent, open-world), and the guidance is served as
`adapt://` resources and through the `help` tool for clients that do not show
resources. These are offline-tested; no AI client has yet been observed reading
them. An agent that has a shell does not need MCP to learn ADAPT: it can run
`adapt agent primer` (see [MCP access](mcp-access.md#instructions-titles-and-annotations)).
See the [MCP architecture](https://modelcontextprotocol.io/docs/learn/architecture).

## Current tools

The original twelve tools cover host diagnostics/setup status, device listing,
device information, screenshots, UI summaries, taps, swipes, keys, text input,
APK installation, and bounded log capture. Added tools are:

- `android_versions`: offline Android 10-17 planning matrix.
- `android_capabilities`: one explicitly selected device's SDK and eligibility.
- `developer_options_inspect`: thirteen allowlisted setting observations, without changes.
- `tool_catalog`: curated acquisition guidance and local PATH observations.
- `dex_inspect` and `dex_displays`: existing-display and DeX-state observations.
- `help`: ADAPT's built-in guidance (primer, session policy, safety rules,
  workflows, guides, the error reference, one failure guide), the same text as
  the `adapt://` resources. Offline; no device.

The current server exposes 38 tools: 24 observation tools and 14 tools that act
on the phone (input, installation, app launch and stop, test runs, Developer
Options profiles, permissions). The complete list and access configuration are in
[MCP access](mcp-access.md). Use protocol tool discovery for the authoritative installed list. Later
integrations can add tools without turning this document into an exhaustive
API schema. Profiles, host-tool installation, and mirror launching are not
implicitly granted by exposing read-only inspection tools.

## Start locally

Users install the published wheel with pipx, adding the `mcp` extra. This is
the form from the root [README](../README.md#download-and-install):

```text
pipx install "adapt-adb[mcp] @ https://github.com/primary-null/ADAPT/releases/download/v0.1.1/adapt_adb-0.1.1-py3-none-any.whl"
pipx ensurepath
adapt mcp --read-only --device SERIAL
```

Contributors install from a trusted checkout instead
([README](../README.md#install-from-a-checkout-contributors)):
`python -m pip install -e ".[mcp]"`.

The server waits for its client's protocol input; it is not an interactive
question-and-answer console. Do not expect a menu from that process. It emits
no banner or automatic help tips into protocol output.

Replace `SERIAL` with the exact transport name that `adapt devices` prints.
For Codex, the documented local registration form is:

```text
codex mcp add adapt -- adapt mcp --read-only --device SERIAL
```

Claude Code takes the same stdio form (the shape of its documented
`claude mcp add NAME -- COMMAND ARGS` command; not live-checked here):

```text
claude mcp add adapt -- adapt mcp --read-only --device SERIAL
```

Or configure the Codex equivalent in `~/.codex/config.toml`:

```toml
[mcp_servers.adapt]
command = "adapt"
args = ["mcp", "--read-only", "--device", "SERIAL"]
```

Use the full executable path if the AI application's PATH does not find ADAPT.
Codex supports stdio server commands and arguments; see its [official MCP guide](https://learn.chatgpt.com/docs/extend/mcp).
ADAPT changes a client's settings only when you run `adapt agent install`, shows
the change first, and keeps a backup.

Other local clients may use a configuration shaped like this, but check that
client's current schema and approval controls rather than assuming identical files:

```json
{
  "mcpServers": {
    "adapt": {
      "command": "adapt",
      "args": ["mcp", "--read-only", "--device", "SERIAL"]
    }
  }
}
```

ADAPT is not published to PyPI. Install the release wheel as shown above, and
do not install a similarly named registry package instead.

The server announces itself as `adapt` with the installed ADAPT package version.

## Register ADAPT with a client (`adapt agent`)

*(Development tree; offline-tested only.)* `adapt agent` automates the
registration shown above and installs the discovery files agents read:

```text
adapt agent clients                         # which clients are installed here; is adapt registered?
adapt agent config claude-code              # print the entry or the client's own command; writes nothing
adapt agent install claude-code --dry-run   # show the diff and the exact command line; writes nothing
adapt agent install claude-code --yes       # after the user approved that diff
adapt agent verify                          # start adapt mcp as a child process: handshake and tools/list
adapt agent skill install --scope project   # copy the Agent Skill to .agents/skills/adapt and .claude/skills/adapt
adapt agent agents-md --write --yes         # add a short ADAPT block to the project's AGENTS.md
adapt agent restore --list                  # every file ADAPT changed, with its backup
```

ADAPT changes a client's settings only when you run `adapt agent install`,
shows the change first, and keeps a backup. What that means in practice:

- It uses the client's own registration command when that command is on `PATH`
  and can express the whole entry. Otherwise it merges the config file: it shows
  a unified diff, copies the original to `<file>.adapt-backup-<UTC>`, records the
  backup in `agent-installs.json` beside ADAPT's preferences, and replaces the
  file atomically. `adapt agent uninstall CLIENT` removes only the entry ADAPT
  added; `adapt agent restore ID --yes` puts a recorded backup back.
- It refuses, and prints the entry for you to paste, when the file does not
  parse, when rewriting would lose comments (JSONC, YAML, TOML comments inside the
  entry), when a different `adapt` entry already exists (`--replace` overrides), or
  when the client is print-only. It never registers a client that is not installed.
- The server is registered read-only unless you pass `--allow-actions`. No device
  serial is written unless you pass `--device`. User scope writes the absolute
  path of `adapt`; project scope writes a bare `adapt` so a committed file holds
  no user name.
- Where a client has a tool-call timeout setting, it writes 180 seconds, so an APK
  install or a test run is not cut at the 60 s default of Codex, Roo Code and Pi.
- A run that is not interactive needs `--yes`, which is the user's approval.
- It never starts, restarts or stops a client and never touches a device. Restart
  or reload the client yourself afterwards.

The table and notes below are generated from the one client table in
`src/adapt_adb/agent_clients.py` (`adapt agent config` and `install` read the
same table), and a test fails when this file drifts from it. Every fact comes
from the client's own documentation as read on 2026-11-16; a client whose facts
are third-party or unstated is print-only. No registration below has been
live-checked against the real client.

<!-- adapt:clients-table:begin (generated by `adapt agent` from the client table; do not edit) -->

Shape of each client's entry, config paths and how `adapt agent install` treats it. `{home}`, `{project}`, `{appdata}`, `{xdg}` (XDG_CONFIG_HOME or `~/.config`) and `{copilot_home}` (COPILOT_HOME or `~/.copilot`) are resolved from environment variables at run time.

| Client (key) | Status | Format and server key | User config | Project config | Timeout written | Own command |
|---|---|---|---|---|---|---|
| Claude Code (`claude-code`) | install | json: `mcpServers`, `type: "stdio"` | `{home}/.claude.json` | `{project}/.mcp.json` | - | `claude mcp add --scope user adapt -- adapt mcp --read-only --device SERIAL`<br>`claude mcp add --scope project adapt -- adapt mcp --read-only --device SERIAL` |
| Claude Desktop (`claude-desktop`) | install | json: `mcpServers` | windows: `{appdata}/Claude/claude_desktop_config.json`; linux: `{xdg}/Claude/claude_desktop_config.json` | - | - | - |
| OpenAI Codex (CLI) (`codex`) | install | toml: `mcp_servers` | `{home}/.codex/config.toml` | `{project}/.codex/config.toml` | `tool_timeout_sec = 180` | `codex mcp add adapt -- adapt mcp --read-only --device SERIAL` |
| Cursor (`cursor`) | install | json: `mcpServers`, `type: "stdio"` (required) | `{home}/.cursor/mcp.json` | `{project}/.cursor/mcp.json` | - | - |
| VS Code (Copilot harness) (`vscode`) | install | json: `mcpServers`, `type: "stdio"` | `{copilot_home}/mcp-config.json` | `{project}/.mcp.json` | - | - |
| VS Code (Local harness, deprecated) (`vscode-local`) | install | json: `servers`, `type: "stdio"` | - | `{project}/.vscode/mcp.json` | - | - |
| Gemini CLI (`gemini-cli`) | install | json: `mcpServers` | `{home}/.gemini/settings.json` | `{project}/.gemini/settings.json` | - | - |
| Google Antigravity (`antigravity`) | install | json: `mcpServers` | `{home}/.gemini/config/mcp_config.json` | `{project}/.agents/mcp_config.json` | - | - |
| Roo Code (`roo-code`) | install | json: `mcpServers` | - | `{project}/.roo/mcp.json` | `timeout = 180` | - |
| OpenCode (`opencode`) | install | jsonc: `mcp.servers`, `command` is one array, `type: "local"` (required) | `{xdg}/opencode/opencode.jsonc` | `{project}/opencode.jsonc` | - | `opencode mcp add adapt --global -- adapt mcp --read-only --device SERIAL`<br>`opencode mcp add adapt -- adapt mcp --read-only --device SERIAL` |
| Pi coding agent (`pi`) | install | json: `mcpServers` | `{home}/.pi/agent/mcp.json` | `{project}/.pi/mcp.json` | `timeout = 180` | `pi mcp add adapt -- adapt mcp --read-only --device SERIAL`<br>`pi mcp add -l adapt -- adapt mcp --read-only --device SERIAL` |
| Cline (CLI) (`cline`) | install | json: `mcpServers` | `{home}/.cline/data/settings/cline_mcp_settings.json` | - | - | `cline mcp add adapt --yes -- adapt mcp --read-only --device SERIAL` |
| Amp (`amp`) | install | json: `amp.mcpServers` | `{home}/.config/amp/settings.json` | `{project}/.amp/settings.json` | - | `amp mcp add adapt -- adapt mcp --read-only --device SERIAL` |
| Kiro (`kiro`) | install | json: `mcpServers` | `{home}/.kiro/settings/mcp.json` | `{project}/.kiro/settings/mcp.json` | - | - |
| Windsurf / Devin Desktop (`windsurf`) | print-only | json: `mcpServers` | `{xdg}/devin/mcp_config.json` | `{project}/.devin/mcp_config.json` | - | - |
| Zed (`zed`) | print-only | json: `context_servers` | - | `{project}/.zed/settings.json` | - | - |
| Continue (`continue`) | print-only | yaml: `mcpServers`, `type: "stdio"` (required) | `{home}/.continue/config.yaml` | `{project}/.continue/mcpServers/adapt.yaml` | - | - |
| Goose (`goose`) | print-only | yaml: `extensions`, `type: "stdio"` (required) | windows: `{appdata}/Block/goose/config/config.yaml`; linux: `{xdg}/goose/config.yaml` | - | - | - |
| JetBrains AI Assistant (`jetbrains`) | print-only | json: `mcpServers` | - | - | - | - |
| JetBrains Junie (`junie`) | print-only | json: `mcpServers` | `{home}/.junie/mcp/mcp.json` | `{project}/.junie/mcp/mcp.json` | - | - |
| Grok (community CLI) (`grok`) | print-only | toml: `mcp_servers` | - | `{project}/.grok/config.toml` | - | - |

Notes per client (evidence: each client's own documentation read on 2026-11-16; third-party facts are marked):

- **Claude Code** (`claude-code`): User scope uses `claude mcp add` only: ~/.claude.json is Claude Code's own state file, which ADAPT never edits. No timeout key is written (its unit is not stated).
- **Claude Desktop** (`claude-desktop`): No command-line registration and no project scope. Restart Claude Desktop yourself afterwards; ADAPT never starts or stops a client. macOS is not covered by the sources read.
- **OpenAI Codex (CLI)** (`codex`): `codex mcp add` cannot set `tool_timeout_sec` (default 60 s), so `install` edits the TOML file unless you pass --method cli. Project config applies in trusted projects only.
- **Cursor** (`cursor`): `type: "stdio"` is required. No command-line registration is documented.
- **VS Code (Copilot harness)** (`vscode`): User file is ~/.copilot/mcp-config.json (or $COPILOT_HOME). `code --add-mcp` is not used: which file it writes is not stated. Project scope shares .mcp.json with Claude Code.
- **VS Code (Local harness, deprecated)** (`vscode-local`): Project scope only (the user-profile file's path is not stated). The Local harness is deprecated, with removal announced for February 2027.
- **Gemini CLI** (`gemini-cli`): `gemini mcp add` takes the command's arguments positionally, which makes `--read-only` ambiguous, so `install` edits the JSON file. No timeout key is written (its unit is not stated).
- **Google Antigravity** (`antigravity`): No command-line registration is documented. Its per-call timeout is 180 s already.
- **Roo Code** (`roo-code`): Project scope only (the global file's path is not stated). Detected only when the project already has a .roo folder, because the extension itself cannot be detected from outside.
- **OpenCode** (`opencode`): Registered through `opencode mcp add` only: its config is JSONC and ADAPT does not edit JSONC. `command` is one array. Evidence: official; user path third-party.
- **Pi coding agent** (`pi`): `pi mcp add` cannot set `timeout` (default 60 s), so `install` edits the JSON file unless you pass --method cli. Pi runs tools without asking by default: keep the session read-only.
- **Cline (CLI)** (`cline`): Registered through `cline mcp add` only. The Cline VS Code extension keeps its settings elsewhere (path not stated): use `adapt agent config cline` and paste it into the extension.
- **Amp** (`amp`): The key is the single name `amp.mcpServers`. Workspace servers need `amp mcp approve adapt`.
- **Kiro** (`kiro`): No command-line registration is documented.
- **Windsurf / Devin Desktop** (`windsurf`): Print-only: the documentation now redirects to Devin Desktop and the server key is not stated on the pages read. The legacy path ~/.codeium/windsurf/mcp_config.json is third-party. Evidence: unverified.
- **Zed** (`zed`): Print-only: settings.json is JSONC that holds the editor's own settings, and its user path is shown without an OS label. Paste the snippet into Zed's settings yourself. Evidence: official; project path third-party.
- **Continue** (`continue`): Print-only: YAML is not edited by ADAPT. A project JSON file under .continue/mcpServers/ is the documented alternative.
- **Goose** (`goose`): Print-only: YAML is not edited by ADAPT. Goose runs tools autonomously by default; set GOOSE_MODE=approve. `goose configure` is interactive.
- **JetBrains AI Assistant** (`jetbrains`): Print-only: configured in the IDE settings UI; paste the JSON there.
- **JetBrains Junie** (`junie`): Print-only: its documentation could not be read; the paths come from a third-party table. Evidence: third-party.
- **Grok (community CLI)** (`grok`): Print-only: whether xAI ships an official CLI is unverified; two third-party listings name `.grok/config.toml`. Evidence: third-party.

Print-only means ADAPT prints the entry and never writes that client's settings. No client registration in this table has been live-checked.

<!-- adapt:clients-table:end -->

## Access is layered

1. The AI host decides whether to connect, which tools the model sees, and
   when to ask the user for approval.
2. ADAPT enforces its schemas, session policy, target selection, bounded
   subprocesses, shared-daemon preflight, and guarded operations.
3. Android must independently enable debugging and authorize the host key or
   wireless pairing. MCP cannot manufacture that permission.

The local server runs with the OS permissions of the user who launches it.
Session flags restrict this ADAPT server; they are not an operating-system
sandbox and do not constrain a separate terminal tool available to the same AI.
Do not run ADAPT as administrator/root to solve ordinary ADB access problems.

```text
adapt mcp --read-only --device SERIAL
adapt mcp --device SERIAL_A --device SERIAL_B
```

`--read-only` hides and rejects tap/swipe/key/type/install calls. It means no
interactive or installation actions through the exposed tools, not zero I/O:
screenshots and UI hierarchy diagnostics still access the device and can use
owned temporary files. Screens, logs, and device identity may contain private
information; choose whether the connected AI client should receive them.

`--device` restricts device operations to exact supplied transport strings.
Every device call in that session must include an allowed `device`, even when
only one phone is connected. The one exception: in a session started with
exactly one `--device` target, the tools that act on the phone (`tap`, `swipe`,
`key`, `type`, `install_apk`) use that target when `device` is omitted, because
the operator chose it explicitly. USB and Wi-Fi identifiers are separate allowed
transports; permissions do not automatically follow a phone to a new endpoint.
Device-list/error results are filtered. Whole-host `doctor` is hidden/blocked
in a restricted session because it describes all transports. Host tool catalog
and setup-status information are still available and are not device-scoped.

Without these flags, the full local tool set remains available. Read-only tools
(`device_info`, `screenshot`, `ui_summary`, `logcat_tail`) use the only ready
device when a call names none, and fail with `need_device` when several are
ready. The tools that act on the phone never pick a device: a call without
`device` returns `need_device` with the candidate list and does nothing, even
with a single phone connected. Pass `device` with an exact transport from
`list_devices`; the advertised schema lists `device` as required for these tools.
Client approval prompts remain
client-specific, and enabling an action tool does not make every future call
user-approved. If you coordinate shared devices, use your own reservation
process alongside ADAPT's exact-target restrictions.

## Reading results and errors

A failed call returns `is_error` with an `error` code, a fixed `message`, a
`devices` list and an additive `failure` object (a stable `id`, causes, steps,
`next_tool` and a `retry` value) that an agent can act on. Codes include
`need_device`, `unknown_device`, `no_device`, `unsafe_shared_server` (the shared
ADB server on port 5037 is unverified or unsafe, and ADAPT did not contact it),
`adb_command_refused` (ADAPT's safety rules refused an adb command before it
ran; the message never repeats the command) and `internal_error` (unexpected
failure, with no exception text). The complete list and the `failure` shape are
in [MCP access](mcp-access.md#errors).

`doctor` reports the shared server as `daemon`: `ok`, `version_mismatch`,
`root_owned`, `foreign_owner`, `unverified` or `missing_adb`; for the unsafe
states `daemon_reason` carries ADAPT's short explanation (`null` otherwise).
`setup_status` reports `installed`, and `distribution_package: true` when the
adb is the Linux distribution's package: it counts as installed when it meets
the minimum version, but it is not verified Google Platform-Tools.

## Useful AI test loop

The observation tools `app_validate`, `app_logs`, `debug_app_inspect` and
`build_modes` add installed-app checks, bounded app-UID logs and build terminology.

`logging_guide` provides offline diagnostic guidance; `diagnostic_logs` reads
recent fixed-tag window/DeX/print system logs on one explicitly scoped target.
These can contain data from multiple apps/users; review locally before sharing.
Neither tool enables verbose logging. See [Rapid ADAPT](rapid-adapt.md).
Device calls require an explicit target and honor read-only/exact-device session
restrictions. Validate the expected version/debuggable flag before a test journey,
then compare fresh UI evidence and logs. These tools do not infer R8, a production
signer or exact APK identity from installed version/flags. They never select a
debug app, clear log buffers or export files through MCP. See
[development validation](development-validation.md) for CLI examples and limits.

Start with version/capability and Developer Options inspection. Capture a fresh
screenshot/UI summary, choose a visible element, perform one permitted action,
and observe again. Treat failed UI dumps as failures rather than reuse stale
coordinates. An AI must not equate an SDK version with proven OEM support.

The `fast-ui` CLI profile can make animation-independent tests more repeatable;
`touches` can make input visible in recordings. Each saves original values before
writes. Restore after the run, inspect conflicts, and retain recovery evidence.
These mutation workflows are currently explicit CLI operations, not silently
available through the inspection-only MCP tool. Disabled animations can hide
animation/lifecycle bugs, so keep a normal-settings test path too.

## Extensions and remote access

For a phone reachable through Tailscale, first pair/connect and verify it with
`adapt tailscale`, then start `adapt mcp --read-only --device TAIL_IP:CONNECT_PORT`.
The existing exact-target policy applies to that transport. An explicit remote
USB worker can use `adapt tailscale worker-mcp-config --peer LAB_HOST --device SERIAL
--json`; this generates an offline read-only SSH entry, without contacting the
worker or verifying its tailnet identity. See [Tailscale mode](tailscale.md).

An explicitly selected worker can now be reached using OpenSSH stdio:
`adapt ssh mcp-config android-worker --device SERIAL --json` generates a
read-only client entry. The client launches SSH directly, and the worker runs
ADAPT with its own local ADB daemon and existing exact-target enforcement.
No configuration is written and no connection starts while generating the entry.
See [SSH workers](ssh-workers.md) for POSIX/Windows shell selection and host/key
prerequisites. This is separate from direct Android SSH and from raw ADB forwarding.

Useful additions include display-aware screenshot/input tools and test prompts
that require fresh observations (prompts are not implemented; the workflow text
exists as the `adapt://workflows/...` resources).
Profile mutation would need explicit session capabilities and snapshot/recovery
semantics. Tool installation should retain a separate confirmation boundary.

This release does not implement an HTTP listener, OAuth, remote user accounts,
or a hosted tunnel service. A remote-only MCP client cannot directly launch
this local stdio process. Remote deployment needs its own authenticated and
scoped design; exposing raw ADB or adding an unauthenticated proxy is not an
ADAPT setup step. See the [MCP security guidance](https://modelcontextprotocol.io/specification/latest/basic/security_best_practices).

Checked 2026-11-14. Protocol tests use the installed official Python MCP SDK;
registration acceptance in each external AI application remains separate.
