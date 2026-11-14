"""Unit tests for platform-tools download and extraction hardening in adapt_adb.adb."""

import io
import stat
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from adapt_adb.adb import (
    _MAX_ARCHIVE_MEMBERS,
    _MAX_EXPANDED_BYTES,
    _MAX_MEMBER_BYTES,
    GoogleRedirectHandler,
    _atomic_swap_dir,
    _download_to_path_with_digests,
    _extract_digest_from_repo_xml,
    _safe_extract_zip,
    download_and_extract_platform_tools,
)


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


# ---------------------------------------------------------------------------
# HP-DEF-01: Extraction expansion budget
# ---------------------------------------------------------------------------


def test_safe_extract_zip_too_many_members_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject archives exceeding the maximum member count before writing files."""
    monkeypatch.setattr("adapt_adb.adb._MAX_ARCHIVE_MEMBERS", 3)

    zip_bytes = _build_zip_bytes({
        f"platform-tools/file{i}.txt": b"content" for i in range(4)
    })
    zip_path = tmp_path / "too_many.zip"
    zip_path.write_bytes(zip_bytes)

    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()

    with pytest.raises(ValueError, match=r"Archive member count \(4\) exceeds maximum allowed \(3\)"):
        _safe_extract_zip(zip_path, staging_dir)

    assert list(staging_dir.iterdir()) == []


def test_safe_extract_zip_declared_member_size_exceeds_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject archives where a single member's declared size exceeds the per-member limit."""
    monkeypatch.setattr("adapt_adb.adb._MAX_MEMBER_BYTES", 50)

    zip_bytes = _build_zip_bytes({
        "platform-tools/big.bin": b"x" * 100,
    })
    zip_path = tmp_path / "big_member.zip"
    zip_path.write_bytes(zip_bytes)

    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()

    with pytest.raises(ValueError, match=r"declared size \(100 bytes\) exceeds per-member limit \(50 bytes\)"):
        _safe_extract_zip(zip_path, staging_dir)

    # Must fail before any file or directory is written into staging
    assert list(staging_dir.iterdir()) == []


def test_safe_extract_zip_declared_total_size_exceeds_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject archives where total declared size exceeds the expanded size limit."""
    monkeypatch.setattr("adapt_adb.adb._MAX_MEMBER_BYTES", 100)
    monkeypatch.setattr("adapt_adb.adb._MAX_EXPANDED_BYTES", 150)

    zip_bytes = _build_zip_bytes({
        "platform-tools/file1.bin": b"x" * 80,
        "platform-tools/file2.bin": b"y" * 80,
    })
    zip_path = tmp_path / "total_too_big.zip"
    zip_path.write_bytes(zip_bytes)

    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()

    with pytest.raises(ValueError, match=r"total declared size \(160 bytes\) exceeds maximum allowed \(150 bytes\)"):
        _safe_extract_zip(zip_path, staging_dir)

    assert list(staging_dir.iterdir()) == []


def test_safe_extract_zip_streaming_member_limit_triggers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Trigger per-member streaming enforcement when actual unpacked bytes exceed limit."""
    zip_bytes = _build_zip_bytes({
        "platform-tools/streamed.bin": b"x" * 50,
    })
    zip_path = tmp_path / "stream_member.zip"
    zip_path.write_bytes(zip_bytes)

    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()

    # Allow declared checks to pass with 100, but forge declared size as 10,
    # then restore full size at open time while setting member limit to 25.
    orig_infolist = zipfile.ZipFile.infolist
    orig_open = zipfile.ZipFile.open

    def forged_infolist(self):
        infos = orig_infolist(self)
        for info in infos:
            info.file_size = 10
        return infos

    def restored_open(self, name, *args, **kwargs):
        if isinstance(name, zipfile.ZipInfo):
            name.file_size = 50
        return orig_open(self, name, *args, **kwargs)

    monkeypatch.setattr("adapt_adb.adb._MAX_MEMBER_BYTES", 25)
    monkeypatch.setattr("adapt_adb.adb._DOWNLOAD_CHUNK_SIZE", 16)
    monkeypatch.setattr(zipfile.ZipFile, "infolist", forged_infolist)
    monkeypatch.setattr(zipfile.ZipFile, "open", restored_open)

    with pytest.raises(ValueError, match=r"extracted size exceeded per-member limit \(25 bytes\)"):
        _safe_extract_zip(zip_path, staging_dir)


def test_safe_extract_zip_streaming_total_limit_triggers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Trigger total streaming enforcement when cumulative unpacked bytes exceed total limit."""
    zip_bytes = _build_zip_bytes({
        "platform-tools/part1.bin": b"a" * 25,
        "platform-tools/part2.bin": b"b" * 25,
    })
    zip_path = tmp_path / "stream_total.zip"
    zip_path.write_bytes(zip_bytes)

    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()

    orig_infolist = zipfile.ZipFile.infolist
    orig_open = zipfile.ZipFile.open

    def forged_infolist(self):
        infos = orig_infolist(self)
        for info in infos:
            info.file_size = 10
        return infos

    def restored_open(self, name, *args, **kwargs):
        if isinstance(name, zipfile.ZipInfo):
            name.file_size = 25
        return orig_open(self, name, *args, **kwargs)

    monkeypatch.setattr("adapt_adb.adb._MAX_MEMBER_BYTES", 50)
    monkeypatch.setattr("adapt_adb.adb._MAX_EXPANDED_BYTES", 30)
    monkeypatch.setattr("adapt_adb.adb._DOWNLOAD_CHUNK_SIZE", 16)
    monkeypatch.setattr(zipfile.ZipFile, "infolist", forged_infolist)
    monkeypatch.setattr(zipfile.ZipFile, "open", restored_open)

    with pytest.raises(ValueError, match=r"total extracted size exceeded limit \(30 bytes\)"):
        _safe_extract_zip(zip_path, staging_dir)


def test_safe_extract_zip_normal_archive_extracts_correctly(tmp_path: Path) -> None:
    """A standard conforming archive unpacks successfully with platform-tools/ prefix stripped."""
    zip_bytes = _build_zip_bytes({
        "platform-tools/adb.exe": b"fake-adb-binary",
        "platform-tools/lib64/libc++.so": b"fake-library-data",
    })
    zip_path = tmp_path / "valid.zip"
    zip_path.write_bytes(zip_bytes)

    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()

    _safe_extract_zip(zip_path, staging_dir)

    assert (staging_dir / "adb.exe").is_file()
    assert (staging_dir / "adb.exe").read_bytes() == b"fake-adb-binary"
    assert (staging_dir / "lib64" / "libc++.so").is_file()
    assert (staging_dir / "lib64" / "libc++.so").read_bytes() == b"fake-library-data"
    assert not (staging_dir / "platform-tools").exists()


def test_download_and_extract_cleans_up_staging_on_extract_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify download_and_extract_platform_tools removes staging and leaves target intact on extraction failure."""
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    sentinel = target_dir / "preserve.txt"
    sentinel.write_text("must-survive", encoding="utf-8")

    # Zip with declared member size exceeding monkeypatched limit
    zip_bytes = _build_zip_bytes({
        "platform-tools/big.bin": b"x" * 100,
    })
    monkeypatch.setattr("adapt_adb.adb._MAX_MEMBER_BYTES", 50)

    class _FakeResponse:
        def __init__(self, data: bytes):
            self._buf = io.BytesIO(data)

        def read(self, size: int = -1) -> bytes:
            return self._buf.read(size)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("adapt_adb.adb._open_google_url", lambda url, hosts: _FakeResponse(zip_bytes))
    fake_sha256 = "0" * 64
    monkeypatch.setattr(
        "adapt_adb.adb.fetch_platform_tools_digest",
        lambda url, manifest_url=None: ("sha256", fake_sha256),
    )
    monkeypatch.setattr(
        "adapt_adb.adb._download_to_path_with_digests",
        lambda url, dest: (dest.write_bytes(zip_bytes), (fake_sha256, "0" * 40))[1],
    )

    with pytest.raises(ValueError, match="exceeds per-member limit"):
        download_and_extract_platform_tools("https://dl.google.com/platform-tools.zip", target_dir=target_dir)

    assert target_dir.exists()
    assert sentinel.read_text(encoding="utf-8") == "must-survive"
    staging_dirs = list(target_dir.parent.glob(".platform-tools-staging-*"))
    assert staging_dirs == []


# ---------------------------------------------------------------------------
# HP-IMP-03: Explicit redirect hop cap
# ---------------------------------------------------------------------------


def test_google_redirect_handler_explicit_cap_attribute() -> None:
    """GoogleRedirectHandler explicitly caps redirections at 5."""
    assert GoogleRedirectHandler.max_redirections == 5


def test_google_redirect_handler_enforces_cap_offline() -> None:
    """Opener with GoogleRedirectHandler raises HTTPError after exceeding 5 redirect hops."""
    class MockRedirectHTTPSHandler(urllib.request.BaseHandler):
        def __init__(self, max_hops: int):
            self.count = 0
            self.max_hops = max_hops

        def https_open(self, req: urllib.request.Request) -> urllib.response.addinfourl:
            self.count += 1
            if self.count <= self.max_hops:
                resp = urllib.response.addinfourl(
                    io.BytesIO(b""),
                    {"location": f"https://dl.google.com/hop{self.count}"},
                    req.full_url,
                    302,
                )
                resp.code = 302
                resp.msg = "Found"
                return resp
            resp = urllib.response.addinfourl(io.BytesIO(b"success"), {}, req.full_url, 200)
            resp.code = 200
            resp.msg = "OK"
            return resp

    # Case 1: 4 redirect hops (within cap 5) -> succeeds
    opener_ok = urllib.request.OpenerDirector()
    mock_ok = MockRedirectHTTPSHandler(max_hops=4)
    opener_ok.add_handler(mock_ok)
    opener_ok.add_handler(GoogleRedirectHandler(frozenset({"dl.google.com"})))
    opener_ok.add_handler(urllib.request.HTTPErrorProcessor())
    res = opener_ok.open("https://dl.google.com/start")
    assert res.read() == b"success"
    assert mock_ok.count == 5  # 1 initial + 4 redirects

    # Case 2: Exceeding 5 redirect hops -> HTTPError 302 after 5 redirects
    opener_capped = urllib.request.OpenerDirector()
    mock_capped = MockRedirectHTTPSHandler(max_hops=10)
    opener_capped.add_handler(mock_capped)
    opener_capped.add_handler(GoogleRedirectHandler(frozenset({"dl.google.com"})))
    opener_capped.add_handler(urllib.request.HTTPErrorProcessor())

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        opener_capped.open("https://dl.google.com/start")

    assert exc_info.value.code == 302
    assert mock_capped.count == 6  # 1 initial + 5 redirects attempted before cap triggered


# ---------------------------------------------------------------------------
# HP-GAP-01: Untested rollback in _atomic_swap_dir
# ---------------------------------------------------------------------------


def test_atomic_swap_dir_rollback_on_staging_replace_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When staging_dir.replace(target_dir) fails, restore target from backup and propagate exception."""
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    (target_dir / "original.txt").write_text("original content", encoding="utf-8")

    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    (staging_dir / "new.txt").write_text("new content", encoding="utf-8")

    staging_resolved = staging_dir.resolve()
    orig_replace = Path.replace

    def mock_replace(self: Path, target: Path | str) -> Path:
        if self.resolve() == staging_resolved:
            raise PermissionError("Simulated replace permission error")
        return orig_replace(self, target)

    monkeypatch.setattr(Path, "replace", mock_replace)

    with pytest.raises(PermissionError, match="Simulated replace permission error"):
        _atomic_swap_dir(staging_dir, target_dir)

    # Target directory must exist with original contents restored
    assert target_dir.exists()
    assert (target_dir / "original.txt").is_file()
    assert (target_dir / "original.txt").read_text(encoding="utf-8") == "original content"
    assert not (target_dir / "new.txt").exists()

    # Staging directory still exists
    assert staging_dir.exists()
    assert (staging_dir / "new.txt").is_file()

    # No leftover .bak-* directory remains
    bak_dirs = list(tmp_path.glob(".*bak*"))
    assert bak_dirs == []


# ---------------------------------------------------------------------------
# HP-GAP-02: Missing direct tests
# ---------------------------------------------------------------------------


def test_download_to_path_with_digests_exceeds_max_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_download_to_path_with_digests raises ValueError when downloaded stream exceeds byte limit."""
    stream_data = b"a" * 200
    monkeypatch.setattr("adapt_adb.adb._MAX_PLATFORM_TOOLS_ZIP_BYTES", 100)
    monkeypatch.setattr("adapt_adb.adb._DOWNLOAD_CHUNK_SIZE", 32)

    class _FakeStreamResponse:
        def __init__(self, data: bytes):
            self._buf = io.BytesIO(data)

        def read(self, size: int = -1) -> bytes:
            return self._buf.read(size)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        "adapt_adb.adb._open_google_url",
        lambda url, hosts: _FakeStreamResponse(stream_data),
    )

    dest = tmp_path / "dest.zip"
    with pytest.raises(ValueError, match=r"Downloaded archive exceeded 100 byte limit"):
        _download_to_path_with_digests("https://dl.google.com/test.zip", dest)


def test_extract_digest_from_repo_xml_rejects_doctype() -> None:
    """_extract_digest_from_repo_xml rejects XML declaring DOCTYPE."""
    url = "https://dl.google.com/android/repository/repository2-3.xml"
    xml_doctype = b"""<?xml version="1.0"?>
    <!DOCTYPE repo [ <!ELEMENT repo ANY> ]>
    <repo></repo>
    """
    with pytest.raises(ValueError, match="Repository XML contains forbidden DOCTYPE or ENTITY declaration."):
        _extract_digest_from_repo_xml(xml_doctype, url)


def test_extract_digest_from_repo_xml_rejects_entity() -> None:
    """_extract_digest_from_repo_xml rejects XML declaring ENTITY."""
    url = "https://dl.google.com/android/repository/repository2-3.xml"
    xml_entity = b"""<?xml version="1.0"?>
    <!ENTITY custom "expansion">
    <repo>&custom;</repo>
    """
    with pytest.raises(ValueError, match="Repository XML contains forbidden DOCTYPE or ENTITY declaration."):
        _extract_digest_from_repo_xml(xml_entity, url)
