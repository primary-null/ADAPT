# Device state and transport detection

```text
adapt detect scan
adapt detect scan --transport fastboot --json
adapt detect inspect -s SERIAL --json
adapt detect inspect -s SERIAL --transport fastboot --json
```

Scan lists ADB and fastboot independently. It never reboots a phone to discover
another mode. If ADB shared-daemon preflight fails, its group is blocked and
fastboot can still be scanned. Fastboot-only work never resolves or preflights
ADB. Inspection requires an exact serial and explicit transport (default `adb`),
with no fallback target. Matching serial text in two snapshots is not proof of
physical identity. Snapshots can become stale as devices change modes/disconnect.

Root inspection reads `id -u` and looks up `su` using `command -v su`. UID 0
means the queried shell is root, including possible recovery/debug shells.
Finding `su` is only a candidate indicator. ADAPT never executes it or requests
`adb root`. Missing, hidden or denied indicators cannot prove an unrooted phone.

Custom ROM detection looks for self-reported LineageOS, mod-version and crDroid
properties. It reports `custom_rom_indicators` or `unknown`, never definitely
stock. Build type/tags remain separate evidence: test keys, root or unlocked
state alone do not identify a custom ROM. Properties can be altered or hidden.

Bootloader observations combine `ro.boot.flash.locked`,
`ro.boot.vbmeta.device_state` and recognized verified-boot colors. Matching
evidence reports locked/unlocked, disagreement reports `conflicting`, and absent
or unrecognized evidence reports `unknown`. OEM unlocking support and permission
are separate; enabling OEM unlocking does not unlock the bootloader. These are
Android-reported observations, not hardware attestation or unlock guarantees.

Fastboot inspection queries only targeted `getvar unlocked`, `getvar is-userspace`
and `getvar product`. Stderr replies are supported. Each process has a five-second
host timeout. Unsupported, duplicated, empty, failed and timed-out replies stay
explicit. Recognized `is-userspace` values distinguish `fastbootd` from bootloader
fastboot; the listing alone cannot. Root and running-ROM checks are unavailable
in this transport. No root request, reboot, flash, unlock or disconnect is run.

Samsung Download/Odin mode is a different protocol, without an adapter in this
release. An empty fastboot list does not establish device absence, lock status
or healthy USB drivers. Binary selection uses `ADAPT_FASTBOOT`, PATH, then sibling
files in standard Platform-Tools directories. An invalid explicit override does
not silently select another binary. Resolution executes neither ADB nor an
installer. Partial scans exit `2`, including missing host tools. JSON may include
device identities/build evidence; share it deliberately.

`adapt everything` includes independent fastboot enumeration after confirmation.
Explicit `--device` entries remain ADB targets and gain root/ROM/lock and desktop
inspection. Inspect fastboot targets separately with `--transport fastboot`.
These new commands are CLI operations; the MCP tool inventory is unchanged.

Sources checked 2026-11-12: [AOSP lock properties](https://source.android.com/docs/core/architecture/bootloader/locking_unlocking),
[AOSP fastboot protocol](https://android.googlesource.com/platform/system/core/+/main/fastboot/README.md),
and [LineageOS build configuration](https://github.com/LineageOS/android_vendor_lineage/blob/lineage-23.0/config/common.mk).
Root/ROM detection uses incomplete indicators, not a universal detector. Tests
use offline fixtures; no rooted, locked, unlocked or fastboot hardware was used.
