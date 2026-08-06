import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from email.parser import BytesParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class WheelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        uv = shutil.which("uv")
        if uv is None:
            raise unittest.SkipTest("uv is required for wheel integration tests")
        try:
            import setuptools  # noqa: F401
        except ModuleNotFoundError:
            raise unittest.SkipTest("setuptools is required for wheel integration tests")
        cls.temporary = tempfile.TemporaryDirectory()
        temporary = Path(cls.temporary.name)
        source = temporary / "source"
        shutil.copytree(
            ROOT,
            source,
            ignore=shutil.ignore_patterns(
                ".git", ".venv", ".pi-subagents", "build", "*.egg-info", "__pycache__"
            ),
        )
        dist = temporary / "dist"
        cache = temporary / "empty-uv-cache"
        build = subprocess.run(
            [
                uv,
                "build",
                "--wheel",
                "--no-build-isolation",
                "--python",
                sys.executable,
                "--out-dir",
                str(dist),
            ],
            cwd=source,
            env={**os.environ, "UV_CACHE_DIR": str(cache), "UV_OFFLINE": "1"},
            capture_output=True,
            text=True,
            check=False,
        )
        if build.returncode:
            raise AssertionError(build.stdout + build.stderr)
        cls.wheel = next(dist.glob("masamune-*.whl"))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_wheel_contains_tui_module_and_stylesheet(self) -> None:
        with zipfile.ZipFile(self.wheel) as archive:
            self.assertTrue(
                {
                    "masamune/config_editor.py",
                    "masamune/logo.py",
                    "masamune/tui/__init__.py",
                    "masamune/tui/app.py",
                    "masamune/tui/helpers.py",
                    "masamune/tui/models.py",
                    "masamune/tui/screens.py",
                    "masamune/tui/tui.tcss",
                    "masamune/tui/widgets.py",
                    "masamune/tui/workers.py",
                }
                <= set(archive.namelist())
            )

    def test_wheel_marks_tui_dependencies_as_optional(self) -> None:
        with zipfile.ZipFile(self.wheel) as archive:
            metadata_name = next(
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/METADATA")
            )
            metadata = BytesParser().parsebytes(archive.read(metadata_name))
        tui_requirements = [
            requirement
            for requirement in metadata.get_all("Requires-Dist", [])
            if requirement.partition(";")[0]
            .strip()
            .lower()
            .startswith(("textual", "tomlkit"))
        ]
        self.assertEqual(len(tui_requirements), 2)
        self.assertTrue(
            all(
                re.search(r"\bextra\s*==\s*['\"]tui['\"]", requirement)
                for requirement in tui_requirements
            ),
            tui_requirements,
        )

    def test_base_wheel_keeps_cli_available_and_tui_optional(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = Path(directory) / "venv"
            setup = subprocess.run(
                [sys.executable, "-m", "venv", str(environment)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(setup.returncode, 0, setup.stdout + setup.stderr)
            python = environment / (
                "Scripts/python.exe" if os.name == "nt" else "bin/python"
            )
            install = subprocess.run(
                [
                    python,
                    "-m",
                    "pip",
                    "install",
                    "--no-deps",
                    "--no-index",
                    "--no-input",
                    str(self.wheel),
                ],
                env={
                    **os.environ,
                    "PIP_NO_INDEX": "1",
                    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(install.returncode, 0, install.stdout + install.stderr)
            help_result = subprocess.run(
                [python, "-m", "masamune.cli", "--help"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                help_result.returncode, 0, help_result.stdout + help_result.stderr
            )
            self.assertIn("tui", help_result.stdout)
            tui_result = subprocess.run(
                [python, "-m", "masamune.cli", "tui"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                tui_result.returncode, 2, tui_result.stdout + tui_result.stderr
            )
            self.assertIn("morphe-builder[tui]", tui_result.stderr)


if __name__ == "__main__":
    unittest.main()
