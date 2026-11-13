from __future__ import annotations

import hashlib
import io
import json
import stat
import sys
import tarfile
import urllib.request
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from adapt_adb import scrcpy_install as si


LICENSE_TEXT = b"""Copyright (C) 2018 Genymobile

Apache License
Version 2.0, January 2004
"""
SERVER_BYTES = b"pinned-test-server"
CLIENT_BYTES = b"pinned-test-client"


class FakeResponse(io.BytesIO):
    def __init__(self, payload: bytes, url: str, *, content_length: int | None = None):
        super().__init__(payload)
        self._url = url
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def geturl(self) -> str:
        return self._url


class NonInteractiveInput:
    def isatty(self) -> bool:
        return False

    def readline(self) -> str:
        raise AssertionError("non-interactive input must not be read")


def _artifact(platform_name: str = "win32", machine: str = "AMD64") -> si.ScrcpyArtifact:
    return si.detect_scrcpy_artifact(platform_name, machine)


def _zip_payload(
    artifact: si.ScrcpyArtifact,
    *,
    extra: dict[str, bytes] | None = None,
) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        root = artifact.package_dir
        archive.writestr(f"{root}/", b"")
        archive.writestr(f"{root}/{artifact.executable_name}", CLIENT_BYTES)
        archive.writestr(f"{root}/scrcpy-server", SERVER_BYTES)
        archive.writestr(f"{root}/{artifact.license_name}", LICENSE_TEXT)
        archive.writestr(f"{root}/NOTICE-dependencies.txt", b"Dependencies have separate licenses.\n")
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return stream.getvalue()


def _tar_payload(
    artifact: si.ScrcpyArtifact,
    *,
    link_name: str | None = None,
) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        root = tarfile.TarInfo(f"{artifact.package_dir}/")
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        archive.addfile(root)
        for name, data, mode in (
            (artifact.executable_name, CLIENT_BYTES, 0o755),
            ("scrcpy-server", SERVER_BYTES, 0o644),
            (artifact.license_name, LICENSE_TEXT, 0o644),
        ):
            member = tarfile.TarInfo(f"{artifact.package_dir}/{name}")
            member.size = len(data)
            member.mode = mode
            archive.addfile(member, io.BytesIO(data))
        if link_name:
            link = tarfile.TarInfo(f"{artifact.package_dir}/{link_name}")
            link.type = tarfile.SYMTYPE
            link.linkname = artifact.executable_name
            archive.addfile(link)
    return stream.getvalue()


def _test_artifact_for_bytes(
    artifact: si.ScrcpyArtifact, payload: bytes
) -> si.ScrcpyArtifact:
    return replace(
        artifact,
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def test_pinned_release_matrix_and_hashes() -> None:
    cases = {
        ("win32", "AMD64"): (
            "scrcpy-win64-v5.0.1.zip",
            "b12a2c4ee8be317422451fc7dcf8ee20a71b5ea7ef9ad73ddd825a25316227a5",
            11_264_634,
        ),
        ("win32", "i686"): (
            "scrcpy-win32-v5.0.1.zip",
            "0cb65ebf1bce892fa4d1502d699af0c7a1a7f24d7febcb9ef72894e3f23fc2ad",
            10_133_753,
        ),
        ("win32", "ARM64"): (
            "scrcpy-winarm64-v5.0.1.zip",
            "16c3fc2068df64946f670e3504b4752073ebae47a416a45b46e79a5ab19c689b",
            9_825_739,
        ),
        ("linux", "x86_64"): (
            "scrcpy-linux-x86_64-v5.0.1.tar.gz",
            "9f969d30cc574816077edecda65719c1c70b0aee1df1ba5fa00779ac07b8fd6e",
            17_883_742,
        ),
    }
    for host, expected in cases.items():
        artifact = si.detect_scrcpy_artifact(*host)
        assert (artifact.asset_name, artifact.sha256, artifact.size) == expected
        assert artifact.source_url.startswith(
            "https://github.com/Genymobile/scrcpy/releases/download/v5.0.1/"
        )


@pytest.mark.parametrize(
    ("platform_name", "machine"),
    [("linux", "aarch64"), ("darwin", "x86_64"), ("win32", "mips")],
)
def test_unsupported_hosts_refuse_actionably(platform_name: str, machine: str) -> None:
    with pytest.raises(si.UnsupportedScrcpyHostError, match="[Rr]efus|supports only"):
        si.detect_scrcpy_artifact(platform_name, machine)


def test_plan_reuses_existing_path_without_probe(tmp_path: Path) -> None:
    existing = tmp_path / "foreign" / "scrcpy.exe"
    calls: list[str] = []

    def finder(name: str) -> str:
        calls.append(name)
        return str(existing)

    plan = si.plan_scrcpy(platform_name="win32", machine="AMD64", which=finder)
    assert plan["status"] == "reuse_existing"
    assert plan["binary_path"] == str(existing)
    assert plan["version"] == "not_probed"
    assert plan["trusted"] is False
    assert plan["writes"] is False
    assert calls == ["scrcpy"]

    result = si.install_scrcpy(
        platform_name="win32",
        machine="AMD64",
        which=finder,
        confirm=lambda _: (_ for _ in ()).throw(AssertionError("reuse must not prompt")),
    )
    assert result["status"] == "reuse_existing"


def test_plan_is_offline_and_reports_exact_target(tmp_path: Path) -> None:
    plan = si.plan_scrcpy(
        platform_name="win32",
        machine="AMD64",
        install_root=tmp_path / "managed",
        which=lambda _: None,
    )
    assert plan["status"] == "ready"
    assert plan["confirmation_required"] is True
    assert plan["asset"]["sha256"] == _artifact().sha256
    assert Path(plan["target_dir"]).parts[-2:] == ("5.0.1", "scrcpy-win64-v5.0.1")
    assert not (tmp_path / "managed").exists()


def test_noninteractive_without_yes_fails_before_write_or_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdin", NonInteractiveInput())
    opened = False

    def should_not_open(_: si.ScrcpyArtifact):
        nonlocal opened
        opened = True
        raise AssertionError

    with pytest.raises(si.ScrcpyConfirmationRequiredError, match="--yes"):
        si.install_scrcpy(
            dry_run=False,
            platform_name="win32",
            machine="AMD64",
            install_root=tmp_path / "managed",
            which=lambda _: None,
            open_asset=should_not_open,
        )
    assert opened is False
    assert not (tmp_path / "managed").exists()


def test_dry_run_needs_no_confirmation_or_download(tmp_path: Path) -> None:
    def should_not_open(_: si.ScrcpyArtifact):
        raise AssertionError("dry run must not download")

    result = si.install_scrcpy(
        dry_run=True,
        platform_name="win32",
        machine="AMD64",
        install_root=tmp_path / "managed",
        which=lambda _: None,
        open_asset=should_not_open,
        confirm=lambda _: pytest.fail("Dry-run must not ask permission"),
    )
    assert result["status"] == "dry_run"
    assert result["writes"] is False
    assert result["downloads"] is False
    assert not (tmp_path / "managed").exists()


def test_declined_confirmation_has_no_writes(tmp_path: Path) -> None:
    result = si.install_scrcpy(
        platform_name="win32",
        machine="AMD64",
        install_root=tmp_path / "managed",
        which=lambda _: None,
        confirm=lambda _: False,
    )
    assert result["status"] == "cancelled"
    assert result["writes"] is False
    assert not (tmp_path / "managed").exists()


def test_existing_unmanaged_target_is_never_clobbered(tmp_path: Path) -> None:
    artifact = _artifact()
    target = tmp_path / "managed" / si.SCRCPY_VERSION / artifact.package_dir
    target.mkdir(parents=True)
    marker = target / "owner.txt"
    marker.write_text("unrelated", encoding="utf-8")

    plan = si.plan_scrcpy(
        platform_name="win32",
        machine="AMD64",
        install_root=tmp_path / "managed",
        which=lambda _: None,
    )
    assert plan["status"] == "conflict"
    with pytest.raises(si.ScrcpyInstallConflictError, match="overwrite"):
        si.install_scrcpy(
            yes=True,
            platform_name="win32",
            machine="AMD64",
            install_root=tmp_path / "managed",
            which=lambda _: None,
        )
    assert marker.read_text(encoding="utf-8") == "unrelated"


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/Genymobile/scrcpy/releases/download/v5.0.1/scrcpy-win64-v5.0.1.zip",
        "https://user:secret@github.com/Genymobile/scrcpy/releases/download/v5.0.1/scrcpy-win64-v5.0.1.zip",
        "https://github.com:444/Genymobile/scrcpy/releases/download/v5.0.1/scrcpy-win64-v5.0.1.zip",
        "https://github.com/Genymobile/scrcpy/releases/latest/download/scrcpy-win64-v5.0.1.zip",
        "https://github.com/Genymobile/scrcpy/releases/download/v5.0.1/scrcpy-win64-v5.0.1.zip?token=secret",
        "https://example.invalid/asset.zip",
    ],
)
def test_initial_release_url_policy_rejects_escape(url: str) -> None:
    with pytest.raises(si.ScrcpyIntegrityError):
        si._validate_delivery_url(url, _artifact(), initial=True)


def test_redirect_policy_accepts_only_https_asset_delivery_and_sanitizes_query() -> None:
    artifact = _artifact()
    signed = "https://release-assets.githubusercontent.com/opaque/asset?sig=do-not-store"
    si._validate_delivery_url(signed, artifact, initial=False)
    assert si._sanitize_url(signed) == "https://release-assets.githubusercontent.com/opaque/asset"
    with pytest.raises(si.ScrcpyIntegrityError):
        si._validate_delivery_url(
            "http://release-assets.githubusercontent.com/opaque/asset?sig=secret",
            artifact,
            initial=False,
        )
    with pytest.raises(si.ScrcpyIntegrityError):
        si._validate_delivery_url(
            "https://objects.githubusercontent.com/opaque/asset?sig=secret",
            artifact,
            initial=False,
        )


def test_redirect_handler_rejects_downgrade_before_request() -> None:
    artifact = _artifact()
    handler = si._ScrcpyRedirectHandler(artifact)
    request = urllib.request.Request(artifact.source_url)
    with pytest.raises(si.ScrcpyIntegrityError, match="HTTPS"):
        handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "http://release-assets.githubusercontent.com/opaque/asset?sig=secret",
        )


def test_download_is_bounded_hashed_and_redacts_signed_query(tmp_path: Path) -> None:
    payload = b"verified archive bytes"
    artifact = _test_artifact_for_bytes(_artifact(), payload)
    signed = "https://release-assets.githubusercontent.com/opaque/asset?sig=do-not-store"

    def open_asset(_: si.ScrcpyArtifact):
        return FakeResponse(payload, signed, content_length=len(payload)), [
            "https://release-assets.githubusercontent.com/opaque/asset"
        ]

    destination = tmp_path / "asset.zip"
    result = si._download_artifact(artifact, destination, open_asset=open_asset)
    assert destination.read_bytes() == payload
    assert result["sha256"] == artifact.sha256
    assert result["canonical_url"].endswith("/opaque/asset")
    assert "sig" not in json.dumps(result)


def test_download_rejects_pinned_size_before_streaming(tmp_path: Path) -> None:
    payload = b"short"
    artifact = _test_artifact_for_bytes(_artifact(), payload)

    def open_asset(_: si.ScrcpyArtifact):
        return FakeResponse(
            payload,
            artifact.source_url,
            content_length=len(payload) + 1,
        ), []

    with pytest.raises(si.ScrcpyIntegrityError, match="size"):
        si._download_artifact(artifact, tmp_path / "asset.zip", open_asset=open_asset)


def test_safe_zip_extracts_only_expected_root(tmp_path: Path) -> None:
    artifact = _artifact()
    payload = _zip_payload(artifact)
    archive = tmp_path / "asset.zip"
    archive.write_bytes(payload)
    destination = tmp_path / "extract"
    si._extract_archive(archive, destination, artifact)
    assert (destination / artifact.package_dir / artifact.executable_name).read_bytes() == CLIENT_BYTES


@pytest.mark.parametrize(
    "unsafe_name",
    [
        "../escape.txt",
        "/absolute.txt",
        r"C:\escape.txt",
        "payload/file.txt:stream",
        "payload/CON.txt",
        "payload/trailing. ",
    ],
)
def test_archive_path_policy_rejects_traversal_ads_and_reserved_names(
    unsafe_name: str,
) -> None:
    with pytest.raises(si.ScrcpyArchiveSafetyError):
        si._safe_member_parts(unsafe_name)


def test_zip_rejects_case_collision(tmp_path: Path) -> None:
    artifact = _artifact()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(f"{artifact.package_dir}/File.dll", b"a")
        archive.writestr(f"{artifact.package_dir}/file.dll", b"b")
    archive_path = tmp_path / "collision.zip"
    archive_path.write_bytes(stream.getvalue())
    with pytest.raises(si.ScrcpyArchiveSafetyError, match="colliding"):
        si._extract_archive(archive_path, tmp_path / "extract", artifact)


def test_zip_rejects_symlink_member(tmp_path: Path) -> None:
    artifact = _artifact()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        link = zipfile.ZipInfo(f"{artifact.package_dir}/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "scrcpy.exe")
    archive_path = tmp_path / "link.zip"
    archive_path.write_bytes(stream.getvalue())
    with pytest.raises(si.ScrcpyArchiveSafetyError, match="links"):
        si._extract_archive(archive_path, tmp_path / "extract", artifact)


def test_tar_gz_extracts_regular_files_and_preserves_executable_bit(tmp_path: Path) -> None:
    artifact = si.detect_scrcpy_artifact("linux", "x86_64")
    archive_path = tmp_path / "asset.tar.gz"
    archive_path.write_bytes(_tar_payload(artifact))
    destination = tmp_path / "extract"
    si._extract_archive(archive_path, destination, artifact)
    executable = destination / artifact.package_dir / "scrcpy"
    assert executable.read_bytes() == CLIENT_BYTES
    if sys.platform != "win32":
        assert executable.stat().st_mode & stat.S_IXUSR


def test_tar_gz_rejects_links(tmp_path: Path) -> None:
    artifact = si.detect_scrcpy_artifact("linux", "x86_64")
    archive_path = tmp_path / "asset.tar.gz"
    archive_path.write_bytes(_tar_payload(artifact, link_name="alias"))
    with pytest.raises(si.ScrcpyArchiveSafetyError, match="links"):
        si._extract_archive(archive_path, tmp_path / "extract", artifact)


def test_full_mock_install_writes_versioned_receipt_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _artifact()
    payload = _zip_payload(original)
    artifact = _test_artifact_for_bytes(original, payload)
    monkeypatch.setitem(si._ARTIFACTS, ("windows", "x86_64"), artifact)
    monkeypatch.setattr(si, "_SERVER_SHA256", hashlib.sha256(SERVER_BYTES).hexdigest())
    opened = 0

    def open_asset(selected: si.ScrcpyArtifact):
        nonlocal opened
        opened += 1
        assert selected is artifact
        return FakeResponse(
            payload,
            artifact.source_url,
            content_length=len(payload),
        ), []

    root = tmp_path / "managed"
    original_path = sys.path.copy()
    result = si.install_scrcpy(
        yes=True,
        platform_name="win32",
        machine="AMD64",
        install_root=root,
        which=lambda _: None,
        open_asset=open_asset,
    )
    assert result["status"] == "installed"
    assert result["binary_executed"] is False
    assert result["path_modified"] is False
    assert opened == 1
    assert sys.path == original_path

    receipt_path = Path(result["receipt_path"])
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["version"] == si.SCRCPY_VERSION
    assert receipt["asset"]["sha256"] == artifact.sha256
    assert receipt["effects"]["binary_executed"] is False
    assert receipt["effects"]["path_modified"] is False
    assert receipt["license"]["scrcpy"] == "Apache-2.0"
    assert "entire release bundle" in receipt["license"]["bundle_note"]
    assert "NOTICE-dependencies.txt" in receipt["license"]["retained_notices"]
    receipt_text = receipt_path.read_text(encoding="utf-8")
    assert str(tmp_path) not in receipt_text
    assert "sig=" not in receipt_text

    again = si.install_scrcpy(
        yes=True,
        platform_name="win32",
        machine="AMD64",
        install_root=root,
        which=lambda _: None,
        open_asset=lambda _: (_ for _ in ()).throw(AssertionError("must not redownload")),
    )
    assert again["status"] == "already_installed"
    assert opened == 1

    notice = receipt_path.parent / "NOTICE-dependencies.txt"
    notice.write_bytes(b"same-size-tampered-dependency-notice!!\n")
    tampered = si.plan_scrcpy(
        platform_name="win32",
        machine="AMD64",
        install_root=root,
        which=lambda _: None,
    )
    assert tampered["status"] == "conflict"


def test_payload_requires_client_server_and_license(tmp_path: Path) -> None:
    artifact = _artifact()
    package = tmp_path / artifact.package_dir
    package.mkdir()
    (package / artifact.executable_name).write_bytes(CLIENT_BYTES)
    (package / "scrcpy-server").write_bytes(SERVER_BYTES)
    with pytest.raises(si.ScrcpyIntegrityError, match="LICENSE"):
        si._validate_payload(package, artifact)


def test_existing_binary_reuse_does_not_require_a_managed_host_asset():
    result = si.install_scrcpy(platform_name="linux", machine="aarch64", which=lambda _: "/usr/bin/scrcpy")
    assert result["status"] == "reuse_existing"
    assert result["trusted"] is False
    assert result["writes"] is False and result["downloads"] is False
