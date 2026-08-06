import hashlib
import json
import subprocess
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from masamune.apk import ApkMetadata  # pyright: ignore[reportMissingImports]
from masamune.merge import (  # pyright: ignore[reportMissingImports]
    MERGE_PROVENANCE,
    MergeError,
    apkeditor_command,
    merge_splits,
    record_signed_merged_stock,
    select_splits,
    verify_merged_apk,
    write_selection_manifest,
)


class SplitSelectionTest(unittest.TestCase):
    def test_selects_arm64_density_all_languages_and_required_feature(self) -> None:
        metadata = fixture_metadata()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_sources(root, metadata)
            selection = select_splits(root, metadata, arch="arm64", density="xxhdpi")
            names = {item.path for item in selection.files}
            self.assertEqual(
                names,
                {
                    "base.apk",
                    "config.arm64.apk",
                    "config.xxhdpi.apk",
                    "config.en.apk",
                    "config.es.apk",
                    "feature.camera.apk",
                    "feature.camera.arm64.apk",
                },
            )
            self.assertNotIn("config.armv7.apk", names)
            first = write_selection_manifest(selection, root).read_bytes()
            second = write_selection_manifest(selection, root).read_bytes()
            self.assertEqual(first, second)
            manifest = json.loads(first)
            self.assertEqual(manifest["language_policy"], "all-verified")
            self.assertEqual(
                [item["path"] for item in manifest["files"]], sorted(names)
            )

    def test_selects_armv7_independently(self) -> None:
        metadata = fixture_metadata()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_sources(root, metadata)
            selection = select_splits(root, metadata, arch="armv7", density="xxhdpi")
        names = {item.path for item in selection.files}
        self.assertIn("config.armv7.apk", names)
        self.assertIn("feature.camera.armv7.apk", names)
        self.assertNotIn("config.arm64.apk", names)

    def test_rejects_ambiguous_density_and_unsupported_delivery(self) -> None:
        metadata = fixture_metadata() + [
            apk_metadata(
                path="config.xhdpi.apk",
                split_name="config.xhdpi",
                split_type="density",
                density="xhdpi",
            )
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_sources(root, metadata)
            with self.assertRaisesRegex(MergeError, "explicit density"):
                select_splits(root, metadata, arch="arm64")
            unsupported = [
                replace(metadata[0], unsupported_requirements=("play-asset-delivery",))
            ]
            with self.assertRaisesRegex(MergeError, "play-asset-delivery"):
                select_splits(root, unsupported, arch="arm64")


class APKEditorMergeTest(unittest.TestCase):
    def test_uses_argument_array_copies_inputs_and_records_verified_output(
        self,
    ) -> None:
        metadata = [
            replace(fixture_metadata()[0], required_splits=()),
            *fixture_metadata()[1:3],
        ]
        merged_metadata = apk_metadata(abi="arm64")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "verified"
            source.mkdir()
            write_sources(source, metadata)
            before = {item.path: (source / item.path).read_bytes() for item in metadata}
            selection = select_splits(source, metadata, arch="arm64", density="xxhdpi")
            tool = root / "APKEditor.jar"
            tool.write_bytes(b"tool")
            output = root / "merged-stock.apk"
            seen = {}

            def run(command, **kwargs):
                seen["command"] = command
                seen["kwargs"] = kwargs
                inputs = Path(command[command.index("-i") + 1])
                seen["inputs"] = sorted(path.name for path in inputs.iterdir())
                merged = Path(command[command.index("-o") + 1])
                with zipfile.ZipFile(merged, "w") as apk:
                    apk.writestr("AndroidManifest.xml", b"manifest")
                    apk.writestr("lib/arm64-v8a/libtest.so", b"native")
                return subprocess.CompletedProcess(
                    command,
                    0,
                    "download https://example.invalid/?token=secret",
                    "",
                )

            with (
                patch("masamune.merge.subprocess.run", side_effect=run),
                patch("masamune.merge.inspect_apk", return_value=merged_metadata),
            ):
                merge_splits(
                    selection,
                    output,
                    java="java",
                    apkeditor=tool,
                    apkeditor_version="V1.4.9",
                )
            self.assertEqual(seen["command"][:4], ["java", "-jar", str(tool), "m"])
            self.assertNotIn("shell", seen["kwargs"])
            self.assertEqual(
                seen["inputs"], sorted(item.path for item in selection.files)
            )
            self.assertEqual(
                before,
                {item.path: (source / item.path).read_bytes() for item in metadata},
            )
            provenance = json.loads((root / MERGE_PROVENANCE).read_text())
            self.assertFalse(provenance["output"]["google_signature_retained"])
            self.assertEqual(provenance["output"]["sha256"], sha256(output))
            self.assertNotIn("secret", provenance["sanitized_tool_output"])
            self.assertNotIn("https://", provenance["sanitized_tool_output"])
            signed = root / "merged-stock-signed.apk"
            signed.write_bytes(b"signed")
            record_signed_merged_stock(root / MERGE_PROVENANCE, signed, merged_metadata)
            provenance = json.loads((root / MERGE_PROVENANCE).read_text())
            self.assertEqual(
                provenance["signed_output"]["certificate_sha256"], ["a" * 64]
            )
            self.assertEqual(provenance["signed_output"]["path"], signed.name)

    def test_rejects_nonzero_and_missing_output(self) -> None:
        metadata = [apk_metadata()]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_sources(root, metadata)
            selection = select_splits(root, metadata, arch="arm64")
            tool = root / "APKEditor.jar"
            tool.write_bytes(b"tool")
            for code in (1, 0):
                with (
                    self.subTest(code=code),
                    patch(
                        "masamune.merge.subprocess.run",
                        return_value=subprocess.CompletedProcess([], code, "", ""),
                    ),
                    self.assertRaises(MergeError),
                ):
                    merge_splits(
                        selection,
                        root / f"merged-{code}.apk",
                        java="java",
                        apkeditor=tool,
                        apkeditor_version="V1.4.9",
                    )

    def test_accepts_universal_merged_apk_for_architecture(self) -> None:
        selection = select_splits(
            Path.cwd(), [apk_metadata(abi="universal")], arch="armv7"
        )
        with tempfile.TemporaryDirectory() as directory:
            apk = Path(directory) / "merged.apk"
            with zipfile.ZipFile(apk, "w") as archive:
                archive.writestr("AndroidManifest.xml", b"manifest")
            with patch(
                "masamune.merge.inspect_apk",
                return_value=apk_metadata(abi="universal"),
            ):
                verify_merged_apk(apk, selection)

    def test_verifies_merged_identity_architecture_and_split_requirements(self) -> None:
        selection = select_splits_fixture()
        with tempfile.TemporaryDirectory() as directory:
            apk = Path(directory) / "merged.apk"
            with zipfile.ZipFile(apk, "w") as archive:
                archive.writestr("AndroidManifest.xml", b"manifest")
            cases = (
                (apk_metadata(package="com.wrong.app", abi="arm64"), "package"),
                (apk_metadata(version_code="999", abi="arm64"), "version"),
                (apk_metadata(abi="armv7"), "architecture"),
                (apk_metadata(abi="arm64", required_splits=("feature.x",)), "split"),
            )
            for metadata, message in cases:
                with (
                    self.subTest(message=message),
                    patch("masamune.merge.inspect_apk", return_value=metadata),
                    self.assertRaisesRegex(MergeError, message),
                ):
                    verify_merged_apk(apk, selection)

    def test_apkeditor_command_matches_upstream_merge_cli(self) -> None:
        self.assertEqual(
            apkeditor_command(
                "java", Path("APKEditor.jar"), Path("inputs"), Path("out.apk")
            ),
            [
                "java",
                "-jar",
                "APKEditor.jar",
                "m",
                "-i",
                "inputs",
                "-o",
                "out.apk",
            ],
        )


def fixture_metadata() -> list[ApkMetadata]:
    return [
        apk_metadata(required_splits=("feature.camera",)),
        apk_metadata(
            path="config.arm64.apk",
            split_name="config.arm64_v8a",
            split_type="abi",
            abi="arm64",
        ),
        apk_metadata(
            path="config.xxhdpi.apk",
            split_name="config.xxhdpi",
            split_type="density",
            density="xxhdpi",
        ),
        apk_metadata(
            path="config.armv7.apk",
            split_name="config.armeabi_v7a",
            split_type="abi",
            abi="armv7",
        ),
        apk_metadata(
            path="config.en.apk",
            split_name="config.en",
            split_type="language",
            language="en",
        ),
        apk_metadata(
            path="config.es.apk",
            split_name="config.es",
            split_type="language",
            language="es",
        ),
        apk_metadata(
            path="feature.camera.apk",
            split_name="feature.camera",
            split_type="feature",
            feature="feature.camera",
        ),
        apk_metadata(
            path="feature.camera.arm64.apk",
            split_name="config.feature.camera.arm64",
            split_type="abi",
            abi="arm64",
            feature="feature.camera",
        ),
        apk_metadata(
            path="feature.camera.armv7.apk",
            split_name="config.feature.camera.armv7",
            split_type="abi",
            abi="armv7",
            feature="feature.camera",
        ),
    ]


def select_splits_fixture():
    root = Path.cwd()
    metadata = [
        apk_metadata(),
        apk_metadata(
            path="config.arm64.apk",
            split_name="config.arm64_v8a",
            split_type="abi",
            abi="arm64",
        ),
    ]
    return select_splits(root, metadata, arch="arm64")


def apk_metadata(**changes) -> ApkMetadata:
    return replace(
        ApkMetadata(
            path="base.apk",
            package="com.example.app",
            version_name="1.2.3",
            version_code="123",
            split_name=None,
            split_type="base",
            abi=None,
            density=None,
            language=None,
            feature=None,
            platform_split_types=(),
            required_split_types=(),
            required_splits=(),
            required_features=(),
            signers_sha256=("a" * 64,),
        ),
        **changes,
    )


def write_sources(root: Path, metadata: list[ApkMetadata]) -> None:
    for item in metadata:
        (root / item.path).write_bytes(f"verified:{item.path}".encode())


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
