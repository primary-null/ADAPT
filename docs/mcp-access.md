# MCP access

`adapt mcp` starts a **local stdio** [Model Context Protocol](https://modelcontextprotocol.io/docs/learn/architecture) server. An AI client launches it as a child process, lists its tools, and sends named calls with structured arguments. ADAPT validates each call, applies the session policy, runs one bounded operation as your operating-system user, and returns JSON (or, for `screenshot`, a JPEG plus JSON).

What it is not:

- **Not a network service.** There is no HTTP, SSE or WebSocket transport, no listening port, no authentication layer and no remote exposure. Remote use is only a client launching `ssh` that in turn launches `adapt mcp` on a worker ([below](#remote-workers-over-ssh)).
- **Not an LLM.** It embeds no model and needs no API key.
- **Tools only.** MCP resources and prompts are not implemented.
- **Not an Android permission grant.** It automates `adb` for the authorized transports your host already has.

> **Status.** The server, schemas and access policies are covered by offline tests (`tests/test_mcp.py`, `tests/test_mcp_contract.py`, `tests/test_mcp_access.py`). **No live acceptance run with any external AI client, and none of MCP over SSH, is claimed.** Treat the configuration snippets as the documented stdio shape for each client, not as verified registrations.

## Install and start

From a checkout (Python 3.11+; there is no assumed PyPI package):

```text
python -m pip install -e ".[mcp]"
adapt mcp --read-only
```

The `mcp` extra installs `mcp>=1.0.0` and `Pillow>=10.0.0` (Pillow scales screenshots). Without it, `adapt mcp` prints an install hint on stderr and exits 2. The process waits for protocol messages on stdin; it prints no menu, banner, tips or progress to stdout, and its own logging goes to stderr.

## Generic stdio configuration

Most clients accept a command and arguments. This is the shape, with the recommended restrictions:

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

- Replace `SERIAL` with the exact identifier from `adapt devices` (a USB serial, or a wireless `host:port`). Repeat `"--device", "..."` to allow several.
- If the client cannot find `adapt` on its `PATH`, set `command` to the absolute path of the `adapt` executable in your environment (for example `<venv>/bin/adapt`, or `<venv>\\Scripts\\adapt.exe` on Windows).
- Codex-style registration, matching its documented stdio form: `codex mcp add adapt -- adapt mcp --read-only`, or in its config file:

```toml
[mcp_servers.adapt]
command = "adapt"
args = ["mcp", "--read-only", "--device", "SERIAL"]
```

The shipped examples start in `--read-only` mode. Add `--device SERIAL` with your exact transport before connecting a client to a phone.

ADAPT never rewrites a client's settings. Clients that only speak remote HTTPS connectors cannot launch a stdio server; ADAPT provides no HTTP bridge.

## Layers of control

1. **The AI host** decides whether to start the server, which tools the model sees, and whether each call needs your approval.
2. **ADAPT's session policy** (`--read-only`, `--device`) is enforced inside the server regardless of the client's UI.
3. **Schema validation** rejects unknown arguments (`additionalProperties: false`), wrong types, enum values outside the list and numbers outside their `minimum`/`maximum` with `invalid_params`.
4. **The ADB guard** refuses `kill-server`, `sudo`, `uninstall`, `pm clear`, interactive `shell` and unbounded `logcat`, and a shared-server preflight refuses to talk to an unsafe port-5037 server (non-current-user or root owner, or a client/server version mismatch).
5. **Android** still requires an authorized transport, an unlocked screen where the OS demands it, and the user's own device permissions.

## Session policy

| Invocation | Tools exposed |
|---|---|
| `adapt mcp` | 24 |
| `adapt mcp --read-only` | 19 (the observation tools) |
| `adapt mcp --device A` | 23: `doctor` is removed because it describes the whole host, not one transport |
| `adapt mcp --read-only --device A` | **18** read-only tools |

With `--device`, the 17 device-bound tools advertise a **required** `device` parameter restricted by an `enum` of your allowed targets, and a call naming any other target returns `access_denied`. `list_devices` results are filtered to the allowed targets. Host-level tools without a device parameter (`setup_status`, `android_versions`, `tool_catalog`, `logging_guide`, `build_modes`) stay available because they observe no device.

Without `--device`, `device` is optional on tools that accept it: if exactly one device is in state `device` it is used; with several, the call fails with `need_device` and lists the candidates; an unlisted or non-ready target returns `unknown_device` or `no_device`.

`--device` values must be nonblank, free of control characters and surrounding whitespace, and must not begin with `-`; otherwise the command exits 2 without starting.

## Tool catalog

24 tools: **19 observation** (read-only mode keeps them) and **5 mutating** (hidden by `--read-only`). "Device" means bound by `--device` restrictions.

### Observation tools

| Tool | Device | Arguments | Returns / behavior |
|---|---|---|---|
| `doctor` | no | none | Adb path/version, whether it is ADAPT-managed, daemon state (`ok`, `version_mismatch`, `root_owned`, `missing_adb`), OS driver/udev hint, compact device list. Hidden when `--device` is used. |
| `setup_status` | no | none | Whether a usable Platform-Tools is installed, whether setup would download or edit PATH. Changes nothing. |
| `list_devices` | filtered | none | Transports from `adb devices -l`: device, state, model, product, transport. |
| `device_info` | yes | `device` (optional) | Model, SDK, release, screen size/density, lock state (`locked`, `unlocked`, `unknown`). |
| `screenshot` | yes | `device`, `max_dimension` (default 1024, max 1280), `quality` (default 80, 40-90) | `screencap -p` scaled to a JPEG image content item plus JSON metadata. May capture sensitive on-screen data. |
| `ui_summary` | yes | `device` | Compact interactive/labeled UI nodes (cap 80) with bounds and centers; no raw XML. If direct dump fails it writes then removes a temporary `/sdcard/adapt-ui-*.xml` on the device. |
| `logcat_tail` | yes | `device`, `lines` (default 100, 1-500), `tag`, `level` (`V D I W E F`) | `logcat -d -t N`, never streaming. |
| `android_versions` | no | none | Offline Android 10-17 / API 29-37 matrix (9 rows, including 12L). |
| `android_capabilities` | yes | `device` (required) | Version-dependent capability eligibility for one transport. |
| `developer_options_inspect` | yes | `device` (required) | The 13 allowlisted Developer Options settings; no changes. |
| `tool_catalog` | no | `goal` (optional enum) | Catalog records and PATH observations; never installs or executes. Goals: `testing`, `mirroring`, `aosp-build`, `lineage-build`, `image-inspection`, `remote-worker`. |
| `dex_inspect` | yes | `device` (required) | Samsung DeX state and logical displays; never starts DeX or mirrors. |
| `dex_displays` | yes | `device` (required) | Existing logical display IDs; creates nothing. |
| `app_validate` | yes | `device`, `package` (required); `expected_version_code` (0-2^63-1), `expected_debuggable` (bool) | Installed-for-current-user check plus requested version/debuggable checks. No R8/signing inference, no launch. |
| `app_logs` | yes | `device`, `package` (required); `lines` (1-500), `buffer` (`main`, `crash`, `main,crash`), `level` | Finite app-UID snapshot; no clearing, no export. |
| `debug_app_inspect` | yes | `device` (required) | Persisted debug-app and wait-for-debugger settings; attachment stays `not_verified`. |
| `diagnostic_logs` | yes | `device`, `topic` (`window`, `dex`, `print`) required; `minutes` (1-10), `lines` (1-500), `level` | Fixed-tag system log lines; may include other apps and users. |
| `logging_guide` | no | `topic` (`all`, `samsung`, `vendor`, `window`, `dex`, `print`, `wifi`, `app`) | Offline guidance; changes nothing. |
| `build_modes` | no | none | Offline debuggable / variant / R8 / profiling / signing guidance. |

### Mutating tools

| Tool | Arguments | Effect |
|---|---|---|
| `tap` | `x`, `y` (integers; required) | `input tap` on the **default display**. |
| `swipe` | `x1`, `y1`, `x2`, `y2` (required); `duration_ms` (default 300) | `input swipe` on the default display. |
| `key` | `key`: `BACK`, `HOME`, `ENTER`, `APP_SWITCH`, `POWER`, `DEL` | `input keyevent KEYCODE_<KEY>`. |
| `type` | `text` (single line, no control characters) | `input text` with space escaping. |
| `install_apk` | `apk` (required, a path on the host); `reinstall` (default true, `-r`); `grant` (default false, `-g`) | `adb install`, 120 s timeout. `grant` grants all runtime permissions. |

Input tools have **no display targeting**: they act on the phone's default display, never on a DeX or virtual display. Gaps like this are listed in [known limitations](known-limitations.md).

## Errors

A failed call returns `is_error` with `{"error": "...", "message": "...", "devices": []}`. Codes you may see: `unknown_tool`, `invalid_params`, `access_denied`, `no_adb`, `adb_probe_failed`, `unknown_device`, `no_device`, `need_device`, `adb_error`, `adb_timeout`, `adb_launch_failed`, `install_failed`, `logcat_failed`, `invalid_key`, `invalid_level`, `screenshot_failed`, `ui_summary_failed`, `development_inspection_failed`, `checks_failed`, `incomplete_evidence`, `internal_error`. Most messages are generic, but `adb_error`, `install_failed` and `logcat_failed` pass through adb's own error text.

## What read-only does and does not mean

`--read-only` removes the five input/install tools. It does **not** make observation private: `screenshot`, `ui_summary`, `logcat_tail`, `app_logs` and `diagnostic_logs` can expose personal data to the model and to the AI provider. The server runs with your user's permissions, so any file path you give `install_apk` is read as you. Use `--device` for every session that touches a phone, prefer a dedicated test device, and review client approval settings.

## Remote workers over SSH

```text
adapt ssh mcp-config android-worker --device SERIAL --json
```

prints an `mcpServers` entry named `adapt-worker` that makes the client run `ssh` with `BatchMode=yes`, `StrictHostKeyChecking=yes`, no forwarding and no remote command override, then `adapt --no-banner --no-tips --no-progress mcp --read-only --device SERIAL` on the worker. It is observation-only by construction, requires existing host-key trust and key/agent authentication, and writes nothing. The worker owns the USB device and its own ADB server; `adapt tailscale worker-mcp-config` produces the same entry for a tailnet host. See [SSH workers](ssh-workers.md) and the [architecture](architecture.md). **Live MCP-over-SSH use has not been exercised.**

## Hardening checklist

- Start with `--read-only --device SERIAL`; widen only when needed.
- Never share one ADB server with a root-owned or version-mismatched daemon; ADAPT will refuse, and so should you.
- Keep Wireless debugging and tailnet endpoints scoped to the device you intend to expose.
- Treat screenshots and logs as sensitive before sending them anywhere.
- Re-read the live tool list from your client; `adapt mcp` advertises only what the session policy allows.

Related: [command reference](command-reference.md#adapt-mcp), [features](features.md), [known limitations](known-limitations.md), [clients guide](clients.md).
