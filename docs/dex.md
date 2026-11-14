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

Broader hardware acceptance remains outstanding. The fixtures in
`tests/test_dex.py` are synthetic parser examples. `tests/test_dex_real_firmware.py`
reconstructs the structure, not a verbatim capture, of `dumpsys desktopmode`
output observed on one Samsung phone; see
[Recognized desktop-mode shapes](#recognized-desktop-mode-shapes) for exactly what
that does and does not show. The offline test suite does not establish that
simultaneous video, input routing,
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

If none of the three identity properties gets an adb-level answer (for example
a stale serial, or a disconnected, offline, or unauthorized device), `inspect`,
`list-displays`, `plan-dual-mirror`, and `mirror` stop after those queries with
the `device_unreachable` error and exit status 1; the `dumpsys` queries are not
attempted. A device that answers but denies a query is reachable and still gets
a report. When the manufacturer property cannot be read, the report says so and
that Samsung DeX cannot be confirmed; it never calls the device non-Samsung.

The desktop-mode parser recognizes exactly two shapes of `dumpsys desktopmode`
output, described under
[Recognized desktop-mode shapes](#recognized-desktop-mode-shapes). Missing,
conflicting, denied, or newly formatted fields remain `unknown`. An active dual
state is confirmed only when a recognized Samsung desktop-mode state names an
existing nonzero logical display. When the `desktopmode` service does not exist
at all, a separate display-flag source can confirm DeX under stricter rules; see
[Display-flag evidence](#display-flag-evidence-service-absent). Manufacturer
text, a model name, a display named "DeX", or the mere presence of a second
screen never proves DeX.

The display parser reads IDs only from known blocks in the `Logical Displays`
section. It deliberately ignores physical `modeId`, `defaultModeId`, and
`supportedModes[].id` numbers, which are not logical display IDs. An unknown
format produces actionable `unknown` output instead of guessing.

Display IDs are dynamic. The caller must inspect them and pass the intended
existing nonzero ID. ID 0 is always rejected as the second target: standalone
DeX using display 0 cannot provide two distinct phone and DeX surfaces.

## Recognized desktop-mode shapes

The `dex` object of an `inspect` report carries `recognized_shape`, naming the
shape that produced the classification, or `null` when none matched (an
unreadable service, an empty or truncated dump, or an unrecognized format). A
reader can use it to tell whether a result came from the original synthetic
shape or from the firmware-observed one.

| `recognized_shape` | Origin of the shape | Can classify |
| --- | --- | --- |
| `synthetic_current_state` | Written from synthetic fixtures; never seen in real device output | active dual, active standalone, inactive |
| `observed_statemanager_semdesktopmodestate` | Observed on one Samsung phone, Android 13 / API 33 | active dual, inactive |

**Synthetic shape.** One each of `mCurrentState=DesktopModeState{... enabled=N ...}`,
`mDualModeEnabled=`, `mStandaloneModeEnabled=` and `mDesktopDisplayId=`. Enabled
state 4 with dual true, standalone false and a positive ID is active dual
evidence; enabled 4 with standalone true, dual false and ID 0 is active
standalone; any other enabled value with both flags false and ID -1 is inactive.
Anything else, including any repeated field, is `unknown`. A dump containing an
`mCurrentState=DesktopModeState{` line is read only as this shape.

**Observed shape.** Real firmware prints no `mCurrentState` line. It prints the
state in `Current <Name> state:` sections and prints the dual and standalone
flags twice, in two sections, with identical values. A real dump also begins
with roughly 250 lines of timestamped log history that contain `enabled=` and
`State=State{...}` inside log messages; those lines are never parsed. State is
read only from lines indented under a `Current <Name> state:` header, up to the
next such header or the next line at the header's indentation or shallower. The
`mState` line is read only from the `Current StateManager state:` section.

- Active dual evidence requires all of: the `StateManager` `mState=State{...}` line
  holds exactly one `SemDesktopModeState(ENABLED, ..., DISPLAY_TYPE_DUAL)` and a
  `desktopDisplay.N` token; `mDesktopDisplayId` is that same N and greater than
  0; every `mDualModeEnabled` line in the state sections says `true`; and every
  `mStandaloneModeEnabled` line says `false`.
- Inactive requires all of: the `mState=State{...}` line holds no
  `SemDesktopModeState` and no `DisplayInfo(` or `desktopDisplay.` content;
  `mDesktopDisplayId=-1`; and every dual and standalone flag says `false`.
- Anything else is `unknown`, and the issues say what conflicted or was missing:
  flags that disagree between sections, a malformed flag value, a
  `desktopDisplay.N` that does not match `mDesktopDisplayId`, an `mState` shape
  that is not recognized, an enabled state with a display type other than
  `DISPLAY_TYPE_DUAL`, or a standalone-looking state. Repeating a flag with
  identical values is not a conflict. Each flag and the display ID must appear in
  at least one state section; they need not appear in two.

The classification is evidence, not confirmation. As with the synthetic shape,
`inspect` confirms active dual mode only for a Samsung manufacturer identity and
only when the display ID is present in the observed logical displays and is not
display 0. On the observed shape that ID is the desktop display named by
`mDesktopDisplayId` and `desktopDisplay.N` (display 2, named "Desktop", in the
capture), not the separate wireless-sink display that the `DisplayInfo(...)` in
`mState` names. `list-displays`, `plan-dual-mirror` and `mirror` then use that
confirmation exactly as they do for the synthetic shape.

Evidence limits. The observed shape comes from one Samsung phone on
Android 13 / API 33: one capture during a real wireless DeX session in dual mode
and one with DeX idle. Standalone DeX (on the phone's own screen) and wired DeX
were not captured, so the observed shape never reports active standalone, and a
standalone-looking state stays `unknown`. Other One UI versions or models may
print something different; that yields `unknown` rather than a guess, and
`dex inspect` then cannot confirm a DeX display even while DeX is running.
`dex pc-probe` (see [DeX for PC](dex-pc.md)) asks the framework through a helper
instead of parsing this text. A match on one phone is not model-wide certification.

**One UI 8 and One UI 9 have no `desktopmode` service.** On a Samsung tablet on
Android 16 (One UI 8) and a Samsung phone on Android 17 (One UI 9), observed on
2026-11-14, `service list` contained no such service and `dumpsys desktopmode`
answered "Can't find service". On that firmware the service-based shapes above
can never match, so 0.1.1 reports `unknown` there; the development tree
(unreleased, not in 0.1.1) falls back to a second, separate evidence source,
described next. `workflow.status` still reads `eligible` there, which is an
API-level statement and not evidence that detection works. `dex pc-probe` was
also refused on the Android 17 phone during a live wireless DeX session
("Samsung helper returned unrecognized state"), with 0.1.1 and again with the
development tree.

## Display-flag evidence (service absent)

Used **only** when the `desktopmode` probe answered that the service does not
exist (a bare "Can't find service" style reply) and the manufacturer was read as
Samsung. It is never used when the service answered, whatever it said: an
inactive or unrecognized dump, an error, a denied query, an unreadable
manufacturer, a non-Samsung manufacturer, or a long dump that only happens to
contain the not-found phrase all keep their service-based result, and the flags
are not consulted. The flags come from the per-display `FLAG_*` names that
`dumpsys display` already lists for each logical display (the same flags `dex
list-displays` prints).

Rule, stricter than "the wireless flag is present":

- Confirmed active: exactly one logical display carries
  `FLAG_WIRELESS_DEX_DISPLAY`, it is not display 0, it also carries
  `FLAG_EXTERNAL_DEX_HOSTING`, and no other display carries either DeX flag. The
  DeX desktop is that display. The wireless flag alone is not enough: on the
  Android 13 phone the wireless sink display was not the display the desktop was
  drawn on, so a sink-only display cannot be called the desktop.
- `unknown`, each with a specific issue: more than one wireless-DeX display; the
  flag on display 0; the wireless flag without the hosting flag; a second display
  that also carries `FLAG_EXTERNAL_DEX_HOSTING`; `FLAG_EXTERNAL_DEX_HOSTING`
  without `FLAG_WIRELESS_DEX_DISPLAY` (possibly wired DeX; this combination has
  not been observed); or a display list that could not be read.
- No flagged display: the state stays `unknown`, never `inactive`. The reason
  says the desktopmode service is absent on this firmware and no logical display
  carries a DeX flag, so no active DeX display was observed, and that this does
  not show DeX is inactive.

A confirmation reports `classification` `active_wireless_dex_display_flag`,
`recognized_shape` `dumpsys_display_flag_wireless_dex_display`, `desktop_display_id`
and `confirmed_display_id` set to the display, and `service_status` still
`unknown` (the service itself was never read). Whenever the rule was evaluated, the
`dex` object also carries `display_flag_evidence`: the source
(`dumpsys_display_flags`), `desktopmode_service: "absent"`, where the flags were
read, the outcome, the displays carrying a DeX flag, what the source was observed
on, and what was not observed. The confirmation reason states the same.

`list-displays` then reports the display as `confirmed_dex_display_id` with
`confirmed_dex_evidence_source: "dumpsys_display_flags"`, and `plan-dual-mirror`
and `mirror` accept it exactly like a service-confirmed display. Dual mirroring
means display 0 (the phone screen) plus the display carrying the DeX desktop; on
this layout the desktop is drawn directly on the flagged external display, so
that is two distinct existing surfaces and nothing in the plan assumes a virtual
display. The plan is labelled `confirmed_active_samsung_dex_display_flag` and its
notes repeat the evidence limits. Display 0, a missing display and an API outside
33-37 are refused as before.

Evidence limits. The flag layout was first seen on **one** Samsung phone on
Android 17 (One UI 9) during **one** live wireless DeX session started from the
phone's "Wireless DeX" tile to ADAPT's native receiver on a Windows 11 PC.
During it the receiving display (id 6, 1920x1080, named after the PC) carried
`FLAG_WIRELESS_DEX_DISPLAY`, `FLAG_EXTERNAL_DEX_HOSTING` and `FLAG_WIFI_DISPLAY`,
and the DeX desktop was drawn directly on it, with no separate virtual "Desktop"
display. With DeX idle the same phone listed only display 0.

The rule itself, running in the development tree on 2026-11-14, was then
observed on two devices, each during one wireless DeX session to that receiver
on the same PC:

- the Android 17 phone (One UI 9): `dex inspect` confirmed DeX on display 6
  with classification `active_wireless_dex_display_flag`, `dex list-displays`
  reported `confirmed_dex_display_id: 6` with evidence source
  `dumpsys_display_flags`, and `dex plan-dual-mirror --dex-display 6` produced
  a plan for displays 0 and 6. Nothing was launched;
- a Samsung tablet on Android 16 (One UI 8): `dex inspect` confirmed DeX on
  display 6 from `FLAG_WIRELESS_DEX_DISPLAY` with `FLAG_EXTERNAL_DEX_HOSTING`.
  `list-displays` and `plan-dual-mirror` were not run, and no capture of the
  tablet's desktop was taken.

With DeX idle, 0.1.1's `list-displays` listed only display 0 on both devices.
The report's `observed_on` and `not_observed` fields name both devices; during
the recorded sessions they still named only the phone. **Not observed:** wired DeX,
standalone DeX, `dex mirror`, and whether scrcpy can capture a flag-confirmed
display on this firmware; a launch would still not be a health claim. Two
devices are not model-wide certification.

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
that exact display, or `confirmed_active_samsung_dex_display_flag` when the
display-flag source did (service absent). Otherwise it says
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
   mode on that exact ID (from the `desktopmode` service, or, only when that
   service is absent, from display flags); and
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
python -m pytest -q tests/test_dex.py tests/test_dex_real_firmware.py tests/test_dex_display_flag_evidence.py
```

`tests/test_dex_display_flag_evidence.py` covers the display-flag source: fixtures
built from the structure of the Android 17 lines with invented names; confirmation
through `inspect`, `list-displays`, `plan-dual-mirror` and `mirror`; every
`unknown` outcome (no flagged display, two flagged displays, the flag on display 0,
hosting without the wireless flag, the wireless flag without hosting, a second
hosting display, an unreadable display list); the service result always winning
(inactive, unrecognized, denied, errored, and a long dump containing the
not-found phrase); non-Samsung and unreadable manufacturers; an unreachable
device; and the unchanged Android 13 results.

`tests/test_dex_real_firmware.py` covers the observed shape: active dual and idle
fixtures built from the structure of the real output, with timestamped history
lines that must be ignored; confirmation through `inspect`, `list-displays`,
`plan-dual-mirror` and `mirror`; disagreeing or malformed flags, a mismatched
`desktopDisplay.N`, other display types and states, history-only, empty and
truncated dumps; non-Samsung and unreadable manufacturers; and the unchanged
synthetic shape.

The tests mock every ADB query, scrcpy version probe, and process launch. They
cover Samsung and non-Samsung identity, API boundaries, denied or missing
desktop-mode service, active dual/standalone/off states, dynamic logical IDs,
malformed and conflicting fields, exact spaced serials, display-selection
refusals, absence of `--new-display` and fixed ports, default control and
explicit view-only behavior, confirmation without side effects, positive DeX
gating, ADAPT's ADB environment override, honest process health, and cleanup of
only invocation-owned startup processes. They perform no device access,
installation, download, lab action, or real mirror launch.
