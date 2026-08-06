import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from masamune.apk import (
    IntegrityMetadataError,
    validate_zip_archive,
    verify_google_delivery,
)
from masamune.toolchain import (  # pyright: ignore[reportMissingImports]
    ToolchainError,
    _download_bytes,
    cache_lock,
)


class SubprocessSafetyTest(unittest.TestCase):
    def test_source_never_enables_shell_execution(self) -> None:
        source = Path(__file__).parents[1] / "src"
        combined = "\n".join(
            path.read_text(encoding="utf-8") for path in source.glob("*.py")
        )
        self.assertNotIn("shell=True", combined)


class DownloadSafetyTest(unittest.TestCase):
    def test_rejects_google_file_over_size_limit_before_reading(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "files": [
                            {
                                "path": "base.apk",
                                "size": 301 * 1024 * 1024,
                                "algorithm": "sha256",
                                "digest": "",
                                "google_sha1": "",
                                "google_sha256": "",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(IntegrityMetadataError, "invalid size"):
                verify_google_delivery(root, manifest)


class ArchiveSafetyTest(unittest.TestCase):
    def test_rejects_traversal_and_excessive_compression_ratio(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            traversal = root / "traversal.zip"
            with zipfile.ZipFile(traversal, "w") as archive:
                archive.writestr("../escape", b"x")
            with self.assertRaisesRegex(IntegrityMetadataError, "unsafe archive"):
                validate_zip_archive(traversal)

            bomb = root / "bomb.zip"
            with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("payload", b"0" * 100_000)
            with (
                patch("masamune.apk.MAX_ZIP_RATIO", 2),
                self.assertRaisesRegex(IntegrityMetadataError, "compression ratio"),
            ):
                validate_zip_archive(bomb)


    def test_tool_download_retries_and_size_limit(self) -> None:
        calls = 0

        def unavailable(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise URLError("offline")

        with (
            patch("masamune.toolchain.urlopen", side_effect=unavailable),
            self.assertRaises(ToolchainError),
        ):
            _download_bytes("https://example.invalid/tool.jar", attempts=2)
        self.assertEqual(calls, 2)


class CacheLockTest(unittest.TestCase):
    def test_concurrent_cache_writer_times_out_without_removing_owner_lock(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "tool.lock"

            def acquire_second_writer() -> None:
                with cache_lock(lock, timeout=0):
                    self.fail("second writer acquired lock")

            with cache_lock(lock), self.assertRaisesRegex(ToolchainError, "locked"):
                acquire_second_writer()
            self.assertFalse(lock.exists())


if __name__ == "__main__":
    unittest.main()
