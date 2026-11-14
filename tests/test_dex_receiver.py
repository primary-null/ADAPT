"""The native-receiver supervisor reads status files and starts/stops a user-built receiver.

Nothing here starts a real receiver, lists real processes, binds port 7236 or contacts a
device: process creation and process listing are replaced, status files are fabricated in
temporary directories, and killing anything fails the test.
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from typer.testing import CliRunner

from adapt_adb import dex_receiver as dr
from adapt_adb.cli import app
from adapt_adb.failure_guide import FailureGuide
from adapt_adb.help_system import HOW_TO

try:  # reuse the project's forbidden-advice list when the sibling test module is importable
    from tests.test_failure_guide import FORBIDDEN, untagged_raise_sites
except Exception:  # pragma: no cover - fallback copy of the essential patterns
    untagged_raise_sites = None
    FORBIDDEN = [re.compile(p, re.IGNORECASE) for p in (
        r"kill-server", r"\bsudo\s+(\S*/)?adb\b", r"\badb\s+root\b", r"zadig", r"bcdedit", r"testsigning",
        r"(oem|flashing)\s+unlock", r"unlock(ing)?\s+(the\s+)?bootloader", r"\bmagisk\b", r"\bsu\s+-c\b",
        r"\broot(ing)?\s+(the|your)\s+(phone|device)", r"(taskkill|pkill|killall)\b[^\n]*adb", r"chmod\s+777",
        r"\brun\b[^.\n]*\bas administrator\b", r"disable\b[^.\n]*(antivirus|security software|defender)",
    )]

ROOT = Path(__file__).resolve().parents[1]
RECEIVER = ROOT / "native" / "windows-receiver"
REAL_PORT_STATE = dr.port_state
REAL_RECEIVER_PROCESSES = dr.receiver_processes
NOW = 1_800_000_000.0
NOW_MS = int(NOW * 1000)
SENTINEL_NAME = "SENTINEL-PHONE-NAME-Quux"
SENTINEL_ID = "SENTINEL-PEER-ID-7f3a9c"
SENTINEL_ADDRESS = "192.0.2.77"
PHONE_SENTINELS = (SENTINEL_NAME, SENTINEL_ID, SENTINEL_ADDRESS)


def forbid(*args, **kwargs):
    pytest.fail("Unexpected process operation")


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """No real process, port, clock or environment reaches the code under test."""
    for name in (dr.ENV_NAME, dr.ENV_ID, dr.ENV_AUDIO, dr.ENV_DATA_DIR, dr.ENV_LOG_PEER):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(dr.subprocess, "Popen", forbid)
    monkeypatch.setattr(dr.subprocess, "run", forbid)
    monkeypatch.setattr(dr.os, "kill", forbid)
    monkeypatch.setattr(dr, "port_state", lambda port=dr.RECEIVER_PORT: "free")
    monkeypatch.setattr(dr, "_is_windows", lambda: True)
    monkeypatch.setattr(dr, "_now", lambda: NOW)
    ticks = iter(range(0, 10_000))
    monkeypatch.setattr(dr, "_clock", lambda: next(ticks) * 0.5)
    monkeypatch.setattr(dr, "_sleep", lambda seconds: None)
    monkeypatch.setattr(dr, "receiver_processes", lambda: [])


# ---------------------------------------------------------------------------
# Fabricated status files and fake processes
# ---------------------------------------------------------------------------

def doc(**changes):
    """A receiver that is advertising and waiting for a phone."""
    value = {
        "schema_version": 1, "receiver_version": "0.1.0", "process_id": 4242, "started_at_epoch_ms": NOW_MS - 5000,
        "updated_at_epoch_ms": NOW_MS, "running": True, "advertising": True, "session_active": False, "phase": "Ready",
        "phase_code": "ready", "frames": 0,
        "trace": {"attempt": 0, "attempt_active": False, "last_stage": "advertising", "observed_stages": ["starting", "advertising"]},
        "admission": {"open": True, "name_filter": False, "id_filter": False},
        "peer": {"present": False, "admitted": None, "tag": None},
        "ignored_connection_requests": 0, "last_failure": None, "last_session_end": None,
    }
    value.update(changes)
    return value


def end(code, scope="session", stage="peer_rejected", attempt=1, message="text from the receiver"):
    return {"code": code, "scope": scope, "stage": stage, "attempt": attempt, "at_epoch_ms": NOW_MS, "message": message}


def attempt(n, stages, active=False, last=None):
    return {"attempt": n, "attempt_active": active, "last_stage": last or stages[-1], "observed_stages": stages}


def write(directory: Path, value, name="status.json"):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
    return directory



def _status_with(directory, pids):
    with mock.patch.object(dr, "receiver_processes", lambda: pids):
        return dr.status(str(directory))


class FakeProcess:
    def __init__(self, pid, exit_code=None):
        self.pid, self._exit = pid, exit_code

    def poll(self):
        return self._exit

    def terminate(self):
        pytest.fail("terminate called")

    kill = send_signal = terminate

    def wait(self, *a, **k):
        pytest.fail("wait called")


def install_popen(monkeypatch, directory: Path | None, *, pid=777, status=None, exit_code=None):
    calls = []

    def popen(argv, **kwargs):
        calls.append(SimpleNamespace(argv=argv, kwargs=kwargs))
        if status is not None and directory is not None:
            value = status(pid) if callable(status) else status
            write(directory, value)
        return FakeProcess(pid, exit_code)

    monkeypatch.setattr(dr.subprocess, "Popen", popen)
    return calls


def live_doc(pid=777, **changes):
    return doc(process_id=pid, started_at_epoch_ms=NOW_MS, **changes)


@pytest.fixture
def exe(tmp_path):
    folder = tmp_path / "build"
    folder.mkdir()
    path = folder / "dexdesk.exe"
    path.write_bytes(b"MZ not a real program")
    return path


@pytest.fixture
def data(tmp_path):
    return tmp_path / "data"


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

def test_plan_is_offline_names_both_routes_and_the_security_posture():
    plan = dr.receiver_plan()  # any process operation fails the test
    assert plan["status"] == "plan_only" and plan["changes_performed"] is False and plan["runs_nothing"] is True
    assert {route["id"] for route in plan["routes"]} == {"microsoft_wireless_display", "native_receiver"}
    text = json.dumps(plan)
    for needle in ("pc-receiver-plan", "receiver-start", "receiver-status", "receiver-stop", "pc-probe", "pc-attach",
                   "7236", "build_receiver.ps1", "Windows x64", "Wi-Fi Direct", "not authentication", "Admission is open by default",
                   "%LOCALAPPDATA%", "cargo build --release --locked"):
        assert needle in text, needle
    assert plan["native_receiver"]["build"]["executable"] == r"native\windows-receiver\target\release\dexdesk.exe"
    assert "not observed here" in plan["note"]


def test_plan_toolchain_matches_the_pinned_toolchain():
    pinned = re.search(r'channel\s*=\s*"([^"]+)"', (RECEIVER / "rust-toolchain.toml").read_text(encoding="utf-8")).group(1)
    assert pinned in " ".join(dr.receiver_plan()["native_receiver"]["prerequisites"])


def test_plan_command_json_is_clean_and_pc_receiver_plan_points_at_the_second_route():
    runner = CliRunner()
    result = runner.invoke(app, ["dex", "receiver-plan", "--json"])
    assert result.exit_code == 0 and result.stderr == ""
    assert json.loads(result.stdout)["status"] == "plan_only"
    human = runner.invoke(app, ["dex", "receiver-plan"])
    assert human.exit_code == 0 and "ADAPT / EXPERIMENTAL DeX NATIVE RECEIVER" in human.stdout
    microsoft = json.loads(runner.invoke(app, ["dex", "pc-receiver-plan", "--json"]).stdout)
    assert microsoft["alternative_route"]["plan_command"] == "adapt dex receiver-plan"
    assert microsoft["feature"] == "App.WirelessDisplay.Connect~~~~0.0.1.0"  # existing content is intact


# ---------------------------------------------------------------------------
# Status: every state
# ---------------------------------------------------------------------------

def test_status_no_data_directory(tmp_path):
    report = dr.status(str(tmp_path / "missing"))
    assert report["state"] == "not_running" and report["status_file"] == "no_directory" and report["stale"] is False
    assert "does not exist" in report["summary"]
    assert dr.exit_code(report) == 0


def test_status_default_directory_is_shown_symbolically(tmp_path):
    report = dr.status()
    assert report["data_dir"] == "%LOCALAPPDATA%\\DeXDesk" and report["data_dir_source"] == "default"
    assert str(tmp_path) not in json.dumps(report)


def test_status_environment_directory_is_the_receivers_own_variable(tmp_path, monkeypatch):
    write(tmp_path / "env-dir", doc())
    monkeypatch.setenv(dr.ENV_DATA_DIR, str(tmp_path / "env-dir"))
    report = _status_with_default([4242])
    assert report["data_dir_source"] == "environment" and report["state"] == "ready"
    monkeypatch.setenv(dr.ENV_DATA_DIR, "relative\\dir")
    refused = dr.status()
    assert refused["status"] == "refused" and refused["reason"] == "data_dir_invalid" and "absolute" in refused["error"]


def _status_with_default(pids):
    with mock.patch.object(dr, "receiver_processes", lambda: pids):
        return dr.status()


def test_status_no_file(tmp_path):
    (tmp_path / "empty").mkdir()
    report = dr.status(str(tmp_path / "empty"))
    assert report["state"] == "not_running" and report["status_file"] == "missing"
    assert "no status file" in report["summary"]


def test_status_no_file_but_a_dexdesk_process_exists_is_unverified(tmp_path):
    (tmp_path / "empty").mkdir()
    report = _status_with(tmp_path / "empty", [31337])
    assert report["state"] == "unverified" and report["running_dexdesk_processes"] == [31337]
    assert dr.exit_code(report) == 2
    assert any("--data-dir" in step.get("command", "") for step in report["next_steps"])


@pytest.mark.parametrize("content", ["{not json", "[]", "", '{"schema_version": 1}', "﻿{}", json.dumps(doc(frames=-1)),
                                     json.dumps(doc(running="yes")), json.dumps(doc(process_id=True)),
                                     json.dumps(doc(trace="x")), json.dumps(doc(last_failure={"code": "BAD CODE\x1b[31m", "scope": "session", "stage": "peer_rejected", "attempt": 1})),
                                     json.dumps(doc(last_failure={"code": "port_in_use", "scope": "elsewhere", "stage": "starting", "attempt": 0}))])
def test_status_corrupt_or_malformed_file_is_unreadable_not_guessed(tmp_path, content):
    write(tmp_path / "d", content)
    report = _status_with(tmp_path / "d", [4242])
    assert report["state"] == "unreadable" and report["observed"] is None
    assert dr.exit_code(report) == 1
    assert "\x1b" not in json.dumps(report)


def test_status_wrong_schema_version(tmp_path):
    write(tmp_path / "d", doc(schema_version=2))
    report = dr.status(str(tmp_path / "d"))
    assert report["state"] == "unreadable" and report["status_file"] == "unsupported_schema"
    assert "schema version 2" in report["summary"] and report["status_schema_version"] == 2


def test_status_oversized_or_symlinked_file_is_not_trusted(tmp_path):
    write(tmp_path / "big", " " * (dr.MAX_STATUS_BYTES + 1) + json.dumps(doc()))
    assert dr.status(str(tmp_path / "big"))["state"] == "unreadable"


STATE_CASES = {
    "starting": (dict(advertising=False, phase="Starting", phase_code="starting", trace=attempt(0, ["starting"])), [4242]),
    "ready": (dict(), [4242]),
    "connecting": (dict(session_active=True, phase="Connecting(\"<redacted>\")", phase_code="connecting",
                        trace=attempt(1, ["connection_request", "peer_admitted", "wifi_direct_group"], active=True),
                        peer={"present": True, "admitted": True, "tag": "1a2b3c4d"}), [4242]),
    "streaming": (dict(session_active=True, phase="Connected", phase_code="connected", frames=1200,
                       trace=attempt(1, ["connection_request", "peer_admitted", "wifi_direct_group", "rtsp_connected", "video_negotiated",
                                         "playback_accepted", "input_channel_connected", "first_frame_decoded"], active=True)), [4242]),
    "stopping": (dict(running=False, advertising=False, phase="Stopped", phase_code="stopped"), [4242]),
    "not_running": (dict(running=False, advertising=False, phase="Stopped", phase_code="stopped"), []),
}


@pytest.mark.parametrize("state", sorted(STATE_CASES))
def test_status_reports_each_state_from_the_files_own_fields(tmp_path, state):
    changes, pids = STATE_CASES[state]
    write(tmp_path / "d", doc(**changes))
    report = _status_with(tmp_path / "d", pids)
    assert report["state"] == state and report["status"] == "observed"
    assert report["state_label"] and report["summary"] and report["claim_boundary"]
    assert dr.exit_code(report) == 0
    assert report["last_failure"] is None


def test_ready_is_not_called_a_working_desktop(tmp_path):
    write(tmp_path / "d", doc())
    report = _status_with(tmp_path / "d", [4242])
    assert "not a working desktop" in report["summary"]
    assert report["stages"] == {"scope": "before_any_phone", "observed": ["starting", "advertising"], "not_observed": [], "attempt": 0}
    assert any("pc-probe" in step.get("command", "") for step in report["next_steps"]) is False


def test_streaming_points_to_probe_and_attach_with_their_caveats(tmp_path):
    changes, pids = STATE_CASES["streaming"]
    write(tmp_path / "d", doc(**changes))
    report = _status_with(tmp_path / "d", pids)
    commands = [step.get("command", "") for step in report["next_steps"]]
    assert "adapt dex pc-probe -s SERIAL --yes" in commands
    assert "adapt dex pc-attach -s SERIAL --display DISPLAY_ID --yes" in commands
    text = " ".join(step["text"] for step in report["next_steps"])
    assert "positively identifies" in text and "different paths" in text and "separate controllable scrcpy window" in text
    assert "1200 decoded frames" in report["summary"]
    assert "does not certify input, audio or Samsung DeX" in report["claim_boundary"]
    assert report["stages"]["not_observed"] == []


def test_streaming_without_the_input_channel_says_so(tmp_path):
    write(tmp_path / "d", doc(session_active=True, phase_code="connected", frames=5,
                              trace=attempt(1, ["connection_request", "peer_admitted", "first_frame_decoded"], active=True)))
    report = _status_with(tmp_path / "d", [4242])
    assert report["state"] == "streaming"
    assert "input_channel_connected" in report["stages"]["not_observed"]
    assert any("mouse and keyboard channel" in step["text"] for step in report["next_steps"])


def test_connected_with_no_frames_is_still_connecting(tmp_path):
    write(tmp_path / "d", doc(session_active=True, phase_code="connected", frames=0, trace=attempt(1, ["connection_request"], active=True)))
    assert _status_with(tmp_path / "d", [4242])["state"] == "connecting"


def test_receiver_scope_failure_is_not_working_with_plain_explanation(tmp_path):
    write(tmp_path / "d", doc(advertising=False, phase="Error(\"x\")", phase_code="error", trace=attempt(0, ["starting"]),
                              last_failure=end("port_in_use", "receiver", "starting", 0, message=SENTINEL_NAME)))
    report = _status_with(tmp_path / "d", [4242])
    assert report["state"] == "not_working" and dr.exit_code(report) == 2
    failure = report["last_failure"]
    assert failure["code"] == "port_in_use" and failure["scope"] == "receiver" and failure["current"] is True
    assert "7236" in failure["summary"] and failure["possible_causes"] and failure["next_steps"]
    assert failure["message"] == "withheld" and SENTINEL_NAME not in json.dumps(report)
    assert report["next_steps"] == failure["next_steps"]
    human = dr.render_status(report)
    assert "port_in_use" in human and "Possible causes (not established)" in human


def test_session_failure_with_the_receiver_advertising_again_is_ready(tmp_path):
    write(tmp_path / "d", doc(phase="Error(\"x\")", phase_code="error", trace=attempt(1, ["connection_request", "peer_rejected"]),
                              peer={"present": True, "admitted": False, "tag": "deadbeef"},
                              last_failure=end("peer_rejected_name"), last_session_end=end("peer_rejected_name")))
    report = _status_with(tmp_path / "d", [4242])
    assert report["state"] == "ready" and dr.exit_code(report) == 0
    failure = report["last_failure"]
    assert failure["scope"] == "session" and failure["current"] is True and "name restriction" in failure["summary"]
    assert report["last_session_end"]["code"] == "peer_rejected_name"
    assert report["observed"]["peer"] == {"present": True, "admitted": False}  # the tag is not copied
    assert "deadbeef" not in json.dumps(report)


def test_an_old_session_failure_is_marked_as_an_earlier_attempt(tmp_path):
    write(tmp_path / "d", doc(trace=attempt(2, ["connection_request", "peer_admitted"]), last_failure=end("handshake_timeout", attempt=1, stage="rtsp_connected"),
                              last_session_end=end("peer_teardown", attempt=2, stage="peer_admitted")))
    report = _status_with(tmp_path / "d", [4242])
    assert report["last_failure"]["current"] is False
    assert report["last_session_end"]["failure"] is False  # an orderly end, not a failure
    assert "an earlier attempt" in dr.render_status(report)


def test_stale_file_whose_process_is_gone(tmp_path):
    write(tmp_path / "d", doc())
    report = _status_with(tmp_path / "d", [])
    assert report["state"] == "not_running" and report["stale"] is True and report["stale_reason"] == "process_gone"
    assert report["process"] == {"process_id": 4242, "alive": False, "checked": True}
    assert "stale" in report["summary"] and "exited without recording" in report["summary"]


def test_a_panic_leaves_next_steps_from_its_failure(tmp_path):
    write(tmp_path / "d", doc(running=True, last_failure=end("internal_panic", "receiver", "advertising", 0)))
    report = _status_with(tmp_path / "d", [])
    assert report["stale"] and report["last_failure"]["code"] == "internal_panic"
    assert any("receiver-start" in step.get("command", "") for step in report["next_steps"])


def test_live_process_with_an_old_file_is_stale_and_needs_attention(tmp_path):
    write(tmp_path / "d", doc(updated_at_epoch_ms=NOW_MS - 60_000))
    report = _status_with(tmp_path / "d", [4242])
    assert report["state"] == "stale" and report["stale"] is True and dr.exit_code(report) == 2
    assert report["updated_seconds_ago"] == 60.0


def test_a_future_dated_file_is_not_stale(tmp_path):
    write(tmp_path / "d", doc(updated_at_epoch_ms=NOW_MS + 90_000))
    assert _status_with(tmp_path / "d", [4242])["state"] == "ready"


def test_when_processes_cannot_be_listed_the_file_is_read_as_written_and_flagged(tmp_path, monkeypatch):
    write(tmp_path / "d", doc())
    monkeypatch.setattr(dr, "receiver_processes", lambda: None)
    report = dr.status(str(tmp_path / "d"))
    assert report["state"] == "ready" and report["process"]["alive"] is None and report["process"]["checked"] is False
    assert any("cannot check" in warning for warning in report["warnings"])


def test_unknown_fields_and_unknown_reason_codes_are_tolerated(tmp_path):
    value = doc(future_field={"anything": 1}, last_failure=end("brand_new_code", "session", "peer_admitted", 1))
    value["trace"] = attempt(1, ["connection_request", "peer_admitted", "some_new_stage"])
    write(tmp_path / "d", value)
    report = _status_with(tmp_path / "d", [4242])
    assert report["state"] == "ready"
    assert report["last_failure"]["known_code"] is False and "does not know this reason code" in report["last_failure"]["summary"]
    assert "some_new_stage" in report["stages"]["observed"]


def test_ignored_connection_requests_and_receiver_failure_during_a_session_are_noted(tmp_path):
    changes, pids = STATE_CASES["connecting"]
    write(tmp_path / "d", doc(**changes, ignored_connection_requests=3,
                              last_failure=end("wifi_radio_unavailable", "receiver", "starting", 0)))
    report = _status_with(tmp_path / "d", pids)
    assert report["state"] == "connecting"
    assert any("3 connection request" in w for w in report["warnings"]) and any("receiver-level failure" in w for w in report["warnings"])


def test_status_never_reads_the_log_and_never_echoes_phone_values(tmp_path, monkeypatch):
    directory = write(tmp_path / "d", doc(peer={"present": True, "admitted": True, "tag": SENTINEL_ID},
                                          last_failure=end("rtsp_protocol_error", "session", "rtsp_connected", 1, message=f"{SENTINEL_NAME} {SENTINEL_ADDRESS}")))
    (directory / "DeXDesk.log").write_text(f"peer {SENTINEL_NAME} {SENTINEL_ID} {SENTINEL_ADDRESS}", encoding="utf-8")
    reads = []
    original = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: reads.append(self.name) or original(self))
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: reads.append(self.name) or pytest.fail("read_text"))
    report = _status_with(directory, [4242])
    assert reads == ["status.json"]
    blob = json.dumps(report) + dr.render_status(report)
    assert not any(sentinel in blob for sentinel in PHONE_SENTINELS)


# ---------------------------------------------------------------------------
# Reason codes
# ---------------------------------------------------------------------------

def readme_reason_table():
    rows = re.findall(r"^\| `([a-z0-9_]+)` \| (receiver|session|either) \|", (RECEIVER / "README.md").read_text(encoding="utf-8"), re.M)
    return dict(rows)


def source_reason_codes():
    text = (RECEIVER / "src" / "adapt_trace.rs").read_text(encoding="utf-8")
    start = text.index("pub const fn as_str")
    body = text[start:text.index("\n    }\n", start)]
    return set(re.findall(r'Self::\w+ => "([a-z0-9_]+)"', body))


def test_reason_table_covers_exactly_the_codes_the_receiver_defines():
    in_source, in_readme = source_reason_codes(), readme_reason_table()
    assert len(in_source) == 33
    assert set(dr.RECEIVER_GUIDES) == in_source == set(in_readme) == set(dr.RECEIVER_REASON_SCOPES)
    assert dr.RECEIVER_REASON_SCOPES == in_readme  # the scope column too


def test_reason_guides_are_failure_guide_shaped_and_complete():
    for code, guide in dr.RECEIVER_GUIDES.items():
        assert isinstance(guide, FailureGuide)
        assert guide.id == f"dexpc.receiver.{code}" and re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){1,3}", guide.id)
        assert guide.summary.endswith(".") and guide.certainty == "observed"
        assert guide.causes and guide.steps, code
        assert guide.help_topic in HOW_TO and (ROOT / guide.docs).is_file()
        assert guide.aliases == (code,) and guide.diagnose == "adapt dex receiver-status"
        for step in guide.steps:
            assert step.effect in {"read_only", "changes_host", "changes_device", "on_phone", "manual"}, (code, step)
            command = step.command or ""
            assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", command) and not re.search(r"[A-Za-z]:\\|/home/|/Users/|~/", command), (code, command)
            assert "\n" not in command
            if command.startswith("adapt "):
                assert "SERIAL" not in command or "pc-" in command


def test_reason_guides_contain_none_of_the_forbidden_advice():
    extra = [re.compile(p, re.I) for p in (r"taskkill", r"stop-process", r"\bkill\b", r"end task", r"disable\b[^.\n]*firewall",
                                           r"turn off\b[^.\n]*firewall", r"netsh\s+advfirewall", r"\brun\b[^.\n]*\bas administrator\b",
                                           r"factory reset", r"\broot\b")]
    for code, guide in dr.RECEIVER_GUIDES.items():
        texts = [guide.summary, *(c.text for c in guide.causes)]
        for step in guide.steps:
            texts += [step.text, step.command or ""]
        for text in texts:
            for pattern in [*FORBIDDEN, *extra]:
                assert not pattern.search(text), (code, pattern.pattern, text)


def test_the_forbidden_scan_would_catch_bad_receiver_advice():
    for bad in ("taskkill /F /IM dexdesk.exe", "Stop-Process -Name dexdesk", "kill the receiver", "disable the Windows firewall"):
        assert any(pattern.search(bad) for pattern in [re.compile(p, re.I) for p in (r"taskkill", r"stop-process", r"\bkill\b", r"disable\b[^.\n]*firewall")]), bad


def test_causes_are_possibilities_and_orderly_ends_are_not_failures():
    for guide in dr.RECEIVER_GUIDES.values():
        assert not re.search(r"\bbecause\b|the cause is|this means", guide.summary, re.I)
    assert dr.ORDERLY_ENDS == {"peer_teardown", "user_stopped"}
    assert "not a failure" in dr.RECEIVER_GUIDES["peer_teardown"].summary and "not a failure" in dr.RECEIVER_GUIDES["user_stopped"].summary
    for code in ("peer_teardown", "user_stopped"):
        assert dr.RECEIVER_REASON_SCOPES[code] == "session"


def test_explain_code_never_includes_the_receivers_message_text():
    block = dr.explain_code("port_in_use", "receiver", "starting", 0, current=True)
    assert block["message"] == "withheld" and block["known_code"] and block["possible_causes"]
    assert dr.explain_code("nope", "session", "starting", 3, current=False)["known_code"] is False


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------

def test_start_refuses_without_yes_and_says_what_starting_does(exe, data):
    report = dr.start(str(exe), data_dir=str(data))
    assert report["status"] == "refused" and report["reason"] == "yes_required" and report["changes_performed"] is False
    for needle in ("Wi-Fi Direct", "7236", "--yes"):
        assert needle in report["error"]
    assert not data.exists()


def test_start_refuses_on_a_non_windows_platform(exe, monkeypatch):
    monkeypatch.setattr(dr, "_is_windows", lambda: False)
    report = dr.start(str(exe), yes=True)
    assert report["reason"] == "not_windows" and "Windows program" in report["error"]
    result = CliRunner().invoke(app, ["dex", "receiver-start", "--exe", str(exe), "--yes", "--json"])
    assert result.exit_code == 1 and json.loads(result.stdout)["reason"] == "not_windows"


def test_start_refuses_a_missing_or_wrongly_named_executable_and_never_searches(tmp_path, exe, monkeypatch):
    other = exe.with_name("other.exe")
    other.write_bytes(b"MZ")
    assert dr.start(str(other), yes=True)["reason"] == "exe_name"
    assert dr.start(str(tmp_path / "nowhere" / "dexdesk.exe"), yes=True)["reason"] == "exe_missing"
    folder = tmp_path / "folder" / "dexdesk.exe"
    folder.mkdir(parents=True)
    assert dr.start(str(folder), yes=True)["reason"] == "exe_missing"
    monkeypatch.chdir(exe.parent)
    monkeypatch.setattr("shutil.which", forbid)
    monkeypatch.setenv("PATH", str(exe.parent))
    monkeypatch.chdir(tmp_path)
    report = dr.start("dexdesk.exe", yes=True)  # exists on PATH and elsewhere, but is not searched for
    assert report["reason"] == "exe_missing"


def test_start_refuses_when_a_receiver_is_already_running(exe, data, monkeypatch):
    monkeypatch.setattr(dr, "receiver_processes", lambda: [999, 1000])
    report = dr.start(str(exe), data_dir=str(data), yes=True)
    assert report["reason"] == "already_running" and "999" in report["error"] and "1 more" in report["error"]
    assert "single-instance" in report["error"] and report["process_ids"] == [999, 1000]


def test_start_refuses_when_a_fresh_status_says_running_and_processes_cannot_be_listed(exe, data, monkeypatch):
    write(data, doc())
    monkeypatch.setattr(dr, "receiver_processes", lambda: None)
    assert dr.start(str(exe), data_dir=str(data), yes=True)["reason"] == "already_running"


@pytest.mark.parametrize("state", ["in_use", "denied"])
def test_start_refuses_when_the_port_is_busy_and_says_which_check_failed(exe, data, monkeypatch, state):
    monkeypatch.setattr(dr, "port_state", lambda port=7236: state)
    report = dr.start(str(exe), data_dir=str(data), yes=True)
    assert report["reason"] == "port_in_use" and "7236" in report["error"] and "no receiver of this data directory is running" in report["error"]
    assert "Wireless Display" in report["error"]


@pytest.mark.parametrize("option,value", [("expect_name", ""), ("expect_name", "a\nb"), ("expect_name", "x" * 257), ("expect_name", "\x00"),
                                          ("expect_id", ""), ("expect_id", "y" * 1025), ("expect_id", "tab\there"), ("expect_name", "\ud800")])
def test_start_refuses_restriction_values_the_receiver_would_refuse(exe, data, option, value):
    report = dr.start(str(exe), data_dir=str(data), yes=True, **{option: value})
    assert report["reason"] == "restriction_invalid" and (value == "" or value not in report["error"])


def test_start_checks_inherited_restrictions_too(exe, data, monkeypatch):
    report = dr.start(str(exe), data_dir=str(data), yes=True, environ={dr.ENV_NAME: "bad\nname"})
    assert report["reason"] == "restriction_invalid" and "environment" in report["error"] and "bad" not in report["error"]


def test_limits_are_measured_in_bytes_like_the_receiver():
    assert dr._valid_value(dr.ENV_NAME, "é" * 128, dr.NAME_LIMIT_BYTES) is None
    assert "longer" in dr._valid_value(dr.ENV_NAME, "é" * 129, dr.NAME_LIMIT_BYTES)


def test_start_passes_settings_only_through_the_environment(exe, data, monkeypatch):
    calls = install_popen(monkeypatch, data, status=lambda pid: live_doc(pid, admission={"open": False, "name_filter": True, "id_filter": True}))
    report = dr.start(str(exe), data_dir=str(data), expect_name=SENTINEL_NAME, expect_id=SENTINEL_ID, audio=True, yes=True)
    assert report["status"] == "started"
    (call,) = calls
    assert call.argv == [str(exe)]                                    # nothing but the executable on the command line
    assert not any(sentinel in str(call.argv) for sentinel in PHONE_SENTINELS)
    env = call.kwargs["env"]
    assert env[dr.ENV_NAME] == SENTINEL_NAME and env[dr.ENV_ID] == SENTINEL_ID and env[dr.ENV_AUDIO] == "1"
    assert env[dr.ENV_DATA_DIR] == str(data)
    assert not call.kwargs.get("shell")
    assert call.kwargs["stdin"] is subprocess.DEVNULL and call.kwargs["stdout"] is subprocess.DEVNULL and call.kwargs["stderr"] is subprocess.DEVNULL
    assert call.kwargs["creationflags"] & dr.DETACHED_PROCESS
    kwargs_without_env = {k: v for k, v in call.kwargs.items() if k != "env"}
    assert not any(sentinel in repr(kwargs_without_env) for sentinel in PHONE_SENTINELS)
    assert not any(sentinel in json.dumps(report) for sentinel in PHONE_SENTINELS)  # reported as booleans/sources only
    assert report["request"]["restrictions"] == {"name": "option", "id": "option"}
    assert "not authentication" in report["admission_note"] and "audio" in report["audio_note"].lower()


def test_start_without_options_leaves_the_environment_alone_and_says_admission_is_open(exe, data, monkeypatch):
    calls = install_popen(monkeypatch, data, status=lambda pid: live_doc(pid))
    report = dr.start(str(exe), data_dir=str(data), yes=True)
    env = calls[0].kwargs["env"]
    assert dr.ENV_NAME not in env and dr.ENV_ID not in env and dr.ENV_AUDIO not in env
    assert report["status"] == "started" and report["state"] == "ready" and report["admission_note"].startswith("Open admission is in effect")
    assert report["admission_reported_by_receiver"]["open"] is True
    assert "7236" in report["listener"] and "no firewall rule" in report["listener"]
    assert "not a working desktop" in report["claim_boundary"]
    commands = [step.get("command") for step in report["next_steps"]]
    assert "adapt dex receiver-status" in commands and "adapt dex receiver-stop --yes" in commands



def test_start_keeps_an_inherited_restriction_and_reports_its_source(exe, data, monkeypatch):
    calls = install_popen(monkeypatch, data, status=lambda pid: live_doc(pid, admission={"open": False, "name_filter": True, "id_filter": False}))
    report = dr.start(str(exe), data_dir=str(data), yes=True, environ={dr.ENV_NAME: SENTINEL_NAME, "PATH": "x"})
    assert calls[0].kwargs["env"][dr.ENV_NAME] == SENTINEL_NAME
    assert report["request"]["restrictions"] == {"name": "environment", "id": None}
    assert "not authentication" in report["admission_note"] and SENTINEL_NAME not in json.dumps(report)


def test_start_warns_when_the_log_would_hold_phone_details(exe, data, monkeypatch):
    install_popen(monkeypatch, data, status=lambda pid: live_doc(pid))
    monkeypatch.setenv(dr.ENV_LOG_PEER, "1")
    report = dr.start(str(exe), data_dir=str(data), yes=True)
    assert any("ADAPT_DEX_LOG_PEER_DETAILS" in warning for warning in report["warnings"])


def test_start_reports_a_startup_failure_read_from_the_status_file(exe, data, monkeypatch):
    install_popen(monkeypatch, data, status=lambda pid: live_doc(pid, advertising=False, phase="Error(\"x\")", phase_code="error",
                                                                 trace=attempt(0, ["starting"]), last_failure=end("port_in_use", "receiver", "starting", 0)))
    result = CliRunner().invoke(app, ["dex", "receiver-start", "--exe", str(exe), "--data-dir", str(data), "--yes", "--json"])
    report = json.loads(result.stdout)
    assert result.exit_code == 1 and result.stderr == ""
    assert report["status"] == "failed" and report["last_failure"]["code"] == "port_in_use"
    assert "7236" in report["summary"] and report["process_still_running"] is True
    assert "receiver-stop" in json.dumps(report["next_steps"])
    assert "ADAPT does not end the receiver" in report["note"]


def test_start_reports_an_immediate_exit_without_state(exe, data, monkeypatch):
    install_popen(monkeypatch, data, exit_code=0)
    report = dr.start(str(exe), data_dir=str(data), yes=True)
    assert report["status"] == "failed" and report["exit_code"] == 0 and report["state"] == "not_running"
    assert "another instance" in report["summary"]
    install_popen(monkeypatch, data, exit_code=1)
    report = dr.start(str(exe), data_dir=str(data), yes=True)
    assert report["exit_code"] == 1 and "message box" in report["summary"] and report["process_still_running"] is False


def test_start_without_a_status_within_the_wait_is_pending_and_leaves_the_process_alone(exe, data, monkeypatch):
    install_popen(monkeypatch, data)  # FakeProcess fails the test if anything tries to end it
    report = dr.start(str(exe), data_dir=str(data), yes=True, wait_seconds=1)
    assert report["status"] == "start_pending" and dr.exit_code(report) == 2 and report["process_still_running"] is True
    assert "message box" in report["summary"]
    result = CliRunner().invoke(app, ["dex", "receiver-start", "--exe", str(exe), "--data-dir", str(data), "--yes", "--json"])
    assert result.exit_code == 2 and json.loads(result.stdout)["status"] == "start_pending"


def test_start_ignores_a_status_file_left_by_another_process(exe, data, monkeypatch):
    write(data, doc(process_id=111))
    install_popen(monkeypatch, data, status=None)
    report = dr.start(str(exe), data_dir=str(data), yes=True, wait_seconds=1)
    assert report["status"] == "start_pending"


def test_start_ignores_a_file_that_predates_the_launch_even_with_a_reused_pid(exe, data, monkeypatch):
    install_popen(monkeypatch, data, status=lambda pid: doc(process_id=pid, started_at_epoch_ms=NOW_MS - 3_600_000))
    assert dr.start(str(exe), data_dir=str(data), yes=True, wait_seconds=1)["status"] == "start_pending"


def test_start_cli_human_output_and_exit_code(exe, data, monkeypatch):
    install_popen(monkeypatch, data, status=lambda pid: live_doc(pid))
    result = CliRunner().invoke(app, ["dex", "receiver-start", "--exe", str(exe), "--data-dir", str(data), "--yes"])
    assert result.exit_code == 0
    for needle in ("ADAPT / EXPERIMENTAL DeX NATIVE RECEIVER", "Start: started", "Open admission is in effect", "TCP 7236"):
        assert needle in result.stdout
    assert str(data) in result.stdout  # a path the user supplied is echoed as supplied


def test_start_cli_requires_the_exe_option():
    result = CliRunner().invoke(app, ["dex", "receiver-start", "--yes"])
    assert result.exit_code == 2


def test_launch_failure_is_a_refusal_without_the_path_in_the_error(exe, data, monkeypatch):
    def broken(argv, **kwargs):
        raise OSError(193, "%1 is not a valid Win32 application", str(exe))
    monkeypatch.setattr(dr.subprocess, "Popen", broken)
    report = dr.start(str(exe), data_dir=str(data), yes=True)
    assert report["reason"] == "launch_failed" and "not a valid Win32 application" in report["error"] and str(exe.parent) not in report["error"]


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------

def make_stop_world(monkeypatch, directory, *, pid=4242, first=None, then=None, final=None):
    """receiver_processes returns `first` once, then (after writing `final`) `then` forever."""
    state = {"calls": 0}

    def listing():
        state["calls"] += 1
        if state["calls"] == 1:
            return list(first if first is not None else [pid])
        if state["calls"] == 2:
            state["request_present_while_running"] = (directory / "stop.request").is_file()
        if final is not None and state["calls"] == 2:
            write(directory, final)
        return list(then if then is not None else [])
    monkeypatch.setattr(dr, "receiver_processes", listing)
    return state


def test_stop_refuses_without_yes_and_on_other_platforms(data, monkeypatch):
    write(data, doc())
    assert dr.stop(str(data))["reason"] == "yes_required" and not (data / "stop.request").exists()
    monkeypatch.setattr(dr, "_is_windows", lambda: False)
    assert dr.stop(str(data), yes=True)["reason"] == "not_windows"


def test_stop_does_not_write_when_it_cannot_tell_the_process_is_alive(data, monkeypatch):
    write(data, doc())
    monkeypatch.setattr(dr, "receiver_processes", lambda: None)
    report = dr.stop(str(data), yes=True)
    assert report["reason"] == "process_check_unavailable" and not (data / "stop.request").exists()


def test_stop_writes_the_request_only_for_a_live_process_and_reports_the_final_state(data, monkeypatch):
    write(data, doc(session_active=True, phase_code="connected", frames=10, trace=attempt(1, ["connection_request"], active=True)))
    final = doc(running=False, advertising=False, session_active=False, phase="Stopped", phase_code="stopped",
                trace=attempt(1, ["connection_request"]), last_session_end=end("user_stopped", "session", "connection_request", 1))
    state = make_stop_world(monkeypatch, data, final=final)
    result = CliRunner().invoke(app, ["dex", "receiver-stop", "--data-dir", str(data), "--yes", "--json"])
    report = json.loads(result.stdout)
    assert result.exit_code == 0 and result.stderr == ""
    assert report["status"] == "stopped" and report["stop_request_written"] is True and report["process_id"] == 4242
    assert state["request_present_while_running"] is True      # it was in place while the receiver was still running
    assert report["stop_request_removed"] is True and not (data / "stop.request").exists()
    assert report["shutdown_recorded"] is True and report["final_status"] == "recorded_stopped"
    assert report["last_session_end"]["code"] == "user_stopped"
    assert "never ends a process by ID" in report["claim_boundary"]
    assert state["calls"] >= 2


def test_stop_reports_a_receiver_that_exited_without_recording_a_shutdown(data, monkeypatch):
    write(data, doc())
    make_stop_world(monkeypatch, data, final=doc())  # the file still says running
    report = dr.stop(str(data), yes=True)
    assert report["status"] == "stopped" and report["shutdown_recorded"] is False and report["final_status"] == "not_recorded"
    assert "does not record a completed shutdown" in report["summary"]


def test_stop_times_out_clearly_and_leaves_the_request_in_place(data, monkeypatch):
    write(data, doc())
    make_stop_world(monkeypatch, data, then=[4242])  # never exits
    result = CliRunner().invoke(app, ["dex", "receiver-stop", "--data-dir", str(data), "--yes", "--json"])
    report = json.loads(result.stdout)
    assert result.exit_code == 2
    assert report["status"] == "stop_pending" and report["process_still_running"] is True and report["state"] == "stopping"
    assert (data / "stop.request").is_file()
    assert "never ends a process by ID" in json.dumps(report)
    assert "Task Manager" in json.dumps(report["next_steps"])


def test_stop_writes_nothing_when_the_status_files_process_is_gone(data, monkeypatch):
    write(data, doc())
    monkeypatch.setattr(dr, "receiver_processes", lambda: [])
    report = dr.stop(str(data), yes=True)
    assert report["status"] == "nothing_running" and report["stale"] is True and report["stop_request_written"] is False
    assert not (data / "stop.request").exists()


def test_stop_with_no_status_file_and_nothing_running_says_so(tmp_path):
    assert dr.stop(str(tmp_path / "none"), yes=True)["status"] == "nothing_running"
    (tmp_path / "empty").mkdir()
    report = dr.stop(str(tmp_path / "empty"), yes=True)
    assert report["status"] == "nothing_running" and "No receiver is running" in report["summary"]
    assert not (tmp_path / "empty" / "stop.request").exists()


def test_stop_refuses_to_write_for_a_process_that_has_no_status_here(data, monkeypatch):
    data.mkdir()
    monkeypatch.setattr(dr, "receiver_processes", lambda: [555])
    report = dr.stop(str(data), yes=True)
    assert report["reason"] == "no_usable_status" and "555" in report["error"] and "--data-dir" in report["error"]
    assert not (data / "stop.request").exists()


def test_stop_leaves_another_receiver_alone(data, monkeypatch):
    write(data, doc())
    monkeypatch.setattr(dr, "receiver_processes", lambda: [555])  # someone else's dexdesk, not the one in this file
    report = dr.stop(str(data), yes=True)
    assert report["status"] == "nothing_running" and report["other_dexdesk_processes"] == 1
    assert not (data / "stop.request").exists()


def test_stop_does_not_add_a_request_while_the_receiver_is_already_shutting_down(data, monkeypatch):
    write(data, doc(running=False, advertising=False, phase="Stopped", phase_code="stopped"))
    make_stop_world(monkeypatch, data)
    report = dr.stop(str(data), yes=True)
    assert report["status"] == "stopped" and report["stop_request_written"] is False


def test_stop_accepts_a_request_that_is_already_there(data, monkeypatch):
    write(data, doc())
    (data / "stop.request").write_text("stop", encoding="ascii")
    make_stop_world(monkeypatch, data)
    report = dr.stop(str(data), yes=True)
    assert report["status"] == "stopped" and report["stop_request_written"] is False and report["stop_request_already_present"] is True
    assert report["stop_request_removed"] is True


def test_stop_refuses_when_something_that_is_not_a_file_blocks_the_request(data, monkeypatch):
    write(data, doc())
    (data / "stop.request").mkdir()
    monkeypatch.setattr(dr, "receiver_processes", lambda: [4242])
    assert dr.stop(str(data), yes=True)["reason"] == "stop_request_blocked"


def test_the_module_never_calls_a_kill_function_and_adds_no_untagged_raise():
    tree = ast.parse((ROOT / "src" / "adapt_adb" / "dex_receiver.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in {"kill", "terminate", "killpg", "send_signal", "TerminateProcess"}, node.attr
        if isinstance(node, ast.Name):
            assert node.id not in {"TerminateProcess", "killpg"}
    if untagged_raise_sites is not None:
        assert [site for site in untagged_raise_sites() if "dex_receiver" in site] == []


# ---------------------------------------------------------------------------
# Process listing and port probe (the real functions, with process creation faked)
# ---------------------------------------------------------------------------

def test_receiver_processes_parses_tasklist_csv_and_asks_only_for_dexdesk(tmp_path, monkeypatch):
    system32 = tmp_path / "System32"
    system32.mkdir()
    (system32 / "tasklist.exe").write_bytes(b"MZ")
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    seen = []

    def fake_run(argv, **kwargs):
        seen.append(argv)
        out = '"dexdesk.exe","4242","Console","1","52,000 K"\n"DEXDESK.EXE","77","Console","1","1 K"\nINFO: No tasks\n"other.exe","5","x","1","1 K"\n'
        return subprocess.CompletedProcess(argv, 0, out, "")
    monkeypatch.setattr(dr.subprocess, "run", fake_run)
    assert REAL_RECEIVER_PROCESSES() == [4242, 77]
    assert seen[0][0].endswith("tasklist.exe") and "IMAGENAME eq dexdesk.exe" in seen[0] and "/V" not in seen[0]
    monkeypatch.setattr(dr.subprocess, "run", lambda argv, **k: subprocess.CompletedProcess(argv, 1, "", "denied"))
    assert REAL_RECEIVER_PROCESSES() is None
    monkeypatch.setattr(dr, "_is_windows", lambda: False)
    monkeypatch.setattr(dr.subprocess, "run", forbid)
    assert REAL_RECEIVER_PROCESSES() is None


def test_port_probe_binds_without_listening_and_detects_a_held_port():
    import socket
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("0.0.0.0", 0))
    port = holder.getsockname()[1]
    try:
        assert REAL_PORT_STATE(port) == "in_use"
    finally:
        holder.close()
    assert REAL_PORT_STATE(port) == "free"


# ---------------------------------------------------------------------------
# Privacy and wiring
# ---------------------------------------------------------------------------

def test_no_command_prints_phone_values_or_the_default_account_path(tmp_path, exe, monkeypatch):
    directory = write(tmp_path / "default-free", doc(peer={"present": True, "admitted": True, "tag": SENTINEL_ID},
                                                    last_failure=end("rtsp_protocol_error", "session", "rtsp_connected", 1, message=f"{SENTINEL_NAME} {SENTINEL_ADDRESS}")))
    (directory / "DeXDesk.log").write_text(" ".join(PHONE_SENTINELS), encoding="utf-8")
    monkeypatch.setenv(dr.ENV_NAME, SENTINEL_NAME)
    monkeypatch.setenv(dr.ENV_ID, SENTINEL_ID)
    write(tmp_path / "local" / "DeXDesk", doc(last_failure=end("peer_rejected_name", message=SENTINEL_NAME)))
    runner = CliRunner()
    outputs = []
    monkeypatch.setattr(dr, "receiver_processes", lambda: [4242])
    for args in (["receiver-plan"], ["receiver-plan", "--json"], ["receiver-status"], ["receiver-status", "--json"],
                 ["receiver-status", "--data-dir", str(directory)], ["receiver-status", "--data-dir", str(directory), "--json"],
                 ["receiver-stop"], ["receiver-stop", "--yes"], ["receiver-stop", "--yes", "--json"]):
        result = runner.invoke(app, ["dex", *args])
        outputs.append(result.stdout + result.stderr)
    install_popen(monkeypatch, tmp_path / "local" / "DeXDesk", status=lambda pid: live_doc(pid))
    monkeypatch.setattr(dr, "receiver_processes", lambda: [])
    for args in (["--exe", str(exe), "--yes"], ["--exe", str(exe), "--yes", "--json"], ["--exe", str(exe)]):
        result = runner.invoke(app, ["dex", "receiver-start", *args])
        outputs.append(result.stdout + result.stderr)
    for text in outputs:
        assert not any(sentinel in text for sentinel in PHONE_SENTINELS), text[:200]
        assert str(tmp_path / "local") not in text  # the default directory is shown symbolically


def test_the_status_command_reports_each_exit_code(tmp_path, monkeypatch):
    runner = CliRunner()
    write(tmp_path / "ok", doc())
    write(tmp_path / "bad", "{")
    write(tmp_path / "broken", doc(advertising=False, phase_code="error", last_failure=end("wifi_radio_unavailable", "receiver", "starting", 0)))
    monkeypatch.setattr(dr, "receiver_processes", lambda: [4242])
    assert runner.invoke(app, ["dex", "receiver-status", "--data-dir", str(tmp_path / "ok"), "--json"]).exit_code == 0
    assert runner.invoke(app, ["dex", "receiver-status", "--data-dir", str(tmp_path / "bad"), "--json"]).exit_code == 1
    result = runner.invoke(app, ["dex", "receiver-status", "--data-dir", str(tmp_path / "broken"), "--json"])
    assert result.exit_code == 2 and result.stderr == "" and json.loads(result.stdout)["state"] == "not_working"
    human = runner.invoke(app, ["dex", "receiver-status", "--data-dir", str(tmp_path / "ok")])
    assert human.exit_code == 0 and "State: ready for a phone" in human.stdout and "Data directory:" in human.stdout


def test_commands_are_registered_with_help_topics_and_hints():
    help_text = CliRunner().invoke(app, ["dex", "--help"]).stdout
    for name in ("receiver-plan", "receiver-status", "receiver-start", "receiver-stop"):
        assert name in help_text
        topic = HOW_TO[f"dex {name}"]
        assert len(topic) == 3 and f"adapt dex {name}" in topic[1]
    inventory = json.loads(CliRunner().invoke(app, ["help-me", "--all", "--json"]).stdout)["commands"]
    listed = {row["command"]: row for row in inventory}
    for name in ("receiver-plan", "receiver-status", "receiver-start", "receiver-stop"):
        assert listed[f"adapt dex {name}"]["how_to"]
    topic = json.loads(CliRunner().invoke(app, ["help-me", "dex receiver-start", "--json"]).stdout)
    assert "Wi-Fi Direct" in " ".join(topic["how_to"])


def test_the_build_script_is_local_only_and_has_a_dry_run():
    raw = (ROOT / "scripts" / "build_receiver.ps1").read_bytes()
    assert all(byte < 128 for byte in raw), "PowerShell 5.1 reads BOM-less files as ANSI"
    text = raw.decode("ascii")
    for needle in ("--locked", "SupportsShouldProcess", "[switch]$DryRun", r".cargo\bin", "Get-FileHash", "SHA256", "resource_stamp", "--offline"):
        assert needle in text, needle
    for forbidden in ("Invoke-WebRequest", "Invoke-RestMethod", "DownloadFile", "Start-BitsTransfer", "winget", "curl ", "wget ", "git push", "gh ", "Start-Process", "& $exe"):
        assert forbidden not in text, forbidden
    assert "does not download anything itself" in text
