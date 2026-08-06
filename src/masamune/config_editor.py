"""Small round-trip editor for TUI-owned morphe.toml changes."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from threading import Lock

import tomlkit  # pyright: ignore[reportMissingImports]
from tomlkit.container import Container  # pyright: ignore[reportMissingImports]
from tomlkit.exceptions import ParseError  # pyright: ignore[reportMissingImports]
from tomlkit.items import AoT, Table  # pyright: ignore[reportMissingImports]

from .config import ConfigError, load_config

_WRITE_LOCK = Lock()
_APP_FIELDS = (
    "package",
    "name",
    "enabled",
    "include-universal-patches",
    "include-experimental-versions",
    "slug",
    "patched-package",
    "expected-signer",
    "source-dir",
    "version",
    "build-mode",
    "arch",
    "patches-source",
    "patches-version",
    "patches-sha256",
    "fallback-direct",
    "fallback-apkmirror",
    "google-play-profile",
    "google-play-country",
    "google-play-proxy",
    "google-play-dispenser",
)
_FALLBACK_FIELD_KEYS = {
    "fallback-direct": "direct",
    "fallback-apkmirror": "apkmirror",
}
_GOOGLE_FIELD_KEYS = {
    "google-play-profile": "profile",
    "google-play-country": "country",
    "google-play-proxy": "proxy",
    "google-play-dispenser": "dispenser",
}


def add_app(path: Path, fields: Mapping[str, str]) -> None:
    """Append one app while preserving existing formatting and comments."""

    def mutate(document: Container) -> None:
        apps = _apps(document)
        app = tomlkit.table()
        _set_fields(app, fields)
        apps.append(app)

    _update(path, mutate)


def update_app(path: Path, package: str, fields: Mapping[str, str]) -> None:
    """Update editable scalar fields for one configured app."""

    def mutate(document: Container) -> None:
        _set_fields(_find_app(document, package), fields)

    _update(path, mutate)


def set_app_patch_source(path: Path, package: str, source: str, version: str) -> None:
    """Change only patch source fields, preserving every other app setting."""

    def mutate(document: Container) -> None:
        app = _find_app(document, package)
        app["patches-source"] = source
        app["patches-version"] = version

    _update(path, mutate)


def remove_app(path: Path, package: str) -> None:
    """Remove one configured app; validation prevents removing the last app."""

    def mutate(document: Container) -> None:
        apps = _apps(document)
        if len(apps) == 1:
            raise ConfigError("configuration.apps must contain at least one app")
        for index, app in enumerate(apps):
            if str(app.get("package", "")) == package:
                del apps[index]
                return
        raise ValueError("package is not configured")

    _update(path, mutate)


def set_exclusive_patches(
    path: Path,
    package: str,
    patches: Sequence[str],
    patch_options: Mapping[str, Mapping[str, str | int | float | bool]] | None = None,
) -> None:
    """Persist exact patch selection and its scalar option overrides."""
    selected = list(dict.fromkeys(patches))

    def mutate(document: Container) -> None:
        app = _find_app(document, package)
        app["exclusive-patches"] = selected
        app.pop("include-patches", None)
        app.pop("exclude-patches", None)
        if patch_options is None:
            options = app.get("patch-options")
            if isinstance(options, Table):
                for name in tuple(options):
                    if name not in selected:
                        del options[name]
                if not options:
                    app.pop("patch-options", None)
            return
        app.pop("patch-options", None)
        options = tomlkit.table()
        for name in selected:
            values = patch_options.get(name, {})
            if not values:
                continue
            patch = tomlkit.table()
            for key, value in values.items():
                patch[key] = value
            options[name] = patch
        if options:
            app["patch-options"] = options

    _update(path, mutate)


def _set_fields(app: Table, fields: Mapping[str, str]) -> None:
    for key in _APP_FIELDS:
        if key not in fields:
            continue
        value = fields.get(key, "").strip()
        if key in {
            "enabled",
            "include-universal-patches",
            "include-experimental-versions",
        }:
            if not value:
                app.pop(key, None)
            elif value.lower() in {"true", "false"}:
                app[key] = value.lower() == "true"
            else:
                raise ValueError(f"{key} must be true or false")
        elif key in _FALLBACK_FIELD_KEYS:
            urls = [item.strip() for item in value.split(",") if item.strip()]
            fallbacks = app.get("fallbacks")
            if urls:
                if not isinstance(fallbacks, Table):
                    fallbacks = tomlkit.table()
                    app["fallbacks"] = fallbacks
                fallbacks[_FALLBACK_FIELD_KEYS[key]] = urls
            elif isinstance(fallbacks, Table):
                fallbacks.pop(_FALLBACK_FIELD_KEYS[key], None)
                if not fallbacks:
                    app.pop("fallbacks", None)
        elif key in _GOOGLE_FIELD_KEYS:
            google = app.get("google-play")
            if value:
                if not isinstance(google, Table):
                    google = tomlkit.table()
                    app["google-play"] = google
                google[_GOOGLE_FIELD_KEYS[key]] = value
            elif isinstance(google, Table):
                google.pop(_GOOGLE_FIELD_KEYS[key], None)
                if not google:
                    app.pop("google-play", None)
        elif value:
            app[key] = value
        elif key not in {"package", "name"}:
            app.pop(key, None)
    if not str(app.get("package", "")).strip() or not str(app.get("name", "")).strip():
        raise ValueError("package and name are required")


def _apps(document: Container) -> AoT:
    apps = document.get("apps")
    if apps is None:
        apps = tomlkit.aot()
        document["apps"] = apps
    if not isinstance(apps, AoT):
        raise TypeError("configuration.apps must be an array of tables")
    return apps


def _find_app(document: Container, package: str) -> Table:
    for app in _apps(document):
        if str(app.get("package", "")) == package:
            return app
    raise ValueError("package is not configured")


def _update(path: Path, mutate: Callable[[Container], None]) -> None:
    """Write same-directory temp, validate it, then atomically replace source."""
    try:
        document = tomlkit.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ParseError) as error:
        raise ValueError(f"cannot read configuration: {path}") from error
    mutate(document)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}-",
            suffix=".tmp",
            delete=False,
        ) as file:
            temporary = Path(file.name)
            file.write(tomlkit.dumps(document))
            file.flush()
            os.fsync(file.fileno())
        if path.exists() and os.name != "nt":
            os.chmod(temporary, path.stat().st_mode & 0o777)
        load_config(temporary)
        with _WRITE_LOCK:
            os.replace(temporary, path)
    finally:
        if temporary is not None:
            with suppress(FileNotFoundError):
                temporary.unlink()
