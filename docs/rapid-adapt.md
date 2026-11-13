# Rapid ADAPT option

Rapid ADAPT is an option inside the ADAPT application, reached with `adapt rapid`.
It uses the same package, launcher, tools and recovery system as the rest of ADAPT.

This option groups the short development test loop: **check → prepare → reproduce
→ capture → restore**. It is a curated command plan, not an automatic batch runner.
It contacts no device, creates no files and executes no planned commands.

```text
adapt rapid plan -s SERIAL --package com.example.app
adapt rapid plan -s SERIAL --package com.example.app --session-dir NEW_SESSION --json
adapt rapid plan -s SERIAL --package com.example.app --fast-ui
```

The plan includes device capability/settings observations, an optional installed
app check, recoverable USB-powered stay-awake, one deliberate test journey,
focused logs, and explicit restoration commands. Fast UI is opt-in: disabling
animations can hide bugs and changes what performance measurements mean.
Without a package, the capture step selects window component logs instead of
guessing an app identity.

Execute only the chosen steps. Use new snapshot paths; apply refuses to overwrite
existing snapshots. Restore only profiles that were actually applied, including
after a later step fails. This plan does not supervise failures or automatically
roll back a whole workflow. Each individual profile retains its own guarded
recovery record.

Samsung/vendor verbose logging, full bug reports, debugger selection, desktop
activation and tool installation stay in deeper diagnostics. They are not
automatically enabled by Rapid ADAPT or Give Me Everything.

## Stay awake for USB testing

```text
adapt dev-options inspect -s SERIAL --json
adapt dev-options apply --profile stay-awake -s SERIAL --snapshot awake.json
adapt dev-options restore -s SERIAL --snapshot awake.json
```

The profile reads `Settings.Global.stay_on_while_plugged_in`, adds the USB power
bit (`2`), and preserves existing AC/wireless/dock charging bits. A setting already
including USB is left unchanged. Unknown/non-integer bitmasks are refused. As
with other profiles, a device/build/user-bound snapshot precedes writes, readback
is checked, absent originals are restored by deletion, and external changes
cause a restoration conflict instead of being overwritten.

This requests staying awake **while USB-powered**, not an indefinite battery
wake lock. It does not unlock a phone or bypass enterprise/device lock policies.
Readback establishes the requested value, not that the screen will effectively
remain awake. Restore after testing; there is no timed automatic restore.

Android documents charging-dependent Stay awake; its Settings implementation
also accounts for administrator lock restrictions. ADAPT does not modify those
restrictions. Sources: [Developer options](https://developer.android.com/studio/debug/dev-options),
[AOSP StayAwakePreferenceController](https://android.googlesource.com/platform/packages/apps/Settings/+/refs/heads/main/src/com/android/settings/development/StayAwakePreferenceController.java).

## Deeper logging diagnostics

```text
adapt logs guide --topic samsung
adapt logs guide --topic all --json
adapt logs diagnostic -s SERIAL --topic window --minutes 2 --output window.json
adapt logs diagnostic -s SERIAL --topic dex --minutes 2 --output dex.json
adapt logs diagnostic -s SERIAL --topic print --minutes 2 --output print.json
```

Samsung verbose debug logging is treated as the exact label reported by the
owner, with an **unverified firmware-specific automation mapping**. Record model,
Android/One UI and original switch state, enable it manually for a short
reproduction if needed, then restore that original state. It is not equated with
Select debug app, AOSP verbose vendor logging, or Samsung SysDump debug level.
No arbitrary Settings key, property, Binder call or service menu is used.

AOSP's verbose vendor logging controller talks to the dumpstate HAL; it is not
a universal `settings put` switch. Samsung separately documents support-oriented
SysDump workflows, some of which change debug level and restart a device. Those
instructions do not establish how the owner's exact verbose switch works, and
ADAPT does not execute them. Sources:
[AOSP vendor controller](https://android.googlesource.com/platform/packages/apps/Settings/+/refs/heads/main/src/com/android/settings/development/EnableVerboseVendorLoggingPreferenceController.java),
[Samsung support log workflows](https://docs.samsungknox.com/admin/knox-manage/original-console/monitor/collect-bug-reports/).

Window logs help investigate focus, resizing, lifecycle and multi-display issues.
Print logs concern print framework/spooler/service problems. Binary window traces
are separate from text logcat, and their formats/permissions vary by OS. Enabling
one does not guarantee text output. See [Winscope capture](https://source.android.com/docs/core/graphics/winscope/capture/adb).

The diagnostic command reads existing main/system/crash buffers for fixed topic
tags over the last 1–10 minutes, using the **device's clock**, and returns at most
500 lines/128 KiB. It uses a time cursor rather than a global last-N cursor,
which can miss a quieter subsystem on a noisy phone. Reading is file-backed with
a monitored 2 MiB input budget and 10-second deadline; overflow/failure discards
the capture without widening the query. Device/build/user identity is checked
before and after. No buffers are cleared and no producer verbosity is enabled.
OEM tags can differ; empty output is inconclusive. Sources:
[Logcat](https://developer.android.com/tools/logcat),
[AOSP time cursor/filter syntax](https://android.googlesource.com/platform/system/core.git/+/979f3d0116ee62b1461c94899ad7d82d2c00cd3e/logcat/logcat.cpp).

System component logs can include multiple apps/users, identifiers, document
titles or secrets. They are not automatically redacted. Export is optional,
user-scoped, hashed and refuses overwriting existing paths. Review locally before
sharing or sending logs to an AI service. For one installed app, retain the
existing `adapt logs capture --package ...` UID filter. `--level V` reads verbose
records already produced; it cannot enable missing producer logs.

The read-only MCP tools `logging_guide(topic?)` and
`diagnostic_logs(device, topic, minutes?, lines?, level?)` expose these functions.
Exact device restrictions apply to diagnostic capture. No logging-switch or
stay-awake mutations were added to MCP.
