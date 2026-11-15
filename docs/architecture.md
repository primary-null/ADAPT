# Architecture

ADAPT is a single Python package (`adapt_adb`, command `adapt`) that wraps Google's official Platform-Tools (`adb`, optionally `fastboot`) and a few optional helpers (`scrcpy`, `ssh`, `tailscale`) behind typed, bounded workflows. Every device operation is a normal, inspectable subprocess run by the current OS user. There is no daemon, database or network listener of ADAPT's own.

## Design rules

1. **Explicit target.** Device commands take an exact transport identifier. Nothing picks "the" phone for you, except the single-ready-device convenience in `adapt adb` and in the read-only MCP tools of a session started without `--device`. In the development tree (unreleased) the MCP tools that act on the phone never pick one: a call names `device`, or the session was started with exactly one `--device`. In 0.1.1 those tools also use the only ready device.
2. **Observe, then plan, then act.** Inspection commands change nothing; `plan` commands are offline; mutating commands need `--yes` (or an interactive confirmation) and say what they touch. The raw `adapt adb` and `adapt tailscale adb` passthroughs are the exception: they apply the argv safety guard and the port-5037 preflight, then run the command you typed without a confirmation prompt or a recovery snapshot.
3. **Recoverable writes.** Anything that edits device settings first writes an identity-bound recovery snapshot, reads values back, and restores only what it still owns.
4. **Shared-daemon safety.** Port 5037 is shared with IDEs and other agents. ADAPT never runs `adb kill-server`, never uses `sudo adb`, and refuses to talk to a server that is root-owned, owned by someone else, on a non-default endpoint, or version-mismatched.
5. **Claims stay at the evidence.** Reports carry `status`, `claim_boundary` and `note` fields. A port that answers, a property that exists or a process that started is never reported as a working workflow.

## Source layout

```text
ADAPT/
|-- src/adapt_adb/                  Python package (wheel)
|   |-- cli.py                      Typer root: setup, doctor, devices, adb, pair, connect,
|   |                               everything, mcp, tips, help-me, explain, guide, console
|   |-- adb.py                      adb discovery, Platform-Tools download/verify/extract
|   |-- doctor.py                   host checks, 5037 preflight, setup workflow, PATH edits
|   |-- safety.py                   adb argv guard (kill-server, sudo, uninstall, shell, logcat)
|   |-- android_support.py          Android 10-17 matrix and capability evaluation
|   |-- dev_options.py              13 settings, 4 profiles, snapshots, locks, restore
|   |-- development.py              app validate/build-modes, app logs, debug inspect/plan
|   |-- diagnostics.py              fixed-tag system logs and vendor-logging guidance
|   |-- device_state.py             detect scan/inspect (adb + fastboot, no transitions)
|   |-- desktop.py                  generic virtual-display app testing (scrcpy --new-display)
|   |-- dex.py, dex_pc.py           Samsung DeX inspection, dual mirror, experimental pc-*
|   |-- dex_receiver.py             dex receiver-*: start/read/stop a user-built native receiver
|   |-- ssh_worker.py               strict OpenSSH worker plans, runs, MCP config
|   |-- tailscale_mode.py           tailnet peer selection, pair/connect/check, workers
|   |-- tool_catalog.py             16-tool catalog, 6 goals, host assessment, plans
|   |-- scrcpy_install.py           pinned, verified user-scope scrcpy installer
|   |-- everything.py               batch of the implemented setup/check steps
|   |-- rapid.py                    Rapid ADAPT plan generator
|   |-- help_system.py, hints.py,   64 help topics, tips, udev/driver hints, guide topics
|   |   insights.py
|   |-- failure_guide.py,           failure guide registry (data only; 298 guides, including the
|   |   failure_report.py           receiver's reason codes and receiver-* outcomes) and its
|   |                               runtime: classify, context, Why/Try/More, JSON/MCP `failure`
|   |-- banner.py, console.py,      stderr banner, home screen (9 screens), progress dots
|   |   terminal_ui.py, progress.py
|   |-- render/                     screenshot scaling, UI-hierarchy summary
|   |-- mcp/                        server.py, tools.py (24 tools), access.py (policy)
|   |-- data/51-android.rules       Linux udev rules shipped in the wheel (copy of packaging/linux)
|   `-- resources/dex_bridge/       AdaptDexBridge.java, locally built JAR, build.json
|-- tests/                          offline test modules (mocked adb/ssh/tailscale)
|-- scripts/                        build_dex_bridge.py, build_icon.py, build_public_overview.py,
|                                   build_receiver.ps1, build_showcase.py, check_distribution.py,
|                                   install_windows_wireless_display.ps1
|-- packaging/                      Linux udev rules + notes, Windows driver notes
|-- examples/                       MCP client config samples
|-- native/windows-receiver/        Rust wireless-DeX receiver SOURCE (not in the wheel)
`-- docs/                           this documentation
```

The Python wheel contains `src/adapt_adb`, the `dex_bridge` resources and, in the development tree, `data/51-android.rules`. `native/`, `scripts/`, `packaging/` and `examples/` live in the repository only. With 0.1.1, whose wheel lacks the rules file, `doctor`'s udev menu choice needs a **checkout**; the native receiver always has to be built from a checkout.

## 1. Local CLI, MCP and device

```text
   Human terminal                         AI client (MCP host)
        |                                        |
        | argv                                   | spawns child, JSON-RPC over stdio
        v                                        v
 +--------------+                      +-----------------------+
 |  adapt  CLI  |                      |      adapt mcp        |
 | 69 commands  |                      | 24 tools, policy:     |
 +------+-------+                      | --read-only, --device |
        |                              +-----------+-----------+
        |   typed workflow modules                 |
        +-------------------+----------------------+
                            v
          validate arguments / schema, exact target, bounds
                            v
          safety.validate_adb_args  ->  doctor.preflight_adb_server (port 5037)
                            v
        +-------------------------------------------------+
        |  adb  (official Platform-Tools >= 34.0.0)       |   one `-s TARGET` per call
        +----+---------------------+----------------+-----+
             | USB                 | Wi-Fi          | routed (see section 3)
             v                     v                v
        +----------------------------------------------------+
        |  Android 10-17 device(s), user-authorized transport |
        +----------------------------------------------------+

   stdout: results / JSON / MCP frames      stderr: banner, tips, progress, logs
```

Human-only decoration (banner, `HOW TO` tips, the `adapting...` indicator) goes to stderr, only on terminals, and is switched off by `--json`, `adapt mcp` and non-interactive use, so machine output stays clean.

## 2. Host provisioning

```text
 [adapt setup]                                 [adapt tools install scrcpy]
     |                                                  |
     v                                                  v
 5037 preflight -> healthy adb >= 34? --yes--> noop      pinned scrcpy v5.0.1 plan
     | no                                                |
     v                                                  v
 dl.google.com (HTTPS only, allowlisted host)      github.com release asset (bounded
     |  size cap, digest from --sha256 or Google      redirects, size/ratio/member caps)
     |  repository metadata; mismatch = fail           |  SHA-256 per archive
     v                                                  v
 staging dir -> safe zip extract -> adb version check   staged extract -> receipt
     |                                                  |
     v  atomic swap                                     v  new managed directory
 user-scope dir  (%LOCALAPPDATA%\Android\platform-tools  (no PATH change, never launched)
                  or ~/.local/share/android/platform-tools)
     |
     v  PATH: Windows HKCU user Path / Linux ~/.profile block   (skip with --no-path)
```

`adapt everything` composes exactly these two paths plus read-only checks; the other 14 catalog tools are guidance only. In the development tree, `adapt setup` refuses before the preflight on an unsupported host (macOS), and when its own install folder already holds a healthy `adb` that is not on `PATH` it registers that folder on `PATH` instead of downloading again.

## 3. Wireless and routed ADB

### Local wireless debugging (Android 11+)

```text
   PC                                             Phone (Developer options > Wireless debugging)
   adapt pair    IP:PAIR_PORT  + 6-digit code --> "Pair device with pairing code" dialog
   (authorizes this PC's adb key; NOT a connection)        port A (changes each time)

   adapt connect IP:CONNECT_PORT              --> main Wireless debugging screen
   (opens the transport)                                   port B (different from A)
```

`pair` and `connect` use two different ports that both change; Android 10 uses the older USB-first `adb tcpip` flow, which ADAPT never enables for you.

### Tailscale: three distinct paths

```text
 (a) DIRECT PHONE NODE - what `adapt tailscale` implements (Tailscale on PC and phone);
     offline-tested, end-to-end run on a real phone unproven

   PC: adapt + adb + tailscale                         Phone: Tailscale + Wireless debugging
   +-----------------------------+   WireGuard overlay    +------------------------------+
   | tailscale peers (local only)|<=====================>| node 100.x.y.z                |
   | select ONE unique peer      |  direct or relayed     |                              |
   | probe  -> TCP handshake only|----- PORT B ---------->| connect/check port           |
   | pair   -> adb pair          |----- PORT A ---------->| pairing dialog port          |
   | connect/check/adb  (tls)    |  needs service.adb.tls.port == PORT B and API >= 30   |
   +-----------------------------+                        +------------------------------+
   MagicDNS names the node; ADB's mDNS discovery is NOT assumed to cross the overlay.

 (b) SUBNET ROUTER - reaches a private LAN, not a tailnet node

   PC --tailnet--> [router node advertises 192.0.2.0/24] --LAN--> phone ADB listener
   Direct-peer mode refuses a LAN address. Ordinary `adapt pair` / `adapt connect` to the
   routed address are plain adb commands whose route ADAPT does not verify; or use a worker.

 (c) EXIT NODE - routes internet-bound traffic only

   PC --tailnet--> [exit node] --> internet          (not a path to the phone's ADB port;
                                                      ADAPT never selects an exit node)
```

A private routed VPN/hotspot path to a Samsung phone returned authenticated ADB commands in pre-release testing. **A direct Tailscale end-to-end run is unproven**: the strict TLS verifier compares `service.adb.tls.port` to the connect port, and that property was empty on the tested Samsung firmware, so `--mode tls` fails verification there (`--mode legacy` is an explicit, separate choice). See [known limitations](known-limitations.md).

## 4. SSH workers and Tailscale workers

```text
 Operator PC (ADAPT, ssh)                          Worker (Windows or Linux; VM, lab PC, ...)
 +------------------------+   ssh -T -o BatchMode=yes     +-------------------------------+
 | adapt ssh run HOST     |   -o StrictHostKeyChecking=yes| adapt --no-banner ... doctor/ |
 |   --operation OP       |-----------------------------> |   scan/inspect/android/       |
 |   [--device WORKER_ID] |   no forwarding, no agent,    |   desktop/dex/tools --json    |
 +-----------+------------+   no ControlMaster            +---------------+---------------+
             ^                                                             | local adb
             | one JSON object, <= 4 MiB, exit 0-3                         v
             +--------------------------------------------      USB / authorized wireless
                                                                          device(s)

 MCP over SSH: the MCP client itself runs
   ssh ... HOST "adapt --no-banner --no-tips --no-progress mcp --read-only --device ID"
 (printed by `adapt ssh mcp-config` / `adapt tailscale worker-mcp-config`; written nowhere)
```

SSH carries the command, not USB. The worker needs ADAPT, its own safe ADB server, device authorization and (for mirroring) a graphical session. ADAPT has no hypervisor adapter; Proxmox, VMware, Hyper-V, VirtualBox and XCP-ng guests are architecturally equivalent as long as they have working ADB access. For a tailnet worker, `worker-run` first resolves the selected online peer and connects to its numeric address.

## 5. Display paths: scrcpy, generic virtual display, native wireless DeX

```text
 A. GENERIC VIRTUAL DISPLAY (adapt desktop)  - any Android 10-17, scrcpy 3.1+
    Phone --adb--> scrcpy --new-display=WxH/DPI --start-app=PKG --> separate window
    Tests multi-window app behavior. Not Samsung DeX; decorations are OEM-dependent.

 B. EXISTING-DISPLAY DUAL MIRROR (adapt dex mirror)  - Samsung API 33-37, scrcpy 2.0+
    Phone --adb--> scrcpy --display-id=0 --------------> window 1 (phone)
          +--adb--> scrcpy --display-id=N --------------> window 2 (DeX display)
    N must be positively confirmed as the active DeX display by `dex inspect`.

 C. EXPERIMENTAL PHONE-SIDE ACTIVATION (adapt dex pc-*)  - Samsung API 33-37
    pc-start --> supervisor --adb push--> AdaptDexBridge JAR (app_process, shell user)
                                  |-- loopback: ask Samsung for a local wireless display
                                  `-- overlay : overlay_display_devices setting (restored)
                          positive "DeX enabled + dual display" evidence --> scrcpy window
    May be denied by firmware permission. Simulated overlay is not DeX.

 D. RECEIVER ROUTES - Windows PC receives the phone's wireless display
    D1. Microsoft Wireless Display (optional Windows feature, manual, admin approval)
    D2. Native Rust receiver  native/windows-receiver  (manual build, experimental)

        Phone (Samsung DeX wireless)                      Windows PC
        +------------------+  Wi-Fi Direct group + RTSP/RTP (H.264/MPEG-TS) +-------------+
        | DeX desktop      |==============================================>| receiver    |
        | UIBC input  <====|<=============================================| window      |
        +------------------+   TCP 7236 / UIBC (mouse, touch, USB keyboard)  +-------------+
                                                                            (D2 decodes with
    then, optionally:  adapt dex pc-probe / pc-attach --> scrcpy window       Media Foundation,
    on the observed desktop display (the receiver connection stays yours)     draws with D3D11)
```

| Route | Provides | Does not provide |
|---|---|---|
| A. `desktop` | A second, app-scoped display and control window | Samsung DeX, desktop shell proof |
| B. `dex mirror` | Phone and DeX windows via scrcpy | Activation of DeX; works only when DeX is already active |
| C. `dex pc-*` | Trial activation with owned cleanup | A guarantee: loopback may be denied; overlay is not DeX |
| D2. native receiver | PC-side wireless-display sink for DeX (manual build); in the development tree, start, status and stop through `adapt dex receiver-*` | Cryptographic phone identity, Wi-Fi Direct on every radio, a build, download or bundle by ADAPT |

The native receiver is **not** launched by `adapt dex pc-start` and is not in the wheel. In the development tree, `adapt dex receiver-start --exe PATH --yes` starts a copy you built, `receiver-status` reads the `status.json` it writes, and `receiver-stop --yes` asks it to stop through a `stop.request` file; none of these contacts the phone. See [native/windows-receiver](../native/windows-receiver/README.md) and [DeX for PC](dex-pc.md).

## State, files and recovery

| Artifact | Created by | Properties |
|---|---|---|
| Developer Options snapshot (JSON) | `dev-options apply`, `desktop enable` | Exclusive create, mode 0600, bound to serial + build fingerprint + Android user, retained after restore |
| DeX session snapshot and logs | `dex pc-start`, `pc-attach` | Private directory, mode 0600, token-bound stop request, never kills by saved PID |
| Per-device lock | Profile apply/restore, DeX sessions | Serializes writers on the same physical device |
| Log exports | `logs capture/diagnostic --output` | New file only, mode 0600, never overwritten, hash recorded |
| `preferences.json` | `adapt tips on/off` | Tips preference only |
| Managed scrcpy directory + receipt | `tools install scrcpy` | User scope; never replaced in place |

Recovery is best-effort and explicit: a conflict (a setting someone else changed) is reported and left alone, the snapshot is kept, and nothing is overwritten.

## Where each feature lives

See [features](features.md) for the 19 functional areas, [command reference](command-reference.md) for every flag, [MCP access](mcp-access.md) for the 24 tools, and [known limitations](known-limitations.md) for what is unproven or deliberately out of scope.
