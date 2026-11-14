"""Unit tests for tool_doctor and tool_setup_status hardening (DOC-02 and HP-DEF-04)."""

import os
from pathlib import Path

import pytest

from adapt_adb.doctor import DoctorEnv, Server5037State
from adapt_adb.mcp.tools import tool_doctor, tool_setup_status


@pytest.fixture(autouse=True)
def _stub_server_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit tests exercise tool commands offline; prevent live socket inspection."""
    monkeypatch.setattr("adapt_adb.doctor.preflight_adb_server", lambda *a, **k: None)


SAMPLE_DEVICES_ONE = (
    "List of devices attached\n"
    "SERIAL_A               device usb:1-1 product:panther model:Pixel_7 device:panther transport_id:1\n"
)


def _make_base_doctor_env(tmp_path: Path, server_state: Server5037State) -> DoctorEnv:
    home = tmp_path / "home"
    pt_dir = home / ".local" / "share" / "android" / "platform-tools"
    adb_bin = pt_dir / "adb"
    return DoctorEnv(
        host_os="debian",
        arch="x86_64",
        uid_is_root=False,
        home_dir=home,
        environ={"PATH": str(pt_dir)},
        which_adb=adb_bin,
        which_set=True,
        path_executables=[adb_bin],
        existing_paths={adb_bin, pt_dir},
        source_properties={pt_dir / "source.properties": "Pkg.Revision=35.0.2\n"},
        version_outputs={
            adb_bin: (
                "Android Debug Bridge version 1.0.41\n"
                "Version 35.0.2-12147458\n"
                f"Installed as {adb_bin}\n"
            )
        },
        server_state=server_state,
        devices_l_output=SAMPLE_DEVICES_ONE,
    )


# ==============================================================================
# DOC-02: tool_doctor daemon classification tests
# ==============================================================================


def test_doctor_root_owned_tmp_5037(tmp_path, monkeypatch):
    """Root-owned /tmp/5037 is classified as root_owned with daemon_reason."""
    server_state = Server5037State(
        listening=True,
        owner_matches_current_user=True,
        tmp_5037_exists=True,
        tmp_5037_owner_is_root=True,
    )
    env = _make_base_doctor_env(tmp_path, server_state)
    monkeypatch.setattr("adapt_adb.cli._ACTIVE_DOCTOR_ENV", env)
    monkeypatch.setattr(
        "adapt_adb.mcp.tools.list_connected_devices",
        lambda *a, **kw: pytest.fail("list_connected_devices must not be called when server is unsafe"),
    )

    doc = tool_doctor()
    assert doc["daemon"] == "root_owned"
    assert doc["devices"] == []
    assert doc["daemon_reason"] is not None
    assert len(doc["daemon_reason"]) > 0


def test_doctor_foreign_owner(tmp_path, monkeypatch):
    """Listening server owned by another non-root user is classified as foreign_owner."""
    server_state = Server5037State(
        listening=True,
        owner="otheruser",
        owner_uid=1001,
        owner_matches_current_user=False,
    )
    env = _make_base_doctor_env(tmp_path, server_state)
    monkeypatch.setattr("adapt_adb.cli._ACTIVE_DOCTOR_ENV", env)
    monkeypatch.setattr(
        "adapt_adb.mcp.tools.list_connected_devices",
        lambda *a, **kw: pytest.fail("list_connected_devices must not be called when server is unsafe"),
    )

    doc = tool_doctor()
    assert doc["daemon"] == "foreign_owner"
    assert doc["devices"] == []
    assert doc["daemon_reason"] is not None
    assert len(doc["daemon_reason"]) > 0


def test_doctor_probe_error_unverified(tmp_path, monkeypatch):
    """Probe error on listener inspection is classified as unverified."""
    server_state = Server5037State(
        probe_error="Cannot inspect local TCP listeners.",
    )
    env = _make_base_doctor_env(tmp_path, server_state)
    monkeypatch.setattr("adapt_adb.cli._ACTIVE_DOCTOR_ENV", env)
    monkeypatch.setattr(
        "adapt_adb.mcp.tools.list_connected_devices",
        lambda *a, **kw: pytest.fail("list_connected_devices must not be called when server is unsafe"),
    )

    doc = tool_doctor()
    assert doc["daemon"] == "unverified"
    assert doc["devices"] == []
    assert doc["daemon_reason"] is not None
    assert len(doc["daemon_reason"]) > 0


def test_doctor_listening_current_user_version_unknown_unverified(tmp_path, monkeypatch):
    """Listening server owned by current user but with unknown version is unverified."""
    server_state = Server5037State(
        listening=True,
        owner="currentuser",
        owner_uid=1000,
        owner_matches_current_user=True,
        server_version_raw=None,
    )
    env = _make_base_doctor_env(tmp_path, server_state)
    monkeypatch.setattr("adapt_adb.cli._ACTIVE_DOCTOR_ENV", env)
    monkeypatch.setattr(
        "adapt_adb.mcp.tools.list_connected_devices",
        lambda *a, **kw: pytest.fail("list_connected_devices must not be called when server is unsafe"),
    )

    doc = tool_doctor()
    assert doc["daemon"] == "unverified"
    assert doc["devices"] == []
    assert doc["daemon_reason"] is not None
    assert len(doc["daemon_reason"]) > 0


def test_doctor_version_mismatch(tmp_path, monkeypatch):
    """Server version mismatch is classified as version_mismatch."""
    server_state = Server5037State(
        listening=True,
        owner="currentuser",
        owner_uid=1000,
        owner_matches_current_user=True,
        server_version_raw=(
            "Android Debug Bridge version 1.0.41\n"
            "Version 33.0.3-8952118\n"
        ),
    )
    env = _make_base_doctor_env(tmp_path, server_state)
    monkeypatch.setattr("adapt_adb.cli._ACTIVE_DOCTOR_ENV", env)
    monkeypatch.setattr(
        "adapt_adb.mcp.tools.list_connected_devices",
        lambda *a, **kw: pytest.fail("list_connected_devices must not be called when server is unsafe"),
    )

    doc = tool_doctor()
    assert doc["daemon"] == "version_mismatch"
    assert doc["devices"] == []
    assert doc["daemon_reason"] is not None
    assert len(doc["daemon_reason"]) > 0


def test_doctor_healthy_daemon(tmp_path, monkeypatch):
    """Healthy daemon returns daemon == 'ok' and daemon_reason is None."""
    server_state = Server5037State(
        listening=False,
    )
    env = _make_base_doctor_env(tmp_path, server_state)
    monkeypatch.setattr("adapt_adb.cli._ACTIVE_DOCTOR_ENV", env)

    def _fake_run_adb(adb_path, args, **kwargs):
        import subprocess

        if "devices" in args:
            if "-l" in args:
                return subprocess.CompletedProcess(
                    args=[str(adb_path), *args],
                    returncode=0,
                    stdout=SAMPLE_DEVICES_ONE,
                    stderr="",
                )
            return subprocess.CompletedProcess(
                args=[str(adb_path), *args],
                returncode=0,
                stdout="List of devices attached\nSERIAL_A\tdevice\n",
                stderr="",
            )
        return subprocess.CompletedProcess(args=[str(adb_path), *args], returncode=0, stdout="", stderr="")

    monkeypatch.setattr("adapt_adb.mcp.tools.run_adb", _fake_run_adb)

    doc = tool_doctor()
    assert doc["daemon"] == "ok"
    assert doc["daemon_reason"] is None
    assert doc["devices"] == [{"device": "SERIAL_A", "model": "Pixel 7", "state": "device"}]


# ==============================================================================
# HP-DEF-04: tool_setup_status hardening tests
# ==============================================================================


def test_setup_status_distro_path(monkeypatch):
    """A distro-packaged adb at or above the minimum is installed and flagged as a distribution package."""
    distro_adb = Path("/usr/lib/android-sdk/platform-tools/adb")
    monkeypatch.setattr("adapt_adb.mcp.tools.resolve_adb", lambda: distro_adb)
    monkeypatch.setattr("adapt_adb.mcp.tools._extract_version_number", lambda p: "35.0.2")
    monkeypatch.setenv("PATH", str(distro_adb.parent))

    status = tool_setup_status()
    assert status["installed"] is True
    assert status["distribution_package"] is True
    assert status["would_download"] is False


def test_setup_status_unparseable_version(monkeypatch):
    """Unparseable adb version is not installed, would_download is True."""
    custom_adb = Path("/opt/android/adb")
    monkeypatch.setattr("adapt_adb.mcp.tools.resolve_adb", lambda: custom_adb)
    monkeypatch.setattr("adapt_adb.mcp.tools._extract_version_number", lambda p: "unparseable-xyz")
    monkeypatch.setenv("PATH", str(custom_adb.parent))

    status = tool_setup_status()
    assert status["installed"] is False
    assert status["would_download"] is True


def test_setup_status_outdated_version_33(monkeypatch):
    """Outdated adb version 33.0.3 (< 34.0.0) is not installed, would_download is True."""
    custom_adb = Path("/opt/android/adb")
    monkeypatch.setattr("adapt_adb.mcp.tools.resolve_adb", lambda: custom_adb)
    monkeypatch.setattr("adapt_adb.mcp.tools._extract_version_number", lambda p: "33.0.3")
    monkeypatch.setenv("PATH", str(custom_adb.parent))

    status = tool_setup_status()
    assert status["installed"] is False
    assert status["would_download"] is True


def test_setup_status_valid_version_on_path(monkeypatch):
    """Valid adb version 35.0.2 on PATH is installed, would_download is False."""
    custom_adb = Path("/opt/android/adb")
    monkeypatch.setattr("adapt_adb.mcp.tools.resolve_adb", lambda: custom_adb)
    monkeypatch.setattr("adapt_adb.mcp.tools._extract_version_number", lambda p: "35.0.2")
    monkeypatch.setenv("PATH", f"/other/dir{os.pathsep}{custom_adb.parent}")

    status = tool_setup_status()
    assert status["installed"] is True
    assert status["would_download"] is False


def test_setup_status_valid_version_not_on_path(monkeypatch):
    """Valid adb version 35.0.2 NOT on PATH is installed, but would_download and would_path are True."""
    custom_adb = Path("/opt/android/adb")
    monkeypatch.setattr("adapt_adb.mcp.tools.resolve_adb", lambda: custom_adb)
    monkeypatch.setattr("adapt_adb.mcp.tools._extract_version_number", lambda p: "35.0.2")
    monkeypatch.setenv("PATH", "/other/dir")

    status = tool_setup_status()
    assert status["installed"] is True
    assert status["would_download"] is True
    assert status["would_path"] is True
