# SSH worker mode

SSH is an explicit alternative execution location. A selected worker runs ADAPT
and uses its own local ADB/fastboot tools and device connections. The client PC
needs OpenSSH, not local ADB for this mode. Local ADB failures never silently
switch hosts or targets. Connecting to SSH alone does not supply Android USB,
root privileges, bootloader access, a desktop shell or a visible Windows mirror.

```text
adapt ssh status --json
adapt tools plan remote-worker
adapt ssh plan android-worker --operation doctor
adapt ssh run android-worker --operation doctor --json
adapt ssh run android-worker --operation scan --transport all --json
adapt ssh run android-worker --operation inspect --device SERIAL --json
adapt ssh run android-worker --operation inspect --transport fastboot --device SERIAL --json
adapt ssh run android-worker --operation desktop --device SERIAL --json
```

`status` checks the local client location only. `plan` validates and shows local
SSH argv plus remote ADAPT argv without execution, config parsing, key reads or
network access. Neither runs `ssh -G`: a real user's SSH config can contain
`Match exec` commands. `run` is the explicit network action, using existing
OpenSSH configuration and key/agent authentication in batch mode. No passwords
or private-key contents are stored by ADAPT. `ADAPT_SSH` selects a local client
binary; an invalid explicit override refuses execution rather than falling back.
Preview argv uses `ssh` as the portable client placeholder.

The read-only operation allowlist is `doctor`, `scan`, `inspect`, `android`,
`desktop`, `dex` and `tools`. `inspect`, `android`, `desktop` and `dex` require an
exact `--device`. Host/list operations reject a supplied device instead of
pretending to scope a host report. Fastboot transport is supported by scan and
inspect; all-transports scan requires `--transport all`. No arbitrary remote
command, setup/install, device mutation, mirror launch or VM-management action
is exposed. Read-only inspection can still start an absent worker-side ADB daemon
through normal ADB behavior; existing shared-daemon safeguards remain in force.

## Worker setup and shell selection

Use an existing SSH host alias or `[user@]hostname/IP`. Configure the SSH server,
account, verified host-key trust and client key/agent beforehand. Install this
ADAPT version on the worker and configure Android device access there. Normally
use the account that owns that worker's ADB daemon. No worker discovery, SSH
server installation, key generation, known-host changes or remote deployment is
performed by these commands. OpenSSH acquisition is manual through the OS:
[Windows OpenSSH setup](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_install_firstuse)
or Debian's `openssh-client` package. The worker separately needs its SSH server.

The default remote shell is POSIX. Supply an absolute ADAPT path if the worker's
noninteractive PATH does not include its launcher:

```text
adapt ssh plan linux-worker --remote-adapt /home/tester/.local/bin/adapt
adapt ssh run windows-worker --remote-shell powershell --remote-adapt "C:\Program Files\ADAPT\adapt.exe" --operation scan --transport all --json
adapt ssh run android-worker --port 2222 --identity "keys/worker key" --json
```

POSIX commands use shell quoting around each argv element. Windows mode sends
a UTF-16LE encoded, noninteractive PowerShell script to invoke ADAPT, preserving
spaced serials and executable paths. Embedded double quotes in Windows arguments
are rejected because legacy Windows PowerShell's native-argument conversion
cannot reliably preserve them. This release supports POSIX and Windows worker
command construction; live worker/shell acceptance is still outstanding.

Connection options require existing host-key trust, disable automatic host-key
updates, agent/X11/port forwarding and configured LocalCommand, and avoid reusing
a multiplexed connection. The operator's trusted SSH config may still provide
ProxyJump/ProxyCommand, key selection and other connection helpers. ADAPT does
not parse, inventory or sandbox that configuration. See the official
[SSH command semantics](https://man.openbsd.org/ssh.1) and
[configuration options](https://man.openbsd.org/ssh_config.5).

Each normal run has an overall timeout (default 180 seconds, configurable from
1–300 via `--timeout`). Only the invocation's local SSH client is timed out; no
existing SSH or ADB sessions are killed. A remote read-only process may briefly
remain after disconnection. Accepted stdout is one JSON object, at most 4 MiB
after capture. Login chatter, incompatible command output, SSH authentication
failure or non-JSON errors produce explicit failure, never a local fallback.
Raw SSH stderr is not copied into reports. Worker diagnostic exit codes 0–3 and
its JSON are retained. A successful SSH command is not device-feature proof.

## Failure reasons

In the development tree (unreleased; 0.1.1 does not have this), when the SSH client
itself fails (exit 255) the `ssh_failed` report keeps its existing keys and
adds `ssh_failure_reason` and `hint`. ADAPT chooses the reason by matching well-known OpenSSH client messages in the captured stderr, then
discards that text: only the fixed reason code and fixed sentences below are
reported, so host names, addresses, user names and key paths never appear in a
report. For a classified reason, `error` starts with a short specific statement
and still ends with the original generic sentence; for `unknown` it is exactly
the original sentence. The first matching row wins, so a changed host key is
never reported as an unknown one and a key-file problem is not reported as plain
authentication failure.

| `ssh_failure_reason` | OpenSSH message it matches | Check next |
| --- | --- | --- |
| `host_key_changed` | `REMOTE HOST IDENTIFICATION HAS CHANGED`, `Offending ... key`, `Host key for ... has changed` | The stored key no longer matches: verify with the worker's administrator before altering known_hosts. |
| `host_key_unknown` | `Host key verification failed`, `No ... host key is known` | Connect once with ssh yourself and verify the fingerprint out of band; ADAPT never accepts host keys. |
| `name_resolution_failed` | `Could not resolve hostname` | Host name or alias, SSH config, DNS, or use the IP address. |
| `connection_refused` | `Connection refused` | SSH server running on the selected port; firewall. |
| `connection_timed_out` | `timed out`, `Timeout, server ... not responding` | Worker reachable, address and port right, firewall or VPN. |
| `network_unreachable` | `No route to host`, `Network is unreachable` | This PC's network, VPN or tailnet and the route to the worker. |
| `connection_closed` | `Connection closed by`, `Connection reset by`, `kex_exchange_identification`, `closed by remote host`, `Broken pipe` | Worker SSH server logs and access limits; proxy or jump host. |
| `identity_file_problem` | `bad permissions`, `UNPROTECTED PRIVATE KEY FILE`, `no such identity`, `Load key "`, `Identity file ... not accessible` | The key file exists, is a valid private key and is readable only by you. |
| `authentication_failed` | `Permission denied (...)`, `Too many authentication failures`, `No more authentication methods` | Batch mode cannot prompt for a password or passphrase: load a key in the agent or give the identity option. |
| `unknown` | nothing above | Run the command from `adapt ssh plan` in a terminal to read ssh's own message. |

Matching is best effort. Messages that OpenSSH builds from the operating system's
error text (refused, timed out, no route, unreachable, reset, broken pipe) can be
translated on a non-English system and then fall back to `unknown`. Exit 255 is
also what a remote command can return, so a worker-side 255 is indistinguishable
from an ssh failure.

When the worker's reply is not one ADAPT JSON object, the `invalid_response`
report keeps its existing `error` and adds `response_reason` and `hint`, chosen
from the exit code, stderr and stdout, which are likewise not copied. Replies
that exceed the size limits keep their existing, already specific `error`.

| `response_reason` | Chosen when |
| --- | --- |
| `remote_shell_mismatch` | stderr shows a shell of the other family: PowerShell or cmd errors while `--remote-shell posix`, or a POSIX shell failing to find `powershell.exe` while `--remote-shell powershell`. |
| `remote_adapt_not_found` | Exit 127, or stderr says `command not found`, `not found`, `is not recognized as` or `No such file or directory` for `adapt`. |
| `empty_response` | No stdout. |
| `not_json` | Stdout is present but is not one JSON object. |
| `unexpected_exit_code` | Stdout is one JSON object but the exit code is outside 0-3. |

Each reason code has its own failure guide (for example `ssh_failure_reason`
`host_key_unknown` is `adapt explain ssh.host_key_unknown`). Human output prints
it on stderr after the report; with `--json` the report gains a `failure` object
(see [help](help.md#failure-guidance)). Existing keys are unchanged.

## MCP over SSH

```text
adapt ssh mcp-config android-worker --device SERIAL --json
adapt ssh mcp-config windows-worker --remote-shell powershell --device SERIAL --json
```

These commands generate a `client_config.mcpServers` entry only: they write no
configuration, connect to no host and start no MCP server. Adapt its syntax to
the chosen AI client, and rename `adapt-worker` when adding multiple workers.
The generated client command is OpenSSH `ssh`; replace it with an absolute
client path if needed. The worker needs ADAPT's optional MCP dependencies.

The client launches SSH directly with no pseudo-terminal. Its stdin/stdout carry
the remote `adapt mcp --read-only` protocol. Repeat `--device` to allow exact
remote transports; omit it only if whole-worker observations are intended.
Remote banner/tips/progress are suppressed. Read-only and target restrictions
are enforced by the worker's existing MCP server. No HTTP listener or raw ADB
port exposure is added. Each AI client still controls its own connection setup
and approval behavior. See [MCP access](clients.md).

## Where this fits

This works independently of Proxmox, VMware, Hyper-V or other VM managers: choose
the supported guest that actually owns the device access. It is not a hypervisor
management adapter. It is also not direct Android SSH; that requires a separately
installed Android SSH server and a different permission model. This release
does not treat an Android app's SSH account as ADB shell or silently install one.

`everything` detects OpenSSH through the catalog and reports manual acquisition
if missing. It never connects to a worker or replaces its explicit local targets
with remote targets. Checked 2026-11-12. Offline tests cover construction, failed
connections, response handling and MCP configuration. No live SSH worker, Android
device, VM, authentication exchange or graphical session was exercised.
