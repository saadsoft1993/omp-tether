"""Tests for the extension-load gate, with a stand-in omp: the gate must fail closed when omp misbehaves.

The stand-in understands just enough of omp's command line (marketplace add, install, uninstall, a headless run and an
RPC session) and misbehaves in the way each test writes into it. Standard library only; Python 3.10 or newer.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
GATE = HERE / "gate.py"

FAKE_OMP = r"""#!@PYTHON@
import json, os, sys, time
home = Path = None
from pathlib import Path
home = Path(os.environ["HOME"]) / ".omp" / "plugins"
mode = "@MODE@"
args = sys.argv[1:]
def state(): return (home / "node_modules" / "p1").exists()
if args[:1] == ["--version"]:
    print("omp/0.0.0"); sys.exit(0)
if args[:2] == ["plugin", "marketplace"]:
    print("added"); sys.exit(0)
if args[:2] == ["plugin", "install"]:
    (home / "node_modules").mkdir(parents=True, exist_ok=True)
    (home / "node_modules" / "p1").write_text("")
    (home / "installed_plugins.json").write_text(json.dumps({"p1@mkt": {}}))
    (home / "omp-plugins.lock.json").write_text(json.dumps({"plugins": {"p1": {}}}))
    print("Installed p1"); sys.exit(0)
if args[:2] == ["plugin", "uninstall"]:
    (home / "node_modules" / "p1").unlink()
    (home / "installed_plugins.json").write_text("{}")
    (home / "omp-plugins.lock.json").write_text(json.dumps({"plugins": {}}))
    print("Uninstalled"); sys.exit(0)
if "-p" in args:
    print("test reply 1"); sys.exit(0)
if "--mode" in args:
    if mode == "hang_rpc":
        sys.stdout.write("no newline here"); sys.stdout.flush(); time.sleep(300)
    if mode == "silent_rpc_after_uninstall" and not state():
        sys.exit(1)
    if mode == "stderr_flood":
        sys.stderr.write("x" * 5_000_000); sys.stderr.flush()
    line = sys.stdin.readline()
    req = json.loads(line)
    cmds = [{"name": "session", "source": "builtin"}]
    if state():
        cmds.append({"name": "session-control", "source": "extension"})
    print(json.dumps({"id": req["id"], "type": "response", "success": True, "data": {"commands": cmds}}), flush=True)
    time.sleep(2)
    sys.exit(0)
sys.exit(0)
"""


class GateWithFakeOmp(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="olgt-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.omp = self.tmp / "omp"
        repo = self.tmp / "repo"
        (repo / ".omp-plugin").mkdir(parents=True)
        (repo / "plugins" / "p1").mkdir(parents=True)
        (repo / "plugins" / "p1" / "p1.ts").write_text("export default function () {}\n")
        (repo / "plugins" / "p1" / "package.json").write_text(
            json.dumps({"name": "p1", "version": "0.0.1", "omp": {"extensions": ["./p1.ts"]}})
        )
        (repo / ".omp-plugin" / "marketplace.json").write_text(
            json.dumps({"name": "mkt", "owner": {"name": "t"}, "plugins": [{"name": "p1", "source": "./plugins/p1"}]})
        )
        self.repo = repo
        self.expect = self.tmp / "expect.json"
        self.expect.write_text(json.dumps({"p1": {"commands": ["session-control"]}}))

    def run_gate(self, mode: str, limit: float = 60) -> tuple[subprocess.CompletedProcess | None, float]:
        self.omp.write_text(FAKE_OMP.replace("@MODE@", mode).replace("@PYTHON@", sys.executable))
        self.omp.chmod(0o755)
        env = {**os.environ, "OMP_BIN": str(self.omp), "GATE_RPC_TIMEOUT": "3"}
        started = time.monotonic()
        try:
            r = subprocess.run(
                [sys.executable, str(GATE), "--repo", str(self.repo), "--expectations", str(self.expect)],
                capture_output=True,
                text=True,
                env=env,
                timeout=limit,
            )
        except subprocess.TimeoutExpired:
            return None, time.monotonic() - started
        return r, time.monotonic() - started

    def test_a_well_behaved_omp_passes(self) -> None:
        r, _ = self.run_gate("good")
        assert r is not None
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_an_rpc_session_that_never_ends_a_line_fails_within_the_deadline(self) -> None:
        r, took = self.run_gate("hang_rpc", limit=45)
        self.assertIsNotNone(r, "the gate hung: no deadline on reading the RPC session")
        assert r is not None
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertLess(took, 40)

    def test_a_flood_on_stderr_does_not_deadlock_the_gate(self) -> None:
        r, _ = self.run_gate("stderr_flood", limit=45)
        self.assertIsNotNone(r, "the gate hung while omp filled its stderr pipe")
        assert r is not None
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_an_rpc_session_that_dies_after_uninstall_is_not_proof_of_removal(self) -> None:
        r, _ = self.run_gate("silent_rpc_after_uninstall", limit=45)
        assert r is not None
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("after uninstall", r.stdout + r.stderr)


class PlainManifest(unittest.TestCase):
    """A plugin declared with {"extensions": false} must really ship no extension, and ship a runnable command."""

    def make(self, package: dict, bin_mode: int | None = 0o755) -> Path:
        root = Path(tempfile.mkdtemp(prefix="olgp-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        if bin_mode is not None:
            (root / "bin").mkdir()
            (root / "bin" / "tool").write_text("#!/bin/sh\n")
            (root / "bin" / "tool").chmod(bin_mode)
        (root / "package.json").write_text(json.dumps({"name": "p", "version": "0.0.1", **package}))
        return root

    def test_a_plugin_with_an_executable_bin_and_no_extension_passes(self) -> None:
        sys.path.insert(0, str(HERE))
        import gate

        files = gate.check_plain_manifest(self.make({"bin": {"tool": "./bin/tool"}}))
        self.assertEqual(files, ["bin ./bin/tool"])
        self.assertEqual(gate.check_plain_manifest(self.make({"bin": "./bin/tool"})), ["bin ./bin/tool"])

    def test_each_way_of_not_being_one_is_refused(self) -> None:
        sys.path.insert(0, str(HERE))
        import gate

        cases = {
            "declares an extension": self.make({"bin": {"t": "./bin/tool"}, "omp": {"extensions": ["./x.ts"]}}),
            "ships no bin": self.make({}, bin_mode=None),
            "bin file missing": self.make({"bin": {"t": "./bin/nope"}}),
            "bin not executable": self.make({"bin": {"t": "./bin/tool"}}, bin_mode=0o644),
        }
        for name, root in cases.items():
            with self.subTest(name), self.assertRaises(gate.GateError):
                gate.check_plain_manifest(root)


if __name__ == "__main__":
    unittest.main()
