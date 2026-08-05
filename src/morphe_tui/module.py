from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import zipfile
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from .apk import inspect_apk  # pyright: ignore[reportMissingImports]
from .architecture import Architecture  # pyright: ignore[reportMissingImports]
from .errors import IntegrityMetadataError  # pyright: ignore[reportMissingImports]
from .hashing import sha256_file

CORRUPTED_VERSION_CODE = str(2**31 - 1)
MODULE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]+$")
MODULE_PROVENANCE = "module-provenance.json"
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


class ModuleError(RuntimeError):
    """Raised when a root module cannot be built safely."""


UTILS_SH = r"""#!/system/bin/sh

get_base_apk() {
    pm path "$PKG_NAME" 2>/dev/null | sed -n 's/^package://p' | head -n 1
}

installed_version() {
    dumpsys package "$PKG_NAME" 2>/dev/null | sed -n 's/^[[:space:]]*versionName=//p' | head -n 1
}

record_ksu_status() {
    if [ "${KSU:-false}" = true ] && command -v ksud >/dev/null 2>&1; then
        ksud module config set --temp runtime.status "$1" >/dev/null 2>&1 || true
    fi
}

mount_payload() {
    target=$(get_base_apk)
    if [ -z "$target" ]; then
        record_ksu_status "target-missing"
        return 1
    fi
    if [ "$(installed_version)" != "$PKG_VERSION" ]; then
        record_ksu_status "version-mismatch"
        return 1
    fi
    if grep -F " $target " /proc/mounts >/dev/null 2>&1; then
        record_ksu_status "mounted"
        return 0
    fi
    mount --bind "$MODDIR/base.apk" "$target" || return 1
    record_ksu_status "mounted"
}

unmount_payload() {
    target=$(get_base_apk)
    if [ -n "$target" ] && grep -F " $target " /proc/mounts >/dev/null 2>&1; then
        umount "$target" || return 1
    fi
    record_ksu_status "disabled"
}
"""

CUSTOMIZE_SH = r"""#!/system/bin/sh

# shellcheck disable=SC1091
. "$MODPATH/config"

if [ "$ARCH" != "$MODULE_ARCH" ]; then
    abort "Wrong architecture: device=$ARCH module=$MODULE_ARCH"
fi

install_stock() {
    installed=$(dumpsys package "$PKG_NAME" 2>/dev/null | sed -n 's/^[[:space:]]*versionName=//p' | head -n 1)
    [ "$installed" = "$PKG_VERSION" ] && return 0

    [ -f "$MODPATH/stock/base.apk" ] || abort "Missing signed stock payload"
    ui_print "* Installing signed stock $PKG_VERSION"
    pm install -r -d "$MODPATH/stock/base.apk" >/dev/null 2>&1 && return 0

    ui_print "! Replacing incompatible stock removes app data"
    pm uninstall "$PKG_NAME" >/dev/null 2>&1 || abort "Cannot replace installed stock"
    pm install -r -d "$MODPATH/stock/base.apk" >/dev/null 2>&1 || abort "Cannot install signed stock"
}

ui_print "* $MODULE_NAME"
ui_print "* Target: $PKG_NAME $PKG_VERSION ($MODULE_ARCH)"
install_stock
ui_print "* Reboot required to activate bind mount"

set_perm "$MODPATH/base.apk" 0 0 0644
set_perm "$MODPATH/config" 0 0 0644
set_perm "$MODPATH/utils.sh" 0 0 0755
set_perm "$MODPATH/service.sh" 0 0 0755
set_perm "$MODPATH/action.sh" 0 0 0755
set_perm "$MODPATH/uninstall.sh" 0 0 0755
touch "$MODPATH/skip_mount"
"""

SERVICE_SH = r"""#!/system/bin/sh

# shellcheck disable=SC1091
MODDIR=${0%/*}
. "$MODDIR/config"
. "$MODDIR/utils.sh"

until [ "$(getprop sys.boot_completed)" = 1 ]; do
    sleep 2
done

if [ ! -f "$MODDIR/morphe_action_disabled" ]; then
    mount_payload || record_ksu_status "mount-failed"
fi
"""

ACTION_SH = r"""#!/system/bin/sh

# shellcheck disable=SC1091
MODDIR=${0%/*}
. "$MODDIR/config"
. "$MODDIR/utils.sh"

if [ -f "$MODDIR/morphe_action_disabled" ]; then
    rm -f "$MODDIR/morphe_action_disabled"
    if mount_payload; then
        echo "Enabled $MODULE_NAME"
    else
        echo "Failed to enable $MODULE_NAME"
        exit 1
    fi
else
    unmount_payload || exit 1
    touch "$MODDIR/morphe_action_disabled"
    echo "Disabled $MODULE_NAME"
fi
"""

UNINSTALL_SH = r"""#!/system/bin/sh

# shellcheck disable=SC1091
MODDIR=${0%/*}
. "$MODDIR/config"
. "$MODDIR/utils.sh"

unmount_payload >/dev/null 2>&1 || true
"""


def build_module(
    *,
    package: str,
    slug: str | None = None,
    arch: str,
    version_name: str,
    version_code: str,
    patched_apk: Path,
    source_provenance: Path,
    output_directory: Path,
    merged_stock: Path | None = None,
    selected_splits: Sequence[Path] = (),
    update_json_url: str | None = None,
    zip_url: str | None = None,
    changelog_url: str | None = None,
    module_version_code: int | None = None,
) -> Path:
    try:
        architecture = Architecture.from_config(arch)
    except ValueError as error:
        raise ModuleError(str(error)) from None
    internal_arch = architecture.goopdl
    module_arch = architecture.module
    slug = slug or package.replace(".", "-")
    _safe_value(version_name, "version name")
    try:
        parsed_version_code = int(version_code)
    except ValueError:
        raise ModuleError("version code must be a positive integer") from None
    if not version_code.isdigit() or parsed_version_code <= 0:
        raise ModuleError("version code must be a positive integer")
    resolved_module_version_code = module_version_code or parsed_version_code
    if resolved_module_version_code <= 0:
        raise ModuleError("module version code must be positive")
    if not merged_stock and not selected_splits:
        raise ModuleError("module requires verified stock payload")
    module_id = f"morphe.{slug}.{arch}"
    if not MODULE_ID_RE.fullmatch(module_id):
        raise ModuleError("invalid generated module id")
    _verify_apk(
        patched_apk,
        package=package,
        version_name=version_name,
        version_code=version_code,
        arch=internal_arch,
        signed=True,
        label="patched",
        allow_corrupted_version_code=True,
    )
    stock_files: list[tuple[str, Path]] = []
    if merged_stock:
        _verify_apk(
            merged_stock,
            package=package,
            version_name=version_name,
            version_code=version_code,
            arch=internal_arch,
            signed=True,
            label="signed merged stock",
        )
        stock_files.append(("stock/base.apk", merged_stock))
    stock_files.extend(
        _verified_splits(
            source_provenance,
            selected_splits,
            package=package,
            version_name=version_name,
            version_code=version_code,
            arch=internal_arch,
        )
    )
    names = [name for name, _ in stock_files]
    if len(names) != len(set(names)):
        raise ModuleError("stock payload filenames collide")
    update = _update_metadata(
        update_json_url,
        zip_url,
        changelog_url,
        version_name,
        resolved_module_version_code,
    )
    output_directory.mkdir(parents=True, exist_ok=True)
    output = output_directory / f"morphe-{slug}-module-{version_name}-{arch}.zip"
    update_path = output.with_suffix(".update.json")
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"refusing to overwrite module: {output}")
    if update and (update_path.exists() or update_path.is_symlink()):
        raise FileExistsError(f"refusing to overwrite update metadata: {update_path}")
    config = _config(
        module_id,
        f"Morphe {slug.replace('-', ' ').title()} {arch}",
        package,
        version_name,
        version_code,
        module_arch,
    )
    module_prop = _module_prop(
        module_id,
        f"Morphe {slug.replace('-', ' ').title()} ({arch})",
        version_name,
        resolved_module_version_code,
        update_json_url,
    )
    config_data = {
        "module_id": module_id,
        "package": package,
        "version": {"name": version_name, "code": version_code},
        "module_version_code": resolved_module_version_code,
        "architecture": arch,
        "stock_mode": "merged+splits"
        if merged_stock and selected_splits
        else "merged"
        if merged_stock
        else "splits",
    }
    provenance = {
        "schema_version": 1,
        **config_data,
        "payload": {
            "patched": _file_record("base.apk", patched_apk),
            "stock": [_file_record(name, path) for name, path in stock_files],
        },
        "source_provenance": _file_record(source_provenance.name, source_provenance),
        "device_validation": "pending",
    }
    entries: dict[str, tuple[bytes | Path, int]] = {
        "module.prop": (module_prop.encode(), 0o644),
        "config": (config.encode(), 0o644),
        "config.json": (
            (json.dumps(config_data, indent=2, sort_keys=True) + "\n").encode(),
            0o644,
        ),
        MODULE_PROVENANCE: (
            (json.dumps(provenance, indent=2, sort_keys=True) + "\n").encode(),
            0o644,
        ),
        "customize.sh": (CUSTOMIZE_SH.encode(), 0o755),
        "service.sh": (SERVICE_SH.encode(), 0o755),
        "action.sh": (ACTION_SH.encode(), 0o755),
        "uninstall.sh": (UNINSTALL_SH.encode(), 0o755),
        "utils.sh": (UTILS_SH.encode(), 0o755),
        "skip_mount": (b"", 0o644),
        "base.apk": (patched_apk, 0o644),
    }
    entries.update({name: (path, 0o644) for name, path in stock_files})
    with tempfile.NamedTemporaryFile(
        "wb", dir=output_directory, prefix=f".{output.name}-", delete=False
    ) as temporary:
        temporary_path = Path(temporary.name)
    try:
        _write_zip(temporary_path, entries)
        _inspect_module(temporary_path, module_id, entries)
        os.replace(temporary_path, output)
    finally:
        temporary_path.unlink(missing_ok=True)
    if update:
        _atomic_json(update_path, update)
    return output


def _verify_apk(
    path: Path,
    *,
    package: str,
    version_name: str,
    version_code: str,
    arch: str,
    signed: bool,
    label: str,
    allow_corrupted_version_code: bool = False,
) -> None:
    if not path.is_file() or path.is_symlink():
        raise ModuleError(f"missing or invalid {label} APK")
    try:
        metadata = inspect_apk(path, path.name, verify_signature=signed)
    except IntegrityMetadataError as error:
        raise ModuleError(f"invalid {label} APK") from error
    if metadata.package != package:
        raise ModuleError(f"{label} package mismatch")
    version_code_matches = metadata.version_code == version_code or (
        allow_corrupted_version_code and metadata.version_code == CORRUPTED_VERSION_CODE
    )
    if metadata.version_name != version_name or not version_code_matches:
        raise ModuleError(f"{label} version mismatch")
    if (
        metadata.split_type != "base"
        or metadata.required_splits
        or metadata.required_split_types
    ):
        raise ModuleError(f"{label} retains split requirements")
    if metadata.abi not in {arch, "universal"}:
        raise ModuleError(f"{label} architecture mismatch")
    if signed and not metadata.signers_sha256:
        raise ModuleError("patched APK is unsigned")


def _verified_splits(
    provenance_path: Path,
    selected_splits: Sequence[Path],
    *,
    package: str,
    version_name: str,
    version_code: str,
    arch: str,
) -> list[tuple[str, Path]]:
    provenance = _read_json(provenance_path, "source provenance")
    if (
        provenance.get("package") != package
        or provenance.get("version") != {"name": version_name, "code": version_code}
        or provenance.get("architecture") != arch
    ):
        raise ModuleError("source provenance identity mismatch")
    files = provenance.get("files")
    if not isinstance(files, list):
        raise ModuleError("invalid source provenance")
    expected = {
        item.get("normalized_filename"): item
        for item in files
        if isinstance(item, dict) and isinstance(item.get("normalized_filename"), str)
    }
    result = []
    for path in selected_splits:
        if not path.is_file() or path.is_symlink() or path.suffix.lower() != ".apk":
            raise ModuleError("invalid selected stock split")
        item = expected.get(path.name)
        if (
            not isinstance(item, dict)
            or item.get("size") != path.stat().st_size
            or item.get("sha256") != sha256_file(path)
        ):
            raise ModuleError(f"selected split is not verified: {path.name}")
        result.append((f"stock/splits/{path.name}", path))
    return result


def _config(
    module_id: str,
    module_name: str,
    package: str,
    version_name: str,
    version_code: str,
    module_arch: str,
) -> str:
    values = {
        "MODULE_ID": module_id,
        "MODULE_NAME": module_name,
        "PKG_NAME": package,
        "PKG_VERSION": version_name,
        "PKG_VERSION_CODE": version_code,
        "MODULE_ARCH": module_arch,
    }
    for value in values.values():
        _safe_value(value, "module config")
    return "".join(f"{key}={value}\n" for key, value in values.items())


def _module_prop(
    module_id: str,
    name: str,
    version: str,
    version_code: int,
    update_json_url: str | None,
) -> str:
    lines = [
        f"id={module_id}",
        f"name={name}",
        f"version={version}",
        f"versionCode={version_code}",
        "author=Morphe TUI",
        "description=Verified architecture-specific Morphe module",
    ]
    if update_json_url:
        lines.append(f"updateJson={update_json_url}")
    return "\n".join(lines) + "\n"


def _update_metadata(
    update_json_url: str | None,
    zip_url: str | None,
    changelog_url: str | None,
    version: str,
    version_code: int,
) -> dict[str, object] | None:
    values = (update_json_url, zip_url, changelog_url)
    if not any(values):
        return None
    if not all(values):
        raise ModuleError("release update requires update, ZIP, and changelog URLs")
    for value in values:
        _https_url(value or "")
    return {
        "version": version,
        "versionCode": version_code,
        "zipUrl": zip_url,
        "changelog": changelog_url,
    }


def _write_zip(path: Path, entries: dict[str, tuple[bytes | Path, int]]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name in sorted(entries):
            _safe_archive_name(name)
            value, mode = entries[name]
            data = value.read_bytes() if isinstance(value, Path) else value
            info = zipfile.ZipInfo(name, FIXED_ZIP_TIME)
            info.create_system = 3
            info.external_attr = (0o100000 | mode) << 16
            info.compress_type = (
                zipfile.ZIP_STORED if name.endswith(".apk") else zipfile.ZIP_DEFLATED
            )
            archive.writestr(info, data)


def _inspect_module(
    path: Path, module_id: str, entries: dict[str, tuple[bytes | Path, int]]
) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or names != sorted(entries):
                raise ModuleError("module archive contents are not deterministic")
            if archive.testzip() is not None:
                raise ModuleError("module archive CRC verification failed")
            prop = dict(
                line.split("=", 1)
                for line in archive.read("module.prop").decode().splitlines()
            )
            if prop.get("id") != module_id or not prop.get("versionCode", "").isdigit():
                raise ModuleError("invalid module.prop")
            for name in names:
                _safe_archive_name(name)
                if name.endswith(".sh") and b"\r\n" in archive.read(name):
                    raise ModuleError("module shell scripts must use LF endings")
            for name, (value, _) in entries.items():
                expected = value.read_bytes() if isinstance(value, Path) else value
                if (
                    hashlib.sha256(archive.read(name)).digest()
                    != hashlib.sha256(expected).digest()
                ):
                    raise ModuleError(f"module payload mismatch: {name}")
    except (OSError, UnicodeError, ValueError, KeyError, zipfile.BadZipFile) as error:
        raise ModuleError("invalid module archive") from error


def _safe_archive_name(name: str) -> None:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or name in {"", "."}:
        raise ModuleError("unsafe module archive path")


def _safe_value(value: str, label: str) -> None:
    if not value or not re.fullmatch(r"[A-Za-z0-9._ /()+-]+", value):
        raise ModuleError(f"invalid {label}")


def _https_url(value: str) -> None:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ModuleError("invalid release URL")


def _file_record(name: str, path: Path) -> dict[str, object]:
    if not path.is_file() or path.is_symlink():
        raise ModuleError(f"missing or invalid input: {path.name}")
    return {"path": name, "size": path.stat().st_size, "sha256": sha256_file(path)}


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise ModuleError(f"missing or invalid {label}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ModuleError(f"invalid {label}") from error
    if not isinstance(data, dict):
        raise ModuleError(f"invalid {label}")
    return data


def _atomic_json(path: Path, data: object) -> None:
    if path.exists() or path.is_symlink():
        raise FileExistsError(f"refusing to overwrite update metadata: {path}")
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}-", delete=False
    ) as temporary:
        json.dump(data, temporary, indent=2, sort_keys=True)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    try:
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
