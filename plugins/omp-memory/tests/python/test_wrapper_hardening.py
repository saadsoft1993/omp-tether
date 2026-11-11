"""The wrapper hands work to Bun and to shell scripts: where they run, which paths they get, which flags are refused.

A stand-in `bun` records its command line and its working directory (see support.py); nothing real runs here.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest

import support
from omp_memory import info, util
from support import PLUGIN, ScratchCase

IMPORTER_DIR = (PLUGIN / "vendor" / "memory_import").resolve()
REPORT = {
    "command": "dry-run",
    "source": "claude-memory",
    "planDigest": "f" * 64,
    "counts": {"found": 0, "wouldImport": 0},
    "perBank": [],
}


class HostileDirectoryTest(ScratchCase):
    """Bun reads `bunfig.toml` and `.env` from the directory it starts in, so it must never start in the caller's."""

    def setUp(self):
        super().setUp()
        self.env = self.s.wired(**self.s.bun_stdout(json.dumps(REPORT)))
        self.hostile = self.s.root / "cloned-repo"
        self.hostile.mkdir()
        (self.hostile / "bunfig.toml").write_text('preload = ["./evil.ts"]\n')
        (self.hostile / ".env").write_text("OMP_MEMORY_LIB_DIR=/tmp/evil\n")

    def test_the_importer_starts_in_the_plugin_not_in_the_callers_directory(self):
        for args in (
            ("import", "dry-run"),
            ("import", "scan"),
            ("import", "verify"),
            ("import", "doctor"),
            ("setup", "--dry-run"),
        ):
            with self.subTest(args=args):
                r = self.s.run(*args, env=self.env, cwd=self.hostile)
                self.assertEqual(r.code, 0, r.err)
                self.assertEqual(self.s.bun_cwd(), str(IMPORTER_DIR))

    def test_the_apply_time_plan_and_the_apply_start_there_too(self):
        self.s.bank("alpha-1abc", "work/alpha")
        self.assertEqual(self.s.run("import", "dry-run", env=self.env, cwd=self.hostile).code, 0)
        r = self.s.run("import", "import", "--apply", "--no-backup", env=self.env, cwd=self.hostile)
        self.assertEqual(r.code, 0, r.err)
        self.assertEqual(self.s.bun_cwd(), str(IMPORTER_DIR))

    def test_doctor_asks_the_importer_from_the_plugin_directory_too(self):
        self.s.run("doctor", "--deep", env=self.env, cwd=self.hostile)
        if self.s.bun_calls():
            self.assertEqual(self.s.bun_cwd(), str(IMPORTER_DIR))


class RelativePathsTest(ScratchCase):
    """A path the user gives is relative to where they are, not to the directory the importer runs in."""

    def setUp(self):
        super().setUp()
        self.env = self.s.wired(**self.s.bun_stdout(json.dumps(REPORT)))
        self.here = (self.s.root / "work-here").resolve()
        self.here.mkdir()
        (self.here / "map.json").write_text("{}")
        (self.here / "keys.txt").write_text("~/a#b\n")

    def argv_after(self, flag):
        argv = self.s.bun_argv()
        joined = [a for a in argv if a.startswith(f"{flag}=")]
        return joined[0].partition("=")[2] if joined else argv[argv.index(flag) + 1]

    def test_importer_path_flags_are_made_absolute(self):
        for flag, name in (
            ("--out", "plan.json"),
            ("--project-map", "map.json"),
            ("--global-allowlist", "map.json"),
            ("--hindsight-export", "map.json"),
            ("--conf", "map.json"),
        ):
            with self.subTest(flag=flag):
                r = self.s.run("import", "dry-run", flag, name, env=self.env, cwd=self.here)
                self.assertEqual(r.code, 0, r.err)
                self.assertEqual(self.argv_after(flag), str(self.here / name))
        r = self.s.run("import", "dry-run", f"--out={'plan2.json'}", env=self.env, cwd=self.here)
        self.assertEqual(self.argv_after("--out"), str(self.here / "plan2.json"))

    def test_restore_drill_and_export_get_absolute_paths(self):
        (self.here / "backup").mkdir()
        r = self.s.run("restore-drill", "--backup", "backup", env=self.env, cwd=self.here)
        self.assertIn(str(self.here / "backup"), r.err + r.out)
        r = self.s.run("export-hindsight", "--out", "exports", "--banks", "x", env=self.env, cwd=self.here)
        self.assertFalse((PLUGIN / "vendor" / "exports").exists())


class FlagShapeTest(ScratchCase):
    def setUp(self):
        super().setUp()
        self.env = self.s.wired(**self.s.bun_stdout(json.dumps(REPORT)))

    def test_a_boolean_flag_with_a_value_is_refused_before_anything_runs(self):
        for args in (
            ("import", "undo", "--run", "r1", "--apply=false"),
            ("import", "import", "--apply=false"),
            ("import", "dry-run", "--force-closed=false"),
            ("import", "dry-run", "--only-new-banks=0"),
        ):
            with self.subTest(args=args):
                self.s.forget_bun_calls()
                r = self.s.run(*args, env=self.env)
                self.assertEqual(r.code, 2, r.err)
                self.assertIn("takes no value", r.err)
                self.assertEqual(self.s.bun_calls(), [])


@unittest.skipUnless(shutil.which("sqlite3") and shutil.which("bash"), "the smoke script needs bash and sqlite3")
class SmokeIsolationTest(ScratchCase):
    """scripts/smoke.sh runs in a throwaway HOME; a destination the developer's shell exports must not leak into it."""

    def test_destinations_exported_by_the_callers_shell_are_not_used(self):
        real = self.s.root / "real-looking"
        env = {k: v for k, v in os.environ.items() if not k.startswith(("OMP_", "PI_"))}
        env.update(
            OMP_MEMORY_BANKS_DIR=str(real / "banks"),
            OMP_MEMORY_LEDGER=str(real / "ledger.json"),
            OMP_MEMORY_STATE_FILE=str(real / "state.json"),
            OMP_MEMORY_LABELS_FILE=str(real / "labels.json"),
            OMP_MEMORY_BANKS_CONF=str(real / "banks.conf"),
            OMP_MEMORY_BACKUPS_DIR=str(real / "backups"),
            PI_CODING_AGENT_DIR=str(real / "agent"),
            HOME=str(self.s.home),
            TMPDIR=str(self.s.root),
        )
        ran = subprocess.run(
            [str(PLUGIN / "scripts" / "smoke.sh"), "--bun", str(self.s.fake_bun), "--isolation-check"],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertIn("isolation ok", ran.stdout)
        self.assertFalse(real.exists(), "something was created at a destination the caller's shell exported")


class ReadOnlyAdapterTest(unittest.TestCase):
    """The vendored bank adapter can write (it comes from a service that edits memory); this command line never calls that."""

    def test_nothing_in_the_command_line_calls_a_writing_method_of_the_bank_adapter(self):
        import ast
        import inspect
        import re

        from omp_hub.memory.mnemopi import MnemopiAdapter

        mutating = {
            name
            for name, function in inspect.getmembers(MnemopiAdapter, inspect.isfunction)
            if re.search(r"\b(INSERT|UPDATE|DELETE)\b", inspect.getsource(function))
        }
        self.assertTrue({"write", "delete"} <= mutating, mutating)  # the test would be empty otherwise
        found = []
        for path in sorted((PLUGIN / "python" / "omp_memory").glob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in mutating:
                    receiver = ast.unparse(node.func.value)
                    if "adapter" in receiver or "probe" in receiver:
                        found.append(f"{path.name}:{node.lineno} {receiver}.{node.func.attr}")
        self.assertEqual(found, [])


class DisplayTest(unittest.TestCase):
    def test_terminal_control_characters_in_a_path_or_a_cell_are_not_printed(self):
        evil = "/home/x/work/\x1b]0;owned\x07dir\x9b2J"
        for text in (util.tilde(evil) or "", info.table([[evil, "a"]], ["FOLDER", "B"])):
            self.assertNotRegex(text, r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
            self.assertIn("work/", text)


if __name__ == "__main__":
    unittest.main()


class WrapperNormalisationTest(ScratchCase):
    """What the wrapper does to the arguments before the importer sees them must not undo the importer's own rules."""

    def setUp(self):
        super().setUp()
        self.env = self.s.wired(**self.s.bun_stdout(json.dumps(REPORT)))
        self.here = (self.s.root / "work-here").resolve()
        self.here.mkdir()
        (self.here / "keys.txt").write_text("k\n")

    def run_dry(self, *flags):
        return self.s.run("import", "dry-run", *flags, env=self.env, cwd=self.here)

    def test_a_wrapper_consumed_flag_given_twice_is_refused(self):
        for flag in ("--banks-dir", "--ledger", "--lib-dir", "--omp-bin", "--python"):
            with self.subTest(flag=flag):
                self.s.forget_bun_calls()
                r = self.run_dry(flag, "/tmp/a", flag, "/tmp/b")
                self.assertEqual(r.code, 2, r.err)
                self.assertIn("more than once", r.err)
                self.assertEqual(self.s.bun_calls(), [])

    def test_folder_roots_are_made_absolute_like_the_other_paths(self):
        (self.here / "projects").mkdir()
        self.assertEqual(self.run_dry("--folder-roots", "projects,/abs/root").code, 0)
        argv = self.s.bun_argv()
        self.assertEqual(argv[argv.index("--folder-roots") + 1], f"{self.here}/projects,/abs/root")

    def test_release_held_names_a_file_only_in_a_written_path_form(self):
        self.assertEqual(
            self.run_dry("--release-held", "keys.txt").code, 0
        )  # a bare name is a key, whatever is on disk
        argv = self.s.bun_argv()
        self.assertEqual(argv[argv.index("--release-held") + 1], "keys.txt")
        self.assertEqual(self.run_dry("--release-held", "./keys.txt").code, 0)
        argv = self.s.bun_argv()
        self.assertEqual(argv[argv.index("--release-held") + 1], str(self.here / "keys.txt"))

    def test_a_dotdot_component_is_refused_before_it_is_normalised_away(self):
        for flag in ("--out", "--project-map", "--conf"):
            with self.subTest(flag=flag):
                self.s.forget_bun_calls()
                args = (flag, "sub/../x.json")
                r = self.run_dry(*args)
                self.assertEqual(r.code, 2, r.err)
                self.assertIn("'..'", r.err)
                self.assertEqual(self.s.bun_calls(), [])


class OpenHoldersTest(unittest.TestCase):
    def test_an_lsof_that_exits_1_with_an_error_and_no_answer_is_not_nobody(self):
        import subprocess
        from pathlib import Path
        from unittest import mock

        bank = Path("/tmp/definitely-not-there/mnemopi.db")
        done = subprocess.CompletedProcess([], 1, stdout="", stderr="lsof: WARNING: can't stat() fuse file system\n")
        with (
            mock.patch.object(util.Path, "exists", return_value=True),
            mock.patch.object(util.shutil, "which", return_value="/usr/sbin/lsof"),
            mock.patch.object(util.subprocess, "run", return_value=done),
        ):
            self.assertIsNone(util.open_holders({"b": [bank]}))
            done.stderr = ""
            self.assertEqual(util.open_holders({"b": [bank]}), {"b": []})


class InterruptTest(ScratchCase):
    def test_ctrl_c_asks_the_importer_to_stop_before_anything_stronger(self):
        import signal
        import subprocess
        import time

        script = self.s.root / "slow-bun"
        marker = self.s.root / "cleaned-up"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import signal, sys, time, pathlib\n"
            "if sys.argv[1:] == ['--version']:\n    print('1.4.4'); sys.exit(0)\n"
            f"signal.signal(signal.SIGTERM, lambda *a: (pathlib.Path({str(marker)!r}).write_text('term'), sys.exit(143)))\n"
            "print('started', flush=True)\n"
            "time.sleep(60)\n"
        )
        script.chmod(0o755)
        env = {**self.s.wired(), "OMP_MEMORY_BUN": str(script)}
        proc = subprocess.Popen(
            [str(support.LAUNCHER), "import", "dry-run"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(proc.stdout.readline().strip(), "started")
        proc.send_signal(signal.SIGINT)  # the wrapper alone gets it (no process group), as under a debugger
        try:
            code = proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            self.fail("the wrapper did not stop")
        time.sleep(0.2)
        self.assertEqual(code, 130)
        self.assertEqual(marker.read_text(), "term")
