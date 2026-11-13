# Bounded Samsung DeX display tooling

The existing-display commands in `adapt_adb.dex` inspect an explicitly selected Android device and can plan or
launch two scrcpy clients: one for logical display 0 and one for a
caller-selected, already-existing nonzero display. It does not start Samsung
DeX, create a virtual display, alter settings, reboot, disconnect, change ADB
transport, stop an existing mirror, or select an external display implicitly.

Separate experimental `dex pc-*` commands now provide activation trials,
supervised cleanup and API-based attachment to a real receiver session. See
[DeX for PC](dex-pc.md). Shell loopback activation was denied on the tested
Android 13 firmware and overlays did not activate DeX. A separate native Windows
wireless receiver subsequently established a real DeX session; its experimental
source is included under `native/windows-receiver`. These findings do not change
the conservative behavior of the existing-display commands below.

Broader hardware acceptance remains outstanding. The repository fixtures are
synthetic parser examples, not captured output from a Galaxy model, and the
offline test suite does not establish that simultaneous video, input routing,
encoders, firmware, cables, docks, displays, or wireless sinks work on any
particular device.

## Commands

The main CLI registers the `dex` group:

```text
adapt dex inspect -s SERIAL [--json]
adapt dex list-displays -s SERIAL [--json]
adapt dex plan-dual-mirror -s SERIAL --dex-display ID [--json]
adapt dex mirror -s SERIAL --dex-display ID [--yes] [--view-only] [--scrcpy PATH]
```

Every device command requires an explicit serial. ADAPT passes the complete
value as one argv element after `-s`, including mDNS serials containing spaces;
it never enumerates or guesses a target. Before the first ADB command, the
module uses ADAPT's shared-daemon ownership/version preflight. All inspection calls
are bounded, read-only `getprop` or `dumpsys` queries. Explicit mirror launch
starts scrcpy, which transfers/runs its server and can control the selected display.

`inspect` reads:

- `ro.product.manufacturer`
- `ro.product.model`
- `ro.build.version.sdk`
- `dumpsys desktopmode`
- `dumpsys display`

The desktop-mode parser recognizes only a narrow state shape containing one
each of `mCurrentState...enabled`, `mDualModeEnabled`,
`mStandaloneModeEnabled`, and `mDesktopDisplayId`. Missing, duplicated,
conflicting, denied, or newly formatted fields remain `unknown`. An active dual
state is confirmed only when a recognized Samsung desktop-mode state names an
existing nonzero logical display. Manufacturer text, a model name, a display
named "DeX", or the mere presence of a second screen never proves DeX.

The display parser reads IDs only from known blocks in the `Logical Displays`
section. It deliberately ignores physical `modeId`, `defaultModeId`, and
`supportedModes[].id` numbers, which are not logical display IDs. An unknown
format produces actionable `unknown` output instead of guessing.

Display IDs are dynamic. The caller must inspect them and pass the intended
existing nonzero ID. ID 0 is always rejected as the second target: standalone
DeX using display 0 cannot provide two distinct phone and DeX surfaces.

## Planning and launch boundary

`plan-dual-mirror` is side-effect-free apart from its read-only inspection. It
returns two separate argv arrays shaped like:

```json
[
  ["scrcpy", "-s", "SERIAL", "--display-id=0", "--no-audio"],
  ["scrcpy", "-s", "SERIAL", "--display-id=17", "--no-audio"]
]
```

The selected ID must currently exist. The plan says
`confirmed_active_samsung_dex` only when runtime desktop-mode evidence matches
that exact display. Otherwise it says
`external_display_candidate_unconfirmed`; it does not relabel a generic
external or virtual display as DeX. No argv contains `--new-display`, an
automatic TCP/IP option, or a hardcoded port. scrcpy manages its concurrent
session transport. Both clients use `--no-audio` as a conservative
duplicate-capture policy, not because two audio streams are asserted to crash.
Control is on by default; `--view-only` adds `--no-control` to both clients.
Window titles distinguish the phone display from the confirmed DeX display;
unconfirmed plans use an External label.

`mirror` is the only launching command. It refuses unless all of these are
true:

1. `--yes` was explicit;
2. Android is in the researched API 33-37 range;
3. the selected display exists and is not display 0;
4. recognized runtime evidence positively confirms active Samsung DeX dual
   mode on that exact ID; and
5. scrcpy 2.0 or newer is resolved from `--scrcpy PATH`, `PATH`, or an intact verified ADAPT-managed installation (2.0 is the
   minimum used here because the launch policy includes `--no-audio`).

The workflow minimum is Android 13/API 33. scrcpy separately documents generic
secondary-display control from Android 10; that broader capability is not this
DeX workflow. APIs above 37 are reported as `unverified_future`: inspection and
planning remain available, but launch is not treated as certified.

At launch, the environment variable `ADB` is set to ADAPT's selected ADB path,
which is scrcpy's documented override and avoids silently using a bundled or
different ADB client. The code never restarts or kills the ADB server. On
Windows it uses `CREATE_NO_WINDOW` to suppress console clutter while leaving
scrcpy's application windows intentional and visible.

Successful `Popen` calls are reported as `processes_started`, with healthy
streaming explicitly `not_verified`. If the second process cannot be created,
only processes created by that invocation are terminated; pre-existing mirror
processes are never searched for or stopped.

## MCP inspection

`dex_inspect` and `dex_displays` expose read-only inspection with a required
`device` argument. They honor MCP read-only and exact-device session policies.
They do not expose mirror launch or vendor DeX controls. See [MCP setup](clients.md).

## Official sources and claim limits

- [scrcpy video/display documentation](https://github.com/Genymobile/scrcpy/blob/master/doc/video.md)
  documents `--display-id`, `--list-displays`, and the Android 10 boundary for
  controlling a secondary display.
- [scrcpy virtual-display documentation](https://github.com/Genymobile/scrcpy/blob/master/doc/virtual-display.md)
  documents that `--new-display` creates a new virtual display. This tooling
  never uses that option and never presents it as Samsung DeX.
- [scrcpy FAQ](https://github.com/Genymobile/scrcpy/blob/master/FAQ.md)
  documents the `ADB` environment variable for selecting the ADB executable
  and avoiding mismatched ADB clients.
- [Samsung DeX support](https://www.samsung.com/us/support/answer/ANS10001955/)
  documents wired/wireless external-display use, standalone tablet behavior,
  device-dependent availability, and the DeX for PC limitation on One UI 7 and
  later.
- [Samsung DeX workspace support](https://www.samsung.com/us/support/answer/ANS10010217/)
  distinguishes standalone DeX on supported devices from DeX connected to an
  external wired or wireless display.

These sources establish feature semantics, not successful concurrent hardware
operation. No model whitelist is embedded, and no support promise is inferred
from model text.

## Exact offline tests

Run only this owned slice with:

```powershell
python -m pytest -q tests/test_dex.py
```

The tests mock every ADB query, scrcpy version probe, and process launch. They
cover Samsung and non-Samsung identity, API boundaries, denied or missing
desktop-mode service, active dual/standalone/off states, dynamic logical IDs,
malformed and conflicting fields, exact spaced serials, display-selection
refusals, absence of `--new-display` and fixed ports, default control and
explicit view-only behavior, confirmation without side effects, positive DeX
gating, ADAPT's ADB environment override, honest process health, and cleanup of
only invocation-owned startup processes. They perform no device access,
installation, download, lab action, or real mirror launch.
