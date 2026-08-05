import unittest

from morphe_tui.config import parse_config


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
            {
                "apps": [
                    {"package": "com.example.app", "name": "Example"}
                ]
            }
        )
        self.assertIsNone(config.apps[0].source_dir)

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
