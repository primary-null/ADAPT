"""The agent build-test loop tools (offline): schemas, access policy, argv shapes, bounds and lifecycles.

Nothing here touches a device or a real adb: `subprocess.run`/`Popen`, adb resolution and the
shared-server preflight are faked. These tests show the tools behave as designed against faked
adb output; they do not show the tools work on a phone.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp")

from adapt_adb import agent_guide, dev_options, safety
from adapt_adb.failure_guide import GUIDES
from adapt_adb.mcp import access, agent_tools, tools
from adapt_adb.mcp.access import AccessPolicy
from adapt_adb.mcp.server import dispatch_tool_call, get_mcp_tools
from tests.test_dev_options import FakeAdb as FakeDevice

NEW_TOOLS = [
    "app_launch", "app_stop", "app_state", "app_open_link", "log_mark", "test_run", "test_status",
    "dev_options_apply", "dev_options_restore", "permission_grant", "permission_revoke", "ui_tap_text", "ui_wait",
]
NEW_ACTING = [
    "app_launch", "app_stop", "app_open_link", "test_run", "dev_options_apply", "dev_options_restore",
    "permission_grant", "permission_revoke", "ui_tap_text",
]
NEW_OBSERVATION = ["app_state", "log_mark", "test_status", "ui_wait"]
PKG = "com.example.app"
TEST_PKG = "com.example.app.test"
RUNNER = "androidx.test.runner.AndroidJUnitRunner"

# Valid arguments for each acting tool, without `device`.
ACTING_ARGS: dict[str, dict[str, Any]] = {
    "app_launch": {"package": PKG},
    "app_stop": {"package": PKG},
    "app_open_link": {"uri": "https://example.com/a"},
    "test_run": {"test_package": TEST_PKG, "runner": RUNNER},
    "dev_options_apply": {"profile": "fast-ui"},
    "dev_options_restore": {},
    "permission_grant": {"package": PKG, "permission": "android.permission.CAMERA"},
    "permission_revoke": {"package": PKG, "permission": "android.permission.CAMERA"},
    "ui_tap_text": {"text": "OK"},
}

ONE_READY = (
    "List of devices attached\n"
    "SERIAL_A\tdevice usb:1-1 product:example model:Example_Phone device:example transport_id:1\n"
)
TWO_READY = ONE_READY + "SERIAL_B\tdevice usb:1-2 product:example_tab model:Example_Tablet device:example_tab transport_id:2\n"

AM_START_OK = (
    "Starting: Intent { cmp=com.example.app/.MainActivity }\nStatus: ok\nLaunchState: COLD\n"
    "Activity: com.example.app/.MainActivity\nTotalTime: 543\nWaitTime: 551\nComplete\n"
)
RESUMED = (
    "  topResumedActivity=ActivityRecord{1a2b3c u0 com.example.app/.MainActivity t12}\n"
    "  ResumedActivity: ActivityRecord{1a2b3c u0 com.example.app/.MainActivity t12}\n"
)
EXIT_INFO = (
    "ACTIVITY MANAGER PROCESS EXIT INFO (dumpsys activity exit-info)\n"
    "  Historical Process Exit for uid=10123\n"
    "    ApplicationExitInfo #0:\n"
    "      timestamp=2026-11-16 10:05:03.250 pid=4321 realUid=10123 packageUid=10123 definingUid=10123 user=0\n"
    "      process=com.example.app reason=4 (CRASH) subreason=0 (UNKNOWN) status=0\n"
    "      importance=100 pss=0.0MB rss=0.0MB description=crash state=empty trace=null\n"
    "    ApplicationExitInfo #1:\n"
    "      timestamp=2026-11-15 08:00:00.000 pid=111 realUid=10123 packageUid=10123 definingUid=10123 user=0\n"
    "      process=com.example.app reason=10 (USER_REQUESTED) subreason=0 (UNKNOWN) status=0\n"
    "      importance=100 pss=0.0MB rss=0.0MB description=force stop state=empty trace=null\n"
)


def ui_xml(*nodes: tuple[str, str, str, tuple[int, int, int, int]]) -> str:
    rows = "".join(
        f'<node index="{i}" text="{text}" resource-id="{rid}" class="android.widget.Button" content-desc="{desc}" '
        f'clickable="true" visible-to-user="true" bounds="[{b[0]},{b[1]}][{b[2]},{b[3]}]" />'
        for i, (text, rid, desc, b) in enumerate(nodes)
    )
    return f'<?xml version="1.0" encoding="UTF-8"?><hierarchy rotation="0">{rows}</hierarchy>'


OK_AND_CANCEL = ui_xml(("OK", "com.example.app:id/ok", "", (100, 200, 300, 260)), ("Cancel", "com.example.app:id/cancel", "", (400, 200, 600, 260)))
TWO_OKS = ui_xml(("OK", "com.example.app:id/ok1", "", (0, 0, 100, 100)), ("OK", "com.example.app:id/ok2", "", (200, 0, 300, 100)))


class FakeAdb:
    """Records every adb argv; answers device listings; other commands by the first matching rule."""

    def __init__(self, listing: str = ONE_READY) -> None:
        self.listing = listing
        self.calls: list[list[str]] = []
        self.rules: list[tuple[str, subprocess.CompletedProcess[str]]] = []
        self.sequences: dict[str, list[str]] = {}

    def on(self, prefix: str, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.rules.append((prefix, subprocess.CompletedProcess([], returncode, stdout, stderr)))

    @staticmethod
    def command(argv: list[str]) -> str:
        tokens = [str(a) for a in argv][1:]
        if tokens[:1] == ["-s"]:
            tokens = tokens[2:]
        return " ".join(tokens)

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append([str(a) for a in argv])
        if "devices" in argv:
            return subprocess.CompletedProcess(argv, 0, self.listing, "")
        text = self.command(argv)
        if text in self.sequences and self.sequences[text]:
            return subprocess.CompletedProcess(argv, 0, self.sequences[text].pop(0), "")
        for prefix, proc in reversed(self.rules):
            if prefix in text:
                return subprocess.CompletedProcess(argv, proc.returncode, proc.stdout, proc.stderr)
        return subprocess.CompletedProcess(argv, 0, "", "")

    @property
    def actions(self) -> list[list[str]]:
        return [c for c in self.calls if "devices" not in c]

    def commands(self) -> list[str]:
        return [self.command(c) for c in self.actions]


@pytest.fixture
def adb(monkeypatch: pytest.MonkeyPatch) -> FakeAdb:
    monkeypatch.setattr("adapt_adb.doctor.preflight_adb_server", lambda *a, **k: None)
    monkeypatch.setattr(tools, "resolve_adb", lambda: Path("/mock/adb"))
    fake = FakeAdb()
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


@pytest.fixture(autouse=True)
def _no_leftover_runs():
    yield
    agent_tools.shutdown_test_runs()
    with agent_tools._LOCK:
        agent_tools._RUNS.clear()


def call(name: str, args: dict[str, Any] | None = None, policy: AccessPolicy | None = None) -> tuple[Any, dict[str, Any]]:
    result = dispatch_tool_call(name, args or {}, policy=policy)
    return result, json.loads(result.content[-1].text)


def ok(name: str, args: dict[str, Any], policy: AccessPolicy | None = None) -> dict[str, Any]:
    result, payload = call(name, args, policy)
    assert not result.is_error, payload
    return payload


def err(name: str, args: dict[str, Any], policy: AccessPolicy | None = None) -> dict[str, Any]:
    result, payload = call(name, args, policy)
    assert result.is_error, payload
    return payload


# --- schemas, classification, annotations ------------------------------------------------------


def test_new_tools_are_registered_in_order_after_the_existing_ones():
    names = [s["name"] for s in tools.TOOL_SCHEMAS]
    assert names[24:] == NEW_TOOLS and len(names) == 37
    assert len(set(names)) == 37


def test_classification_counts_and_membership():
    assert set(NEW_ACTING) <= access.MUTATING_TOOLS and not set(NEW_ACTING) & access.OBSERVATION_TOOLS
    assert set(NEW_OBSERVATION) <= access.OBSERVATION_TOOLS and not set(NEW_OBSERVATION) & access.MUTATING_TOOLS
    assert (len(access.MUTATING_TOOLS), len(access.OBSERVATION_TOOLS)) == (14, 23)
    assert access.MUTATING_TOOLS | access.OBSERVATION_TOOLS == {s["name"] for s in tools.TOOL_SCHEMAS}
    # Every tool that takes a device is device-bound; test_status names a run instead.
    for name in NEW_TOOLS:
        has_device = "device" in next(s for s in tools.TOOL_SCHEMAS if s["name"] == name)["inputSchema"]["properties"]
        assert (name in access.DEVICE_TOOLS) is has_device, name
    assert "test_status" not in access.DEVICE_TOOLS


def test_exposed_counts_per_session_policy():
    names = [s["name"] for s in tools.TOOL_SCHEMAS]
    counts = {label: sum(1 for n in names if p.exposes(n)) for label, p in {
        "full": AccessPolicy(), "read-only": AccessPolicy(read_only=True), "device": AccessPolicy(allowed_devices=("A",)),
        "read-only device": AccessPolicy(read_only=True, allowed_devices=("A",))}.items()}
    assert counts == {"full": 37, "read-only": 23, "device": 36, "read-only device": 22}


def test_annotations_of_the_new_tools():
    by_name = {t.name: t.annotations for t in get_mcp_tools()}
    for name in NEW_OBSERVATION:
        hints = by_name[name]
        assert hints.read_only_hint is True and hints.destructive_hint is False and hints.idempotent_hint is True
    for name in NEW_ACTING:
        assert by_name[name].read_only_hint is False and by_name[name].destructive_hint is True
    open_world = {n for n, h in by_name.items() if h.open_world_hint}
    assert {"ui_tap_text", "ui_wait", "app_open_link", "app_state", "test_run", "test_status"} <= open_world
    assert not open_world & {"app_launch", "app_stop", "log_mark", "dev_options_apply", "dev_options_restore", "permission_grant", "permission_revoke"}
    idempotent = {n for n in NEW_ACTING if by_name[n].idempotent_hint}
    assert idempotent == {"app_stop", "dev_options_apply", "dev_options_restore", "permission_grant", "permission_revoke"}
    for name in ("app_launch", "app_open_link", "test_run", "ui_tap_text"):
        assert by_name[name].idempotent_hint is False


def test_titles_are_unique_and_descriptions_lead_with_purpose():
    advertised = {t.name: t for t in get_mcp_tools()}
    titles = [t.title for t in advertised.values()]
    assert len(set(titles)) == len(titles)
    for name in NEW_TOOLS:
        description = advertised[name].description
        assert not description.startswith("Execute"), name
        assert len(description) < 900
    for name in NEW_ACTING:
        assert "explicitly" in advertised[name].description  # the shared "acts on the phone" note


def test_acting_tools_require_device_unless_the_session_names_exactly_one():
    full = {t.name: t.model_dump(by_alias=True)["inputSchema"] for t in get_mcp_tools()}
    single = {t.name: t.model_dump(by_alias=True)["inputSchema"] for t in get_mcp_tools(AccessPolicy(allowed_devices=("A",)))}
    two = {t.name: t.model_dump(by_alias=True)["inputSchema"] for t in get_mcp_tools(AccessPolicy(allowed_devices=("A", "B")))}
    for name in NEW_ACTING:
        assert "device" in full[name]["required"] and "device" in two[name]["required"], name
        assert "device" not in single[name].get("required", []), name
        assert single[name]["properties"]["device"]["enum"] == ["A"]
    for name in ("app_state", "log_mark", "ui_wait"):
        assert "device" in single[name]["required"]


def test_schemas_are_strict_and_snapshot_path_is_not_an_argument():
    for name in NEW_TOOLS:
        schema = next(s for s in tools.TOOL_SCHEMAS if s["name"] == name)["inputSchema"]
        assert schema["additionalProperties"] is False
    props = next(s for s in tools.TOOL_SCHEMAS if s["name"] == "dev_options_apply")["inputSchema"]["properties"]
    assert set(props) == {"device", "profile"} and props["profile"]["enum"] == ["fast-ui", "touches", "desktop-testing", "stay-awake"]
    result, payload = call("dev_options_apply", {"device": "SERIAL_A", "profile": "fast-ui", "snapshot": "/tmp/x.json"})
    assert result.is_error and payload["error"] == "invalid_params"


def test_since_is_on_both_log_tools_and_documented():
    for name in ("app_logs", "logcat_tail"):
        schema = next(s for s in tools.TOOL_SCHEMAS if s["name"] == name)
        assert "since" in schema["inputSchema"]["properties"] and "log_mark" in schema["description"]
        assert "since" not in schema["inputSchema"].get("required", [])


# --- access policy ----------------------------------------------------------------------------


@pytest.mark.parametrize("name", NEW_ACTING)
def test_read_only_hides_and_refuses_acting_tools_before_any_adb_call(name, adb):
    policy = AccessPolicy(read_only=True)
    assert name not in {t.name for t in get_mcp_tools(policy)}
    payload = err(name, {**ACTING_ARGS[name], "device": "SERIAL_A"}, policy)
    assert payload["error"] == "access_denied" and adb.calls == []


@pytest.mark.parametrize("name", NEW_OBSERVATION)
def test_read_only_keeps_the_new_observation_tools(name):
    assert name in {t.name for t in get_mcp_tools(AccessPolicy(read_only=True))}


@pytest.mark.parametrize("listing", [ONE_READY, TWO_READY], ids=["one-ready", "two-ready"])
@pytest.mark.parametrize("name", NEW_ACTING)
def test_acting_tools_without_a_device_answer_need_device_and_do_nothing(name, listing, adb):
    adb.listing = listing
    payload = err(name, dict(ACTING_ARGS[name]))
    assert payload["error"] == "need_device" and "explicit `device`" in payload["message"]
    assert payload["failure"]["retry"] == "with_changed_arguments"
    assert adb.actions == []  # only the device listing ran


@pytest.mark.parametrize("name", NEW_ACTING)
def test_device_restriction_denies_another_target_and_a_one_device_session_supplies_its_own(name, adb, monkeypatch):
    policy = AccessPolicy(allowed_devices=("SERIAL_A",))
    assert err(name, {**ACTING_ARGS[name], "device": "SERIAL_B"}, policy)["error"] == "access_denied"
    assert adb.calls == []
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(agent_tools, "call_tool", lambda n, a, **k: seen.append(a) or {"ok": True})
    ok(name, dict(ACTING_ARGS[name]), policy)  # `device` omitted: the single --device is the explicit choice
    assert seen[0]["device"] == "SERIAL_A"


def test_two_device_session_does_not_default_for_acting_tools(adb):
    policy = AccessPolicy(allowed_devices=("SERIAL_A", "SERIAL_B"))
    assert policy.default_device("app_launch") is None
    payload = err("app_launch", {"package": PKG}, policy)
    assert payload["error"] == "need_device"


@pytest.mark.parametrize("name,args", [("app_state", {"package": PKG}), ("log_mark", {}), ("ui_wait", {"text": "OK"})])
def test_device_restriction_binds_the_new_observation_tools(name, args, adb):
    policy = AccessPolicy(allowed_devices=("SERIAL_A",))
    assert err(name, {**args, "device": "SERIAL_B"}, policy)["error"] == "access_denied"
    assert err(name, args, policy)["error"] in {"access_denied", "invalid_params"}
    assert adb.calls == []


# --- allowlisted argv shapes and validators -------------------------------------------------------


SHAPES = [
    (["shell", "cmd", "package", "resolve-activity", "--brief", "-a", "android.intent.action.MAIN", "-c", "android.intent.category.LAUNCHER", PKG], "launcher_resolve"),
    (["shell", "am", "start", "-W", "-n", f"{PKG}/.MainActivity"], "app_start_wait"),
    (["shell", "am", "start", "-n", f"{PKG}/{PKG}.MainActivity"], "app_start"),
    (["shell", "am", "force-stop", PKG], "app_force_stop"),
    (["shell", "pidof", PKG], "app_pid"),
    (["shell", "dumpsys", "activity", "activities"], "activity_resumed"),
    (["shell", "dumpsys", "activity", "exit-info", PKG], "activity_exit_info"),
    (["shell", "getprop", "ro.build.version.sdk"], "device_sdk"),
    (["shell", "date", "+%m-%d_%H:%M:%S.%N"], "device_time"),
    (["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", "'https://example.com/a?b=1&c=2'"], "open_link"),
    (["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", "'myapp://item/42'", "-p", PKG], "open_link_package"),
    (["shell", "am", "instrument", "-w", "-r", f"{TEST_PKG}/{RUNNER}"], "instrument"),
    (["shell", "am", "instrument", "-w", "-r", "-e", "class", f"{PKG}.LoginTest", f"{TEST_PKG}/{RUNNER}"], "instrument_class"),
    (["shell", "am", "instrument", "-w", "-r", "-e", "class", f"{PKG}.LoginTest#testOk", f"{TEST_PKG}/{RUNNER}"], "instrument_method"),
    (["shell", "pm", "grant", PKG, "android.permission.CAMERA"], "permission_grant"),
    (["shell", "pm", "revoke", PKG, "android.permission.CAMERA"], "permission_revoke"),
    (["shell", "input", "tap", "100", "200"], "input_tap"),
]


@pytest.mark.parametrize("argv,name", SHAPES)
def test_each_shape_matches_only_itself_with_or_without_the_serial_prefix(argv, name):
    assert safety.match_agent_shell_shape(argv) == name
    assert safety.match_agent_shell_shape(["-s", "SERIAL_A", *argv]) == name
    assert safety.match_agent_shell_shape([*argv, "extra"]) is None
    assert safety.match_agent_shell_shape(argv[:-1]) is None
    safety.validate_adb_args(argv)  # and the general guard accepts every shape too


@pytest.mark.parametrize("argv", [
    ["shell", "am", "force-stop", "com.example.app; reboot"],
    ["shell", "am", "force-stop", "-a"],
    ["shell", "am", "force-stop", "com.example.app", "--user", "0"],
    ["shell", "pm", "clear", PKG],
    ["shell", "pm", "grant", PKG, "android.permission.CAMERA; id"],
    ["shell", "pm", "grant", PKG, "-x"],
    ["shell", "am", "start", "-n", "com.example.app/.Main;id"],
    ["shell", "am", "start", "-n", "com.example.app/$Inner"],
    ["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", "https://example.com/a&b"],  # unquoted URI
    ["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", "'https://example.com/it's'"],
    ["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", "'x y:z'"],
    ["shell", "am", "instrument", "-w", "-r", "-e", "class", "-bad", f"{TEST_PKG}/{RUNNER}"],
    ["shell", "am", "instrument", "-w", "-r", "-e", "size", "large", f"{TEST_PKG}/{RUNNER}"],
    ["shell", "sh", "-c", "pidof com.example.app"],
    ["shell", "su", "0", "pidof", PKG],
    ["shell", "pidof", "--", PKG],
    ["shell", "input", "text", "hello"],
    ["shell", "date", "+%s"],
    ["uninstall", PKG],
    ["shell", "dumpsys", "activity"],
])
def test_unlisted_or_hostile_argv_matches_no_shape(argv):
    assert safety.match_agent_shell_shape(argv) is None


@pytest.mark.parametrize("value,expected", [
    (PKG, True), ("a.b", True), ("Com.Example.App2", True), ("com.example", True),
    ("app", False), ("com.example.app/.Main", False), ("-com.example", False), ("com.example..app", False), ("1com.example", False),
    ("com.example.app;id", False), ("com example", False), ("com.exa$mple", False), ("", False), (None, False), ("a." * 200 + "b", False),
])
def test_package_pattern(value, expected):
    assert safety.is_package_name(value) is expected


@pytest.mark.parametrize("uri,expected", [
    ("https://example.com/a?b=1&c=2#frag", True), ("myapp://item/42", True), ("mailto:someone@example.com", True), ("tel:+15550100", True),
    ("https://example.com/%E2%9C%93", True), ("custom+scheme.1-x://h", True),
    ("javascript:alert(1)", False), ("JavaScript:alert(1)", False), ("data:text/html;base64,AA", False), ("intent://x#Intent;end", False),
    ("vbscript:x", False), ("no-scheme", False), ("://nohost", False), ("https:", False), ("1http://x", False),
    ("https://example.com/a b", False), ("https://example.com/a\tb", False), ("https://example.com/a\nb", False), ("https://example.com/\x00", False),
    ("https://example.com/'", False), ('https://example.com/"', False), ("https://example.com/`id`", False), ("https://example.com/$(id)", False),
    ("https://example.com/\\x", False), ("https://example.com/<x>", False), ("https://example.com/a|b", False), ("https://exämple.com", False),
    ("https://example.com/" + "a" * 1980, True), ("https://example.com/" + "a" * 1981, False), ("", False), (None, False),
])
def test_uri_validation(uri, expected):
    assert safety.is_safe_uri(uri) is expected


@pytest.mark.parametrize("permission,expected", [
    ("android.permission.CAMERA", True), ("android.permission.POST_NOTIFICATIONS", True), ("android.permission.READ_MEDIA_IMAGES", True),
    ("android.permission.health.READ_STEPS", True), (f"{PKG}.permission.SCAN", True),
    ("android.permission.SYSTEM_ALERT_WINDOW", False), ("android.permission.WRITE_SECURE_SETTINGS", False),
    ("android.permission.MANAGE_EXTERNAL_STORAGE", False), ("android.permission.INSTALL_PACKAGES", False),
    ("com.other.app.permission.SCAN", False), ("android.permission.CAMERA ", False), ("android.permission.CAMERA;id", False),
    ("CAMERA", False), ("", False), (None, False),
])
def test_permission_names_are_runtime_only(permission, expected):
    assert safety.is_runtime_permission(permission, PKG) is expected


@pytest.mark.parametrize("mark,expected", [
    ("11-16 10:05:03.250", True), ("1-16 10:05:03.250", False), ("11-16 10:05:03", False), ("11-16 10:05:03.250 ", False),
    ("11-16T10:05:03.250", False), ("-T 100", False), ("11-16 10:05:03.250\n", False),
])
def test_log_mark_pattern(mark, expected):
    assert safety.is_log_mark(mark) is expected


def test_logcat_since_shape_requires_a_real_mark_and_a_bounded_read():
    assert safety.match_agent_logcat(["logcat", "-d", "-T", "11-16 10:05:03.250"])
    assert safety.match_agent_logcat(["-s", "S", "logcat", "-d", "-T", "11-16 10:05:03.250", "-s", "Tag:W"])
    assert not safety.match_agent_logcat(["logcat", "-d", "-T", "100; id"])
    assert not safety.match_agent_logcat(["logcat", "-T", "11-16 10:05:03.250"])  # no -d


@pytest.mark.parametrize("name,args", [
    ("app_launch", {"package": "bad;id"}), ("app_stop", {"package": "com.example.app/.Main"}), ("app_state", {"device": "SERIAL_A", "package": "badpackage"}),
    ("permission_grant", {"package": "a b.c", "permission": "android.permission.CAMERA"}),
])
def test_bad_packages_are_refused_before_adb_runs(name, args, adb):
    payload = err(name, {"device": "SERIAL_A", **args})
    assert payload["error"] == "invalid_package" and payload["failure"]["id"] == "app.package_invalid"
    assert payload["failure"]["retry"] == "with_changed_arguments"
    assert adb.calls == []


# --- app_launch / app_stop / app_state ------------------------------------------------------------


def test_app_launch_resolves_the_launcher_and_reports_launch_time_and_resumed_activity(adb):
    adb.on("cmd package resolve-activity", stdout=f"priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=true\n{PKG}/.MainActivity\n")
    adb.on("am start", stdout=AM_START_OK)
    adb.on("dumpsys activity activities", stdout=RESUMED)
    payload = ok("app_launch", {"device": "SERIAL_A", "package": PKG})
    assert payload["component"] == f"{PKG}/.MainActivity" and payload["launch_state"] == "COLD"
    assert payload["total_time_ms"] == 543 and payload["wait_time_ms"] == 551 and payload["waited"] is True
    assert payload["resumed_activity"] == {"package": PKG, "activity": ".MainActivity"} and payload["foreground"] is True
    assert payload["content_trust"] == "untrusted_device_data"
    assert adb.commands() == [
        f"shell cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.LAUNCHER {PKG}",
        f"shell am start -W -n {PKG}/.MainActivity",
        "shell dumpsys activity activities",
    ]
    assert all(c[1:3] == ["-s", "SERIAL_A"] for c in adb.actions)


def test_app_launch_with_an_activity_skips_the_lookup_and_normalises_the_class(adb):
    adb.on("am start", stdout=AM_START_OK.replace("/.MainActivity", f"/{PKG}.Settings"))
    ok("app_launch", {"device": "SERIAL_A", "package": PKG, "activity": f"{PKG}.Settings", "wait": False})
    ok("app_launch", {"device": "SERIAL_A", "package": PKG, "activity": f"{PKG}/.Other"})
    starts = [c for c in adb.commands() if c.startswith("shell am start")]
    assert starts == [f"shell am start -n {PKG}/{PKG}.Settings", f"shell am start -W -n {PKG}/.Other"]
    assert not any("resolve-activity" in c for c in adb.commands())


@pytest.mark.parametrize("activity", ["Main;id", "-n", "$Inner", "other.app/.Main", "a b", "Foo$Bar"])
def test_app_launch_refuses_unsafe_activity_names(activity, adb):
    payload = err("app_launch", {"device": "SERIAL_A", "package": PKG, "activity": activity})
    assert payload["error"] == "invalid_params" and adb.calls == []


def test_app_launch_without_a_launcher_names_the_next_step(adb):
    adb.on("cmd package resolve-activity", stdout="No activity found\n")
    payload = err("app_launch", {"device": "SERIAL_A", "package": PKG})
    assert payload["error"] == "no_launcher_activity" and payload["failure"]["id"] == "app.no_launcher_activity"
    assert payload["failure"]["next_tool"]["name"] == "app_validate" and payload["failure"]["retry"] == "with_changed_arguments"
    assert not any("am start" in c for c in adb.commands())


@pytest.mark.parametrize("text,reason", [
    ("Error type 3\nError: Activity class {com.example.app/com.example.app.Gone} does not exist.\n", "activity_or_package_not_found"),
    ("Starting: Intent { cmp=x }\nSecurityException: Permission Denial: not exported\n", "not_exported_or_denied"),
    ("Status: timeout\n", "launch_timeout"),
])
def test_app_launch_failures_are_classified(text, reason, adb):
    adb.on("am start", stdout=text)
    payload = err("app_launch", {"device": "SERIAL_A", "package": PKG, "activity": ".Main"})
    assert payload["error"] == "app_launch_failed" and payload["reason"] == reason
    assert payload["failure"]["id"] == "app.launch_failed" and payload["failure"]["next_tool"]["name"] == "app_validate"


def test_app_stop_forces_stop_and_checks_the_process_is_gone(adb):
    adb.on("pidof", returncode=1)
    payload = ok("app_stop", {"device": "SERIAL_A", "package": PKG})
    assert payload["still_running"] is False
    assert adb.commands() == [f"shell am force-stop {PKG}", f"shell pidof {PKG}"]
    adb.on("pidof", stdout="4321\n")
    assert ok("app_stop", {"device": "SERIAL_A", "package": PKG})["still_running"] is True


def test_app_state_answers_running_foreground_and_crash_without_logs(adb):
    adb.on("pidof", stdout="")
    adb.on("pidof", returncode=1)
    adb.on("dumpsys activity activities", stdout=RESUMED)
    adb.on("getprop ro.build.version.sdk", stdout="34\n")
    adb.on("dumpsys activity exit-info", stdout=EXIT_INFO)
    adb.on("date", stdout="11-16_10:06:00.123456789\n")
    payload = ok("app_state", {"device": "SERIAL_A", "package": PKG})
    assert payload["running"] is False and payload["pids"] == [] and payload["foreground"] is True
    assert payload["last_exit"] == {"at": "11-16 10:05:03.250", "pid": 4321, "reason": "CRASH", "crash": True, "anr": False, "description": "crash"}
    assert payload["device_time"] == "11-16 10:06:00.123" and payload["api_level"] == 34
    assert payload["sources"]["exit_info"] == "observed" and payload["content_trust"] == "untrusted_device_data"
    adb.on("pidof", stdout="4321 4400\n")
    assert ok("app_state", {"device": "SERIAL_A", "package": PKG})["pids"] == [4321, 4400]


def test_app_state_exit_info_needs_api_30(adb):
    adb.on("getprop ro.build.version.sdk", stdout="29\n")
    adb.on("pidof", stdout="9\n")
    payload = ok("app_state", {"device": "SERIAL_A", "package": PKG})
    assert payload["last_exit"] is None and payload["sources"]["exit_info"] == "needs_api_30" and payload["running"] is True
    assert not any("exit-info" in c for c in adb.commands())


def test_exit_info_parser_picks_the_latest_record_and_cleans_text():
    info = agent_tools.parse_exit_info(EXIT_INFO, PKG)
    assert info["reason"] == "CRASH" and info["crash"] and info["at"] == "11-16 10:05:03.250"
    assert agent_tools.parse_exit_info(EXIT_INFO, "com.other.app") is None
    anr = EXIT_INFO.replace("reason=4 (CRASH)", "reason=6 (ANR)").replace("description=crash", "description=\x1b[31mbad\x07 text")
    parsed = agent_tools.parse_exit_info(anr, PKG)
    assert parsed["anr"] and not parsed["crash"] and "\x1b" not in parsed["description"] and "\x07" not in parsed["description"]
    assert agent_tools.parse_exit_info("", PKG) is None


# --- log_mark and since --------------------------------------------------------------------------


def test_log_mark_returns_device_time_with_precision(adb):
    adb.on("date", stdout="11-16_10:06:00.123456789\n")
    assert ok("log_mark", {"device": "SERIAL_A"})["mark"] == "11-16 10:06:00.123"
    adb.on("date", stdout="11-16_10:06:07.%N\n")
    payload = ok("log_mark", {"device": "SERIAL_A"})
    assert payload["mark"] == "11-16 10:06:07.000" and payload["precision"] == "s"
    assert adb.commands()[0] == "shell date +%m-%d_%H:%M:%S.%N"
    adb.on("date", stdout="garbage\n")
    assert err("log_mark", {"device": "SERIAL_A"})["error"] == "adb_error"


def test_logcat_tail_since_reads_a_window_without_clearing(adb):
    adb.on("logcat", stdout="--------- beginning of main\n11-16 10:06:01.000 I/T(1): a\n11-16 10:06:02.000 I/T(1): b\n")
    payload = ok("logcat_tail", {"device": "SERIAL_A", "since": "11-16 10:06:00.123", "lines": 1, "level": "W"})
    assert payload["lines"] == ["11-16 10:06:02.000 I/T(1): b"]
    command = adb.commands()[-1]
    assert command == "logcat -d -T 11-16 10:06:00.123 *:W" and " -c" not in command and "-t " not in command
    assert adb.actions[-1][3:] == ["logcat", "-d", "-T", "11-16 10:06:00.123", "*:W"]


@pytest.mark.parametrize("since", ["100", "11-16 10:06:00", "now", "11-16 10:06:00.123; id", "x" * 18])
def test_logcat_tail_since_rejects_anything_but_a_log_mark(since, adb):
    result, payload = call("logcat_tail", {"device": "SERIAL_A", "since": since})
    assert result.is_error and payload["error"] == "invalid_params" and adb.calls == []


def test_app_logs_since_uses_minus_T_and_keeps_the_uid_filter(monkeypatch):
    from adapt_adb import development as dev
    from tests.test_development import FakeSession, PACKAGE

    fake = FakeSession("exact target")
    monkeypatch.setattr(dev, "Session", lambda serial: fake)
    report = dev.capture_logs("exact target", PACKAGE, lines=20, since="11-16 10:06:00.123")
    log_args = next(a for a in fake.calls if a[0] == "logcat")
    assert log_args[:4] == ["logcat", "-d", "-T", "11-16 10:06:00.123"] and "-t" not in log_args
    assert "--uid=1010123" in log_args and "-c" not in log_args and report["since"] == "11-16 10:06:00.123"
    with pytest.raises(ValueError):
        dev.capture_logs("exact target", PACKAGE, since="11-16")


def test_app_logs_rejects_a_bad_since_through_the_tool(monkeypatch):
    from adapt_adb import development as dev

    monkeypatch.setattr(dev, "Session", lambda serial: pytest.fail("device touched"))
    payload = err("app_logs", {"device": "SERIAL_A", "package": PKG, "since": "11-16 10:06:00.12;"})
    assert payload["error"] == "invalid_params"  # the schema pattern length stops it; the tool also validates


# --- app_open_link ---------------------------------------------------------------------------------


def test_app_open_link_quotes_the_uri_and_can_target_a_package(adb):
    payload = ok("app_open_link", {"device": "SERIAL_A", "uri": "https://example.com/a?b=1&c=2#x", "package": PKG})
    assert payload["uri"] == "https://example.com/a?b=1&c=2#x"
    assert adb.actions[-1][3:] == ["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", "'https://example.com/a?b=1&c=2#x'", "-p", PKG]
    ok("app_open_link", {"device": "SERIAL_A", "uri": "myapp://item/42"})
    assert adb.actions[-1][-1] == "'myapp://item/42'"


@pytest.mark.parametrize("uri", ["javascript:alert(1)", "https://example.com/a b", "https://example.com/\x07", "a:b'c", "x" * 2001, "no-scheme", "intent://x#Intent;end"])
def test_app_open_link_refuses_invalid_uris_without_touching_the_device(uri, adb):
    payload = err("app_open_link", {"device": "SERIAL_A", "uri": uri})
    assert payload["error"] in {"invalid_uri", "invalid_params"} and adb.calls == []
    if payload["error"] == "invalid_uri":
        assert payload["failure"]["id"] == "app.link_invalid"


def test_app_open_link_reports_an_unhandled_link(adb):
    adb.on("am start", stdout="Starting: Intent { act=android.intent.action.VIEW }\nError: Activity not started, unable to resolve Intent\n")
    payload = err("app_open_link", {"device": "SERIAL_A", "uri": "myapp://nobody"})
    assert payload["error"] == "link_not_handled" and payload["failure"]["id"] == "app.link_unhandled"


# --- permissions -------------------------------------------------------------------------------------


def test_permission_grant_and_revoke_use_pm_with_runtime_permissions_only(adb):
    ok("permission_grant", {"device": "SERIAL_A", "package": PKG, "permission": "android.permission.CAMERA"})
    ok("permission_revoke", {"device": "SERIAL_A", "package": PKG, "permission": f"{PKG}.permission.SCAN"})
    assert adb.commands() == [f"shell pm grant {PKG} android.permission.CAMERA", f"shell pm revoke {PKG} {PKG}.permission.SCAN"]
    n = len(adb.calls)
    payload = err("permission_grant", {"device": "SERIAL_A", "package": PKG, "permission": "android.permission.SYSTEM_ALERT_WINDOW"})
    assert payload["error"] == "invalid_permission" and payload["failure"]["id"] == "app.permission_refused" and len(adb.calls) == n


def test_permission_refusal_by_android_is_classified(adb):
    adb.on("pm grant", stderr="Exception occurred while executing 'grant':\njava.lang.SecurityException: Package com.example.app has not requested permission android.permission.CAMERA\n", returncode=1)
    payload = err("permission_grant", {"device": "SERIAL_A", "package": PKG, "permission": "android.permission.CAMERA"})
    assert payload["error"] == "permission_failed" and payload["reason"] == "not_requested_by_app"
    assert payload["failure"]["next_tool"]["name"] == "app_validate"


# --- ui_tap_text and ui_wait ----------------------------------------------------------------------


def test_ui_tap_text_takes_a_fresh_dump_then_taps_the_centre(adb):
    adb.on("uiautomator dump /dev/tty", stdout=OK_AND_CANCEL)
    adb.on("uiautomator dump", stdout=OK_AND_CANCEL)
    payload = ok("ui_tap_text", {"device": "SERIAL_A", "text": "Cancel"})
    assert payload["tapped"] == [500, 230] and payload["match_count"] == 1 and payload["matched"]["resource_id"] == "com.example.app:id/cancel"
    dump_calls = [c for c in adb.actions if "uiautomator" in c]
    assert dump_calls and adb.actions[-1][3:] == ["shell", "input", "tap", "500", "230"]
    assert adb.actions.index(dump_calls[0]) < adb.actions.index(adb.actions[-1])
    ok("ui_tap_text", {"device": "SERIAL_A", "resource_id": "com.example.app:id/ok"})
    assert adb.actions[-1][3:] == ["shell", "input", "tap", "200", "230"]
    assert len([c for c in adb.actions if "uiautomator" in c]) == 2  # a new dump per call, never a cached one


def test_ui_tap_text_fails_clearly_on_zero_matches_and_lists_labels(adb):
    adb.on("uiautomator", stdout=OK_AND_CANCEL)
    payload = err("ui_tap_text", {"device": "SERIAL_A", "text": "Missing"})
    assert payload["error"] == "ui_no_match" and payload["available"] == ["OK", "Cancel"]
    assert payload["failure"]["id"] == "app.ui_no_match" and payload["failure"]["next_tool"]["name"] == "ui_summary"
    assert payload["content_trust"] == "untrusted_device_data"
    assert not any("input tap" in c for c in adb.commands())


def test_ui_tap_text_needs_an_index_when_several_match(adb):
    adb.on("uiautomator", stdout=TWO_OKS)
    payload = err("ui_tap_text", {"device": "SERIAL_A", "text": "OK"})
    assert payload["error"] == "ui_ambiguous" and payload["match_count"] == 2 and len(payload["matches"]) == 2
    assert payload["failure"]["id"] == "app.ui_ambiguous" and "index" in payload["failure"]["argument_hint"]
    assert not any("input tap" in c for c in adb.commands())
    assert ok("ui_tap_text", {"device": "SERIAL_A", "text": "OK", "index": 1})["tapped"] == [250, 50]
    assert err("ui_tap_text", {"device": "SERIAL_A", "text": "OK", "index": 2})["error"] == "ui_no_match"


@pytest.mark.parametrize("args", [{}, {"text": "OK", "resource_id": "com.example.app:id/ok"}, {"resource_id": "bad id"}, {"text": "a\x07b"}])
def test_ui_selectors_are_validated_before_any_adb_call(args, adb):
    for name in ("ui_tap_text", "ui_wait"):
        result, payload = call(name, {"device": "SERIAL_A", **args})
        assert result.is_error and payload["error"] == "invalid_params"
    assert adb.calls == []


def test_ui_tap_text_dump_failure_is_reported_not_tapped(adb):
    adb.on("uiautomator", stdout="", returncode=1)
    payload = err("ui_tap_text", {"device": "SERIAL_A", "text": "OK"})
    assert payload["error"] == "ui_dump_failed" and payload["failure"]["id"] == "app.ui_dump_failed"
    assert not any("input tap" in c for c in adb.commands())


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(agent_tools, "_MONOTONIC", c.monotonic)
    monkeypatch.setattr(agent_tools, "_SLEEP", c.sleep)
    return c


def test_ui_wait_polls_until_the_element_appears(adb, clock):
    adb.sequences["exec-out uiautomator dump /dev/tty"] = [ui_xml(), ui_xml(), OK_AND_CANCEL]
    payload = ok("ui_wait", {"device": "SERIAL_A", "text": "OK", "timeout_s": 10})
    assert payload["met"] is True and payload["timed_out"] is False and payload["polls"] == 3 and payload["first_match"]["text"] == "OK"
    assert payload["content_trust"] == "untrusted_device_data"


def test_ui_wait_gone_and_timeout_is_a_result_with_a_hard_cap(adb, clock):
    adb.on("uiautomator", stdout=OK_AND_CANCEL)
    payload = ok("ui_wait", {"device": "SERIAL_A", "text": "OK", "gone": True, "timeout_s": 5})
    assert payload["met"] is False and payload["timed_out"] is True and payload["condition"] == "gone"
    assert payload["polls"] <= 6 and payload["waited_s"] <= 5 + 1
    result, bad = call("ui_wait", {"device": "SERIAL_A", "text": "OK", "timeout_s": 56})
    assert result.is_error and bad["error"] == "invalid_params"
    adb.sequences["exec-out uiautomator dump /dev/tty"] = [OK_AND_CANCEL, ui_xml()]
    assert ok("ui_wait", {"device": "SERIAL_A", "text": "OK", "gone": True, "timeout_s": 5})["met"] is True


def test_ui_wait_clips_each_dump_to_the_remaining_time(adb, clock, monkeypatch):
    seen: list[Any] = []

    def run(argv, **kwargs):
        if "devices" in argv:
            return subprocess.CompletedProcess(argv, 0, ONE_READY, "")
        seen.append(kwargs.get("timeout"))
        return subprocess.CompletedProcess(argv, 0, ui_xml(), "")

    monkeypatch.setattr(subprocess, "run", run)
    ok("ui_wait", {"device": "SERIAL_A", "text": "OK", "timeout_s": 2})
    assert seen and all(t is not None and t <= 3 for t in seen)


def test_ui_wait_reports_an_error_only_when_no_dump_ever_succeeded(adb, clock):
    adb.on("uiautomator", returncode=1)
    assert err("ui_wait", {"device": "SERIAL_A", "text": "OK", "timeout_s": 2})["error"] == "ui_dump_failed"


def test_ui_wait_in_a_read_only_session_never_uses_the_device_file_fallback(adb, clock):
    adb.on("exec-out uiautomator dump /dev/tty", stdout="", returncode=1)
    result, payload = call("ui_wait", {"device": "SERIAL_A", "text": "OK", "timeout_s": 1}, AccessPolicy(read_only=True))
    assert result.is_error and payload["error"] == "ui_dump_failed"
    assert not any("/sdcard" in " ".join(c) for c in adb.actions)


# --- test_run / test_status ------------------------------------------------------------------------------


PASSING = [
    "INSTRUMENTATION_STATUS: numtests=2\n", "INSTRUMENTATION_STATUS: stream=\n", "com.example.app.LoginTest:\n",
    "INSTRUMENTATION_STATUS: id=AndroidJUnitRunner\n", "INSTRUMENTATION_STATUS: test=testOk\n",
    "INSTRUMENTATION_STATUS: class=com.example.app.LoginTest\n", "INSTRUMENTATION_STATUS: current=1\n", "INSTRUMENTATION_STATUS_CODE: 1\n",
    "INSTRUMENTATION_STATUS: test=testOk\n", "INSTRUMENTATION_STATUS: class=com.example.app.LoginTest\n", "INSTRUMENTATION_STATUS_CODE: 0\n",
    "INSTRUMENTATION_STATUS: test=testBad\n", "INSTRUMENTATION_STATUS: class=com.example.app.LoginTest\n", "INSTRUMENTATION_STATUS_CODE: 1\n",
    "INSTRUMENTATION_STATUS: test=testBad\n", "INSTRUMENTATION_STATUS: class=com.example.app.LoginTest\n",
    "INSTRUMENTATION_STATUS: stack=java.lang.AssertionError: expected:<1> but was:<2>\n", "\tat com.example.app.LoginTest.testBad(LoginTest.kt:42)\n",
    "INSTRUMENTATION_STATUS_CODE: -2\n",
    "INSTRUMENTATION_RESULT: stream=\n", "\n", "Time: 0.5\n", "FAILURES!!!\n", "Tests run: 2,  Failures: 1\n", "\n", "INSTRUMENTATION_CODE: -1\n",
]
PASSED_ALL = [ln for ln in PASSING if "testBad" not in ln][:0] + [
    "INSTRUMENTATION_STATUS: numtests=1\n", "INSTRUMENTATION_STATUS: test=testOk\n", "INSTRUMENTATION_STATUS: class=com.example.app.LoginTest\n",
    "INSTRUMENTATION_STATUS_CODE: 1\n", "INSTRUMENTATION_STATUS: test=testOk\n", "INSTRUMENTATION_STATUS: class=com.example.app.LoginTest\n",
    "INSTRUMENTATION_STATUS_CODE: 0\n", "INSTRUMENTATION_RESULT: stream=\n", "OK (1 test)\n", "INSTRUMENTATION_CODE: -1\n",
]


class FakeProc:
    """A Popen stand-in whose stdout is fed by the test."""

    def __init__(self, lines: list[str] | None = None, *, hold: bool = False) -> None:
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._ended = threading.Event()
        self.killed = False
        self.returncode: int | None = None
        for line in lines or []:
            self._queue.put(line)
        if not hold:
            self.finish()
        self.stdout = iter(self._queue.get, None)

    def push(self, *lines: str) -> None:
        for line in lines:
            self._queue.put(line)

    def finish(self) -> None:
        self._queue.put(None)
        self._ended.set()

    def wait(self, timeout: float | None = None) -> int:
        self._ended.wait(timeout)
        self.returncode = -9 if self.killed else 0
        return self.returncode

    def terminate(self) -> None:
        self.killed = True
        self.finish()

    kill = terminate


@pytest.fixture
def popen(monkeypatch, adb):
    # Waits are shortened for the tests: a run that is still going returns `running` after 0.3 s, not 1 s.
    monkeypatch.setattr(agent_tools, "MIN_TEST_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(agent_tools, "MAX_TEST_WAIT_SECONDS", 0.3)
    spawned: list[tuple[list[str], FakeProc]] = []
    queued: list[FakeProc] = []

    def factory(argv, **kwargs):
        proc = queued.pop(0) if queued else FakeProc(PASSED_ALL)
        spawned.append((list(argv), proc))
        return proc

    monkeypatch.setattr(subprocess, "Popen", factory)
    factory.spawned, factory.queued = spawned, queued  # type: ignore[attr-defined]
    return factory


def run_args(**extra: Any) -> dict[str, Any]:
    return {"device": "SERIAL_A", "test_package": TEST_PKG, "runner": RUNNER, "timeout_s": 1, **extra}


def test_test_run_finishes_within_the_wait_and_parses_counts_and_failures(popen):
    popen.queued.append(FakeProc(PASSING))
    payload = ok("test_run", run_args())
    assert payload["status"] == "failed" and payload["passed"] is False
    assert payload["counts"]["passed"] == 1 and payload["counts"]["failed"] == 1 and payload["counts"]["total"] == 2
    failure = payload["failures"][0]
    assert failure["test"] == "com.example.app.LoginTest#testBad" and "expected:<1>" in failure["message"] and failure["kind"] == "failure"
    assert "LoginTest.kt:42" in failure["stack"] and "claim_boundary" in payload and payload["content_trust"] == "untrusted_device_data"
    argv, _ = popen.spawned[0]
    assert argv == ["/mock/adb" if argv[0] == "/mock/adb" else argv[0], "-s", "SERIAL_A", "shell", "am", "instrument", "-w", "-r", f"{TEST_PKG}/{RUNNER}"]
    assert safety.match_agent_shell_shape(argv[3:]) == "instrument"


def test_test_run_class_and_method_shapes(popen):
    ok("test_run", run_args(**{"class": f"{PKG}.LoginTest"}))
    ok("test_run", run_args(**{"class": f"{PKG}.LoginTest", "method": "testOk"}))
    assert popen.spawned[0][0][3:] == ["shell", "am", "instrument", "-w", "-r", "-e", "class", f"{PKG}.LoginTest", f"{TEST_PKG}/{RUNNER}"]
    assert popen.spawned[1][0][-2] == f"{PKG}.LoginTest#testOk"
    assert [safety.match_agent_shell_shape(a[3:]) for a, _ in popen.spawned] == ["instrument_class", "instrument_method"]


@pytest.mark.parametrize("extra", [
    {"test_package": "bad;id"}, {"runner": "NoDots"}, {"runner": "-w"}, {"class": "-bad"}, {"class": "a.b$C"},
    {"class": ".Rel"}, {"method": "m"}, {"class": "a.b.C", "method": "m;id"}, {"class": "a.b.C", "method": "m#x"},
])
def test_test_run_refuses_unsafe_arguments_before_spawning(extra, popen):
    result, payload = call("test_run", run_args(**extra))
    assert result.is_error and payload["error"] in {"invalid_params", "invalid_package"} and popen.spawned == []


def test_a_run_that_is_not_finished_returns_running_then_test_status_follows_it(popen):
    proc = FakeProc(["INSTRUMENTATION_STATUS: numtests=1\n", "INSTRUMENTATION_STATUS: test=testOk\n",
                     "INSTRUMENTATION_STATUS: class=com.example.app.LoginTest\n", "INSTRUMENTATION_STATUS_CODE: 1\n"], hold=True)
    popen.queued.append(proc)
    started = ok("test_run", run_args())
    assert started["status"] == "running" and len(started["run_id"]) == 12 and "test_status" in started["next"]
    time.sleep(0.1)
    running = ok("test_status", {"run_id": started["run_id"]})
    assert running["status"] == "running" and running["progress"]["current"] == "com.example.app.LoginTest#testOk" and running["progress"]["total"] == 1
    proc.push("INSTRUMENTATION_STATUS: test=testOk\n", "INSTRUMENTATION_STATUS: class=com.example.app.LoginTest\n",
              "INSTRUMENTATION_STATUS_CODE: 0\n", "INSTRUMENTATION_CODE: -1\n")
    proc.finish()
    deadline = time.time() + 5
    while time.time() < deadline and ok("test_status", {"run_id": started["run_id"]})["status"] == "running":
        time.sleep(0.02)
    done = ok("test_status", {"run_id": started["run_id"]})
    assert done["status"] == "passed" and done["passed"] is True and done["counts"]["passed"] == 1 and done["failures"] == []


def test_one_run_per_device_and_other_devices_are_independent(popen, adb):
    adb.listing = TWO_READY
    first = FakeProc(hold=True)
    popen.queued.append(first)
    started = ok("test_run", run_args())
    assert started["status"] == "running"
    busy = err("test_run", run_args())
    assert busy["error"] == "test_run_busy" and busy["run_id"] == started["run_id"]
    assert busy["failure"]["id"] == "app.test_busy" and busy["failure"]["next_tool"]["name"] == "test_status"
    assert len(popen.spawned) == 1
    other = ok("test_run", {**run_args(), "device": "SERIAL_B"})
    assert other["status"] == "passed" and len(popen.spawned) == 2
    first.push("INSTRUMENTATION_CODE: -1\n")
    first.finish()
    deadline = time.time() + 5
    while time.time() < deadline and ok("test_status", {"run_id": started["run_id"]})["status"] == "running":
        time.sleep(0.02)
    assert ok("test_run", run_args())["status"] in {"passed", "error", "failed"}  # the device is free again


def test_hard_total_cap_stops_a_hung_run_and_force_stops_the_test_package(popen, adb, monkeypatch):
    monkeypatch.setattr(agent_tools, "MAX_TEST_RUN_SECONDS", 0.8)
    proc = FakeProc(hold=True)
    popen.queued.append(proc)
    started = ok("test_run", run_args())
    assert started["status"] == "running"
    deadline = time.time() + 5
    status = started
    while time.time() < deadline and status["status"] == "running":
        time.sleep(0.02)
        status = ok("test_status", {"run_id": started["run_id"]})
    assert status["status"] == "timed_out" and proc.killed and "hard limit" in status["reason"]
    deadline = time.time() + 2
    while time.time() < deadline and not any(c == f"shell am force-stop {TEST_PKG}" for c in adb.commands()):
        time.sleep(0.02)
    assert f"shell am force-stop {TEST_PKG}" in adb.commands()
    assert ok("test_status", {"run_id": started["run_id"]})["cleanup"] in {"force_stopped", None}


def test_server_exit_cleans_up_active_runs(popen, adb):
    proc = FakeProc(hold=True)
    popen.queued.append(proc)
    started = ok("test_run", run_args())
    agent_tools.shutdown_test_runs()
    assert proc.killed
    assert ok("test_status", {"run_id": started["run_id"]})["status"] == "cancelled"
    assert f"shell am force-stop {TEST_PKG}" in adb.commands()


def test_running_server_loop_shuts_runs_down_in_its_finally_block(monkeypatch):
    import anyio
    import adapt_adb.mcp.server as server

    stopped: list[bool] = []
    monkeypatch.setattr(agent_tools, "shutdown_test_runs", lambda: stopped.append(True))

    class Boom(Exception):
        pass

    def fake_stdio_server(*a, **k):
        raise Boom

    monkeypatch.setattr("mcp.server.stdio.stdio_server", fake_stdio_server)
    with pytest.raises(Boom):
        anyio.run(server.run_stdio_async)
    assert stopped == [True]


@pytest.mark.parametrize("lines,status", [
    (["adb: error: device offline\n"], "error"),
    (["INSTRUMENTATION_FAILED: com.example.app.test/androidx.test.runner.AndroidJUnitRunner\n"], "error"),
    (["INSTRUMENTATION_RESULT: shortMsg=Process crashed.\n", "INSTRUMENTATION_CODE: 0\n"], "error"),
    (["INSTRUMENTATION_CODE: -1\n"], "error"),
])
def test_runs_that_did_not_run_tests_are_errors_not_passes(lines, status, popen):
    popen.queued.append(FakeProc(lines))
    payload = ok("test_run", run_args())
    assert payload["status"] == status and payload["passed"] is False and payload["reason"]


def test_failures_are_capped_and_the_result_stays_within_the_budget(popen):
    lines = ["INSTRUMENTATION_STATUS: numtests=60\n"]
    for i in range(60):
        lines += [f"INSTRUMENTATION_STATUS: test=t{i}\n", "INSTRUMENTATION_STATUS: class=com.example.app.BigTest\n",
                  "INSTRUMENTATION_STATUS: stack=java.lang.AssertionError: " + "x" * 3000 + "\n", "INSTRUMENTATION_STATUS_CODE: -2\n"]
    lines.append("INSTRUMENTATION_CODE: -1\n")
    popen.queued.append(FakeProc(lines))
    payload = ok("test_run", run_args())
    assert payload["counts"]["failed"] == 60 and len(payload["failures"]) <= agent_tools.MAX_FAILURES_RETURNED
    assert payload["failures_omitted"] >= 40 and len(json.dumps(payload)) <= 21000
    assert all(len(f["stack"]) <= 1250 for f in payload["failures"])


def test_test_status_rejects_bad_or_unknown_ids_and_hides_runs_on_other_devices(popen, adb):
    assert err("test_status", {"run_id": "zzzzzzzzzzzz"})["error"] == "invalid_params"
    unknown = err("test_status", {"run_id": "0" * 12})
    assert unknown["error"] == "unknown_run" and unknown["failure"]["id"] == "app.test_run_unknown"
    started = ok("test_run", run_args())
    assert ok("test_status", {"run_id": started["run_id"]}, AccessPolicy(allowed_devices=("SERIAL_A",)))["run_id"] == started["run_id"]
    assert err("test_status", {"run_id": started["run_id"]}, AccessPolicy(allowed_devices=("SERIAL_B",)))["error"] == "unknown_run"


def test_finished_runs_are_evicted_beyond_the_keep_limit(popen, monkeypatch):
    monkeypatch.setattr(agent_tools, "KEEP_FINISHED_RUNS", 2)
    ids = [ok("test_run", run_args())["run_id"] for _ in range(4)]
    with agent_tools._LOCK:
        agent_tools._evict()
    kept = [i for i in ids if "error" not in call("test_status", {"run_id": i})[1]]
    assert len(kept) == 2 and kept == ids[-2:]


# --- Developer Options with ADAPT-owned snapshots -----------------------------------------------------------


@pytest.fixture
def device(monkeypatch, tmp_path, adb):
    adb.listing = "List of devices attached\nendpoint-5555\tdevice usb:1-1 product:p model:M device:d transport_id:1\n"
    fake = FakeDevice()
    monkeypatch.setattr(dev_options, "_run_process", fake)
    monkeypatch.setattr(dev_options, "_LOCK_ROOT", tmp_path / "locks")
    monkeypatch.setattr(dev_options, "preflight_adb_server", lambda *a, **k: None)
    monkeypatch.setattr(agent_tools, "state_dir", lambda: tmp_path / "state")
    fake.state = tmp_path / "state"  # type: ignore[attr-defined]
    return fake


def snapshots(device) -> list[Path]:
    return sorted(device.state.rglob("*.json")) if device.state.exists() else []


def test_apply_writes_a_snapshot_in_the_state_directory_named_by_identity_and_profile(device):
    payload = ok("dev_options_apply", {"device": "endpoint-5555", "profile": "fast-ui"})
    assert payload["profile"] == "fast-ui" and sorted(payload["changed"]) == ["animator_duration_scale", "transition_animation_scale", "window_animation_scale"]
    [path] = snapshots(device)
    assert path.parent.parent == device.state and len(path.parent.name) == 16
    assert path.name.startswith("fast-ui-") and path.stem == payload["snapshot_id"]
    assert "endpoint-5555" not in str(path) and "physical-123" not in str(path)
    assert str(device.state) not in json.dumps(payload) and "snapshot" not in payload  # only the id is returned, never a path
    assert device.settings[("global", None, "window_animation_scale")] == "0"
    assert "dev_options_restore" in payload["note"]


def test_a_second_apply_for_the_same_device_and_profile_is_refused_while_pending(device):
    ok("dev_options_apply", {"device": "endpoint-5555", "profile": "fast-ui"})
    writes = device._put_count
    payload = err("dev_options_apply", {"device": "endpoint-5555", "profile": "fast-ui"})
    assert payload["error"] == "snapshot_pending" and payload["failure"]["id"] == "devopts.snapshot_pending"
    assert payload["failure"]["next_tool"]["name"] == "dev_options_restore" and payload["pending"][0]["profile"] == "fast-ui"
    assert device._put_count == writes and len(snapshots(device)) == 1
    assert "path" not in payload["pending"][0]
    # Another profile on the same device is a different transaction.
    assert ok("dev_options_apply", {"device": "endpoint-5555", "profile": "touches"})["profile"] == "touches"
    assert len(snapshots(device)) == 2


def test_restore_undoes_pending_snapshots_newest_first_and_apply_is_allowed_again(device):
    before = dict(device.settings)
    ok("dev_options_apply", {"device": "endpoint-5555", "profile": "fast-ui"})
    ok("dev_options_apply", {"device": "endpoint-5555", "profile": "touches"})
    assert device.settings != before
    payload = ok("dev_options_restore", {"device": "endpoint-5555"})
    assert [p["profile"] for p in payload["pending_before"]] == ["fast-ui", "touches"]
    assert [r["profile"] for r in payload["results"]] == ["touches", "fast-ui"] and payload["pending"] == []
    assert device.settings == before
    assert ok("dev_options_restore", {"device": "endpoint-5555"})["pending_before"] == []  # nothing pending: still a success
    assert ok("dev_options_apply", {"device": "endpoint-5555", "profile": "fast-ui"})["ok"]  # a restored snapshot no longer blocks


def test_restore_can_be_limited_to_one_profile(device):
    ok("dev_options_apply", {"device": "endpoint-5555", "profile": "fast-ui"})
    ok("dev_options_apply", {"device": "endpoint-5555", "profile": "touches"})
    payload = ok("dev_options_restore", {"device": "endpoint-5555", "profile": "touches"})
    assert [r["profile"] for r in payload["results"]] == ["touches"]
    assert payload["ok"] is True  # what was asked for is restored
    assert [p["profile"] for p in payload["pending"]] == ["fast-ui"]  # but the other profile is still pending, and said so


def test_restore_conflict_is_reported_and_the_snapshot_stays_pending(device):
    ok("dev_options_apply", {"device": "endpoint-5555", "profile": "fast-ui"})
    device.settings[("global", None, "window_animation_scale")] = "0.75"  # someone else changed it
    payload = err("dev_options_restore", {"device": "endpoint-5555"})
    assert payload["error"] == "restore_incomplete" and payload["failure"]["id"] in {"devopts.restore_conflict", "devopts.restore_partial"}
    assert payload["pending"] and device.settings[("global", None, "window_animation_scale")] == "0.75"
    assert err("dev_options_apply", {"device": "endpoint-5555", "profile": "fast-ui"})["error"] == "snapshot_pending"


def test_a_snapshot_for_another_device_identity_is_not_pending_here(device):
    ok("dev_options_apply", {"device": "endpoint-5555", "profile": "fast-ui"})
    device.serialno = "another-physical-device"  # a different phone behind the same transport name
    assert ok("dev_options_restore", {"device": "endpoint-5555"})["pending_before"] == []
    assert ok("dev_options_apply", {"device": "endpoint-5555", "profile": "fast-ui"})["ok"]


def test_a_damaged_snapshot_counts_as_pending_and_is_never_overwritten(device):
    ok("dev_options_apply", {"device": "endpoint-5555", "profile": "fast-ui"})
    [path] = snapshots(device)
    path.write_text("not json", encoding="utf-8")
    payload = err("dev_options_apply", {"device": "endpoint-5555", "profile": "fast-ui"})
    assert payload["error"] == "snapshot_pending" and payload["pending"][0]["status"] == "unreadable"
    assert path.read_text(encoding="utf-8") == "not json"


def test_apply_failure_returns_a_classified_error_and_keeps_the_snapshot(device):
    device.fail_put_key = "transition_animation_scale"
    payload = err("dev_options_apply", {"device": "endpoint-5555", "profile": "fast-ui"})
    assert payload["error"] == "apply_failed" and payload["failure"]["id"].startswith("devopts.")
    assert len(snapshots(device)) == 1
    assert err("dev_options_apply", {"device": "endpoint-5555", "profile": "fast-ui"})["error"] == "snapshot_pending"


def test_apply_and_restore_validate_the_profile_name(device):
    assert err("dev_options_apply", {"device": "endpoint-5555", "profile": "rm -rf"})["error"] == "invalid_params"
    assert err("dev_options_restore", {"device": "endpoint-5555", "profile": "nope"})["error"] == "invalid_params"
    assert device.calls == []


def test_snapshot_directory_is_per_user_and_never_the_working_directory(monkeypatch, tmp_path):
    variable = "LOCALAPPDATA" if agent_tools.os.name == "nt" else "XDG_STATE_HOME"
    monkeypatch.setenv(variable, str(tmp_path / "state-root"))
    assert agent_tools.state_dir() == tmp_path / "state-root" / "adapt" / "dev-options-snapshots"
    monkeypatch.delenv(variable)
    default = agent_tools.state_dir()
    assert default.parts[-2:] == ("adapt", "dev-options-snapshots") and Path.home() in default.parents


# --- bounded output ------------------------------------------------------------------------------------------


def test_bound_payload_keeps_the_newest_lines_and_says_how_to_narrow():
    payload = {"lines": [f"line {i} " + "x" * 100 for i in range(500)], "since": "11-16 10:00:00.000"}
    bounded = agent_tools.bound_payload(payload)
    assert len(json.dumps(bounded)) <= agent_tools.MAX_RESULT_CHARS
    assert bounded["truncated"] is True and bounded["omitted"]["lines"] > 0 and "since" in bounded["narrow_hint"]
    assert bounded["lines"][-1] == payload["lines"][-1] and bounded["lines"][0] != payload["lines"][0]
    small = {"lines": ["a"]}
    assert agent_tools.bound_payload(small) is small


def test_bound_payload_shortens_giant_strings_and_keeps_the_failure_tag():
    from adapt_adb.failure_report import tag_payload

    tagged = tag_payload({"error": "x", "message": "y" * 50000, "failures": []}, "app.launch_failed")
    bounded = agent_tools.bound_payload(tagged)
    assert len(json.dumps(bounded)) <= agent_tools.MAX_RESULT_CHARS
    assert getattr(bounded, "failure_id", None) == "app.launch_failed" and bounded["truncated"] is True


def test_logcat_tail_result_is_cut_to_the_budget(adb):
    adb.on("logcat", stdout="".join(f"11-16 10:00:{i % 60:02d}.000 I/T(1): " + "z" * 1500 + "\n" for i in range(500)))
    payload = ok("logcat_tail", {"device": "SERIAL_A", "lines": 500})
    assert len(json.dumps(payload)) <= 21000 and payload["truncated"] is True and "log_mark" in payload["narrow_hint"]


def test_every_new_tool_result_stays_within_the_budget_even_when_adb_floods(adb):
    adb.on("", stdout="x" * 500000)
    for name, args in (("app_stop", ACTING_ARGS["app_stop"]), ("app_launch", {**ACTING_ARGS["app_launch"], "activity": ".Main"}),
                       ("app_open_link", ACTING_ARGS["app_open_link"]), ("permission_grant", ACTING_ARGS["permission_grant"])):
        _, payload = call(name, {"device": "SERIAL_A", **args})
        assert len(json.dumps(payload)) <= 21000, name


# --- content trust, failure shape and server instructions -----------------------------------------------------


def test_content_trust_marks_successful_device_text_results_only(adb):
    adb.on("pidof", stdout="1\n")
    adb.on("date", stdout="11-16_10:06:00.123456789\n")
    assert ok("app_state", {"device": "SERIAL_A", "package": PKG})["content_trust"] == "untrusted_device_data"
    assert "content_trust" not in ok("log_mark", {"device": "SERIAL_A"})  # a validated timestamp, not device prose
    assert "content_trust" not in ok("app_stop", {"device": "SERIAL_A", "package": PKG})
    assert {"app_launch", "app_state", "test_run", "test_status", "ui_tap_text", "ui_wait"} <= access.DEVICE_TEXT_TOOLS


def test_every_new_error_code_carries_a_failure_object_with_retry_and_next_tool(adb):
    adb.on("cmd package resolve-activity", stdout="")
    adb.on("uiautomator", stdout=TWO_OKS)
    cases = [
        ("app_launch", {"device": "SERIAL_A", "package": PKG}), ("app_open_link", {"device": "SERIAL_A", "uri": "javascript:x"}),
        ("permission_grant", {"device": "SERIAL_A", "package": PKG, "permission": "x.y"}), ("ui_tap_text", {"device": "SERIAL_A", "text": "OK"}),
        ("test_status", {"run_id": "0" * 12}), ("app_state", {"device": "SERIAL_A", "package": "bad"}),
    ]
    for name, args in cases:
        payload = err(name, args)
        failure = payload["failure"]
        assert failure["id"] in GUIDES and failure["id"] != "internal.unclassified", (name, failure["id"])
        assert failure["retry"] in {"safe_now", "with_changed_arguments", "after_user_action", "not_useful"}
        assert failure["next_tool"] is None or failure["next_tool"]["name"] in {s["name"] for s in tools.TOOL_SCHEMAS}


def test_new_guides_point_only_at_real_tools_exposed_to_the_session():
    for guide_id in ("app.launch_failed", "app.no_launcher_activity", "app.link_invalid", "app.link_unhandled", "app.permission_refused",
                     "app.test_busy", "app.test_run_unknown", "app.ui_no_match", "app.ui_ambiguous", "app.ui_dump_failed", "devopts.snapshot_pending"):
        hint = GUIDES[guide_id].mcp
        assert hint is not None, guide_id
        for name in ((hint.next_tool,) if hint.next_tool else ()) + hint.fallback_tools:
            assert name in access.MUTATING_TOOLS | access.OBSERVATION_TOOLS, (guide_id, name)


POLICIES = {
    "full": AccessPolicy(), "read-only": AccessPolicy(read_only=True), "one-device": AccessPolicy(allowed_devices=("SERIAL",)),
    "read-only one-device": AccessPolicy(read_only=True, allowed_devices=("SERIAL",)), "two-devices": AccessPolicy(allowed_devices=("A", "B")),
}


@pytest.mark.parametrize("label", list(POLICIES))
def test_instructions_name_the_loop_and_only_tools_the_session_exposes(label):
    import re

    policy = POLICIES[label]
    text = agent_guide.mcp_instructions(policy)
    assert 0 < len(text) < 2000 and text.isascii()
    exposed = {s["name"] for s in tools.TOOL_SCHEMAS if policy.exposes(s["name"])}
    allowed_words = {"device", "failure", "content_trust", "untrusted_device_data"}
    for token in re.findall(r"`([a-z_]+)`", text):
        assert token in exposed | allowed_words, (label, token)
    if policy.read_only:
        assert not set(re.findall(r"`([a-z_]+)`", text)) & access.MUTATING_TOOLS
        assert "app_state" in text and "log_mark" in text and "ui_wait" in text
    else:
        loop = ["install_apk", "app_launch", "ui_summary", "ui_tap_text", "app_state", "app_logs", "log_mark", "dev_options_restore"]
        positions = [text.index(f"`{name}`") for name in loop]
        assert positions[:3] == sorted(positions[:3])  # install -> launch -> observe
        assert "dev_options_restore" in text and "test_status" in text and "`ui_wait`" in text
        for name in access.MUTATING_TOOLS:
            assert f"`{name}`" in text


def test_instructions_stay_under_the_claude_code_limit_with_room_to_spare():
    assert max(len(agent_guide.mcp_instructions(p)) for p in POLICIES.values()) < 2000


# --- device naming and schema patterns ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,args", [("app_state", {"package": PKG}), ("log_mark", {}), ("ui_wait", {"text": "OK"})])
def test_a_blank_device_never_picks_the_only_ready_phone_even_for_reads(name, args, adb):
    payload = err(name, {**args, "device": "   "})
    assert payload["error"] == "need_device" and adb.actions == []


def test_the_advertised_pattern_of_since_is_enforced_by_the_server():
    from adapt_adb.mcp import server

    for name in ("logcat_tail", "app_logs"):
        base = {"device": "SERIAL_A", "package": PKG} if name == "app_logs" else {"device": "SERIAL_A"}
        assert server._arguments_match_schema(name, {**base, "since": "11-16 10:06:00.123"})
        for bad in ("a" * 18, "11-16 10:06:00.12x", "11-16 10:06:00.1234", "11-16 10:06:00.123\n"):
            assert not server._arguments_match_schema(name, {**base, "since": bad}), bad


def test_state_dir_is_created_private_and_the_identity_folder_is_a_hash(device):
    ok("dev_options_apply", {"device": "endpoint-5555", "profile": "touches"})
    [path] = snapshots(device)
    assert all(c in "0123456789abcdef" for c in path.parent.name)
    assert path.suffix == ".json" and path.read_text(encoding="utf-8").startswith("{")
