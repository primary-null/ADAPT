"""Activity frames are bounded human output, not protocol data or fake success."""

import io
import threading
from types import SimpleNamespace

import pytest

from adapt_adb import progress


class Terminal(io.StringIO):
    def __init__(self):
        super().__init__()
        self.frames = threading.Event()
        self.frame_count = 0

    def isatty(self):
        return True

    def write(self, text):
        result = super().write(text)
        if "adapting" in text:
            self.frame_count += 1
            if self.frame_count >= 4:
                self.frames.set()
        return result


@pytest.fixture
def terminal(monkeypatch):
    out, err = Terminal(), Terminal()
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.delenv("ADAPT_NO_PROGRESS", raising=False)
    return out, err


def test_indicator_repeats_and_cleans_up_on_failure(terminal, monkeypatch):
    out, err = terminal
    monkeypatch.setattr(progress.sys, "stdout", out)
    monkeypatch.setattr(progress.sys, "stderr", err)
    with pytest.raises(RuntimeError):
        with progress.adapting("Inspecting", delay=0, interval=.01) as activity:
            assert err.frames.wait(1)
            raise RuntimeError("operation failed")
    assert not activity.thread.is_alive()
    assert all(frame in err.getvalue() for frame in ("adapting.", "adapting..", "adapting..."))
    assert err.getvalue().endswith("\r")
    assert "DONE" not in err.getvalue() and out.getvalue() == ""


def test_short_work_is_silent_and_nested_work_uses_one_indicator(terminal, monkeypatch):
    out, err = terminal
    monkeypatch.setattr(progress.sys, "stdout", out)
    monkeypatch.setattr(progress.sys, "stderr", err)
    with progress.adapting("quick", delay=2):
        with progress.adapting("nested", delay=0) as nested:
            assert nested.thread is None
    assert err.getvalue() == ""


@pytest.mark.parametrize("mode", ["json", "mcp", "flag", "environment", "redirected", "dumb"])
def test_machine_and_disabled_modes_never_start_animation(mode, terminal, monkeypatch):
    monkeypatch.setattr(progress.sys, "stdout", terminal[0])
    monkeypatch.setattr(progress.sys, "stderr", terminal[1])
    ctx = SimpleNamespace(params={"no_progress": mode == "flag"}, meta={"adapt_machine_output": mode == "json"}, invoked_subcommand="mcp" if mode == "mcp" else "doctor")
    token = progress._CONTEXT.set(ctx)
    try:
        if mode == "environment":
            monkeypatch.setenv("ADAPT_NO_PROGRESS", "1")
        if mode == "redirected":
            monkeypatch.setattr(progress.sys, "stdout", io.StringIO())
        if mode == "dumb":
            monkeypatch.setenv("TERM", "dumb")
        with progress.adapting(delay=0) as activity:
            assert activity.thread is None
        assert terminal[1].getvalue() == ""
    finally:
        progress._CONTEXT.reset(token)
