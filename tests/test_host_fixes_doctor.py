"""Host fixes: doctor agrees with the adb ADAPT's commands use; origin, platform and privilege facts.

Offline only: injected DoctorEnv fakes and monkeypatched probes; no adb, device or network.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

import adapt_adb.adb as adb_mod
import adapt_adb.cli as cli_mod
import adapt_adb.doctor as doctor_mod
from adapt_adb.cli import app
from adapt_adb.doctor import (
    DebianUsbState,
    DoctorEnv,
    EXIT_MISSING_TOOLCHAIN,
    EXIT_OK,
    EXIT_UNSAFE_SHARED_SERVER,
    Server5037State,
    build_next_actions,
    preflight_adb_server,
    render_doctor_human,
    run_doctor_checks,
)

runner = CliRunner()

V35 = "Android Debug Bridge version 1.0.41\nVersion 35.0.2-12147458\n"
V36 = "Android Debug Bridge version 1.0.41\nVersion 36.0.0-13206524\n"
V33 = "Android Debug Bridge version 1.0.41\nVersion 33.0.3-8952118\n"
V29_DEBIAN = "Android Debug Bridge version 1.0.41\nVersion 29.0.6-debian\n"
V35_DISTRO = "Android Debug Bridge version 1.0.41\nVersion 35.0.2-debian\n"


def _checks(report):
    return {c["id"]: c for c in report["checks"]}


def _win_env(tmp_path: Path, *, on_path: bool) -> tuple[DoctorEnv, Path]:
    home = tmp_path / "Users" / "dev"
    local_app = home / "AppData" / "Local"
    pt_dir = local_app / "Android" / "platform-tools"
    adb_exe = pt_dir / "adb.exe"
    env = DoctorEnv(
        host_os="windows",
        arch="AMD64",
        uid_is_root=False,
        home_dir=home,
        environ={"LOCALAPPDATA": str(local_app), "PATH": str(pt_dir) if on_path else ""},
        which_adb=adb_exe if on_path else None,
        which_set=True,
        path_executables=[adb_exe] if on_path else [],
        existing_paths={adb_exe, pt_dir},
        source_properties={pt_dir / "source.properties": "Pkg.Revision=35.0.2\n"},
        version_outputs={adb_exe: V35},
        server_state=Server5037State(listening=False),
        windows_pnp_devices=[],
        devices_l_output="List of devices attached\n\n",
    )
    return env, adb_exe


def _debian_env(tmp_path: Path, *, host_os: str = "debian") -> DoctorEnv:
    home = tmp_path / "home" / "dev"
    return DoctorEnv(
        host_os=host_os,
        arch="x86_64",
        uid_is_root=False,
        home_dir=home,
        environ={"PATH": "/usr/bin"},
        which_adb=None,
        which_set=True,
        path_executables=[],
        existing_paths=set(),
        source_properties={},
        version_outputs={},
        server_state=Server5037State(listening=False),
        debian_usb_state=DebianUsbState(rule_files=["/etc/udev/rules.d/51-android.rules"]),
        devices_l_output="List of devices attached\n\n",
    )


def _off_path_fallback(tmp_path: Path) -> tuple[DoctorEnv, Path]:
    """PATH has no adb; ADAPT's commands resolve the user-scope copy from a fallback folder."""
    env, adb_exe = _win_env(tmp_path, on_path=False)
    env.command_adb = adb_exe
    env.command_adb_source = "fallback_location"
    return env, adb_exe


# ---------------------------------------------------------------------------
# Item 1a/1b: the adb the commands use is computed, reported and described
# ---------------------------------------------------------------------------

def test_off_path_command_adb_is_reported_and_warns_without_claiming_missing(tmp_path):
    env, adb_exe = _off_path_fallback(tmp_path)
    report = run_doctor_checks(env)
    tc = report["toolchain"]
    assert tc["adb_path"] is None  # PATH facts unchanged
    assert tc["command_adb_path"] == str(adb_exe)
    assert tc["command_adb_source"] == "fallback_location"
    assert tc["command_adb_version"] == "35.0.2-12147458"
    assert tc["command_adb_origin"] == "google_official"
    checks = _checks(report)
    for check_id in ("adb_path", "path_config"):
        assert checks[check_id]["status"] == "warn"
        assert checks[check_id]["exit_code"] == EXIT_OK
        assert str(adb_exe) in checks[check_id]["summary"]
        assert "a folder ADAPT searches outside PATH" in checks[check_id]["summary"]
        assert "adapt setup" in checks[check_id]["remediation"]
        assert "PATH" in checks[check_id]["remediation"]
    assert "will not find it" in checks["adb_path"]["summary"]
    assert "missing" not in checks["adb_path"]["summary"].lower()
    assert checks["adb_path"]["details"]["command_adb_source"] == "fallback_location"
    assert checks["adb_version"]["status"] == "pass"
    assert "not on PATH" in checks["adb_version"]["summary"]
    assert checks["adb_origin"]["status"] == "pass"
    assert report["exit_code"] == EXIT_OK
    assert env.devices_l_invoked == [adb_exe]


def test_off_path_human_output_and_next_actions(tmp_path, monkeypatch):
    env, adb_exe = _off_path_fallback(tmp_path)
    monkeypatch.setattr(doctor_mod, "_is_stdout_tty", lambda: False)
    report = run_doctor_checks(env)
    text = render_doctor_human(report)
    assert f"ADB Path: Not on PATH (ADAPT commands use {adb_exe})" in text
    assert "ADB Version: 35.0.2-12147458" in text and "Origin: google_official" in text
    assert "Not found" not in text
    assert "download and install" not in text
    setup = next(a for a in build_next_actions(report) if a["id"] == "setup")
    assert "PATH" in setup["label"] and "downloads only when" in setup["label"]


def test_off_path_command_adb_below_minimum_still_fails_exit_1(tmp_path):
    env, adb_exe = _off_path_fallback(tmp_path)
    env.version_outputs = {adb_exe: V33}
    report = run_doctor_checks(env)
    checks = _checks(report)
    assert checks["adb_path"]["status"] == "fail" and checks["adb_path"]["exit_code"] == EXIT_MISSING_TOOLCHAIN
    assert str(adb_exe) in checks["adb_path"]["summary"] and "older than 34.0.0" in checks["adb_path"]["summary"]
    assert checks["path_config"]["status"] == "fail"
    assert checks["adb_version"]["status"] == "fail"
    assert report["exit_code"] == EXIT_MISSING_TOOLCHAIN
    assert env.devices_l_invoked == []


def test_nothing_anywhere_keeps_the_existing_missing_contract(tmp_path):
    env, _ = _win_env(tmp_path, on_path=False)
    env.existing_paths = set()
    report = run_doctor_checks(env)
    checks = _checks(report)
    assert checks["adb_path"]["summary"] == "No adb executable found on PATH."
    assert checks["adb_path"]["details"]["command_adb_source"] == "none"
    assert report["toolchain"]["command_adb_path"] is None
    assert report["exit_code"] == EXIT_MISSING_TOOLCHAIN


def test_override_to_missing_file_is_reported_by_name_only(tmp_path):
    env, _ = _win_env(tmp_path, on_path=True)
    secret = str(tmp_path / "SECRET_DIR" / "adb.exe")
    env.environ["ADAPT_ADB"] = secret
    report = run_doctor_checks(env)
    check = _checks(report)["adb_path"]
    assert check["status"] == "warn" and check["exit_code"] == EXIT_OK
    assert "ADAPT_ADB is set but does not name an existing file; ADAPT ignores it." in check["summary"]
    assert check["remediation"] == "Correct or unset ADAPT_ADB."
    assert check["details"]["override_variable"] == "ADAPT_ADB"
    assert check["details"]["override_points_to_file"] is False
    assert "SECRET_DIR" not in json.dumps(report)
    assert report["exit_code"] == EXIT_OK


def test_override_to_missing_file_with_nothing_else_fails_and_says_so(tmp_path):
    env, _ = _win_env(tmp_path, on_path=False)
    env.existing_paths = set()
    env.environ["ADB_PATH"] = str(tmp_path / "nope" / "adb.exe")
    check = _checks(run_doctor_checks(env))["adb_path"]
    assert check["status"] == "fail"
    assert check["summary"].startswith("No adb executable found on PATH.")
    assert "ADB_PATH is set but does not name an existing file" in check["summary"]


def test_injected_default_uses_a_valid_override_like_resolve_adb(tmp_path):
    env, _ = _win_env(tmp_path, on_path=False)
    override = tmp_path / "tools" / "adb.exe"
    env.existing_paths.add(override)
    env.version_outputs[override] = V35
    env.environ["ADAPT_ADB"] = str(override)
    report = run_doctor_checks(env)
    assert report["toolchain"]["command_adb_source"] == "override"
    check = _checks(report)["adb_path"]
    assert check["status"] == "warn"
    assert "the ADAPT_ADB / ADB_PATH environment variable" in check["summary"]
    assert check["details"]["override_points_to_file"] is True


def test_real_resolution_is_delegated_to_adb_resolve_adb_details(monkeypatch):
    fake = adb_mod.AdbResolution(Path("/opt/x/adb"), "fallback_location", "ADAPT_ADB", True)
    monkeypatch.setattr(doctor_mod, "resolve_adb_details", lambda: fake)
    result = doctor_mod._resolve_command_adb({}, None, None)
    assert result == {"path": Path("/opt/x/adb"), "source": "fallback_location",
                      "override_var": "ADAPT_ADB", "override_missing": True}


def test_resolve_adb_details_reports_source_and_keeps_resolve_adb_behaviour(tmp_path, monkeypatch):
    old_path_bin = tmp_path / "old_adb"
    cache_bin = tmp_path / "cache_adb"
    override_bin = tmp_path / "override_adb"
    for f in (old_path_bin, cache_bin, override_bin):
        f.write_bytes(b"fake")
    monkeypatch.setattr("os.access", lambda p, m: True)
    monkeypatch.setattr(adb_mod, "get_search_locations",
                        lambda: [("PATH", old_path_bin), ("~/.local/share/android/platform-tools", cache_bin)])

    def fake_run(argv, **kwargs):
        out = V29_DEBIAN if Path(argv[0]) == old_path_bin.resolve() else V35
        return MagicMock(stdout=out, stderr="", returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.delenv("ADAPT_ADB", raising=False)
    monkeypatch.delenv("ADB_PATH", raising=False)
    res = adb_mod.resolve_adb_details()
    assert (res.path, res.source, res.override_var, res.override_missing) == (cache_bin.resolve(), "fallback_location", None, False)
    assert adb_mod.resolve_adb() == cache_bin.resolve()

    monkeypatch.setenv("ADB_PATH", str(tmp_path / "missing_adb"))
    res = adb_mod.resolve_adb_details()
    assert res.source == "fallback_location" and res.override_var == "ADB_PATH" and res.override_missing is True

    monkeypatch.setenv("ADAPT_ADB", str(override_bin))
    res = adb_mod.resolve_adb_details()
    assert (res.path, res.source, res.override_var) == (override_bin.resolve(), "override", "ADAPT_ADB")

    monkeypatch.delenv("ADAPT_ADB")
    monkeypatch.delenv("ADB_PATH")
    monkeypatch.setattr(adb_mod, "get_search_locations", lambda: [("PATH", cache_bin)])
    assert adb_mod.resolve_adb_details().source == "path"
    monkeypatch.setattr(adb_mod, "get_search_locations", lambda: [("PATH", None)])
    assert adb_mod.resolve_adb_details() == adb_mod.AdbResolution(None, "none", None, False)


# ---------------------------------------------------------------------------
# Item 1c: the port-5037 check verifies against the adb the commands use
# ---------------------------------------------------------------------------

def _own_server(exe: Path, version: str) -> Server5037State:
    return Server5037State(listening=True, pid=4242, owner="HOST\\dev", owner_matches_current_user=True,
                           server_exe=exe, server_version_raw=version)


def test_server_started_from_the_command_adb_is_verified_not_unsafe(tmp_path):
    env, adb_exe = _off_path_fallback(tmp_path)
    env.server_state = _own_server(adb_exe, V35)
    report = run_doctor_checks(env)
    srv = _checks(report)["adb_server_5037"]
    assert srv["status"] == "pass" and srv["exit_code"] == EXIT_OK
    assert srv["details"]["client_adb"] == str(adb_exe)
    assert srv["details"]["client_adb_source"] == "fallback_location"
    assert srv["details"]["client_version"] == "35.0.2-12147458"
    assert report["exit_code"] == EXIT_OK


def test_before_the_fix_this_server_had_no_trusted_client_and_now_has_one(tmp_path, monkeypatch):
    env, adb_exe = _off_path_fallback(tmp_path)
    env.server_state = None
    seen = {}

    def fake_probe(host_os, home, env_arg=None, *, trusted_adb=None):
        seen["trusted"] = trusted_adb
        return _own_server(adb_exe, V35)

    monkeypatch.setattr(doctor_mod, "_probe_server_5037", fake_probe)
    run_doctor_checks(env)
    assert seen["trusted"] == adb_exe


def test_setting_adapt_adb_clears_the_unverified_server_exit_3(tmp_path):
    env, _ = _win_env(tmp_path, on_path=False)
    env.existing_paths = set()
    override = tmp_path / "tools" / "adb.exe"
    env.server_state = _own_server(override, V36)
    assert run_doctor_checks(env)["exit_code"] == EXIT_UNSAFE_SHARED_SERVER
    env.existing_paths.add(override)
    env.version_outputs[override] = V36
    env.environ["ADAPT_ADB"] = str(override)
    report = run_doctor_checks(env)
    assert _checks(report)["adb_server_5037"]["status"] == "pass"
    assert report["exit_code"] == EXIT_OK


@pytest.mark.parametrize("state, fragment", [
    (Server5037State(listening=True, pid=1, owner="root", owner_uid=0, owner_is_root_or_system=True,
                     owner_matches_current_user=False), "root/SYSTEM or another user"),
    (Server5037State(listening=True, pid=1, owner="other", owner_matches_current_user=False), "root/SYSTEM or another user"),
    (Server5037State(listening=True, probe_error="Multiple local TCP 5037 listener owners."), "inspection failed"),
    (Server5037State(listening=True, pid=1, owner=None), "unverified"),
])
def test_ownership_and_fail_closed_rules_are_not_weakened(tmp_path, state, fragment):
    env, _ = _off_path_fallback(tmp_path)
    env.server_state = state
    report = run_doctor_checks(env)
    srv = _checks(report)["adb_server_5037"]
    assert srv["status"] == "fail" and srv["exit_code"] == EXIT_UNSAFE_SHARED_SERVER
    assert fragment in srv["summary"]
    assert report["exit_code"] == EXIT_UNSAFE_SHARED_SERVER
    assert env.devices_l_invoked == []


def test_version_mismatch_against_the_command_adb_still_fails_3(tmp_path):
    env, adb_exe = _off_path_fallback(tmp_path)
    env.server_state = _own_server(adb_exe, V36)
    srv = _checks(run_doctor_checks(env))["adb_server_5037"]
    assert srv["exit_code"] == EXIT_UNSAFE_SHARED_SERVER
    assert srv["details"]["version_mismatch"] is True
    assert env.devices_l_invoked == []


# ---------------------------------------------------------------------------
# Item 2: a distribution adb that meets the minimum is a warning, not a failure
# ---------------------------------------------------------------------------

def _distro_env(tmp_path, version: str, host_os: str = "debian") -> tuple[DoctorEnv, Path]:
    env = _debian_env(tmp_path, host_os=host_os)
    apt_adb = Path("/usr/bin/adb")
    env.which_adb = apt_adb
    env.path_executables = [apt_adb]
    env.existing_paths = {apt_adb}
    env.version_outputs = {apt_adb: version}
    env.dpkg_owners = {apt_adb: "adb"}
    return env, apt_adb


def test_distribution_adb_35_warns_with_exit_0_and_is_usable(tmp_path):
    env, apt_adb = _distro_env(tmp_path, V35_DISTRO)
    report = run_doctor_checks(env)
    checks = _checks(report)
    origin = checks["adb_origin"]
    assert origin["status"] == "warn" and origin["exit_code"] == EXIT_OK
    assert "meets the minimum" in origin["summary"] and "Debian apt" in origin["summary"]
    assert "not verified Google platform-tools" in origin["summary"]
    assert "lags" not in origin["summary"]
    assert checks["adb_version"]["status"] == "pass"
    assert checks["path_config"]["status"] == "pass"
    assert checks["devices_state"]["status"] == "pass"
    assert env.devices_l_invoked == [apt_adb]
    assert report["toolchain"]["origin"] == "debian_apt"
    assert report["exit_code"] == EXIT_OK


def test_distribution_adb_35_on_other_linux_names_the_distribution(tmp_path):
    env, _ = _distro_env(tmp_path, V35_DISTRO, host_os="linux")
    origin = _checks(run_doctor_checks(env))["adb_origin"]
    assert origin["status"] == "warn" and "the Linux distribution" in origin["summary"]
    assert "Debian" not in origin["summary"]


@pytest.mark.parametrize("host_os", ["debian", "linux"])
def test_distribution_adb_below_34_still_fails(tmp_path, host_os):
    env, _ = _distro_env(tmp_path, V29_DEBIAN, host_os=host_os)
    report = run_doctor_checks(env)
    origin = _checks(report)["adb_origin"]
    assert origin["status"] == "fail" and origin["exit_code"] == EXIT_MISSING_TOOLCHAIN
    assert report["exit_code"] == EXIT_MISSING_TOOLCHAIN
    assert env.devices_l_invoked == []


def test_setup_treats_a_distribution_adb_35_as_already_healthy(tmp_path):
    env, _ = _distro_env(tmp_path, V35_DISTRO)
    report = doctor_mod.run_setup_workflow(env, yes=True, downloader=lambda *a, **k: pytest.fail("downloaded"))
    assert report["setup"]["action"] == "noop_already_healthy"
    assert report["exit_code"] == EXIT_OK


def test_everything_counts_the_new_setup_actions_as_ready(monkeypatch):
    from adapt_adb import everything as batch
    from adapt_adb.tool_catalog import HostEnvironment, build_list_report

    host = HostEnvironment("windows", "windows-native", "x86_64", "test", "Synthetic Windows", False)
    inventory = build_list_report(host=host, which=lambda _: None)
    monkeypatch.setattr(batch, "build_list_report", lambda: inventory)
    monkeypatch.setattr(batch, "plan_scrcpy", lambda: {"status": "manual"})
    monkeypatch.setattr(batch.device_state, "scan", lambda **k: {"status": "observed", "transports": {}})
    monkeypatch.setattr(batch, "run_doctor_checks", lambda: {"exit_code": 0})
    for action in ("path_registered", "noop_already_installed"):
        monkeypatch.setattr(batch, "run_setup_workflow",
                            lambda action=action, **k: {"exit_code": 0, "setup": {"action": action}})
        result = batch.run_everything(yes=True)
        step = next(s for s in result["steps"] if s["step"] == "platform-tools")
        assert step["status"] == "reused", action


# ---------------------------------------------------------------------------
# Item 3: root / elevated facts in doctor output
# ---------------------------------------------------------------------------

def test_doctor_human_output_warns_when_running_as_root(tmp_path, monkeypatch):
    env, _ = _distro_env(tmp_path, V35_DISTRO)
    env.uid_is_root = True
    monkeypatch.setattr(doctor_mod, "_is_stdout_tty", lambda: False)
    report = run_doctor_checks(env)
    assert report["host"]["uid_is_root"] is True and report["host"]["elevated"] is False
    text = render_doctor_human(report)
    assert "Warning: ADAPT is running as root." in text
    assert "Run ADAPT as your normal user, without sudo." in text
    assert text.count("Warning:") == 1
    assert report["exit_code"] == EXIT_OK


def test_doctor_human_output_warns_in_an_elevated_windows_session(tmp_path, monkeypatch):
    env, _ = _win_env(tmp_path, on_path=True)
    env.elevated = True
    monkeypatch.setattr(doctor_mod, "_is_stdout_tty", lambda: False)
    report = run_doctor_checks(env)
    assert report["host"]["elevated"] is True
    text = render_doctor_human(report)
    assert "elevated (Administrator) Windows session" in text
    assert "cannot share it" in text


def test_terminal_renderer_also_shows_the_warning(tmp_path, monkeypatch):
    env, _ = _win_env(tmp_path, on_path=True)
    env.elevated = True
    monkeypatch.setattr(doctor_mod, "_is_stdout_tty", lambda: True)
    text = render_doctor_human(run_doctor_checks(env))
    assert text.startswith("Warning: ADAPT is running in an elevated")


def test_no_privilege_warning_for_a_normal_user(tmp_path, monkeypatch):
    env, _ = _win_env(tmp_path, on_path=True)
    monkeypatch.setattr(doctor_mod, "_is_stdout_tty", lambda: False)
    text = render_doctor_human(run_doctor_checks(env))
    assert "Warning:" not in text


# ---------------------------------------------------------------------------
# Item 4: host OS detection; non-Debian Linux and macOS
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ('NAME="Ubuntu"\nID=ubuntu\nID_LIKE=debian\n', "debian"),
    ("ID=debian\n", "debian"),
    ('ID=linuxmint\nID_LIKE="ubuntu debian"\n', "debian"),
    ("ID=raspbian\nID_LIKE=debian\n", "debian"),
    ('ID=fedora\nHOME_URL="https://fedoraproject.org/"\n', "linux"),
    ("ID=arch\n", "linux"),
    ('ID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"\n', "linux"),
    ('ID=alpine\nBUG_REPORT_URL="https://debian.example/"\n', "linux"),
])
def test_linux_distribution_family_comes_from_os_release_ids(text, expected):
    assert doctor_mod._classify_host_os("linux", text, False) == expected


def test_host_os_classification_fallbacks():
    assert doctor_mod._classify_host_os("linux", None, True) == "debian"
    assert doctor_mod._classify_host_os("linux", None, False) == "linux"
    assert doctor_mod._classify_host_os("linux", "", True) == "debian"
    assert doctor_mod._classify_host_os("darwin", None, False) == "macos"
    assert doctor_mod._classify_host_os("win32", "ID=debian", True) == "windows"


def test_detect_host_os_reads_the_injectable_os_release_path(tmp_path, monkeypatch):
    release = tmp_path / "os-release"
    release.write_text("ID=fedora\n", encoding="utf-8")
    monkeypatch.setattr(doctor_mod, "_OS_RELEASE_PATH", release)
    monkeypatch.setattr(doctor_mod, "_DEBIAN_VERSION_PATH", tmp_path / "no-debian_version")
    monkeypatch.setattr(doctor_mod.sys, "platform", "linux")
    assert doctor_mod._detect_host_os() == "linux"
    release.write_text("ID=ubuntu\nID_LIKE=debian\n", encoding="utf-8")
    assert doctor_mod._detect_host_os() == "debian"


def _usb_failing(env: DoctorEnv) -> DoctorEnv:
    env.debian_usb_state = DebianUsbState(has_udev_rules=False, has_uaccess_tag=False, has_plugdev_rule=False,
                                          in_plugdev_group=False, seat="headless")
    return env


def test_non_debian_linux_remediation_has_no_apt_and_does_not_assume_plugdev(tmp_path):
    env = _usb_failing(_distro_env(tmp_path, V35_DISTRO, host_os="linux")[0])
    check = _checks(run_doctor_checks(env))["debian_usb_permissions"]
    text = check["remediation"]
    assert check["status"] == "fail"
    assert "apt install" not in text and "sudo apt" not in text
    assert "usermod" not in text
    assert "your distribution" in text and "differ" in text
    assert "log out and back in" in text


def test_debian_remediation_keeps_apt_plugdev_and_uses_the_real_rules_path(tmp_path):
    from adapt_adb.hints import find_udev_rules_file

    env = _usb_failing(_distro_env(tmp_path, V35_DISTRO)[0])
    text = _checks(run_doctor_checks(env))["debian_usb_permissions"]["remediation"]
    assert "sudo apt install android-sdk-platform-tools-common" in text
    assert "sudo usermod -aG plugdev $USER" in text
    assert str(find_udev_rules_file()) in text
    assert "log out and back in" in text


def test_macos_doctor_says_unsupported_and_skips_linux_checks(tmp_path, monkeypatch):
    env = _debian_env(tmp_path, host_os="macos")
    env.server_state = None  # the real probe must not read Linux /proc on macOS
    monkeypatch.setattr(doctor_mod, "_PROC_ROOT", Path("/definitely/not/proc"))
    monkeypatch.setattr(doctor_mod, "_TMP_5037_PATH", tmp_path / "never-read")
    monkeypatch.setattr(doctor_mod, "_is_stdout_tty", lambda: False)
    report = run_doctor_checks(env)
    checks = _checks(report)
    assert report["host"]["os"] == "macos" and report["host"]["supported"] is False
    assert checks["debian_usb_permissions"]["status"] == "skip"
    assert "not a supported ADAPT platform" in checks["debian_usb_permissions"]["summary"]
    assert checks["windows_pnp"]["status"] == "skip"
    srv = checks["adb_server_5037"]
    assert srv["status"] == "fail" and srv["exit_code"] == EXIT_UNSAFE_SHARED_SERVER
    assert "macOS is not a supported ADAPT host" in srv["summary"]
    text = render_doctor_human(report)
    assert "Platform: macos is not a supported ADAPT host" in text
    assert "Hint: Run 'adapt setup'" not in text
    ids = [a["id"] for a in build_next_actions(report)]
    assert "udev" not in ids and "setup" not in ids


def test_macos_probe_fails_closed_without_listener_inspection(tmp_path):
    state = doctor_mod._probe_server_5037("macos", tmp_path)
    assert state.probe_error and not state.listening


def test_commands_refuse_the_shared_server_on_macos(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor_mod, "_detect_host_os", lambda env=None: "macos")
    with pytest.raises(doctor_mod.UnsafeAdbServerError) as info:
        preflight_adb_server(tmp_path / "adb", ["devices"], environ={})
    assert getattr(info.value, "failure_id") == "adb.server.probe_failed"


def test_host_os_schema_value_reaches_json(tmp_path, monkeypatch):
    env = _debian_env(tmp_path, host_os="macos")
    monkeypatch.setattr(cli_mod, "_ACTIVE_DOCTOR_ENV", env)
    result = runner.invoke(app, ["doctor", "--json"])
    doc = json.loads(result.stdout)
    assert doc["host"]["os"] == "macos" and doc["host"]["supported"] is False
    assert result.exit_code == doc["exit_code"]
