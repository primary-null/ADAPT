# ADAPT Windows wireless receiver (experimental source)

A small native Windows receiver for wireless Samsung DeX, written in Rust. The Cargo package is named `dexdesk`; its window is titled **ADAPT Wireless DeX**. Samsung DeX still runs on the phone: this program is the **PC-side wireless-display sink** that the phone's DeX receiver picker can connect to, and it displays and controls that desktop in its own window.

> **Status: experimental, manual build.** The source is included for review and experimentation. It is **not part of the Python wheel**, **not launched by any `adapt` command**, and carries **no compatibility promise** across Android builds, Wi-Fi radios or Windows versions. Read [known limitations](../../docs/known-limitations.md) first.

## Provenance and license

This directory derives from the MIT-licensed upstream project [DeXDesk](https://github.com/adirh3/DeXDesk) at commit [`5070ccda5c363f5ed0b46d1fe7cf2c15c47fc125`](https://github.com/adirh3/DeXDesk/tree/5070ccda5c363f5ed0b46d1fe7cf2c15c47fc125). The upstream license is preserved in [`LICENSE`](LICENSE) ("Copyright (c) 2026 DeXDesk contributors") and dependency licenses in [`THIRD-PARTY-NOTICES.txt`](THIRD-PARTY-NOTICES.txt). The repository root [`LICENSE`](../../LICENSE) (MIT, ADAPT Contributors) and [`THIRD-PARTY-NOTICES.md`](../../THIRD-PARTY-NOTICES.md) cover the rest of the project. No Samsung software, firmware or proprietary client is included or redistributed.

Code in this tree that is specific to ADAPT includes `src/adapt_trace.rs` (observation stages), the peer admission rule in `src/receiver.rs`, and the `ADAPT_DEX_*` environment variables.

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
- **Video:** transport-stream demux, H.264 decode with Windows Media Foundation, Direct3D 11 presentation. Audio playback is not implemented.
- **Input:** UIBC with generic mouse and touch plus a USB HID keyboard. Right-click is a long press; the wheel becomes a smooth scrolling gesture.
- **Quality settings:** 1080p target 60 FPS (default), 1080p 30, 720p 60. Scroll speed 30-120, always-on-top, window size 640-8192 by 480-8192.

### Peer admission is a name gate, not authentication

The receiver admits a Wi-Fi Direct connection request only if the peer's friendly name **exactly equals** `ADAPT_DEX_EXPECTED_NAME` and, when `ADAPT_DEX_EXPECTED_ID` is set, its Windows peer ID equals that value. **Friendly-name binding is not cryptographic identity**: names can be chosen by anyone. Treat it as a guard against accidental connection, not as security.

### Observation stages

`status.json` records which stages were observed for the current attempt, so an advertisement is never mistaken for a working desktop: `Starting`, `Advertising`, `ConnectionRequest`, `PeerRejected`, `PeerAdmitted`, `WifiDirectGroup`, `RtspConnected`, `VideoNegotiated`, `PlaybackAccepted`, `InputChannelConnected`, `FirstFrameDecoded`. A new attempt starts with a clean set; a decoded frame does not certify input or Samsung DeX, and the file never records a device serial.

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

There are **15 offline tests**: `adapt_trace.rs` 3, `protocol.rs` 4, `transport_stream.rs` 3, `input.rs` 2, `receiver.rs` 2 (admission rule), `settings.rs` 1. They do not start the receiver, touch Wi-Fi Direct or involve a phone. All 15 passed again from the public source during publication checks; see [validation](../../docs/validation.md). Re-run them after source changes. Building or passing tests is not a certification.

## Run

The receiver refuses to start without an exact phone name (it must be non-empty, at most 256 characters, with no control characters).

```powershell
$env:ADAPT_DEX_EXPECTED_NAME = "PHONE_FRIENDLY_NAME"
# optional: exact Windows peer ID of the phone
$env:ADAPT_DEX_EXPECTED_ID = "PEER_ID"
.\target\release\dexdesk.exe
```

Then, on the phone, choose this PC in Samsung DeX's wireless receiver picker.

| Setting | Purpose |
|---|---|
| `ADAPT_DEX_EXPECTED_NAME` | **Required.** Exact friendly name the Wi-Fi Direct request must carry |
| `ADAPT_DEX_EXPECTED_ID` | Optional exact Windows peer ID |
| `ADAPT_DEX_DATA_DIR` | Optional **absolute** directory for state; default `%LOCALAPPDATA%\DeXDesk` |
| `--preview` | Show the window and settings without starting the receiver |
| `--smoke-ui` | Preview that closes itself after about four seconds |

Files written under the data directory: `settings.json`, `DeXDesk.log` (rotated to `DeXDesk.previous.log` at 512 KiB; may contain device names), `status.json`. Hotkey: **Ctrl+Alt+Esc** releases keyboard capture; **Alt+Tab** releases held keys and passes to Windows. A second launch focuses the running window.

### Using ADAPT with it

The Python workflow only observes and attaches; it does not start the receiver. With the phone connected to this receiver and a DeX desktop running, `adapt dex pc-probe -s SERIAL --yes` reports the Samsung state and display IDs, and `adapt dex pc-attach -s SERIAL --display DISPLAY_ID --yes` can open a **separate controllable scrcpy window** on a positively identified desktop display, leaving the receiver connection yours. Native receiver input and scrcpy input are different paths; test them separately. See [DeX for PC](../../docs/dex-pc.md) and the [architecture](../../docs/architecture.md#5-display-paths-scrcpy-generic-virtual-display-native-wireless-dex).

## Source map

| File | Role |
|---|---|
| `src/main.rs` | Entry point; loads settings, creates state, runs the UI |
| `src/ui.rs` | Win32 window, settings dialog, input capture, DWM styling |
| `src/receiver.rs` | Worker thread: admission, group formation, RTSP, RTP sockets, session loop |
| `src/advertise.rs` | Wi-Fi Direct publisher/listener and the undocumented Miracast provider |
| `src/protocol.rs` | RTSP framing, WFD video format parsing, RTP reorder, UIBC packets |
| `src/transport_stream.rs` | MPEG-TS demux to H.264 frames |
| `src/media.rs` | Media Foundation decode and Direct3D 11 presentation |
| `src/input.rs` | Gesture synthesis (long press, scroll) and HID key mapping |
| `src/runtime.rs` | Shared state, logging, status file |
| `src/settings.rs` | Settings, quality presets, data directory |
| `src/adapt_trace.rs` | Observation stages |
| `src/bin/media_check.rs` | Developer tool: decode a `.ts` file in a window (`media_check FILE.ts [WIDTH HEIGHT FPS]`) |
| `src/bin/resource_stamp.rs` | Embeds manifest, icon, version and notices into the executable |

## Known boundaries

- Experimental and manual; one physical Android 13 phone formed a group, decoded changing desktop frames and passed one controlled reconnect. That does not generalize to other phones, radios, Windows versions, long sessions or audio.
- Relies on an undocumented Windows provider; a Windows update can break it.
- Name-based admission only. Peer-to-peer radio traffic is not authenticated by this program beyond what Wi-Fi Direct itself provides.
- Not a replacement for Microsoft's supported Wireless Display receiver; `adapt dex pc-receiver-plan` describes that route.

Back to the [project README](../../README.md).
