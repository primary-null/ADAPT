"""Reason codes for failed SSH worker runs: fixed vocabulary, no raw ssh output in reports."""
import json
import subprocess

import pytest

from adapt_adb import ssh_worker as ssh

OLD_ERROR = ("Verify connectivity, existing host-key trust, key/agent authentication and worker configuration. "
             "No host keys were automatically accepted.")
OLD_INVALID_ERROR = ("Expected one ADAPT JSON object. Check remote ADAPT version/path, selected shell and stdout "
                     "from login scripts; no local fallback ran.")
BASE_KEYS = {"schema_version", "worker", "operation", "device", "network_access", "local_adb_access",
             "automatic_fallback", "status", "exit_code"}
NEW_KEYS = {"ssh_failure_reason", "hint", "response_reason"}
SSH_REASONS = {"host_key_unknown", "host_key_changed", "authentication_failed", "name_resolution_failed",
               "connection_refused", "connection_timed_out", "network_unreachable", "connection_closed",
               "identity_file_problem", "unknown"}
RESPONSE_REASONS = {"remote_adapt_not_found", "remote_shell_mismatch", "not_json", "empty_response",
                    "unexpected_exit_code"}


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    def forbid(*a, **k):
        pytest.fail("Unexpected process or network access")
    monkeypatch.setattr(ssh, "_execute", forbid)
    monkeypatch.setattr(ssh, "resolve_ssh", lambda: "chosen-ssh")


def fake(monkeypatch, code, stdout="", stderr=""):
    monkeypatch.setattr(ssh, "_execute", lambda args, timeout: subprocess.CompletedProcess(args, code, stdout, stderr))


def ssh_report(monkeypatch, stderr, code=255, **kwargs):
    fake(monkeypatch, code, "", stderr)
    return ssh.run("worker", **kwargs)


CHANGED_KEY = """@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @
@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
IT IS POSSIBLE THAT SOMEONE IS DOING SOMETHING NASTY!
Offending ECDSA key in /home/u/.ssh/known_hosts:7
Host key for 10.1.2.3 has changed and you have requested strict checking.
Host key verification failed.
"""
BAD_KEY_PERMISSIONS = """@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
@         WARNING: UNPROTECTED PRIVATE KEY FILE!          @
@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
Permissions 0644 for '/home/u/.ssh/id_ed25519' are too open.
It is required that your private key files are NOT accessible by others.
This private key will be ignored.
Load key "/home/u/.ssh/id_ed25519": bad permissions
u@10.1.2.3: Permission denied (publickey).
"""

CASES = [
    ("host_key_unknown", "Host key verification failed.\r\n"),
    ("host_key_unknown", "No ED25519 host key is known for worker.example and you have requested strict checking.\r\nHost key verification failed.\r\n"),
    ("host_key_unknown", "No RSA host key is known for [10.1.2.3]:2222 and you have requested strict checking."),
    ("host_key_changed", CHANGED_KEY),
    ("host_key_changed", "Offending key for IP in /home/u/.ssh/known_hosts:2\nHost key verification failed."),
    ("authentication_failed", "u@10.1.2.3: Permission denied (publickey).\n"),
    ("authentication_failed", "u@10.1.2.3: Permission denied (publickey,password,keyboard-interactive)."),
    ("authentication_failed", "Received disconnect from 10.1.2.3 port 22:2: Too many authentication failures\nDisconnected from 10.1.2.3 port 22"),
    ("authentication_failed", "No more authentication methods to try."),
    ("name_resolution_failed", "ssh: Could not resolve hostname worker.example: Name or service not known"),
    ("name_resolution_failed", "ssh: Could not resolve hostname worker.example: No such host is known. \r\n"),
    ("connection_refused", "ssh: connect to host 10.1.2.3 port 22: Connection refused"),
    ("connection_timed_out", "ssh: connect to host 10.1.2.3 port 22: Connection timed out"),
    ("connection_timed_out", "ssh: connect to host 10.1.2.3 port 22: Operation timed out"),
    ("connection_timed_out", "Timeout, server 10.1.2.3 not responding."),
    ("network_unreachable", "ssh: connect to host 10.1.2.3 port 22: No route to host"),
    ("network_unreachable", "ssh: connect to host fd00::1 port 22: Network is unreachable"),
    ("connection_closed", "Connection closed by 10.1.2.3 port 22"),
    ("connection_closed", "Connection reset by 10.1.2.3 port 22"),
    ("connection_closed", "kex_exchange_identification: read: Connection reset by peer"),
    ("connection_closed", "kex_exchange_identification: Connection closed by remote host"),
    ("identity_file_problem", BAD_KEY_PERMISSIONS),
    ("identity_file_problem", "no such identity: keys/worker: No such file or directory\nu@10.1.2.3: Permission denied (publickey)."),
    ("identity_file_problem", "Warning: Identity file keys/worker not accessible: No such file or directory.\nPermission denied (publickey)."),
    ("identity_file_problem", 'Load key "keys/worker": invalid format\nPermission denied (publickey).'),
    ("unknown", ""),
    ("unknown", "ssh: something this table has never heard of"),
    ("unknown", "Bad owner or permissions on /home/u/.ssh/config"),
    ("unknown", "Could not create directory '/home/u/.ssh' (Permission denied)."),
    ("unknown", "Unable to negotiate with 10.1.2.3 port 22: no matching host key type found. Their offer: ssh-rsa"),
]


@pytest.mark.parametrize("reason,stderr", CASES)
def test_classifier_maps_openssh_stderr_to_fixed_reason(reason, stderr):
    assert ssh._classify_ssh_failure(255, stderr) == reason
    assert ssh._classify_ssh_failure(255, stderr.upper()) == reason  # case-insensitive
    assert reason in SSH_REASONS


def test_every_reason_code_has_a_case_and_fixed_text():
    assert {reason for reason, _ in CASES} == SSH_REASONS == set(ssh._SSH_FAILURE_TEXT)
    hints = [hint for _, hint in ssh._SSH_FAILURE_TEXT.values()]
    assert len(set(hints)) == len(hints) and all(h.endswith(".") and h.count(". ") == 0 for h in hints)


def test_classifier_ignores_non_ssh_exit_codes_and_missing_stderr():
    assert ssh._classify_ssh_failure(1, "u@h: Permission denied (publickey).") == "unknown"
    assert ssh._classify_ssh_failure(255, None) == "unknown"


@pytest.mark.parametrize("reason,stderr", CASES)
def test_ssh_exit_255_report_has_reason_hint_and_old_sentence(monkeypatch, reason, stderr):
    report = ssh_report(monkeypatch, stderr)
    assert report["status"] == "ssh_failed" and report["exit_code"] == 1 and report["ssh_exit_code"] == 255
    assert BASE_KEYS <= set(report) and report["automatic_fallback"] is False and report["network_access"] is True
    assert report["ssh_failure_reason"] == reason
    assert isinstance(report["hint"], str) and report["hint"] == ssh._SSH_FAILURE_TEXT[reason][1]
    assert "response_reason" not in report
    assert OLD_ERROR in report["error"]
    if reason == "unknown":
        assert report["error"] == OLD_ERROR
    else:
        assert report["error"] == ssh._SSH_FAILURE_TEXT[reason][0] + " " + OLD_ERROR
        assert report["error"].startswith(ssh._SSH_FAILURE_TEXT[reason][0]) and report["error"] != OLD_ERROR


def test_changed_key_beats_unknown_key_and_identity_beats_permission_denied():
    assert "Host key verification failed." in CHANGED_KEY
    assert ssh._classify_ssh_failure(255, CHANGED_KEY) == "host_key_changed"
    assert ssh._classify_ssh_failure(255, "Host key verification failed.\n" + CHANGED_KEY) == "host_key_changed"
    assert "Permission denied (publickey)." in BAD_KEY_PERMISSIONS
    assert ssh._classify_ssh_failure(255, BAD_KEY_PERMISSIONS) == "identity_file_problem"
    assert ssh._classify_ssh_failure(255, "Permission denied (publickey).\n" + BAD_KEY_PERMISSIONS) == "identity_file_problem"


def test_connection_failure_beats_an_earlier_identity_warning_and_local_permission_noise():
    stderr = "Warning: Identity file keys/x not accessible: No such file or directory.\nssh: connect to host 10.1.2.3 port 22: Connection refused"
    assert ssh._classify_ssh_failure(255, stderr) == "connection_refused"
    stderr = "Could not create directory '/home/u/.ssh' (Permission denied).\nu@10.1.2.3: Permission denied (publickey)."
    assert ssh._classify_ssh_failure(255, stderr) == "authentication_failed"


def test_host_key_hints_are_safe_and_authentication_hint_names_batch_mode():
    unknown = ssh._SSH_FAILURE_TEXT["host_key_unknown"][1]
    assert "known_hosts" in unknown and "fingerprint" in unknown and "out of band" in unknown and "never accepts host keys" in unknown
    changed = ssh._SSH_FAILURE_TEXT["host_key_changed"][1]
    assert "no longer matches" in changed and "administrator" in changed and "before altering known_hosts" in changed
    assert not any(word in changed.lower() for word in ("delete", "remove", "ssh-keygen", "accept"))
    auth = ssh._SSH_FAILURE_TEXT["authentication_failed"][1]
    assert "Batch mode cannot prompt for a password or passphrase" in auth and "agent" in auth and "identity option" in auth


# ---- invalid_response -------------------------------------------------------------------------

POSIX_NOT_FOUND = "bash: line 1: adapt: command not found"
RESPONSE_CASES = [
    ("remote_adapt_not_found", 127, "", "", {}),
    ("remote_adapt_not_found", 1, "", POSIX_NOT_FOUND, {}),
    ("remote_adapt_not_found", 1, "", "zsh:1: command not found: adapt", {}),
    ("remote_adapt_not_found", 1, "", "bash: /home/u/bin/adapt: No such file or directory", {"remote_adapt": "/home/u/bin/adapt"}),
    ("remote_adapt_not_found", 1, "", "sh: 1: adapt: not found", {}),
    ("remote_adapt_not_found", 1, "", "The term 'C:\\ADAPT\\adapt.exe' is not recognized as the name of a cmdlet, function, script file, or operable program.",
     {"remote_shell": "powershell", "remote_adapt": "C:\\ADAPT\\adapt.exe"}),
    ("remote_shell_mismatch", 1, "", "'adapt' is not recognized as an internal or external command,\r\noperable program or batch file.", {}),
    ("remote_shell_mismatch", 1, "", "adapt : The term 'adapt' is not recognized as the name of a cmdlet, function, script file, or operable program.", {}),
    ("remote_shell_mismatch", 1, "", "#< CLIXML\n<Objs Version=\"1.1.0.1\"></Objs>", {}),
    ("remote_shell_mismatch", 127, "", "bash: line 1: powershell.exe: command not found", {"remote_shell": "powershell"}),
    ("remote_shell_mismatch", 127, "", "sh: 1: powershell.exe: not found", {"remote_shell": "powershell"}),
    ("remote_shell_mismatch", 127, "", "zsh:1: command not found: powershell.exe", {"remote_shell": "powershell"}),
    ("not_json", 0, "welcome back\n{}", "", {}),
    ("not_json", 0, "[]", "", {}),
    ("not_json", 0, "3", "", {}),
    ("not_json", 1, "Traceback (most recent call last)", "", {}),
    ("empty_response", 0, "", "", {}),
    ("empty_response", 1, " \r\n", "adapt: error: unrelated failure", {}),
    ("empty_response", 1, "", "bash: /etc/motd-extra: No such file or directory", {}),
    ("empty_response", 5, "", "", {}),
    ("unexpected_exit_code", 5, "{}", "", {}),
    ("unexpected_exit_code", 4, json.dumps({"status": "ok"}), "", {}),
]


@pytest.mark.parametrize("reason,code,stdout,stderr,kwargs", RESPONSE_CASES)
def test_invalid_response_gets_response_reason_and_hint(monkeypatch, reason, code, stdout, stderr, kwargs):
    fake(monkeypatch, code, stdout, stderr)
    report = ssh.run("worker", **kwargs)
    assert report["status"] == "invalid_response" and report["exit_code"] == 1 and report["ssh_exit_code"] == code
    assert BASE_KEYS <= set(report) and report["automatic_fallback"] is False
    assert report["error"] == OLD_INVALID_ERROR
    assert report["response_reason"] == reason and reason in RESPONSE_REASONS
    assert report["hint"] == ssh._RESPONSE_TEXT[reason] and "ssh_failure_reason" not in report


def test_every_response_reason_is_covered_and_has_one_sentence_hint():
    assert {case[0] for case in RESPONSE_CASES} == RESPONSE_REASONS == set(ssh._RESPONSE_TEXT)
    assert all(t.endswith(".") and t.count(". ") == 0 for t in ssh._RESPONSE_TEXT.values())


def test_size_limit_invalid_responses_are_unchanged(monkeypatch):
    monkeypatch.setattr(ssh, "MAX_RESPONSE_BYTES", 32)
    fake(monkeypatch, 0, json.dumps({"data": "x" * 100}), "")
    report = ssh.run("worker")
    assert report["status"] == "invalid_response" and not NEW_KEYS & set(report)
    monkeypatch.setattr(ssh, "_execute", lambda args, timeout: (_ for _ in ()).throw(ssh._ResponseLimitError("SSH stderr exceeded the accepted 64 KiB limit.")))
    report = ssh.run("worker")
    assert report["status"] == "invalid_response" and not NEW_KEYS & set(report)


# ---- unchanged outcomes ------------------------------------------------------------------------

@pytest.mark.parametrize("code,status", [(0, "remote_completed"), (2, "remote_reported_failure"), (1, "remote_reported_failure")])
def test_remote_results_are_unchanged_and_carry_no_reason_keys(monkeypatch, code, status):
    fake(monkeypatch, code, json.dumps({"status": "partial"}), "Permission denied (publickey).")
    report = ssh.run("worker", operation="doctor")
    assert report["status"] == status and report["exit_code"] == code and report["ssh_exit_code"] == code
    assert report["remote_report"] == {"status": "partial"}
    assert set(report) == BASE_KEYS | {"ssh_exit_code", "remote_report"} and not NEW_KEYS & set(report)


def test_timeout_and_local_failures_are_unchanged(monkeypatch):
    monkeypatch.setattr(ssh, "_execute", lambda args, timeout: (_ for _ in ()).throw(subprocess.TimeoutExpired(args, timeout)))
    timeout = ssh.run("worker")
    assert timeout["status"] == "timeout" and not NEW_KEYS & set(timeout)
    monkeypatch.setattr(ssh, "_execute", lambda args, timeout: (_ for _ in ()).throw(OSError("sentinel")))
    local = ssh.run("worker")
    assert local["status"] == "ssh_failed" and "ssh_exit_code" not in local and not NEW_KEYS & set(local)


# ---- privacy ------------------------------------------------------------------------------------

SENTINELS = ("sentinel-host.corp.invalid", "203.0.113.77", "sentinel_user", "/home/sentinel_user/.ssh/id_sentinel")
LEAKY_STDERR = [
    "ssh: Could not resolve hostname sentinel-host.corp.invalid: Name or service not known",
    "sentinel_user@203.0.113.77: Permission denied (publickey).",
    "ssh: connect to host 203.0.113.77 port 22: Connection refused",
    "Offending ECDSA key in /home/sentinel_user/.ssh/known_hosts:7\nHost key for sentinel-host.corp.invalid has changed and you have requested strict checking.",
    'Load key "/home/sentinel_user/.ssh/id_sentinel": bad permissions',
    "No ED25519 host key is known for sentinel-host.corp.invalid and you have requested strict checking.",
    "Connection closed by 203.0.113.77 port 22",
    "completely unrecognised text about sentinel_user at sentinel-host.corp.invalid 203.0.113.77 /home/sentinel_user/.ssh/id_sentinel",
]


def test_no_raw_ssh_output_reaches_any_report_hint_or_stream(monkeypatch, capsys):
    reports = []
    leak = " ".join(SENTINELS)
    for stderr in LEAKY_STDERR:
        reports.append(ssh_report(monkeypatch, stderr))
    for code, stdout in ((127, ""), (0, "banner " + leak), (0, ""), (5, json.dumps({"who": leak}))):
        fake(monkeypatch, code, stdout, "bash: line 1: adapt: command not found\n" + leak)
        reports.append(ssh.run("worker"))
    fake(monkeypatch, 0, "banner " + leak, "powershell.exe: not found " + leak)
    reports.append(ssh.run("worker", remote_shell="powershell"))
    assert {r["status"] for r in reports} == {"ssh_failed", "invalid_response"}
    for report in reports:
        dumped = json.dumps(report)
        assert not [s for s in SENTINELS if s in dumped], report
    # sanity: the fixtures really did classify (this is not an all-"unknown" pass)
    assert {r.get("ssh_failure_reason") for r in reports} >= {"name_resolution_failed", "authentication_failed", "host_key_changed", "unknown"}
    captured = capsys.readouterr()
    assert not [s for s in SENTINELS if s in captured.out + captured.err]


def test_classifier_helpers_return_only_fixed_vocabulary():
    for stderr in LEAKY_STDERR:
        assert ssh._classify_ssh_failure(255, stderr) in SSH_REASONS
        assert ssh._classify_invalid_response(1, "x", stderr, json_object=False, remote_shell="posix") in RESPONSE_REASONS
