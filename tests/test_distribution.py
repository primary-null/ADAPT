"""The release gate must reject corrupt, incomplete or misidentified packages."""
import importlib.util
from pathlib import Path
import io
import tarfile
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("distribution_check", ROOT / "scripts/check_distribution.py")
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


def packages(folder, *, extra=None, name="adapt-adb", missing_bridge=False, cli_version=None, missing_rules=False):
    version = CHECK.version()
    info = f"adapt_adb-{version}.dist-info"
    members = {
        info + "/METADATA": f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\nRequires-Python: >=3.11\n",
        info + "/entry_points.txt": "[console_scripts]\nadapt = adapt_adb.cli:app\n",
        info + "/licenses/LICENSE": "MIT",
        info + "/licenses/THIRD-PARTY-NOTICES.md": "Notices",
        "adapt_adb/__main__.py": "",
        "adapt_adb/__init__.py": "__version__ = " + repr(cli_version or version),
        "adapt_adb/resources/dex_bridge/build.json": "{}",
    }
    if not missing_bridge:
        members["adapt_adb/resources/dex_bridge/adapt-dex-bridge.jar"] = "synthetic bridge fixture"
    if not missing_rules:
        members["adapt_adb/data/51-android.rules"] = "synthetic rules fixture"
    if extra:
        members[extra] = "synthetic blocked content"
    with zipfile.ZipFile(folder / f"adapt_adb-{version}-py3-none-any.whl", "w") as archive:
        for path, data in members.items():
            archive.writestr(path, data)
    with tarfile.open(folder / f"adapt_adb-{version}.tar.gz", "w:gz") as archive:
        member = tarfile.TarInfo(f"adapt_adb-{version}/pyproject.toml")
        content = b"[project]";member.size = len(content)
        archive.addfile(member, io.BytesIO(content))


def test_correct_packages_have_hashed_records(tmp_path):
    packages(tmp_path)
    result = CHECK.check_dist(tmp_path, "v" + CHECK.version())
    assert len(result) == 2
    assert all(len(row["sha256"]) == 64 and row["bytes"] > 0 for row in result)


def test_mismatched_tag_cannot_publish(tmp_path):
    packages(tmp_path)
    with pytest.raises(ValueError, match="Tag must"):
        CHECK.check_dist(tmp_path, "v999.0.0")


@pytest.mark.parametrize("path", ["../outside.txt", "adapt_adb/.local/config.json", "adapt_adb/signing.p12"])
def test_private_or_unsafe_files_cannot_ship(tmp_path, path):
    packages(tmp_path, extra=path)
    with pytest.raises(ValueError):
        CHECK.check_dist(tmp_path)


def test_missing_bundled_bridge_is_not_a_complete_cli_package(tmp_path):
    packages(tmp_path, missing_bridge=True)
    with pytest.raises(ValueError, match="Required wheel content missing"):
        CHECK.check_dist(tmp_path)


def test_missing_udev_rules_is_not_a_complete_cli_package(tmp_path):
    packages(tmp_path, missing_rules=True)
    with pytest.raises(ValueError, match="Required wheel content missing: adapt_adb/data/51-android.rules"):
        CHECK.check_dist(tmp_path)


def test_stray_package_data_cannot_ship(tmp_path):
    packages(tmp_path, extra="adapt_adb/data/notes.txt")
    with pytest.raises(ValueError, match="Unexpected package data file"):
        CHECK.check_dist(tmp_path)


def test_wrong_package_identity_cannot_ship(tmp_path):
    packages(tmp_path, name="another-tool")
    with pytest.raises(ValueError, match="metadata"):
        CHECK.check_dist(tmp_path)


def test_wrong_installed_cli_version_cannot_ship(tmp_path):
    packages(tmp_path, cli_version="999.0.0")
    with pytest.raises(ValueError, match="Installed CLI version"):
        CHECK.check_dist(tmp_path)
