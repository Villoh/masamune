"""Data objects shared by TUI screens, widgets, and application state."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ..config import AppConfig

PatchOptionValue = str | int | float | bool


@dataclass(frozen=True)
class Command:
    id: str
    key: str
    action: str
    label: str
    description: str


@dataclass(frozen=True)
class Preferences:
    theme: str = "morphe-dark"
    bindings: dict[str, str] | None = None

    def keymap(self) -> dict[str, str]:
        return {} if self.bindings is None else dict(self.bindings)


@dataclass(frozen=True)
class PatchListEntry:
    name: str
    enabled: bool
    version_count: int
    options: tuple[Mapping[str, object], ...]
    selected: bool


@dataclass(frozen=True)
class DashboardState:
    config_path: Path | None
    apps: tuple[AppConfig, ...] = ()
    error: str | None = None

    @property
    def loaded(self) -> bool:
        return self.config_path is not None and self.error is None
