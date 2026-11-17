"""Offline usage guide and optional human-only contextual instructions."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any
from contextvars import ContextVar

import typer
from typer.core import TyperGroup
from adapt_adb.failure_guide import GUIDES, RENAMED
try:
    from typer import _click as click  # Typer versions with bundled Click.
except ImportError:
    import click

_TIP_CONTEXT: ContextVar[Any] = ContextVar("adapt_tip_context", default=None)


def set_tip_context(ctx: typer.Context) -> None:
    token = _TIP_CONTEXT.set(ctx.find_root())
    ctx.call_on_close(lambda: _TIP_CONTEXT.reset(token))


class GuidedGroup(TyperGroup):
    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        # Capture before parsing: nested --json must silence tips too.
        if ctx.parent is None:
            ctx.meta["adapt_machine_output"] = any(a == "--json" or a.startswith("--json=") for a in args)
        return super().parse_args(ctx, args)

    def resolve_command(self, ctx: click.Context, args: list[str]) -> Any:
        name, command, rest = super().resolve_command(ctx, args)
        if ctx.parent is None and name:
            path = name
            # Record "group subcommand" (static names only) for the unexpected-failure activity line.
            if rest and callable(getattr(command, "get_command", None)) and not rest[0].startswith("-"):
                try:
                    if command.get_command(ctx, rest[0]) is not None:
                        path = f"{name} {rest[0]}"
                except Exception:
                    pass
            ctx.meta["adapt_command_path"] = path
        return name, command, rest

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except _control_flow_exceptions():
            raise
        except Exception as exc:  # Truly unexpected: replace the traceback (design 2.7, D8).
            if os.environ.get("ADAPT_DEBUG") == "1":
                raise
            from adapt_adb.failure_report import report_unexpected
            report_unexpected(ctx, exc)
            raise typer.Exit(code=1) from None


def _control_flow_exceptions() -> tuple[type[BaseException], ...]:
    """Exit/abort/usage exceptions that Click and Typer handle themselves (bundled or not)."""
    found: list[type[BaseException]] = [SystemExit, KeyboardInterrupt, EOFError]
    for module in (getattr(click, "exceptions", None), sys.modules.get("typer.exceptions"), typer):
        for name in ("ClickException", "Exit", "Abort", "TyperException"):
            cls = getattr(module, name, None)
            if isinstance(cls, type) and issubclass(cls, BaseException):
                found.append(cls)
    return tuple(dict.fromkeys(found))


HOW_TO: dict[str, tuple[str, ...]] = {
    "console": ("Open ADAPT's main screen with its original fading logo and grouped tools.", "adapt console", "H returns Home, B goes Back, N/P changes pages. R runs a selected original command with its existing confirmations. Use --no-redraw for basic terminals."),
    "rapid": ("Rapid ADAPT is the focused testing option inside ADAPT: check, prepare, reproduce, capture and restore.", "adapt rapid plan -s SERIAL --package com.example.app", "The plan runs no commands. Stay-awake is USB-powered; fast-ui is opt-in. Restore profiles explicitly after testing."),
    "rapid plan": ("Preview exact commands and recovery paths for a focused device/app test.", "adapt rapid plan -s SERIAL --package com.example.app --session-dir NEW_SESSION --json", "Choose new snapshot paths. Heavy vendor logging, installs and debugger selection are deeper diagnostics, outside this rapid path."),
    "logs guide": ("Distinguish Samsung/vendor verbose logging, window traces, print diagnostics and app debugging.", "adapt logs guide --topic samsung", "This guide changes nothing. Firmware-specific switches remain manual; restore their original state after a short reproduction."),
    "logs diagnostic": ("Capture recent fixed-tag window, DeX or print system messages without changing verbosity.", "adapt logs diagnostic -s SERIAL --topic window --minutes 2 --output window.json", "System component logs can include multiple apps/users and secrets. A severity filter cannot enable producer logs; empty output is inconclusive."),
    "app": ("Check exact current-user installation, version and debugger eligibility; read build terminology.", "adapt app validate -s SERIAL --package com.example.app --expect-version-code 42 --expect-debuggable false --json\nadapt app build-modes", "Non-debuggable does not prove R8 optimization, a release signer or an exact installed APK."),
    "logs": ("Extract a finite main/crash log snapshot for the installed app UID without clearing buffers.", "adapt logs capture -s SERIAL --package com.example.app --lines 200 --output logs.json", "Exports never overwrite. Logs may contain secrets; shared/historical UID logs and omitted system reports limit attribution. Empty logs are not a test pass."),
    "debug": ("Inspect saved debug-app configuration or preview debugger setup before changing app execution.", "adapt debug inspect -s SERIAL --json\nadapt debug plan -s SERIAL --package com.example.app --wait", "Select debug app can force-stop it; Wait for debugger can pause startup. This command only previews selection and does not attach a debugger or make release builds debuggable."),
    "ssh": ("Select an SSH worker explicitly; ADAPT and ADB run on that worker, with its device access.", "adapt ssh status\nadapt ssh plan android-worker --operation doctor\nadapt ssh run android-worker --operation inspect --device SERIAL\nadapt ssh mcp-config android-worker --device SERIAL --json", "Uses existing verified SSH host keys and key/agent authentication. No automatic fallback, remote install, generic shell, mirroring or VM control. Windows workers need --remote-shell powershell."),
    "tailscale": ("Use a phone's tailnet ADB endpoint or select a remote USB worker explicitly.", "adapt tailscale guide\nadapt tailscale peers\nadapt tailscale plan --peer PHONE --port CONNECT_PORT --pairing-port PAIR_PORT", "Tailscale access and Android authorization are separate. Pairing and connection ports differ; an online peer or open TCP port does not prove ADB works."),
    "tailscale status": ("Read local Tailscale daemon state and a minimal peer list.", "adapt tailscale status --json", "This does not probe peers or prove an Android listener is active. Sign in with the official Tailscale client separately."),
    "tailscale peers": ("Choose an exact unique tailnet peer name, ID, MagicDNS name or node IP.", "adapt tailscale peers", "Execution uses the numeric address observed for that peer. Offline, expired and ambiguous peers are refused."),
    "tailscale guide": ("Read the direct-phone, remote USB-worker and subnet-router options offline.", "adapt tailscale guide", "The guide changes no VPN, route or phone settings. Tailscale carries IP traffic; it does not supply a Miracast receiver."),
    "tailscale plan": ("Preview the separate pairing, connection and verification commands offline.", "adapt tailscale plan --peer PHONE --port CONNECT_PORT --pairing-port PAIR_PORT --json", "Use current phone-displayed ports. This plan does not read daemon status or contact the phone."),
    "tailscale probe": ("Check one known TCP port on one selected tailnet peer.", "adapt tailscale probe --peer PHONE --port CONNECT_PORT", "tcp_open proves a TCP handshake only. It does not identify the service, pair the phone or authorize ADB."),
    "tailscale pair": ("Open Pair device with pairing code on the phone and keep that dialog open.", "adapt tailscale pair --peer PHONE --port PAIR_PORT", "Enter the code at the hidden prompt; automation can use --code-stdin. Never put codes in command arguments. Pairing alone does not establish a device connection."),
    "tailscale connect": ("Read the connection port on the main Wireless debugging screen after pairing.", "adapt tailscale connect --peer PHONE --port CONNECT_PORT --json", "Success requires an authorized exact transport, device identity reads and a matching Android TLS listener port. No alternative peer or port is tried."),
    "tailscale check": ("Verify an existing exact tailnet ADB transport without connecting a new one.", "adapt tailscale check --peer PHONE --port CONNECT_PORT --json", "This reads the model, physical/build/user identity, Android API and TLS port. It does not prove an app or mirror works."),
    "tailscale adb": ("Run an existing guarded ADB device command after exact endpoint verification.", "adapt tailscale adb --peer PHONE --port CONNECT_PORT -- shell getprop ro.product.model", "Target/server/listener overrides are refused. Use the returned exact serial with existing logs, app, profile and mirror workflows."),
    "tailscale disconnect": ("Remove only the explicitly selected tailnet ADB transport.", "adapt tailscale disconnect --peer PHONE --port CONNECT_PORT --yes", "This can stop sessions using that transport. It does not disconnect other devices or disable the phone's VPN or debugging listener."),
    "tailscale worker-plan": ("Preview an existing read-only SSH worker operation through a tailnet host.", "adapt tailscale worker-plan --peer LAB_HOST --operation inspect --device USB_SERIAL", "The worker owns USB/ADB access. Planning is offline; runtime requires verified SSH keys and a matching online Tailscale peer."),
    "tailscale worker-run": ("Run one allowlisted read-only ADAPT check on the selected tailnet worker.", "adapt tailscale worker-run --peer LAB_HOST --user USER --operation inspect --device USB_SERIAL", "The numeric tailnet address must have existing SSH host-key trust. Windows workers need --remote-shell powershell. There is no automatic local fallback."),
    "tailscale worker-mcp-config": ("Generate a read-only MCP-over-SSH client entry for one worker-owned device.", "adapt tailscale worker-mcp-config --peer LAB_HOST --device USB_SERIAL --json", "This offline entry uses the supplied host name/address and existing OpenSSH trust. It writes no client settings and makes no connection."),
    "detect": ("Observe ADB and fastboot separately; unknown root/ROM/lock evidence stays unknown.", "adapt detect scan --json\nadapt detect inspect -s SERIAL\nadapt detect inspect --transport fastboot -s SERIAL", "No reboot, root request, unlock or flashing. Samsung Download/Odin mode is not standard fastboot."),
    "desktop": ("Test an app in a separate display or request recoverable desktop/freeform developer settings.", "adapt desktop inspect -s SERIAL\nadapt desktop plan -s SERIAL --app com.example.app\nadapt desktop enable -s SERIAL --snapshot desktop.json --yes\nadapt desktop start -s SERIAL --app com.example.app --yes", "Enable settings may need a manual reboot. Restore with dev-options restore. A virtual display does not prove a desktop shell or Samsung DeX."),
    "everything": ("Detect every catalog tool and run the supported host installers/checks as a confirmed batch.", "adapt everything --dry-run\nadapt everything --yes --json", "Other tools receive manual acquisition guidance. Add --device SERIAL for read-only device checks; no settings, mirror, driver, or VM changes are automatic."),
    "doctor": ("Check host tools, the shared ADB server, and USB permissions before device work.", "adapt doctor --fix", "--fix shows remediation guidance; it is not a blanket automatic repair."),
    "setup": ("Install Google's Platform-Tools in user scope. Preview the changes first.", "adapt setup --dry-run", "Normal interactive setup asks before installation; --yes is an explicit automation choice."),
    "devices": ("List ADB transports and their authorization state.", "adapt devices", "One phone can appear through USB and Wi-Fi. Choose the exact target for device commands."),
    "adb": ("Run one guarded ADB command against an explicit transport.", "adapt adb -- -s SERIAL shell getprop ro.build.version.release", "Complex shell programs are rejected; this interface is not an unrestricted remote shell."),
    "wireless": ("On Android 11+, open Developer options > Wireless debugging and pair this PC.", "adapt pair IP:PAIR_PORT\nadapt connect IP:CONNECT_PORT", "The pairing and connection ports differ and can change. Android 10 does not provide this TLS pairing flow."),
    "pair": ("Read the pairing endpoint from the phone's pairing-code dialog; the hidden prompt requests the code.", "adapt pair IP:PAIR_PORT", "Pairing authorizes a host. Connect afterward using the separate connection port."),
    "connect": ("Use the connection endpoint on the main Wireless debugging screen.", "adapt connect IP:CONNECT_PORT", "A remembered port may be stale. Refresh the endpoint if connection fails."),
    "device_manager": ("Inspect the Android USB interface and its driver in Windows Device Manager.", "adapt doctor", "This option opens Device Manager, then rechecks when you return. It does not install or repair a driver."),
    "udev": ("Linux USB access depends on device rules and your group permissions.", "adapt doctor --fix", "The selected configuration action uses sudo for system rules; ADB itself stays unprivileged."),
    "recheck": ("Rerun the host and transport checks after fixing the reported issue.", "adapt doctor", "Rechecking does not authorize an offline or untrusted device."),
    "tools": ("Find tools by goal, review their official sources, and plan acquisitions.", "adapt tools list --goal mirroring\nadapt tools info scrcpy", "A PATH hit shows location, not verified version or provenance."),
    "tools list": ("Browse tools for testing, mirroring, builds, or image inspection.", "adapt tools list --goal testing", "Use tools info TOOL for its purpose, license, and acquisition steps."),
    "tools info": ("Read a tool's purpose, official source, dependencies, and acquisition guidance.", "adapt tools info scrcpy", "This command provides information; it does not download or execute the tool."),
    "tools plan": ("Choose a goal and review the host-specific prerequisites.", "adapt tools plan lineage-build --device CODENAME --branch BRANCH", "Plans are explain-only. Supplied branch/device names do not prove current upstream support."),
    "tools install": ("Review the supported tool, pinned release, destination, and license before confirming installation.", "adapt tools install scrcpy --dry-run", "Installing a host tool does not itself authorize launching a device session."),
    "dev-options": ("Inspect Developer Options before applying a small, recoverable profile.", "adapt dev-options inspect -s SERIAL", "OEM behavior varies; a settings database value is not proof the option is honored."),
    "dev-options inspect": ("Read debugging, animation, touch, lifecycle and desktop settings for the chosen device.", "adapt dev-options inspect -s SERIAL --json", "Inspection changes no settings. Missing and denied results remain explicit."),
    "dev-options apply": ("Choose stay-awake for USB-powered tests, fast-ui for animations, touches for indicators, or desktop-testing.", "adapt dev-options apply --profile stay-awake -s SERIAL --snapshot stay-awake.json", "Stay-awake preserves existing charging modes and does not unlock the phone. A recovery snapshot precedes writes; restore explicitly after testing."),
    "dev-options restore": ("Restore a saved profile on the matching device, build, and Android user.", "adapt dev-options restore -s SERIAL --snapshot touches.json", "Conflicting external changes are not overwritten. The snapshot is retained."),
    "mcp": ("An AI client starts ADAPT and discovers named device tools through local stdio.", "codex mcp add adapt -- adapt mcp --read-only", "Use --device SERIAL to restrict exact targets. MCP is not an Android permission grant; client approvals and Android authorization remain separate."),
    "agent": ("Give an AI agent everything it needs to use ADAPT: what it is, first steps, the development loop, safety rules, MCP and every command.", "adapt agent primer\nadapt agent primer --json\nadapt agent clients", "The primer prints text or JSON only and touches no device; it is built from the installed commands and MCP tools, so it matches this version. The other commands register ADAPT with AI clients, install the Agent Skill and verify the MCP server."),
    "agent primer": ("Print the agent primer as Markdown, or as JSON with the full command and MCP tool catalog.", "adapt agent primer\nadapt agent primer --json", "Nothing but the primer is written to stdout. It describes what ADAPT offers; it does not prove any workflow works on a device."),
    "agent clients": ("See which AI clients are installed here, where each keeps its MCP settings, and whether adapt is registered.", "adapt agent clients\nadapt agent clients --json", "Reads config files and PATH only; it starts no client and contacts no device. A client marked print-only is one ADAPT never writes."),
    "agent config": ("Print the exact entry, or the client's own command, that registers ADAPT's MCP server.", "adapt agent config claude-code\nadapt agent config codex --scope project --device SERIAL", "Read-only unless you add --allow-actions. No serial is written unless you pass --device. User scope shows the absolute adapt path; project scope a bare adapt."),
    "agent install": ("Register ADAPT's MCP server with an AI client after showing the change.", "adapt agent install claude-code --dry-run\nadapt agent install codex cursor --yes", "ADAPT changes a client's settings only here: it shows a diff first, backs the file up to <file>.adapt-backup-<UTC>, records it, and replaces the file atomically. It refuses a file that does not parse, has comments, or already holds a different adapt entry (--replace overrides). It never starts or stops a client. Not interactive means --yes."),
    "agent uninstall": ("Remove only the adapt entry that adapt agent install added.", "adapt agent uninstall codex --dry-run\nadapt agent uninstall codex --yes", "Needs ADAPT's own record of the install. The file is backed up first; an entry added by the client's own command is removed with that client."),
    "agent restore": ("List or restore the backups ADAPT recorded before it changed a file.", "adapt agent restore --list\nadapt agent restore ID --yes", "The file as it is now is backed up first, so a restore can itself be undone. A file changed after ADAPT wrote it is refused unless --force."),
    "agent skill": ("Print or install the Agent Skill that teaches an agent to use ADAPT.", "adapt agent skill print\nadapt agent skill install --scope project --yes", "The skill is a folder of Markdown copied to .agents/skills/adapt and .claude/skills/adapt, or the user-scope equivalents."),
    "agent skill print": ("Print the packaged SKILL.md.", "adapt agent skill print", "Prints only; nothing is written."),
    "agent skill install": ("Copy the Agent Skill to the folders agents read, after showing the change.", "adapt agent skill install --scope project --dry-run\nadapt agent skill install --scope user --target agents,claude --yes", "A different existing file is refused unless --replace, and backed up first. Targets: agents, claude, kiro, github."),
    "agent agents-md": ("Print, or write, the short ADAPT block for a project's AGENTS.md.", "adapt agent agents-md print\nadapt agent agents-md --write --yes", "The block sits between <!-- adapt:begin --> and <!-- adapt:end -->; running it again replaces only that block. The file is backed up first."),
    "agent verify": ("Start adapt mcp as a child process and check that it answers the MCP handshake and lists its tools.", "adapt agent verify\nadapt agent verify claude-code", "No device is contacted and no AI client is involved, so a pass does not show that a client accepts the registration. It runs only a registered command that is adapt mcp."),
    "guide": ("Read the official Android tooling notes for a selected topic.", "adapt guide testing", "Use help-me --all for every currently registered command."),
    "explain": ("Look up why a failure happened and what to try next, by failure ID, keyword or pasted error text.", "adapt explain device.unauthorized\nadapt explain --list\nadapt explain \"failed to authenticate\"", "Explaining runs nothing and contacts no device. Steps that depend on your setup are labelled; adapt doctor checks this PC read-only."),
    "android": ("Inspect version-dependent capabilities before choosing a device workflow.", "adapt android capabilities -s SERIAL --json", "Version eligibility and tested support are different; inspect runtime evidence too."),
    "dex": ("Inspect the Samsung device and available displays before selecting a desktop mirror.", "adapt dex --help", "A generic virtual display is not proof that Samsung DeX is running."),
    "dex pc-plan": ("Preview experimental phone-side DeX activation without touching a device.", "adapt dex pc-plan -s SERIAL --method loopback --json", "A vendor API may exist but deny shell permission. A simulated display may remain generic; neither is a verified desktop."),
    "dex pc-start": ("Try one explicitly approved activation method with identity-bound recovery and owned process supervision.", "adapt dex pc-start -s SERIAL --method loopback --snapshot SESSION.json --yes", "Existing receivers/displays are not replaced. Close the owned window or use pc-stop; restore is best-effort and conflicts remain visible."),
    "dex pc-stop": ("Request teardown of this snapshot's owned session, without killing processes by saved PID.", "adapt dex pc-stop --snapshot SESSION.json", "An unacknowledged stop stays pending. Keep the snapshot if recovery needs attention."),
    "dex pc-probe": ("Use the bundled temporary helper to observe real Samsung desktop state and display IDs.", "adapt dex pc-probe -s SERIAL --yes --json", "This transfers/runs a local-built JAR and removes it; no APK install, root or display activation."),
    "dex pc-attach": ("Mirror the observed sole external display of a genuinely active Samsung dual DeX session.", "adapt dex pc-attach -s SERIAL --display ID --yes", "The external receiver connection is user-owned and is not disconnected when this mirror closes."),
    "dex pc-receiver-plan": ("Use Microsoft's Wireless Display receiver when vendor shell activation is denied.", "adapt dex pc-receiver-plan --json", "The plan performs no installation. The Windows optional feature needs administrator approval and native phone/receiver pairing."),
    "dex receiver-plan": ("Compare the two PC receiver routes and read the native receiver's build steps, security posture and commands.", "adapt dex receiver-plan --json", "The plan runs nothing and checks nothing on this PC. The native receiver is experimental source you build yourself; it is not in the Python wheel."),
    "dex receiver-status": ("Read what the native receiver recorded in its own status file: not running, starting, ready, connecting, streaming or not working.", "adapt dex receiver-status --json\nadapt dex receiver-status --data-dir DIRECTORY", "Read-only. Advertising is not a working desktop, and a decoded frame does not certify input, audio or Samsung DeX. A status file whose process is gone is reported as stale."),
    "dex receiver-start": ("Start a receiver you built and wait until it is advertising; choose this PC on the phone afterwards.", "adapt dex receiver-start --exe PATH_TO_DEXDESK_EXE --yes\nadapt dex receiver-start --exe PATH_TO_DEXDESK_EXE --expect-name PHONE_NAME --yes", "This advertises the PC over Wi-Fi Direct and opens TCP 7236 on every IPv4 interface. Admission is open unless you restrict it, and the restrictions are not authentication. ADAPT does not build, find or download the program."),
    "dex receiver-stop": ("Ask the native receiver to close through its stop request and wait a bounded time.", "adapt dex receiver-stop --yes", "ADAPT never ends a process by ID. A stop that is not confirmed stays pending; close the receiver window yourself if it is stuck."),
    "dex inspect": ("Check Samsung identity, desktop-mode evidence, and existing logical displays.", "adapt dex inspect -s SERIAL --json", "Unknown service/output stays unknown; no DeX session is started."),
    "dex list-displays": ("Discover current logical display IDs; do not assume the desktop is display 2.", "adapt dex list-displays -s SERIAL", "Standalone DeX on display 0 is not a second phone-plus-desktop surface."),
    "dex plan-dual-mirror": ("Select the existing nonzero desktop display you observed in inspection.", "adapt dex plan-dual-mirror -s SERIAL --dex-display ID", "The plan launches nothing and labels unconfirmed external displays clearly."),
    "dex mirror": ("Launch separate phone and positively confirmed active DeX display windows.", "adapt dex mirror -s SERIAL --dex-display ID --yes", "Control is enabled by default; --view-only disables it. Video/input health still requires observation."),
}


def preferences_path() -> Path:
    if os.environ.get("ADAPT_CONFIG_DIR"):
        return Path(os.environ["ADAPT_CONFIG_DIR"]).expanduser() / "preferences.json"
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return root / "adapt" / "preferences.json"


def read_preferences() -> dict[str, Any]:
    path = preferences_path()
    if path.is_symlink():
        raise ValueError("ADAPT preferences must not be a symlink.")
    if not path.exists():
        return {}
    if path.is_symlink() or path.stat().st_size > 65536:
        raise ValueError("ADAPT preferences must be a small regular JSON file.")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or ("tips" in data and not isinstance(data["tips"], bool)):
        raise ValueError("Invalid ADAPT preferences; --no-tips still disables instructional tips.")
    return data


def save_tips(enabled: bool) -> None:
    data = read_preferences()
    data["tips"] = enabled
    path = preferences_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".preferences-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def tips_enabled(ctx: click.Context | None = None) -> bool:
    if ctx is not None:
        root = ctx.find_root()
        if root.params.get("no_tips") or root.meta.get("adapt_machine_output"):
            return False
    if os.environ.get("ADAPT_TIPS", "").strip().lower() in {"0", "off", "false", "no"}:
        return False
    try:
        return read_preferences().get("tips", True)
    except (OSError, ValueError):
        return False  # A broken preference must not break another command.


def emit_context_tip(topic: str, ctx: click.Context | None = None) -> None:
    ctx = ctx or _TIP_CONTEXT.get()
    # `agent` prints a guide meant to be read as plain text; its help lives in `help-me agent`.
    if topic in {"help-me", "tips", "mcp", "done", "explain"} or topic.split(" ")[0] == "agent" or not tips_enabled(ctx):
        return
    if not all(getattr(stream, "isatty", lambda: False)() for stream in (sys.stdin, sys.stdout, sys.stderr)):
        return
    steps = HOW_TO.get(topic) or HOW_TO.get(topic.split(" ")[0])
    if steps:
        sys.stderr.write("\nHOW TO / " + topic + "\n" + "\n".join("  " + part.replace("\n", "\n  ") for part in steps) + "\n  Hide tips: adapt tips off (or --no-tips for this invocation).\n\n")


def group_tip(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand:
        emit_context_tip(f"{ctx.info_name} {ctx.invoked_subcommand}", ctx)


def command_inventory(root: click.Command) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    def walk(command: click.Command, path: str, parent: click.Context | None) -> None:
        ctx = click.Context(command, info_name=path.split(" ")[-1], parent=parent)
        records.append({"command": path, "description": command.help or "", "usage": command.get_usage(ctx),
                        "parameters": [p.get_help_record(ctx) for p in command.get_params(ctx) if p.get_help_record(ctx)],
                        "how_to": list(HOW_TO.get(path.removeprefix("adapt "), ()))})
        if callable(getattr(command, "list_commands", None)):
            for name in command.list_commands(ctx):
                child = command.get_command(ctx, name)
                if child is not None and not child.hidden:
                    walk(child, f"{path} {name}", ctx)
    walk(root, "adapt", None)
    return records


def linked_failures(*topics: str) -> list[dict[str, str]]:
    """Failure guides whose help topic or raising command matches; help points to failures."""
    wanted = {t for t in topics if t}
    ids = [g.id for g in GUIDES.values() if g.help_topic in wanted or wanted & set(g.commands)]
    if not ids:
        return []
    from adapt_adb.failure_report import list_entries
    return list_entries(ids)


def guide_payload(root: click.Command, topic: str | None, all_commands: bool) -> dict[str, Any]:
    inventory = command_inventory(root)
    if all_commands:
        return {"commands": inventory, "tips_controls": ["adapt tips off", "adapt tips on", "adapt --no-tips COMMAND", "ADAPT_TIPS=0"]}
    available = {r["command"].removeprefix("adapt ") for r in inventory}
    if topic:
        key = topic.strip().lower().removeprefix("adapt ")
        alias = {"wireless": "pair", "device_manager": "doctor", "udev": "doctor", "recheck": "doctor"}.get(key, key)
        if alias not in available:
            failure_id = RENAMED.get(key, key)
            if failure_id in GUIDES:  # Failure IDs are answered here too (same as adapt explain ID).
                from adapt_adb.failure_report import failure_payload, resolve
                return {"topic": key, "failure": failure_payload(resolve(failure_id), full=True)}
            raise ValueError("Unknown help topic. Run 'adapt help-me --all' for available commands.")
        return {"topic": key, "how_to": list(HOW_TO.get(key) or HOW_TO.get(key.split(" ")[0]) or (f"Use adapt {key} --help for available arguments.",)),
                "commands": [r for r in inventory if r["command"] == f"adapt {alias}" or r["command"].startswith(f"adapt {alias} ")],
                "failures": linked_failures(key, alias, *(("connect",) if key == "wireless" else ()))}
    return {"start_here": ["adapt doctor", "adapt devices", "adapt help-me wireless", "adapt tools list", "adapt help-me --all"],
            "description": "Choose a topic for step-by-step help, or --all for every registered command and option.",
            "topics": sorted((available - {"adapt"}) | ({"wireless"} if "pair" in available else set())), "tips_controls": ["adapt tips off", "adapt tips on", "adapt --no-tips COMMAND"]}


def render_guide(payload: dict[str, Any]) -> str:
    if "failure" in payload:
        from adapt_adb.failure_report import render_explain, resolve
        return render_explain(resolve(payload["failure"]["id"]))
    lines = ["ADAPT / HELP ME", ""]
    if "description" in payload:
        lines += [payload["description"], "", *payload["start_here"], "", "Topics:", *("  " + t for t in payload["topics"])]
    if "how_to" in payload:
        lines += [*payload["how_to"], ""]
    for command in payload.get("commands", []):
        lines += [command["command"], "  " + command["description"], "  " + command["usage"]]
        lines += [f"  {flag}  {description}" for flag, description in command["parameters"]]
        lines += ["  " + step.replace("\n", "\n  ") for step in command.get("how_to", [])]
        lines.append("")
    if payload.get("failures"):
        lines += ["Common failures:", *(f"  {f['id']}  {f['summary']}" for f in payload["failures"]),
                  "  Details: adapt explain ID", ""]
    if "tips_controls" in payload:
        lines += ["Tips:", *("  " + t for t in payload["tips_controls"])]
    return "\n".join(lines)
