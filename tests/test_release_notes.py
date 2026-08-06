import unittest

from masamune.release_notes import ReleaseError, render_release_notes


class ReleaseNotesTest(unittest.TestCase):
    def test_renders_without_release_packaging(self) -> None:
        body = render_release_notes(
            {
                "summary": "Verified.",
                "jobs": [
                    {
                        "package": "app.example",
                        "name": "Example|App",
                        "version_name": "1.0",
                        "architecture": "arm64-v8a",
                        "provider": "local",
                        "artifacts": ["example.apk"],
                        "selected_patches": ["Patch`name"],
                        "root_patches": [],
                    }
                ],
            },
            repository="owner/repo",
            tag="v1",
        )
        self.assertIn("| Example\\|App | 1.0 | arm64-v8a | Local APKs |", body)
        self.assertIn("- `Patch\\`name`", body)
        self.assertIn(
            "https://github.com/owner/repo/releases/download/v1/example.apk", body
        )

    def test_invalid_summary_preserves_release_error(self) -> None:
        with self.assertRaisesRegex(ReleaseError, "invalid build summary"):
            render_release_notes({"jobs": "invalid"})


if __name__ == "__main__":
    unittest.main()
