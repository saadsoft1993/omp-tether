"""bin/omp-memory: finding a Python, usage and exit statuses, symbolic links."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import unittest

from support import LAUNCHER, ScratchCase


class LauncherTest(ScratchCase):
    def test_no_arguments_prints_usage_and_exits_2(self):
        r = self.s.run()
        self.assertEqual(r.code, 2)
        self.assertIn("omp-memory sources", r.out)

    def test_help_and_version_exit_0(self):
        self.assertEqual(self.s.run("--help").code, 0)
        r = self.s.run("--version")
        self.assertEqual((r.code, r.out.strip()), (0, "omp-memory 0.1.0"))

    def test_unknown_command_exits_2_on_stderr(self):
        r = self.s.run("frobnicate")
        self.assertEqual(r.code, 2)
        self.assertIn("unknown command 'frobnicate'", r.err)
        self.assertEqual(r.out, "")

    def test_python_override_must_be_absolute_and_recent(self):
        r = self.s.run("--version", env=self.s.env(OMP_MEMORY_PYTHON="python3"))
        self.assertEqual(r.code, 2)
        self.assertIn("absolute path to Python 3.10 or newer", r.err)
        old = self.s.bin / "old-python"
        old.write_text("#!/bin/sh\nexit 1\n")
        old.chmod(0o755)
        r = self.s.run("--version", env=self.s.env(OMP_MEMORY_PYTHON=str(old)))
        self.assertEqual(r.code, 2)
        self.assertIn("OMP_MEMORY_PYTHON is not a usable interpreter", r.err)

    def test_python_override_is_used(self):
        r = self.s.run("--version", env=self.s.env(OMP_MEMORY_PYTHON=sys.executable))
        self.assertEqual(r.code, 0, r.err)

    def test_no_suitable_python_gives_the_install_hint(self):
        # /usr/bin/python3 is too old on macOS; /opt/homebrew or /usr/local may still hold a good one, so only check the shape
        env = self.s.env(PATH="/usr/bin:/bin")
        r = self.s.run("--version", env=env)
        if r.code == 2:
            self.assertIn("needs Python 3.10 or newer", r.err)
        else:
            self.assertEqual(r.code, 0)

    def test_a_symbolic_link_to_the_launcher_works(self):
        link = self.s.bin / "omp-memory"
        os.symlink(LAUNCHER, link)
        proc = subprocess.run([str(link), "--version"], env=self.s.env(), capture_output=True, text=True, check=False)
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, "omp-memory 0.1.0"))

    def test_the_launcher_is_executable_and_shell_only(self):
        mode = LAUNCHER.stat().st_mode
        self.assertTrue(mode & stat.S_IXUSR)
        self.assertEqual(LAUNCHER.read_text().splitlines()[0], "#!/bin/sh")

    def test_nothing_is_written_next_to_the_plugin(self):
        before = sorted(p.name for p in LAUNCHER.parent.parent.iterdir())
        self.s.run("sources")
        self.assertEqual(sorted(p.name for p in LAUNCHER.parent.parent.iterdir()), before)
        self.assertFalse(any(LAUNCHER.parent.parent.rglob("__pycache__")))


if __name__ == "__main__":
    unittest.main()
