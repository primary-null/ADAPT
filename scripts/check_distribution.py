"""Validate release archives without installing or contacting a device."""
from __future__ import annotations

import argparse
import ast
import configparser
import email
import hashlib
from pathlib import Path, PurePosixPath
import tarfile
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]
# The only files allowed under adapt_adb/data/ in the wheel; each is required.
WHEEL_DATA_FILES = (
    "adapt_adb/data/51-android.rules",
    "adapt_adb/data/dev-options-catalog.json",
    "adapt_adb/data/skill/adapt/SKILL.md",
    "adapt_adb/data/skill/adapt/references/failures.md",
)


def version() -> str:
    value = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    if source_version((ROOT / "src/adapt_adb/__init__.py").read_text(encoding="utf-8")) != value:
        raise ValueError("CLI source version differs from package metadata")
    return value


def source_version(text: str) -> str | None:
    for node in ast.parse(text).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets):
            return ast.literal_eval(node.value)
    return None


def safe_name(name: str) -> None:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name:
        raise ValueError(f"Unsafe archive member: {name}")
    if any(part in {".git", ".local", ".venv", "__pycache__"} for part in path.parts):
        raise ValueError(f"Private/generated archive member: {name}")
    if path.suffix in {".p12", ".jks", ".keystore", ".pem", ".key", ".pyc"}:
        raise ValueError(f"Unexpected sensitive/generated file: {name}")


def check_dist(folder: Path, tag: str | None = None) -> list[dict]:
    current = version()
    if tag is not None and tag != "v" + current:
        raise ValueError(f"Tag must be v{current}")
    wheel = folder / f"adapt_adb-{current}-py3-none-any.whl"
    source = folder / f"adapt_adb-{current}.tar.gz"
    packages = sorted([*folder.glob("*.whl"), *folder.glob("*.tar.gz")])
    if set(packages) != {wheel, source}:
        raise ValueError("Expected exactly one version-matched wheel and source archive")
    with zipfile.ZipFile(wheel) as archive:
        if archive.testzip() is not None:
            raise ValueError("Wheel CRC check failed")
        names = archive.namelist()
        for name in names:
            safe_name(name)
        info = f"adapt_adb-{current}.dist-info"
        metadata = email.message_from_bytes(archive.read(info + "/METADATA"))
        if metadata["Name"] != "adapt-adb" or metadata["Version"] != current:
            raise ValueError("Wheel metadata does not match the project")
        if metadata["Requires-Python"] != ">=3.11":
            raise ValueError("Unexpected Python support declaration")
        if source_version(archive.read("adapt_adb/__init__.py").decode("utf-8")) != current:
            raise ValueError("Installed CLI version differs from package metadata")
        entries = configparser.ConfigParser()
        entries.read_string(archive.read(info + "/entry_points.txt").decode())
        if entries["console_scripts"]["adapt"] != "adapt_adb.cli:app":
            raise ValueError("The installed adapt command is missing or incorrect")
        for name in ["adapt_adb/__main__.py", "adapt_adb/resources/dex_bridge/adapt-dex-bridge.jar",
                     "adapt_adb/resources/dex_bridge/build.json", info + "/licenses/LICENSE",
                     info + "/licenses/THIRD-PARTY-NOTICES.md", *WHEEL_DATA_FILES]:
            if name not in names:
                raise ValueError(f"Required wheel content missing: {name}")
        stray = [n for n in names if n.startswith("adapt_adb/data/") and not n.endswith("/") and n not in WHEEL_DATA_FILES]
        if stray:
            raise ValueError(f"Unexpected package data file: {stray[0]}")
    with tarfile.open(source) as archive:
        for member in archive.getmembers():
            safe_name(member.name)
            if not (member.isfile() or member.isdir()):
                raise ValueError("Source archives must not contain links or special files")
        if f"adapt_adb-{current}/pyproject.toml" not in archive.getnames():
            raise ValueError("Source archive lacks the build definition")
    result = []
    for package in [wheel, source]:
        result.append({"file": package.name, "bytes": package.stat().st_size,
                       "sha256": hashlib.sha256(package.read_bytes()).hexdigest()})
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--tag")
    args = parser.parse_args()
    records = check_dist(args.folder, args.tag)
    (args.folder / "SHA256SUMS.txt").write_text(
        "".join(f"{item['sha256']}  {item['file']}\n" for item in records), encoding="ascii")
    for record in records:
        print(f"Verified {record['file']} ({record['bytes']} bytes)")


if __name__ == "__main__":
    main()
