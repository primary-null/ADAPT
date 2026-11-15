"""Failure guidance for `adapt dex receiver-plan`, `receiver-status`, `receiver-start` and `receiver-stop`.

Every outcome that is not a success names a failure guide: after the unchanged report on
stdout, the human form adds the shared guidance on stderr (the one-line `More:` pointer when
stderr is not a terminal, the Why/Try block when it is), and `--json` adds the shared `failure`
object. A report that exits 0 but records a failure gets the pointer only.

Offline: no receiver is started, no process is listed or ended, port 7236 is never bound and
no device or adb is involved. Process creation, process listing, the port probe and the clock
are replaced, and status files are fabricated in temporary folders.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from adapt_adb import dex_receiver as dr
from adapt_adb import failure_guide as fg
from adapt_adb import failure_report as fr
from adapt_adb.cli import app
from tests.test_dex_receiver import (NOW, NOW_MS, attempt, doc, end, forbid, install_popen, live_doc, make_stop_world,
                                     write)

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[1]
NATIVE = "dexpc.native_receiver."
PHONE_TEXT = "SENTINEL-PHONE-TEXT-Zork"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for name in (dr.ENV_NAME, dr.ENV_ID, dr.ENV_AUDIO, dr.ENV_DATA_DIR, dr.ENV_LOG_PEER,
                 "ADAPT_TIPS", "ADAPT_FAILURE_GUIDANCE", "ADAPT_DEBUG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ADAPT_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(dr.subprocess, "Popen", forbid)
    monkeypatch.setattr(dr.subprocess, "run", forbid)
    monkeypatch.setattr(dr.os, "kill", forbid)
    monkeypatch.setattr(dr, "port_state", lambda port=dr.RECEIVER_PORT: "free")
    monkeypatch.setattr(dr, "_is_windows", lambda: True)
    monkeypatch.setattr(dr, "_now", lambda: NOW)
    ticks = iter(range(0, 100_000))
    monkeypatch.setattr(dr, "_clock", lambda: next(ticks) * 0.5)
    monkeypatch.setattr(dr, "_sleep", lambda seconds: None)
    monkeypatch.setattr(dr, "receiver_processes", lambda: [])


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


def _processes(monkeypatch, pids):
    monkeypatch.setattr(dr, "receiver_processes", lambda: None if pids is None else list(pids))


def _status(data):
    return ["dex", "receiver-status", "--data-dir", str(data)]


def _start(exe, data, *extra):
    return ["dex", "receiver-start", "--exe", str(exe), "--data-dir", str(data), "--yes", *extra]


def _stop(data):
    return ["dex", "receiver-stop", "--data-dir", str(data), "--yes"]


def _failed_receiver(code, scope="receiver", stage="advertising", attempt_number=0):
    return doc(advertising=False, phase_code="error", trace=attempt(attempt_number, ["starting", "advertising"]),
               last_failure=end(code, scope, stage, attempt_number, message=PHONE_TEXT))


# ---------------------------------------------------------------------------
# Scenarios: each sets up one outcome and returns the command line.
# ---------------------------------------------------------------------------

def status_data_dir_invalid(mp, tmp, exe, data):
    mp.setenv(dr.ENV_DATA_DIR, "relative-folder")
    return ["dex", "receiver-status"]


def status_unreadable(mp, tmp, exe, data):
    write(data, "{ not json")
    return _status(data)


def status_schema(mp, tmp, exe, data):
    write(data, doc(schema_version=2))
    return _status(data)


def status_unverified(mp, tmp, exe, data):
    data.mkdir()
    _processes(mp, [555])
    return _status(data)


def status_stale(mp, tmp, exe, data):
    write(data, doc(updated_at_epoch_ms=NOW_MS - 60_000))
    _processes(mp, [4242])
    return _status(data)


def status_not_working(mp, tmp, exe, data):
    write(data, _failed_receiver("wifi_radio_unavailable"))
    _processes(mp, [4242])
    return _status(data)


def status_not_working_unknown_code(mp, tmp, exe, data):
    write(data, _failed_receiver("brand_new_reason"))
    _processes(mp, [4242])
    return _status(data)


def status_exited_unrecorded(mp, tmp, exe, data):
    write(data, doc())
    return _status(data)


def status_exited_after_panic(mp, tmp, exe, data):
    write(data, doc(last_failure=end("internal_panic", "receiver", "advertising", 0, message=PHONE_TEXT)))
    return _status(data)


def status_ready_after_a_refused_phone(mp, tmp, exe, data):
    rejected = end("peer_rejected_name", "session", "peer_rejected", 1, message=PHONE_TEXT)
    write(data, doc(trace=attempt(1, ["starting", "advertising", "connection_request", "peer_rejected"]),
                    last_failure=rejected, last_session_end=rejected))
    _processes(mp, [4242])
    return _status(data)


def start_not_windows(mp, tmp, exe, data):
    mp.setattr(dr, "_is_windows", lambda: False)
    return _start(exe, data)


def start_wrong_name(mp, tmp, exe, data):
    other = exe.parent / "other.exe"
    other.write_bytes(b"MZ")
    return _start(other, data)


def start_missing_exe(mp, tmp, exe, data):
    return _start(tmp / "not-built" / "dexdesk.exe", data)


def start_data_dir_is_a_file(mp, tmp, exe, data):
    data.write_text("a file", encoding="utf-8")
    return _start(exe, data)


def start_restriction_invalid(mp, tmp, exe, data):
    return _start(exe, data, "--expect-name", "n" * 300)


def start_without_yes(mp, tmp, exe, data):
    return ["dex", "receiver-start", "--exe", str(exe), "--data-dir", str(data)]


def start_already_running(mp, tmp, exe, data):
    _processes(mp, [999])
    return _start(exe, data)


def start_port_busy(mp, tmp, exe, data):
    mp.setattr(dr, "port_state", lambda port=dr.RECEIVER_PORT: "in_use")
    return _start(exe, data)


def start_launch_failed(mp, tmp, exe, data):
    def broken(argv, **kwargs):
        raise OSError(193, "not a valid application", str(exe))
    mp.setattr(dr.subprocess, "Popen", broken)
    return _start(exe, data)


def start_recorded_failure(mp, tmp, exe, data):
    install_popen(mp, data, status=lambda pid: live_doc(pid, advertising=False, phase_code="error", trace=attempt(0, ["starting"]),
                                                        last_failure=end("port_in_use", "receiver", "starting", 0, message=PHONE_TEXT)))
    return _start(exe, data)


def start_exited_early(mp, tmp, exe, data):
    install_popen(mp, data, exit_code=1)
    return _start(exe, data)


def start_pending(mp, tmp, exe, data):
    install_popen(mp, data)
    return _start(exe, data)


def stop_not_windows(mp, tmp, exe, data):
    mp.setattr(dr, "_is_windows", lambda: False)
    return _stop(data)


def stop_data_dir_invalid(mp, tmp, exe, data):
    mp.setenv(dr.ENV_DATA_DIR, "relative-folder")
    return ["dex", "receiver-stop", "--yes"]


def stop_without_yes(mp, tmp, exe, data):
    write(data, doc())
    return ["dex", "receiver-stop", "--data-dir", str(data)]


def stop_process_check_unavailable(mp, tmp, exe, data):
    write(data, doc())
    _processes(mp, None)
    return _stop(data)


def stop_no_usable_status(mp, tmp, exe, data):
    data.mkdir()
    _processes(mp, [555])
    return _stop(data)


def stop_request_blocked(mp, tmp, exe, data):
    write(data, doc())
    (data / "stop.request").mkdir()
    _processes(mp, [4242])
    return _stop(data)


def stop_request_failed(mp, tmp, exe, data):
    write(data, doc())
    _processes(mp, [4242])

    def denied(*args, **kwargs):
        raise PermissionError(13, "Access is denied")
    mp.setattr(dr, "open", denied, raising=False)  # the module's own open(); nothing else is affected
    return _stop(data)


def stop_pending(mp, tmp, exe, data):
    write(data, doc())
    make_stop_world(mp, data, then=[4242])
    return _stop(data)


def stop_exited_unrecorded(mp, tmp, exe, data):
    write(data, doc())
    make_stop_world(mp, data, final=doc())  # the file still says running after the process is gone
    return _stop(data)


def stop_found_a_stale_file(mp, tmp, exe, data):
    write(data, doc())
    return _stop(data)


# (scenario, expected guide ID, expected exit code)
SCENARIOS = [
    (status_data_dir_invalid, NATIVE + "data_dir_invalid", 1),
    (status_unreadable, NATIVE + "status_unreadable", 1),
    (status_schema, NATIVE + "status_schema_unsupported", 1),
    (status_unverified, NATIVE + "status_missing", 2),
    (status_stale, NATIVE + "status_stale", 2),
    (status_not_working, "dexpc.receiver.wifi_radio_unavailable", 2),
    (status_not_working_unknown_code, NATIVE + "unknown_reason", 2),
    (status_exited_unrecorded, NATIVE + "exited_unrecorded", 0),
    (status_exited_after_panic, "dexpc.receiver.internal_panic", 0),
    (status_ready_after_a_refused_phone, "dexpc.receiver.peer_rejected_name", 0),
    (start_not_windows, NATIVE + "not_windows", 1),
    (start_wrong_name, NATIVE + "exe_not_found", 1),
    (start_missing_exe, NATIVE + "exe_not_found", 1),
    (start_data_dir_is_a_file, NATIVE + "data_dir_invalid", 1),
    (start_restriction_invalid, NATIVE + "restriction_invalid", 1),
    (start_without_yes, NATIVE + "confirmation_required", 1),
    (start_already_running, NATIVE + "already_running", 1),
    (start_port_busy, NATIVE + "port_busy", 1),
    (start_launch_failed, NATIVE + "launch_failed", 1),
    (start_recorded_failure, "dexpc.receiver.port_in_use", 1),
    (start_exited_early, NATIVE + "exited_early", 1),
    (start_pending, NATIVE + "start_timeout", 2),
    (stop_not_windows, NATIVE + "not_windows", 1),
    (stop_data_dir_invalid, NATIVE + "data_dir_invalid", 1),
    (stop_without_yes, NATIVE + "confirmation_required", 1),
    (stop_process_check_unavailable, NATIVE + "process_check_unavailable", 1),
    (stop_no_usable_status, NATIVE + "status_missing", 1),
    (stop_request_blocked, NATIVE + "stop_request_failed", 1),
    (stop_request_failed, NATIVE + "stop_request_failed", 1),
    (stop_pending, NATIVE + "stop_unconfirmed", 2),
    (stop_exited_unrecorded, NATIVE + "exited_unrecorded", 0),
    (stop_found_a_stale_file, NATIVE + "exited_unrecorded", 0),
]
IDS = [scenario.__name__ for scenario, _, _ in SCENARIOS]


def _record(monkeypatch):
    """Wrap the module's operations so a test sees the exact report the command printed."""
    seen: list[dict] = []
    for name in ("status", "start", "stop", "receiver_plan"):
        original = getattr(dr, name)

        def wrapper(*args, _original=original, **kwargs):
            report = _original(*args, **kwargs)
            seen.append(report)
            return report
        monkeypatch.setattr(dr, name, wrapper)
    return seen


def _render(report):
    if report["status"] == "refused":
        return dr.render_refused(report)
    if report["status"] == "observed":
        return dr.render_status(report)
    return dr._RENDERERS[report["status"]](report)


def _more(stderr):
    return [line for line in stderr.splitlines() if line.startswith("More: ")]


# ---------------------------------------------------------------------------
# Each outcome, in JSON and human mode
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scenario,failure_id,code", SCENARIOS, ids=IDS)
def test_json_adds_the_shared_failure_object_and_keeps_keys_and_exit_code(scenario, failure_id, code, monkeypatch, tmp_path, exe, data):
    argv = scenario(monkeypatch, tmp_path, exe, data)
    seen = _record(monkeypatch)
    result = runner.invoke(app, [*argv, "--json"])
    assert result.exit_code == code and result.stderr == ""
    payload = json.loads(result.stdout)
    report = seen[-1]
    assert dr.exit_code(report) == code
    assert {key: value for key, value in payload.items() if key != "failure"} == json.loads(json.dumps(report))
    assert dr.report_failure_id(report) == failure_id
    expected = fr.failure_payload(fr.resolve(failure_id, fr.FailureContext.from_runtime(command=" ".join(argv[:2]))))
    assert payload["failure"] == expected
    assert payload["failure"]["id"] == failure_id and payload["failure"]["explain"] == f"adapt explain {failure_id}"
    assert PHONE_TEXT not in result.stdout


@pytest.mark.parametrize("scenario,failure_id,code", SCENARIOS, ids=IDS)
def test_human_output_keeps_the_report_and_adds_the_pointer(scenario, failure_id, code, monkeypatch, tmp_path, exe, data):
    argv = scenario(monkeypatch, tmp_path, exe, data)
    seen = _record(monkeypatch)
    result = runner.invoke(app, argv)
    assert result.exit_code == code
    assert result.stdout == _render(seen[-1]) + "\n"          # the report and its first lines are unchanged
    assert result.stdout.startswith(dr.HEADER + "\n")
    assert result.stderr == f"More: adapt explain {failure_id}\n"   # CliRunner's stderr is not a terminal
    assert PHONE_TEXT not in result.stdout + result.stderr


@pytest.fixture
def tty(monkeypatch):
    monkeypatch.setattr(fr, "_stderr_isatty", lambda: True)


@pytest.mark.parametrize("scenario,failure_id,code", [s for s in SCENARIOS if s[2]], ids=[i for i, s in zip(IDS, SCENARIOS) if s[2]])
def test_a_failing_exit_prints_the_standard_block_in_a_terminal(scenario, failure_id, code, monkeypatch, tmp_path, exe, data, tty):
    argv = scenario(monkeypatch, tmp_path, exe, data)
    result = runner.invoke(app, argv)
    assert result.exit_code == code
    context = fr.FailureContext.from_runtime(command=" ".join(argv[:2]))
    assert result.stderr == "\n" + fr.render_human(fr.resolve(failure_id, context), color=False) + "\n"
    assert result.stderr.startswith("\nWhy: ") and "\nTry:\n" in result.stderr
    assert _more(result.stderr)[0].startswith(f"More: adapt explain {failure_id}   |   adapt help-me ")
    assert PHONE_TEXT not in result.stderr


@pytest.mark.parametrize("scenario,failure_id,code", [s for s in SCENARIOS if not s[2]], ids=[i for i, s in zip(IDS, SCENARIOS) if not s[2]])
def test_an_exit_zero_report_gets_only_the_pointer_even_in_a_terminal(scenario, failure_id, code, monkeypatch, tmp_path, exe, data, tty):
    argv = scenario(monkeypatch, tmp_path, exe, data)
    result = runner.invoke(app, argv)
    assert result.exit_code == 0
    assert result.stderr == f"More: adapt explain {failure_id}\n"


def test_guidance_off_removes_the_human_lines_but_not_the_json_object(monkeypatch, tmp_path, exe, data):
    argv = start_without_yes(monkeypatch, tmp_path, exe, data)
    monkeypatch.setenv("ADAPT_FAILURE_GUIDANCE", "off")
    human = runner.invoke(app, argv)
    assert human.exit_code == 1 and human.stderr == "" and "Refused (yes_required)" in human.stdout
    machine = runner.invoke(app, [*argv, "--json"])
    assert json.loads(machine.stdout)["failure"]["id"] == NATIVE + "confirmation_required"


# ---------------------------------------------------------------------------
# Successful outcomes stay as they were
# ---------------------------------------------------------------------------

def _ready(mp, tmp, exe, data):
    write(data, doc())
    _processes(mp, [4242])
    return _status(data)


def _nothing_here(mp, tmp, exe, data):
    return _status(tmp / "missing")


def _plan(mp, tmp, exe, data):
    return ["dex", "receiver-plan"]


def _started(mp, tmp, exe, data):
    install_popen(mp, data, status=lambda pid: live_doc(pid))
    return _start(exe, data)


def _stopped(mp, tmp, exe, data):
    write(data, doc())
    make_stop_world(mp, data, final=doc(running=False, advertising=False, phase_code="stopped"))
    return _stop(data)


def _nothing_to_stop(mp, tmp, exe, data):
    return _stop(tmp / "missing")


def _old_failure_while_connecting(mp, tmp, exe, data):
    write(data, doc(session_active=True, phase_code="connecting", trace=attempt(2, ["connection_request"], active=True),
                    last_failure=end("handshake_timeout", "session", "rtsp_connected", 1)))
    _processes(mp, [4242])
    return _status(data)


@pytest.mark.parametrize("scenario", [_ready, _nothing_here, _plan, _started, _stopped, _nothing_to_stop, _old_failure_while_connecting])
def test_successful_outcomes_name_no_guide(scenario, monkeypatch, tmp_path, exe, data):
    argv = scenario(monkeypatch, tmp_path, exe, data)
    human = runner.invoke(app, argv)
    assert human.exit_code == 0 and human.stderr == ""
    machine = runner.invoke(app, [*argv, "--json"])
    assert machine.exit_code == 0 and machine.stderr == "" and "failure" not in json.loads(machine.stdout)


# ---------------------------------------------------------------------------
# Mapping completeness and the registry
# ---------------------------------------------------------------------------

def _module_tree():
    return ast.parse((ROOT / "src" / "adapt_adb" / "dex_receiver.py").read_text(encoding="utf-8"))


def test_every_refusal_reason_in_the_module_has_a_guide():
    reasons = {node.args[0].value for node in ast.walk(_module_tree())
               if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_refuse"
               and node.args and isinstance(node.args[0], ast.Constant)}
    assert reasons == set(dr.REFUSAL_GUIDES)
    for failure_id in (*dr.REFUSAL_GUIDES.values(), *dr.STATE_GUIDES.values()):
        assert failure_id in fg.GUIDES, failure_id
    assert dr.report_failure_id({"status": "refused", "reason": "a reason this version does not know"}) == "internal.unclassified"


def test_every_native_receiver_guide_is_reached_by_an_outcome():
    native = {guide_id for guide_id in fg.GUIDES if guide_id.startswith(NATIVE)}
    assert native == {failure_id for _, failure_id, _ in SCENARIOS if failure_id.startswith(NATIVE)}
    assert len(native) == 19


def test_each_recorded_reason_code_maps_to_its_own_guide_and_orderly_ends_to_none():
    for code in fg.RECEIVER_GUIDES:
        block = dr.explain_code(code, dr.RECEIVER_REASON_SCOPES[code], "advertising", 0, current=True)
        expected = None if code in dr.ORDERLY_ENDS else f"dexpc.receiver.{code}"
        assert dr._recorded_failure_id(block) == expected
        assert dr.report_failure_id({"status": "failed", "last_failure": block}) == (expected or NATIVE + "exited_early")


def test_the_receiver_reason_code_guides_are_unchanged_in_number():
    assert len(fg.RECEIVER_GUIDES) == 33
    assert not any(guide_id.startswith("dexpc.receiver.") for guide_id in fg.GUIDES if guide_id.startswith(NATIVE))


def test_native_receiver_guides_follow_the_registry_conventions():
    for guide_id, guide in fg.GUIDES.items():
        if not guide_id.startswith(NATIVE):
            continue
        assert guide.certainty == "observed" and guide.summary.endswith(".") and guide.steps and guide.causes
        assert guide.docs == "docs/dex-pc.md" and guide.help_topic in {"dex receiver-status", "dex receiver-start", "dex receiver-stop"}
        assert set(guide.commands) <= {"dex receiver-status", "dex receiver-start", "dex receiver-stop"} and guide.commands
        assert guide.aliases, guide_id
        for step in guide.steps:
            assert step.mcp_tool is None
            if step.effect == "changes_host":
                assert step.command and step.command.startswith(("adapt dex receiver-", "powershell -NoProfile -File scripts\\")), step.text


def test_native_receiver_text_recommends_nothing_forbidden_and_names_no_private_value():
    from tests.test_failure_guide import FORBIDDEN, _all_text
    private = [r"\b\d{1,3}(\.\d{1,3}){3}\b", r"\b[0-9a-f]{2}([:-][0-9a-f]{2}){5}\b", r"[A-Za-z]:\\", r"\\Users\\|/home/|/Users/"]
    for guide_id, guide in fg.GUIDES.items():
        if not guide_id.startswith(NATIVE):
            continue
        for text in _all_text(guide):
            for pattern in FORBIDDEN:
                assert not pattern.search(text), (guide_id, text)
            for pattern in private:
                assert not re.search(pattern, text, re.IGNORECASE), (guide_id, text)
            assert not re.search(r"\b(kill|terminate|taskkill)\b", text, re.IGNORECASE), (guide_id, text)


# ---------------------------------------------------------------------------
# adapt explain finds every new ID, by ID and by alias
# ---------------------------------------------------------------------------

NATIVE_IDS = sorted(guide_id for guide_id in fg.GUIDES if guide_id.startswith(NATIVE))


@pytest.mark.parametrize("failure_id", NATIVE_IDS)
def test_explain_shows_each_new_guide_offline(failure_id):
    human = runner.invoke(app, ["explain", failure_id])
    assert human.exit_code == 0 and human.stderr == ""
    assert human.stdout.startswith(f"ADAPT / EXPLAIN / {failure_id}\n")
    machine = json.loads(runner.invoke(app, ["explain", failure_id, "--json"]).stdout)["failure"]
    guide = fg.GUIDES[failure_id]
    assert machine["id"] == failure_id and machine["aliases"] == list(guide.aliases) and machine["docs"] == "docs/dex-pc.md"
    for alias in guide.aliases:
        assert failure_id in fr.search(alias), (failure_id, alias)
        found = runner.invoke(app, ["explain", "--json", "--", alias])  # "--" lets an alias start with a dash
        assert found.exit_code == 0
        body = json.loads(found.stdout)
        listed = [body["failure"]["id"]] if "failure" in body else [entry["id"] for entry in body["failures"]]
        assert failure_id in listed, (failure_id, alias)


def test_explain_lists_the_new_namespace_and_keeps_the_reason_code_listing():
    listed = json.loads(runner.invoke(app, ["explain", "dexpc.native_receiver", "--json"]).stdout)["failures"]
    assert sorted(entry["id"] for entry in listed) == NATIVE_IDS
    codes = json.loads(runner.invoke(app, ["explain", "dexpc.receiver", "--json"]).stdout)["failures"]
    assert len(codes) == 33
    assert fr.search("port_in_use") == ["dexpc.receiver.port_in_use"]


def test_a_refusal_reason_from_the_report_finds_its_guide():
    for reason, failure_id in dr.REFUSAL_GUIDES.items():
        if reason == "port_in_use":
            # The receiver records the same code when it cannot bind the port itself, and that guide owns
            # the keyword (tests/test_failure_phase23.py); ADAPT's own check before starting is port_busy.
            assert fr.search(reason) == ["dexpc.receiver.port_in_use"] and failure_id == NATIVE + "port_busy"
            continue
        assert failure_id in fr.search(reason), reason
