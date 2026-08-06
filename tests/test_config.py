import unittest

from masamune.config import parse_config


class ConfigTests(unittest.TestCase):
    def test_local_source_dir_is_required_and_preserved(self) -> None:
        config = parse_config(
            {
                "apps": [
                    {
                        "package": "com.example.app",
                        "name": "Example",
                        "source-dir": "inputs/{arch}",
                    }
                ]
            }
        )
        self.assertEqual(config.apps[0].source_dir, "inputs/{arch}")

    def test_local_source_dir_can_be_selected_later(self) -> None:
        config = parse_config(
            {"apps": [{"package": "com.example.app", "name": "Example"}]}
        )
        self.assertIsNone(config.apps[0].source_dir)

    def test_only_supported_fallbacks_are_configured(self) -> None:
        config = parse_config(
            {
                "apps": [
                    {
                        "package": "com.example.app",
                        "name": "Example",
                        "fallbacks": {
                            "direct": ["https://downloads.example/app.apk"],
                            "apkmirror": ["https://www.apkmirror.com/apk/example/app/"],
                        },
                    }
                ]
            }
        )
        self.assertEqual(
            config.apps[0].fallbacks.direct, ("https://downloads.example/app.apk",)
        )
        self.assertEqual(
            config.apps[0].fallbacks.apkmirror,
            ("https://www.apkmirror.com/apk/example/app/",),
        )

    def test_archive_and_uptodown_fallbacks_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, r"unknown apps\[0\]\.fallbacks key"):
            parse_config(
                {
                    "apps": [
                        {
                            "package": "com.example.app",
                            "name": "Example",
                            "fallbacks": {
                                "archive": ["https://archive.org/download/app/base.apk"]
                            },
                        }
                    ]
                }
            )

    def test_app_can_include_universal_patches(self) -> None:
        config = parse_config(
            {
                "apps": [
                    {
                        "package": "com.example.app",
                        "name": "Example",
                        "include-universal-patches": True,
                    }
                ]
            }
        )
        self.assertTrue(config.apps[0].include_universal_patches)


if __name__ == "__main__":
    unittest.main()
