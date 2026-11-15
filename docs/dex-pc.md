# Experimental DeX for PC

ADAPT can try to activate a Samsung desktop and mirror it in a controllable
scrcpy window. This is separate from generic Android app displays and from the
older `dex mirror` command, which only mirrors an existing confirmed desktop.
No method is treated as universally supported by an Android/One UI version.

```text
adapt dex pc-plan -s SERIAL --method loopback --json
adapt dex pc-start -s SERIAL --method loopback --snapshot NEW_SESSION.json --yes
adapt dex pc-start -s SERIAL --method overlay --snapshot ANOTHER_SESSION.json --yes
adapt dex pc-status --snapshot SESSION.json --json
adapt dex pc-stop --snapshot SESSION.json
adapt dex pc-restore -s SERIAL --snapshot SESSION.json --yes
```

The supported planning range is Samsung Android 13â€“17/API 33â€“37. This is an
eligibility boundary, not certification. `pc-plan` is offline. `pc-start` requires
`--yes`, host shared-ADB preflight and an exclusive new recovery snapshot bound
to physical serial/build/current user before activation. Existing simulated,
non-internal, wireless or DeX displays are not replaced. Foldable internal
screens are recognized as internal displays rather than assuming every nonzero
ID belongs to an external monitor.

## Activation methods

**Loopback** uses ADAPT's source-bundled, locally compiled helper under Android's
shell UID. It requests a Samsung wireless display connected to phone loopback,
implements the local receiver handshake, and holds it while the supervisor sends
heartbeats. This requires Samsung API availability **and** vendor permission.
The helper never grants that permission, invokes root or changes SELinux. It
does not preemptively disconnect another receiver. Disconnect cleanup requires
both the active display name and address to match its invocation token. The
standard WFD control socket and dynamically allocated RTP pair stay on phone
loopback; no ADB server port or network service is exposed by ADAPT.

**Overlay** requests a system simulated display with
`overlay_display_devices=WIDTHxHEIGHT/DPI,hdmi`. Size/DPI apply to this backend;
loopback resolution is negotiated by firmware. The original absent/`none` value
is saved first. Restore checks identity, current value and readback, and refuses
an externally changed setting. It does not apply other developer profiles or
force a Samsung launcher to masquerade as an enabled DeX service.

Neither method launches scrcpy without positive enabled/dual-display Samsung
state and a matching newly created external display. An overlay must also have
an `overlay:` identity; a concurrently connected physical monitor is not claimed
as ADAPT's simulated display. A generic, blank or unconfirmed display is a failed
activation rather than a working DeX desktop.

On success, a hidden host supervisor owns one scrcpy process and its helper.
Input is enabled unless `--view-only` is explicit; audio is disabled. Existing
phone mirrors and the ADB server remain alone. Closing this owned window or
`pc-stop` requests teardown and recovery. A kernel file lock serializes PC
sessions/recovery per physical phone independently of normal Developer Options
profiles. No saved PID is used to kill another process.

`pc-status` reports the last saved supervisor state, not proof that it is still
alive or that video/input works. Process startup is not visual acceptance. A
stop with no acknowledgment reports `stop_pending`, not completed cleanup.
Snapshots and worker/scrcpy logs are private local files and can contain device
identifiers. Normal window closure can move or destroy app activities according
to OEM display behavior; save work before ending a desktop session.

A refused, failed, `cleanup_conflict` or `stop_pending` result also names a
failure guide: on stderr after the report, or as an additive `failure` object
with `--json` (see [help](help.md#failure-guidance)). A failed session records
the guide's ID in its session file, so `pc-status` names it as well. For example,
a denied loopback activation is `adapt explain dexpc.loopback_denied`, which
points at `adapt dex receiver-plan`.

Recovery remains best-effort after crashes, cable loss, user/build changes or
other setting changes. The helper attempts cleanup on heartbeat loss; that is
not a guarantee after hard termination. `pc-restore` is an explicit overlay
recovery command and refuses a live PC-session lock. Loopback has no blind global
disconnect fallback. Retain snapshots and inspect the phone if cleanup is
unverified. Do not factory reset or unlock a bootloader to repair this workflow.

## Supported receiver alternative

ADAPT knows two PC-side receiver routes: Microsoft's Wireless Display feature,
described here, and its own experimental native receiver, described under
[Native Windows wireless receiver](#native-windows-wireless-receiver) below.
`adapt dex receiver-plan` compares them.

Where vendor shell activation is denied, use a real display receiver and attach
to the desktop that Samsung starts normally:

```text
adapt dex pc-receiver-plan --json
adapt dex pc-probe -s SERIAL --yes --json
adapt dex pc-attach -s SERIAL --display OBSERVED_ID --yes
```

On Windows, install Microsoft's **Wireless Display** optional feature, launch its
receiver, and choose this PC from the phone's Samsung DeX wireless receiver
picker. This may require Windows administrator approval and Android/receiver
connection approval. `pc-receiver-plan` only explains the steps; it installs
nothing. The repository's reviewed
`scripts/install_windows_wireless_display.ps1` installs only
`App.WirelessDisplay.Connect~~~~0.0.1.0` through Windows servicing after elevation,
writes a receipt and never initiates a reboot. Run the installation only after reviewing the script and approving Windows elevation.

`pc-probe` transfers/runs the integrity-checked helper temporarily to observe
Samsung's current desktop API state and logical IDs, then removes its own JAR.
It requires `--yes` even though it changes no display settings. Its output
includes each display's unique ID; with 0.1.1, on one Android 13 phone, the
Wi-Fi display's ID contained a hardware (MAC) address, so review it before
sharing. The development tree (unreleased) replaces MAC-style addresses in the
report with `<hardware-address>`; that has offline tests only. On one Android 17
phone (One UI 9) it refused with "unrecognized state" during an active wireless
DeX session, with 0.1.1 and with the development tree. `pc-attach`
requires the caller-selected nonzero ID to match an observed active Samsung
dual DeX session. It accepts either a sole external physical/Wi-Fi display, or
the live-verified API 33 layout: one system-owned `virtual:android,1000,Desktop,N`
compositor plus exactly one Wi-Fi sink, with Wi-Fi display connected. The latter
selects the compositor, not the sink. Generic overlays, extra displays and
unverified API versions remain refused. It rechecks device/build/
user binding before launching its controlled scrcpy window. This external
receiver session remains user-owned: closing the mirror does not disconnect it.
Actual receiver/firmware/driver pairing and input still need runtime acceptance.

See [Microsoft's receiver setup](https://support.microsoft.com/en-us/windows/hardware/display-graphics/screen-mirroring-and-projecting-to-your-pc-or-wireless-display)
and [Samsung's DeX guidance](https://www.samsung.com/us/support/answer/ANS10001955/).
This route does not reinstate Samsung's retired Windows client or redistribute
Samsung software. The active ADAPT work targets a software wireless receiver
on Windows, with DeX displayed and controlled in its own PC window. External
monitors, docks and dummy plugs are outside this feature's current scope.

## Native Windows wireless receiver

The experimental native receiver source is included in
[`native/windows-receiver`](../native/windows-receiver/README.md). It derives
from the MIT-licensed [DeXDesk revision
5070ccda5c363f5ed0b46d1fe7cf2c15c47fc125](https://github.com/adirh3/DeXDesk/tree/5070ccda5c363f5ed0b46d1fe7cf2c15c47fc125),
with the upstream license and dependency notices preserved.

This receiver replaces the PC-side wireless-display implementation: Windows
Wi-Fi Direct advertisement and group formation, RTSP/RTP negotiation, Windows
Media Foundation video decoding, Direct3D display and UIBC input. Samsung DeX
still runs on the phone. A generic virtual display is a separate testing tool.
No Samsung desktop client or proprietary firmware is redistributed.

Admission is open by default: any Wi-Fi Direct device in radio range that
requests a connection is admitted, with no prompt and no pairing code. Optional
restrictions by exact friendly name and by Windows peer ID narrow that. Neither
is authentication: a device can announce any name, and a peer ID is only as
secret as the radio traffic that carries it. The receiver records stages
separately so advertisement, group formation, negotiation, decoded frames and
input-channel connection are not confused with one another. The implementation
uses an undocumented Windows advertisement-provider export; compatibility with
Windows updates and other radios remains experimental. The receiver README
holds its security posture, status contract and recorded observations.

### Driving the native receiver from ADAPT

These commands are unreleased changes in this repository and are not in the
0.1.1 download.
ADAPT does not build, bundle, download or search for the receiver, and it is not
in the Python wheel. Build it yourself from a source checkout
(`scripts/build_receiver.ps1`, or the commands in the
[receiver README](../native/windows-receiver/README.md)), then:

```text
adapt dex receiver-plan --json
adapt dex receiver-start --exe PATH_TO_DEXDESK_EXE --yes
adapt dex receiver-start --exe PATH_TO_DEXDESK_EXE --expect-name PHONE_NAME --audio --yes
adapt dex receiver-status --json
adapt dex receiver-stop --yes
```

- `receiver-plan` is offline. It compares the two routes and lists the
  prerequisites, the build command, the security posture, the data directory and
  the follow-up commands. It checks nothing on this PC: Wi-Fi Direct support,
  an installed Rust toolchain and a free TCP 7236 are not observed.
- `receiver-start` runs the one file you name, which must exist and be called
  `dexdesk.exe`, as a detached process, and returns once the receiver's own
  `status.json` shows it advertising (a bounded wait of 30 seconds). It refuses
  without `--yes` because starting advertises this PC over Wi-Fi Direct and opens
  TCP 7236 on every IPv4 interface. It also refuses outside Windows, while any
  `dexdesk.exe` process is running (the receiver is single-instance) and when
  TCP 7236 cannot be bound, and it names which of those applies. Settings reach
  the receiver only through its documented environment variables
  (`ADAPT_DEX_EXPECTED_NAME`, `ADAPT_DEX_EXPECTED_ID`, `ADAPT_DEX_AUDIO`,
  `ADAPT_DEX_DATA_DIR`); the command line holds only the executable path, and a
  phone name or ID is never printed back. A restriction already set in your
  environment is kept and reported as coming from there. With no restriction the
  report says that open admission is in effect. If the receiver does not
  advertise in time the result is `start_pending`, and ADAPT leaves the process
  alone.
- `receiver-status` is read-only. It reads `status.json` from the data directory
  (`--data-dir`, else `ADAPT_DEX_DATA_DIR`, else `%LOCALAPPDATA%\DeXDesk`, shown
  symbolically) and checks that the recorded process is still a live
  `dexdesk.exe`. A file whose process is gone, or that has not been updated for
  10 seconds while its process is alive, is reported as stale. The last failure's
  reason code is explained in plain words with possible causes (never asserted
  as fact) and ordered next steps. The receiver's own message text is withheld,
  because it can echo values the phone sent, and its log file is never read.
- `receiver-stop` requests a graceful stop by creating `stop.request` in the data
  directory, and only when `status.json` there names a live `dexdesk.exe` that
  has not begun shutting down. It waits up to 20 seconds, removes the request
  after the process exits, and reports the final recorded state. It never ends a
  process by ID; an unconfirmed stop is `stop_pending` and the request stays in
  place.

| State | Meaning | `receiver-status` exit |
|---|---|---|
| `not_running` | no status file, or a recorded shutdown, or a stale file | 0 |
| `starting` | the process is running and not yet advertising | 0 |
| `ready` | advertising and waiting for a phone | 0 |
| `connecting` | a phone's attempt is in progress, no picture yet | 0 |
| `streaming` | a phone is connected and frames are being decoded | 0 |
| `stopping` | shutdown has begun | 0 |
| `not_working` | running but not advertising, with a receiver-level failure recorded | 2 |
| `stale` | the process is alive but its status file is out of date | 2 |
| `unverified` | a `dexdesk.exe` process exists but this data directory has no status for it | 2 |
| `unreadable` | the status file is corrupt, malformed or from a newer schema | 1 |

`receiver-start` exits 0 when it observed the receiver advertising, 1 when it
refused or the receiver failed or exited, and 2 for `start_pending`.
`receiver-stop` exits 0 when the receiver stopped or nothing was running, 1 when
it refused, and 2 for `stop_pending`.

Each of these outcomes that is not a success also names a failure guide: on
stderr after the report, or as an additive `failure` object with `--json` (see
[help](help.md#failure-guidance)); existing keys and exit codes do not change.
What ADAPT itself observed or refused is a `dexpc.native_receiver.*` guide (for
example `dexpc.native_receiver.port_busy` when TCP 7236 is taken before a start),
and a reason code the receiver recorded is its `dexpc.receiver.CODE` guide. A
report that exits 0 but records a failure, such as a receiver that exited
without recording a shutdown, adds one `More: adapt explain ID` line. This is
covered by offline tests only; no such failure has been observed on hardware.

When `receiver-status` reports `streaming`, the next steps are the existing
`pc-probe` and `pc-attach` commands with their existing caveats: `pc-probe`
transfers and runs the bundled helper temporarily and needs `--yes`, and
`pc-attach` opens a separate controllable scrcpy window only on a display the
probe positively identified, leaving the receiver connection yours. Native
receiver input and scrcpy input are different paths and must be tested
separately.

These commands report what the receiver recorded and what the process list
showed; they do not contact the phone. An advertising receiver is not a working
desktop, a decoded frame does not certify input, audio or Samsung DeX, and the
mouse-and-keyboard channel being connected is not proof that input works. The
`pc-start` supervisor does not launch this receiver.

## Evidence and remaining acceptance

On one Samsung phone with Android 13, the native receiver formed a Wi-Fi Direct
group, negotiated 1920x1080 at a target 30 FPS and decoded changing desktop
frames. The first session recorded 18,749 decoded frames over 626 seconds;
these are observations, not a frame-rate or latency benchmark. Samsung API
state confirmed enabled dual DeX. The firmware used a system-owned virtual
Desktop compositor plus a separate Wi-Fi sink. Display IDs changed across
connections and must always be discovered again.

After a parent-window keyboard-routing fix, a controlled receiver replacement
and one reconnect passed. Native desktop Calculator input and phone Settings
input remained separate in that sequence. Earlier phone blur also appeared
in direct Android capture and cleared at Home; its root cause is unresolved.
No phone image or raw private capture is included in this repository.

Shell loopback activation on this firmware was denied by
`CONFIGURE_WIFI_DISPLAY`, and a simulated overlay did not start genuine DeX.
The native receiver succeeded separately; that does not establish why the
stock Windows receiver's earlier Wi-Fi Direct provisioning timed out.

On 2026-11-14, with one Windows 11 PC and one Wi-Fi adapter, more receiver
records were made (details in [validation](validation.md)). An earlier build,
not the published source, was found holding one session with an Android 13
phone that had lasted about a day and a half. The 0.1.1 source (debug build,
admission restricted by name) held one session of about four minutes with that
phone: every stage through the first decoded frame within about four seconds,
1920x1080 at 30 FPS, the desktop drawn without corruption. The development-tree
source with audio on, admission restricted by name, held a session of about 90
seconds with that phone, one of about 48 minutes with an Android 17 phone (One
UI 9) and a short one with an Android 16 tablet (One UI 8); on both newer
devices the quick-settings tile is named "Wireless DeX" and opens a device
picker. Each device chose 1920x1080 at 30 FPS and the audio output opened at
48 kHz stereo. On the Android 17 phone the owner confirmed that real mouse and
keyboard input in the receiver window worked and that the phone's sound was
heard from the PC, and judged the audio quality great; that is one listener on
one session, whose log recorded 9 audio underruns and 653 video recovery events
over about 48 minutes. Nobody used a real mouse or listened with the Android 13
phone or the tablet; with the Android 13 phone scripted clicks reached the phone
as touches, but a click on a desktop icon did not open the app, and why is not
known. No reconnect, session longer than about 48 minutes, open admission with
a device connecting, failure reason code or release build was observed on the
current source. The `receiver-*` commands were run against the real receiver
with no phone (start refused without `--yes` and refused a second start, and
status and stop worked), and later `receiver-status` reported the Android 17
phone and the tablet streaming, `receiver-stop --yes` ended both sessions, and
`receiver-start --expect-name … --audio --yes` started the tablet session.
During those sessions the development tree's `dex inspect` confirmed DeX from
display flags on both newer devices ([DeX](dex.md#display-flag-evidence-service-absent)),
while `pc-probe` was still refused on the Android 17 phone.

The receiver's offline tests and what they cover are listed in the
[receiver README](../native/windows-receiver/README.md); the publication checks
are recorded in [validation](validation.md). Broader Android/One UI, radio, Windows, audio,
long-duration and Debian acceptance remains open. This is experimental source,
not universal DeX compatibility or a production release certification.

See [known limitations](known-limitations.md) and the
[architecture](architecture.md) for the current product boundary.
