"""Tests for scripts/typecheck.py: it must not report success when the compiler did not really check our files."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import typecheck

ROOT = typecheck.ROOT
LISTING = "\n".join(str(ROOT / t) for t in typecheck.TARGETS) + "\n"
OTHER = str(Path("/some/omp/packages/ai/src/x.ts"))


def classify(code: int, output: str) -> tuple[bool, str]:
    return typecheck.classify(code, output, ROOT, ROOT, typecheck.TARGETS)


class Classify(unittest.TestCase):
    def test_a_clean_run_passes(self) -> None:
        self.assertTrue(classify(0, LISTING)[0])

    def test_dependency_diagnostics_alone_pass(self) -> None:
        out = LISTING + "../oh-my-pi/packages/ai/src/x.ts(3,4): error TS2307: Cannot find module './x.md'\n"
        self.assertTrue(classify(2, out)[0])

    def test_a_diagnostic_in_our_files_fails(self) -> None:
        out = LISTING + f"{typecheck.TARGETS[0]}(10,5): error TS2322: Type 'a' is not assignable to type 'b'\n"
        ok, message = classify(2, out)
        self.assertFalse(ok)
        self.assertIn("omp-session-control", message)

    def test_a_crash_with_no_diagnostics_fails(self) -> None:
        self.assertFalse(classify(2, LISTING + "internal compiler crash\n")[0])
        self.assertFalse(classify(2, "internal compiler crash\n")[0])

    def test_a_missing_executable_or_signal_status_fails(self) -> None:
        for status in (127, 126, 139, -9):
            self.assertFalse(classify(status, LISTING)[0], status)

    def test_a_configuration_error_fails(self) -> None:
        out = (
            LISTING
            + "tsconfig.json(5,5): error TS5102: Option 'baseUrl' has been removed.\nerror TS6053: File not found.\n"
        )
        self.assertFalse(classify(2, out)[0])

    def test_a_global_error_next_to_dependency_diagnostics_fails(self) -> None:
        # a compiler-level error has no "file(line,col):" prefix; it must not hide behind tolerated dependency ones
        dependency = "../oh-my-pi/packages/ai/src/x.ts(3,4): error TS2307: Cannot find module './x.md'\n"
        for line in ("error TS6053: File not found.\n", "error TS18003: No inputs were found in config file.\n"):
            ok, message = classify(2, LISTING + dependency + line)
            self.assertFalse(ok, line)
            self.assertIn("unrecognised", message)

    def test_our_files_missing_from_the_file_list_fails_even_on_exit_zero(self) -> None:
        self.assertFalse(classify(0, OTHER + "\n")[0])
        self.assertFalse(classify(0, "")[0])


class EndToEnd(unittest.TestCase):
    """The real script with a stand-in compiler."""

    def run_script(self, body: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "tsc"
            fake.write_text(f"#!/bin/sh\n{body}\n")
            fake.chmod(0o755)
            omp = Path(tmp) / "omp"
            omp.mkdir()
            env = {**os.environ, "OMP_SRC": str(omp), "TSC": str(fake), **(env_extra or {})}
            return subprocess.run(
                [sys.executable, str(HERE / "typecheck.py")], capture_output=True, text=True, env=env, timeout=60
            )

    def test_a_compiler_that_crashes_is_a_failure(self) -> None:
        r = self.run_script('echo "internal compiler crash" >&2; exit 2')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("FAILED", r.stderr)

    def test_a_compiler_that_is_not_there_is_a_failure(self) -> None:
        r = self.run_script("exit 127")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)

    def test_a_compiler_that_lists_our_files_and_exits_zero_passes(self) -> None:
        listing = " ".join(f"echo {ROOT / t};" for t in typecheck.TARGETS)
        r = self.run_script(f"{listing} exit 0")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("passed", r.stdout)


if __name__ == "__main__":
    unittest.main()
