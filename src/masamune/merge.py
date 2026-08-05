from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .apk import (  # pyright: ignore[reportMissingImports]
    ApkMetadata,
    inspect_apk,
    validate_zip_archive,
)
from .errors import IntegrityMetadataError  # pyright: ignore[reportMissingImports]
from .hashing import sha256_file

SELECTION_MANIFEST = "selected-splits.json"
MERGE_PROVENANCE = "merge-provenance.json"
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_SECRET_RE = re.compile(r"(?i)\b(authorization|cookie|token|password)\b\s*[:=]\s*\S+")


class MergeError(RuntimeError):
    """Raised when verified splits cannot be selected or merged safely."""


@dataclass(frozen=True)
class SelectedSplitSet:
    source: Path
    package: str
    version_name: str
    version_code: str
    arch: str
    density: str | None
    files: tuple[ApkMetadata, ...]


def select_splits(
    source: Path,
    metadata: list[ApkMetadata],
    *,
    arch: str,
    density: str | None = None,
) -> SelectedSplitSet:
    if arch not in {"arm64", "armv7"}:
        raise MergeError(f"unsupported target ABI: {arch}")
    bases = [item for item in metadata if item.split_type == "base"]
    if len(bases) != 1:
        raise MergeError("verified set must contain exactly one base APK")
    unsupported = sorted(
        {
            requirement
            for item in metadata
            for requirement in item.unsupported_requirements
        }
    )
    if unsupported:
        raise MergeError(f"unsupported APK requirement: {', '.join(unsupported)}")
    base = bases[0]
    selected: dict[str, ApkMetadata] = {base.path: base}
    abi_splits = [
        item for item in metadata if item.split_type == "abi" and not item.feature
    ]
    matching_abi = [item for item in abi_splits if item.abi == arch]
    if abi_splits and not matching_abi:
        raise MergeError(f"missing required ABI split for {arch}")
    for item in matching_abi:
        selected[item.path] = item

    density_splits = [
        item for item in metadata if item.split_type == "density" and not item.feature
    ]
    if density_splits:
        if density is None:
            if len(density_splits) != 1:
                raise MergeError("multiple density splits require explicit density")
            chosen_density = density_splits
            density = density_splits[0].density
        else:
            chosen_density = [
                item for item in density_splits if item.density == density
            ]
            if not chosen_density:
                raise MergeError(f"missing requested density split: {density}")
        for item in chosen_density:
            selected[item.path] = item

    # Language policy: include every verified language split. No locale preference exists yet.
    for item in metadata:
        if item.split_type == "language" and not item.feature:
            selected[item.path] = item

    by_name = {item.split_name: item for item in metadata if item.split_name}
    changed = True
    while changed:
        changed = False
        required_names = {
            required for item in selected.values() for required in item.required_splits
        }
        for name in sorted(required_names):
            required = by_name.get(name)
            if required is None:
                raise MergeError(f"missing required split: {name}")
            if required.path not in selected:
                selected[required.path] = required
                changed = True
        available_types = {
            split_type
            for item in selected.values()
            for split_type in item.platform_split_types
        }
        required_types = {
            split_type
            for item in selected.values()
            for split_type in item.required_split_types
        }
        for split_type in sorted(required_types - available_types):
            providers = [
                item
                for item in metadata
                if split_type in item.platform_split_types
                and (not item.abi or item.abi == arch)
                and (not item.density or not density or item.density == density)
            ]
            if not providers:
                raise MergeError(f"missing required split type: {split_type}")
            for provider in providers:
                if provider.path not in selected:
                    selected[provider.path] = provider
                    changed = True
        selected_features = {
            item.split_name
            for item in selected.values()
            if item.split_type == "feature"
        }
        for item in metadata:
            if item.feature not in selected_features or item.path in selected:
                continue
            if item.split_type == "abi" and item.abi != arch:
                continue
            if item.split_type == "density" and density and item.density != density:
                continue
            if item.split_type in {"language", "abi", "density", "neutral"}:
                selected[item.path] = item
                changed = True

    available_types = {
        split_type
        for item in selected.values()
        for split_type in item.platform_split_types
    }
    missing_types = sorted(
        {
            split_type
            for item in selected.values()
            for split_type in item.required_split_types
        }
        - available_types
    )
    if missing_types:
        raise MergeError(f"missing required split type: {', '.join(missing_types)}")
    files = tuple(sorted(selected.values(), key=lambda item: item.path))
    return SelectedSplitSet(
        source.resolve(),
        base.package,
        base.version_name,
        base.version_code,
        arch,
        density,
        files,
    )


def write_selection_manifest(selection: SelectedSplitSet, output: Path) -> Path:
    data = {
        "schema_version": 1,
        "package": selection.package,
        "version": {"name": selection.version_name, "code": selection.version_code},
        "architecture": selection.arch,
        "density": selection.density,
        "language_policy": "all-verified",
        "files": [
            {
                "path": item.path,
                "split_name": item.split_name,
                "split_type": item.split_type,
                "abi": item.abi,
                "density": item.density,
                "language": item.language,
                "feature": item.feature,
                "sha256": sha256_file(_source_file(selection, item)),
            }
            for item in selection.files
        ],
    }
    path = output / SELECTION_MANIFEST
    _atomic_json(path, data)
    return path


def apkeditor_command(
    java: Path | str, apkeditor: Path, input_directory: Path, output: Path
) -> list[str]:
    return [
        str(java),
        "-jar",
        str(apkeditor),
        "m",
        "-i",
        str(input_directory),
        "-o",
        str(output),
    ]


def merge_splits(
    selection: SelectedSplitSet,
    output: Path,
    *,
    java: Path | str,
    apkeditor: Path,
    apkeditor_version: str,
    timeout: int = 600,
) -> Path:
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"refusing to overwrite merged APK: {output}")
    if not apkeditor.is_file() or apkeditor.is_symlink():
        raise MergeError("missing or invalid APKEditor tool")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".morphe-merge-", dir=output.parent) as raw:
        temporary = Path(raw)
        inputs = temporary / "inputs"
        inputs.mkdir()
        for item in selection.files:
            source = _source_file(selection, item)
            destination = inputs / Path(item.path).name
            if destination.exists():
                raise MergeError("selected APK filenames collide")
            shutil.copyfile(source, destination)
            if sha256_file(source) != sha256_file(destination):
                raise MergeError("split copy verification failed")
        merged = temporary / "merged.apk"
        command = apkeditor_command(java, apkeditor, inputs, merged)
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise MergeError("APKEditor merge failed") from None
        sanitized_output = _sanitize_output(
            (result.stdout or "") + "\n" + (result.stderr or "")
        )
        if result.returncode != 0:
            raise MergeError("APKEditor merge failed")
        if not merged.is_file() or merged.is_symlink():
            raise MergeError("APKEditor did not produce merged APK")
        merged_metadata = verify_merged_apk(merged, selection)
        with tempfile.NamedTemporaryFile(
            "wb", dir=output.parent, prefix=f".{output.name}-", delete=False
        ) as temporary_output:
            temporary_path = Path(temporary_output.name)
        try:
            shutil.copyfile(merged, temporary_path)
            if sha256_file(merged) != sha256_file(temporary_path):
                raise MergeError("merged APK copy verification failed")
            os.replace(temporary_path, output)
        finally:
            temporary_path.unlink(missing_ok=True)
    _atomic_json(
        output.with_name(MERGE_PROVENANCE),
        {
            "schema_version": 1,
            "package": selection.package,
            "version": {
                "name": selection.version_name,
                "code": selection.version_code,
            },
            "architecture": selection.arch,
            "selected_splits": [item.split_name or "base" for item in selection.files],
            "tool": {
                "name": "APKEditor",
                "version": apkeditor_version,
                "path": str(apkeditor.resolve()),
                "sha256": sha256_file(apkeditor),
            },
            "output": {
                "path": output.name,
                "size": output.stat().st_size,
                "sha256": sha256_file(output),
                "google_signature_retained": False,
                "metadata": {
                    "package": merged_metadata.package,
                    "version_name": merged_metadata.version_name,
                    "version_code": merged_metadata.version_code,
                    "abi": merged_metadata.abi,
                },
            },
            "sanitized_tool_output": sanitized_output,
        },
    )
    return output


def record_signed_merged_stock(
    provenance_path: Path, signed_stock: Path, metadata: ApkMetadata
) -> None:
    if not signed_stock.is_file() or signed_stock.is_symlink():
        raise MergeError("missing signed merged APK")
    try:
        data = json.loads(provenance_path.read_text(encoding="utf-8"))
        output = data["output"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        raise MergeError("invalid merge provenance") from None
    retained = (
        output.get("google_signature_retained") if isinstance(output, dict) else None
    )
    if not isinstance(retained, bool) or retained:
        raise MergeError("invalid merge provenance")
    data["signed_output"] = {
        "path": signed_stock.name,
        "size": signed_stock.stat().st_size,
        "sha256": sha256_file(signed_stock),
        "certificate_sha256": list(metadata.signers_sha256),
        "metadata": {
            "package": metadata.package,
            "version_name": metadata.version_name,
            "version_code": metadata.version_code,
            "abi": metadata.abi,
        },
    }
    _atomic_json(provenance_path, data)


def verify_merged_apk(path: Path, selection: SelectedSplitSet) -> ApkMetadata:
    try:
        validate_zip_archive(path, required=("AndroidManifest.xml",))
        with zipfile.ZipFile(path) as apk:
            if apk.testzip() is not None or "AndroidManifest.xml" not in apk.namelist():
                raise MergeError("merged APK ZIP verification failed")
    except (OSError, zipfile.BadZipFile, IntegrityMetadataError):
        raise MergeError("merged APK ZIP verification failed") from None
    try:
        metadata = inspect_apk(path, path.name, verify_signature=False)
    except IntegrityMetadataError as error:
        raise MergeError(str(error)) from error
    if metadata.split_type != "base" or metadata.package != selection.package:
        raise MergeError("merged APK package mismatch")
    if (
        metadata.version_name != selection.version_name
        or metadata.version_code != selection.version_code
    ):
        raise MergeError("merged APK version mismatch")
    requires_arch = any(
        item.abi and item.abi != "universal" for item in selection.files
    )
    if requires_arch and metadata.abi != selection.arch:
        raise MergeError("merged APK architecture mismatch")
    if metadata.required_splits or metadata.required_split_types:
        raise MergeError("merged APK retains unresolved split requirements")
    return metadata


def _source_file(selection: SelectedSplitSet, item: ApkMetadata) -> Path:
    relative = Path(item.path)
    if relative.is_absolute() or len(relative.parts) != 1 or relative.name != item.path:
        raise MergeError("unsafe selected APK path")
    path = selection.source / relative
    if not path.is_file() or path.is_symlink():
        raise MergeError(f"missing verified APK: {item.path}")
    return path


def _sanitize_output(value: str) -> str:
    sanitized = _URL_RE.sub("<redacted-url>", value)
    sanitized = _SECRET_RE.sub(lambda match: f"{match.group(1)}=<redacted>", sanitized)
    return sanitized.strip()[:16384]


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}-", delete=False
    ) as temporary:
        json.dump(data, temporary, indent=2, sort_keys=True)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, path)
