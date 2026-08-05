from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from .hashing import sha256_file

USER_AGENT = "morphe-tui"
PROVENANCE_NAME = "toolchain-provenance.json"
APKEDITOR_VERSION = "V1.4.9"
SIGNER_VERSION = "v1.3.0"
APKSIGNER_VERSION = "33.0.2"
CHECKSUM_NAME_RE = re.compile(
    r"(?:checksum.*sha256|sha256(?:sum)?s?)(?:[-_.].*)?(?:\.txt)?$", re.IGNORECASE
)
SIGNATURE_NAME_RE = re.compile(
    r"\.(?:asc|sig|pem|crt|sha1|sha256|sha512)$", re.IGNORECASE
)


class ToolchainError(RuntimeError):
    """Raised when external build tools cannot be prepared safely."""


@dataclass(frozen=True)
class PreparedToolchain:
    """Verified tool paths and resolved versions from one provenance record."""

    provenance: Path
    java: Path
    morphe_cli: Path
    morphe_cli_version: str
    patches: Path
    patches_version: str
    apkeditor: Path
    apkeditor_version: str
    signer: Path
    signer_version: str

    @classmethod
    def from_provenance(cls, path: Path) -> PreparedToolchain:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            java = data["java"]
            entries = data["tools"]
            executable = java["executable"]
            java_sha256 = java["sha256"]
        except (KeyError, OSError, TypeError, UnicodeError, json.JSONDecodeError):
            raise ToolchainError("invalid toolchain provenance") from None
        if (
            not isinstance(data, dict)
            or data.get("schema_version") != 1
            or not isinstance(java, dict)
            or not isinstance(executable, str)
            or not isinstance(java_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", java_sha256)
            or not isinstance(entries, list)
        ):
            raise ToolchainError("invalid toolchain provenance")
        java_path = Path(executable)
        if (
            not java_path.is_file()
            or java_path.is_symlink()
            or sha256_file(java_path) != java_sha256
        ):
            raise ToolchainError("Java executable does not match provenance")
        if len(path.parents) < 4:
            raise ToolchainError("invalid toolchain provenance path")
        cache_root = path.parents[3]
        expected_names = {"morphe-cli", "morphe-patches", "apkeditor", "apk-signer"}
        tools: dict[str, tuple[Path, str]] = {}
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or not isinstance(name := entry.get("name"), str)
                or name not in expected_names
                or name in tools
                or not isinstance(raw_path := entry.get("path"), str)
                or not isinstance(tag := entry.get("resolved_tag"), str)
                or not tag
                or not isinstance(size := entry.get("size"), int)
                or isinstance(size, bool)
                or size < 0
                or not isinstance(digest := entry.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
            ):
                raise ToolchainError("invalid toolchain provenance")
            tool = Path(raw_path)
            try:
                tool.resolve().relative_to((cache_root / "tools" / name).resolve())
            except ValueError:
                raise ToolchainError("toolchain asset path is invalid") from None
            if (
                not tool.is_file()
                or tool.is_symlink()
                or tool.stat().st_size != size
                or sha256_file(tool) != digest
            ):
                raise ToolchainError("toolchain asset does not match provenance")
            tools[name] = (tool, tag)
        if set(tools) != expected_names:
            raise ToolchainError("invalid toolchain provenance")
        cli, cli_version = tools["morphe-cli"]
        patches, patches_version = tools["morphe-patches"]
        apkeditor, apkeditor_version = tools["apkeditor"]
        signer, signer_version = tools["apk-signer"]
        return cls(
            path,
            java_path,
            cli,
            cli_version,
            patches,
            patches_version,
            apkeditor,
            apkeditor_version,
            signer,
            signer_version,
        )


@dataclass(frozen=True)
class ToolSpec:
    key: str
    owner: str
    repo: str
    default_version: str
    asset_pattern: re.Pattern[str]


TOOL_SPECS = (
    ToolSpec(
        "morphe-cli",
        "MorpheApp",
        "morphe-desktop",
        "latest",
        re.compile(r"morphe-(?:desktop|cli)-.+-all\.jar"),
    ),
    ToolSpec(
        "morphe-patches",
        "MorpheApp",
        "morphe-patches",
        "latest",
        re.compile(r"patches-.+\.mpp"),
    ),
    ToolSpec(
        "apkeditor",
        "REAndroid",
        "APKEditor",
        APKEDITOR_VERSION,
        re.compile(r"APKEditor-.+\.jar"),
    ),
    ToolSpec(
        "apk-signer",
        "patrickfav",
        "uber-apk-signer",
        SIGNER_VERSION,
        re.compile(r"uber-apk-signer-.+\.jar"),
    ),
)


EMBEDDED_APKSIGNER_RE = re.compile(r"lib/apksigner[-_].+\.jar")


def default_cache_root() -> Path:
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "morphe-tui"
    if os.environ.get("XDG_CACHE_HOME"):
        return Path(os.environ["XDG_CACHE_HOME"]) / "morphe-tui"
    return Path.home() / ".cache" / "morphe-tui"


def require_java(minimum: int = 21) -> dict[str, str | int]:
    executable = shutil.which("java")
    if not executable:
        raise ToolchainError(f"Java {minimum}+ is required but java was not found")
    try:
        result = subprocess.run(
            [executable, "-version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise ToolchainError(f"Java {minimum}+ version check failed") from None
    output = (result.stderr or result.stdout).strip()
    match = re.search(r'version\s+"([^\"]+)"', output) or re.search(
        r"(?:openjdk|java)\s+([0-9][^\s]*)", output, re.IGNORECASE
    )
    if not match:
        raise ToolchainError(f"Java {minimum}+ version could not be determined")
    version = match.group(1)
    try:
        pieces = version.split(".")
        major = int(pieces[1] if pieces[0] == "1" and len(pieces) > 1 else pieces[0])
    except ValueError:
        raise ToolchainError(
            f"Java {minimum}+ version could not be determined"
        ) from None
    if major < minimum:
        raise ToolchainError(
            f"Java {minimum}+ is required; found Java {major} ({version})"
        )
    resolved = Path(executable).resolve()
    return {
        "executable": str(resolved),
        "version": version,
        "major": major,
        "sha256": sha256_file(resolved),
    }


def _atomic_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}-", delete=False
    ) as temporary:
        json.dump(data, temporary, indent=2, sort_keys=True)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, path)


def _release_cache_path(cache_root: Path, spec: ToolSpec, version: str) -> Path:
    key = hashlib.sha256(f"{spec.owner}/{spec.repo}@{version}".encode()).hexdigest()[
        :16
    ]
    return cache_root / "github-releases" / f"{spec.owner}-{spec.repo}-{key}.json"


def _source_spec(spec: ToolSpec, source: str | None) -> ToolSpec:
    if source is None:
        return spec
    owner, separator, repo = source.partition("/")
    if (
        not separator
        or not owner
        or not repo
        or "/" in repo
        or not re.fullmatch(r"[A-Za-z0-9_.-]+", owner)
        or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo)
    ):
        raise ToolchainError(f"invalid repository source for {spec.key}")
    return replace(spec, owner=owner, repo=repo)


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ToolchainError(f"invalid cached metadata: {path.name}") from error
    if not isinstance(value, dict):
        raise ToolchainError(f"invalid cached metadata: {path.name}")
    return value


def github_release(
    spec: ToolSpec,
    version: str,
    cache_root: Path,
    *,
    opener: Callable[..., Any] = urlopen,
) -> dict[str, object]:
    suffix = "latest" if version == "latest" else f"tags/{quote(version, safe='')}"
    endpoint = (
        f"https://api.github.com/repos/{spec.owner}/{spec.repo}/releases/{suffix}"
    )
    headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    cache_path = _release_cache_path(cache_root, spec, version)
    try:
        with opener(Request(endpoint, headers=headers), timeout=30) as response:
            raw = response.read()
        release = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        if not cache_path.is_file() or cache_path.is_symlink():
            raise ToolchainError(
                f"GitHub release lookup failed for {spec.key}"
            ) from None
        release = _read_json(cache_path)
    if not isinstance(release, dict):
        raise ToolchainError(f"invalid GitHub release metadata for {spec.key}")
    _validate_release(spec, release)
    if version != "latest" and release["tag_name"] != version:
        raise ToolchainError(f"GitHub release tag mismatch for {spec.key}")
    _atomic_json(cache_path, release)
    return release


def _validate_release(spec: ToolSpec, release: dict[str, object]) -> None:
    draft = release.get("draft")
    if not isinstance(draft, bool) or draft:
        raise ToolchainError(f"invalid GitHub release metadata for {spec.key}")
    if not isinstance(release.get("tag_name"), str) or not release["tag_name"]:
        raise ToolchainError(f"invalid GitHub release metadata for {spec.key}")
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise ToolchainError(f"invalid GitHub release metadata for {spec.key}")
    for asset in assets:
        if (
            not isinstance(asset, dict)
            or not isinstance(asset.get("name"), str)
            or not isinstance(asset.get("browser_download_url"), str)
            or not isinstance(asset.get("size"), int)
            or isinstance(asset.get("size"), bool)
            or asset["size"] <= 0
        ):
            raise ToolchainError(f"invalid GitHub release metadata for {spec.key}")


def _assets(release: dict[str, object]) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], release["assets"])


def _tag(release: dict[str, object]) -> str:
    return cast(str, release["tag_name"])


def _asset_name(asset: dict[str, object]) -> str:
    return cast(str, asset["name"])


def _asset_url(asset: dict[str, object]) -> str:
    return cast(str, asset["browser_download_url"])


def _asset_size(asset: dict[str, object]) -> int:
    return cast(int, asset["size"])


def select_release_asset(
    spec: ToolSpec, release: dict[str, object]
) -> dict[str, object]:
    matches = [
        asset
        for asset in _assets(release)
        if spec.asset_pattern.fullmatch(_asset_name(asset))
        and not SIGNATURE_NAME_RE.search(_asset_name(asset))
    ]
    if len(matches) != 1:
        raise ToolchainError(
            f"expected exactly one {spec.key} release asset; found {len(matches)}"
        )
    asset = matches[0]
    _validate_asset_url(spec, _tag(release), asset)
    return asset


def _validate_asset_url(spec: ToolSpec, tag: str, asset: dict[str, object]) -> None:
    name = _asset_name(asset)
    parsed = urlparse(_asset_url(asset))
    prefix = f"/{spec.owner}/{spec.repo}/releases/download/{tag}/"
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() != "github.com"
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith(prefix)
        or parsed.path.rsplit("/", 1)[-1] != name
        or "/" in name
        or "\\" in name
        or name in {"", ".", ".."}
    ):
        raise ToolchainError(f"invalid download URL for {spec.key}")


def select_checksum_asset(release: dict[str, object]) -> dict[str, object] | None:
    matches = [
        asset
        for asset in _assets(release)
        if CHECKSUM_NAME_RE.fullmatch(_asset_name(asset))
        and not SIGNATURE_NAME_RE.search(_asset_name(asset))
    ]
    if len(matches) > 1:
        raise ToolchainError("ambiguous SHA-256 checksum assets")
    return matches[0] if matches else None


def parse_sha256_checksum(content: bytes, filename: str) -> str:
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeError as error:
        raise ToolchainError("invalid SHA-256 checksum file") from error
    matches: list[str] = []
    escaped = re.escape(filename)
    path_prefix = r"(?:[^\s/]+/)*"
    patterns = (
        re.compile(rf"^([0-9a-fA-F]{{64}})\s+\*?{path_prefix}{escaped}$"),
        re.compile(
            rf"^SHA256\s*\({path_prefix}{escaped}\)\s*=\s*([0-9a-fA-F]{{64}})$",
            re.IGNORECASE,
        ),
    )
    for line in lines:
        for pattern in patterns:
            match = pattern.fullmatch(line.strip())
            if match:
                matches.append(match.group(1).lower())
    if len(set(matches)) != 1:
        raise ToolchainError(f"missing or ambiguous SHA-256 checksum for {filename}")
    return matches[0]


def _download_bytes(url: str, *, limit: int | None = None, attempts: int = 3) -> bytes:
    if attempts < 1:
        raise ValueError("download attempts must be positive")
    content = b""
    for attempt in range(attempts):
        try:
            with urlopen(
                Request(url, headers={"User-Agent": USER_AGENT}), timeout=60
            ) as response:
                content = response.read(-1 if limit is None else limit + 1)
            break
        except OSError:
            if attempt + 1 == attempts:
                raise ToolchainError("tool asset download failed") from None
    if limit is not None and len(content) > limit:
        raise ToolchainError("tool metadata download exceeds size limit")
    return content


@contextmanager
def cache_lock(path: Path, *, timeout: float = 30.0) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise ToolchainError(f"tool cache is locked: {path.name}") from None
            time.sleep(0.1)
    try:
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        os.close(descriptor)
        descriptor = None
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        path.unlink(missing_ok=True)


def apksigner_jar(cache_root: Path | None = None) -> Path:
    """Extract the `apksigner` JAR bundled inside the cached uber-apk-signer release.

    `apksigner verify --print-certs` reports every signer of a rotated signing
    lineage, while uber-apk-signer's own verify output keeps only the newest one.
    """
    if cache_root is None:
        configured = os.environ.get("MORPHE_APKSIGNER_JAR")
        if configured:
            path = Path(configured)
            if path.is_file() and not path.is_symlink():
                return path
    root = cache_root or default_cache_root()
    candidates = [
        path
        for path in (root / "tools" / "apk-signer").rglob("uber-apk-signer-*.jar")
        if path.parent.name == SIGNER_VERSION
    ]
    if len(candidates) != 1:
        raise ToolchainError(
            "apksigner is unavailable; run 'morphe-tui' first"
        )
    source = candidates[0]
    metadata = _read_json(source.with_name(f"{source.name}.json"))
    if (
        metadata.get("asset") != source.name
        or metadata.get("tag") != SIGNER_VERSION
        or metadata.get("sha256") != sha256_file(source)
    ):
        raise ToolchainError("cached uber-apk-signer failed integrity verification")
    try:
        with zipfile.ZipFile(source) as archive:
            names = [
                name
                for name in archive.namelist()
                if EMBEDDED_APKSIGNER_RE.fullmatch(name)
            ]
            if len(names) != 1:
                raise ToolchainError(
                    f"expected one embedded apksigner JAR; found {len(names)}"
                )
            content = archive.read(names[0])
    except (OSError, zipfile.BadZipFile) as error:
        raise ToolchainError("cannot read cached uber-apk-signer") from error
    target = root / "tools" / "apksigner" / SIGNER_VERSION / "apksigner.jar"
    expected = hashlib.sha256(content).hexdigest()
    if target.is_file() and not target.is_symlink() and sha256_file(target) == expected:
        return target
    with cache_lock(root / "locks" / f"apksigner-{SIGNER_VERSION}.lock"):
        if (
            not target.is_file()
            or target.is_symlink()
            or sha256_file(target) != expected
        ):
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "wb", dir=target.parent, prefix=".apksigner-", delete=False
            ) as temporary:
                temporary.write(content)
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, target)
    return target


def _cached_asset_matches(
    path: Path,
    sidecar: Path,
    *,
    asset: dict[str, object],
    tag: str,
    expected_sha256: str | None,
) -> bool:
    if (
        not path.is_file()
        or path.is_symlink()
        or not sidecar.is_file()
        or sidecar.is_symlink()
    ):
        return False
    try:
        metadata = _read_json(sidecar)
        digest = sha256_file(path)
    except (OSError, ToolchainError):
        return False
    return (
        path.stat().st_size == _asset_size(asset)
        and metadata.get("tag") == tag
        and metadata.get("asset") == _asset_name(asset)
        and metadata.get("source_url") == _asset_url(asset)
        and metadata.get("size") == _asset_size(asset)
        and metadata.get("sha256") == digest
        and (expected_sha256 is None or digest == expected_sha256)
    )


def cache_release_asset(
    spec: ToolSpec,
    release: dict[str, object],
    cache_root: Path,
    *,
    expected_sha256: str | None = None,
) -> dict[str, object]:
    asset = select_release_asset(spec, release)
    checksum_asset = (
        None if expected_sha256 is not None else select_checksum_asset(release)
    )
    upstream_sha256 = None
    tag = _tag(release)
    name = _asset_name(asset)
    url = _asset_url(asset)
    size = _asset_size(asset)
    if expected_sha256 is None and checksum_asset:
        _validate_asset_url(spec, tag, checksum_asset)
        checksum = _download_bytes(_asset_url(checksum_asset), limit=1024 * 1024)
        if len(checksum) != _asset_size(checksum_asset):
            raise ToolchainError("downloaded checksum size mismatch")
        upstream_sha256 = parse_sha256_checksum(checksum, name)
        expected_sha256 = upstream_sha256
    directory = cache_root / "tools" / spec.key / f"{spec.owner}-{spec.repo}" / tag
    path = directory / name
    sidecar = directory / f"{name}.json"
    lock = (
        cache_root
        / "locks"
        / f"{spec.key}-{hashlib.sha256(f'{spec.owner}/{spec.repo}@{tag}'.encode()).hexdigest()[:16]}.lock"
    )
    with cache_lock(lock):
        reused = _cached_asset_matches(
            path,
            sidecar,
            asset=asset,
            tag=tag,
            expected_sha256=expected_sha256,
        )
        if not reused:
            directory.mkdir(parents=True, exist_ok=True)
            content = _download_bytes(url, limit=size)
            if len(content) != size:
                raise ToolchainError(f"downloaded size mismatch for {spec.key}")
            digest = hashlib.sha256(content).hexdigest()
            if expected_sha256 and digest != expected_sha256:
                raise ToolchainError(f"downloaded SHA-256 mismatch for {spec.key}")
            with tempfile.NamedTemporaryFile(
                "wb", dir=directory, prefix=f".{name}-", delete=False
            ) as temporary:
                temporary.write(content)
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, path)
            _atomic_json(
                sidecar,
                {
                    "asset": name,
                    "sha256": digest,
                    "size": len(content),
                    "source_url": url,
                    "tag": tag,
                    "upstream_sha256": upstream_sha256,
                    "expected_sha256": expected_sha256,
                },
            )
    digest = sha256_file(path)
    return {
        "name": spec.key,
        "repository": f"{spec.owner}/{spec.repo}",
        "resolved_tag": tag,
        "asset": name,
        "path": str(path.resolve()),
        "size": path.stat().st_size,
        "sha256": digest,
        "upstream_sha256": upstream_sha256,
        "expected_sha256": expected_sha256,
        "source_url": url,
    }


def prepare_toolchain(
    cache_root: Path,
    versions: dict[str, str],
    sources: dict[str, str] | None = None,
    *,
    patches_sha256: str | None = None,
) -> Path:
    java = require_java()
    tools = []
    sources = sources or {}
    for default_spec in TOOL_SPECS:
        spec = _source_spec(default_spec, sources.get(default_spec.key))
        requested = versions.get(spec.key, spec.default_version)
        release = github_release(spec, requested, cache_root)
        prepared = cache_release_asset(
            spec,
            release,
            cache_root,
            expected_sha256=patches_sha256 if spec.key == "morphe-patches" else None,
        )
        prepared["requested_version"] = requested
        tools.append(prepared)
    by_name = {tool["name"]: tool for tool in tools}
    cli = by_name["morphe-cli"]
    patches = by_name["morphe-patches"]
    provenance = {"schema_version": 1, "java": java, "tools": tools}
    path = (
        cache_root
        / "toolchains"
        / f"{cli['repository'].replace('/', '-')}@{cli['resolved_tag']}"
        / f"{patches['repository'].replace('/', '-')}@{patches['resolved_tag']}"
        / PROVENANCE_NAME
    )
    _atomic_json(path, provenance)
    return path
