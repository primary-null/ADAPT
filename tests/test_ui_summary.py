"""Unit tests for adapt_adb.render.ui_summary."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from adapt_adb.render.ui_summary import UiDumpUnavailableError, capture_ui_summary, parse_ui_summary

SAMPLE_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<hierarchy rotation="0">'
    '<node index="0" text="Sign In" resource-id="com.example:id/sign_in" '
    'class="android.widget.Button" content-desc="Sign in button" '
    'clickable="true" visible-to-user="true" bounds="[100,200][300,260]" />'
    "</hierarchy>"
)


def _proc(argv: list[str], stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=argv, returncode=returncode, stdout=stdout, stderr=stderr)


def test_primary_dev_tty_success_does_not_rm() -> None:
    calls: list[list[str]] = []

    def fake_runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        return _proc(argv, stdout=f"UI hierchary dumped to: /dev/tty\n{SAMPLE_XML}")

    result = capture_ui_summary(Path("adb"), "SERIAL_A", runner=fake_runner)

    assert calls == [
        ["adb", "-s", "SERIAL_A", "exec-out", "uiautomator", "dump", "/dev/tty"],
    ]
    assert "<hierarchy" not in str(result)
    assert len(result["nodes"]) == 1
    assert result["nodes"][0]["text"] == "Sign In"
    assert result["nodes"][0]["center"] == [200, 230]


def test_fallback_dump_then_rm_on_success() -> None:
    calls: list[list[str]] = []

    def fake_runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        if argv[-1] == "/dev/tty":
            return _proc(argv, stdout="UI hierchary dumped to: /dev/tty\n")
        if argv[-2] == "dump" and argv[-1].startswith("/sdcard/adapt-ui-"):
            return _proc(argv, stdout=f"UI hierarchy dumped to: {argv[-1]}\n")
        if argv[-2] == "cat" and argv[-1].startswith("/sdcard/adapt-ui-"):
            return _proc(argv, stdout=SAMPLE_XML)
        if argv[-3:-1] == ["rm", "-f"] and argv[-1].startswith("/sdcard/adapt-ui-"):
            return _proc(argv, stdout="")
        raise AssertionError(f"Unexpected command: {argv}")

    result = capture_ui_summary(Path("adb"), "SERIAL_A", runner=fake_runner)

    assert calls[0] == ["adb", "-s", "SERIAL_A", "exec-out", "uiautomator", "dump", "/dev/tty"]
    assert calls[1][-1] == calls[2][-1] == calls[3][-1]
    assert calls[1][-1].startswith("/sdcard/adapt-ui-")
    assert len(result["nodes"]) == 1
    assert result["nodes"][0]["resource_id"] == "com.example:id/sign_in"


def test_fallback_failure_still_runs_rm_and_ignores_rm_error() -> None:
    calls: list[list[str]] = []

    def fake_runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        if argv[-1] == "/dev/tty":
            return _proc(argv, stdout="", stderr="ERROR: null root node")
        if argv[-2] == "dump" and argv[-1].startswith("/sdcard/adapt-ui-"):
            return _proc(argv, stdout=f"UI hierarchy dumped to: {argv[-1]}\n")
        if argv[-2] == "cat" and argv[-1].startswith("/sdcard/adapt-ui-"):
            return _proc(argv, stdout="", stderr="cat failed", returncode=1)
        if argv[-3:-1] == ["rm", "-f"] and argv[-1].startswith("/sdcard/adapt-ui-"):
            raise OSError("rm transport failure")
        raise AssertionError(f"Unexpected command: {argv}")

    with pytest.raises(RuntimeError, match="UI dump read failed"):
        capture_ui_summary(Path("adb"), "SERIAL_A", runner=fake_runner)

    assert calls[0] == ["adb", "-s", "SERIAL_A", "exec-out", "uiautomator", "dump", "/dev/tty"]
    assert calls[1][-1] == calls[2][-1] == calls[3][-1]
    assert calls[1][-1].startswith("/sdcard/adapt-ui-")


def test_parse_ui_summary_caps_at_max_80_nodes() -> None:
    nodes_xml = "".join(
        f'<node index="{i}" text="Item {i}" resource-id="com.example:id/item_{i}" '
        f'class="android.widget.TextView" clickable="true" visible-to-user="true" '
        f'bounds="[0,{i * 10}][100,{(i + 1) * 10}]" />'
        for i in range(100)
    )
    xml_text = f"<hierarchy>{nodes_xml}</hierarchy>"
    summary = parse_ui_summary(xml_text)
    assert len(summary["nodes"]) == 80


def test_device_file_fallback_refused_when_not_allowed() -> None:
    """MCP-01: without permission the failed direct dump never writes, reads or removes a device file."""
    calls: list[list[str]] = []

    def fake_runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        return _proc(argv, stdout="", stderr="ERROR: null root node", returncode=1)

    with pytest.raises(UiDumpUnavailableError):
        capture_ui_summary(Path("adb"), "SERIAL_A", runner=fake_runner, allow_device_file=False)

    assert calls == [["adb", "-s", "SERIAL_A", "exec-out", "uiautomator", "dump", "/dev/tty"]]


def test_direct_dump_still_works_when_device_file_not_allowed() -> None:
    def fake_runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return _proc(argv, stdout=SAMPLE_XML)

    result = capture_ui_summary(Path("adb"), "SERIAL_A", runner=fake_runner, allow_device_file=False)
    assert result["nodes"][0]["text"] == "Sign In"


def test_every_runner_call_decodes_utf8_and_non_ascii_survives() -> None:
    """UX-01: device XML is decoded as UTF-8 (not the host locale codec), invalid bytes replaced."""
    label = "Paramètres – 设置 – Ünïcødé"
    xml_bytes = SAMPLE_XML.replace("Sign In", label).encode("utf-8")
    seen_kwargs: list[dict[str, Any]] = []

    def fake_runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen_kwargs.append(dict(kwargs))
        # Decode exactly as subprocess would with the requested codec.
        decode = lambda raw: raw.decode(kwargs["encoding"], kwargs["errors"])  # noqa: E731
        if argv[-1] == "/dev/tty":
            return _proc(argv, stdout=decode(b"\xff"))  # undecodable byte, no hierarchy
        if argv[-2] == "cat":
            return _proc(argv, stdout=decode(xml_bytes))
        return _proc(argv, stdout="")

    result = capture_ui_summary(Path("adb"), "SERIAL_A", runner=fake_runner)

    assert result["nodes"][0]["text"] == label
    assert len(seen_kwargs) == 4  # direct dump, fallback dump, cat, rm
    for kwargs in seen_kwargs:
        assert kwargs["text"] is True
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"


@pytest.mark.parametrize(
    "xml_text",
    [
        "</hierarchy><hierarchy rotation=\"0\"><node /></hierarchy-broken>",
        "noise </hierarchy> more noise <hierarchy rotation=\"0\">",
        "<hierarchy rotation=\"0\"><node text=\"x\" clickable=\"true\" bounds=\"[0,0][10,10]\" />",
        "<?xml version='1.0'?></hierarchy>",
    ],
)
def test_reversed_or_unterminated_hierarchy_raises(xml_text: str) -> None:
    """UX-07: never silently return an empty summary for a broken hierarchy."""
    with pytest.raises(ValueError):
        parse_ui_summary(xml_text)
