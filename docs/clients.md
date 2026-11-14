# ADAPT and AI clients through MCP

ADAPT exposes a local stdio MCP server. An AI application starts `adapt mcp`,
asks for its tools, then sends named calls with structured arguments. ADAPT
validates those arguments, applies session restrictions, performs a bounded
operation, and returns data or an error. It does not embed a language model or
require an OpenAI/Anthropic API key to run the local server itself.

MCP also defines resources (readable context) and prompts (reusable workflows).
ADAPT currently implements tools; resources and prompts are extension ideas,
not features already present. See the [MCP architecture](https://modelcontextprotocol.io/docs/learn/architecture).

## Current tools

The original twelve tools cover host diagnostics/setup status, device listing,
device information, screenshots, UI summaries, taps, swipes, keys, text input,
APK installation, and bounded log capture. Added tools are:

- `android_versions`: offline Android 10-17 planning matrix.
- `android_capabilities`: one explicitly selected device's SDK and eligibility.
- `developer_options_inspect`: thirteen allowlisted setting observations, without changes.
- `tool_catalog`: curated acquisition guidance and local PATH observations.
- `dex_inspect` and `dex_displays`: existing-display and DeX-state observations.

The current server exposes 24 tools: 19 observation tools and five interaction
or installation tools. The complete list and access configuration are in
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

Or configure the equivalent in `~/.codex/config.toml`:

```toml
[mcp_servers.adapt]
command = "adapt"
args = ["mcp", "--read-only", "--device", "SERIAL"]
```

Use the full executable path if the AI application's PATH does not find ADAPT.
Codex supports stdio server commands and arguments; see its [official MCP guide](https://learn.chatgpt.com/docs/extend/mcp).
This setup is user-run guidance: ADAPT does not rewrite an AI client's settings.

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

Useful additions include display-aware screenshot/input tools,
versioned workflow resources, and test prompts that require fresh observations.
Profile mutation would need explicit session capabilities and snapshot/recovery
semantics. Tool installation should retain a separate confirmation boundary.

This release does not implement an HTTP listener, OAuth, remote user accounts,
or a hosted tunnel service. A remote-only MCP client cannot directly launch
this local stdio process. Remote deployment needs its own authenticated and
scoped design; exposing raw ADB or adding an unauthenticated proxy is not an
ADAPT setup step. See the [MCP security guidance](https://modelcontextprotocol.io/specification/latest/basic/security_best_practices).

Checked 2026-11-14. Protocol tests use the installed official Python MCP SDK;
registration acceptance in each external AI application remains separate.
