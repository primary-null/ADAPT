# ADAPT Windows wireless receiver (experimental source)

A small native Windows receiver for wireless Samsung DeX, written in Rust. The Cargo package is named `dexdesk`; its window is titled **ADAPT Wireless DeX**. Samsung DeX still runs on the phone: this program is the **PC-side wireless-display sink** that the phone's DeX receiver picker can connect to, and it displays and controls that desktop in its own window.

> **Status: experimental, manual build.** The source is included for review and experimentation. It is **not part of the Python wheel**, **never built, found or downloaded by ADAPT** (`adapt dex receiver-start` runs only a `dexdesk.exe` that you built and name yourself), and carries **no compatibility promise** across Android builds, Wi-Fi radios or Windows versions. Read [known limitations](../../docs/known-limitations.md) first.

## Provenance and license

This directory derives from the MIT-licensed upstream project [DeXDesk](https://github.com/adirh3/DeXDesk) at commit [`5070ccda5c363f5ed0b46d1fe7cf2c15c47fc125`](https://github.com/adirh3/DeXDesk/tree/5070ccda5c363f5ed0b46d1fe7cf2c15c47fc125). The upstream license is preserved in [`LICENSE`](LICENSE) ("Copyright (c) 2026 DeXDesk contributors") and dependency licenses in [`THIRD-PARTY-NOTICES.txt`](THIRD-PARTY-NOTICES.txt). The repository root [`LICENSE`](../../LICENSE) (MIT, ADAPT Contributors) and [`THIRD-PARTY-NOTICES.md`](../../THIRD-PARTY-NOTICES.md) cover the rest of the project. No Samsung software, firmware or proprietary client is included or redistributed.

Code in this tree that is specific to ADAPT includes `src/adapt_trace.rs` (observation stages, failure reason codes and the `status.json` document), the peer admission rule and the classification of how a session ended in `src/receiver.rs`, the privacy rules in `src/runtime.rs`, and the `ADAPT_DEX_*` environment variables.

## What it does

```text
 Samsung phone (DeX wireless)                          Windows PC (this program)
 +--------------------------+                          +------------------------------+
 | DeX desktop              |   Wi-Fi Direct group     | WinRT Wi-Fi Direct publisher |
 | receiver picker  ------->|=========================>| + connection listener        |
 |                          |   (this PC = group owner,| + Miracast advertisement via |
 |                          |    GO intent 15)         |   undocumented provider      |
 |                          |                          +--------------+---------------+
 |                          |   RTSP / WFD negotiation on TCP 7236    |
 |                          |<========================================+
 |   H.264 in MPEG-TS/RTP --|====== UDP RTP/RTCP port pair ==========>| demux -> Media
 |                          |                                         | Foundation H.264
 |   UIBC input channel  <--|<====== mouse/touch + USB-keyboard HID ==| decode -> D3D11
 +--------------------------+                                         | window
                                                                      +--------------+
```

- **Advertisement and group:** a `WiFiDirectAdvertisementPublisher` and `WiFiDirectConnectionListener` (autonomous group owner, normal listen discoverability), plus a Miracast sink advertisement set through functions resolved at runtime from `wlanapi.dll` (`WFDOpenHandle`, `WlanNotifyVsIeProviderInt`, `WFDCloseHandle`). That provider is **undocumented**; a Windows build that does not export it fails with a clear message.
- **Session:** the PC reserves TCP port 7236, accepts the phone's RTSP session (or dials the phone), negotiates a video format, and pairs an RTP/RTCP UDP port pair. Only a fixed set of CEA video modes (from 640x480 up to 1920x1080) is accepted; anything else is refused.
- **Video:** transport-stream demux, H.264 decode with Windows Media Foundation, Direct3D 11 presentation.
- **Audio (experimental, off by default):** the phone's sound is advertised as 48 kHz, 16-bit stereo LPCM. By default the receiver discards it. To play it through the default Windows output device, set `"audio": true` in `settings.json`, or start the receiver with `ADAPT_DEX_AUDIO=1` (`ADAPT_DEX_AUDIO=0` forces it off). Any audio failure turns audio off for that session and never interrupts video. Audio is not synchronised to video; it is played with roughly 40-100 ms of buffering. `media_check FILE.ts` also reports what audio a captured stream contains. Audible sound has been confirmed with one device only (an Android 17 phone), where one listener judged the quality great; see "Known boundaries" for what has and has not been observed.
- **Input:** UIBC with generic mouse and touch plus a USB HID keyboard. Right-click is a long press; the wheel becomes a smooth scrolling gesture.
- **Quality settings:** 1080p target 60 FPS (default), 1080p 30, 720p 60. Scroll speed 30-120, always-on-top, window size 640-8192 by 480-8192.

### Security posture

This is what the receiver does as built, read from the code in this directory. None of it is a security guarantee.

- **Admission is open by default.** Any Wi-Fi Direct device in radio range that requests a connection is admitted: there is no prompt and no pairing code of ours. Whoever runs the receiver is expected to control who is in range and what Windows itself allows. This is a deliberate owner decision; the filters below narrow it.
- **The filters are not authentication.** When `ADAPT_DEX_EXPECTED_NAME` is set, the phone's friendly name must **exactly equal** it (case-sensitive); when `ADAPT_DEX_EXPECTED_ID` is set, its Windows peer ID must equal it. A device can announce any name, and a peer ID is only as secret as the radio traffic that carries it. Treat them as a guard against accidental connection. An empty value restricts nothing. A value that is set but unusable (not valid Unicode, too long, control characters) stops the receiver from starting with `invalid_configuration` instead of silently leaving admission open. A refused phone is simply never connected; the receiver does not send it a rejection.
- **What an admitted phone gets.** It joins a Wi-Fi Direct group this PC owns (group owner intent 15). The PC then runs the RTSP exchange with it, decodes the H.264 video it sends (Windows Media Foundation) and sends it this PC's mouse and keyboard events. RTSP, RTP and MPEG-TS from the phone are parsed with bounded buffers and deadlines, but a media decoder is a large attack surface, and the peer-address checks below compare addresses inside a group; they do not authenticate anyone.
- **What listens where.** Everything is IPv4. The program creates no firewall rules, so Windows Defender Firewall alone decides who can reach it. Nothing listens in the preview modes.

| Endpoint | Bound to | Lifetime | Served |
|---|---|---|---|
| Wi-Fi Direct advertisement, autonomous group owner, normal discoverability | the Wi-Fi radio | start-up until exit | any Wi-Fi Direct device in range can see it |
| TCP 7236 | **0.0.0.0**, every IPv4 interface | start-up until exit | see below |
| UDP RTP (an even port) and RTCP (the next port), chosen by the OS | the PC's own Wi-Fi Direct group address | one pair per session, after the group exists | datagrams whose source is the phone's group address |
| outbound TCP to the phone's port 7236 and to the UIBC port it announces | the PC's group address | per session | the phone |

**Why TCP 7236 is bound on every interface, and what that exposes.** `run` in `src/receiver.rs` binds `0.0.0.0:7236` at start-up and holds it for the life of the process. The code shows two uses. It claims the port that the Miracast advertisement announces: start-up fails with `port_in_use` if the bind is refused, which the error text attributes to another receiver such as Windows' own Wireless Display. And it is the fallback for a phone that connects *to* the PC: the PC first dials the phone's port 7236 itself, and only if that fails does it wait up to 5 seconds on this listener for an inbound connection from the phone's group address (`connect_phone`). The listener is not narrowed to the group address because that address is known only after the Wi-Fi Direct group has formed, the phone can connect immediately afterwards, and a wildcard bind also keeps the "port in use" check strict. None of that can be verified without a phone and a radio, so the bind is left unchanged.

In practice any host that can reach this PC's port 7236 through any interface and through the firewall can open a TCP connection; the operating system completes the handshake and queues it. The program accepts a queued connection only while a session is being set up (the fallback wait above), compares its source address with the phone's group address, closes any other connection at once and **never reads a byte from it**. Connections that arrive between sessions stay queued, bounded by the OS listen backlog, and are closed unread when the next connection attempt begins. Log lines about rejected connections are limited to three per attempt. A stranger on the same network can therefore occupy backlog slots but cannot reach the RTSP parser. The address comparison is a filter on a private point-to-point group, not authentication.

### What is written to disk

Everything lives in the data directory: `%LOCALAPPDATA%\DeXDesk`, or the **absolute** directory named by `ADAPT_DEX_DATA_DIR`. The program creates it with default permissions: under `%LOCALAPPDATA%` that is your user profile's access control, while a directory you choose is as private as you make it. `dexdesk --print-data-dir` prints the resolved path without starting anything.

| File | Written | Contents |
|---|---|---|
| `status.json` | on every stage or phase change and at least once a second while the window is open | the status document below; never the phone's name, peer ID or address; replaced atomically |
| `status.pending.json` | transient | the next `status.json`, before it is renamed into place |
| `DeXDesk.log` | every log line (also echoed to stderr; a release build has no console, and a supervisor that redirects stderr to a pipe must keep reading it or the receiver blocks on a full pipe, while a closed pipe is ignored) | Unix-second timestamps, stages, error text, the quality target, the selected video mode, and a per-run peer tag; see below |
| `DeXDesk.previous.log` | at start-up, when `DeXDesk.log` is over 512 KiB | the previous log; an older one is deleted |
| `settings.json` | when settings are saved | quality, scroll speed, always-on-top, window size |
| `stop.request` | by a supervisor | see "Stopping" |

**The log withholds the phone by default.** Earlier builds wrote the phone's friendly name, its Wi-Fi Direct peer ID and both group IP addresses to `DeXDesk.log`. Now a connection is logged as `peer 1a2b3c4d`: a 32-bit tag from a random key that exists only in that process, so it is stable within a run, differs from run to run and cannot be reversed. The group addresses are replaced by `addresses withheld`. Set `ADAPT_DEX_LOG_PEER_DETAILS=1` (also `true`, `yes`, `on`) before starting to log the raw name, peer ID and addresses; you need that once to learn the peer ID for `ADAPT_DEX_EXPECTED_ID`. The first log line of every run says which mode is active. `status.json` never contains them in either mode. The log still holds the text of errors, which can echo a status line or a value the phone sent. While the receiver runs the log is truncated in place when it passes 512 KiB, so its history is bounded.

### Observation stages

`status.json` records which stages were observed for the current attempt, so an advertisement is never mistaken for a working desktop: `starting`, `advertising`, `connection_request`, `peer_rejected`, `peer_admitted`, `wifi_direct_group`, `rtsp_connected`, `video_negotiated`, `playback_accepted`, `input_channel_connected`, `first_frame_decoded` (`trace.last_stage` and the members of `trace.observed_stages`, snake_case). A new attempt starts with a clean set; a decoded frame does not certify input or Samsung DeX, and the file never records a device serial.

## Supervising the receiver: the `status.json` contract

This section is the interface for the ADAPT CLI and any other supervising program. The receiver writes `status.json` in the data directory; a supervisor only reads it. Rules for readers:

- Ignore fields you do not know. `schema_version` is raised only for an incompatible change (renamed or removed field, changed meaning); new fields and new reason codes are additive.
- The file is replaced by a rename, so a read can occasionally fail or find the old file while the rename races a reader: retry after a short pause.
- `process_id` identifies the receiver process. A file whose process is gone, or whose `updated_at_epoch_ms` is more than a few seconds old while the process is alive, is stale. After a crash it keeps the last state it had, except that a panic leaves an `internal_panic` failure (see below).
- Start-up errors that happen before the receiver has any state (the data directory cannot be created or the log cannot be opened) are **not** in `status.json`: the process prints `dexdesk: could not start: ...` to stderr, shows a message box and exits with code 1. A window that cannot be created is recorded as an `internal_error` failure in `status.json` and then reported the same way.

### Fields

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | integer | `1` |
| `receiver_version` | string | crate version, as printed by `dexdesk --version` |
| `process_id` | integer | the receiver's process ID |
| `started_at_epoch_ms` | integer | when the process created its state |
| `updated_at_epoch_ms` | integer | when this file was written |
| `running` | boolean | `false` once shutdown has begun |
| `advertising` | boolean | `true` while the Wi-Fi Direct advertisement is up and a phone can connect; `false` before it starts, after it is lost and after the receiver thread ends |
| `session_active` | boolean | one connection attempt currently owns the single session slot (from the connection request until its cleanup) |
| `phase` | string | the human-oriented phase: `Starting`, `Ready`, `Connecting("<redacted>")`, `Connected` or `Error("text")`. The phone's name is never included |
| `phase_code` | string | `starting`, `ready`, `connecting` (from admission until the first picture), `connected` (first picture decoded), `error`, or `stopped` (written once `running` is `false`, unless the phase is `error`) |
| `frames` | integer | decoded frames of the current session; `0` when no session is active |
| `trace` | object | `attempt` (counter, 0 before the first request), `attempt_active`, `last_stage`, `observed_stages` (sorted list); stage names above |
| `admission` | object | `open` (neither filter is set), `name_filter`, `id_filter`: which filters are active, never their values |
| `peer` | object | the phone of the last attempt: `present`, `admitted` (`true`, `false` or `null`) and `tag` (the same random per-run tag the log uses, or `null`); reset when an attempt begins |
| `ignored_connection_requests` | integer | requests ignored because a session was already active (they are not queued) |
| `last_failure` | object or `null` | the most recent failure; stays until another failure replaces it, so a later orderly session end or a new attempt does not clear it |
| `last_session_end` | object or `null` | how the most recent connection attempt ended, failure or not |

`last_failure` and `last_session_end` have the same shape:

| Field | Type | Meaning |
|---|---|---|
| `code` | string | one of the reason codes below |
| `scope` | string | `receiver` (start-up, or the advertisement was lost) or `session` (one connection attempt) |
| `stage` | string | the last stage reached before the end; the end happened while the receiver was trying to reach the next one |
| `attempt` | integer | `trace.attempt` at that time (0 if no request had arrived); compare with `trace.attempt` to tell an old failure from a new one |
| `at_epoch_ms` | integer | when it was recorded |
| `message` | string | human text, at most 240 characters, control characters replaced by spaces. Not for parsing; it can echo a value the phone sent |

`last_failure` never holds `user_stopped` or `peer_teardown`; `last_session_end` holds any session end. A `receiver`-scope failure appears only in `last_failure`.

### Reading the state

- **Ready for a phone:** `advertising` is `true` and `session_active` is `false`. `phase_code` may still be `error` there: the last session failed (see `last_failure`) and the receiver is advertising again.
- **The receiver is not working:** `advertising` is `false` while `running` is `true` and `phase_code` is no longer `starting`. `last_failure.scope == "receiver"` says why (a start-up failure, or the advertisement was lost: `wifi_radio_unavailable`, `wifi_direct_resource_in_use` or `advertisement_aborted`, possibly with a stage later than `starting`). Until a first failure or `advertising: true` appears, the receiver is still starting.
- **A phone is streaming:** `phase_code == "connected"` and `frames > 0`. The mouse and keyboard channel is up when `input_channel_connected` is in `trace.observed_stages`.
- **A session just ended:** `trace.attempt_active` is `false` and `last_session_end.attempt == trace.attempt`.

### Reason codes

Codes are snake_case, never renamed or reused. Anything not classified is `internal_error`. "Typical stage" is the usual `stage` value; it is the last stage reached, not the one that failed.

| Code | Scope | When | Typical stage |
|---|---|---|---|
| `invalid_configuration` | receiver | `ADAPT_DEX_EXPECTED_NAME` or `ADAPT_DEX_EXPECTED_ID` is set but not valid Unicode, too long (name 256, ID 1024 bytes) or contains control characters | `starting` |
| `port_in_use` | receiver | binding TCP 7236 was refused as "address in use" or "access denied" | `starting` |
| `wifi_direct_init_failed` | receiver | the Windows Runtime, or the Wi-Fi Direct publisher or listener, could not be created, configured, subscribed or started | `starting` |
| `miracast_provider_unavailable` | receiver | `wlanapi.dll`, one of its three undocumented exports, or `WFDOpenHandle` is missing or failed | `starting` |
| `miracast_provider_failed` | receiver | `WlanNotifyVsIeProviderInt` returned an error while enabling the sink advertisement | `starting` |
| `wifi_radio_unavailable` | receiver | the publisher aborted and Windows reported the Wi-Fi radio unavailable: no Wi-Fi Direct capable adapter, a disabled adapter or Wi-Fi switched off (Windows does not say which) | `starting`, or later if the advertisement is lost while running |
| `wifi_direct_resource_in_use` | receiver | the publisher aborted because another program holds Wi-Fi Direct | as above |
| `advertisement_aborted` | receiver | the publisher aborted with no reason, or a reason other than the two above | as above |
| `advertisement_timeout` | receiver | the publisher did not reach `Started` within 8 seconds | `starting` |
| `peer_rejected_name` | session | the phone's name differs from `ADAPT_DEX_EXPECTED_NAME` (checked before the ID) | `peer_rejected` |
| `peer_rejected_id` | session | the phone's peer ID differs from `ADAPT_DEX_EXPECTED_ID` | `peer_rejected` |
| `wifi_direct_connect_failed` | session | Windows could not read the request, or could not connect the Wi-Fi Direct group or list its endpoints | `connection_request` or `peer_admitted` |
| `wifi_direct_connect_timeout` | session | the group did not form within 20 seconds | `peer_admitted` |
| `no_ipv4_endpoint` | session | the group had no IPv4 endpoint pair | `peer_admitted` |
| `rtsp_connect_failed` | session | dialling the phone's port 7236 failed and no inbound connection from the phone came within 5 seconds, or the fallback listener failed | `wifi_direct_group` |
| `rtp_port_unavailable` | session | an RTP/RTCP UDP port pair could not be reserved | `rtsp_connected` |
| `control_connection_failed` | session | a read or write on the RTSP TCP connection failed (reset, 2-second write timeout) | `rtsp_connected` or later |
| `rtsp_protocol_error` | session | malformed or oversized RTSP, missing `CSeq` or `Session`, a repeated SETUP, a second input channel, no video format at PLAY, an empty presentation URL | `rtsp_connected` or later |
| `rtsp_request_rejected` | session | the phone answered one of this PC's requests with anything but `200` | `rtsp_connected` or later |
| `unsupported_video_format` | session | the phone selected a video format outside the accepted CEA set, sent a malformed one, or changed mode mid-session | `rtsp_connected` or `video_negotiated` |
| `input_channel_unavailable` | session | the phone did not offer a UIBC port with pointer and USB keyboard support | `playback_accepted` |
| `input_channel_failed` | session | connecting or writing the UIBC TCP channel failed, or its outgoing queue overflowed | `playback_accepted` or later |
| `decoder_init_failed` | session | the video worker could not start (Windows Runtime, Direct3D or the H.264 decoder) | `video_negotiated` |
| `decoder_failed` | session | decoding or presenting a picture failed | `video_negotiated` or later |
| `media_stream_error` | session | an RTP receive error, or invalid MPEG-TS from the phone | `video_negotiated` or later |
| `handshake_timeout` | session | no decoded frame within 90 seconds of the RTSP connection | before `first_frame_decoded` |
| `partial_message_timeout` | session | an RTSP message stayed incomplete for 10 seconds | any session stage |
| `control_idle_timeout` | session | 150 seconds with neither an RTSP message nor a new decoded frame, after the first frame | `first_frame_decoded` |
| `peer_disconnected` | session | the phone closed the control connection without TEARDOWN | any session stage |
| `peer_teardown` | session | the phone sent TEARDOWN: an orderly end, not a failure | any session stage |
| `user_stopped` | session | Disconnect, closing the window, `stop.request` or exiting the app ended the session: an orderly end, not a failure | any session stage |
| `internal_error` | either | anything not classified above, including a worker thread that panicked in a build that unwinds | any |
| `internal_panic` | either | the panic hook ran; a release build aborts immediately afterwards, so this is the last thing `status.json` says | any |

A phone that is refused by a filter leaves the receiver advertising and `phase_code` `ready`; the refusal is in `last_failure` and `last_session_end`.

### Stopping

Create a file named `stop.request` (any content) in the data directory. The window checks for it about once a second, then closes, ends the session and exits. Create it only after the new process's `status.json` exists: the receiver deletes a stale `stop.request` when it starts, and nothing else deletes it, so the supervisor should remove it once the process has exited. `running` becomes `false` and `last_session_end` is `user_stopped` if a session was active. Prefer this to killing the process: a normal shutdown runs the code that clears the Miracast sink mode, stops the publisher and closes the Wi-Fi Direct device, and killing the process skips it (whether Windows cleans up afterwards has not been verified here).

### Command line

| Option | Effect |
|---|---|
| (none) | start the receiver |
| `-V`, `--version` | print `dexdesk VERSION` and exit |
| `--print-data-dir` | print the data directory and exit |
| `--print-status-path` | print the full path of `status.json` and exit |
| `-h`, `--help` | print usage and exit |
| `--preview`, `--preview-settings`, `--smoke-ui` | see "Run" |

The informational options start nothing: no Wi-Fi Direct, no listener, no media and no window. They cannot be combined with other options. An unknown option is an error (exit code 2) and starts nothing. Exit codes: `0` normal, `1` the receiver could not start (see above), `2` a command-line error. A release build is a Windows GUI program: output goes to a redirected stdout, and otherwise to the console of the parent process.

## Requirements

- x64 Windows (the manifest targets Windows 10/11) with a Wi-Fi adapter and driver that support Wi-Fi Direct. Close other wireless-display receivers (including Windows' own Wireless Display) first: TCP 7236 must be free.
- A Samsung phone with wireless DeX. Whether firmware permits it is outside this program's control.
- Rust toolchain **1.99.1** (pinned in `rust-toolchain.toml`, minimal profile). The recorded offline test run used the `x86_64-pc-windows-gnu` host toolchain; other targets are untested. Crates are pinned by `Cargo.lock`.

## Build

From this directory:

```powershell
cargo build --release --locked
```

Optionally embed the manifest, icon, version info and the license texts as resources in the produced executable:

```powershell
cargo run --release --locked --bin resource_stamp -- target\release\dexdesk.exe app.manifest
```

`app.manifest` requests `asInvoker` (no elevation) and per-monitor-v2 DPI awareness. `resource_stamp` embeds `LICENSE` and `THIRD-PARTY-NOTICES.txt` so a distributed binary carries its notices; **if you redistribute a binary, keep those notices with it.**

Offline tests (they need the locked crates already in your Cargo cache):

```powershell
cargo test --locked --offline
```

There are **119 offline tests** (113 in the library, 6 in the executable): `transport_stream.rs` 25 (video and audio demux, damaged and hostile streams), `adapt_trace.rs` 23 (stages, reason codes, error classification, the status document, peer privacy), `protocol.rs` 14 (RTSP framing and limits, RTP reorder, video modes, UIBC packets), `receiver.rs` 14 (admission and filters, deadlines, response tracking, error codes), `runtime.rs` 15 (poison-proof locks, status writing, stop reasons, the session slot), `audio.rs` 12 (queue policy, render planning, sample conversion, the enable switch), `main.rs` 6 (argument parsing), `settings.rs` 4, `advertise.rs` 2, `input.rs` 2, `media.rs` 2. They do not start the receiver, open a socket, touch Wi-Fi Direct or involve a phone; the few that write files use a private directory under the system temporary directory. All 119 passed with `cargo test --offline`. See [validation](../../docs/validation.md) for broader validation limits. Re-run them after source changes. Building or passing tests is not a certification.

## Run

No configuration is required: start the receiver and choose this PC on the phone. To accept only one phone, set either or both optional variables first (a name must be at most 256 characters with no control characters).

```powershell
# optional: accept only a phone with this exact friendly name
$env:ADAPT_DEX_EXPECTED_NAME = "PHONE_FRIENDLY_NAME"
# optional: accept only the phone with this exact Windows peer ID
$env:ADAPT_DEX_EXPECTED_ID = "PEER_ID"
.\target\release\dexdesk.exe
```

Then, on the phone, choose this PC in Samsung DeX's wireless receiver picker.

| Setting | Purpose |
|---|---|
| `ADAPT_DEX_EXPECTED_NAME` | Optional. When set, only a phone with this exact friendly name is admitted |
| `ADAPT_DEX_EXPECTED_ID` | Optional. When set, only the phone with this exact Windows peer ID is admitted |
| `ADAPT_DEX_DATA_DIR` | Optional **absolute** directory for state; default `%LOCALAPPDATA%\DeXDesk` |
| `ADAPT_DEX_LOG_PEER_DETAILS` | Optional. `1`, `true`, `yes` or `on` writes the phone's name, peer ID and group addresses to the log; by default they are withheld (see "What is written to disk") |
| `--preview` | Show the window and settings without starting the receiver |
| `--preview-settings` | Preview with settings open immediately; no receiver or single-instance check |
| `--smoke-ui` | Preview that closes itself after about four seconds |
| `--version`, `--print-data-dir`, `--print-status-path`, `--help` | Print one thing and exit without starting anything (see "Command line") |

Files written under the data directory, and what they contain, are listed under "What is written to disk". Hotkey: **Ctrl+Alt+Esc** releases keyboard capture; **Alt+Tab** releases held keys and passes to Windows. A second normal launch focuses the running window. Preview modes skip the single-instance check.

### Using ADAPT with it

ADAPT can start, read and stop this receiver, but it never builds, bundles, downloads or searches for it. Build it with `scripts\build_receiver.ps1` from the repository root (`-DryRun` previews it; it runs `cargo build --release --locked` and prints the SHA-256) or with the commands above, then:

```text
adapt dex receiver-plan
adapt dex receiver-start --exe PATH_TO_DEXDESK_EXE --yes
adapt dex receiver-status
adapt dex receiver-stop --yes
```

`receiver-start` refuses without `--yes` (it advertises this PC over Wi-Fi Direct), refuses while another `dexdesk.exe` runs or TCP 7236 is taken, and passes `--expect-name`, `--expect-id` and `--audio` to this program only through `ADAPT_DEX_EXPECTED_NAME`, `ADAPT_DEX_EXPECTED_ID` and `ADAPT_DEX_AUDIO`. `receiver-status` applies the supervisor rules above to `status.json` (including a process-liveness check for a stale file), explains the recorded reason code, withholds its `message` text and never reads the log. `receiver-stop` writes `stop.request` only when `status.json` names a live `dexdesk.exe` and never ends a process by ID. See [DeX for PC](../../docs/dex-pc.md#driving-the-native-receiver-from-adapt) for the states and exit codes.

The Python workflow does not drive the Samsung side of it: with the phone connected to this receiver and a DeX desktop running, `adapt dex pc-probe -s SERIAL --yes` reports the Samsung state and display IDs, and `adapt dex pc-attach -s SERIAL --display DISPLAY_ID --yes` can open a **separate controllable scrcpy window** on a positively identified desktop display, leaving the receiver connection yours. Native receiver input and scrcpy input are different paths; test them separately. See [DeX for PC](../../docs/dex-pc.md) and the [architecture](../../docs/architecture.md#5-display-paths-scrcpy-generic-virtual-display-native-wireless-dex).

## Source map

| File | Role |
|---|---|
| `src/main.rs` | Entry point; parses arguments, loads settings, creates state, runs the UI |
| `src/ui.rs` | Win32 window, settings dialog, input capture, DWM styling |
| `src/receiver.rs` | Worker thread: admission, group formation, RTSP, RTP sockets, session loop, classification of how each attempt ended |
| `src/advertise.rs` | Wi-Fi Direct publisher/listener and the undocumented Miracast provider |
| `src/protocol.rs` | RTSP framing, WFD video format parsing, RTP reorder, UIBC packets |
| `src/transport_stream.rs` | MPEG-TS demux to H.264 frames |
| `src/media.rs` | Media Foundation decode and Direct3D 11 presentation |
| `src/input.rs` | Gesture synthesis (long press, scroll) and HID key mapping |
| `src/runtime.rs` | Shared state, logging with peer privacy, the status file, poison-proof locks, the panic hook |
| `src/settings.rs` | Settings, quality presets, data directory and the status/stop-request file names |
| `src/adapt_trace.rs` | Observation stages, reason codes, error classification, the `status.json` document, peer privacy |
| `src/bin/media_check.rs` | Developer tool: decode a `.ts` file in a window (`media_check FILE.ts [WIDTH HEIGHT FPS]`) |
| `src/bin/resource_stamp.rs` | Embeds manifest, icon, version and notices into the executable |

## Known boundaries

- Experimental and manual. Recorded observations, all on one Windows 11 PC with one Wi-Fi adapter, with Samsung devices. With one Android 13 phone: an earlier revision formed a group, decoded changing desktop frames and passed one controlled reconnect, and one instance of an earlier build stayed connected for about a day and a half. On 2026-11-14, with admission restricted by device name every time: the source as published in 0.1.1 (debug build) ran one session of about four minutes with that phone; the current source (uncommitted at the time) ran one session of about a minute and a half with that phone (debug build), one of about 48 minutes with an Android 17 phone (One UI 9, debug build) and a short one with an Android 16 tablet (One UI 8; the build profile is not recorded). In each, every stage through the first decoded frame completed within about five seconds and the device chose 1920x1080 at 30 FPS; window captures showed the desktop drawn correctly with the two phones (none was taken with the tablet). Stopping through `stop.request` (directly or through `adapt dex receiver-stop`) ended every current-source session where it was tried. Not observed: a reconnect on the current source, open admission with a device connecting, a session longer than about 48 minutes, a release build. That does not generalize to other devices, radios or Windows versions.
- Audio: in all three current-source sessions, with audio enabled, the device's stream carried an audio stream of the assumed type, its frames were accepted and the Windows output device opened at 48 kHz stereo, with a few underruns per minute (9 over the 48-minute session). On the Android 17 phone the owner heard the phone's sound from the PC and judged the audio quality great; that is one listener's judgement on one session, not a measurement. **Nobody listened** with the Android 13 phone or the Android 16 tablet.
- Input: on the Android 17 phone the owner confirmed that real mouse and keyboard use inside the window worked (which apps and for how long were not recorded). With the Android 13 phone, scripted clicks in the window produced touch events on the phone with matching timing, but a click on a desktop icon did not open the app, and the cause is not known; no real mouse or keyboard was used with that phone or with the tablet.
- Relies on an undocumented Windows provider; a Windows update can break it.
- Open admission by default; the optional name and peer ID restrictions are not cryptographic authentication beyond what Wi-Fi Direct itself provides. TCP 7236 is bound on every IPv4 interface (see "Security posture").
- The failure codes, the `wifi_radio_unavailable` / `wifi_direct_resource_in_use` split and the loss-of-advertisement report come from reading the code and the Windows API bindings. None of them was triggered in the recorded sessions; they have **not** been observed on a radio or a phone, and Windows does not say whether `wifi_radio_unavailable` means a missing adapter, a disabled one or Wi-Fi switched off.
- A Wi-Fi Direct peer that vanishes without closing its TCP connection is noticed only by the 90 s handshake and 150 s idle deadlines; the receiver does not watch the group's connection status.
- Not a replacement for Microsoft's supported Wireless Display receiver; `adapt dex pc-receiver-plan` describes that route.

Back to the [project README](../../README.md).
