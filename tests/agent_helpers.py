"""Shared sandbox for the `adapt agent` tests: every path points into a temporary folder.

Nothing here reads or writes the real user profile. A test builds a `Sandbox`, which creates a
fake home, project and config folder, and passes `sandbox.ctx(...)` (or `sandbox.env`) to the code
under test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from adapt_adb.agent_setup import Context

FAKE_ADAPT = "FAKE_BIN/adapt"


@dataclass
class Sandbox:
    root: Path
    on_path: set[str] = field(default_factory=set)
    commands: list[list[str]] = field(default_factory=list)
    run_code: int = 0
    clock: list[int] = field(default_factory=lambda: [0])

    def __post_init__(self) -> None:
        for name in ("home", "project", "appdata", "local", "config", "bin"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self.adapt = str(self.root / "bin" / "adapt")

    @property
    def home(self) -> Path:
        return self.root / "home"

    @property
    def project(self) -> Path:
        return self.root / "project"

    @property
    def env(self) -> dict[str, str]:
        return {
            "HOME": str(self.home), "USERPROFILE": str(self.home), "APPDATA": str(self.root / "appdata"),
            "LOCALAPPDATA": str(self.root / "local"), "XDG_CONFIG_HOME": str(self.home / ".config"),
            "ADAPT_CONFIG_DIR": str(self.root / "config"), "COPILOT_HOME": str(self.home / ".copilot"),
            "PATH": str(self.root / "bin"),
        }

    @property
    def manifest(self) -> Path:
        return self.root / "config" / "agent-installs.json"

    def runner(self, argv: list[str], cwd: str | None, timeout: float) -> tuple[int, str]:
        self.commands.append(list(argv))
        return self.run_code, "fake client output"

    def which(self, name: str) -> str | None:
        return str(self.root / "bin" / name) if name in self.on_path else None

    def tick(self) -> datetime:
        self.clock[0] += 1
        return datetime(2026, 11, 16, 12, 0, self.clock[0], tzinfo=timezone.utc)

    def ctx(self, *, platform: str = "linux", project: bool = True, environ: dict[str, str] | None = None) -> Context:
        return Context(environ=self.env if environ is None else environ, project_dir=self.project if project else None,
                       platform=platform, which=self.which, runner=self.runner, now=self.tick)

    def touch(self, relative: str, text: str | bytes = "") -> Path:
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
        return path

    def files(self, folder: Path | None = None) -> list[str]:
        base = folder or self.root
        return sorted(p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file())


def tree_snapshot(root: Path) -> dict[str, bytes]:
    """Every file under `root` with its bytes, to prove a refusal changed nothing."""
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def fake_which(names: set[str]) -> Callable[[str], str | None]:
    return lambda name: f"/fake/{name}" if name in names else None
