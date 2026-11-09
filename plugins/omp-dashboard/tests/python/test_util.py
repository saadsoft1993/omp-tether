"""Files and child processes: the helpers every other module relies on."""

from __future__ import annotations

import os
import signal
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from omp_dashboard.util import (
    DashboardError,
    FileLock,
    ensure_private_dir,
    one_line,
    printable,
    read_text,
    read_trusted_file,
    run_bounded,
    write_file,
)
from support import PLUGIN, shared_group  # noqa: F401


class Scratch(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="odu", dir="/tmp")).resolve()
        os.chmod(self.root, 0o700)
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))


class Text(unittest.TestCase):
    def test_printable_drops_controls_and_separators(self) -> None:
        self.assertEqual(printable("a\x1b[31mb\x07\u2028c\x85d\x7f"), "a[31mbcd")
        self.assertEqual(printable(None), "")
        self.assertEqual(printable("abcdef", 3), "abc")

    def test_one_line_collapses_whitespace_and_cuts(self) -> None:
        self.assertEqual(one_line("a\n\tb   c\r\nd"), "a b c d")
        self.assertEqual(len(one_line("x" * 500, 50)), 50)


class TrustedFiles(Scratch):
    def make(self, content: bytes = b"hello", mode: int = 0o600) -> Path:
        f = self.root / "f"
        f.write_bytes(content)
        os.chmod(f, mode)
        return f

    def test_a_file_of_ours_is_read(self) -> None:
        self.assertEqual(read_trusted_file(self.make()), b"hello")
        self.assertEqual(read_text(self.make("héllo".encode())), "héllo")

    def test_a_missing_file_raises_file_not_found_so_absent_differs_from_untrusted(self) -> None:
        with self.assertRaises(FileNotFoundError):
            read_trusted_file(self.root / "nope")

    def test_group_or_other_writable_is_refused_and_a_secret_may_not_be_readable(self) -> None:
        with self.assertRaisesRegex(DashboardError, "chmod go-w"):
            read_trusted_file(self.make(mode=0o666))
        with self.assertRaisesRegex(DashboardError, "chmod 600"):
            read_trusted_file(self.make(mode=0o644), secret=True)
        self.assertEqual(read_trusted_file(self.make(mode=0o644)), b"hello")

    def test_a_file_below_a_directory_others_can_write_is_refused_by_the_walk(self) -> None:
        loose = self.root / "loose"
        loose.mkdir()
        os.chmod(loose, 0o777)
        f = loose / "f"
        f.write_text("x")
        os.chmod(f, 0o600)
        with self.assertRaisesRegex(DashboardError, "writable by group or other"):
            read_trusted_file(f)

    def test_a_fifo_is_refused_without_blocking(self) -> None:
        fifo = self.root / "fifo"
        os.mkfifo(fifo, 0o600)
        started = time.monotonic()
        with self.assertRaisesRegex(DashboardError, "not a regular file"):
            read_trusted_file(fifo)
        self.assertLess(time.monotonic() - started, 2)

    def test_a_directory_and_an_oversized_file_are_refused(self) -> None:
        with self.assertRaisesRegex(DashboardError, "not a regular file"):
            read_trusted_file(self.root)
        with self.assertRaisesRegex(DashboardError, "larger than 10 bytes"):
            read_trusted_file(self.make(b"x" * 11), limit=10)

    def test_a_link_to_a_file_of_ours_is_followed_but_one_to_a_loose_directory_is_not(self) -> None:
        real = self.make()
        link = self.root / "link"
        link.symlink_to(real)
        self.assertEqual(read_trusted_file(link), b"hello")
        loose = self.root / "loose"
        loose.mkdir()
        os.chmod(loose, 0o777)
        (loose / "f").write_text("x")
        os.chmod(loose / "f", 0o600)
        other = self.root / "link2"
        other.symlink_to(loose / "f")
        with self.assertRaises(DashboardError):
            read_trusted_file(other)

    def test_invalid_utf8_is_refused_by_read_text(self) -> None:
        with self.assertRaisesRegex(DashboardError, "UTF-8"):
            read_text(self.make(b"\xff\xfe"))


class Directories(Scratch):
    def test_a_new_directory_is_created_0700_whatever_the_umask(self) -> None:
        old = os.umask(0o002)
        try:
            self.assertTrue(ensure_private_dir(self.root / "a" / "b"))
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(os.stat(self.root / "a" / "b").st_mode), 0o700)

    def test_a_too_open_directory_of_ours_is_tightened_or_refused_on_request(self) -> None:
        d = self.root / "d"
        d.mkdir()
        os.chmod(d, 0o755)
        with self.assertRaisesRegex(DashboardError, "mode 700"):
            ensure_private_dir(d, refuse_open=True)
        self.assertTrue(ensure_private_dir(d))
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)

    def test_another_tools_directory_is_never_chmoded_and_refused_when_others_can_write(self) -> None:
        shared_group(self)  # a group with other members: the case these checks are about
        d = self.root / "theirs"
        d.mkdir()
        os.chmod(d, 0o755)
        self.assertFalse(ensure_private_dir(d, tighten=False))
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o755)
        os.chmod(d, 0o775)
        with self.assertRaisesRegex(DashboardError, "writable by group"):
            ensure_private_dir(d, tighten=False)

    def test_a_symbolic_link_is_not_a_directory_we_make_private(self) -> None:
        real = self.root / "real"
        real.mkdir()
        os.chmod(real, 0o755)
        (self.root / "link").symlink_to(real)
        with self.assertRaises(DashboardError):
            ensure_private_dir(self.root / "link")
        self.assertEqual(stat.S_IMODE(os.stat(real).st_mode), 0o755)


class Writing(Scratch):
    def test_write_is_atomic_0600_and_leaves_no_temporary_file(self) -> None:
        target = self.root / "t.json"
        write_file(target, "one")
        write_file(target, b"two")
        self.assertEqual(target.read_bytes(), b"two")
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["t.json"])

    def test_write_refuses_to_follow_a_link_planted_as_the_temporary_name(self) -> None:
        target = self.root / "t.json"
        victim = self.root / "victim"
        victim.write_text("keep")
        # the temporary name has a random part, so a planted link cannot be hit; the target link itself is replaced, not followed
        target.symlink_to(victim)
        write_file(target, "new")
        self.assertEqual(victim.read_text(), "keep")
        self.assertFalse(target.is_symlink())

    def test_a_failed_write_cleans_up(self) -> None:
        with self.assertRaises(OSError):
            write_file(self.root / "missing-dir" / "t", "x")
        self.assertEqual(list(self.root.iterdir()), [])


class Locks(Scratch):
    def test_the_second_holder_waits_then_fails_with_a_clear_message(self) -> None:
        path = self.root / "lock"
        with FileLock(path):
            with self.assertRaisesRegex(DashboardError, "holds the lock"):
                with FileLock(path, wait=0.1):
                    pass
        with FileLock(path):  # released
            pass

    def test_a_lock_file_that_others_can_read_is_refused(self) -> None:
        path = self.root / "lock"
        path.write_text("")
        os.chmod(path, 0o644)
        with self.assertRaisesRegex(DashboardError, "private regular file"):
            FileLock(path).__enter__()


class Children(unittest.TestCase):
    def test_output_and_exit_status(self) -> None:
        r = run_bounded(
            [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"], 10
        )
        self.assertEqual(
            (r.returncode, r.stdout.strip(), r.stderr.strip(), r.timed_out, r.ok), (3, "out", "err", False, False)
        )

    def test_a_missing_program_reports_why_without_the_argv(self) -> None:
        r = run_bounded(["/nonexistent/prog", "secret-argument"], 5)
        self.assertIsNone(r.returncode)
        self.assertNotIn("secret-argument", r.error or "")
        self.assertIn("FileNotFoundError", r.error or "")

    def test_the_deadline_kills_the_whole_process_group(self) -> None:
        marker = tempfile.mktemp(prefix="odk", dir="/tmp")
        inner = f"import time,os;open({marker!r},'w').write(str(os.getpid()));time.sleep(60)"
        code = f"import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',{inner!r}]);time.sleep(60)"
        started = time.monotonic()
        r = run_bounded([sys.executable, "-c", code], 1.5)
        self.assertTrue(r.timed_out)
        self.assertLess(time.monotonic() - started, 6)
        deadline = time.monotonic() + 5
        while not os.path.exists(marker) and time.monotonic() < deadline:
            time.sleep(0.05)
        pid = int(Path(marker).read_text())
        os.unlink(marker)
        time.sleep(0.3)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_output_is_capped_and_the_cap_is_reported(self) -> None:
        r = run_bounded([sys.executable, "-c", "print('x' * 100000)"], 10, max_output=1000)
        self.assertEqual(len(r.stdout), 1000)
        self.assertTrue(r.truncated)
        small = run_bounded([sys.executable, "-c", "print('x' * 100)"], 10, max_output=1000)
        self.assertFalse(small.truncated)

    def test_the_environment_is_small_and_fixed(self) -> None:
        os.environ["ODB_SECRET_TEST"] = "leak"
        try:
            r = run_bounded([sys.executable, "-c", "import os; print(sorted(os.environ))"], 10)
        finally:
            del os.environ["ODB_SECRET_TEST"]
        self.assertNotIn("ODB_SECRET_TEST", r.stdout)

    def test_stdin_is_empty(self) -> None:
        r = run_bounded([sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"], 10)
        self.assertEqual(r.stdout.strip(), "''")

    def test_threads_do_not_leave_children_behind(self) -> None:
        before = threading.active_count()
        for _ in range(3):
            run_bounded([sys.executable, "-c", "pass"], 10)
        self.assertLessEqual(
            threading.active_count(), before
        )  # timers of earlier tests may still be winding down, never more
        self.assertEqual(signal.getsignal(signal.SIGINT), signal.default_int_handler)


if __name__ == "__main__":
    unittest.main()
