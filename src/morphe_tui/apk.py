from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
import subprocess
import zipfile
from dataclasses import asdict, dataclass
from importlib import import_module
from importlib.metadata import version as package_version
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

from .errors import ApkMismatch, IntegrityMetadataError
from .hashing import sha256_file
from .toolchain import APKSIGNER_VERSION, ToolchainError, apksigner_jar

ANDROID_NS = "{http://schemas.android.com/apk/res/android}"
MANIFEST_NAME = ".goopdl-integrity.json"
PROVENANCE_NAME = "provenance.json"
PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$")
BASE64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
DIGEST_LENGTHS = {"sha1": 20, "sha256": 32}
SIGNER_DIGEST_RE = re.compile(
    r"^Signer.*certificate SHA-256 digest:\s*([0-9a-fA-F]{64})$", re.MULTILINE
)
MAX_DOWNLOAD_FILES = 20
MAX_APK_FILE_SIZE = 300 * 1024 * 1024
MAX_ZIP_ENTRIES = 100_000
MAX_ZIP_UNCOMPRESSED = 2 * 1024 * 1024 * 1024
MAX_ZIP_RATIO = 1_000
# Google rotated both signing keys, so each package has one certificate for
# Android 7-12 (SDK 24-32) and the rotated one for Android 13+ (SDK 33+).
EXPECTED_SIGNERS = {
    "com.google.android.youtube": (
        "3d7a1223019aa39d9ea0e3436ab7c0896bfb4fb679f4de5fe7c23f326c8f994a",
        "5aad2bee6db95d17e05a08d7d1e64c10a1511879154483916b6ae6c7fd9cb0c6",
    ),
    "com.google.android.apps.youtube.music": (
        "a2a1ad7ba7f41dfca4514e2afeb90691719af6d0fdbed4b09bbf0ed897701ceb",
        "6a2f65ec694a6a632acdcb5080912a565f903d4b8d83f0eb8e44fbdf2660d8e1",
    ),
}
ABI_NAMES = {
    "arm64_v8a": "arm64",
    "arm64-v8a": "arm64",
    "armeabi_v7a": "armv7",
    "armeabi-v7a": "armv7",
}
DENSITIES = {"ldpi", "mdpi", "tvdpi", "hdpi", "xhdpi", "xxhdpi", "xxxhdpi"}


@dataclass(frozen=True)
class ApkMetadata:
    path: str
    package: str
    version_name: str
    version_code: str
    split_name: str | None
    split_type: str
    abi: str | None
    density: str | None
    language: str | None
    feature: str | None
    platform_split_types: tuple[str, ...]
    required_split_types: tuple[str, ...]
    required_splits: tuple[str, ...]
    required_features: tuple[str, ...]
    signers_sha256: tuple[str, ...]
    unsupported_requirements: tuple[str, ...] = ()


def _decode_digest(value: object, algorithm: str) -> bytes:
    if not isinstance(value, str) or not BASE64URL_RE.fullmatch(value):
        raise IntegrityMetadataError(f"invalid {algorithm} Base64url digest")
    try:
        digest = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
    except (binascii.Error, ValueError) as error:
        raise IntegrityMetadataError(f"invalid {algorithm} Base64url digest") from error
    if len(digest) != DIGEST_LENGTHS[algorithm]:
        raise IntegrityMetadataError(f"invalid {algorithm} digest length")
    if base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=") != value:
        raise IntegrityMetadataError(f"non-canonical {algorithm} Base64url digest")
    return digest


def read_google_manifest(directory: Path, manifest: Path) -> list[dict[str, object]]:
    if not manifest.is_file() or manifest.is_symlink():
        raise IntegrityMetadataError("missing or invalid integrity manifest")
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise IntegrityMetadataError("missing or invalid integrity manifest") from error
    if not isinstance(data, dict) or set(data) != {"version", "files"}:
        raise IntegrityMetadataError("invalid integrity manifest schema")
    if (
        data["version"] != 1
        or isinstance(data["version"], bool)
        or not isinstance(data["files"], list)
    ):
        raise IntegrityMetadataError("invalid integrity manifest schema")
    return data["files"]


def verify_google_delivery(directory: Path, manifest: Path) -> None:
    """Revalidate manifest coverage, paths, sizes, and Google-provided digests."""
    entries = read_google_manifest(directory, manifest)
    if not entries or len(entries) > MAX_DOWNLOAD_FILES:
        raise IntegrityMetadataError("invalid Google download file count")
    expected: set[Path] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "path",
            "size",
            "algorithm",
            "digest",
            "google_sha1",
            "google_sha256",
        }:
            raise IntegrityMetadataError("invalid integrity manifest entry")
        relative = entry["path"]
        if (
            not isinstance(relative, str)
            or not relative
            or "\\" in relative
            or Path(relative).is_absolute()
            or any(part in ("", ".", "..") for part in relative.split("/"))
        ):
            raise IntegrityMetadataError("invalid integrity manifest path")
        path = directory.joinpath(*relative.split("/"))
        current = directory
        for part in relative.split("/"):
            current /= part
            if current.is_symlink():
                raise IntegrityMetadataError(f"symlink not allowed: {relative}")
        if path in expected:
            raise IntegrityMetadataError("duplicate integrity manifest path")
        expected.add(path)
        size, algorithm = entry["size"], entry["algorithm"]
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or size > MAX_APK_FILE_SIZE
        ):
            raise IntegrityMetadataError(f"invalid size for {relative}")
        if not isinstance(algorithm, str) or algorithm not in DIGEST_LENGTHS:
            raise IntegrityMetadataError(f"invalid digest algorithm for {relative}")
        digest = _decode_digest(entry["digest"], algorithm)
        google_digests = {
            "sha1": entry["google_sha1"],
            "sha256": entry["google_sha256"],
        }
        for google_algorithm, encoded in google_digests.items():
            if not isinstance(encoded, str):
                raise IntegrityMetadataError(f"invalid Google digest for {relative}")
            if encoded:
                _decode_digest(encoded, google_algorithm)
        if google_digests[algorithm] != entry["digest"]:
            raise IntegrityMetadataError(f"selected Google digest mismatch: {relative}")
        if not path.is_file() or path.is_symlink():
            raise IntegrityMetadataError(f"missing regular file: {relative}")
        if path.stat().st_size != size:
            raise IntegrityMetadataError(f"size mismatch: {relative}")
        calculated_hash = hashlib.new(algorithm)
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                calculated_hash.update(chunk)
        calculated = calculated_hash.digest()
        if not hmac.compare_digest(calculated, digest):
            raise IntegrityMetadataError(f"digest mismatch: {relative}")
    actual = {
        path
        for path in directory.rglob("*")
        if path != manifest and (path.is_file() or path.is_symlink())
    }
    if not expected or expected != actual:
        raise IntegrityMetadataError(
            "integrity manifest does not cover downloaded files"
        )
    if any(path.suffix.lower() != ".apk" for path in actual):
        raise IntegrityMetadataError("unexpected non-APK download")


def signer_fingerprints(path: Path) -> tuple[str, ...]:
    """Return every signer certificate SHA-256 verified by `apksigner`.

    A rotated signing lineage has one certificate per supported SDK range, and
    all of them are legitimate, so the whole set is returned.
    """
    try:
        jar = apksigner_jar()
    except ToolchainError as error:
        raise IntegrityMetadataError(f"cannot verify APK signature: {error}") from error
    try:
        result = subprocess.run(
            ["java", "-jar", str(jar), "verify", "--print-certs", "--", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise IntegrityMetadataError(
            f"APK signature verification failed: {path.name}"
        ) from error
    fingerprints = (
        set(SIGNER_DIGEST_RE.findall(result.stdout)) if not result.returncode else set()
    )
    if not fingerprints:
        raise IntegrityMetadataError(
            f"invalid, unsigned, or unsupported APK signature: {path.name}"
        )
    return tuple(sorted(value.lower() for value in fingerprints))


def validate_zip_archive(path: Path, *, required: tuple[str, ...] = ()) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ZIP_ENTRIES:
                raise IntegrityMetadataError(
                    f"archive has too many entries: {path.name}"
                )
            total = 0
            names = set()
            for entry in entries:
                name = entry.filename
                parts = PurePosixPath(name.replace("\\", "/"))
                if (
                    not name
                    or name.startswith(("/", "\\"))
                    or "\\" in name
                    or re.match(r"^[A-Za-z]:", name)
                    or ".." in parts.parts
                ):
                    raise IntegrityMetadataError(f"unsafe archive path: {path.name}")
                if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                    raise IntegrityMetadataError(
                        f"archive contains symlink: {path.name}"
                    )
                total += entry.file_size
                if total > MAX_ZIP_UNCOMPRESSED:
                    raise IntegrityMetadataError(
                        f"archive exceeds uncompressed size limit: {path.name}"
                    )
                if entry.file_size and (
                    entry.compress_size == 0
                    or entry.file_size / entry.compress_size > MAX_ZIP_RATIO
                ):
                    raise IntegrityMetadataError(
                        f"archive compression ratio exceeds limit: {path.name}"
                    )
                names.add(name)
            if any(name not in names for name in required):
                raise IntegrityMetadataError(
                    f"archive missing required file: {path.name}"
                )
    except zipfile.BadZipFile as error:
        raise IntegrityMetadataError(f"malformed archive: {path.name}") from error


def _manifest_root(path: Path) -> ElementTree.Element:
    try:
        validate_zip_archive(path, required=("AndroidManifest.xml",))
        with zipfile.ZipFile(path) as apk:
            raw = apk.read("AndroidManifest.xml")
        if raw.lstrip().startswith(b"<"):
            return ElementTree.fromstring(raw)
        root_logger = logging.getLogger()
        handler = logging.NullHandler() if not root_logger.handlers else None
        if handler:
            root_logger.addHandler(handler)
        try:
            printer = import_module("axml.axml").AXMLPrinter(raw)
        finally:
            if handler:
                root_logger.removeHandler(handler)
        if not printer.is_valid():
            raise ValueError("invalid binary Android manifest")
        return ElementTree.fromstring(printer.get_xml(pretty=False))
    except Exception as error:
        raise IntegrityMetadataError(
            f"malformed or unsupported APK manifest: {path.name}"
        ) from error


def _bool(value: str | None) -> bool:
    return value is not None and value.lower() in {"true", "1"}


def inspect_apk(
    path: Path, relative: str, *, verify_signature: bool = True
) -> ApkMetadata:
    root = _manifest_root(path)
    package = root.get("package", "")
    version_name = root.get(ANDROID_NS + "versionName", "")
    version_code = root.get(ANDROID_NS + "versionCode", "")
    split_name = root.get("split")
    if (
        not PACKAGE_RE.fullmatch(package)
        or not version_code
        or (not split_name and not version_name)
    ):
        raise IntegrityMetadataError(f"missing APK identity metadata: {path.name}")
    config_for = root.get(ANDROID_NS + "configForSplit")
    feature = split_name if _bool(root.get(ANDROID_NS + "isFeatureSplit")) else None
    abi = density = language = None
    if split_name and split_name.startswith("config."):
        qualifier = split_name.removeprefix("config.")
        abi = ABI_NAMES.get(qualifier)
        density = (
            qualifier if qualifier in DENSITIES or qualifier.endswith("dpi") else None
        )
        if (
            not abi
            and not density
            and re.fullmatch(r"[a-z]{2,3}(?:-r[A-Z]{2})?", qualifier)
        ):
            language = qualifier
    try:
        with zipfile.ZipFile(path) as apk:
            native_abis = {
                name.split("/", 2)[1]
                for name in apk.namelist()
                if re.match(r"lib/[^/]+/[^/]+\.so$", name)
            }
    except zipfile.BadZipFile as error:
        raise IntegrityMetadataError(f"malformed APK ZIP: {path.name}") from error
    detected = {ABI_NAMES.get(value, value) for value in native_abis}
    if abi and detected and abi not in detected:
        raise IntegrityMetadataError(f"unsupported mixed ABI APK: {path.name}")
    if not abi and len(detected) > 1:
        abi = "universal"
    else:
        abi = abi or (next(iter(detected)) if detected else None)
    application = root.find("application")
    split_type_values = root.get(ANDROID_NS + "splitTypes", "")
    required_type_values = root.get(ANDROID_NS + "requiredSplitTypes", "")
    if application is not None:
        split_type_values += "," + application.get(ANDROID_NS + "splitTypes", "")
        required_type_values += "," + application.get(
            ANDROID_NS + "requiredSplitTypes", ""
        )
    platform_split_types = tuple(
        sorted(value for value in split_type_values.split(",") if value)
    )
    required_split_types = tuple(
        sorted(value for value in required_type_values.split(",") if value)
    )
    required_splits = tuple(
        sorted(
            {
                node.get(ANDROID_NS + "name", "")
                for node in root.findall("uses-split")
                if node.get(ANDROID_NS + "name")
            }
        )
    )
    required_features = tuple(
        sorted(
            {
                node.get(ANDROID_NS + "name", "")
                for node in root.findall("uses-feature")
                if node.get(ANDROID_NS + "name")
                and node.get(ANDROID_NS + "required", "true").lower() != "false"
            }
        )
    )
    unsupported_requirements = {
        f"shared-library:{node.get(ANDROID_NS + 'name')}"
        for node in root.findall("./application/uses-library")
        if node.get(ANDROID_NS + "name")
        and node.get(ANDROID_NS + "required", "true").lower() != "false"
    }
    distribution_namespace = "{http://schemas.android.com/apk/distribution}"
    for module in root.findall(f".//{distribution_namespace}module"):
        module_type = module.get(distribution_namespace + "type", "")
        if module_type == "asset-pack":
            unsupported_requirements.add("play-asset-delivery")
    split_type = (
        "base"
        if not split_name
        else "feature"
        if feature
        else "abi"
        if abi
        else "density"
        if density
        else "language"
        if language
        else "neutral"
    )
    return ApkMetadata(
        relative,
        package,
        version_name,
        version_code,
        split_name,
        split_type,
        abi,
        density,
        language,
        feature or config_for,
        platform_split_types,
        required_split_types,
        required_splits,
        required_features,
        signer_fingerprints(path) if verify_signature else (),
        tuple(sorted(unsupported_requirements)),
    )


def verify_apk_set(
    directory: Path,
    package: str,
    *,
    version_name: str | None,
    version_code: str | None,
    arch: str,
    expected_signer: str | None = None,
) -> list[ApkMetadata]:
    paths = sorted(directory.glob("*.apk"), key=lambda item: item.name)
    metadata = [inspect_apk(path, path.name) for path in paths]
    if not metadata or sum(item.split_type == "base" for item in metadata) != 1:
        raise IntegrityMetadataError("APK set must contain exactly one base APK")
    if any(item.package != package for item in metadata):
        raise ApkMismatch("APK package mismatch")
    base = next(item for item in metadata if item.split_type == "base")
    if (
        any(item.version_code != base.version_code for item in metadata)
        or any(
            item.version_name and item.version_name != base.version_name
            for item in metadata
        )
        or (version_name and base.version_name != version_name)
        or (version_code and base.version_code != version_code)
    ):
        raise ApkMismatch("APK version mismatch")
    signers = {item.signers_sha256 for item in metadata}
    if len(signers) != 1:
        raise IntegrityMetadataError("APK signer mismatch")
    pinned = EXPECTED_SIGNERS.get(
        package, (expected_signer,) if expected_signer else ()
    )
    if pinned and set(pinned) != set(metadata[0].signers_sha256):
        raise IntegrityMetadataError("APK signer lineage differs from pinned lineage")
    split_names = {item.split_name for item in metadata if item.split_name}
    missing = sorted(
        {required for item in metadata for required in item.required_splits}
        - split_names
    )
    if missing:
        raise IntegrityMetadataError(f"missing required split: {', '.join(missing)}")
    available_types = {
        split_type for item in metadata for split_type in item.platform_split_types
    }
    missing_types = sorted(
        {split_type for item in metadata for split_type in item.required_split_types}
        - available_types
    )
    if missing_types:
        raise IntegrityMetadataError(
            f"missing required split type: {', '.join(missing_types)}"
        )
    incompatible = sorted(
        {
            item.abi
            for item in metadata
            if item.abi and item.abi not in {arch, "universal"}
        }
    )
    if incompatible:
        raise ApkMismatch(f"wrong ABI split for {arch}: {', '.join(incompatible)}")
    return metadata


def write_provenance(
    directory: Path,
    manifest: Path,
    metadata: list[ApkMetadata],
    *,
    package: str,
    arch: str,
    profile: str | None,
    region: str | None,
) -> None:
    google = {
        entry["path"]: entry for entry in read_google_manifest(directory, manifest)
    }
    files = []
    for item in sorted(metadata, key=lambda value: value.path):
        path = directory / item.path
        entry = google[item.path]
        files.append(
            {
                "original_filename": item.path,
                "normalized_filename": item.path,
                "size": path.stat().st_size,
                "google_digest": {
                    "algorithm": entry["algorithm"],
                    "value": entry["digest"],
                },
                "sha256": sha256_file(path),
                "apk": asdict(item),
            }
        )
    base = next(item for item in metadata if item.split_type == "base")
    version_name, version_code = base.version_name, base.version_code
    provenance = {
        "schema_version": 2,
        "provider": "local",
        "inspection_tools": {
            "axml": package_version("axml"),
            "apksigner": APKSIGNER_VERSION,
        },
        "package": package,
        "version": {"name": version_name, "code": version_code},
        "architecture": arch,
        "profile": profile,
        "region": region,
        "certificate_sha256": list(base.signers_sha256),
        "files": files,
    }
    (directory / PROVENANCE_NAME).write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
