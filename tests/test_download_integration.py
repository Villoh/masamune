import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

from masamune.build import BuildError
from masamune.config import load_config
from masamune.config_editor import update_app
from masamune.orchestrator import (
    Reporter,
    _apkmirror_version_code_hint,
    _downloaded_source_directory,
    _source_directory,
)
from masamune.paths import (
    migrate_legacy_downloads,  # pyright: ignore[reportMissingImports]
)
from masamune.tui.workers import run_download_task


class DownloadIntegrationTests(unittest.TestCase):
    def test_build_source_falls_back_to_verified_download(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "com-example-app" / "1.2.3" / "arm64-v8a"
            source.mkdir(parents=True)
            (source / "provenance.json").write_text(
                json.dumps(
                    {
                        "package": "com.example.app",
                        "version": {"name": "1.2.3", "code": "123"},
                    }
                ),
                encoding="utf-8",
            )
            job = SimpleNamespace(
                arch="arm64-v8a",
                app=SimpleNamespace(
                    slug="com-example-app",
                    package="com.example.app",
                    version="auto",
                    source_dir=None,
                    expected_signer=None,
                ),
            )
            with patch("masamune.orchestrator.verify_apk_set", return_value=[]):
                resolved = _downloaded_source_directory(job, root)
        self.assertEqual(resolved, source)

    def test_missing_download_error_includes_requested_architecture(self) -> None:
        with TemporaryDirectory() as directory:
            job = SimpleNamespace(
                arch="arm-v7a",
                app=SimpleNamespace(
                    slug="com-example-app",
                    package="com.example.app",
                    version="auto",
                    source_dir=None,
                ),
            )
            with self.assertRaisesRegex(BuildError, r"com\.example\.app \(arm-v7a\)"):
                _source_directory(job, download_root=Path(directory))

    def test_legacy_downloads_move_under_dedicated_folder(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "masamune"
            legacy = root / "com-example-app" / "1.2.3" / "arm64-v8a"
            legacy.mkdir(parents=True)
            (legacy / "provenance.json").write_text(
                '{"package": "com.example.app"}', encoding="utf-8"
            )
            destination = root / "downloads"
            self.assertEqual(migrate_legacy_downloads(destination), 1)
            self.assertFalse((root / "com-example-app").exists())
            self.assertTrue(
                (destination / "com-example-app" / "1.2.3" / "arm64-v8a").is_dir()
            )

    def test_automatic_version_code_hint_discovers_apkmirror_catalog(self) -> None:
        with patch(
            "masamune.orchestrator.resolve_apkmirror_version_code_for_package",
            return_value="91551240",
        ) as resolver:
            code = _apkmirror_version_code_hint(
                (),
                version_name="9.15.51",
                arch="arm64",
                reporter=Reporter(),
                package="com.google.android.apps.youtube.music",
            )
        self.assertEqual(code, "91551240")
        resolver.assert_called_once_with(
            "com.google.android.apps.youtube.music",
            version_name="9.15.51",
            arch="arm64",
        )

    def test_download_worker_clears_provider_hooks_on_failure(self) -> None:
        with (
            patch("masamune.tui.workers.set_terminal_owner") as terminal,
            patch("masamune.tui.workers.set_build_cancel_event") as cancel,
        ):

            def runner(*args, **kwargs):
                raise RuntimeError("failed")

            with self.assertRaisesRegex(RuntimeError, "failed"):
                run_download_task(
                    object(),
                    runner=runner,
                    reporter=object(),
                    cancel_event=Event(),
                    output_sink=lambda _: None,
                )

        self.assertEqual(terminal.call_args_list[-1].args, (None,))
        self.assertEqual(cancel.call_args_list[-1].args, (None,))

    def test_editing_app_preserves_unexposed_provider_values(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "morphe.toml"
            path.write_text(
                """[[apps]]
package = "com.example.app"
name = "Example"
include-experimental-versions = true
[apps.google-play]
profile = "default"
[apps.fallbacks]
direct = ["https://downloads.example/app.apk"]
""",
                encoding="utf-8",
            )
            update_app(
                path,
                "com.example.app",
                {"package": "com.example.app", "name": "Renamed"},
            )
            app = load_config(path).apps[0]

        self.assertEqual(app.name, "Renamed")
        self.assertTrue(app.include_experimental_versions)
        self.assertEqual(app.google_play.profile, "default")
        self.assertEqual(app.fallbacks.direct, ("https://downloads.example/app.apk",))


if __name__ == "__main__":
    unittest.main()
