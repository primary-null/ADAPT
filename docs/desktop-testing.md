# Android desktop and multi-display app testing

```text
adapt desktop inspect -s SERIAL --json
adapt desktop plan -s SERIAL --app com.example.app --size 1920x1080 --dpi 160
adapt desktop enable -s SERIAL --snapshot desktop.json --yes
adapt desktop start -s SERIAL --app com.example.app --yes
adapt dev-options restore -s SERIAL --snapshot desktop.json
```

Inspection reports API eligibility, advertised freeform/secondary-display
features, two developer settings and recognized logical displays. No SDK number,
feature flag or stored setting is treated as a working desktop shell or DeX.

`plan` validates and shows argv offline, without binary resolution or device
access. `start` requires `--yes`, observed API 29–37 and scrcpy 3.1+. It creates
one separate app test display at the requested resolution/DPI, using ADAPT's
selected ADB through the `ADB` environment variable. Existing mirrors remain
open. It does not install the app, force-stop it, modify settings, change
transport, restart ADB or invoke root. The required package avoids depending on
a secondary-display launcher. Some apps refuse or redirect secondary launches.

`--view-only` disables control but still launches the app/display after consent.
Audio is disabled. On normal close, scrcpy removes its display; the
`--no-vd-destroy-content` flag requests moving apps back to the phone. This flag
is available in 3.1+, though basic virtual displays appeared in 3.0. Process
creation reports `process_started` and stream health `not_verified`, not proven
rendering/input. Protected content can remain blank; app lifecycle/state can
change when moving displays.

`enable` is separate and optional. The `desktop-testing` profile requests `1`
for `global enable_freeform_support` and
`global force_desktop_mode_on_external_displays`. The existing transaction saves
original/absent values before writes, verifies readback, attempts partial-failure
rollback and offers conflict-aware restoration. Physical device/build/user
identity and a per-device lock protect recovery. It never reboots. A manual
reboot may be needed after enabling/restoring settings. A written value proves
storage only; OEMs may ignore it. The profile does not supply missing desktop
firmware, force app resizing or modify privileged ROM configuration overlays.

## Research and testing scope

Android 10 introduced developer-oriented multi-display/freeform desktop testing.
Its documented Force desktop workflow includes a reboot and simulated or physical
display. [AOSP testing guidance](https://source.android.com/docs/core/display/multi_display/testing-dev-environment)
describes it. ADAPT does not replace `overlay_display_devices` or reboot.

Connected-display desktop support reached general availability on supported
devices with Android 16 QPR3. [Android's release explanation](https://developer.android.com/blog/posts/android-devices-extend-seamlessly-to-connected-displays)
describes resizeable windows, taskbar and keyboard/mouse interaction. Android 17
adds per-display eligibility. [AOSP multi-window configuration](https://source.android.com/docs/core/display/multi-window)
and [desktop windowing](https://source.android.com/docs/core/display/desktop-windowing)
show that OEM configuration, display trust/capabilities, available space and
low-RAM status matter. Ordinary settings cannot make every virtual display
eligible for the native desktop shell.

scrcpy's [virtual-display documentation](https://github.com/Genymobile/scrcpy/blob/master/doc/virtual-display.md)
describes the separate app-display surface; it is useful even without a full
desktop shell. It is distinct from physical DisplayPort desktop sessions and
activating Samsung DeX. Compare layout/text at different sizes and densities,
state restoration, keyboard traversal, mouse hover/scroll, dialogs and returning
to the phone. Use actual desktop window controls to test window resizing where
available. Follow Android's [desktop app guidance](https://developer.android.com/develop/ui/compose/layouts/adaptive/support-desktop-windowing).

Enabling/starting remains explicit CLI work, never automatic in `everything` or
MCP. Checked 2026-11-12 with primary sources and offline tests. Physical desktop
rendering/input, Samsung DeX activation and native Debian acceptance remain
untested in this pass.
