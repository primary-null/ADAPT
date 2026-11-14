# ADAPT Windows 11 Packaging & Setup

Host setup instructions and driver guidance for running **ADAPT** (`adapt`) on Windows 11 without requiring a full Android Studio installation.

---

## 1. Obtaining `platform-tools` & User-Scope Installation

ADAPT wraps Google's official standalone Android SDK Platform-Tools (`adb.exe`, `fastboot.exe`, and supporting DLLs).

### Option A: Standalone Zip (`adapt setup` / Direct Download)
1. Download the official Windows archive from Google CDN:
   `https://dl.google.com/android/repository/platform-tools-latest-windows.zip`
2. Verify the SHA-256 hash against the [official Platform-Tools release notes](https://developer.android.com/tools/releases/platform-tools).
3. Unpack into a non-elevated, user-scope directory without spaces:
   - `%LOCALAPPDATA%\Android\platform-tools` *(recommended)*
   - `%USERPROFILE%\.android\platform-tools`

### Option B: WinGet (Optional)
On Windows 11, you can optionally install Google's official package via `winget`, which fetches directly from Google's CDN and validates the SHA-256 checksum:

```powershell
winget install --id Google.PlatformTools --scope user
```

---

## 2. User-Scope `PATH` Configuration

Never modify the machine-wide `HKLM` `PATH` (which requires Administrator elevation and UAC prompts). Always register `platform-tools` in the **user-scope** `PATH` (`HKCU\Environment`):

```powershell
$targetDir = "$env:LOCALAPPDATA\Android\platform-tools"
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -split ';' -notcontains $targetDir) {
    [Environment]::SetEnvironmentVariable("Path", "$userPath;$targetDir", "User")
}
$env:Path += ";$targetDir"
```

Verify installation with `adb version` (ensure version `>= 34.0.0` and check `Get-Command adb -All` so outdated third-party `adb.exe` binaries do not shadow the official binary).

---

## 3. USB Drivers: Google USB Driver vs. Samsung OEM

Windows 11 requires a signed driver bound to the phone's composite ADB interface (USB class `0xFF`, subclass `0x42`, protocol `0x01`).

| Device Family | Required Driver | Notes |
|---|---|---|
| **Google Pixel & Nexus** | **Google USB Driver** (`android_winusb.inf`) or native in-box `WinUSB.sys` | Fully WHQL-signed. Available from [developer.android.com/studio/run/win-usb](https://developer.android.com/studio/run/win-usb). Can be installed via Device Manager or `pnputil /add-driver android_winusb.inf /install`. |
| **Samsung Galaxy** | **Samsung Mobile USB Composite Driver** (OEM installer) | Windows 11 attaches MTP by default, leaving the `SAMSUNG_Android` ADB interface unbound (yellow exclamation mark in Device Manager) without the official Samsung OEM driver. Generic `WinUSB` binding often breaks Samsung mode-switching. |
| **Other OEMs** | **Vendor-specific OEM USB Drivers** | Refer to the [official OEM USB driver directory](https://developer.android.com/studio/run/oem-usb) when native WinUSB descriptors (WCID / MS OS 2.0) are not provided by the device firmware. |

`adapt doctor` reads Windows PnP device status and fails (exit 2) when a device it confirms as Android reports a driver problem. On the main branch (unreleased), a problem reported only by USB devices it does not confirm as Android is a warning that lists them, so an unrelated device no longer fails the check; check Device Manager if your phone is among them. ADAPT never installs drivers.

Run ADAPT from a normal, non-elevated terminal. An `adb` server started from an elevated (Administrator) session cannot be shared with tools in non-elevated sessions; on the main branch ADAPT prints a warning when it runs elevated.

---

## 4. Critical Safety Rules

### Never Use Zadig or Force-Bind `WinUSB` to the Composite Parent
- **Why**: Tools like Zadig (or automated `libwdi` installers) frequently replace the driver for the entire USB composite parent (`USB\VID_xxxx&PID_xxxx`) instead of the specific ADB child interface (`...&MI_01`), breaking MTP file transfer, USB tethering, and OEM mode-switching.
- **Policy**: Only use official WHQL-signed INF packages (`android_winusb.inf`) or official OEM driver installers.

### Never Enable `testsigning` or Disable Driver Signature Enforcement
- **Why**: Third-party "Universal ADB Drivers" often fail on Windows 11 with Code 52 due to revoked or expired cross-certificates.
- **Policy**: Never run `bcdedit /set testsigning on` or disable Windows 11 kernel-mode driver signature enforcement. Always use properly WHQL-signed Google or OEM drivers.

### Never Run `adb kill-server` Unilaterally
- **Why**: Stopping the ADB server disrupts active port forwardings, reverse tunnels, logcat streams, and concurrent IDE or agent workflows on the same machine.
- **Policy**: Never run `adb kill-server` during normal operations or diagnostics (`adapt doctor`). Only restart the server if an explicit client/server version mismatch error (`adb server version (X) doesn't match this client (Y)`) is caught.

### Protect Existing SDKs and Host Keys
- **Existing SDKs**: Never overwrite `%ANDROID_HOME%` or `%ANDROID_SDK_ROOT%` without explicit user confirmation.
- **RSA Keypair**: Never delete or reset `%USERPROFILE%\.android\adbkey` and `adbkey.pub` without a backup; doing so revokes authorization across all previously paired physical devices.
