# Bounded Developer Options

ADAPT exposes a deliberately bounded Developer Options surface. It reads a fixed
allowlist and mutates only fixed profiles, each built from allowlisted entries:
Settings Provider keys, `debug.*` system properties and a handful of command
pairs such as `wm density` and `cmd battery`. It does not use raw Binder calls,
poke SystemProperties, change USB configuration, grant privileges, install
software, or select a device implicitly.

Nothing described here has been run on a device by ADAPT beyond the one
recorded `touches` cycle in [known limitations](known-limitations.md). The
profiles, parsers and guidance are *offline-tested* against a fake shell; the
read output formats were written from AOSP source reading, not captured from a
phone. A device run comes next.

The command group is registered in the main `adapt` CLI. Shared-daemon preflight runs before device queries.

Every device command requires a nonblank explicit ADB serial and targets exactly
that transport with `adb -s SERIAL`. ADAPT separately reads `ro.serialno`,
`ro.build.fingerprint`, and the current Android user. A USB serial, emulator
endpoint, or wireless `host:port` is never treated as physical device identity.
If the on-device identity is unavailable, mutation and restoration are refused.
Every device command ADAPT runs here must match an allowlisted argv shape in
`safety.py`; anything else is refused before it reaches adb.

## Commands

```text
adapt dev-options inspect -s SERIAL [--json] [--all]
adapt dev-options profiles -s SERIAL [--package PKG] [--json]
adapt dev-options apply --profile NAME -s SERIAL --snapshot PATH [--package PKG] [--option NAME]...
adapt dev-options restore -s SERIAL --snapshot PATH [--accept-uncertain]
adapt dev-options catalog [--json] [--layer aosp|pixel|samsung|adb_control] [--api N]
adapt dev-options pixel-16kb-check -s SERIAL [--json]
```

`inspect`, `profiles` and `pixel-16kb-check` read the phone and change nothing.
`catalog` is offline and contacts no phone.

## Inspect

```text
adapt dev-options inspect -s SERIAL
adapt dev-options inspect -s SERIAL --json
adapt dev-options inspect -s SERIAL --all
```

Inspection reads these settings:

- Developer mode, USB debugging, and wireless debugging
- Charging-dependent stay-awake bitmask
- Window, transition, and animator duration scales
- Show touches and pointer location for the current Android user
- Don't keep activities and show background ANRs
- Freeform-window support and force desktop on external displays

Each result is reported as `observed`, `missing`, `denied`, `malformed`,
`timeout`, or `adb_error`. An observed Settings Provider value is not proof that
an OEM exposes, supports, or currently honors the corresponding Developer
Option. JSON output is plain JSON with no ANSI decoration.

### Detection block

`inspect` (and the MCP `developer_options_inspect` tool, which stays read-only)
also returns a `detection` block, read with allowlisted property reads:

| Field | Source | Meaning |
|---|---|---|
| `oem` | `ro.product.manufacturer`, `ro.product.brand` | `pixel` (Google maker and brand), `samsung` (either says Samsung) or `other`. An emulator image that reports Google is labelled `pixel`; `emulator_hint` is set for `emulator-*` serials |
| `samsung.version` | `ro.build.version.oneui` | One UI version decoded from the five-digit value (`60101` is 6.1.1). **Samsung does not document this property**; treat it as a hint |
| `samsung.fallback_sep`, `fallback_sem` | `ro.build.version.sep`, `ro.build.version.sem` | Raw values when the One UI property is missing; not mapped to a One UI version |
| `samsung.estimated_major` | Android API level | One UI major version estimated from the API level when the property is missing (also undocumented) |
| `samsung.auto_blocker_applies` | One UI 6 or later | See [Samsung](#samsung-one-ui) |
| `pixel.build_id`, `pixel.soc_model` | `ro.build.id`, `ro.soc.model` | Tells Pixel quarterly releases apart; they share an API level |
| `build_type` | `ro.build.type` | `user`, `userdebug` or `eng` |
| `page_size` | `getconf PAGE_SIZE` | 4096 or 16384 |
| `form_factor` | `ro.build.characteristics`, `pm list features` | Hints only: tablet, hinge sensor, freeform windows, secondary displays, picture-in-picture, TV, watch, automotive |
| `device_admin` | `dumpsys device_policy` | Counts only (device owner, profile owner, active administrators); the text names organizations and apps, so none of it is kept. A false value means none was recognised, not that none exists |
| `transport` | the serial's form | `usb`, `tcp` (wireless debugging or `host:port`) or `local` (emulator). ADAPT cannot prove a transport is USB |
| `unread` | | Each read that failed, with its failure kind |

Detection is a hint for gating and guidance, not proof that an option is honoured.
Other maker skins (Xiaomi, OPPO, OnePlus, Motorola) are not detected.

### `--all`

`inspect --all` also reads every catalog entry that has an allowlisted read
command for this phone's API level and maker layer: `settings get`, `getprop`,
`wm density|size`, `cmd uimode night`, `dumpsys battery`, `dumpsys deviceidle get
deep`, `cmd netpolicy get restrict-background` and `getconf PAGE_SIZE`. Entries
whose read needs a pipe, a package or an unlisted command are skipped with a
reason. Reads are bounded: 10 seconds per command, 60 seconds in all, 256
characters per value. The result is in `catalog_reads`.

## Profiles

```text
adapt dev-options profiles -s SERIAL
adapt dev-options profiles -s SERIAL --json
adapt dev-options profiles -s SERIAL --package com.example.app
```

`profiles` lists every profile with its API range, maker layers, package need,
mechanisms and whether this phone is eligible, with the reason when it is not.
Eligibility is computed from read-only facts (API level, maker layer, transport,
the package you pass). The registry the listing reads (`dev_profiles.py`) is also
what the MCP schema derives its profile enum from.

| Profile | API | Package | Mechanisms | Needs after apply | Restore |
|---|---|---|---|---|---|
| `fast-ui` | any | none | settings | | exact |
| `touches` | any | none | settings | | exact |
| `stay-awake` | any | none | settings | | exact |
| `desktop-testing` | 29-37 | none | settings | manual reboot may be needed | exact |
| `layout-debug` | 29-37 | none | property, setting | app restart; effect check | exact |
| `lifecycle-stress` | 29-37 | none | settings | effect check | exact |
| `performance-capture` | 29-37 | **required** | command, setting | | **approximate** (compile) |
| `battery-doze` | 29-37 | optional (standby bucket) | commands | USB only; screen off for Doze | **approximate** (Doze) |
| `rtl-large-font` | 29-37 (locale 33+) | optional (per-app locale) | property, setting, commands | app restart; effect check | exact |
| `accessibility-display` | 29-37 (bold text 31+) | none | command, settings, property | effect check | exact |
| `input-visualization` | 29-37 (key presses 35+) | none | settings | effect check | exact |
| `network-degraded` | 30-37 (rate limit 33+) | none | setting, command | USB only; effect check | exact |
| `background-restricted` | 29-37 | **required** | commands | | **approximate** (app op) |
| `debugger-attach` | 29-37 | **required** | command | effect check | exact |

Every profile applies on Pixel, Samsung and other makers; the caveats are
reported as warnings (Samsung may clamp `font_scale`, Game Optimizing Service and
Device Care change performance numbers, sleeping-app lists add restrictions
ADAPT cannot read, Auto Blocker can make every write fail).

What each new profile writes:

- **`layout-debug`**: property `debug.layout=true` (show layout bounds) and global `debug_view_attributes=1`.
- **`lifecycle-stress`**: global `always_finish_activities=1` (Don't keep activities), secure `anr_show_background=1` and `show_first_crash_dialog_dev_option=1`. Settings only: `am kill`, memory trims and rotation stay separate steps you run yourself.
- **`performance-capture`**: `cmd package compile -m speed-profile -f PKG` and the USB stay-awake bit. Leave animation scales at 1 (`fast-ui` invalidates frame numbers) and reset frame stats yourself (`dumpsys gfxinfo PKG reset`).
- **`battery-doze`**: `cmd battery unplug`, `dumpsys deviceidle force-idle` and, with `--package`, the `rare` standby bucket. Turn the screen off first. Refused over wireless adb.
- **`rtl-large-font`**: property and global setting `debug.force_rtl`, `font_scale` (1.3, or 2.0 from Android 14), `wm density` about 1.15 times the physical density and, with `--package` on Android 13 or later, the per-app locale `ar`. The system language cannot be changed from the shell.
- **`accessibility-display`**: `cmd uimode night yes`, `high_text_contrast_enabled=1`, `font_weight_adjustment=300` (Android 12+). Options: `daltonizer` (deuteranomaly simulation) and `force-dark` (`debug.hwui.force_dark`). Colour simulation does not appear in screenshots.
- **`input-visualization`**: show touches, and show key presses on Android 15 or later. Option: `pointer-location`.
- **`network-degraded`**: global `ingress_rate_limit_bytes_per_second=16000` (Android 13+, downloads only; the kernel may ignore it). Option: `airplane-mode` (Android 11+). Refused over wireless adb.
- **`background-restricted`**: app op `RUN_ANY_IN_BACKGROUND` set to `ignore` for the package and Data Saver on.
- **`debugger-attach`**: `am set-debug-app -w --persistent PKG`. The persistent flag is what makes the debug app readable back from the settings database. **Without a restore the app waits for a debugger at every launch, including after a reboot.** The app must be debuggable.

### Left out

Research steps ADAPT does not apply, and why: thermal status and Game Optimizing
Service reads (low confidence), battery saver, airplane mode's Wi-Fi and mobile
data companions `svc wifi|data` (wireless-cutting, medium confidence), revoking
runtime permissions (per-permission state is not saved), `am kill`, memory trims
and rotation (one-time actions, not restorable state), and every step whose
catalog entry is low confidence. They stay in the [catalog](#catalog) as reference.

### Effect checks

A stored value, or a readback of one, is not proof of effect. The result of
`apply` says which profiles need an effect check (a screenshot after an app
restart, or a lifecycle log line) and what to look for: `layout-debug`,
`lifecycle-stress`, `rtl-large-font`, `accessibility-display`,
`input-visualization`, `network-degraded` and `debugger-attach`. Open questions
the research could not settle without a device: whether a shell write of
`always_finish_activities` takes effect without a reboot, and whether
`debug.force_rtl` flips layouts without the locale refresh the Settings app does.

## Apply

```text
adapt dev-options apply --profile fast-ui -s SERIAL --snapshot PATH
adapt dev-options apply --profile layout-debug -s SERIAL --snapshot PATH
adapt dev-options apply --profile debugger-attach -s SERIAL --snapshot PATH --package com.example.app
adapt dev-options apply --profile input-visualization -s SERIAL --snapshot PATH --option pointer-location
```

`fast-ui` writes only the three global animation scales, each to numeric `0`.
`touches` writes only the current user's system `show_touches` key to numeric
`1`. `desktop-testing` requests `1` for the two global desktop/freeform developer
settings on observed API 29-37 devices. A manual reboot may be required, and
OEM firmware may ignore the settings. See [desktop testing](desktop-testing.md).

`stay-awake` adds the USB power bit to `stay_on_while_plugged_in`, preserving
existing charging modes. Already USB-enabled values cause no write. It does not
unlock the phone or override device policy; restore explicitly after testing.
See [Rapid ADAPT and deeper diagnostics](rapid-adapt.md).

`apply` refuses, before reading or writing anything it does not need, an
unknown profile or option, a missing or invalid `--package`, a package given to
a profile that takes none, an API level outside the profile's range, and (for
`battery-doze` and `network-degraded`) a wireless or TCP transport.

The result includes `claim_boundary` (what was not established), `needs_app_restart`,
`needs_effect_check` with `effect_check`, `restore_exactness` with
`approximate_restore`, `skipped_steps` (steps gated out by API, package or option)
and the recorded `transport`.

**Property changes take effect when the app restarts.** ADAPT does not restart
apps, poke SystemProperties or use raw Binder calls to make a running app notice.

Before the first device write, ADAPT exclusively creates `PATH` with:

- the physical serial, build fingerprint, and Android user;
- the package, options, API level and transport that scoped the profile;
- each original value, including whether the key or property was absent (or, for a
  command pair, the observed state);
- the intended value; and
- per-entry attempted/applied transaction state.

An existing path, including a dangling symlink, is never overwritten. A
per-device host file lock in the system temporary folder prevents overlapping
apply/restore processes; if that folder cannot be used, the command stops before
changing anything with `Error: Cannot use the system temporary folder for the
per-device transaction lock.` (`adapt explain devopts.lock_unavailable`). Every
write is read back. A partial apply attempts to restore values that still equal
the profile value, but it will not overwrite a concurrently changed value. The
snapshot is retained with errors even when rollback is incomplete. Retrying
`apply` with the same path is intentionally refused; choose restore or inspect
the retained snapshot.

## Entry types

Three mechanisms share one snapshot model.

| Type | Read | Write | Restore |
|---|---|---|---|
| Setting | `settings get TABLE KEY` | `settings put` | the saved value, or `settings delete` when the key was absent |
| Property | `getprop NAME` | `setprop NAME VALUE` | `setprop` to the saved value, or to an empty value when it was unset |
| Command pair | the control's read command, parsed strictly | the control's command | the control's reset or the saved state |

- **Value rules.** Each setting and command carries a rule: a bounded number, an enum or a strict pattern. A value outside its rule is refused before any argv is built, at apply and when a snapshot is loaded. Settings that are not numeric are supported (for example enums and package names).
- **Properties.** Only an allowlist of `debug.*` names the shell user can set (`debug.layout`, `debug.force_rtl`, `debug.hwui.force_dark`), with plain-token values. `getprop` cannot tell an unset property from one set to empty, so an unset property is restored as empty.
- **Command pairs.** Read output that does not match the parser exactly is refused before any write. A prior state ADAPT cannot put back (a battery override already active, Doze already forced, a custom dark-theme schedule, an exempt standby bucket) is refused as `unsupported_state`.

| Control | Read | Apply | Restore | Exact |
|---|---|---|---|---|
| `wm_density` | `wm density` | `wm density N` | `wm density reset` or the saved override | yes |
| `battery_state` | `dumpsys battery` | `cmd battery unplug` | `cmd battery reset` | yes |
| `doze_state` | `dumpsys deviceidle get deep` | `force-idle` | `unforce` | no: normal scheduling, not the same step |
| `night_mode` | `cmd uimode night` | `cmd uimode night yes` | the saved `yes`/`no`/`auto` | yes |
| `debug_app` | `settings get global debug_app`, `wait_for_debugger` | `am set-debug-app -w --persistent PKG` | `am clear-debug-app` or the saved app and flag | yes |
| `package_compile` | `dumpsys package PKG` (dexopt block) | `cmd package compile -m speed-profile -f PKG` | `cmd package compile --reset PKG` | no: install-time filter, which can differ from the earlier one |
| `standby_bucket` | `am get-standby-bucket` | `am set-standby-bucket ... rare` | the saved bucket | yes (the system may move the app on its own, which is a conflict) |
| `app_background` | `cmd appops get ... RUN_ANY_IN_BACKGROUND` | `cmd appops set ... ignore` | the saved mode (`default` for allow) | no: explicit allow cannot be told from default |
| `app_locale` | `cmd locale get-app-locales` | `set-app-locales ... --locales ar` | the saved list, or cleared | yes |
| `data_saver` | `cmd netpolicy get restrict-background` | `... set restrict-background true` | the saved state | yes |
| `airplane_mode` | `settings get global airplane_mode_on` | `cmd connectivity airplane-mode enable` | `enable` or `disable` as saved | yes |

A control marked not exact is always reset rather than compared: a background
recompile or another actor's change does not block the restore, and the restore
result lists it under `approximate_restore`.

**Per-app scope.** Per-app entries record the package in the snapshot, validate
it as an Android application id, and require it to match the snapshot's package
at restore.

**Transport check.** The transport is classified from the serial. Profiles that
can cut the network (`battery-doze`, `network-degraded`) are refused when it is a
TCP or wireless-debugging transport, before the phone is contacted. An alias that
hides a TCP transport behind a USB-looking name is not detected.

**Schema versions.** New snapshots are written as schema 2. Schema 1 snapshots
(the four original profiles) are still accepted and restored; they are validated
against their original shape and never rewritten as schema 2.

## Restore

```text
adapt dev-options restore -s SERIAL --snapshot PATH
```

Restore accepts only the documented schemas, profiles, tables, keys, properties,
controls, users, scopes and values. Unknown or malformed content is rejected before
ADB runs. The current physical serial, build fingerprint, and Android user must
exactly match the snapshot.

Restore first reads every entry that the snapshot recorded as applied. If any
value differs from both the profile value and its original value, or an earlier
attempt has an uncertain outcome, the operation reports conflicts and changes
nothing. There is no force mode. Original present values are written exactly as
saved; originally absent keys are deleted and unset properties are emptied. Each
result is read back. If a restore fails partway through, ADAPT attempts to return
entries already restored in that invocation to the applied profile, where the
current value still makes that safe (reset-only controls are not reapplied).

Restore re-reads each entry immediately before writing it. An entry that changed after the first check is left untouched, the result reports a partial restore (`restore_partial`), and running restore again finishes the job once the conflict is resolved. If ADAPT was interrupted after writing an entry but before recording it, restore refuses that entry as uncertain by default; pass `--accept-uncertain` to restore it, which works only when the device still holds exactly the value ADAPT intended to write.

## Maker layers

### Samsung One UI

**Auto Blocker** (One UI 6 and later) can block commands sent over USB and turn
USB debugging off. When `inspect` detects One UI 6 or later (from the property, or
estimated from the Android version when the property is missing) it says so, and
when a write or restore fails on such a phone the failure guidance
(`adapt explain devopts.samsung_auto_blocker`) tells you to check Settings >
Security and privacy > Auto Blocker. ADAPT never changes Auto Blocker. Whether it
affects shell writes, and the One UI version properties themselves, are not
documented by Samsung.

Samsung may also clamp `font_scale`, ignore AOSP desktop keys, and throttle apps
through Game Optimizing Service. Samsung-only options (GPUWatch, the verbose debug
switch, service codes, sleeping apps) are in the catalog as guidance and are never
automated.

### Pixel

Almost every Pixel option is an AOSP option that only Pixel builds make visible.
Quarterly releases add options without changing the API level, so gate on what the
device reports (`pixel.build_id`), not on the API number alone.

```text
adapt dev-options pixel-16kb-check -s SERIAL
```

`pixel-16kb-check` reads the page size and says how to test 16 KB pages safely:
the Android emulator image with 16 KB pages, or a device that already boots with
them. The **Boot with 16KB page size** option on supported Pixel models needs an
unlocked bootloader (OEM unlocking) and **wipes the device**; ADAPT never changes
it. The page size is what `getconf` reports for this boot; it says nothing about
whether an app works on 16 KB pages. See Google's
[16 KB page size guide](https://developer.android.com/guide/practices/page-sizes).

## Catalog

```text
adapt dev-options catalog
adapt dev-options catalog --json --layer samsung --api 34
```

The catalog is a curated subset of desk research shipped as package data
(`adapt_adb/data/dev-options-catalog.json`): 176 entries (31 read-only, 95
reversible shell changes, 43 that only you can toggle on the phone, 7 that are
never automated). Each entry has its mechanism, read and reference commands, API
range, reboot need, OEM notes, sources and confidence (high 59, medium 97, low 20
in the subset). It is offline; filter with `--layer` and `--api`.

- `read_only` entries are what `inspect --all` can read.
- `adapt_profile` entries are used by a profile (`automated_by` names it).
- `reference_only` entries have documented commands that ADAPT does **not** run.
- `guidance_only` entries say what to do on the phone, for example the **background process limit**: "Do this on the device".
- `never_automate` entries are listed with the reason: OEM unlocking, revoking USB debugging authorizations, converting to file encryption, the 16 KB page size boot toggle, the Samsung USB settings service code, clearing app data and screen lock credentials. Disabling USB or wireless debugging is read-only for the same reason: it would drop the connection ADAPT runs on.

Privileged entries (root, system signature or an OEM tool) are left out.

## Recovery limits

Snapshots make recovery possible; they do not guarantee it. The device can
disconnect, reboot, switch users, change builds, reject a permission, or be
modified by a person or another process. A host crash can leave an entry with
an `attempted` but not confirmed `applied` state; ADAPT reports that state as
uncertain instead of guessing. Successful readback confirms only the value
observed at that moment. The snapshot is retained after successful restore as
recovery evidence and should be protected because it contains device identity.
Debug properties also clear on reboot.

The `fast-ui` profile can hide animation-dependent races and visual defects. It
makes no performance or CI speed claim.

## What is and is not proven

- **Offline-tested:** each entry type's apply, readback and restore (including absent originals, conflicts, partial failure with rollback), schema 1 compatibility, argv shapes and validators, the wireless refusal, profile gating by API and maker layer, detection parsing (synthetic values only), the catalog schema and clean JSON output.
- **Not proven:** that any profile works on a real device. The read output formats are from AOSP source reading and may differ on a given build or OEM; a mismatch is refused before any write, not guessed at. The recorded device evidence is one `touches` apply and restore on an Android 13 phone.
- **Not established by a stored value:** that an app or OEM honours the option; see the effect checks above.

## Primary references

- [Android Developer options](https://developer.android.com/studio/debug/dev-options)
- [Android Debug Bridge](https://developer.android.com/tools/adb)
- [AOSP Settings keys](https://android.googlesource.com/platform/frameworks/base/+/refs/heads/android16-release/core/java/android/provider/Settings.java)
- [Support 16 KB page sizes](https://developer.android.com/guide/practices/page-sizes)
- [Samsung Auto Blocker](https://www.samsung.com/us/support/answer/ANS10003636/)
