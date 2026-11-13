"""Unit tests for adapt_adb.hints."""

from __future__ import annotations

from pathlib import Path

from adapt_adb.hints import (
    debian_udev_hints,
    udev_rules_path,
    udev_rules_text,
    windows_driver_hints,
)


def test_udev_rules_path_points_to_packaged_rules() -> None:
    path = udev_rules_path()
    assert isinstance(path, Path)
    assert path.name == "51-android.rules"
    assert path.is_file()

    content = path.read_text(encoding="utf-8")
    assert 'SUBSYSTEM=="usb"' in content
    assert 'ATTR{bInterfaceClass}=="ff"' in content
    assert 'ATTR{bInterfaceSubClass}=="42"' in content
    assert 'ATTR{bInterfaceProtocol}=="01"' in content
    assert 'TAG+="uaccess"' in content
    assert 'GROUP="plugdev"' in content
    assert udev_rules_text() == content


def test_windows_driver_hints_loads_packaging_readme() -> None:
    hints = windows_driver_hints()
    assert isinstance(hints, str)
    assert len(hints) > 100
    assert "Google USB Driver" in hints
    assert "android_winusb.inf" in hints
    assert "Samsung" in hints
    assert "Zadig" in hints
    assert "testsigning" in hints
    assert "kill-server" in hints


def test_debian_udev_hints_loads_packaging_readme() -> None:
    hints = debian_udev_hints()
    assert isinstance(hints, str)
    assert len(hints) > 100
    assert "51-android.rules" in hints
    assert "/etc/udev/rules.d/51-android.rules" in hints
    assert "plugdev" in hints
    assert "uaccess" in hints
    assert "sudo adb" in hints
    assert "kill-server" in hints
