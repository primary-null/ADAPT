"""MCP failure guidance: the additive `failure` key, policy-aware next tool, unsafe shared server.

Offline: adb resolution, enumeration and the shared-server preflight are faked; nothing is spawned.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from adapt_adb import failure_report as fr
from adapt_adb.doctor import UnsafeAdbServerError
from adapt_adb.mcp import tools
from adapt_adb.mcp.access import AccessPolicy
from adapt_adb.mcp.server import dispatch_tool_call


def _payload(result):
    return json.loads(result.content[-1].text)


@pytest.fixture
def no_spawn(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("adb was spawned"))
    monkeypatch.setattr(tools, "resolve_adb", lambda: Path("adb"))


def _devices(monkeypatch, entries):
    monkeypatch.setattr("adapt_adb.doctor.preflight_adb_server", lambda *a, **k: None)
    monkeypatch.setattr("adapt_adb.adb.get_connected_devices", lambda *a, **k: [dict(e) for e in entries])


@pytest.mark.parametrize("tool,args", [("list_devices", {}), ("device_info", {}), ("tap", {"x": 1, "y": 2}),
                                       ("screenshot", {}), ("ui_summary", {}), ("logcat_tail", {})])
def test_unsafe_shared_server_fails_closed_with_specific_code(tool, args, monkeypatch, no_spawn):
    def refuse(*a, **k):
        raise fr.tagged(UnsafeAdbServerError("Listening ADB server ownership/version is unverified or unsafe."),
                        "adb.server.foreign_owner")
    monkeypatch.setattr("adapt_adb.doctor.preflight_adb_server", refuse)
    result = dispatch_tool_call(tool, args)
    payload = _payload(result)
    assert result.is_error is True
    assert payload["error"] == "unsafe_shared_server"
    assert payload["message"] == "Shared ADB server on port 5037 is unverified or unsafe; ADAPT did not contact it."
    assert payload["devices"] == []
    failure = payload["failure"]
    assert failure["id"] == "adb.server.foreign_owner"
    assert failure["next_tool"] == {"name": "doctor", "arguments": {}}
    assert failure["retry"] == "after_user_action" and failure["user_action_required"] is True


def test_untagged_unsafe_server_uses_generic_guide(monkeypatch, no_spawn):
    monkeypatch.setattr("adapt_adb.doctor.preflight_adb_server",
                        lambda *a, **k: (_ for _ in ()).throw(UnsafeAdbServerError("x")))
    payload = _payload(dispatch_tool_call("list_devices", {}))
    assert payload["error"] == "unsafe_shared_server" and payload["failure"]["id"] == "adb.server.unverified"


def test_dispatcher_catches_unsafe_server_raised_inside_a_tool(monkeypatch, no_spawn):
    monkeypatch.setattr(tools, "tool_device_info", lambda **k: (_ for _ in ()).throw(UnsafeAdbServerError("x")))
    payload = _payload(dispatch_tool_call("device_info", {}))
    assert payload["error"] == "unsafe_shared_server"


def test_unsafe_server_next_tool_respects_device_restriction(monkeypatch, no_spawn):
    monkeypatch.setattr("adapt_adb.doctor.preflight_adb_server",
                        lambda *a, **k: (_ for _ in ()).throw(UnsafeAdbServerError("x")))
    policy = AccessPolicy(allowed_devices=("A",))
    payload = _payload(dispatch_tool_call("device_info", {"device": "A"}, policy=policy))
    assert payload["failure"]["next_tool"] == {"name": "setup_status", "arguments": {}}
    assert not policy.exposes("doctor")


def test_policy_refusals_carry_failure(monkeypatch, no_spawn):
    unknown = _payload(dispatch_tool_call("no_such_tool", {}))
    assert unknown["error"] == "unknown_tool" and unknown["failure"]["id"] == "mcp.unknown_tool"
    invalid = _payload(dispatch_tool_call("tap", {"x": "one"}))
    assert invalid["error"] == "invalid_params" and invalid["failure"]["id"] == "mcp.invalid_params"
    assert invalid["failure"]["retry"] == "with_changed_arguments"
    assert "x" in invalid["failure"]["argument_hint"] and "y" in invalid["failure"]["argument_hint"]
    denied = _payload(dispatch_tool_call("tap", {"x": 1, "y": 2}, policy=AccessPolicy(read_only=True)))
    assert denied["error"] == "access_denied" and denied["failure"]["retry"] == "not_useful"
    assert denied["failure"]["next_tool"] is None


def test_device_resolution_errors_carry_refined_failure(monkeypatch, no_spawn):
    _devices(monkeypatch, [{"serial": "A", "state": "unauthorized"}])
    payload = _payload(dispatch_tool_call("device_info", {}))
    assert payload["error"] == "no_device" and payload["failure"]["id"] == "device.unauthorized"
    assert payload["failure"]["ask_user"] and payload["failure"]["next_tool"]["name"] == "list_devices"
    assert payload["message"].startswith("No connected device in 'device' state.")  # unchanged

    _devices(monkeypatch, [{"serial": "A", "state": "device"}, {"serial": "B", "state": "device"}])
    payload = _payload(dispatch_tool_call("device_info", {}))
    assert payload["error"] == "need_device" and payload["failure"]["id"] == "device.multiple"
    assert payload["failure"]["next_tool"] is None and "devices[].device" in payload["failure"]["argument_hint"]

    payload = _payload(dispatch_tool_call("device_info", {"device": "Z"}))
    assert payload["error"] == "unknown_device" and payload["failure"]["id"] == "device.not_found"

    _devices(monkeypatch, [{"serial": "A", "state": "offline"}, {"serial": "B", "state": "device"}])
    payload = _payload(dispatch_tool_call("device_info", {"device": "A"}))
    assert payload["error"] == "no_device" and payload["failure"]["id"] == "device.offline"


def test_device_restricted_session_never_names_hidden_devices_in_failure(monkeypatch, no_spawn):
    _devices(monkeypatch, [{"serial": "HIDDENSERIAL", "state": "unauthorized"}, {"serial": "A", "state": "offline"}])
    payload = _payload(dispatch_tool_call("device_info", {"device": "A"}, policy=AccessPolicy(allowed_devices=("A",))))
    assert "HIDDENSERIAL" not in json.dumps(payload)
    assert payload["failure"]["id"] == "device.offline"


def test_adb_missing_and_exception_codes_carry_failure(monkeypatch):
    monkeypatch.setattr(tools, "resolve_adb", lambda: None)
    payload = _payload(dispatch_tool_call("list_devices", {}))
    assert payload["error"] == "no_adb" and payload["failure"]["id"] == "adb.not_found"
    assert payload["failure"]["next_tool"] == {"name": "setup_status", "arguments": {}}
    for exc, code, guide in ((subprocess.TimeoutExpired(["adb"], 1), "adb_timeout", "adb.timeout"),
                             (OSError("launch"), "adb_launch_failed", "adb.exec_failed"),
                             (KeyError("boom"), "internal_error", "internal.unclassified")):
        monkeypatch.setattr(tools, "tool_list_devices", lambda exc=exc: (_ for _ in ()).throw(exc))
        payload = _payload(dispatch_tool_call("list_devices", {}))
        assert payload["error"] == code and payload["failure"]["id"] == guide
        assert "boom" not in json.dumps(payload["failure"])


def test_probe_failure_carries_failure(monkeypatch, no_spawn):
    monkeypatch.setattr("adapt_adb.doctor.preflight_adb_server", lambda *a, **k: None)
    monkeypatch.setattr("adapt_adb.adb.get_connected_devices", lambda *a, **k: (_ for _ in ()).throw(tools.AdbProbeError("x")))
    payload = _payload(dispatch_tool_call("list_devices", {}))
    assert payload["error"] == "adb_probe_failed" and payload["failure"]["id"] == "adb.enumeration_failed"


@pytest.mark.parametrize("policy", [AccessPolicy(), AccessPolicy(read_only=True), AccessPolicy(allowed_devices=("A",)),
                                    AccessPolicy(read_only=True, allowed_devices=("A",))])
def test_next_tool_is_always_exposed(policy, monkeypatch):
    monkeypatch.setattr(tools, "resolve_adb", lambda: None)
    for name, args in (("list_devices", {}), ("device_info", {"device": "A"}), ("tap", {"x": 1, "y": 2, "device": "A"})):
        payload = _payload(dispatch_tool_call(name, args, policy=policy))
        failure = payload["failure"]
        if failure["next_tool"] is not None:
            assert policy.exposes(failure["next_tool"]["name"])
        assert all(r["mcp_tool"] is None or policy.exposes(r["mcp_tool"]) for r in failure["recommendations"])


def test_success_payload_is_unchanged(monkeypatch, no_spawn):
    _devices(monkeypatch, [{"serial": "A", "state": "device"}])
    payload = _payload(dispatch_tool_call("list_devices", {}))
    assert "failure" not in payload and "error" not in payload
    assert payload["devices"][0]["device"] == "A"


def test_direct_tool_payload_keeps_plain_error_shape(monkeypatch, no_spawn):
    monkeypatch.setattr("adapt_adb.doctor.preflight_adb_server",
                        lambda *a, **k: (_ for _ in ()).throw(UnsafeAdbServerError("x")))
    payload = tools.tool_list_devices()
    assert payload == {"error": "unsafe_shared_server", "message": fr.UNSAFE_SHARED_SERVER_MESSAGE, "devices": []}
    assert json.loads(json.dumps(payload)) == payload
