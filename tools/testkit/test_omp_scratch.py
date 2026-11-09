"""The test kit must not hang when a helper process misbehaves."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


class RelayStart(unittest.TestCase):
    def test_a_relay_that_prints_nothing_fails_within_the_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "bun"
            fake.write_text("#!/bin/sh\nsleep 300\n")
            fake.chmod(0o755)
            code = (
                f"import sys; sys.path.insert(0, {str(HERE)!r}); import omp_scratch as k; k.RELAY_START_TIMEOUT = 2\n"
                f"try:\n    k.ScratchRelay({str(fake)!r})\nexcept RuntimeError as e:\n    print('raised', e)\n"
            )
            try:
                r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20)
            except subprocess.TimeoutExpired:
                self.fail("ScratchRelay hung on a relay that never printed a line")
            self.assertIn("raised", r.stdout, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
