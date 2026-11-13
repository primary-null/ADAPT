# Managed scrcpy provisioner

ADAPT has an optional, confirm-before-install provisioner for one pinned
Genymobile scrcpy release. It is exposed as two integration functions in
`adapt_adb.scrcpy_install`:

- `plan_scrcpy()` returns an offline, JSON-serializable plan and performs no
  writes, downloads, executable probes, or device access.
- `install_scrcpy(yes=False, dry_run=False)` reuses an existing `scrcpy` on
  `PATH`, reuses an intact ADAPT-managed install, or installs the pinned
  release after confirmation.

The main CLI provides:

```text
adapt tools install scrcpy [--yes] [--dry-run] [--json]
```

## Pinned upstream evidence

The provisioner pins scrcpy **5.0.1**, tag `v5.0.1`, tag commit
`a60891aea193d92e7e5c3942700eca63f9d19a5f`, published
`2026-10-08T06:44:27Z`. Research was refreshed on 2026-11-12.

The official tagged Windows documentation publishes these exact assets and
SHA-256 values:

- Windows x86-64: `scrcpy-win64-v5.0.1.zip`, 11,264,634 bytes,
  `b12a2c4ee8be317422451fc7dcf8ee20a71b5ea7ef9ad73ddd825a25316227a5`.
- Windows x86: `scrcpy-win32-v5.0.1.zip`, 10,133,753 bytes,
  `0cb65ebf1bce892fa4d1502d699af0c7a1a7f24d7febcb9ef72894e3f23fc2ad`.
- Windows ARM64: `scrcpy-winarm64-v5.0.1.zip`, 9,825,739 bytes,
  `16c3fc2068df64946f670e3504b4752073ebae47a416a45b46e79a5ab19c689b`.

The official tagged Linux documentation publishes the x86-64 static asset
`scrcpy-linux-x86_64-v5.0.1.tar.gz`, 17,883,742 bytes, SHA-256
`9f969d30cc574816077edecda65719c1c70b0aee1df1ba5fa00779ac07b8fd6e`.
The sizes and hashes also matched the version-specific GitHub Releases API
metadata at research time. ADAPT does not resolve or trust the mutable
`releases/latest` URL.

Authoritative upstream sources:

- [scrcpy 5.0.1 release](https://github.com/Genymobile/scrcpy/releases/tag/v5.0.1)
- [tagged Windows release instructions and hashes](https://github.com/Genymobile/scrcpy/blob/v5.0.1/doc/windows.md)
- [tagged Linux release instructions and hash](https://github.com/Genymobile/scrcpy/blob/v5.0.1/doc/linux.md)
- [tagged release-verification policy](https://github.com/Genymobile/scrcpy/blob/v5.0.1/doc/verify-release.md)
- [tagged packaging script](https://github.com/Genymobile/scrcpy/blob/v5.0.1/release/package_client.sh)
- [tagged Apache-2.0 scrcpy license](https://github.com/Genymobile/scrcpy/blob/v5.0.1/LICENSE)

The tagged packaging script establishes that each archive has one
`scrcpy-<target>-v5.0.1` root and includes the matching `scrcpy-server`.
ADAPT additionally verifies the bundled server against upstream's published
SHA-256
`764eb6f79811d5211fe9df341120882ba9994c7a61b897d7bf3fb662e53bc536`.

## Host and architecture policy

Selection is exact and fails closed:

- native Windows `x86_64`/`AMD64` selects `win64`;
- native Windows `x86`/`i686` selects `win32`;
- native Windows `ARM64`/`aarch64` selects `winarm64`; and
- native Linux `x86_64`/`AMD64` selects `linux-x86_64`.

When a managed installation is needed, Linux ARM64 and every other unlisted combination receive an actionable refusal. Existing PATH binaries can still be reused without claiming their provenance or version.
ADAPT never substitutes a similarly named cross-platform or cross-architecture
asset. macOS is intentionally outside this provisioner's approved matrix.

## Confirmation and non-effects

An already-found `scrcpy` is reused by default without running it or claiming a
verified version or publisher. An intact ADAPT-managed 5.0.1 install is also
reused after receipt and required-file hashes are checked.

A new install requires an interactive `y`/`yes` confirmation. Automation must
pass `--yes`, which maps to `yes=True`. A non-interactive invocation without
that opt-in fails before any write or network request. `--dry-run` maps to
`dry_run=True`; it performs no write and no download. Dry-run requires no confirmation, including in non-interactive or JSON mode.

The provisioner never:

- changes process, user, or system `PATH`;
- installs a package or uses admin/root privileges;
- executes a downloaded shell script;
- launches or version-probes scrcpy;
- starts, stops, or connects to an ADB server;
- contacts a device or starts/stops a mirror; or
- changes the separate Google Platform-Tools host allowlist.

## Destination and placement

Managed content is versioned under user scope:

- Windows: `%LOCALAPPDATA%\ADAPT\tools\scrcpy\5.0.1\<package-root>`
- Linux: `${XDG_DATA_HOME:-~/.local/share}/adapt/tools/scrcpy/5.0.1/<package-root>`

Downloads and extraction use same-filesystem temporary siblings. The package is
validated while staged, receives its receipt, and is placed using an atomic
no-clobber rename. Linux requires `renameat2(RENAME_NOREPLACE)`; ADAPT refuses an
unsafe fallback if the runtime lacks it. Existing unrelated, incomplete, or
unverifiable targets are never adopted, deleted, replaced, or upgraded. The
current API intentionally implements no upgrade option.

## Network and archive controls

The initial URL must be the exact HTTPS
`github.com/Genymobile/scrcpy/releases/download/v5.0.1/<asset>` URL, with no
credentials, non-default port, fragment, or query. Every redirect remains HTTPS
and is limited to the exact GitHub URL or GitHub's narrowly documented release
delivery host `release-assets.githubusercontent.com`. Redirect count is bounded.
Signed delivery queries may transit in memory but are stripped from receipts
and errors; credentials and unexpected hosts are rejected.

The response must match both the pinned byte count and SHA-256. Downloads are
streamed with a 256 MiB cap and removed on success or failure.

Before extraction, ADAPT validates the whole ZIP or tar.gz inventory. It caps
member count, per-file size, total expanded size, and compression ratio, and
rejects:

- absolute paths, `..`, empty/ambiguous path components, and overlong names;
- symlinks, hard links, devices, FIFOs, and other special files;
- encrypted ZIP members;
- Windows alternate-data-stream syntax, reserved device names, trailing dots
  or spaces, and case/Unicode-normalized path collisions;
- file/directory prefix collisions; and
- files outside the single pinned package root.

Extraction copies regular files one at a time rather than using `extractall()`.
The staged payload must contain a non-empty host client, matching
`scrcpy-server`, and the upstream license file. Linux executable permission is
restored without retaining setuid/setgid bits.

## Receipt and licenses

`adapt-install-receipt.json` is written inside the managed package before final
placement. It records release version/tag/commit/date, generic host OS and
architecture, source and query-free canonical URL, asset byte count and hash,
every extracted regular file's relative path/size/SHA-256, retained license or
notice paths, and explicit non-effects. It does not store usernames, home
directories, absolute workstation paths, redirect secrets, device identities,
or ADB state.

scrcpy source is Apache-2.0, and its upstream license is retained. The release
bundle also contains separately licensed dependencies (for example FFmpeg,
SDL, platform-tools/ADB, and their transitive components depending on the
asset). ADAPT retains every file supplied by the upstream archive and expressly
does **not** claim that the whole bundle is Apache-2.0.

## Using the CLI

```text
adapt tools install scrcpy --dry-run
adapt tools install scrcpy
adapt tools install scrcpy --yes --json
```

The first command previews without permission prompts or changes. The second
shows publisher, pinned release, source, destination, and license information
before confirmation if a new install is needed. The third is an explicit
non-interactive installation approval. JSON without `--yes` refuses a new
installation, but may report reuse of an existing binary without prompting.

No command launches a mirror. The managed binary can be selected by ADAPT's
mirror tooling or its reported path, without changing global PATH. Existing
PATH installations are kept as-is; an installer invocation is not an implicit
upgrade. Release receipts verify against ADAPT's pinned metadata. Runtime GPG
signature verification is not currently implemented.

## Validation limits

Unit tests use in-memory archives, fake HTTP responses, and temporary
directories only. They do not perform a real release download, external write,
package install, executable launch, ADB operation, device operation, or
privileged action. Source-level tests on this Windows workstation do not
establish native Debian runtime, loader, graphics/audio, USB, or mirror
acceptance. A separately authorized native Debian validation remains required
for those claims.
