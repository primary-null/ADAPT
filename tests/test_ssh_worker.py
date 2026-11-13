"""Offline SSH construction, remote results and explicit worker boundaries."""
import base64
import json
import shlex
import subprocess
import sys
import time

import pytest
from typer.testing import CliRunner

from adapt_adb import ssh_worker as ssh
from adapt_adb.cli import app
from adapt_adb.tool_catalog import GOALS, TOOL_CATALOG


def forbid(*a, **k):
    pytest.fail("Unexpected process, configuration or local ADB access")


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(ssh, "_execute", forbid)
    monkeypatch.setattr(ssh, "resolve_ssh", lambda: "chosen-ssh")
    import adapt_adb.adb as adb
    import adapt_adb.doctor as doctor
    monkeypatch.setattr(adb, "resolve_adb", forbid)
    monkeypatch.setattr(doctor, "preflight_adb_server", forbid)


def test_plan_is_offline_and_preserves_exact_posix_device_arguments(monkeypatch):
    monkeypatch.setattr(ssh, "resolve_ssh", forbid)
    target = "adb-Phone (2)'$(must-not-run)._adb-tls-connect._tcp"
    report = ssh.plan("android-worker", operation="inspect", device=target)
    assert shlex.split(report["ssh_argv"][-1]) == report["remote_argv"]
    assert report["remote_argv"][-3:] == ["-s", target, "--json"]
    assert report["network_access"] is False and report["local_adb_access"] is False
    assert report["automatic_fallback"] is False


@pytest.mark.parametrize("host", ["-oProxyCommand=bad", "user@host;bad", "ssh://host", "host\nother", "host name", "user@@host"])
def test_host_cannot_inject_ssh_flags_or_shell(host):
    with pytest.raises(ValueError):
        ssh.run(host)


@pytest.mark.parametrize("kwargs", [{"operation":"unlock"}, {"operation":"inspect"}, {"operation":"doctor", "device":"phone"}, {"operation":"inspect", "device":"phone", "transport":"all"}, {"remote_adapt":"adapt; reboot"}, {"port":0}, {"identity":"bad\nkey"}, {"operation":"inspect", "device":"bad\ntarget"}])
def test_invalid_scope_or_inputs_never_execute(kwargs):
    with pytest.raises(ValueError):
        ssh.run("worker", **kwargs)


def test_powershell_encoding_handles_spaces_and_literal_shell_syntax():
    target = "adb-Phone (2)$(not_a_command)._adb-tls-connect._tcp"
    report = ssh.plan("user@windows-worker", operation="inspect", device=target,
                      remote_shell="powershell", remote_adapt=r"C:\Program Files\ADAPT\adapt.exe")
    command = report["ssh_argv"][-1]
    assert command.startswith("powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand ")
    script = base64.b64decode(command.split()[-1]).decode("utf-16le")
    assert "'" + target + "'" in script
    assert "& 'C:\\Program Files\\ADAPT\\adapt.exe'" in script
    assert script.endswith("exit $LASTEXITCODE")
    assert target not in command


def test_legacy_powershell_does_not_silently_change_quoted_target():
    with pytest.raises(ValueError, match="double quotes"):
        ssh.run("worker", operation="inspect", device='phone"name', remote_shell="powershell")


def test_transport_options_do_not_reuse_forwarded_adb_or_accept_host_keys():
    args = ssh.plan("worker", port=2222, identity="keys/worker key")["ssh_argv"]
    for setting in ("StrictHostKeyChecking=yes", "BatchMode=yes", "ClearAllForwardings=yes", "ControlPath=none", "ForwardAgent=no", "UpdateHostKeys=no", "PermitLocalCommand=no"):
        assert setting in args
    assert args[-3] == "--" and args[-2] == "worker"
    assert args[args.index("-p") + 1] == "2222"
    assert args[args.index("-i") + 1] == "keys/worker key"


def test_remote_partial_report_preserves_worker_and_exit_code(monkeypatch):
    calls = []
    def run(args, timeout):
        calls.append((args, timeout))
        return subprocess.CompletedProcess(args, 2, json.dumps({"status":"partial", "bootloader":{"status":"unknown"}}), "")
    monkeypatch.setattr(ssh, "_execute", run)
    report = ssh.run("worker", operation="inspect", device="phone", transport="fastboot", timeout=90)
    assert calls[0][0][0] == "chosen-ssh" and calls[0][1] == 90
    assert report["exit_code"] == 2 and report["remote_report"]["bootloader"]["status"] == "unknown"
    assert report["worker"]["host"] == "worker" and report["local_adb_access"] is False


@pytest.mark.parametrize("code,stdout,expected", [(255,"", "ssh_failed"), (127,"", "invalid_response"), (0,"welcome\n{}", "invalid_response"), (0,"[]", "invalid_response"), (0,"{}", "remote_completed")])
def test_ssh_failure_and_invalid_remote_output_do_not_fallback(code, stdout, expected, monkeypatch):
    monkeypatch.setattr(ssh, "_execute", lambda args, timeout: subprocess.CompletedProcess(args, code, stdout, "sensitive diagnostic text"))
    report = ssh.run("worker")
    assert report["status"] == expected and report["automatic_fallback"] is False
    assert "sensitive diagnostic text" not in json.dumps(report)


def test_timeout_is_not_remote_success_and_does_not_use_local_adb(monkeypatch):
    monkeypatch.setattr(ssh, "_execute", lambda args, timeout: (_ for _ in ()).throw(subprocess.TimeoutExpired(args, timeout)))
    report = ssh.run("worker")
    assert report["status"] == "timeout" and report["exit_code"] == 1


def test_output_cap_rejects_large_worker_report(monkeypatch):
    monkeypatch.setattr(ssh, "MAX_RESPONSE_BYTES", 32)
    monkeypatch.setattr(ssh, "_execute", lambda args, timeout: subprocess.CompletedProcess(args, 0, json.dumps({"data":"x" * 100}), ""))
    assert ssh.run("worker")["status"] == "invalid_response"


def test_mcp_config_is_offline_read_only_scoped_and_protocol_silent(monkeypatch):
    monkeypatch.setattr(ssh, "resolve_ssh", forbid)
    result = CliRunner().invoke(app, ["ssh", "mcp-config", "worker", "--device", "exact target", "--device", "exact target", "--json"])
    assert result.exit_code == 0 and result.stderr == ""
    report = json.loads(result.stdout)
    entry = report["client_config"]["mcpServers"]["adapt-worker"]
    remote = shlex.split(entry["args"][-1])
    assert remote[-4:] == ["mcp", "--read-only", "--device", "exact target"]
    assert remote.count("--device") == 1 and report["configuration_written"] is False
    assert "-T" in entry["args"] and "--json" not in remote


def test_status_and_preview_are_clean_cli_json():
    runner = CliRunner()
    for args in (["ssh","status","--json"], ["ssh","plan","worker","--operation","scan","--transport","all","--json"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 0 and result.stderr == ""
        assert json.loads(result.stdout)["network_access"] is False


def test_remote_worker_catalog_does_not_install_ssh():
    assert "openssh" in GOALS["remote-worker"]
    assert TOOL_CATALOG["openssh"].optional and TOOL_CATALOG["openssh"].action_class == 2


def test_real_executor_uses_argv_no_stdin_and_timeout(monkeypatch):
    captured = []
    popen = subprocess.Popen
    def start(args, **kwargs):
        process = popen(args, **kwargs)
        captured.append((args, kwargs, process))
        return process
    monkeypatch.setattr(ssh.subprocess, "Popen", start)
    result = EXECUTOR([sys.executable, "-c", "import sys; print('{}'); sys.stderr.write('diagnostic')"], 1)
    assert json.loads(result.stdout)=={} and result.stderr=="diagnostic"
    assert captured[0][1]["shell"] is False
    assert captured[0][1]["stdin"] == subprocess.DEVNULL
    assert captured[0][2].returncode==0 and captured[0][2].stdout.closed and captured[0][2].stderr.closed


@pytest.mark.parametrize("fd,limit_name,message", [(1,"MAX_RESPONSE_BYTES","4 MiB"),(2,"MAX_STDERR_BYTES","64 KiB")])
def test_real_executor_kills_over_limit_child_and_returns_invalid_response(monkeypatch,fd,limit_name,message):
    monkeypatch.setattr(ssh,limit_name,1024)
    children=[];popen=subprocess.Popen
    def start(args,**kwargs):
        child=popen(args,**kwargs);children.append(child);return child
    monkeypatch.setattr(ssh.subprocess,"Popen",start)
    script=f"import os,time; os.write({fd}, b'x'*8192); time.sleep(30)"
    monkeypatch.setattr(ssh,"_execute",lambda args,timeout:EXECUTOR([sys.executable,"-c",script],timeout))
    started=time.monotonic()
    report=ssh.run("worker",timeout=10)
    assert time.monotonic()-started<2
    assert report["status"]=="invalid_response" and message in report["error"]
    assert children[0].returncode is not None
    assert children[0].stdout.closed and children[0].stderr.closed


def test_real_executor_small_response_parses(monkeypatch):
    monkeypatch.setattr(ssh,"_execute",lambda args,timeout:EXECUTOR([sys.executable,"-c","print('{\"status\":\"ok\"}')"],timeout))
    report=ssh.run("worker",timeout=1)
    assert report["status"]=="remote_completed" and report["remote_report"]=={"status":"ok"}


def test_real_executor_timeout_kills_and_reaps(monkeypatch):
    children=[];popen=subprocess.Popen
    def start(args,**kwargs):
        child=popen(args,**kwargs);children.append(child);return child
    monkeypatch.setattr(ssh.subprocess,"Popen",start)
    with pytest.raises(subprocess.TimeoutExpired):
        EXECUTOR([sys.executable,"-c","import time; time.sleep(30)"],.1)
    assert children[0].returncode is not None and children[0].stdout.closed and children[0].stderr.closed


def test_real_executor_invalid_utf8_is_rejected():
    with pytest.raises(UnicodeError):
        EXECUTOR([sys.executable,"-c","import os; os.write(1,b'\\xff')"],1)


EXECUTOR = ssh._execute
