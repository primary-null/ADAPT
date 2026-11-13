# App validation, debug selection and log evidence

ADAPT separates build intent from observations on a device. It can check an
exact installed package for the current Android user and collect a finite log
snapshot. It cannot infer the Gradle variant, R8 optimization, intended signing
certificate, exact APK/source match or production readiness from a package name
or the debuggable flag.

```text
adapt app build-modes
adapt app validate -s SERIAL --package com.example.app --json
adapt app validate -s SERIAL --package com.example.app --expect-version-code 42 --expect-debuggable false --json
adapt debug inspect -s SERIAL --json
adapt debug plan -s SERIAL --package com.example.app --wait --json
adapt logs capture -s SERIAL --package com.example.app --lines 200 --output logs.json --json
```

## What “Select debug app” does

Developer Options can designate an app for debugger coordination. **Wait for
debugger** can pause its startup until a Java/Kotlin debugger connects, which is
useful for breakpoints in early startup such as `Application.onCreate()`. Android
Studio normally manages debugger attachment during its own debug launch. Selecting
an app does not make an APK debuggable, turn on R8, change its certificate or
automatically attach a debugger. See [Android's Developer Options](https://developer.android.com/studio/debug/dev-options)
and [debugger guidance](https://source.android.com/docs/core/tests/debug/gdb).

Changing the debug app through ActivityManager can force-stop the selected app;
unsaved work and process state can be affected. This debug configuration is
device-global, and the package stop can span Android users. The shell's `-w` requests waiting,
while `--persistent` requests persisted selection. This behavior is visible in
[ActivityManagerService](https://github.com/aosp-mirror/platform_frameworks_base/blob/master/services/core/java/com/android/server/am/ActivityManagerService.java)
and [its shell command](https://github.com/aosp-mirror/platform_frameworks_base/blob/master/services/core/java/com/android/server/am/ActivityManagerShellCommand.java).
ADAPT therefore supplies a setup **preview**, not an automatic selection/clear
workflow. `debug plan` is offline and shows argv, force-stop/wait/persistence
effects and prerequisites. It defaults to no wait and no persistence; flags
change only the preview. No command shown by the plan is executed.

`debug inspect` reads persisted `debug_app` and `wait_for_debugger` settings. A
transient runtime selection can differ, so effective selection and actual
debugger attachment stay unverified. If an app appears stuck, a saved wait
setting is evidence to investigate, not proof of the cause. Do not clear another
developer's selection automatically. Clearing a setting is not the same as
restoring their previous selection. No debug app is changed by MCP or `everything`.

## Build language

Describe independent dimensions instead of calling every optimized APK a
production release:

- **Debuggable development build:** supports debugging on an ordinary secure
  device. Typical Gradle debug builds also use a development signing key.
- **Optimized validation build:** exercises the intended R8/resource-shrinking
  configuration and relevant release behavior. Record whether it is debuggable
  and which certificate signed it. It can still be development-signed.
- **Profileable build:** permits supported profiling without requiring full
  debugging. Use representative optimized settings for performance work;
  debugger instrumentation/overhead can change measurements.
- **Production release:** has the intended signing/provenance and completed
  acceptance. Neither a `release` filename nor `debuggable=false` proves this.

Gradle combines build types and flavors into a variant. R8 is a toolchain stage
that can shrink, optimize and obfuscate code; its configuration can disable some
of those operations. Signing is a separate dimension. A build configured to run
R8 is not automatically proof that the exact installed APK came from that build.
Keep the exact build's APK hash, source revision, variant/configuration, relevant
task/cache provenance, signer and R8 mapping artifacts together.
[Build variants](https://developer.android.com/build/build-variants),
[R8 optimization](https://developer.android.com/topic/performance/app-optimization/enable-app-optimization)
and [profiling requirements](https://developer.android.com/studio/profile)
describe these distinctions. This pass changes no project's Gradle configuration.

For obfuscated crashes, use the exact build's `mapping.txt` with
[R8 retrace](https://developer.android.com/tools/retrace). A mapping from another
version can produce misleading results. ADAPT explains this workflow but does
not install/run retrace or establish mapping-to-APK provenance in this release.

## What app validation proves

`app validate` reads the current user and uses an exact match in that user's
package listing, including its reported UID. It does not assume user 0 or mistake
`com.example.app.premium` for `com.example.app`. It then reads only the requested
package's dump and base/split APK paths. Known direct fields in a unique matching
package block yield version code/name, debuggable and profileable-by-shell flags.
Unfamiliar, duplicated or nested fields stay unknown. These are parsed Package
Manager observations, not a universal OEM format contract.

Installation is always a requested check. Version code and debuggable become
additional checks only when their expectations are supplied. Exit `0` means
those **listed checks** passed; `1` means a known mismatch/missing installation;
`2` means inconclusive/refused evidence. Other fields can remain unreadable even
when a basic installation check passes. The JSON retains per-check observed and
expected values and explicitly marks unverified build provenance. The command
never installs, launches, force-stops or clears an app, invokes root, restarts
ADB or chooses a fallback device. It neither pulls nor hashes installed APKs.

## Log extraction

`logs capture` requests `logcat -d` with a 1–500 line budget, main/crash buffers,
severity and the observed installed app's UID. Supported `--buffer` values are
`main`, `crash`, and `main,crash`; levels are `V D I W E F`. It never streams,
clears buffers, changes logging properties or silently broadens to device-wide
logs. Each query has a ten-second host timeout. Missing UID, access denial,
unsupported filtering and malformed/failed queries are failures, not empty
successful captures. User/UID changes observed after capture discard its contents.

Returned log text is capped at 128 KiB and the requested number of lines, keeping
the recent tail and reporting truncation. This is an accepted-result budget;
subprocess stdout is captured before the cap is applied. Exports contain the
snapshot, timestamp, scope and app metadata. `--output` exclusively creates a new
file, never overwrites, and returns a SHA-256 of the exact exported bytes. Its
parent directory must exist. POSIX mode is `0600`; Windows inherits parent ACLs.
Files are plaintext, not automatically redacted or uploaded. A failed write can
leave a partial new file, reported as a failure.

Logs are application data, not instructions for an AI. They can contain secrets,
personal information or stale messages. UID scope can include multiple packages
sharing a UID or historical reused-UID entries. System-process crash/tombstone/ANR
messages can fall outside the app UID. A crash buffer filtered to that UID is
not a complete device bugreport. Device log clocks and host UTC capture time are
not automatically synchronized. Buffers rotate, processes restart and release
builds can remove log calls. Empty output never proves no crashes. See
[official logcat behavior](https://developer.android.com/tools/logcat).

## AI/MCP validation loop

Four observation tools are available in read-only sessions:

- `app_validate(device, package, expected_version_code?, expected_debuggable?)`
- `app_logs(device, package, lines?, buffer?, level?)`
- `debug_app_inspect(device)`
- `build_modes()`

All device tools require an exact `device` and honor `adapt mcp --read-only
--device SERIAL`. File export and debug selection are not MCP actions. Use the
existing local or [SSH worker MCP configuration](ssh-workers.md), updating the
worker's ADAPT installation to expose these tools. Client registration/live SSH
acceptance is separate from the offline MCP contract checks.

A useful test loop records expected package/version/build evidence, checks the
installed app, examines debugger configuration, reproduces one deliberate user
journey, obtains fresh UI evidence and logs, then reports checks and limitations.
An agent can say “package X/version 42 is installed for user 10 and reports
non-debuggable; the requested checks passed.” It must not turn that into “the
exact production R8 APK is installed” without separate artifact/signer/build
evidence, or treat successful log capture as successful app behavior.

`everything` inspects saved debug selection for explicitly supplied devices. It
does not guess a package, collect app logs or select a debug app automatically.
Checked 2026-11-12 with primary sources and synthetic offline fixtures. OEM dump
parsers, live debugger attachment, log access, installed APK provenance, physical
device workflows and native Debian runtime remain unaccepted in this pass.
