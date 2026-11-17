# Android capability planning and inspection

Checked against primary sources on **2026-11-12**.

ADAPT's Android support model separates four claims:

- `unsupported_by_os`: the documented platform floor is above the reported SDK.
- `version_eligible`: the SDK meets the documented floor, but no device result is claimed.
- `unknown_oem_dependent`: the SDK alone is insufficient because the OEM build, host tool, display topology, permissions, or runtime state controls the result.
- `observed_runtime_evidence`: a caller explicitly supplied a successful feature observation. The normal inspection command does not manufacture this state from `ro.build.version.sdk`.

`unsupported_by_baseline` means the device predates ADAPT's Android 10/API 29 support baseline. `unknown_future_os` means the SDK is newer than the latest mapping verified here (Android 17/API 37); ADAPT does not guess the future Android name or feature behavior.

## Commands

The command group is registered in the main CLI:

```text
adapt android capabilities -s SERIAL
adapt android capabilities -s SERIAL --json
adapt android versions
adapt android versions --json
```

`capabilities` requires one explicit, complete ADB target. A spaced mDNS target is passed unchanged as one argv element after `-s`. The command never enumerates devices and never falls back to an implicit target.

The live inspection is bounded and read-only:

- runs the shared `doctor.preflight_adb_server` gate before contacting the target;
- reads `ro.build.version.sdk`, `ro.build.version.release`, `ro.product.manufacturer`, and `ro.product.model` with exact-target `getprop` calls;
- optionally inspects the local `scrcpy --version` as host-availability evidence;
- does not change settings, enable TCP ADB, pair, install, download, restart or kill ADB, or run a privileged command;
- returns generic classified errors without raw ADB output or the target string.

`versions` is entirely offline. Its Android 10-17/API 29-37 rows are planning data, not a claim that ADAPT has run native acceptance tests on every OS and OEM combination.

## Capability boundaries

USB ADB is version-eligible throughout the baseline, but requires user-enabled debugging, authorization, cable/port health, and host driver or udev access. A successful exact-target property read proves only that the selected ADB transport answered; it does not identify that transport as USB. Android's device-debugging guide documents USB debugging and notes that Developer Options placement can differ by device. [Android Developer Options](https://developer.android.com/studio/debug/dev-options)

Pairing-based wireless ADB begins on Android 11/API 30 for phones. Android 10/API 29 instead uses the documented USB-first legacy flow with `adb tcpip 5555`; this inspector reports that eligibility but never enables it. Android 17 adds ADB Wi-Fi 2.0, which also requires adb 37.0.0+ and mDNS service-version evidence. [Android Debug Bridge](https://developer.android.com/tools/adb)

ADB `screencap` is broadly available, but secure/protected windows can be blank or absent, so an SDK threshold cannot prove useful screenshot output. [AOSP screencap documentation](https://android.googlesource.com/platform/frameworks/base/+/84bf8073a8a80cf464eba1dada0eb7585f9943d5/docs/html/tools/help/shell.jd), [`FLAG_SECURE`](https://developer.android.com/reference/android/view/WindowManager.LayoutParams#FLAG_SECURE)

UI Automator is available from API 18 and can interact across apps, but it works from visible accessibility-backed UI. Custom-rendered or non-accessible content may expose limited or no nodes. [UI Automator legacy API](https://developer.android.com/training/testing/other-components/ui-automator-legacy)

scrcpy audio forwarding starts at Android 11. Android 11 must be unlocked when capture starts; Android 10 and earlier cannot provide scrcpy audio. Host version availability is not proof that capture works on a target. [Genymobile scrcpy audio](https://github.com/Genymobile/scrcpy/blob/master/doc/audio.md)

scrcpy can list and mirror an existing display with `--list-displays` and `--display-id`; controlling a secondary display requires Android 10+. A new virtual display uses `--new-display`, but launcher, system decorations, focus, app behavior, and OEM compatibility vary. The Android 15+ UHID pointer fix in scrcpy 3.3 is a narrower input-routing fact, not proof of all display input modes. [Genymobile display selection](https://github.com/Genymobile/scrcpy/blob/master/doc/video.md#display), [virtual displays](https://github.com/Genymobile/scrcpy/blob/master/doc/virtual-display.md), [scrcpy 3.3 release](https://github.com/Genymobile/scrcpy/releases/tag/v3.3)

These are generic Android/scrcpy display capabilities. They do **not** claim Samsung DeX support or treat a generic secondary/virtual display as DeX validation.

Developer Options locations and names can differ by device. AOSP contains `adb_enabled` and hidden `adb_wifi_enabled` settings, but SDK level alone does not prove an OEM exposes a key, permits shell reads, or accepts writes. This module therefore leaves key availability `unknown_oem_dependent`; the existing Developer Options inspector is the separate target-level read path. That inspector now also reports read-only maker detection (`pixel`, `samsung` or `other` from the maker and brand; the Samsung One UI version, which Samsung does not document; the Pixel build id and SoC; build type; page size; form factor hints; whether a device administrator is present) and lists which Developer Options profiles fit the phone (`adapt dev-options profiles`). Detection is a hint for gating and guidance, not proof that an option is honoured; other maker skins are not detected. See [Developer Options](dev-options.md). [Android Developer Options](https://developer.android.com/studio/debug/dev-options), [AOSP Settings keys](https://android.googlesource.com/platform/frameworks/base/+/HEAD/core/java/android/provider/Settings.java), [AOSP adbd settings flow](https://android.googlesource.com/platform/packages/modules/adb/+/refs/heads/main/docs/dev/how_adbd_starts.md)

## MCP access

`android_capabilities` exposes the same inspector with a required `device`
argument. `android_versions` exposes the offline matrix. Both are available
in read-only sessions. See [MCP setup and access](clients.md).

## Tests and acceptance boundary

Run:

```text
python -m pytest -q tests/test_android_support.py
```

The tests use mocked ADB, daemon-gate, and host-tool probes. They cover API 29-37 mapping, older/future SDKs, malformed SDKs, release mismatches, optional evidence, spaced mDNS preservation, exit-zero error text, denied/timeout cases, unsafe-daemon refusal, and clean JSON. No real device, emulator, USB stack, wireless network, OEM Developer Options implementation, audio path, secondary display, virtual display, or input route was accepted by these tests.

