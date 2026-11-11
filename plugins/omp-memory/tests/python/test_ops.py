"""`backup`, `restore-drill` and `export-hindsight`: plans without --apply, the bundled scripts with it."""

from __future__ import annotations

import contextlib
import hashlib
import io
import shutil
import sqlite3
import subprocess
import unittest
from unittest import mock

from support import ScratchCase

# The bundled scripts need bash, the sqlite3 command line and shasum or sha256sum (macOS and Linux).
SCRIPTS_RUN = shutil.which("sqlite3") and (shutil.which("shasum") or shutil.which("sha256sum"))
SKIP = "the bundled backup scripts need sqlite3 and shasum or sha256sum"


def tree_digest(root):
    """{relative path: sha256} of every file except SQLite's -shm and -wal side files, which any reader of a WAL database may create."""
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.name.endswith(("-shm", "-wal")):
            out[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


class OpsTest(ScratchCase):
    def setUp(self):
        super().setUp()
        self.db = self.s.bank("sms-1qfllt3yk702w", "work/sms", wm=(("w1", "alpha", "bank"), ("w2", "beta", "global")))
        self.s.bank("tools-2guil9dlpgfxr", "work/tools")
        self.dest = self.s.home / "Backups" / "omp-memory"

    def test_backup_without_apply_is_a_plan_and_writes_nothing(self):
        r = self.s.run("backup")
        self.assertEqual(r.code, 0, r.err)
        self.assertIn("plan only", r.out)
        self.assertIn("2 bank(s)", r.out)
        self.assertFalse(self.dest.exists())

    def test_backup_with_no_banks_says_there_is_nothing_to_back_up_instead_of_failing(self):
        """A machine where omp has written no memory yet: the stranger's first import."""
        for state in ("missing", "empty"):
            with self.subTest(state):
                banks = self.s.root / f"no-banks-{state}"
                if state == "empty":
                    banks.mkdir()
                for extra in ([], ["--apply"]):
                    r = self.s.run("backup", "--banks-dir", str(banks), *extra)
                    self.assertEqual(r.code, 0, r.err + r.out)
                    self.assertIn("nothing to back up", r.out)
                    self.assertIn("first import needs no backup", r.out)
                    self.assertNotIn("shasum", r.err + r.out)
                    self.assertNotIn("No such file", r.err + r.out)
                self.assertFalse(self.dest.exists())

    @unittest.skipUnless(SCRIPTS_RUN, SKIP)
    def test_the_backup_script_itself_handles_no_banks_without_a_raw_error(self):
        from omp_memory.runtime import OPS

        for state in ("missing", "empty"):
            with self.subTest(state):
                banks = self.s.root / f"script-no-banks-{state}" / "banks"
                if state == "empty":
                    banks.mkdir(parents=True)
                ran = subprocess.run(
                    [str(OPS / "backup_mnemopi.sh"), "--dest", str(self.dest), "--src", str(banks)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(ran.returncode, 0, ran.stderr)
                self.assertIn("nothing to back up", ran.stderr)
                self.assertNotIn("BACKUP_DIR=", ran.stdout)
                self.assertNotIn("shasum", ran.stderr)
                self.assertFalse(self.dest.exists())

    @unittest.skipUnless(SCRIPTS_RUN, SKIP)
    def test_backup_prints_its_directory_under_tilde_even_when_home_is_a_link(self):
        link = self.s.root / "home-link"
        link.symlink_to(self.s.home)
        r = self.s.run("backup", "--apply", env=self.s.env(HOME=str(link)))
        self.assertEqual(r.code, 0, r.err)
        self.assertRegex(r.out, r"BACKUP_DIR=~/Backups/omp-memory/\d{8}T\d{6}Z")
        self.assertNotIn(str(self.s.home), r.out + r.err)

    def test_backup_rejects_stray_arguments(self):
        self.assertEqual(self.s.run("backup", "now").code, 2)

    @unittest.skipUnless(SCRIPTS_RUN, SKIP)
    def test_backup_apply_copies_every_bank_with_checksums_and_leaves_the_banks_alone(self):
        before = tree_digest(self.s.banks)
        r = self.s.run("backup", "--apply")
        self.assertEqual(r.code, 0, r.err + r.out)
        [stamp] = list(self.dest.iterdir())
        self.assertEqual(
            sorted(p.name for p in stamp.iterdir()),
            ["MANIFEST.tsv", "SHA256SUMS", "sms-1qfllt3yk702w.db", "tools-2guil9dlpgfxr.db"],
        )
        copy = sqlite3.connect(stamp / "sms-1qfllt3yk702w.db")
        self.assertEqual(copy.execute("SELECT COUNT(*) FROM working_memory").fetchone()[0], 2)
        copy.close()
        self.assertEqual(tree_digest(self.s.banks), before)  # banks are only read (the check uses a mode=ro copy)

    @unittest.skipUnless(SCRIPTS_RUN, SKIP)
    def test_restore_drill_uses_the_newest_backup_and_reports_ok(self):
        self.assertEqual(self.s.run("backup", "--apply").code, 0)
        r = self.s.run("restore-drill")
        self.assertEqual(r.code, 0, r.err + r.out)
        self.assertIn("RESULT: OK", r.out)
        self.assertIn("same / same", r.out)

    @unittest.skipUnless(SCRIPTS_RUN, SKIP)
    def test_restore_drill_fails_on_a_damaged_backup(self):
        self.s.run("backup", "--apply")
        [stamp] = list(self.dest.iterdir())
        (stamp / "sms-1qfllt3yk702w.db").write_bytes(b"damaged")
        r = self.s.run("restore-drill")
        self.assertEqual(r.code, 1)
        self.assertIn("checksum mismatch", r.err)

    def test_restore_drill_without_any_backup_says_how_to_make_one(self):
        r = self.s.run("restore-drill")
        self.assertEqual(r.code, 1)  # a refusal: there is nothing to restore from
        self.assertIn("backup --apply", r.err)

    def test_export_hindsight_is_a_dry_run_unless_apply_is_given(self):
        from omp_memory import ops

        with (
            mock.patch.object(ops, "run_script", return_value=0) as run_script,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            ops.cmd_export_hindsight(["--banks", "one"], None)
            self.assertEqual(run_script.call_args.args[1], ["--banks", "one", "--dry-run"])
            ops.cmd_export_hindsight(["--banks", "one", "--apply"], None)
            self.assertEqual(run_script.call_args.args[1], ["--banks", "one"])
            ops.cmd_export_hindsight(["--dry-run"], None)
            self.assertEqual(run_script.call_args.args[1], ["--dry-run"])

    @unittest.skipUnless(SCRIPTS_RUN, SKIP)
    def test_export_hindsight_plan_runs_the_bundled_script_and_touches_nothing(self):
        out = self.s.root / "hs-out"
        r = self.s.run(
            "export-hindsight", "--banks", "one", "--out", str(out), "--overlay-dir", str(self.s.root / "ov")
        )
        self.assertEqual(r.code, 0, r.err + r.out)
        self.assertIn("plan: mode=pages banks=one", r.out)
        self.assertFalse(out.exists())

    @unittest.skipUnless(SCRIPTS_RUN, SKIP)
    def test_export_hindsight_has_no_defaults(self):
        r = self.s.run("export-hindsight")
        self.assertEqual(r.code, 2, r.err + r.out)
        self.assertIn("there is no default", r.err)


if __name__ == "__main__":
    unittest.main()
