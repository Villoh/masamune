from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path

from ..errors import IntegrityMetadataError
from ..hashing import sha256_file
from .contract import Provider, ProviderRequest, ProviderResult
from .errors import (
    ProviderAmbiguous,
    ProviderFallbackError,
    ProviderUnavailable,
    VersionUnavailable,
)
from .urls import UrlProvider


def fallback_download(
    request: ProviderRequest,
    providers: Sequence[Provider],
) -> ProviderResult:
    cached = _cached_result(request, providers)
    if cached is not None:
        return cached
    failures: list[str] = []
    for provider in providers:
        try:
            result = provider.download(request)
        except ProviderFallbackError as error:
            failures.append(f"{provider.name}: {_fallback_reason(error)}")
            continue
        if result.provider != provider.name:
            raise IntegrityMetadataError("provider result identity mismatch")
        return result
    raise VersionUnavailable(
        f"version unavailable from all providers: {request.package} "
        f"{request.version_name} ({'; '.join(failures)})"
    )


def _cached_result(
    request: ProviderRequest, providers: Sequence[Provider]
) -> ProviderResult | None:
    if not request.output.exists():
        return None
    if request.output.is_symlink():
        raise IntegrityMetadataError("trusted output directory must not be a symlink")
    provenance = request.output / "provenance.json"
    if not provenance.is_file() or provenance.is_symlink():
        raise IntegrityMetadataError("trusted output provenance is missing")
    try:
        data = json.loads(provenance.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise IntegrityMetadataError("trusted output provenance is invalid") from None
    if not isinstance(data, dict) or data.get("schema_version") not in (1, 2):
        raise IntegrityMetadataError("trusted output provenance is invalid")
    provider = data.get("provider")
    if provider == "local" and data.get("schema_version") == 2:
        provider = "google-play"
    if not isinstance(provider, str) or provider not in {
        item.name for item in providers
    }:
        raise IntegrityMetadataError("trusted output provenance has invalid provider")
    version = data.get("version")
    if (
        data.get("package") != request.package
        or data.get("architecture") != request.arch
        or not isinstance(version, dict)
        or (
            request.version_name is not None
            and version.get("name") != request.version_name
        )
        or (
            request.version_code is not None
            and version.get("code") != request.version_code
        )
    ):
        raise IntegrityMetadataError("trusted output provenance does not match request")
    # Google Play's own writer (schema 2) records each file under "files" with
    # a "normalized_filename", while every other provider (schema 1) records a
    # simpler "artifacts" list keyed by "path". Same shape once read.
    name_key = "path" if data["schema_version"] == 1 else "normalized_filename"
    artifacts = data.get("artifacts" if data["schema_version"] == 1 else "files")
    if not isinstance(artifacts, list) or not artifacts:
        raise IntegrityMetadataError("trusted output provenance has invalid artifacts")
    names: set[str] = set()
    for artifact in artifacts:
        if (
            not isinstance(artifact, dict)
            or not isinstance(name := artifact.get(name_key), str)
            or Path(name).name != name
            or name in names
            or not isinstance(size := artifact.get("size"), int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(digest := artifact.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise IntegrityMetadataError(
                "trusted output provenance has invalid artifacts"
            )
        names.add(name)
        artifact_path = request.output / name
        if (
            not artifact_path.is_file()
            or artifact_path.is_symlink()
            or artifact_path.stat().st_size != size
            or sha256_file(artifact_path) != digest
        ):
            raise IntegrityMetadataError(
                "trusted output artifact does not match provenance"
            )
    try:
        children = {child.name for child in request.output.iterdir()}
    except OSError:
        raise IntegrityMetadataError("trusted output directory is unreadable") from None
    allowed = {*names, provenance.name}
    if (
        data["schema_version"] == 2
        and (request.output / ".goopdl-integrity.json").exists()
    ):
        allowed.add(".goopdl-integrity.json")
    if children != allowed:
        raise IntegrityMetadataError("trusted output contains unprovenanced files")
    return ProviderResult(provider, request.output, provenance)


def providers_for(
    google_play: Provider,
    *,
    direct: Sequence[str],
    apkmirror: Sequence[str],
) -> tuple[Provider, ...]:
    from .apkmirror import ApkMirrorProvider

    return (
        google_play,
        ApkMirrorProvider(tuple(apkmirror)),
        UrlProvider("direct", direct),
    )


def _fallback_reason(error: ProviderFallbackError) -> str:
    if isinstance(error, ProviderAmbiguous):
        return "ambiguous source"
    if isinstance(error, ProviderUnavailable):
        return "provider unavailable"
    return "requested version unavailable"
