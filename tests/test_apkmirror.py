import io
import unittest
from pathlib import Path
from unittest.mock import patch

from masamune.errors import IntegrityMetadataError
from masamune.providers import ProviderRequest, ProviderResult
from masamune.providers.apkmirror import (
    ApkMirrorAsset,
    ApkMirrorProvider,
    resolve_apkmirror_asset,
    resolve_apkmirror_catalog,
    resolve_apkmirror_version_code,
    resolve_apkmirror_version_code_for_package,
)
from masamune.providers.errors import ProviderAmbiguous

RELEASE_PAGE = """
<html><body>
<div class="table-row headerFont">
  <div class="table-cell"><a href="/apk/google-inc/youtube/y-21-04-223-release/a1-android-apk-download/">
  YouTube 21.04.223</a></div>
  <div class="table-cell">APK</div>
  <div class="table-cell">1561052632</div>
  <div class="table-cell">armeabi-v7a</div>
  <div class="table-cell">nodpi</div>
</div>
<div class="table-row headerFont">
  <div class="table-cell"><a href="/apk/google-inc/youtube/y-21-04-223-release/a2-android-apk-download/">
  YouTube 21.04.223</a></div>
  <div class="table-cell">APK</div>
  <div class="table-cell">1561052632</div>
  <div class="table-cell">arm64-v8a</div>
  <div class="table-cell">nodpi</div>
</div>
</body></html>
"""

VARIANT_PAGE = """
<html><body>
<a class="btn btn-flat" href="/apk/download-page/">Download APK</a>
</body></html>
"""

DOWNLOAD_PAGE = """
<html><body>
<span><a id="download-link" rel="nofollow"
  href="/wp-content/themes/APKMirror/download.php?key=test">here</a></span>
</body></html>
"""


def fake_opener(pages):
    calls = []

    def opener(request, timeout):
        calls.append(request.full_url)
        assert timeout == 60
        assert request.get_header("User-agent") == "masamune"
        for fragment, body in pages.items():
            if request.full_url.rstrip("/").endswith(fragment.rstrip("/")):
                return io.BytesIO(body.encode("utf-8"))
        raise AssertionError(f"unexpected URL: {request.full_url}")

    return opener, calls


class ApkMirrorResolverTest(unittest.TestCase):
    def test_provider_discovers_catalog_when_no_url_is_configured(self) -> None:
        request = ProviderRequest(
            "com.google.android.youtube", "21.04.223", "", "arm64", Path("output")
        )
        result = ProviderResult("apkmirror", request.output, Path("provenance.json"))

        def opener(*_args, **_kwargs):
            return None

        with (
            patch(
                "masamune.providers.apkmirror.resolve_apkmirror_catalog",
                return_value="https://www.apkmirror.com/apk/google-inc/youtube/",
            ) as catalog,
            patch(
                "masamune.providers.apkmirror.resolve_apkmirror_asset",
                return_value=ApkMirrorAsset(
                    "https://example.test/youtube.apk", False, "123"
                ),
            ) as asset,
            patch("masamune.providers.apkmirror._download_assets", return_value=result),
        ):
            provider = ApkMirrorProvider((), opener=opener)
            self.assertIs(provider.download(request), result)

        catalog.assert_called_once_with(request.package, opener=provider.opener)
        asset.assert_called_once_with(
            "https://www.apkmirror.com/apk/google-inc/youtube/",
            version_name=request.version_name,
            arch=request.arch,
            opener=provider.opener,
        )

    def test_resolves_catalog_page_to_direct_asset_for_requested_arch(self) -> None:
        opener, calls = fake_opener(
            {
                "-21-04-223-release/": RELEASE_PAGE,
                "/a2-android-apk-download/": VARIANT_PAGE,
                "download-page": DOWNLOAD_PAGE,
            }
        )
        asset = resolve_apkmirror_asset(
            "https://www.apkmirror.com/apk/google-inc/youtube/",
            version_name="21.04.223",
            arch="arm64",
            opener=opener,
        )
        self.assertEqual(
            asset.url,
            "https://www.apkmirror.com/wp-content/themes/APKMirror/download.php?key=test",
        )
        self.assertFalse(asset.bundle)
        self.assertEqual(asset.version_code, "1561052632")
        self.assertIn("youtube-21-04-223-release", calls[0])
        self.assertTrue(any("/a2-android-apk-download/" in url for url in calls))
        self.assertFalse(any("/a1-android-apk-download/" in url for url in calls))

    def test_ignores_bundle_variants_when_a_plain_apk_exists(self) -> None:
        page = RELEASE_PAGE.replace(
            '<div class="table-row headerFont">',
            """<div class="table-row headerFont">
  <div class="table-cell"><a href="/apk/google-inc/youtube/y-21-04-223-release/bundle-android-apk-download/">YouTube 21.04.223</a></div>
  <div class="table-cell">BUNDLE</div><div class="table-cell">arm64-v8a</div>
</div><div class="table-row headerFont">""",
            1,
        )
        opener, calls = fake_opener(
            {
                "-21-04-223-release/": page,
                "/a2-android-apk-download/": VARIANT_PAGE,
                "download-page": DOWNLOAD_PAGE,
            }
        )
        resolve_apkmirror_asset(
            "https://www.apkmirror.com/apk/google-inc/youtube/",
            version_name="21.04.223",
            arch="arm64",
            opener=opener,
        )
        self.assertTrue(any("/a2-android-apk-download/" in url for url in calls))
        self.assertFalse(any("bundle-android-apk-download/" in url for url in calls))

    def test_resolves_universal_fallback_variant(self) -> None:
        page = (
            RELEASE_PAGE
            + """
<div class="table-row headerFont">
  <div class="table-cell"><a href="/apk/google-inc/youtube/y-release/u1-android-apk-download/">
  YouTube 21.04.223</a></div>
  <div class="table-cell">APK</div>
  <div class="table-cell">1561052632</div>
  <div class="table-cell">universal</div>
</div>
"""
        )
        opener, calls = fake_opener(
            {
                "-21-04-223-release/": page,
                "/u1-android-apk-download/": VARIANT_PAGE,
                "download-page": DOWNLOAD_PAGE,
            }
        )
        asset = resolve_apkmirror_asset(
            "https://www.apkmirror.com/apk/google-inc/youtube/",
            version_name="21.04.223",
            arch="universal",
            opener=opener,
        )
        self.assertFalse(asset.bundle)
        self.assertTrue(any("/u1-android-apk-download/" in url for url in calls))

    def test_selects_armv7_variant_independently(self) -> None:
        opener, calls = fake_opener(
            {
                "-21-04-223-release/": RELEASE_PAGE,
                "/a1-android-apk-download/": VARIANT_PAGE,
                "download-page": DOWNLOAD_PAGE,
            }
        )
        resolve_apkmirror_asset(
            "https://www.apkmirror.com/apk/google-inc/youtube/",
            version_name="21.04.223",
            arch="armv7",
            opener=opener,
        )
        self.assertTrue(any("/a1-android-apk-download/" in url for url in calls))
        self.assertFalse(any("/a2-android-apk-download/" in url for url in calls))

    def test_rejects_asset_hosted_outside_apkmirror(self) -> None:
        hostile = DOWNLOAD_PAGE.replace(
            "/wp-content/themes/APKMirror/download.php?key=test",
            "https://evil.invalid/download.php",
        )
        opener, _ = fake_opener(
            {
                "-21-04-223-release/": RELEASE_PAGE,
                "/a2-android-apk-download/": VARIANT_PAGE,
                "download-page": hostile,
            }
        )
        with self.assertRaises(IntegrityMetadataError):
            resolve_apkmirror_asset(
                "https://www.apkmirror.com/apk/google-inc/youtube/",
                version_name="21.04.223",
                arch="arm64",
                opener=opener,
            )

    def test_rejects_non_apk_asset(self) -> None:
        bundle = DOWNLOAD_PAGE.replace(
            "/wp-content/themes/APKMirror/download.php",
            "/wp-content/themes/APKMirror/bundle.apkm",
        )
        opener, _ = fake_opener(
            {
                "-21-04-223-release/": RELEASE_PAGE,
                "/a2-android-apk-download/": VARIANT_PAGE,
                "download-page": bundle,
            }
        )
        with self.assertRaises(IntegrityMetadataError):
            resolve_apkmirror_asset(
                "https://www.apkmirror.com/apk/google-inc/youtube/",
                version_name="21.04.223",
                arch="arm64",
                opener=opener,
            )

    def test_discovers_catalog_from_package(self) -> None:
        search = """
        <a href="/apk/redditinc/reddit/#disqus_thread">Comments</a>
        <a href="/apk/redditinc/reddit/">Reddit</a>
        <a href="/apk/redditinc/reddit/reddit-2026-30-0-release/">Release</a>
        """
        catalog = (
            '<a href="https://play.google.com/store/apps/details?id='
            'com.reddit.frontpage">View on Play Store</a>'
        )
        opener, calls = fake_opener(
            {
                "s=com.reddit.frontpage": search,
                "/apk/redditinc/reddit/": catalog,
            }
        )

        self.assertEqual(
            resolve_apkmirror_catalog("com.reddit.frontpage", opener=opener),
            "https://www.apkmirror.com/apk/redditinc/reddit/",
        )
        self.assertEqual(len(calls), 2)

    def test_discovery_ignores_related_package_catalog(self) -> None:
        search = """
        <a href="/apk/yuliskov/youtube-for-android-tv/">YouTube TV</a>
        <a href="/apk/google-inc/youtube/">YouTube</a>
        """
        opener, calls = fake_opener(
            {
                "s=com.google.android.youtube": search,
                "/apk/yuliskov/youtube-for-android-tv/": (
                    '<a href="https://play.google.com/store/apps/details?id='
                    'com.google.android.youtube.tv">View on Play Store</a>'
                ),
                "/apk/google-inc/youtube/": (
                    '<a href="https://play.google.com/store/apps/details?id='
                    'com.google.android.youtube">View on Play Store</a>'
                ),
            }
        )

        self.assertEqual(
            resolve_apkmirror_catalog("com.google.android.youtube", opener=opener),
            "https://www.apkmirror.com/apk/google-inc/youtube/",
        )
        self.assertEqual(len(calls), 3)

    def test_version_code_skips_catalog_without_requested_release(self) -> None:
        search = """
        <a href="/apk/ogmods/ogyoutube-3/">OGYouTube</a>
        <a href="/apk/google-inc/youtube/">YouTube</a>
        """
        play_store = (
            '<a href="https://play.google.com/store/apps/details?id='
            'com.google.android.youtube">View on Play Store</a>'
        )
        opener, calls = fake_opener(
            {
                "s=com.google.android.youtube": search,
                "/apk/ogmods/ogyoutube-3/": play_store,
                "/apk/google-inc/youtube/": play_store,
                "/apk/ogmods/ogyoutube-3/ogyoutube-3-21-04-223-release/": "<html />",
                "/apk/google-inc/youtube/youtube-21-04-223-release/": RELEASE_PAGE,
            }
        )

        self.assertEqual(
            resolve_apkmirror_version_code_for_package(
                "com.google.android.youtube",
                version_name="21.04.223",
                arch="arm64",
                opener=opener,
            ),
            "1561052632",
        )
        self.assertEqual(len(calls), 5)

    def test_resolves_version_code_from_package_without_download_pages(self) -> None:
        search = '<a href="/apk/redditinc/reddit/">Reddit</a>'
        catalog = (
            '<a href="https://play.google.com/store/apps/details?id='
            'com.reddit.frontpage">View on Play Store</a>'
        )
        opener, calls = fake_opener(
            {
                "s=com.reddit.frontpage": search,
                "/apk/redditinc/reddit/": catalog,
                "-2026-14-0-release/": RELEASE_PAGE.replace(
                    "com.google.android.youtube", "com.reddit.frontpage"
                ),
            }
        )

        version_code = resolve_apkmirror_version_code_for_package(
            "com.reddit.frontpage",
            version_name="2026.14.0",
            arch="arm64",
            opener=opener,
        )

        self.assertEqual(version_code, "1561052632")
        self.assertEqual(len(calls), 3)

    def test_resolves_version_code_from_release_page_only(self) -> None:
        opener, calls = fake_opener({"-21-04-223-release/": RELEASE_PAGE})

        version_code = resolve_apkmirror_version_code(
            "https://www.apkmirror.com/apk/google-inc/youtube/",
            version_name="21.04.223",
            arch="arm64",
            opener=opener,
        )

        self.assertEqual(version_code, "1561052632")
        self.assertEqual(len(calls), 1)
        self.assertIn("youtube-21-04-223-release", calls[0])

    def test_picks_latest_universal_upload_when_several_share_a_version_name(
        self,
    ) -> None:
        page = """
<html><body>
<div class="table-row headerFont">
  <div class="table-cell"><a href="/apk/reddit-inc/reddit/reddit-2026-14-0-release/r1-android-apk-download/">
  Reddit 2026.14.0</a></div>
  <div class="table-cell">BUNDLE</div>
  <div class="table-cell">2614001</div>
  <div class="table-cell">universal</div>
</div>
<div class="table-row headerFont">
  <div class="table-cell"><a href="/apk/reddit-inc/reddit/reddit-2026-14-0-release/r2-android-apk-download/">
  Reddit 2026.14.0</a></div>
  <div class="table-cell">BUNDLE</div>
  <div class="table-cell">2614141</div>
  <div class="table-cell">universal</div>
</div>
<div class="table-row headerFont">
  <div class="table-cell"><a href="/apk/reddit-inc/reddit/reddit-2026-14-0-release/r3-android-apk-download/">
  Reddit 2026.14.0</a></div>
  <div class="table-cell">BUNDLE</div>
  <div class="table-cell">2614121</div>
  <div class="table-cell">universal</div>
</div>
</body></html>
"""
        opener, calls = fake_opener(
            {
                "-14-0-release/": page,
                "/r2-android-apk-download/": VARIANT_PAGE,
                "download-page": DOWNLOAD_PAGE,
            }
        )
        asset = resolve_apkmirror_asset(
            "https://www.apkmirror.com/apk/reddit-inc/reddit/",
            version_name="2026.14.0",
            arch="arm64",
            opener=opener,
        )
        self.assertTrue(asset.bundle)
        self.assertEqual(asset.version_code, "2614141")
        self.assertTrue(any("/r2-android-apk-download/" in url for url in calls))
        self.assertFalse(any("/r1-android-apk-download/" in url for url in calls))
        self.assertFalse(any("/r3-android-apk-download/" in url for url in calls))

    def test_stays_ambiguous_when_universal_uploads_share_a_version_code(
        self,
    ) -> None:
        # Same version code, different pages (e.g. distinct minSdk targeting):
        # genuinely different content, must not be auto-resolved.
        page = """
<html><body>
<div class="table-row headerFont">
  <div class="table-cell"><a href="/apk/google-inc/youtube/y-release/u1-android-apk-download/">
  YouTube 21.04.223</a></div>
  <div class="table-cell">BUNDLE</div>
  <div class="table-cell">1561052632</div>
  <div class="table-cell">universal</div>
</div>
<div class="table-row headerFont">
  <div class="table-cell"><a href="/apk/google-inc/youtube/y-release/u2-android-apk-download/">
  YouTube 21.04.223</a></div>
  <div class="table-cell">BUNDLE</div>
  <div class="table-cell">1561052632</div>
  <div class="table-cell">universal</div>
</div>
</body></html>
"""
        opener, _ = fake_opener({"-21-04-223-release/": page})
        with self.assertRaisesRegex(ProviderAmbiguous, "ambiguous"):
            resolve_apkmirror_asset(
                "https://www.apkmirror.com/apk/google-inc/youtube/",
                version_name="21.04.223",
                arch="arm64",
                opener=opener,
            )

    def test_fails_when_release_has_no_matching_architecture(self) -> None:
        only_x86 = RELEASE_PAGE.replace("arm64-v8a", "x86_64").replace(
            "armeabi-v7a", "x86"
        )
        opener, _ = fake_opener({"-21-04-223-release/": only_x86})
        with self.assertRaises(IntegrityMetadataError):
            resolve_apkmirror_asset(
                "https://www.apkmirror.com/apk/google-inc/youtube/",
                version_name="21.04.223",
                arch="arm64",
                opener=opener,
            )


if __name__ == "__main__":
    unittest.main()
