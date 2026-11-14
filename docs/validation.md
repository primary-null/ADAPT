# Validation scope

ADAPT 0.1.1 is an alpha pre-release. This repository contains unreleased
changes that the 0.1.1 download does not have (`adapt explain` and failure
guidance, explicit devices for the MCP tools that act on the phone, the MCP
transport label read from the serial, doctor and setup fixes, DeX state parsing
and display-flag detection, hardware addresses withheld from `dex pc-probe`
output, SSH reason codes, and the native receiver's status file, audio option
and `adapt dex receiver-*` commands). When the 2026-11-14 records below were
made, these changes were uncommitted on the `distribution/public-release`
branch; this page calls that code "the working tree". This page records which
checks exist, what each one supports, and which code each one ran against: a
record for 0.1.1 is not a record for the working tree, and the reverse. Offline tests, physical observations, planning-only commands and
release artifacts are separate forms of evidence; their counts must not be
added together as a pass rate. Where a date, commit or version was not
recorded, this page says so and does not supply one. The gaps are collected
under "Not recorded" at the end.

## Offline tests

Offline tests mock `adb`, `ssh`, `tailscale`, downloads and archive fixtures.
No device is involved.

- Python suite at the 0.1.1 release commit (`45b2a0a`, tag `v0.1.1`): pytest
  collects 917 tests in 33 test modules. All 917 passed with
  `python -m pytest -q -p no:cacheprovider -x -q` on Windows with Python
  3.13.15, run on an export of that commit on 2026-11-14 while these
  documents were corrected. The run is also entered in
  `publication-checks.json`.
- Earlier recorded run: `publication-checks.json` is dated 2026-11-13 and
  records 909 tests in 32 modules passing on Windows with Python 3.13.15.
  Collecting tests at commit `718514d` gives the same 909 tests in 32 modules,
  so that entry describes source at or near `718514d`, before the release
  workflow and the 0.1.1 change. The commit it was run on is not recorded. Two
  other parts of that file are older: its `public_source_sha256` list matches
  the initial public commit `21fb3a8` (34 of 34 files) and differs from 0.1.1
  in 17 of 34 files, and its `distribution.artifacts` are local builds of
  0.1.0. Neither is a 0.1.1 record.
- Python suite in the uncommitted working tree on 2026-11-14 (the unreleased
  changes above): `python -m pytest --collect-only -q -p no:cacheprovider`
  collected 1706 tests, and 1741 in 55 test modules a few minutes later,
  while tests were still being added. Later the same day, after the
  display-flag DeX detection, the transport-label and hardware-address fixes
  and the remaining failure guidance were added, the same command collected
  2034 tests in 59 test modules. These are collection counts, not a run; this
  page records no full-suite result for that tree, and the count changes as
  work continues.
- Native receiver in the working tree: the source has 119 `#[test]` functions
  (counted with a search for `#[test]` under `native/windows-receiver/src`).
  The record of the second hardware run below says the merged build had 118
  offline tests passing; the receiver's README says all 119 passed with
  `cargo test --offline`. Neither statement names a toolchain run record or a
  commit (the code is uncommitted), and the documentation pass that added
  this entry ran no Rust toolchain.
- Native receiver: the source at 0.1.1 has 20 `#[test]` functions (15 in the
  initial public commit). `publication-checks.json` records all 20 passing
  with `cargo test --locked --offline` (Rust 1.99.1, `x86_64-pc-windows-gnu`)
  on 2026-11-13; the count was raised to 20 in the open-admission commit
  `369e625`, and the commit the run used is not recorded. These tests do not
  start the receiver or involve a phone. No `cargo test` run on 0.1.1 is
  recorded (the 2026-11-14 hardware run built the 0.1.1 receiver but records no
  test run), and no CI job runs the native tests.
- Release workflow (`.github/workflows/release.yml`, started by a `v*` tag or
  by hand). The `build` job runs on Ubuntu with Python 3.11: `python -m pytest
  -q`, `python -m build`, `twine check` and `scripts/check_distribution.py`.
  At 0.1.1 the `install` job runs on Windows and Ubuntu with Python 3.11 and
  3.13: it
  installs the built wheel with `pipx`, runs `pip check`, `adapt --help` and
  `adapt android versions`, then installs the `adapt-adb[mcp] @ <file URI>`
  form and runs `adapt mcp --help`. The source archive gets `adapt --help` on
  Ubuntu with Python 3.11. The `publish` job runs only for a tag and only
  after both jobs pass; it attests the files and creates the pre-release. This
  describes what the workflow is configured to do. The repository records no
  run number or result for the `v0.1.0` or `v0.1.1` runs. At 0.1.1 the
  workflow does not run the suite on Windows or on Python 3.13, does not test
  Python 3.12, runs no native tests and contacts no device. In the working
  tree the `install` job's matrix lists Python 3.11, 3.12 and 3.13; nothing
  else in the workflow changed, and no run of the changed workflow is
  recorded.
- Privacy checks (a secret scan and a private-identifier scan of the selected
  public tree, generated assets, and package and archive metadata) are
  recorded in `publication-checks.json` under the prepublication phase. No
  record shows them repeated for later commits or for 0.1.1. Automated
  scanners cannot guarantee the absence of every sensitive value.

## Planning-only commands

`rapid plan`, `tools plan` and the other `plan` commands print steps or
eligibility and run nothing. Only offline tests cover them; no physical run
applies.

## Physical observations

Physical observations come from Samsung devices only. There are two sets of
records: observations on pre-release builds made before publication, with two
phones (Android 13/API 33 and Android 17/API 37), and a set made on 2026-11-14
on one Windows 11 PC with one Wi-Fi adapter, with an Android 13 phone, an
Android 17 phone (One UI 9) and an Android 16 tablet (One UI 8). The 2026-11-14
set used the released 0.1.1 code for every smoke pass; the working tree's
Python code was run on devices only for the DeX inspection commands and the
`receiver-*` commands named below. The receiver runs used the 0.1.1 receiver
source and the working tree's receiver source. The 2026-11-14 record does not
say whether its phones are the same units as the earlier phones. These observations do not certify every baseline in the
Android 10-17 capability matrix or every OEM implementation. Raw captures are
not included in this repository, and the records contain no serial numbers,
addresses or account names.

### Before publication (pre-release builds)

The record names a Windows 11 host for the `doctor` observation, and the
native receiver runs only on Windows. It names no other host operating system,
and the Windows build is not recorded.

The observations were first recorded in the initial public commit (`21fb3a8`,
2026-11-13) under the heading "Physical observations before publication". They
were made on pre-release builds. The record gives no date, ADAPT version or
commit, firmware or One UI build, adb or scrcpy version, transport or captured
output for any single observation.

| Area | Phone | Commands named | What the record says | Not recorded |
|---|---|---|---|---|
| USB and Wi-Fi ADB, host preflight, device inspection | Not split by phone | None here; `features.md` labels `doctor`, `devices`, `adb`, `pair` and `connect` as observed, including simultaneous USB and Wi-Fi | Exercised in bounded sessions | Which transport each phone used; command output; pass or fail per command |
| `android capabilities` | API 33 and API 37 (`features.md`) | `android capabilities` | Run on both phones | Output and versions |
| Private routed VPN/hotspot path | One phone; API level not recorded | None | Authenticated ADB commands returned, and one retained connection was rechecked. This is not Tailscale | Which phone; the route; the commands |
| Tailscale | Pairing success: API 37 phone (`feature-inventory.json`); other items not stated | None; the `tailscale` group only | Local peer parsing worked and one pairing succeeded. The strict verifier needs `service.adb.tls.port` to equal the connect port, and that property was empty on the tested firmware, so no direct Tailscale ADB run completed. Pairing success and an open port alone do not prove an ADB transport | Which phone had the empty property; whether the pairing used a tailnet address |
| Developer Options | Both phones | `dev-options` (inspection of the stay-awake settings; command not named) | The stay-awake settings were read | Any `apply` or `restore` on a device |
| Logs | API 33 | Not named | A DeX system log was captured | Tags, duration, result |
| DeX activation by ADAPT | API 33 | `dex pc-start`, methods `loopback` and `overlay` (`dex-pc.md`) | `loopback` was denied by the firmware permission `CONFIGURE_WIFI_DISPLAY`; the simulated `overlay` display did not start genuine DeX. ADAPT's activation has never been observed succeeding | Any attempt on the API 37 phone; the number of attempts |
| DeX display inspection and dual mirroring | API 33 | Not named | `feature-inventory.json` lists live display inspection and dual mirroring; `dex-pc.md` records that Samsung API state confirmed enabled dual DeX during the receiver session | Which commands; which DeX session |
| Native Windows receiver (not an `adapt` command) | API 33 | None | An earlier source revision formed a Wi-Fi Direct group, negotiated 1920x1080 at a target 30 FPS and decoded changing desktop frames. After a keyboard-routing fix, one controlled receiver replacement and reconnect passed, and phone and native desktop keyboard input stayed separate. Details: `dex-pc.md`, "Evidence" | Which source revision; the date; the Windows build and Wi-Fi radio; any long session. Audio is not implemented |
| Console | No phone | `adapt console` | Interactive use in a Windows terminal (`feature-inventory.json`) | The terminal program |

After the initial public commit, the adb safety guard (`f682625`), doctor
preflight and setup confirmation (`0fcbc03`), Developer Options restore
ownership check (`98fb03e`), DeX overlay readback and session and SSH output
bounds (`59f17c2`), MCP server (`cae5fbb`), diagnostics, console and reporting
(`afb0de0`), receiver network and decoder paths (`427d1bb`), receiver settings
dialog (`009111f`), Windows port-5037 listener probe and doctor device listing
(`758fb20`), receiver admission (`369e625`) and release workflow (`a5b9713`)
changed. The record does not say which of these were in place for any
observation. The message of `758fb20` says its fixes were "found by running
against real devices"; no run record, device, API level or ADAPT version goes
with that statement, and `publication-checks.json` states that the
publication pass itself did no live device work.

### 2026-11-14: released 0.1.1 on an Android 13 phone

- **Code:** the 0.1.1 release commit (`45b2a0a`), exported with `git archive`
  into a scratch directory and run as `python -m adapt_adb` from there. The
  working tree was not used.
- **Host:** Windows 11, x86-64, Python 3.13, Google Platform-Tools 37.0.1 on
  `PATH`. The Windows build is not recorded.
- **Device:** one Samsung phone, Android 13 (API 33), connected by USB and
  unlocked. Two other transports were attached to the same adb server; every
  device command named this phone with `-s`.
- **Operator:** an automated agent with the owner's permission. No taps,
  swipes, key events, installs or uninstalls were sent.

| Command | Exit | Observed |
|---|---|---|
| `adapt devices` | 0 | Listed all three transports with their states |
| `adapt doctor --json` | 0 | `status: ok`; the shared server check passed (current user, matching version); `path_config` warned that the first adb on `PATH` shadows other copies; `devices_state` warned about the offline transport |
| `adapt android capabilities --json` | 0 | SDK 33, release 13, manufacturer and model read; `covered_version`; no changes |
| `adapt dev-options inspect --json` | 0 | Each setting read with `status: observed` |
| `adapt dex inspect --json` | 0 | Workflow `eligible`; DeX classification `unknown`, although a dual-mode wireless DeX session was active (next section). The phone prints its state in a form the 0.1.1 parser does not recognise |
| `adapt dex list-displays --json` | 0 | Built-in displays, a 1920x1080 display named "Desktop" and a display flagged `FLAG_WIRELESS_DEX_DISPLAY`; `confirmed_dex_display_id` `null` for the same parser reason |
| `adapt dex pc-probe --yes --json` | 0 | `status: observed`; the helper reported DeX enabled and four displays including one of type Wi-Fi; the temporary helper was removed; no display changes |
| `adapt detect inspect --json` | 0 | Shell uid 2000, root `unknown`, bootloader `locked`, build `user` / `release-keys` |
| `adapt desktop inspect --json` | 0 | Features and settings read; `samsung_dex: not_inferred` |
| `adapt dev-options apply --profile touches` | 0 | `show_touches` read back as `1` (was `0`) |
| `adapt dev-options restore` | 0 | "Readback matched the snapshot"; `show_touches` read back as `0` |
| `adapt mcp --read-only --device` (stdio, MCP Python SDK 2.3.0 client) | n/a | 18 tools exposed; `list_devices` returned only the selected phone; `device_info` returned model, SDK, screen and lock state; `screenshot` returned a JPEG; `tap` was refused with `access_denied` |

Findings, and their state in the working tree:

1. The 0.1.1 MCP server announces version `0.1.0`. The working tree announces
   the package version (offline-tested only).
2. `dex inspect` did not recognise active DeX on this phone, so `dex
   plan-dual-mirror` and `dex mirror` cannot confirm a DeX display there. The
   working tree parses this phone's form for active dual-mode and idle DeX,
   tested offline against output from this phone; it has not been re-run on the
   phone. `dex pc-probe`, which asks the framework through the bundled helper,
   did report DeX as enabled.
3. `dex pc-probe` output includes the Wi-Fi display's unique ID, which here
   contained a hardware (MAC) address. The working tree replaces MAC-style
   addresses in the probe's observations with `<hardware-address>`; that change
   has offline tests only (`pc-probe` was not run to completion on a device
   with the working tree).
4. The screenshot's reported size was 1024 x 820 for a 1768 x 2208 panel,
   which implies a landscape capture. The image was not opened.

This shows these commands returning the results above on released code, on
one phone, one host and one day. The one change made (`show_touches`) was
restored and verified by readback. It does not cover Linux, Tailscale, SSH
workers, DeX activation, `setup`, scrcpy installation or mirroring, app, log
or debug commands, or any MCP tool that changes the phone.

### 2026-11-14: released 0.1.1 on an Android 17 phone and an Android 16 tablet

Same PC and code as above. Both devices were Samsung devices connected by
wireless debugging (mDNS transports) and prepared and unlocked by the owner:
a phone with Android 17 (API 37, One UI 9) and a tablet with Android 16
(API 36, One UI 8). The record does not say whether the phone is the same unit
as the earlier API 37 phone.

| Command | Phone (API 37) | Tablet (API 36) |
|---|---|---|
| `android capabilities`, `dev-options inspect`, `detect inspect`, `desktop inspect` | Exit 0, values read | Same |
| `dev-options apply --profile touches`, then `restore` | Exit 0 both; `show_touches` was already `1`, so nothing changed and the cycle shows little | Same |
| `dex inspect` | Exit 0, DeX `unknown`: the `desktopmode` service was not available | Same |
| `dex list-displays` (DeX idle) | Built-in display only | Built-in display only |
| `adapt mcp --read-only --device` | 18 tools; `list_devices`, `device_info` and `screenshot` returned results; `tap` refused | Same |

Findings with 0.1.1, and their state in the working tree:

1. One UI 8 and One UI 9 have no `desktopmode` service, so 0.1.1's `dex
   inspect` can only report `unknown` on these devices (while still reporting
   the workflow as `eligible`), and its `dex plan-dual-mirror` and `dex mirror`
   cannot confirm a DeX display. The working tree, when the service is absent,
   reads the display flags instead; that was observed confirming DeX on both
   devices during a wireless DeX session (below).
2. On the phone, during an active wireless DeX session, `dex pc-probe` refused
   with "Samsung helper returned unrecognized state" (exit 1). The working tree
   was refused the same way.
3. During that session 0.1.1's `dex list-displays` showed a second 1920x1080
   display flagged `FLAG_WIRELESS_DEX_DISPLAY`, `FLAG_EXTERNAL_DEX_HOSTING` and
   `FLAG_WIFI_DISPLAY`. 0.1.1 lists it but does not use those flags as DeX
   evidence; the working tree does (finding 1).
4. MCP `list_devices` labels a wireless-debugging transport `usb`; the label
   defaults to `usb` when the device line carries no transport hint. (This is the
   0.1.1 behaviour that was observed. The working tree reads the label from the
   serial; that change has offline tests only.)

### 2026-11-14: wireless DeX session found running (earlier receiver build)

When the Android 13 pass started, the phone was already in a wireless DeX
session with a receiver process on the same PC. It was only read.

- The receiver had been started on 2026-11-13 from a scratch release build of
  upstream DeXDesk `5070ccda` plus an early ADAPT patch set. Its `receiver.rs`,
  `media.rs`, `protocol.rs` and `ui.rs` differ from the published source, so it
  is not the published receiver.
- Its status, about 35.5 hours after start, showed every stage through
  `first_frame_decoded` and 3,818,280 frames (about 30 per second on average).
  The log showed repeated video recovery lines for missing RTP packets.
- The phone reported a `WIRELESS_DEX` display at 1920x1080 in dual mode, and a
  formed Wi-Fi Direct group with the phone as client.

This shows one earlier receiver build holding one session with one phone for
about a day and a half. Whether picture and input stayed usable was not
observed. It says nothing about the published source.

### 2026-11-14: native receiver, 0.1.1 source

After the owner confirmed the older session was not in use, it was closed with
its `stop.request` file. Its `status.json` still read `Connected` and
`running: true` after it exited.

- **Build:** `native/windows-receiver` at `45b2a0a`, debug profile, `cargo
  build --offline --locked`, data directory redirected to a scratch folder,
  admission restricted by name to the phone. Same Android 13 phone and PC.
- **Start:** the phone's quick-settings DeX tile offered the PC at once; two
  taps started the session.
- **Session:** every stage through `first_frame_decoded` within about four
  seconds. The receiver's target was 1080p at 60 FPS; the phone chose 1920x1080
  at 30 FPS. 7,499 frames in 251 seconds (29.9 per second). A capture of the
  window showed the DeX desktop drawn without corruption. 33 video recovery
  events covered 97 missing RTP packets.
- **Input:** clicks sent by a script into the window produced touch events on
  the phone with matching press and release timing. A click and a double-click
  on a desktop icon did not open the app; why was not determined. No real mouse
  click was made and keyboard input was not tried.
- **Stop:** `stop.request` closed the receiver in about two seconds and the
  phone left DeX. `status.json` was left reading `Connected`, `running: true`,
  `session_active: true`, and the log has no shutdown line.
- **Not covered:** audio (not implemented in this source), reconnect, a second
  phone, sessions longer than about four minutes, real mouse and keyboard use,
  open admission.

### 2026-11-14: native receiver, working-tree source with audio

Build: the working tree's `native/windows-receiver` with the session,
diagnostics and audio changes, debug profile, uncommitted, admission restricted
by name, `ADAPT_DEX_AUDIO=1`. Same PC.

- **Android 13 phone:** every stage through `first_frame_decoded` in about
  three to four seconds; 1920x1080 at 30 FPS; 2,621 frames in 89 seconds (29.4
  per second); the desktop drawn correctly; 15 video recovery events.
  `status.json` carried `schema_version: 1`, `phase_code`, the `admission`
  flags, `advertising` and a `peer` object with no name, ID or address; the
  log showed a peer tag and "addresses withheld". The transport stream carried
  video (`0x1b`) and audio (`0x83`) and the log reported "Audio output open:
  48000 Hz, 2 ch, 16-bit", with five underruns in about 90 seconds. On stop,
  `status.json` read `running: false`, `session_active: false`, `advertising:
  false` and `last_session_end.code: user_stopped`; `phase_code` still read
  `connected`, and `stop.request` was left in place.
- **Android 17 phone:** on this firmware the tile is named "Wireless DeX" and
  opens a device picker; choosing the PC and "Start now" started the session.
  Every stage through `first_frame_decoded` within about four seconds;
  1920x1080 at 30 FPS; about 30 frames per second over the first minute; a
  window capture showed the DeX desktop with an app window drawn correctly. The
  audio output opened the same way, with three underruns and 17 video recovery
  events in the first minute. The session ran about 48 minutes in total:
  86,202 decoded frames, 653 video recovery events and 9 audio underruns in the
  log. About 46 minutes in, the log also showed the audio path trimming queued
  sound to hold delay down about 400 times (roughly one 70 ms trim every seven
  seconds). The session was read with the working tree's `adapt dex
  receiver-status` ("a phone is streaming", with the frame count and stages)
  and ended with its `adapt dex receiver-stop --yes` (`stopped`,
  `user_stopped`).
- **Owner's check on the Android 17 phone session:** the owner reported that
  real mouse and keyboard use inside the receiver window worked and that sound
  played on the phone was heard from the PC, and, asked afterwards, that the
  audio quality was great. No further detail was recorded (which apps, how
  long, whether any stutter was noticed). This is one person's report on one
  session with one phone and one PC; it establishes that input and audio
  worked there, and "great" is that listener's judgement, not a measurement.
- **Android 16 tablet (One UI 8):** the record says "working tree" but does not
  name the receiver's build profile. Started with the working tree's `adapt dex
  receiver-start --exe … --expect-name … --audio --yes`; the result reported
  the name filter active and audio enabled, and the tablet's name appeared
  neither in the report nor on the receiver's command line. The tablet's
  "Wireless DeX" tile opened a device picker; choosing the PC and "Start now"
  started the session. Every stage through `first_frame_decoded` in about five
  seconds; 1920x1080 at 30 FPS; about 2,200 frames in roughly 75 seconds; 15
  video recovery events; the audio stream (`0x83`) was present and the output
  opened at 48 kHz stereo, with five underruns in about two minutes. `adapt dex
  receiver-status` reported "a phone is streaming" and `adapt dex
  receiver-stop --yes` ended the session. No window capture was taken, nobody
  used a real mouse or keyboard, and nobody listened on this device.

On the Android 13 phone and the Android 16 tablet nobody listened or used a real
mouse or keyboard; audible sound and real input were confirmed on the Android 17
phone only. Admission was restricted by device name in every session with a
device. Not covered on any device: reconnect, open admission with a device
connecting, sessions longer than about 48 minutes, a release build, and failure
paths (no failure reason code was triggered).

### 2026-11-14: `adapt dex receiver-*` against the real receiver

Working-tree code and the debug build above, same Windows 11 PC, no phone.

| Command | Exit | Observed |
|---|---|---|
| `receiver-status` (nothing running) | 0 | "not running", with the start command as the next step |
| `receiver-start` without `--yes` | 1 | Refused, saying that starting advertises the PC over Wi-Fi Direct; nothing started |
| `receiver-start --yes` | 0 | Started detached; result `started`, "ready for a phone", open admission stated |
| `receiver-status` (running) | 0 | "ready for a phone", process alive, admission open, stage `advertising` |
| `receiver-start` again | 1 | Refused: a receiver is already running |
| `receiver-stop --yes` | 0 | "stopped"; the process exited and the status file recorded a completed shutdown |
| `receiver-status` (after stop) | 0 | "not running", process gone |

Later the same day `receiver-status` read the streaming sessions with the
Android 17 phone and the Android 16 tablet, `receiver-stop --yes` ended both,
and `receiver-start` with `--expect-name` and `--audio` started the tablet
session (previous section). Not covered: `receiver-status` while a device is
still connecting, any failure state read from a real receiver, the
`--expect-id` option, and `scripts/build_receiver.ps1` beyond its dry-run
switches.

### 2026-11-14: working-tree DeX detection during live wireless DeX sessions

Code: the uncommitted working tree. Same PC, during the receiver sessions above.

| Command | Phone, Android 17 / One UI 9 (about 46 minutes into the session) | Tablet, Android 16 / One UI 8 |
|---|---|---|
| `dex inspect` | Exit 0 (`--json`). DeX confirmed on display 6 from display flags: classification `active_wireless_dex_display_flag`, `recognized_shape: dumpsys_display_flag_wireless_dex_display`, and a reason stating that the `desktopmode` service is absent on this firmware | DeX confirmed on display 6 from `FLAG_WIRELESS_DEX_DISPLAY` with `FLAG_EXTERNAL_DEX_HOSTING`, the `desktopmode` service being absent (exit code and options not recorded) |
| `dex list-displays --json` | Exit 0; `confirmed_dex_display_id: 6`, evidence source `dumpsys_display_flags` | Not run |
| `dex plan-dual-mirror --dex-display 6 --json` | Exit 0; a plan for display 0 plus display 6. Nothing was launched | Not run |
| `dex pc-probe --yes --json` | Exit 1; still refused: "Samsung helper returned unrecognized state" | Not run |

This shows the display-flag rule confirming DeX on two firmwares, each in one
wireless session to this receiver. Not observed: wired DeX, standalone DeX,
`dex mirror` or any scrcpy capture of the flagged display, and the flag rule
with DeX idle on the working tree (0.1.1 listed only display 0 on both devices
with DeX idle). During these sessions the report's `display_flag_evidence.observed_on` and
`not_observed` fields named only the Android 17 phone and listed the tablet with
DeX running as not observed; they were updated afterwards to name both devices.

### Not observed

Not observed on any device, by the records above: a Linux host; Android 10 or
any version other than 13, 16 and 17; Tailscale end to end; SSH workers or MCP
over SSH; any external AI client; any MCP tool that acts on the phone (`tap`
was only refused); `setup` on a fresh host; `tools install scrcpy`; `app`;
`debug`; `everything`; app log capture; DeX activation by ADAPT succeeding;
wired or standalone DeX; scrcpy capturing a DeX display confirmed from display
flags; `dex pc-probe` completing on One UI 9; audible receiver audio or real
mouse and keyboard input on any device other than the Android 17 phone; a
receiver reconnect, open admission with a device connecting, a failure reason
code, a release build or a session longer than about 48 minutes on the current
receiver source; and any of the working tree's Python changes against a device
other than the DeX inspection and `receiver-*` commands above (failure
guidance, explicit devices for MCP tools, the MCP transport label, doctor and
setup fixes, the Android 13 DeX parser, hardware addresses withheld from
`pc-probe`, SSH reason codes). These are covered by offline tests only; no
failure guidance has been seen on a real failure. Profile rollback, conflicts
and partial-failure recovery are covered offline; the only apply and restore
cycle that changed a value is the Android 13 one, with 0.1.1.

## Release checks required by `docs/releasing.md`

| Step | Record in this repository |
|---|---|
| 2. Local offline tests, `python -m build`, `twine check`, `check_distribution.py --tag` | None for a local run. The `build` job is configured to repeat them; no run result is recorded |
| 4. Workflow gates (tests, package checks, installs on Windows and Linux with Python 3.11 and 3.13) | Configuration only (`.github/workflows/release.yml`); no run number or result |
| 5. Attestation and pre-release publication | None in the repository; the release itself lives on GitHub |
| 6. Download the wheel without a GitHub login, check `SHA256SUMS.txt`, and test the exact README `pipx` commands from a clean temporary pipx home | **None.** The workflow installs from a local path or file URI, not from the published URL |
| Verification with `gh attestation verify` | **None** for the wheel, the source archive or `SHA256SUMS.txt` |

## Outstanding acceptance

Native Debian runtime, other Linux distributions, other OEMs, Android
versions other than 13, 16 and 17, fresh host provisioning across
architectures, external MCP clients, MCP tools that act on the phone, live SSH
workers, secondary-display MCP input, direct Tailnet validation, successful
ADAPT-initiated DeX activation, DeX detection beyond one wireless session on
each of two firmwares (wired and standalone DeX, mirroring the flagged
display), a device run of the rest of the working tree's Python code, real
mouse, keyboard and audio on the native receiver beyond one phone, receiver
reconnects, failure codes and open admission on real hardware, and
long-duration native receiver behavior remain separate acceptance work.

## Not recorded

- For each pre-publication observation: the date, ADAPT version or commit,
  firmware or One UI build, adb and scrcpy versions, Windows build, transport,
  exact commands and outcome. The 2026-11-14 records give date, code, host and
  transport, but not the Windows build or the firmware build numbers.
- Any device run from Linux, and any device run of the working tree's Python
  code other than the DeX inspection and `receiver-*` commands above.
- For the owner's check on the Android 17 phone: which apps were used, for how
  long, and whether any stutter was noticed. For the Android 16 tablet: the
  exit code and options of the working tree's `dex inspect`.
- The commit of the working-tree code used on 2026-11-14 (it was uncommitted).
- The result of the `v0.1.0` and `v0.1.1` release workflow runs, and any run of
  the working tree's workflow.
- The `docs/releasing.md` step 6 check and the attestation verification.
- The commit used for the recorded `cargo test` run, any native test run on
  0.1.1, and a run record (toolchain, output) for the working tree's native
  tests.
- A full Python suite result for the working tree.
- Any repeat of the privacy scans on 0.1.1.
- Checksums of the 0.1.1 wheel and source archive; the release carries them in
  `SHA256SUMS.txt`, not this repository.

The showcase contains nine generated source-derived illustrations and one
rendered capture of actual offline CLI output. It contains no Android screen
images. Generated examples are not physical-test evidence.
