import hashlib
import json
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from masamune.apk import ApkMetadata  # pyright: ignore[reportMissingImports]
from masamune.module import (  # pyright: ignore[reportMissingImports]
    ACTION_SH,
    CUSTOMIZE_SH,
    SERVICE_SH,
    UNINSTALL_SH,
    UTILS_SH,
    ModuleError,
    build_module,
)


class ModuleBuildTest(unittest.TestCase):
    def test_builds_deterministic_magisk_kernelsu_module(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            patched = root / "patched.apk"
            stock = root / "stock.apk"
            split = root / "config.arm64.apk"
            for path in (patched, stock, split):
                path.write_bytes(path.name.encode())
            provenance = root / "provenance.json"
            provenance.write_text(
                json.dumps(
                    {
                        "package": "com.google.android.youtube",
                        "version": {"name": "21.04.223", "code": "1561052632"},
                        "architecture": "arm64",
                        "files": [
                            {
                                "normalized_filename": split.name,
                                "size": split.stat().st_size,
                                "sha256": sha256(split),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            metadata = replace(apk_metadata(), abi="universal")
            with patch("masamune.module.inspect_apk", return_value=metadata):
                first = build_module(
                    package="com.google.android.youtube",
                    slug="youtube",
                    arch="arm64-v8a",
                    version_name="21.04.223",
                    version_code="1561052632",
                    patched_apk=patched,
                    source_provenance=provenance,
                    output_directory=root / "one",
                    merged_stock=stock,
                    selected_splits=(split,),
                    module_version_code=42,
                    update_json_url="https://example.invalid/youtube-arm64.json",
                    zip_url="https://example.invalid/youtube-arm64.zip",
                    changelog_url="https://example.invalid/changelog.md",
                )
                second = build_module(
                    package="com.google.android.youtube",
                    slug="youtube",
                    arch="arm64-v8a",
                    version_name="21.04.223",
                    version_code="1561052632",
                    patched_apk=patched,
                    source_provenance=provenance,
                    output_directory=root / "two",
                    merged_stock=stock,
                    selected_splits=(split,),
                    update_json_url="https://example.invalid/youtube-arm64.json",
                    zip_url="https://example.invalid/youtube-arm64.zip",
                    changelog_url="https://example.invalid/changelog.md",
                    module_version_code=42,
                )
            self.assertEqual(
                first.name, "morphe-youtube-module-21.04.223-arm64-v8a.zip"
            )
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with zipfile.ZipFile(first) as archive:
                names = archive.namelist()
                self.assertEqual(names, sorted(names))
                self.assertIn("base.apk", names)
                self.assertIn("stock/base.apk", names)
                self.assertIn("stock/splits/config.arm64.apk", names)
                self.assertIn("customize.sh", names)
                self.assertIn("service.sh", names)
                self.assertIn("action.sh", names)
                self.assertIn("uninstall.sh", names)
                self.assertIn("utils.sh", names)
                self.assertIn("skip_mount", names)
                prop = archive.read("module.prop").decode()
                config = archive.read("config").decode()
                self.assertIn("id=morphe.youtube.arm64-v8a", prop)
                self.assertIn("MODULE_ARCH=arm64", config)
                self.assertIn(
                    "updateJson=https://example.invalid/youtube-arm64.json", prop
                )
                self.assertIn("versionCode=42", prop)
                self.assertEqual(
                    archive.getinfo("service.sh").external_attr >> 16 & 0o777, 0o755
                )
                module_provenance = json.loads(archive.read("module-provenance.json"))
                self.assertEqual(module_provenance["device_validation"], "pending")
                self.assertEqual(module_provenance["module_version_code"], 42)
            update = json.loads(first.with_suffix(".update.json").read_text())
            self.assertEqual(update["versionCode"], 42)
            self.assertEqual(
                update["zipUrl"], "https://example.invalid/youtube-arm64.zip"
            )

    def test_builds_armv7_music_module_with_runtime_arch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            patched = root / "patched.apk"
            stock = root / "stock.apk"
            patched.write_bytes(b"patched")
            stock.write_bytes(b"stock")
            provenance = root / "provenance.json"
            provenance.write_text(
                json.dumps(
                    {
                        "package": "com.google.android.apps.youtube.music",
                        "version": {"name": "9.15.51", "code": "91551230"},
                        "architecture": "armv7",
                        "files": [],
                    }
                ),
                encoding="utf-8",
            )
            metadata = replace(
                apk_metadata(),
                package="com.google.android.apps.youtube.music",
                version_name="9.15.51",
                version_code="91551230",
                abi="armv7",
            )
            with patch("masamune.module.inspect_apk", return_value=metadata):
                output = build_module(
                    package="com.google.android.apps.youtube.music",
                    slug="youtube-music",
                    arch="arm-v7a",
                    version_name="9.15.51",
                    version_code="91551230",
                    patched_apk=patched,
                    source_provenance=provenance,
                    output_directory=root / "out",
                    merged_stock=stock,
                )
            self.assertEqual(
                output.name, "morphe-youtube-music-module-9.15.51-arm-v7a.zip"
            )
            with zipfile.ZipFile(output) as archive:
                self.assertIn(
                    "id=morphe.youtube-music.arm-v7a",
                    archive.read("module.prop").decode(),
                )
                self.assertIn("MODULE_ARCH=arm", archive.read("config").decode())

    def test_rejects_unverified_split_and_non_root_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            patched = root / "patched.apk"
            split = root / "split.apk"
            patched.write_bytes(b"patched")
            split.write_bytes(b"modified")
            provenance = root / "provenance.json"
            provenance.write_text(
                json.dumps(
                    {
                        "package": "com.google.android.youtube",
                        "version": {"name": "21.04.223", "code": "1561052632"},
                        "architecture": "arm64",
                        "files": [
                            {
                                "normalized_filename": split.name,
                                "size": split.stat().st_size,
                                "sha256": "0" * 64,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch("masamune.module.inspect_apk", return_value=apk_metadata()),
                self.assertRaisesRegex(ModuleError, "not verified"),
            ):
                build_module(
                    package="com.google.android.youtube",
                    arch="arm64-v8a",
                    version_name="21.04.223",
                    version_code="1561052632",
                    patched_apk=patched,
                    source_provenance=provenance,
                    output_directory=root / "out",
                    selected_splits=(split,),
                )
            with (
                patch(
                    "masamune.module.inspect_apk",
                    return_value=replace(
                        apk_metadata(), package="app.morphe.android.youtube"
                    ),
                ),
                self.assertRaisesRegex(ModuleError, "package mismatch"),
            ):
                build_module(
                    package="com.google.android.youtube",
                    arch="arm64-v8a",
                    version_name="21.04.223",
                    version_code="1561052632",
                    patched_apk=patched,
                    source_provenance=provenance,
                    output_directory=root / "out",
                    selected_splits=(split,),
                )

    def test_shell_entrypoints_use_portable_module_paths(self) -> None:
        scripts = (CUSTOMIZE_SH, SERVICE_SH, ACTION_SH, UNINSTALL_SH, UTILS_SH)
        for script in scripts:
            self.assertTrue(script.startswith("#!/system/bin/sh"))
            self.assertNotIn("\r\n", script)
            self.assertNotIn("/data/adb/modules/", script)
        self.assertIn("MODDIR=${0%/*}", SERVICE_SH)
        self.assertIn("${KSU:-false}", UTILS_SH)
        self.assertIn("ksud module config", UTILS_SH)

    def test_customize_installs_signed_stock_on_version_mismatch(self) -> None:
        self.assertIn('installed=$(dumpsys package "$PKG_NAME"', CUSTOMIZE_SH)
        self.assertIn('[ "$installed" = "$PKG_VERSION" ] && return 0', CUSTOMIZE_SH)
        self.assertIn('[ -f "$MODPATH/stock/base.apk" ] || abort', CUSTOMIZE_SH)
        self.assertIn('pm install -r -d "$MODPATH/stock/base.apk"', CUSTOMIZE_SH)
        self.assertIn('pm uninstall "$PKG_NAME"', CUSTOMIZE_SH)
        self.assertIn("install_stock", CUSTOMIZE_SH)


def apk_metadata() -> ApkMetadata:
    return replace(
        ApkMetadata(
            path="base.apk",
            package="com.google.android.youtube",
            version_name="21.04.223",
            version_code="1561052632",
            split_name=None,
            split_type="base",
            abi="arm64",
            density=None,
            language=None,
            feature=None,
            platform_split_types=(),
            required_split_types=(),
            required_splits=(),
            required_features=(),
            signers_sha256=("a" * 64,),
        ),
        unsupported_requirements=(),
    )


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
