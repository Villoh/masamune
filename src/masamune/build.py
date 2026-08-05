from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .apk import (  # pyright: ignore[reportMissingImports]
    ApkMetadata,
    inspect_apk,
    validate_zip_archive,
)
from .architecture import Architecture  # pyright: ignore[reportMissingImports]
from .config import AppConfig, BuildConfig
from .errors import IntegrityMetadataError  # pyright: ignore[reportMissingImports]
from .hashing import sha256_file

YOUTUBE_PACKAGE = "com.google.android.youtube"
DEFAULT_PATCHED_PACKAGES = {
    "com.google.android.youtube": "app.morphe.android.youtube",
    "com.google.android.apps.youtube.music": "app.morphe.android.apps.youtube.music",
}
# A root module payload must keep the stock package name (see docs/modules.md),
# and GmsCore support has no meaning under the mount-based root install model.
# GmsCore defaults on and must be explicitly disabled. Change package name
# defaults off, so removing an explicit selection is enough; passing it to
# --disable makes current Morphe CLI reject otherwise valid root commands.
_ROOT_REMOVED_PATCHES = ("GmsCore support", "Change package name")
_ROOT_DISABLED_PATCHES = ("GmsCore support",)
# Morphe patches can apply "Disable Play Store updates" transitively while
# patching Reddit, forcing android:versionCode to Int.MAX_VALUE. Keep sentinel
# acceptance scoped to patched outputs; verified stock remains strict.
CORRUPTED_VERSION_CODE = str(2**31 - 1)


@dataclass(frozen=True)
class BuildJob:
    app: AppConfig
    arch: Architecture
    output_directory: Path
    cache_key: str


class BuildError(RuntimeError):
    """Raised when patched APK build cannot be trusted."""


def expand_build_jobs(config: BuildConfig, root: Path) -> tuple[BuildJob, ...]:
    jobs = []
    for app in config.apps:
        if not app.enabled:
            continue
        arches = (
            tuple(Architecture)
            if app.arch in {"all", "both"}
            else (Architecture.from_config(app.arch),)
        )
        for arch in arches:
            jobs.append(
                BuildJob(
                    app,
                    arch,
                    root / app.slug / arch.value,
                    f"{app.package}/{arch.value}",
                )
            )
    return tuple(jobs)


def _architecture(value: Architecture | str) -> Architecture:
    try:
        return Architecture.from_config(value)
    except ValueError as error:
        raise BuildError(str(error)) from None


def create_keystore(
    path: Path,
    *,
    alias: str,
    password: str,
    keytool: str = "keytool",
    repository: Path | None = None,
) -> Path:
    if not alias or not password:
        raise BuildError("keystore alias and password are required")
    resolved = path.resolve()
    if repository and _inside(resolved, repository.resolve()):
        raise BuildError("keystore must be outside repository")
    if path.exists() or path.is_symlink():
        raise FileExistsError(f"refusing to overwrite keystore: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["MORPHE_KEYSTORE_PASSWORD"] = password
    command = [
        keytool,
        "-genkeypair",
        "-keystore",
        str(path),
        "-storetype",
        "PKCS12",
        "-alias",
        alias,
        "-keyalg",
        "RSA",
        "-keysize",
        "4096",
        "-validity",
        "10000",
        "-dname",
        "CN=Masamune",
        "-storepass:env",
        "MORPHE_KEYSTORE_PASSWORD",
        "-keypass:env",
        "MORPHE_KEYSTORE_PASSWORD",
    ]
    try:
        subprocess.run(
            command, check=True, capture_output=True, text=True, env=env, timeout=60
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        path.unlink(missing_ok=True)
        raise BuildError("keystore creation failed") from None
    if not path.is_file() or path.is_symlink():
        raise BuildError("keytool did not create keystore")
    return path


# Fixed and publicly known, matching MorpheApp/morphe-manager's own
# KeystoreManager.DEFAULT: the auto-generated keystore's real protection is
# its location in the per-user cache directory (like Android's app-private
# storage), not an unguessable password.
AUTO_KEYSTORE_PASSWORD = "masamune"


def ensure_user_keystore(path: Path, *, alias: str, keytool: str = "keytool") -> None:
    """Auto-generate a per-user keystore on first use if `path` is missing.

    Mirrors morphe-manager's lazy per-user keystore: no `keytool` required
    from the user, no shared/public template file needed, so `uv tool
    install` (with no `.github/` checkout) still gets a real signing key.
    """
    if path.is_file():
        return
    create_keystore(path, alias=alias, password=AUTO_KEYSTORE_PASSWORD, keytool=keytool)


def morphe_patch_command(
    java: Path | str,
    cli: Path,
    patches: Path,
    stock: Path,
    output: Path,
    *,
    arch: Architecture | str = Architecture.ARM64,
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
    exclusive: Sequence[str] = (),
    options: Mapping[str, Mapping[str, str | int | float | bool]] | None = None,
) -> list[str]:
    if exclusive and include:
        raise BuildError("exclusive patches cannot be combined with include")
    command = [
        str(java),
        "-jar",
        str(cli),
        "patch",
        str(stock),
        f"--out={output}",
        "--unsigned",
        f"--striplibs={_architecture(arch).android_abi}",
        f"--patches={patches}",
    ]
    if exclusive:
        command.append("--exclusive")
    for name in exclusive or include:
        command.append(f"--enable={name}")
    for name in exclude:
        command.append(f"--disable={name}")
    flattened: dict[str, str] = {}
    for values in (options or {}).values():
        for key, value in values.items():
            if key in flattened:
                raise BuildError(f"duplicate Morphe option key: {key}")
            flattened[key] = _option(value)
    command.extend(f"--options={key}={flattened[key]}" for key in sorted(flattened))
    return command


def sign_apk(
    *,
    input_apk: Path,
    output: Path,
    package: str,
    version_name: str,
    version_code: str,
    arch: Architecture | str,
    java: Path | str,
    signer: Path,
    keystore: Path,
    keystore_alias: str,
    keystore_password: str,
    allow_corrupted_version_code: bool = False,
) -> ApkMetadata:
    architecture = _architecture(arch)
    internal_arch = architecture.goopdl
    if any(
        not path.is_file() or path.is_symlink()
        for path in (input_apk, signer, keystore)
    ):
        raise BuildError("missing or invalid signing input")
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"refusing to overwrite signed APK: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".morphe-sign-", dir=output.parent) as raw:
        signed_dir = Path(raw) / "signed"
        signed_dir.mkdir()
        command = [
            str(java),
            "-jar",
            str(signer),
            "--apks",
            str(input_apk),
            "--out",
            str(signed_dir),
            "--ks",
            str(keystore),
            "--ksAlias",
            keystore_alias,
        ]
        try:
            result = subprocess.run(
                command,
                input=f"{keystore_password}\n{keystore_password}\n",
                check=False,
                capture_output=True,
                text=True,
                timeout=300,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise BuildError("APK signing failed") from None
        candidates = list(signed_dir.glob("*.apk"))
        if result.returncode or len(candidates) != 1 or candidates[0].is_symlink():
            raise BuildError("APK signing failed")
        metadata = _verify_output(
            candidates[0],
            package=package,
            version_name=version_name,
            version_code=version_code,
            arch=internal_arch,
            allow_corrupted_version_code=allow_corrupted_version_code,
        )
        shutil.copyfile(candidates[0], output)
        if sha256_file(candidates[0]) != sha256_file(output):
            output.unlink(missing_ok=True)
            raise BuildError("signed APK publication failed")
    return metadata


def build_apk(
    *,
    source_package: str,
    slug: str | None = None,
    patched_package: str | None = None,
    arch: Architecture | str,
    java: Path | str,
    cli: Path,
    patches: Path,
    signer: Path,
    merged_stock: Path,
    source_provenance: Path,
    merge_provenance: Path,
    output_directory: Path,
    version_name: str,
    version_code: str,
    keystore: Path,
    keystore_alias: str,
    keystore_password: str,
    toolchain_provenance: Path | None = None,
    root: bool = False,
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
    exclusive: Sequence[str] = (),
    options: Mapping[str, Mapping[str, str | int | float | bool]] | None = None,
    stage_reporter: Callable[[str, str], None] | None = None,
) -> Path:
    slug = slug or source_package.replace(".", "-")
    patched_package = patched_package or DEFAULT_PATCHED_PACKAGES.get(
        source_package, source_package
    )
    architecture = _architecture(arch)
    effective_include = tuple(
        name for name in include if not root or name not in _ROOT_REMOVED_PATCHES
    )
    effective_exclusive = tuple(
        name for name in exclusive if not root or name not in _ROOT_REMOVED_PATCHES
    )
    if root and exclusive and not effective_exclusive:
        raise BuildError("root build has no patches after disabling GmsCore support")
    effective_exclude = tuple(exclude)
    if root:
        effective_exclude += tuple(
            name for name in _ROOT_DISABLED_PATCHES if name not in effective_exclude
        )
    effective_options = {
        name: dict(values)
        for name, values in (options or {}).items()
        if not root or name not in _ROOT_REMOVED_PATCHES
    }
    required = (
        cli,
        patches,
        signer,
        merged_stock,
        source_provenance,
        merge_provenance,
        keystore,
    ) + ((toolchain_provenance,) if toolchain_provenance else ())
    if any(not path.is_file() or path.is_symlink() for path in required):
        raise BuildError("missing or invalid build input")
    output_directory.mkdir(parents=True, exist_ok=True)
    mode = "root" if root else "non-root"
    suffix = "-root" if root else ""
    output = (
        output_directory / f"{slug}-{version_name}-{architecture.value}{suffix}.apk"
    )
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"refusing to overwrite patched APK: {output}")
    with tempfile.TemporaryDirectory(
        prefix=".morphe-build-", dir=output_directory
    ) as raw:
        temporary = Path(raw)
        unsigned = temporary / f"{slug}-unsigned.apk"
        if stage_reporter is not None:
            stage_reporter("patch", mode)
        try:
            result = subprocess.run(
                morphe_patch_command(
                    java,
                    cli,
                    patches,
                    merged_stock,
                    unsigned,
                    arch=architecture,
                    include=effective_include,
                    exclude=effective_exclude,
                    exclusive=effective_exclusive,
                    options=effective_options,
                ),
                check=False,
                capture_output=True,
                text=True,
                timeout=1800,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise BuildError("Morphe patch command failed") from None
        if result.returncode or not unsigned.is_file() or unsigned.is_symlink():
            detail = (result.stderr or result.stdout).strip().splitlines()
            suffix = f": {detail[0]}" if detail else ""
            raise BuildError(f"Morphe patch command failed ({mode}){suffix}")
        if stage_reporter is not None:
            stage_reporter("sign", mode)
        metadata = sign_apk(
            input_apk=unsigned,
            output=output,
            package=source_package if root else patched_package,
            version_name=version_name,
            version_code=version_code,
            arch=architecture,
            java=java,
            signer=signer,
            keystore=keystore,
            keystore_alias=keystore_alias,
            keystore_password=keystore_password,
            allow_corrupted_version_code=True,
        )
    provenance = {
        "schema_version": 1,
        "package": source_package if root else patched_package,
        "source_package": source_package,
        "build_mode": mode,
        "version": {"name": version_name, "code": version_code},
        "architecture": architecture.value,
        "patches": {
            "included": list(effective_exclusive or effective_include),
            "excluded": list(effective_exclude),
            "exclusive": bool(effective_exclusive),
            "options": effective_options,
        },
        "inputs": {
            "source_provenance": _link(source_provenance),
            "merge_provenance": _link(merge_provenance),
            "merged_stock": _link(merged_stock),
            "morphe_cli": _link(cli),
            "patches_bundle": _link(patches),
            "signer": _link(signer),
            **(
                {
                    "toolchain_provenance": _link(toolchain_provenance),
                    "toolchain": _toolchain_inputs(toolchain_provenance),
                }
                if toolchain_provenance
                else {}
            ),
        },
        "output": {
            "path": output.name,
            "size": output.stat().st_size,
            "sha256": sha256_file(output),
            "certificate_sha256": list(metadata.signers_sha256),
        },
        "smoke_test": {
            "status": "pending",
            "required": f"install and launch on matching {architecture.value} device",
        },
    }
    _atomic_json(output.with_suffix(".provenance.json"), provenance)
    return output


def _verify_output(
    path: Path,
    *,
    package: str,
    version_name: str,
    version_code: str,
    arch: str,
    allow_corrupted_version_code: bool = False,
):
    try:
        validate_zip_archive(path, required=("AndroidManifest.xml",))
        with zipfile.ZipFile(path) as apk:
            if apk.testzip() is not None or "AndroidManifest.xml" not in apk.namelist():
                raise BuildError("patched APK ZIP verification failed")
        metadata = inspect_apk(path, path.name)
    except (OSError, zipfile.BadZipFile, IntegrityMetadataError) as error:
        raise BuildError("patched APK verification failed") from error
    version_code_matches = metadata.version_code == version_code or (
        allow_corrupted_version_code and metadata.version_code == CORRUPTED_VERSION_CODE
    )
    if (
        metadata.package != package
        or metadata.version_name != version_name
        or not version_code_matches
    ):
        raise BuildError(
            "patched APK identity mismatch: "
            f"expected {package} {version_name} ({version_code}); "
            f"got {metadata.package} {metadata.version_name} ({metadata.version_code})"
        )
    if metadata.abi not in {arch, "universal"} or not metadata.signers_sha256:
        raise BuildError("patched APK architecture or signature mismatch")
    return metadata


def _option(value: str | float | bool) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    text = str(value)
    if not text or any(char in text for char in "\r\n\x00"):
        raise BuildError("invalid Morphe option value")
    return text


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _link(path: Path) -> dict[str, Any]:
    return {
        "path": str(path.resolve()),
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _toolchain_inputs(path: Path) -> dict[str, dict[str, str]]:
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))["tools"]
        tools = {
            entry["name"]: {
                key: entry[key] for key in ("repository", "resolved_tag", "sha256")
            }
            for entry in entries
            if entry["name"] in {"morphe-cli", "morphe-patches"}
        }
    except (KeyError, OSError, TypeError, UnicodeError, json.JSONDecodeError):
        raise BuildError("invalid toolchain provenance") from None
    if set(tools) != {"morphe-cli", "morphe-patches"} or any(
        not all(isinstance(value, str) for value in tool.values())
        for tool in tools.values()
    ):
        raise BuildError("invalid toolchain provenance")
    return tools


def _atomic_json(path: Path, data: object) -> None:
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}-", delete=False
    ) as temporary:
        json.dump(data, temporary, indent=2, sort_keys=True)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, path)
