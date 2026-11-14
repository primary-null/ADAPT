"""Explicit tailnet ADB transport, with peer verification and no daemon/network changes."""
from __future__ import annotations

import getpass
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import typer

from adapt_adb import dev_options, ssh_worker
from adapt_adb.adb import is_host_only_adb_command
from adapt_adb.failure_report import FailureContext, attach_failure, classify, current_command, emit_failure, emit_pointer, tagged
from adapt_adb.progress import operation
from adapt_adb.safety import validate_adb_args

tailscale_app = typer.Typer(help="Tailscale mode: select tailnet peers, pair/connect ADB, run targeted commands, or choose an SSH worker.", no_args_is_help=True)
_V4 = ipaddress.ip_network("100.64.0.0/10")
_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
_HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
_AUTH_STATE_HINTS: dict[str, tuple[str, str | None]] = {
    "Running": ("running", None),
    "NeedsLogin": ("needs_login", "Sign in with the Tailscale app or tailscale login; ADAPT changed nothing."),
    "NeedsMachineAuth": ("needs_machine_auth", "Ask a tailnet admin to approve this device; ADAPT changed nothing."),
    "Stopped": ("stopped", "Start the Tailscale application or service; ADAPT changed nothing."),
    "Starting": ("starting", "Wait for Tailscale to start and retry; ADAPT changed nothing."),
    "InUseOtherUser": ("in_use_other_user", "Another OS user owns the Tailscale daemon; ADAPT changed nothing."),
    "NoState": ("no_state", "Start Tailscale and check its status; ADAPT changed nothing."),
}


class TailscaleError(ValueError):
    pass


_AUTH_GUIDES = {"needs_login": "tailscale.needs_login", "needs_machine_auth": "tailscale.needs_machine_auth",
                "in_use_other_user": "tailscale.in_use_other_user"}


def report_failure_id(report: dict[str, Any]) -> str | None:
    """Guide for a status, probe or worker-run report that is not a working result."""
    status = report.get("status")
    if status == "missing_client":
        return "tailscale.not_found"
    if status == "not_running":
        return _AUTH_GUIDES.get(report.get("auth_state"), "tailscale.not_running")
    if status == "tcp_unreachable":
        return "tailscale.tcp_unreachable"
    return ssh_worker.report_failure_id(report)  # worker-run returns an SSH worker report


def resolve_tailscale() -> str | None:
    explicit = os.environ.get("ADAPT_TAILSCALE", "").strip()
    if explicit:
        candidate = Path(explicit).expanduser()
        return str(candidate.resolve()) if candidate.is_file() else None
    found = shutil.which("tailscale")
    if found:
        return found
    if os.name == "nt":
        candidate = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Tailscale/tailscale.exe"
        if candidate.is_file():
            return str(candidate)
    return None


def _execute(argv: list[str], *, timeout: int = 12, input: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, input=input, capture_output=True, text=True, encoding="utf-8", errors="strict",
            timeout=timeout, check=False, shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        raise tagged(TailscaleError("The selected local command failed or timed out; no fallback or service restart ran."), "tailscale.local_command_failed") from exc


def _tail_ip(value: Any) -> str | None:
    if not isinstance(value, str) or "%" in value:
        return None
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    return str(address) if (address.version == 4 and address in _V4) or (address.version == 6 and address in _V6) else None


def _clean(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 1024 or re.search(r"[\x00-\x1f\x7f-\x9f]", value):
        return ""
    return value


def parse_status(text: str) -> dict[str, Any]:
    try:
        if len(text.encode("utf-8")) > 8 * 1024 * 1024:
            raise tagged(ValueError(), "tailscale.status_unrecognized")
        raw = json.loads(text)
        if not isinstance(raw, dict) or not isinstance(raw.get("BackendState"), str):
            raise tagged(ValueError(), "tailscale.status_unrecognized")
        source = raw.get("Peer")
        if source is None:
            source = {}
        if not isinstance(source, dict):
            raise tagged(ValueError(), "tailscale.status_unrecognized")
        peers, malformed = [], 0
        for value in source.values():
            if not isinstance(value, dict):
                malformed += 1; continue
            peer_id = _clean(value.get("ID"))
            addresses = value.get("TailscaleIPs")
            if not peer_id or not isinstance(addresses, list):
                malformed += 1; continue
            peers.append({"peer_id": peer_id, "hostname": _clean(value.get("HostName")),
                "dns_name": _clean(value.get("DNSName")).rstrip("."), "os": _clean(value.get("OS")),
                "addresses": list(dict.fromkeys(ip for address in addresses if (ip := _tail_ip(address)))),
                "online": value.get("Online") if type(value.get("Online")) is bool else None,
                "expired": value.get("Expired") is True})
        self_val = raw.get("Self")
        if isinstance(self_val, dict):
            self_ips = self_val.get("TailscaleIPs")
            self_addresses = list(dict.fromkeys(ip for address in self_ips if (ip := _tail_ip(address)))) if isinstance(self_ips, list) else []
            self_dict: dict[str, Any] | None = {
                "hostname": _clean(self_val.get("HostName")),
                "dns_name": _clean(self_val.get("DNSName")).rstrip("."),
                "addresses": self_addresses,
            }
        else:
            self_dict = None
        version_clean = _clean(raw.get("Version"))
        client_version = version_clean if version_clean else None
        return {"schema_version": 1, "backend_state": _clean(raw["BackendState"]),
            "client_version": client_version, "self": self_dict, "peers": peers,
            "malformed_peer_records": malformed, "peer_contact": False,
            "note": "Cached local Tailscale daemon observations. Online does not prove ADB is listening or authorized. User/keys/auth URLs and full raw status are not returned."}
    except (ValueError, TypeError) as exc:
        raise tagged(TailscaleError("Tailscale returned unrecognized or oversized status JSON."), "tailscale.status_unrecognized") from exc


def status() -> dict[str, Any]:
    binary = resolve_tailscale()
    if not binary:
        report = {"schema_version": 1, "status": "missing_client", "exit_code": 1, "peer_contact": False,
            "setup": "Install and sign into the official Tailscale client: https://tailscale.com/download"}
        if os.environ.get("ADAPT_TAILSCALE", "").strip():
            report["override_error"] = "ADAPT_TAILSCALE is set but does not point to an existing file; no other Tailscale client was tried."
        return report
    result = _execute([binary, "status", "--json"])
    if result.returncode:
        try:
            report = parse_status(result.stdout)
        except TailscaleError:
            report = None
        if report and report.get("backend_state") != "Running":
            auth_state, hint = _AUTH_STATE_HINTS.get(report.get("backend_state", ""), ("unknown", "Check Tailscale status outside ADAPT; ADAPT changed nothing."))
            return {**report, "status": "not_running", "exit_code": 1, "binary": binary, "auth_state": auth_state, "hint": hint}
        raise tagged(TailscaleError("Cannot read the local Tailscale daemon. Check its service/sign-in; no settings were changed."), "tailscale.daemon_unreadable")
    report = parse_status(result.stdout)
    is_running = report.get("backend_state") == "Running"
    auth_state, hint = _AUTH_STATE_HINTS.get(report.get("backend_state", ""), ("unknown", "Check Tailscale status outside ADAPT; ADAPT changed nothing."))
    return {**report, "status": "running" if is_running else "not_running",
            "exit_code": 0 if is_running else 1, "binary": binary,
            "auth_state": auth_state, "hint": hint}


def _validate_peer(peer: str) -> str:
    if not isinstance(peer, str) or peer != peer.strip() or not peer or len(peer) > 253:
        raise tagged(TailscaleError("Use an exact tailnet peer name, ID, MagicDNS name or Tailscale IP."), "tailscale.peer_invalid")
    candidate = peer[1:-1] if peer.startswith("[") and peer.endswith("]") else peer
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        if not _HOST.fullmatch(candidate.rstrip(".")) or any(not 1 <= len(label) <= 63 or label.startswith("-") or label.endswith("-") for label in candidate.rstrip(".").split(".")):
            raise tagged(TailscaleError("Peer must be a tailnet name/address, not a URL, endpoint, shell expression or option."), "tailscale.peer_invalid")
    else:
        normalized = _tail_ip(candidate)
        if not normalized:
            raise tagged(TailscaleError("Direct Tailscale mode accepts tailnet node IPs only; use an explicit worker for other networks."), "tailscale.peer_not_tailnet")
        return normalized
    return candidate.rstrip(".")


def _port(port: int) -> int:
    if type(port) is not int or not 1 <= port <= 65535:
        raise tagged(TailscaleError("Choose the current phone-displayed TCP port (1-65535); pairing and connection ports differ."), "tailscale.port_invalid")
    return port


def _select(peer: str) -> dict[str, Any]:
    query = _validate_peer(peer).casefold()
    report = status()
    if report.get("status") != "running":
        hint = report.get("hint")
        suffix = f" {hint}" if hint else ""
        raise tagged(TailscaleError(f"Tailscale must be running and signed in on this PC before selecting a peer.{suffix}"), report_failure_id(report) or "tailscale.not_running")
    matches = []
    for candidate in report["peers"]:
        aliases = {candidate["peer_id"], candidate["hostname"], candidate["dns_name"], candidate["dns_name"].split(".")[0], *candidate["addresses"]}
        if query in {name.casefold() for name in aliases if name}:
            matches.append(candidate)
    if len(matches) != 1:
        if not matches and isinstance(report.get("self"), dict):
            self_node = report["self"]
            self_dns = self_node.get("dns_name") or ""
            self_aliases = {self_node.get("hostname"), self_dns, self_dns.split(".")[0], *self_node.get("addresses", [])}
            if query in {name.casefold() for name in self_aliases if name}:
                raise tagged(TailscaleError("The selected name or address is this PC's own Tailscale node; choose a remote peer from adapt tailscale peers."), "tailscale.peer_is_self")
        raise tagged(TailscaleError("Peer selection is missing or ambiguous. Run adapt tailscale peers and choose an exact unique name or IP."),
                     "tailscale.peer_ambiguous" if matches else "tailscale.peer_not_found")
    selected = matches[0]
    if selected["online"] is not True or selected["expired"] or not selected["addresses"]:
        raise tagged(TailscaleError("The selected peer is offline, expired or has no usable tailnet address. No other peer was tried."),
                     "tailscale.peer_expired" if selected["expired"] else "tailscale.peer_offline")
    address = next((ip for ip in selected["addresses"] if ipaddress.ip_address(ip).version == 4), selected["addresses"][0])
    if sum(address in node["addresses"] for node in report["peers"]) != 1:
        raise tagged(TailscaleError("The selected address is ambiguous in Tailscale status."), "tailscale.address_ambiguous")
    return {**selected, "address": address}


def _endpoint(selected: dict[str, Any], port: int) -> str:
    address = selected["address"]
    return f"[{address}]:{_port(port)}" if ":" in address else f"{address}:{_port(port)}"


def _still_selected(selected: dict[str, Any]) -> None:
    current = _select(selected["peer_id"])
    if current["peer_id"] != selected["peer_id"] or selected["address"] not in current["addresses"]:
        raise tagged(TailscaleError("Tailnet peer identity/address changed during the operation. Inspect the selected endpoint; no automatic disconnect ran."), "tailscale.peer_changed")


def plan(peer: str, port: int, *, pairing_port: int | None = None, mode: str = "tls") -> dict[str, Any]:
    _validate_peer(peer); _port(port)
    if pairing_port is not None:
        _port(pairing_port)
    if mode not in {"tls", "legacy"}:
        raise tagged(TailscaleError("ADB mode must be tls or explicit legacy."), "tailscale.mode_invalid")
    commands = []
    if pairing_port is not None:
        commands.append(["adapt", "tailscale", "pair", "--peer", peer, "--port", str(pairing_port)])
    commands += [["adapt", "tailscale", "connect", "--peer", peer, "--port", str(port), "--mode", mode],
                 ["adapt", "tailscale", "check", "--peer", peer, "--port", str(port), "--mode", mode],
                 ["adapt", "tailscale", "adb", "--peer", peer, "--port", str(port), "--mode", mode, "--", "shell", "getprop", "ro.product.model"]]
    return {"schema_version": 1, "status": "plan_only", "mode": mode, "peer": peer, "connection_port": port,
        "pairing_port": pairing_port, "device_access": False, "peer_contact": False, "commands": commands,
        "requirements": ["Both endpoints signed into a permitted tailnet; selected peer is online", "Tailnet policy permits the selected TCP ports; Tailscale does not grant ADB authorization", "Android 11+ TLS: enable Wireless debugging and supply current pair/connect ports separately", "Android may require an active/trusted Wi-Fi network even though ADB traffic uses Tailscale", "No multicast/mDNS auto-discovery assumption across the overlay; MagicDNS is a separate naming service"],
        "legacy_boundary": "Legacy is explicit and requires an already enabled TCP ADB listener. It does not enable tcpip, root or change the phone. The overlay protects its path, but a legacy listener may also be exposed on other phone interfaces.",
        "note": "Offline preview only. Execution binds to one peer from local Tailscale status and uses its numeric address; no DNS fallback, scanning, VPN/ACL/route changes or shared ADB restart."}


def guide() -> dict[str, Any]:
    return {"schema_version": 1, "status": "offline_reference", "device_access": False, "peer_contact": False,
        "paths": {"direct_phone": "Android Tailscale + an authorized ADB listener. Pair/connect using the phone's current ports and the observed tailnet address.",
                  "usb_worker": "An explicitly chosen Windows/Linux tailnet worker owns the USB phone and runs ADAPT/ADB there through existing strict OpenSSH. The phone itself needs no Tailscale client.",
                  "subnet_router": "An approved subnet router can route to a LAN ADB listener, but route/service ownership needs separate verification. Direct-peer mode does not silently treat a LAN address as a tailnet node."},
        "options": ["status / peers: local daemon observations", "plan: offline pairing/connect workflow", "probe: one selected TCP-port reachability check", "pair: hidden or explicit stdin pairing secret, never an argv value", "connect / check: verify one authorized ADB transport and observed device identity", "adb: run one existing guarded ADB command on that exact endpoint", "disconnect: explicitly remove only the selected transport", "worker-plan / worker-run / worker-mcp-config: existing remote ADAPT functions over the selected tailnet peer"],
        "network": "Tailscale uses encrypted WireGuard paths, direct where possible and relayed where needed. A peer-online flag or Tailscale ping is not proof of an open ADB port. Relay latency matters for scrcpy.",
        "boundaries": ["No Tailscale installation, login, VPN toggle, ACL edit, subnet-route/exit-node change, public Funnel, raw ADB-server exposure or phone tcpip switch is automatic", "Ports can change after reboot or toggling Wireless debugging; re-read them", "Android normally permits one active VPN service; do not replace another VPN silently", "A verified connection can be used by existing app, logs, developer-option and scrcpy commands with the returned exact serial", "Tailscale transports IP traffic; it does not reproduce Wi-Fi Direct/Miracast radio handshakes"],
        "sources": ["https://tailscale.com/how-it-works", "https://tailscale.com/docs/features/magicdns", "https://tailscale.com/docs/install/android", "https://developer.android.com/tools/adb", "https://github.com/tailscale/tailscale/issues/1990"]}


@operation("Checking tailnet ADB port")
def probe(peer: str, port: int) -> dict[str, Any]:
    _port(port); selected = _select(peer)
    try:
        with socket.create_connection((selected["address"], port), timeout=4):
            opened = True
    except OSError:
        opened = False
    _still_selected(selected)
    return {"schema_version": 1, "status": "tcp_open" if opened else "tcp_unreachable", "exit_code": 0 if opened else 1,
        "peer": selected, "endpoint": _endpoint(selected, port), "adb_authorized": "not_checked", "peer_contact": True,
        "note": "One TCP connection only, no ADB request or scan. Check service port, Android Wi-Fi state and tailnet policy; no network settings were changed."}


def _adb_query(adb: Path, endpoint: str, args: list[str]) -> str:
    return dev_options._run_adb(adb, endpoint, args)


def _check(selected: dict[str, Any], port: int, adb: Path, mode: str) -> dict[str, Any]:
    if mode not in {"tls", "legacy"}:
        raise tagged(TailscaleError("ADB mode must be tls or explicit legacy."), "tailscale.mode_invalid")
    endpoint = _endpoint(selected, port)
    if _adb_query(adb, endpoint, ["get-state"]) != "device":
        raise tagged(TailscaleError("The exact tailnet ADB transport is not authorized and ready; no other transport was used."), "tailscale.adb_not_ready")
    identity = dev_options._read_identity(adb, endpoint)
    model = _adb_query(adb, endpoint, ["shell", "getprop", "ro.product.model"])
    api = _adb_query(adb, endpoint, ["shell", "getprop", "ro.build.version.sdk"])
    tls = _adb_query(adb, endpoint, ["shell", "getprop", "service.adb.tls.port"])
    if mode == "tls" and (not re.fullmatch(r"[0-9]{1,3}", api) or int(api) < 30 or tls != str(port)):
        if not tls.strip() and re.fullmatch(r"[0-9]{1,3}", api) and int(api) >= 30:
            raise tagged(TailscaleError("ADB responded, but the device reported no Wireless debugging TLS port (service.adb.tls.port is empty). The connection was left intact; re-read the current port on the phone's Wireless debugging screen or deliberately choose legacy mode."), "tailscale.tls_port_empty")
        raise tagged(TailscaleError("ADB responded, but its Android TLS listener did not match this port. The connection was left intact; inspect the current port or deliberately choose legacy mode."), "tailscale.tls_port_mismatch")
    _still_selected(selected)
    from dataclasses import asdict
    return {"schema_version": 1, "status": "verified", "peer": selected, "serial": endpoint, "device": asdict(identity),
        "model": model, "android_api": int(api) if re.fullmatch(r"[0-9]{1,3}", api) else None, "mode": mode,
        "tls_listener_port_match": tls == str(port), "peer_contact": True,
        "note": "Observed tailnet peer, exact authorized ADB endpoint and physical/build/user identity. This is not a claim about app correctness, a working mirror or access to other devices."}


@operation("Verifying tailnet ADB")
def check(peer: str, port: int, *, mode: str = "tls") -> dict[str, Any]:
    plan(peer, port, mode=mode)
    selected = _select(peer)
    return _check(selected, port, dev_options._resolve_adb_path(None), mode)


@operation("Connecting tailnet ADB")
def connect(peer: str, port: int, *, mode: str = "tls") -> dict[str, Any]:
    plan(peer, port, mode=mode)
    selected = _select(peer); endpoint = _endpoint(selected, port)
    adb = dev_options._resolve_adb_path(None)
    from adapt_adb.doctor import preflight_adb_server
    preflight_adb_server(adb, ["connect", endpoint])
    result = _execute([str(adb), "connect", endpoint], timeout=30)
    text = (result.stdout or "").strip()
    if result.returncode or not any(line.startswith(("connected to " + endpoint, "already connected to " + endpoint)) for line in text.splitlines()) or re.search(r"cannot|failed|error|unable", text, re.I):
        raise tagged(TailscaleError("ADB could not connect to the selected tailnet endpoint. Check pairing, current connection port and tailnet reachability; no fallback or disconnect ran."), "tailscale.connect_failed")
    return _check(selected, port, adb, mode)


@operation("Pairing tailnet ADB")
def pair(peer: str, port: int, code: str) -> dict[str, Any]:
    _port(port)
    if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
        raise tagged(TailscaleError("Use the current six-digit pairing code through the hidden prompt or --code-stdin; never put it in argv."), "tailscale.pair_code_missing")
    selected = _select(peer); endpoint = _endpoint(selected, port)
    adb = dev_options._resolve_adb_path(None)
    from adapt_adb.doctor import preflight_adb_server
    preflight_adb_server(adb, ["pair", endpoint])
    result = _execute([str(adb), "pair", endpoint], timeout=30, input=code + "\n")
    output = (result.stdout or "") + "\n" + (result.stderr or "")
    # ADB prints this prompt without a newline. Piped stdin is not echoed, so
    # the success response can share the prompt's line (AOSP commandline.cpp).
    response = (result.stdout or "").removeprefix("Enter pairing code: ")
    success = re.search(r"(?im)^Successfully paired to " + re.escape(endpoint) + r"(?:\s|$)", response)
    if result.returncode or not success or re.search(r"(?im)^\s*(?:error|failed|cannot|unable)\b", output):
        if re.search(r"refused|10061", output, re.I):
            reason = "The selected pairing port refused the TCP connection."
        elif re.search(r"protocol fault|couldn't read|connection reset", output, re.I):
            reason = "The endpoint did not complete the ADB pairing protocol (protocol/read failure)."
        else:
            reason = "Pairing failed or expired."
        raise tagged(TailscaleError(reason + " Re-open the phone pairing dialog and use its current pairing port and code; raw output and the secret were discarded."),
                     "tailscale.pair_refused" if reason.startswith("The selected pairing port refused") else "tailscale.pair_failed")
    _still_selected(selected)
    return {"schema_version": 1, "status": "paired", "peer": selected, "pairing_endpoint": endpoint,
        "connection_verified": False, "note": "Pairing is host authorization, not a connection. Connect using the separate current connection port. Pairing code was not stored or returned."}


def adb_command(peer: str, port: int, args: list[str], *, mode: str = "tls") -> int:
    # The caller supplies a device command only. Global/server/transport changes
    # have separate workflows and cannot override the mode's selected target.
    if args and args[0] == "--":
        args = args[1:]
    if not args or args[0].startswith("-") or is_host_only_adb_command(args) or args[0] in {"connect", "disconnect", "pair", "tcpip", "usb", "devices", "start-server", "kill-server", "server", "root", "unroot", "reconnect", "attach", "detach"} or (args[0] == "forward" and "--remove-all" in args):
        raise tagged(TailscaleError("Supply one device ADB command after --. Target, server, pairing and listener changes are managed separately."), "tailscale.adb_command_invalid")
    # Reject unsafe commands before even looking up a tailnet peer.
    validate_adb_args(["-s", "tailnet-placeholder:1", *args])
    report = check(peer, port, mode=mode)
    adb = dev_options._resolve_adb_path(None)
    argv = [str(adb), "-s", report["serial"], *args]
    result = subprocess.run(argv, check=False, timeout=60 if args[0] == "shell" else None, shell=False)
    return result.returncode


def disconnect(peer: str, port: int, *, yes: bool = False) -> dict[str, Any]:
    _port(port)
    if not yes:
        raise tagged(TailscaleError("Disconnecting can stop sessions on this one transport; use --yes. No other transport is disconnected."), "tailscale.disconnect_needs_yes")
    selected = _select(peer); endpoint = _endpoint(selected, port)
    adb = dev_options._resolve_adb_path(None)
    from adapt_adb.doctor import preflight_adb_server
    preflight_adb_server(adb, ["disconnect", endpoint])
    result = _execute([str(adb), "disconnect", endpoint])
    if result.returncode or not (result.stdout or "").lower().startswith("disconnected "):
        raise tagged(TailscaleError("Selected-endpoint disconnect was not confirmed; no broad disconnect was attempted."), "tailscale.disconnect_unconfirmed")
    return {"schema_version": 1, "status": "disconnected", "endpoint": endpoint, "peer": selected,
        "scope": "explicit_selected_transport_only", "phone_vpn_or_listener_changed": False}


def worker(peer: str, *, action: str = "plan", operation: str = "inspect", device: str | None = None,
           user: str | None = None, remote_shell: str = "posix", remote_adapt: str = "adapt") -> dict[str, Any]:
    peer = _validate_peer(peer)
    if user is not None and not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}", user):
        raise tagged(TailscaleError("Use a remote account name without shell syntax or @host."), "tailscale.worker_input_invalid")
    host = (user + "@" if user else "") + peer
    if action == "run":
        selected = _select(peer)
        host = (user + "@" if user else "") + selected["address"]
        return ssh_worker.run(host, operation=operation, device=device, remote_shell=remote_shell, remote_adapt=remote_adapt)
    run_target_note = "worker-run resolves the peer in local Tailscale status and connects to its numeric tailnet address, so the SSH known_hosts entry must cover that address."
    if action == "mcp":
        return {**ssh_worker.mcp_config(host, devices=[device] if device else None, remote_shell=remote_shell, remote_adapt=remote_adapt),
            "tailscale_peer_not_verified_offline": True, "run_target_note": run_target_note}
    if action != "plan":
        raise tagged(TailscaleError("Unknown worker action."), "tailscale.worker_input_invalid")
    return {**ssh_worker.plan(host, operation=operation, device=device, remote_shell=remote_shell, remote_adapt=remote_adapt),
            "tailscale_peer_not_verified_offline": True, "run_target_note": run_target_note,
            "note": "Explicit SSH-worker plan over a tailnet name/address. The worker owns USB/ADB access. Existing SSH host-key and key/agent checks apply; no raw ADB-server exposure or fallback."}


def _emit(fn, json_output: bool, **kwargs):
    context = FailureContext.from_runtime(command=current_command())
    try:
        report = fn(**kwargs)
        failure_id = report_failure_id(report)
    except (ValueError, dev_options.DevOptionsError, OSError, subprocess.SubprocessError) as exc:
        report = {"schema_version": 1, "status": "refused", "exit_code": 2, "error": str(exc)}
        failure_id = classify(exc)
    if json_output and failure_id:
        report = attach_failure(report, failure_id, context)
    typer.echo(json.dumps(report, indent=2) if json_output else "ADAPT / TAILSCALE MODE\n" + json.dumps(report, indent=2))
    if failure_id and not json_output:
        emit_failure(failure_id, context) if report.get("exit_code", 0) else emit_pointer(failure_id)
    if report.get("exit_code", 0):
        raise typer.Exit(report["exit_code"])


@tailscale_app.command("status")
@tailscale_app.command("peers")
def status_command(json_output: bool = typer.Option(False, "--json")):
    """Read local daemon state and minimal peer records; do not contact peers."""
    _emit(status, json_output)


@tailscale_app.command("guide")
def guide_command(json_output: bool = typer.Option(False, "--json")):
    """Explain direct-phone, USB-worker and subnet-router paths offline."""
    _emit(guide, json_output)


@tailscale_app.command("plan")
def plan_command(peer: str = typer.Option(..., "--peer"), port: int = typer.Option(..., "--port"), pairing_port: int | None = typer.Option(None, "--pairing-port"), mode: str = typer.Option("tls", "--mode"), json_output: bool = typer.Option(False, "--json")):
    """Preview direct ADB over Tailscale without reading status or touching a phone."""
    _emit(plan, json_output, peer=peer, port=port, pairing_port=pairing_port, mode=mode)


@tailscale_app.command("probe")
def probe_command(peer: str = typer.Option(..., "--peer"), port: int = typer.Option(..., "--port"), json_output: bool = typer.Option(False, "--json")):
    """Test one selected peer TCP port; this does not prove ADB authorization."""
    _emit(probe, json_output, peer=peer, port=port)


@tailscale_app.command("connect")
def connect_command(peer: str = typer.Option(..., "--peer"), port: int = typer.Option(..., "--port"), mode: str = typer.Option("tls", "--mode"), json_output: bool = typer.Option(False, "--json")):
    """Connect and verify exactly one peer's Android ADB listener."""
    _emit(connect, json_output, peer=peer, port=port, mode=mode)


@tailscale_app.command("check")
def check_command(peer: str = typer.Option(..., "--peer"), port: int = typer.Option(..., "--port"), mode: str = typer.Option("tls", "--mode"), json_output: bool = typer.Option(False, "--json")):
    """Verify an existing tailnet ADB transport, TLS port and device identity."""
    _emit(check, json_output, peer=peer, port=port, mode=mode)


@tailscale_app.command("pair")
def pair_command(peer: str = typer.Option(..., "--peer"), port: int = typer.Option(..., "--port"), code_stdin: bool = typer.Option(False, "--code-stdin", help="Read one six-digit secret from stdin instead of a hidden terminal prompt."), json_output: bool = typer.Option(False, "--json")):
    """Pair with Android's current TLS pairing port; code is never an argv value."""
    def pairing():
        _validate_peer(peer); _port(port)
        if code_stdin:
            code = sys.stdin.readline(64).strip()
        elif sys.stdin.isatty():
            code = getpass.getpass("Android pairing code: ").strip()
        else:
            raise tagged(TailscaleError("Pairing needs a hidden terminal prompt or explicit --code-stdin; no secret argv option exists."), "tailscale.pair_code_missing")
        return pair(peer, port, code)
    _emit(pairing, json_output)


@tailscale_app.command("disconnect")
def disconnect_command(peer: str = typer.Option(..., "--peer"), port: int = typer.Option(..., "--port"), yes: bool = typer.Option(False, "--yes"), json_output: bool = typer.Option(False, "--json")):
    """Disconnect this one endpoint only; VPN/listener and other devices are preserved."""
    _emit(disconnect, json_output, peer=peer, port=port, yes=yes)


@tailscale_app.command("adb", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def adb_command_cli(ctx: typer.Context, peer: str = typer.Option(..., "--peer"), port: int = typer.Option(..., "--port"), mode: str = typer.Option("tls", "--mode")):
    """Run a guarded device command: ... -- shell getprop ro.product.model."""
    try:
        code = adb_command(peer, port, list(ctx.args), mode=mode)
    except (ValueError, dev_options.DevOptionsError, OSError, subprocess.SubprocessError) as exc:
        typer.echo(str(exc), err=True)
        emit_failure(classify(exc), FailureContext.from_runtime(command="tailscale adb"))
        raise typer.Exit(2) from exc
    raise typer.Exit(code)


@tailscale_app.command("worker-plan")
def worker_plan_command(peer: str = typer.Option(..., "--peer"), operation: str = typer.Option("inspect", "--operation"), device: str | None = typer.Option(None, "--device"), user: str | None = typer.Option(None, "--user"), remote_shell: str = typer.Option("posix", "--remote-shell"), remote_adapt: str = typer.Option("adapt", "--remote-adapt"), json_output: bool = typer.Option(False, "--json")):
    """Plan an existing SSH-worker operation on a tailnet host, offline."""
    _emit(worker, json_output, peer=peer, operation=operation, device=device, user=user, remote_shell=remote_shell, remote_adapt=remote_adapt)


@tailscale_app.command("worker-run")
def worker_run_command(peer: str = typer.Option(..., "--peer"), operation: str = typer.Option("inspect", "--operation"), device: str | None = typer.Option(None, "--device"), user: str | None = typer.Option(None, "--user"), remote_shell: str = typer.Option("posix", "--remote-shell"), remote_adapt: str = typer.Option("adapt", "--remote-adapt"), json_output: bool = typer.Option(False, "--json")):
    """Run an allowlisted read-only check on an explicitly verified tailnet worker."""
    _emit(worker, json_output, peer=peer, action="run", operation=operation, device=device, user=user, remote_shell=remote_shell, remote_adapt=remote_adapt)


@tailscale_app.command("worker-mcp-config")
def worker_mcp_command(peer: str = typer.Option(..., "--peer"), device: str = typer.Option(..., "--device"), user: str | None = typer.Option(None, "--user"), remote_shell: str = typer.Option("posix", "--remote-shell"), remote_adapt: str = typer.Option("adapt", "--remote-adapt"), json_output: bool = typer.Option(False, "--json")):
    """Generate a target-restricted, read-only MCP-over-SSH entry without connecting."""
    _emit(worker, json_output, peer=peer, action="mcp", device=device, user=user, remote_shell=remote_shell, remote_adapt=remote_adapt)
