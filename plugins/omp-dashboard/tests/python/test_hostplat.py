"""The platform module: listener owners and the process table on macOS (lsof, ps) and Linux (/proc, ss), parsed from fixtures that are
written the way each tool prints them, plus real checks against this machine on whichever of the two it is."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from omp_dashboard import hostplat
from omp_dashboard.hostplat import Platform, PlatformError
from omp_dashboard.util import RunResult
from support import PLUGIN  # noqa: F401

PROC_NET_TCP = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 12345 1 0000000000000000 100 0 0 10 0
   1: 00000000:0050 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 23456 1 0000000000000000 100 0 0 10 0
   2: 0100007F:1F91 0100007F:C350 01 00000000:00000000 00:00000000 00000000  1000        0 34567 1 0000000000000000 100 0 0 10 0
"""
PROC_NET_TCP6 = """  sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000000000000000000001000000:1F92 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000  1001        0 45678 1 0000000000000000 100 0 0 10 0
"""
SS_OUTPUT = """LISTEN 0 4096 127.0.0.1:8080 0.0.0.0:* uid:1000 ino:12345 sk:1 cgroup:/ <->
LISTEN 0 4096 [::1]:8081 [::]:* uid:1001 ino:12346 sk:2 cgroup:/ v6only:1 <->
ESTAB 0 0 127.0.0.1:8080 127.0.0.1:50000 uid:1000 ino:12347 sk:3 cgroup:/ <->
"""


class Parsers(unittest.TestCase):
    def test_proc_net_tcp_lists_only_listening_sockets_on_the_port(self) -> None:
        self.assertEqual(hostplat.parse_proc_net_tcp(PROC_NET_TCP, 8080), {1000})
        self.assertEqual(hostplat.parse_proc_net_tcp(PROC_NET_TCP, 80), {0})
        self.assertEqual(
            hostplat.parse_proc_net_tcp(PROC_NET_TCP, 8081), set(), "an established connection is not a listener"
        )
        self.assertEqual(hostplat.parse_proc_net_tcp(PROC_NET_TCP6, 8082), {1001})
        self.assertEqual(hostplat.parse_proc_net_tcp("garbage\nnot enough fields", 8080), set())

    def test_lsof_field_output(self) -> None:
        self.assertEqual(hostplat.parse_lsof_uids("p123\nu501\nf9\np124\nu0\n"), {501, 0})
        self.assertEqual(hostplat.parse_lsof_uids(""), set())
        self.assertEqual(hostplat.parse_lsof_uids("uabc\nxu501"), set())

    def test_ss_output(self) -> None:
        self.assertEqual(hostplat.parse_ss_uids(SS_OUTPUT, 8080), {1000})
        self.assertEqual(hostplat.parse_ss_uids(SS_OUTPUT, 8081), {1001})
        self.assertEqual(hostplat.parse_ss_uids(SS_OUTPUT, 9999), set())

    def test_ps_info_with_a_command_that_holds_spaces(self) -> None:
        text = "  501 Mon Nov  9 14:02:16 2026 /Applications/Some App/bin/omp\n"
        info = hostplat.parse_ps_info(text)
        assert info is not None
        self.assertEqual((info["uid"], info["command"]), (501, "/Applications/Some App/bin/omp"))
        self.assertEqual(info["start"], 1794232936.0)
        self.assertIsNone(hostplat.parse_ps_info("501 Mon Nov  9 omp\n"))
        self.assertIsNone(hostplat.parse_ps_info("501 Xyz Nov  9 14:02:16 2026 omp\n"))
        self.assertIsNone(hostplat.parse_ps_info(""))

    def test_ps_table(self) -> None:
        self.assertEqual(
            hostplat.parse_ps_table("  1 0 0\n 20 1 501\nbad line\n 21 20 abc\n"), {1: (0, 0), 20: (1, 501)}
        )

    def test_proc_stat_with_a_command_that_holds_spaces_and_parentheses(self) -> None:
        rest = "S 1 20 20 0 -1 4194560 100 0 0 0 1 2 0 0 20 0 1 0 987654 1000 100 18446744073709551615"
        self.assertEqual(hostplat.parse_proc_stat(f"20 (omp) {rest}"), (1, 987654))
        self.assertEqual(hostplat.parse_proc_stat(f"20 (my (odd) name) {rest}"), (1, 987654))
        self.assertIsNone(hostplat.parse_proc_stat("20 (omp) S 1"))
        self.assertIsNone(hostplat.parse_proc_stat("no parenthesis"))

    def test_proc_status_uid_is_the_effective_one(self) -> None:
        self.assertEqual(
            hostplat.parse_proc_status_uid("Name:\tomp\nUid:\t1000\t1001\t1000\t1000\nGid:\t1\t1\t1\t1\n"), 1001
        )
        self.assertIsNone(hostplat.parse_proc_status_uid("Name:\tomp\n"))


class FakeProc:
    """A /proc tree in a scratch directory."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="odh", dir="/tmp"))
        (self.root / "net").mkdir()
        (self.root / "net" / "tcp").write_text(PROC_NET_TCP)
        (self.root / "net" / "tcp6").write_text(PROC_NET_TCP6)
        (self.root / "stat").write_text("cpu  1 2 3\nbtime 1000000\nprocesses 5\n")

    def process(self, pid: int, ppid: int, uid: int, comm: str, ticks: int) -> None:
        d = self.root / str(pid)
        d.mkdir()
        (d / "stat").write_text(
            f"{pid} ({comm}) S {ppid} {pid} {pid} 0 -1 4194560 1 0 0 0 1 2 0 0 20 0 1 0 {ticks} 1000 100 99\n"
        )
        (d / "status").write_text(f"Name:\t{comm}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
        (d / "comm").write_text(comm + "\n")


class Linux(unittest.TestCase):
    def setUp(self) -> None:
        self.proc = FakeProc()
        self.addCleanup(shutil.rmtree, self.proc.root, True)
        self.plat = Platform("linux", proc_root=self.proc.root)

    def test_listener_uids_reads_both_tables(self) -> None:
        self.assertEqual(self.plat.listener_uids(8080), {1000})
        self.assertEqual(self.plat.listener_uids(8082), {1001})
        self.assertEqual(self.plat.listener_uids(65000), set())

    def test_a_listener_is_unknown_when_neither_table_can_be_read_and_there_is_no_ss(self) -> None:
        (self.proc.root / "net" / "tcp").unlink()
        (self.proc.root / "net" / "tcp6").unlink()
        calls: list[list[str]] = []

        def runner(argv, timeout, **kw):
            calls.append(argv)
            return RunResult(0, SS_OUTPUT, "")

        plat = Platform("linux", proc_root=self.proc.root, run=runner)
        if any(os.access(c, os.X_OK) for c in hostplat.SS_CANDIDATES):
            self.assertEqual(plat.listener_uids(8080), {1000})
            self.assertEqual(calls[0][1:], ["-H", "-l", "-t", "-n", "-e"])
        else:
            with self.assertRaises(PlatformError):
                plat.listener_uids(8080)

    def test_process_info_converts_start_ticks_to_epoch_seconds(self) -> None:
        self.proc.process(20, 1, 1000, "omp", 250_000)
        info = self.plat.process_info(20)
        assert info is not None
        self.assertEqual((info.uid, info.command), (1000, "omp"))
        self.assertAlmostEqual(info.start, 1_000_000 + 250_000 / hostplat._clock_ticks())

    def test_a_process_that_is_gone_is_none_not_an_error(self) -> None:
        self.assertIsNone(self.plat.process_info(999))

    def test_a_missing_proc_is_an_error_not_a_silent_none(self) -> None:
        shutil.rmtree(self.proc.root)
        with self.assertRaises(PlatformError):
            Platform("linux", proc_root=self.proc.root).process_table()
        with self.assertRaises(PlatformError):
            Platform("linux", proc_root=self.proc.root).process_info(5)

    def test_descendants_stop_at_a_process_of_another_account(self) -> None:
        self.proc.process(1, 0, 0, "init", 1)
        self.proc.process(20, 1, 1000, "omp", 5)
        self.proc.process(21, 20, 1000, "bash", 6)
        self.proc.process(22, 21, 1000, "tool", 7)
        self.proc.process(23, 20, 1001, "theirs", 8)
        self.proc.process(24, 23, 1000, "below-theirs", 9)
        self.assertEqual(sorted(self.plat.descendants(20, 1000)), [21, 22])

    def test_ancestors_walk_up_to_but_not_including_init(self) -> None:
        self.proc.process(1, 0, 0, "init", 1)
        self.proc.process(10, 1, 1000, "tmux", 2)
        self.proc.process(11, 10, 1000, "bash", 3)
        self.proc.process(12, 11, 1000, "omp", 4)
        self.assertEqual(self.plat.ancestors(12), [11, 10])

    def test_a_loop_in_the_table_ends(self) -> None:
        self.proc.process(30, 31, 1000, "a", 1)
        self.proc.process(31, 30, 1000, "b", 2)
        self.assertEqual(len(self.plat.ancestors(30)), 2)


class Darwin(unittest.TestCase):
    def runner(self, outputs: dict[str, RunResult]):
        calls: list[list[str]] = []

        def run(argv, timeout, **kw):
            calls.append(argv)
            for key, result in outputs.items():
                if key in argv:
                    return result
            return RunResult(1, "", "")

        run.calls = calls  # type: ignore[attr-defined]
        return run

    def test_listener_uids_uses_lsof_any_address_and_treats_exit_one_as_no_listener(self) -> None:
        run = self.runner({hostplat.LSOF: RunResult(0, "p1\nu501\n", "")})
        plat = Platform("darwin", run=run)
        self.assertEqual(plat.listener_uids(8080), {501})
        self.assertEqual(run.calls[0], [hostplat.LSOF, "-nP", "-iTCP:8080", "-sTCP:LISTEN", "-Fu"])
        self.assertEqual(
            Platform("darwin", run=self.runner({hostplat.LSOF: RunResult(1, "", "")})).listener_uids(8080), set()
        )

    def test_lsof_failing_or_missing_is_an_error_never_an_empty_answer(self) -> None:
        for result in (
            RunResult(2, "", ""),
            RunResult(None, "", "", error="FileNotFoundError"),
            RunResult(None, "", "", timed_out=True),
        ):
            with self.assertRaises(PlatformError):
                Platform("darwin", run=self.runner({hostplat.LSOF: result})).listener_uids(8080)

    def test_process_info_runs_ps_in_utc_and_the_c_locale(self) -> None:
        run = self.runner({hostplat.PS: RunResult(0, "  501 Mon Nov  9 14:02:16 2026 /usr/local/bin/omp\n", "")})
        info = Platform("darwin", run=run).process_info(4242)
        assert info is not None
        self.assertEqual((info.uid, info.command), (501, "/usr/local/bin/omp"))
        self.assertEqual(run.calls[0], [hostplat.PS, "-o", "uid=,lstart=,comm=", "-p", "4242"])
        self.assertEqual(hostplat.PS_ENV["TZ"], "UTC0")

    def test_a_pid_that_ps_does_not_know_is_none_and_ps_failing_is_an_error(self) -> None:
        self.assertIsNone(Platform("darwin", run=self.runner({hostplat.PS: RunResult(1, "", "")})).process_info(7))
        with self.assertRaises(PlatformError):
            Platform("darwin", run=self.runner({hostplat.PS: RunResult(None, "", "", error="OSError")})).process_info(7)

    def test_the_table_comes_from_one_ps_call(self) -> None:
        run = self.runner({hostplat.PS: RunResult(0, " 1 0 0\n 20 1 501\n 21 20 501\n", "")})
        plat = Platform("darwin", run=run)
        self.assertEqual(plat.descendants(20, 501), [21])
        self.assertEqual(len(run.calls), 1)


class ThisMachine(unittest.TestCase):
    """The real tools on whichever platform runs the tests."""

    def test_the_platform_is_known_and_has_a_noun(self) -> None:
        plat = Platform()
        self.assertIn(plat.kind, ("darwin", "linux"))
        self.assertEqual(hostplat.noun(plat.kind), "Mac" if plat.kind == "darwin" else "machine")
        with self.assertRaises(PlatformError):
            hostplat.system("windows")

    def test_a_listener_of_ours_is_seen_as_ours(self) -> None:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen()
            port = s.getsockname()[1]
            self.assertEqual(Platform().listener_uids(port), {os.getuid()})
        time.sleep(0.05)
        self.assertEqual(Platform().listener_uids(port), set())

    def test_a_listener_on_every_address_is_seen_too(self) -> None:
        with socket.socket() as s:
            s.bind(("0.0.0.0", 0))
            s.listen()
            self.assertEqual(Platform().listener_uids(s.getsockname()[1]), {os.getuid()})

    def test_process_info_of_ourselves_and_of_a_child(self) -> None:
        plat = Platform()
        me = plat.process_info(os.getpid())
        assert me is not None
        self.assertEqual(me.uid, os.getuid())
        self.assertLess(abs(me.start - time.time()), 3600 * 24 * 365)
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            time.sleep(0.2)
            info = plat.process_info(child.pid)
            assert info is not None
            self.assertEqual(info.uid, os.getuid())
            self.assertLess(abs(info.start - time.time()), 5, "the start time is this run's, within clock rounding")
            self.assertIn(child.pid, plat.descendants(os.getpid(), os.getuid()))
            self.assertIn(os.getpid(), plat.ancestors(child.pid))
        finally:
            child.kill()
            child.wait()
        time.sleep(0.1)
        self.assertIsNone(plat.process_info(child.pid))

    def test_a_pid_that_cannot_exist_is_none(self) -> None:
        self.assertIsNone(Platform().process_info(2**22 - 3))


if __name__ == "__main__":
    unittest.main()
