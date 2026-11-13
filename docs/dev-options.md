# Bounded Developer Options

ADAPT exposes a deliberately small Developer Options surface. It reads a fixed
allowlist and mutates only four fixed profiles. It does not write properties, use
raw Binder calls, change USB configuration, grant privileges, install software,
or select a device implicitly.

The command group is registered in the main `adapt` CLI. Shared-daemon preflight runs before device queries.

Every command requires a nonblank explicit ADB serial and targets exactly that
transport with `adb -s SERIAL`. ADAPT separately reads `ro.serialno`,
`ro.build.fingerprint`, and the current Android user. A USB serial, emulator
endpoint, or wireless `host:port` is never treated as physical device identity.
If the on-device identity is unavailable, mutation and restoration are refused.

## Inspect

```text
adapt dev-options inspect -s SERIAL
adapt dev-options inspect -s SERIAL --json
```

Inspection reads only these settings:

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

## Apply

```text
adapt dev-options apply --profile fast-ui -s SERIAL --snapshot PATH
adapt dev-options apply --profile touches -s SERIAL --snapshot PATH
adapt dev-options apply --profile stay-awake -s SERIAL --snapshot PATH
adapt dev-options apply --profile desktop-testing -s SERIAL --snapshot PATH
```

`fast-ui` writes only the three global animation scales, each to numeric `0`.
`touches` writes only the current user's system `show_touches` key to numeric
`1`. `desktop-testing` requests `1` for the two global desktop/freeform developer
settings on observed API 29–37 devices. A manual reboot may be required, and
OEM firmware may ignore the settings. See [desktop testing](desktop-testing.md).

`stay-awake` adds the USB power bit to `stay_on_while_plugged_in`, preserving
existing charging modes. Already USB-enabled values cause no write. It does not
unlock the phone or override device policy; restore explicitly after testing.
See [Rapid ADAPT and deeper diagnostics](rapid-adapt.md).

Before the first device write, ADAPT exclusively creates `PATH` with:

- the physical serial, build fingerprint, and Android user;
- each original numeric value, including whether the key was absent;
- the fixed intended profile value; and
- per-setting attempted/applied transaction state.

An existing path, including a dangling symlink, is never overwritten. A
per-device host file lock prevents overlapping apply/restore processes. Every
write is read back. A partial apply attempts to restore values that still equal
the profile value, but it will not overwrite a concurrently changed value. The
snapshot is retained with errors even when rollback is incomplete. Retrying
`apply` with the same path is intentionally refused; choose restore or inspect
the retained snapshot.

## Restore

```text
adapt dev-options restore -s SERIAL --snapshot PATH
```

Restore accepts only the documented schema, profile, tables, keys, users, and
numeric values. Unknown or malformed content is rejected before ADB runs. The
current physical serial, build fingerprint, and Android user must exactly match
the snapshot.

Restore first reads every setting that the snapshot recorded as applied. If any
value differs from both the profile value and its original value, or an earlier
attempt has an uncertain outcome, the operation reports conflicts and changes
nothing. There is no force mode. Original present values are written exactly as
their saved numeric strings; originally absent keys are deleted. Each result is
read back. If a restore fails partway through, ADAPT attempts to return settings
already restored in that invocation to the applied profile, where the current
value still makes that safe.

Restore re-reads each setting immediately before writing it. A setting that changed after the first check is left untouched, the result reports a partial restore (`restore_partial`), and running restore again finishes the job once the conflict is resolved. If ADAPT was interrupted after writing a setting but before recording it, restore refuses that setting as uncertain by default; pass `--accept-uncertain` to restore it, which works only when the device still holds exactly the value ADAPT intended to write.

## Recovery limits

Snapshots make recovery possible; they do not guarantee it. The device can
disconnect, reboot, switch users, change builds, reject a permission, or be
modified by a person or another process. A host crash can leave a setting with
an `attempted` but not confirmed `applied` state; ADAPT reports that state as
uncertain instead of guessing. Successful readback confirms only the value
observed at that moment. The snapshot is retained after successful restore as
recovery evidence and should be protected because it contains device identity.

The `fast-ui` profile can hide animation-dependent races and visual defects. It
makes no performance or CI speed claim.

## Primary references

- [Android Developer options](https://developer.android.com/studio/debug/dev-options)
- [Android Debug Bridge](https://developer.android.com/tools/adb)
- [AOSP Settings keys](https://android.googlesource.com/platform/frameworks/base/+/refs/heads/android16-release/core/java/android/provider/Settings.java)
