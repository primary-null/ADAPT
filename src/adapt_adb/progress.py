"""Delayed human-only progress; protocol and redirected streams stay silent."""

from __future__ import annotations

import os
import re
import sys
import threading
from contextvars import ContextVar
from functools import wraps
from typing import Any

_CONTEXT: ContextVar[Any] = ContextVar("adapt_progress_context", default=None)
_NESTED: ContextVar[bool] = ContextVar("adapt_progress_nested", default=False)
_DISPLAY_LOCK = threading.Lock()


def set_progress_context(ctx: Any) -> None:
    token = _CONTEXT.set(ctx.find_root())
    ctx.call_on_close(lambda: _CONTEXT.reset(token))


def progress_enabled() -> bool:
    ctx = _CONTEXT.get()
    if ctx is not None and (ctx.params.get("no_progress") or ctx.meta.get("adapt_machine_output") or ctx.invoked_subcommand == "mcp"):
        return False
    if os.environ.get("ADAPT_NO_PROGRESS", "").strip().lower() in {"1", "true", "on", "yes"}:
        return False
    if os.environ.get("TERM", "").strip().lower() == "dumb":
        return False
    return all(getattr(s, "isatty", lambda: False)() for s in (sys.stdout, sys.stderr))


class adapting:
    """Repeat adapting., adapting.., adapting... until this operation exits.

    Short work never displays a frame. Exit clears the line on success, failure,
    or cancellation, leaving the actual operation to report its result. This is
    an activity indicator, not a percentage or completion claim.
    """

    def __init__(self, label: str = "", *, enabled: bool = True, delay: float = .4, interval: float = .35):
        self.label = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", label)[:64]
        self.enabled = enabled
        self.delay = max(0, delay)
        self.interval = max(.01, interval)
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.token: Any = None

    def __enter__(self):
        if not self.enabled or _NESTED.get() or not progress_enabled() or not _DISPLAY_LOCK.acquire(blocking=False):
            return self
        self.token = _NESTED.set(True)
        stream = sys.stderr

        def animate() -> None:
            width = 0
            try:
                if self.stop.wait(self.delay):
                    return
                frame = 0
                while not self.stop.is_set():
                    text = "adapting" + ("." * (frame % 3 + 1)).ljust(3) + ("  " + self.label if self.label else "")
                    width = max(width, len(text))
                    stream.write("\r" + text.ljust(width))
                    stream.flush()
                    frame += 1
                    if self.stop.wait(self.interval):
                        break
            except (OSError, ValueError):
                pass  # Losing a terminal must not fail the underlying operation.
            finally:
                if width:
                    try:
                        stream.write("\r" + " " * width + "\r")
                        stream.flush()
                    except (OSError, ValueError):
                        pass

        self.thread = threading.Thread(target=animate, name="adapt-progress", daemon=True)
        try:
            self.thread.start()
        except Exception:
            _NESTED.reset(self.token)
            _DISPLAY_LOCK.release()
            self.thread = None
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.thread is not None:
            self.stop.set()
            self.thread.join(timeout=1)
            try:
                _NESTED.reset(self.token)
            finally:
                _DISPLAY_LOCK.release()
        return False


def operation(label: str):
    """Decorate non-prompting work without altering its result or exceptions."""
    def decorate(fn):
        @wraps(fn)
        def run(*args, **kwargs):
            with adapting(label):
                return fn(*args, **kwargs)
        return run
    return decorate
