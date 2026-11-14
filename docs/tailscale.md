# Tailscale mode

Tailscale mode is part of ADAPT. Open **Devices, wireless & Tailscale** on the
main screen, choose `tailscale`, then choose an action. `H` returns Home.
`adapt help-me tailscale` and each command's `--help` explain the same workflow.
Contextual tips follow `adapt tips off` / `adapt tips on` and `--no-tips`.

## Direct Android ADB across networks

Install and sign into the official Tailscale client on the PC and phone.
ADAPT uses an existing client; it does not install it, change VPN settings,
authenticate an account or edit tailnet policy. Both nodes must be permitted
to communicate on the phone's current ADB ports.

```text
adapt tailscale status --json
adapt tailscale peers
adapt tailscale guide
```

Choose an exact unique peer name, MagicDNS name, peer ID or node IP. Runtime
selection reads local daemon status and binds to that peer's observed numeric
tailnet address. A missing, ambiguous, offline or expired peer is refused.
There is no DNS fallback, port scan or selection of another phone. Status
returns minimal peer observations, not account records, keys or login URLs.
Its output still includes private peer names and addresses: review before sharing.

On the main branch (unreleased; not in 0.1.1), `status` also reports this PC's
own node, the client version and an `auth_state` with a hint when the daemon is
not running or not signed in (for example `needs_login` or `stopped`); a peer
query that names this PC's own node is refused; and the TLS check, when ADB
answers but `service.adb.tls.port` is empty, says so and leaves the connection
in place. These changes are offline-tested only.

Each refusal and non-working status also names a failure guide (for example
`auth_state` `needs_login` is `adapt explain tailscale.needs_login`, and the
empty TLS port is `tailscale.tls_port_empty`): on stderr after the report, or as
an additive `failure` object with `--json` (see [help](help.md#failure-guidance)).

On Android 11 or later, enable Developer options > Wireless debugging.
Android may still require an active trusted Wi-Fi connection to keep this
listener enabled, even when its TCP traffic uses Tailscale across networks.

1. Open **Pair device with pairing code**. Read its pairing port and keep the
   dialog open while pairing. Enter the code only at the hidden terminal prompt.
2. Return to the **main Wireless debugging screen**. Read its separate connection
   port. Pairing ports are temporary; connection ports can change too.
3. Connect and check the exact endpoint before starting device work.

Replace `PHONE`, `PAIR_PORT` and `CONNECT_PORT` with current values:

```text
adapt tailscale plan --peer PHONE --port CONNECT_PORT --pairing-port PAIR_PORT
adapt tailscale pair --peer PHONE --port PAIR_PORT
adapt tailscale connect --peer PHONE --port CONNECT_PORT --json
adapt tailscale check --peer PHONE --port CONNECT_PORT --json
adapt tailscale adb --peer PHONE --port CONNECT_PORT -- shell getprop ro.product.model
```

There is no pairing-code argument. For deliberate automation, `pair --code-stdin`
reads one six-digit code from stdin. ADAPT passes it to official ADB through
stdin, discards raw pairing output, and returns no code or pairing GUID.
The plan is offline and requires no secret. Do not place a code in a command
string, terminal history or saved plan.

## What a result proves

`status` / `peers` read cached local daemon observations. `online` is not a
service check. `probe --peer PHONE --port CONNECT_PORT` performs one bounded
TCP handshake to that port; `tcp_open` does not identify or authenticate ADB.

`pair` reports `paired` only when ADB confirms the exact pairing endpoint. This
authorizes the host key but does not prove a device transport is connected.

`connect` reports `verified` only after exact-target `get-state`, physical
serial/build/user identity, model, Android SDK and TLS listener-port reads.
Default `--mode tls` requires Android API 30+ and a listener matching the
requested port. `check` verifies an existing transport without connecting it.
These observations do not prove application correctness, a working mirror,
or identity attestation against a compromised phone.

`adb` rechecks that endpoint before running one existing guarded ADB device
command. It rejects target overrides, host commands, shared-server changes
and listener changes. Existing safety rules still apply. For app checks,
logs, developer profiles, MCP or scrcpy, use the exact `serial` returned by
verification in their existing explicit-target options. USB and Wi-Fi
transports remain distinct, even for one phone.

```text
adapt app validate -s TAIL_IP:CONNECT_PORT --package com.example.app --json
adapt logs capture -s TAIL_IP:CONNECT_PORT --package com.example.app --output logs.json
adapt mcp --read-only --device TAIL_IP:CONNECT_PORT
```

The existing MCP tools can use the authorized endpoint; no pairing or VPN
mutation tools are added to MCP. An AI client still starts a local stdio
server. Tailscale does not create a public MCP HTTP service.

`disconnect --peer PHONE --port CONNECT_PORT --yes` removes only that selected
ADB transport. It can stop sessions using it. It does not disable the phone's
listener or VPN, forget pairing keys, disconnect other devices or restart ADB.

## Android 10 and version differences

Android 10 lacks the Android 11 TLS pairing flow. Explicit `--mode legacy`
can verify an already enabled TCP ADB listener; ADAPT does not enable it or
fall back to it after TLS failure. A legacy listener can also be reachable on
the phone's other interfaces. Use the existing authorized USB workflow to
decide whether to enable it, or choose an SSH USB worker instead.

The Android 10-17 capability matrix describes version eligibility, not proof
on every Samsung/OEM build. This mode verifies observed service behavior.

## Remote USB worker

A Windows or Linux machine can own the phone over USB while this PC reaches
that machine through Tailscale and existing OpenSSH. The phone itself needs
no Tailscale client in this arrangement.

```text
adapt tailscale worker-plan --peer LAB_HOST --operation inspect --device USB_SERIAL
adapt tailscale worker-run --peer LAB_HOST --user USER --operation inspect --device USB_SERIAL --json
adapt tailscale worker-mcp-config --peer LAB_HOST --device USB_SERIAL --json
```

The worker needs ADAPT/ADB, USB authorization, an SSH server, key/agent login
and preverified host-key trust. `worker-run` resolves the selected tailnet peer
and connects to its numeric IP; that IP must be trusted in SSH's host-key
configuration. Offline plans and MCP configurations retain the supplied host
name/address, which must have its own correct trust entry. Configuration output
is not tailnet verification. Windows workers need `--remote-shell powershell`.

Supported checks are `doctor`, `scan`, `inspect`, `android`, `desktop`, `dex`
and `tools`; device checks require the exact worker-owned transport. There is
no generic remote shell, automatic fallback or raw ADB-server forwarding.
MCP configuration is read-only and target-restricted. It writes no client file.
See [SSH workers](ssh-workers.md) and [AI clients](clients.md).

## Troubleshooting and boundaries

- An open port followed by pairing failure can mean an expired dialog, wrong
  port, different service or a protocol failure. Re-read the two ports; do not
  equate TCP reachability with authorization or blindly reuse an old code.
- A successful pairing still needs the main connection port. If TLS verification
  fails, the transport is left intact for inspection; no fallback disconnect runs.
- MagicDNS names nodes. Android's ADB service discovery uses mDNS, which is a
  separate mechanism; this flow uses explicit ports instead of assuming multicast
  discovery crosses the overlay.
- A relay path can carry ADB but increase interactive mirror latency. Peer online
  state or Tailscale ping does not prove an Android service is listening.
- Android normally has one active VPN service. Resolve other VPN requirements
  explicitly instead of silently replacing them.
- Subnet routing is a separate approved topology. Direct-peer mode deliberately
  requires an observed tailnet node, not an arbitrary LAN service behind a router.
- An [exit node](https://tailscale.com/docs/features/exit-nodes) routes internet
  traffic; a [subnet router](https://tailscale.com/docs/features/subnet-routers)
  reaches advertised private networks. Neither turns the phone into a local
  Wi-Fi/broadcast neighbor of this PC. A phone's own tailnet address already
  provides the direct-node path used here; no exit-node selection is required.
- Do not expose ADB server port 5037 publicly, use `adb -a` or Funnel, disable
  firewalls, or edit ACLs/routes/exit nodes as an automatic remedy.
- Tailscale carries IP traffic. It does not supply Wi-Fi Direct radio negotiation
  or activate Samsung DeX/Miracast. A mirror still needs an actual display source.

## Sources and validation

Checked 2026-11-12 against [Tailscale's architecture](https://tailscale.com/how-it-works),
[MagicDNS](https://tailscale.com/docs/features/magicdns),
[Android installation](https://tailscale.com/docs/install/android) and
[Android's ADB guide](https://developer.android.com/tools/adb).
The [upstream Tailscale ADB issue](https://github.com/tailscale/tailscale/issues/1990)
includes firsthand successful separate pair/connect reports and older failures;
it does not establish universal Android compatibility.

Offline tests cover endpoint selection, TLS verification, secret stdin handling,
failure classification, command guards and clean machine output. Native Debian,
Android 10 legacy, an SSH USB worker and mirroring each need their own runtime
acceptance. A live TCP or pairing result alone is not an ADB validation pass.
