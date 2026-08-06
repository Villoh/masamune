from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from ..apk import ApkMetadata, verify_apk_set
from ..errors import ApkMismatch, IntegrityMetadataError
from ..hashing import sha256_file
from .contract import ProviderRequest, ProviderResult
from .errors import ProviderArtifactMismatch, ProviderUnavailable, VersionUnavailable

MAX_FILES = 20
MAX_APKM_FILES = 64
MAX_FILE_SIZE = 300 * 1024 * 1024
DOWNLOAD_ATTEMPTS = 3
USER_AGENT = "masamune"


@dataclass(frozen=True)
class UrlProvider:
    name: str
    urls: Sequence[str]
    opener: Callable[..., Any] = urlopen

    def download(self, request: ProviderRequest) -> ProviderResult:
        return download_urls(request, self.name, self.urls, opener=self.opener)


def download_urls(
    request: ProviderRequest,
    provider: str,
    urls: Sequence[str],
    *,
    opener: Callable[..., Any] = urlopen,
) -> ProviderResult:
    if provider != "direct":
        raise ValueError(f"unsupported URL provider: {provider}")
    return _download_assets(request, provider, urls, opener=opener)


def _download_assets(
    request: ProviderRequest,
    provider: str,
    urls: Sequence[str],
    *,
    bundles: Sequence[bool] | None = None,
    opener: Callable[..., Any] = urlopen,
) -> ProviderResult:
    if bundles is not None and len(bundles) != len(urls):
        raise ValueError("bundle metadata must match provider URLs")
    if not urls:
        raise ProviderUnavailable(f"{provider} source is not configured")
    if len(urls) > MAX_FILES:
        raise IntegrityMetadataError("provider returned too many files")
    if request.output.exists() or request.output.is_symlink():
        raise FileExistsError(f"refusing to overwrite trusted output: {request.output}")
    request.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        dir=request.output.parent, prefix=f".{request.output.name}-{provider}-"
    ) as raw:
        temporary = Path(raw)
        sources: list[dict[str, object]] = []
        names: set[str] = set()
        for index, value in enumerate(urls):
            url = _validated_url(provider, value)
            bundle = bundles[index] if bundles is not None else False
            name = f"provider-{index}.apkm" if bundle else _filename(url, index)
            if name in names:
                raise IntegrityMetadataError("duplicate provider filename")
            names.add(name)
            destination = temporary / name
            size, digest = _download(
                url,
                destination,
                opener=opener,
                redirect_provider=provider if provider == "apkmirror" else None,
            )
            if bundle:
                _extract_bundle(destination, temporary, provider)
                destination.unlink()
            sources.append(
                {
                    "source": _sanitized_source(url),
                    "filename": name,
                    "size": size,
                    "sha256": digest,
                }
            )
        try:
            metadata = verify_apk_set(
                temporary,
                request.package,
                version_name=request.version_name,
                version_code=request.version_code,
                arch=request.arch,
                expected_signer=request.expected_signer,
            )
        except ApkMismatch as error:
            raise ProviderArtifactMismatch(str(error)) from None
        provenance = _provenance(request, provider, sources, metadata, temporary)
        _write_json(temporary / "provenance.json", provenance)
        os.replace(temporary, request.output)
    return ProviderResult(provider, request.output, request.output / "provenance.json")


def _extract_bundle(source: Path, destination: Path, provider: str) -> None:
    try:
        archive = zipfile.ZipFile(source)
    except (OSError, zipfile.BadZipFile):
        raise IntegrityMetadataError(
            f"{provider} bundle is not a ZIP archive"
        ) from None
    with archive:
        apk_members = []
        for member in archive.infolist():
            path = PurePosixPath(member.filename)
            if (
                "\\" in member.filename
                or path.is_absolute()
                or re.match(r"^[A-Za-z]:", member.filename)
                or ".." in path.parts
            ):
                raise IntegrityMetadataError(
                    f"{provider} bundle has unsafe member path"
                )
            if not member.is_dir() and member.filename.lower().endswith(".apk"):
                apk_members.append(member)
        if not apk_members or len(apk_members) > MAX_APKM_FILES:
            raise IntegrityMetadataError(
                f"{provider} bundle has invalid APK file count"
            )
        total = 0
        names: set[str] = set()
        for member in apk_members:
            name = PurePosixPath(member.filename).name
            if name in names:
                raise IntegrityMetadataError(
                    f"{provider} bundle has duplicate APK names"
                )
            names.add(name)
            if member.file_size > MAX_FILE_SIZE - total:
                raise IntegrityMetadataError(f"{provider} bundle exceeds size limit")
            total += member.file_size
            with (
                archive.open(member) as input,
                (destination / name).open("xb") as output,
            ):
                while chunk := input.read(1024 * 1024):
                    output.write(chunk)


def _validated_url(provider: str, value: str) -> str:
    if not isinstance(value, str) or len(value) > 4096:
        raise IntegrityMetadataError("invalid provider URL")
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise IntegrityMetadataError("invalid provider URL")
    if provider == "apkmirror" and not (
        host == "apkmirror.com" or host.endswith(".apkmirror.com")
    ):
        raise IntegrityMetadataError("APKMirror requires explicit APKMirror asset URLs")
    return value


def _filename(url: str, index: int) -> str:
    name = unquote(urlparse(url).path.rsplit("/", 1)[-1])
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or not name.lower().endswith(".apk")
        or not re.fullmatch(r"[A-Za-z0-9._+-]+", name)
    ):
        name = f"provider-{index}.apk"
    return name


def _download(
    url: str,
    destination: Path,
    *,
    opener: Callable[..., Any],
    redirect_provider: str | None = None,
) -> tuple[int, str]:
    for attempt in range(DOWNLOAD_ATTEMPTS):
        digest = hashlib.sha256()
        size = 0
        try:
            with (
                opener(
                    Request(url, headers={"User-Agent": USER_AGENT}), timeout=60
                ) as response,
                destination.open("xb") as output,
            ):
                final_url = getattr(response, "geturl", lambda: url)()
                if redirect_provider:
                    parsed = urlparse(final_url)
                    host = (parsed.hostname or "").lower()
                    allowed = (
                        host == "apkmirror.com"
                        or host.endswith(
                            (".apkmirror.com", ".r2.cloudflarestorage.com")
                        )
                        if redirect_provider == "apkmirror"
                        else False
                    )
                    if (
                        parsed.scheme != "https"
                        or parsed.username is not None
                        or parsed.password is not None
                        or parsed.fragment
                        or not allowed
                    ):
                        raise IntegrityMetadataError(
                            f"{redirect_provider} redirected to untrusted host: {host or '<missing>'}"
                        )
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_FILE_SIZE:
                        raise IntegrityMetadataError("provider file exceeds size limit")
                    digest.update(chunk)
                    output.write(chunk)
        except HTTPError as error:
            destination.unlink(missing_ok=True)
            if error.code in {404, 410}:
                raise VersionUnavailable("provider version is unavailable") from None
            if attempt + 1 == DOWNLOAD_ATTEMPTS:
                raise ProviderUnavailable("provider request failed") from None
            continue
        except (OSError, URLError):
            destination.unlink(missing_ok=True)
            if attempt + 1 == DOWNLOAD_ATTEMPTS:
                raise ProviderUnavailable("provider request failed") from None
            continue
        if size == 0:
            destination.unlink(missing_ok=True)
            raise IntegrityMetadataError("provider returned empty file")
        return size, digest.hexdigest()
    raise ProviderUnavailable("provider request failed")


def _sanitized_source(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.hostname}{parsed.path}"


def _provenance(
    request: ProviderRequest,
    provider: str,
    sources: list[dict[str, object]],
    metadata: list[ApkMetadata],
    directory: Path,
) -> dict[str, object]:
    base = next(item for item in metadata if item.split_type == "base")
    return {
        "schema_version": 1,
        "provider": provider,
        "package": request.package,
        "version": {"name": base.version_name, "code": base.version_code},
        "architecture": request.arch,
        "source_metadata_trusted": False,
        "sources": sources,
        "files": [
            asdict(item) for item in sorted(metadata, key=lambda item: item.path)
        ],
        "artifacts": [
            {
                "path": artifact.name,
                "size": artifact.stat().st_size,
                "sha256": sha256_file(artifact),
            }
            for artifact in sorted(directory.glob("*.apk"))
        ],
    }


def _write_json(path: Path, data: object) -> None:
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}-", delete=False
    ) as temporary:
        json.dump(data, temporary, indent=2, sort_keys=True)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, path)
