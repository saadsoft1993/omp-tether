"""A file this command line writes itself (state, configuration, labels) must never be able to replace a bank or any other file.

Every case points the destination at something that is not its own kind of file and checks that the file is byte for byte
the same afterwards, with and without `--apply`.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import unittest

import support  # noqa: F401  (puts the plugin's python/ and vendor/ directories on sys.path)
from support import ScratchCase

REPORT = {"command": "dry-run", "source": "claude-memory", "counts": {"found": 0, "wouldImport": 0}, "perBank": []}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class StateDestinationTest(ScratchCase):
    def setUp(self):
        super().setUp()
        self.db = self.s.bank("alpha-1abc", "work/alpha")
        self.env = self.s.wired(**self.s.bun_stdout(json.dumps(REPORT)))

    def with_state(self, target, **extra):
        return {**self.env, "OMP_MEMORY_STATE_FILE": str(target), **extra}

    def test_a_bank_database_as_the_state_file_is_refused_and_not_touched(self):
        before = digest(self.db)
        for args in (("import", "dry-run"), ("backup", "--apply")):
            with self.subTest(args=args):
                r = self.s.run(*args, env=self.with_state(self.db))
                self.assertEqual(r.code, 2, r.out)
                self.assertIn("inside the banks directory", r.err)
                self.assertEqual(digest(self.db), before)

    def test_a_database_outside_the_banks_directory_as_the_state_file_is_refused_too(self):
        copy = self.s.root / "copy.db"
        shutil.copy(self.db, copy)
        before = digest(copy)
        r = self.s.run("import", "dry-run", env=self.with_state(copy))
        self.assertEqual(r.code, 2, r.out)
        self.assertIn("not an omp-memory state file", r.err)
        self.assertEqual(digest(copy), before)

    def test_apply_with_a_bank_as_the_state_file_is_refused_before_anything_runs(self):
        before = digest(self.db)
        self.s.forget_bun_calls()
        r = self.s.run(
            "import", "import", "--apply", "--no-backup", "--skip-dry-run-check", env=self.with_state(self.db)
        )
        self.assertEqual(r.code, 2, r.err)
        self.assertEqual(digest(self.db), before)
        self.assertEqual(self.s.bun_calls(), [])

    def test_a_symlink_as_the_state_file_is_refused_and_its_target_untouched(self):
        victim = self.s.root / "victim.txt"
        victim.write_text("precious\n")
        link = self.s.root / "state-link.json"
        os.symlink(victim, link)
        r = self.s.run("import", "dry-run", env=self.with_state(link))
        self.assertNotEqual(r.code, 0, r.out)
        self.assertEqual(victim.read_text(), "precious\n")
        self.assertTrue(link.is_symlink())

    def test_any_file_of_another_kind_is_refused(self):
        for name, content in (
            ("notes.txt", b"some notes\n"),
            ("other.json", b'{"hello": "world"}\n'),
            ("list.json", b"[1, 2]\n"),
            ("broken.json", b'{"schemaVersion": 1, '),
        ):
            with self.subTest(name=name):
                path = self.s.root / name
                path.write_bytes(content)
                r = self.s.run("import", "dry-run", env=self.with_state(path))
                self.assertNotEqual(r.code, 0, r.out)
                self.assertEqual(path.read_bytes(), content)

    def test_a_state_file_inside_the_banks_directory_is_refused(self):
        inside = self.s.banks / "alpha-1abc" / "state.json"
        r = self.s.run("import", "dry-run", env=self.with_state(inside))
        self.assertNotEqual(r.code, 0, r.out)
        self.assertIn("inside the banks directory", r.err)
        self.assertFalse(inside.exists())

    def test_a_directory_as_the_state_file_is_refused(self):
        target = self.s.root / "a-directory"
        target.mkdir()
        r = self.s.run("import", "dry-run", env=self.with_state(target))
        self.assertNotEqual(r.code, 0, r.out)

    def test_a_real_state_file_and_an_absent_one_still_work(self):
        fresh = self.s.root / "fresh" / "state.json"
        self.assertEqual(self.s.run("import", "dry-run", env=self.with_state(fresh)).code, 0)
        self.assertEqual(json.loads(fresh.read_text())["schemaVersion"], 1)
        self.assertEqual(self.s.run("import", "dry-run", env=self.with_state(fresh)).code, 0)
        self.assertEqual(oct(fresh.stat().st_mode & 0o777), "0o600")

    def test_an_empty_file_is_taken_as_a_new_state_file(self):
        empty = self.s.root / "empty.json"
        empty.write_bytes(b"")
        self.assertEqual(self.s.run("import", "dry-run", env=self.with_state(empty)).code, 0)

    def test_the_temporary_file_is_exclusive_and_leaves_nothing_behind(self):
        target = self.s.root / "d" / "state.json"
        self.assertEqual(self.s.run("import", "dry-run", env=self.with_state(target)).code, 0)
        self.assertEqual(sorted(p.name for p in target.parent.iterdir()), ["state.json"])


class ConfigAndLabelsDestinationTest(ScratchCase):
    def setUp(self):
        super().setUp()
        self.db = self.s.bank("alpha-1abc", "work/alpha")

    def test_the_labels_file_cannot_be_a_bank(self):
        before = digest(self.db)
        env = self.s.wired(OMP_MEMORY_LABELS_FILE=str(self.db))
        r = self.s.run("labels", "set", "alpha-1abc", "Alpha", env=env)
        self.assertNotEqual(r.code, 0, r.out)
        self.assertEqual(digest(self.db), before)

    def test_the_labels_file_cannot_be_a_symlink(self):
        victim = self.s.root / "victim.txt"
        victim.write_text("precious\n")
        link = self.s.root / "labels-link.json"
        os.symlink(victim, link)
        r = self.s.run("labels", "set", "alpha-1abc", "Alpha", env=self.s.wired(OMP_MEMORY_LABELS_FILE=str(link)))
        self.assertNotEqual(r.code, 0, r.out)
        self.assertEqual(victim.read_text(), "precious\n")

    def test_the_configuration_file_cannot_replace_another_file(self):
        other = self.s.root / "other.json"
        other.write_text('{"hello": "world"}\n')
        env = self.s.wired(OMP_MEMORY_CONFIG=str(other))
        r = self.s.run("config", "set", "backupsDir", str(self.s.root / "b"), env=env)
        self.assertNotEqual(r.code, 0, r.out)
        self.assertEqual(other.read_text(), '{"hello": "world"}\n')

    def test_a_labels_file_of_its_own_kind_is_updated(self):
        labels = self.s.root / "labels.json"
        labels.write_text(json.dumps({"version": 1, "labels": {"alpha-1abc": {"label": "Old"}}}))
        labels.chmod(0o600)
        env = self.s.wired(OMP_MEMORY_LABELS_FILE=str(labels))
        self.assertEqual(self.s.run("labels", "set", "alpha-1abc", "Alpha", env=env).code, 0)
        self.assertEqual(json.loads(labels.read_text())["labels"]["alpha-1abc"]["label"], "Alpha")


if __name__ == "__main__":
    unittest.main()


class DisjointKindsTest(ScratchCase):
    """State, configuration and label files are told apart by their own markers; none may overwrite another's."""

    def setUp(self):
        super().setUp()
        self.s.bank("alpha-1abc", "work/alpha")
        self.env = self.s.wired(**self.s.bun_stdout(json.dumps(REPORT)))

    def test_a_configuration_file_cannot_be_the_state_file(self):
        config = self.s.root / "config.json"
        config.write_text(json.dumps({"schemaVersion": 1, "kind": "omp-memory-config", "backupsDir": "/tmp/b"}))
        before = config.read_bytes()
        env = {**self.env, "OMP_MEMORY_STATE_FILE": str(config)}
        r = self.s.run("import", "dry-run", env=env)
        self.assertEqual(r.code, 2, r.out)
        self.assertEqual(config.read_bytes(), before)

    def test_a_hand_written_configuration_file_without_a_marker_cannot_be_the_state_file_either(self):
        config = self.s.root / "config.json"
        config.write_text(json.dumps({"schemaVersion": 1, "backupsDir": "/tmp/b"}))
        before = config.read_bytes()
        r = self.s.run("import", "dry-run", env={**self.env, "OMP_MEMORY_STATE_FILE": str(config)})
        self.assertEqual(r.code, 2, r.out)
        self.assertEqual(config.read_bytes(), before)

    def test_unrelated_versioned_json_is_neither_state_nor_configuration(self):
        other = self.s.root / "package.json"
        other.write_text(json.dumps({"schemaVersion": 1, "name": "something else"}))
        before = other.read_bytes()
        r = self.s.run("import", "dry-run", env={**self.env, "OMP_MEMORY_STATE_FILE": str(other)})
        self.assertEqual(r.code, 2, r.out)
        r = self.s.run(
            "config", "set", "backupsDir", str(self.s.root / "b"), env={**self.env, "OMP_MEMORY_CONFIG": str(other)}
        )
        self.assertNotEqual(r.code, 0, r.out)
        self.assertEqual(other.read_bytes(), before)

    def test_a_state_file_cannot_be_the_configuration_or_labels_file(self):
        state = self.s.root / "state.json"
        self.assertEqual(self.s.run("import", "dry-run", env={**self.env, "OMP_MEMORY_STATE_FILE": str(state)}).code, 0)
        before = state.read_bytes()
        r = self.s.run(
            "config", "set", "backupsDir", str(self.s.root / "b"), env={**self.env, "OMP_MEMORY_CONFIG": str(state)}
        )
        self.assertNotEqual(r.code, 0, r.out)
        r = self.s.run("labels", "set", "alpha-1abc", "A", env={**self.env, "OMP_MEMORY_LABELS_FILE": str(state)})
        self.assertNotEqual(r.code, 0, r.out)
        self.assertEqual(state.read_bytes(), before)

    def test_two_destinations_that_are_the_same_file_are_refused(self):
        same = self.s.root / "same.json"
        env = {**self.env, "OMP_MEMORY_STATE_FILE": str(same), "OMP_MEMORY_LABELS_FILE": str(same)}
        for args in (("import", "dry-run"), ("labels", "set", "alpha-1abc", "A")):
            r = self.s.run(*args, env=env)
            self.assertEqual(r.code, 2, (args, r.out))
            self.assertIn("same file", r.err)
        self.assertFalse(same.exists())

    def test_the_configuration_file_cannot_lie_inside_the_banks_directory(self):
        inside = self.s.banks / "alpha-1abc" / "config.json"
        r = self.s.run(
            "config", "set", "backupsDir", str(self.s.root / "b"), env={**self.env, "OMP_MEMORY_CONFIG": str(inside)}
        )
        self.assertNotEqual(r.code, 0, r.out)
        self.assertIn("inside the banks directory", r.err)
        self.assertFalse(inside.exists())

    def test_the_configuration_this_command_writes_is_read_back(self):
        cfg = self.s.root / "ok" / "config.json"
        env = {**self.env, "OMP_MEMORY_CONFIG": str(cfg)}
        self.assertEqual(self.s.run("config", "set", "backupsDir", str(self.s.root / "b"), env=env).code, 0)
        self.assertEqual(json.loads(cfg.read_text())["kind"], "omp-memory-config")
        self.assertEqual(self.s.run("config", "set-group", "work", "~/work", env=env).code, 0)
        self.assertEqual(self.s.run("config", "show", env=env).code, 0)


class ExecutablePathTest(ScratchCase):
    def setUp(self):
        super().setUp()
        self.env = self.s.wired(**self.s.bun_stdout(json.dumps(REPORT)))
        self.here = (self.s.root / "caller").resolve()
        self.here.mkdir()

    def test_a_relative_program_path_is_resolved_against_the_callers_directory_and_a_bare_name_is_kept(self):
        r = self.s.run(
            "import",
            "dry-run",
            "--python",
            "./venv/bin/python",
            "--sqlite3",
            "./bin/sqlite3",
            env=self.env,
            cwd=self.here,
        )
        self.assertEqual(r.code, 0, r.err)
        argv = self.s.bun_argv()
        self.assertEqual(argv[argv.index("--python") + 1], str(self.here / "venv/bin/python"))
        self.assertEqual(argv[argv.index("--sqlite3") + 1], str(self.here / "bin/sqlite3"))
        r = self.s.run("import", "dry-run", "--sqlite3", "sqlite3", env=self.env, cwd=self.here)
        self.assertEqual(r.code, 0, r.err)
        argv = self.s.bun_argv()
        self.assertEqual(argv[argv.index("--sqlite3") + 1], "sqlite3")
