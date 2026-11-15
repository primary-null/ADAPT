# `adapt doctor` and `adapt setup` Contract

**Version:** `1.0`  
**Applies to:** `adapt` (`adapt-adb`, v1 Python 3.11+ / `uv`) on **Windows 11** and **Debian** (stable/testing). Other Linux distributions are reported as `linux` and get distribution-neutral remediation. macOS is detected and reported as `macos`, but it is **not a supported host** (see §3.3 and §5.1).

This document defines the machine-checkable behavioral, diagnostic, bootstrap, safety, and exit-code contract for `adapt doctor` and `adapt setup`. Implementations and automated test suites MUST conform to every invariant in this specification.

---

## 1. Command Roles & Invariants

| Command | Mode | Mutates Host State? | Purpose |
|---|---|---|---|
| `adapt doctor` | Read-only diagnostic | **No** (never installs, downloads, kills processes, or modifies env/files) | Inspects ADB toolchain, origin, `PATH`, `ANDROID_HOME`, OS USB/driver readiness, port `5037` ownership/parity, and `adb devices -l` states. |
| `adapt setup` | User-scope bootstrap | **User scope only** (binary unpack + user `PATH`; never elevated/root, never touches `ANDROID_HOME`) | Downloads, verifies, and unpacks official Google `platform-tools` into a user-scope directory and registers it on user `PATH`. Emits non-destructive hints for drivers/`udev`. |

Both commands MUST support `--json` to emit machine-checkable structured output on `stdout` (see [Section 4](#4-machine-checkable-json-contract)). In `--json` mode, `stdout` MUST contain strictly one valid JSON document and nothing else; diagnostic logs go to `stderr` (or bounded memory).

When `--json` is omitted, TTY stdout sessions (`sys.stdout.isatty()`) render styled `rich` human output (compact header, toolchain table, aligned check glyphs `✓`/`⚠`/`✗`/`·`, redacted device state counts, optional `--fix` remediation panel, and a numbered `Next:` menu (`[1]`, ..., `[q]`)). When **both** `sys.stdin.isatty()` and `sys.stdout.isatty()` are true (interactive TTY), `adapt doctor` prompts for a `Next:` selection and can run `adapt setup` (user-scope platform-tools install), open Device Manager on Windows (`devmgmt.msc`, never Zadig), run Debian `udev`/`plugdev` `sudo` commands (for udev rules and `plugdev` group membership only — **never** `sudo adb`), or re-run `adapt doctor` until the user enters `q` (exiting with the latest doctor exit code). `--json` and non-TTY sessions (including pipes or invocations where `stdin` is not a TTY) stay text-only / JSON-only without prompting or blocking. `--fix` remains a read-only remediation command preview panel (never kills servers, runs `sudo adb`, or wipes `adbkey`), while still showing the `Next:` menu on interactive TTYs. The `--json` schema and exit-code contract remain unchanged.

---

## 2. Exit Code Contract

Exit codes are strictly ordered by precedence. When multiple failures exist simultaneously, `adapt doctor` (and the doctor runs inside `adapt setup`, reported in setup's `doctor_exit_code`; setup's own exit code follows §5.3) MUST return the highest-precedence non-zero code according to the table below:

| Precedence | Exit Code | Name | Condition | Action Permitted |
|---|---|---|---|---|
| 1 (Highest) | `3` | `UNSAFE_SHARED_SERVER` | Port `5037` (or `/tmp/5037`) is bound by `root`/`SYSTEM` (not current user), **OR** the running ADB server's version mismatches the resolved `adb` binary version. | **Report only. Do NOT run `adb kill-server`, kill processes, or spawn a conflicting server.** |
| 2 | `1` | `MISSING_TOOLCHAIN` | `adb` binary is missing, unexecutable, `< 34.0.0` (e.g., Debian `apt` `1:29.0.6`), not on `PATH` while ADAPT's commands have no usable `>= 34.0.0` copy either, or shadowed on `PATH` by an incompatible/`< 34.0.0` `adb`. (When `PATH` has no `adb` but the copy ADAPT's commands use is `>= 34.0.0`, `adb_path` and `path_config` are `warn` with exit `0`; see §3.2.) | Report missing/outdated toolchain; `adapt setup` may install user-scope `platform-tools` (unless blocked by code `3`). |
| 3 | `2` | `DEVICE_UNAUTHORIZED_OR_NO_PERMISSIONS` | Toolchain is valid (`>= 34.0.0`) and server state is safe, **AND** at least one connected device reports `unauthorized` or `no permissions`, **OR** a blocking host USB permission/driver error exists (Windows PnP Android device with `Problem != CM_PROB_NONE`, or Debian missing `udev` rule / `plugdev` on headless SSH session). | Report device/permission blocker and safe remediation steps; never wipe `adbkey` or run `sudo adb`. |
| 4 (Lowest) | `0` | `OK` | Toolchain is valid (`>= 34.0.0`, Google origin preferred/verified), `PATH` resolves cleanly, `ANDROID_HOME` (if present) is intact/untouched, port `5037` is safe (idle or same-user matching server), host USB prerequisites pass, and all `0+` connected devices are ready (`device`, etc., with zero `unauthorized` or `no permissions`). | Proceed normally. Note: `0` connected devices is `0` (`OK`) when host prerequisites pass. |

> [!IMPORTANT]
> **Preventing Accidental Client Auto-Kill:** Standard `adb` CLI commands (including `adb devices -l`) automatically print `adb server version (X) doesn't match this client (Y); killing...` and terminate the running daemon if client and server versions differ. Therefore, `adapt doctor` MUST evaluate `adb_server_5037` **before** invoking `adb devices -l`, and MUST skip invoking `adb devices -l` whenever exit code `3` is triggered.

---

## 3. Checks `adapt doctor` Must Run

`adapt doctor` MUST execute all applicable checks for the host OS and emit a deterministic result per check (`pass`, `warn`, `fail`, or `skip`).

### 3.1 Summary Matrix

| Check ID | Host OS | Failure Exit Code | Summary |
|---|---|---|---|
| `adb_path` | Windows, Linux | `1`; `warn` when only the copy ADAPT's commands use is found and it is `>= 34.0.0` | Locate `adb` on `PATH`, determine the `adb` ADAPT's commands use, and scan known user-scope / SDK fallback locations. |
| `adb_version` | Windows, Linux | `1` | Parse `adb version`; require `platform-tools` version `>= 34.0.0`. |
| `adb_origin` | Windows, Linux | `1` (if `< 34`), `warn` if unverified or distribution-packaged `>= 34` | Classify binary origin: Google official (`source.properties` / user-scope dir) vs. distribution package (`debian_apt`: `/usr/bin/adb` / `dpkg -S`) vs. unknown. |
| `path_config` | Windows, Linux | `1` (if missing from `PATH` with no usable command copy, or shadowed by `< 34`); `warn` when missing from `PATH` but the command copy is `>= 34` | Verify `PATH` resolution and detect shadowing by secondary `adb` binaries. |
| `android_home` | Windows, Debian | `warn` (never mutates) | Inspect `ANDROID_HOME` and `ANDROID_SDK_ROOT` if set; guarantee present-but-untouched invariant. |
| `windows_pnp` | Windows only (`skip` on Debian) | `2` (on Android-related PnP problem device; `warn` for other USB problem devices) | Inspect Windows PnP `USBDevice` / `AndroidDevice` status; provide safe driver hints. |
| `debian_usb_permissions` | Debian and other Linux (`skip` on Windows and macOS) | `2` (on missing rules/group blocking access; `warn` if advisory) | Inspect Android-specific `udev` rules (`/etc/udev/rules.d/` / `/lib/udev/rules.d/`), the groups those rules grant (`plugdev` / `adbusers`), and `systemd-logind` seat status. |
| `adb_server_5037` | Windows, Debian | `3` | Verify every listener on port `5037` (and `/tmp/5037` on Debian) belongs to one process owned by the current user, not `root`, and that the running server version matches client `adb` without killing. |
| `devices_state` | Windows, Debian | `2` (on `unauthorized` / `no permissions`) | Run `adb devices -l` (only if `adb >= 34.0.0` and `adb_server_5037` is safe); report per-state counts. |

---

### 3.2 Detailed Check Specifications

#### 1. `adb_path`
- **Probe:**
  - Resolve the active `adb` executable via `PATH` (`shutil.which("adb")`).
  - Determine the **command adb**: the `adb` every other ADAPT command uses (`adb.resolve_adb()`). It is the file named by `ADAPT_ADB` or `ADB_PATH` when that names an existing file, otherwise the registered managed copy described below, otherwise the first copy reporting `>= 34.0.0` among `PATH`, `$ANDROID_HOME/platform-tools`, the user-scope install folders (`%LOCALAPPDATA%\Android\platform-tools`, `%LOCALAPPDATA%\Android\Sdk\platform-tools` on Windows; `~/.local/share/android/platform-tools` on Linux), otherwise the first copy found. Its source is reported as `override`, `registered_managed_copy`, `path`, `fallback_location` or `none`.
  - **Registered managed copy (Linux and other POSIX hosts):** `adapt setup` registers its folder by appending a marker comment (`# ADAPT managed platform-tools PATH`) and an exact `export PATH=...` line to `~/.profile`. A shell that does not read `~/.profile`, such as the non-login shell an SSH command runs in (`adapt ssh run`), then finds another `adb` first, for example the distribution's `/usr/bin/adb`. The command adb is the managed copy instead, with source `registered_managed_copy`, only when all of these hold: neither `ADAPT_ADB` nor `ADB_PATH` is set (even to a missing file); `~/.profile` contains both setup lines for `~/.local/share/android/platform-tools` or `~/.android/platform-tools` (`--isolated`); no such registered folder is on the current `PATH`; a different `adb` is first on `PATH`; and the managed `adb` exists, is executable and reports `>= 34.0.0`. When the folder is on `PATH`, `PATH` order wins as before, also when another `adb` was deliberately put first. `~/.profile` is only read: regular files only, at most 1 MiB, and a missing or unreadable file means "not registered". Commands and `adapt doctor` share this one rule (`adb.find_registered_managed_adb`).
  - **Windows is unchanged:** setup registers its folder in `HKCU\Environment\Path`, which Windows places after the machine `PATH`. A new process can therefore correctly resolve another `adb` from the machine `PATH` first, so a registry entry is not evidence of which `adb` a fresh shell would use, and ADAPT keeps plain `PATH` order there.
  - Also scan known user-scope and standard locations without modifying them:
    - **Windows 11:** `%LOCALAPPDATA%\Android\platform-tools\adb.exe`, `%USERPROFILE%\.android\platform-tools\adb.exe`, `%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe`, and `%ANDROID_HOME%\platform-tools\adb.exe` (if set).
    - **Debian:** `~/.local/share/android/platform-tools/adb`, `~/.android/platform-tools/adb`, `/usr/bin/adb`, and `$ANDROID_HOME/platform-tools/adb` (if set).
- **Pass:** An executable `adb` is resolved on `PATH`.
- **Warn (`exit_code: 0`):**
  - `PATH` has no `adb`, but the command adb exists and reports `>= 34.0.0`. The summary names that copy, says in general terms where it came from (the override variable, `PATH`, or a folder ADAPT searches outside `PATH`), that ADAPT's commands use it, and that tools looking on `PATH` will not find it. Remediation: add its folder to `PATH`, or run `adapt setup` to register ADAPT's user-scope copy (setup downloads only when no healthy copy is installed, §5.1).
  - `adb` is on `PATH`, but `ADAPT_ADB` / `ADB_PATH` is set to something that is not an existing file. ADAPT's commands ignore such an override; the summary says so and names the variable (never its value).
- **Fail (`exit_code: 1`):** No `adb` binary found on `PATH`, and the command adb is absent, `< 34.0.0` or reports no version (details record any fallback copies found off-`PATH`). A set override that names no existing file is reported in the summary.
- **Details:** in addition to `resolved_adb` and `fallbacks_found`: `command_adb`, `command_adb_source`, `override_variable` (the first set override variable name, or `null`) and `override_points_to_file` (`null` when no override is set).

#### 2. `adb_version`
- **Probe:** Execute `<resolved_adb> version` with a bounded timeout (default `5s`). `<resolved_adb>` is the first `adb` on `PATH`; when `PATH` has none, it is the command adb (§3.2.1), and the summary says that this copy is not on `PATH`. `adb_version` and `adb_origin` fail as "missing" only when neither exists.
  - Parse both the ADB protocol/binary line (`Android Debug Bridge version X.Y.Z`) and the platform-tools version line (`Version MM.m.p-...`).
- **Pass:** Parsed `Version` satisfies `tuple >= (34, 0, 0)`. (Version `>= 34.0.0` is required for modern USB descriptors, Openscreen mDNS, and Android 11+ TLS wireless pairing `adb pair`).
- **Fail (`exit_code: 1`):** `adb version` fails, cannot be parsed, or reports `< 34.0.0` (such as Debian 12 Bookworm's `1:29.0.6-13`).

#### 3. `adb_origin`
- **Probe:** Determine provenance of `<resolved_adb>`:
  - **Debian:**
    - Check whether `realpath(<resolved_adb>)` is `/usr/bin/adb` or `dpkg -S <resolved_adb>` reports package `adb` -> classify as `debian_apt`.
    - Check whether `<dir>/source.properties` exists and whether `<resolved_adb>` lives in a user-scope Google unpack path (`~/.local/share/android/platform-tools`, `~/.android/platform-tools`) or valid SDK -> classify as `google_official`.
    - Otherwise classify as `unknown`.
  - **Windows 11:**
    - Check `<dir>\source.properties` (`Pkg.Revision`), official user-scope paths (`%LOCALAPPDATA%\Android\platform-tools`, `%USERPROFILE%\.android\platform-tools`, `%LOCALAPPDATA%\Android\Sdk\platform-tools`), or verified package manager shims (`winget` `Google.PlatformTools` / `scoop`) -> classify as `google_official`.
    - Otherwise classify as `unknown` (e.g., third-party emulator bundle).
  - **Other Linux:** the `/usr/bin/adb` path rule also applies, so a distribution-packaged `adb` is classified `debian_apt` there too; summaries then say "the Linux distribution" rather than Debian `apt`.
- **Pass:** `origin == "google_official"` and version `>= 34.0.0`.
- **Warn (`exit_code: 0`):** `origin == "unknown"` but version `>= 34.0.0`, **or** `origin == "debian_apt"` with version `>= 34.0.0` (newer distributions ship `>= 34`; the summary says the copy meets the minimum but is the distribution's package, not verified Google platform-tools). Such a copy is usable: `path_config` may pass, `adapt setup` treats it as healthy, and `devices_state` runs.
- **Fail (`exit_code: 1`):** `origin == "debian_apt"` with version `< 34.0.0` (for example Debian 12's `1:29.0.6`, which breaks TLS wireless pairing/mDNS) or `origin == "unknown"` with version `< 34.0.0`.

#### 4. `path_config`
- **Probe:**
  - Enumerate **all** `adb` / `adb.exe` executables across every entry in `PATH` in precedence order.
  - Inspect user-scope `PATH` configuration:
    - **Windows 11:** `HKCU\Environment\Path` (read-only via `winreg`).
    - **Debian:** Current `PATH` and user shell profiles (`~/.profile`, `~/.bashrc`, `~/.zshrc`).
- **Pass:** First `adb` on `PATH` is a valid `>= 34.0.0` binary (whatever its origin) and no conflicting older `adb` shadows it. (If multiple `adb` copies exist after the active one, emit a `warn` listing the shadowed paths).
- **Warn (`exit_code: 0`):** No `adb` on `PATH`, but the command adb (§3.2.1) reports `>= 34.0.0`; same remediation as `adb_path`.
- **Fail (`exit_code: 1`):** No `adb` on `PATH` and no usable command adb, or the first `adb` on `PATH` is `< 34.0.0` / broken even if a newer copy exists later on `PATH` or off-`PATH`.
- **Warn (`exit_code: 0`, `adb.managed_not_on_path`):** the command adb is the registered managed copy (§3.2.1). The summary names that copy, the profile file that registers it, the folder missing from `PATH` in this shell and the `adb` that plain `adb` runs here. In this case `adb_version` and `adb_origin` judge the copy ADAPT's commands use, as the server check already does; the `toolchain` PATH fields still describe `PATH`. Other tools that run plain `adb` in such a shell still get the other copy, and a client of a different version can disturb a running server, so the remediation is to add the folder to `PATH` in a startup file that kind of shell reads.
- **Details:** `path_executables`, `shadowed_executables`, `command_adb`, `command_adb_source`, `registered_managed_copy` (`null`, or `adb`, `folder`, `registration_file`, `folder_on_path` (always `false`) and `path_adb`).
- **Tests:** the registered-managed-copy rule is covered by offline tests (`tests/test_registered_managed_adb.py`) with injected environments and temporary folders. The problem was observed on a Debian 13 virtual machine. The change was re-run there once on 2026-11-14: the SSH worker used the managed copy and the server check passed. No other host has been tried.

#### 5. `android_home` (Present-but-Untouched)
- **Probe:**
  - Read `ANDROID_HOME` and `ANDROID_SDK_ROOT` from process/user environment (read-only).
  - If set: verify whether the directory exists on disk and whether `<dir>/platform-tools/adb(.exe)` exists.
- **Pass:** Either unset, or set to an existing directory (with status details recorded).
- **Warn:** Set to a non-existent path or missing `platform-tools` inside it.
- **Invariant:** `adapt doctor` and `adapt setup` MUST NEVER modify, overwrite, or unset `ANDROID_HOME` / `ANDROID_SDK_ROOT`, and MUST NEVER write into, delete from, or overwrite files inside the referenced SDK directory.

#### 6. `windows_pnp` (Windows 11 Only)
- **Probe:** Read-only query of Plug and Play devices via PowerShell / WMI / `pnputil`:
  - `Get-PnpDevice -Class "USBDevice", "AndroidDevice"` (or `pnputil /enum-devices /class AndroidDevice` and USB composite ADB interfaces matching `VID_*&PID_*&MI_*` / status != `OK`).
  - Check `Problem` code (`CM_PROB_NONE` vs non-zero error codes such as Code 52 unsigned driver block or unbound `SAMSUNG_Android` composite interface). `pnputil` output is read from `Problem Code:`, or from `Problem:` when `Problem Code:` is absent.
  - Collection stays broad so that no problem device becomes invisible: classes `USBDevice` / `AndroidDevice` / `AndroidUsbDeviceClass`, Android/ADB friendly names, and any `USB\VID_*` device with a problem. The PowerShell and `pnputil` paths MUST apply the same relevance rule.
  - A device that reports a problem is **confirmed Android-related** when its class is `AndroidDevice` or `AndroidUsbDeviceClass`, **OR** its friendly name matches Android/ADB/Pixel/Fastboot/Bootloader/Samsung mobile USB names, **OR** its instance id carries a `VID_xxxx` from a short heuristic list of Android phone and chipset vendors (for example Google `18D1`, Samsung `04E8`, Xiaomi `2717`, Qualcomm `05C6`, MediaTek `0E8D`). Membership in the generic `USBDevice` class alone does not confirm it.
- **Pass:** No detected PnP device reports a problem (or no Android PnP devices currently attached).
- **Fail (`exit_code: 2`):** One or more confirmed Android-related PnP devices report a driver problem (`Problem != CM_PROB_NONE` / `Status == Error`). They are listed in `details.problem_devices`; any other USB problem devices are still listed in `details.other_usb_problem_devices`.
- **Warn (`exit_code: 0`):** Only devices that are not confirmed Android-related report a problem. They are listed in `details.other_usb_problem_devices`, `details.problem_devices` is empty, the summary states that no Android USB driver problem was detected, and the remediation asks the user to check Device Manager if their phone is among them.
- **Details:** Whenever the probe is available, `details` MUST contain `inspected_count`, `probe_available`, `problem_devices` and `other_usb_problem_devices` (an empty list when none). Instance ids are redacted with the serial-redaction rule.
- **Remediation Hints (Safe Only):**
  - **Google Pixel / Android Dev Devices:** Official Google USB Driver (`android_winusb.inf` from `developer.android.com/studio/run/win-usb`, installed via `pnputil /add-driver android_winusb.inf /install`) or native WinUSB (WCID / MS OS 2.0).
  - **OEM Devices (e.g., Samsung Galaxy):** Official OEM USB driver (`developer.android.com/studio/run/oem-usb`, e.g., Samsung Mobile USB Composite Driver) rather than forcing WinUSB onto the USB composite parent.
  - MUST NOT mention or invoke Zadig or `bcdedit /set testsigning on`.

#### 7. `debian_usb_permissions` (Debian and Other Linux)
- **Probe:**
  - **Udev Rules:** Search `/etc/udev/rules.d/` (e.g., `51-android.rules`), `/lib/udev/rules.d/` (including `android-sdk-platform-tools-common` rules) and `/usr/lib/udev/rules.d/`. Directories that resolve to the same path are read once, and a file in `/etc/udev/rules.d/` overrides a same-named file in a later directory.
  - **Android-relevant rule files:** Comment lines (first non-blank character `#`) are ignored. A rule file is Android-relevant only if its file name contains `android` (case-insensitive), **OR** it matches the ADB USB interface (`ATTR{bInterfaceClass}=="ff"` together with `ATTR{bInterfaceSubClass}=="42"`, or an `ID_USB_INTERFACES` value containing `:ff42`), **OR** it references `adb_user`, **OR** it references `ID_DEBUG_APPLIANCE` (newer systemd grants `uaccess` to ADB/fastboot devices through that property). A bare `idVendor` or `bInterfaceClass` occurrence MUST NOT qualify, so stock files such as `69-libmtp.rules` or `70-uaccess.rules` do not count.
  - `has_uaccess_tag`, `has_plugdev_rule` (`plugdev`) and `has_adbusers_rule` (`adbusers`) are derived only from Android-relevant files, ignoring comment lines.
  - **Group Membership:** Check `os.getgroups()` plus the effective GID `os.getegid()` against `grp` for `plugdev` and `adbusers`.
  - **Effective group access:** Membership only counts when an Android-relevant rule grants that group: `plugdev` membership with a `plugdev` rule, or `adbusers` membership with an `adbusers` rule. Membership alone does not grant access. `details` reports `has_adbusers_rule` and `effective_group_access` in addition to the existing keys.
  - **Logind Seat:** Query `loginctl` / `$XDG_SESSION_ID` to determine if the current session is attached to an active local seat (`uaccess` applies) or is a headless/SSH session (effective group access is strictly required because `uaccess` does not apply over SSH).
- **Pass:** Android-relevant udev rules are present **AND** (user has an active local `logind` seat with a `uaccess` rule **OR** user has effective group access). Rule text shows how access is configured; it is not proof that a connected device is accessible.
- **Fail (`exit_code: 2`):**
  - Android-relevant udev rules are completely missing, **OR**
  - Session is headless/SSH (no local seat) and user has no effective group access, **OR**
  - Rules lack `TAG+="uaccess"` and user has no effective group access.
  - *(Note: If local seat + `uaccess` works but there is no effective group access, emit `warn` advisory for future SSH usage rather than failing.)*
- **Remediation Hints (Printed Only, Never Auto-Executed with `sudo adb`):**
  - ADAPT ships `51-android.rules` as package data (`adapt_adb/data/51-android.rules`, byte-identical to `packaging/linux/51-android.rules`). One resolver (`adapt_adb.hints.find_udev_rules_file`) locates it; every printed instruction (this remediation, the `udev` Next action, `adapt setup`'s Next steps) uses its resolved absolute path, as `sudo cp <path> /etc/udev/rules.d/51-android.rules`. Only if no copy can be found is the rule line below printed instead.
  - **Debian:** recommend `sudo apt install android-sdk-platform-tools-common` or copying ADAPT's rules file. The rule is:
    ```udev
    SUBSYSTEM=="usb", ATTR{bInterfaceClass}=="ff", ATTR{bInterfaceSubClass}=="42", ATTR{bInterfaceProtocol}=="01", TAG+="uaccess", GROUP="plugdev", MODE="0660"
    ```
  - **Other Linux (`host.os == "linux"`):** remediation MUST NOT prescribe `apt` packages or assume a `plugdev` group exists. It names the distribution's own Android udev rules package or ADAPT's rules file, and says that for SSH/headless sessions the user joins the group the rules grant, whose name (like the package name) differs between distributions.
  - Reload command: `sudo udevadm control --reload-rules && sudo udevadm trigger`
  - Group command (Debian): `sudo usermod -aG plugdev $USER`. Every instruction that changes group membership says it takes effect after logging out and back in. The interactive `udev` Next action adds the user to `plugdev` on Debian, and elsewhere only when a `plugdev` group exists.

#### 8. `adb_server_5037` (Shared-Server Ownership & Version Parity)
- **Probe (Non-Destructive, No Client Auto-Kill):**
  - Inspect OS socket/process state for `127.0.0.1:5037` (and UNIX socket `/tmp/5037` on Debian):
    - **Debian:** Inspect `/tmp/5037` `stat` UID/owner if present, and inspect any process listening on `127.0.0.1:5037` (`ss -ltnp` / `/proc/net/tcp` + `/proc/<pid>/status` UID + `realpath(/proc/<pid>/exe)`).
      - Every row of both `/proc/net/tcp` and `/proc/net/tcp6` MUST be scanned without early exit. Every `LISTEN` row on port `5037` counts, whatever its bind address (fail closed, as on Windows).
      - Ownership is evaluated over all listener rows: any row owned by `root` (while the current effective UID is not `0`) marks the listener as root-owned, and any row owned by another UID marks it as foreign. The reported owner is the most concerning listener (`root` first, then another user, otherwise the current user).
      - Listener rows with more than one distinct UID, or whose socket inodes resolve to more than one distinct PID (including every `pid=` match from the `ss -ltnp` fallback), MUST be reported as multiple listener owners (an inspection failure). One process holding both an IPv4 and an IPv6 socket is a normal dual-stack server, not multiple owners.
      - A server is verifiable only when every listener row is attributed to one single PID owned by the current user. If some listener inodes resolve to a PID and others do not, or a listener table cannot be read or contains a malformed row, the probe MUST fail closed as an inspection failure rather than report idle or verified.
    - **Windows 11:** Inspect TCP listener on `127.0.0.1:5037` (`Get-NetTCPConnection -LocalPort 5037 -State Listen` -> owning PID -> process owner & executable `Path`).
  - Also inspect `~/.android/adbkey` (Debian) / `%USERPROFILE%\.android\adbkey` (Windows) existence and owner (warn if owned by `root` on Debian; **never delete or regenerate**).
  - If a server process is listening on `5037`:
    1. Check owner UID/user: on Debian, fail immediately if owner is `root` (`uid == 0`) or a different user than current effective user; on Windows, fail if owned by `NT AUTHORITY\SYSTEM` or a different user account when inaccessible.
    2. Check version parity **without** sending commands from a mismatched `adb` client binary: run `<server_process_exe> version` on the running server's own binary path and compare its `Version` against `<resolved_adb> version`. (If the running server binary cannot be queried or its `Version` differs from `<resolved_adb> version`, fail!)
  - **Which client `adb`:** for this check (and for `devices_state`), `<resolved_adb>` is the **command adb** (§3.2.1): the copy ADAPT's own commands use and verify in their pre-flight. When no command adb resolves, it is the `PATH` `adb`. The server's executable is queried only when it is that same file. A server that ADAPT's commands started from an `ADAPT_ADB` override or a fallback folder is therefore verified, not called unsafe. `details` add `client_adb` and `client_adb_source` (`override`, `registered_managed_copy`, `path`, `fallback_location` or `none`); `client_version` is that copy's version.
  - **macOS:** ADAPT has no listener probe for macOS; it does not read Linux `/proc` state there. The check fails closed (`exit_code: 3`) with a summary saying macOS is not a supported host and the server state is unverified.
- **Pass:** Either no process is listening on `5037` (idle), **OR** the running server is owned by the current user **AND** its binary version matches `<resolved_adb>`.
- **Fail (`exit_code: 3`):**
  - Port `5037` or `/tmp/5037` is owned by `root` / another user, **OR**
  - More than one listener owner is present on port `5037`, or listener inspection fails, **OR**
  - Running server version mismatches `<resolved_adb>` version.
- **Strict Rule:** `adapt doctor` and `adapt setup` MUST report the conflicting PID, owner, and version mismatch and exit `3`. They MUST NOT invoke `adb kill-server` or terminate the running server.

#### 9. `devices_state` (`adb devices -l`)
- **Precondition:** Run **ONLY** if the command adb used by `adb_server_5037` reports `>= 34.0.0` (whatever its origin, including a distribution package) AND `adb_server_5037` passed (same user, no version mismatch). If `adb_server_5037` failed, set `devices_state` status to `skip` (to avoid `adb` client auto-killing a mismatched shared server). The listing runs with that same command adb.
- **Probe:** Run `<resolved_adb> devices -l` with a bounded timeout (default `10s`). Parse each device line into state categories:
  - `device` (ready)
  - `unauthorized` (RSA prompt pending/dismissed on device)
  - `no permissions` (Linux `udev` / `plugdev` USB access denied)
  - `offline`
  - `authorizing`
  - `connecting`
  - `bootloader` / `recovery` / `sideload`
  - `unknown`
- **Serial Hygiene:** `adapt doctor` MUST NOT write raw hardware serial numbers, device IPs, or host usernames into repository files or persistent logs. In JSON/console output, report state counts and redacted/hashed or alias-mapped identifiers (`device_count_by_state`, optional `--show-serials` only for interactive local stdout if explicitly requested, never persisted to disk).
- **Pass:** Zero devices in `unauthorized` or `no permissions` (0 connected devices is valid and passes). (Emit `warn` if any device is `offline`).
- **Fail (`exit_code: 2`):** One or more devices are in `unauthorized` or `no permissions`.

### 3.3 Host Platform and Privilege

- **`host.os`:** `windows`; `debian` for the Debian family (the `ID` or `ID_LIKE` field of `/etc/os-release` names `debian` or `ubuntu`, or, without readable os-release data, `/etc/debian_version` exists); `linux` for any other Linux; `macos` for macOS. `host.supported` is `true` for `windows`, `debian` and `linux` and `false` otherwise.
- **Unsupported host (macOS):** `adapt doctor` still reports the generic `adb` checks, skips `debian_usb_permissions` and `windows_pnp` as not applicable, fails `adb_server_5037` closed (§3.2.8) and says in human output that the platform is unsupported. `adapt setup` refuses (§5.1 step 0).
- **Root / Administrator:** `host.uid_is_root` reports an effective UID of `0` (POSIX); `host.elevated` reports an elevated Windows session. Every command that can start or talk to `adb` (`doctor`, `setup`, `devices`, `adb`, `pair`, `connect`, `everything` and the device command groups) then prints a warning once on `stderr` in human mode: as root, that any `adb` server ADAPT starts runs as root and can leave root-owned files (`~/.android/adbkey`, `/tmp/5037`) that later block normal use, so ADAPT should run as the normal user without `sudo`; when elevated, that an elevated `adb` server cannot be shared with tools in non-elevated sessions. The rendered doctor report (both renderers, from `doctor`, `setup` and the `Next:` menu) leaves its own copy of that line out when the command already printed it, so a human CLI run shows the warning exactly once across stdout and stderr. When the report is rendered without the CLI having warned (the interactive console, library use), it starts with the same warning line. It never appears with `--json` (`host.uid_is_root` and `host.elevated` carry the facts), never prompts and never changes an exit code.

---

## 4. Machine-Checkable JSON Contract

When `--json` is passed to `adapt doctor` (or `adapt setup`), `stdout` MUST emit a single JSON object conforming to this schema:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["schema_version", "command", "host", "exit_code", "status", "checks", "devices"],
  "properties": {
    "schema_version": { "type": "string", "const": "1" },
    "command": { "type": "string", "enum": ["doctor", "setup"] },
    "host": {
      "type": "object",
      "required": ["os", "arch", "uid_is_root"],
      "properties": {
        "os": { "type": "string", "enum": ["windows", "debian", "linux", "macos"] },
        "arch": { "type": "string" },
        "uid_is_root": { "type": "boolean" },
        "seat": { "type": ["string", "null"], "enum": ["local", "headless", null] },
        "elevated": { "type": "boolean", "description": "Elevated (Administrator) Windows session; false elsewhere." },
        "supported": { "type": "boolean", "description": "false for hosts ADAPT does not support (macOS)." }
      }
    },
    "exit_code": { "type": "integer", "enum": [0, 1, 2, 3] },
    "status": {
      "type": "string",
      "enum": ["ok", "missing_toolchain", "device_unauthorized_or_no_permissions", "unsafe_shared_server"]
    },
    "toolchain": {
      "type": "object",
      "required": ["adb_path", "platform_tools_version", "origin", "android_home_present"],
      "properties": {
        "adb_path": { "type": ["string", "null"], "description": "First adb on PATH." },
        "platform_tools_version": { "type": ["string", "null"], "description": "Version of the PATH adb." },
        "origin": { "type": "string", "enum": ["google_official", "debian_apt", "unknown", "none"], "description": "Origin of the PATH adb." },
        "android_home_present": { "type": "boolean" },
        "command_adb_path": { "type": ["string", "null"], "description": "The adb ADAPT's commands use (§3.2.1)." },
        "command_adb_source": { "type": "string", "enum": ["override", "registered_managed_copy", "path", "fallback_location", "none"] },
        "command_adb_version": { "type": ["string", "null"] },
        "command_adb_origin": { "type": "string", "enum": ["google_official", "debian_apt", "unknown", "none"] },
        "registered_managed_copy": { "type": ["object", "null"], "description": "Set when the command adb is the managed copy setup registered in ~/.profile and this PATH lacks its folder (§3.2.4)." }
      }
    },
    "checks": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["id", "status", "exit_code", "summary"],
        "properties": {
          "id": {
            "type": "string",
            "enum": [
              "adb_path",
              "adb_version",
              "adb_origin",
              "path_config",
              "android_home",
              "windows_pnp",
              "debian_usb_permissions",
              "adb_server_5037",
              "devices_state"
            ]
          },
          "status": { "type": "string", "enum": ["pass", "warn", "fail", "skip"] },
          "exit_code": { "type": "integer", "enum": [0, 1, 2, 3] },
          "summary": { "type": "string" },
          "details": { "type": "object" },
          "remediation": { "type": ["string", "null"] },
          "failure_id": {
            "type": ["string", "null"],
            "description": "Failure-guide ID of the branch this check took (for example `adb.too_old`, `host.udev_missing`, `device.unauthorized`); null when the check passed or was skipped. `adapt explain ID` shows the guide. Every doctor and setup report sets it on every check."
          }
        }
      }
    },
    "devices": {
      "type": "object",
      "required": ["total", "by_state"],
      "properties": {
        "total": { "type": "integer", "minimum": 0 },
        "by_state": {
          "type": "object",
          "properties": {
            "device": { "type": "integer", "minimum": 0 },
            "unauthorized": { "type": "integer", "minimum": 0 },
            "no_permissions": { "type": "integer", "minimum": 0 },
            "offline": { "type": "integer", "minimum": 0 },
            "other": { "type": "integer", "minimum": 0 }
          }
        }
      }
    },
    "setup": {
      "description": "Present only for `adapt setup`.",
      "type": "object",
      "required": ["action", "dry_run", "no_path", "target_dir", "url", "downloaded", "path_updated"],
      "properties": {
        "action": {
          "type": "string",
          "enum": [
            "installed",
            "noop_already_healthy",
            "path_registered",
            "noop_already_installed",
            "dry_run",
            "aborted_unsafe_server",
            "aborted_needs_yes",
            "aborted_unconfirmed",
            "unsupported_architecture",
            "unsupported_platform",
            "failed"
          ]
        },
        "reused_existing": { "type": "boolean", "description": "Present (true) when setup reused a healthy copy in its own install folder instead of downloading (§5.1 step 2b)." },
        "would_download": { "type": "boolean", "description": "dry_run only." },
        "would_update_path": { "type": "boolean", "description": "dry_run only." },
        "dry_run": { "type": "boolean" },
        "no_path": { "type": "boolean" },
        "target_dir": { "type": ["string", "null"] },
        "url": { "type": ["string", "null"] },
        "downloaded": { "type": "boolean" },
        "path_updated": { "type": "boolean" },
        "error": { "type": "string" }
      }
    },
    "doctor_exit_code": {
      "description": "Setup reports only: the exit code of the doctor run the report describes (pre-flight, or post-setup after `installed`/`path_registered`), before setup's own exit rule (§5.3). `exit_code` is setup's exit code.",
      "type": "integer", "enum": [0, 1, 2, 3]
    },
    "doctor_status": {
      "description": "Setup reports only: the doctor run's status; equal to `status`.",
      "type": "string"
    },
    "error": {
      "description": "Present for setup actions `failed`, `unsupported_architecture` and `unsupported_platform`; same text as `setup.error`.",
      "type": "string"
    },
    "failure": {
      "description": "Setup reports only. Present when setup.action is `failed`, `unsupported_architecture`, `unsupported_platform` or `aborted_*`: the failure guidance object (see docs/help.md, Failure guidance). `adapt doctor` reports never carry it; the checks are the diagnosis.",
      "type": "object",
      "required": ["schema", "id", "summary", "certainty", "causes", "recommendations", "avoid", "help_topic", "docs", "explain", "context_unknown"]
    }
  }
}
```

### Status-to-Exit-Code Mapping (Exact)

| `exit_code` | `status` |
|---|---|
| `0` | `"ok"` |
| `1` | `"missing_toolchain"` |
| `2` | `"device_unauthorized_or_no_permissions"` |
| `3` | `"unsafe_shared_server"` |

`adapt setup` is the one exception (see §5.3): `status` and `doctor_exit_code` always follow this table, but `exit_code` is setup's own. When setup did not do what was asked, a healthy host's `exit_code` becomes `1`, so `"status": "ok"` can appear with `"exit_code": 1`. When setup succeeded and the toolchain checks pass, `exit_code` is `0` even if other findings remain, so for example `"status": "device_unauthorized_or_no_permissions"` can appear with `"exit_code": 0` and `"doctor_exit_code": 2`.

---

## 5. What `adapt setup` May Do

`adapt setup` automates **user-scope** ADB provisioning without administrative privileges (`sudo` / UAC) and without disturbing existing shared servers or SDKs.

### 5.1 Pre-Flight Gate
0. On a host that is not Windows or Linux (`host.supported == false`, i.e. macOS), `adapt setup` stops with action `unsupported_platform` and an error naming the platform, before the gates below. Nothing is downloaded or changed, even with `--dry-run`. `target_dir` and `url` are `null`; the report shape and exit code follow `unsupported_architecture` (not-done rule, so an unverifiable listener still yields `3`). No later step could act on such a host, so deciding this first changes no safety outcome.
1. Run `adb_server_5037` check **first**. If port `5037` is owned by `root`/another user or has a running server mismatch, `adapt setup` MUST abort immediately with exit code `3` and MUST NOT kill the server.
2. Check if a valid `adb >= 34.0.0` is already installed and first on `PATH` (unless `--force` is passed to repair/re-install into user scope): `adb_path`, `adb_version`, `adb_origin` and `path_config` all have exit code `0` **and** `PATH` has an `adb`. A distribution-packaged `adb >= 34.0.0` counts as healthy (its origin is a `warn`). If already healthy, `adapt setup` stops without downloading (`noop_already_healthy`) and exits `0` (§5.3 success rule). A usable copy that is only off `PATH` is not "healthy" here; step 2b applies.
   - **2b. Existing healthy copy, PATH registration missing:** if setup's own install folder (the target directory, honouring `--isolated`) already holds an `adb` that exists, starts and reports `>= 34.0.0`, and is classified `google_official`, and the first `adb` on `PATH` is not that copy, setup does **not** download again (unless `--force`). It registers that folder on `PATH` instead, with the same mechanism as §5.2 step 4: action `path_registered`, `downloaded: false`, `path_updated: true`, `reused_existing: true`, followed by the post-setup doctor run. With `--dry-run` the action is `dry_run` with `would_download: false` and `would_update_path` reflecting `--no-path`. With `--no-path` nothing is changed: action `noop_already_installed`. A `PATH` change is a user-profile change, so it needs the same confirmation as a download (step 4): without `--yes` and without a terminal, `aborted_needs_yes`; a declined prompt, `aborted_unconfirmed`. If the `PATH` update itself fails, the action is `failed` with `reused_existing: true` and the error `Could not add <dir> to your user PATH.`
3. On Debian/Linux hosts, check the host architecture. The official Linux platform-tools archive contains x86_64 binaries only, so on any other architecture (for example `aarch64`) `adapt setup` stops with action `unsupported_architecture` and an error naming the detected architecture. Nothing is downloaded, even with `--dry-run`. Windows hosts are not gated.
4. Before a mutating download/unpack, or a `PATH`-only registration (step 2b), `adapt setup` prompts on `stderr` for confirmation unless `--yes`/`-y` is passed. When ADAPT cannot show a prompt (input or error output is not a terminal), `adapt setup` stops before changing anything unless `--yes` is given.

### 5.2 Allowed Actions
1. **Download Official Google Platform-Tools Zip (HTTPS Only):**
   - **Windows 11:** `https://dl.google.com/android/repository/platform-tools-latest-windows.zip`
   - **Debian:** `https://dl.google.com/android/repository/platform-tools-latest-linux.zip`
   - Stream download to a bounded staging path inside the target user cache/install parent (clean up the zip immediately after extraction/verification; never accumulate unbounded files in `%TEMP%` or `/tmp`).
2. **Verify Integrity Before Unpacking:**
   - Enforce TLS (`https://`) to `dl.google.com`.
   - Verify the downloaded archive against **SHA-256** when published or provided via `--sha256`, or fall back to **SHA-1** from the official Google repository XML (`https://dl.google.com/android/repository/repository2-3.xml`); always compute and record the archive's SHA-256 in output. Setup prefers an XML archive `<url>` equal to the downloaded filename, else stable channel-0 (or missing `channelRef`) SHA-256/SHA-1, and must not use a later channel digest to verify `latest-*`.
   - Validate zip entries against path traversal (`..` / absolute paths / zip-slip) before extracting any file.
3. **Unpack to User-Scope Directories Named in Research:**
   - **Windows 11:**
     - Primary: `%LOCALAPPDATA%\Android\platform-tools`
     - Isolated alternative (`--isolated`): `%USERPROFILE%\.android\platform-tools`
   - **Debian:**
     - Primary: `~/.local/share/android/platform-tools`
     - Isolated alternative (`--isolated`): `~/.android/platform-tools`
   - Extract into a temporary sibling staging directory, verify `<staging>/adb(.exe) version` reports `>= 34.0.0`, and atomically swap into the target directory.
   - **Never** unpack into `C:\Program Files`, `/usr/bin`, `/usr/local/bin`, or inside `$ANDROID_HOME` / `$ANDROID_SDK_ROOT`.
4. **Configure User-Scope `PATH` Only:**
   - **Windows 11:** Read and update `HKCU\Environment\Path` via `winreg` (idempotent; no duplicate entries), broadcast `WM_SETTINGCHANGE`, and update the current process environment. If an older `adb.exe` exists in another user `PATH` entry, place the user-scope `platform-tools` directory ahead of it in user `PATH` (or warn if a system `HKLM` `PATH` entry shadows it). **Never** write to `HKLM`.
   - **Debian:** Idempotently add a managed block to `~/.profile` prepending `$HOME/.local/share/android/platform-tools` (or selected isolated dir) to `PATH` so it takes precedence over `/usr/bin/adb` if Debian `apt` `adb` is installed. ADAPT does not edit `.bashrc`, `.zshrc`, `/etc/environment` or `/etc/profile`.
5. **Post-Setup Verification & Safe OS Hints:**
   - Re-run `adapt doctor` checks, report them (with the resulting code in `doctor_exit_code`) and exit by the §5.3 success rule: `0` when the toolchain checks pass, otherwise the doctor's code.
   - Print safe, copy-pasteable OS instructions if `windows_pnp` (`pnputil` / Google USB INF / OEM driver page) or `debian_usb_permissions` (`udev` rules / `plugdev` group) need attention.

### 5.3 Setup Outcomes and Exit Codes
`setup.action` reports what happened:

| `setup.action` | Meaning | Exit code |
|---|---|---|
| `installed` | Downloaded, unpacked and (unless `--no-path`) put on `PATH`. | Success rule (post-setup doctor run). |
| `noop_already_healthy` | Toolchain already healthy; nothing downloaded. | Success rule (pre-flight doctor run). |
| `path_registered` | Healthy copy already in setup's install folder; its folder was put on `PATH`; nothing downloaded (§5.1 step 2b). | Success rule (post-setup doctor run). |
| `noop_already_installed` | Healthy copy already in setup's install folder, but `--no-path` was given; nothing changed. | Success rule (pre-flight doctor run). |
| `dry_run` | `--dry-run`; reports `would_download` / `would_update_path`. | Pre-flight exit code (unchanged: a dry run installs nothing, so it reports the host as it is). |
| `aborted_unsafe_server` | Port `5037` gate failed (§5.1 step 1). | `3`. |
| `aborted_needs_yes` | No terminal to confirm and no `--yes`. | Not-done rule. |
| `aborted_unconfirmed` | User declined the confirmation prompt. | Not-done rule. |
| `unsupported_architecture` | Non-x86_64 Debian/Linux host (§5.1 step 3). | Not-done rule. |
| `unsupported_platform` | Host is not Windows or Linux, e.g. macOS (§5.1 step 0). | Not-done rule. |
| `failed` | Download, verification or unpack raised an error. | Not-done rule. |

**Success rule:** when setup did what was asked (or nothing was needed) and the toolchain checks (`adb_path`, `adb_version`, `adb_origin`, `path_config`) all have exit code `0`, setup exits `0`. Remaining findings that are not about the toolchain (USB permissions or drivers, device state, the shared server on port `5037`) are still reported in the checks, in the human doctor report and `Next:` steps, and in `status` / `doctor_status` / `doctor_exit_code`, but they do not change setup's exit code. When the toolchain checks still fail afterwards, setup exits with the doctor's code (normally `1`). In an interactive terminal, setup exits with this code after the `Next:` menu closes.

**Not-done rule:** when setup did not do what was asked, a pre-flight exit code of `0` becomes `1`; a non-zero pre-flight exit code (`1`, `2` or `3`) is kept, following the normal precedence. `status` is not changed and keeps describing host health. Every setup report carries the doctor run's own result in `doctor_status` and `doctor_exit_code`. The process exit code always equals the report's `exit_code`; the human doctor report printed by setup shows the doctor's own status line (`Status: <status> (exit code <doctor_exit_code>)`).

For `failed`, `unsupported_architecture` and `unsupported_platform`, the report carries the error text in both `setup.error` and a top-level `error`. With `--json`, stdout is still the full report described in §4. Human output prints `Error: <message>` on `stderr` and exits without printing the doctor report or the next-actions menu.

**Failure guidance.** For `failed`, `unsupported_architecture`, `unsupported_platform` and every `aborted_*` action, `--json` adds a top-level `failure` object (additive; existing keys are unchanged). Its `id` names the situation, for example `setup.digest_unavailable`, `setup.download_failed`, `setup.integrity_failed`, `setup.needs_confirmation`, `setup.declined`, `setup.unsupported_arch`, `setup.unsupported_platform`, `setup.path_update_failed` (a `failed` step 2b registration) or, for `aborted_unsafe_server`, the `failure_id` of the `adb_server_5037` check (an `adb.server.*` guide, or `host.unsupported_platform`). When `adapt setup --json` itself raises before producing a report, the error payload `{"schema_version": "1", "command": "setup", "exit_code": 1, "status": "missing_toolchain", "error": ...}` gains the same `failure` key. Human output keeps its first error line and adds the guidance on `stderr` after it; `adapt explain ID` shows the full guide. A successful install that updated `PATH` adds one line telling you to reopen terminals that were already open.

Human setup output for `path_registered` and `noop_already_installed` says that healthy platform-tools are already installed in the target directory and that nothing was downloaded; `path_registered` also prints the reopen-terminals line. A step 2b dry run prints `[DRY-RUN] Would not download: healthy platform-tools are already installed.` instead of the download URL. The non-interactive `Next:` steps on Linux print the resolved rules-file path (§3.2.7) and the log-out note.

In plain human doctor output, when `PATH` has no `adb` but the command adb exists, the `ADB Path:` line reads `Not on PATH (ADAPT commands use <path>)`, the `ADB Version:` and `Origin:` lines describe that copy (the JSON `toolchain.platform_tools_version` and `origin` keep describing the `PATH` adb), and the closing hint suggests `adapt setup` to put platform-tools on `PATH`; "download and install" is suggested only when no usable copy exists. Host notes (§3.3) appear right after the `OS:` line.

Human doctor output (both the plain and the terminal renderer) ends with `Run 'adapt doctor --fix' to see how to fix the items above.` when a check is `fail` or `warn` and has remediation text, and `--fix` was not given. The JSON report is unchanged.

**Failure IDs and the single source of remediation text.** Every check in a doctor or setup report carries `failure_id` (§4): the failure-guide ID of the branch it took, or `null` for `pass` and `skip`. Distinct failing branches of one check have distinct IDs; for example `adb_server_5037` uses `adb.server.endpoint_unverified`, `host.unsupported_platform`, `adb.server.probe_failed`, `adb.server.foreign_owner`, `adb.server.version_mismatch`, `adb.server.unverified` and, for its warning, `adb.adbkey_root_owned`. The `remediation` text of each branch now lives in the failure-guide registry (`adapt_adb/failure_guide.py`, `doctor_remediation`); `run_doctor_checks` reads it from there and fills in values such as ADAPT's rules-file path. The text itself is unchanged. In the terminal renderer, each `fail` or `warn` check with an ID is followed by one line `More: adapt explain ID`. The plain renderer (redirected output) adds one line before the `--fix` pointer, `More: adapt explain FIRST_ID  |  also: OTHER_IDS`, and with `--fix` each remediation line ends with `(adapt explain ID)`. `ADAPT_FAILURE_GUIDANCE=off` removes these pointers.

---

## 6. Forbidden Operations (Hard Guardrails)

Neither `adapt doctor` nor `adapt setup` (nor any helper they invoke) may **ever** perform or emit commands recommending the following forbidden operations:

| # | Forbidden Operation | Scope | Rationale | Verification Rule |
|---|---|---|---|---|
| 1 | `adb kill-server` | Runtime & remediation text | Destroys shared ADB server (`localhost:5037`) used concurrently by other agents, IDEs, test runners, and `scrcpy` mirrors. | Never invoke `adb` with `kill-server` in `doctor` or `setup`; never trigger implicit client auto-kill on version mismatch. |
| 2 | `sudo adb` | Runtime & remediation text | Running `adb` as root creates root-owned `/tmp/5037` and `~/.android/adbkey`, breaking unprivileged users and agents. | Never prefix `adb` with `sudo`; `adapt` runs strictly unprivileged. |
| 3 | Overwrite `ANDROID_HOME` | Env & filesystem | Mutating `ANDROID_HOME` (or `ANDROID_SDK_ROOT`) or overwriting files inside it breaks Android Studio, Gradle, and user SDKs. | `ANDROID_HOME` and `ANDROID_SDK_ROOT` are strictly read-only; never unpack into or modify them. |
| 4 | Wipe `adbkey` | `~/.android/adbkey*`, `%USERPROFILE%\.android\adbkey*` | Deleting or resetting host RSA keys revokes authorization across all previously paired physical devices. | Never delete, truncate, overwrite, or move `adbkey` or `adbkey.pub`. |
| 5 | `Zadig` | Runtime & remediation text | Forcing WinUSB via Zadig onto USB composite parents corrupts MTP, tethering, and OEM driver stacks on Windows 11. | Never invoke, download, or recommend Zadig. |
| 6 | `bcdedit` `testsigning` | Runtime & remediation text | Disabling Windows Driver Signature Enforcement (`bcdedit /set testsigning on`) compromises OS security. | Never invoke or recommend `bcdedit` or disabling driver signature enforcement. |
| 7 | `logcat` without `-d` | Any `adb logcat` invocation | Streaming `logcat` without `-d` blocks indefinitely, hanging CLI/agent invocations and exhausting buffers. | `doctor` and `setup` do not need `logcat`; any future `adb logcat` call MUST include `-d` (plus bounded timeout). |
| 8 | Unbounded `TEMP` logs | `%TEMP%`, `/tmp`, `tempfile` | Unbounded log files or uncleaned zip/capture artifacts exhaust disk space and leak host/device metadata. | All temporary downloads/logs MUST be size-bounded, rotated/capped, and cleaned up on completion or failure; no raw serials written to disk. |
