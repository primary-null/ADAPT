"""Unit tests for adapt_adb.adb helper functions."""

from pathlib import Path
import sys
from unittest.mock import MagicMock

from adapt_adb.adb import (
    add_to_windows_user_path,
    get_adb_version,
    get_connected_devices,
    get_default_platform_tools_dir,
    get_search_locations,
    has_serial_flag,
)


def test_has_serial_flag():
    """Verify various forms of serial arguments are detected."""
    assert has_serial_flag(["-s", "dev123"]) is True
    assert has_serial_flag(["-sdev123"]) is True
    assert has_serial_flag(["--serial", "dev123"]) is True
    assert has_serial_flag(["--serial=dev123"]) is True
    # Command operands are opaque; this is a shell operand, not a global selector.
    assert has_serial_flag(["shell", "-s"]) is False
    assert has_serial_flag(["shell", "ls"]) is False
    assert has_serial_flag([]) is False


def test_get_connected_devices_parsing(monkeypatch):
    """Verify stdout from `adb devices` is correctly parsed into device dicts."""
    sample_stdout = (
        "List of devices attached\n"
        "mock-dev-1\tdevice\n"
        "mock-dev-2\tunauthorized\n"
        "mock-dev-3\toffline\n"
        "\n"
    )
    mock_run = MagicMock(return_value=MagicMock(stdout=sample_stdout, stderr="", returncode=0))
    monkeypatch.setattr("subprocess.run", mock_run)

    devices = get_connected_devices(Path("/mock/adb"))
    assert len(devices) == 3
    assert devices[0] == {"serial": "mock-dev-1", "state": "device"}
    assert devices[1] == {"serial": "mock-dev-2", "state": "unauthorized"}
    assert devices[2] == {"serial": "mock-dev-3", "state": "offline"}


def test_get_adb_version_parsing(monkeypatch):
    """Verify stdout from `adb version` extracts first non-empty line."""
    sample_stdout = (
        "Android Debug Bridge version 1.0.41\n"
        "Version 35.0.0-11411520\n"
        "Installed as /path/to/adb\n"
    )
    mock_run = MagicMock(return_value=MagicMock(stdout=sample_stdout, stderr="", returncode=0))
    monkeypatch.setattr("subprocess.run", mock_run)

    ver = get_adb_version(Path("/mock/adb"))
    assert ver == "Android Debug Bridge version 1.0.41"


def test_get_default_platform_tools_dir():
    """Verify default platform-tools directory returns a valid Path."""
    path = get_default_platform_tools_dir()
    assert isinstance(path, Path)
    assert "platform-tools" in path.parts


def test_get_search_locations():
    """Verify candidate search locations structure."""
    locations = get_search_locations()
    descriptions = [desc for desc, _ in locations]
    assert "PATH" in descriptions
    assert "ANDROID_HOME/platform-tools" in descriptions
    assert "~/.local/share/android/platform-tools" in descriptions
    if sys.platform == "win32":
        assert len(locations) == 5
        assert "LOCALAPPDATA Android platform-tools" in descriptions
        assert "LOCALAPPDATA Android Sdk" in descriptions
    else:
        assert len(locations) == 3
        assert not any("LOCALAPPDATA" in desc for desc in descriptions)


class _FakeWinReg:
    HKEY_CURRENT_USER = object()
    KEY_READ = 0x20019
    KEY_SET_VALUE = 0x0002
    REG_EXPAND_SZ = 2
    REG_SZ = 1

    def __init__(self, initial_path: str | None, reg_type: int = REG_EXPAND_SZ) -> None:
        self.value = initial_path
        self.reg_type = reg_type
        self.set_calls: list[tuple[str, int, str]] = []
        self.closed = False

    def OpenKey(self, root, subkey, reserved, access):
        assert root is self.HKEY_CURRENT_USER
        assert subkey == r"Environment"
        return self

    def QueryValueEx(self, key, name):
        assert name == "Path"
        if self.value is None:
            raise FileNotFoundError(name)
        return self.value, self.reg_type

    def SetValueEx(self, key, name, reserved, reg_type, value):
        assert name == "Path"
        self.value = value
        self.reg_type = reg_type
        self.set_calls.append((name, reg_type, value))

    def CloseKey(self, key):
        self.closed = True


def test_add_to_windows_user_path_appends_when_no_shadowing_adb(tmp_path, monkeypatch):
    """Appends target_dir when no other HKCU Path entry contains adb.exe."""
    other_dir = tmp_path / "tools"
    other_dir.mkdir()
    target_dir = tmp_path / "Android" / "platform-tools"

    fake_reg = _FakeWinReg(str(other_dir))
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", fake_reg)

    assert add_to_windows_user_path(target_dir) is True
    assert fake_reg.closed is True
    assert fake_reg.value == f"{other_dir};{target_dir}"
    assert len(fake_reg.set_calls) == 1


def test_add_to_windows_user_path_prepends_when_shadowed_by_adb_exe(tmp_path, monkeypatch):
    """Prepends target_dir when an existing HKCU Path entry contains adb.exe."""
    other_dir = tmp_path / "tools"
    other_dir.mkdir()
    old_adb_dir = tmp_path / "old-sdk" / "platform-tools"
    old_adb_dir.mkdir(parents=True)
    (old_adb_dir / "adb.exe").write_bytes(b"old-adb")
    target_dir = tmp_path / "Android" / "platform-tools"

    fake_reg = _FakeWinReg(f"{other_dir};{old_adb_dir}")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", fake_reg)

    assert add_to_windows_user_path(target_dir) is True
    assert fake_reg.closed is True
    assert fake_reg.value == f"{target_dir};{other_dir};{old_adb_dir}"
    assert len(fake_reg.set_calls) == 1


def test_add_to_windows_user_path_prepends_when_shadowed_via_env_var(tmp_path, monkeypatch):
    """Expands %ENV_VAR% HKCU entries when checking for shadowing adb.exe."""
    old_adb_dir = tmp_path / "env-sdk"
    old_adb_dir.mkdir()
    (old_adb_dir / "adb.exe").write_bytes(b"old-adb")
    target_dir = tmp_path / "Android" / "platform-tools"

    monkeypatch.setenv("ADAPT_TEST_OLD_SDK", str(old_adb_dir))
    fake_reg = _FakeWinReg(r"%ADAPT_TEST_OLD_SDK%")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", fake_reg)

    assert add_to_windows_user_path(target_dir) is True
    assert fake_reg.value == f"{target_dir};%ADAPT_TEST_OLD_SDK%"


def test_add_to_windows_user_path_idempotent_when_already_present(tmp_path, monkeypatch):
    """Leaves HKCU Path untouched when target_dir is already present and unshadowed."""
    other_dir = tmp_path / "tools"
    other_dir.mkdir()
    target_dir = tmp_path / "Android" / "platform-tools"
    initial = f"{other_dir};{str(target_dir).upper()}\\"

    fake_reg = _FakeWinReg(initial)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", fake_reg)

    assert add_to_windows_user_path(target_dir) is True
    assert fake_reg.closed is True
    assert fake_reg.value == initial
    assert fake_reg.set_calls == []


def test_add_to_windows_user_path_reorders_if_present_after_shadowing_adb(tmp_path, monkeypatch):
    """Moves target_dir ahead if already present behind a shadowing adb.exe, then stays idempotent."""
    old_adb_dir = tmp_path / "old-adb"
    old_adb_dir.mkdir()
    (old_adb_dir / "adb.exe").write_bytes(b"old-adb")
    target_dir = tmp_path / "Android" / "platform-tools"

    fake_reg = _FakeWinReg(f"{old_adb_dir};{target_dir}")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", fake_reg)

    assert add_to_windows_user_path(target_dir) is True
    assert fake_reg.value == f"{target_dir};{old_adb_dir}"
    assert len(fake_reg.set_calls) == 1

    assert add_to_windows_user_path(target_dir) is True
    assert fake_reg.value == f"{target_dir};{old_adb_dir}"
    assert len(fake_reg.set_calls) == 1


def test_add_to_windows_user_path_creates_value_when_missing(tmp_path, monkeypatch):
    """Creates HKCU Path with REG_EXPAND_SZ when Path value does not yet exist."""
    target_dir = tmp_path / "Android" / "platform-tools"
    fake_reg = _FakeWinReg(None)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", fake_reg)

    assert add_to_windows_user_path(target_dir) is True
    assert fake_reg.value == str(target_dir)
    assert fake_reg.set_calls == [("Path", _FakeWinReg.REG_EXPAND_SZ, str(target_dir))]


def test_detect_order(tmp_path, monkeypatch):
    """resolve_adb() honors ADAPT_ADB override first, then prefers candidate >= 34.0.0."""
    from adapt_adb.adb import resolve_adb

    override_bin = tmp_path / "override_adb"
    override_bin.write_bytes(b"fake")
    old_path_bin = tmp_path / "old_adb"
    old_path_bin.write_bytes(b"fake")
    cache_bin = tmp_path / "cache_adb"
    cache_bin.write_bytes(b"fake")

    monkeypatch.setattr("os.access", lambda p, m: True)
    monkeypatch.setattr(
        "adapt_adb.adb.get_search_locations",
        lambda: [("PATH", old_path_bin), ("user_cache", cache_bin)],
    )

    def _fake_run(argv, **kwargs):
        exe = Path(argv[0])
        if exe == old_path_bin.resolve():
            return MagicMock(stdout="Android Debug Bridge version 1.0.41\nVersion 29.0.6-debian\n", stderr="", returncode=0)
        return MagicMock(stdout="Android Debug Bridge version 1.0.41\nVersion 35.0.2-123456\n", stderr="", returncode=0)

    monkeypatch.setattr("subprocess.run", _fake_run)

    # 1. With ADAPT_ADB set, override wins
    monkeypatch.setenv("ADAPT_ADB", str(override_bin))
    assert resolve_adb() == override_bin.resolve()

    # 2. Without ADAPT_ADB, prefers cache_bin (35.0.2) over old_path_bin (29.0.6)
    monkeypatch.delenv("ADAPT_ADB", raising=False)
    monkeypatch.delenv("ADB_PATH", raising=False)
    assert resolve_adb() == cache_bin.resolve()


def test_is_host_only_adb_command():
    """Verify host-only adb commands are recognized while device commands return False."""
    from adapt_adb.adb import is_host_only_adb_command

    assert is_host_only_adb_command(["version"]) is True
    assert is_host_only_adb_command(["shell", "ls"]) is False
    assert is_host_only_adb_command(["-P", "5037", "version"]) is True
    assert is_host_only_adb_command(["devices"]) is True
    assert is_host_only_adb_command(["start-server"]) is True
    assert is_host_only_adb_command(["help"]) is True
    assert is_host_only_adb_command(["shell", "getprop"]) is False
    assert is_host_only_adb_command(["logcat"]) is False
    assert is_host_only_adb_command([]) is False

