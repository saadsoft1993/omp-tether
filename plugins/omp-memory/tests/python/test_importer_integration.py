"""The real bundled importer, reached through the launcher, against scratch banks built with the published Mnemopi library.

Needs Bun 1.4.4 or newer; skipped with the reason otherwise. The library is `@oh-my-pi/pi-mnemopi` from npm at a fixed
version, installed once by the importer's own `setup` into `$OMP_MEMORY_TEST_LIB_DIR` (default
`<tmp>/omp-memory-test-lib/<version>`), which needs the network that one time; without it these tests are skipped.
Nothing is embedded (the writes here are refused before any row is built), so no embedding model is needed.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import ClassVar

import support  # noqa: F401  (puts the plugin's python/ and vendor/ directories on sys.path)
from omp_memory import config as config_module
from omp_memory import runtime
from support import PLUGIN, ScratchCase

HAVE_HOLDER_CHECK = bool(shutil.which("lsof")) or Path("/usr/sbin/lsof").exists() or sys.platform.startswith("linux")
VERSION = os.environ.get("OMP_MEMORY_TEST_VERSION", "18.18.3")
IMPORTER = PLUGIN / "vendor" / "memory_import" / "cli.ts"


def library_root() -> Path:
    return Path(
        os.environ.get("OMP_MEMORY_TEST_LIB_DIR") or Path(tempfile.gettempdir()) / "omp-memory-test-lib" / VERSION
    )


def find_prerequisites():
    """(bun path, library root) or None plus the reason it is missing."""
    try:
        bun = runtime.find_bun(config_module.Config()).path
    except Exception as err:
        return None, f"no Bun 1.4.4 or newer: {err}"
    root = library_root()
    package = root / "node_modules" / "@oh-my-pi" / "pi-mnemopi" / "package.json"
    try:
        installed = json.loads(package.read_text())["version"]
    except (OSError, ValueError, KeyError):
        installed = None
    if installed != VERSION:
        ran = subprocess.run(
            [str(bun), str(IMPORTER), "setup", "--lib-dir", str(root), "--omp-version", VERSION, "--no-embeddings"],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "HOME": tempfile.gettempdir()},
        )
        if ran.returncode != 0:
            return (
                None,
                f"the library {VERSION} is not installed in {root} and setup failed (no network?): {ran.stderr[-200:]}",
            )
    return (bun, root), ""


PREREQ, WHY = find_prerequisites()

HOLDER = (
    "import sqlite3, sys, time\n"
    "db = sqlite3.connect(sys.argv[1])\n"
    "db.execute('SELECT count(*) FROM working_memory').fetchone()\n"
    "print('ready', flush=True)\n"
    "time.sleep(120)\n"
)


@unittest.skipUnless(PREREQ, WHY)
class RealImporterTest(ScratchCase):
    def setUp(self):
        super().setUp()
        bun, root = PREREQ
        self.bun, self.root_lib = bun, root
        # the stand-in omp prints the library's version; the library comes from the scratch install root
        self.env = self.s.env(
            OMP_MEMORY_BUN=str(bun),
            OMP_MEMORY_LIB_DIR=str(root),
            OMP_BIN=str(self.s.fake_omp),
            FAKE_OMP_VERSION=VERSION,
        )
        # a folder with Claude Code memory, and the bank omp would use for it (named by the importer's own bank id code)
        self.folder = self.s.home / "work" / "alpha"
        self.folder.mkdir(parents=True)
        self.folder = self.folder.resolve()
        project = self.s.home / ".claude" / "projects" / str(self.folder).replace("/", "-").replace(".", "-")
        (project / "memory").mkdir(parents=True)
        (project / "11111111-1111-4111-8111-111111111111.jsonl").write_text(
            f'{{"type":"user","cwd":"{self.folder}"}}\n'
        )
        (project / "memory" / "one.md").write_text(
            "---\nname: one\ndescription: d\ntype: project\n---\n\nA plain note.\n"
        )
        probe = f'import {{ projectBankSegment }} from "{IMPORTER.parent}/bank_id.ts"; console.log(projectBankSegment(process.argv[1]));'
        ran = subprocess.run(
            [str(bun), "-e", probe, str(self.folder)], capture_output=True, text=True, env=self.env, check=False
        )
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.bank = ran.stdout.strip()
        self.db = self.s.bank(self.bank, "work/alpha")
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE memory_embeddings SET model = 'BAAI/bge-base-en-v1.5'")
        conn.commit()
        conn.close()

    def count(self):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT COUNT(*) FROM working_memory").fetchone()[0]
        finally:
            conn.close()

    def import_apply(self, *extra):
        # the importer's own guards are under test here, so the wrapper's backup and dry-run requirements are skipped
        return self.s.run(
            "import",
            "import",
            "--source",
            "claude-memory",
            "--apply",
            "--no-backup",
            "--skip-dry-run-check",
            *extra,
            env=self.env,
        )

    def test_a_bank_that_another_process_has_open_is_refused_and_left_alone(self):
        if not HAVE_HOLDER_CHECK:
            self.skipTest("neither lsof nor /proc is available")
        holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(self.db)], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "ready")
            before = self.count()
            result = self.import_apply()
            self.assertEqual(result.code, 1, result.out + result.err)  # a refusal
            self.assertIn(f"bank {self.bank}: open in process {holder.pid}", result.err)
            self.assertEqual(self.count(), before)
            # undo --apply is a write path too
            undo = self.s.run("import", "undo", "--run", "imp-none", "--apply", env=self.env)
            self.assertNotEqual(undo.code, 0)
            # the read-only plan is not blocked by the open bank
            plan = self.s.run("import", "dry-run", "--source", "claude-memory", env=self.env)
            self.assertEqual(plan.code, 0, plan.err)
            self.assertEqual(plan.json()["counts"]["imported"], 0)
        finally:
            holder.kill()
            holder.wait()
            holder.stdout.close()
        time.sleep(0.2)

    def test_import_without_apply_never_writes_even_with_every_other_flag(self):
        before = self.count()
        result = self.s.run("import", "import", "--source", "claude-memory", "--banks", self.bank, env=self.env)
        self.assertEqual(result.code, 2)
        self.assertEqual(self.count(), before)
        self.assertFalse((self.s.home / ".config" / "omp-memory-import" / "ledger.json").exists())

    def bank_bytes(self):
        """{relative path: sha256} of every file under the banks directory, SQLite's -shm and -wal side files left out."""
        import hashlib

        found = {}
        for path in sorted(self.s.banks.rglob("*")):
            if path.is_file() and not path.name.endswith(("-shm", "-wal")):
                found[str(path.relative_to(self.s.banks))] = hashlib.sha256(path.read_bytes()).hexdigest()
        return found

    def test_no_command_without_apply_changes_a_byte_of_a_bank(self):
        before = self.bank_bytes()
        self.assertIn(f"{self.bank}/mnemopi.db", before)
        commands = [
            ("banks",),
            ("sources",),
            ("doctor", "--deep"),
            ("backup",),
            ("import", "scan", "--source", "claude-memory"),
            ("import", "dry-run", "--source", "claude-memory"),
            ("import", "dry-run", "--source", "claude-memory", "--only-new-banks"),
            ("import", "verify"),
            ("import", "doctor"),
            ("import", "undo", "--run", "imp-none"),
            ("import", "import", "--source", "claude-memory"),
            ("import", "import", "--source", "claude-memory", "--no-backup", "--skip-dry-run-check"),
            ("import", "undo", "--run", "imp-none", "--no-backup"),
        ]
        for args in commands:
            with self.subTest(args=args):
                self.s.run(*args, env=self.env)
                self.assertEqual(self.bank_bytes(), before, args)
        # the dry run recorded its own state and the importer's ledger was never created
        self.assertFalse((self.s.home / ".config" / "omp-memory-import" / "ledger.json").exists())
        # an apply that the gates refuse leaves the banks as they were as well
        refused = self.s.run("import", "import", "--source", "claude-memory", "--apply", env=self.env)
        self.assertEqual(refused.code, 1, refused.err)
        self.assertIn("no verified backup", refused.err)  # the dry run above is recorded; the backup is what is missing
        self.assertEqual(self.bank_bytes(), before)

    def test_the_importers_own_doctor_and_scan_run_through_the_launcher(self):
        scan = self.s.run("import", "scan", "--source", "claude-memory", env=self.env)
        self.assertEqual(scan.code, 0, scan.err)
        self.assertEqual(scan.json()["counts"]["found"], 1)
        doctor = self.s.run("import", "doctor", env=self.env)
        self.assertIn(doctor.code, (0, 1))
        report = doctor.json()
        self.assertTrue(report["library"]["ok"], report["library"]["reasons"])
        self.assertEqual(report["library"]["libraryVersion"], VERSION)
        self.assertEqual(report["library"]["libraryKind"], "explicit")
        self.assertEqual(report["bun"]["version"], ".".join(map(str, runtime.find_bun(config_module.Config()).version)))
        # the bank id the importer computes locally is the bank name this scratch bank was given
        mine = [b for b in report["banks"] if b["bank"] == self.bank]
        self.assertEqual([b["bankIdMatches"] for b in mine], [True])

    def test_a_different_omp_version_is_refused_with_the_setup_command_and_nothing_is_written(self):
        before = self.count()
        env = {**self.env, "FAKE_OMP_VERSION": "99.1.0"}
        result = self.s.run("import", "dry-run", "--source", "claude-memory", env=env)
        self.assertEqual(result.code, 1, result.out)  # a refusal
        self.assertIn("no @oh-my-pi/pi-mnemopi@99.1.0", result.err)
        self.assertIn("Run `omp-memory setup`", result.err)
        self.assertEqual(self.count(), before)

    def test_library_command_reports_where_it_looked(self):
        r = self.s.run("doctor", "--json", env=self.env)
        checks = {c["name"]: c for c in r.json()["checks"]}
        # nobody has run the write test for this library yet: plain doctor must not say OK, and it writes nothing
        self.assertEqual(checks["library"]["status"], "warn", checks["library"])
        self.assertIn("not yet verified", checks["library"]["detail"])
        self.assertIn(str(self.root_lib), checks["library"]["detail"])
        self.assertFalse((self.s.home / ".cache" / "omp-memory").exists())
        deep = self.s.run("doctor", "--deep", "--json", env=self.env)
        self.assertEqual(deep.code, 0, deep.out + deep.err)
        document = deep.json()  # ONE valid JSON document, not two
        self.assertTrue(document["deep"]["selfTest"]["ok"])
        same_run = {c["name"]: c for c in document["checks"]}["library"]
        self.assertEqual(same_run["status"], "ok", same_run)  # the summary reflects the test this very run made
        self.assertIn("self-test passed", same_run["detail"])
        again = {c["name"]: c for c in self.s.run("doctor", "--json", env=self.env).json()["checks"]}
        self.assertEqual(again["library"]["status"], "ok", again["library"])
        self.assertIn("self-test passed", again["library"]["detail"])
        empty = self.s.run("doctor", "--json", env={**self.env, "OMP_MEMORY_LIB_DIR": str(self.s.root / "none")})
        library = {c["name"]: c for c in empty.json()["checks"]}["library"]
        self.assertEqual(library["status"], "fail")
        self.assertIn("omp-memory setup", library["fix"])


class RegistryHandler(http.server.BaseHTTPRequestHandler):
    seen: ClassVar[list[str]] = []

    def do_GET(self):
        RegistryHandler.seen.append(self.path)
        self.send_response(404)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


@unittest.skipUnless(PREREQ, WHY)
class SetupTest(ScratchCase):
    def setUp(self):
        super().setUp()
        self.env = self.s.env(OMP_MEMORY_BUN=str(PREREQ[0]), OMP_BIN=str(self.s.fake_omp), FAKE_OMP_VERSION="9.9.9")
        self.cache = self.s.home / ".cache" / "omp-memory" / "lib" / "9.9.9"

    def test_dry_run_prints_what_it_would_fetch_and_uses_no_network(self):
        r = self.s.run("setup", "--dry-run", env={**self.env, "OMP_MEMORY_NPM_REGISTRY": "http://127.0.0.1:1"})
        self.assertEqual(r.code, 0, r.err)
        self.assertIn("omp-memory setup: omp 9.9.9", r.err)
        self.assertIn("will fetch from http://127.0.0.1:1: @oh-my-pi/pi-mnemopi@9.9.9", r.err)
        self.assertIn("into ~/.cache/omp-memory/lib/9.9.9", r.err)  # shown under ~
        self.assertNotIn(str(self.s.home), r.err)
        self.assertIn("dry run: nothing was fetched or written", r.err)
        self.assertFalse(self.cache.exists())

    def test_a_version_npm_does_not_have_is_refused_and_installs_nothing(self):
        RegistryHandler.seen = []
        server = http.server.HTTPServer(("127.0.0.1", 0), RegistryHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            r = self.s.run("setup", "--registry", f"http://127.0.0.1:{server.server_port}", env=self.env)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(r.code, 1, r.err)  # a refusal
        self.assertIn("@oh-my-pi/pi-mnemopi@9.9.9 does not exist", r.err)
        self.assertIn("nothing was installed", r.err)
        self.assertEqual(RegistryHandler.seen, ["/@oh-my-pi%2fpi-mnemopi/9.9.9"])
        self.assertFalse(self.cache.exists())
        self.assertFalse((self.s.home / ".cache" / "omp-memory").exists())

    def test_without_an_omp_it_says_how_to_name_a_version(self):
        env = {k: v for k, v in self.env.items() if k != "OMP_BIN"}
        env["PATH"] = "/usr/bin:/bin"
        r = self.s.run("setup", "--dry-run", env=env)
        self.assertEqual(r.code, 2)
        self.assertIn("--omp-version", r.err)

    def test_stray_arguments_are_refused_before_anything_runs(self):
        self.s.bun_log.write_text("untouched")
        r = self.s.run("setup", "--install-everything", env=self.env)
        self.assertEqual(r.code, 2)
        self.assertIn("unexpected argument", r.err)


if __name__ == "__main__":
    unittest.main()
