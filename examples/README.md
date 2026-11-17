# MCP client examples

These examples start the local `adapt mcp --read-only --device SERIAL` server.
`SERIAL` is a placeholder that you replace before use (see below).

- `mcp.json`: generic stdio configuration shape.
- `claude.mcp.json`: the same shape for clients using `mcpServers`.
- `codex.toml`: an `[mcp_servers.adapt]` configuration block.
- `cursor.mcp.json`: Cursor (`type: "stdio"` is required).
- `vscode.mcp.json`: VS Code with the Copilot harness (`mcpServers`, `type: "stdio"`).
- `opencode.jsonc`: OpenCode (`mcp.servers`; `command` is one array).
- `zed.settings.json`: Zed (`context_servers`).
- `gemini.settings.json`: Gemini CLI.
- `goose.config.yaml`: Goose (`extensions`, YAML).

These six are produced from the same client table as `adapt agent config`, and a
test fails when they drift (`adapt agent config CLIENT` prints the current
entry; `adapt agent install CLIENT --dry-run` shows the change to a real file).
They use a bare `adapt` and `--device SERIAL`; at user scope the installer
writes the absolute path of `adapt` instead.

Check your client's current configuration format and approval controls. No
external client registration has been live-accepted as part of this release.

## Install ADAPT first

Users install the published wheel with [pipx](https://pipx.pypa.io/stable/how-to/install-pipx.html),
adding the `mcp` extra because the MCP server needs the MCP SDK:

```text
pipx install "adapt-adb[mcp] @ https://github.com/primary-null/ADAPT/releases/download/v0.1.1/adapt_adb-0.1.1-py3-none-any.whl"
pipx ensurepath
adapt --help
```

This is the form from the root README ([Download and install](../README.md#download-and-install));
use the wheel URL of the release you intend to install. ADAPT is not published
to PyPI, so do not run `pipx install adapt-adb`. Contributors working on ADAPT
itself install from a checkout instead ([Install from a checkout](../README.md#install-from-a-checkout-contributors)).

If the client cannot find `adapt`, use the full path of the `adapt` executable
(for a checkout, `<venv>/bin/adapt` or `<venv>\Scripts\adapt.exe`).

## Replace `SERIAL` before connecting a phone

Each configuration passes `--device SERIAL`. Replace `SERIAL` with the exact
transport name that `adapt devices` prints (a USB serial, or a wireless
`host:port`); repeat `--device` to allow several. Do not commit your real path
or device identifier back into these examples. Read-only observation may expose
screens, logs and identifiers to the AI client. Remove `--read-only` only when
you intend to expose the 14 tools that act on the phone: `tap`, `swipe`,
`key`, `type`, `install_apk`, `app_launch`, `app_stop`, `app_open_link`,
`test_run`, `dev_options_apply`, `dev_options_restore`, `permission_grant`,
`permission_revoke` and `ui_tap_text`.

## If `--device` is omitted

The session is then not limited to particular transports:

- Read-only tools (`device_info`, `screenshot`, `ui_summary`, `logcat_tail`)
  use the only ready device when a call names none. With several ready devices
  they return `need_device` and list the candidates.
- Tools that act on the phone (`tap`, `swipe`, `key`, `type`, `install_apk`;
  available only without `--read-only`) refuse a call that names no `device`.
  They return `need_device` with the candidate list, even if only one phone is
  connected, and do nothing. The client must pass `device` with an exact
  transport name.

With `--device`, every device call must name an allowed transport. The one
exception: a session started with exactly one `--device` lets the tools that act
on the phone omit `device` and then uses that target.

ADAPT implements local stdio only. It does not provide a remote HTTPS server
or authentication bridge for clients that require remote connectors.

See [MCP access](../docs/mcp-access.md) for tools, policies and setup, and
[AI clients](../docs/clients.md) for additional guidance.
