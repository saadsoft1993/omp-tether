"""Kill and Force kill: every identity check, in order, with nothing signalled when one fails; and a real process."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from omp_dashboard.control import ControlError
from omp_dashboard.hostplat import Platform, ProcessInfo
from omp_dashboard.kill import FORCE_WINDOW_S, Killer, confirm_matches, target_problem
from support import host_entry

UID = os.getuid()


class FakePlatform:
    kind = "linux"

    def __init__(self) -> None:
        self.infos: dict[int, ProcessInfo | None] = {}
        self.tree: list[int] = []

    def process_info(self, pid: int) -> ProcessInfo | None:
        return self.infos.get(pid)

    def descendants(self, pid: int, uid: int) -> list[int]:
        return list(self.tree)

    def descendants_info(self, pid: int, uid: int, root_start: float | None = None) -> list[tuple[int, float]]:
        return [(p, self.infos[p].start if self.infos.get(p) else 0.0) for p in self.tree]  # type: ignore[union-attr]


class KillCase(unittest.TestCase):
    def setUp(self) -> None:
        self.host = host_entry(1, pid=4242, startedAt=1_000_000_000_000)
        self.plat = FakePlatform()
        self.plat.infos[4242] = ProcessInfo(UID, 1_000_000_000.5, "/usr/local/bin/omp")
        self.sent: list[tuple[int, int]] = []
        self.now = [0.0]
        self.alive = True

        def send(pid: int, sig: int) -> None:
            self.sent.append((pid, sig))
            if sig == signal.SIGKILL:
                self.plat.infos[pid] = None

        self.killer = Killer(
            lambda: [self.host],
            self.plat,
            send=send,
            sleep=lambda s: self.advance(s),
            clock=lambda: self.now[0],
            uid=UID,
        )  # type: ignore[arg-type]
        self.sid = self.host["sessionId"]

    def advance(self, seconds: float) -> None:
        self.now[0] += seconds

    def refused(self, code: str, fn, *a) -> None:
        with self.assertRaises(ControlError) as ctx:
            fn(*a)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(self.sent, [], "nothing was signalled")


class Identity(KillCase):
    def test_the_confirmation_is_the_exact_name_or_the_project_folder(self) -> None:
        self.assertTrue(confirm_matches(self.host, "session 1"))
        self.assertTrue(confirm_matches(self.host, " project1 "))
        for typed in ("", "Session 1", None, 5, "/work/project1"):
            self.assertFalse(confirm_matches(self.host, typed))
        self.refused("confirm", self.killer.terminate, self.sid, "wrong")

    def test_each_identity_check_refuses_and_signals_nothing(self) -> None:
        self.plat.infos[4242] = ProcessInfo(UID + 1, 1_000_000_000.5, "omp")
        self.refused("identity", self.killer.terminate, self.sid, "session 1")
        self.plat.infos[4242] = ProcessInfo(UID, 1_000_000_000.5, "/bin/bash")
        self.refused("identity", self.killer.terminate, self.sid, "session 1")
        self.plat.infos[4242] = ProcessInfo(UID, 1_000_000_009.0, "omp")  # started after the room did: a reused pid
        self.refused("identity", self.killer.terminate, self.sid, "session 1")
        self.plat.infos[4242] = None
        self.refused("identity", self.killer.terminate, self.sid, "session 1")

    def test_a_missing_pid_or_session_is_refused(self) -> None:
        for pid in (None, 0, 1, True, "4242"):
            self.host["pid"] = pid
            self.refused("no_pid", self.killer.terminate, self.sid, "session 1")
        self.refused("gone", self.killer.terminate, "no-such-session", "x")
        self.assertIn("reused", target_problem({"startedAt": 1000}, ProcessInfo(UID, 99.0, "omp"), UID) or "")
        self.assertIn("reused", target_problem({}, ProcessInfo(UID, 1.0, "omp"), UID) or "")


class Signals(KillCase):
    def test_term_that_works_reports_exited_and_never_sends_kill(self) -> None:
        def send(pid: int, sig: int) -> None:
            self.sent.append((pid, sig))
            self.plat.infos[4242] = None

        self.killer.send = send
        self.assertEqual(self.killer.terminate(self.sid, "session 1"), {"outcome": "exited", "pid": 4242})
        self.assertEqual(self.sent, [(4242, signal.SIGTERM)])

    def test_force_is_only_offered_after_a_failed_term_for_the_same_process_and_for_a_while(self) -> None:
        self.refused("no_failed_term", self.killer.force, self.sid, "session 1")
        self.assertEqual(self.killer.terminate(self.sid, "session 1")["outcome"], "still_running")
        self.assertEqual(self.sent, [(4242, signal.SIGTERM)])
        self.sent.clear()
        self.plat.infos[4242] = ProcessInfo(UID, 1_000_000_001.0, "omp")  # another process now has the pid
        with self.assertRaises(ControlError):
            self.killer.force(self.sid, "session 1")
        self.plat.infos[4242] = ProcessInfo(UID, 1_000_000_000.5, "omp")
        self.advance(FORCE_WINDOW_S + 1)
        with self.assertRaises(ControlError) as ctx:
            self.killer.force(self.sid, "session 1")
        self.assertEqual(ctx.exception.code, "no_failed_term")
        self.assertEqual(self.sent, [])

    def test_force_kills_the_session_and_what_is_below_it_but_never_pid_one_or_the_hub(self) -> None:
        self.killer.terminate(self.sid, "session 1")
        self.sent.clear()
        self.plat.tree = [5001, 5002, 1, os.getpid()]
        self.plat.infos[5001] = ProcessInfo(UID, 7001.0, "tool")
        self.plat.infos[5002] = ProcessInfo(UID, 7002.0, "tool")
        result = self.killer.force(self.sid, "session 1")
        self.assertEqual(result["outcome"], "exited")
        self.assertEqual({p for p, s in self.sent}, {4242, 5001, 5002})
        self.assertTrue(all(s == signal.SIGKILL for _, s in self.sent))
        self.assertEqual(result["descendants"], 2)


@unittest.skipIf(sys.platform.startswith("win"), "unix only")
class RealProcess(unittest.TestCase):
    """A real process named omp that this test started: the identity checks run against the real process table."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="odk", dir="/tmp"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        self.omp = self.dir / "omp"
        shutil.copy("/bin/sh", self.omp)  # a copy of the shell, so the process table names it omp
        if sys.platform == "darwin":  # a copied system binary is killed by the kernel until it is signed again
            subprocess.run(
                ["/usr/bin/codesign", "--force", "--sign", "-", str(self.omp)], check=True, capture_output=True
            )
        self.procs: list[subprocess.Popen] = []
        self.addCleanup(lambda: [p.kill() for p in self.procs])

    def start(self, code: str) -> subprocess.Popen:
        # The shell says when it has got as far as the code's last setup step (the `: > ready` in it); a fixed sleep raced with
        # a busy machine, and a shell that had not yet set its trap died on the first SIGTERM.
        ready = self.dir / f"ready{len(self.procs)}"
        p = subprocess.Popen([str(self.omp), "-c", code.replace("@READY@", f": > {ready}")])
        self.procs.append(p)
        threading.Thread(target=p.wait, daemon=True).start()  # reaped as soon as it ends, as a shell would
        deadline = time.monotonic() + 10
        while "@READY@" in code and not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.3)
        return p

    def host_for(self, p: subprocess.Popen) -> dict:
        return host_entry(1, pid=p.pid, startedAt=int(time.time() * 1000))

    def test_term_ends_it(self) -> None:
        p = self.start("sleep 60 & trap 'kill $!; exit 0' TERM; @READY@; wait")
        host = self.host_for(p)
        result = Killer(lambda: [host], Platform()).terminate(host["sessionId"], "session 1", wait_s=8)
        self.assertEqual(result, {"outcome": "exited", "pid": p.pid})

    def test_a_process_that_ignores_term_needs_force_and_its_children_go_too(self) -> None:
        code = "trap '' TERM; sleep 60 & @READY@; wait"
        p = self.start(code)
        host = self.host_for(p)
        killer = Killer(lambda: [host], Platform())
        self.assertEqual(killer.terminate(host["sessionId"], "session 1", wait_s=1.0)["outcome"], "still_running")
        self.assertIsNone(p.poll())
        result = killer.force(host["sessionId"], "session 1", wait_s=8)
        self.assertEqual(result["outcome"], "exited")
        self.assertEqual(result["descendants"], 1)

    def test_a_process_that_is_not_called_omp_is_never_signalled(self) -> None:
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.procs.append(p)
        time.sleep(0.5)
        host = self.host_for(p)
        with self.assertRaises(ControlError) as ctx:
            Killer(lambda: [host], Platform()).terminate(host["sessionId"], "session 1", wait_s=1)
        self.assertEqual(ctx.exception.code, "identity")
        self.assertIsNone(p.poll())


if __name__ == "__main__":
    unittest.main()
