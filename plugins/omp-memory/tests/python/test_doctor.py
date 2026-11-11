"""`doctor`: the checks, the sessions-holding-banks check against a real second process, and refusals."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from unittest import mock

from support import PLUGIN, ScratchCase

HAVE_LSOF = bool(shutil.which("lsof")) or os.access("/usr/sbin/lsof", os.X_OK)
HAVE_HOLDER_CHECK = HAVE_LSOF or sys.platform.startswith("linux")  # Linux without lsof reads /proc/<pid>/fd
HOLDER = (
    "import sqlite3, sys, time\n"
    "db = sqlite3.connect(sys.argv[1])\n"
    "db.execute('SELECT count(*) FROM working_memory').fetchone()\n"
    "print('ready', flush=True)\n"
    "time.sleep(60)\n"
)


LIB_OK = {
    "ompVersion": "18.18.0",
    "location": {
        "kind": "cache",
        "root": "/x/lib",
        "pkgDir": "/x/lib/node_modules/@oh-my-pi/pi-mnemopi",
        "version": "18.18.0",
    },
    "candidates": [],
    "setupCommand": "omp-memory setup",
    "selfTest": {"status": "passed", "runs": [], "failedStep": None, "detail": "passed (stub embedder)"},
}
LIB_MISSING = {
    "ompVersion": "18.18.3",
    "location": None,
    "candidates": [
        {
            "kind": "omp-plugins",
            "pkgDir": "/x/plugins/node_modules/@oh-my-pi/pi-mnemopi",
            "why": "it is 18.17.0, omp is 18.18.3",
        },
        {
            "kind": "cache",
            "pkgDir": "/x/cache/lib/18.18.3/node_modules/@oh-my-pi/pi-mnemopi",
            "why": "not installed there",
        },
    ],
    "setupCommand": "omp-memory setup",
}


class DoctorTest(ScratchCase):
    def wired(self, library=LIB_OK, **extra):
        return self.s.wired(**self.s.bun_stdout(json.dumps(library)), **extra)

    def checks(self, *args, env=None):
        r = self.s.run("doctor", "--json", *args, env=env or self.wired())
        return r, {c["name"]: c for c in r.json()["checks"]}

    def test_a_ready_setup(self):
        self.s.bank("sms-1qfllt3yk702w")
        r, got = self.checks()
        self.assertEqual(r.code, 0, r.out)
        self.assertEqual(got["bun"]["status"], "ok")
        self.assertEqual(got["omp"]["status"], "ok")
        self.assertEqual(got["library"]["status"], "ok")
        self.assertTrue(got["banks dir"]["detail"].endswith("1 bank(s)"))
        self.assertEqual(got["open banks"]["status"], "ok")
        self.assertEqual(got["vendored code"]["status"], "ok")
        self.assertEqual(got["groups"]["status"], "ok")

    def test_it_states_which_bun_omp_and_library_are_used(self):
        self.s.bank("sms-1qfllt3yk702w")
        _, got = self.checks()
        self.assertIn("1.4.4", got["bun"]["detail"])
        self.assertIn(str(self.s.fake_bun), got["bun"]["detail"])
        self.assertIn("OMP_MEMORY_BUN", got["bun"]["detail"])
        self.assertIn("18.18.0", got["omp"]["detail"])
        self.assertIn(str(self.s.fake_omp), got["omp"]["detail"])
        self.assertIn("@oh-my-pi/pi-mnemopi 18.18.0 = omp 18.18.0", got["library"]["detail"])
        self.assertIn("the omp-memory cache (made by setup)", got["library"]["detail"])
        self.assertIn("/x/lib/node_modules/@oh-my-pi/pi-mnemopi", got["library"]["detail"])
        self.assertIn("self-test passed (stub embedder), cached", got["library"]["detail"])

    def test_a_library_nobody_has_tested_is_not_ok_and_says_how_to_verify_it(self):
        untested = {
            **LIB_OK,
            "selfTest": {"status": "not-run", "runs": [], "failedStep": None, "detail": "not yet verified"},
        }
        r, got = self.checks(env=self.wired(untested))
        self.assertEqual(got["library"]["status"], "warn")
        self.assertIn("not yet verified", got["library"]["detail"])
        self.assertIn("doctor --deep", got["library"]["fix"])
        self.assertEqual(r.code, 0)  # a warning does not fail doctor
        text = self.s.run("doctor", env=self.wired(untested)).out
        self.assertIn("fix (library): omp-memory doctor --deep", text)

    def test_a_library_that_failed_the_self_test_is_a_failure_naming_the_step(self):
        failed = {
            **LIB_OK,
            "selfTest": {
                "status": "failed",
                "runs": [],
                "failedStep": "embedding",
                "detail": 'step "embedding": row x has no memory_embeddings row',
            },
        }
        r, got = self.checks(env=self.wired(failed))
        self.assertEqual(r.code, 1)
        self.assertEqual(got["library"]["status"], "fail")
        self.assertIn('step "embedding"', got["library"]["detail"])
        self.assertIn("Writing is refused", got["library"]["detail"])

    def test_a_library_without_a_self_test_field_is_treated_as_untested(self):
        old = {key: value for key, value in LIB_OK.items() if key != "selfTest"}
        _, got = self.checks(env=self.wired(old))
        self.assertEqual(got["library"]["status"], "warn")

    def test_no_library_at_omps_exact_version_fails_and_names_the_setup_command(self):
        self.s.bank("sms-1qfllt3yk702w")
        r, got = self.checks(env=self.wired(LIB_MISSING, FAKE_OMP_VERSION="18.18.3"))
        self.assertEqual(r.code, 1)
        self.assertEqual(got["library"]["status"], "fail")
        self.assertIn("exact version 18.18.3", got["library"]["detail"])
        self.assertIn("it is 18.17.0, omp is 18.18.3", got["library"]["detail"])
        self.assertIn("omp-memory setup", got["library"]["fix"])
        text = self.s.run("doctor", env=self.wired(LIB_MISSING, FAKE_OMP_VERSION="18.18.3")).out
        self.assertIn("fix (library): omp-memory setup", text)

    def test_the_importer_is_asked_with_omps_path_and_the_configured_library_directory(self):
        self.s.run("doctor", env=self.wired(OMP_MEMORY_LIB_DIR=str(self.s.root / "lib")))
        argv = self.s.bun_argv()
        self.assertEqual(argv[1], "library")
        self.assertEqual(argv[argv.index("--omp-bin") + 1], str(self.s.fake_omp))
        self.assertEqual(argv[argv.index("--lib-dir") + 1], str(self.s.root / "lib"))

    def test_missing_bun_and_missing_omp_are_reported_with_a_fix(self):
        env = self.s.env(OMP_MEMORY_BUN=str(self.s.root / "nope"), OMP_BIN=str(self.s.fake_omp))
        r, got = self.checks(env=env)
        self.assertEqual(r.code, 1)
        self.assertEqual(got["bun"]["status"], "fail")
        self.assertIn("fix", got["bun"])
        self.assertEqual(got["library"]["status"], "warn")
        text = self.s.run("doctor", env=env)
        self.assertIn("not ready", text.out)
        self.assertIn("fix (bun)", text.out)
        no_omp = self.s.env(
            OMP_MEMORY_BUN=str(self.s.fake_bun), OMP_BIN=str(self.s.root / "no-omp"), PATH="/usr/bin:/bin"
        )
        _, got = self.checks(env=no_omp)
        self.assertEqual(got["omp"]["status"], "warn")

    def test_safety_records_are_shown_and_a_missing_backup_is_a_warning_with_the_command(self):
        self.s.bank("sms-1qfllt3yk702w")
        _, got = self.checks()
        self.assertEqual(got["safety records"]["status"], "warn")
        self.assertIn("verified backup: none recorded", got["safety records"]["detail"])
        self.assertIn("backup --apply", got["safety records"]["fix"])

    def test_banks_directory_that_does_not_exist_is_a_warning(self):
        _, got = self.checks()
        self.assertEqual(got["banks dir"]["status"], "warn")

    @unittest.skipUnless(HAVE_HOLDER_CHECK, "neither lsof nor /proc is available")
    def test_a_bank_that_another_process_has_open_is_reported_with_its_pid(self):
        db = self.s.bank("sms-1qfllt3yk702w")
        self.s.bank("other-1abc", "work/other")
        holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(db)], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "ready")
            _, got = self.checks()
            self.assertEqual(got["open banks"]["status"], "warn")
            self.assertIn("sms-1qfllt3yk702w", got["open banks"]["detail"])
            self.assertIn(str(holder.pid), got["open banks"]["detail"])
            self.assertNotIn("other-1abc", got["open banks"]["detail"])
            self.assertIn("refuses these banks", got["open banks"]["fix"])
        finally:
            holder.kill()
            holder.wait()
            holder.stdout.close()
        time.sleep(0.1)
        _, got = self.checks()
        self.assertEqual(got["open banks"]["status"], "ok")

    @unittest.skipUnless(HAVE_HOLDER_CHECK, "neither lsof nor /proc is available")
    def test_a_holder_is_found_when_the_banks_directory_is_reached_through_a_symbolic_link(self):
        from omp_memory import util

        db = self.s.bank("sms-1qfllt3yk702w")
        link = self.s.root / "via-link"
        link.symlink_to(self.s.banks, target_is_directory=True)
        holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(db)], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "ready")
            found = util.open_holders({"x": [link / db.parent.name / "mnemopi.db"]})
            self.assertEqual(found["x"], [holder.pid])
        finally:
            holder.kill()
            holder.wait()
            holder.stdout.close()

    @unittest.skipUnless(sys.platform.startswith("linux"), "/proc is Linux only")
    def test_without_lsof_the_proc_scan_finds_the_process_that_holds_a_bank(self):
        from omp_memory import util

        db = self.s.bank("sms-1qfllt3yk702w")
        holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(db)], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "ready")
            with (
                mock.patch.object(util.shutil, "which", return_value=None),
                mock.patch.object(util.os, "access", return_value=False),
            ):
                found = util.open_holders({"x": [db, db.with_name("mnemopi.db-wal")], "y": [self.s.banks / "none"]})
            self.assertEqual(found["x"], [holder.pid])
            self.assertEqual(found["y"], [])
        finally:
            holder.kill()
            holder.wait()
            holder.stdout.close()

    def test_without_lsof_it_says_it_cannot_tell(self):
        from omp_memory import util

        self.s.bank("sms-1qfllt3yk702w")
        with (
            mock.patch.object(util.shutil, "which", return_value=None),
            mock.patch.object(util.os, "access", return_value=False),
            mock.patch.object(util, "_proc_holders", return_value=None),
        ):
            self.assertIsNone(util.open_holders({"x": [self.s.banks / "sms-1qfllt3yk702w" / "mnemopi.db"]}))

    def test_edited_vendored_files_are_detected(self):
        from omp_memory import core, doctor

        copy = self.s.root / "vendor"
        shutil.copytree(PLUGIN / "vendor", copy)
        with mock.patch.object(core, "VENDOR", copy):
            self.assertEqual(doctor.vendor_check()["status"], "ok")
            (copy / "memory_import" / "engine.ts").write_text("// changed\n")
            result = doctor.vendor_check()
        self.assertEqual(result["status"], "fail")
        self.assertIn("memory_import/engine.ts", result["detail"])

    def test_an_added_file_under_vendor_is_detected_but_bytecode_caches_are_not(self):
        from omp_memory import core, doctor

        copy = self.s.root / "vendor"
        shutil.copytree(PLUGIN / "vendor", copy)
        with mock.patch.object(core, "VENDOR", copy):
            (copy / "memory_import" / "__pycache__").mkdir()
            (copy / "memory_import" / "__pycache__" / "x.cpython-314.pyc").write_bytes(b"\0")
            self.assertEqual(doctor.vendor_check()["status"], "ok")
            (copy / "memory_import" / "extra.ts").write_text("// added\n")
            result = doctor.vendor_check()
        self.assertEqual(result["status"], "fail")
        self.assertIn("memory_import/extra.ts", result["detail"])

    def test_labels_file_problems_are_warnings(self):
        self.s.bank("sms-1qfllt3yk702w")
        labels = self.s.home / ".config" / "omp-memory" / "labels.json"
        labels.parent.mkdir(parents=True)
        labels.write_text("{not json")
        _, got = self.checks()
        self.assertEqual(got["labels file"]["status"], "warn")

    def test_text_output_has_a_verdict(self):
        self.s.bank("sms-1qfllt3yk702w")
        out = self.s.run("doctor", env=self.s.wired()).out
        self.assertIn("ready", out.splitlines()[-1])


if __name__ == "__main__":
    unittest.main()
