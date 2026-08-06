from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlparse

from .architecture import Architecture  # pyright: ignore[reportMissingImports]


class ConfigError(ValueError):
    """Raised when build configuration is malformed or unsupported."""


@dataclass(frozen=True)
class ToolchainConfig:
    morphe_source: str
    morphe_version: str
    patches_source: str
    patches_version: str
    include_universal_patches: bool = False
    include_experimental_versions: bool = False
    patches_sha256: str | None = None


@dataclass(frozen=True)
class GooglePlayConfig:
    profile: str | None
    country: str | None
    proxy: str | None
    dispenser: str | None


@dataclass(frozen=True)
class FallbackConfig:
    direct: tuple[str, ...]
    apkmirror: tuple[str, ...]


@dataclass(frozen=True)
class AppConfig:
    package: str
    name: str
    enabled: bool
    slug: str
    patched_package: str | None
    expected_signer: str | None
    source_dir: str | None
    cli_source: str | None
    morphe_version: str | None
    patches_source: str | None
    patches_version: str | None
    patches_sha256: str | None
    version: str
    version_code: str | None
    build_mode: str
    arch: str
    density: str | None
    include_patches: tuple[str, ...]
    exclude_patches: tuple[str, ...]
    exclusive_patches: tuple[str, ...]
    patch_options: Mapping[str, Mapping[str, str | int | float | bool]]
    include_universal_patches: bool | None
    include_experimental_versions: bool | None
    google_play: GooglePlayConfig
    fallbacks: FallbackConfig


@dataclass(frozen=True)
class BuildConfig:
    toolchain: ToolchainConfig
    apps: tuple[AppConfig, ...]


_TOP_KEYS = {"toolchain", "apps"}
_PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$")
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_TOOLCHAIN_KEYS = {
    "morphe-source",
    "morphe-version",
    "patches-source",
    "patches-version",
    "patches-sha256",
    "include-universal-patches",
    "include-experimental-versions",
}
_APP_KEYS = {
    "package",
    "name",
    "enabled",
    "slug",
    "patched-package",
    "expected-signer",
    "source-dir",
    "cli-source",
    "morphe-version",
    "patches-source",
    "patches-version",
    "patches-sha256",
    "version",
    "version-code",
    "build-mode",
    "arch",
    "density",
    "include-patches",
    "exclude-patches",
    "exclusive-patches",
    "patch-options",
    "include-universal-patches",
    "include-experimental-versions",
    "google-play",
    "fallbacks",
}
_GOOGLE_KEYS = {"profile", "country", "proxy", "dispenser"}
_FALLBACK_KEYS = {"direct", "apkmirror"}
_BUILD_MODES = {"apk", "module", "both"}
_ARCH_CHOICES = {"all", "both", *(architecture.value for architecture in Architecture)}
_DEFAULT_TOOLCHAIN = {
    "morphe-source": "MorpheApp/morphe-desktop",
    "morphe-version": "latest",
    "patches-source": "MorpheApp/morphe-patches",
    "patches-version": "latest",
    "patches-sha256": None,
    "include-universal-patches": False,
    "include-experimental-versions": False,
}


def user_config_path() -> Path:
    if os.name == "nt" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "masamune" / "morphe.toml"
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "masamune" / "morphe.toml"


def default_config_path() -> Path:
    local = Path("morphe.toml")
    return local if local.is_file() else user_config_path()


def ensure_config_file(path: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as file:
            file.write("# Masamune configuration. Add applications from the TUI.\n")
    except FileExistsError:
        pass


def load_config(path: Path) -> BuildConfig:
    try:
        import tomllib
    except ModuleNotFoundError:
        raise ConfigError("TOML configuration requires Python 3.11+") from None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"cannot read configuration: {path}") from error
    return parse_config(data)


def parse_config(data: object) -> BuildConfig:
    root = _table(data, "configuration")
    _keys(root, _TOP_KEYS, "configuration")
    toolchain_raw = dict(_DEFAULT_TOOLCHAIN)
    toolchain_raw.update(_table(root.get("toolchain", {}), "toolchain"))
    _keys(toolchain_raw, _TOOLCHAIN_KEYS, "toolchain")
    toolchain = ToolchainConfig(
        _repository(toolchain_raw["morphe-source"], "toolchain.morphe-source"),
        _text(toolchain_raw["morphe-version"], "toolchain.morphe-version"),
        _repository(toolchain_raw["patches-source"], "toolchain.patches-source"),
        _text(toolchain_raw["patches-version"], "toolchain.patches-version"),
        _bool(
            toolchain_raw["include-universal-patches"],
            "toolchain.include-universal-patches",
        ),
        _bool(
            toolchain_raw["include-experimental-versions"],
            "toolchain.include-experimental-versions",
        ),
        _sha256(toolchain_raw.get("patches-sha256"), "toolchain.patches-sha256"),
    )
    apps_raw = root.get("apps", [])
    if not isinstance(apps_raw, list):
        raise ConfigError("configuration.apps must be an array of tables")
    apps = tuple(_app(value, index) for index, value in enumerate(apps_raw))
    packages = [app.package for app in apps]
    if len(packages) != len(set(packages)):
        raise ConfigError("configuration contains duplicate app packages")
    slugs = [app.slug for app in apps]
    if len(slugs) != len(set(slugs)):
        raise ConfigError("configuration contains duplicate app slugs")
    return BuildConfig(toolchain, apps)


def _app(value: object, index: int) -> AppConfig:
    label = f"apps[{index}]"
    raw = _table(value, label)
    _keys(raw, _APP_KEYS, label)
    package = _text(raw.get("package"), f"{label}.package")
    if not _PACKAGE_RE.fullmatch(package):
        raise ConfigError(f"{label}.package is not a valid Android package")
    name = _text(raw.get("name"), f"{label}.name")
    enabled = _bool(raw.get("enabled", True), f"{label}.enabled")
    slug = _text(raw.get("slug", package.replace(".", "-")), f"{label}.slug")
    if not _SLUG_RE.fullmatch(slug):
        raise ConfigError(
            f"{label}.slug must contain lowercase letters, digits, and hyphens"
        )
    patched_package = raw.get("patched-package")
    if patched_package is not None:
        patched_package = _text(patched_package, f"{label}.patched-package")
        if not _PACKAGE_RE.fullmatch(patched_package):
            raise ConfigError(f"{label}.patched-package is not a valid Android package")
    expected_signer = raw.get("expected-signer")
    if expected_signer is not None:
        expected_signer = _text(expected_signer, f"{label}.expected-signer").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_signer):
            raise ConfigError(f"{label}.expected-signer must be a SHA-256 fingerprint")
    source_dir = raw.get("source-dir")
    if source_dir is not None:
        source_dir = _text(source_dir, f"{label}.source-dir")
    sources = tuple(
        _repository(raw.get(key), f"{label}.{key}")
        if raw.get(key) is not None
        else None
        for key in ("cli-source", "patches-source")
    )
    versions = tuple(
        _text(raw[key], f"{label}.{key}") if key in raw else None
        for key in ("morphe-version", "patches-version")
    )
    patches_sha256 = _sha256(raw.get("patches-sha256"), f"{label}.patches-sha256")
    if patches_sha256 is not None and not versions[1]:
        raise ConfigError(f"{label}.patches-sha256 requires patches-version")
    version = _text(raw.get("version", "auto"), f"{label}.version")
    version_code = raw.get("version-code")
    if version_code is not None:
        if isinstance(version_code, bool) or not isinstance(version_code, (str, int)):
            raise ConfigError(f"{label}.version-code must be a positive integer")
        version_code = str(version_code)
        try:
            valid_version_code = version_code.isdigit() and int(version_code) > 0
        except ValueError:
            valid_version_code = False
        if not valid_version_code:
            raise ConfigError(f"{label}.version-code must be a positive integer")
        if version in {"auto", "latest"}:
            raise ConfigError(f"{label}.version-code requires an explicit version")
    build_mode = _choice(
        raw.get("build-mode", "apk"), _BUILD_MODES, f"{label}.build-mode"
    )
    arch = _choice(raw.get("arch", "arm64-v8a"), _ARCH_CHOICES, f"{label}.arch")
    density = raw.get("density")
    if density is not None:
        density = _text(density, f"{label}.density")
    included = _strings(raw.get("include-patches", []), f"{label}.include-patches")
    excluded = _strings(raw.get("exclude-patches", []), f"{label}.exclude-patches")
    exclusive = _strings(raw.get("exclusive-patches", []), f"{label}.exclusive-patches")
    if exclusive and (included or excluded):
        raise ConfigError(
            f"{label}.exclusive-patches cannot be combined with include-patches or exclude-patches"
        )
    if set(included) & set(excluded):
        raise ConfigError(f"{label} includes and excludes the same patch")
    options = _patch_options(raw.get("patch-options", {}), f"{label}.patch-options")
    selected_names = set(exclusive or included)
    if set(options) & set(excluded) or (
        selected_names and not set(options).issubset(selected_names)
    ):
        raise ConfigError(f"{label}.patch-options contains an unselected patch")
    include_universal_patches = raw.get("include-universal-patches")
    if include_universal_patches is not None:
        include_universal_patches = _bool(
            include_universal_patches, f"{label}.include-universal-patches"
        )
    include_experimental_versions = raw.get("include-experimental-versions")
    if include_experimental_versions is not None:
        include_experimental_versions = _bool(
            include_experimental_versions, f"{label}.include-experimental-versions"
        )
    google = _google(raw.get("google-play", {}), f"{label}.google-play")
    fallbacks = _fallbacks(raw.get("fallbacks", {}), f"{label}.fallbacks")
    return AppConfig(
        package=package,
        name=name,
        enabled=enabled,
        slug=slug,
        patched_package=patched_package,
        expected_signer=expected_signer,
        source_dir=source_dir,
        cli_source=sources[0],
        morphe_version=versions[0],
        patches_source=sources[1],
        patches_version=versions[1],
        patches_sha256=patches_sha256,
        version=version,
        version_code=version_code,
        build_mode=build_mode,
        arch=arch,
        density=density,
        include_patches=included,
        exclude_patches=excluded,
        exclusive_patches=exclusive,
        patch_options=options,
        include_universal_patches=include_universal_patches,
        include_experimental_versions=include_experimental_versions,
        google_play=google,
        fallbacks=fallbacks,
    )


def _google(value: object, label: str) -> GooglePlayConfig:
    raw = _table(value, label)
    _keys(raw, _GOOGLE_KEYS, label)
    country = raw.get("country")
    if country is not None:
        country = _text(country, f"{label}.country").upper()
        if len(country) != 2 or not country.isalpha():
            raise ConfigError(f"{label}.country must be a two-letter code")
    proxy = _optional_url(raw.get("proxy"), f"{label}.proxy", {"http", "https"})
    dispenser = _optional_url(raw.get("dispenser"), f"{label}.dispenser", {"https"})
    profile = raw.get("profile")
    return GooglePlayConfig(
        _text(profile, f"{label}.profile") if profile is not None else None,
        country,
        proxy,
        dispenser,
    )


def _fallbacks(value: object, label: str) -> FallbackConfig:
    raw = _table(value, label)
    _keys(raw, _FALLBACK_KEYS, label)
    values = []
    for key in ("direct", "apkmirror"):
        urls = _strings(raw.get(key, []), f"{label}.{key}")
        for url in urls:
            _url(url, f"{label}.{key}", {"https"})
        values.append(urls)
    return FallbackConfig(*values)


def _patch_options(
    value: object, label: str
) -> Mapping[str, Mapping[str, str | int | float | bool]]:
    raw = _table(value, label)
    result: dict[str, Mapping[str, str | int | float | bool]] = {}
    for patch, options_value in raw.items():
        patch_name = _text(patch, label)
        options = _table(options_value, f"{label}.{patch_name}")
        checked: dict[str, str | int | float | bool] = {}
        for key, option in options.items():
            option_name = _text(key, f"{label}.{patch_name}")
            if not isinstance(option, (str, int, float, bool)):
                raise ConfigError(
                    f"{label}.{patch_name}.{option_name} must be a scalar TOML value"
                )
            checked[option_name] = option
        result[patch_name] = MappingProxyType(checked)
    return MappingProxyType(result)


def _table(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ConfigError(f"{label} must be a table")
    return value


def _keys(raw: Mapping[str, object], allowed: set[str], label: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(f"unknown {label} key: {', '.join(unknown)}")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label} must be a non-empty string")
    return value.strip()


def _repository(value: object, label: str) -> str:
    repository = _text(value, label)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ConfigError(f"{label} must be owner/repo")
    return repository


def _bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{label} must be a boolean")
    return value


def _sha256(value: object, label: str) -> str | None:
    if value is None:
        return None
    digest = _text(value, label).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ConfigError(f"{label} must be a SHA-256 digest")
    return digest


def app_toolchain(app: AppConfig, default: ToolchainConfig) -> ToolchainConfig:
    return ToolchainConfig(
        app.cli_source or default.morphe_source,
        app.morphe_version or default.morphe_version,
        app.patches_source or default.patches_source,
        app.patches_version or default.patches_version,
        default.include_universal_patches,
        default.include_experimental_versions,
        app.patches_sha256 or default.patches_sha256,
    )


def app_include_universal_patches(app: AppConfig, default: ToolchainConfig) -> bool:
    if app.include_universal_patches is not None:
        return app.include_universal_patches
    return default.include_universal_patches


def app_include_experimental_versions(app: AppConfig, default: ToolchainConfig) -> bool:
    if app.include_experimental_versions is not None:
        return app.include_experimental_versions
    return default.include_experimental_versions


def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ConfigError(f"{label} must be an array of strings")
    result = tuple(_text(item, label) for item in value)
    if len(result) != len(set(result)):
        raise ConfigError(f"{label} contains duplicates")
    return result


def _choice(value: object, choices: set[str], label: str) -> str:
    text = _text(value, label)
    if text not in choices:
        raise ConfigError(f"{label} must be one of: {', '.join(sorted(choices))}")
    return text


def _optional_url(value: object, label: str, schemes: set[str]) -> str | None:
    if value is None:
        return None
    text = _text(value, label)
    _url(text, label, schemes)
    return text


def _url(value: str, label: str, schemes: set[str]) -> None:
    parsed = urlparse(value)
    if (
        parsed.scheme not in schemes
        or not parsed.hostname
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ConfigError(f"{label} contains an invalid URL")
