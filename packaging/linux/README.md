# ADAPT Linux / Debian Packaging & Setup

Host setup instructions and packaging artifacts for running **ADAPT** (`adapt`) on Debian and Linux systems.

---

## 1. Prefer Official Google `platform-tools` Over Debian `apt`

Do not rely on Debian's standard `apt install adb` package for modern Android workflows:

- **Debian Stable Lag**: Debian stable (e.g., Bookworm) ships `adb` `1:29.0.6` (Android 10 era), which is 5+ years behind current releases (`35.x+`).
- **Missing Features**: Outdated APT builds lack TLS wireless pairing (`adb pair <host>:<port> <code>`), built-in Openscreen mDNS discovery, and modern USB descriptor optimizations.

### Recommended Installation (`platform-tools-latest-linux.zip`)

ADAPT (via `adapt setup`) or manual bootstrapping should install Google's standalone distribution into a user-scope directory (`~/.local/share/android/platform-tools`):

1. Download the official archive from Google:
   `https://dl.google.com/android/repository/platform-tools-latest-linux.zip`
2. Verify the SHA-256 checksum against the [official Platform-Tools release notes](https://developer.android.com/tools/releases/platform-tools).
3. Extract into `~/.local/share/android/platform-tools`.

---

## 2. User-Scope `PATH` in `~/.profile`

Never modify system-wide environment files (`/etc/environment` or `/etc/profile`). Configure `PATH` in your user profile (`~/.profile`, or `~/.bashrc` / `~/.zshrc` for interactive shells) so that the user-scope Google `platform-tools` binary takes precedence over any system binary:

```sh
# ~/.profile
if [ -d "$HOME/.local/share/android/platform-tools" ] ; then
    PATH="$HOME/.local/share/android/platform-tools:$PATH"
fi
export PATH
```

Reload your shell configuration (`source ~/.profile`) and confirm with `command -v adb` and `adb version` (should report `>= 34.0.0`).

---

## 3. Udev Rules & Device Permissions (`51-android.rules`)

Linux creates raw USB device nodes under `/dev/bus/usb/` owned by `root:root` (`0660`). To access Android devices without root privileges, install [`51-android.rules`](./51-android.rules):

```sh
sudo tee /etc/udev/rules.d/51-android.rules < packaging/linux/51-android.rules > /dev/null
sudo udevadm control --reload-rules && sudo udevadm trigger
```

- **Desktop Sessions (`TAG+="uaccess"`)**: `systemd-logind` automatically grants POSIX ACL read/write permissions to the active local seat user.
- **SSH / Headless Sessions (`GROUP="plugdev"`, `MODE="0660"`)**: When no local graphical/console seat is active, membership in `plugdev` is required:
  ```sh
  sudo usermod -aG plugdev "$USER"
  ```
  Log out and back in (or run `newgrp plugdev`), then replug the USB cable.

*(Alternatively, Debian's `android-sdk-platform-tools-common` package can provide baseline udev rules via APT without installing the outdated `adb` binary.)*

---

## 4. Critical Safety Rules

### Never Run `sudo adb`
- **Why**: Running `sudo adb` starts the background ADB daemon as `root`, creating a root-owned socket (`/tmp/5037`) and writing keys under `/root/.android` or polluting ownership of `~/.android/adbkey`. Subsequent unprivileged `adb` commands and ADAPT checks will fail with permission and protocol errors.
- **Recovery**: If the server was accidentally started as root, stop it with `sudo pkill -9 adb`, remove `/tmp/5037` if stale, and run `adb devices` as your normal unprivileged user.

### Never Run `adb kill-server` Unilaterally
- **Why**: Killing the ADB server tears down active port forwardings (`adb forward`), reverse tunnels (`adb reverse`), logcat streams, and ongoing test or agent sessions sharing the host.
- **Policy**: Never invoke `adb kill-server` during routine setup or diagnostics. Only restart the server if an explicit client/server version mismatch error (`adb server version (X) doesn't match this client (Y)`) is encountered.

### Protect Host Keys (`~/.android/adbkey`)
- Never delete or overwrite `~/.android/adbkey` and `~/.android/adbkey.pub` without an explicit backup, as doing so revokes USB and wireless debugging authorization across all previously paired physical devices.
