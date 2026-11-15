"""Defects found by running ADAPT on a real Debian 13 host.

Offline only: injected DoctorEnv fakes and temporary folders; no adb, device or network.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import adapt_adb.doctor as doctor_mod
from adapt_adb.doctor import DebianUsbState, DoctorEnv, Server5037State, run_doctor_checks

HOST_OS = "windows" if os.name == "nt" else "debian"
ADB_NAME = "adb.exe" if os.name == "nt" else "adb"


def _checks(report):
    return {c["id"]: c for c in report["checks"]}


def _headless_env(tmp_path: Path, state: DebianUsbState) -> DoctorEnv:
    return DoctorEnv(
        host_os="debian",
        arch="x86_64",
        uid_is_root=False,
        home_dir=tmp_path / "home" / "dev",
        environ={"PATH": "/usr/bin"},
        which_adb=None,
        which_set=True,
        path_executables=[],
        existing_paths=set(),
        source_properties={},
        version_outputs={},
        server_state=Server5037State(listening=False),
        debian_usb_state=state,
        devices_l_output="List of devices attached\n\n",
    )


# ---------------------------------------------------------------------------
# One adb reached through two PATH folders is not a shadowing second copy
# ---------------------------------------------------------------------------

def test_a_linked_path_folder_does_not_count_the_same_adb_twice(tmp_path):
    real = tmp_path / "usr" / "bin"
    real.mkdir(parents=True)
    (real / ADB_NAME).write_bytes(b"")
    link = tmp_path / "bin"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this host does not allow creating a directory link")
    environ = {"PATH": os.pathsep.join([str(real), str(link)])}
    _which, path_bins, _fallbacks = doctor_mod._discover_candidates(HOST_OS, tmp_path, environ, None)
    assert path_bins == [real / ADB_NAME]


def test_two_different_adb_files_are_still_both_reported(tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    for folder in (first, second):
        folder.mkdir()
        (folder / ADB_NAME).write_bytes(b"")
    environ = {"PATH": os.pathsep.join([str(first), str(second)])}
    _which, path_bins, _fallbacks = doctor_mod._discover_candidates(HOST_OS, tmp_path, environ, None)
    assert path_bins == [first / ADB_NAME, second / ADB_NAME]


def test_injected_paths_keep_their_spelling(tmp_path):
    # With an injected file set there is no real file to resolve; the key is the path itself.
    env = DoctorEnv(existing_paths=set())
    assert doctor_mod._same_file_key(Path("/bin/adb"), "debian", env) == "/bin/adb".replace("/", os.sep)
    assert doctor_mod._same_file_key(Path("C:/PT/ADB.exe"), "windows", env) == str(Path("C:/PT/ADB.exe")).lower()


# ---------------------------------------------------------------------------
# Headless session: say what is missing, the group or the rule that grants it
# ---------------------------------------------------------------------------

def test_headless_member_of_plugdev_without_a_rule_is_told_the_rule_is_missing(tmp_path):
    # Debian 13: systemd's own rule gives uaccess, the user is in plugdev, no rule grants plugdev.
    state = DebianUsbState(
        has_udev_rules=True, has_uaccess_tag=True, has_plugdev_rule=False,
        rule_files=["/lib/udev/rules.d/70-uaccess.rules"], in_plugdev_group=True, seat="headless",
    )
    check = _checks(run_doctor_checks(_headless_env(tmp_path, state)))["debian_usb_permissions"]
    assert check["status"] == "fail"
    assert check["failure_id"] == "host.udev_group_missing"
    assert "this user is in `plugdev`, but no Android udev rule grants that group" in check["summary"]
    assert "requires membership" not in check["summary"]


def test_headless_member_of_adbusers_without_a_rule_names_that_group(tmp_path):
    state = DebianUsbState(
        has_plugdev_rule=False, in_plugdev_group=False, in_adbusers_group=True, seat="headless",
    )
    check = _checks(run_doctor_checks(_headless_env(tmp_path, state)))["debian_usb_permissions"]
    assert "this user is in `adbusers`" in check["summary"]


def test_headless_user_in_no_group_is_still_told_to_join_one(tmp_path):
    state = DebianUsbState(in_plugdev_group=False, in_adbusers_group=False, seat="headless")
    check = _checks(run_doctor_checks(_headless_env(tmp_path, state)))["debian_usb_permissions"]
    assert check["status"] == "fail"
    assert "requires membership in a group" in check["summary"]


def test_headless_member_with_a_matching_rule_passes(tmp_path):
    state = DebianUsbState(seat="headless")
    check = _checks(run_doctor_checks(_headless_env(tmp_path, state)))["debian_usb_permissions"]
    assert check["status"] == "pass"
