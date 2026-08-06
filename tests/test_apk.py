import base64
import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

from masamune.apk import (  # pyright: ignore[reportMissingImports]
    ANDROID_NS,
    EXPECTED_SIGNERS,
    MANIFEST_NAME,
    PROVENANCE_NAME,
    ApkMetadata,
    _manifest_root,
    inspect_apk,
    signer_fingerprints,
    verify_apk_set,
    verify_google_delivery,
    write_provenance,
)
from masamune.errors import ApkMismatch, IntegrityMetadataError


class IntegrityManifestTest(unittest.TestCase):
    def test_sanitized_delivery_fixture_documents_both_digests(self) -> None:
        fixture = json.loads(
            (
                Path(__file__).parent / "fixtures/google-delivery-sanitized.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(set(fixture["base"]["fields"]), {"1", "2", "19"})
        self.assertEqual(set(fixture["splits"][0]["fields"]), {"1", "2", "4", "9"})
        self.assertFalse(
            {"url", "cookies", "token", "dispenser"}
            & {
                key.lower()
                for item in (fixture, fixture["base"], *fixture["splits"])
                for key in item
            }
        )

    def test_accepts_valid_fixture_and_sha1_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = write_manifest(
                root,
                {"base.apk": (b"base", "sha256"), "config.apk": (b"split", "sha1")},
            )
            verify_google_delivery(root, manifest)

    def test_rejects_one_byte_modification_and_truncation(self) -> None:
        for name, content in (("modified", b"basf"), ("truncated", b"bas")):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                manifest = write_manifest(root, {"base.apk": (b"base", "sha256")})
                (root / "base.apk").write_bytes(content)
                with self.assertRaises(IntegrityMetadataError):
                    verify_google_delivery(root, manifest)

    def test_rejects_unsafe_or_incomplete_manifest(self) -> None:
        cases = {
            "missing split": lambda root, data: data["files"].pop(),
            "extra file": lambda root, data: (root / "extra.apk").write_bytes(b"extra"),
            "traversal": lambda root, data: data["files"][0].update(path="../base.apk"),
            "size": lambda root, data: data["files"][0].update(size=999),
            "unknown digest": lambda root, data: data["files"][0].update(
                algorithm="md5"
            ),
            "secret field": lambda root, data: data.update(
                url="https://secret.invalid/token"
            ),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                manifest = write_manifest(
                    root,
                    {
                        "base.apk": (b"base", "sha256"),
                        "split.apk": (b"split", "sha256"),
                    },
                )
                data = json.loads(manifest.read_text())
                mutate(root, data)
                manifest.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(IntegrityMetadataError):
                    verify_google_delivery(root, manifest)


class ApkInspectionTest(unittest.TestCase):
    certificate = base64.b64decode(
        "MIICuTCCAaGgAwIBAgIBATANBgkqhkiG9w0BAQsFADAgMR4wHAYDVQQDDBVTYW5pdGl6ZWQgdGVzdCBzaWduZXIwHhcNMjQwMTAxMDAwMDAwWhcNMzQwMTAxMDAwMDAwWjAgMR4wHAYDVQQDDBVTYW5pdGl6ZWQgdGVzdCBzaWduZXIwggEiMA0GCSqGSIb3DQEBAQUAA4IBDwAwggEKAoIBAQDnFnuspvvxIbGP4I/5ZF/zN4CURM8WrSnHpegN+/6ZANcDe0LBpm1HVbpTDgaUd5ve8h3Gi/zi11glLZgXu9IUfafHvX5oFK7CQEVxWwaLghVylgS1WNg8MJyax1/2sMDXXCU5dPywNaIwaQBWvbhB58F+UP4m5hQaqXmiOFSqvsq5N7aLKRcJDdwnXjA3P4Pd48HZSe9rr2M36eeA+2YlTSK1ZrBEBA/NvN0aaNOEpfFV0kzJZ2yesmG/c3/YWH+7KO6RsyF7485PP/crI1mmZOXBs3nUi6hUs1GpSWNhz6zLd4r2lXb6oMWrKEKN7FZ7t1wUOFmI44NE7dsRShTDAgMBAAEwDQYJKoZIhvcNAQELBQADggEBAA6i+NlnxVeYIazWiPZNHMyckmYVLMddmbFWqod9qudCxvFlkiPhxdyw18Xyxjk3B8qeEmvoYOd0W9k6X5QlKFLKMO+AjAg2ne7AOeuVcmj76aLdykBW67pUhiNKtMiaO0pNchZt2rN4iAJ/6EaoS1z/mZtIARY1Gj45KdJp07zg0o+cYyjZ8mk/dWAgGj3iHwCMCyWnCWtuFTwNQqTQqFkypn5E9ECxizz8XerZLyAbMDI1H3cepZS5LybIRIYuxLYf+28qB6kcPCIWumHr5a8X/kOByZ6MVEJ+j4m+zbO8WcjcfnQqtmF7PEUK8PM+Sq64oq9o+VILFITUCpDo4nU="
    )
    fingerprint = hashlib.sha256(certificate).hexdigest()

    def test_inspects_identity_split_types_features_and_signer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.arm64.apk"
            write_test_apk(
                path,
                self.certificate,
                manifest_xml(
                    split="config.arm64_v8a",
                    required="feature.camera",
                    feature="android.hardware.camera",
                ),
                native_abi="arm64_v8a",
            )
            with patch(
                "masamune.apk.signer_fingerprints",
                return_value=(self.fingerprint,),
            ):
                metadata = inspect_apk(path, path.name)
            self.assertEqual(
                (metadata.package, metadata.version_name, metadata.version_code),
                ("com.example.app", "", "123"),
            )
            self.assertEqual((metadata.split_type, metadata.abi), ("abi", "arm64"))
            self.assertEqual(metadata.required_splits, ("feature.camera",))
            self.assertEqual(metadata.required_features, ("android.hardware.camera",))
            self.assertEqual(metadata.signers_sha256, (self.fingerprint,))

    def test_reads_binary_manifest_and_rejects_unterminated_input(self) -> None:
        fixtures = Path(__file__).with_name("fixtures")
        script = """
import sys
from pathlib import Path
from masamune.apk import _manifest_root

try:
    _manifest_root(Path(sys.argv[1]))
except Exception:
    raise SystemExit(0 if sys.argv[2] == "reject" else 1)
raise SystemExit(0 if sys.argv[2] == "accept" else 1)
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid = root / "valid.apk"
            malformed = root / "malformed.apk"
            write_test_apk(
                valid,
                self.certificate,
                (fixtures / "axml-manifest.bin").read_bytes(),
            )
            write_test_apk(
                malformed,
                self.certificate,
                (fixtures / "axml-manifest-unterminated.bin").read_bytes(),
            )
            parsed = _manifest_root(valid)
            self.assertEqual(parsed.get("package"), "org.t0t0.androguard.TC")
            self.assertEqual(parsed.get(ANDROID_NS + "versionCode"), "1")
            self.assertEqual(parsed.get(ANDROID_NS + "versionName"), "1.0")
            with self.assertRaises(IntegrityMetadataError):
                _manifest_root(malformed)
            for path, expected in ((valid, "accept"), (malformed, "reject")):
                completed = subprocess.run(
                    [sys.executable, "-O", "-c", script, str(path), expected],
                    capture_output=True,
                    check=False,
                    cwd=Path(__file__).parent.parent,
                    text=True,
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    completed.stdout + completed.stderr,
                )
                self.assertEqual(completed.stdout, "")
                self.assertEqual(completed.stderr, "")

    def test_reads_complete_rotated_signing_lineage(self) -> None:
        legacy = "3d7a1223019aa39d9ea0e3436ab7c0896bfb4fb679f4de5fe7c23f326c8f994a"
        rotated = "5aad2bee6db95d17e05a08d7d1e64c10a1511879154483916b6ae6c7fd9cb0c6"
        source_stamp = (
            "3257d599a49d2c961a471ca9843f59d341a405884583fc087df4237b733bbd6d"
        )
        output = (
            f"Signer (minSdkVersion=33) certificate SHA-256 digest: {rotated}\n"
            f"Signer (minSdkVersion=24) certificate SHA-256 digest: {legacy}\n"
            f"Source Stamp Signer certificate SHA-256 digest: {source_stamp}\n"
        )
        result = subprocess.CompletedProcess([], 0, output, "")
        with (
            patch(
                "masamune.apk.apksigner_jar", return_value=Path("apksigner.jar")
            ),
            patch(
                "masamune.apk.subprocess.run", return_value=result
            ) as run_apksigner,
        ):
            self.assertEqual(signer_fingerprints(Path("app.apk")), (legacy, rotated))
        self.assertEqual(
            run_apksigner.call_args.args[0],
            [
                "java",
                "-jar",
                "apksigner.jar",
                "verify",
                "--print-certs",
                "--",
                "app.apk",
            ],
        )

    def test_rejects_failed_apksigner_verification(self) -> None:
        result = subprocess.CompletedProcess([], 1, "", "verification failed")
        with (
            patch(
                "masamune.apk.apksigner_jar", return_value=Path("apksigner.jar")
            ),
            patch("masamune.apk.subprocess.run", return_value=result),
            self.assertRaisesRegex(IntegrityMetadataError, "invalid"),
        ):
            signer_fingerprints(Path("unsigned.apk"))

    def test_rejects_malformed_or_unsigned_apk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.apk"
            with zipfile.ZipFile(path, "w") as apk:
                apk.writestr("AndroidManifest.xml", manifest_xml())
            with (
                patch(
                    "masamune.apk.signer_fingerprints",
                    side_effect=IntegrityMetadataError("unsigned APK"),
                ),
                self.assertRaisesRegex(IntegrityMetadataError, "unsigned"),
            ):
                inspect_apk(path, path.name)

    def test_rejects_wrong_package_version_certificate_and_required_split(self) -> None:
        base = apk_metadata(path="base.apk")
        cases = {
            "package": (
                [
                    base,
                    apk_metadata(
                        path="split.apk",
                        split_name="config.en",
                        split_type="language",
                        package="com.wrong.app",
                    ),
                ],
                {},
                "package",
            ),
            "version": (
                [
                    base,
                    apk_metadata(
                        path="split.apk",
                        split_name="config.en",
                        split_type="language",
                        version_code="124",
                    ),
                ],
                {},
                "version",
            ),
            "certificate": (
                [
                    base,
                    apk_metadata(
                        path="split.apk",
                        split_name="config.en",
                        split_type="language",
                        signers_sha256=("b" * 64,),
                    ),
                ],
                {},
                "signer",
            ),
            "required split": (
                [apk_metadata(path="base.apk", required_splits=("feature.camera",))],
                {},
                "missing required split",
            ),
            "requested version": ([base], {"version_code": "999"}, "version"),
        }
        for name, (metadata, overrides, message) in cases.items():
            with (
                self.subTest(name=name),
                patch("masamune.apk.inspect_apk", side_effect=metadata),
                tempfile.TemporaryDirectory() as directory,
            ):
                for item in metadata:
                    (Path(directory) / item.path).write_bytes(b"placeholder")
                error = (
                    ApkMismatch
                    if name in {"package", "version", "requested version"}
                    else IntegrityMetadataError
                )
                with self.assertRaisesRegex(error, message):
                    verify_apk_set(
                        Path(directory),
                        "com.example.app",
                        version_name=None,
                        version_code=overrides.get("version_code"),
                        arch="arm64",
                    )

    def test_rejects_optional_pinned_signer(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("masamune.apk.inspect_apk", return_value=apk_metadata()),
        ):
            (Path(directory) / "base.apk").write_bytes(b"placeholder")
            with self.assertRaisesRegex(IntegrityMetadataError, "pinned lineage"):
                verify_apk_set(
                    Path(directory),
                    "com.example.app",
                    version_name=None,
                    version_code=None,
                    arch="arm64",
                    expected_signer="b" * 64,
                )

    def test_requires_complete_pinned_google_lineage(self) -> None:
        lineage = EXPECTED_SIGNERS["com.google.android.youtube"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "base.apk"
            path.write_bytes(b"placeholder")
            with patch(
                "masamune.apk.inspect_apk",
                return_value=apk_metadata(
                    package="com.google.android.youtube",
                    signers_sha256=lineage,
                ),
            ):
                verify_apk_set(
                    Path(directory),
                    "com.google.android.youtube",
                    version_name=None,
                    version_code=None,
                    arch="arm64",
                )
            with (
                patch(
                    "masamune.apk.inspect_apk",
                    return_value=apk_metadata(
                        package="com.google.android.youtube",
                        signers_sha256=(lineage[1],),
                    ),
                ),
                self.assertRaisesRegex(IntegrityMetadataError, "lineage"),
            ):
                verify_apk_set(
                    Path(directory),
                    "com.google.android.youtube",
                    version_name=None,
                    version_code=None,
                    arch="arm64",
                )

    def test_rejects_wrong_abi_and_pinned_signer(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "masamune.apk.inspect_apk",
                return_value=apk_metadata(
                    package="com.google.android.youtube", abi="armv7"
                ),
            ),
        ):
            (Path(directory) / "base.apk").write_bytes(b"placeholder")
            with self.assertRaises(IntegrityMetadataError):
                verify_apk_set(
                    Path(directory),
                    "com.google.android.youtube",
                    version_name=None,
                    version_code=None,
                    arch="arm64",
                )

    def test_accepts_universal_abi(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "masamune.apk.inspect_apk",
                return_value=apk_metadata(abi="universal"),
            ),
        ):
            root = Path(directory)
            (root / "base.apk").write_bytes(b"placeholder")
            verify_apk_set(
                root,
                "com.example.app",
                version_name=None,
                version_code=None,
                arch="arm64",
            )


class ProvenanceTest(unittest.TestCase):
    def test_deterministic_provenance_contains_no_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = write_manifest(
                root,
                {
                    "config.en.apk": (b"split", "sha256"),
                    "base.apk": (b"base", "sha256"),
                },
            )
            metadata = [
                apk_metadata(
                    path="config.en.apk",
                    version_name="",
                    split_name="config.en",
                    split_type="language",
                ),
                apk_metadata(),
            ]
            write_provenance(
                root,
                manifest,
                metadata,
                package="com.example.app",
                arch="arm64",
                profile="D2",
                region="US",
            )
            first = (root / PROVENANCE_NAME).read_bytes()
            write_provenance(
                root,
                manifest,
                metadata,
                package="com.example.app",
                arch="arm64",
                profile="D2",
                region="US",
            )
            self.assertEqual(first, (root / PROVENANCE_NAME).read_bytes())
            data = json.loads(first)
            self.assertEqual(data["version"], {"name": "1.2.3", "code": "123"})
            self.assertEqual(data["schema_version"], 2)
            self.assertEqual(data["provider"], "google-play")
            self.assertEqual(data["inspection_tools"]["axml"], "0.0.2")
            self.assertEqual(data["inspection_tools"]["apksigner"], "33.0.2")
            self.assertEqual(data["files"][0]["original_filename"], "base.apk")
            self.assertEqual(data["files"][0]["normalized_filename"], "base.apk")
            self.assertNotIn("token", first.decode().lower())
            self.assertNotIn("url", first.decode().lower())


@unittest.skipUnless(
    os.environ.get("MORPHE_LIVE_TEST_PACKAGE"),
    "set MORPHE_LIVE_TEST_PACKAGE for opt-in Google Play verification",
)
class LiveGooglePlayTest(unittest.TestCase):
    def test_download_is_verified_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "masamune",
                    "download",
                    os.environ["MORPHE_LIVE_TEST_PACKAGE"],
                    "--output",
                    str(Path(directory) / "verified"),
                ],
                check=True,
            )
            output = Path(directory) / "verified"
            provenance = json.loads((output / PROVENANCE_NAME).read_text())
            package = os.environ["MORPHE_LIVE_TEST_PACKAGE"]
            self.assertEqual(provenance["package"], package)
            self.assertEqual(provenance["downloader"]["name"], "masamune")
            self.assertTrue(provenance["files"])
            if package in EXPECTED_SIGNERS:
                self.assertTrue(
                    set(provenance["certificate_sha256"])
                    & set(EXPECTED_SIGNERS[package])
                )
            for entry in provenance["files"]:
                self.assertTrue((output / entry["normalized_filename"]).is_file())


def apk_metadata(**changes: Any) -> ApkMetadata:
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


def manifest_xml(*, split=None, required=None, feature=None) -> bytes:
    split_attribute = f' split="{split}"' if split else ""
    uses_split = f'<uses-split android:name="{required}" />' if required else ""
    uses_feature = (
        f'<uses-feature android:name="{feature}" android:required="true" />'
        if feature
        else ""
    )
    version_name = "" if split else ' android:versionName="1.2.3"'
    return f'<?xml version="1.0" encoding="utf-8"?><manifest xmlns:android="http://schemas.android.com/apk/res/android" package="com.example.app"{version_name} android:versionCode="123"{split_attribute}>{uses_split}{uses_feature}<application /></manifest>'.encode()


def lp(value: bytes) -> bytes:
    return struct.pack("<I", len(value)) + value


def write_test_apk(
    path: Path, certificate: bytes, manifest: bytes, native_abi: str | None = None
) -> None:
    with zipfile.ZipFile(path, "w") as apk:
        apk.writestr("AndroidManifest.xml", manifest)
        if native_abi:
            apk.writestr(f"lib/{native_abi}/libtest.so", b"test")
    data = path.read_bytes()
    eocd = data.rfind(b"PK\x05\x06")
    central = struct.unpack_from("<I", data, eocd + 16)[0]
    signed_data = lp(b"") + lp(lp(certificate)) + lp(b"")
    signer = lp(signed_data) + lp(b"") + lp(b"")
    value = lp(lp(signer))
    pair = struct.pack("<Q", 4 + len(value)) + struct.pack("<I", 0x7109871A) + value
    size = len(pair) + 24
    block = (
        struct.pack("<Q", size) + pair + struct.pack("<Q", size) + b"APK Sig Block 42"
    )
    updated = bytearray(data[:central] + block + data[central:])
    struct.pack_into("<I", updated, eocd + len(block) + 16, central + len(block))
    path.write_bytes(updated)


def write_manifest(root: Path, files: dict[str, tuple[bytes, str]]) -> Path:
    entries = []
    for name, (content, algorithm) in files.items():
        (root / name).write_bytes(content)
        entries.append(
            {
                "path": name,
                "size": len(content),
                "algorithm": algorithm,
                "digest": encoded_digest(content, algorithm),
                "google_sha1": encoded_digest(content, "sha1"),
                "google_sha256": encoded_digest(content, "sha256"),
            }
        )
    manifest = root / MANIFEST_NAME
    manifest.write_text(json.dumps({"version": 1, "files": entries}), encoding="utf-8")
    return manifest


def encoded_digest(content: bytes, algorithm: str) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.new(algorithm, content).digest())
        .decode()
        .rstrip("=")
    )


if __name__ == "__main__":
    unittest.main()
