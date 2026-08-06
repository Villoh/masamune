import tempfile
import unittest
from pathlib import Path

try:
    from masamune.config_editor import (  # pyright: ignore[reportMissingImports]
        add_app,
        remove_app,
        set_app_patch_source,
        set_exclusive_patches,
        update_app,
    )
except ModuleNotFoundError as error:
    if error.name != "tomlkit":
        raise
    add_app = remove_app = set_app_patch_source = set_exclusive_patches = update_app = (
        None
    )

from masamune.config import ConfigError, load_config

_CONFIG = """# keep root comment
[toolchain]
patches-source = "MorpheApp/morphe-patches"

[[apps]]
# keep app comment
package = "com.example.one"
name = "One"
include-patches = ["Old"]
exclude-patches = []

[apps.patch-options.Old]
value = true

[[apps]]
package = "com.example.two"
name = "Two"
"""


@unittest.skipUnless(add_app, "tomlkit extra not installed")
class ConfigEditorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "morphe.toml"
        self.path.write_text(_CONFIG, encoding="utf-8")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_add_app_can_initialize_empty_configuration(self) -> None:
        assert add_app is not None
        self.path.write_text("# empty\n", encoding="utf-8")

        add_app(
            self.path,
            {
                "package": "com.example.first",
                "name": "First",
            },
        )

        self.assertEqual(load_config(self.path).apps[0].package, "com.example.first")

    def test_add_update_remove_preserve_comments_and_validate(self) -> None:
        assert add_app is not None and update_app is not None and remove_app is not None
        add_app(
            self.path,
            {
                "package": "com.example.three",
                "name": "Three",
                "version": "latest",
                "build-mode": "both",
                "arch": "both",
                "patches-source": "owner/patches",
                "patches-version": "latest",
                "enabled": "false",
                "fallback-direct": "https://downloads.example.invalid/three.apk",
                "fallback-apkmirror": "https://www.apkmirror.com/apk/example/three/",
            },
        )
        update_app(
            self.path,
            "com.example.three",
            {
                "package": "com.example.three",
                "name": "Three edited",
                "version": "auto",
                "build-mode": "apk",
                "arch": "arm64-v8a",
                "enabled": "true",
                "patches-version": "v1.0.0",
                "patches-sha256": "a" * 64,
                "fallback-direct": "",
                "fallback-apkmirror": "",
            },
        )
        remove_app(self.path, "com.example.two")
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("# keep root comment", text)
        self.assertIn("# keep app comment", text)
        config = load_config(self.path)
        self.assertEqual(
            [(app.package, app.name) for app in config.apps],
            [
                ("com.example.one", "One"),
                ("com.example.three", "Three edited"),
            ],
        )
        self.assertEqual(config.apps[1].patches_source, "owner/patches")
        self.assertEqual(config.apps[1].patches_version, "v1.0.0")
        self.assertEqual(config.apps[1].patches_sha256, "a" * 64)
        self.assertTrue(config.apps[1].enabled)
        self.assertEqual(config.apps[1].fallbacks.direct, ())
        self.assertEqual(config.apps[1].fallbacks.apkmirror, ())

    def test_set_app_patch_source_preserves_other_settings(self) -> None:
        assert set_app_patch_source is not None
        set_app_patch_source(
            self.path,
            "com.example.one",
            "owner/community-patches",
            "v2.0.0",
        )
        app = load_config(self.path).apps[0]
        self.assertEqual(app.patches_source, "owner/community-patches")
        self.assertEqual(app.patches_version, "v2.0.0")
        self.assertEqual(app.include_patches, ("Old",))
        self.assertEqual(app.name, "One")
        self.assertIn("# keep root comment", self.path.read_text(encoding="utf-8"))

    def test_exclusive_patches_replace_include_exclude_and_prune_options(self) -> None:
        assert set_exclusive_patches is not None
        set_exclusive_patches(self.path, "com.example.one", ["Theme", "Theme"])
        config = load_config(self.path)
        app = config.apps[0]
        self.assertEqual(app.exclusive_patches, ("Theme",))
        self.assertEqual(app.include_patches, ())
        self.assertEqual(app.exclude_patches, ())
        self.assertEqual(dict(app.patch_options), {})
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn("include-patches", text)
        self.assertNotIn("patch-options", text)

    def test_exclusive_patches_write_option_overrides(self) -> None:
        assert set_exclusive_patches is not None
        set_exclusive_patches(
            self.path,
            "com.example.one",
            ["Theme"],
            {"Theme": {"dark": "#000000", "enabled": False}},
        )
        app = load_config(self.path).apps[0]
        self.assertEqual(
            dict(app.patch_options["Theme"]),
            {"dark": "#000000", "enabled": False},
        )

    def test_invalid_edit_does_not_replace_source(self) -> None:
        assert update_app is not None
        before = self.path.read_bytes()
        with self.assertRaises(ConfigError):
            update_app(
                self.path,
                "com.example.one",
                {"package": "bad", "name": "One"},
            )
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(list(self.path.parent.glob(".morphe.toml-*.tmp")))

    def test_cannot_remove_last_app(self) -> None:
        assert remove_app is not None
        remove_app(self.path, "com.example.two")
        before = self.path.read_bytes()
        with self.assertRaises(ConfigError):
            remove_app(self.path, "com.example.one")
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
