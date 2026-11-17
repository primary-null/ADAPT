# Features

ADAPT (Android Device Agent Provisioning Toolkit) is an alpha toolkit of **19 functional areas** exposed through **70 visible CLI leaf commands in 14 groups** and **24 MCP tools**. This guide says what each area does, which commands and tools implement it, what it deliberately does not do, and how much evidence stands behind it. It describes this repository's development tree, which contains unreleased changes that the 0.1.1 download does not have; each such change is marked *development tree* below. 0.1.1 has 64 visible leaf commands and 59 authored help topics.

Counts, flags and examples are taken from the development-tree source. See the [command reference](command-reference.md) for every flag, [MCP access](mcp-access.md) for the tool schemas, [architecture](architecture.md) for topology diagrams and [known limitations](known-limitations.md) for open gaps. `docs/feature-inventory.json` holds the machine-readable inventory behind the counts.

## At a glance

| What | Count |
|---|---|
| Functional areas | 19 |
| Visible CLI entries / leaf commands / groups (root + 13) | 84 / **70** / **14** |
| Hidden compatibility aliases | 2 (`home`, `give-me-everything`) |
| MCP tools | **24** = 19 observation + 5 mutating; `--read-only --device` exposes 18 (12 bound to the allowed devices, 6 host-level or offline) |
| Catalog tools / goals | **16** / **6** (2 with automatic acquisition, 14 manual guidance) |
| Developer Options profiles / inspected settings | **4** / **13** |
| Authored help topics | **64** |
| Android baselines in the offline matrix | 9 (API 29-37 = Android 10-17, including 12L) |
| Samsung devices with recorded observations | Pre-release builds: 2 phones (API 33 and API 37). 2026-11-14: an API 33 phone, an API 37 phone and an API 36 tablet, with released 0.1.1 and native receiver sessions with each. All from a Windows 11 host |
| Offline tests | 0.1.1: 917 Python tests in 33 modules (all passed on Windows, Python 3.13) and 20 native `#[test]` functions. Development tree: 2034 Python tests in 59 modules collected on 2026-11-14 (a collection count that is still changing; no full-suite result is recorded, see [validation](validation.md)) and 119 native `#[test]` functions. No CI job runs the native tests. Not a pass rate |
| Failure guides (`adapt explain --list`) | **298** in the development tree, including 33 native receiver reason codes and 19 native receiver command outcomes; none in 0.1.1 |

These counts are for the development tree. It adds `adapt explain` and the four `dex receiver-*` commands to 0.1.1, with a help topic each.

### Evidence levels used below

| Label | Meaning |
|---|---|
| **Offline-tested** | Automated tests with mocked `adb`/`ssh`/`tailscale`/archives. No device was involved. |
| **Observed** | Also exercised in bounded sessions on a Samsung device from a Windows 11 host: on pre-release builds, or with the released 0.1.1 code on 2026-11-14 (the label says which). A snapshot, not certification; development-tree changes to an observed command are offline-tested only, except the DeX inspection and `dex receiver-*` runs named in section 15. |
| **Planning** | Prints advice or exact commands; executes nothing. |
| **Experimental** | Firmware-, radio- or Windows-version-dependent; manual steps; no compatibility promise. |

Counts of tests are not a pass rate and are never added to physical observations. Physical evidence covers only Samsung devices driven from a Windows 11 host: two phones (Android 13/API 33 and Android 17/API 37) on pre-release builds, and on 2026-11-14 short passes of 0.1.1 on an Android 13 phone, an Android 17 phone and an Android 16 tablet, all from one Windows 11 PC with one Wi-Fi adapter. The development tree's Python code was run on devices only for the DeX inspection commands during DeX sessions on the Android 17 phone and the Android 16 tablet and for the `dex receiver-*` commands; no run from Linux is recorded (see [validation](validation.md)). Linux is offline-tested only, and macOS is not supported. Every other API level, OEM, host architecture and external AI client is untested. Android 10 is covered by the offline matrix and the legacy USB-first flow only.

---

## 1. Host provisioning

**`adapt setup`** | MCP `setup_status` (read-only) | *Offline-tested*

Downloads Google's official Platform-Tools archive (HTTPS, `dl.google.com` only) into user scope, verifies its digest (explicit `--sha256`, else Google's repository metadata; a mismatch always fails), extracts it with path-traversal and size protections, checks the staged `adb` version (>= 34.0.0), swaps it in atomically and, unless `--no-path`, adds it to the user `PATH`. `--dry-run` previews, `--isolated` uses `~/.android/platform-tools`, `--force` replaces a healthy install. On Linux it needs an x86-64 host, because Google's Linux archive contains x86-64 programs only. *Development tree:* if setup's own install folder already holds a healthy `adb` that is not on `PATH`, setup adds that folder to `PATH` without downloading again; on macOS it refuses before doing anything; an aborted or failed setup exits non-zero; and a setup that succeeded with a usable `adb` exits 0 even when USB-permission or device findings remain (they are still reported, and `--json` carries the doctor's own result in `doctor_status` and `doctor_exit_code`).

Does not: use `sudo`/UAC, touch `ANDROID_HOME` or system paths, install USB drivers or OS packages, or replace another ADB installation. It asks for confirmation when standard input and standard error are both terminals; with neither `--yes` nor a terminal it stops before downloading or changing anything. Fresh-installer acceptance on both host OSes is not established.

```text
adapt setup --dry-run
adapt setup --yes --isolated --no-path
```

## 2. Doctor and driver guidance

**`adapt doctor`** | MCP `doctor` | *Observed* (Windows 11 host with the two Samsung phones) and offline-tested

Locates `adb` (PATH, `ANDROID_HOME`, user-scope directories, `ADAPT_ADB`), checks the version, origin and PATH configuration, inspects the **shared server on port 5037** (owner, root/system ownership, version match), checks Windows PnP USB driver state or Linux `udev`/`plugdev`, and lists transports. Exit codes: 3 unsafe server, 1 missing toolchain, 2 device unauthorized/permissions, 0 ok. `--json` emits a machine report; `--fix` prints remediation text only. The Linux path (`udev`/`plugdev`, distribution-package detection) is offline-tested only. 0.1.1 treats every Linux host as Debian. *Development tree:* doctor reports Debian-family hosts (from `/etc/os-release`) as `debian`, other Linux as `linux` with distribution-neutral advice, and macOS as unsupported; it reports the `adb` that ADAPT's commands use and says when it is not on `PATH`; a distribution-packaged `adb` at or above 34.0.0 is a warning rather than a failure; the Linux port-5037 check scans every listener in `/proc/net/tcp` and `/proc/net/tcp6`; on Windows, a driver problem on a USB device that is not confirmed as Android is a warning rather than a failure; and running as root or Administrator prints a warning.

Does not: repair drivers, kill the daemon, delete adb keys, or run `sudo` by itself. In an interactive terminal a `Next action` menu follows; its `udev` choice runs the udev-rule sudo commands and its `setup` choice runs setup, **only if you pick them**. Opening Device Manager on Windows is for manual driver work. In 0.1.1 the `udev` choice reads `packaging/linux/51-android.rules` from a source checkout, and the 0.1.1 wheel does not contain that file. *Development tree:* the wheel includes the rules file and doctor's advice names its installed path.

## 3. ADB and wireless transports

**`adapt devices`, `adapt adb`, `adapt pair`, `adapt connect`** | MCP `list_devices`, `device_info` | *Observed* (USB, simultaneous USB and Wi-Fi, private routed wireless) and offline-tested

- `devices` runs `adb devices -l` behind the shared-server preflight.
- `adb` passes one command to the resolved adb after a safety check. Refused: `kill-server`, `sudo`, `uninstall`, `pm clear`, bare `shell`, unbounded `logcat`, and shell payloads ADAPT cannot inspect (unquoted `; | & < > $`, nested shells). Shell commands time out after 60 s. With exactly one ready device and no `-s`, it pins that device; with several you must pass `-s`. (The MCP tools that act on the phone do not pick a device this way in the development tree; see section 19.) Anything not refused runs as typed with no snapshot, including `adb root`, `adb reboot bootloader` and `adb sideload`. It is a guarded command runner, not a general shell.
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

**`adapt help-me`, `adapt explain` (development tree), `adapt tips`, `adapt guide`** | *Offline-tested*

`help-me` explains workflows and, with `--all`, lists every command and option of the installed build. There are **66 authored topics** (59 in 0.1.1) with a generic example and a boundary note, covering command groups, most commands and four workflow topics (`wireless`, `device_manager`, `udev`, `recheck`). `tips on|off|status` stores a per-user preference; `--no-tips` and `ADAPT_TIPS=0` override it per run. Tips never appear for `--json`, MCP or non-terminal output. `guide` prints short official-tooling notes for `testing`, `rom`, `ai`, `wireless`. *Development tree:* when a command fails in a terminal, ADAPT adds a short `Why:`/`Try:`/`More:` block after its error line (one `More: adapt explain ID` line when output is redirected); `adapt explain ID|KEYWORD|TEXT` shows the full guide offline and `adapt explain --list` lists every known failure; JSON and MCP errors carry the same guidance as a `failure` object; and an unexpected error prints a short report instead of a Python traceback (`ADAPT_DEBUG=1` shows it). Errors and refused or failed results get a specific guide where ADAPT tagged them (297 guides) and the generic `internal.unclassified` guide otherwise. The `dex receiver-*` commands use the same guidance: their refusals and failures name a `dexpc.native_receiver.*` guide, or the `dexpc.receiver.CODE` guide of a reason code the receiver recorded, and `receiver-status` also keeps explaining that code in its own `last_failure` block. The guidance has offline tests only and has not been seen on a real failure ([help](help.md#failure-guidance)).

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

**`adapt android versions|capabilities`** | MCP `android_versions`, `android_capabilities` | *Offline-tested* matrix; *Observed* on API 33 and 37 (pre-release builds) and on API 33, 36 and 37 with 0.1.1

| API | Android | | API | Android | | API | Android |
|---|---|---|---|---|---|---|---|
| 29 | 10 | | 32 | 12L | | 35 | 15 |
| 30 | 11 | | 33 | 13 | | 36 | 16 |
| 31 | 12 | | 34 | 14 | | 37 | 17 |

`versions` is offline. `capabilities` reads identity properties from one exact transport and rates 11 capabilities (USB ADB, pairing-based wireless ADB from API 30, ADB Wi-Fi 2.0 at API 37, legacy USB-first TCP, screenshot, UI Automator, scrcpy audio from API 30, existing-display mirroring, new virtual display from API 29, input routing, developer-option keys). The result is **version eligibility, not OEM certification**. ADB Wi-Fi 2.0 additionally needs adb 37.0.0+ and runtime mDNS evidence.

## 10. Root, ROM and bootloader detection

**`adapt detect scan|inspect`** | *Offline-tested*; `inspect` *observed* with 0.1.1 on three Samsung devices (2026-11-14)

`scan` lists ADB and fastboot transports independently. `inspect` reads `getprop`, `id -u` and `command -v su` over ADB (or `fastboot getvar` over fastboot) and reports root indicators, custom-ROM property markers (Lineage, crDroid) and bootloader lock evidence from three Android-reported properties. Everything is a **heuristic**: a missing `su` does not prove a device is unrooted, `test-keys` or an unlocked bootloader does not prove a custom ROM, and self-reported properties are not attestation. **Root and ROM indicators are not proof.** Samsung Download/Odin mode is not fastboot and is not detected. It never reboots, unlocks, flashes, runs `su` or requests `adb root`; there is no automatic unlock, flash or ROM build anywhere in ADAPT.

## 11. Recoverable developer profiles

**`adapt dev-options inspect|apply|restore`** | MCP `developer_options_inspect` | *Observed*: inspection on pre-release builds (the stay-awake settings on both phones) and with 0.1.1 on three devices; one `apply --profile touches` and `restore` with 0.1.1 on an Android 13 phone (`show_touches` 0, then 1, then 0). Other profiles, rollback and conflicts are offline-tested only

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

**`adapt logs capture|diagnostic|guide`** | MCP `app_logs`, `diagnostic_logs`, `logging_guide`, `logcat_tail` | *Observed* (DeX system-log capture on the API 33 phone only); app log capture and tag filtering are offline-tested

- `capture`: finite `logcat -d` for the package's **UID** on `main`/`crash`, up to 500 lines and 128 KiB, never clearing buffers, no device-wide fallback.
- `diagnostic`: recent lines for fixed tags (`window`, `dex`, `print`) on the device clock, 1-10 minutes; never enables verbose producers.
- `guide`: offline notes separating Samsung verbose switches (manual), vendor logging, window, DeX, print, Wi-Fi and app logging.

Exports (`--output`) create new 0600 files and never overwrite. Logs may contain private data; an empty capture does not prove the absence of a crash and is not a test pass.

## 14. Generic desktop app testing

**`adapt desktop inspect|plan|enable|start`** | *Offline-tested*; `inspect` *observed* with 0.1.1 on three Samsung devices (2026-11-14)

`inspect` observes freeform/secondary-display features, two desktop settings and displays. `plan` previews `scrcpy --new-display=SIZE/DPI --start-app=PACKAGE --no-audio --no-vd-destroy-content`. `enable` applies the `desktop-testing` profile with a snapshot. `start` (needs `--yes`, API 29-37, scrcpy 3.1+) launches one scrcpy process for a separate app display and leaves existing mirrors alone. A virtual display tests multi-window behavior; it **does not prove Samsung DeX** or desktop window decorations.

## 15. Samsung DeX and the PC bridge

**`adapt dex ...` (16 commands)** | MCP `dex_inspect`, `dex_displays` | *Experimental*; *Observed* on pre-release builds on the API 33 phone (display inspection, dual mirroring and a receiver-started session; not ADAPT's own activation) and with 0.1.1 on three devices (`inspect`, `list-displays`, `pc-probe`); development-tree `inspect` confirmed DeX from display flags on an Android 17 phone and an Android 16 tablet, one wireless session each

| Command | Role |
|---|---|
| `inspect`, `list-displays` | Read DeX/desktop-mode evidence and logical displays. Display IDs are runtime values |
| `receiver-plan`, `receiver-status`, `receiver-start`, `receiver-stop` | *Development tree.* Explain both PC receiver routes; read, start and gracefully stop a native receiver you built (see below) |
| `plan-dual-mirror`, `mirror` | Plan or launch two scrcpy windows (display 0 and a confirmed DeX display); API 33-37, scrcpy 2.0+ |
| `pc-plan`, `pc-receiver-plan` | Offline previews (phone-side activation; Microsoft receiver setup) |
| `pc-probe`, `pc-attach` | Run a temporary locally built helper to observe Samsung state; attach a scrcpy window to a positively identified, externally started DeX desktop |
| `pc-start`, `pc-status`, `pc-stop`, `pc-restore` | Trial activation (`loopback` or `overlay`) with an owned supervisor, snapshot, stop and recovery |

DeX state detection: `inspect` reads `dumpsys desktopmode`. With 0.1.1 it reported `unknown` on an Android 13 phone while wireless DeX was active, because that phone prints its state in a form the 0.1.1 parser does not recognise. *Development tree:* that form is recognised for active dual-mode and idle DeX (tested offline against that phone's output, not re-run on the phone; standalone and wired DeX were not captured), and a device that answers no query is reported as unreachable instead of non-Samsung. One UI 8 and 9 (an Android 16 tablet and an Android 17 phone) have no `desktopmode` service, so with 0.1.1 `inspect` reports `unknown` there and `plan-dual-mirror` and `mirror` cannot confirm a DeX display. *Development tree:* when that service is absent on a Samsung device, `inspect` confirms DeX only when exactly one external display carries both `FLAG_WIRELESS_DEX_DISPLAY` and `FLAG_EXTERNAL_DEX_HOSTING` ([DeX](dex.md#display-flag-evidence-service-absent)). On 2026-11-14 that confirmed DeX on the Android 17 phone and the Android 16 tablet, each during one wireless DeX session to the native receiver, and on the phone `plan-dual-mirror` produced a plan for the flagged display; nothing was launched, so scrcpy capturing it is not observed, and wired and standalone DeX were not observed. `pc-probe` refused with "unrecognized state" on the Android 17 phone during an active wireless DeX session (with 0.1.1 and with the development tree), and with 0.1.1 its output includes display unique IDs, which can contain a hardware address; in the development tree MAC-style addresses are withheld from the report (offline-tested only).

Boundaries: Samsung API 33-37 only for activation (a researched range; only an API 33 phone was tried); an external or virtual display alone is not DeX; ADAPT's own activation has never been observed succeeding (on the API 33 phone firmware denied `loopback` with the vendor permission `CONFIGURE_WIFI_DISPLAY`, and `overlay` did not start DeX); no silent fallback between methods; the helper is a shell-user `app_process` JAR compiled from `AdaptDexBridge.java`, not an APK, root or a Samsung binary. **Samsung Wireless DeX needs firmware permission**; when shell activation is denied the supported alternative is a receiver on the PC.

The **native Windows wireless receiver** source is included under `native/windows-receiver` (upstream MIT DeXDesk revision `5070ccda5c363f5ed0b46d1fe7cf2c15c47fc125`). It is a manual-build, experimental component outside the Python wheel; ADAPT never builds, bundles or downloads it. See its [README](../native/windows-receiver/README.md) and [DeX for PC](dex-pc.md). Admission is open by default: any phone that asks to connect is admitted, and `ADAPT_DEX_EXPECTED_NAME` and `ADAPT_DEX_EXPECTED_ID` are optional exact-match restrictions that are not authentication. It targets x64 Windows. *Development tree:* it writes a documented `status.json` (stages, failure reason codes, no phone name, ID or address), withholds the phone's details from its log unless `ADAPT_DEX_LOG_PEER_DETAILS` is set, and has experimental audio that is off by default; `receiver-start --exe PATH --yes` starts a copy you built (refusing without `--yes`, while another receiver runs or when TCP 7236 is taken), `receiver-status` reads its status file, and `receiver-stop --yes` asks it to stop through `stop.request` and never ends a process by ID. 0.1.1 has none of this and is not launched by any 0.1.1 command.

Recorded observations, all on one Windows 11 PC: an earlier revision formed a group, decoded desktop frames and passed one controlled reconnect with the API 33 phone, and an earlier build held one session for about a day and a half; on 2026-11-14 the 0.1.1 source held a session of about four minutes with an Android 13 phone, and the development-tree source with audio on and admission restricted by name held a session of about 90 seconds with an Android 13 phone, one of about 48 minutes with an Android 17 phone and a short one with an Android 16 tablet; window captures showed the desktop drawn correctly with the two phones (none was taken with the tablet), and the audio output opened each time. On the Android 17 phone the owner confirmed that real mouse and keyboard input worked and that the phone's sound was heard from the PC, and judged the audio quality great (one listener, one session). Nobody used a real mouse or listened with the other two devices; with the Android 13 phone scripted clicks reached the phone as touches, but a click on a desktop icon did not open the app. Reconnects on the current source, sessions over about 48 minutes, open admission with a device connecting, a release build and the failure reason codes are not observed. The `receiver-*` commands were run against the real receiver with no phone, and `receiver-status` and `receiver-stop` also with the Android 17 phone and the Android 16 tablet streaming. Offline tests: 20 `#[test]` functions at 0.1.1 (a passing run is recorded in [publication checks](publication-checks.json), 2026-11-13) and 119 in the development tree ([validation](validation.md) says what is recorded about running them); no CI job runs them, and they do not start the receiver or involve a phone. There is no compatibility promise across radios, Windows versions or Android builds. MCP input tools have **no display targeting**.

## 16. SSH workers and hypervisor guidance

**`adapt ssh status|plan|run|mcp-config`** | *Offline-tested* (command construction, quoting, option enforcement); no live SSH acceptance

Runs one allowlisted, read-only ADAPT operation (`doctor`, `scan`, `inspect`, `android`, `desktop`, `dex`, `tools`) on an explicitly chosen worker through OpenSSH with `BatchMode`, strict host-key checking, no forwarding, no agent and no ControlMaster, and returns one JSON object (max 4 MiB). *Development tree:* a failed run reports a fixed reason code (for example an SSH authentication or host-key problem, a missing remote `adapt`, or a reply that is not one ADAPT JSON object) with a hint, and never copies ssh's own output into the report. `mcp-config` prints an observation-only MCP-over-SSH entry. The worker owns USB/ADB; SSH alone gives no device or display access. Hypervisor guidance (Proxmox/KVM, VMware, Hyper-V, VirtualBox, XCP-ng) is architectural only: there are no hypervisor adapters and no VM/USB-assignment management. See [SSH workers](ssh-workers.md) and [hypervisors](hypervisors.md).

## 17. Tailscale mode

**`adapt tailscale ...` (13 commands)** | *Observed* in part (local peer parsing, and one pairing success recorded on the API 37 phone; **no complete direct Tailscale run**) and offline-tested

| Command | Role |
|---|---|
| `status`, `peers` | Local daemon peers (identical), no peer contact |
| `guide`, `plan` | Offline direct-phone, USB-worker and subnet-router guidance; command plan |
| `probe` | One TCP handshake to one selected peer port |
| `pair`, `connect`, `check`, `adb`, `disconnect` | Pair (code never in argv), connect, verify identity and TLS port, run one guarded command, disconnect that endpoint only |
| `worker-plan`, `worker-run`, `worker-mcp-config` | `ssh` worker operations against a tailnet host |

Binds to one unique online peer's numeric tailnet address; no DNS fallback, scanning or second phone. **Tailscale only transports IP traffic**: it does not authorize ADB, install or sign in, edit ACLs, routes or exit nodes, or provide Miracast. **A direct Tailscale end-to-end run is unproven**: the strict TLS verifier requires `service.adb.tls.port` to equal the connect port, and that property was empty on the tested Samsung firmware. Separately, a private routed VPN/hotspot path (not Tailscale) carried authenticated ADB commands to one phone in pre-release testing; that is not Tailscale evidence.

## 18. Rapid ADAPT

**`adapt rapid plan`** | *Planning*

Prints the **check -> prepare -> reproduce -> capture -> restore** loop as exact commands (capability and option inspection, optional app validation, `stay-awake` and optional `fast-ui` with their snapshots, one deliberate journey, focused log capture, explicit restores). It is **rapid integrated planning, not an auto-runner**: it executes nothing, creates no files, and does not supervise or roll back a session.

## 19. AI and MCP access

**`adapt mcp`, `adapt agent primer` (development tree)** | *Offline-tested*

A local stdio MCP server with **24 tools: 19 observation and 5 mutating** (`tap`, `swipe`, `key`, `type`, `install_apk`). `--read-only` hides the five; `--device` pins exact transports and removes `doctor`, so `--read-only --device` exposes **18** tools. Strict schemas, bounded outputs (logs <= 500 lines, screenshots <= 1280 px JPEG), generic error objects. No network service. *Development tree:* the five tools that act on the phone never choose a device; a call must name `device` unless the server was started with exactly one `--device`. Without `--device`, the read-only `device_info`, `screenshot`, `ui_summary` and `logcat_tail` use the only ready device when none is named; the other device tools always need `device`. Error results carry a `failure` object, and new error codes cover a refused adb command (`adb_command_refused`) and an unverified shared server (`unsafe_shared_server`). In 0.1.1 the five mutating tools also use the only ready device. **No external AI client has been live-accepted** and MCP-over-SSH is untested. With 0.1.1, `--read-only --device` sessions driven by a scripted client against three Samsung devices (2026-11-14) listed 18 tools, returned `list_devices`, `device_info` and `screenshot`, and refused `tap`; `list_devices` labelled the two wireless-debugging transports `usb`. *Development tree:* `list_devices` reads the label from the serial (`tcp` for a host:port endpoint or a wireless-debugging service name, `local` for an emulator, otherwise `usb`); that, the explicit-device rule and the new error codes have offline tests only. No tool that acts on the phone has been run against a device. *Development tree, agent orientation:* the server sends short `instructions` (under 2,000 characters, shaped by the session's `--read-only` and `--device` policy) and advertises a human `title` and the standard annotations on every tool: observation tools are `readOnlyHint`; all five tools that act on the phone are marked destructive (input can confirm an action in any app, and `install_apk` can replace an installed app); `openWorldHint` is true for the screen, UI, log and input tools, whose content ADAPT does not control. For any agent that has a shell, `adapt agent primer` prints a Markdown primer (`--json` for the structured form with the full command and MCP tool catalog): what ADAPT is, first steps, the development loop, safety rules written for an agent, `--json` and exit-code conventions, and how to start and register `adapt mcp`. The command and tool lists are read from the live code and every quoted command is checked against the command tree; all of this is offline-tested only, and **no AI client has been observed reading the instructions or the primer**. MCP resources and prompts are not implemented. Full detail: [MCP access](mcp-access.md).

---

## Safety invariants

- No `adb kill-server`, no `sudo adb`; `uninstall` and `pm clear` are refused by the guard. Other destructive commands typed into `adapt adb` are not.
- Shared ADB server on 5037 is verified before use; unsafe servers abort commands with exit 3. The Linux check is offline-tested only.
- No discovery by scanning networks or hosts; Tailscale peers, SSH workers and wireless endpoints are named exactly. For local ADB, `adapt adb` and the read-only MCP tools use the only ready device when none is named and refuse when several are ready; in the development tree the MCP tools that act on the phone need a named device (or a one-target `--device` session); `--device` pins an MCP session.
- Writes are allowlisted, snapshotted, read back and restored only when still ours.
- No automatic unlock, flash, root or ROM build.
- Secrets (pairing codes) never appear in argv for Tailscale mode and are not stored or returned.
- Logs, screenshots and snapshots may contain private data and are not redacted automatically.
