# `adapt doctor` and `adapt setup` Contract

**Version:** `1.0`  
**Applies to:** `adapt` (`adapt-adb`, v1 Python 3.11+ / `uv`) on **Windows 11** and **Debian** (stable/testing).

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

Exit codes are strictly ordered by precedence. When multiple failures exist simultaneously, `adapt doctor` (and `adapt setup` pre-flight/post-flight) MUST return the highest-precedence non-zero code according to the table below:

| Precedence | Exit Code | Name | Condition | Action Permitted |
|---|---|---|---|---|
| 1 (Highest) | `3` | `UNSAFE_SHARED_SERVER` | Port `5037` (or `/tmp/5037`) is bound by `root`/`SYSTEM` (not current user), **OR** the running ADB server's version mismatches the resolved `adb` binary version. | **Report only. Do NOT run `adb kill-server`, kill processes, or spawn a conflicting server.** |
| 2 | `1` | `MISSING_TOOLCHAIN` | `adb` binary is missing, unexecutable, `< 34.0.0` (e.g., Debian `apt` `1:29.0.6`), not on `PATH`, or shadowed on `PATH` by an incompatible/`< 34.0.0` `adb`. | Report missing/outdated toolchain; `adapt setup` may install user-scope `platform-tools` (unless blocked by code `3`). |
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
| `adb_path` | Windows, Debian | `1` | Locate `adb` on `PATH` and in known user-scope / SDK fallback locations. |
| `adb_version` | Windows, Debian | `1` | Parse `adb version`; require `platform-tools` version `>= 34.0.0`. |
| `adb_origin` | Windows, Debian | `1` (if `< 34` or Debian `apt`), `warn` if unverified | Classify binary origin: Google official (`source.properties` / user-scope dir) vs. Debian `apt` (`/usr/bin/adb` / `dpkg -S`) vs. unknown. |
| `path_config` | Windows, Debian | `1` (if missing from `PATH` or shadowed by `< 34`) | Verify `PATH` resolution and detect shadowing by secondary `adb` binaries. |
| `android_home` | Windows, Debian | `warn` (never mutates) | Inspect `ANDROID_HOME` and `ANDROID_SDK_ROOT` if set; guarantee present-but-untouched invariant. |
| `windows_pnp` | Windows only (`skip` on Debian) | `2` (on PnP problem device) | Inspect Windows PnP `USBDevice` / `AndroidDevice` status; provide safe driver hints. |
| `debian_usb_permissions` | Debian only (`skip` on Windows) | `2` (on missing rules/group blocking access; `warn` if advisory) | Inspect `udev` rules (`/etc/udev/rules.d/` / `/lib/udev/rules.d/`), `plugdev` group, and `systemd-logind` seat status. |
| `adb_server_5037` | Windows, Debian | `3` | Verify port `5037` (and `/tmp/5037` on Debian) is not owned by `root` and running server version matches client `adb` without killing. |
| `devices_state` | Windows, Debian | `2` (on `unauthorized` / `no permissions`) | Run `adb devices -l` (only if `adb >= 34.0.0` and `adb_server_5037` is safe); report per-state counts. |

---

### 3.2 Detailed Check Specifications

#### 1. `adb_path`
- **Probe:**
  - Resolve the active `adb` executable via `PATH` (`shutil.which("adb")`).
  - Also scan known user-scope and standard locations without modifying them:
    - **Windows 11:** `%LOCALAPPDATA%\Android\platform-tools\adb.exe`, `%USERPROFILE%\.android\platform-tools\adb.exe`, `%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe`, and `%ANDROID_HOME%\platform-tools\adb.exe` (if set).
    - **Debian:** `~/.local/share/android/platform-tools/adb`, `~/.android/platform-tools/adb`, `/usr/bin/adb`, and `$ANDROID_HOME/platform-tools/adb` (if set).
- **Pass:** An executable `adb` is resolved on `PATH`.
- **Fail (`exit_code: 1`):** No `adb` binary found on `PATH` (details record any fallback copies found off-`PATH`).

#### 2. `adb_version`
- **Probe:** Execute `<resolved_adb> version` with a bounded timeout (default `5s`).
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
- **Pass:** `origin == "google_official"` and version `>= 34.0.0`.
- **Warn:** `origin == "unknown"` but version `>= 34.0.0`.
- **Fail (`exit_code: 1`):** `origin == "debian_apt"` (Debian `apt` `adb` lags 5+ years behind official platform-tools and breaks TLS wireless pairing/mDNS) or `origin == "unknown"` with version `< 34.0.0`.

#### 4. `path_config`
- **Probe:**
  - Enumerate **all** `adb` / `adb.exe` executables across every entry in `PATH` in precedence order.
  - Inspect user-scope `PATH` configuration:
    - **Windows 11:** `HKCU\Environment\Path` (read-only via `winreg`).
    - **Debian:** Current `PATH` and user shell profiles (`~/.profile`, `~/.bashrc`, `~/.zshrc`).
- **Pass:** First `adb` on `PATH` is a valid `>= 34.0.0` binary and no conflicting older `adb` shadows it. (If multiple `adb` copies exist after the active one, emit a `warn` listing the shadowed paths).
- **Fail (`exit_code: 1`):** No `adb` on `PATH`, or the first `adb` on `PATH` is `< 34.0.0` / broken even if a newer copy exists later on `PATH` or off-`PATH`.

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
  - Check `Problem` code (`CM_PROB_NONE` vs non-zero error codes such as Code 52 unsigned driver block or unbound `SAMSUNG_Android` composite interface).
- **Pass:** All detected Android/ADB PnP devices report `CM_PROB_NONE` / `OK` (or no Android PnP devices currently attached).
- **Fail (`exit_code: 2`):** One or more Android/ADB PnP devices report a driver problem (`Problem != CM_PROB_NONE` / `Status == Error`).
- **Remediation Hints (Safe Only):**
  - **Google Pixel / Android Dev Devices:** Official Google USB Driver (`android_winusb.inf` from `developer.android.com/studio/run/win-usb`, installed via `pnputil /add-driver android_winusb.inf /install`) or native WinUSB (WCID / MS OS 2.0).
  - **OEM Devices (e.g., Samsung Galaxy):** Official OEM USB driver (`developer.android.com/studio/run/oem-usb`, e.g., Samsung Mobile USB Composite Driver) rather than forcing WinUSB onto the USB composite parent.
  - MUST NOT mention or invoke Zadig or `bcdedit /set testsigning on`.

#### 7. `debian_usb_permissions` (Debian Only)
- **Probe:**
  - **Udev Rules:** Search `/etc/udev/rules.d/` (e.g., `51-android.rules`) and `/lib/udev/rules.d/` (including `android-sdk-platform-tools-common` rules) for Android USB / ADB interface rules (`bInterfaceClass=="ff"`, `bInterfaceSubClass=="42"`, `bInterfaceProtocol=="01"`, or vendor `idVendor` rules containing `TAG+="uaccess"` or `GROUP="plugdev"` / `GROUP="adbusers"`).
  - **Group Membership:** Check `os.getgroups()` / `grp` for `plugdev` (and note `adbusers` if applicable).
  - **Logind Seat:** Query `loginctl` / `$XDG_SESSION_ID` to determine if the current session is attached to an active local seat (`uaccess` applies) or is a headless/SSH session (`plugdev` group membership is strictly required because `uaccess` does not apply over SSH).
- **Pass:** Udev rules are present **AND** (user has an active local `logind` seat with `uaccess` rule **OR** user belongs to `plugdev`).
- **Fail (`exit_code: 2`):**
  - Udev rules for Android/ADB are completely missing, **OR**
  - Session is headless/SSH (no local seat) and user is not in `plugdev` (or rules lack `TAG+="uaccess"` on a local seat without `plugdev`).
  - *(Note: If local seat + `uaccess` works but `plugdev` is absent, emit `warn` advisory for future SSH usage rather than failing.)*
- **Remediation Hints (Printed Only, Never Auto-Executed with `sudo adb`):**
  - Recommend `sudo apt install android-sdk-platform-tools-common` or installing `/etc/udev/rules.d/51-android.rules` with:
    ```udev
    SUBSYSTEM=="usb", ATTR{bInterfaceClass}=="ff", ATTR{bInterfaceSubClass}=="42", ATTR{bInterfaceProtocol}=="01", TAG+="uaccess", GROUP="plugdev", MODE="0660"
    ```
  - Reload command: `sudo udevadm control --reload-rules && sudo udevadm trigger`
  - Group command: `sudo usermod -aG plugdev $USER` (followed by re-login).

#### 8. `adb_server_5037` (Shared-Server Ownership & Version Parity)
- **Probe (Non-Destructive, No Client Auto-Kill):**
  - Inspect OS socket/process state for `127.0.0.1:5037` (and UNIX socket `/tmp/5037` on Debian):
    - **Debian:** Inspect `/tmp/5037` `stat` UID/owner if present, and inspect any process listening on `127.0.0.1:5037` (`ss -ltnp` / `/proc/net/tcp` + `/proc/<pid>/status` UID + `realpath(/proc/<pid>/exe)`).
    - **Windows 11:** Inspect TCP listener on `127.0.0.1:5037` (`Get-NetTCPConnection -LocalPort 5037 -State Listen` -> owning PID -> process owner & executable `Path`).
  - Also inspect `~/.android/adbkey` (Debian) / `%USERPROFILE%\.android\adbkey` (Windows) existence and owner (warn if owned by `root` on Debian; **never delete or regenerate**).
  - If a server process is listening on `5037`:
    1. Check owner UID/user: on Debian, fail immediately if owner is `root` (`uid == 0`) or a different user than current effective user; on Windows, fail if owned by `NT AUTHORITY\SYSTEM` or a different user account when inaccessible.
    2. Check version parity **without** sending commands from a mismatched `adb` client binary: run `<server_process_exe> version` on the running server's own binary path and compare its `Version` against `<resolved_adb> version`. (If the running server binary cannot be queried or its `Version` differs from `<resolved_adb> version`, fail!)
- **Pass:** Either no process is listening on `5037` (idle), **OR** the running server is owned by the current user **AND** its binary version matches `<resolved_adb>`.
- **Fail (`exit_code: 3`):**
  - Port `5037` or `/tmp/5037` is owned by `root` / another user, **OR**
  - Running server version mismatches `<resolved_adb>` version.
- **Strict Rule:** `adapt doctor` and `adapt setup` MUST report the conflicting PID, owner, and version mismatch and exit `3`. They MUST NOT invoke `adb kill-server` or terminate the running server.

#### 9. `devices_state` (`adb devices -l`)
- **Precondition:** Run **ONLY** if `adb_version` passed (`>= 34.0.0`) AND `adb_server_5037` passed (same user, no version mismatch). If `adb_server_5037` failed, set `devices_state` status to `skip` (to avoid `adb` client auto-killing a mismatched shared server).
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
        "os": { "type": "string", "enum": ["windows", "debian", "linux"] },
        "arch": { "type": "string" },
        "uid_is_root": { "type": "boolean" },
        "seat": { "type": ["string", "null"], "enum": ["local", "headless", null] }
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
        "adb_path": { "type": ["string", "null"] },
        "platform_tools_version": { "type": ["string", "null"] },
        "origin": { "type": "string", "enum": ["google_official", "debian_apt", "unknown", "none"] },
        "android_home_present": { "type": "boolean" }
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
          "remediation": { "type": ["string", "null"] }
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

---

## 5. What `adapt setup` May Do

`adapt setup` automates **user-scope** ADB provisioning without administrative privileges (`sudo` / UAC) and without disturbing existing shared servers or SDKs.

### 5.1 Pre-Flight Gate
1. Run `adb_server_5037` check **first**. If port `5037` is owned by `root`/another user or has a running server mismatch, `adapt setup` MUST abort immediately with exit code `3` and MUST NOT kill the server.
2. Check if a valid Google `adb >= 34.0.0` is already installed and first on `PATH` (unless `--force` is passed to repair/re-install into user scope). If already healthy, `adapt setup` exits `0` without downloading.
3. Before a mutating download/unpack, `adapt setup` prompts on `stderr` for confirmation unless `--yes`/`-y` is passed. When ADAPT cannot show a prompt (input or error output is not a terminal), `adapt setup` stops before changing anything unless `--yes` is given.

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
   - Re-run `adapt doctor` checks and return the resulting exit code (`0`, `1`, `2`, or `3`).
   - Print safe, copy-pasteable OS instructions if `windows_pnp` (`pnputil` / Google USB INF / OEM driver page) or `debian_usb_permissions` (`udev` rules / `plugdev` group) need attention.

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
