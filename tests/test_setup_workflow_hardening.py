"""Hardening tests for `adapt setup` outcomes, PATH handling and zip validation.

Offline only: in-memory `DoctorEnv` fakes and fake downloaders; no network, no adb.
"""

from __future__ import annotations

import builtins
import io
import json
import shlex
import stat
import sys
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

import adapt_adb.cli as cli_mod
from adapt_adb.cli import app
from adapt_adb.doctor import (
    DebianUsbState,
    DoctorEnv,
    EXIT_DEVICE_OR_PERMISSIONS,
    EXIT_MISSING_TOOLCHAIN,
    EXIT_OK,
    Server5037State,
    ensure_debian_user_path,
    run_setup_workflow,
    validate_zip_members_safe,
)

runner = CliRunner()

FAKE_VERSION_35 = (
    "Android Debug Bridge version 1.0.41\n"
    "Version 35.0.2-12147458\n"
    "Installed as /home/user/.local/share/android/platform-tools/adb\n"
)

# Required top-level keys from the JSON schema in docs/doctor.md section 4.
SCHEMA_REQUIRED_KEYS = (
    "schema_version",
    "command",
    "host",
    "exit_code",
    "status",
    "checks",
    "devices",
)


def _healthy_windows_env(tmp_path: Path) -> DoctorEnv:
    home = tmp_path / "Users" / "dev"
    local_app = home / "AppData" / "Local"
    pt_dir = local_app / "Android" / "platform-tools"
    adb_exe = pt_dir / "adb.exe"
    return DoctorEnv(
        host_os="windows",
        arch="AMD64",
        uid_is_root=False,
        home_dir=home,
        environ={"LOCALAPPDATA": str(local_app), "PATH": str(pt_dir)},
        which_adb=adb_exe,
        which_set=True,
        path_executables=[adb_exe],
        existing_paths={adb_exe, pt_dir},
        source_properties={pt_dir / "source.properties": "Pkg.Revision=35.0.2\n"},
        version_outputs={adb_exe: FAKE_VERSION_35},
        server_state=Server5037State(listening=False),
        windows_pnp_devices=[],
        devices_l_output="List of devices attached\n\n",
    )


def _missing_windows_env(tmp_path: Path, path_value: str = r"C:\Windows\System32") -> DoctorEnv:
    home = tmp_path / "Users" / "dev"
    local_app = home / "AppData" / "Local"
    return DoctorEnv(
        host_os="windows",
        arch="AMD64",
        uid_is_root=False,
        home_dir=home,
        environ={"LOCALAPPDATA": str(local_app), "PATH": path_value},
        which_adb=None,
        which_set=True,
        path_executables=[],
        existing_paths=set(),
        source_properties={},
        version_outputs={},
        server_state=Server5037State(listening=False),
        windows_pnp_devices=[],
        devices_l_output="List of devices attached\n\n",
    )


def _debian_env(tmp_path: Path, *, arch: str, healthy: bool) -> DoctorEnv:
    home = tmp_path / "home" / "dev"
    pt_dir = home / ".local" / "share" / "android" / "platform-tools"
    adb_bin = pt_dir / "adb"
    return DoctorEnv(
        host_os="debian",
        arch=arch,
        uid_is_root=False,
        home_dir=home,
        environ={"PATH": f"{pt_dir}:/usr/bin" if healthy else "/usr/bin"},
        which_adb=adb_bin if healthy else None,
        which_set=True,
        path_executables=[adb_bin] if healthy else [],
        existing_paths={adb_bin, pt_dir} if healthy else set(),
        source_properties=(
            {pt_dir / "source.properties": "Pkg.Revision=35.0.2\n"} if healthy else {}
        ),
        version_outputs={adb_bin: FAKE_VERSION_35} if healthy else {},
        server_state=Server5037State(listening=False),
        debian_usb_state=DebianUsbState(
            has_udev_rules=True,
            has_uaccess_tag=True,
            has_plugdev_rule=True,
            rule_files=["/etc/udev/rules.d/51-android.rules"],
            in_plugdev_group=True,
            seat="local",
        ),
        debian_profile_texts={},
        devices_l_output="List of devices attached\n\n",
    )


def _no_download(*args, **kwargs):
    pytest.fail("setup must not download in this scenario")


class _TtyStream(io.StringIO):
    def isatty(self) -> bool:  # noqa: D401 - test stub
        return True


def _force_no_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.stderr.isatty", lambda: False)


def _force_tty_answer(monkeypatch: pytest.MonkeyPatch, answer: str) -> None:
    monkeypatch.setattr(sys, "stdin", _TtyStream())
    monkeypatch.setattr(sys, "stderr", _TtyStream())
    monkeypatch.setattr(builtins, "input", lambda *a: answer)


# --- HP-DEF-02: aborted setup must not exit 0 ---------------------------------


def test_aborted_needs_yes_on_healthy_host_exits_1(tmp_path, monkeypatch):
    env = _healthy_windows_env(tmp_path)
    _force_no_tty(monkeypatch)
    report = run_setup_workflow(env, force=True, downloader=_no_download)
    assert report["setup"]["action"] == "aborted_needs_yes"
    assert report["setup"]["downloaded"] is False
    assert report["exit_code"] == EXIT_MISSING_TOOLCHAIN
    assert report["status"] == "ok"


def test_aborted_unconfirmed_on_healthy_host_exits_1(tmp_path, monkeypatch):
    env = _healthy_windows_env(tmp_path)
    _force_tty_answer(monkeypatch, "n")
    report = run_setup_workflow(env, force=True, downloader=_no_download)
    assert report["setup"]["action"] == "aborted_unconfirmed"
    assert report["exit_code"] == EXIT_MISSING_TOOLCHAIN
    assert report["status"] == "ok"


def test_aborted_keeps_existing_missing_toolchain_code(tmp_path, monkeypatch):
    env = _missing_windows_env(tmp_path)
    _force_no_tty(monkeypatch)
    report = run_setup_workflow(env, downloader=_no_download)
    assert report["setup"]["action"] == "aborted_needs_yes"
    assert report["exit_code"] == EXIT_MISSING_TOOLCHAIN
    assert report["status"] == "missing_toolchain"


def test_aborted_keeps_existing_device_permissions_code(tmp_path, monkeypatch):
    env = _healthy_windows_env(tmp_path)
    env.devices_l_output = "List of devices attached\nSERIAL0001\tunauthorized transport_id:1\n\n"
    _force_tty_answer(monkeypatch, "no")
    report = run_setup_workflow(env, force=True, downloader=_no_download)
    assert report["setup"]["action"] == "aborted_unconfirmed"
    assert report["exit_code"] == EXIT_DEVICE_OR_PERMISSIONS
    assert report["status"] == "device_unauthorized_or_no_permissions"


def test_noop_and_dry_run_outcomes_still_exit_0(tmp_path, monkeypatch):
    _force_no_tty(monkeypatch)
    noop = run_setup_workflow(_healthy_windows_env(tmp_path), downloader=_no_download)
    assert noop["setup"]["action"] == "noop_already_healthy"
    assert noop["exit_code"] == EXIT_OK
    dry = run_setup_workflow(
        _healthy_windows_env(tmp_path), force=True, dry_run=True, downloader=_no_download
    )
    assert dry["setup"]["action"] == "dry_run"
    assert dry["exit_code"] == EXIT_OK


# --- HP-DEF-03: failure output keeps the documented JSON schema -----------------


def _raising_downloader(url, target_dir, expected_sha256=None):
    raise ValueError("SHA-256 checksum mismatch")


@pytest.mark.parametrize("healthy", [True, False])
def test_setup_failure_returns_full_report(tmp_path, healthy):
    env = _healthy_windows_env(tmp_path) if healthy else _missing_windows_env(tmp_path)
    report = run_setup_workflow(
        env, force=True, yes=True, downloader=_raising_downloader
    )
    setup = report["setup"]
    assert setup["action"] == "failed"
    assert setup["error"] == "SHA-256 checksum mismatch"
    assert report["error"] == "SHA-256 checksum mismatch"
    assert setup["downloaded"] is False
    assert setup["path_updated"] is False
    assert setup["dry_run"] is False
    assert setup["url"].startswith("https://dl.google.com/")
    assert setup["target_dir"]
    assert report["exit_code"] == EXIT_MISSING_TOOLCHAIN
    assert report["status"] == ("ok" if healthy else "missing_toolchain")


def test_setup_json_failure_matches_schema(tmp_path, monkeypatch):
    env = _missing_windows_env(tmp_path)
    monkeypatch.setattr(cli_mod, "_ACTIVE_DOCTOR_ENV", env)
    monkeypatch.setattr(cli_mod, "download_and_extract_platform_tools", _raising_downloader)
    result = runner.invoke(app, ["setup", "--yes", "--json"])
    assert result.exit_code == 1
    report = json.loads(result.stdout)
    for key in SCHEMA_REQUIRED_KEYS:
        assert key in report, key
    assert report["command"] == "setup"
    assert report["setup"]["action"] == "failed"
    assert report["error"] == "SHA-256 checksum mismatch"
    assert report["exit_code"] == 1


def test_setup_json_failure_on_healthy_host_exits_1(tmp_path, monkeypatch):
    env = _healthy_windows_env(tmp_path)
    monkeypatch.setattr(cli_mod, "_ACTIVE_DOCTOR_ENV", env)
    monkeypatch.setattr(cli_mod, "download_and_extract_platform_tools", _raising_downloader)
    result = runner.invoke(app, ["setup", "--yes", "--force", "--json"])
    assert result.exit_code == 1
    report = json.loads(result.stdout)
    assert report["setup"]["action"] == "failed"
    assert report["status"] == "ok"
    assert report["exit_code"] == 1


def test_setup_human_failure_prints_error_only(tmp_path, monkeypatch):
    env = _missing_windows_env(tmp_path)
    monkeypatch.setattr(cli_mod, "_ACTIVE_DOCTOR_ENV", env)
    monkeypatch.setattr(cli_mod, "download_and_extract_platform_tools", _raising_downloader)

    def _no_menu(*a, **kw):
        pytest.fail("failed setup must not enter the next-actions menu")

    monkeypatch.setattr(cli_mod, "_run_next_menu", _no_menu)
    result = runner.invoke(app, ["setup", "--yes"])
    assert result.exit_code == 1
    assert "Error: SHA-256 checksum mismatch" in result.stderr
    assert "Downloaded official" not in result.stdout
    assert "Next:" not in result.stdout


# --- HP-IMP-02: architecture gate ---------------------------------------------


@pytest.mark.parametrize("dry_run", [False, True])
def test_aarch64_debian_setup_is_gated(tmp_path, monkeypatch, dry_run):
    _force_no_tty(monkeypatch)
    env = _debian_env(tmp_path, arch="aarch64", healthy=False)
    report = run_setup_workflow(env, yes=True, dry_run=dry_run, downloader=_no_download)
    setup = report["setup"]
    assert setup["action"] == "unsupported_architecture"
    assert setup["dry_run"] is dry_run
    assert setup["downloaded"] is False
    assert setup["path_updated"] is False
    assert setup["url"].startswith("https://dl.google.com/")
    assert setup["target_dir"]
    assert "aarch64" in setup["error"]
    assert "x86_64" in setup["error"]
    assert report["error"] == setup["error"]
    assert report["exit_code"] == EXIT_MISSING_TOOLCHAIN
    assert env.debian_profile_texts == {}


def test_aarch64_forced_on_healthy_host_exits_1(tmp_path, monkeypatch):
    env = _debian_env(tmp_path, arch="aarch64", healthy=True)
    report = run_setup_workflow(env, force=True, yes=True, downloader=_no_download)
    assert report["setup"]["action"] == "unsupported_architecture"
    assert report["status"] == "ok"
    assert report["exit_code"] == EXIT_MISSING_TOOLCHAIN


def test_aarch64_healthy_without_force_is_still_noop(tmp_path):
    env = _debian_env(tmp_path, arch="aarch64", healthy=True)
    report = run_setup_workflow(env, yes=True, downloader=_no_download)
    assert report["setup"]["action"] == "noop_already_healthy"
    assert report["exit_code"] == EXIT_OK


@pytest.mark.parametrize("arch", ["x86_64", "amd64", "AMD64"])
def test_x86_64_debian_spellings_are_not_gated(tmp_path, arch):
    env = _debian_env(tmp_path, arch=arch, healthy=False)
    report = run_setup_workflow(env, dry_run=True, downloader=_no_download)
    assert report["setup"]["action"] == "dry_run"


def test_windows_arm64_is_not_gated(tmp_path):
    env = _missing_windows_env(tmp_path)
    env.arch = "ARM64"
    report = run_setup_workflow(env, dry_run=True, downloader=_no_download)
    assert report["setup"]["action"] == "dry_run"


def test_unsupported_architecture_cli_output(tmp_path, monkeypatch):
    env = _debian_env(tmp_path, arch="aarch64", healthy=False)
    monkeypatch.setattr(cli_mod, "_ACTIVE_DOCTOR_ENV", env)
    monkeypatch.setattr(cli_mod, "download_and_extract_platform_tools", _no_download)
    human = runner.invoke(app, ["setup", "--yes"])
    assert human.exit_code == 1
    assert "Error:" in human.stderr
    assert "aarch64" in human.stderr
    assert "Downloaded official" not in human.stdout

    env_json = _debian_env(tmp_path, arch="aarch64", healthy=False)
    monkeypatch.setattr(cli_mod, "_ACTIVE_DOCTOR_ENV", env_json)
    result = runner.invoke(app, ["setup", "--yes", "--json"])
    assert result.exit_code == 1
    report = json.loads(result.stdout)
    for key in SCHEMA_REQUIRED_KEYS:
        assert key in report, key
    assert report["setup"]["action"] == "unsupported_architecture"


# --- HP-DEF-05: exact managed export line check -------------------------------


def _profile_target(home: Path) -> Path:
    return home / ".local" / "share" / "android" / "platform-tools"


def _export_line(path: Path | str) -> str:
    return "export PATH=" + shlex.quote(str(path)) + ':"$PATH"'


def test_profile_commented_path_still_appends_block():
    home = Path("/home/u")
    target = _profile_target(home)
    profile = home / ".profile"
    env = DoctorEnv(debian_profile_texts={profile: f"# old {target}\n"})
    assert ensure_debian_user_path(target, home, env)
    text = env.debian_profile_texts[profile]
    assert _export_line(target) in text.splitlines()
    assert text.startswith(f"# old {target}\n")


def test_profile_suffixed_path_still_appends_block():
    home = Path("/home/u")
    target = _profile_target(home)
    profile = home / ".profile"
    existing = _export_line(f"{target}-old") + "\n"
    env = DoctorEnv(debian_profile_texts={profile: existing})
    assert ensure_debian_user_path(target, home, env)
    lines = env.debian_profile_texts[profile].splitlines()
    assert _export_line(target) in lines
    assert _export_line(f"{target}-old") in lines


def test_profile_exact_line_is_idempotent():
    home = Path("/home/u")
    target = _profile_target(home)
    profile = home / ".profile"
    env = DoctorEnv(debian_profile_texts={})
    assert ensure_debian_user_path(target, home, env)
    first = env.debian_profile_texts[profile]
    assert ensure_debian_user_path(target, home, env)
    assert env.debian_profile_texts[profile] == first
    assert first.splitlines().count(_export_line(target)) == 1


def test_profile_indented_exact_line_counts_as_present():
    home = Path("/home/u")
    target = _profile_target(home)
    profile = home / ".profile"
    existing = "  " + _export_line(target) + "  \n"
    env = DoctorEnv(debian_profile_texts={profile: existing})
    assert ensure_debian_user_path(target, home, env)
    assert env.debian_profile_texts[profile] == existing


def test_real_profile_file_uses_exact_check(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    target = _profile_target(home)
    profile = home / ".profile"
    profile.write_text(
        f"# {_export_line(target)}\n{_export_line(f'{target}-old')}\n", encoding="utf-8"
    )
    assert ensure_debian_user_path(target, home)
    first = profile.read_text(encoding="utf-8")
    assert "# ADAPT managed platform-tools PATH" in first
    assert ensure_debian_user_path(target, home)
    assert profile.read_text(encoding="utf-8") == first


# --- HP-DEF-06: case-insensitive PATH de-duplication on Windows ----------------


def _ok_downloader(url, target_dir, expected_sha256=None):
    return "a" * 64


def test_windows_path_dedup_ignores_case_and_trailing_backslash(tmp_path):
    home = tmp_path / "Users" / "dev"
    target = home / "AppData" / "Local" / "Android" / "platform-tools"
    variant = str(target).lower() + "\\"
    other = r"C:\Windows\System32"
    env = _missing_windows_env(tmp_path, path_value=f"{variant};{other}")
    report = run_setup_workflow(env, yes=True, downloader=_ok_downloader)
    assert report["setup"]["action"] == "installed"
    entries = env.environ["PATH"].split(";")
    assert entries[0] == str(target)
    assert other in entries
    assert len(entries) == 2


def test_non_windows_path_dedup_stays_exact(tmp_path):
    env = _debian_env(tmp_path, arch="x86_64", healthy=False)
    target = _profile_target(tmp_path / "home" / "dev")
    env.environ["PATH"] = f"{target}/:/usr/bin"
    report = run_setup_workflow(env, yes=True, downloader=_ok_downloader)
    assert report["setup"]["action"] == "installed"
    # A differently spelled entry is not treated as the same on POSIX hosts.
    assert env.environ["PATH"] == f"{target}:{target}/:/usr/bin"


# --- HP-IMP-01: zip validator delegates to the production validator ------------


def _zip_with(info: zipfile.ZipInfo, data: bytes = b"x") -> zipfile.ZipFile:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(info, data)
    buf.seek(0)
    return zipfile.ZipFile(buf, "r")


class _StubZip:
    """Minimal ZipFile stand-in whose member names are not normalised by zipfile."""

    def __init__(self, names: list[str]) -> None:
        self._infos = []
        for name in names:
            info = zipfile.ZipInfo("placeholder")
            info.filename = name
            self._infos.append(info)

    def infolist(self) -> list[zipfile.ZipInfo]:
        return self._infos


def test_zip_symlink_member_rejected(tmp_path):
    info = zipfile.ZipInfo("platform-tools/adb")
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with _zip_with(info, b"/etc/passwd") as zf:
        with pytest.raises(ValueError, match="symlink"):
            validate_zip_members_safe(zf, tmp_path)


def test_zip_drive_relative_member_rejected(tmp_path):
    with _zip_with(zipfile.ZipInfo("C:evil.txt")) as zf:
        with pytest.raises(ValueError, match="Unsafe"):
            validate_zip_members_safe(zf, tmp_path)


def test_zip_backslash_traversal_rejected(tmp_path):
    with _zip_with(zipfile.ZipInfo("..\\evil.txt")) as zf:
        with pytest.raises(ValueError, match="Unsafe"):
            validate_zip_members_safe(zf, tmp_path)
    # Also check the raw backslash spelling, which zipfile rewrites only on Windows.
    with pytest.raises(ValueError, match="Unsafe"):
        validate_zip_members_safe(_StubZip(["..\\evil.txt"]), tmp_path)


def test_zip_nul_byte_member_rejected(tmp_path):
    with pytest.raises(ValueError, match="Unsafe"):
        validate_zip_members_safe(_StubZip(["platform-tools/adb\x00.txt"]), tmp_path)


def test_zip_normal_members_accepted(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("platform-tools/", b"")
        zf.writestr("platform-tools/adb", b"bin")
        zf.writestr("platform-tools/lib64/libc++.so", b"lib")
    buf.seek(0)
    with zipfile.ZipFile(buf, "r") as zf:
        validate_zip_members_safe(zf, tmp_path)
