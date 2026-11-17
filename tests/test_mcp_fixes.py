"""MCP fixes: explicit device for tools that act on the phone, server version, refused adb
commands, setup_status and distribution adb, refusal wording, and the example configurations.

Offline: adb resolution, device listings and subprocess are faked; nothing real is spawned.
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp")

import adapt_adb
from adapt_adb.adb import AdbProbeError
from adapt_adb.doctor import UnsafeAdbServerError
from adapt_adb.failure_guide import GUIDES
from adapt_adb.mcp import server as mcp_server
from adapt_adb.mcp import tools
from adapt_adb.mcp.access import MUTATING_TOOLS, AccessPolicy
from adapt_adb.mcp.server import create_server, dispatch_tool_call, get_mcp_tools
from adapt_adb.safety import ForbiddenArgv, SafetyError, check_no_destructive_package_ops, validate_adb_args

ROOT = Path(__file__).resolve().parents[1]

ONE_READY = (
    "List of devices attached\n"
    "SERIAL_A\tdevice usb:1-1 product:example model:Example_Phone device:example transport_id:1\n"
)
TWO_READY = ONE_READY + (
    "SERIAL_B\tdevice usb:1-2 product:example_tab model:Example_Tablet device:example_tab transport_id:2\n"
)
NONE_READY = "List of devices attached\nSERIAL_A\tunauthorized usb:1-1 transport_id:1\n"

# The tools that change or act on the phone, with valid arguments (no `device`).
MUTATING_ARGS: dict[str, dict[str, Any]] = {
    "tap": {"x": 10, "y": 20},
    "swipe": {"x1": 1, "y1": 2, "x2": 3, "y2": 4},
    "key": {"key": "BACK"},
    "type": {"text": "hello"},
    "install_apk": {"apk": "/tmp/app-debug.apk"},
}


def _payload(result: Any) -> dict[str, Any]:
    return json.loads(result.content[-1].text)


class FakeAdb:
    """Records every adb argv; answers device listings and succeeds for everything else."""

    def __init__(self, listing: str) -> None:
        self.listing = listing
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append([str(a) for a in argv])
        if "devices" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=self.listing, stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="Success\n", stderr="")

    @property
    def actions(self) -> list[list[str]]:
        """Calls that are neither the device listing nor the version probe."""
        return [c for c in self.calls if "devices" not in c and "version" not in c]


@pytest.fixture
def adb(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("adapt_adb.doctor.preflight_adb_server", lambda *a, **k: None)
    monkeypatch.setattr(tools, "resolve_adb", lambda: Path("/mock/adb"))

    def install(listing: str) -> FakeAdb:
        fake = FakeAdb(listing)
        monkeypatch.setattr(subprocess, "run", fake)
        return fake

    return install


# ---------------------------------------------------------------------------
# Item 1: tools that act on the phone never pick a device implicitly
# ---------------------------------------------------------------------------

def test_the_set_of_tools_acting_on_the_phone_is_pinned():
    # The original five; the agent-loop tools added later are pinned in tests/test_mcp_agent_tools.py.
    assert set(MUTATING_ARGS) == {"tap", "swipe", "key", "type", "install_apk"} <= set(MUTATING_TOOLS)


@pytest.mark.parametrize("listing", [ONE_READY, TWO_READY, NONE_READY], ids=["one-ready", "two-ready", "none-ready"])
@pytest.mark.parametrize("name", sorted(MUTATING_ARGS))
def test_mutating_tool_without_device_returns_need_device_and_does_nothing(name, listing, adb):
    fake = adb(listing)
    result = dispatch_tool_call(name, dict(MUTATING_ARGS[name]))
    payload = _payload(result)
    assert result.is_error is True and payload["error"] == "need_device"
    assert {d["device"] for d in payload["devices"]} == {"SERIAL_A", "SERIAL_B"} & {
        d.split("\t")[0] for d in listing.splitlines()[1:]
    }
    assert "explicit `device`" in payload["message"]
    assert "`device`" in payload["message"] and "--device SERIAL" in payload["message"]
    assert fake.actions == []  # only the read-only device listing ran


@pytest.mark.parametrize("name", sorted(MUTATING_ARGS))
def test_mutating_tool_without_device_carries_retry_with_changed_arguments(name, adb):
    adb(ONE_READY)
    failure = _payload(dispatch_tool_call(name, dict(MUTATING_ARGS[name])))["failure"]
    assert failure["retry"] == "with_changed_arguments"
    assert failure["id"] in GUIDES
    assert "devices[].device" in failure["argument_hint"] and "only one" in failure["argument_hint"]
    assert failure["user_action_required"] is False


@pytest.mark.parametrize("name", sorted(MUTATING_ARGS))
def test_blank_device_is_not_an_explicit_device(name, adb):
    fake = adb(ONE_READY)
    payload = _payload(dispatch_tool_call(name, {**MUTATING_ARGS[name], "device": "   "}))
    assert payload["error"] == "need_device" and fake.actions == []


@pytest.mark.parametrize("name", sorted(MUTATING_ARGS))
def test_direct_tool_functions_also_refuse_an_unnamed_device(name, adb):
    fake = adb(ONE_READY)
    result = getattr(tools, f"tool_{name}")(**MUTATING_ARGS[name])
    assert result["error"] == "need_device" and fake.actions == []


@pytest.mark.parametrize("name", sorted(MUTATING_ARGS))
def test_explicit_device_works_as_before(name, adb):
    fake = adb(ONE_READY)
    result = dispatch_tool_call(name, {**MUTATING_ARGS[name], "device": "SERIAL_A"})
    assert result.is_error is False
    assert len(fake.actions) == 1 and fake.actions[0][1:3] == ["-s", "SERIAL_A"]


@pytest.mark.parametrize("name", sorted(MUTATING_ARGS))
def test_one_device_session_uses_that_device_when_omitted(name, adb):
    fake = adb(TWO_READY)  # a second phone is connected but not allowed
    policy = AccessPolicy(allowed_devices=("SERIAL_B",))
    result = dispatch_tool_call(name, dict(MUTATING_ARGS[name]), policy=policy)
    assert result.is_error is False
    assert len(fake.actions) == 1 and fake.actions[0][1:3] == ["-s", "SERIAL_B"]


def test_one_device_session_with_a_repeated_target_still_counts_as_one(adb):
    fake = adb(ONE_READY)
    policy = AccessPolicy(allowed_devices=("SERIAL_A", "SERIAL_A"))
    assert dispatch_tool_call("tap", {"x": 1, "y": 2}, policy=policy).is_error is False
    assert fake.actions[0][1:3] == ["-s", "SERIAL_A"]


@pytest.mark.parametrize("name", sorted(MUTATING_ARGS))
def test_multi_device_session_without_argument_returns_need_device(name, adb):
    fake = adb(TWO_READY + "SERIAL_C\tdevice usb:1-3 model:Hidden transport_id:3\n")
    policy = AccessPolicy(allowed_devices=("SERIAL_A", "SERIAL_B"))
    result = dispatch_tool_call(name, dict(MUTATING_ARGS[name]), policy=policy)
    payload = _payload(result)
    assert result.is_error is True and payload["error"] == "need_device"
    assert {d["device"] for d in payload["devices"]} == {"SERIAL_A", "SERIAL_B"}  # allowed targets only
    assert "SERIAL_C" not in json.dumps(payload)
    assert fake.actions == []


def test_restricted_sessions_still_deny_a_device_outside_the_allowed_list(adb):
    fake = adb(TWO_READY)
    for policy in (AccessPolicy(allowed_devices=("SERIAL_A",)), AccessPolicy(allowed_devices=("SERIAL_A", "SERIAL_C"))):
        payload = _payload(dispatch_tool_call("tap", {"x": 1, "y": 2, "device": "SERIAL_B"}, policy=policy))
        assert payload["error"] == "access_denied"
    assert fake.calls == []


def test_read_only_session_still_hides_and_denies_mutating_tools(adb):
    fake = adb(ONE_READY)
    policy = AccessPolicy(read_only=True, allowed_devices=("SERIAL_A",))
    assert _payload(dispatch_tool_call("tap", {"x": 1, "y": 2}, policy=policy))["error"] == "access_denied"
    assert not MUTATING_TOOLS & {t.name for t in get_mcp_tools(policy)}
    assert fake.calls == []


@pytest.mark.parametrize("name,args", [("device_info", {}), ("logcat_tail", {}), ("ui_summary", {}), ("screenshot", {})])
def test_read_only_tools_keep_the_sole_ready_device_convenience(name, args, adb, monkeypatch):
    fake = adb(ONE_READY)
    seen: list[str | None] = []
    real = tools.resolve_target_device

    def spy(adb_path, requested=None, **kw):
        seen.append(requested)
        return real(adb_path, requested, **kw)

    monkeypatch.setattr(tools, "resolve_target_device", spy)
    dispatch_tool_call(name, args)
    assert seen == [None]  # resolved without a name...
    assert any(call[1:3] == ["-s", "SERIAL_A"] for call in fake.actions)  # ...to the only ready device


def test_read_only_tools_still_need_device_when_several_are_ready(adb):
    adb(TWO_READY)
    payload = _payload(dispatch_tool_call("device_info", {}))
    assert payload["error"] == "need_device" and payload["failure"]["id"] == "device.multiple"
    assert "explicit" not in payload["message"]


def _tool(policy: AccessPolicy, name: str) -> Any:
    return next(t for t in get_mcp_tools(policy) if t.name == name).model_dump(by_alias=True)


@pytest.mark.parametrize("name", sorted(MUTATING_ARGS))
def test_advertised_schemas_show_the_explicit_device_requirement(name):
    unrestricted = _tool(AccessPolicy(), name)
    assert "device" in unrestricted["inputSchema"]["required"]
    assert "device" in unrestricted["inputSchema"]["properties"]
    assert "explicit" in unrestricted["description"]
    assert "explicit" in unrestricted["inputSchema"]["properties"]["device"]["description"] or "Required" in unrestricted["inputSchema"]["properties"]["device"]["description"]
    for required in MUTATING_ARGS[name]:
        assert required in unrestricted["inputSchema"]["required"]

    multi = _tool(AccessPolicy(allowed_devices=("A", "B")), name)
    assert "device" in multi["inputSchema"]["required"]
    assert multi["inputSchema"]["properties"]["device"]["enum"] == ["A", "B"]

    single = _tool(AccessPolicy(allowed_devices=("A",)), name)
    assert "device" not in single["inputSchema"]["required"]  # omission is valid here
    assert single["inputSchema"]["properties"]["device"]["enum"] == ["A"]
    assert "may be omitted" in single["description"]


def test_advertised_schemas_of_read_only_tools_are_unchanged():
    assert "required" not in _tool(AccessPolicy(), "device_info")["inputSchema"]
    assert "device" in _tool(AccessPolicy(allowed_devices=("A",)), "device_info")["inputSchema"]["required"]
    assert "may be omitted" not in _tool(AccessPolicy(allowed_devices=("A",)), "device_info")["description"]
    # The server-side validator keeps `device` optional so an omission reaches the tool and gets need_device.
    for name in MUTATING_ARGS:
        assert "device" not in mcp_server._TOOL_SCHEMAS[name].get("required", [])


def test_policy_helpers():
    assert AccessPolicy().default_device("tap") is None
    assert AccessPolicy(allowed_devices=("A",)).default_device("tap") == "A"
    assert AccessPolicy(allowed_devices=("A", "B")).default_device("tap") is None
    assert AccessPolicy(allowed_devices=("A",)).default_device("device_info") is None
    assert AccessPolicy(allowed_devices=("A", "B")).permits("tap", {"x": 1, "y": 2}) is True
    assert AccessPolicy(allowed_devices=("A", "B")).permits("tap", {"device": "C"}) is False
    assert AccessPolicy(allowed_devices=("A", "B")).permits("device_info", {}) is False  # unchanged


def test_need_device_over_the_official_client(adb):
    import anyio
    from mcp import Client

    fake = adb(ONE_READY)
    server = create_server()

    async def run() -> None:
        async with Client(server) as client:
            result = await client.call_tool("tap", {"x": 1, "y": 2})
            assert result.is_error and _payload(result)["error"] == "need_device"
            tools_listed = {t.name: t for t in (await client.list_tools()).tools}
            assert "device" in tools_listed["tap"].input_schema["required"]
            result = await client.call_tool("tap", {"x": 1, "y": 2, "device": "SERIAL_A"})
            assert not result.is_error

    anyio.run(run)
    assert len(fake.actions) == 1


def test_unsafe_server_still_wins_over_the_explicit_device_check(monkeypatch):
    monkeypatch.setattr(tools, "resolve_adb", lambda: Path("adb"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("adb was spawned"))

    def refuse(*a, **k):
        raise UnsafeAdbServerError("x")

    monkeypatch.setattr("adapt_adb.doctor.preflight_adb_server", refuse)
    assert _payload(dispatch_tool_call("tap", {"x": 1, "y": 2}))["error"] == "unsafe_shared_server"


# ---------------------------------------------------------------------------
# Item 2: the announced server version is the package version
# ---------------------------------------------------------------------------

def test_server_announces_the_package_version():
    import anyio
    from mcp import Client

    assert adapt_adb.__version__ != "0.1.0"

    async def run() -> None:
        async with Client(create_server()) as client:
            assert client.server_info.name == "adapt"
            assert client.server_info.version == adapt_adb.__version__

    anyio.run(run)


def test_legacy_server_constructor_also_announces_the_package_version(monkeypatch):
    from types import SimpleNamespace

    class LegacyServer:
        def __init__(self, name):
            self.name = name
            self.version = None

    _, types, _ = mcp_server._require_mcp_sdk()
    import anyio

    monkeypatch.setattr(mcp_server, "_require_mcp_sdk", lambda: (anyio, types, SimpleNamespace(Server=LegacyServer)))
    assert mcp_server.create_server().version == adapt_adb.__version__


# ---------------------------------------------------------------------------
# Item 3: refused adb commands and other exceptions
# ---------------------------------------------------------------------------

def _assert_refused(result: Any, secret: str = "DEVICE-TEXT-LEAK") -> dict[str, Any]:
    payload = _payload(result)
    assert result.is_error is True
    assert payload["error"] == "adb_command_refused"
    assert "safety rules refused" in payload["message"] and "before it ran" in payload["message"]
    assert payload["devices"] == []
    assert secret not in json.dumps(payload)
    assert payload["failure"]["id"] in GUIDES
    return payload


@pytest.mark.parametrize("raised", [ForbiddenArgv, tools.ForbiddenCommandError, SafetyError])
@pytest.mark.parametrize("name,args", [
    ("tap", {"x": 1, "y": 2, "device": "SERIAL_A"}),
    ("swipe", {"x1": 1, "y1": 2, "x2": 3, "y2": 4, "device": "SERIAL_A"}),
    ("key", {"key": "BACK", "device": "SERIAL_A"}),
    ("type", {"text": "hi", "device": "SERIAL_A"}),
    ("install_apk", {"apk": "a.apk", "device": "SERIAL_A"}),
    ("logcat_tail", {"device": "SERIAL_A"}),
    ("device_info", {"device": "SERIAL_A"}),
])
def test_guard_refusal_in_an_input_tool_is_a_specific_error(raised, name, args, adb, monkeypatch):
    fake = adb(ONE_READY)
    real = tools.validate_adb_args

    def guard(argv):
        if "devices" in argv:
            return real(argv)
        raise raised("DEVICE-TEXT-LEAK")

    monkeypatch.setattr(tools, "validate_adb_args", guard)
    _assert_refused(dispatch_tool_call(name, args))
    assert fake.actions == []


def test_real_guard_refusal_reaches_the_client_without_spawning(adb, monkeypatch):
    fake = adb(ONE_READY)
    monkeypatch.setattr(tools, "escape_adb_input_text", lambda text: "x; pm clear com.example")
    payload = _assert_refused(dispatch_tool_call("type", {"text": "hi", "device": "SERIAL_A"}), "com.example")
    assert "pm clear" not in json.dumps(payload) and fake.actions == []


@pytest.mark.parametrize("tool", ["screenshot", "ui_summary"])
@pytest.mark.parametrize("raised,code", [
    (tools.ForbiddenCommandError("DEVICE-TEXT-LEAK"), "adb_command_refused"),
    (UnsafeAdbServerError("DEVICE-TEXT-LEAK"), "unsafe_shared_server"),
])
def test_render_tools_report_refusals_with_their_specific_codes(tool, raised, code, adb, monkeypatch):
    adb(ONE_READY)

    def boom(*a, **k):
        raise raised

    monkeypatch.setattr(tools, "capture_screenshot_jpeg", boom)
    monkeypatch.setattr(tools, "capture_ui_summary", boom)
    payload = _payload(dispatch_tool_call(tool, {"device": "SERIAL_A"}))
    assert payload["error"] == code and "DEVICE-TEXT-LEAK" not in json.dumps(payload)


def test_development_tool_does_not_echo_a_refused_command(adb, monkeypatch):
    from adapt_adb import development

    adb(ONE_READY)

    def boom(**kwargs):
        raise ForbiddenArgv("DEVICE-TEXT-LEAK")

    monkeypatch.setattr(development, "validate_app", boom)
    _assert_refused(dispatch_tool_call("app_validate", {"device": "SERIAL_A", "package": "com.example.app"}))


@pytest.mark.parametrize("tool,patched", [
    ("dex_inspect", "tool_dex_inspect"), ("android_capabilities", "tool_android_capabilities"),
    ("developer_options_inspect", "tool_developer_options_inspect"),
])
def test_refusal_inside_inspection_tools_is_not_internal_error(tool, patched, monkeypatch):
    def boom(**kwargs):
        raise tools.ForbiddenCommandError("DEVICE-TEXT-LEAK")

    monkeypatch.setattr(tools, patched, boom)
    _assert_refused(dispatch_tool_call(tool, {"device": "SERIAL_A"}))


def test_adb_probe_error_in_the_doctor_tool_is_not_internal_error(monkeypatch):
    # `doctor` enumerates devices outside any handler; a failed `adb devices` used to be internal_error.
    def boom():
        raise AdbProbeError("device enumeration failed (exit 1) DEVICE-TEXT-LEAK")

    monkeypatch.setattr(tools, "tool_doctor", boom)
    payload = _payload(dispatch_tool_call("doctor", {}))
    assert payload["error"] == "adb_probe_failed" and payload["failure"]["id"] == "adb.enumeration_failed"
    assert "DEVICE-TEXT-LEAK" not in json.dumps(payload)


def test_doctor_enumeration_failure_through_the_real_tool_path(tmp_path, monkeypatch):
    from adapt_adb.doctor import Server5037State
    from tests.test_mcp_status_hardening import _make_base_doctor_env

    env = _make_base_doctor_env(tmp_path, Server5037State(listening=False))
    monkeypatch.setattr("adapt_adb.cli._ACTIVE_DOCTOR_ENV", env)
    monkeypatch.setattr("adapt_adb.doctor.preflight_adb_server", lambda *a, **k: None)

    def failed_listing(*a, **k):
        raise AdbProbeError("device enumeration failed (exit 1)")

    monkeypatch.setattr(tools, "list_connected_devices", failed_listing)
    payload = _payload(dispatch_tool_call("doctor", {}))
    assert payload["error"] == "adb_probe_failed"


def test_unknown_exceptions_remain_internal_error_without_their_text(monkeypatch):
    monkeypatch.setattr(tools, "tool_list_devices", lambda: (_ for _ in ()).throw(KeyError("DEVICE-TEXT-LEAK")))
    payload = _payload(dispatch_tool_call("list_devices", {}))
    assert payload["error"] == "internal_error" and "DEVICE-TEXT-LEAK" not in json.dumps(payload)


# ---------------------------------------------------------------------------
# Item 4: setup_status with a distribution-packaged adb
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path,version,installed,distribution", [
    ("/usr/bin/adb", "35.0.2", True, True),
    ("/bin/adb", "34.0.0", True, True),
    ("/usr/lib/android-sdk/platform-tools/adb", "34.0.4", True, True),
    ("/usr/bin/adb", "33.0.3", False, True),
    ("/usr/bin/adb", "unparseable-xyz", False, True),
    ("/opt/android/adb", "35.0.2", True, False),
    ("/opt/android/adb", "33.0.3", False, False),
])
def test_setup_status_distribution_rules(path, version, installed, distribution, monkeypatch):
    adb_path = Path(path)
    monkeypatch.setattr(tools, "resolve_adb", lambda: adb_path)
    monkeypatch.setattr(tools, "_extract_version_number", lambda p: version)
    monkeypatch.setenv("PATH", str(adb_path.parent))
    status = tools.tool_setup_status()
    assert status["installed"] is installed
    assert status["distribution_package"] is distribution
    assert status["would_download"] is (not installed)  # directory is on PATH


def test_setup_status_would_download_depends_on_the_adb_directory_being_on_path(monkeypatch):
    adb_path = Path("/usr/bin/adb")
    monkeypatch.setattr(tools, "resolve_adb", lambda: adb_path)
    monkeypatch.setattr(tools, "_extract_version_number", lambda p: "35.0.2")
    monkeypatch.setenv("PATH", "/somewhere/else")
    assert tools.tool_setup_status()["would_download"] is True
    monkeypatch.setenv("PATH", f"/somewhere/else:{adb_path.parent}".replace(":", __import__("os").pathsep))
    assert tools.tool_setup_status()["would_download"] is False


def test_setup_status_without_adb_is_not_installed_and_not_a_distribution_package(monkeypatch):
    monkeypatch.setattr(tools, "resolve_adb", lambda: None)
    status = tools.tool_setup_status()
    assert status["installed"] is False and status["distribution_package"] is False


# ---------------------------------------------------------------------------
# Item 5: the refusal wording promises no override that does not exist
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [
    ["uninstall", "com.example.app"],
    ["-s", "SERIAL_A", "uninstall", "com.example.app"],
    ["shell", "pm", "clear", "com.example.app"],
    ["shell", "pm", "uninstall", "com.example.app"],
    ["shell", "cmd", "package", "uninstall", "com.example.app"],
])
def test_destructive_package_refusal_names_no_nonexistent_flag(argv):
    with pytest.raises(ForbiddenArgv) as info:
        validate_adb_args(argv)
    message = str(info.value)
    assert "confirmation" not in message and "flag" not in message
    assert "plain adb outside ADAPT" in message and "does not run this operation" in message
    assert message.startswith("Forbidden command '")
    # The internal parameter keeps working for programmatic callers.
    assert validate_adb_args(argv, allow_destructive=True) == argv
    check_no_destructive_package_ops(argv, allow_destructive=True)


def test_safety_module_text_no_longer_promises_a_confirmation_flag():
    source = (ROOT / "src" / "adapt_adb" / "safety.py").read_text(encoding="utf-8")
    assert "confirmation flag" not in source


# ---------------------------------------------------------------------------
# Item 6: example configurations
# ---------------------------------------------------------------------------

def _example_args() -> dict[str, list[str]]:
    examples = ROOT / "examples"
    found: dict[str, list[str]] = {}
    for name in ("mcp.json", "claude.mcp.json"):
        found[name] = json.loads((examples / name).read_text(encoding="utf-8"))["mcpServers"]["adapt"]["args"]
    found["codex.toml"] = tomllib.loads((examples / "codex.toml").read_text(encoding="utf-8"))["mcp_servers"]["adapt"]["args"]
    return found


@pytest.mark.parametrize("name", ["mcp.json", "claude.mcp.json", "codex.toml"])
def test_every_example_names_a_device_placeholder(name):
    args = _example_args()[name]
    assert args[0] == "mcp" and "--read-only" in args
    assert args[args.index("--device") + 1] == "SERIAL"
    assert args.count("--device") == 1


def test_example_readme_explains_omission_and_the_published_install():
    text = (ROOT / "examples" / "README.md").read_text(encoding="utf-8")
    flat = " ".join(text.split())
    root_readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "no registry package" not in flat.lower() and "no registry package is assumed" not in flat
    assert "pipx" in text
    # The exact install forms of the root README are reused, not invented.
    for line in root_readme.splitlines():
        if line.startswith('pipx install "') and "adapt_adb-" in line and "[mcp]" in line:
            assert line in text
            break
    else:  # pragma: no cover
        pytest.fail("root README no longer shows the MCP install form")
    assert "need_device" in flat and "only ready device" in flat
    assert "checkout" in flat  # contributors



# ---------------------------------------------------------------------------
# Item 7: the documentation matches the behaviour
# ---------------------------------------------------------------------------

def test_docs_describe_the_contract_changes():
    access = (ROOT / "docs" / "mcp-access.md").read_text(encoding="utf-8")
    clients = (ROOT / "docs" / "clients.md").read_bytes().decode("utf-8").replace(chr(13) + chr(10), chr(10))
    for code in ("need_device", "unsafe_shared_server", "adb_command_refused", "internal_error"):
        assert f"`{code}`" in access and f"`{code}`" in clients
    for text in (access, clients):
        assert "distribution_package" in text and "daemon_reason" in text
        for value in ("root_owned", "foreign_owner", "unverified", "version_mismatch", "missing_adb"):
            assert value in text
        assert "pipx install" in text and "adapt_adb-" in text
        assert "--device SERIAL" in text
    assert "Explicit device for tools that act on the phone" in access
    assert "exactly one `--device`" in access and "exactly one `--device`" in clients
    assert "adapt_adb.__version__" in access
    assert "tests/test_mcp_fixes.py" in access
    for name in MUTATING_ARGS:
        assert f"`{name}`" in access
