<p align="center">
  <img src="docs/assets/adapt-title.gif" alt="ADAPT - Android Device Agent Provisioning Toolkit" width="720">
</p>

<h3 align="center">Android Device Agent Provisioning Toolkit</h3>

<p align="center">
  Set up adb, inspect Android devices and prepare manual test sessions from the terminal or from an AI agent.<br>
  Device settings that ADAPT changes are snapshotted first and can be restored.
</p>

<p align="center">
  <b>Alpha</b> &middot; MIT &middot; Python 3.11+ &middot; Windows 11 &middot; Linux x86-64 (offline tests only) &middot; Android 10-17 planning matrix, observed on Android 13, 16 and 17
</p>

---

ADAPT wraps Google's official Platform-Tools (`adb`) in a command-line toolkit and a local stdio [MCP](https://modelcontextprotocol.io) server. It installs `adb` in user scope, checks your host and the shared ADB server, connects over USB, Wi-Fi and routed networks such as Tailscale, inspects device capabilities, and gives developers Developer Options test profiles, bounded log capture, app checks and Samsung DeX display tooling. CLI device workflows require `-s SERIAL`. The five MCP tools that act on the phone need an explicit `device` unless the server was started with exactly one `--device`; the read-only MCP tools and the raw `adapt adb` runner use the only ready device when no target is given ([For AI agents](#for-ai-agents)). Developer Options profiles, desktop mode and the DeX overlay write a snapshot before changing settings and can restore from it; host changes (PATH, udev rules, the scrcpy install) and MCP input or app installs are not undone by ADAPT. Capability, detection, DeX, development and diagnostics reports include a `claim_boundary` or `note` field saying what was not established. The raw `adapt adb` passthrough refuses a short list of commands ([Status](#status-and-honest-limits)) and runs everything else as typed, with no recovery snapshot.

![ADAPT overview: command groups, transports, MCP policy and safety guards](docs/assets/showcase-overview.png)

<sub>The [offline showcase](docs/showcase/index.html) (open the HTML file in a browser after cloning) uses generated, source-derived illustrations and one capture of real offline CLI output. It contains no Android screenshots and is not test evidence.</sub>

## Why ADAPT

- **Shared ADB server guard.** ADAPT never runs `adb kill-server` or `sudo adb`. Before it contacts the ADB server it checks that the server on port 5037 is owned by your user (not root or SYSTEM) and matches its adb version, and refuses otherwise. The `adapt adb` passthrough refuses package uninstall and app-data clear. (Doctor's optional Linux udev step runs `sudo`, only when you select it.)
- **Explicit targets.** CLI device workflows require a serial. An MCP session started with `--device` accepts only the listed transports. The MCP tools that act on the phone (`tap`, `swipe`, `key`, `type`, `install_apk`) never pick a device: a call must name one, unless the session was started with exactly one `--device`. Read-only MCP tools and the guarded `adb` runner use the only ready device and refuse when several are connected.
- **Recoverable profiles.** Developer Options profiles write an identity-bound snapshot first, read each value back, and restore only values that still hold ADAPT's setting. One apply and restore of the `touches` profile that changed a value was recorded on one Android 13 phone with 0.1.1; conflicts, rollback and partial failures are covered by offline tests only.
- **For agents.** A local MCP server with 24 tools (19 observation, 5 mutating), `--read-only` and `--device` restrictions and strict input schemas. Most commands also offer `--json`.
- **Failure guidance** (unreleased, not in 0.1.1). When a command fails in a terminal, ADAPT prints a short Why / Try / More block after the error; redirected output gets one `More: adapt explain ID` line. Errors and refused or failed results get a specific guide where ADAPT tagged them and a generic one otherwise; the exception is the `dex receiver-*` commands, whose refusals print a reason only and whose `receiver-status` explains a receiver reason code in its own report. The guidance has offline tests only and has not yet been seen on a real failure. `adapt explain` looks up any failure offline, `--json` output and MCP errors carry the same guidance as a `failure` object, and an unexpected error prints a short report instead of a Python traceback (`ADAPT_DEBUG=1` shows the traceback).
- **Labelled evidence.** Root, ROM and bootloader results are reported as indicators, and Android version checks as eligibility, not proof.
- **Coverage.** Wired and wireless ADB, Tailscale and SSH workers, scrcpy, virtual displays, Samsung DeX, logs and app/debug checks. How much each area has been exercised varies; see [Status](#status-and-honest-limits).

## At a glance

Counts are for this repository's development tree (unreleased changes), which is ahead of the 0.1.1 download: 0.1.1 has 64 visible leaf commands and 59 help topics, without `adapt explain` and the four `dex receiver-*` commands.

| | |
|---|---|
| Functional areas | **19** |
| CLI | **69** visible leaf commands in **13** groups (root + 12), plus 2 hidden aliases |
| MCP | **24** tools: 19 observation + 5 mutating; `--read-only --device` exposes 18 (12 device-bound, `list_devices` filtered to the allowed targets, 5 host-level) |
| Tool catalog | **16** tools, **6** goals; 2 installed by ADAPT with checksum checks (Platform-Tools, scrcpy), 14 manual guidance |
| Developer Options | **4** profiles, **13** inspected settings |
| Built-in help | **64** authored topics, `adapt explain` for failures, a home console with 9 areas |
| Android matrix | 9 baselines, API 29-37 (Android 10-17 including 12L) |
| Physical evidence | Samsung only: **2** phones (API 33 and API 37) on pre-release builds; on 2026-11-14, short runs of 0.1.1 and native receiver sessions with an API 33 phone, an API 37 phone and an API 36 tablet, all from one Windows 11 PC. Details in [Status](#status-and-honest-limits) |

## Download and install

**[Download ADAPT 0.1.1 (alpha)](https://github.com/primary-null/ADAPT/releases/tag/v0.1.1)**

The commands below install 0.1.1. Parts of this README describe unreleased changes in this repository that 0.1.1 does not have; [Status](#status-and-honest-limits) lists them. To try them, install from a checkout.

ADAPT is a terminal application for Windows and Linux; macOS is not supported. Install **Python 3.11 or newer** and
[pipx](https://pipx.pypa.io/stable/how-to/install-pipx.html), then run:

```text
pipx install "https://github.com/primary-null/ADAPT/releases/download/v0.1.1/adapt_adb-0.1.1-py3-none-any.whl"
pipx ensurepath
adapt --help
```

Open a new terminal after `pipx ensurepath` if `adapt` is not found. Installing the wheel needs no Git checkout
and no administrator rights. ADAPT is not published to PyPI; use the GitHub URL above rather than
`pipx install adapt-adb`. The same wheel installs on Windows and Linux and includes the Android DeX bridge
resource; the experimental native Windows receiver remains a separate manual-build project. USB drivers (Windows)
and udev rules (Linux) are separate steps that may need administrator rights; ADAPT does not install drivers. The
0.1.1 wheel does not include `51-android.rules`, so with 0.1.1 doctor's udev shortcut needs
`packaging/linux/51-android.rules` from a source checkout; in the development tree the file ships inside the wheel and
doctor prints its installed path.

For the optional MCP server, use this instead of the first install command:

```text
pipx install "adapt-adb[mcp] @ https://github.com/primary-null/ADAPT/releases/download/v0.1.1/adapt_adb-0.1.1-py3-none-any.whl"
```

You can also download the `.whl` from the release and install the local file with
`pipx install ./adapt_adb-0.1.1-py3-none-any.whl`. Verify it against the release's `SHA256SUMS.txt`
(`Get-FileHash` on Windows or `sha256sum` on Linux). GitHub build provenance attestations are provided.
The `.tar.gz` is a source distribution for developers, not a desktop installer.

**Updates:** use the new release's wheel URL with `pipx install --force URL`, retaining `[mcp]` if used.
These URLs pin an exact version; `pipx upgrade` does not discover a different GitHub release automatically.
Uninstall with `pipx uninstall adapt-adb`; this leaves Platform-Tools installed by `adapt setup`, its PATH entry,
a scrcpy installed by ADAPT, udev rules and snapshot files in place. The package does not certify devices or
bundle an Android SDK. See [Release process](docs/releasing.md) for checks, compatibility and verification.

### Install from a checkout (contributors)

ADAPT needs **Python 3.11 or newer**. Editable installation is for developing ADAPT itself.

```text
git clone https://github.com/primary-null/ADAPT.git
cd ADAPT
python -m venv .venv
# PowerShell: .\.venv\Scripts\Activate.ps1      bash/zsh: source .venv/bin/activate
python -m pip install -e .            # CLI only
python -m pip install -e ".[mcp]"     # CLI + MCP server (adds the MCP SDK and Pillow)
adapt --help                          # or: python -m adapt_adb --help
```

## Quick start

```text
adapt doctor                  # host, adb, port 5037 safety, drivers/udev, devices
adapt setup --dry-run         # preview the user-scope Platform-Tools install
adapt setup                   # install it (asks first; outside a terminal, pass --yes)
adapt devices                 # list transports
```

Wireless debugging (Android 11+): pair and connect use **two different ports**. Android 10 needs the USB-first
`adb tcpip` flow, which ADAPT does not run for you.

```text
adapt pair 192.0.2.10:PAIR_PORT          # prompts for the pairing code
adapt connect 192.0.2.10:CONNECT_PORT
```

Let `adapt pair` prompt for the code: a code typed as a second argument is visible in the process list.

Inspect a device you name explicitly (`SERIAL` is the identifier from `adapt devices`):

```text
adapt android capabilities -s SERIAL --json
adapt dev-options inspect -s SERIAL
adapt app validate -s SERIAL --package com.example.app --expect-debuggable false
adapt logs capture -s SERIAL --package com.example.app --lines 200 --output app-logs.json
```

Try ADAPT without a device: `adapt android versions`, `adapt help-me --all`, `adapt tools list`, `adapt rapid plan -s SERIAL --package com.example.app`, and in the development tree `adapt explain --list`.

Run bare `adapt` in a terminal for the home console (nine areas, `H` Home, `B` Back).

## What it does

| Area | Commands | Highlights |
|---|---|---|
| Host provisioning and doctor | `setup`, `doctor`, `everything` | Google download checked against the digest in Google's SDK repository metadata, guarded extraction, 5037 preflight, driver/udev guidance |
| ADB and wireless | `devices`, `adb`, `pair`, `connect` | Guarded runner that refuses ambiguous targets, separate pair/connect |
| Tailscale and SSH workers | `tailscale` (13), `ssh` (4) | One selected tailnet peer, pairing code never passed as an argument, strict OpenSSH options, SSH failures reported with a reason code and hint; no complete live run of either |
| Capabilities and detection | `android`, `detect` | Android 10-17 matrix, root/ROM/bootloader *indicators* |
| Developer testing | `dev-options`, `app`, `debug`, `logs`, `rapid` | 4 snapshot-backed profiles, bounded UID logs, app validation, plan-only Rapid loop |
| Displays and DeX | `desktop`, `dex` (16) | scrcpy virtual displays, phone and DeX mirrors of an active DeX session, experimental DeX activation (never recorded succeeding), start/status/stop for a native receiver you build yourself (unreleased) |
| Tools | `tools`, `guide` | 16-tool catalog, scrcpy installer pinned to v5.0.1 with SHA-256 checks |
| AI and help | `mcp`, `help-me`, `explain`, `tips`, `console` | 24 MCP tools, built-in help topics, offline failure lookup (`explain`, unreleased), home console |

Details for every area: [Features](docs/features.md). Every flag of every command: [Command reference](docs/command-reference.md).

## For AI agents

```json
{
  "mcpServers": {
    "adapt": { "command": "adapt", "args": ["mcp", "--read-only", "--device", "SERIAL"] }
  }
}
```

`adapt mcp` is a local stdio server (no network listener). `--read-only` keeps the 19 observation tools; `--device` restricts calls to the listed transports and drops host-wide `doctor`; together they expose 18 tools. Pass `--device SERIAL`; the [example configurations](examples/README.md) already do.

- **Tools that act on the phone** (`tap`, `swipe`, `key`, `type`, `install_apk`) never choose a device. A call without `device` fails with `need_device`, even when only one phone is connected, unless the server was started with exactly one `--device`; that target is then the default.
- **Read-only tools.** Without `--device`, `device_info`, `screenshot`, `ui_summary` and `logcat_tail` use the only device in state `device` when no `device` is named, and fail with `need_device` when several are connected; the other device tools always need `device`. With `--device`, read-only device tools must name one of the listed targets.
- **0.1.1 differs:** there, without `--device`, the five tools that act on the phone also use the only ready device, errors carry no `failure` object, and the server reports its version as 0.1.0.

Read-only still lets a model see screens and logs, so treat it as sensitive. Evidence: on 2026-11-14, `--read-only --device` sessions of 0.1.1 driven by a scripted client against an Android 13 phone, an Android 17 phone and an Android 16 tablet each listed 18 tools, returned `list_devices`, `device_info` and a screenshot, and refused `tap`. With 0.1.1, `list_devices` labelled the two wireless-debugging transports `usb`; the development tree reads the label from the serial (`tcp` for a host:port endpoint or a wireless-debugging service name), which has offline tests only. No external AI client has been live-accepted, no tool that acts on the phone has been run against a phone, and the unreleased MCP changes (the explicit-device rule, the transport label, the `failure` object and the new error codes) have offline tests only. See [MCP access](docs/mcp-access.md).

## How it fits together

```text
 terminal ----> adapt CLI ---+                         +--> USB ----------> Android
                              +--> argument checks --> adb --> Wi-Fi ------> device(s)
 AI client --stdio--> adapt mcp --+   safety guard       +--> routed (Tailscale / VPN)
                       (--read-only, --device)   5037 preflight
 SSH worker:  adapt ssh run HOST --operation OP   -->  remote adapt (read-only) --> its own adb
```

SSH workers and routed paths are implemented and offline-tested; [Status](#status-and-honest-limits) lists what has been observed. More topologies, including routed ADB over Tailscale, SSH workers and the DeX display paths: [Architecture](docs/architecture.md).

## Status and honest limits

ADAPT 0.1.1 is **alpha**: command names, JSON fields and exit codes may change between releases, and no device, OEM, Android version or host system is certified.

**This repository is ahead of the 0.1.1 download.** Its development tree contains these unreleased changes, which this README describes and 0.1.1 does not have: `adapt explain` and failure guidance after errors; the MCP tools that act on the phone requiring an explicit `device`; MCP `list_devices` reading the transport label from the serial; doctor and setup fixes (Debian-family, other Linux and macOS told apart, the udev rules file shipped in the wheel, a report of the adb ADAPT's commands use, setup registering its own earlier install on PATH, a non-zero exit when setup is aborted or fails, a scan of every Linux port-5037 listener, an x86-64 check before setup downloads on Linux, a warning when run as root or Administrator); `dex inspect` recognising active and idle DeX in the form one Android 13 phone prints, confirming DeX from display flags when the `desktopmode` service is absent (One UI 8 and 9), and reporting an unreachable device as an error; `dex pc-probe` withholding MAC-style hardware addresses; reason codes for SSH worker failures; and, for the native receiver, a status file, privacy defaults, experimental audio and the `adapt dex receiver-*` commands. Most of these have offline tests only; the table below says what was observed.

**Evidence comes in three kinds, which are never added together.**

- **Offline tests.** The test suite mocks `adb`, `ssh`, `tailscale` and downloaded archives; no phone is involved. The [release workflow](.github/workflows/release.yml) runs it on Ubuntu with Python 3.11 and installs the wheel on Windows and Ubuntu. In the development tree the install matrix lists Python 3.11, 3.12 and 3.13; the workflow file in 0.1.1 lists 3.11 and 3.13 only, and no run of the updated workflow is recorded. These tests show that the code handles simulated device responses as designed, not that a real device responds that way.
- **Physical observations.** Two Samsung phones, Android 13 (API 33) and Android 17 (API 37), observed on pre-release builds before the first public release. On 2026-11-14 the released 0.1.1 code was run from one Windows 11 PC against three Samsung devices: an Android 13 phone over USB, and an Android 17 phone and an Android 16 tablet over wireless debugging. The native receiver was run on the same PC with all three devices. Every smoke pass used 0.1.1; the development tree's Python code was run on devices only for `dex inspect`, `dex list-displays`, `dex plan-dual-mirror` and `dex pc-probe` during DeX sessions and for the `adapt dex receiver-*` commands. No session from a Linux host is recorded.
- **Planning.** Commands that print a plan and run nothing: `rapid plan`, `tools plan`, `debug plan`, `desktop plan`, `ssh plan`, `tailscale plan`, `tailscale worker-plan`, `dex pc-plan`, `dex pc-receiver-plan`, `dex receiver-plan` and `dex plan-dual-mirror`.

Recorded physical observations:

| Area | What the record says |
|---|---|
| USB and Wi-Fi ADB, host preflight, inspection of a selected device | Exercised in bounded sessions on pre-release builds. With 0.1.1: on the Android 13 phone (USB) `devices`, `doctor --json`, `android capabilities`, `dev-options inspect`, `detect inspect`, `desktop inspect`, `dex list-displays` and `dex pc-probe` returned results; on the Android 17 phone and the Android 16 tablet (wireless debugging) the inspection commands did too |
| Developer Options | Pre-release builds: the stay-awake settings were read on both phones. 0.1.1: `apply --profile touches` and `restore` on the Android 13 phone; `show_touches` read back as `1`, then `0`. On the other two devices the value was already `1`, so the cycle changed nothing |
| MCP | 0.1.1, `--read-only --device`, scripted client, all three devices: 18 tools listed; `list_devices`, `device_info` and `screenshot` returned results; `tap` was refused. No external AI client; no tool that acts on the phone |
| `dex inspect` | With 0.1.1 it reported `unknown` on the Android 13 phone while a wireless DeX session was active, because that phone prints its DeX state in a form the 0.1.1 parser does not recognise; the development tree parses that form (tested offline against that phone's output, not re-run on the phone). The Android 17 phone (One UI 9) and the Android 16 tablet (One UI 8) have no `desktopmode` service, so 0.1.1 reports `unknown` there. The development tree then reads display flags: during one wireless DeX session on each of these two devices it confirmed DeX on the receiving display, and on the phone `dex plan-dual-mirror` produced a plan for that display (nothing was launched, so scrcpy capturing it is not observed). Wired and standalone DeX were not observed. `dex pc-probe` refused during an active DeX session on the Android 17 phone, with 0.1.1 and with the development tree |
| Routed ADB | A private VPN/hotspot route (not Tailscale) returned authenticated ADB commands and a retained connection recheck to one phone |
| Direct Tailscale ADB | Not completed: the strict check needs `service.adb.tls.port`, which was empty on the tested firmware |
| DeX activation (`dex pc-start`) | Not observed succeeding: on the API 33 phone, firmware denied the loopback method and the overlay method did not start DeX |
| Native Windows receiver | Described under [Native Windows receiver](#native-windows-receiver-experimental-source): an earlier revision, the 0.1.1 source and the development-tree source each held a session with the Android 13 phone, and the development-tree source also with the Android 17 phone and the Android 16 tablet. Real mouse, keyboard and sound were confirmed by a person on the Android 17 phone only |
| `adapt dex receiver-*` (unreleased) | Against the real receiver on Windows 11 with no phone: `receiver-start` refused without `--yes` and refused a second start, and `receiver-status` and `receiver-stop` reported and stopped it. With devices: `receiver-status` reported the Android 17 phone and the Android 16 tablet streaming, `receiver-stop` ended both sessions, and `receiver-start` with `--expect-name` and `--audio` started the tablet session |

No physical observation is recorded for `setup`, `tools install scrcpy`, `app`, `debug`, `everything` or `ssh`; their evidence is offline tests. Nothing recorded shows these working: DeX activation by ADAPT, a direct Tailscale ADB connection, an MCP tool that acts on the phone, any external AI client, SSH workers or MCP over SSH, any device session from Linux, wired or standalone DeX detection, failure guidance on a real failure, or any Android version other than 13, 16 and 17.

**Host platforms**

- **Linux.** In the development tree, doctor tells Debian-family hosts (from `/etc/os-release`) from other Linux and prescribes `apt` and `plugdev` only on Debian-family hosts; 0.1.1 treats every Linux host as Debian. Distributions other than Debian-family are untested, and their package and group names differ. `adapt setup` needs an x86-64 host, because Google publishes Linux Platform-Tools only for x86-64. With 0.1.1, doctor's udev shortcut needs `packaging/linux/51-android.rules` from a source checkout.
- **macOS** is not supported. In the development tree doctor reports it as unsupported and `adapt setup` refuses to run.
- **Android 10** needs the USB-first `adb tcpip` flow, which ADAPT does not run for you.

**Other limits**

- **AI clients.** With 0.1.1, MCP tools called without a device use the only ready device, including the five that act on the phone; in the development tree only the read-only tools do. `--read-only` still shows the model screens and logs. MCP input tools act on the phone's default display only.
- **Privacy.** Logs and screenshots are not redacted. `dex pc-probe` output includes each display's unique ID; with 0.1.1, on the tested phone, the Wi-Fi display's ID contained a hardware (MAC) address. The development tree replaces MAC-style addresses in that report with `<hardware-address>` (offline-tested only). Developer Options snapshots contain the device serial and build fingerprint.

**What ADAPT will not do**

- Run `adb kill-server` or `sudo adb`, or use an ADB server that is owned by root, SYSTEM or another user, runs a different adb version, or is selected with a non-default address or port.
- Unlock, flash, root or build ROMs, install USB drivers or OS packages, or change VPN or tailnet settings through its own commands.
- Present root, ROM or bootloader indicators as proof, or treat Samsung Download mode as fastboot.
- Run a test session for you: Rapid ADAPT prints a plan, and the debug commands never attach a debugger.
- Install the catalog's ROM and image tools; those entries are guidance only.
- Undo host changes: `pipx uninstall adapt-adb` leaves Platform-Tools, its PATH entry, a managed scrcpy, udev rules and snapshot files in place.

**The `adapt adb` passthrough** refuses `kill-server`, `sudo`, package uninstall and app-data clear (including raw binder calls to the package and activity services), bare `shell`, `logcat` without `-d` or `-t`, and shell command lines it cannot inspect (pipes, redirection, chaining, substitution, nested shells). It also refuses to guess between several ready devices and to use an unverified ADB server; with exactly one ready device and no `-s`, it uses that device. Everything else, including `adb root`, `adb reboot bootloader`, `adb remount` and `adb shell rm`, runs as typed, with no snapshot.

Full list: [Known limitations](docs/known-limitations.md). Recorded checks: [Validation](docs/validation.md).

## Native Windows receiver (experimental source)

`native/windows-receiver` contains Rust source for a PC-side wireless-DeX receiver, derived from the MIT-licensed [DeXDesk](https://github.com/adirh3/DeXDesk) (commit `5070ccda5c363f5ed0b46d1fe7cf2c15c47fc125`). It is **manual-build and experimental, outside the Python package**: you build it yourself (in the development tree, `scripts/build_receiver.ps1` wraps the Cargo build). It targets x64 Windows and uses an undocumented Windows advertisement provider. By default it **admits any phone** that requests a Wi-Fi Direct connection; `ADAPT_DEX_EXPECTED_NAME` and `ADAPT_DEX_EXPECTED_ID` optionally restrict it to an exact friendly name or Windows peer ID, and neither is authentication.

In the development tree it also writes a documented `status.json` with stage names and failure reason codes, never writes the phone's name, peer ID or addresses to its status file and keeps them out of its log unless you opt in, and has an experimental audio option that is off by default. `adapt dex receiver-start --exe PATH --yes`, `receiver-status` and `receiver-stop --yes` start a receiver you built, read its status file and ask it to stop; 0.1.1 has none of this and does not play audio.

Recorded on one Windows 11 PC with one Wi-Fi adapter, with Samsung devices:

- With an Android 13 phone, a revision from before the first public release formed a group, decoded desktop frames and survived one controlled reconnect. On 2026-11-14 a running instance of an earlier build (not the published source) was found holding one session that had started about a day and a half before; whether picture and input stayed usable was not observed.
- The 0.1.1 source (debug build, admission restricted by name) on 2026-11-14, with the Android 13 phone: one session of about four minutes; every stage through the first decoded frame within about four seconds; the phone chose 1920x1080 at 30 FPS; a capture of the window showed the DeX desktop drawn without corruption.
- The development-tree source with audio on, admission restricted by name, the same day: one session of about 90 seconds with the Android 13 phone, one of about 48 minutes with an Android 17 phone (One UI 9) and a short one with an Android 16 tablet (One UI 8; the record covers about two minutes). In each, every stage through the first decoded frame completed within about five seconds, the device chose 1920x1080 at 30 FPS, the new status fields were written and the Windows audio output opened at 48 kHz stereo; window captures showed the desktop drawn correctly with the two phones (none was taken with the tablet). On the Android 17 phone the owner confirmed that real mouse and keyboard input worked and that the phone's sound was heard from the PC, and judged the audio quality great; that is one listener on one session, whose log recorded 9 audio underruns over the 48 minutes. Nobody used a real mouse or listened with the other two devices. Audio stays off by default.
- Not established: real input or sound on any device other than the Android 17 phone (with the Android 13 phone, scripted clicks reached the phone as touches but a click on a desktop icon did not open the app, and why is not known), open admission with a device connecting, reconnects on the current source, sessions longer than about 48 minutes, any failure reason code on real hardware, a release build, and any device other than those three.

Its offline Rust tests do not start the receiver or involve a phone. See [validation](docs/validation.md) and its [README](native/windows-receiver/README.md).

## Documentation

| Start here | Reference | Guides |
|---|---|---|
| [Features](docs/features.md) | [Command reference](docs/command-reference.md) | [Doctor and setup](docs/doctor.md) |
| [Architecture](docs/architecture.md) | [MCP access](docs/mcp-access.md) | [Tailscale](docs/tailscale.md), [SSH workers](docs/ssh-workers.md), [Hypervisors](docs/hypervisors.md) |
| [Known limitations](docs/known-limitations.md) | [Android support](docs/android-support.md) | [Developer options](docs/dev-options.md), [App and debug validation](docs/development-validation.md), [Rapid ADAPT](docs/rapid-adapt.md) |
| [Validation](docs/validation.md) | [Tools catalog](docs/tools.md), [scrcpy](docs/scrcpy.md) | [Samsung DeX](docs/dex.md), [DeX for PC](docs/dex-pc.md), [Desktop testing](docs/desktop-testing.md) |
| [Offline showcase](docs/showcase/index.html) | [Device detection](docs/device-detection.md) | [Console](docs/console.md), [Help and tips](docs/help.md), [Give Me Everything](docs/everything.md), [AI clients](docs/clients.md) |

Platform notes: [Windows](packaging/windows/README.md), [Linux](packaging/linux/README.md). MCP client examples: [`examples/`](examples/README.md).

Printable overview, generated for 0.1.0 and not regenerated since (it says 64 commands and predates the 2026-11-14 records): [ADAPT overview PDF](docs/ADAPT-overview.pdf). Static title fallback: [PNG](docs/assets/adapt-title.png).

## Contributing, license and notices

Contributions are welcome; read [CONTRIBUTING.md](CONTRIBUTING.md) first (safety rules, privacy rules, offline tests). ADAPT is released under the [MIT License](LICENSE). Third-party and upstream notices, including the DeXDesk-derived receiver, are in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md). ADAPT is not affiliated with Google, Samsung, Microsoft or Tailscale.
