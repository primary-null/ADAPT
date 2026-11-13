"""Tailnet selection must lead to one verified ADB endpoint, never an implicit fallback."""
import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from adapt_adb import tailscale_mode as ts
from adapt_adb.cli import app
from adapt_adb.dev_options import DeviceIdentity


def forbid(*a, **k):
    pytest.fail("Unexpected peer, ADB, socket or process action")


def raw_status():
    return {"BackendState":"Running","AuthURL":"private-login-url","User":{"private":"private-user-metadata"},
        "Peer":{"nodekey:private":{"ID":"node1","HostName":"phone","DNSName":"phone.example.ts.net.","OS":"android", "Online":True,
        "TailscaleIPs":["100.64.0.2","fd7a:115c:a1e0::2"],"PublicKey":"not-for-output","Expired":False}}}


@pytest.fixture
def context(monkeypatch):
    value=raw_status()
    monkeypatch.setattr(ts,"status",lambda:{**ts.parse_status(json.dumps(value)),"status":"running"})
    monkeypatch.setattr(ts.dev_options,"_resolve_adb_path",lambda p:Path("selected-adb"))
    monkeypatch.setattr(ts.dev_options,"_read_identity",lambda *a:DeviceIdentity("physical-phone","build",0))
    calls=[]
    def query(adb,endpoint,args):
        calls.append((str(adb),endpoint,args))
        return "device" if args==["get-state"] else "33" if args[-1]=="ro.build.version.sdk" else "EXAMPLE_PHONE_MODEL" if args[-1]=="ro.product.model" else "37123"
    monkeypatch.setattr(ts,"_adb_query",query)
    monkeypatch.setattr('adapt_adb.doctor.preflight_adb_server',lambda *a,**k:None)
    return value,calls


def test_status_minimizes_private_keys_and_preserves_usable_node_addresses():
    value=raw_status()
    value['Peer']['nodekey:private']['TailscaleIPs'] += ['8.8.8.8','127.0.0.1','fd7a:115c:a1e0::2%zone']
    report=ts.parse_status(json.dumps(value))
    text=json.dumps(report)
    assert all(secret not in text for secret in ('private-login-url','nodekey:private','not-for-output','private-user-metadata'))
    assert 'User' not in report and 'AuthURL' not in report
    assert report['peers'][0]['addresses']==['100.64.0.2','fd7a:115c:a1e0::2']
    assert report['peer_contact'] is False


@pytest.mark.parametrize('value', ['[]','{}','not json','{"BackendState":"Running","Peer":[]}'])
def test_malformed_status_is_not_a_peer_source(value):
    with pytest.raises(ValueError):ts.parse_status(value)


@pytest.mark.parametrize('peer', ['phone','phone.example.ts.net.','100.64.0.2','[fd7a:115c:a1e0:0:0:0:0:2]','node1'])
def test_names_ip_and_ids_select_one_peer_without_dns_fallback(context,peer):
    selected=ts._select(peer)
    assert selected['peer_id']=='node1' and selected['address']=='100.64.0.2'


def test_ambiguous_offline_expired_and_unknown_peers_stop_before_adb(context,monkeypatch):
    raw,_=context
    monkeypatch.setattr(ts.dev_options,'_resolve_adb_path',forbid)
    raw['Peer']['other']={**raw['Peer']['nodekey:private'],'ID':'node2','TailscaleIPs':['100.64.0.3']}
    with pytest.raises(ValueError,match='ambiguous'):ts.connect('phone',37123)
    raw['Peer'].pop('other')
    raw['Peer']['nodekey:private']['Online']=False
    with pytest.raises(ValueError,match='offline'):ts.connect('phone',37123)
    raw['Peer']['nodekey:private']['Online']=True;raw['Peer']['nodekey:private']['Expired']=True
    with pytest.raises(ValueError,match='expired'):ts.connect('phone',37123)
    with pytest.raises(ValueError,match='missing'):ts.connect('unknown',37123)


@pytest.mark.parametrize('peer,port', [('8.8.8.8',37123),('127.0.0.1',37123),('192.168.1.2',37123),('https://phone',37123),('-s',37123),('phone;cmd',37123),('phone',0),('phone',True),('phone',65536)])
def test_plan_validates_endpoints_before_status_or_process_access(monkeypatch,peer,port):
    monkeypatch.setattr(ts,'status',forbid)
    with pytest.raises(ValueError):ts.plan(peer,port)


def test_plan_pairing_ports_and_worker_config_are_offline(monkeypatch):
    monkeypatch.setattr(ts,'status',forbid)
    monkeypatch.setattr(ts,'_execute',forbid)
    report=ts.plan('phone',37123,pairing_port=41111)
    assert report['pairing_port']!=report['connection_port'] and not report['peer_contact']
    assert report['commands'][0][-1]=='41111' and report['commands'][1][6]=='37123'
    worker=ts.worker('lab.example.ts.net',action='mcp',device='exact USB',user='dev')
    argv=worker['client_config']['mcpServers']['adapt-worker']['args']
    assert 'StrictHostKeyChecking=yes' in argv and '--read-only' in argv[-1]
    assert 'exact USB' in argv[-1] and worker['configuration_written'] is False


def test_ipv6_selection_builds_bracketed_adb_endpoint(context):
    raw,_=context
    raw['Peer']['nodekey:private']['TailscaleIPs']=['fd7a:115c:a1e0::2']
    assert ts._endpoint(ts._select('phone'),37123)=='[fd7a:115c:a1e0::2]:37123'


def test_connect_verifies_exact_tailnet_adb_identity_and_current_tls_port(context,monkeypatch):
    _,calls=context;executed=[]
    monkeypatch.setattr(ts,'_execute',lambda argv,**k:executed.append(argv) or subprocess.CompletedProcess(argv,0,'connected to 100.64.0.2:37123\n',''))
    report=ts.connect('phone',37123)
    assert report['status']=='verified' and report['serial']=='100.64.0.2:37123'
    assert report['tls_listener_port_match'] and report['device']['ro_serialno']=='physical-phone'
    assert report['model']=='EXAMPLE_PHONE_MODEL'
    assert executed==[['selected-adb','connect','100.64.0.2:37123']]
    assert all(endpoint=='100.64.0.2:37123' for _,endpoint,_ in calls)


def test_reachable_or_connected_does_not_mean_authorized_or_tls(context,monkeypatch):
    monkeypatch.setattr(ts,'_adb_query',lambda *a:'unauthorized')
    with pytest.raises(ValueError,match='not authorized'):ts.check('phone',37123)
    monkeypatch.setattr(ts,'_adb_query',lambda a,e,args:'device' if args==['get-state'] else '29' if args[-1]=='ro.build.version.sdk' else '')
    with pytest.raises(ValueError,match='TLS listener'):ts.check('phone',5555)
    assert ts.check('phone',5555,mode='legacy')['mode']=='legacy'


def test_pairing_secret_is_only_stdin_and_never_returned_even_when_adb_echoes_it(context,monkeypatch):
    seen=[]
    def execute(argv,**kwargs):
        seen.append((argv,kwargs))
        return subprocess.CompletedProcess(argv,0,'Successfully paired to 100.64.0.2:41111 [guid=private]\n','')
    monkeypatch.setattr(ts,'_execute',execute)
    report=ts.pair('phone',41111,'123456')
    assert seen[0][1]['input']=='123456\n' and '123456' not in str(seen[0][0])
    assert '123456' not in json.dumps(report) and 'guid=private' not in json.dumps(report)
    monkeypatch.setattr(ts,'_execute',lambda argv,**k:subprocess.CompletedProcess(argv,1,'secret 123456','secret 123456'))
    with pytest.raises(ValueError) as caught:ts.pair('phone',41111,'123456')
    assert '123456' not in str(caught.value)


def test_tcp_probe_is_one_port_not_adb_authorization(context,monkeypatch):
    class Connected:
        def __enter__(self):return self
        def __exit__(self,*a):pass
    calls=[]
    monkeypatch.setattr(ts.socket,'create_connection',lambda addr,**k:calls.append(addr) or Connected())
    report=ts.probe('phone',37123)
    assert calls==[('100.64.0.2',37123)] and report['adb_authorized']=='not_checked'


def test_official_pair_prompt_and_success_can_share_one_line(context,monkeypatch):
    output='Enter pairing code: Successfully paired to 100.64.0.2:41111 [guid=private]\n'
    monkeypatch.setattr(ts,'_execute',lambda argv,**kw:subprocess.CompletedProcess(argv,0,output,''))
    assert ts.pair('phone',41111,'123456')['status']=='paired'
    output='Enter pairing code: Successfully paired to 100.64.0.2:411110 [guid=private]\n'
    with pytest.raises(ts.TailscaleError):ts.pair('phone',41111,'123456')


def test_pair_failure_is_classified_without_secret_or_raw_output(context,monkeypatch):
    output='Enter pairing code: '
    error="error: protocol fault (couldn't read status message): 123456 private-guid"
    monkeypatch.setattr(ts,'_execute',lambda argv,**kw:subprocess.CompletedProcess(argv,1,output,error))
    with pytest.raises(ts.TailscaleError,match='protocol/read failure') as caught:
        ts.pair('phone',41111,'123456')
    assert '123456' not in str(caught.value) and 'private-guid' not in str(caught.value)


def test_adb_passthrough_cannot_override_target_or_run_a_host_command(context,monkeypatch):
    monkeypatch.setattr(ts,'status',forbid)
    for args in (['-s','other','shell','getprop'],['reconnect','offline'],['devices'],['kill-server'],['forward','--remove-all'],['shell','pm','clear','pkg'],['logcat']):
        with pytest.raises(ValueError):ts.adb_command('phone',37123,args)


def test_adb_passthrough_runs_selected_numeric_endpoint_with_existing_guard(context,monkeypatch):
    calls=[]
    monkeypatch.setattr(ts.subprocess,'run',lambda argv,**k:calls.append((argv,k)) or subprocess.CompletedProcess(argv,0))
    assert ts.adb_command('phone',37123,['--','shell','getprop','ro.product.model'])==0
    assert calls[0][0]==['selected-adb','-s','100.64.0.2:37123','shell','getprop','ro.product.model']
    assert calls[0][1]['shell'] is False


def test_cli_passthrough_remains_a_command_not_a_shell_string(monkeypatch):
    calls=[]
    monkeypatch.setattr(ts,'adb_command',lambda peer,port,args,**kw:calls.append((peer,port,args)) or 0)
    result=CliRunner().invoke(app,['tailscale','adb','--peer','phone','--port','37123','--','shell','getprop','ro.product.model'])
    assert result.exit_code==0,result.output
    assert calls[0][2]==['shell','getprop','ro.product.model']


def test_explicit_disconnect_targets_only_one_transport(context,monkeypatch):
    calls=[]
    monkeypatch.setattr(ts,'_execute',lambda argv,**k:calls.append(argv) or subprocess.CompletedProcess(argv,0,'disconnected 100.64.0.2:37123',''))
    with pytest.raises(ValueError,match='--yes'):ts.disconnect('phone',37123)
    assert not calls
    assert ts.disconnect('phone',37123,yes=True)['status']=='disconnected'
    assert calls==[['selected-adb','disconnect','100.64.0.2:37123']]


def test_offline_json_and_missing_client_are_clean(monkeypatch):
    monkeypatch.setattr(ts,'resolve_tailscale',lambda:None)
    runner=CliRunner()
    result=runner.invoke(app,['tailscale','status','--json'])
    assert result.exit_code==1 and json.loads(result.stdout)['status']=='missing_client' and not result.stderr
    result=runner.invoke(app,['tailscale','guide','--json'])
    assert result.exit_code==0 and not json.loads(result.stdout)['peer_contact'] and not result.stderr


def test_peer_identity_change_during_verification_refuses_without_disconnect(context,monkeypatch):
    raw,_=context
    def changed(adb,endpoint,args):
        if args[-1]=='service.adb.tls.port':
            raw['Peer']['nodekey:private']['ID']='replacement'
            return '37123'
        return 'device' if args==['get-state'] else '33'
    monkeypatch.setattr(ts,'_adb_query',changed)
    monkeypatch.setattr(ts,'_execute',forbid)
    with pytest.raises(ValueError):ts.check('phone',37123)


def test_real_space_padded_long_metadata_never_redefines_authoritative_identity():
    from adapt_adb.adb import parse_devices_short, enrich_devices_long
    identities=parse_devices_short('List of devices attached\nUSB_A\tdevice\nopaque name\tdevice\n')
    long='List of devices attached\nUSB_A                  device product:test model:EXAMPLE_MODEL_A transport_id:1\nopaque name            device model:fold_test transport_id:2\nUNKNOWN                device model:EXAMPLE_MODEL_A\n'
    rows=enrich_devices_long(identities,long)
    assert [r['serial'] for r in rows]==['USB_A','opaque name']
    assert rows[0]['model']=='EXAMPLE MODEL A' and rows[1]['model']=='fold test'
