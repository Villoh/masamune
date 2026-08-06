import unittest

from masamune.architecture import (
    Architecture,  # pyright: ignore[reportMissingImports]
)


class ArchitectureTest(unittest.TestCase):
    def test_maps_canonical_architectures_to_each_consumer(self) -> None:
        self.assertEqual(
            (
                Architecture.ARM64.value,
                Architecture.ARM64.goopdl,
                Architecture.ARM64.android_abi,
                Architecture.ARM64.module,
            ),
            ("arm64-v8a", "arm64", "arm64-v8a", "arm64"),
        )
        self.assertEqual(
            (
                Architecture.ARMV7.value,
                Architecture.ARMV7.goopdl,
                Architecture.ARMV7.android_abi,
                Architecture.ARMV7.module,
            ),
            ("arm-v7a", "armv7", "armeabi-v7a", "arm"),
        )
        self.assertIs(
            Architecture.from_goopdl("arm64"), Architecture.from_config("arm64-v8a")
        )


if __name__ == "__main__":
    unittest.main()
