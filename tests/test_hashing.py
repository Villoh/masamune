from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from masamune.hashing import sha256_file


class Sha256FileTests(unittest.TestCase):
    def test_hashes_large_file_in_chunks(self) -> None:
        payload = b"morphe" * (1024 * 1024)
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "artifact.apk"
            path.write_bytes(payload)
            self.assertEqual(sha256_file(path), hashlib.sha256(payload).hexdigest())


if __name__ == "__main__":
    unittest.main()
