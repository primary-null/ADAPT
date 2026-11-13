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

Recovery remains best-effort after crashes, cable loss, user/build changes or
other setting changes. The helper attempts cleanup on heartbeat loss; that is
not a guarantee after hard termination. `pc-restore` is an explicit overlay
recovery command and refuses a live PC-session lock. Loopback has no blind global
disconnect fallback. Retain snapshots and inspect the phone if cleanup is
unverified. Do not factory reset or unlock a bootloader to repair this workflow.

## Supported receiver alternative

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
It requires `--yes` even though it changes no display settings. `pc-attach`
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

The native source has an exact friendly-name admission gate and an optional
Windows peer identifier. Neither provides cryptographic device identity. It
records stages separately so advertisement, group formation, negotiation,
decoded frames and input-channel connection are not confused with one another.
The implementation uses an undocumented Windows advertisement-provider export;
compatibility with Windows updates and other radios remains experimental.

Build and run this receiver manually using its README. It is not included in
the Python wheel and `adapt dex pc-start` does not launch it. When the phone
has started a real desktop through the receiver, `pc-probe` and `pc-attach`
can inspect and attach to eligible existing layouts. Native receiver input
and scrcpy input are different paths and must be tested separately.

## Evidence and remaining acceptance

On one Galaxy Z Fold3 with Android 13, the native receiver formed a Wi-Fi Direct
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

All 15 offline native tests passed from the public source. The publication checks are recorded
in [validation](validation.md). Broader Android/One UI, radio, Windows, audio,
long-duration and Debian acceptance remains open. This is experimental source,
not universal DeX compatibility or a production release certification.

See [known limitations](known-limitations.md) and the
[architecture](architecture.md) for the current product boundary.
