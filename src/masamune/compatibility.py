from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9A-Za-z_-]+)+$")
VERSION_CODE_RE = re.compile(r"^[1-9][0-9]*$")
MAPPING_CACHE_NAME = "google-version-mappings.json"
_CONFIRMED_SOURCES = {"google-response", "apk-manifest"}
# Older Morphe CLI versions printed this literal marker as the
# "Package name:" value for Universal patches (compatible with any app,
# e.g. "Change package name"). Newer versions (morphe-desktop >= v1.12.0)
# omit the "Compatible packages:" block entirely instead, so a patch with
# no package line at all is also treated as universal.
UNIVERSAL_PACKAGE_MARKER = "(universal)"


class CompatibilityError(RuntimeError):
    """Raised when patches or stock versions cannot be resolved safely."""


PatchOptionScalar = str | int | float | bool


@dataclass(frozen=True)
class PatchOption:
    title: str
    description: str
    required: bool
    key: str
    default: PatchOptionScalar | None
    values: tuple[PatchOptionScalar, ...]
    type: str


@dataclass(frozen=True)
class PatchCompatibility:
    name: str
    enabled: bool
    versions: tuple[str, ...]
    options: tuple[PatchOption, ...] = ()


@dataclass(frozen=True)
class CompatibilityResult:
    package: str
    selected_patches: tuple[str, ...]
    compatible_versions: tuple[str, ...]
    selected_version: str


@dataclass(frozen=True)
class ConfirmedVersion:
    version_name: str
    version_code: str
    source: str


GoogleProbe = Callable[
    [str, str, str, str | None, str | None, str | None], ConfirmedVersion | None
]
CandidateLookup = Callable[[str, str, str, str | None, str | None], str | None]
FallbackDownload = Callable[
    [str, str, str, str | None, str | None], ConfirmedVersion | None
]


def morphe_command(
    java: Path | str,
    cli: Path,
    action: str,
    patches: Path,
    package: str,
    *,
    include_universal_patches: bool = False,
    include_experimental_versions: bool = False,
    with_options: bool = False,
) -> list[str]:
    command = [str(java), "-jar", str(cli), action, f"--patches={patches}"]
    if action == "list-patches":
        command.extend(
            (
                f"--filter-package-name={package}",
                "--with-packages",
                "--with-versions",
                "--with-descriptions=false",
                f"--with-options={'true' if with_options else 'false'}",
                "--index=false",
                f"--with-universal-patches={'true' if include_universal_patches else 'false'}",
                f"--include-experimental={'true' if include_experimental_versions else 'false'}",
            )
        )
    elif action == "list-versions":
        command.extend(
            (
                f"--filter-package-names={package}",
                f"--include-experimental={'true' if include_experimental_versions else 'false'}",
            )
        )
    else:
        raise ValueError(f"unsupported Morphe compatibility action: {action}")
    return command


def run_morphe_action(
    java: Path | str,
    cli: Path,
    patches: Path,
    package: str,
    action: str,
    *,
    include_universal_patches: bool = False,
    include_experimental_versions: bool = False,
    with_options: bool = False,
) -> str:
    try:
        result = subprocess.run(
            morphe_command(
                java,
                cli,
                action,
                patches,
                package,
                include_universal_patches=include_universal_patches,
                include_experimental_versions=include_experimental_versions,
                with_options=with_options,
            ),
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise CompatibilityError(f"Morphe {action} command failed") from None
    return result.stdout


def run_morphe_compatibility(
    java: Path | str,
    cli: Path,
    patches: Path,
    package: str,
    *,
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
    exclusive: Sequence[str] = (),
    requested_version: str = "auto",
    include_universal_patches: bool = False,
    include_experimental_versions: bool = False,
) -> CompatibilityResult:
    outputs = [
        run_morphe_action(
            java,
            cli,
            patches,
            package,
            action,
            include_universal_patches=include_universal_patches,
            include_experimental_versions=include_experimental_versions,
        )
        for action in ("list-patches", "list-versions")
    ]
    return resolve_patch_compatibility(
        package,
        parse_patch_list(outputs[0], package),
        parse_version_list(outputs[1], package),
        include=include,
        exclude=exclude,
        exclusive=exclusive,
        requested_version=requested_version,
    )


def parse_patch_list(output: str, package: str) -> tuple[PatchCompatibility, ...]:
    lines = [_clean(line) for line in output.splitlines()]
    patches: list[PatchCompatibility] = []
    name: str | None = None
    enabled: bool | None = None
    current_package: str | None = None
    versions: list[str] = []
    options: list[PatchOption] = []
    in_options = False
    option_title: str | None = None
    option_description: list[str] = []
    option_required: bool | None = None
    option_key: str | None = None
    option_default: str | None = None
    option_values: list[str] = []
    option_type: str | None = None
    option_section: str | None = None

    def finish_option() -> None:
        nonlocal option_title, option_description, option_required, option_key
        nonlocal option_default, option_values, option_type, option_section
        if option_title is not None:
            if option_required is None or option_key is None or option_type is None:
                raise CompatibilityError(
                    f"invalid option metadata for patch: {name or 'unknown'}"
                )
            options.append(
                PatchOption(
                    option_title,
                    "\n".join(option_description).strip(),
                    option_required,
                    option_key,
                    _patch_option_scalar(option_default, option_type)
                    if option_default is not None
                    else None,
                    tuple(
                        _patch_option_scalar(_patch_option_value(value), option_type)
                        for value in option_values
                    ),
                    option_type,
                )
            )
        option_title = None
        option_description = []
        option_required = None
        option_key = None
        option_default = None
        option_values = []
        option_type = None
        option_section = None

    def finish() -> None:
        nonlocal name, enabled, current_package, versions, options, in_options
        finish_option()
        universal = (
            current_package is None or current_package == UNIVERSAL_PACKAGE_MARKER
        )
        if name is not None and (current_package == package or universal):
            if enabled is None or (not universal and not versions):
                raise CompatibilityError(
                    f"invalid compatibility metadata for patch: {name}"
                )
            patches.append(
                PatchCompatibility(
                    name,
                    enabled,
                    tuple(dict.fromkeys(versions)),
                    tuple(options),
                )
            )
        name = None
        enabled = None
        current_package = None
        versions = []
        options = []
        in_options = False

    for line in lines:
        stripped = line.strip()
        if in_options:
            if line.startswith("\t\t") and option_section == "values":
                if stripped:
                    option_values.append(stripped)
                continue
            if line.startswith("\tTitle:"):
                finish_option()
                option_title = stripped.removeprefix("Title:").strip()
                continue
            if line.startswith("\tDescription:") and option_title is not None:
                option_description = [stripped.removeprefix("Description:").strip()]
                option_section = "description"
                continue
            if line.startswith("\tRequired:") and option_title is not None:
                required = stripped.removeprefix("Required:").strip().lower()
                if required not in {"true", "false"}:
                    raise CompatibilityError("invalid Morphe patch option output")
                option_required = required == "true"
                option_section = None
                continue
            if line.startswith("\tKey:") and option_title is not None:
                option_key = stripped.removeprefix("Key:").strip()
                option_section = None
                continue
            if line.startswith("\tDefault:") and option_title is not None:
                option_default = stripped.removeprefix("Default:").strip()
                option_section = None
                continue
            if line.startswith("\tPossible values:") and option_title is not None:
                option_section = "values"
                continue
            if line.startswith("\tType:") and option_title is not None:
                option_type = stripped.removeprefix("Type:").strip()
                option_section = None
                continue
            if line.startswith("\t"):
                if option_section == "description":
                    option_description.append(stripped)
                continue
            finish_option()
            in_options = False

        if stripped.startswith("Name:"):
            finish()
            name = stripped.removeprefix("Name:").strip()
            if not name:
                raise CompatibilityError("invalid Morphe patch list output")
        elif stripped.startswith("Enabled:") and name is not None:
            value = stripped.removeprefix("Enabled:").strip().lower()
            if value not in {"true", "false"}:
                raise CompatibilityError("invalid Morphe patch list output")
            enabled = value == "true"
        elif stripped == "Options:" and name is not None:
            in_options = True
        elif stripped.startswith("Package name:") and name is not None:
            current_package = stripped.removeprefix("Package name:").strip()
        elif current_package == package and VERSION_RE.fullmatch(stripped):
            versions.append(stripped)
    finish()
    names = [patch.name for patch in patches]
    if len(names) != len(set(names)):
        raise CompatibilityError("duplicate patch names in Morphe output")
    return tuple(patches)


def _patch_option_value(value: str) -> str:
    match = re.fullmatch(r"(.+?) \(.+\)", value)
    return match.group(1) if match else value


def _patch_option_scalar(value: str, type_name: str) -> PatchOptionScalar:
    kind = type_name.rsplit(".", 1)[-1]
    if kind == "Boolean":
        if value.lower() not in {"true", "false"}:
            raise CompatibilityError("invalid Boolean patch option value")
        return value.lower() == "true"
    if kind in {"Byte", "Short", "Int", "Long"}:
        try:
            return int(value)
        except ValueError:
            raise CompatibilityError("invalid integer patch option value") from None
    if kind in {"Float", "Double"}:
        try:
            return float(value)
        except ValueError:
            raise CompatibilityError("invalid numeric patch option value") from None
    return value


def parse_version_list(output: str, package: str) -> tuple[str, ...]:
    lines = [_clean(line).strip() for line in output.splitlines()]
    packages = [
        line.removeprefix("Package name:").strip()
        for line in lines
        if line.startswith("Package name:")
    ]
    if packages != [package]:
        raise CompatibilityError(f"Morphe reported no versions for package: {package}")
    versions = []
    for line in lines:
        match = re.fullmatch(
            r"([0-9]+(?:\.[0-9A-Za-z_-]+)+)\s+\([0-9]+ patches\)", line
        )
        if match:
            versions.append(match.group(1))
    if not versions:
        raise CompatibilityError(
            f"Morphe reported no compatible versions for package: {package}"
        )
    return tuple(dict.fromkeys(versions))


def resolve_patch_compatibility(
    package: str,
    patches: Sequence[PatchCompatibility],
    version_order: Sequence[str],
    *,
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
    exclusive: Sequence[str] = (),
    requested_version: str = "auto",
) -> CompatibilityResult:
    available = {patch.name: patch for patch in patches}
    if not available:
        raise CompatibilityError(f"package has no matching patches: {package}")
    requested_names = set(include) | set(exclude) | set(exclusive)
    missing = sorted(requested_names - set(available))
    if missing:
        raise CompatibilityError(f"unknown patch for {package}: {', '.join(missing)}")
    if exclusive:
        selected = tuple(name for name in exclusive)
    else:
        selected = tuple(
            name
            for name, patch in available.items()
            if patch.enabled and name not in set(exclude)
        )
        for name in include:
            if name not in selected:
                selected += (name,)
    if not selected:
        raise CompatibilityError(f"no patches selected for package: {package}")
    # Universal patches (e.g. "Change package name") have no per-app version
    # list -- they apply regardless of version, so they must not constrain
    # (or empty out) the version intersection below.
    versioned = [
        available[name].versions for name in selected if available[name].versions
    ]
    if not versioned:
        raise CompatibilityError(
            f"no version-compatible patches selected for package: {package}"
        )
    common = set(versioned[0])
    for versions in versioned[1:]:
        common &= set(versions)
    compatible = tuple(version for version in version_order if version in common)
    if not compatible:
        raise CompatibilityError(
            f"selected patches have no common version for {package}"
        )
    if requested_version in {"auto", "latest"}:
        selected_version = compatible[0]
    elif requested_version not in compatible:
        raise CompatibilityError(
            f"requested version {requested_version} is incompatible with selected patches"
        )
    else:
        selected_version = requested_version
    return CompatibilityResult(package, selected, compatible, selected_version)


def resolve_version_code_candidate(
    cache_root: Path,
    *,
    package: str,
    version_name: str,
    arch: str,
    profile: str | None,
    region: str | None,
    explicit_version_code: str | None = None,
    fallback_metadata: CandidateLookup | None = None,
) -> ConfirmedVersion | None:
    _validate_request(package, version_name, arch, explicit_version_code)
    if explicit_version_code is not None:
        return ConfirmedVersion(version_name, explicit_version_code, "explicit")
    key = _mapping_key(package, version_name, arch, profile, region)
    cached = _read_mapping_cache(cache_root).get(key)
    if cached is not None:
        return ConfirmedVersion(version_name, cached, "cache")
    if fallback_metadata is None:
        return None
    candidate = fallback_metadata(package, version_name, arch, profile, region)
    if candidate is None:
        return None
    if not VERSION_CODE_RE.fullmatch(candidate):
        raise CompatibilityError("fallback metadata returned invalid version code")
    return ConfirmedVersion(version_name, candidate, "metadata")


def confirm_version_code(
    cache_root: Path,
    *,
    package: str,
    version_name: str,
    arch: str,
    profile: str | None,
    region: str | None,
    version_code: str,
) -> ConfirmedVersion:
    key = _mapping_key(package, version_name, arch, profile, region)
    return _confirm_and_cache(
        cache_root,
        key,
        version_name,
        ConfirmedVersion(version_name, version_code, "apk-manifest"),
    )


def resolve_google_version(
    cache_root: Path,
    *,
    package: str,
    version_name: str,
    arch: str,
    profile: str | None,
    region: str | None,
    explicit_version_code: str | None = None,
    google_probe: GoogleProbe,
    fallback_metadata: CandidateLookup | None = None,
    fallback_download: FallbackDownload | None = None,
) -> ConfirmedVersion:
    candidate = resolve_version_code_candidate(
        cache_root,
        package=package,
        version_name=version_name,
        arch=arch,
        profile=profile,
        region=region,
        explicit_version_code=explicit_version_code,
        fallback_metadata=fallback_metadata,
    )
    if candidate is not None and candidate.source in {"explicit", "cache"}:
        return candidate
    code = candidate.version_code if candidate is not None else None
    resolved = google_probe(package, version_name, arch, profile, region, code)
    if resolved is not None:
        return _confirm_and_cache(
            cache_root,
            _mapping_key(package, version_name, arch, profile, region),
            version_name,
            resolved,
        )
    if fallback_download is not None:
        resolved = fallback_download(package, version_name, arch, profile, region)
        if resolved is not None:
            return _confirm_and_cache(
                cache_root,
                _mapping_key(package, version_name, arch, profile, region),
                version_name,
                resolved,
            )
    raise CompatibilityError(f"version unavailable for {package} {version_name} {arch}")


def resolve_google_versions(
    cache_root: Path,
    *,
    package: str,
    version_name: str,
    arches: Iterable[str],
    profile: str | None,
    region: str | None,
    explicit_version_code: str | None = None,
    google_probe: GoogleProbe,
    fallback_metadata: CandidateLookup | None = None,
    fallback_download: FallbackDownload | None = None,
) -> dict[str, ConfirmedVersion]:
    return {
        arch: resolve_google_version(
            cache_root,
            package=package,
            version_name=version_name,
            arch=arch,
            profile=profile,
            region=region,
            explicit_version_code=explicit_version_code,
            google_probe=google_probe,
            fallback_metadata=fallback_metadata,
            fallback_download=fallback_download,
        )
        for arch in arches
    }


def _confirm_and_cache(
    cache_root: Path,
    key: str,
    expected_name: str,
    resolved: ConfirmedVersion,
) -> ConfirmedVersion:
    if resolved.version_name != expected_name:
        raise CompatibilityError(
            f"resolved version name mismatch: expected {expected_name}, got {resolved.version_name}"
        )
    if resolved.source not in _CONFIRMED_SOURCES:
        raise CompatibilityError("version mapping lacks confirmed evidence")
    if not VERSION_CODE_RE.fullmatch(resolved.version_code):
        raise CompatibilityError("resolved invalid version code")
    mappings = _read_mapping_cache(cache_root)
    mappings[key] = resolved.version_code
    _write_mapping_cache(cache_root, mappings)
    return resolved


def _mapping_key(
    package: str,
    version_name: str,
    arch: str,
    profile: str | None,
    region: str | None,
) -> str:
    raw = json.dumps(
        [package, version_name, arch, profile, region],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _read_mapping_cache(cache_root: Path) -> dict[str, str]:
    path = cache_root / MAPPING_CACHE_NAME
    if not path.exists():
        return {}
    if not path.is_file() or path.is_symlink():
        raise CompatibilityError("invalid Google version mapping cache")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CompatibilityError("invalid Google version mapping cache") from error
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "mappings"}:
        raise CompatibilityError("invalid Google version mapping cache")
    mappings = raw["mappings"]
    if raw["schema_version"] != 1 or not isinstance(mappings, dict):
        raise CompatibilityError("invalid Google version mapping cache")
    if any(
        not isinstance(key, str)
        or not re.fullmatch(r"[0-9a-f]{64}", key)
        or not isinstance(value, str)
        or not VERSION_CODE_RE.fullmatch(value)
        for key, value in mappings.items()
    ):
        raise CompatibilityError("invalid Google version mapping cache")
    return dict(mappings)


def _write_mapping_cache(cache_root: Path, mappings: Mapping[str, str]) -> None:
    cache_root.mkdir(parents=True, exist_ok=True)
    path = cache_root / MAPPING_CACHE_NAME
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=cache_root, prefix=f".{path.name}-", delete=False
    ) as temporary:
        json.dump(
            {"schema_version": 1, "mappings": dict(sorted(mappings.items()))},
            temporary,
            indent=2,
            sort_keys=True,
        )
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, path)


def _validate_request(
    package: str, version_name: str, arch: str, explicit_version_code: str | None
) -> None:
    if not package or "." not in package or not VERSION_RE.fullmatch(version_name):
        raise CompatibilityError("invalid Google version resolution request")
    if arch not in {"arm64", "armv7"}:
        raise CompatibilityError("unsupported Google resolution architecture")
    if explicit_version_code is not None and not VERSION_CODE_RE.fullmatch(
        explicit_version_code
    ):
        raise CompatibilityError("explicit version code must be a positive integer")


def _clean(line: str) -> str:
    return ANSI_RE.sub("", line).removeprefix("INFO: ")
