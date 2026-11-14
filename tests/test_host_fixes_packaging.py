"""Host fixes: the udev rules file ships as package data, one resolver finds it, CI tests 3.12."""

from __future__ import annotations

import tomllib
from pathlib import Path

import adapt_adb.hints as hints

ROOT = Path(__file__).resolve().parents[1]
PACKAGING_RULES = ROOT / "packaging" / "linux" / "51-android.rules"
PACKAGE_DATA_RULES = ROOT / "src" / "adapt_adb" / "data" / "51-android.rules"


def test_package_data_rules_are_byte_identical_to_packaging_rules():
    assert PACKAGE_DATA_RULES.read_bytes() == PACKAGING_RULES.read_bytes()


def test_package_data_directory_holds_only_the_rules_file():
    names = sorted(p.name for p in PACKAGE_DATA_RULES.parent.iterdir() if p.name != "__pycache__")
    assert names == ["51-android.rules"]


def test_pyproject_declares_the_rules_as_package_data():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "data/51-android.rules" in config["tool"]["setuptools"]["package-data"]["adapt_adb"]


def test_resolver_prefers_package_data_and_falls_back_to_the_checkout(tmp_path, monkeypatch):
    assert hints.find_udev_rules_file() == PACKAGE_DATA_RULES.resolve()
    assert hints.udev_rules_path() == PACKAGE_DATA_RULES.resolve()
    monkeypatch.setattr(hints, "_PACKAGE_RULES", tmp_path / "missing.rules")
    assert hints.find_udev_rules_file() == PACKAGING_RULES.resolve()
    monkeypatch.setattr(hints, "_CHECKOUT_RULES", tmp_path / "also-missing.rules")
    assert hints.find_udev_rules_file() is None


def test_rules_file_comment_points_at_no_checkout_only_path():
    text = PACKAGE_DATA_RULES.read_text(encoding="utf-8")
    assert "< packaging/linux/51-android.rules" not in text
    assert "Log out and back in" in text


def test_doctor_uses_the_same_resolver():
    import adapt_adb.doctor as doctor_mod

    assert doctor_mod.find_udev_rules_file is hints.find_udev_rules_file
    assert str(hints.find_udev_rules_file()) in (doctor_mod.udev_install_command() or "")


def test_release_matrix_tests_every_declared_python():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = sorted(c.rsplit(" :: ", 1)[1] for c in config["project"]["classifiers"]
                      if c.startswith("Programming Language :: Python :: 3."))
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    matrix_line = next(line for line in workflow.splitlines() if line.strip().startswith("python: ["))
    for version in declared:
        assert f"'{version}'" in matrix_line, version
