import hashlib
import io
import json
import tempfile
import unittest
from dataclasses import replace
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from masamune.apk import ApkMetadata
from masamune.errors import (
    ApkMismatch,
    GooglePlayAuthUnavailable,
    GooglePlayVersionUnavailable,
    IntegrityMetadataError,
)
from masamune.providers import (
    ProviderRequest,
    ProviderResult,
    ProviderUnavailable,
    UrlProvider,
    VersionUnavailable,
    fallback_download,
)
from masamune.providers.apkmirror import _fetch as apkmirror_fetch
from masamune.providers.errors import ProviderAmbiguous, ProviderArtifactMismatch
from masamune.providers.google_play import (
    GooglePlayProvider,
    _delivery_supports_architecture,
)


class StubProvider:
    def __init__(self, name, result=None, error=None):
        self.name = name
        self.result = result
        self.error = error
        self.calls = 0

    def download(self, request):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result or ProviderResult(
            self.name, request.output, request.output / "provenance.json"
        )


class ProviderFallbackTest(unittest.TestCase):
    def test_uses_declared_provider_order(self) -> None:
        request = ProviderRequest(
            "com.example.app", "1.2.3", "123", "arm64", Path("trusted")
        )
        providers = (
            StubProvider("google-play", error=VersionUnavailable("missing")),
            StubProvider("direct", error=ProviderUnavailable("offline")),
            StubProvider("apkmirror"),
        )
        result = fallback_download(request, providers)
        self.assertEqual(result.provider, "apkmirror")
        self.assertEqual([provider.calls for provider in providers], [1, 1, 1])

    def test_only_recoverable_error_falls_through(self) -> None:
        request = ProviderRequest(
            "com.example.app", "1.2.3", "123", "arm64", Path("trusted")
        )
        next_provider = StubProvider("direct")
        with self.assertRaisesRegex(IntegrityMetadataError, "certificate mismatch"):
            fallback_download(
                request,
                (
                    StubProvider(
                        "google-play",
                        error=IntegrityMetadataError("certificate mismatch"),
                    ),
                    next_provider,
                ),
            )
        self.assertEqual(next_provider.calls, 0)

    def test_cached_provider_identity_is_read_from_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "trusted"
            output.mkdir()
            artifact = output / "base.apk"
            artifact.write_bytes(b"verified APK")
            (output / "provenance.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "provider": "direct",
                        "package": "com.example.app",
                        "version": {"name": "1.2.3", "code": "123"},
                        "architecture": "arm64",
                        "artifacts": [
                            {
                                "path": artifact.name,
                                "size": artifact.stat().st_size,
                                "sha256": hashlib.sha256(
                                    artifact.read_bytes()
                                ).hexdigest(),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            request = ProviderRequest(
                "com.example.app", "1.2.3", "123", "arm64", output
            )
            direct = StubProvider("direct")
            result = fallback_download(
                request,
                (
                    GooglePlayProvider(
                        None,
                        None,
                        None,
                        None,
                        runner=lambda _: self.fail(
                            "Google Play must not claim direct cache"
                        ),
                    ),
                    direct,
                ),
            )
        self.assertEqual(result.provider, "direct")
        self.assertEqual(direct.calls, 0)

    def test_cached_provenance_must_match_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "trusted"
            output.mkdir()
            artifact = output / "base.apk"
            artifact.write_bytes(b"verified APK")
            (output / "provenance.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "provider": "direct",
                        "package": "wrong.package",
                        "version": {"name": "1.2.3", "code": "123"},
                        "architecture": "arm64",
                        "artifacts": [
                            {
                                "path": artifact.name,
                                "size": artifact.stat().st_size,
                                "sha256": hashlib.sha256(
                                    artifact.read_bytes()
                                ).hexdigest(),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            request = ProviderRequest(
                "com.example.app", "1.2.3", "123", "arm64", output
            )
            with self.assertRaisesRegex(IntegrityMetadataError, "does not match"):
                fallback_download(request, (StubProvider("direct"),))

    def test_google_play_schema_v2_provenance_supports_cache_hit(self) -> None:
        # write_provenance() (apk.py) writes schema_version 2 with a "files"
        # list keyed by "normalized_filename", not schema 1's "artifacts".
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "trusted"
            output.mkdir()
            artifact = output / "base.apk"
            artifact.write_bytes(b"verified APK")
            (output / "provenance.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "provider": "google-play",
                        "package": "com.example.app",
                        "version": {"name": "1.2.3", "code": "123"},
                        "architecture": "arm64",
                        "files": [
                            {
                                "normalized_filename": artifact.name,
                                "size": artifact.stat().st_size,
                                "sha256": hashlib.sha256(
                                    artifact.read_bytes()
                                ).hexdigest(),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            request = ProviderRequest(
                "com.example.app", "1.2.3", "123", "arm64", output
            )
            google_play = StubProvider(
                "google-play", error=AssertionError("must not re-download")
            )
            result = fallback_download(request, (google_play,))
            self.assertEqual(result.provider, "google-play")
            self.assertEqual(google_play.calls, 0)

    def test_artifact_mismatch_and_ambiguity_are_recoverable(self) -> None:
        request = ProviderRequest(
            "com.example.app", "1.2.3", "123", "arm64", Path("trusted")
        )
        next_provider = StubProvider("apkmirror")
        result = fallback_download(
            request,
            (
                StubProvider(
                    "google-play", error=ProviderArtifactMismatch("wrong ABI")
                ),
                next_provider,
            ),
        )
        self.assertEqual(result.provider, "apkmirror")
        self.assertEqual(next_provider.calls, 1)
        result = fallback_download(
            request,
            (
                StubProvider("direct", error=ProviderAmbiguous("ambiguous")),
                StubProvider("apkmirror"),
            ),
        )
        self.assertEqual(result.provider, "apkmirror")


class GooglePlayProviderTest(unittest.TestCase):
    def test_delivery_preflight_rejects_wrong_abi(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "delivery.json"
            path.write_text(
                json.dumps({"splits": ["base", "config.arm64_v8a"]}),
                encoding="utf-8",
            )
            self.assertFalse(_delivery_supports_architecture(path, "armv7"))
            self.assertTrue(_delivery_supports_architecture(path, "arm64"))

    def test_delivery_preflight_accepts_universal_and_multiarch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "delivery.json"
            path.write_text(json.dumps({"splits": ["base"]}), encoding="utf-8")
            self.assertTrue(_delivery_supports_architecture(path, "armv7"))
            path.write_text(
                json.dumps(
                    {"splits": ["base", "config.arm64_v8a", "config.armeabi_v7a"]}
                ),
                encoding="utf-8",
            )
            self.assertTrue(_delivery_supports_architecture(path, "armv7"))

    def test_google_requires_version_code_before_running_goopdl(self) -> None:
        request = ProviderRequest(
            "com.example.app", "1.2.3", None, "arm64", Path("trusted")
        )
        provider = GooglePlayProvider(None, None, None, None)
        with (
            patch(
                "masamune.providers.google_play.download_google_play",
                side_effect=AssertionError("goopdl must not run without version code"),
            ),
            self.assertRaises(VersionUnavailable),
        ):
            provider.download(request)

    def test_missing_google_version_code_falls_back(self) -> None:
        request = ProviderRequest(
            "com.example.app", "1.2.3", None, "arm64", Path("trusted")
        )
        fallback = StubProvider("direct")
        with patch(
            "masamune.providers.google_play.download_google_play",
            side_effect=AssertionError("goopdl must not run without version code"),
        ):
            result = fallback_download(
                request,
                (GooglePlayProvider(None, None, None, None), fallback),
            )
        self.assertEqual(result.provider, "direct")
        self.assertEqual(fallback.calls, 1)

    def test_maps_only_declared_google_fallbacks(self) -> None:
        request = ProviderRequest(
            "com.example.app", "1.2.3", "123", "arm64", Path("trusted")
        )
        provider = GooglePlayProvider(None, None, None, None)
        with (
            patch(
                "masamune.providers.google_play.download_google_play",
                side_effect=GooglePlayAuthUnavailable("auth missing"),
            ),
            self.assertRaises(ProviderUnavailable),
        ):
            provider.download(request)
        with (
            patch(
                "masamune.providers.google_play.download_google_play",
                side_effect=GooglePlayVersionUnavailable("version missing"),
            ),
            self.assertRaises(VersionUnavailable),
        ):
            provider.download(request)
        with (
            patch(
                "masamune.providers.google_play.download_google_play",
                side_effect=ApkMismatch("wrong ABI"),
            ),
            self.assertRaises(ProviderArtifactMismatch),
        ):
            provider.download(request)
        with (
            patch(
                "masamune.providers.google_play.download_google_play",
                side_effect=IntegrityMetadataError("bad signature"),
            ),
            self.assertRaises(IntegrityMetadataError),
        ):
            provider.download(request)


class CatalogFetchTest(unittest.TestCase):
    def test_apkmirror_not_found_is_recoverable(self) -> None:
        def opener(request, timeout):
            raise HTTPError(request.full_url, 404, "not found", Message(), None)

        with self.assertRaises(VersionUnavailable):
            apkmirror_fetch("https://www.apkmirror.com/example", opener=opener)


class UrlProviderTest(unittest.TestCase):
    def test_direct_download_is_verified_and_publishes_sanitized_provenance(
        self,
    ) -> None:
        content = b"apk bytes"

        def opener(request, timeout):
            self.assertEqual(timeout, 60)
            return io.BytesIO(content)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "trusted"
            request = ProviderRequest(
                "com.example.app", "1.2.3", "123", "arm64", output, "a" * 64
            )
            with patch(
                "masamune.providers.urls.verify_apk_set",
                return_value=[apk_metadata()],
            ):
                result = UrlProvider(
                    "direct",
                    ("https://downloads.example.invalid/base.apk?token=secret",),
                    opener,
                ).download(request)
            provenance = json.loads(result.provenance.read_text())
        self.assertEqual(
            provenance["sources"][0]["source"],
            "https://downloads.example.invalid/base.apk",
        )
        self.assertEqual(
            provenance["sources"][0]["sha256"], hashlib.sha256(content).hexdigest()
        )
        self.assertNotIn("secret", json.dumps(provenance))

    def test_url_provider_maps_not_found_to_missing_version(self) -> None:
        def opener(request, timeout):
            raise HTTPError(request.full_url, 404, "not found", Message(), None)

        request = ProviderRequest(
            "com.example.app", "1.2.3", "123", "arm64", Path("trusted")
        )
        with self.assertRaises(VersionUnavailable):
            UrlProvider(
                "direct", ("https://downloads.example.invalid/base.apk",), opener
            ).download(request)

    def test_url_provider_maps_network_failure_to_unavailable(self) -> None:
        def opener(request, timeout):
            raise OSError("offline")

        request = ProviderRequest(
            "com.example.app", "1.2.3", "123", "arm64", Path("trusted")
        )
        with self.assertRaises(ProviderUnavailable):
            UrlProvider(
                "direct", ("https://downloads.example.invalid/base.apk",), opener
            ).download(request)

    def test_url_provider_rejects_non_direct_provider_names(self) -> None:
        request = ProviderRequest(
            "com.example.app", "1.2.3", "123", "arm64", Path("trusted")
        )
        with self.assertRaisesRegex(ValueError, "unsupported URL provider"):
            UrlProvider("apkmirror", ()).download(request)


def apk_metadata() -> ApkMetadata:
    return replace(
        ApkMetadata(
            "base.apk",
            "com.example.app",
            "1.2.3",
            "123",
            None,
            "base",
            "arm64",
            None,
            None,
            None,
            (),
            (),
            (),
            (),
            ("a" * 64,),
        ),
        unsupported_requirements=(),
    )


if __name__ == "__main__":
    unittest.main()
