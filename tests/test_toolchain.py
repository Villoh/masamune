import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from masamune.toolchain import (  # pyright: ignore[reportMissingImports]
    SIGNER_VERSION,
    TOOL_SPECS,
    PreparedToolchain,
    ToolchainError,
    _release_cache_path,
    apksigner_jar,
    cache_release_asset,
    github_release,
    parse_sha256_checksum,
    require_java,
    select_release_asset,
)


class ToolchainTest(unittest.TestCase):
    def setUp(self) -> None:
        self.signer = next(spec for spec in TOOL_SPECS if spec.key == "apk-signer")
        self.release = json.loads(
            (
                Path(__file__).parent / "fixtures/github-release-uber-apk-signer.json"
            ).read_text(encoding="utf-8")
        )

    def test_extracts_and_reuses_embedded_apksigner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            uber = (
                root
                / "tools"
                / "apk-signer"
                / "patrickfav-uber-apk-signer"
                / "v1.3.0"
                / "uber-apk-signer-1.3.0.jar"
            )
            uber.parent.mkdir(parents=True)
            with zipfile.ZipFile(uber, "w") as archive:
                archive.writestr("lib/apksigner_33_0_2.jar", b"apksigner")
            uber.with_name(f"{uber.name}.json").write_text(
                json.dumps(
                    {
                        "asset": uber.name,
                        "tag": "v1.3.0",
                        "sha256": hashlib.sha256(uber.read_bytes()).hexdigest(),
                    }
                )
            )
            extracted = apksigner_jar(root)
            self.assertEqual(extracted.read_bytes(), b"apksigner")
            extracted.write_bytes(b"tampered")
            self.assertEqual(apksigner_jar(root).read_bytes(), b"apksigner")
            uber.write_bytes(b"tampered")
            with self.assertRaisesRegex(ToolchainError, "integrity"):
                apksigner_jar(root)

    def test_embedded_apksigner_must_exist_and_be_unambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ToolchainError, "run 'masamune'"):
                apksigner_jar(root)
            uber = root / "tools/apk-signer/repo/v1.3.0/uber-apk-signer-1.jar"
            uber.parent.mkdir(parents=True)
            with zipfile.ZipFile(uber, "w") as archive:
                archive.writestr("lib/apksigner_1.jar", b"one")
                archive.writestr("lib/apksigner_2.jar", b"two")
            uber.with_name(f"{uber.name}.json").write_text(
                json.dumps(
                    {
                        "asset": uber.name,
                        "tag": "v1.3.0",
                        "sha256": hashlib.sha256(uber.read_bytes()).hexdigest(),
                    }
                )
            )
            with self.assertRaisesRegex(ToolchainError, "found 2"):
                apksigner_jar(root)

    def test_requires_java_21_with_clear_version(self) -> None:
        result = subprocess.CompletedProcess(
            ["java", "-version"], 0, "", 'openjdk version "17.0.12"\n'
        )
        with (
            patch("masamune.toolchain.shutil.which", return_value="/bin/java"),
            patch("masamune.toolchain.subprocess.run", return_value=result),
            self.assertRaisesRegex(ToolchainError, "Java 21.*found Java 17"),
        ):
            require_java()

    def test_release_lookup_uses_token_caches_and_selects_one_asset(self) -> None:
        requests = []

        def open_fixture(request, timeout):
            requests.append((request, timeout))
            return io.BytesIO(json.dumps(self.release).encode())

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict("os.environ", {"GITHUB_TOKEN": "secret"}),
        ):
            cache = Path(directory)
            release = github_release(
                self.signer, SIGNER_VERSION, cache, opener=open_fixture
            )
            asset = select_release_asset(self.signer, release)
            self.assertEqual(asset["name"], "uber-apk-signer-1.3.0.jar")
            self.assertEqual(
                requests[0][0].get_header("Authorization"), "Bearer secret"
            )
            cached = github_release(
                self.signer,
                SIGNER_VERSION,
                cache,
                opener=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    URLError("offline")
                ),
            )
            self.assertEqual(cached["tag_name"], "v1.3.0")
            cache_text = next((cache / "github-releases").iterdir()).read_text()
            self.assertNotIn("secret", cache_text)

    def test_source_is_part_of_release_cache_key(self) -> None:
        alternate = replace(self.signer, owner="example", repo="patches")
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            self.assertNotEqual(
                _release_cache_path(cache, self.signer, "v1.3.0"),
                _release_cache_path(cache, alternate, "v1.3.0"),
            )

    def test_rejects_ambiguous_release_assets(self) -> None:
        release = deepcopy(self.release)
        duplicate = dict(release["assets"][0])
        duplicate["name"] = "uber-apk-signer-other.jar"
        duplicate["browser_download_url"] = (
            "https://github.com/patrickfav/uber-apk-signer/releases/download/"
            "v1.3.0/uber-apk-signer-other.jar"
        )
        release["assets"].append(duplicate)
        with self.assertRaisesRegex(ToolchainError, "exactly one"):
            select_release_asset(self.signer, release)

    def test_rejects_authenticated_asset_url(self) -> None:
        release = deepcopy(self.release)
        release["assets"][0]["browser_download_url"] += "?token=secret"
        with self.assertRaisesRegex(ToolchainError, "invalid download URL"):
            select_release_asset(self.signer, release)

    def test_verifies_upstream_checksum_and_reuses_matching_file(self) -> None:
        digest = hashlib.sha256(b"jar").hexdigest()
        checksum = f"{digest}  uber-apk-signer-1.3.0.jar\n".encode()
        self.assertEqual(
            parse_sha256_checksum(checksum, "uber-apk-signer-1.3.0.jar"), digest
        )
        nested = f"{digest}  patches/build/libs/patches-1.16.0.mpp\n".encode()
        self.assertEqual(parse_sha256_checksum(nested, "patches-1.16.0.mpp"), digest)
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            with patch(
                "masamune.toolchain._download_bytes",
                side_effect=[checksum, b"jar"],
            ):
                first = cache_release_asset(self.signer, self.release, cache)
            with patch(
                "masamune.toolchain._download_bytes", return_value=checksum
            ) as download:
                second = cache_release_asset(self.signer, self.release, cache)
            self.assertEqual(first["path"], second["path"])
            self.assertEqual(second["sha256"], digest)
            self.assertEqual(second["upstream_sha256"], digest)
            download.assert_called_once()
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "masamune.toolchain._download_bytes", return_value=b"jar"
            ) as download:
                result = cache_release_asset(
                    self.signer,
                    self.release,
                    Path(directory),
                    expected_sha256=digest,
                )
            self.assertEqual(result["sha256"], digest)
            self.assertIsNone(result["upstream_sha256"])
            self.assertEqual(result["expected_sha256"], digest)
            download.assert_called_once()

    def test_prepared_toolchain_exposes_named_verified_tools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            names = {
                "morphe-cli": "cli.jar",
                "morphe-patches": "patches.mpp",
                "apkeditor": "apkeditor.jar",
                "apk-signer": "signer.jar",
            }
            assets = {
                name: root / "tools" / name / "repo" / "v1" / filename
                for name, filename in names.items()
            }
            for asset in assets.values():
                asset.parent.mkdir(parents=True, exist_ok=True)
                asset.write_bytes(b"tool")
            provenance = (
                root / "toolchains" / "cli" / "patches" / "toolchain-provenance.json"
            )
            provenance.parent.mkdir(parents=True)
            provenance.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "java": {
                            "executable": str(Path(sys.executable).resolve()),
                            "sha256": hashlib.sha256(
                                Path(sys.executable).read_bytes()
                            ).hexdigest(),
                        },
                        "tools": [
                            {
                                "name": name,
                                "path": str(asset),
                                "resolved_tag": "v1",
                                "size": asset.stat().st_size,
                                "sha256": hashlib.sha256(
                                    asset.read_bytes()
                                ).hexdigest(),
                            }
                            for name, asset in assets.items()
                        ],
                    }
                ),
                encoding="utf-8",
            )
            toolchain = PreparedToolchain.from_provenance(provenance)
            data = json.loads(provenance.read_text())
            data["tools"][0]["sha256"] = "0" * 64
            provenance.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ToolchainError, "does not match"):
                PreparedToolchain.from_provenance(provenance)
        self.assertEqual(toolchain.morphe_cli, assets["morphe-cli"])
        self.assertEqual(toolchain.patches_version, "v1")
        self.assertEqual(toolchain.apkeditor_version, "v1")
        self.assertEqual(toolchain.signer_version, "v1")


if __name__ == "__main__":
    unittest.main()
