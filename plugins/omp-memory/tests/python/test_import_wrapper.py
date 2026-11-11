"""`import`: the arguments handed to the bundled importer, and what is refused before it is started.

A stand-in `bun` records its command line and environment, so nothing here opens a bank or runs the importer.
"""

from __future__ import annotations

import os
import unittest

from support import PLUGIN, ScratchCase

IMPORTER = str(PLUGIN / "vendor" / "memory_import" / "cli.ts")


class ImportWrapperTest(ScratchCase):
    def run_import(self, *args, **extra):
        env = self.s.wired(**extra)
        return self.s.run("import", *args, env=env)

    def test_command_line_is_the_importer_with_the_configured_paths_and_nothing_else(self):
        r = self.run_import("dry-run", "--source", "claude-memory", "--only-new-banks")
        self.assertEqual(r.code, 0, r.err)
        argv = self.s.bun_argv()
        self.assertEqual(argv[:2], [IMPORTER, "dry-run"])
        self.assertEqual(argv[2:5], ["--source", "claude-memory", "--only-new-banks"])
        flags = dict(zip(argv[5::2], argv[6::2], strict=False))
        self.assertEqual(flags["--banks-dir"], str(self.s.banks))
        self.assertEqual(flags["--ledger"], str(self.s.home / ".config/omp-memory-import/ledger.json"))
        self.assertEqual(flags["--omp-bin"], str(self.s.fake_omp))
        for absent in ("--lib-dir", "--conf", "--folder-roots", "--src-dir"):
            self.assertNotIn(absent, argv)  # nothing is passed that the user has not configured
        self.assertTrue(os.path.isabs(flags["--python"]))

    def test_apply_is_never_added(self):
        for sub in ("scan", "dry-run", "verify", "undo", "doctor", "project-map"):
            extra = ["--run", "r1"] if sub == "undo" else []
            self.run_import(sub, *extra)
            self.assertNotIn("--apply", self.s.bun_argv(), sub)

    def test_apply_is_forwarded_only_when_given(self):
        self.run_import("import", "--apply", "--only-new-banks", "--no-backup", "--skip-dry-run-check")
        self.assertIn("--apply", self.s.bun_argv())
        self.run_import("undo", "--run", "imp-1", "--apply")
        self.assertIn("--apply", self.s.bun_argv())
        self.run_import("undo", "--run", "imp-1")
        self.assertNotIn("--apply", self.s.bun_argv())

    def test_import_without_apply_is_refused_before_the_importer_starts(self):
        self.s.bun_log.write_text("untouched")
        r = self.run_import("import", "--source", "claude-memory")
        self.assertEqual(r.code, 2)
        self.assertIn("pass --apply", r.err)
        self.assertEqual(self.s.bun_log.read_text(), "untouched")

    def test_the_documented_flags_pass_through_unchanged_and_in_order(self):
        self.run_import(
            "import",
            "--apply",
            "--only-new-banks",
            "--banks",
            "a-1,b-2",
            "--release-held",
            "k1,k2",
            "--out",
            "/tmp/r.json",
            "--no-backup",
            "--skip-dry-run-check",
        )
        argv = self.s.bun_argv()
        start = argv.index("--apply")
        self.assertEqual(
            argv[start : start + 8],
            ["--apply", "--only-new-banks", "--banks", "a-1,b-2", "--release-held", "k1,k2", "--out", "/tmp/r.json"],
        )
        # the two safety escapes belong to omp-memory: the importer never sees them
        self.assertNotIn("--no-backup", argv)
        self.assertNotIn("--skip-dry-run-check", argv)

    def test_a_flag_given_on_the_command_line_wins_over_the_configuration(self):
        other = self.s.root / "other-banks"
        self.run_import("scan", "--banks-dir", str(other), "--ledger", "/tmp/ledger-x.json")
        argv = self.s.bun_argv()
        self.assertEqual(argv.count("--banks-dir"), 1)
        self.assertEqual(argv[argv.index("--banks-dir") + 1], str(other))
        self.assertEqual(argv[argv.index("--ledger") + 1], "/tmp/ledger-x.json")

    def test_the_equals_form_is_understood_too(self):
        self.run_import("scan", f"--banks-dir={self.s.root}/eq-banks")
        argv = self.s.bun_argv()
        self.assertEqual(argv[argv.index("--banks-dir") + 1], f"{self.s.root}/eq-banks")

    def test_bun_be_bun_is_removed_from_the_importer_environment(self):
        self.run_import("scan", BUN_BE_BUN="1")
        self.assertNotIn("BUN_BE_BUN", self.s.bun_env())

    def test_exit_status_of_the_importer_is_the_exit_status_of_the_command(self):
        self.assertEqual(self.run_import("verify", FAKE_BUN_EXIT="1").code, 1)
        self.assertEqual(self.run_import("verify", FAKE_BUN_EXIT="3").code, 3)

    def test_force_closed_is_passed_on_with_a_warning(self):
        r = self.run_import("import", "--apply", "--force-closed", "--no-backup", "--skip-dry-run-check")
        self.assertIn("--force-closed", self.s.bun_argv())
        self.assertIn("sessions", r.err)

    def test_apply_announces_which_banks_will_be_written(self):
        r = self.run_import("import", "--apply", "--no-backup", "--skip-dry-run-check")
        self.assertIn("will write to the banks in ~/.omp/agent/memories/mnemopi/banks", r.err)

    def test_unknown_subcommand_and_missing_subcommand(self):
        self.assertEqual(self.run_import("frobnicate").code, 2)
        r = self.run_import()
        self.assertEqual(r.code, 2)
        self.assertIn("usage: omp-memory import", r.out)
        self.assertEqual(self.run_import("--help").code, 0)
        self.assertEqual(self.run_import("scan", "--help").code, 0)

    def test_a_configured_library_directory_is_passed_as_lib_dir_and_a_flag_wins(self):
        self.run_import("scan", OMP_MEMORY_LIB_DIR=str(self.s.root / "lib"))
        argv = self.s.bun_argv()
        self.assertEqual(argv[argv.index("--lib-dir") + 1], str(self.s.root / "lib"))
        self.run_import("scan", "--lib-dir", str(self.s.root / "other"), OMP_MEMORY_LIB_DIR=str(self.s.root / "lib"))
        argv = self.s.bun_argv()
        self.assertEqual(argv.count("--lib-dir"), 1)
        self.assertEqual(argv[argv.index("--lib-dir") + 1], str(self.s.root / "other"))

    def test_no_omp_source_tree_setting_exists_any_more(self):
        r = self.s.run("config", "set", "ompSrcDir", str(self.s.root))
        self.assertEqual(r.code, 2)
        self.assertIn("unknown setting", r.err)
        self.assertNotIn("--src-dir", self.s.run("import", "--help").out)
        self.run_import("scan", "--src-dir", "/tmp/x")  # the importer, not the wrapper, refuses an unknown flag
        self.assertIn("--src-dir", self.s.bun_argv())

    def test_the_importer_is_told_where_the_setup_command_lives(self):
        self.run_import("scan")
        self.assertEqual(self.s.bun_env()["OMP_MEMORY_SETUP_COMMAND"], "omp-memory setup")

    def test_a_banks_conf_the_user_set_and_the_roots_of_their_groups_are_passed_on(self):
        conf = self.s.root / "banks.conf"
        conf.write_text("shop work/shop\n")
        self.run_import("scan", OMP_MEMORY_BANKS_CONF=str(conf))
        argv = self.s.bun_argv()
        self.assertEqual(argv[argv.index("--conf") + 1], str(conf))
        self.assertEqual(self.s.run("config", "set-group", "Shop", "work/shop", "~/other", "/abs/x").code, 0)
        self.run_import("scan")
        argv = self.s.bun_argv()
        roots = argv[argv.index("--folder-roots") + 1].split(",")
        self.assertEqual(roots, sorted(["/abs/x", str(self.s.home / "other"), str(self.s.home / "work/shop")]))
        self.assertNotIn("--conf", argv)  # unset, so not passed

    def test_project_map_does_not_need_the_library(self):
        env = self.s.env(OMP_MEMORY_BUN=str(self.s.fake_bun), OMP_BIN=str(self.s.fake_omp))
        r = self.s.run("import", "project-map", "--hindsight-export", "/tmp/x.json", env=env)
        self.assertEqual(r.code, 0, r.err)
        self.assertNotIn("--lib-dir", self.s.bun_argv())

    def test_an_old_bun_is_refused_with_what_was_found(self):
        r = self.s.run("import", "scan", env=self.s.wired(FAKE_BUN_VERSION="1.3.5"))
        self.assertEqual(r.code, 2)
        self.assertIn("no Bun 1.4.4 or newer", r.err)
        self.assertIn("1.3.5", r.err)

    def test_a_configured_bun_is_never_replaced_by_another(self):
        r = self.s.run("import", "scan", env=self.s.wired(OMP_MEMORY_BUN=str(self.s.root / "missing-bun")))
        self.assertEqual(r.code, 2)
        self.assertIn("no Bun", r.err)

    def test_python_for_the_redaction_bridge_is_this_interpreter_unless_given(self):
        self.run_import("scan")
        argv = self.s.bun_argv()
        self.assertTrue(argv[argv.index("--python") + 1].startswith("/"))
        self.run_import("scan", "--python", "/opt/x/python3")
        argv = self.s.bun_argv()
        self.assertEqual(argv[argv.index("--python") + 1], "/opt/x/python3")
        self.assertEqual(argv.count("--python"), 1)


if __name__ == "__main__":
    unittest.main()
