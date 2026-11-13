# Hypervisors and remote workers

ADAPT calls ADB on the Windows or Linux computer running ADAPT. Its runtime has
no Proxmox API dependency or hypervisor-specific device adapter. Optional SSH
worker mode runs ADAPT on an explicitly selected remote computer.
A supported guest OS with working ADB access can therefore use the same commands
whether its VM is hosted by Proxmox/KVM, VMware, Hyper-V, VirtualBox or XCP-ng.
This is an architectural compatibility assessment, not tested certification of
those guest/device combinations.

The important boundary is device access: the worker needs a usable USB device
connection or an authorized, reachable Android wireless-debugging endpoint.
USB forwarding, guest drivers, networking and firewall configuration belong to
the chosen virtualization platform. SSH connectivity alone does not provide USB,
an authorized ADB transport or a visible graphical session for scrcpy.

For a remote worker, install ADAPT inside a supported Windows/Linux guest, set up
its device access there, and run diagnostics there. An existing SSH client can
run commands on that worker without a hypervisor adapter, for example:

```text
adapt ssh run android-worker --operation doctor --json
```

Here `android-worker` is a user-configured SSH host alias; SSH authentication and
host-key verification remain with OpenSSH. See [SSH worker mode](ssh-workers.md)
for offline previews, remote inspection and MCP configuration. A locally launched MCP client can
likewise use a separately configured SSH stdio command if it supports that
launch model. ADAPT's own daemon checks continue to require a safe local ADB
server on the worker. See [MCP access](clients.md) for session restrictions.

Managing VM lifecycle or host USB assignments would require separate platform
integrations. For example, Proxmox exposes an HTTPS management API on port 8006;
that API is distinct from SSH. No management adapter is needed for the current
ADB feature set. [Proxmox's official pveproxy documentation](https://github.com/proxmox/pve-docs/blob/master/pveproxy.adoc)
describes that API, and the [OpenSSH manuals](https://www.openssh.org/manual.html)
describe remote command and transport configuration.

Android Emulator acceleration is another separate concern. Android documents
Windows Hypervisor Platform, Linux KVM and macOS Hypervisor.Framework, plus
restrictions on accelerated emulators inside other VMs. Do not infer accelerated
emulator support merely from a successful guest SSH session. On a chosen emulator
host, the SDK's `emulator -accel-check` reports local acceleration availability.
See [Android's official acceleration requirements](https://developer.android.com/studio/run/emulator-acceleration).
ADAPT currently neither runs that check nor provisions nested virtualization.

Checked 2026-11-12 against local source and the linked primary documentation.
No lab hosts, VMs, SSH servers or devices were contacted for this assessment.
