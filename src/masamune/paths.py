from __future__ import annotations

import json
import os
import shutil
from pathlib import Path


def default_data_path() -> Path:
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "masamune"
    root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return root / "masamune"


def default_download_path() -> Path:
    return default_data_path() / "downloads"


def migrate_legacy_downloads(destination: Path) -> int:
    """Move recognizable legacy APK sets below the dedicated downloads folder."""
    legacy = destination.parent
    if legacy == destination or not legacy.is_dir() or legacy.is_symlink():
        return 0
    destination.mkdir(parents=True, exist_ok=True)
    moved = 0
    for app_root in legacy.iterdir():
        if (
            app_root == destination
            or not app_root.is_dir()
            or app_root.is_symlink()
            or not _looks_like_download(app_root)
        ):
            continue
        target = destination / app_root.name
        if target.exists() or target.is_symlink():
            continue
        try:
            shutil.move(str(app_root), str(target))
        except OSError:
            continue
        moved += 1
    return moved


def _looks_like_download(app_root: Path) -> bool:
    for version_root in app_root.iterdir():
        if not version_root.is_dir() or version_root.is_symlink():
            continue
        for architecture_root in version_root.iterdir():
            provenance = architecture_root / "provenance.json"
            if not architecture_root.is_dir() or architecture_root.is_symlink():
                continue
            if not provenance.is_file() or provenance.is_symlink():
                continue
            try:
                data = json.loads(provenance.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and isinstance(data.get("package"), str):
                return True
    return False
