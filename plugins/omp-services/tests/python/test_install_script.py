"""install.sh: finds omp and Python, installs the plugin only after a yes, then hands over to `omp-services init`."""

from __future__ import annotations

import os
import pty
import select
import subprocess
import time
import unittest

import support

SCRIPT = support.PLUGIN / "install.sh"


class InstallScriptTests(support.Case):
    def run_script(self, extra_env=None):
        return subprocess.run(
            ["/bin/sh", str(SCRIPT)],
            env={**self.s.env, **(extra_env or {})},
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            timeout=120,
        )

    def test_missing_omp_stops_with_a_message(self):
        done = self.run_script({"OMP_BIN": "/nonexistent/omp", "PATH": "/usr/bin:/bin", "HOME": str(self.s.home)})
        self.assertEqual(done.returncode, 2)
        self.assertIn("omp was not found", done.stderr)

    def test_not_installed_and_no_terminal_changes_nothing(self):
        self.s.omp_will_install("omp-services", "0.1.0", support.PLUGIN, "omp-tether")
        done = self.run_script()
        self.assertEqual(done.returncode, 3, done.stdout + done.stderr)
        self.assertIn("omp plugin install omp-services@omp-tether", done.stdout)
        self.assertFalse(any(c.startswith("plugin install") for c in self.s.omp_calls()))
        self.assertFalse(self.s.config_dir.exists())

    def test_installed_plugin_hands_over_to_init_which_refuses_without_a_terminal(self):
        self.s.omp_install("omp-services", "0.1.0", tree=support.PLUGIN)
        done = self.run_script()
        self.assertEqual(done.returncode, 3, done.stdout + done.stderr)
        self.assertIn("omp-services init", done.stdout)
        self.assertIn("not confirmed", done.stdout)
        self.assertFalse(self.s.data_dir.exists())

    def converse(self, answers, extra_env=None):
        """Run the script on a pseudo-terminal and answer its questions in order."""
        pid, fd = pty.fork()
        if pid == 0:
            os.execve("/bin/sh", ["/bin/sh", str(SCRIPT)], {**self.s.env, **(extra_env or {})})
        queue = list(answers)
        seen = b""
        deadline = time.time() + 90
        while time.time() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.5)
            if ready:
                try:
                    data = os.read(fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                seen += data
                if seen.rstrip().endswith(b"[y/N]") and queue:
                    os.write(fd, queue.pop(0).encode() + b"\n")
                    seen += b"\n"
        _, status = os.waitpid(pid, 0)
        return os.waitstatus_to_exitcode(status), seen.decode(errors="replace")

    def test_a_person_confirming_installs_the_plugin_and_runs_init(self):
        self.s.omp_will_install("omp-services", "0.1.0", support.PLUGIN, "omp-tether")
        rc, text = self.converse(["y", "y"])
        self.assertEqual(rc, 0, text)
        self.assertIn("plugin install omp-services@omp-tether", " ".join(self.s.omp_calls()))
        self.assertTrue((self.s.data_dir / "state.json").is_file(), text)

    def test_saying_no_changes_nothing(self):
        self.s.omp_will_install("omp-services", "0.1.0", support.PLUGIN, "omp-tether")
        rc, text = self.converse(["n"])
        self.assertEqual(rc, 3, text)
        self.assertFalse(any(c.startswith("plugin install") for c in self.s.omp_calls()))


if __name__ == "__main__":
    unittest.main()
