"""/omp-services in a real omp TUI: scratch HOME, fake model provider, nothing of the real account.

The plugin is installed the way a user installs it, from this repository's marketplace catalog. The scratch HOME means
`doctor` and `setup` look at (and, after a yes, create files in) that home only; with no service declared and the
heartbeat off, setup writes no unit, so no service manager is involved. Needs omp (OMP_BIN, or on PATH); skipped
otherwise.

Run: python3 -m unittest discover -s plugins/omp-services/tests/integration -v
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "tools" / "testkit"))

import omp_scratch as kit

OMP = kit.find_omp()
ESC = b"\x1b"


@unittest.skipUnless(OMP, "omp is required (OMP_BIN)")
class SlashCommandInRealOmp(unittest.TestCase):
    scratch: kit.ScratchHome
    omp: kit.PtyOmp

    @classmethod
    def setUpClass(cls) -> None:
        cls.scratch = kit.ScratchHome()
        try:
            cls.scratch.install_plugin(ROOT, "omp-services", "omp-tether")
            cls.omp = kit.PtyOmp(cls.scratch)
            kit.wait_for(lambda: len(cls.omp.text()) > 200, timeout=90, what="the terminal UI")
            kit.wait_for(lambda: cls.omp.alive(), timeout=5, what="omp running")
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        if getattr(cls, "omp", None):
            cls.omp.stop()
        if getattr(cls, "scratch", None):
            cls.scratch.cleanup()

    @property
    def state_file(self) -> Path:
        return self.scratch.home / ".local" / "share" / "omp-services" / "state.json"

    def test_1_doctor_reports_that_nothing_is_set_up_and_changes_nothing(self):
        self.omp.type("/omp-services doctor")
        kit.wait_for(lambda: "omp-services doctor 0.1.0" in self.omp.text(), timeout=90, what="the doctor notice")
        text = self.omp.text()
        self.assertIn("has not been set up", text)
        self.assertIn("omp-services init", text)
        self.assertFalse((self.scratch.home / ".config" / "omp-services").exists())
        self.assertFalse(self.state_file.exists())

    def test_2_usage_for_anything_else(self):
        self.omp.type("/omp-services rm -rf")
        kit.wait_for(lambda: "usage: /omp-services doctor | setup" in self.omp.text(), timeout=30, what="usage")

    def test_3_setup_shows_the_plan_and_cancelling_changes_nothing(self):
        self.omp.type("/omp-services setup")
        kit.wait_for(lambda: "Apply exactly this plan?" in self.omp.text(), timeout=90, what="the confirmation")
        text = self.omp.text()
        self.assertIn("omp-services init:", text)
        self.assertIn("(mode 0700)", text)
        os.write(self.omp.fd, ESC)
        kit.wait_for(lambda: "nothing was changed" in self.omp.text(), timeout=30, what="the cancel notice")
        self.assertFalse(self.state_file.exists())
        self.assertFalse((self.scratch.home / ".config" / "omp-services").exists())

    def test_4_confirming_applies_the_plan(self):
        before = self.omp.text().count("Apply exactly this plan?")
        self.omp.type("/omp-services setup")
        kit.wait_for(
            lambda: self.omp.text().count("Apply exactly this plan?") > before, timeout=90, what="the confirmation"
        )
        os.write(self.omp.fd, b"\r")
        kit.wait_for(lambda: "setup finished" in self.omp.text(), timeout=90, what="the finished notice")
        self.assertTrue(self.state_file.is_file())
        self.assertEqual((self.scratch.home / ".config" / "omp-services").stat().st_mode & 0o777, 0o700)
        self.assertTrue((self.scratch.home / ".local" / "share" / "omp-services" / "bin" / "omp-services").is_file())
        self.assertTrue(self.omp.alive(), "the session survived all of it")


if __name__ == "__main__":
    unittest.main()
