"""Hardening tests for the bundled backup, restore-drill and Hindsight page-export code.

The scripts come from `vendor/memory_ops` unless OMP_MEMORY_OPS_DIR points at another directory of the same files
(or OMP_MEMORY_SOURCE_DIR at a checkout that has a `memory_ops` directory), which is how a fix is tested before the
vendor copy is refreshed. Every run uses a scratch HOME and synthetic banks."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import sqlite3
import subprocess
import sys
import unittest
import urllib.request
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from support import VENDOR, ScratchCase, make_bank

if os.environ.get("OMP_MEMORY_OPS_DIR"):
    OPS = Path(os.environ["OMP_MEMORY_OPS_DIR"])
elif os.environ.get("OMP_MEMORY_SOURCE_DIR"):
    OPS = Path(os.environ["OMP_MEMORY_SOURCE_DIR"]) / "memory_ops"
else:
    OPS = VENDOR / "memory_ops"

SCRIPTS_RUN = shutil.which("sqlite3") and shutil.which("bash") and (shutil.which("shasum") or shutil.which("sha256sum"))
SKIP = "the bundled backup scripts need sqlite3 and shasum or sha256sum"
# The name the backup gives to the database that sits next to the banks directory (see backup_mnemopi.sh).
PARENT_COPY = "_parent-mnemopi"


def digest_tree(root: Path) -> dict[str, str]:
    """{relative path: sha256} of every file under root except SQLite's -shm and -wal side files."""
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.name.endswith(("-shm", "-wal")):
            out[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def rows_of(database: Path, table: str = "working_memory") -> int:
    con = sqlite3.connect(f"file:{quote(str(database))}?immutable=1", uri=True)
    try:
        return con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
    finally:
        con.close()


class ScriptCase(ScratchCase):
    def setUp(self):
        super().setUp()
        self.tmp = self.s.root / "tmp"
        self.tmp.mkdir()
        self.mnemopi = self.s.banks.parent
        self.dest = self.s.root / "backups"

    def script(self, name: str, *args: str):
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.s.home),
            "TMPDIR": str(self.tmp),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        return subprocess.run(
            ["bash", str(OPS / name), *args], env=env, capture_output=True, text=True, timeout=120, check=False
        )

    def backup(
        self, src: Path | None = None, dest: Path | None = None
    ) -> tuple[subprocess.CompletedProcess, Path | None]:
        ran = self.script("backup_mnemopi.sh", "--dest", str(dest or self.dest), "--src", str(src or self.s.banks))
        found = [line[len("BACKUP_DIR=") :] for line in ran.stdout.splitlines() if line.startswith("BACKUP_DIR=")]
        return ran, (Path(found[-1]) if found else None)

    def drill(self, backup: Path, *extra: str, src: Path | None = None):
        return self.script("restore_drill.sh", "--backup", str(backup), "--src", str(src or self.s.banks), *extra)

    def parent_db(self, rows: int) -> Path:
        """A database next to the banks directory (the older global bank location)."""
        holder = self.s.root / "parent-holder"
        make_bank(holder, "x", wm=tuple((f"p{i}", f"parent {i}", "global") for i in range(rows)))
        target = self.mnemopi / "mnemopi.db"
        shutil.copy(holder / "x" / "mnemopi.db", target)
        return target


@unittest.skipUnless(SCRIPTS_RUN, SKIP)
class RestoreScratchTest(ScriptCase):
    def setUp(self):
        super().setUp()
        self.s.bank("alpha-1", "work/a", wm=(("w1", "one", "bank"), ("w2", "two", "bank")))
        self.s.bank("beta-2", "work/b")
        ran, self.stamp = self.backup()
        self.assertEqual(ran.returncode, 0, ran.stderr)

    def test_a_scratch_that_overlaps_live_data_or_the_backup_is_refused(self):
        link = self.s.root / "link-to-banks"
        link.symlink_to(self.s.banks)
        cases = {
            "banks directory itself": self.s.banks,
            "parent of the banks directory": self.mnemopi,
            "a home that holds the banks": self.s.home,
            "a new directory inside the banks": self.s.banks / "newdir",
            "a symlink to the banks": link,
            "the backup directory": self.stamp,
            "a new directory inside the backup": self.stamp / "sub",
            "the folder that holds the backup": self.stamp.parent,
        }
        before_banks = digest_tree(self.s.banks)
        before_backup = digest_tree(self.stamp)
        for label, scratch in cases.items():
            with self.subTest(label):
                ran = self.drill(self.stamp, "--scratch", str(scratch))
                self.assertEqual(ran.returncode, 2, ran.stdout + ran.stderr)
                self.assertIn("overlap", ran.stderr)
                self.assertTrue(self.s.banks.is_dir())
                self.assertEqual(digest_tree(self.s.banks), before_banks)
                self.assertEqual(digest_tree(self.stamp), before_backup)
                self.assertFalse((self.s.banks / "newdir").exists())
                self.assertFalse((self.stamp / "sub").exists())

    def test_a_banks_symlink_in_the_scratch_does_not_redirect_the_restore(self):
        scratch = self.s.root / "scratch-parent"
        victim = self.s.root / "victim"
        scratch.mkdir()
        victim.mkdir()
        (scratch / "banks").symlink_to(victim)
        (scratch / "keepme.txt").write_text("mine")
        ran = self.drill(self.stamp, "--scratch", str(scratch))
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertEqual(list(victim.iterdir()), [])
        self.assertTrue((scratch / "banks").is_symlink())
        self.assertEqual(sorted(p.name for p in scratch.iterdir()), ["banks", "keepme.txt"])

    def test_a_failed_checksum_deletes_nothing_the_run_did_not_create(self):
        scratch = self.s.root / "scratch-parent"
        for name in ("banks", "snap"):
            (scratch / name).mkdir(parents=True)
            (scratch / name / "precious.txt").write_text("mine")
        with open(self.stamp / "beta-2.db", "ab") as handle:
            handle.write(b"x")
        ran = self.drill(self.stamp, "--scratch", str(scratch))
        self.assertEqual(ran.returncode, 1, ran.stdout + ran.stderr)
        self.assertIn("checksum mismatch", ran.stderr)
        self.assertEqual((scratch / "banks" / "precious.txt").read_text(), "mine")
        self.assertEqual((scratch / "snap" / "precious.txt").read_text(), "mine")
        self.assertEqual(sorted(p.name for p in scratch.iterdir()), ["banks", "snap"])

    def test_a_given_scratch_gets_a_fresh_private_child_that_is_removed_afterwards(self):
        scratch = self.s.root / "scratch-parent"
        scratch.mkdir()
        ran = self.drill(self.stamp, "--scratch", str(scratch))
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertIn("RESULT: OK, identical to source", ran.stdout)
        self.assertEqual(list(scratch.iterdir()), [])
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_keep_leaves_exactly_the_private_child_it_made(self):
        scratch = self.s.root / "scratch-parent"
        scratch.mkdir()
        ran = self.drill(self.stamp, "--scratch", str(scratch), "--keep")
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        [child] = list(scratch.iterdir())
        self.assertEqual(child.stat().st_mode & 0o777, 0o700)
        self.assertIn(str(child.resolve()), ran.stdout + ran.stderr)
        self.assertEqual(rows_of(child / "banks" / "alpha-1" / "mnemopi.db"), 2)

    def test_a_missing_scratch_parent_is_created_and_a_default_scratch_is_removed(self):
        scratch = self.s.root / "not" / "yet"
        ran = self.drill(self.stamp, "--scratch", str(scratch))
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertEqual(list(scratch.iterdir()), [])
        ran = self.drill(self.stamp)
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertEqual(list(self.tmp.iterdir()), [])


@unittest.skipUnless(SCRIPTS_RUN, SKIP)
class ParentDatabaseNameTest(ScriptCase):
    def setUp(self):
        super().setUp()
        self.s.bank("default", None, wm=(("d1", "global one", "global"), ("d2", "global two", "global")))
        self.parent_db(rows=3)

    def test_the_default_bank_and_the_parent_database_get_distinct_backup_files(self):
        ran, stamp = self.backup()
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertEqual(
            sorted(p.name for p in stamp.iterdir()),
            sorted(["MANIFEST.tsv", "SHA256SUMS", "default.db", f"{PARENT_COPY}.db"]),
        )
        self.assertEqual(rows_of(stamp / "default.db"), 2)
        self.assertEqual(rows_of(stamp / f"{PARENT_COPY}.db"), 3)
        sums = (stamp / "SHA256SUMS").read_text()
        for name in ("default.db", f"{PARENT_COPY}.db"):
            digest = hashlib.sha256((stamp / name).read_bytes()).hexdigest()
            self.assertIn(f"{digest}  {name}", sums)
        manifest = {
            row.split("\t")[0]: row.split("\t") for row in (stamp / "MANIFEST.tsv").read_text().splitlines()[1:]
        }
        self.assertEqual(manifest["default"][1], str(self.s.banks / "default" / "mnemopi.db"))
        self.assertEqual(manifest[PARENT_COPY][1], str(self.mnemopi / "mnemopi.db"))

    def test_the_drill_restores_each_database_to_its_own_place_and_compares_it_with_its_own_source(self):
        _, stamp = self.backup()
        scratch = self.s.root / "scratch-parent"
        ran = self.drill(stamp, "--scratch", str(scratch), "--keep")
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertIn("RESULT: OK, identical to source", ran.stdout)
        self.assertEqual(ran.stdout.count("same / same"), 2, ran.stdout)
        [work] = list(scratch.iterdir())
        self.assertEqual(rows_of(work / "banks" / "default" / "mnemopi.db"), 2)
        self.assertEqual(rows_of(work / "mnemopi.db"), 3)

    def test_the_drill_notices_when_only_the_parent_database_changed(self):
        _, stamp = self.backup()
        con = sqlite3.connect(self.mnemopi / "mnemopi.db")
        con.execute("DELETE FROM working_memory")
        con.commit()
        con.close()
        ran = self.drill(stamp)
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertIn("DRIFT", ran.stdout)
        self.assertEqual(ran.stdout.count("same / same"), 1, ran.stdout)
        self.assertEqual(self.drill(stamp, "--strict").returncode, 1)

    def test_a_clash_on_the_reserved_name_fails_instead_of_replacing_an_earlier_copy(self):
        self.s.bank(PARENT_COPY, None, wm=(("c1", "clash", "bank"),))
        ran, stamp = self.backup()
        self.assertEqual(ran.returncode, 1, ran.stdout + ran.stderr)
        self.assertIn(PARENT_COPY, ran.stderr)
        self.assertEqual(rows_of(stamp / f"{PARENT_COPY}.db"), 1)  # the bank's own copy is still the one there

    def _old_backup(self, with_manifest: bool) -> Path:
        stamp = self.s.root / f"old-backup-{with_manifest}" / "20260101T000000Z"
        stamp.mkdir(parents=True)
        shutil.copy(self.mnemopi / "mnemopi.db", stamp / "default.db")  # what the earlier script made of the parent db
        digest = hashlib.sha256((stamp / "default.db").read_bytes()).hexdigest()
        (stamp / "SHA256SUMS").write_text(f"{digest}  default.db\n")
        if with_manifest:
            (stamp / "MANIFEST.tsv").write_text(
                f"bank\tsource\tbytes\tseconds\tintegrity\ndefault\t{self.mnemopi / 'mnemopi.db'}\t1\t0\tok\n"
            )
        return stamp

    def test_an_old_backup_whose_default_db_is_the_parent_database_still_restores_as_before(self):
        shutil.rmtree(self.s.banks / "default")  # an older machine: only the parent-level database exists
        for with_manifest in (True, False):
            with self.subTest(manifest=with_manifest):
                stamp = self._old_backup(with_manifest)
                scratch = self.s.root / f"scratch-{with_manifest}"
                ran = self.drill(stamp, "--scratch", str(scratch), "--keep")
                self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
                self.assertIn("RESULT: OK, identical to source", ran.stdout)
                [work] = list(scratch.iterdir())
                self.assertEqual(rows_of(work / "mnemopi.db"), 3)
                self.assertFalse((work / "banks" / "default").exists())


@unittest.skipUnless(SCRIPTS_RUN, SKIP)
class SqlQuotingTest(ScriptCase):
    EVIL = 'x"; DELETE FROM working_memory; --'

    def test_table_names_are_quoted_and_the_backup_is_only_read(self):
        db = self.s.bank("alpha-1", "work/a", wm=(("w1", "one", "bank"), ("w2", "two", "bank")))
        con = sqlite3.connect(db)
        con.execute('CREATE TABLE "x""; DELETE FROM working_memory; --" (a)')
        con.execute("CREATE TABLE x (a)")
        con.execute("INSERT INTO x VALUES (1)")
        try:
            con.execute('CREATE VIRTUAL TABLE "f""q" USING fts5(c)')
        except sqlite3.OperationalError:
            pass  # no FTS5 in this SQLite: the plain tables still cover the injection
        con.commit()
        con.close()
        ran, stamp = self.backup()
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        before = digest_tree(stamp)
        drill = self.drill(stamp)
        self.assertEqual(drill.returncode, 0, drill.stdout + drill.stderr)
        self.assertIn("RESULT: OK, identical to source", drill.stdout)
        self.assertEqual(digest_tree(stamp), before)  # the backup file was not opened for writing
        self.assertEqual(rows_of(stamp / "alpha-1.db"), 2)
        self.assertEqual(rows_of(db), 2)

    def test_paths_with_quotes_spaces_and_uri_characters_work(self):
        banks = self.s.root / 'it\'s a "tricky" #1 100%? place' / "banks"
        self.s.banks = banks  # the helper builds banks under self.s.banks
        db = self.s.bank("alpha-1", "work/a", wm=(("w1", "one", "bank"),))
        dest = self.s.root / "dest's dir"
        ran, stamp = self.backup(src=banks, dest=dest)
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertEqual(rows_of(stamp / "alpha-1.db"), 1)
        scratch = self.s.root / "scratch's parent"
        drill = self.drill(stamp, "--scratch", str(scratch), src=banks)
        self.assertEqual(drill.returncode, 0, drill.stdout + drill.stderr)
        self.assertIn("RESULT: OK, identical to source", drill.stdout)
        self.assertEqual(rows_of(db), 1)


def load_pages():
    source = os.environ.get("OMP_MEMORY_SOURCE_DIR")
    path = (Path(source) / "memory_ops" if source else VENDOR / "memory_ops") / "hindsight_pages.py"
    spec = importlib.util.spec_from_file_location("hindsight_pages_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StubPager:
    def __init__(self, rows):
        self.rows = rows

    def get(self, offset, limit):
        return {"items": self.rows[offset : offset + limit], "total": len(self.rows)}


class StubResponse:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


class StubOpener:
    def __init__(self, rows):
        self.rows = rows

    def open(self, request, timeout=None):
        import json
        from urllib.parse import parse_qs, urlsplit

        query = parse_qs(urlsplit(request.full_url).query)
        offset, limit = int(query["offset"][0]), int(query["limit"][0])
        body = {"items": self.rows[offset : offset + limit], "total": len(self.rows)}
        return StubResponse(json.dumps(body).encode())


def make_rows(tag: str, n: int = 3):
    return [{"id": f"{tag}{i}", "text": f"{tag} fact {i}"} for i in range(n)]


class HindsightPagesFilesTest(ScratchCase):
    def setUp(self):
        super().setUp()
        self.hp = load_pages()
        self.out = self.s.root / "export"
        self.out.mkdir(mode=0o700)
        self.victim = self.s.root / "victim.txt"
        self.victim.write_text("unrelated")
        self.old_umask = os.umask(0)
        self.addCleanup(os.umask, self.old_umask)

    def export(self, rows, stamp="20260101T000000Z"):
        return self.hp.export_bank(StubPager(rows), "b1", self.out, stamp, 2, 0)

    def test_a_symlink_where_the_partial_file_goes_is_refused_and_its_target_left_alone(self):
        (self.out / ".b1-20260101T000000Z.jsonl.partial").symlink_to(self.victim)
        with self.assertRaises((self.hp.ExportError, OSError)):
            self.export(make_rows("a"))
        self.assertEqual(self.victim.read_text(), "unrelated")
        self.assertFalse((self.out / "b1-20260101T000000Z.jsonl").exists())

    def test_an_existing_partial_file_is_refused_not_truncated(self):
        partial = self.out / ".b1-20260101T000000Z.jsonl.partial"
        partial.write_text("someone else is writing")
        with self.assertRaises((self.hp.ExportError, OSError)):
            self.export(make_rows("a"))
        self.assertEqual(partial.read_text(), "someone else is writing")

    def test_an_existing_export_is_never_overwritten(self):
        self.export(make_rows("a"))
        final = self.out / "b1-20260101T000000Z.jsonl"
        meta = self.out / "b1-20260101T000000Z.meta.json"
        kept = (final.read_bytes(), meta.read_bytes())
        with self.assertRaises((self.hp.ExportError, OSError)):
            self.export(make_rows("z"))
        self.assertEqual((final.read_bytes(), meta.read_bytes()), kept)
        self.assertFalse(list(self.out.glob(".*")))

    def test_a_symlink_where_the_meta_or_combined_file_goes_is_refused(self):
        self.export(make_rows("a"))
        meta = self.out / "b1-20260101T000000Z.meta.json"
        meta.unlink()
        meta.symlink_to(self.victim)
        (self.out / "b1-20260101T000000Z.jsonl").unlink()
        with self.assertRaises((self.hp.ExportError, OSError)):
            self.export(make_rows("a"))
        self.assertEqual(self.victim.read_text(), "unrelated")
        combined = self.out / "hindsight-export-20260101T000000Z.json"
        combined.symlink_to(self.victim)
        (self.out / "b1-20260101T000000Z.jsonl").write_text('{"id": "q"}\n')
        with self.assertRaises(OSError):
            self.hp.write_combined(self.out, "20260101T000000Z", ["b1"])
        self.assertEqual(self.victim.read_text(), "unrelated")

    def test_every_file_is_created_private_whatever_the_umask(self):
        self.export(make_rows("a"))
        self.hp.write_combined(self.out, "20260101T000000Z", ["b1"])
        files = [p for p in self.out.iterdir()]
        self.assertEqual(len(files), 3)
        for path in files:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600, path.name)

    def _main(self, rows):
        token = self.s.root / "token"
        token.write_text("t" * 40)
        token.chmod(0o600)
        code = self.hp.main(
            [
                "--api-url",
                "http://127.0.0.1:9",
                "--token-file",
                str(token),
                "--banks",
                "b1",
                "--out",
                str(self.out),
                "--pause",
                "0",
                "--page",
                "2",
            ],
            opener=StubOpener(rows),
        )
        return code

    def test_two_exports_in_the_same_second_do_not_overwrite_each_other(self):
        import datetime as real

        class Frozen(real.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 1, 1, 0, 0, 0, tzinfo=tz)

        original = self.hp.datetime
        self.hp.datetime = Frozen
        self.addCleanup(setattr, self.hp, "datetime", original)
        self.assertEqual(self._main(make_rows("first")), 0)
        first = {p.name: p.read_bytes() for p in self.out.iterdir()}
        self.assertEqual(self._main(make_rows("second")), 0)
        for name, content in first.items():
            self.assertEqual((self.out / name).read_bytes(), content, name)
        jsonl = sorted(self.out.glob("b1-*.jsonl"))
        self.assertEqual(len(jsonl), 2)
        self.assertEqual(len(list(self.out.glob("hindsight-export-*.json"))), 2)
        self.assertEqual(len(list(self.out.glob("b1-*.meta.json"))), 2)
        self.assertFalse(list(self.out.glob(".*")))

    def test_the_export_keeps_the_mode_of_an_existing_output_directory(self):
        shared = self.s.root / "shared"
        shared.mkdir()
        shared.chmod(0o755)
        self.out = shared
        self.assertEqual(self._main(make_rows("a")), 0)
        self.assertEqual(shared.stat().st_mode & 0o777, 0o755)
        fresh = self.s.root / "fresh" / "out"
        self.out = fresh
        self.assertEqual(self._main(make_rows("a")), 0)
        self.assertEqual(fresh.stat().st_mode & 0o777, 0o700)

    def test_a_token_file_readable_by_others_and_a_url_with_a_password_are_refused(self):
        token = self.s.root / "token"
        token.write_text("t" * 40)
        token.chmod(0o644)
        with self.assertRaises(ValueError) as caught:
            self.hp.read_token_file(token)
        self.assertNotIn("t" * 40, str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            self.hp.check_url("http://alice:s3cret@127.0.0.1:1")
        self.assertNotIn("s3cret", str(caught.exception))

    def test_the_default_opener_ignores_an_environment_proxy(self):
        with mock.patch.dict(
            os.environ, {"http_proxy": "http://proxy.invalid:3128", "HTTP_PROXY": "http://proxy.invalid:3128"}
        ):
            pager = self.hp.Pager("http://localhost:1", "t" * 40, "b", 5)
        proxies = [h for h in pager._opener.handlers if isinstance(h, urllib.request.ProxyHandler)]
        self.assertTrue(all(not h.proxies for h in proxies))


@unittest.skipUnless(SCRIPTS_RUN, SKIP)
class DirectoryModeAndHostTest(ScriptCase):
    def test_backup_keeps_the_mode_of_an_existing_destination_and_makes_a_new_one_private(self):
        self.s.bank("alpha-1", "work/a")
        shared = self.s.root / "Documents"
        shared.mkdir()
        shared.chmod(0o755)
        ran, _ = self.backup(dest=shared)
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertEqual(shared.stat().st_mode & 0o777, 0o755)
        ran, _ = self.backup(dest=self.s.root / "new" / "dest")
        self.assertEqual((self.s.root / "new" / "dest").stat().st_mode & 0o777, 0o700)

    def test_a_bank_name_that_is_not_plain_is_not_backed_up(self):
        self.s.bank("alpha-1", "work/a")
        self.s.bank("it's", "work/b")
        ran, stamp = self.backup()
        self.assertEqual(ran.returncode, 1, ran.stdout + ran.stderr)
        self.assertIn("not a usable bank name", ran.stderr)
        self.assertEqual(sorted(p.name for p in stamp.glob("*.db")), ["alpha-1.db"])

    def test_the_hindsight_export_script_keeps_an_existing_output_directory_mode(self):
        shared = self.s.root / "Documents"
        shared.mkdir()
        shared.chmod(0o755)
        token = self.s.root / "token"
        token.write_text("t" * 40)
        token.chmod(0o600)
        # port 9 on localhost refuses the connection: the export fails, after the directory was dealt with
        ran = self.script(
            "export_hindsight.sh",
            "--banks",
            "a",
            "--out",
            str(shared),
            "--api-url",
            "http://127.0.0.1:9",
            "--token-file",
            str(token),
        )
        self.assertEqual(ran.returncode, 1, ran.stdout + ran.stderr)
        self.assertEqual(shared.stat().st_mode & 0o777, 0o755)

    def test_an_ssh_host_that_starts_with_a_dash_is_refused(self):
        ran = self.script(
            "export_hindsight.sh",
            "--dry-run",
            "--banks",
            "a",
            "--out",
            str(self.s.root / "o"),
            "--mode",
            "bank",
            "--ssh-host",
            "-Jhost",
            "--remote-export-cmd",
            "c {bank}",
        )
        self.assertEqual(ran.returncode, 2, ran.stdout + ran.stderr)
        self.assertIn("bad ssh host", ran.stderr)


STUB_SSH = """#!/bin/sh
exit "${STUB_SSH_EXIT:-0}"
"""
STUB_SCP = """#!/bin/sh
# the last argument is where the archive goes
for last; do :; done
[ "${STUB_SCP_EXIT:-0}" = 0 ] || exit "$STUB_SCP_EXIT"
cp "$STUB_ARCHIVE" "$last"
"""
STUB_DATE = """#!/bin/sh
case "$*" in
*%Y%m%dT%H%M%SZ*) echo 20260101T000000Z ;;
*) exec /bin/date "$@" ;;
esac
"""
STAMP = "20260101T000000Z"


@unittest.skipUnless(SCRIPTS_RUN and shutil.which("unzip"), "needs sqlite3, shasum or sha256sum, and unzip")
class ArchiveExportTest(ScriptCase):
    """`--mode bank`: the archive made on another host is fetched with ssh and scp (stand-ins here) and published."""

    def setUp(self):
        super().setUp()
        self.stubs = self.s.root / "stubs"
        self.stubs.mkdir()
        for name, body in (("ssh", STUB_SSH), ("scp", STUB_SCP), ("date", STUB_DATE)):
            (self.stubs / name).write_text(body)
            (self.stubs / name).chmod(0o755)
        self.out = self.s.root / "export-out"
        self.out.mkdir(mode=0o700)
        self.victim = self.s.root / "victim.txt"
        self.victim.write_text("unrelated")
        import zipfile

        self.archive = self.s.root / "archive.zip"
        with zipfile.ZipFile(self.archive, "w") as zf:
            zf.writestr("bank.json", "{}")

    def export(self, **extra):
        env = {
            "PATH": f"{self.stubs}:{os.environ['PATH']}",
            "HOME": str(self.s.home),
            "TMPDIR": str(self.tmp),
            "STUB_ARCHIVE": str(self.archive),
            **extra,
        }
        return subprocess.run(
            [
                "bash",
                str(OPS / "export_hindsight.sh"),
                "--mode",
                "bank",
                "--banks",
                "b1",
                "--out",
                str(self.out),
                "--ssh-host",
                "stand-in",
                "--remote-export-cmd",
                "hs {bank} {out}",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def names(self):
        return sorted(p.name for p in self.out.iterdir())

    def test_repeating_the_export_in_one_second_keeps_both_archives_and_checksums(self):
        for _ in range(2):
            ran = self.export()
            self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertEqual(
            self.names(),
            sorted(
                [
                    f"export-b1-{STAMP}.zip",
                    f"export-b1-{STAMP}.zip.sha256",
                    f"export-b1-{STAMP}-2.zip",
                    f"export-b1-{STAMP}-2.zip.sha256",
                ]
            ),
        )
        for name in ("", "-2"):
            zip_path = self.out / f"export-b1-{STAMP}{name}.zip"
            digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
            self.assertEqual((self.out / f"{zip_path.name}.sha256").read_text(), f"{digest}  {zip_path.name}\n")
            self.assertEqual(zip_path.stat().st_mode & 0o777, 0o600)

    def test_an_existing_destination_file_is_not_overwritten(self):
        taken = self.out / f"export-b1-{STAMP}.zip"
        taken.write_text("an earlier export")
        ran = self.export()
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertEqual(taken.read_text(), "an earlier export")
        self.assertTrue((self.out / f"export-b1-{STAMP}-2.zip").is_file())

    def test_a_symlink_at_the_destination_or_the_checksum_name_is_not_followed(self):
        (self.out / f"export-b1-{STAMP}.zip").symlink_to(self.victim)
        (self.out / f"export-b1-{STAMP}-2.zip.sha256").symlink_to(self.victim)
        (self.out / f"export-b1-{STAMP}-3.zip").symlink_to(self.s.root / "dangling")
        ran = self.export()
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertEqual(self.victim.read_text(), "unrelated")
        self.assertFalse((self.s.root / "dangling").exists())
        self.assertTrue((self.out / f"export-b1-{STAMP}.zip").is_symlink())
        self.assertTrue((self.out / f"export-b1-{STAMP}-4.zip").is_file())
        self.assertFalse((self.out / f"export-b1-{STAMP}-2.zip").exists())  # its checksum name was taken

    def test_a_symlink_to_a_directory_at_the_destination_does_not_receive_the_archive(self):
        elsewhere = self.s.root / "elsewhere"
        elsewhere.mkdir()
        (self.out / f"export-b1-{STAMP}.zip").symlink_to(elsewhere)
        ran = self.export()
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_a_failed_transfer_or_a_bad_archive_deletes_nothing_it_did_not_create(self):
        taken = self.out / f"export-b1-{STAMP}.zip"
        taken.write_text("an earlier export")
        sum_link = self.out / f"export-b1-{STAMP}.zip.sha256"
        sum_link.symlink_to(self.victim)
        before = self.names()
        ran = self.export(STUB_SCP_EXIT="1")
        self.assertEqual(ran.returncode, 1, ran.stdout + ran.stderr)
        self.assertIn("FAIL remote export b1", ran.stderr)
        self.archive.write_bytes(b"not a zip")
        ran = self.export()
        self.assertEqual(ran.returncode, 1, ran.stdout + ran.stderr)
        self.assertIn("zip test failed", ran.stderr)
        self.assertEqual(taken.read_text(), "an earlier export")
        self.assertTrue(sum_link.is_symlink())
        self.assertEqual(self.victim.read_text(), "unrelated")
        self.assertEqual(self.names(), before)  # no work directory or partial file is left behind

    def test_the_work_directory_is_private_and_removed(self):
        ran = self.export()
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertFalse([n for n in self.names() if n.startswith(".")])
        self.assertEqual(self.out.stat().st_mode & 0o777, 0o700)


SECRET_PIN = "493817"
SECRET_BANK = "password-" + SECRET_PIN


@unittest.skipUnless(SCRIPTS_RUN, SKIP)
class SecretNamesInScriptsTest(ScriptCase):
    """A PIN assignment or an API key in a bank name or a path is printed as an index label by the scripts themselves."""

    def assertClean(self, ran, *needles):
        text = (ran.stdout + ran.stderr).lower()
        for needle in (SECRET_PIN, *needles):
            self.assertNotIn(needle.lower(), text)

    def test_backup_and_restore_drill_name_a_secret_bank_by_index(self):
        self.s.bank("plain-1abc", "work/plain")
        self.s.bank(SECRET_BANK, "work/other")
        ran, stamp = self.backup()
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertClean(ran)
        self.assertEqual(sorted(p.name for p in stamp.glob("*.db")), [f"{SECRET_BANK}.db", "plain-1abc.db"])
        drill = self.drill(stamp)
        self.assertEqual(drill.returncode, 0, drill.stdout + drill.stderr)
        self.assertClean(drill)
        self.assertIn("bank#1", drill.stdout)
        self.assertIn("plain-1abc", drill.stdout)

    def test_a_drift_line_and_a_kept_scratch_name_use_the_index(self):
        self.s.bank("plain-1abc", "work/plain")
        _, stamp = self.backup()
        self.s.bank(SECRET_BANK, "work/other")  # appears after the backup
        scratch = self.s.root / f"scratch-{SECRET_BANK}"
        drill = self.drill(stamp, "--scratch", str(scratch), "--keep")
        self.assertEqual(drill.returncode, 0, drill.stdout + drill.stderr)
        self.assertClean(drill)
        self.assertIn("DRIFT: bank bank#1 exists in source but not in the backup", drill.stdout)
        self.assertRegex(drill.stdout, r"scratch kept: folder#1")

    def test_a_bank_name_that_is_not_usable_is_reported_by_index(self):
        self.s.bank("plain-1abc", "work/plain")
        self.s.bank("password=" + SECRET_PIN, "work/other")
        ran, _ = self.backup()
        self.assertEqual(ran.returncode, 1, ran.stdout + ran.stderr)
        self.assertClean(ran)
        self.assertIn("bank#1: not a usable bank name", ran.stderr)

    def test_messages_that_echo_a_path_or_an_argument_name_it_by_index(self):
        for args in (
            ("backup_mnemopi.sh", "--dest", str(self.dest), "--src", str(self.s.root / f"x-{SECRET_BANK}"), "--bogus"),
            ("backup_mnemopi.sh", "--dest", str(self.dest), "--src", str(self.s.root / f"x-{SECRET_BANK}")),
            ("restore_drill.sh", "--backup", str(self.s.root), "--src", str(self.s.root / f"x-{SECRET_BANK}")),
            ("restore_drill.sh", "--backup", str(self.s.root / f"y-{SECRET_BANK}")),
            ("restore_drill.sh", f"--password={SECRET_PIN}"),
            ("export_hindsight.sh", f"--password={SECRET_PIN}"),
        ):
            with self.subTest(args=args[:1]):
                self.assertClean(self.script(*args))

    def test_the_export_script_plan_and_errors_name_banks_by_index(self):
        plan = self.script(
            "export_hindsight.sh",
            "--dry-run",
            "--banks",
            f"{SECRET_BANK} plain-1abc",
            "--out",
            str(self.s.root / f"o-{SECRET_BANK}"),
            "--overlay-dir",
            str(self.s.root / "ov"),
        )
        self.assertEqual(plan.returncode, 0, plan.stdout + plan.stderr)
        self.assertClean(plan)
        self.assertIn("banks=bank#1 plain-1abc", plan.stdout)
        bad = self.script(
            "export_hindsight.sh",
            "--dry-run",
            "--banks",
            f"password={SECRET_PIN}",
            "--out",
            str(self.s.root / "o"),
            "--overlay-dir",
            "x",
        )
        self.assertEqual(bad.returncode, 2)
        self.assertClean(bad)
        self.assertIn("bad bank name: bank#1", bad.stderr)

    def test_the_page_exporter_names_banks_and_paths_by_index(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout

        hp = load_pages()
        out, err = io.StringIO(), io.StringIO()
        token = self.s.root / "token"
        token.write_text("t" * 40)
        token.chmod(0o600)
        with redirect_stdout(out), redirect_stderr(err):
            code = hp.main(
                [
                    "--banks",
                    SECRET_BANK,
                    "--out",
                    str(self.s.root / f"o-{SECRET_BANK}"),
                    "--overlay-dir",
                    str(self.s.root / f"ov-{SECRET_BANK}"),
                    "--dry-run",
                ]
            )
            failed = hp.main(
                [
                    "--banks",
                    SECRET_BANK,
                    "--out",
                    str(self.s.root / "o"),
                    "--overlay-dir",
                    str(self.s.root / f"ov-{SECRET_BANK}"),
                    "--pause",
                    "0",
                ]
            )
            bad = hp.main(
                ["--banks", "a", "--out", str(self.s.root / "o"), "--overlay-dir", "x", f"--password={SECRET_PIN}"]
            )
        self.assertEqual((code, failed, bad), (0, 1, 2))
        self.assertNotIn(SECRET_PIN, out.getvalue() + err.getvalue())
        self.assertIn("banks=bank#1", out.getvalue())
        self.assertIn("FAIL bank#1:", err.getvalue())


class LabelsDirectoryTest(ScratchCase):
    def test_saving_a_label_keeps_the_mode_of_an_existing_directory(self):
        source = os.environ.get("OMP_MEMORY_SOURCE_DIR")
        root = Path(source) if source else VENDOR
        shared = self.s.root / "config"
        shared.mkdir()
        shared.chmod(0o755)
        code = (
            "import sys; from omp_hub.memory.folders import LabelStore; LabelStore(sys.argv[1]).set('bank-1', 'Work')"
        )
        env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.s.home),
            "PYTHONPATH": str(root),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        ran = subprocess.run(
            [sys.executable, "-c", code, str(shared / "labels.json")],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertEqual(shared.stat().st_mode & 0o777, 0o755)
        new = self.s.root / "new-config"
        ran = subprocess.run(
            [sys.executable, "-c", code, str(new / "labels.json")], env=env, capture_output=True, text=True, check=False
        )
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertEqual(new.stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
