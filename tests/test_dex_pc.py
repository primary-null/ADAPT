"""PC-session gates and recovery do not equate a fake screen with DeX."""
import json
import io
import hashlib
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from adapt_adb import dex_pc as pc
from adapt_adb.cli import app


def forbid(*a, **k):
    pytest.fail("Unexpected live process or device operation")


@pytest.fixture
def session(tmp_path, monkeypatch):
    identity = pc.dev_options.DeviceIdentity("synthetic-physical", "synthetic-build", 10)
    state = {"schema_version":1,"token":"a"*32,"serial":"exact target","identity":asdict(identity),
             "method":"overlay","size":"1920x1080","dpi":160,"view_only":False,
             "overlay_value":"1920x1080/160,hdmi","original_overlay":"null","status":"prepared","events":[]}
    path = tmp_path / "session.json"
    pc._save(path,state)
    monkeypatch.setattr(pc.dev_options,"_read_identity",lambda *a:identity)
    monkeypatch.setattr(pc.dev_options,"_resolve_adb_path",lambda *a:Path("chosen-adb"))
    monkeypatch.setattr(pc.dev_options,"_LOCK_ROOT",tmp_path/"locks")
    return state,path,identity


def test_plan_and_status_are_offline_and_clean_json(session,monkeypatch):
    monkeypatch.setattr(pc,"_adb",forbid)
    monkeypatch.setattr(pc.subprocess,"Popen",forbid)
    result = CliRunner().invoke(app,["dex","pc-plan","-s","exact target","--json"])
    assert result.exit_code==0 and result.stderr==""
    assert json.loads(result.stdout)["device_access"] is False
    result = CliRunner().invoke(app,["dex","pc-status","--snapshot",str(session[1]),"--json"])
    assert result.exit_code==0 and "synthetic-physical" not in result.stdout


def test_start_refuses_before_device_access_without_yes(monkeypatch):
    monkeypatch.setattr(pc.dev_options,"_resolve_adb_path",forbid)
    with pytest.raises(pc.PcSessionError,match="--yes"):
        pc.start("exact target")


@pytest.mark.parametrize("values", [{"method":"other"},{"serial":"bad\nserial"},{"size":"9999x1080"},{"dpi":True}])
def test_bad_plan_inputs_fail_closed(values):
    with pytest.raises(ValueError):
        pc.plan(**{"serial":"exact target",**values})


def test_resource_integrity_rejects_changed_source(tmp_path,monkeypatch):
    (tmp_path/"build.json").write_text(json.dumps({"jar_sha256":"0"*64,"source_sha256":"0"*64}))
    (tmp_path/"adapt-dex-bridge.jar").write_bytes(b"changed")
    (tmp_path/"AdaptDexBridge.java").write_text("changed")
    monkeypatch.setattr(pc,"RESOURCE",tmp_path)
    with pytest.raises(pc.PcSessionError,match="integrity"):
        pc.helper_path()


def test_fold_internal_screen_is_not_the_new_desktop_and_inactive_dex_is_not_success():
    before={"displays":[{"id":0,"type":1},{"id":1,"type":1}]}
    inactive={"displays":[*before["displays"],{"id":7,"type":4,"unique_id":"overlay:1"}],"dex_enabled":2,"dex_display_type":0}
    assert pc.classify(before,inactive) is None
    assert pc.classify(before,{**inactive,"dex_enabled":4,"dex_display_type":102}) == 7
    assert pc.classify(before,{**inactive,"dex_enabled":4,"dex_display_type":101}) is None
    unrelated={**inactive,"displays":[*before["displays"],{"id":7,"type":2,"unique_id":"local:external"}],"dex_enabled":4,"dex_display_type":102}
    assert pc.classify(before,unrelated) is None


def test_overlay_restore_removes_only_own_value_and_checks_identity(session,monkeypatch):
    state,path,identity=session
    value=[state["overlay_value"]];calls=[]
    def adb(binary,serial,args):
        calls.append(args)
        if "get" in args:return value[0]
        if "delete" in args:value[0]="null";return "Deleted 1 rows"
        pytest.fail(str(args))
    monkeypatch.setattr(pc,"_adb",adb)
    assert pc._restore_overlay(Path("adb"),state)=="restored_verified"
    assert sum("delete" in a for a in calls)==1
    monkeypatch.setattr(pc.dev_options,"_read_identity",lambda *a:pc.dev_options.DeviceIdentity("other","build",10))
    monkeypatch.setattr(pc,"_adb",forbid)
    with pytest.raises(pc.PcSessionError,match="differs"):
        pc._restore_overlay(Path("adb"),state)


def test_external_overlay_change_is_preserved(session,monkeypatch):
    monkeypatch.setattr(pc,"_adb",lambda b,s,a:"1280x720/240")
    with pytest.raises(pc.PcSessionError,match="externally"):
        pc._restore_overlay(Path("adb"),session[0])


@pytest.mark.parametrize("field,value", [("original_overlay","arbitrary"),("overlay_value","bad;command"),("status","unknown"),("view_only","yes")])
def test_tampered_snapshot_cannot_drive_recovery(session,field,value):
    state,path,_=session
    state[field]=value;pc._save(path,state)
    with pytest.raises(pc.PcSessionError,match="snapshot"):
        pc._load(path)


def test_current_user_type_is_strict(session):
    state,path,_=session
    state["identity"]["android_user"]=False
    pc._save(path,state)
    with pytest.raises(pc.PcSessionError):pc._load(path)


def test_worker_cleans_overlay_when_decorative_screen_does_not_confirm_dex(session,monkeypatch):
    state,path,_=session
    value=["null"];calls=[]
    def adb(binary,serial,args):
        calls.append(args)
        if "get" in args:return value[0]
        if "put" in args:value[0]=state["overlay_value"]
        if "delete" in args:value[0]="null"
        return ""
    monkeypatch.setattr(pc,"_adb",adb)
    monkeypatch.setattr(pc,"resolve_scrcpy",lambda:Path("scrcpy"))
    monkeypatch.setattr(pc,"_check_scrcpy_version",lambda *a:"5.0.1")
    baseline={"displays":[{"id":0,"type":1},{"id":1,"type":1}],"wifi_state":0,"dex_enabled":2,"dex_display_type":0}
    probes=iter([baseline,{**baseline,"displays":[*baseline["displays"],{"id":7,"type":4}]}])
    monkeypatch.setattr(pc,"_probe",lambda *a:next(probes))
    clock=iter([0,1,12])
    monkeypatch.setattr(pc.time,"monotonic",lambda:next(clock))
    monkeypatch.setattr(pc.subprocess,"Popen",forbid)
    pc.worker(path)
    final=pc._load(path)
    assert final["status"]=="failed" and "not a working DeX" in final["error"]
    assert final["cleanup"]=="restored_verified" and value[0]=="null"
    assert any(a[:2]==["shell","rm"] for a in calls)


def test_existing_external_display_blocks_without_overlay_mutation(session,monkeypatch):
    calls=[]
    monkeypatch.setattr(pc,"_adb",lambda b,s,a:calls.append(a) or "")
    monkeypatch.setattr(pc,"resolve_scrcpy",lambda:Path("scrcpy"))
    monkeypatch.setattr(pc,"_check_scrcpy_version",lambda *a:"5.0.1")
    monkeypatch.setattr(pc,"_probe",lambda *a:{"displays":[{"id":0,"type":1},{"id":5,"type":3}],"wifi_state":0,"dex_enabled":2,"dex_display_type":0})
    pc.worker(session[1])
    final=pc._load(session[1])
    assert final["status"]=="failed"
    assert not any("put" in a for a in calls)


def test_stop_never_kills_a_process_from_saved_pid(session,monkeypatch):
    state,path,_=session
    state["status"]="failed";state["worker_pid"]=99999;pc._save(path,state)
    monkeypatch.setattr(pc.subprocess,"Popen",forbid)
    monkeypatch.setattr(pc,"_stop_owned_process",forbid)
    assert pc.stop(path)["status"]=="failed"


def test_recovery_snapshot_is_readable_even_if_helper_bundle_is_unavailable(session,monkeypatch):
    monkeypatch.setattr(pc,"helper_path",forbid)
    assert pc._load(session[1])["serial"]=="exact target"


def test_loopback_permission_denial_is_recorded_without_mirroring_or_settings_change(session,monkeypatch):
    state,path,_=session
    state["method"]="loopback";pc._save(path,state)
    calls=[]
    monkeypatch.setattr(pc,"_adb",lambda b,s,a:calls.append(a) or "")
    monkeypatch.setattr(pc,"resolve_scrcpy",lambda:Path("scrcpy"))
    monkeypatch.setattr(pc,"_check_scrcpy_version",lambda *a:"5.0.1")
    baseline={"displays":[{"id":0,"type":1},{"id":1,"type":1}],"wifi_state":0,"dex_enabled":2,"dex_display_type":0}
    monkeypatch.setattr(pc,"_probe",lambda *a:baseline)
    starts=[]
    denied=json.dumps({"event":"error","message":"CONFIGURE_WIFI_DISPLAY permission denied"})+"\n"
    process=SimpleNamespace(stdout=io.StringIO(denied),stdin=io.StringIO(),poll=lambda:1)
    monkeypatch.setattr(pc.subprocess,"Popen",lambda argv,**kwargs:starts.append(argv) or process)
    pc.worker(path)
    final=pc._load(path)
    assert final["status"]=="failed" and "CONFIGURE_WIFI_DISPLAY" in final["error"]
    assert len(starts)==1 and "serve" in starts[0]
    assert final["cleanup"]=="wireless_inactive_verified"
    assert not any("settings" in a for a in calls)


def test_receiver_plan_and_probe_consent_are_explicit(monkeypatch):
    monkeypatch.setattr(pc.dev_options,"_resolve_adb_path",forbid)
    assert pc.receiver_plan()["changes_performed"] is False
    with pytest.raises(pc.PcSessionError,match="--yes"):
        pc.runtime_probe("phone")


def test_attach_requires_real_dual_dex_and_observed_external_id(monkeypatch):
    monkeypatch.setattr(pc,"runtime_probe",lambda *a,**k:{"observations":{"dex_enabled":2,"dex_display_type":0,"displays":[{"id":0,"type":1},{"id":7,"type":4}]}})
    monkeypatch.setattr(pc.subprocess,"Popen",forbid)
    with pytest.raises(pc.PcSessionError,match="active Samsung"):
        pc.attach("phone",7,yes=True)


@pytest.mark.parametrize("compositor", [False, True])
def test_attach_preserves_external_receiver_and_targets_its_exact_display(session,monkeypatch,tmp_path,compositor):
    _,_,identity=session
    binding=hashlib.sha256(json.dumps(asdict(identity),sort_keys=True).encode()).hexdigest()
    observations={"dex_enabled":4,"dex_display_type":102,"displays":[{"id":0,"type":1},{"id":1,"type":1},{"id":17,"type":3}]}
    if compositor:
        observations["wifi_state"] = 2
        observations["displays"].append({"id":23,"type":5,"unique_id":"virtual:android,1000,Desktop,0"})
    monkeypatch.setattr(pc,"runtime_probe",lambda *a,**k:{"api":33,"identity_binding":binding,"observations":observations})
    monkeypatch.setattr(pc,"resolve_scrcpy",lambda:Path("chosen-scrcpy"))
    monkeypatch.setattr(pc,"_check_scrcpy_version",lambda *a:"5.0.1")
    monkeypatch.setattr(pc,"_default_snapshot",lambda:tmp_path/"attach.json")
    monkeypatch.setattr(pc.time,"sleep",lambda *a:None)
    monkeypatch.setattr(pc,"_adb",forbid)
    starts=[]
    process=SimpleNamespace(pid=123,poll=lambda:None)
    monkeypatch.setattr(pc.subprocess,"Popen",lambda argv,**kw:starts.append((argv,kw)) or process)
    display = 23 if compositor else 17
    report=pc.attach("exact target",display,yes=True)
    assert report["display_ownership"]=="external_user_session" and report["status"]=="process_started"
    assert starts[0][0][:3]==["chosen-scrcpy","-s","exact target"] and f"--display-id={display}" in starts[0][0]
    assert starts[0][1]["env"]["ADB"]=="chosen-adb"
    assert "--verbosity=warn" in starts[0][0]
    assert report["healthy_streaming"]=="not_verified"


@pytest.mark.parametrize("mutation", ["inactive", "disconnected", "overlay", "other_uid", "missing_sink", "extra_display", "sink_selected", "unknown_api"])
def test_compositor_attach_refuses_unconfirmed_or_ambiguous_layout(monkeypatch, mutation):
    desktop={"id":23,"type":5,"unique_id":"virtual:android,1000,Desktop,0"}
    sink={"id":31,"type":3}
    state={"dex_enabled":4,"dex_display_type":102,"wifi_state":2,
           "displays":[{"id":0,"type":1},desktop,sink]}
    observed={"api":33,"observations":state}
    display=23
    if mutation=="inactive": state["dex_enabled"]=2
    if mutation=="disconnected": state["wifi_state"]=0
    if mutation=="overlay": desktop.update(type=4,unique_id="overlay:1")
    if mutation=="other_uid": desktop["unique_id"]="virtual:android,2000,Desktop,0"
    if mutation=="missing_sink": state["displays"].remove(sink)
    if mutation=="extra_display": state["displays"].append({"id":47,"type":5})
    if mutation=="sink_selected": display=31
    if mutation=="unknown_api": observed["api"]=34
    monkeypatch.setattr(pc,"runtime_probe",lambda *a,**k:observed)
    monkeypatch.setattr(pc.subprocess,"Popen",forbid)
    with pytest.raises(pc.PcSessionError,match="active Samsung"):
        pc.attach("exact target",display,yes=True)


def test_stop_request_does_not_claim_completion_without_supervisor_ack(session,monkeypatch):
    clock=iter([0,16])
    monkeypatch.setattr(pc.time,"monotonic",lambda:next(clock))
    result=pc.stop(session[1])
    assert result["status"]=="stop_pending"
    assert session[1].with_name("session.json.stop").read_text()==session[0]["token"]


@pytest.mark.parametrize("readback", ["1280x720/240", "null"])
def test_overlay_readback_mismatch_launches_nothing_and_records_cleanup(session,monkeypatch,readback):
    state,path,_=session
    value=["null"];calls=[]
    def adb(binary,serial,args):
        calls.append(args)
        if "get" in args:return value[0]
        if "put" in args:value[0]=readback
        return ""
    monkeypatch.setattr(pc,"_adb",adb)
    monkeypatch.setattr(pc,"resolve_scrcpy",lambda:Path("scrcpy"))
    monkeypatch.setattr(pc,"_check_scrcpy_version",lambda *a:"5.0.1")
    monkeypatch.setattr(pc,"_probe",lambda *a:{"displays":[{"id":0,"type":1}],"wifi_state":0,"dex_enabled":2,"dex_display_type":0})
    monkeypatch.setattr(pc.subprocess,"Popen",forbid)
    pc.worker(path)
    final=pc._load(path)
    assert "readback did not match" in final["error"]
    assert value[0]==readback and not any("delete" in a for a in calls)
    if readback=="null":
        assert final["status"]=="failed" and final["cleanup"]=="already_original"
    else:
        assert final["status"]=="cleanup_conflict"
        assert final["overlay_cleanup_current"]==readback
        assert final["overlay_ownership"]=="unowned_left_untouched"
        assert "unowned" in final["cleanup_error"] and "left it untouched" in final["cleanup_error"]


def test_log_pump_caps_writes_and_keeps_draining(monkeypatch):
    monkeypatch.setattr(pc,"MAX_SCRCPY_LOG_BYTES",128)
    log=io.BytesIO()
    class Producer(io.BytesIO):
        def read1(self,size):
            # The pump must keep reading after it has written the marker.
            chunk=super().read1(40)
            if self.tell()>200: assert len(log.getvalue())==128
            return chunk
    source=Producer(b"x"*1000)
    pc._pump_log(source,log)
    assert source.tell()==1000
    assert len(log.getvalue())==128
    assert log.getvalue().count(pc.LOG_TRUNCATION_MARKER)==1


def test_log_pump_preserves_small_startup_diagnostic():
    log=io.BytesIO()
    pc._pump_log(io.BytesIO(b"scrcpy startup failed\n"),log)
    assert log.getvalue()==b"scrcpy startup failed\n"


@pytest.mark.parametrize("startup_failed", [False, True])
def test_worker_joins_log_pump_and_closes_log_on_mirror_exit(session,monkeypatch,startup_failed):
    state,path,_=session
    value=["null"]
    def adb(binary,serial,args):
        if "get" in args:return value[0]
        if "put" in args:value[0]=state["overlay_value"]
        if "delete" in args:value[0]="null"
        return ""
    monkeypatch.setattr(pc,"_adb",adb)
    monkeypatch.setattr(pc,"resolve_scrcpy",lambda:Path("scrcpy"))
    monkeypatch.setattr(pc,"_check_scrcpy_version",lambda *a:"5.0.1")
    baseline={"displays":[{"id":0,"type":1}],"wifi_state":0,"dex_enabled":2,"dex_display_type":0}
    active={**baseline,"displays":[*baseline["displays"],{"id":7,"type":4,"unique_id":"overlay:1"}],"dex_enabled":4,"dex_display_type":102}
    probes=iter([baseline,active])
    monkeypatch.setattr(pc,"_probe",lambda *a:next(probes))
    monkeypatch.setattr(pc.time,"sleep",lambda *a:None)
    polls=iter([1,1] if startup_failed else [None,1,1])
    process=SimpleNamespace(stdout=io.BytesIO(b"startup diagnostic\n"),poll=lambda:next(polls))
    def start(argv,**kwargs):
        assert kwargs["stdout"]==pc.subprocess.PIPE and kwargs["stderr"]==pc.subprocess.STDOUT
        return process
    monkeypatch.setattr(pc.subprocess,"Popen",start)
    logs=[];open_log=pc._log
    def log_file(log_path):
        stream=open_log(log_path);logs.append(stream);return stream
    monkeypatch.setattr(pc,"_log",log_file)
    threads=[];thread=pc.threading.Thread
    def new_thread(*args,**kwargs):
        result=thread(*args,**kwargs);threads.append(result);return result
    monkeypatch.setattr(pc.threading,"Thread",new_thread)
    pc.worker(path)
    final=pc._load(path)
    assert final["status"]==("failed" if startup_failed else "stopped")
    if startup_failed: assert "see its session log" in final["error"]
    assert final["cleanup"]=="restored_verified"
    assert len(threads)==1 and threads[0].daemon and not threads[0].is_alive()
    assert logs[0].closed and process.stdout.closed
    assert path.with_name(path.name+".scrcpy.log").read_bytes()==b"startup diagnostic\n"
