# Features

ADAPT (Android Device Agent Provisioning Toolkit) is an alpha toolkit of **19 functional areas** exposed through **64 visible CLI leaf commands in 13 groups** and **24 MCP tools**. This guide says what each area does, which commands and tools implement it, what it deliberately does not do, and how much evidence stands behind it.

Counts, flags and examples are taken from the source in this repository. See the [command reference](command-reference.md) for every flag, [MCP access](mcp-access.md) for the tool schemas, [architecture](architecture.md) for topology diagrams and [known limitations](known-limitations.md) for open gaps. `docs/feature-inventory.json` holds the machine-readable inventory behind the counts.

## At a glance

| What | Count |
|---|---|
| Functional areas | 19 |
| Visible CLI entries / leaf commands / groups (root + 12) | 77 / **64** / **13** |
| Hidden compatibility aliases | 2 (`home`, `give-me-everything`) |
| MCP tools | **24** = 19 observation + 5 mutating (18 read-only tools with an exact-device restriction) |
| Catalog tools / goals | **16** / **6** (2 with automatic acquisition, 14 manual guidance) |
| Developer Options profiles / inspected settings | **4** / **13** |
| Authored help topics | **59** |
| Android baselines in the offline matrix | 9 (API 29-37 = Android 10-17, including 12L) |
| Physical Samsung baselines with recorded observations | 2 (API 33 and API 37) |

### Evidence levels used below

| Label | Meaning |
|---|---|
| **Offline-tested** | Automated tests with mocked `adb`/`ssh`/`tailscale`/archives. No device was involved. |
| **Observed** | Also exercised in bounded sessions on one or both Samsung phones. A snapshot of one build, not certification. |
| **Planning** | Prints advice or exact commands; executes nothing. |
| **Experimental** | Firmware-, radio- or Windows-version-dependent; manual steps; no compatibility promise. |

Counts of tests are not a pass rate and are never added to physical observations. Physical evidence covers only two Samsung phones (Android 13/API 33 and Android 17/API 37); every other API level, OEM, host architecture and external client is untested. Android 10 is covered by the offline matrix and the legacy USB-first flow only.

---

## 1. Host provisioning

**`adapt setup`** | MCP `setup_status` (read-only) | *Offline-tested*

Downloads Google's official Platform-Tools archive (HTTPS, `dl.google.com` only) into user scope, verifies its digest (explicit `--sha256`, else Google's repository metadata; a mismatch always fails), extracts it with path-traversal and size protections, checks the staged `adb` version (>= 34.0.0), swaps it in atomically and, unless `--no-path`, adds it to the user `PATH`. `--dry-run` previews, `--isolated` uses `~/.android/platform-tools`, `--force` replaces a healthy install.

Does not: use `sudo`/UAC, touch `ANDROID_HOME` or system paths, install USB drivers or OS packages, or replace another ADB installation. The confirmation prompt appears only in an interactive terminal. Fresh-installer acceptance on both host OSes is not established.

```text
adapt setup --dry-run
adapt setup --yes --isolated --no-path
```

## 2. Doctor and driver guidance

**`adapt doctor`** | MCP `doctor` | *Observed* (Windows 11 host with the two Samsung phones) and offline-tested

Locates `adb` (PATH, `ANDROID_HOME`, user-scope directories, `ADAPT_ADB`), checks the version, origin and PATH configuration, inspects the **shared server on port 5037** (owner, root/system ownership, version match), checks Windows PnP USB driver state or Linux `udev`/`plugdev`, and lists transports. Exit codes: 3 unsafe server, 1 missing toolchain, 2 device unauthorized/permissions, 0 ok. `--json` emits a machine report; `--fix` prints remediation text only.

Does not: repair drivers, kill the daemon, delete adb keys, or run `sudo` by itself. In an interactive terminal a `Next action` menu follows; its `udev` choice runs the udev-rule sudo commands and its `setup` choice runs setup, **only if you pick them**. Opening Device Manager on Windows is for manual driver work.

## 3. ADB and wireless transports

**`adapt devices`, `adapt adb`, `adapt pair`, `adapt connect`** | MCP `list_devices`, `device_info` | *Observed* (USB, simultaneous USB and Wi-Fi, private routed wireless) and offline-tested

- `devices` runs `adb devices -l` behind the shared-server preflight.
- `adb` passes one command to the resolved adb after a safety check. Refused: `kill-server`, `sudo`, `uninstall`, `pm clear`, bare `shell`, unbounded `logcat`, and shell payloads ADAPT cannot inspect (unquoted `; | & < > $`, nested shells). Shell commands time out after 60 s. With several ready devices you must pass `-s`. It is a guarded command runner, not a general shell.
- `pair` and `connect` wrap `adb pair` and `adb connect` with bounded timeouts and result checks. **Pairing is not a connection**; Android 11+ uses two different, changing ports. Android 10 uses the legacy USB-first workflow.

```text
adapt devices
adapt pair 192.0.2.10:PAIR_PORT
adapt connect 192.0.2.10:CONNECT_PORT
adapt adb -s SERIAL shell getprop ro.build.version.release
```

## 4. Console and branding

**`adapt console`** (hidden alias `home`; bare `adapt` in a terminal) | *Observed* in a Windows terminal; offline-tested

An original fading ASCII wordmark and **nine screens**: Host/drivers/setup; Devices/wireless/Tailscale; Apps/developer testing; Displays/Samsung DeX; Tools/ROM workflows/Give Me Everything; AI/MCP/SSH workers; Rapid ADAPT; Logs/help/preferences; All commands. `H` Home, `B` Back, `N`/`P` pages, `R` runs a selected command. `--no-redraw` gives append-only output for screen readers and basic terminals; `--no-banner` or `ADAPT_NO_BANNER=1` hides the logo.

The console is a browser over the CLI: each selection runs the normal command with its normal confirmations. It is not a second execution engine.

## 5. Help, tips and guides

**`adapt help-me`, `adapt tips`, `adapt guide`** | *Offline-tested*

`help-me` explains workflows and, with `--all`, lists every command and option of the installed build. There are **59 authored topics** with a generic example and a boundary note, covering command groups, most commands and four workflow topics (`wireless`, `device_manager`, `udev`, `recheck`). `tips on|off|status` stores a per-user preference; `--no-tips` and `ADAPT_TIPS=0` override it per run. Tips never appear for `--json`, MCP or non-terminal output. `guide` prints short official-tooling notes for `testing`, `rom`, `ai`, `wireless`.

## 6. Goal-based tool catalog

**`adapt tools list|info|plan`** | MCP `tool_catalog` | *Planning* (offline-tested)

A curated catalog of **16 tools across 6 goals**, with purpose, source, license, acquisition guidance and PATH observation. `plan GOAL` adds a host assessment and, for builds, your supplied branch and device codename.

| Goal | Tools |
|---|---|
| `testing` | platform-tools, android-sdk-command-line, perfetto |
| `mirroring` | platform-tools, scrcpy |
| `remote-worker` | openssh |
| `aosp-build` | git, repo, gpg, java, python, ccache |
| `lineage-build` | git, repo, gpg, java, python, ccache |
| `image-inspection` | python, avbtool, unpack_bootimg, simg2img, lpunpack, payload-dumper-go |

Aliases: `adb`, `fastboot` (platform-tools), `ssh` (openssh), `sdkmanager`, `python3`, `javac`. A PATH hit means "an executable with that name exists": no provenance, version or branch-compatibility check, and nothing is run. Build-host labels are guidance: native Windows `unsupported`, WSL `qualified_unverified`, x86-64 Linux `requires_upstream_review`. **ADAPT never builds a ROM, syncs sources, or flashes**; the catalog is manual ROM-gathering guidance and most entries are not installed by ADAPT.

## 7. Verified scrcpy installation

**`adapt tools install scrcpy`** | *Offline-tested*

Installs the pinned scrcpy v5.0.1 into a managed user directory after confirmation: per-archive SHA-256, allowlisted GitHub hosts, bounded redirects, download size, expansion ratio and member count, zip-slip/tar-bomb protection, and a receipt. Managed assets: Windows x86-64, x86, ARM64 and Linux x86-64. Reuses an existing scrcpy instead of replacing it. Never edits `PATH` or launches scrcpy. `--dry-run` previews; `--json` requires `--yes`.

## 8. Give Me Everything

**`adapt everything`** (hidden alias `give-me-everything`) | *Offline-tested*

A confirmed batch of the **implemented** steps only: host check, Platform-Tools (reuse or verified install), scrcpy (reuse or pinned install), host check again, a read-only fastboot listing, catalog PATH observation, and read-only per-device inspections for explicit `--device` targets. **Only 2 of the 16 catalog tools have an installer; the other 14 get guidance** and are reported as `located_unverified` or `manual_required`. It performs no device mutation, system package or driver install, pairing, profile, APK install, mirror launch, SSH or hypervisor action. It is not atomic. `--json` execution requires `--yes`.

```text
adapt everything --dry-run
adapt everything --yes --device SERIAL --json
```

## 9. Android capability matrix

**`adapt android versions|capabilities`** | MCP `android_versions`, `android_capabilities` | *Offline-tested* matrix; *Observed* on API 33 and 37

| API | Android | | API | Android | | API | Android |
|---|---|---|---|---|---|---|---|
| 29 | 10 | | 32 | 12L | | 35 | 15 |
| 30 | 11 | | 33 | 13 | | 36 | 16 |
| 31 | 12 | | 34 | 14 | | 37 | 17 |

`versions` is offline. `capabilities` reads identity properties from one exact transport and rates 11 capabilities (USB ADB, pairing-based wireless ADB from API 30, ADB Wi-Fi 2.0 at API 37, legacy USB-first TCP, screenshot, UI Automator, scrcpy audio from API 30, existing-display mirroring, new virtual display from API 29, input routing, developer-option keys). The result is **version eligibility, not OEM certification**. ADB Wi-Fi 2.0 additionally needs adb 37.0.0+ and runtime mDNS evidence.

## 10. Root, ROM and bootloader detection

**`adapt detect scan|inspect`** | *Offline-tested*

`scan` lists ADB and fastboot transports independently. `inspect` reads `getprop`, `id -u` and `command -v su` over ADB (or `fastboot getvar` over fastboot) and reports root indicators, custom-ROM property markers (Lineage, crDroid) and bootloader lock evidence from three Android-reported properties. Everything is a **heuristic**: a missing `su` does not prove a device is unrooted, `test-keys` or an unlocked bootloader does not prove a custom ROM, and self-reported properties are not attestation. **Root and ROM indicators are not proof.** Samsung Download/Odin mode is not fastboot and is not detected. It never reboots, unlocks, flashes, runs `su` or requests `adb root`; there is no automatic unlock, flash or ROM build anywhere in ADAPT.

## 11. Recoverable developer profiles

**`adapt dev-options inspect|apply|restore`** | MCP `developer_options_inspect` | *Observed* (stay-awake inspection on both phones) and offline-tested (snapshot, rollback, conflicts)

13 inspected settings and 4 profiles:

| Profile | Writes | Notes |
|---|---|---|
| `fast-ui` | `window_animation_scale`, `transition_animation_scale`, `animator_duration_scale` = 0 | Opt-in; can hide animation-dependent bugs |
| `touches` | `show_touches` = 1 | |
| `stay-awake` | ORs the **USB bit (2)** into `stay_on_while_plugged_in`, **preserving AC/wireless bits** | Needs USB power; does not unlock or override device policy; readback is not proof of wakefulness; refuses unknown values |
| `desktop-testing` | `enable_freeform_support` = 1, `force_desktop_mode_on_external_displays` = 1 | API 29-37 only; may need a manual reboot; OEM may ignore |

| Inspected (13) | Table / key |
|---|---|
| developer_mode, usb_debugging, wireless_debugging | `global`: `development_settings_enabled`, `adb_enabled`, `adb_wifi_enabled` |
| stay_awake | `global`: `stay_on_while_plugged_in` |
| three animation scales | `global`: `window_animation_scale`, `transition_animation_scale`, `animator_duration_scale` |
| show_touches, pointer_location | `system`: `show_touches`, `pointer_location` |
| dont_keep_activities | `global`: `always_finish_activities` |
| show_background_anrs | `secure`: `anr_show_background` |
| freeform_windows, force_desktop | `global`: `enable_freeform_support`, `force_desktop_mode_on_external_displays` |

`apply` writes only the allowlisted keys of one profile, after exclusively creating a 0600 snapshot bound to device serial, build fingerprint and Android user; each write is read back, and a failure rolls back what was applied. `restore` puts back originals **only if a setting still holds the value the profile applied**; anything else is a conflict that aborts without changes and keeps the snapshot. Restore is **best-effort** and explicit.

## 12. App, build and debug validation

**`adapt app validate|build-modes`, `adapt debug inspect|plan`** | MCP `app_validate`, `build_modes`, `debug_app_inspect` | *Offline-tested*

`app validate` checks that an exact package is installed for the current user and, optionally, `--expect-version-code` and `--expect-debuggable true|false` (exit 0 passed, 1 failed, 2 inconclusive). `debug inspect` reads the persisted debug-app and wait-for-debugger settings. `debug plan` prints the `am set-debug-app` command and notes that **running it would force-stop the app**. `build-modes` explains debuggable vs variant vs R8 vs profileable vs signing.

**Debug app inspect/plan never attaches a debugger.** A non-debuggable result does not prove R8 or a signer, and `passed` means only the requested checks passed.

## 13. Logs and vendor diagnostics

**`adapt logs capture|diagnostic|guide`** | MCP `app_logs`, `diagnostic_logs`, `logging_guide`, `logcat_tail` | *Observed* (DeX system-log capture on the API 33 phone) and offline-tested

- `capture`: finite `logcat -d` for the package's **UID** on `main`/`crash`, up to 500 lines and 128 KiB, never clearing buffers, no device-wide fallback.
- `diagnostic`: recent lines for fixed tags (`window`, `dex`, `print`) on the device clock, 1-10 minutes; never enables verbose producers.
- `guide`: offline notes separating Samsung verbose switches (manual), vendor logging, window, DeX, print, Wi-Fi and app logging.

Exports (`--output`) create new 0600 files and never overwrite. Logs may contain private data; an empty capture does not prove the absence of a crash and is not a test pass.

## 14. Generic desktop app testing

**`adapt desktop inspect|plan|enable|start`** | *Offline-tested*

`inspect` observes freeform/secondary-display features, two desktop settings and displays. `plan` previews `scrcpy --new-display=SIZE/DPI --start-app=PACKAGE --no-audio --no-vd-destroy-content`. `enable` applies the `desktop-testing` profile with a snapshot. `start` (needs `--yes`, API 29-37, scrcpy 3.1+) launches one scrcpy process for a separate app display and leaves existing mirrors alone. A virtual display tests multi-window behavior; it **does not prove Samsung DeX** or desktop window decorations.

## 15. Samsung DeX and the PC bridge

**`adapt dex ...` (12 commands)** | MCP `dex_inspect`, `dex_displays` | *Experimental*; *Observed* on the API 33 phone

| Command | Role |
|---|---|
| `inspect`, `list-displays` | Read DeX/desktop-mode evidence and logical displays. Display IDs are runtime values |
| `plan-dual-mirror`, `mirror` | Plan or launch two scrcpy windows (display 0 and a confirmed DeX display); API 33-37, scrcpy 2.0+ |
| `pc-plan`, `pc-receiver-plan` | Offline previews (phone-side activation; Microsoft receiver setup) |
| `pc-probe`, `pc-attach` | Run a temporary locally built helper to observe Samsung state; attach a scrcpy window to a positively identified, externally started DeX desktop |
| `pc-start`, `pc-status`, `pc-stop`, `pc-restore` | Trial activation (`loopback` or `overlay`) with an owned supervisor, snapshot, stop and recovery |

Boundaries: Samsung API 33-37 only; an external or virtual display alone is not DeX; activation can be denied by firmware (a vendor permission); no silent fallback between methods; the helper is a shell-user `app_process` JAR compiled from `AdaptDexBridge.java`, not an APK, root or a Samsung binary. **Samsung Wireless DeX needs firmware permission**; when shell activation is denied the supported alternative is a receiver on the PC.

The **native Windows wireless receiver** source is included under `native/windows-receiver` (upstream MIT DeXDesk revision `5070ccda5c363f5ed0b46d1fe7cf2c15c47fc125`). It is a manual-build, experimental component outside the Python wheel and is **not launched by ADAPT**; see its [README](../native/windows-receiver/README.md). Recorded observations: on the API 33 phone it formed a Wi-Fi Direct group, decoded desktop frames, and passed one controlled reconnect. All 15 offline native tests passed again from the public export; see [publication checks](publication-checks.json). There is no compatibility promise across radios, Windows versions or Android builds. MCP input tools have **no display targeting**.

## 16. SSH workers and hypervisor guidance

**`adapt ssh status|plan|run|mcp-config`** | *Offline-tested* (command construction, quoting, option enforcement); no live SSH acceptance

Runs one allowlisted, read-only ADAPT operation (`doctor`, `scan`, `inspect`, `android`, `desktop`, `dex`, `tools`) on an explicitly chosen worker through OpenSSH with `BatchMode`, strict host-key checking, no forwarding, no agent and no ControlMaster, and returns one JSON object (max 4 MiB). `mcp-config` prints an observation-only MCP-over-SSH entry. The worker owns USB/ADB; SSH alone gives no device or display access. Hypervisor guidance (Proxmox/KVM, VMware, Hyper-V, VirtualBox, XCP-ng) is architectural only: there are no hypervisor adapters and no VM/USB-assignment management. See [SSH workers](ssh-workers.md) and [hypervisors](hypervisors.md).

## 17. Tailscale mode

**`adapt tailscale ...` (13 commands)** | *Observed* (local peer parsing, a pairing success and a private routed path, but **no complete direct Tailscale run**) and offline-tested

| Command | Role |
|---|---|
| `status`, `peers` | Local daemon peers (identical), no peer contact |
| `guide`, `plan` | Offline direct-phone, USB-worker and subnet-router guidance; command plan |
| `probe` | One TCP handshake to one selected peer port |
| `pair`, `connect`, `check`, `adb`, `disconnect` | Pair (code never in argv), connect, verify identity and TLS port, run one guarded command, disconnect that endpoint only |
| `worker-plan`, `worker-run`, `worker-mcp-config` | `ssh` worker operations against a tailnet host |

Binds to one unique online peer's numeric tailnet address; no DNS fallback, scanning or second phone. **Tailscale only transports IP traffic**: it does not authorize ADB, install or sign in, edit ACLs, routes or exit nodes, or provide Miracast. **A direct Tailscale end-to-end run is unproven**: the strict TLS verifier requires `service.adb.tls.port` to equal the connect port, and that property was empty on the tested Samsung firmware. A private routed VPN path worked in pre-release testing.

## 18. Rapid ADAPT

**`adapt rapid plan`** | *Planning*

Prints the **check -> prepare -> reproduce -> capture -> restore** loop as exact commands (capability and option inspection, optional app validation, `stay-awake` and optional `fast-ui` with their snapshots, one deliberate journey, focused log capture, explicit restores). It is **rapid integrated planning, not an auto-runner**: it executes nothing, creates no files, and does not supervise or roll back a session.

## 19. AI and MCP access

**`adapt mcp`** | *Offline-tested*

A local stdio MCP server with **24 tools: 19 observation and 5 mutating** (`tap`, `swipe`, `key`, `type`, `install_apk`). `--read-only` hides the five; `--device` pins exact transports and removes `doctor`, so `--read-only --device` exposes **18** tools. Strict schemas, bounded outputs (logs <= 500 lines, screenshots <= 1280 px JPEG), generic error objects. No network service. **No external AI client has been live-accepted**, and MCP-over-SSH is untested. Full detail: [MCP access](mcp-access.md).

---

## Safety invariants

- No `adb kill-server`, no `sudo adb`; destructive package commands are refused by the guard.
- Shared ADB server on 5037 is verified before use; unsafe servers abort commands with exit 3.
- Exact targets; no discovery by scanning networks or hosts.
- Writes are allowlisted, snapshotted, read back and restored only when still ours.
- No automatic unlock, flash, root or ROM build.
- Secrets (pairing codes) never appear in argv for Tailscale mode and are not stored or returned.
- Logs, screenshots and snapshots may contain private data and are not redacted automatically.
