"""Pure TUI formatting, preferences, history, and keybinding helpers."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from threading import Lock

from rich.text import Text
from textual.app import App  # pyright: ignore[reportMissingImports]
from textual.binding import (  # pyright: ignore[reportMissingImports]
    Binding,
    BindingType,
)
from textual.keys import (  # pyright: ignore[reportMissingImports]
    KEY_ALIASES,
    _normalize_key_list,
)
from textual.screen import Screen  # pyright: ignore[reportMissingImports]
from textual.theme import Theme  # pyright: ignore[reportMissingImports]

from ..cli import redact
from .models import Command, Preferences

_COMMANDS = (
    Command("palette", "ctrl+p", "command_palette", "Commands", "Open commands"),
    Command("help", "?", "show_help", "Key help", "Toggle available keys"),
    Command("sidebar", "ctrl+b", "toggle_sidebar", "Sidebar", "Collapse sidebar"),
    Command("dashboard", "1", "show_dashboard", "Dashboard", "Show dashboard"),
    Command("bundles", "2", "show_bundles", "Bundles", "Browse patch sources"),
    Command("downloads", "3", "show_downloads", "Downloads", "Download verified stock"),
    Command("build_matrix", "4", "show_build_matrix", "Build", "Show build view"),
    Command("builds", "5", "show_builds", "Builds", "Show build history"),
    Command("patches", "6", "show_patches", "Patches", "List compatible patches"),
    Command("cache", "7", "show_cache", "Cache", "Show cache controls"),
    Command("theme", "t", "change_theme", "Theme", "Choose theme"),
    Command("quit", "q", "quit", "Quit", "Quit TUI"),
)
_BINDINGS = tuple(
    Binding(command.key, command.action, command.label, id=command.id)
    for command in _COMMANDS
)
_COMMAND_IDS = frozenset(command.id for command in _COMMANDS)
_BINDING_RE = re.compile(r"[a-z0-9_?]+(?:\+[a-z0-9_?]+)*")
_BINDING_ALIASES = {
    **{alias: key for key, aliases in KEY_ALIASES.items() for alias in aliases},
    "?": "question_mark",
}

_CACHE_AREAS = (
    ("tools", "Pinned external tools", "preserved"),
    ("toolchains", "Resolved toolchain manifests", "preserved"),
    ("github-releases", "GitHub release metadata", "disposable"),
    ("google-version-mappings.json", "Confirmed version mappings", "disposable"),
    ("locks", "Runtime lock files", "runtime"),
    ("work", "Temporary intermediate state", "disposable"),
)
_CACHE_CLEANABLE_NAMES = frozenset(name for name, _purpose, _policy in _CACHE_AREAS)

_RESERVED_BINDINGS = frozenset(
    binding.key if isinstance(binding, Binding) else binding[0]
    for binding in (*App.BINDINGS, *Screen.BINDINGS)
) | {"question_mark"}
_VIEWS = (
    ("dashboard", "Dashboard", "▦"),
    ("bundles", "Bundles", "≡"),
    ("downloads", "Downloads", "⇩"),
    ("build", "Build", "▶"),
    ("builds", "Builds", "▤"),
    ("patches", "Patches", "≡"),
    ("cache", "Cache", "▣"),
)
_EYE_SCHEDULE_LENGTH = 12
_THEME_NAMES = ("morphe-dark", "morphe-light", "morphe-high-contrast")
_PREFERENCE_LOCK = Lock()
_BUILDS_HISTORY_LOCK = Lock()
_BUILDS_HISTORY_LIMIT = 50

_THEMES = (
    Theme(
        "morphe-dark",
        primary="#f2f2f2",
        secondary="#9b9b9b",
        accent="#ffffff",
        foreground="#f2f2f2",
        background="#101010",
        surface="#1a1a1a",
        panel="#252525",
    ),
    Theme(
        "morphe-light",
        primary="#111111",
        secondary="#5c5c5c",
        accent="#000000",
        foreground="#111111",
        background="#f7f7f7",
        surface="#ffffff",
        panel="#e8e8e8",
        dark=False,
    ),
    Theme(
        "morphe-high-contrast",
        primary="#ffffff",
        secondary="#ffffff",
        accent="#ffffff",
        foreground="#ffffff",
        background="#000000",
        surface="#000000",
        panel="#000000",
    ),
)


def _cell(value: object) -> Text:
    """Render table cells literally so configuration cannot inject markup."""
    return Text(redact(str(value)))


def _format_bytes(size: int) -> str:
    units = ("B", "KiB", "MiB", "GiB")
    try:
        value = float(size)
    except (TypeError, ValueError):
        return "0 B"
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{size} B"
        value /= 1024
    return f"{size} B"


def _path_usage(path: Path) -> tuple[int, int]:
    if not path.exists() or path.is_symlink():
        return (0, 0)
    if path.is_file():
        try:
            return (1, path.stat().st_size)
        except OSError:
            return (0, 0)
    files = 0
    size = 0
    for child in path.rglob("*"):
        if child.is_symlink() or not child.is_file():
            continue
        try:
            size += child.stat().st_size
        except OSError:
            continue
        files += 1
    return (files, size)


def _cache_areas(
    cache: Path, keystore: Path | None
) -> tuple[tuple[str, Path, str, str, bool], ...]:
    areas = [
        (name, cache / name, purpose, policy, True)
        for name, purpose, policy in _CACHE_AREAS
    ]
    if keystore is None:
        areas.insert(
            -1,
            (
                "masamune.p12",
                cache / "masamune.p12",
                "Auto-generated signing key",
                "preserved",
                True,
            ),
        )
    else:
        areas.insert(
            -1,
            (
                "keystore (external)",
                keystore,
                f"Configured signing key: {redact(str(keystore))}",
                "external",
                False,
            ),
        )
    return tuple(areas)


def _cache_inventory(
    cache: Path, keystore: Path | None
) -> list[tuple[str, str, str, bool, bool, int, int]]:
    return [
        (name, purpose, policy, count_in_total, path.exists(), *(_path_usage(path)))
        for name, path, purpose, policy, count_in_total in _cache_areas(cache, keystore)
    ]


def _binding_key(binding: BindingType) -> str:
    return binding.key if isinstance(binding, Binding) else binding[0]


def preference_path() -> Path:
    """Return user-only TUI preference location, outside build inputs."""
    if os.name == "nt" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "masamune" / "tui.json"
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "masamune" / "tui.json"


def _normalize_binding(key: str) -> str:
    normalized = "+".join(
        _normalize_key_list(part) for part in key.lower().strip().split("+")
    )
    return _BINDING_ALIASES.get(normalized, normalized)


def validate_keybindings(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise TypeError("keybindings must be an object")
    bindings: dict[str, str] = {}
    for command_id, key in value.items():
        if command_id not in _COMMAND_IDS:
            raise ValueError("invalid keybinding")
        if not isinstance(key, str):
            raise TypeError("invalid keybinding")
        normalized = _normalize_binding(key)
        if not _BINDING_RE.fullmatch(normalized):
            raise ValueError("invalid keybinding")
        if normalized in _RESERVED_BINDINGS:
            raise ValueError("keybinding is reserved")
        bindings[str(command_id)] = normalized
    if len(set(bindings.values())) != len(bindings):
        raise ValueError("keybindings collide")
    defaults = {command.id: _normalize_binding(command.key) for command in _COMMANDS}
    effective = {**defaults, **bindings}
    if len(set(effective.values())) != len(effective):
        raise ValueError("keybindings collide")
    return bindings


def load_preferences(path: Path | None = None) -> Preferences:
    try:
        data = json.loads((path or preference_path()).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise TypeError
        theme = data.get("theme", "morphe-dark")
        if not isinstance(theme, str) or theme not in _THEME_NAMES:
            raise ValueError
        bindings = validate_keybindings(data.get("bindings", {}))
        return Preferences(theme, bindings or None)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return Preferences()


def save_preferences(preferences: Preferences, path: Path | None = None) -> None:
    destination = path or preference_path()
    bindings = validate_keybindings(preferences.keymap())
    if preferences.theme not in _THEME_NAMES:
        raise ValueError("invalid theme")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(destination.parent, 0o700)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=".tui-",
            suffix=".tmp",
            delete=False,
        ) as file:
            temporary = Path(file.name)
            if os.name != "nt":
                os.fchmod(file.fileno(), 0o600)
            json.dump(
                {"theme": preferences.theme, "bindings": bindings}, file, sort_keys=True
            )
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        with _PREFERENCE_LOCK:
            os.replace(temporary, destination)
    finally:
        if temporary is not None:
            with suppress(FileNotFoundError):
                temporary.unlink()


def builds_history_path() -> Path:
    """TUI-owned build log, next to preferences — not part of cache dir."""
    return preference_path().parent / "builds.json"


def load_build_history(path: Path | None = None) -> list[dict[str, object]]:
    try:
        data = json.loads((path or builds_history_path()).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    return (
        [entry for entry in data if isinstance(entry, dict)]
        if isinstance(data, list)
        else []
    )


def _write_build_history(history: list[dict[str, object]], destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=".builds-",
            suffix=".tmp",
            delete=False,
        ) as file:
            temporary = Path(file.name)
            json.dump(history, file)
            file.flush()
            os.fsync(file.fileno())
        with _BUILDS_HISTORY_LOCK:
            os.replace(temporary, destination)
    finally:
        if temporary is not None:
            with suppress(FileNotFoundError):
                temporary.unlink()


def append_build_history(
    record: dict[str, object], path: Path | None = None
) -> list[dict[str, object]]:
    destination = path or builds_history_path()
    history = [record, *load_build_history(destination)][:_BUILDS_HISTORY_LIMIT]
    _write_build_history(history, destination)
    return history


def remove_build_history_entry(
    timestamp: str, path: Path | None = None
) -> list[dict[str, object]]:
    destination = path or builds_history_path()
    history = [
        entry
        for entry in load_build_history(destination)
        if entry.get("timestamp") != timestamp
    ]
    _write_build_history(history, destination)
    return history
