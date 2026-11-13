"""Unit tests for hardened platform-tools download and extraction in adapt_adb.adb."""

import hashlib
import io
import stat
import subprocess
import urllib.error
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from adapt_adb.adb import (
    PLATFORM_TOOLS_REPOSITORY_XML_URL,
    PLATFORM_TOOLS_URL_LINUX,
    PLATFORM_TOOLS_URL_WINDOWS,
    download_and_extract_platform_tools,
    get_default_platform_tools_dir,
)


class _FakeResponse:
    """Minimal context-manager HTTP response wrapping in-memory bytes."""

    def __init__(self, data: bytes) -> None:
        self._buf = io.BytesIO(data)

    def read(self, size: int = -1) -> bytes:
        return self._buf.read(size)

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._buf.close()
        return False


def _build_zip_bytes(entries: dict[str, bytes], *, symlink_names: set[str] | None = None) -> bytes:
    """Create a tiny in-memory zip archive from {archive_path: content}."""
    buf = io.BytesIO()
    symlink_names = symlink_names or set()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            info = zipfile.ZipInfo(name)
            info.filename = name
            if name in symlink_names:
                info.create_system = 3  # Unix
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
            elif name.endswith("/"):
                info.external_attr = 0o755 << 16
            else:
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o755) << 16
            zf.writestr(info, content)
    return buf.getvalue()


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fake_adb_version_run(
    version_line: str = "Version 35.0.0-11411520",
    *,
    returncode: int = 0,
    recorded_cmds: list[list[str]] | None = None,
):
    """Build a fake subprocess.run callable for `<staging>/adb version` checks."""

    def _run(cmd: list[str], **kwargs):
        if recorded_cmds is not None:
            recorded_cmds.append(list(cmd))
        stdout = (
            f"Android Debug Bridge version 1.0.41\n{version_line}\nInstalled as {cmd[0]}\n"
            if version_line
            else ""
        )
        return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)

    return _run


def test_download_verifies_explicit_sha256_and_cleans_staging(tmp_path: Path) -> None:
    """Explicit SHA-256 is verified, zip is unpacked cleanly, and staging files are removed."""
    zip_bytes = _build_zip_bytes(
        {
            "platform-tools/adb": b"fake-adb-binary",
            "platform-tools/adb.exe": b"fake-adb-exe",
            "platform-tools/source.properties": b"Pkg.Revision=35.0.0\n",
        }
    )
    digest = _sha256_hex(zip_bytes)
    target_dir = tmp_path / "install" / "platform-tools"

    def fake_urlopen(url: str, timeout: int = 30) -> _FakeResponse:
        assert url == PLATFORM_TOOLS_URL_LINUX
        return _FakeResponse(zip_bytes)

    with patch("adapt_adb.adb._open_google_url", side_effect=fake_urlopen) as mock_open:
        with patch("subprocess.run", side_effect=_fake_adb_version_run()):
            returned_digest = download_and_extract_platform_tools(
                PLATFORM_TOOLS_URL_LINUX,
                target_dir=target_dir,
                expected_sha256=digest,
            )

    assert returned_digest == digest
    mock_open.assert_called_once()
    assert (target_dir / "adb").read_bytes() == b"fake-adb-binary"
    assert (target_dir / "adb.exe").read_bytes() == b"fake-adb-exe"
    assert (target_dir / "source.properties").read_bytes() == b"Pkg.Revision=35.0.0\n"

    # No leftover staging zip or staging directory in parent
    siblings = [p.name for p in target_dir.parent.iterdir()]
    assert siblings == ["platform-tools"]


def test_download_fetches_sha256_from_google_repo_xml(tmp_path: Path) -> None:
    """When expected_sha256 is omitted, SHA-256 is fetched from Google repository XML."""
    zip_bytes = _build_zip_bytes(
        {
            "platform-tools/adb.exe": b"windows-adb",
            "platform-tools/source.properties": b"Pkg.Revision=36.0.0\n",
        }
    )
    digest = _sha256_hex(zip_bytes)
    repo_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<sdk:sdk-repository xmlns:sdk="http://schemas.android.com/sdk/android/repo/repository2/03">
  <remotePackage path="platform-tools">
    <channelRef ref="channel-0"/>
    <archives>
      <archive>
        <complete>
          <size>{len(zip_bytes)}</size>
          <checksum type="sha256">{digest}</checksum>
          <url>platform-tools_r36.0.0-windows.zip</url>
        </complete>
        <host-os>windows</host-os>
      </archive>
    </archives>
  </remotePackage>
</sdk:sdk-repository>
""".encode("utf-8")

    target_dir = tmp_path / "platform-tools"

    def fake_urlopen(url: str, timeout: int = 30) -> _FakeResponse:
        if url == PLATFORM_TOOLS_REPOSITORY_XML_URL:
            return _FakeResponse(repo_xml)
        if url == PLATFORM_TOOLS_URL_WINDOWS:
            return _FakeResponse(zip_bytes)
        raise AssertionError(f"Unexpected URL: {url}")

    with patch("adapt_adb.adb._open_google_url", side_effect=fake_urlopen):
        with patch("subprocess.run", side_effect=_fake_adb_version_run("Version 36.0.0-123456")):
            actual = download_and_extract_platform_tools(
                PLATFORM_TOOLS_URL_WINDOWS,
                target_dir=target_dir,
            )

    assert actual == digest
    assert (target_dir / "adb.exe").read_bytes() == b"windows-adb"


def test_download_rejects_sha256_mismatch(tmp_path: Path) -> None:
    """A SHA-256 mismatch aborts without modifying target_dir."""
    zip_bytes = _build_zip_bytes({"platform-tools/adb": b"tampered"})
    wrong_digest = "0" * 64
    target_dir = tmp_path / "platform-tools"
    target_dir.mkdir()
    (target_dir / "existing.txt").write_text("keep-me", encoding="utf-8")

    with patch("adapt_adb.adb._open_google_url", return_value=_FakeResponse(zip_bytes)):
        with pytest.raises(ValueError, match="SHA-256 checksum mismatch"):
            download_and_extract_platform_tools(
                PLATFORM_TOOLS_URL_LINUX,
                target_dir=target_dir,
                expected_sha256=wrong_digest,
            )

    assert (target_dir / "existing.txt").read_text(encoding="utf-8") == "keep-me"
    assert not (target_dir / "adb").exists()
    assert [p.name for p in target_dir.parent.iterdir()] == ["platform-tools"]


def test_download_fails_when_digest_unavailable_by_default(tmp_path: Path) -> None:
    """When repo XML has no checksums  download aborts without downloading zip; when XML has sha1-only, it verifies sha1 and returns sha256."""
    no_checksum_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<sdk-repository>
  <remotePackage path="platform-tools">
    <archives>
      <archive>
        <complete>
          <url>platform-tools_r36.0.0-linux.zip</url>
        </complete>
        <host-os>linux</host-os>
      </archive>
    </archives>
  </remotePackage>
</sdk-repository>
"""
    target_dir = tmp_path / "platform-tools"

    def fake_urlopen_no_checksum(url: str, timeout: int = 30) -> _FakeResponse:
        if url == PLATFORM_TOOLS_REPOSITORY_XML_URL:
            return _FakeResponse(no_checksum_xml)
        raise AssertionError("Zip should not be downloaded when digest is unavailable")

    with patch("adapt_adb.adb._open_google_url", side_effect=fake_urlopen_no_checksum):
        with pytest.raises(RuntimeError, match="SHA-256 or SHA-1 digest unavailable"):
            download_and_extract_platform_tools(
                PLATFORM_TOOLS_URL_LINUX,
                target_dir=target_dir,
            )

    assert not target_dir.exists()

    # Success case: sha1-only XML for linux, zip bytes whose sha1 matches, unpack proceeds, returned value is sha256 of the zip
    zip_bytes = _build_zip_bytes(
        {
            "platform-tools/adb": b"linux-adb-sha1-verified",
            "platform-tools/source.properties": b"Pkg.Revision=36.0.0\n",
        }
    )
    expected_sha1 = hashlib.sha1(zip_bytes).hexdigest()
    expected_sha256 = _sha256_hex(zip_bytes)
    sha1_only_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<sdk-repository>
  <remotePackage path="platform-tools">
    <archives>
      <archive>
        <complete>
          <checksum type="sha1">{expected_sha1}</checksum>
          <url>platform-tools_r36.0.0-linux.zip</url>
        </complete>
        <host-os>linux</host-os>
      </archive>
    </archives>
  </remotePackage>
</sdk-repository>
""".encode("utf-8")

    def fake_urlopen_sha1(url: str, timeout: int = 30) -> _FakeResponse:
        if url == PLATFORM_TOOLS_REPOSITORY_XML_URL:
            return _FakeResponse(sha1_only_xml)
        if url == PLATFORM_TOOLS_URL_LINUX:
            return _FakeResponse(zip_bytes)
        raise AssertionError(f"Unexpected URL: {url}")

    with patch("adapt_adb.adb._open_google_url", side_effect=fake_urlopen_sha1):
        with patch("subprocess.run", side_effect=_fake_adb_version_run("Version 36.0.0-123456")):
            actual_sha256 = download_and_extract_platform_tools(
                PLATFORM_TOOLS_URL_LINUX,
                target_dir=target_dir,
            )

    assert actual_sha256 == expected_sha256
    assert (target_dir / "adb").read_bytes() == b"linux-adb-sha1-verified"


def test_download_mixed_channels_uses_channel_0_sha1(tmp_path: Path) -> None:
    """Channel-0 SHA-1 is used over channel-2 SHA-256 when downloading latest-linux.zip."""
    zip_a = _build_zip_bytes(
        {
            "platform-tools/adb": b"stable-channel-0-adb",
            "platform-tools/source.properties": b"Pkg.Revision=36.0.0\n",
        }
    )
    zip_b = _build_zip_bytes(
        {
            "platform-tools/adb": b"preview-channel-2-adb",
            "platform-tools/source.properties": b"Pkg.Revision=36.1.0-rc1\n",
        }
    )
    sha1_a = hashlib.sha1(zip_a).hexdigest()
    sha256_a = _sha256_hex(zip_a)
    sha256_b = _sha256_hex(zip_b)

    mixed_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<sdk:sdk-repository xmlns:sdk="http://schemas.android.com/sdk/android/repo/repository2/03">
  <remotePackage path="platform-tools">
    <channelRef ref="channel-0"/>
    <archives>
      <archive>
        <complete>
          <checksum type="sha1">{sha1_a}</checksum>
          <url>platform-tools_r36.0.0-linux.zip</url>
        </complete>
        <host-os>linux</host-os>
      </archive>
    </archives>
  </remotePackage>
  <remotePackage path="platform-tools">
    <channelRef ref="channel-2"/>
    <archives>
      <archive>
        <complete>
          <checksum type="sha256">{sha256_b}</checksum>
          <url>platform-tools_r36.1.0-rc1-linux.zip</url>
        </complete>
        <host-os>linux</host-os>
      </archive>
    </archives>
  </remotePackage>
</sdk:sdk-repository>
""".encode("utf-8")

    target_dir = tmp_path / "platform-tools"

    # Serving zip B must fail closed with SHA-1 mismatch against channel-0's sha1_a
    def fake_urlopen_zip_b(url: str, timeout: int = 30) -> _FakeResponse:
        if url == PLATFORM_TOOLS_REPOSITORY_XML_URL:
            return _FakeResponse(mixed_xml)
        if url == PLATFORM_TOOLS_URL_LINUX:
            return _FakeResponse(zip_b)
        raise AssertionError(f"Unexpected URL: {url}")

    with patch("adapt_adb.adb._open_google_url", side_effect=fake_urlopen_zip_b):
        with pytest.raises(ValueError, match="SHA-1 checksum mismatch"):
            download_and_extract_platform_tools(
                PLATFORM_TOOLS_URL_LINUX,
                target_dir=target_dir,
            )

    assert not target_dir.exists()

    # Serving zip A succeeds, unpacks zip A, and returns sha256 of zip A
    def fake_urlopen_zip_a(url: str, timeout: int = 30) -> _FakeResponse:
        if url == PLATFORM_TOOLS_REPOSITORY_XML_URL:
            return _FakeResponse(mixed_xml)
        if url == PLATFORM_TOOLS_URL_LINUX:
            return _FakeResponse(zip_a)
        raise AssertionError(f"Unexpected URL: {url}")

    with patch("adapt_adb.adb._open_google_url", side_effect=fake_urlopen_zip_a):
        with patch("subprocess.run", side_effect=_fake_adb_version_run("Version 36.0.0-123456")):
            actual_sha256 = download_and_extract_platform_tools(
                PLATFORM_TOOLS_URL_LINUX,
                target_dir=target_dir,
            )

    assert actual_sha256 == sha256_a
    assert (target_dir / "adb").read_bytes() == b"stable-channel-0-adb"


def test_download_exact_filename_wins_over_channel_0_versioned_sha1(tmp_path: Path) -> None:
    """Exact archive URL basename match (platform-tools-latest-linux.zip) wins over channel-0 versioned SHA-1."""
    zip_a = _build_zip_bytes(
        {
            "platform-tools/adb": b"versioned-channel-0-adb",
            "platform-tools/source.properties": b"Pkg.Revision=36.0.0\n",
        }
    )
    zip_c = _build_zip_bytes(
        {
            "platform-tools/adb": b"exact-latest-linux-adb",
            "platform-tools/source.properties": b"Pkg.Revision=36.0.1\n",
        }
    )
    sha1_a = hashlib.sha1(zip_a).hexdigest()
    sha256_c = _sha256_hex(zip_c)

    exact_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<sdk:sdk-repository xmlns:sdk="http://schemas.android.com/sdk/android/repo/repository2/03">
  <remotePackage path="platform-tools">
    <channelRef ref="channel-0"/>
    <archives>
      <archive>
        <complete>
          <checksum type="sha1">{sha1_a}</checksum>
          <url>platform-tools_r36.0.0-linux.zip</url>
        </complete>
        <host-os>linux</host-os>
      </archive>
    </archives>
  </remotePackage>
  <remotePackage path="platform-tools">
    <channelRef ref="channel-2"/>
    <archives>
      <archive>
        <complete>
          <checksum type="sha256">{sha256_c}</checksum>
          <url>platform-tools-latest-linux.zip</url>
        </complete>
        <host-os>linux</host-os>
      </archive>
    </archives>
  </remotePackage>
</sdk:sdk-repository>
""".encode("utf-8")

    target_dir = tmp_path / "platform-tools"

    def fake_urlopen(url: str, timeout: int = 30) -> _FakeResponse:
        if url == PLATFORM_TOOLS_REPOSITORY_XML_URL:
            return _FakeResponse(exact_xml)
        if url == PLATFORM_TOOLS_URL_LINUX:
            return _FakeResponse(zip_c)
        raise AssertionError(f"Unexpected URL: {url}")

    with patch("adapt_adb.adb._open_google_url", side_effect=fake_urlopen):
        with patch("subprocess.run", side_effect=_fake_adb_version_run("Version 36.0.1-123456")):
            actual_sha256 = download_and_extract_platform_tools(
                PLATFORM_TOOLS_URL_LINUX,
                target_dir=target_dir,
            )

    assert actual_sha256 == sha256_c
    assert (target_dir / "adb").read_bytes() == b"exact-latest-linux-adb"


def test_download_rejects_sha1_mismatch(tmp_path: Path) -> None:
    """When repo XML provides sha1 and the downloaded zip sha1 mismatches, fail closed without unpacking."""
    zip_bytes = _build_zip_bytes({"platform-tools/adb": b"tampered-sha1"})
    wrong_sha1 = "0" * 40
    sha1_only_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<sdk-repository>
  <remotePackage path="platform-tools">
    <archives>
      <archive>
        <complete>
          <checksum type="sha1">{wrong_sha1}</checksum>
          <url>platform-tools_r36.0.0-linux.zip</url>
        </complete>
        <host-os>linux</host-os>
      </archive>
    </archives>
  </remotePackage>
</sdk-repository>
""".encode("utf-8")
    target_dir = tmp_path / "platform-tools"

    def fake_urlopen(url: str, timeout: int = 30) -> _FakeResponse:
        if url == PLATFORM_TOOLS_REPOSITORY_XML_URL:
            return _FakeResponse(sha1_only_xml)
        if url == PLATFORM_TOOLS_URL_LINUX:
            return _FakeResponse(zip_bytes)
        raise AssertionError(f"Unexpected URL: {url}")

    with patch("adapt_adb.adb._open_google_url", side_effect=fake_urlopen):
        with pytest.raises(ValueError, match="SHA-1 checksum mismatch"):
            download_and_extract_platform_tools(
                PLATFORM_TOOLS_URL_LINUX,
                target_dir=target_dir,
            )

    assert not target_dir.exists()


def test_download_missing_digest_aborts_without_download(tmp_path: Path) -> None:
    target_dir = tmp_path / "platform-tools"
    with patch("adapt_adb.adb.fetch_platform_tools_digest", return_value=None):
        with patch("adapt_adb.adb._open_google_url") as download:
            with patch("subprocess.run") as execute:
                with pytest.raises(RuntimeError, match="digest unavailable"):
                    download_and_extract_platform_tools(
                        PLATFORM_TOOLS_URL_LINUX, target_dir=target_dir,
                    )
    download.assert_not_called()
    execute.assert_not_called()
    assert not target_dir.exists()


@pytest.mark.parametrize(
    "malicious_entry",
    [
        "../evil.txt",
        "platform-tools/../../evil.txt",
        "platform-tools/sub/../../../evil.txt",
        r"..\..\evil.txt",
        "/etc/passwd",
        r"\Windows\System32\evil.dll",
        "//server/share/evil.txt",
        "C:\\evil.txt",
        "C:evil.txt",
    ],
)
def test_rejects_zip_slip_paths(tmp_path: Path, malicious_entry: str) -> None:
    """Malicious zip-slip / traversal / absolute paths are rejected before touching target_dir."""
    zip_bytes = _build_zip_bytes(
        {
            "platform-tools/adb": b"legit",
            malicious_entry: b"pwned",
        }
    )
    digest = _sha256_hex(zip_bytes)
    target_dir = tmp_path / "safe" / "platform-tools"

    with patch("adapt_adb.adb._open_google_url", return_value=_FakeResponse(zip_bytes)):
        with pytest.raises(ValueError, match="Unsafe"):
            download_and_extract_platform_tools(
                PLATFORM_TOOLS_URL_LINUX,
                target_dir=target_dir,
                expected_sha256=digest,
            )

    assert not target_dir.exists()
    assert not (tmp_path / "evil.txt").exists()
    assert not (tmp_path / "safe" / "evil.txt").exists()


def test_rejects_zip_symlink_entry(tmp_path: Path) -> None:
    """Symlink entries inside zip archives are rejected."""
    zip_bytes = _build_zip_bytes(
        {"platform-tools/adb_link": b"/etc/passwd"},
        symlink_names={"platform-tools/adb_link"},
    )
    digest = _sha256_hex(zip_bytes)
    target_dir = tmp_path / "platform-tools"

    with patch("adapt_adb.adb._open_google_url", return_value=_FakeResponse(zip_bytes)):
        with pytest.raises(ValueError, match="symlink"):
            download_and_extract_platform_tools(
                PLATFORM_TOOLS_URL_LINUX,
                target_dir=target_dir,
                expected_sha256=digest,
            )

    assert not target_dir.exists()


def test_atomic_swap_replaces_existing_dir_cleanly(tmp_path: Path) -> None:
    """Atomic swap replaces stale files and rolls back if swap fails."""
    target_dir = tmp_path / "platform-tools"
    target_dir.mkdir()
    (target_dir / "stale_old_file.txt").write_text("old", encoding="utf-8")

    zip_bytes = _build_zip_bytes({"adb": b"new-binary"})
    digest = _sha256_hex(zip_bytes)

    with patch("adapt_adb.adb._open_google_url", return_value=_FakeResponse(zip_bytes)):
        with patch("subprocess.run", side_effect=_fake_adb_version_run()):
            download_and_extract_platform_tools(
                PLATFORM_TOOLS_URL_LINUX,
                target_dir=target_dir,
                expected_sha256=digest,
            )

    assert not (target_dir / "stale_old_file.txt").exists()
    assert (target_dir / "adb").read_bytes() == b"new-binary"
    assert [p.name for p in target_dir.parent.iterdir()] == ["platform-tools"]


def test_extracts_to_user_scope_default_dir_when_target_omitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Omitting target_dir extracts atomically into get_default_platform_tools_dir()."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))
    monkeypatch.setenv("HOME", str(tmp_path / "Home"))
    monkeypatch.delenv("ANDROID_HOME", raising=False)
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)

    expected_dir = get_default_platform_tools_dir()
    zip_bytes = _build_zip_bytes({"platform-tools/adb": b"default-dir-adb"})
    digest = _sha256_hex(zip_bytes)

    with patch("adapt_adb.adb._open_google_url", return_value=_FakeResponse(zip_bytes)):
        with patch("subprocess.run", side_effect=_fake_adb_version_run()):
            download_and_extract_platform_tools(
                PLATFORM_TOOLS_URL_LINUX,
                expected_sha256=digest,
            )

    assert expected_dir.is_dir()
    assert (expected_dir / "adb").read_bytes() == b"default-dir-adb"


def test_never_writes_into_android_home_or_sdk_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refuses to write into ANDROID_HOME or ANDROID_SDK_ROOT or overlap with them."""
    sdk_dir = tmp_path / "Android" / "Sdk"
    sdk_dir.mkdir(parents=True)
    sentinel = sdk_dir / "sentinel.txt"
    sentinel.write_text("do-not-touch", encoding="utf-8")

    monkeypatch.setenv("ANDROID_HOME", str(sdk_dir))
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)

    zip_bytes = _build_zip_bytes({"platform-tools/adb": b"should-not-write"})
    digest = _sha256_hex(zip_bytes)

    with patch("adapt_adb.adb._open_google_url") as mock_open:
        for bad_target in (sdk_dir, sdk_dir / "platform-tools", tmp_path / "Android"):
            with pytest.raises(ValueError, match="ANDROID_HOME"):
                download_and_extract_platform_tools(
                    PLATFORM_TOOLS_URL_LINUX,
                    target_dir=bad_target,
                    expected_sha256=digest,
                )

        # Also check ANDROID_SDK_ROOT
        monkeypatch.delenv("ANDROID_HOME", raising=False)
        monkeypatch.setenv("ANDROID_SDK_ROOT", str(sdk_dir))
        with pytest.raises(ValueError, match="ANDROID_SDK_ROOT"):
            download_and_extract_platform_tools(
                PLATFORM_TOOLS_URL_LINUX,
                target_dir=sdk_dir / "platform-tools",
                expected_sha256=digest,
            )

    mock_open.assert_not_called()
    assert sentinel.read_text(encoding="utf-8") == "do-not-touch"


def test_rejects_non_https_or_untrusted_host(tmp_path: Path) -> None:
    """Only HTTPS URLs on dl.google.com are accepted."""
    target_dir = tmp_path / "platform-tools"
    with patch("adapt_adb.adb._open_google_url") as mock_open:
        with pytest.raises(ValueError, match="HTTPS"):
            download_and_extract_platform_tools(
                "http://dl.google.com/android/repository/platform-tools-latest-linux.zip",
                target_dir=target_dir,
                expected_sha256="a" * 64,
            )
        with pytest.raises(ValueError, match="allowed hosts"):
            download_and_extract_platform_tools(
                "https://evil.example.com/platform-tools.zip",
                target_dir=target_dir,
                expected_sha256="a" * 64,
            )
    mock_open.assert_not_called()


def test_staging_adb_version_below_34_refuses_swap_and_keeps_existing(tmp_path: Path) -> None:
    """Staged adb < 34.0.0 aborts before swap, leaves target_dir intact, and cleans staging."""
    target_dir = tmp_path / "platform-tools"
    target_dir.mkdir()
    (target_dir / "existing.txt").write_text("keep-me", encoding="utf-8")
    (target_dir / "adb.exe").write_bytes(b"existing-adb")

    zip_bytes = _build_zip_bytes(
        {
            "platform-tools/adb.exe": b"old-33-adb",
            "platform-tools/source.properties": b"Pkg.Revision=33.0.3\n",
        }
    )
    digest = _sha256_hex(zip_bytes)
    recorded_cmds: list[list[str]] = []

    with patch("adapt_adb.adb._open_google_url", return_value=_FakeResponse(zip_bytes)):
        with patch(
            "subprocess.run",
            side_effect=_fake_adb_version_run(
                "Version 33.0.3-9482159",
                recorded_cmds=recorded_cmds,
            ),
        ):
            with pytest.raises(RuntimeError, match="34.0.0"):
                download_and_extract_platform_tools(
                    PLATFORM_TOOLS_URL_WINDOWS,
                    target_dir=target_dir,
                    expected_sha256=digest,
                )

    assert len(recorded_cmds) == 1
    assert recorded_cmds[0][1] == "version"
    assert ".platform-tools-staging-" in recorded_cmds[0][0]
    assert (target_dir / "existing.txt").read_text(encoding="utf-8") == "keep-me"
    assert (target_dir / "adb.exe").read_bytes() == b"existing-adb"
    assert not (target_dir / "source.properties").exists()
    assert [p.name for p in target_dir.parent.iterdir()] == ["platform-tools"]


@pytest.mark.parametrize(
    "case",
    ["nonzero_exit", "unparseable_output", "oserror", "missing_binary"],
)
def test_staging_adb_version_unreadable_refuses_swap(tmp_path: Path, case: str) -> None:
    """Unreadable, failing, or missing staged adb refuses swap and cleans staging."""
    target_dir = tmp_path / "platform-tools"
    target_dir.mkdir()
    (target_dir / "existing.txt").write_text("keep-me", encoding="utf-8")

    entries = (
        {"platform-tools/source.properties": b"Pkg.Revision=35.0.0\n"}
        if case == "missing_binary"
        else {"platform-tools/adb": b"bad-adb"}
    )
    zip_bytes = _build_zip_bytes(entries)
    digest = _sha256_hex(zip_bytes)

    def _fake_run(cmd, **kwargs):
        if case == "oserror":
            raise OSError("cannot execute")
        if case == "nonzero_exit":
            return SimpleNamespace(stdout="", stderr="broken", returncode=1)
        return SimpleNamespace(
            stdout="Android Debug Bridge version 1.0.41\n",
            stderr="",
            returncode=0,
        )

    with patch("adapt_adb.adb._open_google_url", return_value=_FakeResponse(zip_bytes)):
        with patch("subprocess.run", side_effect=_fake_run):
            with pytest.raises(RuntimeError):
                download_and_extract_platform_tools(
                    PLATFORM_TOOLS_URL_LINUX,
                    target_dir=target_dir,
                    expected_sha256=digest,
                )

    assert (target_dir / "existing.txt").read_text(encoding="utf-8") == "keep-me"
    assert not (target_dir / "adb").exists()
    assert [p.name for p in target_dir.parent.iterdir()] == ["platform-tools"]


@pytest.mark.parametrize(
    ("zip_entries", "version_line", "expected_rel_adb"),
    [
        (
            {"platform-tools/adb": b"adb-34"},
            "Version 34.0.0",
            Path("adb"),
        ),
        (
            {
                "platform-tools/adb": b"adb-35",
                "NOTICE.txt": b"notice",
            },
            "Version 35.0.2-12147458",
            Path("platform-tools") / "adb",
        ),
    ],
)
def test_staging_adb_version_34_or_higher_swaps(
    tmp_path: Path,
    zip_entries: dict[str, bytes],
    version_line: str,
    expected_rel_adb: Path,
) -> None:
    """Staged adb >= 34.0.0 (at staging root or platform-tools/) passes check and swaps cleanly."""
    target_dir = tmp_path / "install" / "platform-tools"
    zip_bytes = _build_zip_bytes(zip_entries)
    digest = _sha256_hex(zip_bytes)
    recorded_cmds: list[list[str]] = []

    with patch("adapt_adb.adb._open_google_url", return_value=_FakeResponse(zip_bytes)):
        with patch(
            "subprocess.run",
            side_effect=_fake_adb_version_run(version_line, recorded_cmds=recorded_cmds),
        ):
            returned = download_and_extract_platform_tools(
                PLATFORM_TOOLS_URL_LINUX,
                target_dir=target_dir,
                expected_sha256=digest,
            )

    assert returned == digest
    assert len(recorded_cmds) == 1
    assert recorded_cmds[0][1] == "version"
    assert ".platform-tools-staging-" in recorded_cmds[0][0]
    assert (target_dir / expected_rel_adb).read_bytes() == zip_entries[
        next(k for k in zip_entries if k.endswith("adb"))
    ]
    assert [p.name for p in target_dir.parent.iterdir()] == ["platform-tools"]

