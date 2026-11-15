"""The managed adb `adapt setup` registered, used when a shell's PATH lacks its folder.

Offline only: injected DoctorEnv fakes, temporary folders and monkeypatched probes. No real
adb runs, no device or network is touched, and no ADB server is started or stopped.
"""

from __future__ import annotations

import os
import types
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import adapt_adb.adb as adb_mod
import adapt_adb.doctor as doctor_mod
from adapt_adb.adb import (
    MANAGED_PATH_MARKER,
    REGISTERED_MANAGED_SOURCE,
    find_registered_managed_adb,
    managed_path_export_line,
    read_profile_text_bounded,
)
from adapt_adb.doctor import (
    DebianUsbState,
    DoctorEnv,
    Server5037State,
    ensure_debian_user_path,
    render_doctor_human,
    run_doctor_checks,
)

# Captured at import, before tests/conftest.py disables the real lookup for every test.
REAL_LOOKUP = adb_mod._real_managed_registration

V35 = "Android Debug Bridge version 1.0.41\nVersion 35.0.2-12147458\n"
V36 = "Android Debug Bridge version 1.0.41\nVersion 36.0.0-13206524\n"
V33 = "Android Debug Bridge version 1.0.41\nVersion 33.0.3-8952118\n"
V34_DISTRO = "Android Debug Bridge version 1.0.41\nVersion 34.0.5-debian\n"

HOME = Path("/home/example")
MANAGED_DIR = HOME / ".local" / "share" / "android" / "platform-tools"
MANAGED_ADB = MANAGED_DIR / "adb"
ISOLATED_DIR = HOME / ".android" / "platform-tools"
ISOLATED_ADB = ISOLATED_DIR / "adb"
DISTRO_ADB = Path("/usr/bin/adb")
PROFILE = HOME / ".profile"


def setup_block(folder: Path) -> str:
    """What `adapt setup` appends to ~/.profile."""
    return f"\n{MANAGED_PATH_MARKER}\n{managed_path_export_line(folder)}\n"


# ---------------------------------------------------------------------------
# The rule itself (adb.find_registered_managed_adb)
# ---------------------------------------------------------------------------


def find(*, environ=None, path_adb=DISTRO_ADB, text=None, usable=(MANAGED_ADB, ISOLATED_ADB), on_path=()):
    return find_registered_managed_adb(
        {"PATH": "/usr/bin"} if environ is None else environ,
        HOME,
        path_adb,
        is_usable=lambda p: p in usable,
        read_text=lambda p: (setup_block(MANAGED_DIR) if text is None else text) if p == PROFILE else None,
        on_path=lambda folder: folder in on_path,
        same_file=lambda a, b: a == b,
    )


def test_trigger_selects_the_registered_managed_copy():
    found = find()
    assert found is not None
    assert (found.adb, found.folder, found.profile, found.path_adb) == (MANAGED_ADB, MANAGED_DIR, PROFILE, DISTRO_ADB)


@pytest.mark.parametrize("environ", [
    {"PATH": "/usr/bin", "ADAPT_ADB": "/opt/tools/adb"},
    {"PATH": "/usr/bin", "ADB_PATH": "/opt/tools/adb"},
    {"PATH": "/usr/bin", "ADB_PATH": "/missing/adb"},  # set but invalid still counts as the user's choice
])
def test_an_override_variable_disables_it(environ):
    assert find(environ=environ) is None


@pytest.mark.parametrize("text", [
    "",
    f"\n{managed_path_export_line(MANAGED_DIR)}\n",                          # PATH line, no marker
    f"\n{MANAGED_PATH_MARKER}\n",                                            # marker, no PATH line
    f"\n{MANAGED_PATH_MARKER}\n# {managed_path_export_line(MANAGED_DIR)}\n",  # PATH line commented out
    setup_block(Path("/opt/elsewhere/platform-tools")),                      # another folder
    f"\nexport PATH={MANAGED_DIR}:$PATH\n",                                  # hand-written, not setup's
])
def test_without_setups_marker_and_exact_path_line_it_does_not_apply(text):
    assert find(text=text) is None


def test_a_missing_or_unreadable_profile_does_not_apply():
    assert find_registered_managed_adb(
        {"PATH": "/usr/bin"}, HOME, DISTRO_ADB,
        is_usable=lambda p: True, read_text=lambda p: None,
        on_path=lambda folder: False, same_file=lambda a, b: a == b,
    ) is None


def test_a_missing_or_unusable_managed_copy_does_not_apply():
    assert find(usable=()) is None


def test_managed_folder_on_path_keeps_path_order():
    # The user put the managed folder on PATH, possibly after another adb on purpose.
    assert find(on_path=(MANAGED_DIR,)) is None


def test_no_other_adb_on_path_does_not_apply():
    assert find(path_adb=None) is None
    assert find(path_adb=MANAGED_ADB) is None


def test_isolated_setup_folder_counts_and_the_last_registered_folder_wins():
    found = find(text=setup_block(ISOLATED_DIR))
    assert found is not None and found.adb == ISOLATED_ADB
    found = find(text=setup_block(ISOLATED_DIR) + setup_block(MANAGED_DIR))
    assert found is not None and found.adb == MANAGED_ADB
    # When either registered folder is already on PATH, PATH order wins.
    assert find(text=setup_block(ISOLATED_DIR) + setup_block(MANAGED_DIR), on_path=(ISOLATED_DIR,)) is None


def test_setups_profile_writer_and_this_reader_agree():
    env = DoctorEnv(debian_profile_texts={PROFILE: "# existing\n"})
    assert ensure_debian_user_path(MANAGED_DIR, HOME, env) is True
    assert find(text=env.debian_profile_texts[PROFILE]) is not None


# ---------------------------------------------------------------------------
# Reading the profile: read-only, bounded, tolerant
# ---------------------------------------------------------------------------


def test_profile_read_is_bounded_read_only_and_tolerant(tmp_path):
    assert read_profile_text_bounded(tmp_path / "missing") is None
    assert read_profile_text_bounded(tmp_path) is None  # a folder, not a file
    profile = tmp_path / ".profile"
    profile.write_bytes(setup_block(MANAGED_DIR).encode())
    before = (profile.stat().st_mtime_ns, profile.read_bytes())
    assert read_profile_text_bounded(profile) == setup_block(MANAGED_DIR)
    assert (profile.stat().st_mtime_ns, profile.read_bytes()) == before
    profile.write_bytes(b"x" * 101)
    assert read_profile_text_bounded(profile, limit=100) is None
    profile.write_bytes(b"\xff\xfe" + MANAGED_PATH_MARKER.encode())
    assert MANAGED_PATH_MARKER in read_profile_text_bounded(profile)


def test_profile_read_errors_are_tolerated(tmp_path, monkeypatch):
    profile = tmp_path / ".profile"
    profile.write_text("x")

    def deny(*_args, **_kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "open", deny)
    assert read_profile_text_bounded(profile) is None


# ---------------------------------------------------------------------------
# What ADAPT's commands use (adb.resolve_adb_details), with temporary folders
# ---------------------------------------------------------------------------


def _real_host(tmp_path, monkeypatch, *, profile_text=None, on_path=False, platform="linux",
               managed_version=V35, override=None):
    """A POSIX-like host in tmp_path: managed copy, another adb on PATH, and ~/.profile."""
    home = tmp_path / "home"
    managed_dir = home / ".local" / "share" / "android" / "platform-tools"
    other_dir = tmp_path / "usr" / "bin"
    managed_dir.mkdir(parents=True)
    other_dir.mkdir(parents=True)
    managed, other = managed_dir / "adb", other_dir / "adb"
    for f in (managed, other):
        f.write_bytes(b"not a real adb")
        f.chmod(0o755)
    (home / ".profile").write_text(setup_block(managed_dir) if profile_text is None else profile_text)

    monkeypatch.setattr(adb_mod, "_real_managed_registration", REAL_LOOKUP)
    monkeypatch.setattr(adb_mod, "sys", types.SimpleNamespace(platform=platform))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(adb_mod, "get_search_locations",
                        lambda: [("PATH", other.resolve()), ("~/.local/share/android/platform-tools", managed)])
    path_entries = [str(other_dir)] + ([str(managed_dir)] if on_path else [])
    monkeypatch.setenv("PATH", os.pathsep.join(path_entries))
    monkeypatch.delenv("ADAPT_ADB", raising=False)
    monkeypatch.delenv("ADB_PATH", raising=False)
    if override:
        monkeypatch.setenv(*override)
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append([str(a) for a in argv])
        assert argv[1:] == ["version"], argv  # resolution only ever asks for the version
        out = managed_version if Path(argv[0]).resolve() == managed.resolve() else V34_DISTRO
        return MagicMock(stdout=out, stderr="", returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    return types.SimpleNamespace(home=home, managed=managed, managed_dir=managed_dir, other=other, calls=calls)


def test_commands_use_the_registered_copy_when_path_lacks_it(tmp_path, monkeypatch):
    host = _real_host(tmp_path, monkeypatch)
    res = adb_mod.resolve_adb_details()
    assert (res.path, res.source, res.override_var, res.override_missing) == (
        host.managed.resolve(), REGISTERED_MANAGED_SOURCE, None, False)
    assert res.registration.folder == host.managed_dir
    assert res.registration.profile == host.home / ".profile"
    assert adb_mod.resolve_adb() == host.managed.resolve()
    assert all(call[1:] == ["version"] for call in host.calls)


def test_commands_keep_path_order_when_the_folder_is_on_path(tmp_path, monkeypatch):
    host = _real_host(tmp_path, monkeypatch, on_path=True)
    res = adb_mod.resolve_adb_details()
    assert (res.path, res.source, res.registration) == (host.other.resolve(), "path", None)


@pytest.mark.parametrize("case", ["no_marker", "too_old", "override", "windows"])
def test_commands_are_unchanged_when_a_condition_fails(tmp_path, monkeypatch, case):
    kwargs = {
        "no_marker": {"profile_text": "export PATH=\"$HOME/bin:$PATH\"\n"},
        "too_old": {"managed_version": V33},
        "override": {"override": ("ADB_PATH", str(tmp_path / "missing" / "adb"))},
        "windows": {"platform": "win32"},
    }[case]
    host = _real_host(tmp_path, monkeypatch, **kwargs)
    res = adb_mod.resolve_adb_details()
    assert res.source == "path" and res.path == host.other.resolve() and res.registration is None


def test_doctor_real_mode_reports_what_the_commands_use(tmp_path, monkeypatch):
    host = _real_host(tmp_path, monkeypatch)
    result = doctor_mod._resolve_command_adb({}, None, None)
    assert result["path"] == host.managed.resolve()
    assert result["source"] == REGISTERED_MANAGED_SOURCE
    assert result["registration"] == adb_mod.resolve_adb_details().registration


def test_injected_doctor_and_real_commands_agree_on_the_same_folders(tmp_path, monkeypatch):
    host = _real_host(tmp_path, monkeypatch)
    real = adb_mod.resolve_adb_details()
    env = DoctorEnv(
        host_os="debian", arch="x86_64", uid_is_root=False, home_dir=host.home,
        environ={"PATH": str(host.other.parent)}, which_adb=host.other, which_set=True,
        path_executables=[host.other], version_outputs={host.other: V34_DISTRO, host.managed: V35},
        dpkg_owners={host.other: "adb", host.managed: None},
        server_state=Server5037State(listening=False), debian_usb_state=DebianUsbState(),
        devices_l_output="List of devices attached\n\n",
    )
    report = run_doctor_checks(env)
    assert Path(report["toolchain"]["command_adb_path"]).resolve() == real.path
    assert report["toolchain"]["command_adb_source"] == real.source == REGISTERED_MANAGED_SOURCE


# ---------------------------------------------------------------------------
# adapt doctor with injected environments
# ---------------------------------------------------------------------------


def debian_env(**changes) -> DoctorEnv:
    """Managed copy registered in ~/.profile; this shell's PATH has only the distribution adb."""
    env = DoctorEnv(
        host_os="debian", arch="x86_64", uid_is_root=False, home_dir=HOME,
        environ={"PATH": "/usr/local/bin:/usr/bin:/bin"},
        which_adb=DISTRO_ADB, which_set=True, path_executables=[DISTRO_ADB],
        existing_paths={DISTRO_ADB, MANAGED_ADB, MANAGED_DIR},
        source_properties={MANAGED_DIR / "source.properties": "Pkg.Revision=35.0.2\n"},
        dpkg_owners={DISTRO_ADB: "adb", MANAGED_ADB: None},
        version_outputs={DISTRO_ADB: V34_DISTRO, MANAGED_ADB: V35},
        server_state=Server5037State(listening=False),
        debian_usb_state=DebianUsbState(),
        debian_profile_texts={PROFILE: "# user settings\n" + setup_block(MANAGED_DIR)},
        devices_l_output="List of devices attached\n\n",
    )
    return replace(env, **changes)


def _checks(report):
    return {c["id"]: c for c in report["checks"]}


def test_doctor_reports_the_registered_copy_and_the_missing_path_entry():
    report = run_doctor_checks(debian_env())
    tc = report["toolchain"]
    assert tc["command_adb_path"] == str(MANAGED_ADB)
    assert tc["command_adb_source"] == REGISTERED_MANAGED_SOURCE
    assert tc["command_adb_version"] == "35.0.2-12147458"
    assert tc["command_adb_origin"] == "google_official"
    # The PATH facts still describe PATH.
    assert tc["adb_path"] == str(DISTRO_ADB)
    assert tc["origin"] == "debian_apt"
    assert tc["registered_managed_copy"] == {
        "adb": str(MANAGED_ADB), "folder": str(MANAGED_DIR), "registration_file": str(PROFILE),
        "folder_on_path": False, "path_adb": str(DISTRO_ADB),
    }
    checks = _checks(report)
    path_check = checks["path_config"]
    assert (path_check["status"], path_check["failure_id"]) == ("warn", "adb.managed_not_on_path")
    for fragment in (str(MANAGED_ADB), str(MANAGED_DIR), str(PROFILE), str(DISTRO_ADB), "does not include"):
        assert fragment in path_check["summary"]
    assert str(MANAGED_DIR) in path_check["remediation"]
    assert path_check["details"]["command_adb_source"] == REGISTERED_MANAGED_SOURCE
    assert path_check["details"]["registered_managed_copy"] == tc["registered_managed_copy"]
    # Version and origin judge the copy the commands use.
    assert checks["adb_version"]["status"] == "pass" and "registered managed adb" in checks["adb_version"]["summary"]
    assert checks["adb_origin"]["status"] == "pass"
    assert str(MANAGED_ADB) in checks["adb_path"]["summary"]
    assert checks["adb_server_5037"]["details"]["client_adb"] == str(MANAGED_ADB)
    assert checks["adb_server_5037"]["details"]["client_adb_source"] == REGISTERED_MANAGED_SOURCE
    assert report["exit_code"] == 0


def test_doctor_json_shape_only_adds_keys():
    report = run_doctor_checks(debian_env())
    baseline = run_doctor_checks(debian_env(debian_profile_texts={PROFILE: "# user settings\n"}))
    assert set(baseline["toolchain"]) <= set(report["toolchain"])
    assert set(report["toolchain"]) == set(baseline["toolchain"])  # the new key is always present
    assert baseline["toolchain"]["registered_managed_copy"] is None
    for check_id, check in _checks(baseline).items():
        assert set(check) == set(_checks(report)[check_id])
    for check_id in ("adb_path", "path_config", "adb_server_5037"):
        assert set(_checks(baseline)[check_id]["details"]) <= set(_checks(report)[check_id]["details"])
    assert _checks(baseline)["path_config"]["details"]["registered_managed_copy"] is None


def test_human_report_names_the_adb_the_commands_use():
    text = render_doctor_human(run_doctor_checks(debian_env()))
    assert f"ADAPT commands use: {MANAGED_ADB}" in text
    assert "PATH in this shell does not include its folder" in text
    assert "ADAPT commands use:" not in render_doctor_human(
        run_doctor_checks(debian_env(debian_profile_texts={PROFILE: ""})))


@pytest.mark.parametrize("case", [
    "override_valid", "override_missing", "no_marker", "managed_missing", "managed_too_old",
    "folder_on_path", "folder_on_path_after_another_adb", "no_other_adb", "windows_host",
])
def test_doctor_is_unchanged_when_a_condition_fails(case):
    other = Path("/opt/tools/adb")
    env = {
        "override_valid": lambda: debian_env(environ={"PATH": "/usr/bin", "ADAPT_ADB": str(other)},
                                             existing_paths={DISTRO_ADB, MANAGED_ADB, MANAGED_DIR, other}),
        "override_missing": lambda: debian_env(environ={"PATH": "/usr/bin", "ADB_PATH": "/missing/adb"}),
        "no_marker": lambda: debian_env(debian_profile_texts={PROFILE: f"{managed_path_export_line(MANAGED_DIR)}\n"}),
        "managed_missing": lambda: debian_env(existing_paths={DISTRO_ADB}),
        "managed_too_old": lambda: debian_env(version_outputs={DISTRO_ADB: V34_DISTRO, MANAGED_ADB: V33}),
        "folder_on_path": lambda: debian_env(environ={"PATH": f"{MANAGED_DIR}:/usr/bin"},
                                             which_adb=MANAGED_ADB, path_executables=[MANAGED_ADB, DISTRO_ADB]),
        # The user deliberately put another adb first; PATH order wins.
        "folder_on_path_after_another_adb": lambda: debian_env(environ={"PATH": f"/usr/bin:{MANAGED_DIR}"},
                                                               path_executables=[DISTRO_ADB, MANAGED_ADB]),
        "no_other_adb": lambda: debian_env(environ={"PATH": "/usr/bin"}, which_adb=None, path_executables=[]),
        "windows_host": lambda: debian_env(host_os="windows"),
    }[case]()
    report = run_doctor_checks(env)
    tc = report["toolchain"]
    assert tc["command_adb_source"] != REGISTERED_MANAGED_SOURCE
    assert tc["registered_managed_copy"] is None
    assert _checks(report)["path_config"]["failure_id"] != "adb.managed_not_on_path"
    if case.startswith("folder_on_path") or case in ("no_marker", "managed_missing", "managed_too_old"):
        assert tc["command_adb_source"] == "path" and tc["command_adb_path"] == tc["adb_path"]
    if case == "folder_on_path_after_another_adb":
        assert tc["command_adb_path"] == str(DISTRO_ADB)
        assert _checks(report)["path_config"]["failure_id"] == "adb.path_shadowed"


def test_server_check_refuses_a_mismatch_with_the_selected_copy():
    # A server started by another version still refuses, now judged against the managed copy.
    env = debian_env(server_state=Server5037State(listening=True, pid=4242, owner="example",
                                                  owner_matches_current_user=True, server_version_raw=V36))
    report = run_doctor_checks(env)
    server = _checks(report)["adb_server_5037"]
    assert (server["status"], server["failure_id"]) == ("fail", "adb.server.version_mismatch")
    assert server["details"]["client_adb"] == str(MANAGED_ADB)
    assert report["exit_code"] == doctor_mod.EXIT_UNSAFE_SHARED_SERVER
    assert _only_version_and_listing(env)


def test_server_started_by_the_managed_copy_is_verified_not_called_unsafe():
    # The original problem: a server from the managed copy, and the distribution adb first on PATH.
    env = debian_env(server_state=Server5037State(listening=True, pid=4242, owner="example",
                                                  owner_matches_current_user=True, server_version_raw=V35))
    server = _checks(run_doctor_checks(env))["adb_server_5037"]
    assert server["status"] == "pass", server["summary"]
    assert server["details"]["version_mismatch"] is False
    assert _only_version_and_listing(env)
    # Without the registration the same host compares against the distribution adb and refuses.
    env = debian_env(debian_profile_texts={PROFILE: ""}, server_state=env.server_state)
    assert _checks(run_doctor_checks(env))["adb_server_5037"]["failure_id"] == "adb.server.version_mismatch"


def _only_version_and_listing(env: DoctorEnv) -> bool:
    """Doctor ran only `adb version` and the device listing: no start-server, kill-server or reconnect."""
    return all(cmd[1:] in (["version"], ["devices"], ["devices", "-l"]) for cmd in env.commands_invoked)
