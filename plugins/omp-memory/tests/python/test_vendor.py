"""The vendored copies (vendor/) against the source tree they came from, and against the checksums
recorded when they were copied.

The comparison with the checkout is skipped, with the reason, when $OMP_MEMORY_SOURCE_DIR does not name one. The recorded
checksums are always checked: they catch an edit of a vendored file that was not made upstream.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support import PLUGIN, ScratchCase

SYNC = PLUGIN / "scripts" / "vendor_sync.py"
spec = importlib.util.spec_from_file_location("vendor_sync", SYNC)
vendor_sync = importlib.util.module_from_spec(spec)
sys.modules["vendor_sync"] = vendor_sync
spec.loader.exec_module(vendor_sync)

SOURCE = vendor_sync.default_source()
NO_SOURCE = (
    f"the source tree is not at {SOURCE}"
    if SOURCE
    else "OMP_MEMORY_SOURCE_DIR is not set (set it to the source tree to compare the vendored copies with it)"
)


class RecordedChecksumsTest(unittest.TestCase):
    def test_every_vendored_file_matches_its_recorded_checksum(self):
        record = vendor_sync.read_record()
        self.assertTrue(record, "vendor/SOURCE.sha256 is missing or empty: run scripts/sync-vendor.sh")
        for rel, expected in record.items():
            actual = hashlib.sha256((vendor_sync.VENDOR / rel).read_bytes()).hexdigest()
            self.assertEqual(
                actual, expected, f"{rel} was edited after it was vendored; change it upstream and re-sync"
            )

    def test_no_file_is_vendored_without_a_record(self):
        record = vendor_sync.read_record()
        self.assertEqual(sorted(vendor_sync.vendored_files()), sorted(record))

    def test_every_record_line_comes_from_the_manifest(self):
        entries = vendor_sync.parse_manifest(vendor_sync.MANIFEST.read_text())
        listed = {rel for kind, rel, _ in entries if kind == "file"}
        trees = [rel for kind, rel, _ in entries if kind == "tree"]
        for rel in vendor_sync.read_record():
            self.assertTrue(rel in listed or any(rel.startswith(t + "/") for t in trees), rel)


class SourceDriftTest(unittest.TestCase):
    @unittest.skipUnless(SOURCE is not None and SOURCE.is_dir(), NO_SOURCE)
    def test_vendored_copies_match_the_source(self):
        problems = vendor_sync.compare(SOURCE)
        self.assertEqual(
            problems, [], "vendor/ drifted from the source; run scripts/sync-vendor.sh and commit the result"
        )

    @unittest.skipUnless(SOURCE is not None and SOURCE.is_dir(), NO_SOURCE)
    def test_the_shell_wrapper_checks_too(self):
        ran = subprocess.run(
            [str(PLUGIN / "scripts" / "sync-vendor.sh"), "--check"], capture_output=True, text=True, check=False
        )
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertIn("matches the source", ran.stdout)


class DriftIsDetectedTest(unittest.TestCase):
    """Run against a small fake source checkout and a copy of vendor/, so the check itself is tested without the real one."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ompmem-vendor-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.source = self.root / "source"
        (self.source / "memory_import" / "test").mkdir(parents=True)
        (self.source / "memory_import" / "a.ts").write_text("a\n")
        (self.source / "memory_import" / "test" / "t.ts").write_text("t\n")
        (self.source / "memory_import" / "tsconfig.json").write_text("{}")
        (self.source / "memory_ops").mkdir()
        (self.source / "memory_ops" / "run.sh").write_text("#!/bin/sh\n")
        (self.source / "memory_ops" / "run.sh").chmod(0o755)
        self.plugin = self.root / "plugin"
        (self.plugin / "scripts").mkdir(parents=True)
        (self.plugin / "scripts" / "vendor_manifest.txt").write_text(
            "tree memory_import exclude tsconfig.json\nfile memory_ops/run.sh\n"
        )
        shutil.copy(SYNC, self.plugin / "scripts" / "vendor_sync.py")
        self.sync()

    def sync(self, *args):
        env = {**os.environ, "OMP_MEMORY_SOURCE_DIR": str(self.source)}
        return subprocess.run(
            [sys.executable, str(self.plugin / "scripts" / "vendor_sync.py"), *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def check(self):
        return self.sync("--check")

    def test_a_fresh_sync_is_clean_and_keeps_the_executable_bit(self):
        self.assertEqual(self.check().returncode, 0, self.check().stdout)
        self.assertTrue(os.access(self.plugin / "vendor" / "memory_ops" / "run.sh", os.X_OK))
        self.assertFalse((self.plugin / "vendor" / "memory_import" / "tsconfig.json").exists())

    def test_a_changed_source_file_is_drift(self):
        (self.source / "memory_import" / "a.ts").write_text("changed\n")
        ran = self.check()
        self.assertEqual(ran.returncode, 1)
        self.assertIn("memory_import/a.ts: vendored copy differs", ran.stdout)

    def test_a_new_source_file_is_drift_until_synced(self):
        (self.source / "memory_import" / "b.ts").write_text("b\n")
        ran = self.check()
        self.assertEqual(ran.returncode, 1)
        self.assertIn("memory_import/b.ts: not vendored yet", ran.stdout)
        self.assertEqual(self.sync().returncode, 0)
        self.assertEqual(self.check().returncode, 0)

    def test_a_local_edit_of_a_vendored_file_is_drift(self):
        (self.plugin / "vendor" / "memory_import" / "a.ts").write_text("local edit\n")
        self.assertEqual(self.check().returncode, 1)
        self.assertEqual(self.sync().returncode, 0)  # a sync overwrites the edit
        self.assertEqual((self.plugin / "vendor" / "memory_import" / "a.ts").read_text(), "a\n")

    def test_a_file_removed_from_the_source_is_removed_by_sync(self):
        (self.source / "memory_import" / "test" / "t.ts").unlink()
        self.assertIn("vendored but no longer selected", self.check().stdout)
        self.sync()
        self.assertFalse((self.plugin / "vendor" / "memory_import" / "test").exists())

    def test_a_stray_file_in_vendor_is_drift(self):
        (self.plugin / "vendor" / "extra.txt").write_text("x")
        self.assertIn("extra.txt: vendored but no longer selected", self.check().stdout)

    def test_no_source_named_is_a_clear_error_not_a_pass(self):
        env = {key: value for key, value in os.environ.items() if key != "OMP_MEMORY_SOURCE_DIR"}
        ran = subprocess.run(
            [sys.executable, str(self.plugin / "scripts" / "vendor_sync.py"), "--check"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(ran.returncode, 2)
        self.assertIn("no source checkout given", ran.stderr)
        self.assertIn("OMP_MEMORY_SOURCE_DIR", ran.stderr)

    def test_a_missing_source_is_a_clear_error_not_a_pass(self):
        ran = subprocess.run(
            [
                sys.executable,
                str(self.plugin / "scripts" / "vendor_sync.py"),
                "--check",
                "--source",
                str(self.root / "nope"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(ran.returncode, 2)
        self.assertIn("source checkout not found", ran.stderr)

    def test_manifest_paths_cannot_leave_the_source(self):
        with self.assertRaises(ValueError):
            vendor_sync.parse_manifest("file ../secret\n")
        with self.assertRaises(ValueError):
            vendor_sync.parse_manifest("file /etc/passwd\n")
        with self.assertRaises(ValueError):
            vendor_sync.parse_manifest("copy a b\n")


class StandaloneTest(ScratchCase):
    def test_the_plugin_reads_banks_through_its_own_copies_not_through_a_hub(self):
        from omp_memory import core

        for name in ("mnemopi", "folders", "sources", "instructions"):
            module = sys.modules[f"omp_hub.memory.{name}"]
            self.assertTrue(Path(module.__file__).resolve().is_relative_to((PLUGIN / "vendor").resolve()), name)
        self.assertTrue(core.VENDOR.resolve() == (PLUGIN / "vendor").resolve())

    def test_vendored_python_modules_import_only_each_other(self):
        """The hub's memory modules must not reach for the hub (serve, control, service...)."""
        for path in (PLUGIN / "vendor" / "omp_hub" / "memory").glob("*.py"):
            for line in path.read_text().splitlines():
                stripped = line.strip()
                if stripped.startswith(
                    ("from omp_hub", "import omp_hub", "from memory", "from hub", "import hub", "import serve")
                ):
                    self.fail(f"{path.name}: {stripped}")
                if stripped.startswith("from .") and not stripped.startswith(
                    (
                        "from . import graph",
                        "from .folders",
                        "from .model",
                        "from .graph",
                        "from .instructions",
                        "from .mnemopi",
                        "from .sources",
                    )
                ):
                    self.fail(f"{path.name}: unexpected relative import: {stripped}")


if __name__ == "__main__":
    unittest.main()
