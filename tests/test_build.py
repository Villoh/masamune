import json
import subprocess
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from masamune.apk import ApkMetadata  # pyright: ignore[reportMissingImports]
from masamune.architecture import (
    Architecture,  # pyright: ignore[reportMissingImports]
)
from masamune.build import (  # pyright: ignore[reportMissingImports]
    AUTO_KEYSTORE_PASSWORD,
    DEFAULT_PATCHED_PACKAGES,
    BuildError,
    _verify_output,
    build_apk,
    create_keystore,
    ensure_user_keystore,
    morphe_patch_command,
    sign_apk,
)


class YouTubeBuildTest(unittest.TestCase):
    def test_default_morphe_patched_packages(self) -> None:
        self.assertEqual(
            DEFAULT_PATCHED_PACKAGES["com.google.android.youtube"],
            "app.morphe.android.youtube",
        )
        self.assertEqual(
            DEFAULT_PATCHED_PACKAGES["com.google.android.apps.youtube.music"],
            "app.morphe.android.apps.youtube.music",
        )

    def test_accepts_universal_signed_output_for_target_architecture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "patched.apk"
            with zipfile.ZipFile(output, "w") as apk:
                apk.writestr("AndroidManifest.xml", b"manifest")
            metadata = replace(
                ApkMetadata(
                    "patched.apk",
                    "app.morphe.android.youtube",
                    "21.04.223",
                    "1541200000",
                    None,
                    "base",
                    "universal",
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
            with patch("masamune.build.inspect_apk", return_value=metadata):
                result = _verify_output(
                    output,
                    package="app.morphe.android.youtube",
                    version_name="21.04.223",
                    version_code="1541200000",
                    arch="armv7",
                )
            self.assertEqual(result.abi, "universal")

    def test_builds_unsigned_patches_signs_and_records_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {
                name: root / name
                for name in (
                    "cli.jar",
                    "patches.mpp",
                    "signer.jar",
                    "merged.apk",
                    "source.json",
                    "merge.json",
                    "builder.p12",
                )
            }
            for path in paths.values():
                path.write_bytes(path.name.encode())
            calls = []
            stages: list[str] = []

            def run(command, **kwargs):
                calls.append((command, kwargs))
                if "patch" in command:
                    output = Path(
                        next(
                            value.removeprefix("--out=")
                            for value in command
                            if value.startswith("--out=")
                        )
                    )
                    output.write_bytes(b"unsigned")
                else:
                    output = (
                        Path(command[command.index("--out") + 1])
                        / "youtube-unsigned-aligned-signed.apk"
                    )
                    with zipfile.ZipFile(output, "w") as apk:
                        apk.writestr("AndroidManifest.xml", b"manifest")
                        apk.writestr("lib/arm64-v8a/libx.so", b"x")
                return subprocess.CompletedProcess(command, 0, "", "")

            metadata = replace(
                ApkMetadata(
                    "x",
                    "app.morphe.android.youtube",
                    "21.04.223",
                    "1541200000",
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
            with (
                patch("masamune.build.subprocess.run", side_effect=run),
                patch("masamune.build.inspect_apk", return_value=metadata),
            ):
                output = build_apk(
                    source_package="com.google.android.youtube",
                    slug="youtube",
                    patched_package="app.morphe.android.youtube",
                    arch=Architecture.ARM64,
                    java="java",
                    cli=paths["cli.jar"],
                    patches=paths["patches.mpp"],
                    signer=paths["signer.jar"],
                    merged_stock=paths["merged.apk"],
                    source_provenance=paths["source.json"],
                    merge_provenance=paths["merge.json"],
                    output_directory=root / "out",
                    version_name="21.04.223",
                    version_code="1541200000",
                    keystore=paths["builder.p12"],
                    keystore_alias="morphe",
                    keystore_password="secret",
                    include=("Hide ads",),
                    options={"Hide ads": {"enabled": True}},
                    stage_reporter=lambda stage, _mode: stages.append(stage),
                )
            self.assertEqual(stages, ["patch", "sign"])
            self.assertEqual(
                output.name,
                f"youtube-21.04.223-{Architecture.ARM64.value}.apk",
            )
            self.assertIn("--unsigned", calls[0][0])
            self.assertIn("--patches=" + str(paths["patches.mpp"]), calls[0][0])
            self.assertNotIn("secret", calls[1][0])
            self.assertEqual(calls[1][1]["input"], "secret\nsecret\n")
            provenance_text = output.with_suffix(".provenance.json").read_text()
            self.assertNotIn("secret", provenance_text)
            provenance = json.loads(provenance_text)
            self.assertEqual(provenance["package"], "app.morphe.android.youtube")
            self.assertEqual(provenance["source_package"], "com.google.android.youtube")
            self.assertEqual(provenance["smoke_test"]["status"], "pending")
            self.assertEqual(provenance["output"]["certificate_sha256"], ["a" * 64])
            self.assertEqual(
                provenance["inputs"]["morphe_cli"]["sha256"], sha256(paths["cli.jar"])
            )

    def test_signs_and_verifies_merged_stock_before_patch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "merged.apk"
            signer = root / "signer.jar"
            keystore = root / "builder.p12"
            for path in (source, signer, keystore):
                path.write_bytes(b"input")
            output = root / "merged-signed.apk"
            metadata = replace(
                ApkMetadata(
                    "x",
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

            def run(command, **_kwargs):
                signed = Path(command[command.index("--out") + 1]) / "signed.apk"
                with zipfile.ZipFile(signed, "w") as apk:
                    apk.writestr("AndroidManifest.xml", b"manifest")
                    apk.writestr("lib/arm64-v8a/libx.so", b"x")
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                patch("masamune.build.subprocess.run", side_effect=run),
                patch("masamune.build.inspect_apk", return_value=metadata),
            ):
                result = sign_apk(
                    input_apk=source,
                    output=output,
                    package="com.example.app",
                    version_name="1.2.3",
                    version_code="123",
                    arch="arm64-v8a",
                    java="java",
                    signer=signer,
                    keystore=keystore,
                    keystore_alias="morphe",
                    keystore_password="secret",
                )
            self.assertEqual(result.signers_sha256, ("a" * 64,))
            self.assertTrue(output.is_file())

    def test_builds_real_morphe_v1_12_style_arguments_and_rejects_option_collisions(
        self,
    ) -> None:
        command = morphe_patch_command(
            "java",
            Path("morphe.jar"),
            Path("patches.mpp"),
            Path("stock.apk"),
            Path("out.apk"),
            exclusive=("Hide ads",),
            options={
                "Hide ads": {"enabled": True, "server": "https://example.invalid"}
            },
        )
        self.assertEqual(
            command[:5], ["java", "-jar", "morphe.jar", "patch", "stock.apk"]
        )
        self.assertIn("--exclusive", command)
        self.assertIn("--enable=Hide ads", command)
        self.assertIn("--options=enabled=true", command)
        self.assertLess(
            command.index("--patches=patches.mpp"), command.index("--enable=Hide ads")
        )
        self.assertEqual(sum(arg.startswith("--patches=") for arg in command), 1)
        with self.assertRaisesRegex(BuildError, "duplicate"):
            morphe_patch_command(
                "java",
                Path("m.jar"),
                Path("p.mpp"),
                Path("s.apk"),
                Path("o.apk"),
                options={"A": {"x": 1}, "B": {"x": 2}},
            )

    def test_creates_external_keystore_without_password_in_argv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "keys" / "builder.p12"

            def run(command, **kwargs):
                path.write_bytes(b"keystore")
                self.assertNotIn("secret", command)
                self.assertEqual(kwargs["env"]["MORPHE_KEYSTORE_PASSWORD"], "secret")
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("masamune.build.subprocess.run", side_effect=run):
                self.assertEqual(
                    create_keystore(
                        path,
                        alias="morphe",
                        password="secret",
                        repository=root / "repo",
                    ),
                    path,
                )

    def test_ensure_user_keystore_generates_once_then_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "keystore.p12"
            calls = []

            def run(command, **kwargs):
                calls.append(command)
                path.write_bytes(b"keystore")
                self.assertEqual(
                    kwargs["env"]["MORPHE_KEYSTORE_PASSWORD"], AUTO_KEYSTORE_PASSWORD
                )
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("masamune.build.subprocess.run", side_effect=run):
                ensure_user_keystore(path, alias="morphe")
                self.assertEqual(len(calls), 1)
                # Second call must not invoke keytool again; keystore already exists.
                ensure_user_keystore(path, alias="morphe")
                self.assertEqual(len(calls), 1)


class MultiAppBuildTest(unittest.TestCase):
    def test_root_build_rejects_gmscore_only_selection(self) -> None:
        from masamune.build import build_apk

        with self.assertRaisesRegex(BuildError, "no patches.*GmsCore"):
            build_apk(
                source_package="com.google.android.youtube",
                arch="arm64-v8a",
                java="java",
                cli=Path("cli.jar"),
                patches=Path("patches.mpp"),
                signer=Path("signer.jar"),
                merged_stock=Path("stock.apk"),
                source_provenance=Path("source.json"),
                merge_provenance=Path("merge.json"),
                output_directory=Path("out"),
                version_name="21.04.223",
                version_code="1561052632",
                keystore=Path("builder.p12"),
                keystore_alias="morphe",
                keystore_password="secret",
                root=True,
                exclusive=("GmsCore support",),
            )

    def test_root_build_disables_gmscore_before_patching(self) -> None:
        from masamune.build import build_apk

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [
                root / name
                for name in (
                    "cli",
                    "patches",
                    "signer",
                    "stock",
                    "source",
                    "merge",
                    "key",
                )
            ]
            for path in paths:
                path.write_bytes(b"x")

            def command(*_args, **kwargs):
                self.assertEqual(kwargs["include"], ("Hide ads",))
                self.assertIn("GmsCore support", kwargs["exclude"])
                raise RuntimeError("checked")

            with (
                patch("masamune.build.morphe_patch_command", side_effect=command),
                self.assertRaisesRegex(RuntimeError, "checked"),
            ):
                build_apk(
                    source_package="com.google.android.youtube",
                    arch="arm64-v8a",
                    java="java",
                    cli=paths[0],
                    patches=paths[1],
                    signer=paths[2],
                    merged_stock=paths[3],
                    source_provenance=paths[4],
                    merge_provenance=paths[5],
                    output_directory=root / "out",
                    version_name="21.04.223",
                    version_code="1561052632",
                    keystore=paths[6],
                    keystore_alias="morphe",
                    keystore_password="secret",
                    root=True,
                    include=("GmsCore support", "Hide ads"),
                )

    def test_root_build_also_disables_change_package_name(self) -> None:
        from masamune.build import build_apk

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [
                root / name
                for name in (
                    "cli",
                    "patches",
                    "signer",
                    "stock",
                    "source",
                    "merge",
                    "key",
                )
            ]
            for path in paths:
                path.write_bytes(b"x")

            def command(*_args, **kwargs):
                self.assertNotIn("Change package name", kwargs["include"])
                self.assertNotIn("Change package name", kwargs["exclude"])
                self.assertIn("GmsCore support", kwargs["exclude"])
                raise RuntimeError("checked")

            with (
                patch("masamune.build.morphe_patch_command", side_effect=command),
                self.assertRaisesRegex(RuntimeError, "checked"),
            ):
                build_apk(
                    source_package="com.reddit.frontpage",
                    patched_package="app.morphe.reddit.frontpage",
                    arch="arm64-v8a",
                    java="java",
                    cli=paths[0],
                    patches=paths[1],
                    signer=paths[2],
                    merged_stock=paths[3],
                    source_provenance=paths[4],
                    merge_provenance=paths[5],
                    output_directory=root / "out",
                    version_name="2026.14.0",
                    version_code="2614141",
                    keystore=paths[6],
                    keystore_alias="morphe",
                    keystore_password="secret",
                    root=True,
                    include=("Change package name", "GmsCore support"),
                )

    def test_skips_disabled_apps_when_expanding_build_jobs(self) -> None:
        from masamune.build import expand_build_jobs
        from masamune.config import parse_config

        config = parse_config(
            {
                "apps": [
                    {
                        "package": "com.example.disabled",
                        "name": "Disabled",
                        "enabled": False,
                    },
                    {
                        "package": "com.example.enabled",
                        "name": "Enabled",
                    },
                ]
            }
        )
        jobs = expand_build_jobs(config, Path("outputs"))
        self.assertEqual([job.app.package for job in jobs], ["com.example.enabled"])

    def test_expands_both_architectures_without_job_collisions(self) -> None:
        from masamune.build import expand_build_jobs
        from masamune.config import parse_config

        config = parse_config(
            {
                "apps": [
                    {
                        "package": "com.google.android.youtube",
                        "name": "YouTube",
                        "arch": "both",
                    },
                    {
                        "package": "com.google.android.apps.youtube.music",
                        "name": "YouTube Music",
                        "arch": "both",
                    },
                ]
            }
        )
        jobs = expand_build_jobs(config, Path("outputs"))
        self.assertEqual(len(jobs), 4)
        self.assertEqual({job.arch for job in jobs}, {"arm64-v8a", "arm-v7a"})
        self.assertEqual(len({job.output_directory for job in jobs}), 4)
        self.assertEqual(len({job.cache_key for job in jobs}), 4)

    def test_expands_configured_reddit_app_without_hardcoded_spec(self) -> None:
        from masamune.build import expand_build_jobs
        from masamune.config import parse_config

        config = parse_config(
            {
                "apps": [
                    {
                        "package": "com.reddit.frontpage",
                        "name": "Reddit",
                        "slug": "reddit",
                        "patched-package": "app.morphe.reddit",
                    }
                ]
            }
        )
        (job,) = expand_build_jobs(config, Path("outputs"))
        self.assertEqual(job.output_directory, Path("outputs/reddit/arm64-v8a"))
        self.assertEqual(job.app.patched_package, "app.morphe.reddit")

    def test_builds_youtube_music_armv7_with_independent_output(self) -> None:
        from masamune.build import build_apk

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {
                name: root / name
                for name in (
                    "cli.jar",
                    "patches.mpp",
                    "signer.jar",
                    "merged.apk",
                    "source.json",
                    "merge.json",
                    "builder.p12",
                    "tools.json",
                )
            }
            for path in paths.values():
                path.write_bytes(path.name.encode())
            paths["tools.json"].write_text(
                json.dumps(
                    {
                        "tools": [
                            {
                                "name": "morphe-cli",
                                "repository": "MorpheApp/morphe-desktop",
                                "resolved_tag": "v1",
                                "sha256": "a" * 64,
                            },
                            {
                                "name": "morphe-patches",
                                "repository": "j-hc/revanced-patches",
                                "resolved_tag": "v2",
                                "sha256": "b" * 64,
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            calls = []

            def run(command, **kwargs):
                calls.append(command)
                if "patch" in command:
                    Path(
                        next(
                            value.removeprefix("--out=")
                            for value in command
                            if value.startswith("--out=")
                        )
                    ).write_bytes(b"unsigned")
                else:
                    signed = Path(command[command.index("--out") + 1]) / "signed.apk"
                    with zipfile.ZipFile(signed, "w") as apk:
                        apk.writestr("AndroidManifest.xml", b"manifest")
                        apk.writestr("lib/armeabi-v7a/libx.so", b"x")
                return subprocess.CompletedProcess(command, 0, "", "")

            metadata = replace(
                ApkMetadata(
                    "x",
                    "app.morphe.android.apps.youtube.music",
                    "9.15.51",
                    "91551240",
                    None,
                    "base",
                    "armv7",
                    None,
                    None,
                    None,
                    (),
                    (),
                    (),
                    (),
                    ("b" * 64,),
                ),
                unsupported_requirements=(),
            )
            with (
                patch("masamune.build.subprocess.run", side_effect=run),
                patch("masamune.build.inspect_apk", return_value=metadata),
            ):
                output = build_apk(
                    source_package="com.google.android.apps.youtube.music",
                    slug="youtube-music",
                    patched_package="app.morphe.android.apps.youtube.music",
                    arch="arm-v7a",
                    java="java",
                    cli=paths["cli.jar"],
                    patches=paths["patches.mpp"],
                    signer=paths["signer.jar"],
                    merged_stock=paths["merged.apk"],
                    source_provenance=paths["source.json"],
                    merge_provenance=paths["merge.json"],
                    output_directory=root / "out" / "arm-v7a",
                    version_name="9.15.51",
                    version_code="91551240",
                    keystore=paths["builder.p12"],
                    keystore_alias="morphe",
                    keystore_password="secret",
                    toolchain_provenance=paths["tools.json"],
                )
            self.assertEqual(output.name, "youtube-music-9.15.51-arm-v7a.apk")
            self.assertIn("--striplibs=armeabi-v7a", calls[0])
            provenance = json.loads(output.with_suffix(".provenance.json").read_text())
            self.assertEqual(
                provenance["package"], "app.morphe.android.apps.youtube.music"
            )
            self.assertEqual(provenance["architecture"], "arm-v7a")
            self.assertEqual(
                provenance["inputs"]["toolchain"]["morphe-patches"]["repository"],
                "j-hc/revanced-patches",
            )


def sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
