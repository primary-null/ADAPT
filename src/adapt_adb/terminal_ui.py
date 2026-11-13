"""ADAPT's interactive home and command browser; existing commands remain authoritative."""
from __future__ import annotations

import math
import os
import shlex
import shutil
import subprocess
import sys
import textwrap
from typing import Any, TextIO

from adapt_adb import banner
from adapt_adb.console import _safe
from adapt_adb.help_system import command_inventory, tips_enabled

AREAS = (
    ("Host, drivers & setup", ("doctor", "setup")),
    ("Devices, wireless & Tailscale", ("devices", "pair", "connect", "adb", "detect", "tailscale")),
    ("Apps & developer testing", ("app", "debug", "dev-options", "android")),
    ("Displays & Samsung DeX", ("dex", "desktop")),
    ("Tools, ROM workflows & Give Me Everything", ("tools", "guide", "everything")),
    ("AI / MCP & SSH workers", ("mcp", "ssh")),
    ("Rapid ADAPT", ("rapid",)),
    ("Logs, help & preferences", ("logs", "help-me", "tips")),
    ("All ADAPT commands", ()),
)


def _enable_vt():
    """Enable VT only on our console output handle and return a restoration hook."""
    if os.name != "nt":
        return True, lambda: None
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel.GetStdHandle.restype = wintypes.HANDLE
    kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    handle = kernel.GetStdHandle(-11 & 0xffffffff)
    mode = wintypes.DWORD()
    if not kernel.GetConsoleMode(handle, ctypes.byref(mode)) or not kernel.SetConsoleMode(handle, mode.value | 4):
        return False, lambda: None
    return True, lambda: kernel.SetConsoleMode(handle, mode.value)


def split_arguments(value: str) -> list[str]:
    """Parse argv without evaluating a shell, preserving Windows path backslashes."""
    if any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value):
        raise ValueError("Arguments cannot contain terminal control characters.")
    if os.name != "nt":
        return shlex.split(value)
    import ctypes
    from ctypes import wintypes
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    count = ctypes.c_int()
    pointer = shell.CommandLineToArgvW("adapt " + value, ctypes.byref(count))
    if not pointer:
        raise ValueError("Arguments could not be parsed. Use double quotes around paths with spaces.")
    try:
        return [pointer[i] for i in range(1, count.value)]
    finally:
        kernel.LocalFree(pointer)


class Screen:
    def __init__(self, stdin: TextIO, stdout: TextIO, stderr: TextIO, *, no_banner: bool = False, no_redraw: bool = False):
        self.stdin, self.stdout, self.stderr = stdin, stdout, stderr
        self.no_banner = no_banner
        self.redraw = not no_redraw and os.environ.get("TERM", "").lower() != "dumb"
        self.restore = lambda: None

    def __enter__(self):
        if self.redraw:
            self.redraw, self.restore = _enable_vt()
        if self.redraw:
            self.stdout.write("\x1b[?1049h")
            self.stdout.flush()
        return self

    def __exit__(self, *exc):
        try:
            if self.redraw:
                self.stdout.write("\x1b[?1049l")
                self.stdout.flush()
        finally:
            self.restore()

    def draw(self, title: str, body: list[str], page: int = 0, *, detail: bool = False) -> tuple[int, int]:
        width, height = shutil.get_terminal_size(fallback=(100, 32))
        width, height = max(12, width), max(12, height)
        def wrap(value: str) -> list[str]:
            return [part for line in value.split("\n") for part in
                    (textwrap.wrap(_safe(line), width=width, replace_whitespace=False) or [""])]
        art = ""
        if not self.no_banner and not banner._env_disabled() and os.environ.get("TERM", "").lower() != "dumb":
            art = banner.render_banner(width=width if height >= 23 else min(width, 24),
                color=self.stderr.isatty(), encoding=getattr(self.stderr, "encoding", None) or "utf-8")
        title_lines = wrap("ADAPT / " + title)
        nav = "[H] Home   [B] Back   [N/P] Page   [Q] Exit" + ("   [R] Run" if detail else "")
        nav_lines = wrap(nav)
        header_height = len(art.splitlines()) + len(title_lines) + 1
        capacity = max(1, height - header_height - len(nav_lines) - 4)
        lines = [part for line in body for part in wrap(line)]
        pages = max(1, math.ceil(len(lines) / capacity))
        page = max(0, min(page, pages - 1))
        if self.redraw:
            # Clear only this viewport, never the saved terminal scrollback.
            self.stdout.write("\x1b[H\x1b[2J")
            self.stdout.flush()
        self.stderr.write(art)
        self.stderr.flush()
        self.stdout.write("\n".join([*title_lines, "", *lines[page * capacity:(page + 1) * capacity],
            "", f"Page {page + 1}/{pages}", *nav_lines]) + "\n")
        self.stdout.flush()
        return page, pages

    def read(self, prompt: str = "ADAPT > ") -> str | None:
        self.stdout.write(prompt)
        self.stdout.flush()
        value = self.stdin.readline()
        return value.rstrip("\r\n") if value else None


def run_console(root: Any, *, no_banner: bool = False, no_redraw: bool = False,
                no_tips: bool = False, no_progress: bool = False,
                stdin: TextIO | None = None, stdout: TextIO | None = None, stderr: TextIO | None = None) -> None:
    stdin, stdout, stderr = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr
    if not all(getattr(stream, "isatty", lambda: False)() for stream in (stdin, stdout, stderr)):
        raise ValueError("ADAPT's interactive console needs a terminal. Use adapt --help or adapt help-me --all --json for redirected output.")
    records = command_inventory(root)
    by_path = {record["command"]: record for record in records}
    # Stack entries contain (screen kind, selected path/area, page).
    stack: list[tuple[str, Any, int]] = [("home", None, 0)]
    message = ""
    with Screen(stdin, stdout, stderr, no_banner=no_banner, no_redraw=no_redraw) as screen:
        try:
            while True:
                kind, selected, page = stack[-1]
                entries: list[Any] = []
                if kind == "home":
                    title = "MAIN MENU"
                    body = ["Choose an area. Supply a device explicitly when running its command."]
                    entries = list(AREAS)
                    body += [f"[{i}] {entry[0]}" for i, entry in enumerate(entries, 1)]
                elif kind == "area":
                    title, names = AREAS[selected]
                    entries = [r for r in records if r["command"].count(" ") == 1 and r["command"] != "adapt console"
                               and (not names or r["command"].split(" ")[1] in names)]
                    body = [f"[{i}] {r['command']}  /  {r['description']}" for i, r in enumerate(entries, 1)]
                elif kind == "group":
                    record = by_path[selected]
                    title = selected.removeprefix("adapt ").upper()
                    entries = [r for r in records if r["command"].rsplit(" ", 1)[0] == selected]
                    body = [record["description"], ""] + [f"[{i}] {r['command']}  /  {r['description']}" for i, r in enumerate(entries, 1)]
                else:
                    record = by_path[selected]
                    title = selected.removeprefix("adapt ").upper()
                    body = [record["description"], "", record["usage"], ""]
                    body += [f"{flags}  {description}" for flags, description in record["parameters"]]
                    if not no_tips and tips_enabled():
                        body += ["", "HOW TO"] + [line for step in record["how_to"] for line in step.splitlines()]
                    if selected == "adapt mcp":
                        body += ["", "Start MCP through your AI client's stdio configuration, outside this menu."]
                if message:
                    body = [message, "", *body]
                    message = ""
                runnable = kind == "detail" and selected not in {"adapt console", "adapt mcp"}
                page, pages = screen.draw(title, body, page, detail=runnable)
                stack[-1] = (kind, selected, page)
                choice = screen.read()
                if choice is None or choice.strip().lower() in {"q", "quit", "exit"}:
                    return
                choice = choice.strip().lower()
                if choice in {"h", "home", "0"}:
                    stack = [("home", None, 0)]
                elif choice in {"b", "back"}:
                    if len(stack) > 1:
                        stack.pop()
                elif choice in {"n", "p"}:
                    stack[-1] = (kind, selected, min(page + 1, pages - 1) if choice == "n" else max(page - 1, 0))
                elif choice == "r" and runnable:
                    screen.draw("RUN / " + selected, ["Enter arguments for this command, or H to return home.",
                        "Use double quotes around paths with spaces. Existing prompts and confirmations remain interactive."])
                    raw = screen.read("Arguments > ")
                    if raw is None:
                        return
                    if raw.strip().lower() in {"h", "home"}:
                        stack = [("home", None, 0)]
                        continue
                    try:
                        args = split_arguments(raw)
                    except ValueError as exc:
                        message = str(exc)
                        continue
                    prefix = [sys.executable, "-m", "adapt_adb", "--no-banner"]
                    if no_tips:
                        prefix.append("--no-tips")
                    if no_progress:
                        prefix.append("--no-progress")
                    screen.draw("RUN / " + selected, ["The selected command is running. Its normal prompts follow.",
                        "Return to the menu after it completes."])
                    if screen.redraw:
                        # Leave the alternate screen so the command's output stays in the terminal scrollback.
                        stdout.write("\x1b[?1049l")
                        stdout.flush()
                    try:
                        try:
                            result = subprocess.run([*prefix, *selected.split()[1:], *args], check=False)
                            status = f"Command finished with exit code {result.returncode}."
                        except KeyboardInterrupt:
                            status = "Command interrupted."
                        except OSError:
                            status = "Could not start the selected ADAPT command."
                        answer = screen.read(status + " Enter: Back | H: Home | Q: Exit > ")
                    finally:
                        if screen.redraw:
                            stdout.write("\x1b[?1049h")
                            stdout.flush()
                    if answer is None or answer.strip().lower() in {"q", "exit", "quit"}:
                        return
                    if answer.strip().lower() in {"h", "home"}:
                        stack = [("home", None, 0)]
                elif choice.isdecimal() and 1 <= int(choice) <= len(entries):
                    entry = entries[int(choice) - 1]
                    if kind == "home":
                        stack.append(("area", int(choice) - 1, 0))
                    else:
                        path = entry["command"]
                        has_children = any(r["command"].rsplit(" ", 1)[0] == path for r in records)
                        stack.append(("group" if has_children else "detail", path, 0))
                else:
                    message = "Choose a displayed number, H for Home, B for Back, N/P for pages, or Q to exit."
        except KeyboardInterrupt:
            return
