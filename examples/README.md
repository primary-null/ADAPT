# MCP client examples

These examples start the local `adapt mcp --read-only` server. Install ADAPT
from a trusted checkout first; no registry package is assumed.

- `mcp.json`: generic stdio configuration shape.
- `claude.mcp.json`: the same shape for clients using `mcpServers`.
- `codex.toml`: an `[mcp_servers.adapt]` configuration block.

Check your client's current configuration format and approval controls. No
external client registration has been live-accepted as part of this release.

Before connecting a phone, append `--device SERIAL` to the argument list and
replace `SERIAL` with that exact transport. Read-only observation may expose
screens, logs and identifiers to the AI client. Remove `--read-only` only when
you intend to expose the five interaction/installation tools.

If the client cannot find `adapt`, use the executable path in your installed
virtual environment, such as `<venv>/bin/adapt` or `<venv>\Scripts\adapt.exe`.
Do not commit your real path or device identifier back into these examples.

ADAPT implements local stdio only. It does not provide a remote HTTPS server
or authentication bridge for clients that require remote connectors.

See [MCP access](../docs/mcp-access.md) for tools, policies and setup, and
[AI clients](../docs/clients.md) for additional guidance.
