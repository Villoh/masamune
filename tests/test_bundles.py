import json
import unittest

from masamune.bundles import (  # pyright: ignore[reportMissingImports]
    BundleError,
    load_bundle_catalog,
    load_community_bundles,
)


class _Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self.payload


class BundleCatalogTest(unittest.TestCase):
    def test_loads_apps_from_release_patch_list(self) -> None:
        release = {
            "tag_name": "v2.0.0",
            "assets": [
                {
                    "name": "patches-list.json",
                    "browser_download_url": "https://github.com/owner/repo/releases/download/v2.0.0/patches-list.json",
                }
            ],
        }
        patches = {
            "patches": [
                {
                    "name": "One",
                    "compatiblePackages": [
                        {
                            "packageName": "com.example.app",
                            "name": "Example",
                            "targets": [{"version": "2.0"}, {"version": "1.0"}],
                        }
                    ],
                },
                {
                    "name": "Two",
                    "compatiblePackages": [
                        {
                            "packageName": "com.example.app",
                            "name": "Example",
                            "targets": [{"version": "2.0"}],
                        }
                    ],
                },
            ]
        }

        def opener(request, timeout):
            self.assertEqual(timeout, 30)
            return _Response(
                release if "api.github.com" in request.full_url else patches
            )

        catalog = load_bundle_catalog("owner/repo", opener=opener)
        self.assertEqual(catalog.source, "owner/repo")
        self.assertEqual(catalog.version, "v2.0.0")
        self.assertEqual(catalog.apps[0].package, "com.example.app")
        self.assertEqual(catalog.apps[0].versions, ("2.0", "1.0"))
        self.assertEqual(catalog.apps[0].patch_count, 2)

    def test_falls_back_to_default_branch_when_github_api_is_unavailable(self) -> None:
        patches = {
            "patches": [
                {
                    "name": "Fallback",
                    "compatiblePackages": [
                        {
                            "packageName": "com.example.app",
                            "name": "Example",
                            "targets": [{"version": "1.0"}],
                        }
                    ],
                }
            ]
        }

        def opener(request, timeout):
            self.assertEqual(timeout, 30)
            if "api.github.com" in request.full_url:
                raise OSError("rate limited")
            if "/main/" in request.full_url:
                return _Response(patches)
            raise OSError("missing branch")

        catalog = load_bundle_catalog("owner/repo", opener=opener)
        self.assertEqual(catalog.version, "latest")
        self.assertEqual(catalog.apps[0].package, "com.example.app")

    def test_rejects_invalid_source(self) -> None:
        with self.assertRaises(BundleError):
            load_bundle_catalog("not-a-repository")

    def test_uses_tagged_raw_patch_list_when_release_has_no_asset(self) -> None:
        def opener(request, timeout):
            if "api.github.com" in request.full_url:
                return _Response({"tag_name": "v1", "assets": []})
            return _Response({"patches": []})

        catalog = load_bundle_catalog("owner/repo", opener=opener)
        self.assertEqual(catalog.version, "v1")
        self.assertEqual(catalog.apps, ())

    def test_loads_community_bundle_index(self) -> None:
        payload = {
            "bundles": [
                {
                    "source": "github",
                    "repo": "owner/repo",
                    "name": "Community",
                    "author": "Author",
                    "repoDescription": "Description",
                    "patchCount": 4,
                    "targetApps": ["com.example.app"],
                    "patches": [
                        {
                            "name": "Patch",
                            "compatiblePackages": [
                                {
                                    "packageName": "com.example.app",
                                    "name": "Example",
                                    "targets": [{"version": "1.0"}],
                                }
                            ],
                        }
                    ],
                }
            ],
            "store": {},
            "compatibilities": [],
        }

        def opener(_request, timeout):
            self.assertEqual(timeout, 30)
            return _Response(payload)

        bundles = load_community_bundles(opener=opener)
        self.assertEqual(len(bundles), 1)
        self.assertEqual(bundles[0].repo, "owner/repo")
        self.assertEqual(bundles[0].apps[0].name, "Example")


if __name__ == "__main__":
    unittest.main()
