import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from masamune.compatibility import (  # pyright: ignore[reportMissingImports]
    CompatibilityError,
    ConfirmedVersion,
    confirm_version_code,
    morphe_command,
    parse_patch_list,
    parse_version_list,
    resolve_google_version,
    resolve_google_versions,
    resolve_patch_compatibility,
    resolve_version_code_candidate,
)
from masamune.config import (  # pyright: ignore[reportMissingImports]
    ConfigError,
    app_include_experimental_versions,
    app_include_universal_patches,
    app_toolchain,
    load_config,
    parse_config,
)

FIXTURES = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).parent.parent


class ConfigTest(unittest.TestCase):
    def test_empty_configuration_is_valid_before_first_app(self) -> None:
        from masamune.config import parse_config

        config = parse_config({})

        self.assertEqual(config.apps, ())

    def test_loads_strict_youtube_and_music_configuration(self) -> None:
        text = """
[toolchain]
morphe-source = "MorpheApp/morphe-desktop"
morphe-version = "v1.12.0"
patches-source = "MorpheApp/morphe-patches"
patches-version = "latest"

[[apps]]
package = "com.google.android.youtube"
name = "YouTube"
version = "21.04.223"
version-code = 1541200000
build-mode = "both"
arch = "both"
density = "xxhdpi"
include-patches = ["Hide ads", "SponsorBlock"]
exclude-patches = []

[apps.patch-options."SponsorBlock"]
toast-on-connection-error = true
server = "https://sponsor.ajay.app"

[apps.google-play]
profile = "D2"
country = "us"
proxy = "https://proxy.example.invalid"
dispenser = "https://dispenser.example.invalid/token"

[apps.fallbacks]
direct = ["https://downloads.example.invalid/youtube.apk"]
apkmirror = ["https://www.apkmirror.com/apk/google-inc/youtube/"]

[[apps]]
package = "com.google.android.apps.youtube.music"
name = "YouTube Music"
version = "auto"
build-mode = "apk"
arch = "arm64-v8a"
exclusive-patches = ["Music only"]
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "morphe.toml"
            path.write_text(text, encoding="utf-8")
            config = load_config(path)
        self.assertEqual(config.toolchain.morphe_version, "v1.12.0")
        youtube, music = config.apps
        self.assertEqual(youtube.version_code, "1541200000")
        self.assertEqual((youtube.build_mode, youtube.arch), ("both", "both"))
        self.assertTrue(
            youtube.patch_options["SponsorBlock"]["toast-on-connection-error"]
        )
        self.assertEqual(youtube.google_play.country, "US")
        self.assertEqual(music.exclusive_patches, ("Music only",))

    def test_example_uses_arch_neutral_profile_for_multi_arch_app(self) -> None:
        config = parse_config(
            {
                "apps": [
                    {
                        "package": "com.google.android.apps.youtube.music",
                        "name": "YouTube Music",
                        "arch": "both",
                    },
                    {
                        "package": "com.reddit.frontpage",
                        "name": "Reddit",
                    },
                ]
            }
        )
        music = next(app for app in config.apps if app.arch == "both")
        self.assertIsNone(music.google_play.profile)
        self.assertIsNone(music.density)
        reddit = next(
            app for app in config.apps if app.package == "com.reddit.frontpage"
        )
        self.assertEqual(reddit.fallbacks.apkmirror, ())
        self.assertIsNone(reddit.patched_package)

    def test_app_toolchain_overrides_each_source_without_changing_defaults(
        self,
    ) -> None:
        text = """
[toolchain]
morphe-source = "MorpheApp/morphe-desktop"
morphe-version = "v1"
patches-source = "MorpheApp/morphe-patches"
patches-version = "v2"

[[apps]]
package = "com.google.android.youtube"
name = "YouTube"
cli-source = "j-hc/revanced-cli"
morphe-version = "v3"
patches-source = "j-hc/revanced-patches"
patches-version = "v4"
patches-sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "morphe.toml"
            path.write_text(text, encoding="utf-8")
            config = load_config(path)
        selected = app_toolchain(config.apps[0], config.toolchain)
        self.assertEqual(selected.morphe_source, "j-hc/revanced-cli")
        self.assertEqual(selected.morphe_version, "v3")
        self.assertEqual(selected.patches_source, "j-hc/revanced-patches")
        self.assertEqual(selected.patches_version, "v4")
        self.assertEqual(selected.patches_sha256, "a" * 64)
        self.assertIsInstance(selected.patches_source, str)

    def test_universal_patches_default_off_and_overridable_per_app(self) -> None:
        text = """
[toolchain]
include-universal-patches = true

[[apps]]
package="com.example.app"
name="App"

[[apps]]
package="com.example.other"
name="Other"
include-universal-patches = false
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "morphe.toml"
            path.write_text(text, encoding="utf-8")
            config = load_config(path)
        self.assertTrue(config.toolchain.include_universal_patches)
        inherited, overridden = config.apps
        self.assertIsNone(inherited.include_universal_patches)
        self.assertTrue(app_include_universal_patches(inherited, config.toolchain))
        self.assertFalse(overridden.include_universal_patches)
        self.assertFalse(app_include_universal_patches(overridden, config.toolchain))

    def test_universal_patches_default_off_without_toolchain_setting(self) -> None:
        text = '[[apps]]\npackage="com.example.app"\nname="App"\n'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "morphe.toml"
            path.write_text(text, encoding="utf-8")
            config = load_config(path)
        self.assertFalse(config.toolchain.include_universal_patches)
        self.assertFalse(
            app_include_universal_patches(config.apps[0], config.toolchain)
        )

    def test_experimental_versions_default_off_and_overridable_per_app(self) -> None:
        text = """
[toolchain]
include-experimental-versions = true

[[apps]]
package="com.example.app"
name="App"

[[apps]]
package="com.example.other"
name="Other"
include-experimental-versions = false
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "morphe.toml"
            path.write_text(text, encoding="utf-8")
            config = load_config(path)
        self.assertTrue(config.toolchain.include_experimental_versions)
        inherited, overridden = config.apps
        self.assertIsNone(inherited.include_experimental_versions)
        self.assertTrue(app_include_experimental_versions(inherited, config.toolchain))
        self.assertFalse(overridden.include_experimental_versions)
        self.assertFalse(
            app_include_experimental_versions(overridden, config.toolchain)
        )

    def test_accepts_optional_expected_signer(self) -> None:
        text = """
[[apps]]
package="com.example.app"
name="App"
expected-signer="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "morphe.toml"
            path.write_text(text, encoding="utf-8")
            config = load_config(path)
        self.assertEqual(config.apps[0].expected_signer, "a" * 64)

    def test_rejects_unknown_keys_and_invalid_combinations(self) -> None:
        cases = {
            "unknown": '[[apps]]\npackage="com.example.app"\nname="App"\nwat=true\n',
            "exclusive": '[[apps]]\npackage="com.example.app"\nname="App"\nexclusive-patches=["A"]\ninclude-patches=["A"]\n',
            "version code": '[[apps]]\npackage="com.example.app"\nname="App"\nversion="auto"\nversion-code=12\n',
            "bad arch": '[[apps]]\npackage="com.example.app"\nname="App"\narch="x86"\n',
            "bad source": '[[apps]]\npackage="com.example.app"\nname="App"\npatches-source="not-a-repo"\n',
            "multiple patch sources": '[[apps]]\npackage="com.example.app"\nname="App"\npatches-source=["owner/one", "owner/two"]\n',
            "bad signer": '[[apps]]\npackage="com.example.app"\nname="App"\nexpected-signer="not-a-fingerprint"\n',
        }
        for name, text in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "bad.toml"
                path.write_text(text, encoding="utf-8")
                with self.assertRaises(ConfigError):
                    load_config(path)


class PatchCompatibilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.patch_output = (FIXTURES / "morphe-list-patches.txt").read_text(
            encoding="utf-8"
        )
        self.version_output = (FIXTURES / "morphe-list-versions.txt").read_text(
            encoding="utf-8"
        )

    def test_builds_verified_morphe_cli_commands(self) -> None:
        command = morphe_command(
            "java",
            Path("morphe.jar"),
            "list-versions",
            Path("patches.mpp"),
            "com.example.app",
        )
        self.assertEqual(command[:4], ["java", "-jar", "morphe.jar", "list-versions"])
        self.assertIn("--patches=patches.mpp", command)
        self.assertIn("--filter-package-names=com.example.app", command)

    def test_list_patches_universal_flag_defaults_to_false(self) -> None:
        command = morphe_command(
            "java",
            Path("morphe.jar"),
            "list-patches",
            Path("patches.mpp"),
            "com.example.app",
        )
        self.assertIn("--with-universal-patches=false", command)

    def test_list_patches_can_opt_into_universal_patches(self) -> None:
        command = morphe_command(
            "java",
            Path("morphe.jar"),
            "list-patches",
            Path("patches.mpp"),
            "com.example.app",
            include_universal_patches=True,
        )
        self.assertIn("--with-universal-patches=true", command)

    def test_list_patches_can_request_option_metadata(self) -> None:
        command = morphe_command(
            "java",
            Path("morphe.jar"),
            "list-patches",
            Path("patches.mpp"),
            "com.example.app",
            with_options=True,
        )
        self.assertIn("--with-options=true", command)

    def test_patch_list_parses_typed_option_metadata(self) -> None:
        output = """Name: Theme
Enabled: true
Options:
\tTitle: Enabled
\tDescription: Toggle feature.
\tRequired: false
\tKey: enabled
\tDefault: false
\tType: kotlin.Boolean
\t
\tTitle: Color
\tDescription: Pick or enter a color.
\tRequired: false
\tKey: color
\tDefault: #000000
\tPossible values:
\t\t#000000 (Black)
\t\t#FFFFFF (White)
\tType: kotlin.String
Compatible packages:
\tPackage name: com.example.app
\tCompatible versions:
\t\t1.0.0
"""
        patch = parse_patch_list(output, "com.example.app")[0]
        self.assertEqual(len(patch.options), 2)
        self.assertEqual(patch.options[0].default, False)
        self.assertEqual(patch.options[1].values, ("#000000", "#FFFFFF"))

    def test_experimental_versions_flag_defaults_to_false_for_both_actions(
        self,
    ) -> None:
        for action in ("list-patches", "list-versions"):
            command = morphe_command(
                "java",
                Path("morphe.jar"),
                action,
                Path("patches.mpp"),
                "com.example.app",
            )
            self.assertIn("--include-experimental=false", command)

    def test_experimental_versions_can_be_opted_into_for_both_actions(self) -> None:
        for action in ("list-patches", "list-versions"):
            command = morphe_command(
                "java",
                Path("morphe.jar"),
                action,
                Path("patches.mpp"),
                "com.example.app",
                include_experimental_versions=True,
            )
            self.assertIn("--include-experimental=true", command)

    def test_universal_patch_is_selectable_and_does_not_narrow_versions(self) -> None:
        package = "com.google.android.youtube"
        patches = parse_patch_list(self.patch_output, package)
        versions = parse_version_list(self.version_output, package)
        result = resolve_patch_compatibility(
            package,
            patches,
            versions,
            exclusive=("Hide ads", "Change package name"),
        )
        self.assertEqual(result.selected_patches, ("Hide ads", "Change package name"))
        self.assertEqual(result.compatible_versions, ("21.04.223", "20.51.39"))

    def test_universal_patch_with_no_package_line_is_recognized(self) -> None:
        # morphe-desktop >= v1.12.0 omits the "Compatible packages:" block
        # entirely for Universal patches instead of printing the literal
        # "(universal)" marker.
        output = (
            "Name: Change package name\n"
            "Enabled: false\n"
            "\n"
            "Name: Hide ads\n"
            "Enabled: true\n"
            "Compatible packages:\n"
            "\tPackage name: com.example.app\n"
            "\tCompatible versions:\n"
            "\t\t1.0.0\n"
        )
        patches = parse_patch_list(output, "com.example.app")
        names = {patch.name for patch in patches}
        self.assertIn("Change package name", names)

    def test_only_universal_patches_selected_is_rejected(self) -> None:
        package = "com.google.android.youtube"
        patches = parse_patch_list(self.patch_output, package)
        versions = parse_version_list(self.version_output, package)
        with self.assertRaisesRegex(CompatibilityError, "no version-compatible"):
            resolve_patch_compatibility(
                package,
                patches,
                versions,
                exclusive=("Change package name",),
            )

    def test_parses_ansi_and_selects_latest_common_version(self) -> None:
        package = "com.google.android.youtube"
        patches = parse_patch_list(self.patch_output, package)
        versions = parse_version_list(self.version_output, package)
        result = resolve_patch_compatibility(package, patches, versions)
        self.assertEqual(result.selected_patches, ("Hide ads", "SponsorBlock"))
        self.assertEqual(result.selected_version, "21.04.223")
        self.assertEqual(result.compatible_versions, ("21.04.223",))

    def test_respects_explicit_version_and_rejects_no_common_version(self) -> None:
        package = "com.google.android.youtube"
        patches = parse_patch_list(self.patch_output, package)
        versions = parse_version_list(self.version_output, package)
        result = resolve_patch_compatibility(
            package,
            patches,
            versions,
            exclusive=("Hide ads",),
            requested_version="20.51.39",
        )
        self.assertEqual(result.selected_version, "20.51.39")
        with self.assertRaisesRegex(CompatibilityError, "incompatible"):
            resolve_patch_compatibility(
                package,
                patches,
                versions,
                exclusive=("SponsorBlock",),
                requested_version="20.51.39",
            )
        with self.assertRaisesRegex(CompatibilityError, "no common version"):
            resolve_patch_compatibility(
                package,
                patches,
                versions,
                exclusive=("SponsorBlock", "Legacy only"),
            )

    def test_invokes_both_morphe_commands_without_shell(self) -> None:
        completed = [
            Mock(stdout=self.patch_output),
            Mock(stdout=self.version_output),
        ]
        with patch(
            "masamune.compatibility.subprocess.run", side_effect=completed
        ) as run:
            from masamune.compatibility import run_morphe_compatibility

            result = run_morphe_compatibility(
                "java",
                Path("morphe.jar"),
                Path("patches.mpp"),
                "com.google.android.youtube",
                exclusive=("Hide ads",),
            )
        self.assertEqual(result.selected_version, "21.04.223")
        self.assertEqual(run.call_count, 2)
        self.assertTrue(
            all(
                call.args[0] and isinstance(call.args[0], list)
                for call in run.call_args_list
            )
        )
        self.assertTrue(all("shell" not in call.kwargs for call in run.call_args_list))


class GoogleVersionResolverTest(unittest.TestCase):
    package = "com.google.android.youtube"
    version = "21.04.223"

    def test_current_version_then_cache_hit(self) -> None:
        probe = Mock(
            return_value=ConfirmedVersion(self.version, "1541200000", "google-response")
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = self.resolve(root, probe)
            probe.reset_mock()
            cached = self.resolve(root, probe)
        self.assertEqual(current.version_code, "1541200000")
        self.assertEqual(cached.source, "cache")
        probe.assert_not_called()

    def test_historical_candidate_is_confirmed_by_google(self) -> None:
        calls = []

        def probe(package, version, arch, profile, region, candidate):
            calls.append(candidate)
            if candidate == "1530000000":
                return ConfirmedVersion(version, candidate, "google-response")
            return None

        metadata = Mock(return_value="1530000000")
        with tempfile.TemporaryDirectory() as directory:
            result = self.resolve(Path(directory), probe, fallback_metadata=metadata)
        self.assertEqual(result.version_code, "1530000000")
        self.assertEqual(calls, ["1530000000"])

    def test_metadata_candidate_is_not_cached_until_manifest_confirms_it(self) -> None:
        metadata = Mock(return_value="1530000000")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = resolve_version_code_candidate(
                root,
                package=self.package,
                version_name=self.version,
                arch="arm64",
                profile="D2",
                region="US",
                fallback_metadata=metadata,
            )
            self.assertEqual(
                candidate, ConfirmedVersion(self.version, "1530000000", "metadata")
            )
            confirm_version_code(
                root,
                package=self.package,
                version_name=self.version,
                arch="arm64",
                profile="D2",
                region="US",
                version_code="1530000000",
            )
            cached = resolve_version_code_candidate(
                root,
                package=self.package,
                version_name=self.version,
                arch="arm64",
                profile="D2",
                region="US",
                fallback_metadata=Mock(),
            )
        self.assertEqual(cached, ConfirmedVersion(self.version, "1530000000", "cache"))

    def test_rejects_mismatch_without_caching_or_fallback(self) -> None:
        probe = Mock(
            return_value=ConfirmedVersion("21.05.001", "1541200001", "google-response")
        )
        fallback = Mock()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            try:
                self.resolve(root, probe, fallback_download=fallback)
            except CompatibilityError as error:
                self.assertIn("mismatch", str(error))
            else:
                self.fail("version-name mismatch accepted")
            self.assertFalse((root / "google-version-mappings.json").exists())
        fallback.assert_not_called()

    def test_rejects_unconfirmed_fallback_metadata(self) -> None:
        fallback = Mock(
            return_value=ConfirmedVersion(self.version, "1530000000", "metadata")
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaisesRegex(CompatibilityError, "confirmed evidence"),
        ):
            self.resolve(
                Path(directory),
                Mock(return_value=None),
                fallback_download=fallback,
            )

    def test_unavailable_version_fails_after_fallback_download(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaisesRegex(CompatibilityError, "unavailable"),
        ):
            self.resolve(
                Path(directory),
                Mock(return_value=None),
                fallback_metadata=Mock(return_value=None),
                fallback_download=Mock(return_value=None),
            )

    def test_resolves_architectures_independently(self) -> None:
        def probe(package, version, arch, profile, region, candidate):
            code = "1541200000" if arch == "arm64" else "1541200001"
            return ConfirmedVersion(version, code, "google-response")

        with tempfile.TemporaryDirectory() as directory:
            result = resolve_google_versions(
                Path(directory),
                package=self.package,
                version_name=self.version,
                arches=("arm64", "armv7"),
                profile="D2",
                region="US",
                google_probe=probe,
            )
        self.assertEqual(
            {arch: value.version_code for arch, value in result.items()},
            {"arm64": "1541200000", "armv7": "1541200001"},
        )

    def test_explicit_version_code_has_first_priority(self) -> None:
        probe = Mock()
        with tempfile.TemporaryDirectory() as directory:
            result = self.resolve(
                Path(directory), probe, explicit_version_code="1541200000"
            )
        self.assertEqual(result.source, "explicit")
        probe.assert_not_called()

    def resolve(self, root, probe, **kwargs):
        return resolve_google_version(
            root,
            package=self.package,
            version_name=self.version,
            arch="arm64",
            profile="D2",
            region="US",
            google_probe=probe,
            **kwargs,
        )


if __name__ == "__main__":
    unittest.main()
