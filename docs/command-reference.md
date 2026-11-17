# Command reference

Every visible `adapt` command with its exact flags, generated from the Typer command tree of this checkout and then annotated from the source modules. It covers **70 visible leaf commands in 14 groups** (the root plus 13 subcommand groups: 84 visible entries) and two hidden aliases. Run `adapt help-me --all` for the same inventory from an installed build.

This reference describes this repository's development tree, which contains unreleased changes. The 0.1.1 download has 64 visible leaf commands: it lacks `adapt explain`, `adapt agent primer` and the four `adapt dex receiver-*` commands, and it differs in the behaviour noted *(development tree)* below.

Conventions: `SERIAL` is an exact ADB transport identifier; placeholders in capitals are yours to supply; example addresses use the RFC 5737 documentation range. Examples are syntax-checked against the source command tree and are **not** a claim that the workflow succeeded on any device. `--json` output is one JSON document on stdout; ADAPT writes banners, tips and progress to stderr and silences them for `--json`, MCP and non-interactive output.

## Contents

[Global options](#global-options) | [Environment](#environment-variables) | [Hidden aliases](#hidden-aliases) | [Root commands](#root-commands) | [adapt dev-options](#adapt-dev-options) | [adapt tools](#adapt-tools) | [adapt android](#adapt-android) | [adapt dex](#adapt-dex) | [adapt detect](#adapt-detect) | [adapt desktop](#adapt-desktop) | [adapt ssh](#adapt-ssh) | [adapt app](#adapt-app) | [adapt logs](#adapt-logs) | [adapt debug](#adapt-debug) | [adapt rapid](#adapt-rapid) | [adapt tailscale](#adapt-tailscale) | [adapt agent](#adapt-agent)

## Global options

Placed before the command: `adapt --no-banner doctor`.

| Flag | Effect |
|---|---|
| `--no-banner` | Hide the ASCII wordmark (or set `ADAPT_NO_BANNER=1`; `ADAPT_BANNER=0` also disables it). The banner goes to stderr only for human terminals and is never shown for `--json` or `mcp`. |
| `--no-tips` | Hide contextual usage tips for this invocation (or `ADAPT_TIPS=0`, or `adapt tips off`). |
| `--no-progress` | Disable the `adapting...` activity indicator (or `ADAPT_NO_PROGRESS=1`). |
| `--version` | Print `adapt <version>` on stdout and exit 0, without the banner, tips, progress output or any adb access. |

## Environment variables

| Variable | Used by |
|---|---|
| `ADAPT_ADB` (also `ADB_PATH`) | Explicit adb executable; checked first by every command and by MCP tools. |
| `ANDROID_SERIAL` | Default target for `adapt adb` only. |
| `ANDROID_HOME`, `ANDROID_SDK_ROOT` | Searched for `platform-tools`; never written to. |
| `ADAPT_FASTBOOT` | Explicit fastboot executable for `detect`. |
| `ADAPT_SSH` | Explicit OpenSSH client for `ssh` and worker commands. |
| `ADAPT_TAILSCALE` | Explicit Tailscale client for `tailscale` commands. |
| `ADAPT_NO_BANNER`, `ADAPT_BANNER`, `ADAPT_NO_PROGRESS`, `ADAPT_TIPS` | Presentation switches described above. |
| `ADAPT_CONFIG_DIR` | Directory holding `preferences.json`. |
| `ADAPT_FAILURE_GUIDANCE` | `full`, `short` or `off`: how much failure guidance a human sees on stderr (see [help](help.md#failure-guidance)). |
| `ADAPT_DEBUG` | `1` shows the Python traceback for an unexpected internal error instead of the generic report. |

## Exit codes

There is no single table: each family reports its own codes, listed under the command. In general `0` is success, `1` a failure or refusal inside a typed workflow, `2` a refused request or partial/inconclusive evidence, and `3` an unsafe shared ADB server (`doctor`, `setup`, `everything`, `devices`, `adb`, `pair`, `connect`). Always read the report's `status` and `claim_boundary`/`note` fields rather than relying on an exit code alone.

*(development tree)* Failure guidance never changes an exit code or an existing key. Under `--json`, error payloads and reports that are refused, failed, partial or inconclusive gain an additive `failure` object whose `id` can be looked up with `adapt explain ID`; every `doctor` check carries `failure_id` (`null` when it passed or was skipped). The `dex receiver-*` commands follow the same rule: a refused, failed, `start_pending` or `stop_pending` report, and a `receiver-status` state of `unreadable`, `stale`, `unverified` or `not_working`, gain a `failure` object (`dexpc.native_receiver.*` for what ADAPT itself observed or refused, `dexpc.receiver.CODE` for a reason code the receiver recorded); a report that exits 0 but records a failure (a receiver that exited without recording a shutdown, or a phone attempt the running receiver recorded as failed) gains it too and adds only the `More:` line in human output. `receiver-status` still explains a recorded reason code in its own `last_failure` block. See [help](help.md#failure-guidance).

## Hidden aliases

Registered but omitted from `--help`; kept so existing scripts and the console keep working. They share their target's parameters exactly.

| Hidden command | Same as | Parameters |
|---|---|---|
| `adapt home` | `adapt console` | `--no-redraw` |
| `adapt give-me-everything` | `adapt everything` | `--yes`/`-y`, `--dry-run`, `--device`/`-s`, `--no-path`, `--json` |

## Root commands

Host setup, ADB transports, help, and the MCP server.

### `adapt console`

Open ADAPT's main screen, with Home/Back navigation and paged command screens.

**Effects.** Interactive terminal only; runs nothing until you choose a command.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `no_redraw` | `--no-redraw` | flag | no | off | Use an append-only console for screen readers or basic terminals. |

- Opens ADAPT's home screen: the fading ASCII wordmark, nine functional screens (Host/drivers/setup; Devices/wireless/Tailscale; Apps/developer testing; Displays/Samsung DeX; Tools/ROM workflows/Give Me Everything; AI/MCP/SSH workers; Rapid ADAPT; Logs/help/preferences; All commands), `H` Home, `B` Back, `N`/`P` page, `R` run a selected command.
- Running bare `adapt` in an interactive terminal opens this screen; in a non-interactive stream it prints help and exits 2.
- The console is a browser for existing commands: each selection executes the ordinary CLI command with its own confirmations.

```text
adapt console
adapt console --no-redraw
```

### `adapt everything`

Give Me Everything: detect all catalog tools and run supported confirmed setup/checks.

**Effects.** `--dry-run`: local PATH observation only. A real run may download Platform-Tools and scrcpy into user scope, update the user PATH (unless `--no-path`), and performs a read-only fastboot listing plus read-only inspections of explicit `--device` targets.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `yes` | `--yes` `-y` | flag | no | off | Approve the supported batch setup. Required with `--json` and in non-interactive shells. |
| `dry_run` | `--dry-run` | flag | no | off | Show scope and tool detection without downloads, writes, or device access. |
| `devices` | `--device` `-s` | text, repeatable | no |  | Also run read-only inspections on this exact device; repeat for several. No implicit target. |
| `no_path` | `--no-path` | flag | no | off | Do not update the user PATH during Platform-Tools setup. |
| `json_output` | `--json` | flag | no | off | Emit one JSON report. New batch execution requires --yes. |

- Batch of the **implemented** setup and check steps only: Platform-Tools (reuse or verified install), scrcpy (reuse or pinned install), host checks before and after, a fastboot transport listing, and per-device read-only inspection (`android`, `developer_options`, `dex`, `device_state`, `desktop`, `debug_selection`).
- Only 2 of the 16 catalog tools have an installer. The other 14 receive manual acquisition guidance and are reported as `located_unverified` or `manual_required`.
- Never installs system packages or drivers, syncs or builds ROM sources, unlocks, flashes, pairs, applies profiles, installs APKs, launches mirrors, or touches SSH/hypervisors.
- Not atomic: completed independent steps are kept if a later step fails.
- Exit codes: 0 completed, 1 failed, 2 completed with manual/skipped/partial steps (or refused), 3 blocked by an unsafe shared ADB server.

```text
adapt everything --dry-run
adapt everything --yes --no-path --json
adapt everything --yes --device SERIAL --json
```

### `adapt help-me`

Learn workflows or explore everything this installed ADAPT can do.

**Effects.** Offline. Reads the installed command tree.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `topic` | `TOPIC` (argument) | text | no |  | A command path or topic, for example `wireless` or `"dev-options apply"`. |
| `all_commands` | `--all` | flag | no | off | Show every registered command and option. |
| `json_output` | `--json` | flag | no | off | Emit the guide as JSON. |

- 66 authored help topics (commands plus `wireless`, `device_manager`, `udev`, `recheck`) each with a generic example and a boundary note. A topic view also lists its common failures.
- `--all` lists every registered command and option of the installed build. A failure ID (for example `adb.not_found`) shows the same guide as `adapt explain`. An unknown topic exits 2.

```text
adapt help-me
adapt help-me wireless
adapt help-me "dev-options apply"
adapt help-me --all --json
```

### `adapt explain`

Explain a failure: what happened, possible causes and what to try next. Runs nothing.

**Effects.** Offline. Reads the built-in failure guide registry only; no device, network or file access.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `query` | `QUERY...` (argument) | text, repeatable | no |  | A failure ID (`device.unauthorized`), a keyword (`5037`), or pasted error text. |
| `list_all` | `--list` | flag | no | off | List every known failure ID with a one-line summary. |
| `json_output` | `--json` | flag | no | off | Emit the explanation or list as JSON. |

- No query, or `--list`, lists every ID grouped by area. One match shows the full guide (all causes, all steps with platform labels, setup variations, help topic and docs path); several matches are listed.
- No match exits 2 (`{"error": "unknown_failure", "message": ...}` with `--json`). stderr stays empty for `--json`.
- Failing commands print `More: adapt explain ID` so the ID can be looked up later.

```text
adapt explain --list
adapt explain device.unauthorized
adapt explain "failed to authenticate"
adapt explain adb.server.unverified --json
```

### `adapt tips`

Enable or disable instructional tips without disabling errors or confirmations.

**Effects.** Writes a small preferences file only for `on`/`off`.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `mode` | `MODE` (argument) | text | no | `status` | `on`, `off` or `status` (default). `on`/`off` persist for the current OS user. |
| `json_output` | `--json` | flag | no | off | Emit preference status as JSON. |

- Tips are instructional text on stderr, shown only when stdin, stdout and stderr are all terminals; errors and confirmations are never suppressed. They are silent for `--json`, MCP and non-interactive output.
- Per-invocation overrides: `--no-tips` or `ADAPT_TIPS=0`. File: `%LOCALAPPDATA%\adapt\preferences.json` on Windows, `$XDG_CONFIG_HOME/adapt/preferences.json` (default `~/.config`) elsewhere; `ADAPT_CONFIG_DIR` relocates the directory.

```text
adapt tips
adapt tips off
adapt tips status --json
```

### `adapt doctor`

Locate adb, inspect port 5037 safety, OS prerequisites, and device states.

**Effects.** Read-only host inspection (adb discovery, port 5037 server probe, drivers/udev, `adb devices`). Interactive menu actions you select can change the host.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `json_output` | `--json` | flag | no | off | Emit machine-checkable JSON diagnostic report on stdout. |
| `fix` | `--fix` | flag | no | off | Print safe remediation commands only (never kills server, runs sudo, or wipes adbkey). |

- Checks: `adb_path`, `adb_version` (>= 34.0.0), `adb_origin`, `path_config`, `adb_server_5037`, Windows PnP driver state or Linux udev/plugdev, `android_home`, `devices_state`.
- *(development tree)* `host.os` is `windows`, `debian` (Debian family, from `/etc/os-release`), `linux` (other Linux, with distribution-neutral advice) or `macos` (reported as unsupported). The toolchain checks report the `adb` that ADAPT's commands use and say when it is not on `PATH`. A distribution-packaged `adb` at or above 34.0.0 is a `warn`. On Linux, every listener on port 5037 in `/proc/net/tcp` and `/proc/net/tcp6` is checked. On Windows, a driver problem only on USB devices not confirmed as Android is a `warn`. Running as root or in an elevated Administrator session prints a warning in human output. The udev advice names the installed path of the `51-android.rules` file that ships in the wheel. Details: [doctor](doctor.md).
- Exit codes by precedence: **3** unsafe shared ADB server (not current-user-owned, root/system-owned, or version differs from the client), **1** missing/outdated toolchain, **2** device unauthorized/offline or missing USB permissions, **0** ok.
- `--fix` prints remediation commands only; it never kills the server, runs sudo or deletes the adb key.
- In an interactive terminal without `--json`, a `Next action` menu follows. Choosing `setup`, `udev` (which runs the sudo commands for the udev rules) or `wireless` performs that action; quit with `q`.

```text
adapt doctor
adapt doctor --fix
adapt doctor --json
```

### `adapt devices`

List connected devices via 'adb devices -l'.

**Effects.** Read-only `adb devices -l` after the shared-server preflight.

No parameters.

- One phone can appear twice (USB and Wi-Fi). Choose an exact identifier for device commands.
- Exit 1 if adb is missing, 3 if the preflight refuses.

```text
adapt devices
```

### `adapt setup`

Download and configure official Google platform-tools in user scope.

**Effects.** Downloads Google's official Platform-Tools archive from `dl.google.com` into user scope and may update the user PATH. Never touches `ANDROID_HOME`, system paths or other ADB installations.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `yes` | `--yes` `-y` | flag | no | off | Run without the confirmation prompt (the prompt only appears in an interactive terminal). |
| `dry_run` | `--dry-run` | flag | no | off | Report planned download and PATH actions without mutating host state. |
| `no_path` | `--no-path` | flag | no | off | Unpack platform-tools without modifying user PATH. |
| `json_output` | `--json` | flag | no | off | Emit machine-checkable JSON setup report on stdout. |
| `force` | `--force` `-f` | flag | no | off | Force download and unpack even if adb is already usable. |
| `isolated` | `--isolated` | flag | no | off | Unpack into isolated ~/.android/platform-tools directory. |
| `sha256` | `--sha256` | text | no |  | Expected SHA-256 of the archive. A mismatch always fails. Without it ADAPT looks up Google's repository metadata digest (SHA-256, or SHA-1 fallback) and refuses to continue if none is found. |

- Targets: Windows `%LOCALAPPDATA%\Android\platform-tools`; Linux `~/.local/share/android/platform-tools`; `--isolated` uses `~/.android/platform-tools` on both.
- Order: *(development tree)* refuse on an unsupported host (macOS, `unsupported_platform`) -> port 5037 preflight (aborts with exit 3 on an unsafe server) -> noop if a healthy adb (>= 34.0.0) is already first on `PATH` and `--force` is absent -> *(development tree)* if setup's own install folder already holds a healthy adb that is not on `PATH`, add that folder to `PATH` after confirmation without downloading (`path_registered`; `noop_already_installed` with `--no-path`) -> Linux architecture check (`unsupported_architecture` unless x86-64) -> `--dry-run` report -> confirm (a prompt on interactive terminals; non-interactive runs stop here unless `--yes` is given) -> download, digest check, safe zip extraction (path traversal and size limits), staged adb version check, atomic swap -> PATH update.
- The archive digest is mandatory: explicit `--sha256`, else Google's repository metadata. A mismatch always fails.
- PATH update: Windows user `Path` (HKCU); Linux a managed block appended to `~/.profile`. Skipped by `--no-path`.
- Exit codes follow `doctor` (0/1/2/3); an exception exits 1. *(development tree)* When setup did what was asked (installed, registered on `PATH`, or nothing needed) and the toolchain checks pass, it exits 0 even if USB-permission or device findings remain; those stay in the printed report and in the JSON `status`, `doctor_status` and `doctor_exit_code` fields. An unsafe shared server found after setup still exits 3. `--dry-run` keeps the doctor pre-check's code. When setup did not do what was asked (aborted, failed, or refused for the platform or architecture), a healthy pre-check's exit 0 becomes 1; in 0.1.1 an aborted setup keeps the pre-check's code. It installs no USB drivers and no system packages.

```text
adapt setup --dry-run
adapt setup --yes --isolated --no-path
adapt setup --sha256 HEX_DIGEST --json
```

### `adapt mcp`

Start the ADAPT stdio MCP server.

**Effects.** Starts a long-running local stdio server; no network listener. Tools run ADB as the current OS user.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `read_only` | `--read-only` | flag | no | off | Expose inspection tools and deny interaction/install calls. |
| `device` | `--device` | text, repeatable | no |  | Restrict the session to this exact transport; repeat to allow several. Blank, whitespace-padded, control-character or `-`-prefixed values exit 2. |

- Requires the `mcp` extra (`adapt-adb[mcp]`; from a checkout, `pip install -e ".[mcp]"`); without it the command prints an install hint and exits 2.
- `--read-only` exposes the 19 observation tools; `--device` pins exact targets and requires `device` on the 17 device-bound tools, except that a session started with exactly one `--device` lets the five tools that act on the phone omit it. *(development tree)* Without `--device`, the tools that act on the phone (`tap`, `swipe`, `key`, `type`, `install_apk`) still need `device` and never pick one; in 0.1.1 they use the only ready device. Error results carry a `failure` object. See [MCP access](mcp-access.md).
- Never prints a banner, tips or progress to stdout; logging goes to stderr. Do not run it expecting an interactive prompt.
- *(development tree)* The server sends short orientation `instructions` at initialization (built by `adapt_adb/agent_guide.py`, the module behind `adapt agent primer`) and advertises a `title` and the standard annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`) on every tool, derived from the observation/acting classification in `mcp/access.py`. Offline-tested only; no AI client has been observed reading them. MCP resources and prompts are not implemented.

```text
adapt mcp --read-only
adapt mcp --read-only --device SERIAL
codex mcp add adapt -- adapt mcp --read-only
```

### `adapt adb`

Pass remaining arguments through to the resolved adb binary.

**Effects.** Runs one guarded adb command as the current user.

Takes the remaining arguments as the command to pass through (unknown options are accepted and forwarded).

No parameters.

- Everything after `adb` is passed through after safety validation. ADB global options accepted before the command: `-s`/`--serial`, `-d`, `-e`, `-t`, `-H`, `-P`, `-L`, `-a`, `--exit-on-write-error`. A `--` separator is optional.
- Refused: `kill-server`, anything starting with `sudo`, `uninstall`, `shell pm clear|uninstall`, `shell cmd package clear|uninstall`, bare `shell` (interactive), `logcat` without `-d` or `-t`. There is no confirmation flag: use direct adb for deliberate destructive operations. Device commands sent with `shell`, `exec-out` or `exec-in` must be one plain command: shell operators, nested interpreters other than `sh -c`, and opaque command wrappers such as `env`, `xargs` or `find -exec` are refused, while `run-as`, `timeout N` and `su` are allowed and the command inside them is checked the same way.
- Shell payloads containing unquoted `; | & < > $` backtick or newlines, shell indirection (`sh`, `bash`, `eval`, `source`), or more than two nested `sh -c` are rejected because their effect cannot be inspected. Shell commands time out after 60 seconds.
- Target selection for device commands: explicit `-s`/`--serial` (or `ANDROID_SERIAL`) must name a device in state `device`; with no selector exactly one ready device is required and is pinned with `-s`. Host-only commands (`version`, `help`, `start-server`, `devices`, `connect`, `disconnect`, `pair`, `mdns`) skip device selection.
- Exit 1 on refusal/target errors, 3 if the shared-server preflight refuses (including a non-default server endpoint), otherwise adb's exit code.

```text
adapt adb version
adapt adb -s SERIAL shell getprop ro.build.version.release
adapt adb -s SERIAL logcat -d -t 200
```

### `adapt pair`

Pair with an Android device over Wi-Fi (official adb pair).

**Effects.** Authorizes this PC with a phone's Wireless debugging pairing endpoint (`adb pair`); not a connection.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `endpoint` | `ENDPOINT` (argument) | text | yes |  | ip:pairing-port from Developer options -> Wireless debugging -> Pair with pairing code. |
| `code` | `CODE` (argument) | text | no |  | Pairing code. If omitted in a terminal ADAPT prompts without echo. A code given here is visible in process listings. |

- Android 11+ only. The pairing endpoint (`IP:PAIR_PORT`) and the connection endpoint are different ports and both change.
- ADAPT passes the code to adb on stdin, 30 second timeout, and treats the result as success only when adb prints `Successfully paired to`.

```text
adapt pair 192.0.2.10:PAIR_PORT
```

### `adapt connect`

Connect to an Android device over Wi-Fi (official adb connect).

**Effects.** Runs `adb connect` to one endpoint after the shared-server preflight.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `endpoint` | `ENDPOINT` (argument) | text | yes |  | ip:connect-port from Wireless debugging screen (default port 5555 if :port omitted). |

- Use the connection endpoint from the main Wireless debugging screen. Port 5555 is assumed if `:port` is omitted (legacy listeners). Success requires `connected to` / `already connected to` in adb's output.

```text
adapt connect 192.0.2.10:CONNECT_PORT
```

### `adapt guide`

Print official Google/AOSP tool recommendations and best practices.

**Effects.** Offline text.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `topic` | `TOPIC` (argument) | text | no |  | `testing`, `rom`, `ai` or `wireless`; omit or `all` to list. |

- Short official-tooling notes for four topics.

```text
adapt guide
adapt guide wireless
```

## `adapt dev-options`

Inspect and reversibly apply bounded Developer Options profiles. 3 commands.

### `adapt dev-options inspect`

Read allowlisted Developer Options without changing the device.

**Effects.** Read-only: 13 allowlisted settings through `settings get`.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `serial` | `--serial` `-s` | text | yes |  | Explicit ADB target serial. |
| `json_output` | `--json` | flag | no | off | Emit JSON only. |

- Settings (table/key): developer_mode `global/development_settings_enabled`, usb_debugging `global/adb_enabled`, wireless_debugging `global/adb_wifi_enabled`, stay_awake `global/stay_on_while_plugged_in`, window/transition/animator duration scales `global/*_scale`, show_touches `system/show_touches`, pointer_location `system/pointer_location`, dont_keep_activities `global/always_finish_activities`, show_background_anrs `secure/anr_show_background`, freeform_windows `global/enable_freeform_support`, force_desktop `global/force_desktop_mode_on_external_displays`.
- Missing and denied reads are reported explicitly; a stored value is not proof an OEM honors it.

```text
adapt dev-options inspect -s SERIAL --json
```

### `adapt dev-options apply`

Apply a bounded profile after persisting its original values.

**Effects.** Writes only the allowlisted keys of one profile, after exclusively creating a recovery snapshot.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `profile` | `--profile` | text | yes |  | `fast-ui`, `touches`, `stay-awake` or `desktop-testing`. |
| `serial` | `--serial` `-s` | text | yes |  | Explicit ADB target serial. |
| `snapshot` | `--snapshot` | path | yes |  | New recovery snapshot path. Created exclusively (mode 0600); an existing file is refused. |

- Profiles: `fast-ui` (three animation scales to 0), `touches` (`show_touches=1`), `stay-awake` (ORs the USB bit `2` into `stay_on_while_plugged_in`, preserving AC/wireless bits; refuses values outside 0-15), `desktop-testing` (`enable_freeform_support=1`, `force_desktop_mode_on_external_displays=1`; requires observed API 29-37; may need a manual reboot).
- The snapshot binds device serial, build fingerprint and Android user, and is written before the first write; every write is read back and a failure rolls back what was applied. Stay-awake requires USB power and does not unlock the phone.
- There is no `--json` flag. Exit 1 on any failure.

```text
adapt dev-options apply --profile stay-awake -s SERIAL --snapshot stay-awake.json
adapt dev-options apply --profile fast-ui -s SERIAL --snapshot fast-ui.json
```

### `adapt dev-options restore`

Restore a matching snapshot without overwriting external changes.

**Effects.** Restores the original values recorded in the snapshot, under a per-device lock.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `serial` | `--serial` `-s` | text | yes |  | Explicit ADB target serial. |
| `snapshot` | `--snapshot` | path | yes |  | Snapshot written by `apply`. |

- Refuses when device/build/user identity differs. A setting is restored only if it still equals the value the profile applied; a setting already at its original value is skipped; anything else is a conflict. Any conflict aborts before changing anything, retains the snapshot and exits 1.
- Best-effort: restore is explicit and not an automatic session supervisor. No `--json` flag.

```text
adapt dev-options restore -s SERIAL --snapshot stay-awake.json
```

## `adapt tools`

Curated tool catalog, goal plans and the verified scrcpy installer. 4 commands.

### `adapt tools list`

List curated tools and observe their candidates on the current PATH.

**Effects.** Offline catalog plus PATH lookups of candidate executables.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `goal` | `--goal` | text | no |  | One of `testing`, `mirroring`, `remote-worker`, `aosp-build`, `lineage-build`, `image-inspection`. |
| `json_output` | `--json` | flag | no | off | Emit machine-readable JSON only. |

- 16 tools, 6 goals. A PATH hit shows a location only: no version, provenance or branch compatibility check, and nothing is executed.

```text
adapt tools list
adapt tools list --goal mirroring --json
```

### `adapt tools info`

Show provenance, license, acquisition, and PATH observation for a tool.

**Effects.** Offline catalog record plus a PATH lookup.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `tool` | `TOOL` (argument) | text | yes |  | Catalog ID, or alias `adb`, `fastboot`, `ssh`, `sdkmanager`, `python3`, `javac`. |
| `json_output` | `--json` | flag | no | off | Emit machine-readable JSON only. |

- Shows purpose, source, license, acquisition guidance and dependency hints.

```text
adapt tools info scrcpy
adapt tools info fastboot
```

### `adapt tools plan`

Explain missing tools and acquisition boundaries without changing the host.

**Effects.** Explain-only.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `goal` | `GOAL` (argument) | text | yes |  | One of `testing`, `mirroring`, `remote-worker`, `aosp-build`, `lineage-build`, `image-inspection`. |
| `json_output` | `--json` | flag | no | off | Emit machine-readable JSON only. |
| `branch` | `--branch` | text | no |  | Your AOSP/LineageOS branch or tag (`[A-Za-z0-9][A-Za-z0-9._+/-]{0,127}`). Echoed, never verified. |
| `device` | `--device` | text | no |  | Your LineageOS device codename (same character rules). Echoed, never verified. |

- Classifies the host (Windows native, WSL, Linux native, other) for the goal. Native Windows is `unsupported` for AOSP/LineageOS builds; WSL is `qualified_unverified`; x86-64 Linux is `requires_upstream_review`; other architectures `unsupported_architecture`. These are guidance labels, not certification.
- Never syncs sources, builds, installs prerequisites, unlocks or flashes.

```text
adapt tools plan testing
adapt tools plan lineage-build --device DEVICE_CODENAME --branch BRANCH
```

### `adapt tools install`

Reuse an existing scrcpy or install the verified pinned release after confirmation.

**Effects.** Downloads and unpacks the pinned scrcpy release into a managed user directory after confirmation. Never changes PATH or launches scrcpy.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `tool` | `TOOL` (argument) | text | yes |  | Only `scrcpy` is installable; anything else exits 2. |
| `yes` | `--yes` `-y` | flag | no | off | Approve the pinned user-scope scrcpy installation. Required with `--json`. |
| `dry_run` | `--dry-run` | flag | no | off | Preview only; no download, writes, or confirmation. |
| `json_output` | `--json` | flag | no | off | Emit JSON; a new installation requires --yes. |

- Pinned scrcpy v5.0.1 with per-archive SHA-256 checks, bounded download size, redirect and archive-member limits, and an install receipt. Managed assets exist for Windows x86-64/x86/ARM64 and Linux x86-64; other hosts need upstream review.
- Reuses an existing compatible scrcpy rather than replacing it. Exit 2 when refused (unsupported tool/host, JSON without `--yes`, conflict), 1 on failure.

```text
adapt tools install scrcpy --dry-run
adapt tools install scrcpy --yes
```

## `adapt android`

Android 10-17 planning matrix and per-device capability eligibility. 2 commands.

### `adapt android capabilities`

Read Android identity properties and evaluate capability boundaries.

**Effects.** Read-only property reads (`getprop`) on one exact transport.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `serial` | `--serial` `-s` | text | yes |  | Explicit complete ADB target. |
| `json_output` | `--json` | flag | no | off | Emit JSON only. |

- Evaluates 11 capability rows (USB ADB, pairing-based wireless ADB, ADB Wi-Fi 2.0, legacy USB-first TCP, screenshot, UI Automator, scrcpy audio, existing-display mirroring, new virtual display, input routing, developer-option keys) against the reported API.
- Version eligibility is not OEM certification. ADB Wi-Fi 2.0 needs API 37, adb 37.0.0+ and runtime mDNS evidence.

```text
adapt android capabilities -s SERIAL --json
```

### `adapt android versions`

Show the offline Android/API planning matrix without contacting adb.

**Effects.** Offline; never contacts adb.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `json_output` | `--json` | flag | no | off | Emit JSON only. |

- Nine baselines: API 29 Android 10, 30 Android 11, 31 Android 12, 32 Android 12L, 33 Android 13, 34 Android 14, 35 Android 15, 36 Android 16, 37 Android 17. Planning data, not a test claim.

```text
adapt android versions
adapt android versions --json
```

## `adapt dex`

Samsung DeX display inspection, dual mirroring and the experimental `pc-*` workflow and the native receiver's `receiver-*` commands. 16 commands.

### `adapt dex inspect`

Read identity, desktop-mode state, and logical displays.

**Effects.** Read-only: `getprop`, `dumpsys desktopmode`, `dumpsys display`.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `serial` | `--serial` `-s` | text | yes |  | Explicit complete ADB target. |
| `json_output` | `--json` | flag | no | off | Emit JSON only. |

- Classifies DeX as `active_dual`, `active_standalone`, `inactive` or `unknown`; an external display alone is not DeX evidence, and a non-Samsung manufacturer is never confirmed.
- Workflow status: `eligible` for API 33-37, `below_minimum`, `unverified_future`, `unknown`. Exit 1 on a DeX error.

```text
adapt dex inspect -s SERIAL --json
```

### `adapt dex list-displays`

List currently observed logical display IDs and metadata.

**Effects.** Read-only (same probes as `inspect`).

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `serial` | `--serial` `-s` | text | yes |  | Explicit complete ADB target. |
| `json_output` | `--json` | flag | no | off | Emit JSON only. |

- Display IDs are runtime values: never assume the desktop is display 2.

```text
adapt dex list-displays -s SERIAL
```

### `adapt dex plan-dual-mirror`

Return two exact scrcpy argv arrays without launching either.

**Effects.** Read-only probes; launches nothing.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `serial` | `--serial` `-s` | text | yes |  | Explicit complete ADB target. |
| `dex_display` | `--dex-display` | integer | yes |  | Existing nonzero logical display ID. |
| `json_output` | `--json` | flag | no | off | Emit JSON only. |

- Returns two exact scrcpy argv arrays: phone `--display-id=0` and the selected display, both `--no-audio` with distinct window titles. Display 0 is refused (a standalone desktop cannot be a second surface); the ID must exist now.

```text
adapt dex plan-dual-mirror -s SERIAL --dex-display DISPLAY_ID
```

### `adapt dex mirror`

Launch phone and confirmed active dual-mode DeX mirrors.

**Effects.** Launches two scrcpy processes (phone and DeX display). Changes no device setting.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `serial` | `--serial` `-s` | text | yes |  | Explicit complete ADB target. |
| `dex_display` | `--dex-display` | integer | yes |  | Logical display ID that `dex inspect` positively confirmed as the active DeX display. |
| `yes` | `--yes` `-y` | flag | no | off | Required: launches two scrcpy windows. |
| `view_only` | `--view-only` | flag | no | off | Disable control on both windows. |
| `scrcpy` | `--scrcpy` | path | no |  | Explicit scrcpy executable. Otherwise PATH, then ADAPT's managed install; never installed implicitly. |

- Requires `--yes`, API 33-37, a positively confirmed `active_dual` display equal to `--dex-display`, and scrcpy 2.0+. Control is enabled unless `--view-only`.
- Process creation only: video and input health are not verified. Closing the windows closes the mirrors.

```text
adapt dex mirror -s SERIAL --dex-display DISPLAY_ID --yes
adapt dex mirror -s SERIAL --dex-display DISPLAY_ID --view-only --yes
```

### `adapt dex receiver-plan`

Explain both PC receiver routes, including the experimental native one; runs nothing.

**Effects.** Offline; runs and checks nothing on this PC.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `json_output` | `--json` | flag | no | off | Emit one JSON document on stdout instead of the human rendering. |

- Compares Microsoft's Wireless Display feature (`pc-receiver-plan`) with ADAPT's own native receiver. For the native one it lists the prerequisites (Windows x64, a Wi-Fi adapter with Wi-Fi Direct support, the pinned Rust toolchain, TCP 7236 free), the build command and output path, the security posture (open admission by default; optional name/ID restrictions are not authentication; it listens on TCP 7236), the data directory and the follow-up commands. Whether any prerequisite holds on this PC is not observed.

```text
adapt dex receiver-plan --json
```

### `adapt dex receiver-status`

Read the native receiver's own status file and say what state it is in; read-only.

**Effects.** Reads `status.json` in the receiver's data directory and lists `dexdesk.exe` processes (image name and PID only). Writes nothing; never reads the receiver's log.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `data_dir` | `--data-dir` | path | no | `ADAPT_DEX_DATA_DIR`, else `%LOCALAPPDATA%\DeXDesk` | The receiver's data directory. The default is shown symbolically, not expanded. |
| `json_output` | `--json` | flag | no | off | Emit one JSON document on stdout instead of the human rendering. |

- Reports `state`: `not_running`, `starting`, `ready`, `connecting`, `streaming`, `stopping`, `not_working`, `stale`, `unverified` or `unreadable`. A file whose process is gone, or not updated for 10 seconds while its process is alive, is stale. The last failure's reason code is explained with possible causes and ordered next steps; the receiver's message text is withheld. When a phone is streaming the next steps are `pc-probe` and `pc-attach`.
- Exit codes: `0` a normal state, `1` unreadable status or a refusal, `2` `not_working`, `stale` or `unverified`. JSON keys: `schema_version`, `status` (`observed` or `refused`), `state`, `summary`, `data_dir`, `data_dir_source`, `status_file`, `process`, `stale`, `observed`, `stages`, `last_failure`, `last_session_end`, `warnings`, `next_steps`, `claim_boundary`.
- Reports what the receiver recorded; an advertising receiver is not a working desktop and a decoded frame does not certify input, audio or Samsung DeX.

```text
adapt dex receiver-status
adapt dex receiver-status --data-dir DIRECTORY --json
```

### `adapt dex receiver-start`

Start a user-built dexdesk.exe detached and wait for it to advertise; advertises this PC over Wi-Fi Direct.

**Effects.** Starts one detached `dexdesk.exe` process that advertises this PC over Wi-Fi Direct and listens on TCP 7236 on every IPv4 interface. Creates no firewall rule and writes no file of its own.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `exe` | `--exe` | path | yes |  | An existing file named `dexdesk.exe` that you built. Never searched for, downloaded or bundled. |
| `data_dir` | `--data-dir` | path | no | `ADAPT_DEX_DATA_DIR`, else `%LOCALAPPDATA%\DeXDesk` | Passed to the receiver as `ADAPT_DEX_DATA_DIR`; must be absolute after resolution. |
| `expect_name` | `--expect-name` | text | no |  | Admit only a phone with exactly this friendly name (at most 256 bytes, no control characters). Passed as `ADAPT_DEX_EXPECTED_NAME`, never on the command line. Not authentication. |
| `expect_id` | `--expect-id` | text | no |  | Admit only the phone with exactly this Windows peer ID (at most 1024 bytes). Passed as `ADAPT_DEX_EXPECTED_ID`. Not authentication. |
| `audio` | `--audio` | flag | no | off | Experimental audio: sets `ADAPT_DEX_AUDIO=1`. Audible, correct sound is not established. |
| `yes` | `--yes` | flag | no | off | Required: starting advertises this PC and opens TCP 7236. |
| `json_output` | `--json` | flag | no | off | Emit one JSON document on stdout instead of the human rendering. |

- Refuses outside Windows, without `--yes`, for a missing or wrongly named file, for an unusable restriction value, while any `dexdesk.exe` process runs (single instance) and when TCP 7236 cannot be bound, and says which. Returns once the receiver's `status.json` (for the process it started) shows it advertising, within 30 seconds. The report says open admission is in effect when no restriction was given; values are never printed back.
- Exit codes: `0` advertising observed (or already connecting or streaming), `1` refused, failed or exited (the recorded reason code is explained), `2` `start_pending` (no status in time; the process is left alone). Start-up errors the receiver reports only on stderr or in a message box are not captured.

```text
adapt dex receiver-start --exe PATH_TO_DEXDESK_EXE --yes
adapt dex receiver-start --exe PATH_TO_DEXDESK_EXE --expect-name PHONE_NAME --audio --yes --json
```

### `adapt dex receiver-stop`

Ask the native receiver to stop through its stop.request file; never ends a process by ID.

**Effects.** Creates `stop.request` in the data directory only when `status.json` there names a live `dexdesk.exe` that is still running; waits up to 20 s; removes the file after the process exits.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `data_dir` | `--data-dir` | path | no | `ADAPT_DEX_DATA_DIR`, else `%LOCALAPPDATA%\DeXDesk` | The receiver's data directory. |
| `yes` | `--yes` | flag | no | off | Required: closes the receiver window and ends any phone session. |
| `json_output` | `--json` | flag | no | off | Emit one JSON document on stdout instead of the human rendering. |

- Reports the final recorded state. If nothing is running it says so and writes nothing; if a `dexdesk.exe` process has no usable status here it refuses rather than guess. Never kills a process: an unconfirmed stop is `stop_pending` (exit 2) and the request stays in place.
- Exit codes: `0` stopped or nothing running, `1` refused, `2` `stop_pending`.

```text
adapt dex receiver-stop --yes
adapt dex receiver-stop --data-dir DIRECTORY --yes --json
```

### `adapt dex pc-plan`

Offline preview of experimental activation; no device or process access.

**Effects.** Offline; no device or process access.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `serial` | `--serial` `-s` | text | yes |  | Exact ADB transport identifier: a USB serial or a wireless `host:port` endpoint. Must be nonblank, free of control characters and must not start with `-`. Never inferred. |
| `method` | `--method` | text | no | `loopback` | `loopback` (default; asks Samsung for a local wireless display) or `overlay` (temporary simulated display setting). |
| `size` | `--size` | text | no | `1920x1080` | Display size `WIDTHxHEIGHT`; each side 320-4096 pixels. |
| `dpi` | `--dpi` | integer | no | `160` | Display density, 80-640. |
| `view_only` | `--view-only` | flag | no | off | Pass `--no-control` to scrcpy so the mirror cannot send input. |
| `json_output` | `--json` | flag | no | off | Emit one JSON document on stdout instead of the human rendering. |

- Preview of experimental activation. Overlay value is `SIZE/DPI,hdmi`. Loopback size is negotiated by firmware; `--size`/`--dpi` configure only the overlay method.

```text
adapt dex pc-plan -s SERIAL --method loopback --json
adapt dex pc-plan -s SERIAL --method overlay --size 1920x1080 --dpi 160
```

### `adapt dex pc-receiver-plan`

Explain Microsoft's receiver setup without installs or device actions.

**Effects.** Offline; installs and starts nothing.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `json_output` | `--json` | flag | no | off | Emit one JSON document on stdout instead of the human rendering. |

- Describes Microsoft's optional Wireless Display receiver (`App.WirelessDisplay.Connect~~~~0.0.1.0`, needs administrator approval). `scripts/install_windows_wireless_display.ps1` is a separate, reviewed, elevated script that installs only that capability and writes a receipt; ADAPT never runs it.
- The JSON also carries `alternative_route`, which points to ADAPT's own experimental native receiver and `adapt dex receiver-plan`.

```text
adapt dex pc-receiver-plan --json
```

### `adapt dex pc-probe`

Inspect live Samsung desktop/display APIs using a temporary local-built helper.

**Effects.** Pushes the bundled helper JAR to `/data/local/tmp`, runs it as the Android shell user, then deletes it. No APK install, root or display change.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `serial` | `--serial` `-s` | text | yes |  | Exact ADB transport identifier: a USB serial or a wireless `host:port` endpoint. Must be nonblank, free of control characters and must not start with `-`. Never inferred. |
| `yes` | `--yes` | flag | no | off | Required: pushes and runs the bundled helper JAR temporarily. |
| `json_output` | `--json` | flag | no | off | Emit one JSON document on stdout instead of the human rendering. |

- Limited to observed Samsung API 33-37. Reports framework-level Wi-Fi display, DeX enabled and display-type state plus all logical displays. The helper is compiled locally from `AdaptDexBridge.java`; its hashes are checked against `build.json` before use.

```text
adapt dex pc-probe -s SERIAL --yes --json
```

### `adapt dex pc-attach`

Mirror a positively observed externally started DeX desktop; preserve its receiver.

**Effects.** Runs the probe, then launches one controllable scrcpy window on the named display. The receiver connection stays user-owned.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `serial` | `--serial` `-s` | text | yes |  | Exact ADB transport identifier: a USB serial or a wireless `host:port` endpoint. Must be nonblank, free of control characters and must not start with `-`. Never inferred. |
| `display` | `--display` | integer | yes |  | Observed nonzero logical display ID from `pc-probe`. |
| `yes` | `--yes` | flag | no | off | Required: pushes/runs the helper to verify, then launches one scrcpy window. |
| `view_only` | `--view-only` | flag | no | off | Pass `--no-control` to scrcpy so the mirror cannot send input. |
| `json_output` | `--json` | flag | no | off | Emit one JSON document on stdout instead of the human rendering. |

- Requires `--yes`, Samsung API 33-37, DeX enabled state 4 with desktop display type 102, and the display to be either the sole external display or (API 33) a system desktop compositor display alongside exactly one Wi-Fi sink. Anything else is refused.
- Writes a receipt next to a new private session directory. Closing the scrcpy window ends the mirror only.

```text
adapt dex pc-attach -s SERIAL --display DISPLAY_ID --yes
```

### `adapt dex pc-start`

Start an owned, confirmed DeX display session; no silent method fallback.

**Effects.** Experimental. Spawns a detached supervisor that pushes the helper, activates one display method, launches an owned scrcpy window, and restores state on stop.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `serial` | `--serial` `-s` | text | yes |  | Exact ADB transport identifier: a USB serial or a wireless `host:port` endpoint. Must be nonblank, free of control characters and must not start with `-`. Never inferred. |
| `method` | `--method` | text | no | `loopback` | `loopback` (default) or `overlay`. No silent fallback between methods. |
| `size` | `--size` | text | no | `1920x1080` | Display size `WIDTHxHEIGHT`; each side 320-4096 pixels. |
| `dpi` | `--dpi` | integer | no | `160` | Display density, 80-640. |
| `view_only` | `--view-only` | flag | no | off | Pass `--no-control` to scrcpy so the mirror cannot send input. |
| `snapshot` | `--snapshot` | path | no |  | New session snapshot path. Default: a new private directory under the OS user state/data location. |
| `yes` | `--yes` | flag | no | off | Required: activation changes device display state and spawns a supervisor. |
| `json_output` | `--json` | flag | no | off | Emit one JSON document on stdout instead of the human rendering. |

- Requires `--yes`, Samsung API 33-37, and no existing wireless, DeX or non-internal display. `overlay` additionally requires `overlay_display_devices` to be unset (`null`/`none`) and only restores it if it is still the invocation's value.
- `loopback` is expected to be denied on some firmware (vendor permission). A simulated overlay display is not DeX; launch needs positive Samsung enabled/dual-display evidence. Up to 70 s startup wait, then a stop is requested.
- Snapshot is mode 0600 and bound to serial, build and Android user. Exit 1 on `failed`, `cleanup_conflict` or refusal.

```text
adapt dex pc-start -s SERIAL --method loopback --snapshot dex-session.json --yes
```

### `adapt dex pc-status`

Read the saved supervisor state only; no live device queries.

**Effects.** Reads the snapshot file only; no device queries.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `snapshot` | `--snapshot` | path | yes |  | Session snapshot written by `pc-start`. |
| `json_output` | `--json` | flag | no | off | Emit one JSON document on stdout instead of the human rendering. |

- Reports status, display, cleanup and last error.

```text
adapt dex pc-status --snapshot dex-session.json --json
```

### `adapt dex pc-stop`

Ask the matching supervisor to close its own mirror and recover its state.

**Effects.** Writes a token-bound stop request next to the snapshot and waits up to 15 s for the supervisor.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `snapshot` | `--snapshot` | path | yes |  | Session snapshot written by `pc-start`. |
| `json_output` | `--json` | flag | no | off | Emit one JSON document on stdout instead of the human rendering. |

- Never kills a saved PID. An unacknowledged stop is reported as `stop_pending` (exit 2); keep the snapshot.

```text
adapt dex pc-stop --snapshot dex-session.json
```

### `adapt dex pc-restore`

Recover an inactive owned overlay session without overwriting external changes.

**Effects.** Overlay sessions only: reverts `overlay_display_devices` if it still holds the session's value.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `serial` | `--serial` `-s` | text | yes |  | Exact ADB transport identifier: a USB serial or a wireless `host:port` endpoint. Must be nonblank, free of control characters and must not start with `-`. Never inferred. |
| `snapshot` | `--snapshot` | path | yes |  | Session snapshot written by `pc-start`. |
| `yes` | `--yes` | flag | no | off | Required. |
| `json_output` | `--json` | flag | no | off | Emit one JSON document on stdout instead of the human rendering. |

- Requires `--yes` and the snapshot's exact serial; identity must match. Loopback recovery is delegated to the helper's STOP/heartbeat watchdog; no blind global disconnect is performed.

```text
adapt dex pc-restore -s SERIAL --snapshot dex-session.json --yes
```

## `adapt detect`

Observe ADB/fastboot transports, root indicators, ROM and bootloader state without transitions. 2 commands.

### `adapt detect scan`

List independent ADB and fastboot transports without mode changes.

**Effects.** Read-only `adb devices` and `fastboot devices`; no mode change.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `transport` | `--transport` | text | no | `all` | `all` (default), `adb` or `fastboot`. |
| `json_output` | `--json` | flag | no | off | Emit one JSON document on stdout instead of the human rendering. |

- ADB and fastboot are reported independently; matching serial text does not prove physical identity. Exit 2 when partial.

```text
adapt detect scan
adapt detect scan --transport fastboot --json
```

### `adapt detect inspect`

Inspect one exact target using only the explicitly selected transport.

**Effects.** Read-only `getprop`, `id -u` and `command -v su` over adb, or `fastboot getvar` for the fastboot transport.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `serial` | `--serial` `-s` | text | yes |  | Exact ADB transport identifier: a USB serial or a wireless `host:port` endpoint. Must be nonblank, free of control characters and must not start with `-`. Never inferred. |
| `transport` | `--transport` | text | no | `adb` | `adb` (default) or `fastboot`. No automatic switching. |
| `json_output` | `--json` | flag | no | off | Emit one JSON document on stdout instead of the human rendering. |

- Reports root indicators, custom-ROM property markers (`ro.lineage.version`, `ro.modversion`, `ro.crdroid.version`) and bootloader lock evidence. All are heuristics: missing `su` does not prove an unrooted device, `test-keys` or an unlocked bootloader does not prove a custom ROM, and Android-reported properties are not hardware attestation.
- Samsung Download/Odin mode is not fastboot and is not detected. No reboot, unlock, flash, `su` execution or `adb root`. Exit 2 when partial.

```text
adapt detect inspect -s SERIAL
adapt detect inspect -s SERIAL --transport fastboot --json
```

## `adapt desktop`

Generic Android desktop/virtual-display app testing, distinct from Samsung DeX. 4 commands.

### `adapt desktop inspect`

Observe desktop settings, advertised features and current displays.

**Effects.** Read-only `getprop`, `pm list features`, `settings get` (two keys) and `dumpsys display`.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `serial` | `--serial` `-s` | text | yes |  | Exact ADB transport identifier: a USB serial or a wireless `host:port` endpoint. Must be nonblank, free of control characters and must not start with `-`. Never inferred. |
| `json_output` | `--json` | flag | no | off | Emit one JSON document on stdout instead of the human rendering. |

- Virtual-display eligibility is `version_eligible_oem_unverified` for API 29-37. `desktop_shell` is always `not_verified` and `samsung_dex` is never inferred.

```text
adapt desktop inspect -s SERIAL --json
```

### `adapt desktop plan`

Offline preview of a separate app display; no device access or writes.

**Effects.** Offline; no device access.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `serial` | `--serial` `-s` | text | yes |  | Exact ADB transport identifier: a USB serial or a wireless `host:port` endpoint. Must be nonblank, free of control characters and must not start with `-`. Never inferred. |
| `app` | `--app` | text | yes |  | Exact Android package name to launch; not a component or command. |
| `size` | `--size` | text | no | `1920x1080` | Display size `WIDTHxHEIGHT`; each side 320-4096 pixels. |
| `dpi` | `--dpi` | integer | no | `160` | Display density, 80-640. |
| `view_only` | `--view-only` | flag | no | off | Pass `--no-control` to scrcpy so the mirror cannot send input. |
| `json_output` | `--json` | flag | no | off | Emit one JSON document on stdout instead of the human rendering. |

- Plans `scrcpy --new-display=SIZE/DPI --start-app=PACKAGE --no-audio --no-vd-destroy-content`.

```text
adapt desktop plan -s SERIAL --app com.example.app --size 1920x1080 --dpi 160 --json
```

### `adapt desktop start`

Start one confirmed app/display session; leave existing mirrors open.

**Effects.** Launches one scrcpy process that creates a separate virtual display and starts the app on it.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `serial` | `--serial` `-s` | text | yes |  | Exact ADB transport identifier: a USB serial or a wireless `host:port` endpoint. Must be nonblank, free of control characters and must not start with `-`. Never inferred. |
| `app` | `--app` | text | yes |  | Exact Android package name to launch; not a component or command. |
| `size` | `--size` | text | no | `1920x1080` | Display size `WIDTHxHEIGHT`; each side 320-4096 pixels. |
| `dpi` | `--dpi` | integer | no | `160` | Display density, 80-640. |
| `view_only` | `--view-only` | flag | no | off | Pass `--no-control` to scrcpy so the mirror cannot send input. |
| `yes` | `--yes` | flag | no | off | Required: launches one scrcpy process creating a virtual display. |
| `json_output` | `--json` | flag | no | off | Emit one JSON document on stdout instead of the human rendering. |

- Requires `--yes`, observed API 29-37 and scrcpy 3.1+. Existing mirrors, settings, transports and servers are untouched. Stream health is not verified. A virtual display tests multi-window behavior; it does not prove Samsung DeX or desktop window decorations.

```text
adapt desktop start -s SERIAL --app com.example.app --size 1920x1080 --dpi 160 --yes
```

### `adapt desktop enable`

Request desktop/freeform developer settings with a recovery snapshot.

**Effects.** Applies the `desktop-testing` profile (same code path as `dev-options apply`).

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `serial` | `--serial` `-s` | text | yes |  | Exact ADB transport identifier: a USB serial or a wireless `host:port` endpoint. Must be nonblank, free of control characters and must not start with `-`. Never inferred. |
| `snapshot` | `--snapshot` | path | yes |  | New recovery snapshot path (same format as `dev-options apply`). |
| `yes` | `--yes` | flag | no | off | Required: writes two Developer Options settings. |
| `json_output` | `--json` | flag | no | off | Emit one JSON document on stdout instead of the human rendering. |

- Settings are requested, not certified; no reboot is performed. Undo with `adapt dev-options restore` and the same snapshot.

```text
adapt desktop enable -s SERIAL --snapshot desktop.json --yes
```

## `adapt ssh`

Read-only ADAPT checks on an explicitly selected SSH worker. 4 commands.

### `adapt ssh status`

Locate the local OpenSSH client without execution or network access.

**Effects.** Locates the OpenSSH client; reads no SSH config or keys and contacts no host.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `json_output` | `--json` | flag | no | off | Emit one JSON document on stdout instead of the human rendering. |

- `ADAPT_SSH` selects an explicit client.

```text
adapt ssh status --json
```

### `adapt ssh plan`

Preview one remote operation offline; no SSH config or key reads.

**Effects.** Offline.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `host` | `HOST` (argument) | text | yes |  | SSH host alias, hostname or IP, optionally `user@host`. No URLs, whitespace or options. |
| `operation_name` | `--operation` | text | no | `doctor` | `doctor` (default), `scan`, `inspect`, `android`, `desktop`, `dex` or `tools`. `inspect`, `android`, `desktop` and `dex` require `--device`. |
| `device` | `--device` `-s` | text | no |  | Exact device identifier **on the worker**. Required for `inspect`, `android`, `desktop`, `dex`; refused for the others. |
| `transport` | `--transport` | text | no | `adb` | `adb` (default), `fastboot` or `all`. `all` is valid only with `scan`; `fastboot` only with `scan`/`inspect`. |
| `remote_shell` | `--remote-shell` | text | no | `posix` | `posix` (default) or `powershell`: how the remote worker interprets the command line. |
| `remote_adapt` | `--remote-adapt` | text | no | `adapt` | `adapt`/`adapt.exe` on the worker's PATH, or an absolute path ending in that name. |
| `port` | `--port` | integer | no |  | SSH port, 1-65535 (`ssh -p`). |
| `identity` | `--identity` | text | no |  | Local private-key file path passed as `ssh -i`. ADAPT does not read it. |
| `json_output` | `--json` | flag | no | off | Emit one JSON document on stdout instead of the human rendering. |

- Builds the exact `ssh` argv and the remote `adapt --no-banner --no-tips --no-progress ... --json` command. Fixed options: `BatchMode=yes`, `StrictHostKeyChecking=yes`, `UpdateHostKeys=no`, `CheckHostIP=no`, `VerifyHostKeyDNS=no`, `ClearAllForwardings=yes`, `ForwardAgent=no`, `ForwardX11=no`, `PermitLocalCommand=no`, `RemoteCommand=none`, `ControlPath=none`, `ControlMaster=no`, `ControlPersist=no`, `ConnectTimeout=10`, `ConnectionAttempts=1`, `ServerAliveInterval=15`, `ServerAliveCountMax=2`.
- PowerShell workers receive an encoded command; arguments containing a double quote are refused.

```text
adapt ssh plan android-worker --operation doctor --json
adapt ssh plan android-worker --operation inspect --device SERIAL
```

### `adapt ssh run`

Run an allowlisted read-only check on this exact SSH worker.

**Effects.** Runs `ssh` once against the named worker. The worker runs a read-only ADAPT check and owns its ADB/fastboot access.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `host` | `HOST` (argument) | text | yes |  | SSH host alias, hostname or IP, optionally `user@host`. No URLs, whitespace or options. |
| `operation_name` | `--operation` | text | no | `doctor` | `doctor` (default), `scan`, `inspect`, `android`, `desktop`, `dex` or `tools`. `inspect`, `android`, `desktop` and `dex` require `--device`. |
| `device` | `--device` `-s` | text | no |  | Exact device identifier **on the worker**. Required for `inspect`, `android`, `desktop`, `dex`; refused for the others. |
| `transport` | `--transport` | text | no | `adb` | `adb` (default), `fastboot` or `all`. `all` is valid only with `scan`; `fastboot` only with `scan`/`inspect`. |
| `remote_shell` | `--remote-shell` | text | no | `posix` | `posix` (default) or `powershell`: how the remote worker interprets the command line. |
| `remote_adapt` | `--remote-adapt` | text | no | `adapt` | `adapt`/`adapt.exe` on the worker's PATH, or an absolute path ending in that name. |
| `port` | `--port` | integer | no |  | SSH port, 1-65535 (`ssh -p`). |
| `identity` | `--identity` | text | no |  | Local private-key file path passed as `ssh -i`. ADAPT does not read it. |
| `timeout` | `--timeout` | integer | no | `180` | Seconds before the SSH client is abandoned; 1-300. |
| `json_output` | `--json` | flag | no | off | Emit one JSON document on stdout instead of the human rendering. |

- Needs existing host-key trust and key/agent authentication; nothing is auto-accepted. No local ADB fallback. Remote JSON over 4 MiB is rejected. Remote exit codes 0-3 are relayed.
- *(development tree)* An SSH client failure (exit 255) adds a fixed `ssh_failure_reason` and `hint`, and a reply that is not one ADAPT JSON object adds `response_reason` and `hint`; ssh's own output is never copied into the report ([SSH workers](ssh-workers.md#failure-reasons)).
- SSH connectivity alone gives no USB access, authorized ADB transport or graphical session.

```text
adapt ssh run android-worker --operation doctor --json
adapt ssh run android-worker --operation inspect --device SERIAL --json
```

### `adapt ssh mcp-config`

Generate an observation-only MCP-over-SSH client entry without launching.

**Effects.** Offline; writes nothing and starts nothing.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `host` | `HOST` (argument) | text | yes |  | SSH host alias, hostname or IP, optionally `user@host`. No URLs, whitespace or options. |
| `devices` | `--device` `-s` | text, repeatable | no |  | Exact device identifier on the worker; repeat to allow several. Each becomes a `--device` on the remote `adapt mcp --read-only`. |
| `remote_shell` | `--remote-shell` | text | no | `posix` | `posix` (default) or `powershell`: how the remote worker interprets the command line. |
| `remote_adapt` | `--remote-adapt` | text | no | `adapt` | `adapt`/`adapt.exe` on the worker's PATH, or an absolute path ending in that name. |
| `port` | `--port` | integer | no |  | SSH port, 1-65535 (`ssh -p`). |
| `identity` | `--identity` | text | no |  | Local private-key file path passed as `ssh -i`. ADAPT does not read it. |
| `json_output` | `--json` | flag | no | off | Emit one JSON document on stdout instead of the human rendering. |

- Prints an `mcpServers` entry named `adapt-worker` that launches SSH with the strict options and remote `adapt mcp --read-only [--device ...]`.

```text
adapt ssh mcp-config android-worker --device SERIAL --json
```

## `adapt app`

Installed-app identity, version and debuggable validation. 2 commands.

### `adapt app validate`

Check current-user installation and optional version/debuggable expectations.

**Effects.** Read-only `pm`/`dumpsys` queries for one package for the current Android user.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `serial` | `--serial` `-s` | text | yes |  | Exact ADB transport identifier: a USB serial or a wireless `host:port` endpoint. Must be nonblank, free of control characters and must not start with `-`. Never inferred. |
| `package` | `--package` | text | yes |  | Exact Android application ID such as `com.example.app`; not a component, wildcard or command. |
| `expected_version_code` | `--expect-version-code` | integer | no |  | Require this `versionCode`: a nonnegative 64-bit integer. |
| `expected_debuggable` | `--expect-debuggable` | text | no |  | `true` or `false`. Compares the installed debuggable flag only; says nothing about R8 or signing. |
| `json_output` | `--json` | flag | no | off | Emit one JSON document on stdout instead of the human rendering. |

- Exit 0 when every requested check passed, 1 if one failed, 2 if inconclusive. `passed` means only the listed checks. It never launches the app, verifies APK signatures or infers R8.

```text
adapt app validate -s SERIAL --package com.example.app --expect-version-code 42 --expect-debuggable false --json
```

### `adapt app build-modes`

Explain debuggable, release, R8, profiling, signing and evidence for AI workflows.

**Effects.** Offline reference.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `json_output` | `--json` | flag | no | off | Emit one JSON document on stdout instead of the human rendering. |

- Separates debuggable, build variant, R8, profileable, signing, installed and production-ready evidence.

```text
adapt app build-modes
```

## `adapt logs`

Bounded app and focused system log extraction; vendor logging guidance. 3 commands.

### `adapt logs capture`

Capture a finite app-UID log snapshot; optionally export without overwriting.

**Effects.** Read-only `logcat -d` filtered by the package's UID.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `serial` | `--serial` `-s` | text | yes |  | Exact ADB transport identifier: a USB serial or a wireless `host:port` endpoint. Must be nonblank, free of control characters and must not start with `-`. Never inferred. |
| `package` | `--package` | text | yes |  | Exact Android application ID such as `com.example.app`; not a component, wildcard or command. |
| `lines` | `--lines` | integer | no | `200` | Maximum lines returned, 1-500 (default 200). Output is also capped at 128 KiB. |
| `buffer` | `--buffer` | text | no | `main,crash` | `main`, `crash` or `main,crash` (default). |
| `level` | `--level` | text | no | `V` | Minimum severity `V`, `D`, `I`, `W`, `E` or `F` (default `V`). A reader filter, not a producer switch. |
| `output` | `--output` | path | no |  | Write the JSON report to this **new** file (mode 0600); an existing path is never overwritten. |
| `json_output` | `--json` | flag | no | off | Emit one JSON document on stdout instead of the human rendering. |

- Never clears buffers; refuses to fall back to a device-wide dump. Aborts if the UID or Android user changes during capture. Logs may hold secrets; empty output does not prove no crash.

```text
adapt logs capture -s SERIAL --package com.example.app --lines 200 --output app-logs.json
```

### `adapt logs guide`

Explain Samsung/vendor/window/print logging without contacting a device.

**Effects.** Offline.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `topic` | `--topic` | text | no | `all` | `all` (default), `samsung`, `vendor`, `window`, `dex`, `print`, `wifi` or `app`. |
| `json_output` | `--json` | flag | no | off | Emit one JSON document on stdout instead of the human rendering. |

- Separates Samsung/vendor verbose switches (manual), window, DeX, print, Wi-Fi and app logging.

```text
adapt logs guide --topic samsung
```

### `adapt logs diagnostic`

Capture recent fixed-tag system diagnostics; never enable verbose producers.

**Effects.** Read-only `logcat -d` on `main`, `system` and `crash` buffers with fixed tags.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `serial` | `--serial` `-s` | text | yes |  | Exact ADB transport identifier: a USB serial or a wireless `host:port` endpoint. Must be nonblank, free of control characters and must not start with `-`. Never inferred. |
| `topic` | `--topic` | text | yes |  | `window`, `dex` or `print`. |
| `minutes` | `--minutes` | integer | no | `2` | Look-back window on the device clock, 1-10 (default 2). |
| `lines` | `--lines` | integer | no | `200` | Maximum lines returned, 1-500 (default 200). Output is also capped at 128 KiB. |
| `level` | `--level` | text | no | `V` | Minimum severity `V`, `D`, `I`, `W`, `E` or `F` (default `V`). |
| `output` | `--output` | path | no |  | Write the JSON report to this **new** file (mode 0600); an existing path is never overwritten. |
| `json_output` | `--json` | flag | no | off | Emit one JSON document on stdout instead of the human rendering. |

- Tags: window `WindowManager, WindowManagerShell, ActivityTaskManager, ShellTransitions`; dex `DesktopModeService, WifiDisplayController, WifiDisplayAdapter, WifiP2pService, RemoteDisplay`; print `PrintManagerService, PrintSpooler, RemotePrintSpooler, RemotePrintService, PrintService`.
- Uses the device clock, never enables verbose producers, 2 MiB read and 10 s time budgets. Logs can contain multiple apps/users.

```text
adapt logs diagnostic -s SERIAL --topic window --minutes 2 --output window.json
```

## `adapt debug`

Debug-app configuration inspection and a debugger-setup preview. 2 commands.

### `adapt debug inspect`

Read persisted debug-app/wait configuration; do not infer debugger attachment.

**Effects.** Read-only `settings get global debug_app` and `wait_for_debugger`.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `serial` | `--serial` `-s` | text | yes |  | Exact ADB transport identifier: a USB serial or a wireless `host:port` endpoint. Must be nonblank, free of control characters and must not start with `-`. Never inferred. |
| `json_output` | `--json` | flag | no | off | Emit one JSON document on stdout instead of the human rendering. |

- Persisted configuration only; runtime selection and debugger attachment are `not_verified`.

```text
adapt debug inspect -s SERIAL --json
```

### `adapt debug plan`

Offline setup preview; executing set-debug-app would force-stop the chosen app.

**Effects.** Offline; prints the `am set-debug-app` command and executes nothing.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `serial` | `--serial` `-s` | text | yes |  | Exact ADB transport identifier: a USB serial or a wireless `host:port` endpoint. Must be nonblank, free of control characters and must not start with `-`. Never inferred. |
| `package` | `--package` | text | yes |  | Exact Android application ID such as `com.example.app`; not a component, wildcard or command. |
| `wait` | `--wait` | flag | no | off | Plan `am set-debug-app -w` (wait for debugger). |
| `persistent` | `--persistent` | flag | no | off | Plan `am set-debug-app --persistent`. |
| `json_output` | `--json` | flag | no | off | Emit one JSON document on stdout instead of the human rendering. |

- The planned command would force-stop the selected app (possibly across users); the plan says so. Restoring a prior selection is separate from clearing.

```text
adapt debug plan -s SERIAL --package com.example.app --wait --json
```

## `adapt rapid`

The Rapid ADAPT planning option. 1 command.

### `adapt rapid plan`

Preview the rapid test loop, with exact target and explicit recovery commands.

**Effects.** Offline; executes nothing and creates no files.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `serial` | `--serial` `-s` | text | yes |  | Exact ADB transport identifier: a USB serial or a wireless `host:port` endpoint. Must be nonblank, free of control characters and must not start with `-`. Never inferred. |
| `package` | `--package` | text | no |  | Optional exact application ID; adds app validation and app-log capture steps. |
| `session_dir` | `--session-dir` | path | no | `rapid-session` | Directory used in the planned snapshot/log paths (default `rapid-session`). Nothing is created. |
| `fast_ui` | `--fast-ui` | flag | no | off | Also plan the opt-in `fast-ui` profile and its restore. |
| `json_output` | `--json` | flag | no | off | Emit one JSON document on stdout instead of the human rendering. |

- Emits the check -> prepare -> reproduce -> capture -> restore loop as exact commands. It is a plan, not a runner or session supervisor.

```text
adapt rapid plan -s SERIAL --package com.example.app --session-dir test-session --json
adapt rapid plan -s SERIAL --package com.example.app --fast-ui
```

## `adapt tailscale`

Tailnet peers, ADB over a verified tailnet endpoint, and tailnet SSH workers. 13 commands.

### `adapt tailscale peers`

Read local daemon state and minimal peer records; do not contact peers.

**Effects.** Runs `tailscale status --json` locally; contacts no peer.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `json_output` | `--json` | flag | no | off | Emit one JSON document on stdout instead of the human rendering. |

- Identical to `status`. Returns minimal records (ID, host/DNS name, OS, node IPs, online, expired). Online does not mean ADB is listening. `ADAPT_TAILSCALE` selects the client.

```text
adapt tailscale peers
```

### `adapt tailscale status`

Read local daemon state and minimal peer records; do not contact peers.

**Effects.** Runs `tailscale status --json` locally; contacts no peer.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `json_output` | `--json` | flag | no | off | Emit one JSON document on stdout instead of the human rendering. |

- Identical to `peers`.

```text
adapt tailscale status --json
```

### `adapt tailscale guide`

Explain direct-phone, USB-worker and subnet-router paths offline.

**Effects.** Offline.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `json_output` | `--json` | flag | no | off | Emit one JSON document on stdout instead of the human rendering. |

- Direct phone, USB worker and subnet-router paths.

```text
adapt tailscale guide
```

### `adapt tailscale plan`

Preview direct ADB over Tailscale without reading status or touching a phone.

**Effects.** Offline: does not read Tailscale status or contact a phone.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `peer` | `--peer` | text | yes |  | Exact, unique tailnet peer: node name, ID, MagicDNS name or Tailscale node IP. Ambiguous, offline or expired peers are refused. |
| `port` | `--port` | integer | yes |  | TCP port, 1-65535, as currently displayed on the phone. Pairing and connection ports differ and change. |
| `pairing_port` | `--pairing-port` | integer | no |  | Optional current pairing port; adds a `pair` step to the plan. |
| `mode` | `--mode` | text | no | `tls` | `tls` (default: Android 11+ Wireless debugging listener) or `legacy` (an already enabled TCP ADB listener). |
| `json_output` | `--json` | flag | no | off | Emit one JSON document on stdout instead of the human rendering. |

- Lists the pair, connect, check and adb steps.

```text
adapt tailscale plan --peer PHONE_PEER --port CONNECT_PORT --pairing-port PAIR_PORT --json
```

### `adapt tailscale probe`

Test one selected peer TCP port; this does not prove ADB authorization.

**Effects.** Opens one TCP connection to the selected peer's numeric tailnet address (4 s timeout).

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `peer` | `--peer` | text | yes |  | Exact, unique tailnet peer: node name, ID, MagicDNS name or Tailscale node IP. Ambiguous, offline or expired peers are refused. |
| `port` | `--port` | integer | yes |  | TCP port, 1-65535, as currently displayed on the phone. Pairing and connection ports differ and change. |
| `json_output` | `--json` | flag | no | off | Emit one JSON document on stdout instead of the human rendering. |

- `tcp_open` proves a TCP handshake only: no ADB request, no authorization.

```text
adapt tailscale probe --peer PHONE_PEER --port CONNECT_PORT
```

### `adapt tailscale connect`

Connect and verify exactly one peer's Android ADB listener.

**Effects.** Runs `adb connect` to `TAILNET_IP:PORT`, then verifies identity.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `peer` | `--peer` | text | yes |  | Exact, unique tailnet peer: node name, ID, MagicDNS name or Tailscale node IP. Ambiguous, offline or expired peers are refused. |
| `port` | `--port` | integer | yes |  | TCP port, 1-65535, as currently displayed on the phone. Pairing and connection ports differ and change. |
| `mode` | `--mode` | text | no | `tls` | `tls` (default: Android 11+ Wireless debugging listener) or `legacy` (an already enabled TCP ADB listener). |
| `json_output` | `--json` | flag | no | off | Emit one JSON document on stdout instead of the human rendering. |

- Success needs an authorized transport in state `device`, a model/API/identity read and, for `tls`, `service.adb.tls.port` equal to `--port` with API >= 30. If the check fails the connection is left intact and no fallback runs.

```text
adapt tailscale connect --peer PHONE_PEER --port CONNECT_PORT --json
```

### `adapt tailscale check`

Verify an existing tailnet ADB transport, TLS port and device identity.

**Effects.** Read-only verification of an existing transport.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `peer` | `--peer` | text | yes |  | Exact, unique tailnet peer: node name, ID, MagicDNS name or Tailscale node IP. Ambiguous, offline or expired peers are refused. |
| `port` | `--port` | integer | yes |  | TCP port, 1-65535, as currently displayed on the phone. Pairing and connection ports differ and change. |
| `mode` | `--mode` | text | no | `tls` | `tls` (default: Android 11+ Wireless debugging listener) or `legacy` (an already enabled TCP ADB listener). |
| `json_output` | `--json` | flag | no | off | Emit one JSON document on stdout instead of the human rendering. |

- Same verification as `connect` without connecting.

```text
adapt tailscale check --peer PHONE_PEER --port CONNECT_PORT --json
```

### `adapt tailscale pair`

Pair with Android's current TLS pairing port; code is never an argv value.

**Effects.** Runs `adb pair` against the selected peer's pairing port.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `peer` | `--peer` | text | yes |  | Exact, unique tailnet peer: node name, ID, MagicDNS name or Tailscale node IP. Ambiguous, offline or expired peers are refused. |
| `port` | `--port` | integer | yes |  | TCP port, 1-65535, as currently displayed on the phone. Pairing and connection ports differ and change. |
| `code_stdin` | `--code-stdin` | flag | no | off | Read the six-digit code from stdin instead of the hidden prompt. There is no argv option for the code. |
| `json_output` | `--json` | flag | no | off | Emit one JSON document on stdout instead of the human rendering. |

- Code is six digits and read from a hidden prompt or `--code-stdin`; it is not stored or returned. Pairing is authorization, not a connection.

```text
adapt tailscale pair --peer PHONE_PEER --port PAIR_PORT
```

### `adapt tailscale disconnect`

Disconnect this one endpoint only; VPN/listener and other devices are preserved.

**Effects.** Runs `adb disconnect` for this one endpoint.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `peer` | `--peer` | text | yes |  | Exact, unique tailnet peer: node name, ID, MagicDNS name or Tailscale node IP. Ambiguous, offline or expired peers are refused. |
| `port` | `--port` | integer | yes |  | TCP port, 1-65535, as currently displayed on the phone. Pairing and connection ports differ and change. |
| `yes` | `--yes` | flag | no | off | Required. Disconnects only this endpoint. |
| `json_output` | `--json` | flag | no | off | Emit one JSON document on stdout instead of the human rendering. |

- Does not touch other devices, the phone's VPN or its listener.

```text
adapt tailscale disconnect --peer PHONE_PEER --port CONNECT_PORT --yes
```

### `adapt tailscale adb`

Run a guarded device command: ... -- shell getprop ro.product.model.

**Effects.** Runs one guarded device command on the verified exact endpoint.

Takes the remaining arguments as the command to pass through (unknown options are accepted and forwarded).

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `peer` | `--peer` | text | yes |  | Exact, unique tailnet peer: node name, ID, MagicDNS name or Tailscale node IP. Ambiguous, offline or expired peers are refused. |
| `port` | `--port` | integer | yes |  | TCP port, 1-65535, as currently displayed on the phone. Pairing and connection ports differ and change. |
| `mode` | `--mode` | text | no | `tls` | `tls` (default: Android 11+ Wireless debugging listener) or `legacy` (an already enabled TCP ADB listener). |

- Put the adb command after `--`. Host/server commands (`connect`, `pair`, `tcpip`, `usb`, `root`, `devices`, `kill-server`, ...) and options are refused; ADAPT's normal command guard applies and `shell` times out after 60 s.

```text
adapt tailscale adb --peer PHONE_PEER --port CONNECT_PORT -- shell getprop ro.product.model
```

### `adapt tailscale worker-plan`

Plan an existing SSH-worker operation on a tailnet host, offline.

**Effects.** Offline; same as `ssh plan` against a tailnet host.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `peer` | `--peer` | text | yes |  | Exact, unique tailnet peer: node name, ID, MagicDNS name or Tailscale node IP. Ambiguous, offline or expired peers are refused. |
| `operation` | `--operation` | text | no | `inspect` | Same operations as `ssh plan` (default `inspect`). |
| `device` | `--device` | text | no |  | Exact device identifier on the worker (needed for device-scoped operations). |
| `user` | `--user` | text | no |  | Remote account name (letters, digits, `_`, `.`, `-`; no `@` or shell syntax). |
| `remote_shell` | `--remote-shell` | text | no | `posix` | `posix` (default) or `powershell`: how the remote worker interprets the command line. |
| `remote_adapt` | `--remote-adapt` | text | no | `adapt` | `adapt`/`adapt.exe` on the worker's PATH, or an absolute path ending in that name. |
| `json_output` | `--json` | flag | no | off | Emit one JSON document on stdout instead of the human rendering. |

- The worker owns USB/ADB access.

```text
adapt tailscale worker-plan --peer LAB_HOST --operation inspect --device USB_SERIAL
```

### `adapt tailscale worker-run`

Run an allowlisted read-only check on an explicitly verified tailnet worker.

**Effects.** Selects the peer from local Tailscale status (online, unique), then runs `ssh run` against its numeric address.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `peer` | `--peer` | text | yes |  | Exact, unique tailnet peer: node name, ID, MagicDNS name or Tailscale node IP. Ambiguous, offline or expired peers are refused. |
| `operation` | `--operation` | text | no | `inspect` | Same operations as `ssh run` (default `inspect`). |
| `device` | `--device` | text | no |  | Exact device identifier on the worker (needed for device-scoped operations). |
| `user` | `--user` | text | no |  | Remote account name (letters, digits, `_`, `.`, `-`; no `@` or shell syntax). |
| `remote_shell` | `--remote-shell` | text | no | `posix` | `posix` (default) or `powershell`: how the remote worker interprets the command line. |
| `remote_adapt` | `--remote-adapt` | text | no | `adapt` | `adapt`/`adapt.exe` on the worker's PATH, or an absolute path ending in that name. |
| `json_output` | `--json` | flag | no | off | Emit one JSON document on stdout instead of the human rendering. |

- Existing SSH host-key and key/agent checks apply.

```text
adapt tailscale worker-run --peer LAB_HOST --user USER --operation inspect --device USB_SERIAL
```

### `adapt tailscale worker-mcp-config`

Generate a target-restricted, read-only MCP-over-SSH entry without connecting.

**Effects.** Offline; same as `ssh mcp-config` for one device.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `peer` | `--peer` | text | yes |  | Exact, unique tailnet peer: node name, ID, MagicDNS name or Tailscale node IP. Ambiguous, offline or expired peers are refused. |
| `device` | `--device` | text | yes |  | Exact device identifier on the worker. Required. |
| `user` | `--user` | text | no |  | Remote account name (letters, digits, `_`, `.`, `-`; no `@` or shell syntax). |
| `remote_shell` | `--remote-shell` | text | no | `posix` | `posix` (default) or `powershell`: how the remote worker interprets the command line. |
| `remote_adapt` | `--remote-adapt` | text | no | `adapt` | `adapt`/`adapt.exe` on the worker's PATH, or an absolute path ending in that name. |
| `json_output` | `--json` | flag | no | off | Emit one JSON document on stdout instead of the human rendering. |

- Peer is not verified offline.

```text
adapt tailscale worker-mcp-config --peer LAB_HOST --device USB_SERIAL --json
```

## `adapt agent`

Orient an AI agent. *(development tree)* Not in 0.1.1. The group prints text or JSON only: it reads the installed command tree and the MCP tool classification, and it never runs adb, touches a device or opens a connection. Banners, tips and progress are not shown for it.

### `adapt agent primer`

Print the agent primer: what ADAPT is, first steps, the development loop, safety rules, MCP and every command.

**Effects.** Offline. Writes only the primer to stdout.

| Parameter | Flags | Type | Required | Default | Description |
|---|---|---|---|---|---|
| `json_output` | `--json` | flag | no | off | Emit the structured primer, with the full command and MCP tool catalog, as JSON. |

- Markdown by default (ASCII only). The JSON document has `what_is_adapt`, `first_steps`, `development_loop`, `safety_rules`, `output_conventions`, `mcp` (start flags, registration forms, per-session tool counts and every tool with its observation or acting kind), `commands` (every visible leaf command with its summary and whether it takes `-s`, `--json` and `--yes`), `quoted_commands` and `claim_boundary`.
- Every command the primer quotes is checked against the real command tree by `tests/test_agent_guide.py`; that proves the commands and flags exist, not that a workflow works on a device.
- Bare `adapt agent` prints the same Markdown primer. The root `adapt --help` text points agents here.

```text
adapt agent primer
adapt agent primer --json
```

---

Related: [features](features.md), [architecture](architecture.md), [MCP access](mcp-access.md), [known limitations](known-limitations.md). Existing topic guides such as [Tailscale](tailscale.md), [SSH workers](ssh-workers.md) and [DeX for PC](dex-pc.md) go deeper on individual command families.
