"""omp-session-control in a real omp TUI: scratch HOME, fake model provider, scratch relay on a random port.

The plugin is installed the way a user installs it: from the repository's own marketplace catalog, with
`omp plugin marketplace add <repo>` and `omp plugin install`. Nothing touches the real account, the network or a
running service. Needs omp and Bun (OMP_BIN and BUN_BIN, or both on PATH); skipped otherwise. Takes about two minutes.

Run: python3 -m unittest discover -s plugins/omp-session-control/tests/integration -v
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
PLUGIN = ROOT / "plugins" / "omp-session-control"
sys.path.insert(0, str(ROOT / "tools" / "testkit"))

import omp_scratch as kit

OMP = kit.find_omp()
BUN = kit.find_bun()
IDLE_MS = 3000
SECOND_IDLE_MS = 5000
SUITE = PLUGIN / "tests" / "conformance" / "suite.ts"
NEEDS = unittest.skipUnless(OMP and BUN, "omp and bun are required (OMP_BIN, BUN_BIN)")


def control_dir(scratch: kit.ScratchHome) -> Path:
    return scratch.home / ".config" / "omp-session-control"


def registries(directory: Path, pid: int | None = None) -> list[dict]:
    out = []
    for f in directory.glob("*.json") if directory.exists() else []:
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if pid is None or data.get("pid") == pid:
            out.append(data)
    return out


def request(reg: dict, frame: dict | str) -> dict:
    """One request on a fresh connection, one reply."""
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(10)
        s.connect(reg["socket"])
        s.sendall((frame if isinstance(frame, str) else json.dumps(frame)).encode() + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf.split(b"\n")[0])


def conformance(directory: Path, pid: int, *flags: str) -> subprocess.CompletedProcess:
    assert BUN
    return subprocess.run(
        [BUN, "--no-install", str(SUITE), "--dir", str(directory), "--pid", str(pid), *flags],
        capture_output=True,
        text=True,
        cwd=PLUGIN,
        timeout=180,
    )


@NEEDS
class SessionControlInRealOmp(unittest.TestCase):
    relay: kit.ScratchRelay
    scratch: kit.ScratchHome
    omp: kit.PtyOmp
    install_log: str

    @classmethod
    def setUpClass(cls) -> None:
        assert BUN
        cls.relay = kit.ScratchRelay(BUN)
        cls.scratch = kit.ScratchHome(cls.relay.url)
        try:
            cls.install_log = cls.scratch.install_plugin(ROOT, "omp-session-control", "omp-tether")
            # The idle timeout comes from omp's plugin settings (no environment variable): the suite's idle check
            # below only passes when the extension read the stored value, because the default is 60 s.
            cls.scratch.cli("plugin", "config", "set", "omp-session-control", "idleTimeoutMs", str(IDLE_MS))
            cls.omp = kit.PtyOmp(cls.scratch)
            # A hosted session shows up in `omp collab list` once the (scratch) relay answered: the TUI is up.
            kit.wait_for(cls.omp.collab_hosts, timeout=90, what="the session in the Collab listing")
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        if getattr(cls, "omp", None):
            cls.omp.stop()
        if getattr(cls, "relay", None):
            cls.relay.stop()
        if getattr(cls, "scratch", None):
            cls.scratch.cleanup()

    # -- helpers ----------------------------------------------------------------------------------------------------

    @property
    def ctl_dir(self) -> Path:
        return control_dir(self.scratch)

    def reg(self) -> dict | None:
        regs = registries(self.ctl_dir, self.omp.pid)
        return regs[0] if len(regs) == 1 else None

    def op(self, op: str, args: dict | None = None) -> dict:
        reg = self.reg()
        assert reg, "no registry"
        reply = request(
            reg,
            {
                "v": 1,
                "id": "t",
                "nonce": reg["nonce"],
                "sessionId": reg["sessionId"],
                "generation": reg["generation"],
                "op": op,
                **({"args": args} if args is not None else {}),
            },
        )
        self.assertTrue(reply["ok"], reply)
        return reply["result"]

    def snapshot(self) -> dict:
        reg = self.reg()
        assert reg
        return request(reg, {"v": 1, "id": "s", "nonce": reg["nonce"], "op": "snapshot"})["result"]

    # -- tests, in order --------------------------------------------------------------------------------------------

    def test_00_installed_from_the_marketplace_and_inert(self) -> None:
        self.assertIn("Installed omp-session-control from omp-tether", self.install_log)
        time.sleep(3)  # long enough for session_start and a first reconcile
        self.assertTrue(self.omp.alive())
        self.assertFalse((self.scratch.home / ".config").exists(), "installing must not create anything outside ~/.omp")
        self.omp.type("/session-control status")
        kit.wait_for(lambda: "inert" in self.omp.text(), timeout=20, what="the status text")
        self.assertIn("omp-session-control 0.1.0", self.omp.text())
        self.assertFalse((self.scratch.home / ".config").exists(), "status must not create anything either")

    def test_01_on_command_enables_this_session(self) -> None:
        self.omp.type("/session-control on")
        reg = kit.wait_for(self.reg, timeout=30, what="the registry after /session-control on")
        self.assertEqual(stat.S_IMODE(self.ctl_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.ctl_dir / "enabled").stat().st_mode), 0o600)
        self.assertEqual(reg["pid"], self.omp.pid)
        self.assertEqual(reg["sessionId"], self.omp.collab_hosts()[0]["sessionId"])
        self.omp.type("/session-control status")
        kit.wait_for(lambda: "listening" in self.omp.text(), timeout=20, what="the listening status")

    def test_10_conformance_read_only_and_limits(self) -> None:
        r = conformance(self.ctl_dir, self.omp.pid, "--exclusive", "--idle-ms", str(IDLE_MS))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("0 failed", r.stdout)

    def test_20_stop_interrupts_a_running_tool_call(self) -> None:
        self.assertEqual(self.op("abort"), {"aborted": False})  # idle: nothing to stop
        self.omp.type("RUN_SLEEP please")
        # the `sleep` is not a separate process we can see, so the proof is in the session file
        kit.wait_for(
            lambda: any(
                e.get("customType") == "tool_execution_start" and e["data"].get("args", {}).get("command") == "sleep 60"
                for e in self.omp.session_entries()
            ),
            timeout=30,
            what="the sleep 60 tool call to start",
        )
        time.sleep(1)
        self.assertTrue(self.snapshot()["busy"])
        started = time.monotonic()
        self.assertEqual(self.op("abort"), {"aborted": True})
        kit.wait_for(lambda: self.snapshot()["busy"] is False, timeout=15, what="the turn to end")
        self.assertLess(time.monotonic() - started, 20)  # far less than the 60 s the tool would have run
        results = [
            e["message"] for e in self.omp.session_entries() if (e.get("message") or {}).get("role") == "toolResult"
        ]
        self.assertEqual(results[-1]["toolName"], "bash")
        self.assertTrue(results[-1]["isError"])
        self.assertEqual(results[-1]["content"][0]["text"], "Command aborted")
        self.assertTrue(self.omp.alive())

    def test_21_rename_changes_the_listing_and_the_session_file(self) -> None:
        result = self.op("rename", {"name": "  Rename  test ✓ "})
        self.assertEqual(result["name"], "Rename test ✓")
        kit.wait_for(
            lambda: (self.omp.collab_hosts() or [{}])[0].get("sessionName") == "Rename test ✓",
            timeout=20,
            what="omp's own listing to show the new name",
        )
        kit.wait_for(
            lambda: any(
                e.get("type") == "title_change" and e.get("title") == "Rename test ✓"
                for e in self.omp.session_entries()
            ),
            timeout=20,
            what="the title_change entry",
        )
        self.assertEqual(self.snapshot()["name"], "Rename test ✓")

    def test_30_conformance_mutations_and_off_switch(self) -> None:
        r = conformance(self.ctl_dir, self.omp.pid, "--mutate", "--off-switch")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(self.omp.alive())
        self.assertIsNotNone(kit.wait_for(self.reg, timeout=15, what="control to be back after the off switch"))

    def test_40_hostile_clients_do_not_hurt_the_session(self) -> None:
        reg = self.reg()
        assert reg
        for i in range(60):
            with socket.socket(socket.AF_UNIX) as s:
                s.connect(reg["socket"])
                s.sendall([b'{"v":1,"id":"x","nonce":"', b"\xff\xfe\x00\n", b"[" * 5000 + b"\n", b"{bad\n"][i % 4])
                if i % 2:
                    s.shutdown(socket.SHUT_RDWR)
        self.assertTrue(self.omp.alive())
        time.sleep(IDLE_MS / 1000 + 1)  # let abandoned connections time out
        self.assertEqual(self.snapshot()["pid"], self.omp.pid)
        self.assertTrue(self.omp.alive())

    def test_50_live_disabled_extensions_edit_closes_and_reopens_control(self) -> None:
        old = self.reg()
        assert old
        self.scratch.write_config("disabledExtensions:\n  - extension-module:omp-session-control\n")
        kit.wait_for(lambda: self.reg() is None, timeout=20, what="control to close after disabledExtensions")
        self.assertFalse(Path(old["socket"]).exists())
        self.scratch.write_config()
        new = kit.wait_for(self.reg, timeout=20, what="control to come back")
        self.assertNotEqual(new["nonce"], old["nonce"])

    def test_55_a_replaced_control_directory_is_picked_up_again(self) -> None:
        old = self.reg()
        assert old
        directory = self.ctl_dir
        # The session removes its own socket and registry the moment it sees them go, so it can unlink an entry between
        # rmtree listing it and deleting it. That is the extension behaving correctly, not an error: delete until gone.
        for _ in range(20):
            shutil.rmtree(directory, ignore_errors=True)
            if not directory.exists():
                break
        self.assertFalse(directory.exists())
        directory.mkdir(mode=0o700)
        (directory / "enabled").write_text("")
        new = kit.wait_for(self.reg, timeout=20, what="a new registry in the recreated directory")
        self.assertNotEqual(new["nonce"], old["nonce"])
        self.assertEqual(self.snapshot()["pid"], self.omp.pid)
        self.assertTrue(self.omp.alive())

    def test_60_a_second_session_removes_what_a_dead_one_left(self) -> None:
        directory = self.ctl_dir
        dead = subprocess.Popen(["/usr/bin/true"])
        dead.wait()
        stale_sock = directory / "deaddeaddeaddead.sock"
        with socket.socket(socket.AF_UNIX) as s:
            s.bind(str(stale_sock))
        stale_reg = directory / ("0" * 32 + ".json")
        stale_reg.write_text(json.dumps({"v": 1, "pid": dead.pid, "socket": str(stale_sock), "sessionId": "x"}))
        stale_reg.chmod(0o600)
        # the environment beats the stored setting
        second = kit.PtyOmp(self.scratch, {"OMP_SESSION_CONTROL_IDLE_MS": str(SECOND_IDLE_MS)})
        try:
            kit.wait_for(lambda: registries(directory, second.pid), timeout=60, what="the second session's registry")
            self.assertFalse(stale_reg.exists() or stale_sock.exists(), "the dead session's files are removed")
            self.assertEqual(len(registries(directory)), 2)
            r = conformance(directory, second.pid, "--idle-ms", str(SECOND_IDLE_MS))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        finally:
            second.stop()
        # a clean exit removes only that session's files
        kit.wait_for(
            lambda: not registries(directory, second.pid), timeout=15, what="the second session's registry to go"
        )
        self.assertEqual([r["pid"] for r in registries(directory)], [self.omp.pid])

    def test_90_exit_closes_the_socket_and_removes_the_registry(self) -> None:
        reg = self.reg()
        assert reg
        self.omp.stop()
        self.assertFalse(self.omp.alive())
        self.assertEqual(registries(self.ctl_dir), [])
        self.assertFalse(Path(reg["socket"]).exists())
        with socket.socket(socket.AF_UNIX) as s, self.assertRaises(OSError):
            s.connect(reg["socket"])
        log = (self.ctl_dir / "events.log").read_text()
        self.assertIn('"kind":"listening"', log)
        self.assertIn('"kind":"closed"', log)
        self.assertNotIn(str(self.scratch.home), log)  # the log holds kinds, pids and session ids, no paths
        self.assertEqual(stat.S_IMODE((self.ctl_dir / "events.log").stat().st_mode), 0o600)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    unittest.main()
