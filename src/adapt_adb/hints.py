"""User-facing Windows USB driver and Debian/Linux udev hint loaders for ADAPT.

Loads packaged guidance and udev rule paths without modifying host system state
or installing any files. ``find_udev_rules_file`` is the single resolver for the
`51-android.rules` file: the CLI, the doctor and these helpers all use it.
"""

from __future__ import annotations

import importlib.resources
from pathlib import Path

__all__ = [
    "UDEV_RULES_NAME",
    "debian_readme_path",
    "debian_udev_hints",
    "find_udev_rules_file",
    "packaging_dir",
    "udev_rules_path",
    "udev_rules_text",
    "windows_driver_hints",
    "windows_readme_path",
]


UDEV_RULES_NAME = "51-android.rules"

# Shipped in the wheel as package data; a byte-identical copy of
# packaging/linux/51-android.rules (a test keeps the two in step).
_PACKAGE_RULES = Path(__file__).resolve().parent / "data" / UDEV_RULES_NAME
_CHECKOUT_RULES = Path(__file__).resolve().parents[2] / "packaging" / "linux" / UDEV_RULES_NAME


def find_udev_rules_file() -> Path | None:
    """Return the absolute path of an existing `51-android.rules`, or None.

    The package-data copy comes first because it exists in every installed copy;
    the source checkout's `packaging/linux` copy is the fallback.
    """
    for candidate in (_PACKAGE_RULES, _CHECKOUT_RULES):
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def packaging_dir() -> Path:
    """Locate the `packaging/` directory via importlib.resources or repo-relative pathlib."""
    try:
        pkg_root = importlib.resources.files("adapt_adb")
        candidate = Path(str(pkg_root)).joinpath("packaging")
        if candidate.is_dir():
            return candidate.resolve()
    except Exception:
        pass

    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "packaging"
        if (
            candidate.is_dir()
            and (candidate / "linux" / "51-android.rules").is_file()
        ):
            return candidate

    # Fallback to standard repo layout: <repo>/src/adapt_adb/hints.py -> <repo>/packaging
    return here.parents[2] / "packaging"


def udev_rules_path() -> Path:
    """Return the path to the packaged `51-android.rules` file without installing it."""
    return find_udev_rules_file() or _PACKAGE_RULES


def udev_rules_text() -> str:
    """Read and return the contents of the packaged `51-android.rules` file."""
    return udev_rules_path().read_text(encoding="utf-8")


def debian_readme_path() -> Path:
    """Return the path to `packaging/linux/README.md`."""
    return packaging_dir() / "linux" / "README.md"


def windows_readme_path() -> Path:
    """Return the path to `packaging/windows/README.md`."""
    return packaging_dir() / "windows" / "README.md"


def windows_driver_hints() -> str:
    """Load and return user-facing Windows 11 USB driver and setup hints."""
    return windows_readme_path().read_text(encoding="utf-8")


def debian_udev_hints() -> str:
    """Load and return user-facing Debian/Linux udev and platform-tools setup hints."""
    return debian_readme_path().read_text(encoding="utf-8")
