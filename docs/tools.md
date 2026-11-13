# Goal-based tool catalog

ADAPT's tool catalog answers: “What should I grab or review if I want to test, mirror, build, or inspect this?” It combines a guided catalog with optional pinned scrcpy installation. It is not a universal ROM builder or flasher.

The command group is registered in the main `adapt` CLI.

## Commands

```text
adapt tools list [--goal GOAL] [--json]
adapt tools info TOOL [--json]
adapt tools plan GOAL [--json] [--branch BRANCH] [--device CODENAME]
adapt tools install scrcpy [--dry-run] [--yes] [--json]
```

Supported goals are `testing`, `mirroring`, `remote-worker`, `aosp-build`, `lineage-build`, and `image-inspection`.

- `list` shows the curated records for all goals or one goal and performs a read-only lookup of executable candidates on the current process environment's `PATH`.
- `info` shows purpose, executable candidates, dependency hints, supported host classifications, official/upstream and license links, research date, and actionable acquisition guidance. `adb` and `fastboot` are accepted aliases for `platform-tools`.
- `plan` explains which catalogued tools are absent, which action class applies, and the next official/upstream acquisition page. It does not perform the actions.

`--json` writes one JSON object to stdout. Validation errors are also JSON when `--json` is selected, with a nonzero `exit_code` and no human error mixed into stdout.

## What detection means

Detection uses `shutil.which` only. ADAPT never invokes a discovered executable, so a result such as:

```json
{
  "status": "found",
  "version": "not_probed",
  "trusted": false
}
```

means only that a named candidate is on this environment's `PATH`. It does not prove the file's publisher, integrity, version, license, compatibility, or safety. The catalog does not crawl drives or source trees.

Windows, WSL, and native Linux results are deliberately separate. A native Windows `git.exe`, `java.exe`, or `adb.exe` is never credited to WSL. The initial implementation does not run `wsl.exe --list --verbose`; on native Windows it reports WSL as `not_assessed`. It never starts, stops, configures, or mutates a WSL distribution.

## Host classifications

- Native Windows supports useful native testing, mirroring, hashing, and selected inspection workflows, but it is classified as unsupported for current AOSP or LineageOS compilation.
- WSL is a separate Linux environment classified as `qualified_unverified`. The selected guide, distro, ext4 source location, resources, packages, and branch still need review.
- Native x86-64 Linux is classified as `requires_upstream_review` for builds. A generic Debian or Linux label is not automatic readiness certification; use the exact upstream version guide and review distribution-specific packages.
- ARM64 is not certified as a build host because current AOSP requirements specify 64-bit x86 Linux. The catalog fails closed instead of generalizing x86 requirements.

## Branch and device context

Java and Python requirements vary across Android and LineageOS generations. The catalog therefore gives no universal JDK or Python installation recommendation. Select a branch first and follow its official guide, preferring source-provided prebuilts when that guide makes them authoritative. Historical branches can require an isolated legacy environment.

An `aosp-build` plan accepts an optional user-supplied `--branch`. Without one it states that a branch is not selected and does not claim branch-level readiness.

A `lineage-build` plan requires both `--branch` and `--device`. Missing context returns `status: incomplete`, exit code 2, and the official device index. Supplied values produce the exact device build-page link, but remain `user_supplied_unverified`: runtime is offline, so ADAPT does not claim that the device page exists, that the branch is currently listed, or that the device remains supported.

The binary catalog is only part of ROM planning. Device/common trees, kernel policy, proprietary files from a permitted matching source, partition and recovery layout, signing policy, storage, RAM, and source revisions are separate inputs. The tool catalog never substitutes a similar device or infers universal recovery support.

## Curated acquisition policy

The catalog covers:

- Google's Platform-Tools (`adb` and `fastboot`) through the existing `adapt setup` path;
- scrcpy from its Genymobile upstream;
- optional OpenSSH client detection and manual OS acquisition for the `remote-worker` goal (Platform-Tools/device access belong on the worker);
- Git, Repo, GnuPG, branch-specific Java and Python, and optional ccache;
- official Perfetto and Android SDK Command-Line Tools guidance;
- AOSP `avbtool`, `unpack_bootimg`, `simg2img`, and `lpunpack`, preferably from a pinned source tree; and
- optional `payload-dumper-go`, clearly labeled third-party upstream.

Every record includes dated research scope, official/upstream source and license links, host classifications, dependencies, and an actionable acquisition page. A source link is guidance, not an allowlist expansion.

Platform-Tools uses the existing Google-only setup controls. Pinned scrcpy installation is also Class 1 and uses a separate upstream-only provisioner. Other package, SDK, upstream release, or source-tree acquisition is manual/explain-only. In particular, the catalog does not widen the Platform-Tools download allowlist and does not download `payload-dumper-go`.

## Action classes and boundaries

- **Class 0 — observe:** current-environment PATH lookup and supplied-context reporting. No discovered program is executed.
- **Class 1 — user scope:** the existing Platform-Tools setup or pinned scrcpy installation, after the user explicitly chooses it.
- **Class 2 — privileged/manual:** operating-system packages, drivers, SDK components, upstream release review, udev, and other admin or package-manager work. The plan stops at guidance.
- **Class 3 — project/device mutation:** source synchronization/build, blob extraction, image changes, and device work require a separate purpose-specific authorization and workflow.
- **Class 4 — prohibited automatic behavior:** destructive device actions, arbitrary scripts or binaries, privilege escalation, trust bypass, and universal device scripts.

The `tools list/info/plan` commands perform no installation, download, network request, package-manager operation, root/admin action, external write, source synchronization, build, image extraction, WSL mutation, or device access. `tools install scrcpy` can download and write a verified user-scope release after confirmation; dry-run changes nothing. See [scrcpy installation](scrcpy.md). Plans contain no executable bootloader, partition, recovery, erase, format, or install operations. They also never execute a downloaded shell script or unknown binary.

## Research freshness

`adapt everything` combines the two implemented managed installers with catalog
detection and host checks after confirmation. It reports the remaining manual
acquisitions and can inspect explicitly selected devices. Preview with
`adapt everything --dry-run`; see [batch setup](everything.md).

Catalog records were researched on 2026-11-12. Runtime operation is offline. Host packages, upstream releases, licenses, AOSP tags, LineageOS devices/branches, and support status can change, so their current state must be verified at the linked authoritative page before acquisition or build work.
