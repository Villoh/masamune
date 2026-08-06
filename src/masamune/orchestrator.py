from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from threading import Event
from typing import Any

from .apk import (  # pyright: ignore[reportMissingImports]
    EXPECTED_SIGNERS,
    ApkMetadata,
    verify_apk_set,
)
from .architecture import Architecture  # pyright: ignore[reportMissingImports]
from .build import (
    AUTO_KEYSTORE_PASSWORD,
    BuildError,
    build_apk,
    ensure_user_keystore,
    expand_build_jobs,
    sign_apk,
)
from .bundles import (  # pyright: ignore[reportMissingImports]
    load_bundle_catalog,
    load_community_bundles,
)
from .cli import redact  # pyright: ignore[reportMissingImports]
from .compatibility import (
    confirm_version_code,
    parse_patch_list,
    resolve_version_code_candidate,
    run_morphe_action,
    run_morphe_compatibility,
)
from .config import (
    FallbackConfig,
    GooglePlayConfig,
    ToolchainConfig,
    app_include_experimental_versions,
    app_include_universal_patches,
    app_toolchain,
    load_config,
)
from .errors import (  # pyright: ignore[reportMissingImports]
    BuildCancelled,
    IntegrityMetadataError,
)
from .merge import (
    merge_splits,
    record_signed_merged_stock,
    select_splits,
    write_selection_manifest,
)
from .module import build_module
from .paths import default_download_path
from .providers import (
    ProviderRequest,
    ProviderResult,
    VersionUnavailable,
    fallback_download,
    providers_for,
)
from .providers.apkmirror import (
    resolve_apkmirror_version_code,
    resolve_apkmirror_version_code_for_package,
)
from .providers.errors import ProviderAmbiguous, ProviderUnavailable
from .providers.google_play import GooglePlayProvider
from .release_notes import render_release_notes
from .toolchain import (
    PreparedToolchain,
    ToolchainError,
    apksigner_jar,
    prepare_toolchain,
)


@dataclass(frozen=True)
class BuildResult:
    package: str
    version_name: str
    version_code: str
    architecture: str
    artifacts: tuple[str, ...]
    certificate_sha256: tuple[str, ...] = ()
    name: str = ""
    patches_repository: str = ""
    patches_tag: str = ""
    selected_patches: tuple[str, ...] = ()
    root_patches: tuple[str, ...] = ()
    provider: str = ""


_SENSITIVE_FIELD_NAMES = frozenset(
    {"authorization", "cookie", "dispenser", "password", "secret", "token"}
)


def _is_sensitive_field(name: object) -> bool:
    return any(
        part in _SENSITIVE_FIELD_NAMES
        for part in str(name).lower().replace("_", "-").split("-")
    )


def _sanitize_event_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            redact(str(key)): (
                "<redacted>"
                if _is_sensitive_field(key)
                else _sanitize_event_value(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_sanitize_event_value(item) for item in value]
    return redact(str(value))


class Reporter:
    def __init__(
        self,
        json_output: bool = False,
        sink: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self.json_output = json_output
        self.sink = sink
        self.log_path: Path | None = None

    def set_log_path(self, path: Path | None) -> None:
        self.log_path = path

    def event(self, event: str, message: str, **fields: object) -> None:
        safe = {
            redact(key): (
                "<redacted>"
                if _is_sensitive_field(key)
                else _sanitize_event_value(value)
            )
            for key, value in fields.items()
        }
        record = {
            "event": redact(event),
            "message": redact(message),
            **safe,
        }
        suffix = " ".join(f"{key}={value}" for key, value in safe.items())
        line = (
            f"[{record['event']}] {record['message']}{(' ' + suffix) if suffix else ''}"
        )
        if self.log_path is not None:
            with suppress(OSError), self.log_path.open("a", encoding="utf-8") as log:
                log.write(line + "\n")
        if self.sink is not None:
            with suppress(Exception):
                self.sink(record)
            return
        if self.json_output:
            print(json.dumps(record, sort_keys=True), file=sys.stderr)
        else:
            print(line, file=sys.stderr)


def run_download(
    request: ProviderRequest,
    *,
    cache: Path,
    google_play: GooglePlayConfig,
    fallbacks: FallbackConfig,
    reporter: Reporter | None = None,
    cancel_event: Event | None = None,
    provider_name: str = "automatic",
) -> ProviderResult:
    """Download and publish one verified APK set outside build orchestration."""
    reporter = reporter or Reporter()
    if cancel_event is not None and cancel_event.is_set():
        raise BuildCancelled("download cancelled by user")
    if provider_name not in {"automatic", "google-play", "apkmirror", "direct"}:
        raise ValueError(f"unsupported provider: {provider_name}")
    candidate = (
        resolve_version_code_candidate(
            cache,
            package=request.package,
            version_name=request.version_name or "",
            arch=request.arch,
            profile=google_play.profile,
            region=google_play.country,
            explicit_version_code=request.version_code,
            fallback_metadata=(
                lambda *_: _apkmirror_version_code_hint(
                    fallbacks.apkmirror,
                    version_name=request.version_name or "",
                    arch=request.arch,
                    reporter=reporter,
                    package=request.package,
                )
            ),
        )
        if request.version_name is not None
        and provider_name in {"automatic", "google-play"}
        else None
    )
    resolved_request = ProviderRequest(
        request.package,
        request.version_name,
        candidate.version_code if candidate is not None else request.version_code,
        request.arch,
        request.output,
        request.expected_signer,
    )
    reporter.event(
        "download",
        "preparing verified stock",
        package=request.package,
        arch=request.arch,
        destination=request.output,
    )
    available_providers = providers_for(
        GooglePlayProvider(
            profile=google_play.profile,
            region=google_play.country,
            dispenser=google_play.dispenser,
            proxy=google_play.proxy,
        ),
        direct=fallbacks.direct,
        apkmirror=fallbacks.apkmirror,
    )
    providers = (
        available_providers
        if provider_name == "automatic"
        else tuple(item for item in available_providers if item.name == provider_name)
    )
    provider = fallback_download(resolved_request, providers)
    if cancel_event is not None and cancel_event.is_set():
        raise BuildCancelled("download cancelled by user")
    source = _read_json(provider.provenance, "source provenance")
    version = source.get("version")
    if (
        not isinstance(version, dict)
        or not isinstance(version.get("name"), str)
        or (
            request.version_name is not None
            and version.get("name") != request.version_name
        )
        or not isinstance(version.get("code"), str)
    ):
        raise IntegrityMetadataError("source provenance version mismatch")
    version_name = version["name"]
    version_code = version["code"]
    if request.version_name is not None:
        confirm_version_code(
            cache,
            package=request.package,
            version_name=version_name,
            arch=request.arch,
            profile=google_play.profile,
            region=google_play.country,
            version_code=version_code,
        )
    metadata = verify_apk_set(
        provider.directory,
        request.package,
        version_name=request.version_name,
        version_code=version_code,
        arch=request.arch,
        expected_signer=request.expected_signer,
    )
    reporter.event(
        "download",
        "trusted stock acquired",
        package=request.package,
        arch=request.arch,
        provider=provider.provider,
        version=version.get("name"),
        version_code=version_code,
        files=len(metadata),
    )
    return provider


def _downloaded_source_directory(job: Any, root: Path) -> Path | None:
    architecture = Architecture.from_config(job.arch)
    app_root = root / job.app.slug
    if not app_root.is_dir() or app_root.is_symlink():
        return None
    candidates: list[Path] = []
    for version_root in app_root.iterdir():
        if not version_root.is_dir() or version_root.is_symlink():
            continue
        if job.app.version != "auto" and version_root.name != job.app.version:
            continue
        path = version_root / architecture.value
        provenance = path / "provenance.json"
        if not path.is_dir() or path.is_symlink() or not provenance.is_file():
            continue
        try:
            data = _read_json(provenance, "download provenance")
            version = data.get("version")
            if (
                data.get("package") != job.app.package
                or not isinstance(version, dict)
                or version.get("name") != version_root.name
                or not isinstance(version.get("code"), str)
            ):
                continue
            verify_apk_set(
                path,
                job.app.package,
                version_name=version_root.name,
                version_code=version["code"],
                arch=architecture.goopdl,
                expected_signer=job.app.expected_signer,
            )
        except (IntegrityMetadataError, OSError, ValueError):
            continue
        candidates.append(path)
    if len(candidates) > 1:
        raise BuildError(
            f"multiple verified downloads found for {job.app.package} {architecture.value}; "
            "configure source-dir or remove extra versions"
        )
    return candidates[0] if candidates else None


def _source_directory(job: Any, *, download_root: Path | None = None) -> Path:
    template = job.app.source_dir
    if not template:
        if download_root is not None:
            for root in (download_root, download_root.parent):
                source = _downloaded_source_directory(job, root)
                if source is not None:
                    return source
        architecture = Architecture.from_config(job.arch)
        raise BuildError(
            f"local APK directory required for {job.app.package} "
            f"({architecture.value}); no verified download found"
        )
    architecture = Architecture.from_config(job.arch)
    value = template.format(
        arch=architecture.value,
        abi=architecture.android_abi,
        module=architecture.module,
    )
    path = Path(value).expanduser()
    if not path.is_dir() or path.is_symlink():
        raise BuildError(f"local APK directory not found: {path}")
    return path


def _local_version_name(job: Any, *, download_root: Path | None = None) -> str:
    source = _source_directory(job, download_root=download_root)
    metadata = verify_apk_set(
        source,
        job.app.package,
        version_name=None,
        version_code=None,
        arch=Architecture.from_config(job.arch).goopdl,
        expected_signer=job.app.expected_signer,
    )
    base = next(item for item in metadata if item.split_type == "base")
    return base.version_name


def run_build(
    args: argparse.Namespace,
    *,
    reporter: Reporter | None = None,
    cancel_event: Event | None = None,
) -> dict[str, object]:
    reporter = reporter or Reporter()
    config = load_config(args.config)
    if args.output.exists() or args.output.is_symlink():
        raise FileExistsError(f"refusing to overwrite build output: {args.output}")
    # Freeze relative CLI/TUI paths before worker execution; signing tools run
    # outside UI context and must not depend on a later working directory.
    keystore = (
        args.keystore.expanduser().resolve() if args.keystore is not None else None
    )
    if keystore is None:
        keystore = args.cache / "masamune.p12"
        ensure_user_keystore(keystore, alias=args.keystore_alias)
        password = AUTO_KEYSTORE_PASSWORD
    else:
        password = os.environ.get("MORPHE_KEYSTORE_PASSWORD")
        if not password:
            raise BuildError("MORPHE_KEYSTORE_PASSWORD is required")
        if not keystore.is_file() or keystore.is_symlink():
            raise BuildError("missing or invalid builder keystore")
    results: list[BuildResult] = []
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".m-", dir=args.output.parent) as raw:
        staging = Path(raw) / "artifacts"
        staging.mkdir()
        reporter.set_log_path(staging / "build.log")
        reporter.event("tools", "preparing toolchains")
        toolchains = _prepare_toolchains(args.cache, config)
        os.environ["MORPHE_APKSIGNER_JAR"] = str(apksigner_jar(args.cache))
        for job in expand_build_jobs(config, args.output):
            if cancel_event is not None and cancel_event.is_set():
                raise BuildCancelled("build cancelled by user")
            toolchain = _toolchain(toolchains[app_toolchain(job.app, config.toolchain)])
            source_version = _local_version_name(
                job, download_root=default_download_path()
            )
            reporter.event(
                "resolve",
                "resolving compatible version",
                package=job.app.package,
                arch=job.arch,
                version=source_version,
            )
            compatibility = run_morphe_compatibility(
                toolchain.java,
                toolchain.morphe_cli,
                toolchain.patches,
                job.app.package,
                include=job.app.include_patches,
                exclude=job.app.exclude_patches,
                exclusive=job.app.exclusive_patches,
                requested_version=source_version,
                include_universal_patches=app_include_universal_patches(
                    job.app, config.toolchain
                ),
                include_experimental_versions=app_include_experimental_versions(
                    job.app, config.toolchain
                ),
            )
            result = _build_job(
                job,
                compatibility.selected_version,
                selected_patches=compatibility.selected_patches,
                staging=staging,
                cache=args.cache,
                toolchain=toolchain,
                keystore=keystore,
                alias=args.keystore_alias,
                password=password,
                reporter=reporter,
            )
            results.append(
                replace(
                    result,
                    selected_patches=compatibility.selected_patches,
                    root_patches=tuple(
                        patch
                        for patch in compatibility.selected_patches
                        if patch != "GmsCore support"
                    ),
                )
            )
        if not results:
            raise BuildError("no app/architecture could be built")
        summary = _summary(results)
        _write_summary(staging, summary)
        os.replace(staging, args.output)
    reporter.set_log_path(args.output / "build.log")
    reporter.event("complete", "build completed", jobs=len(results), output=args.output)
    return summary


@dataclass(frozen=True)
class _BuildContext:
    source_package: str
    slug: str
    patched_package: str
    arch: Architecture
    java: Path
    cli: Path
    patches: Path
    signer: Path
    toolchain_provenance: Path
    merged_stock: Path
    source_provenance: Path
    merge_provenance: Path
    output_directory: Path
    version_name: str
    version_code: str
    keystore: Path
    keystore_alias: str
    keystore_password: str = field(repr=False)
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    exclusive: tuple[str, ...] = ()
    options: Any = None
    stage_reporter: Callable[[str, str], None] | None = None


def _build_job(
    job: Any,
    version_name: str,
    *,
    selected_patches: Sequence[str] | None = None,
    staging: Path,
    cache: Path,
    toolchain: PreparedToolchain,
    keystore: Path,
    alias: str,
    password: str,
    reporter: Reporter,
) -> BuildResult:
    architecture = Architecture.from_config(job.arch)
    internal_arch = architecture.goopdl
    slug = job.app.slug
    provider, metadata, version_code = _obtain_verified_source(
        job,
        version_name,
        cache=cache,
        internal_arch=internal_arch,
        trusted=cache / "trusted" / slug / architecture.value / version_name,
        reporter=reporter,
    )
    work = staging / slug / architecture.value
    signed_stock, merge_provenance = _merge_and_sign_stock(
        job,
        version_name,
        version_code,
        source_directory=provider.directory,
        metadata=metadata,
        work=work,
        java=toolchain.java,
        apkeditor=toolchain.apkeditor,
        apkeditor_version=toolchain.apkeditor_version,
        signer=toolchain.signer,
        keystore=keystore,
        alias=alias,
        password=password,
        internal_arch=internal_arch,
        reporter=reporter,
    )
    context = _BuildContext(
        source_package=job.app.package,
        slug=slug,
        patched_package=job.app.patched_package,
        arch=architecture,
        java=toolchain.java,
        cli=toolchain.morphe_cli,
        patches=toolchain.patches,
        signer=toolchain.signer,
        toolchain_provenance=toolchain.provenance,
        merged_stock=signed_stock,
        source_provenance=provider.provenance,
        merge_provenance=merge_provenance,
        output_directory=work,
        version_name=version_name,
        version_code=version_code,
        keystore=keystore,
        keystore_alias=alias,
        keystore_password=password,
        include=(() if selected_patches is not None else job.app.include_patches),
        exclude=() if selected_patches is not None else job.app.exclude_patches,
        exclusive=(
            tuple(selected_patches)
            if selected_patches is not None
            else job.app.exclusive_patches
        ),
        options=job.app.patch_options,
        stage_reporter=lambda stage, mode: reporter.event(
            stage,
            (
                f"applying Morphe patches ({mode} APK)"
                if stage == "patch"
                else f"signing patched APK ({mode} APK)"
            ),
            package=job.app.package,
            arch=job.arch,
        ),
    )
    artifacts = _build_artifacts(context, job.app.build_mode)
    patches_repository, patches_tag = _patch_release(toolchain.provenance)
    return BuildResult(
        job.app.package,
        version_name,
        version_code,
        architecture.value,
        tuple(str(artifact.relative_to(staging)) for artifact in artifacts),
        metadata[0].signers_sha256,
        job.app.name,
        patches_repository,
        patches_tag,
        provider=provider.provider,
    )


def _obtain_verified_source(
    job: Any,
    version_name: str,
    *,
    cache: Path,
    internal_arch: str,
    trusted: Path,
    reporter: Reporter,
) -> tuple[ProviderResult, list[ApkMetadata], str]:
    del trusted
    source_directory = _source_directory(job, download_root=default_download_path())
    reporter.event(
        "source",
        "verifying local APK set",
        package=job.app.package,
        arch=job.arch,
        path=source_directory,
    )
    metadata = verify_apk_set(
        source_directory,
        job.app.package,
        version_name=version_name,
        version_code=job.app.version_code,
        arch=internal_arch,
        expected_signer=job.app.expected_signer,
    )
    base = next(item for item in metadata if item.split_type == "base")
    provenance = cache / "local-sources" / job.app.slug / internal_arch / version_name
    provenance.mkdir(parents=True, exist_ok=True)
    provenance_path = provenance / "source-provenance.json"
    provenance_path.write_text(
        json.dumps(
            {
                "provider": "local",
                "directory": str(source_directory),
                "package": base.package,
                "version": {"name": base.version_name, "code": base.version_code},
                "architecture": internal_arch,
                "files": [item.path for item in metadata],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    reporter.event(
        "source",
        "local APK set verified",
        package=job.app.package,
        arch=job.arch,
        version=base.version_name,
        version_code=base.version_code,
        files=len(metadata),
    )
    if job.app.package not in EXPECTED_SIGNERS and not job.app.expected_signer:
        reporter.event(
            "signer",
            "unpinned signer accepted",
            package=job.app.package,
            certificate_sha256=",".join(metadata[0].signers_sha256),
        )
    return (
        ProviderResult("local", source_directory, provenance_path),
        metadata,
        base.version_code,
    )


def _merge_and_sign_stock(
    job: Any,
    version_name: str,
    version_code: str,
    *,
    source_directory: Path,
    metadata: list[ApkMetadata],
    work: Path,
    java: Path | str,
    apkeditor: Path,
    apkeditor_version: str,
    signer: Path,
    keystore: Path,
    alias: str,
    password: str,
    internal_arch: str,
    reporter: Reporter,
) -> tuple[Path, Path]:
    selection = select_splits(
        source_directory, metadata, arch=internal_arch, density=job.app.density
    )
    work.mkdir(parents=True, exist_ok=True)
    write_selection_manifest(selection, work)
    reporter.event(
        "merge", "merging selected split APKs", package=job.app.package, arch=job.arch
    )
    stock = merge_splits(
        selection,
        work / f"{job.app.slug}-{version_name}-{job.arch}-stock.apk",
        java=java,
        apkeditor=apkeditor,
        apkeditor_version=apkeditor_version,
    )
    reporter.event(
        "sign", "signing merged stock", package=job.app.package, arch=job.arch
    )
    signed_stock = work / f"{job.app.slug}-{version_name}-{job.arch}-stock-signed.apk"
    signed_metadata = sign_apk(
        input_apk=stock,
        output=signed_stock,
        package=job.app.package,
        version_name=version_name,
        version_code=version_code,
        arch=job.arch,
        java=java,
        signer=signer,
        keystore=keystore,
        keystore_alias=alias,
        keystore_password=password,
    )
    merge_provenance = work / "merge-provenance.json"
    record_signed_merged_stock(merge_provenance, signed_stock, signed_metadata)
    return signed_stock, merge_provenance


def _build_artifacts(context: _BuildContext, build_mode: str) -> list[Path]:
    artifacts: list[Path] = []
    if build_mode in {"apk", "both"}:
        artifacts.append(_build_apk(context))
    if build_mode in {"module", "both"}:
        root_apk = _build_apk(context, root=True)
        artifacts.extend(
            (
                root_apk,
                build_module(
                    package=context.source_package,
                    slug=context.slug,
                    arch=context.arch,
                    version_name=context.version_name,
                    version_code=context.version_code,
                    patched_apk=root_apk,
                    source_provenance=context.source_provenance,
                    output_directory=context.output_directory,
                    merged_stock=context.merged_stock,
                    module_version_code=_module_version_code(),
                ),
            )
        )
    return artifacts


def _build_apk(context: _BuildContext, *, root: bool = False) -> Path:
    return build_apk(
        source_package=context.source_package,
        slug=context.slug,
        patched_package=context.patched_package,
        arch=context.arch,
        java=context.java,
        cli=context.cli,
        patches=context.patches,
        signer=context.signer,
        toolchain_provenance=context.toolchain_provenance,
        merged_stock=context.merged_stock,
        source_provenance=context.source_provenance,
        merge_provenance=context.merge_provenance,
        output_directory=context.output_directory,
        version_name=context.version_name,
        version_code=context.version_code,
        keystore=context.keystore,
        keystore_alias=context.keystore_alias,
        keystore_password=context.keystore_password,
        include=context.include,
        exclude=context.exclude,
        exclusive=context.exclusive,
        options=context.options,
        stage_reporter=context.stage_reporter,
        root=root,
    )


def run_verify(
    directory: Path,
    package: str,
    *,
    arch: str,
    version_name: str | None = None,
    version_code: str | None = None,
    expected_signer: str | None = None,
) -> dict[str, object]:
    metadata = verify_apk_set(
        directory,
        package,
        version_name=version_name,
        version_code=version_code,
        arch=arch,
        expected_signer=expected_signer,
    )
    base = next(item for item in metadata if item.split_type == "base")
    return {
        "status": "verified",
        "package": base.package,
        "version": {"name": base.version_name, "code": base.version_code},
        "architecture": arch,
        "files": len(metadata),
    }


def run_download_versions(
    config_path: Path, *, cache: Path, package: str
) -> dict[str, object]:
    """Resolve Morphe-supported stock versions without downloading stock APKs."""
    config = load_config(config_path)
    app = next((item for item in config.apps if item.package == package), None)
    if app is None:
        raise BuildError("package is not configured")
    selection = app_toolchain(app, config.toolchain)
    prepare_options = (
        {"patches_sha256": selection.patches_sha256}
        if selection.patches_sha256 is not None
        else {}
    )
    toolchain_path = prepare_toolchain(
        cache,
        {
            "morphe-cli": selection.morphe_version,
            "morphe-patches": selection.patches_version,
        },
        {
            "morphe-cli": selection.morphe_source,
            "morphe-patches": selection.patches_source,
        },
        **prepare_options,
    )
    toolchain = _toolchain(toolchain_path)
    compatibility = run_morphe_compatibility(
        toolchain.java,
        toolchain.morphe_cli,
        toolchain.patches,
        app.package,
        include=app.include_patches,
        exclude=app.exclude_patches,
        exclusive=app.exclusive_patches,
        requested_version=app.version,
        include_universal_patches=app_include_universal_patches(app, config.toolchain),
        include_experimental_versions=app_include_experimental_versions(
            app, config.toolchain
        ),
    )
    return {
        "package": package,
        "versions": list(compatibility.compatible_versions),
        "selected": compatibility.selected_version,
    }


def run_list_patches(config_path: Path, *, cache: Path) -> dict[str, object]:
    config = load_config(config_path)
    toolchains = _prepare_toolchains(cache, config)
    results = []
    for app in config.apps:
        toolchain = _toolchain(toolchains[app_toolchain(app, config.toolchain)])
        result = run_morphe_compatibility(
            toolchain.java,
            toolchain.morphe_cli,
            toolchain.patches,
            app.package,
            include=app.include_patches,
            exclude=app.exclude_patches,
            exclusive=app.exclusive_patches,
            requested_version=app.version,
            include_universal_patches=app_include_universal_patches(
                app, config.toolchain
            ),
            include_experimental_versions=app_include_experimental_versions(
                app, config.toolchain
            ),
        )
        results.append(asdict(result))
    return {"apps": results}


def run_bundle_catalog(source: str, *, version: str = "latest") -> dict[str, object]:
    """List applications exposed by one GitHub patch bundle release."""
    catalog = load_bundle_catalog(source, version=version)
    return {
        "source": catalog.source,
        "version": catalog.version,
        "apps": [asdict(app) for app in catalog.apps],
    }


def run_community_bundles() -> dict[str, object]:
    """Load public bundle summaries used by the TUI discovery screen."""
    return {"bundles": [asdict(bundle) for bundle in load_community_bundles()]}


def run_patch_catalog(
    config_path: Path, *, cache: Path, package: str
) -> dict[str, object]:
    """List every patch the configured source offers for one configured app."""
    config = load_config(config_path)
    app = next((item for item in config.apps if item.package == package), None)
    if app is None:
        raise BuildError("package is not configured")
    selection = app_toolchain(app, config.toolchain)
    prepare_options = (
        {"patches_sha256": selection.patches_sha256}
        if selection.patches_sha256 is not None
        else {}
    )
    toolchain_path = prepare_toolchain(
        cache,
        {
            "morphe-cli": selection.morphe_version,
            "morphe-patches": selection.patches_version,
        },
        {
            "morphe-cli": selection.morphe_source,
            "morphe-patches": selection.patches_source,
        },
        **prepare_options,
    )
    toolchain = _toolchain(toolchain_path)
    output = run_morphe_action(
        toolchain.java,
        toolchain.morphe_cli,
        toolchain.patches,
        app.package,
        "list-patches",
        include_universal_patches=app_include_universal_patches(app, config.toolchain),
        include_experimental_versions=app_include_experimental_versions(
            app, config.toolchain
        ),
        with_options=True,
    )
    patches = parse_patch_list(output, app.package)
    selected = set(app.exclusive_patches)
    if not selected:
        selected = {patch.name for patch in patches if patch.enabled}
        selected.update(app.include_patches)
        selected.difference_update(app.exclude_patches)
    return {
        "package": app.package,
        "selected": [patch.name for patch in patches if patch.name in selected],
        "configured_options": {
            patch: dict(values) for patch, values in app.patch_options.items()
        },
        "patches": [asdict(patch) for patch in patches],
    }


_DISPOSABLE_CACHE_PATHS = ("work", "github-releases", "google-version-mappings.json")
_CLEANABLE_CACHE_PATHS = (
    "work",
    "github-releases",
    "google-version-mappings.json",
    "tools",
    "toolchains",
    "locks",
)


def run_clean(
    cache: Path, *, selected: Iterable[str] | None = None
) -> dict[str, object]:
    cache = cache.resolve()
    names = tuple(_DISPOSABLE_CACHE_PATHS if selected is None else selected)
    if any(name not in _CLEANABLE_CACHE_PATHS for name in names):
        raise BuildError("invalid cache selection")
    removed: list[str] = []
    for name in dict.fromkeys(names):
        path = (cache / name).resolve()
        if path.parent != cache or path.name != name:
            raise BuildError("invalid disposable cache path")
        if not path.exists():
            continue
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError:
            raise BuildError("cannot remove disposable cache") from None
        removed.append(str(path))
    return {
        "removed": removed,
        "preserved": [
            str(cache / name)
            for name in ("tools", "toolchains", "locks", "masamune.p12")
        ],
    }


def _module_version_code() -> int | None:
    value = os.environ.get("MORPHE_MODULE_VERSION_CODE")
    if value is None:
        return None
    try:
        version_code = int(value)
    except ValueError:
        raise BuildError(
            "MORPHE_MODULE_VERSION_CODE must be a positive integer"
        ) from None
    if version_code <= 0:
        raise BuildError("MORPHE_MODULE_VERSION_CODE must be a positive integer")
    return version_code


def _prepare_toolchains(cache: Path, config: Any) -> dict[ToolchainConfig, Path]:
    toolchains: dict[ToolchainConfig, Path] = {}
    for app in config.apps:
        if not app.enabled:
            continue
        selection = app_toolchain(app, config.toolchain)
        if selection not in toolchains:
            prepare_options = (
                {"patches_sha256": selection.patches_sha256}
                if selection.patches_sha256 is not None
                else {}
            )
            toolchains[selection] = prepare_toolchain(
                cache,
                {
                    "morphe-cli": selection.morphe_version,
                    "morphe-patches": selection.patches_version,
                },
                {
                    "morphe-cli": selection.morphe_source,
                    "morphe-patches": selection.patches_source,
                },
                **prepare_options,
            )
    return toolchains


def _apkmirror_version_code(
    urls: tuple[str, ...], *, version_name: str, arch: str
) -> str | None:
    codes = {
        resolve_apkmirror_version_code(url, version_name=version_name, arch=arch)
        for url in urls
    }
    if not codes:
        return None
    if len(codes) != 1:
        raise ProviderAmbiguous("APKMirror sources disagree on version code")
    return codes.pop()


def _apkmirror_version_code_hint(
    urls: tuple[str, ...],
    *,
    version_name: str,
    arch: str,
    reporter: Reporter,
    package: str,
) -> str | None:
    try:
        if urls:
            return _apkmirror_version_code(urls, version_name=version_name, arch=arch)
        return resolve_apkmirror_version_code_for_package(
            package, version_name=version_name, arch=arch
        )
    except (
        IntegrityMetadataError,
        ProviderAmbiguous,
        ProviderUnavailable,
        VersionUnavailable,
    ) as error:
        reporter.event(
            "hint",
            "APKMirror version-code hint unavailable; continuing without it",
            package=package,
            arch=arch,
            reason=str(error),
        )
        return None


def _toolchain(path: Path) -> PreparedToolchain:
    return PreparedToolchain.from_provenance(path)


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise IntegrityMetadataError(f"invalid {label}") from error
    if not isinstance(value, dict):
        raise IntegrityMetadataError(f"invalid {label}")
    return value


def _patch_release(path: Path) -> tuple[str, str]:
    data = _read_json(path, "toolchain provenance")
    tools = data.get("tools")
    if not isinstance(tools, list):
        raise ToolchainError("invalid toolchain provenance")
    for tool in tools:
        if (
            isinstance(tool, dict)
            and tool.get("name") == "morphe-patches"
            and isinstance(tool.get("repository"), str)
            and isinstance(tool.get("resolved_tag"), str)
        ):
            return tool["repository"], tool["resolved_tag"]
    raise ToolchainError("invalid toolchain provenance")


def _summary(
    results: list[BuildResult], skipped: list[dict[str, object]] | None = None
) -> dict[str, object]:
    jobs = [asdict(result) for result in results]
    skipped = skipped or []
    summary = f"Built {len(jobs)} verified job(s)."
    if skipped:
        summary += f" Skipped {len(skipped)} unavailable app/architecture job(s)."
    return {
        "status": "complete",
        "jobs": jobs,
        "skipped": skipped,
        "summary": summary,
    }


def _write_summary(directory: Path, summary: dict[str, object]) -> None:
    (directory / "build-summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    try:
        body = render_release_notes(summary)
    except Exception as error:
        raise BuildError("invalid build summary") from error
    (directory / "CHANGELOG.md").write_text(body, encoding="utf-8")
