"""Startup identity for human ADAPT CLI sessions.

The banner is deliberately written to stderr so stdout stays safe for command
output and MCP JSON-RPC. It is disabled for non-interactive and machine-facing
sessions, and it degrades to compact, ASCII-only output when the terminal does.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Sequence

import typer

_RESET = "\x1b[0m"
_LETTER_COLORS = (
    "\x1b[38;5;46m",  # green
    "\x1b[38;5;120m",  # pale green
    "\x1b[97m",  # white
    "\x1b[37m",  # light gray
    "\x1b[90m",  # gray
)

# Five 5x5 letter masks. The occupied cells are 85 / 125 (68%), close to the
# requested 69% visual density while leaving each letter easy to identify.
_LOGO_MASK: tuple[tuple[str, ...], ...] = (
    (" ### ", "#### ", " ### ", "#### ", "#####"),
    ("## ##", "## ##", "## ##", "## ##", "  #  "),
    ("#####", "## ##", "#####", "#### ", "  #  "),
    ("## ##", "## ##", "## ##", "##   ", "  #  "),
    ("## ##", "#### ", "## ##", "##   ", "  #  "),
)
_SHADES = ("█", "▓", "█", "▓", "▒")
_SUBTITLE = "ANDROID DEVICE AGENT PROVISIONING TOOLKIT"
_SHORT_SUBTITLE = "DEVICE AGENT TOOLKIT"

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


def _env_disabled() -> bool:
    raw = os.environ.get("ADAPT_NO_BANNER", "").strip().lower()
    if raw in _TRUTHY:
        return True
    banner = os.environ.get("ADAPT_BANNER", "").strip().lower()
    return banner in _FALSY


def _machine_facing(ctx: typer.Context) -> bool:
    if ctx.invoked_subcommand == "mcp":
        return True
    return any(arg == "--json" or arg.startswith("--json=") for arg in sys.argv[1:])


def should_show_banner(ctx: typer.Context, *, no_banner: bool) -> bool:
    """Return whether this invocation is an interactive, human-facing session."""

    if no_banner or _env_disabled() or _machine_facing(ctx):
        return False
    if os.environ.get("TERM", "").strip().lower() == "dumb":
        return False
    stderr = getattr(sys, "stderr", None)
    if stderr is None or not hasattr(stderr, "isatty") or not stderr.isatty():
        return False
    return True


def _can_encode(text: str, encoding: str | None) -> bool:
    try:
        text.encode(encoding or "utf-8")
    except (LookupError, UnicodeEncodeError):
        return False
    return True


def _color_enabled(requested: bool | None) -> bool:
    if "NO_COLOR" in os.environ:
        return False
    if os.environ.get("TERM", "").strip().lower() == "dumb":
        return False
    if requested is not None:
        return requested
    stderr = getattr(sys, "stderr", None)
    return bool(stderr is not None and hasattr(stderr, "isatty") and stderr.isatty())


def _terminal_width(width: int | None) -> int:
    if width is not None:
        return max(1, width)
    return max(1, shutil.get_terminal_size(fallback=(80, 24)).columns)


def _paint(parts: Sequence[str], colors: Sequence[str], enabled: bool) -> str:
    if not enabled:
        return "".join(parts)
    return "".join(f"{color}{part}{_RESET}" for part, color in zip(parts, colors))


def _render_logo(*, color: bool, use_unicode: bool) -> list[str]:
    """Render the mask with two literal spaces between colored letters."""

    lines: list[str] = []
    for row in _LOGO_MASK:
        parts = [
            segment.replace("#", _SHADES[index] if use_unicode else "#")
            for index, segment in enumerate(row)
        ]
        if color:
            lines.append(
                "  ".join(
                    f"{_LETTER_COLORS[index]}{part}{_RESET}"
                    for index, part in enumerate(parts)
                )
            )
        else:
            lines.append("  ".join(parts))
    return lines


def _compact_title(*, color: bool) -> str:
    return _paint(tuple("ADAPT"), _LETTER_COLORS, color)


def render_banner(
    width: int | None = None,
    color: bool | None = None,
    encoding: str | None = None,
) -> str:
    """Render a width-aware banner; the no-argument API remains supported.

    ``color`` and ``encoding`` are injectable to make rendering deterministic in
    tests and previews. ``NO_COLOR`` always wins over an explicit color request.
    """

    columns = _terminal_width(width)
    colored = _color_enabled(color)
    stream_encoding = encoding or getattr(getattr(sys, "stderr", None), "encoding", None)
    use_unicode = _can_encode("".join(_SHADES), stream_encoding)

    if columns >= 41:
        logo = _render_logo(color=colored, use_unicode=use_unicode)
        left_pad = " " * max(0, (columns - 33) // 2)
        subtitle_pad = " " * max(0, (columns - len(_SUBTITLE)) // 2)
        return "\n".join(
            [*(left_pad + line for line in logo), "", subtitle_pad + _SUBTITLE]
        ) + "\n"

    if columns >= 33:
        logo = _render_logo(color=colored, use_unicode=use_unicode)
        subtitle_pad = " " * max(0, (columns - len(_SHORT_SUBTITLE)) // 2)
        return "\n".join([*logo, "", subtitle_pad + _SHORT_SUBTITLE]) + "\n"

    title = _compact_title(color=colored)
    if columns >= len(_SHORT_SUBTITLE):
        title_pad = " " * max(0, (columns - len("ADAPT")) // 2)
        subtitle_pad = " " * max(0, (columns - len(_SHORT_SUBTITLE)) // 2)
        return f"{title_pad}{title}\n{subtitle_pad}{_SHORT_SUBTITLE}\n"

    short = "DEVICE TOOLKIT"
    if columns >= len(short):
        return f"{title}\n{short}\n"
    return _paint(tuple("ADAPT"[:columns]), _LETTER_COLORS, colored) + "\n"


# Retained as a stable, ASCII-only constant for callers that imported BANNER.
BANNER = render_banner(width=80, color=False, encoding="ascii")


def emit_banner(ctx: typer.Context, *, no_banner: bool) -> None:
    """Write the banner to stderr only when the human-session gates allow it."""

    # Keep a hard machine-mode guard even if callers wrap or replace the policy
    # helper. MCP and JSON stdout must remain protocol-clean.
    if _machine_facing(ctx) or not should_show_banner(ctx, no_banner=no_banner):
        return
    stderr = sys.stderr
    text = render_banner(encoding=getattr(stderr, "encoding", None))
    stderr.write(text if text.endswith("\n") else text + "\n")
    stderr.flush()
