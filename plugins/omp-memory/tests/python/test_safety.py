"""`import --apply` refuses without a recent verified backup and a recent dry run: every refusal path and the success path.

A stand-in `bun` stands for the importer (it records its command line and can print a dry-run report); the backup is
made by the real bundled backup script on scratch banks, so the verification of a real backup is exercised.
"""

from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path

from omp_memory import safety
from omp_memory.util import CliError
from support import PLUGIN, ScratchCase

HAVE_BACKUP_TOOLS = bool(shutil.which("sqlite3")) and bool(shutil.which("shasum") or shutil.which("sha256sum"))
NEEDS_TOOLS = "the bundled backup script needs sqlite3 and shasum or sha256sum"
REPORT = {
    "command": "dry-run",
    "source": "claude-memory",
    "planDigest": "e" * 64,
    "counts": {"found": 5, "imported": 0, "wouldImport": 3},
    "perBank": [
        {"bank": "sms-1qfllt3yk702w", "wouldImport": 2, "imported": 0},
        {"bank": "brand-new-1abc", "wouldImport": 1, "imported": 0},
        {"bank": "tools-2guil9dlpgfxr", "wouldImport": 0, "imported": 0},
    ],
}
SOURCE = ("--source", "claude-memory")


class SafetyCase(ScratchCase):
    def setUp(self):
        super().setUp()
        self.s.bank("sms-1qfllt3yk702w", "work/sms")
        self.s.bank("tools-2guil9dlpgfxr", "work/tools")
        self.env = self.s.wired(**self.s.bun_stdout(json.dumps(REPORT)))

    def state(self):
        return json.loads(self.s.state_file.read_text())

    def write_state(self, state):
        self.s.state_file.write_text(json.dumps(state))
        self.s.state_file.chmod(0o600)

    def dry_run(self, *flags, code=0):
        r = self.s.run("import", "dry-run", *flags, env=self.env)
        self.assertEqual(r.code, code, r.err)
        return r

    def backup(self):
        r = self.s.run("backup", "--apply", env=self.env)
        self.assertEqual(r.code, 0, r.err + r.out)
        return r

    def apply(self, *flags):
        self.s.forget_bun_calls()
        return self.s.run("import", "import", "--apply", *flags, env=self.env)

    def assert_importer_did_not_write(self):
        """The importer must not start to write: at most it was asked to plan (its dry run) to see which banks it reaches."""
        self.assertFalse(any("--apply" in call for call in self.s.bun_calls()), self.s.bun_calls())

    def refused(self, r, *fragments):
        self.assertEqual(r.code, 1, r.err + r.out)
        self.assert_importer_did_not_write()
        for fragment in fragments:
            self.assertIn(fragment, r.err)

    def age(self, kind, seconds):
        state = self.state()
        for entry in state[kind]:
            entry["at"] -= seconds
        self.write_state(state)


class DryRunRecordTest(SafetyCase):
    def test_a_successful_dry_run_is_recorded_with_the_banks_it_would_write(self):
        r = self.dry_run(*SOURCE)
        self.assertIn('"wouldImport": 2', r.out)  # the report still reaches the user's stdout, unchanged
        [entry] = self.state()["dryRuns"]
        self.assertEqual(entry["source"], "claude-memory")
        self.assertEqual(entry["targetBanks"], ["brand-new-1abc", "sms-1qfllt3yk702w"])
        self.assertEqual(entry["wouldImport"], 3)  # the plan's number (3), not the count of items found (5)
        self.assertIn("dry run recorded", r.err)
        self.assertEqual(oct(self.s.state_file.stat().st_mode & 0o777), "0o600")

    def test_without_the_importers_own_number_the_banks_are_summed_not_the_items_found(self):
        old = {key: value for key, value in REPORT.items() if key != "counts"}
        old["counts"] = {"found": 9, "imported": 0}
        safety.record_dry_run(self.s.state_file, "x", old)
        self.assertEqual(self.state()["dryRuns"][-1]["wouldImport"], 3)  # 2 + 1 + 0 over the banks

    def test_a_failed_or_unreadable_dry_run_is_not_recorded(self):
        failing = self.s.wired(**self.s.bun_stdout(json.dumps(REPORT)), FAKE_BUN_EXIT="1")
        self.assertEqual(self.s.run("import", "dry-run", *SOURCE, env=failing).code, 1)
        garbled = self.s.wired(**self.s.bun_stdout("not json"))
        r = self.s.run("import", "dry-run", *SOURCE, env=garbled)
        self.assertEqual(r.code, 0)
        self.assertIn("could not be read", r.err)
        self.assertFalse(self.s.state_file.exists())

    def test_the_fingerprint_ignores_apply_out_and_run_id_and_the_order_of_banks_but_not_the_targets(self):
        base = ["--source", "claude-memory", "--banks-dir", "/b", "--ledger", "/l"]
        same = safety.fingerprint([*base, "--banks", "b,a"])
        self.assertEqual(
            same, safety.fingerprint(["--banks", "a,b", *base, "--out", "/x.json", "--run-id", "r1", "--apply"])
        )
        self.assertEqual(
            safety.fingerprint(["--banks-dir", "/b", "--ledger", "/l"]), safety.fingerprint(base)
        )  # default source
        for other in (
            [*base, "--only-new-banks"],
            [*base, "--banks", "a"],
            ["--source", "hindsight", "--banks-dir", "/b", "--ledger", "/l"],
            ["--source", "claude-memory", "--banks-dir", "/other", "--ledger", "/l"],
            [*base, "--release-held", "k1"],
        ):
            self.assertNotEqual(safety.fingerprint(other), safety.fingerprint(base), other)


@unittest.skipUnless(HAVE_BACKUP_TOOLS, NEEDS_TOOLS)
class BackupRecordTest(SafetyCase):
    def test_backup_apply_verifies_the_copies_and_records_them(self):
        r = self.backup()
        self.assertIn("backup verified (2 database(s)", r.err)
        [entry] = self.state()["backups"]
        self.assertEqual(entry["banksDir"], str(self.s.banks))
        self.assertEqual(sorted(entry["files"]), ["sms-1qfllt3yk702w.db", "tools-2guil9dlpgfxr.db"])
        self.assertTrue(all(len(digest) == 64 for digest in entry["files"].values()))
        self.assertTrue((self.s.home / "Backups" / "omp-memory").is_dir())

    def test_a_backup_plan_records_nothing(self):
        self.assertEqual(self.s.run("backup").code, 0)
        self.assertFalse(self.s.state_file.exists())

    def test_a_backup_whose_copy_does_not_match_its_checksum_is_not_recorded(self):
        self.backup()
        [entry] = self.state()["backups"]
        directory = Path(entry["dir"])
        copy = directory / "sms-1qfllt3yk702w.db"
        copy.write_bytes(copy.read_bytes() + b"x")
        _, problems = safety.verify_backup_dir(directory)
        self.assertEqual(problems, ["sms-1qfllt3yk702w.db does not match its checksum"])
        with self.assertRaises(CliError):
            safety.record_backup(self.s.state_file, self.s.banks, directory)
        self.assertEqual(len(self.state()["backups"]), 1)


@unittest.skipUnless(HAVE_BACKUP_TOOLS, NEEDS_TOOLS)
class ImportApplyTest(SafetyCase):
    def test_refused_without_a_dry_run_and_without_a_backup(self):
        self.refused(self.apply(*SOURCE), "no dry run with these exact options")

    def test_refused_without_a_backup_even_when_a_dry_run_exists(self):
        self.dry_run(*SOURCE)
        self.refused(
            self.apply(*SOURCE), "no verified backup", "no backup of this banks directory is recorded", "backup --apply"
        )

    def test_refused_without_a_dry_run_even_when_a_backup_exists(self):
        self.backup()
        self.refused(self.apply(*SOURCE), "no dry run", "dry-run")

    def test_refused_when_the_dry_run_used_other_options(self):
        self.backup()
        self.dry_run(*SOURCE)
        self.refused(self.apply(*SOURCE, "--only-new-banks"), "no dry run with these exact options")
        self.refused(
            self.apply("--source", "hindsight", "--hindsight-export", "/x.json"), "no dry run with these exact options"
        )

    def test_refused_when_the_dry_run_is_older_than_24_hours(self):
        self.backup()
        self.dry_run(*SOURCE)
        self.age("dryRuns", 24 * 3600 + 60)
        self.refused(self.apply(*SOURCE), "no dry run with these exact options was made in the last 24 hours")

    def test_refused_when_the_backup_is_older_than_24_hours(self):
        self.backup()
        self.dry_run(*SOURCE)
        self.age("backups", 24 * 3600 + 60)
        self.refused(self.apply(*SOURCE), "newest recorded backup is 24 h")

    def test_refused_when_a_backed_up_file_was_changed_or_removed_afterwards(self):
        self.backup()
        self.dry_run(*SOURCE)
        [entry] = self.state()["backups"]
        copy = Path(entry["dir"]) / "sms-1qfllt3yk702w.db"
        copy.write_bytes(copy.read_bytes() + b"x")
        self.refused(self.apply(*SOURCE), "sms-1qfllt3yk702w.db changed or is missing")
        shutil.rmtree(entry["dir"])
        self.refused(self.apply(*SOURCE), "changed or is missing")

    def test_refused_when_the_backup_does_not_cover_a_bank_the_plan_writes_to(self):
        # the plan writes to `sms-...` and to a new bank; a bank that exists but was made after the backup is not covered
        self.backup()
        self.s.bank("late-1xyz", "work/late")
        report = {**REPORT, "perBank": [{"bank": "late-1xyz", "wouldImport": 1, "imported": 0}]}
        env = self.s.wired(**self.s.bun_stdout(json.dumps(report)))
        self.assertEqual(self.s.run("import", "dry-run", *SOURCE, env=env).code, 0)
        self.refused(self.apply(*SOURCE), "does not cover late-1xyz")

    def test_backup_coverage_is_judged_from_a_plan_made_at_apply_time_not_from_the_recorded_one(self):
        # the recorded dry run names only a new bank; the plan made when --apply runs also writes to a bank that exists now
        # and was made after the backup
        self.backup()
        stale = {**REPORT, "perBank": [{"bank": "brand-new-1abc", "wouldImport": 1, "imported": 0}]}
        env = self.s.wired(**self.s.bun_stdout(json.dumps(stale)))
        self.assertEqual(self.s.run("import", "dry-run", *SOURCE, env=env).code, 0)
        self.s.bank("late-1xyz", "work/late")
        now = {**REPORT, "perBank": [{"bank": "late-1xyz", "wouldImport": 1, "imported": 0}]}
        self.env = self.s.wired(**self.s.bun_stdout(json.dumps(now)))
        self.refused(self.apply(*SOURCE), "does not cover late-1xyz")

    def test_the_plan_made_at_apply_time_is_a_dry_run_without_apply_or_out_and_a_failing_one_refuses(self):
        self.backup()
        self.dry_run(*SOURCE, "--out", str(self.s.root / "plan.json"))
        self.assertEqual(self.apply(*SOURCE, "--out", str(self.s.root / "plan.json")).code, 0)
        calls = self.s.bun_calls()
        self.assertIn("--apply", calls[-1])
        self.assertIn("dry-run", calls[-2])
        self.assertNotIn("--apply", calls[-2])
        self.assertNotIn("--out", calls[-2])
        failing = self.s.wired(**self.s.bun_stdout(json.dumps(REPORT)), FAKE_BUN_EXIT="1")
        self.env = failing
        self.refused(self.apply(*SOURCE), "could not plan this import")

    def test_an_apply_is_refused_when_the_plan_made_now_is_not_the_one_the_dry_run_showed(self):
        self.backup()
        shown = {**REPORT, "planDigest": "a" * 64}
        self.env = self.s.wired(**self.s.bun_stdout(json.dumps(shown)))
        self.dry_run(*SOURCE)
        self.assertEqual(self.apply(*SOURCE).code, 0)  # the same plan: allowed
        changed = {**REPORT, "planDigest": "b" * 64}
        self.env = self.s.wired(**self.s.bun_stdout(json.dumps(changed)))
        self.refused(self.apply(*SOURCE), "not what your dry run showed")
        self.refused(self.apply(*SOURCE, "--no-backup"), "not what your dry run showed")
        self.assertEqual(self.apply(*SOURCE, "--skip-dry-run-check").code, 0)  # the explicit escape

    def test_the_importer_is_told_to_enforce_the_digest_and_the_backed_up_banks_itself(self):
        self.backup()
        shown = {**REPORT, "planDigest": "c" * 64}
        self.env = self.s.wired(**self.s.bun_stdout(json.dumps(shown)))
        self.dry_run(*SOURCE)
        self.assertEqual(self.apply(*SOURCE).code, 0)
        argv = self.s.bun_argv()
        self.assertEqual(argv[argv.index("--expect-plan-digest") + 1], "c" * 64)
        covered = [a for a in argv if a.startswith("--backed-up-banks=")]
        self.assertEqual(covered, ["--backed-up-banks=sms-1qfllt3yk702w,tools-2guil9dlpgfxr"])
        self.assertIn("--exempt-banks=", argv)
        self.assertNotIn("--no-plan-check", argv)
        self.assertNotIn("--no-backup-check", argv)

    def test_the_escapes_become_explicit_importer_flags(self):
        shown = {**REPORT, "planDigest": "d" * 64}
        self.env = self.s.wired(**self.s.bun_stdout(json.dumps(shown)))
        self.assertEqual(self.apply(*SOURCE, "--skip-dry-run-check", "--no-backup").code, 0)
        argv = self.s.bun_argv()
        self.assertIn("--no-plan-check", argv)
        self.assertIn("--no-backup-check", argv)
        self.assertNotIn("--expect-plan-digest", argv)

    def test_a_dry_run_recorded_without_a_digest_is_not_enough(self):
        self.backup()
        old = {k: v for k, v in REPORT.items() if k != "planDigest"}
        self.env = self.s.wired(**self.s.bun_stdout(json.dumps(old)))
        self.dry_run(*SOURCE)
        self.refused(self.apply(*SOURCE), "no plan digest")

    def test_a_flag_given_twice_is_refused_so_the_fingerprint_cannot_differ_from_what_the_importer_uses(self):
        for command in (("dry-run",), ("import", "--apply")):
            r = self.s.run("import", *command, *SOURCE, "--banks", "a", "--banks", "b", env=self.env)
            self.assertEqual(r.code, 2, r.err)
            self.assertIn("--banks was given more than once", r.err)
        with self.assertRaises(CliError):
            safety.fingerprint(["--banks", "a", "--banks", "b"])

    def test_a_file_named_by_a_flag_is_part_of_the_fingerprint(self):
        listing = self.s.root / "released.txt"
        listing.write_text("~/work/a#one\n")
        self.backup()
        self.dry_run(*SOURCE, "--release-held", str(listing))
        self.assertEqual(self.apply(*SOURCE, "--release-held", str(listing)).code, 0)
        listing.write_text("~/work/a#one\n~/work/a#two\n")  # changed after the plan was read
        self.refused(self.apply(*SOURCE, "--release-held", str(listing)), "no dry run with these exact options")

    def test_refused_when_the_backup_is_of_another_banks_directory(self):
        self.backup()
        self.dry_run(*SOURCE, "--banks-dir", str(self.s.banks))
        other = self.s.root / "other-banks"
        shutil.copytree(self.s.banks, other)
        self.dry_run(*SOURCE, "--banks-dir", str(other))
        self.refused(self.apply(*SOURCE, "--banks-dir", str(other)), "no backup of this banks directory is recorded")

    def test_allowed_with_a_fresh_backup_and_a_dry_run_of_the_same_options(self):
        self.backup()
        self.dry_run(*SOURCE, "--only-new-banks")
        self.s.bun_log.write_text("untouched")
        r = self.s.run("import", "import", "--apply", "--only-new-banks", *SOURCE, "--run-id", "imp-1", env=self.env)
        self.assertEqual(r.code, 0, r.err)
        self.assertIn("backup", r.err)
        self.assertIn("verified", r.err)
        argv = self.s.bun_argv()
        self.assertEqual(argv[1], "import")
        self.assertIn("--apply", argv)
        self.assertEqual(argv[argv.index("--run-id") + 1], "imp-1")

    def test_a_dry_run_with_the_default_source_matches_an_apply_that_names_it(self):
        self.backup()
        self.dry_run()
        r = self.s.run("import", "import", "--apply", "--source", "claude-memory", env=self.env)
        self.assertEqual(r.code, 0, r.err)

    def test_no_backup_skips_only_the_backup_check_and_says_so(self):
        self.dry_run(*SOURCE)
        r = self.apply(*SOURCE, "--no-backup")
        self.assertEqual(r.code, 0, r.err)
        self.assertIn("warning: --no-backup", r.err)
        refused = self.apply(*SOURCE, "--only-new-banks", "--no-backup")  # still needs its dry run
        self.refused(refused, "no dry run")

    def test_skip_dry_run_check_skips_only_the_dry_run_check_and_needs_a_backup_of_every_bank(self):
        self.refused(self.apply(*SOURCE, "--skip-dry-run-check"), "no verified backup")
        self.backup()
        r = self.apply(*SOURCE, "--skip-dry-run-check")
        self.assertEqual(r.code, 0, r.err)
        self.assertIn("warning: --skip-dry-run-check", r.err)
        self.s.bank("late-1xyz", "work/late")
        planned = {**REPORT, "perBank": [{"bank": "late-1xyz", "wouldImport": 1, "imported": 0}]}
        self.env = self.s.wired(**self.s.bun_stdout(json.dumps(planned)))
        self.refused(self.apply(*SOURCE, "--skip-dry-run-check"), "does not cover late-1xyz")

    def test_both_escapes_together_allow_the_apply(self):
        r = self.apply(*SOURCE, "--no-backup", "--skip-dry-run-check")
        self.assertEqual(r.code, 0, r.err)

    def test_the_escapes_are_refused_on_anything_but_an_apply(self):
        for args in (("dry-run",), ("scan",), ("undo", "--run", "r1")):
            r = self.s.run("import", *args, "--no-backup", env=self.env)
            self.assertEqual(r.code, 2, args)
            self.assertIn("only apply to `import import --apply`", r.err)

    def test_undo_apply_and_the_read_commands_need_no_records(self):
        for args in (("undo", "--run", "r1", "--apply"), ("verify",), ("scan",)):
            self.s.bun_log.write_text("untouched")
            r = self.s.run("import", *args, env=self.env)
            self.assertEqual(r.code, 0, (args, r.err))
            self.assertNotEqual(self.s.bun_log.read_text(), "untouched")

    def test_a_damaged_state_file_counts_as_no_records_and_is_never_overwritten(self):
        self.s.state_file.parent.mkdir(parents=True)
        self.s.state_file.write_text("{not json")
        r = self.apply(*SOURCE)
        self.assertEqual(r.code, 2, r.err)  # refused before anything runs; nothing is written over it
        self.assertEqual(self.s.state_file.read_text(), "{not json")
        self.assertEqual(self.dry_run(*SOURCE, code=2).code, 2)
        self.assertEqual(self.s.state_file.read_text(), "{not json")
        self.s.state_file.unlink()  # once the user removes it, recording works again
        self.dry_run(*SOURCE)
        self.assertEqual(len(self.state()["dryRuns"]), 1)

    def test_the_state_file_keeps_only_the_newest_records(self):
        for _ in range(safety.KEEP + 3):
            self.dry_run(*SOURCE)
        self.assertEqual(len(self.state()["dryRuns"]), safety.KEEP)


def importer_rows(db_path, run_id="imp-1", extra_native=False):
    """Make the bank at `db_path` look like one this tool filled: every row carries an origin key and a run id."""
    import sqlite3

    meta = json.dumps(
        {"originKey": "k#1", "importRunId": run_id, "source": "claude-code", "originHash": "h", "tool": "import"}
    )
    db = sqlite3.connect(db_path)
    db.execute(
        "UPDATE working_memory SET metadata_json = ?, trust_tier = 'IMPORTED', source = 'import-claude-code'", (meta,)
    )
    if extra_native:
        db.execute(
            "INSERT INTO working_memory (id, content, source, timestamp, session_id, importance, metadata_json, scope)"
            " VALUES ('native-1', 'omp wrote this', 'coding-agent-learn', '2026-11-05T10:00:00.000Z', 's', 0.5, '{}', 'bank')"
        )
    db.commit()
    db.close()


NEW_ONLY = {
    **REPORT,
    "counts": {"found": 1, "imported": 0, "wouldImport": 1},
    "perBank": [{"bank": "brand-new-1abc", "wouldImport": 1, "imported": 0}],
}


class FirstImportTest(ScratchCase):
    """A machine where omp has written no memory yet: there is nothing a backup could protect."""

    def setUp(self):
        super().setUp()
        self.env = self.s.wired(**self.s.bun_stdout(json.dumps(NEW_ONLY)))

    def test_the_first_import_needs_a_dry_run_but_no_backup_and_says_why(self):
        self.assertFalse(self.s.banks.exists())
        self.assertEqual(self.s.run("import", "dry-run", *SOURCE, env=self.env).code, 0)
        self.s.bun_log.write_text("untouched")
        r = self.s.run("import", "import", "--apply", *SOURCE, env=self.env)
        self.assertEqual(r.code, 0, r.err)
        self.assertIn("no backup needed", r.err)
        self.assertEqual(self.s.bun_argv()[1], "import")
        self.assertNotIn("no verified backup", r.err)

    def test_the_dry_run_is_still_required(self):
        self.s.bun_log.write_text("untouched")
        r = self.s.run("import", "import", "--apply", *SOURCE, env=self.env)
        self.assertEqual(r.code, 1)
        self.assertIn("no dry run with these exact options", r.err)
        self.assertEqual(self.s.bun_log.read_text(), "untouched")


class RerunAfterOwnImportTest(ScratchCase):
    """The README says re-running is safe: banks this tool created hold only what it wrote, so they need no backup."""

    def setUp(self):
        super().setUp()
        self.db = self.s.bank("made-by-import-1abc", "work/made")
        self.s.bank("omps-own-2def", "work/own")
        report = {**REPORT, "perBank": [{"bank": "made-by-import-1abc", "wouldImport": 1, "imported": 0}]}
        self.env = self.s.wired(**self.s.bun_stdout(json.dumps(report)))
        self.ledger = self.s.home / ".config" / "omp-memory-import" / "ledger.json"

    def ledger_says_created(self, *banks):
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        created = {bank: {"runId": "imp-1", "at": "2026-11-11T10:00:00.000Z"} for bank in banks}
        self.ledger.write_text(json.dumps({"version": 1, "entries": {}, "runs": {}, "banksCreated": created}))

    def attempt(self):
        self.assertEqual(self.s.run("import", "dry-run", *SOURCE, env=self.env).code, 0)
        self.s.forget_bun_calls()
        return self.s.run("import", "import", "--apply", *SOURCE, env=self.env)

    def test_a_bank_the_importer_created_and_still_only_holds_its_rows_needs_no_backup(self):
        importer_rows(self.db)
        self.ledger_says_created("made-by-import-1abc")
        r = self.attempt()
        self.assertEqual(r.code, 0, r.err)
        self.assertIn("no backup needed", r.err)
        self.assertIn("1 bank(s) were created by an earlier import", r.err)

    def test_an_emptied_bank_the_importer_created_needs_no_backup(self):
        import sqlite3

        importer_rows(self.db)
        db = sqlite3.connect(self.db)
        for table in ("working_memory", "memory_embeddings", "annotations"):  # what an undo removes
            db.execute(f"DELETE FROM {table}")
        db.commit()
        db.close()
        self.ledger_says_created("made-by-import-1abc")
        self.assertEqual(self.attempt().code, 0)

    def test_an_exemption_is_passed_apart_from_verified_coverage_so_the_importer_can_re_judge_it(self):
        importer_rows(self.db)
        self.ledger_says_created("made-by-import-1abc")
        self.assertEqual(self.attempt().code, 0)
        argv = self.s.bun_argv()
        self.assertIn("--exempt-banks=made-by-import-1abc", argv)
        self.assertNotIn("made-by-import-1abc", next(a for a in argv if a.startswith("--backed-up-banks=")))

    def test_once_omp_wrote_into_it_the_bank_is_the_users_and_a_backup_is_required_again(self):
        importer_rows(self.db, extra_native=True)
        self.ledger_says_created("made-by-import-1abc")
        r = self.attempt()
        self.assertEqual(r.code, 1)
        self.assertIn("no verified backup", r.err)
        self.assertFalse(any("--apply" in call for call in self.s.bun_calls()), self.s.bun_calls())

    def native(self, sql, *params):
        import sqlite3

        db = sqlite3.connect(self.db)
        db.execute(sql, params)
        db.commit()
        db.close()

    def exempt(self):
        from omp_memory import safety

        return safety.holds_only_importer_rows(self.db)

    def test_the_exemption_is_closed_for_every_kind_of_content_a_bank_can_hold(self):
        importer_rows(self.db)
        self.assertTrue(self.exempt())
        import sqlite3

        db = sqlite3.connect(self.db)
        user_tables = [
            r[0]
            for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite!_%' ESCAPE '!'"
                " AND name NOT LIKE 'fts!_%' ESCAPE '!' AND name NOT IN ('working_memory', 'annotations', 'memory_embeddings')"
            )
        ]
        db.close()
        self.assertIn("episodic_memory", user_tables)
        self.assertIn("scratchpad", user_tables)
        for table in user_tables:
            with self.subTest(table=table):
                import shutil

                copy = self.db.with_name("copy.db")
                shutil.copy(self.db, copy)
                db = sqlite3.connect(copy)
                columns = [r for r in db.execute(f'PRAGMA table_info("{table}")') if r[3] and r[4] is None and not r[5]]
                if columns:
                    db.close()
                    continue  # a table with required columns is covered by the named cases below
                try:
                    db.execute(f'INSERT INTO "{table}" DEFAULT VALUES')
                    db.commit()
                except sqlite3.Error:
                    db.close()
                    continue
                db.close()
                from omp_memory import safety

                self.assertFalse(safety.holds_only_importer_rows(copy), table)

    def test_a_bank_with_native_episodic_memory_only_is_not_exempt(self):
        self.native("DELETE FROM working_memory")
        self.native(
            "INSERT INTO episodic_memory (id, content, source, timestamp, session_id, importance, metadata_json)"
            " VALUES ('e1', 'omp wrote this', 'coding-agent-learn', '2026-11-05T10:00:00.000Z', 's', 0.5, '{}')"
        )
        self.assertFalse(self.exempt())

    def test_a_native_scratchpad_row_ends_the_exemption(self):
        importer_rows(self.db)
        self.native("INSERT INTO scratchpad (id, content) VALUES ('s1', 'a note')")
        self.assertFalse(self.exempt())

    def test_unmerged_wal_content_or_an_unreadable_bank_is_not_exempt(self):
        importer_rows(self.db)
        wal = self.db.with_name("mnemopi.db-wal")
        wal.write_bytes(b"x" * 64)
        self.assertFalse(self.exempt())
        wal.unlink()
        self.db.with_name("mnemopi.db-shm").write_bytes(b"\0" * 32768)
        self.assertTrue(self.exempt())  # a shm file alone is only Bun's leftover
        self.db.write_bytes(b"not a database" * 50)
        self.assertFalse(self.exempt())

    def test_rows_that_only_look_like_importer_rows_are_not_exempt(self):
        importer_rows(self.db)
        self.native("UPDATE working_memory SET trust_tier = 'PEER'")
        self.assertFalse(self.exempt())

    def test_a_row_missing_or_nulling_any_provenance_field_is_not_exempt(self):
        import sqlite3

        fields = ("source", "tool", "originKey", "originHash")
        for field in fields:
            for how in ("missing", "null", "number"):
                with self.subTest(field=field, how=how):
                    importer_rows(self.db)
                    db = sqlite3.connect(self.db)
                    if how == "missing":
                        db.execute(
                            "UPDATE working_memory SET metadata_json = json_remove(metadata_json, ?)", (f"$.{field}",)
                        )
                    elif how == "null":
                        db.execute(
                            "UPDATE working_memory SET metadata_json = json_set(metadata_json, ?, NULL)",
                            (f"$.{field}",),
                        )
                    else:
                        db.execute(
                            "UPDATE working_memory SET metadata_json = json_set(metadata_json, ?, 7)", (f"$.{field}",)
                        )
                    db.commit()
                    db.close()
                    self.assertFalse(self.exempt())
        importer_rows(self.db)
        self.native("UPDATE working_memory SET trust_tier = NULL")
        self.assertFalse(self.exempt())
        importer_rows(self.db)
        self.native("UPDATE working_memory SET metadata_json = NULL")
        self.assertFalse(self.exempt())

    def test_embeddings_or_annotations_of_other_rows_end_the_exemption(self):
        importer_rows(self.db)
        self.native("INSERT INTO annotations (memory_id, kind, value) VALUES ('someone-elses', 'k', 'v')")
        self.assertFalse(self.exempt())

    def test_a_bank_that_was_there_before_the_import_is_never_exempt(self):
        importer_rows(self.db)  # looks like ours, but the ledger does not say this tool created it
        self.ledger_says_created("some-other-bank")
        self.assertEqual(self.attempt().code, 1)

    def test_a_damaged_ledger_exempts_nothing(self):
        importer_rows(self.db)
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text("{not json")
        self.assertEqual(self.attempt().code, 1)


class DocsTest(unittest.TestCase):
    def test_the_two_checks_and_their_escapes_are_documented(self):
        readme = (PLUGIN / "README.md").read_text()
        for phrase in ("--no-backup", "--skip-dry-run-check", "24 hours", "backup --apply", "dry-run"):
            self.assertIn(phrase, readme)


if __name__ == "__main__":
    unittest.main()
