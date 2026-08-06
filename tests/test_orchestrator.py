import argparse
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, call, patch

from masamune.apk import ApkMetadata
from masamune.build import AUTO_KEYSTORE_PASSWORD, BuildError
from masamune.errors import GooglePlayAuthUnavailable, GooglePlayVersionUnavailable
from masamune.orchestrator import (  # pyright: ignore[reportMissingImports]
    BuildResult,
    Reporter,
    _apkmirror_version_code_hint,
    _build_job,
    _summary,
    _write_summary,
    run_build,
    run_clean,
    run_patch_catalog,
    run_verify,
)
from masamune.providers import (
    ProviderAmbiguous,
    ProviderRequest,
    ProviderResult,
    ProviderUnavailable,
    VersionUnavailable,
)
from masamune.providers.google_play import GooglePlayProvider
from masamune.toolchain import PreparedToolchain


def prepared_toolchain(provenance: Path = Path("tools.json")) -> PreparedToolchain:
    return PreparedToolchain(
        provenance,
        Path("java"),
        Path("cli.jar"),
        "v1",
        Path("patches.mpp"),
        "v1",
        Path("apkeditor.jar"),
        "v1",
        Path("signer.jar"),
        "v1",
    )


class ReporterTest(unittest.TestCase):
    def test_sink_receives_redacted_events_in_order_without_stderr(self) -> None:
        events: list[dict[str, object]] = []
        reporter = Reporter(sink=events.append)
        with redirect_stderr(io.StringIO()) as stderr:
            reporter.event(
                "resolve", "token=secret", package="com.example.app", token="secret"
            )
            reporter.event(
                "complete", "build complete", output="https://host/path?token=x"
            )
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(
            events,
            [
                {
                    "event": "resolve",
                    "message": "token=<redacted>",
                    "package": "com.example.app",
                    "token": "<redacted>",
                },
                {
                    "event": "complete",
                    "message": "build complete",
                    "output": "<redacted-url>",
                },
            ],
        )

    def test_sink_recursively_redacts_sensitive_event_data(self) -> None:
        events: list[dict[str, object]] = []
        Reporter(sink=events.append).event(
            "token=event secret",
            "password=multi word secret",
            name="authorization=secret name",
            details={
                "token": "nested secret",
                "items": [
                    {"cookie": "nested cookie"},
                    "https://user:password@example.invalid/path?token=query#fragment",
                ],
            },
        )
        self.assertEqual(
            events,
            [
                {
                    "event": "token=<redacted>",
                    "message": "password=<redacted>",
                    "name": "authorization=<redacted>",
                    "details": {
                        "token": "<redacted>",
                        "items": [
                            {"cookie": "<redacted>"},
                            "<redacted-url>",
                        ],
                    },
                }
            ],
        )

    def test_no_sink_preserves_stderr_output(self) -> None:
        with redirect_stderr(io.StringIO()) as stderr:
            Reporter().event("tools", "preparing toolchains", cache="cache")
        self.assertEqual(
            stderr.getvalue(), "[tools] preparing toolchains cache=cache\n"
        )


class BuildOrchestrationTest(unittest.TestCase):

    def test_build_job_preserves_verified_phase_order(self) -> None:
        staging = Path("staging")
        work = staging / "youtube" / "arm64-v8a"
        provider = ProviderResult("google-play", Path("trusted"), Path("source.json"))
        metadata = [Mock(signers_sha256=("a" * 64,))]
        job = SimpleNamespace(
            arch="arm64-v8a",
            app=SimpleNamespace(
                package="com.google.android.youtube",
                slug="youtube",
                patched_package="app.morphe.android.youtube",
                include_patches=("Hide ads",),
                exclude_patches=(),
                exclusive_patches=(),
                patch_options={},
                build_mode="apk",
                name="YouTube",
            ),
        )
        phases: list[str] = []

        def obtain(*args: object, **kwargs: object) -> tuple[object, object, str]:
            phases.append("obtain")
            return provider, metadata, "1541200000"

        def merge(*args: object, **kwargs: object) -> tuple[Path, Path]:
            self.assertEqual(phases, ["obtain"])
            phases.append("merge")
            return work / "stock-signed.apk", work / "merge-provenance.json"

        def build(context: Any, mode: str) -> list[Path]:
            self.assertEqual(phases, ["obtain", "merge"])
            self.assertEqual(mode, "apk")
            self.assertEqual(context.exclusive, ("Hide ads",))
            self.assertEqual(context.include, ())
            self.assertEqual(context.exclude, ())
            self.assertNotIn("secret", repr(context))
            phases.append("build")
            return [work / "youtube.apk"]

        with (
            patch(
                "masamune.orchestrator._obtain_verified_source",
                side_effect=obtain,
            ),
            patch(
                "masamune.orchestrator._merge_and_sign_stock", side_effect=merge
            ),
            patch("masamune.orchestrator._build_artifacts", side_effect=build),
            patch(
                "masamune.orchestrator._patch_release",
                return_value=("repo", "v1"),
            ),
        ):
            result = _build_job(
                job,
                "21.04.223",
                selected_patches=("Hide ads",),
                staging=staging,
                cache=Path("cache"),
                toolchain=prepared_toolchain(),
                keystore=Path("builder.p12"),
                alias="morphe",
                password="secret",
                reporter=Reporter(),
            )
        self.assertEqual(phases, ["obtain", "merge", "build"])
        self.assertEqual(
            result.artifacts, (str(work.relative_to(staging) / "youtube.apk"),)
        )
        self.assertEqual(result.provider, "google-play")

    def test_sink_failure_does_not_block_successful_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "morphe.toml"
            config.write_text(
                '[[apps]]\npackage="com.google.android.youtube"\nname="YouTube"\n'
                'version="21.04.223"\narch="arm64-v8a"\n'
                'patches-source="MorpheApp/morphe-patches"\n'
                'patches-version="v1"\n\n'
                '[[apps]]\npackage="com.google.android.apps.youtube.music"\n'
                'name="YouTube Music"\nversion="21.04.223"\narch="arm64-v8a"\n'
                'patches-source="j-hc/revanced-patches"\npatches-version="v2"\n',
                encoding="utf-8",
            )
            keystore = root / "builder.p12"
            keystore.write_bytes(b"key")
            output = root / "output"
            args = argparse.Namespace(
                config=config,
                cache=root / "cache",
                output=output,
                keystore=keystore,
                keystore_alias="morphe",
                json=False,
            )
            compatibility = Mock(
                selected_version="21.04.223", selected_patches=("SponsorBlock",)
            )
            result = BuildResult(
                "com.google.android.youtube",
                "21.04.223",
                "1561052632",
                "arm64-v8a",
                ("youtube/arm64-v8a/youtube.apk",),
                ("a" * 64,),
            )
            events: list[dict[str, object]] = []

            def sink(event: dict[str, object]) -> None:
                events.append(event)
                raise RuntimeError("TUI closed")

            with (
                patch.dict("os.environ", {"MORPHE_KEYSTORE_PASSWORD": "secret"}),
                patch("masamune.orchestrator._local_version_name", return_value="21.04.223"),
                patch(
                    "masamune.orchestrator.prepare_toolchain",
                    return_value=root / "tools.json",
                ) as prepare,
                patch(
                    "masamune.orchestrator.apksigner_jar",
                    return_value=root / "apksigner.jar",
                ),
                patch(
                    "masamune.orchestrator._toolchain",
                    return_value=prepared_toolchain(),
                ),
                patch(
                    "masamune.orchestrator.run_morphe_compatibility",
                    return_value=compatibility,
                ),
                patch("masamune.orchestrator._build_job", return_value=result),
            ):
                summary = run_build(args, reporter=Reporter(sink=sink))
            self.assertEqual(summary["status"], "complete")
            self.assertEqual(events[-1]["event"], "complete")
            self.assertEqual(prepare.call_count, 2)
            self.assertEqual(
                prepare.call_args_list,
                [
                    call(
                        root / "cache",
                        {"morphe-cli": "latest", "morphe-patches": "v1"},
                        {
                            "morphe-cli": "MorpheApp/morphe-desktop",
                            "morphe-patches": "MorpheApp/morphe-patches",
                        },
                    ),
                    call(
                        root / "cache",
                        {"morphe-cli": "latest", "morphe-patches": "v2"},
                        {
                            "morphe-cli": "MorpheApp/morphe-desktop",
                            "morphe-patches": "j-hc/revanced-patches",
                        },
                    ),
                ],
            )
            self.assertEqual(
                json.loads((output / "build-summary.json").read_text())["jobs"][0][
                    "certificate_sha256"
                ],
                ["a" * 64],
            )
            self.assertTrue((output / "CHANGELOG.md").is_file())
            log = (output / "build.log").read_text(encoding="utf-8")
            self.assertIn("[complete] build completed", log)
            self.assertNotIn("secret", log)

    def test_auto_generates_per_user_keystore_when_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "morphe.toml"
            config.write_text(
                '[[apps]]\npackage="com.google.android.youtube"\nname="YouTube"\n'
                'version="21.04.223"\narch="arm64-v8a"\n'
                'patches-source="MorpheApp/morphe-patches"\n'
                'patches-version="v1"\n',
                encoding="utf-8",
            )
            cache = root / "cache"
            output = root / "output"
            args = argparse.Namespace(
                config=config,
                cache=cache,
                output=output,
                keystore=None,
                keystore_alias="morphe",
                json=False,
            )
            compatibility = Mock(
                selected_version="21.04.223", selected_patches=("SponsorBlock",)
            )
            result = BuildResult(
                "com.google.android.youtube",
                "21.04.223",
                "1561052632",
                "arm64-v8a",
                ("youtube/arm64-v8a/youtube.apk",),
                ("a" * 64,),
            )
            with (
                patch("masamune.orchestrator.ensure_user_keystore") as ensure,
                patch(
                    "masamune.orchestrator.prepare_toolchain",
                    return_value=root / "tools.json",
                ),
                patch(
                    "masamune.orchestrator.apksigner_jar",
                    return_value=root / "apksigner.jar",
                ),
                patch(
                    "masamune.orchestrator._toolchain",
                    return_value=prepared_toolchain(),
                ),
                patch(
                    "masamune.orchestrator.run_morphe_compatibility",
                    return_value=compatibility,
                ),
                patch(
                    "masamune.orchestrator._build_job", return_value=result
                ) as build_job,
            ):
                summary = run_build(args)
            self.assertEqual(summary["status"], "complete")
            ensure.assert_called_once_with(cache / "masamune.p12", alias="morphe")
            self.assertEqual(
                build_job.call_args.kwargs["password"], AUTO_KEYSTORE_PASSWORD
            )

    def test_apkmirror_version_code_hint_discovers_catalog_without_configured_url(
        self,
    ) -> None:
        with patch(
            "masamune.orchestrator.resolve_apkmirror_version_code_for_package",
            return_value="2614141",
        ) as resolve:
            hint = _apkmirror_version_code_hint(
                (),
                version_name="2026.14.0",
                arch="arm64",
                reporter=Reporter(),
                package="com.reddit.frontpage",
            )
        self.assertEqual(hint, "2614141")
        resolve.assert_called_once_with(
            "com.reddit.frontpage", version_name="2026.14.0", arch="arm64"
        )

    def test_apkmirror_version_code_hint_ignores_ambiguous_pages(self) -> None:
        with (
            patch(
                "masamune.orchestrator._apkmirror_version_code",
                side_effect=ProviderAmbiguous("APKMirror sources disagree"),
            ),
            redirect_stderr(io.StringIO()) as stderr,
        ):
            hint = _apkmirror_version_code_hint(
                ("https://www.apkmirror.com/apk/reddit-inc/reddit/",),
                version_name="1.2.3",
                arch="arm64",
                reporter=Reporter(json_output=True),
                package="com.reddit.frontpage",
            )
        self.assertIsNone(hint)
        self.assertIn("APKMirror version-code hint unavailable", stderr.getvalue())

    def test_apkmirror_version_code_hint_ignores_unavailable_pages(self) -> None:
        with (
            patch(
                "masamune.orchestrator._apkmirror_version_code",
                side_effect=ProviderUnavailable("APKMirror request failed"),
            ),
            redirect_stderr(io.StringIO()) as stderr,
        ):
            hint = _apkmirror_version_code_hint(
                ("https://www.apkmirror.com/apk/reddit-inc/reddit/",),
                version_name="1.2.3",
                arch="arm64",
                reporter=Reporter(json_output=True),
                package="com.reddit.frontpage",
            )
        self.assertIsNone(hint)
        self.assertIn("APKMirror version-code hint unavailable", stderr.getvalue())

    def test_apkmirror_version_code_hint_ignores_missing_versions(self) -> None:
        with (
            patch(
                "masamune.orchestrator._apkmirror_version_code",
                side_effect=VersionUnavailable("APKMirror version is unavailable"),
            ),
            redirect_stderr(io.StringIO()) as stderr,
        ):
            hint = _apkmirror_version_code_hint(
                ("https://www.apkmirror.com/apk/reddit-inc/reddit/",),
                version_name="2026.14.0",
                arch="arm64",
                reporter=Reporter(json_output=True),
                package="com.reddit.frontpage",
            )
        self.assertIsNone(hint)
        self.assertIn("APKMirror version-code hint unavailable", stderr.getvalue())


    def test_release_body_lists_builds_requirements_and_patch_changelogs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            summary = _summary(
                [
                    BuildResult(
                        "com.google.android.youtube",
                        "21.04.223",
                        "1541200000",
                        "arm64-v8a",
                        (
                            "youtube/arm64-v8a/youtube.apk",
                            "youtube/arm64-v8a/youtube-root.apk",
                            "youtube/arm64-v8a/youtube-module.zip",
                        ),
                        name="YouTube",
                        patches_repository="MorpheApp/morphe-patches",
                        patches_tag="v1.2.3",
                    ),
                    BuildResult(
                        "com.google.android.apps.youtube.music",
                        "8.01.53",
                        "1541200001",
                        "arm-v7a",
                        ("music/arm-v7a/music.apk",),
                        name="YouTube Music",
                        patches_repository="MorpheApp/morphe-patches",
                        patches_tag="v1.2.3",
                    ),
                ]
            )
            _write_summary(output, summary)
            body = (output / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn("| YouTube | 21.04.223 | arm64-v8a |", body)
        self.assertIn("| YouTube Music | 8.01.53 | arm-v7a |", body)
        self.assertIn("Morphe MicroG-RE", body)
        self.assertIn("zygisk-detach", body)
        self.assertIn(
            "https://github.com/MorpheApp/morphe-patches/releases/tag/v1.2.3",
            body,
        )
        self.assertIn("YouTube, YouTube Music", body)

    def test_build_failure_leaves_no_published_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "morphe.toml"
            config.write_text(
                '[[apps]]\npackage="com.google.android.youtube"\nname="YouTube"\n'
                'version="21.04.223"\narch="arm64-v8a"\n',
                encoding="utf-8",
            )
            keystore = root / "builder.p12"
            keystore.write_bytes(b"key")
            args = argparse.Namespace(
                config=config,
                cache=root / "cache",
                output=root / "output",
                keystore=keystore,
                keystore_alias="morphe",
                json=False,
            )
            with (
                patch.dict("os.environ", {"MORPHE_KEYSTORE_PASSWORD": "secret"}),
                patch("masamune.orchestrator._local_version_name", return_value="21.04.223"),
                patch(
                    "masamune.orchestrator.prepare_toolchain",
                    return_value=root / "tools.json",
                ),
                patch(
                    "masamune.orchestrator.apksigner_jar",
                    return_value=root / "apksigner.jar",
                ),
                patch(
                    "masamune.orchestrator._toolchain",
                    return_value=prepared_toolchain(),
                ),
                patch(
                    "masamune.orchestrator.run_morphe_compatibility",
                    return_value=Mock(selected_version="21.04.223"),
                ),
                patch(
                    "masamune.orchestrator._build_job",
                    side_effect=BuildError("patch failed"),
                ),
                self.assertRaises(BuildError),
            ):
                run_build(args)
            self.assertFalse(args.output.exists())


class PatchCatalogTest(unittest.TestCase):
    _CONFIG = """
[[apps]]
package = "com.example.app"
name = "Example"
exclusive-patches = ["Hide ads"]
"""
    _OUTPUT = """
Name: Hide ads
Enabled: true
Package name: com.example.app
1.2.3
Name: Theme
Enabled: false
Package name: com.example.app
1.2.3
"""

    def test_catalog_lists_available_patches_and_current_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "morphe.toml"
            config.write_text(self._CONFIG, encoding="utf-8")
            with (
                patch(
                    "masamune.orchestrator.prepare_toolchain",
                    return_value=Path("tools"),
                ) as prepare,
                patch(
                    "masamune.orchestrator._toolchain",
                    return_value=prepared_toolchain(),
                ),
                patch(
                    "masamune.orchestrator.run_morphe_action",
                    return_value=self._OUTPUT,
                ) as action,
            ):
                result = run_patch_catalog(
                    config, cache=Path("cache"), package="com.example.app"
                )
        self.assertEqual(prepare.call_count, 1)
        self.assertEqual(action.call_args.args[4], "list-patches")
        self.assertTrue(action.call_args.kwargs["with_options"])
        self.assertEqual(result["selected"], ["Hide ads"])
        self.assertEqual(result["configured_options"], {})
        patches = result["patches"]
        assert isinstance(patches, list)
        self.assertEqual(
            [(entry["name"], entry["enabled"]) for entry in patches],
            [("Hide ads", True), ("Theme", False)],
        )

    def test_catalog_prepares_only_selected_app_toolchain(self) -> None:
        config_text = (
            self._CONFIG
            + """
[[apps]]
package = "com.example.other"
name = "Other"
patches-source = "other/patches"
patches-version = "v9.9.9"
"""
        )
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "morphe.toml"
            config.write_text(config_text, encoding="utf-8")
            with (
                patch(
                    "masamune.orchestrator.prepare_toolchain",
                    return_value=Path("tools"),
                ) as prepare,
                patch(
                    "masamune.orchestrator._toolchain",
                    return_value=prepared_toolchain(),
                ),
                patch(
                    "masamune.orchestrator.run_morphe_action",
                    return_value=self._OUTPUT,
                ),
            ):
                run_patch_catalog(
                    config, cache=Path("cache"), package="com.example.app"
                )
        self.assertEqual(prepare.call_count, 1)
        self.assertEqual(
            prepare.call_args.args[2]["morphe-patches"],
            "MorpheApp/morphe-patches",
        )

    def test_catalog_rejects_unconfigured_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "morphe.toml"
            config.write_text(self._CONFIG, encoding="utf-8")
            with self.assertRaisesRegex(BuildError, "not configured"):
                run_patch_catalog(
                    config, cache=Path("cache"), package="com.example.other"
                )


class OrchestratorUxTest(unittest.TestCase):
    def test_verify_returns_machine_readable_identity(self) -> None:
        metadata = [
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
            )
        ]
        with patch("masamune.orchestrator.verify_apk_set", return_value=metadata):
            result = run_verify(
                Path("trusted"),
                "com.example.app",
                version_name="1.2.3",
                version_code="123",
                arch="arm64",
            )
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["files"], 1)

    def test_clean_removes_disposable_data_and_preserves_trusted_caches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            for name in ("work", "toolchains", "tools"):
                (cache / name).mkdir()
                (cache / name / "marker").write_text(name)
            (cache / "github-releases").mkdir()
            (cache / "github-releases" / "release.json").write_text("{}")
            (cache / "google-version-mappings.json").write_text("{}")
            result = run_clean(cache)
            self.assertFalse((cache / "work").exists())
            self.assertFalse((cache / "github-releases").exists())
            self.assertFalse((cache / "google-version-mappings.json").exists())
            self.assertTrue((cache / "toolchains" / "marker").is_file())
            self.assertTrue((cache / "tools" / "marker").is_file())
            self.assertEqual(
                result["removed"],
                [
                    str((cache / "work").resolve()),
                    str((cache / "github-releases").resolve()),
                    str((cache / "google-version-mappings.json").resolve()),
                ],
            )

    def test_clean_selected_areas_can_purge_reusable_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            for name in ("toolchains", "tools", "locks"):
                (cache / name).mkdir()
                (cache / name / "marker").write_text(name)
            result = run_clean(cache, selected=("toolchains", "tools"))
            self.assertFalse((cache / "toolchains").exists())
            self.assertFalse((cache / "tools").exists())
            self.assertTrue((cache / "locks" / "marker").is_file())
            self.assertEqual(
                result["removed"],
                [str((cache / "toolchains").resolve()), str((cache / "tools").resolve())],
            )

    def test_google_provider_scopes_proxy_and_translates_failures(self) -> None:
        request = ProviderRequest("com.example.app", "1", "1", "arm64", Path("trusted"))
        runner = Mock(side_effect=GooglePlayAuthUnavailable("dispenser unavailable"))
        provider = GooglePlayProvider(
            None, None, None, "https://proxy.invalid", runner=runner
        )
        with patch.dict(
            "os.environ", {"HTTPS_PROXY": "https://old.invalid"}, clear=True
        ):
            with self.assertRaisesRegex(ProviderUnavailable, "dispenser unavailable"):
                provider.download(request)
            self.assertEqual(os.environ["HTTPS_PROXY"], "https://old.invalid")
            self.assertNotIn("HTTP_PROXY", os.environ)

    def test_google_provider_translates_missing_version(self) -> None:
        request = ProviderRequest("com.example.app", "1", "1", "arm64", Path("trusted"))
        provider = GooglePlayProvider(
            None,
            None,
            None,
            None,
            runner=Mock(
                side_effect=GooglePlayVersionUnavailable("version unavailable")
            ),
        )
        with self.assertRaisesRegex(VersionUnavailable, "version unavailable"):
            provider.download(request)


if __name__ == "__main__":
    unittest.main()
