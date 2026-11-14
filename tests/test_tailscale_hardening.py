"""Hardening tests for Tailscale mode peer normalization, error hints, self-targeting, and overrides."""
import json
import os
import subprocess
from pathlib import Path

import pytest

from adapt_adb import ssh_worker
from adapt_adb import tailscale_mode as ts
from adapt_adb.dev_options import DeviceIdentity


def forbid(*a, **k):
    pytest.fail("Unexpected peer, ADB, socket or process action")


def raw_status_with_self():
    return {
        "BackendState": "Running",
        "Version": "1.76.1",
        "AuthURL": "private-login-url",
        "User": {"private": "private-user-metadata"},
        "Self": {
            "ID": "selfnode",
            "HostName": "my-laptop",
            "DNSName": "my-laptop.example.ts.net.",
            "OS": "windows",
            "TailscaleIPs": ["100.64.0.1", "fd7a:115c:a1e0::1", "8.8.8.8"],
            "PublicKey": "not-for-output",
        },
        "Peer": {
            "nodekey:private": {
                "ID": "node1",
                "HostName": "phone",
                "DNSName": "phone.example.ts.net.",
                "OS": "android",
                "Online": True,
                "TailscaleIPs": ["100.64.0.2", "fd7a:115c:a1e0::2"],
                "PublicKey": "not-for-output",
                "Expired": False,
            }
        },
    }


def test_worker_normalizes_peer_and_sets_run_target_note(monkeypatch):
    monkeypatch.setattr(ts, "_execute", forbid)

    # 1. Bare bracketed IPv6 in plan
    plan_rep = ts.worker("[fd7a:115c:a1e0::2]", operation="doctor")
    assert plan_rep["worker"]["host"] == "fd7a:115c:a1e0::2"
    assert "fd7a:115c:a1e0::2" in plan_rep["ssh_argv"]
    assert not any("[" in arg and "]" in arg and "fd7a:115c:a1e0::2" in arg for arg in plan_rep["ssh_argv"])
    assert "run_target_note" in plan_rep
    assert "worker-run resolves the peer in local Tailscale status and connects to its numeric tailnet address, so the SSH known_hosts entry must cover that address." == plan_rep["run_target_note"]

    # 2. Bare bracketed IPv6 in mcp
    mcp_rep = ts.worker("[fd7a:115c:a1e0::2]", action="mcp", device="SERIAL123")
    args = mcp_rep["client_config"]["mcpServers"]["adapt-worker"]["args"]
    assert "fd7a:115c:a1e0::2" in args
    assert not any("[" in arg and "]" in arg and "fd7a:115c:a1e0::2" in arg for arg in args)
    assert "run_target_note" in mcp_rep
    assert "worker-run resolves the peer in local Tailscale status and connects to its numeric tailnet address, so the SSH known_hosts entry must cover that address." == mcp_rep["run_target_note"]

    # 3. user="ops" yields ops@fd7a:115c:a1e0::2
    user_rep = ts.worker("[fd7a:115c:a1e0::2]", operation="doctor", user="ops")
    assert user_rep["worker"]["host"] == "ops@fd7a:115c:a1e0::2"

    # 4. trailing-dot DNS name is normalised
    dns_rep = ts.worker("host.example.ts.net.", operation="doctor")
    assert dns_rep["worker"]["host"] == "host.example.ts.net"

    # 5. whitespace-padded peer is still refused
    with pytest.raises(ts.TailscaleError):
        ts.worker("  fd7a:115c:a1e0::2  ")


def test_worker_run_resolves_through_select_and_connects_to_numeric_address(monkeypatch):
    status_val = {
        **ts.parse_status(json.dumps(raw_status_with_self())),
        "status": "running",
        "exit_code": 0,
    }
    monkeypatch.setattr(ts, "status", lambda: status_val)

    calls = []
    monkeypatch.setattr(ssh_worker, "run", lambda host, **kw: calls.append((host, kw)) or {"status": "success"})

    # action="run" uses numeric address
    ts.worker("phone", action="run", user="ops")
    assert calls[0][0] == "ops@100.64.0.2"

    # offline or unknown peer raises before ssh_worker.run
    monkeypatch.setattr(ssh_worker, "run", forbid)
    with pytest.raises(ts.TailscaleError):
        ts.worker("unknown-peer", action="run", user="ops")

    # set peer offline
    status_val["peers"][0]["online"] = False
    with pytest.raises(ts.TailscaleError):
        ts.worker("phone", action="run", user="ops")


def test_override_error_reported_for_invalid_env_overrides(monkeypatch):
    monkeypatch.setenv("ADAPT_TAILSCALE", "C:/nonexistent/tailscale_binary.exe")
    rep_ts = ts.status()
    assert rep_ts["status"] == "missing_client"
    assert rep_ts["override_error"] == "ADAPT_TAILSCALE is set but does not point to an existing file; no other Tailscale client was tried."
    assert "nonexistent" not in str(rep_ts)

    monkeypatch.delenv("ADAPT_TAILSCALE", raising=False)
    monkeypatch.setattr(ts, "resolve_tailscale", lambda: None)
    rep_ts_unset = ts.status()
    assert rep_ts_unset["status"] == "missing_client"
    assert "override_error" not in rep_ts_unset

    monkeypatch.setenv("ADAPT_SSH", "C:/nonexistent/ssh_binary.exe")
    rep_ssh = ssh_worker.status()
    assert rep_ssh["status"] == "missing_client"
    assert rep_ssh["override_error"] == "ADAPT_SSH is set but does not point to an existing file; no other SSH client was tried."
    assert "nonexistent" not in str(rep_ssh)

    monkeypatch.delenv("ADAPT_SSH", raising=False)
    monkeypatch.setattr(ssh_worker, "resolve_ssh", lambda: None)
    rep_ssh_unset = ssh_worker.status()
    assert rep_ssh_unset["status"] == "missing_client"
    assert "override_error" not in rep_ssh_unset


@pytest.mark.parametrize("backend_state,expected_auth,has_hint", [
    ("Running", "running", False),
    ("NeedsLogin", "needs_login", True),
    ("NeedsMachineAuth", "needs_machine_auth", True),
    ("Stopped", "stopped", True),
    ("Starting", "starting", True),
    ("InUseOtherUser", "in_use_other_user", True),
    ("NoState", "no_state", True),
    ("CustomState", "unknown", True),
])
def test_backend_states_mapping_and_hints(monkeypatch, backend_state, expected_auth, has_hint):
    data = {"BackendState": backend_state, "Peer": {}}
    monkeypatch.setattr(ts, "resolve_tailscale", lambda: "mock-tailscale")
    monkeypatch.setattr(ts, "_execute", lambda *a, **k: subprocess.CompletedProcess(["tailscale"], 0, json.dumps(data), ""))

    rep = ts.status()
    assert rep["auth_state"] == expected_auth
    if has_hint:
        assert isinstance(rep["hint"], str) and len(rep["hint"]) > 0
        assert "ADAPT changed nothing" in rep["hint"]
        assert rep["status"] == "not_running"
        assert rep["exit_code"] == 1
    else:
        assert rep["hint"] is None
        assert rep["status"] == "running"
        assert rep["exit_code"] == 0


def test_status_nonzero_exit_behavior(monkeypatch):
    monkeypatch.setattr(ts, "resolve_tailscale", lambda: "mock-tailscale")

    # non-zero exit + valid NeedsLogin JSON -> not_running report
    nl_data = json.dumps({"BackendState": "NeedsLogin", "AuthURL": "https://login.tailscale.com/secret", "Peer": {}})
    monkeypatch.setattr(ts, "_execute", lambda *a, **k: subprocess.CompletedProcess(["tailscale"], 1, nl_data, ""))
    rep = ts.status()
    assert rep["status"] == "not_running"
    assert rep["exit_code"] == 1
    assert rep["auth_state"] == "needs_login"
    assert rep["hint"] is not None
    assert "https://login.tailscale.com/secret" not in json.dumps(rep)

    # non-zero exit + invalid output -> TailscaleError
    monkeypatch.setattr(ts, "_execute", lambda *a, **k: subprocess.CompletedProcess(["tailscale"], 1, "failed to connect to daemon", ""))
    with pytest.raises(ts.TailscaleError, match="Cannot read the local Tailscale daemon"):
        ts.status()

    # non-zero exit + Running JSON -> TailscaleError (must never produce running on non-zero exit)
    running_data = json.dumps({"BackendState": "Running", "Peer": {}})
    monkeypatch.setattr(ts, "_execute", lambda *a, **k: subprocess.CompletedProcess(["tailscale"], 1, running_data, ""))
    with pytest.raises(ts.TailscaleError, match="Cannot read the local Tailscale daemon"):
        ts.status()


def test_select_not_running_includes_hint_in_error_and_never_leaks_auth_url(monkeypatch):
    nl_data = json.dumps({"BackendState": "NeedsLogin", "AuthURL": "https://login.tailscale.com/secret_auth_url", "Peer": {}})
    monkeypatch.setattr(ts, "resolve_tailscale", lambda: "mock-tailscale")
    monkeypatch.setattr(ts, "_execute", lambda *a, **k: subprocess.CompletedProcess(["tailscale"], 0, nl_data, ""))

    rep = ts.status()
    assert "secret_auth_url" not in json.dumps(rep)

    with pytest.raises(ts.TailscaleError) as excinfo:
        ts._select("phone")
    err_msg = str(excinfo.value)
    assert "Tailscale must be running and signed in on this PC before selecting a peer." in err_msg
    assert "Sign in with the Tailscale app or tailscale login; ADAPT changed nothing." in err_msg
    assert "secret_auth_url" not in err_msg


def test_self_targeting_detection_and_keys(monkeypatch):
    raw = raw_status_with_self()
    rep = ts.parse_status(json.dumps(raw))

    # Self contains only hostname, dns_name, addresses
    assert set(rep["self"].keys()) == {"hostname", "dns_name", "addresses"}
    assert rep["self"]["hostname"] == "my-laptop"
    assert rep["self"]["dns_name"] == "my-laptop.example.ts.net"
    assert rep["self"]["addresses"] == ["100.64.0.1", "fd7a:115c:a1e0::1"]
    assert rep["client_version"] == "1.76.1"

    # Absent version becomes None
    raw_no_ver = dict(raw)
    del raw_no_ver["Version"]
    rep_no_ver = ts.parse_status(json.dumps(raw_no_ver))
    assert rep_no_ver["client_version"] is None

    # Absent Self becomes None
    raw_no_self = dict(raw)
    del raw_no_self["Self"]
    rep_no_self = ts.parse_status(json.dumps(raw_no_self))
    assert rep_no_self["self"] is None

    # _select targeting self aliases raises specific self message
    monkeypatch.setattr(ts, "status", lambda: {**rep, "status": "running", "exit_code": 0})

    for self_alias in ("my-laptop", "MY-LAPTOP", "my-laptop.example.ts.net", "100.64.0.1", "fd7a:115c:a1e0::1"):
        with pytest.raises(ts.TailscaleError, match=r"The selected name or address is this PC's own Tailscale node; choose a remote peer from adapt tailscale peers\."):
            ts._select(self_alias)

    # Unknown target raises generic missing/ambiguous message
    with pytest.raises(ts.TailscaleError, match=r"Peer selection is missing or ambiguous\. Run adapt tailscale peers and choose an exact unique name or IP\."):
        ts._select("unknown-node")


def test_check_tls_port_empty_versus_mismatch(monkeypatch):
    monkeypatch.setattr(ts.dev_options, "_read_identity", lambda *a: DeviceIdentity("physical-phone", "build", 0))
    monkeypatch.setattr(ts, "_still_selected", lambda *a: None)

    selected = {"peer_id": "node1", "address": "100.64.0.2", "addresses": ["100.64.0.2"]}

    # Case 1: empty service.adb.tls.port -> specific message
    def query_empty(adb, endpoint, args):
        if args == ["get-state"]:
            return "device"
        if args[-1] == "ro.build.version.sdk":
            return "33"
        if args[-1] == "ro.product.model":
            return "PHONE"
        if args[-1] == "service.adb.tls.port":
            return ""
        return ""

    monkeypatch.setattr(ts, "_adb_query", query_empty)
    with pytest.raises(ts.TailscaleError, match=r"ADB responded, but the device reported no Wireless debugging TLS port \(service\.adb\.tls\.port is empty\)\. The connection was left intact; re-read the current port on the phone's Wireless debugging screen or deliberately choose legacy mode\."):
        ts._check(selected, 37123, Path("mock-adb"), "tls")

    # Case 2: non-empty mismatching service.adb.tls.port -> general message
    def query_mismatch(adb, endpoint, args):
        if args == ["get-state"]:
            return "device"
        if args[-1] == "ro.build.version.sdk":
            return "33"
        if args[-1] == "ro.product.model":
            return "PHONE"
        if args[-1] == "service.adb.tls.port":
            return "5555"
        return ""

    monkeypatch.setattr(ts, "_adb_query", query_mismatch)
    with pytest.raises(ts.TailscaleError, match=r"ADB responded, but its Android TLS listener did not match this port\. The connection was left intact; inspect the current port or deliberately choose legacy mode\."):
        ts._check(selected, 37123, Path("mock-adb"), "tls")


def test_guide_is_offline_and_does_not_execute(monkeypatch):
    monkeypatch.setattr(ts, "_execute", forbid)
    guide_rep = ts.guide()
    assert guide_rep["status"] == "offline_reference"
    assert guide_rep["peer_contact"] is False
    assert guide_rep["device_access"] is False
