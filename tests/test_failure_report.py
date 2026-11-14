"""Failure guidance runtime: classification, context, rendering, suppression and privacy.

Offline: no adb, network or device; streams and environment are faked.
"""

from __future__ import annotations

import io
import itertools
import json
import subprocess
from dataclasses import fields, replace

import pytest

from adapt_adb import failure_guide as fg
from adapt_adb import failure_report as fr
from adapt_adb import help_system as help_mod
from adapt_adb.adb import AdbProbeError
from adapt_adb.doctor import UnsafeAdbServerError
from adapt_adb.mcp.access import AccessPolicy
from adapt_adb.safety import NeedSerial


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("ADAPT_CONFIG_DIR", str(tmp_path / "config"))
    for name in ("ADAPT_TIPS", "ADAPT_FAILURE_GUIDANCE", "ADAPT_ADB", "ADB_PATH", "ADB_SERVER_SOCKET",
                 "ANDROID_ADB_SERVER_PORT", "ADB_SERVER_PORT", "NO_COLOR", "TERM"):
        monkeypatch.delenv(name, raising=False)


class Root:
    """Minimal stand-in for a Click root context."""

    def __init__(self, *, machine=False, no_tips=False):
        self.meta = {"adapt_machine_output": machine}
        self.params = {"no_tips": no_tips}

    def find_root(self):
        return self


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def test_classify_priority_tag_chain_class_default_type_fallback():
    assert fr.classify(fr.tagged(ValueError("x"), "setup.archive_unsafe")) == "setup.archive_unsafe"
    tagged_server = fr.tagged(UnsafeAdbServerError("m"), "adb.server.foreign_owner")
    assert fr.classify(tagged_server) == "adb.server.foreign_owner"
    assert fr.classify(UnsafeAdbServerError("m")) == "adb.server.unverified"
    try:
        try:
            raise tagged_server
        except UnsafeAdbServerError as inner:
            raise RuntimeError("wrapper") from inner
    except RuntimeError as outer:
        assert fr.classify(outer) == "adb.server.foreign_owner"
    assert fr.classify(AdbProbeError("x")) == "adb.enumeration_failed"
    assert fr.classify(NeedSerial("x")) == "device.multiple"
    assert fr.classify(subprocess.TimeoutExpired(["adb"], 1)) == "adb.timeout"
    assert fr.classify(PermissionError("denied")) == "host.os_error"
    assert fr.classify(ImportError("mcp"), area="mcp") == "mcp.sdk_missing"
    assert fr.classify(KeyError("x")) == "internal.unclassified"
    assert fr.classify(fr.tagged(ValueError("x"), "not.a_real_id")) == "internal.unclassified"


def test_tagging_keeps_type_and_message():
    exc = fr.tagged(RuntimeError("unchanged message"), "setup.digest_unavailable")
    assert type(exc) is RuntimeError and str(exc) == "unchanged message"
    assert fr.explicit_id(exc) == "setup.digest_unavailable"
    assert fr.explicit_id(UnsafeAdbServerError("m")) is None  # class default is not an instance tag


@pytest.mark.parametrize("text,key", [
    ("failed to connect to 192.0.2.1:5555: Connection refused", "refused"),
    ("cannot connect: Connection timed out", "timed_out"),
    ("No route to host", "no_route"),
    ("Network is unreachable", "no_route"),
    ("failed to authenticate to 192.0.2.1:37000", "auth"),
    ("Failed: Wrong password or connection was dropped.", "wrong_code"),
    ("protocol fault (couldn't read status message)", "protocol"),
    ("Unable to start pairing client.", "pairing_client"),
    ("something new", "unknown"),
])
def test_wireless_symptoms(text, key):
    assert fr.adb_symptom(text, None) == key


def test_device_state_keys():
    assert [fr.device_state_key(s) for s in ("device", "unauthorized", "offline", "no permissions", "recovery", None)] == [
        "device", "unauthorized", "offline", "no_permissions", "other", "other"]
    assert fr.device_state_failure_id("other") == "device.not_ready"


@pytest.mark.parametrize("check,expected", [
    ({"summary": "Selected non-default ADB server endpoint is unverified.", "details": {}}, "adb.server.endpoint_unverified"),
    ({"summary": "ADB listener inspection failed; idle state is unverified.", "details": {}}, "adb.server.probe_failed"),
    ({"summary": "Port 5037 ...", "details": {"owner_is_root_or_system": True}}, "adb.server.foreign_owner"),
    ({"summary": "Port 5037 ...", "details": {"owner_matches_current_user": False}}, "adb.server.foreign_owner"),
    ({"summary": "Running ...", "details": {"owner_matches_current_user": True, "version_mismatch": True}}, "adb.server.version_mismatch"),
    ({"summary": "Listening ...", "details": {"owner_matches_current_user": True}}, "adb.server.unverified"),
    (None, "adb.server.unverified"),
])
def test_server_check_failure_id(check, expected):
    assert fr.server_check_failure_id(check) == expected


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------

def test_context_reads_presence_only(monkeypatch):
    monkeypatch.setenv("ADAPT_ADB", "SENTINEL_PATH_VALUE")
    monkeypatch.setenv("ADB_SERVER_SOCKET", "tcp:SENTINEL_HOST:1")
    monkeypatch.setattr(fr.sys, "platform", "linux")
    monkeypatch.setenv("WSL_DISTRO_NAME", "SENTINEL_DISTRO")
    ctx = fr.FailureContext.from_runtime(command="adb", device_state="offline", unknown_key=1)
    assert ctx.host_os == "linux" and ctx.wsl is True
    assert ctx.adb_override_set is True and ctx.adb_server_env_override is True
    assert ctx.device_state == "offline" and ctx.command == "adb"
    assert "SENTINEL" not in repr(ctx)


def test_every_context_field_is_constructible_and_scalar():
    values = {"command": "adb", "surface": "mcp", "host_os": "windows", "adb_origin": "unknown",
              "doctor_checks": {"adb_path": "fail"}, "device_state": "absent", "adb_symptom": "auth"}
    kwargs = {}
    for f in fields(fr.FailureContext):
        if f.name in values:
            kwargs[f.name] = values[f.name]
        elif "count" in f.name or f.name == "api_level":
            kwargs[f.name] = 1
        else:
            kwargs[f.name] = True
    ctx = fr.FailureContext(**kwargs)
    assert all(getattr(ctx, k) == v for k, v in kwargs.items())


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

WIN = fr.FailureContext(host_os="windows", adb_override_set=False)
LINUX = fr.FailureContext(host_os="linux", adb_override_set=False, ssh_session=False)


@pytest.mark.parametrize("failure_id,ctx,first_cause,first_step_command", [
    ("adb.not_found", WIN, "Google's Platform-Tools are not installed", "adapt setup --dry-run"),
    ("adb.not_found", replace(WIN, adb_override_set=True), "ADAPT_ADB or ADB_PATH is set", "adapt setup --dry-run"),
    ("wireless.connect_failed", fr.FailureContext(adb_symptom="auth"), "This PC has not been paired", None),
    ("wireless.connect_failed", fr.FailureContext(adb_symptom="timed_out"), "The phone and this PC cannot reach", None),
    ("wireless.pair_failed", fr.FailureContext(adb_symptom="wrong_code"), "ADB reported a wrong pairing code", None),
    ("device.none_ready", fr.FailureContext(device_state="unauthorized"), "A phone is connected but has not approved", "adapt devices"),
    ("device.none_ready", fr.FailureContext(device_state="absent"), "The cable charges only", "adapt devices"),
    ("device.not_found", fr.FailureContext(target_from_env=True), "The target came from the ANDROID_SERIAL", "adapt devices"),
    ("adb.server.endpoint_unverified", fr.FailureContext(adb_server_env_override=True, adb_server_option_set=False),
     "An environment variable selects another server", "adapt doctor --json"),
    ("adb.server.endpoint_unverified", fr.FailureContext(adb_server_env_override=False, adb_server_option_set=True),
     "The command line passes a server option", "adapt doctor --json"),
])
def test_resolver_table(failure_id, ctx, first_cause, first_step_command):
    resolved = fr.resolve(failure_id, ctx)
    assert resolved.causes[0].text.startswith(first_cause)
    if first_step_command:
        assert resolved.steps[0].step.command == first_step_command


def test_contradicted_items_drop_and_unknown_items_are_labelled():
    linux = fr.resolve("adb.server.foreign_owner", LINUX)
    assert any("sudo at some point" in c.text for c in linux.causes)
    assert not any("Administrator or SYSTEM process" in c.text for c in linux.causes)
    assert fr.context_unknown(linux) == []
    unknown = fr.resolve("adb.server.foreign_owner", fr.FailureContext())
    assert any(c.text.startswith("On Windows: ") for c in unknown.causes)
    assert any(c.text.startswith("On Linux: ") for c in unknown.causes)
    assert "host_os" in fr.context_unknown(unknown, max_causes=None)
    text = fr.render_human(unknown, max_causes=None)
    assert "Not sure which applies? adapt doctor checks this PC without changing anything." in text
    payload = fr.failure_payload(unknown)
    assert "host_os" in payload["context_unknown"]


def test_override_cause_absent_when_override_is_known_unset():
    resolved = fr.resolve("adb.not_found", WIN)
    assert not any("ADAPT_ADB or ADB_PATH is set" in c.text for c in resolved.causes)


def test_endpoint_guide_names_variables_without_values(monkeypatch):
    monkeypatch.setenv("ADB_SERVER_SOCKET", "tcp:SENTINEL_HOST:5038")
    ctx = fr.FailureContext.from_runtime(command="devices", adb_server_option_set=False)
    text = fr.render_human(fr.resolve("adb.server.endpoint_unverified", ctx))
    assert "ADB_SERVER_SOCKET" in text and "SENTINEL_HOST" not in text
    assert "-H, -P or -L" not in text.split("Try:")[0]


def test_templates_fill_known_values_and_degrade_when_unknown():
    assert "ADB lists 3 ready devices" in fr.resolve("device.multiple", fr.FailureContext(ready_count=3)).summary
    assert "ADB lists several ready devices" in fr.resolve("device.multiple").summary
    assert "{" not in fr.resolve("device.multiple").summary


def test_unknown_id_resolves_to_generic_guide_with_activity():
    resolved = fr.resolve("nope.missing", activity="listing devices")
    assert resolved.id == "internal.unclassified"
    assert resolved.summary == "ADAPT stopped while listing devices, and it does not have a specific explanation for this error."


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def test_block_format_caps_and_more_line():
    text = fr.render_human(fr.resolve("adb.not_found", WIN), width=80)
    lines = text.splitlines()
    assert lines[0].startswith("Why: ADAPT found no adb program")
    assert "  Possible causes, most likely first:" in lines
    assert "Try:" in lines
    assert "       adapt setup --dry-run   [read-only]" in lines
    assert "       adapt setup   [changes this PC]" in lines
    assert lines[-1] == "More: adapt explain adb.not_found   |   adapt help-me setup"
    assert sum(1 for ln in lines if ln.startswith("  - ")) <= fr.COMPACT_CAUSES
    assert sum(1 for ln in lines if ln[:5].strip().rstrip(".").isdigit()) <= fr.COMPACT_STEPS


def test_needs_admin_tag_and_phone_tag():
    text = fr.render_human(fr.resolve("setup.unsupported_arch", fr.FailureContext()))
    assert "[changes this PC] [needs admin]" in text
    assert "[on the phone]" in fr.render_human(fr.resolve("device.unauthorized"))


@pytest.mark.parametrize("width", [40, 60, 100])
def test_prose_wraps_to_width_but_commands_never_wrap(width):
    for guide_id in fg.GUIDES:
        text = fr.render_human(fr.resolve(guide_id, fr.FailureContext()), width=width, max_causes=None, max_steps=None)
        commands = {s.command for s in fg.GUIDES[guide_id].steps if s.command}
        for line in text.splitlines():
            stripped = line.strip()
            if any(stripped.startswith(c) for c in commands):
                continue
            if line.startswith("More: "):
                continue
            assert len(line) <= width or " " not in stripped, (guide_id, line)
        for command in commands:
            assert any(ln.strip().startswith(command + "   [") for ln in text.splitlines()), (guide_id, command)


def test_width_is_bounded(monkeypatch):
    monkeypatch.setattr(fr.shutil, "get_terminal_size", lambda fallback: type("S", (), {"columns": 300})())
    assert fr._width() == 100
    monkeypatch.setattr(fr.shutil, "get_terminal_size", lambda fallback: type("S", (), {"columns": 10})())
    assert fr._width() == 40


def test_colour_only_on_labels_and_change_tags():
    plain = fr.render_human(fr.resolve("adb.not_found", WIN), color=False)
    assert "\x1b" not in plain
    coloured = fr.render_human(fr.resolve("adb.not_found", WIN), color=True)
    assert "\x1b[1mWhy:\x1b[0m" in coloured and "\x1b[1mTry:\x1b[0m" in coloured and "\x1b[1mMore:\x1b[0m" in coloured
    assert "\x1b[33m[changes this PC]\x1b[0m" in coloured
    assert "\x1b[33m[read-only]" not in coloured


def test_colour_requires_tty_and_respects_no_color(monkeypatch):
    monkeypatch.setattr(fr, "_stderr_isatty", lambda: True)
    assert fr._color_allowed()
    monkeypatch.setenv("NO_COLOR", "1")
    assert not fr._color_allowed()
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("TERM", "dumb")
    assert not fr._color_allowed()


def test_control_characters_are_escaped():
    guide = fg.FailureGuide(id="internal.test_only", summary="bad \x1b[2J text.", certainty="observed",
                            causes=(fg.Cause("c\x07"),), steps=(fg.Step("s", "adapt doctor\x1b"),), help_topic=None)
    resolved = fr.Resolved(guide, guide.summary, (fr.ShownCause("c\x07"),), (fr.ShownStep(guide.steps[0], "s"),))
    text = fr.render_human(resolved)
    assert "\x1b" not in text and "\x07" not in text and "\\x1b" in text


def test_explain_view_shows_everything():
    text = fr.render_explain(fr.resolve("adb.server.foreign_owner"))
    assert text.startswith("ADAPT / EXPLAIN / adb.server.foreign_owner")
    assert "On Windows:" in text and "On Linux:" in text and "Varies by setup:" in text
    assert "Docs: docs/doctor.md" in text and "http" not in text


def test_search_exact_keyword_pasted_and_none():
    assert fr.search("device.unauthorized") == ["device.unauthorized"]
    assert fr.search("unauthorized") == ["device.unauthorized"]
    assert set(fr.search("error: failed to authenticate to 192.0.2.4:41234")) == {"wireless.pair_failed", "wireless.connect_failed"}
    assert len(fr.search("5037")) > 1
    assert fr.search("qqqq zzzz") == []
    assert fr.search("   ") == []


# ---------------------------------------------------------------------------
# Suppression matrix and emitting
# ---------------------------------------------------------------------------

def _tty(monkeypatch, value=True):
    monkeypatch.setattr(fr, "_stderr_isatty", lambda: value)


def test_guidance_mode_matrix(monkeypatch):
    _tty(monkeypatch, True)
    assert fr.guidance_mode(Root()) == "full"
    assert fr.guidance_mode(Root(machine=True)) == "off"
    assert fr.guidance_mode(Root(no_tips=True)) == "short"
    monkeypatch.setenv("ADAPT_TIPS", "0")
    assert fr.guidance_mode(Root()) == "short"
    monkeypatch.delenv("ADAPT_TIPS")
    help_mod.save_tips(False)
    assert fr.guidance_mode(Root()) == "short"
    help_mod.save_tips(True)
    assert fr.guidance_mode(Root()) == "full"
    _tty(monkeypatch, False)
    assert fr.guidance_mode(Root()) == "short"
    assert fr.guidance_mode(None) == "short"
    for mode in ("full", "short", "off"):
        monkeypatch.setenv("ADAPT_FAILURE_GUIDANCE", mode)
        assert fr.guidance_mode(Root()) == mode
    assert fr.guidance_mode(Root(machine=True)) == "off"


def test_env_beats_preference_and_broken_preference_is_short(monkeypatch):
    _tty(monkeypatch, True)
    path = help_mod.preferences_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"failure_guidance": "off"}', encoding="utf-8")
    assert fr.guidance_mode(Root()) == "off"
    monkeypatch.setenv("ADAPT_FAILURE_GUIDANCE", "full")
    assert fr.guidance_mode(Root()) == "full"
    monkeypatch.delenv("ADAPT_FAILURE_GUIDANCE")
    path.write_text("not json", encoding="utf-8")
    assert fr.guidance_mode(Root()) == "short"
    path.write_text('{"failure_guidance": "loud"}', encoding="utf-8")
    assert fr.guidance_mode(Root()) == "short"


def test_reading_mode_never_creates_preferences(monkeypatch):
    _tty(monkeypatch, True)
    fr.guidance_mode(Root())
    assert not help_mod.preferences_path().exists()


def test_emit_full_short_off_and_dedupe(monkeypatch):
    err = io.StringIO()
    monkeypatch.setattr(fr.sys, "stderr", err)
    root = Root()
    monkeypatch.setenv("ADAPT_FAILURE_GUIDANCE", "short")
    fr.emit_failure("adb.not_found", ctx=root)
    fr.emit_failure("adb.not_found", ctx=root)
    assert err.getvalue() == "More: adapt explain adb.not_found\n"
    monkeypatch.setenv("ADAPT_FAILURE_GUIDANCE", "full")
    err.seek(0); err.truncate()
    fr.emit_failure("device.offline", ctx=root)
    assert err.getvalue().startswith("\nWhy: ") and "More: adapt explain device.offline" in err.getvalue()
    monkeypatch.setenv("ADAPT_FAILURE_GUIDANCE", "off")
    err.seek(0); err.truncate()
    fr.emit_failure("device.multiple", ctx=root)
    assert err.getvalue() == ""


def test_emit_never_raises(monkeypatch):
    monkeypatch.setattr(fr, "guidance_mode", lambda ctx: (_ for _ in ()).throw(RuntimeError("boom")))
    fr.emit_failure("adb.not_found", ctx=Root())


# ---------------------------------------------------------------------------
# Machine output
# ---------------------------------------------------------------------------

def test_failure_payload_shape():
    payload = fr.failure_payload(fr.resolve("device.unauthorized", fr.FailureContext(host_os="windows")))
    assert set(payload) == {"schema", "id", "summary", "certainty", "causes", "recommendations", "avoid",
                            "help_topic", "docs", "explain", "context_unknown"}
    assert payload["schema"] == 1 and payload["id"] == "device.unauthorized"
    assert payload["explain"] == "adapt explain device.unauthorized"
    rec = payload["recommendations"][0]
    assert set(rec) == {"text", "command", "effect", "changes_state", "needs_admin", "applies"}
    assert rec["effect"] == "on_phone" and rec["changes_state"] is False and rec["applies"] == "yes"
    assert len(payload["causes"]) <= fr.JSON_CAUSES and len(payload["recommendations"]) <= fr.JSON_STEPS
    json.dumps(payload)


def test_attach_failure_is_additive_and_copies():
    original = {"error": "x", "exit_code": 1}
    result = fr.attach_failure(original, "setup.failed")
    assert original == {"error": "x", "exit_code": 1}
    assert result["error"] == "x" and result["exit_code"] == 1 and result["failure"]["id"] == "setup.failed"


def test_unknown_step_marked_in_json():
    payload = fr.failure_payload(fr.resolve("adb.exec_failed", fr.FailureContext()))
    assert any(r["applies"] == "unknown" for r in payload["recommendations"])


def test_mcp_failure_next_tool_respects_policy():
    for policy in (AccessPolicy(), AccessPolicy(read_only=True), AccessPolicy(allowed_devices=("A",)),
                   AccessPolicy(read_only=True, allowed_devices=("A",))):
        for error in fr.MCP_ERROR_MAP:
            failure = fr.mcp_failure(error, {"devices": []}, exposes=policy.exposes)
            if failure["next_tool"] is not None:
                assert policy.exposes(failure["next_tool"]["name"]), (policy, error)
            for rec in failure["recommendations"]:
                assert rec["mcp_tool"] is None or policy.exposes(rec["mcp_tool"])
    server = fr.mcp_failure("unsafe_shared_server", {"devices": []}, exposes=AccessPolicy(allowed_devices=("A",)).exposes)
    assert server["next_tool"] == {"name": "setup_status", "arguments": {}}
    assert fr.mcp_failure("unsafe_shared_server", {"devices": []})["next_tool"]["name"] == "doctor"


def test_mcp_failure_agent_fields():
    multiple = fr.mcp_failure("need_device", {"devices": [{"device": "A", "state": "device"}, {"device": "B", "state": "device"}]})
    assert multiple["retry"] == "with_changed_arguments" and multiple["next_tool"] is None
    assert "devices[].device" in multiple["argument_hint"]
    assert "ADB lists 2 ready devices" in multiple["summary"]
    unauthorized = fr.mcp_failure("no_device", {"devices": [{"device": "A", "state": "unauthorized"}]})
    assert unauthorized["id"] == "device.unauthorized" and unauthorized["user_action_required"] is True
    assert unauthorized["ask_user"] and unauthorized["retry"] == "after_user_action"
    mixed = fr.mcp_failure("no_device", {"devices": [{"device": "A", "state": "unauthorized"}, {"device": "B", "state": "offline"}]})
    assert mixed["id"] == "device.none_ready"
    requested = fr.mcp_failure("no_device", {"devices": [{"device": "A", "state": "unauthorized"}, {"device": "B", "state": "offline"}]},
                               requested_device="B")
    assert requested["id"] == "device.offline"
    denied = fr.mcp_failure("access_denied", {})
    assert denied["retry"] == "not_useful" and denied["next_tool"] is None
    refined = fr.mcp_failure("unsafe_shared_server", {}, failure_id="adb.server.foreign_owner")
    assert refined["id"] == "adb.server.foreign_owner"
    assert fr.mcp_failure("some_future_code", {})["id"] == "internal.unclassified"


# ---------------------------------------------------------------------------
# Unexpected exceptions
# ---------------------------------------------------------------------------

def test_report_unexpected_human_and_json(monkeypatch):
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(fr.sys, "stdout", out)
    monkeypatch.setattr(fr.sys, "stderr", err)
    root = Root()
    root.meta["adapt_command_path"] = "devices"
    monkeypatch.setenv("ADAPT_FAILURE_GUIDANCE", "full")
    fr.report_unexpected(root, KeyError("C:/secret/path"))
    text = err.getvalue()
    assert text.startswith("Error: ADAPT stopped unexpectedly while listing devices (KeyError).\n")
    assert "secret" not in text and "ADAPT_DEBUG=1" in text and "More: adapt explain --list" in text
    assert out.getvalue() == ""
    err.seek(0); err.truncate()
    machine = Root(machine=True)
    fr.report_unexpected(machine, KeyError("C:/secret/path"))
    data = json.loads(out.getvalue())
    assert data["status"] == "error" and data["exit_code"] == 1 and data["error"] == "internal_error"
    assert data["failure"]["id"] == "internal.unclassified" and err.getvalue() == ""
    assert "secret" not in out.getvalue()


# ---------------------------------------------------------------------------
# Privacy sentinels
# ---------------------------------------------------------------------------

SENTINELS = ("SENTINELUSER", "SENTINELHOME", "SENTINELLOCAL", "SENTINELADB", "SENTINELSERIAL", "203.0.113.77", "SENTINELHOST")


def _contexts():
    states = (None, "unauthorized", "offline", "no_permissions", "other", "absent")
    for host_os, state, flag, symptom in itertools.product(
            (None, "windows", "linux", "macos"), states, (None, True, False), (None, "auth", "refused")):
        yield fr.FailureContext(host_os=host_os, device_state=state, adb_override_set=flag,
                                adb_server_env_override=flag, adb_server_option_set=flag, target_from_env=flag,
                                ssh_session=flag, adb_symptom=symptom, ready_count=2, not_ready_count=1)


def test_no_sentinel_reaches_any_surface(monkeypatch, tmp_path):
    for name, value in (("USER", "SENTINELUSER"), ("USERNAME", "SENTINELUSER"), ("HOME", str(tmp_path / "SENTINELHOME")),
                        ("LOCALAPPDATA", str(tmp_path / "SENTINELLOCAL")), ("ADAPT_ADB", str(tmp_path / "SENTINELADB")),
                        ("ANDROID_SERIAL", "SENTINELSERIAL"), ("ADB_SERVER_SOCKET", "tcp:SENTINELHOST:5038")):
        monkeypatch.setenv(name, value)
    payload_devices = {"devices": [{"device": "SENTINELSERIAL", "state": "unauthorized", "model": "203.0.113.77"}]}
    outputs = []
    for guide_id in fg.GUIDES:
        for ctx in _contexts():
            resolved = fr.resolve(guide_id, replace(ctx, command="adb"))
            outputs.append(fr.render_human(resolved, max_causes=None, max_steps=None, color=True))
            outputs.append(json.dumps(fr.failure_payload(resolved, full=True)))
        runtime = fr.FailureContext.from_runtime(command="adb")
        outputs.append(fr.render_explain(fr.resolve(guide_id, runtime)))
        outputs.append(fr.render_short(guide_id))
        outputs.append(json.dumps(fr.mcp_failure("no_device", payload_devices, failure_id=guide_id,
                                                 requested_device="SENTINELSERIAL")))
    outputs.append(fr.render_list(fr.list_entries()))
    blob = "\n".join(outputs)
    for sentinel in SENTINELS:
        assert sentinel not in blob, sentinel
