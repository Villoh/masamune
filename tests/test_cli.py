import unittest
from pathlib import Path

from masamune.cli import error_code, parser, redact
from masamune.errors import MasamuneUnavailableError


class CliTests(unittest.TestCase):
    def test_redact_removes_secrets_and_urls(self) -> None:
        value = redact("token=secret https://example.test/path?token=x")
        self.assertEqual(value, "token=<redacted>")

    def test_error_code_maps_unavailable_and_generic_errors(self) -> None:
        self.assertEqual(error_code(MasamuneUnavailableError("missing")), 2)
        self.assertEqual(error_code(RuntimeError("failed")), 1)

    def test_parser_accepts_tui_options(self) -> None:
        args = parser().parse_args(
            ["tui", "--config", "custom.toml", "--output", "build"]
        )
        self.assertEqual(args.action, "tui")
        self.assertEqual(args.config, Path("custom.toml"))
        self.assertEqual(args.output, Path("build"))
        self.assertEqual(args.keystore_alias, "masamune")
        self.assertTrue(callable(args.handler))

    def test_parser_without_command_has_no_handler(self) -> None:
        args = parser().parse_args([])
        self.assertIsNone(args.action)
        self.assertFalse(hasattr(args, "handler"))


if __name__ == "__main__":
    unittest.main()
