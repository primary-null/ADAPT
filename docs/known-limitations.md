# Known limitations

ADAPT is an **alpha** source release. This page lists what is unproven, experimental, deliberately out of scope or easy to misread. When another document sounds more confident than this one, trust this one and the `claim_boundary`/`note` fields in command output.

## How to read the evidence

- **Automated tests are offline.** They mock `adb`, `ssh`, `tailscale`, archives and fixtures. The prepublication run is recorded in [`publication-checks.json`](publication-checks.json) and [validation](validation.md): one Windows host, 32 Python test modules. Test counts are not a pass rate, are never added to physical observations, and do not certify distributions, other operating systems or any device.
- **Physical observations cover exactly two Samsung phones:** Android 13 (API 33) and Android 17 (API 37). That is two data points, not a compatibility list. The offline matrix spans Android 10-17 (API 29-37, including 12L, 9 baselines); every other API level, every other OEM, tablets, wearables and TV are untested.
- **Research topologies, generated illustrations and the showcase are not test evidence.** The showcase contains no Android screenshots; its generated panels are source-derived illustrations.
- **No build certification.** The package is installed from a checkout. No PyPI package, signed release, wheel or sdist is assumed or certified, and no installer acceptance on a fresh machine is claimed.

## Platform and install

| Limitation | Detail |
|---|---|
| Install from checkout | Python 3.11+ and `pip install -e .` (add `.[mcp]` for the MCP server). Name `adapt-adb`; nothing is assumed to exist on PyPI |
| Host operating systems | Windows 11 and Debian/Linux are the targets. **Native Debian runtime acceptance is outstanding**, as is fresh provisioning across architectures |
| Platform-Tools | Official Google archive, `adb >= 34.0.0`. Debian's apt `adb` is treated as outdated. `setup` fetches the Windows or Linux archive; `everything` limits batch setup to x86-64 hosts |
| `setup` prompt | Asks for confirmation **only in an interactive terminal**; non-interactive runs proceed. Preview with `--dry-run` |
| udev rules | `doctor`'s udev action reads `packaging/linux/51-android.rules` from a **checkout**; the wheel does not ship it |
| Drivers | ADAPT never installs USB drivers or OS packages; Windows Device Manager and Linux udev changes are manual |
| `doctor --fix` | Prints advice. The interactive menu's `udev` and `setup` choices run privileged or installing steps only when selected |

## Transports and Tailscale

- **Pairing is not a connection.** Android 11+ needs separate, changing pairing and connection ports; Android 10 needs the legacy USB-first `adb tcpip` flow, which ADAPT never enables for you.
- **A direct Tailscale end-to-end run is unproven.** `adapt tailscale connect|check --mode tls` requires `service.adb.tls.port` to equal the connect port and API >= 30. On the tested Samsung firmware that property was **empty**, so the strict verifier cannot pass there: an open verifier gap, not a confirmed Tailscale failure. `--mode legacy` is a deliberate, separate choice for an already-enabled TCP listener.
- A **private routed VPN/hotspot path did return authenticated ADB commands** and a retained connection recheck in pre-release testing. That is a routed path to one phone, not Tailscale validation.
- A peer being `online`, a TCP port being open (`probe`) or a pairing success proves none of "ADB is authorized and ready". Ports change after reboot or toggling Wireless debugging.
- Android normally allows one active VPN; ADAPT never changes VPN, ACL, subnet-route or exit-node settings. Relay paths can add latency that hurts mirroring. MagicDNS naming and ADB's mDNS discovery are separate; mDNS is not assumed to cross the overlay.
- Direct-peer mode accepts only tailnet node IPs. A **subnet router** reaches a LAN; an **exit node** carries internet-bound traffic only. Neither is selected or configured by ADAPT.
- `ssh` and `tailscale worker-*`: no live SSH acceptance was run. SSH connectivity alone gives no USB, authorized ADB transport or graphical session. There are no hypervisor adapters and no VM or USB-assignment management.

## Detection, tools and builds

- **Root and ROM indicators are not proof.** Missing `su` does not prove a device is unrooted; `test-keys`, root or an unlocked bootloader do not prove a custom ROM; absence of a ROM property does not prove stock. Properties are self-reported, not attested. Bootloader unlock *capability* is separate from lock *state*.
- **Samsung Download/Odin mode is not fastboot.** `detect` does not recognize it.
- **No automatic unlock, flash, root escalation or ROM build.** `tools plan` for `aosp-build`/`lineage-build` explains prerequisites and host suitability only. The tool catalog is manual ROM-tool gathering guidance: **only 2 of 16 catalog tools have automated acquisition** (`platform-tools` via `setup`, `scrcpy` via `tools install scrcpy`). A PATH hit is a location, not provenance or compatibility.
- `everything` also runs a read-only fastboot listing, is not atomic, and in JSON or non-interactive use requires `--yes`.

## Developer profiles, debug and Rapid ADAPT

- **Stay-awake needs USB power.** It ORs only the USB bit into `stay_on_while_plugged_in`, preserving other bits, refuses unknown values, and does not unlock the phone or override device policy. A readback is not proof the screen stays on.
- **Restore is best-effort.** It restores only settings that still hold the profile's value; conflicts abort and are reported, never overwritten. `desktop-testing` may need a manual reboot and some OEMs ignore it. Snapshots can describe your device; protect them.
- `dev-options apply|restore` have no `--json` flag.
- **Debug inspect/plan never attach a debugger.** Persisted settings may differ from a transient runtime selection. `am set-debug-app` (planned, never run by ADAPT) force-stops the target app. A non-debuggable app does **not** prove R8 or a release signer; `app validate` checks only what you request.
- **Rapid ADAPT is integrated planning, not an auto-runner.** It prints commands, executes none, creates no files, and does not supervise or roll back a session. Applied profiles must be restored by you.
- `logs` and MCP log tools: logs and screenshots can hold secrets or personal data and are not redacted. An empty log is not a pass or proof of no crash. Samsung verbose-logging switches are manual vendor controls ADAPT cannot toggle.

## Samsung DeX, displays and the PC bridge

- `dex` experimental activation is limited to **observed Samsung API 33-37** and refuses to replace an existing wireless, DeX or non-internal display.
- **Loopback activation was denied by a firmware permission on the tested API 33 phone, and a simulated overlay display did not start genuine DeX.** Capability presence is not permission, and a secondary or virtual display is not DeX.
- Display IDs are runtime values and change across connections; never assume the desktop is display 2. `pc-attach` accepts only layouts it recognizes as an active DeX session.
- `dex mirror` needs scrcpy 2.0+; `desktop start` needs 3.1+. Process start is reported, not video or input health.
- **MCP input tools (`tap`, `swipe`, `key`, `type`) target the default display** and have no display targeting; they cannot drive a DeX or virtual display.
- The experimental helper runs as the Android shell user via `app_process`; it is not an APK, root or a Samsung binary, and cleanup after a crash is best-effort.
- A virtual display (`desktop`) tests multi-window behavior only; decorations, focus and input vary by OEM.

## Native Windows receiver

Source lives in `native/windows-receiver`, derived from upstream MIT **DeXDesk** at commit `5070ccda5c363f5ed0b46d1fe7cf2c15c47fc125`, with its license and dependency notices preserved. See its [README](../native/windows-receiver/README.md).

- **Manual build, experimental, not in the Python wheel,** and not launched by any ADAPT command.
- **Friendly-name binding is not cryptographic.** The receiver admits a peer whose Wi-Fi Direct name equals a configured name and, optionally, whose Windows peer ID equals a configured ID. Names can be spoofed.
- **It relies on an undocumented Windows advertisement provider** (functions resolved at runtime from `wlanapi.dll`). Windows updates may remove or change it; some Windows versions or radios will not work.
- **The 15 offline native tests passed from the public export;** they do not start the receiver, exercise Wi-Fi Direct or involve a phone. This result is recorded separately from physical observations.
- On one physical Android 13 phone it formed a group, decoded desktop frames and passed one controlled reconnect. That is **no universal compatibility**: other Android/One UI builds, radios, Windows versions, long-duration sessions and Debian are unaccepted. Audio playback is not implemented. Port 7236 must be free; only one instance runs.

## MCP

- **No external AI client has been live-accepted** and MCP-over-SSH has not been exercised. Configuration snippets are the documented stdio shape, not verified registrations.
- stdio only: no HTTP/SSE, no authentication layer, no remote exposure. Clients limited to remote HTTPS connectors cannot use it.
- `--read-only` removes the five mutating tools but not observation of screens and logs; `--device` removes `doctor`. The shipped example configs omit both flags.
- `ui_summary` may create then remove a temporary file on device storage. `install_apk` reads a host path as your user. Tools and prompts only: no MCP resources or prompts.

## `adapt adb`

A guarded runner, not a shell. Interactive `shell`, streaming `logcat`, `kill-server`, `sudo`, `uninstall` and `pm clear` are refused with no override, and shell programs ADAPT cannot inspect (pipes, redirection, chaining, substitutions, shell indirection) are rejected. Use plain `adb` for deliberate destructive work.

## Out of scope

iOS; emulator provisioning or acceleration checks; hypervisor and VM lifecycle management; ROM building, unlocking and flashing; automatic driver installation; HTTP MCP transport; any publishing or repository write performed by ADAPT itself; and certification of any device, OEM or Android version.

## Privacy reminder

Command output, snapshots, logs, screenshots and Tailscale peer lists can contain device identifiers, network names, addresses and personal data. Review them before sharing, and use placeholders (such as `SERIAL`, `PHONE_PEER` and RFC 5737 addresses like `192.0.2.10`) in public reports.

See also: [features](features.md), [architecture](architecture.md), [MCP access](mcp-access.md), [command reference](command-reference.md).
