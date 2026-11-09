"""Regression tests for the verification pass (R2-R12), each written before its fix and run against the code as it was."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

from omp_dashboard import alerts, util
from omp_dashboard.config import load_config
from omp_dashboard.hostplat import Platform, ProcessInfo
from omp_dashboard.kill import Killer
from omp_dashboard.pairing import PairingReader, PairingStore, PairingWriter
from omp_dashboard.paths import from_env
from omp_dashboard.util import DashboardError, run_bounded
from support import MAIN, PLUGIN, Client, ScratchTestCase, host_entry
from test_review1 import FakePlatform, Tmp, cli

# R2 ---------------------------------------------------------------------------------------------------------------------------


class R2LiveVerdict(Tmp):
    def paired(self) -> tuple[PairingStore, str]:
        store = PairingStore(self.root / "store", "c")
        session = store.writer.redeem(store.writer.create_token(), "x")
        assert session
        return store, f"c={session}"

    def test_a_verdict_is_not_reused_after_a_directory_above_the_store_became_writable(self) -> None:
        store, cookie = self.paired()
        self.assertTrue(store.paired_device(cookie))  # the verdict is cached now
        os.chmod(self.root, 0o777)  # a change above the store, on the SAME live instance
        self.assertIsNone(store.paired_device(cookie), "the cached verdict outlived the change it rested on")

    def test_a_replaced_devices_file_is_judged_again_but_an_untouched_one_is_not(self) -> None:
        store, cookie = self.paired()
        seen: list[str] = []
        real = __import__("omp_dashboard.pairing", fromlist=["check_path"]).check_path

        def spy(p, secret=False):
            seen.append(Path(p).name)
            return real(p, secret=secret)

        with mock.patch("omp_dashboard.pairing.check_path", spy):
            self.assertTrue(store.paired_device(cookie))
            first = len(seen)
            self.assertTrue(store.paired_device(cookie))
            self.assertEqual(len(seen), first, "nothing changed: the verdict may be reused")
            devices = self.root / "store" / "devices.json"
            data = devices.read_bytes()
            fresh = devices.with_name("devices.new")
            fresh.write_bytes(data)
            os.chmod(fresh, 0o600)
            os.replace(fresh, devices)  # same content, same mode, another file
            self.assertTrue(store.paired_device(cookie))
            self.assertGreater(len(seen), first, "a replaced leaf must be walked again before it is used")

    @unittest.skipUnless(sys.platform == "darwin", "macOS ACLs")
    def test_an_acl_added_to_the_live_store_stops_the_live_instance_at_once(self) -> None:
        store, cookie = self.paired()
        self.assertTrue(store.paired_device(cookie))
        subprocess.run(
            ["/bin/chmod", "+a", "group:everyone allow write,append", str(self.root / "store" / "devices.json")],
            check=True,
        )
        self.assertIsNone(store.paired_device(cookie))


# R3 ---------------------------------------------------------------------------------------------------------------------------


class R3Killpg(unittest.TestCase):
    def test_a_reaped_leader_whose_descendant_keeps_the_pipe_open_is_not_followed_by_a_group_signal(self) -> None:
        calls: list[int] = []
        real = os.killpg
        # The leader exits at once; a descendant in a session of its own keeps stdout open past the deadline.
        inner = "import time;time.sleep(6)"
        code = f"import subprocess,sys;subprocess.Popen([sys.executable,'-c',{inner!r}],start_new_session=True)"
        with mock.patch.object(os, "killpg", side_effect=lambda pid, sig: (calls.append(pid), real(pid, sig))):
            r = run_bounded([sys.executable, "-c", code], 0.3)
        self.assertEqual(calls, [], "the leader was reaped: its group id may belong to somebody else now")
        self.assertIsNotNone(r)


# R4 ---------------------------------------------------------------------------------------------------------------------------


class R4Root(unittest.TestCase):
    def setUp(self) -> None:
        self.uid = os.getuid()
        self.host = host_entry(1, pid=4242, startedAt=1_000_000_000_000)
        self.plat = FakePlatform()
        self.plat.infos[4242] = ProcessInfo(self.uid, 1_000_000_000.5, "/usr/local/bin/omp")
        self.sent: list[tuple[int, int]] = []
        self.now = [0.0]
        self.killer = Killer(
            lambda: [self.host],
            self.plat,  # type: ignore[arg-type]
            send=lambda pid, sig: self.sent.append((pid, sig)),
            sleep=lambda s: self.now.__setitem__(0, self.now[0] + s),
            clock=lambda: self.now[0],
            uid=self.uid,
        )
        self.sid = self.host["sessionId"]
        self.killer.failed_term[self.sid] = (4242, 1_000_000_000.5, 0.0)

    def test_children_found_while_the_pid_named_a_replacement_process_are_not_signalled(self) -> None:
        self.plat.tree = [(5001, 1000.0)]
        self.plat.infos[5001] = ProcessInfo(
            self.uid, 1000.0, "tool"
        )  # the replacement's child: its own start time is unchanged

        def enumerate_then_replace(pid, uid, root_start=None):
            self.plat.infos[4242] = ProcessInfo(
                self.uid, 2_000_000_000.0, "omp"
            )  # the pid was reused before the table was read
            return list(self.plat.tree)

        self.plat.descendants_info = enumerate_then_replace  # type: ignore[method-assign]
        self.killer.force(self.sid, "session 1", wait_s=1)
        self.assertEqual(self.sent, [], "nothing below a pid that no longer names the session may be signalled")

    def test_the_enumeration_itself_is_bound_to_the_root_start_time(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            time.sleep(0.3)
            plat = Platform()
            mine = plat.process_info(os.getpid())
            assert mine is not None
            self.assertTrue([r for r in plat.descendants_info(os.getpid(), os.getuid()) if r[0] == child.pid])
            self.assertTrue(
                [r for r in plat.descendants_info(os.getpid(), os.getuid(), mine.start) if r[0] == child.pid]
            )
            self.assertEqual(
                plat.descendants_info(os.getpid(), os.getuid(), mine.start + 1000),
                [],
                "another start time: another process",
            )
        finally:
            child.kill()
            child.wait()

    def test_the_result_says_whether_the_session_itself_is_still_running(self) -> None:
        self.plat.tree = [(5001, 1000.0)]
        self.plat.infos[5001] = ProcessInfo(self.uid, 1000.0, "tool")
        survivors = {5001}

        def send(pid, sig):
            self.sent.append((pid, sig))
            if pid not in survivors:
                self.plat.infos[pid] = None

        self.killer.send = send
        r = self.killer.force(self.sid, "session 1", wait_s=1)
        self.assertEqual((r["outcome"], r["remaining"], r["parent_running"]), ("still_running", 1, False))


# R5 ---------------------------------------------------------------------------------------------------------------------------


class R5RevokeAll(Tmp):
    def test_a_redemption_cannot_land_between_unpairing_and_cancelling_links(self) -> None:
        w = PairingWriter(self.root / "store")
        token = w.create_token()
        other = PairingWriter(self.root / "store")
        outcome: list[str | None] = []
        started = threading.Event()
        real_save = w._save

        def save(name, data):
            if not started.is_set():
                started.set()
                thread.start()
                time.sleep(
                    0.4
                )  # the other process tries to redeem while this one holds the lock, between the two writes
            return real_save(name, data)

        thread = threading.Thread(target=lambda: outcome.append(other.redeem(token, "late")))
        w._save = save  # type: ignore[method-assign]
        devices, links = w.revoke_everything()
        thread.join(10)
        self.assertEqual((devices, links), (0, 1))
        self.assertEqual(outcome, [None], "the redemption waited for the lock and found nothing: nobody is left paired")
        self.assertEqual(other.devices(), [])

    def test_the_command_line_uses_the_single_locked_operation(self) -> None:
        text = (PLUGIN / "python/omp_dashboard/cli.py").read_text()
        self.assertIn("revoke_everything", text)
        self.assertNotIn("clear_pending", text)


# R6 ---------------------------------------------------------------------------------------------------------------------------


class SlowBody:
    """Answers the headers at once, then the body a byte at a time, in one of three framings."""

    HEADS: ClassVar[dict[str, bytes]] = {
        "http10": b"HTTP/1.0 200 OK\r\n\r\n",
        "close": b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: 100000\r\n\r\n",
        "chunked": b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n",
    }

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.conns: list[socket.socket] = []
        self.stop = False
        threading.Thread(target=self.run, daemon=True).start()

    def run(self) -> None:
        while not self.stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.conns.append(conn)
            threading.Thread(target=self.drip, args=(conn,), daemon=True).start()

    def drip(self, conn: socket.socket) -> None:
        try:
            conn.recv(4096)
            conn.sendall(self.HEADS[self.mode])
            for _ in range(2000):
                conn.sendall(b"1\r\nx\r\n" if self.mode == "chunked" else b"x")
                time.sleep(0.2)
        except OSError:
            pass

    def close(self) -> None:
        self.stop = True
        self.sock.close()
        for c in self.conns:
            c.close()


class R6Deadlines(unittest.TestCase):
    def each(self):
        for mode in ("http10", "close", "chunked"):
            slow = SlowBody(mode)
            self.addCleanup(slow.close)
            yield mode, slow

    def test_a_relay_that_sends_headers_at_once_and_drips_the_body_is_cut_at_the_deadline(self) -> None:
        from omp_dashboard import relay

        for mode, slow in self.each():
            started = time.monotonic()
            with self.assertRaises(relay.RelayCheckError, msg=mode):
                relay.prove_listener(slow.port, b"\x00" * 32, timeout=1.0)
            self.assertLess(time.monotonic() - started, 4, mode)

    def test_an_ntfy_server_that_drips_the_body_cannot_hold_the_watcher(self) -> None:
        for mode, slow in self.each():
            started = time.monotonic()
            with self.assertRaises(OSError, msg=mode):
                alerts.ntfy_send(f"http://127.0.0.1:{slow.port}", "a-long-enough-topic-1234", "T", "B", timeout=1.0)
            self.assertLess(time.monotonic() - started, 4, mode)

    def test_the_doctor_probe_is_cut_too(self) -> None:
        from omp_dashboard import doctor

        for mode, slow in self.each():
            started = time.monotonic()
            self.assertIsNotNone(doctor._probe_hub("127.0.0.1", slow.port), mode)
            self.assertLess(time.monotonic() - started, 5, mode)


# R7 ---------------------------------------------------------------------------------------------------------------------------


@unittest.skipUnless(sys.platform == "darwin", "macOS ACLs")
class R7SecretAcl(Tmp):
    def readable_parent(self) -> Path:
        parent = self.root / "p"
        parent.mkdir(mode=0o700)
        # Traversal and read for everybody, inherited by what is created below; no write: the parent passes the ordinary ancestor check.
        subprocess.run(
            ["/bin/chmod", "+a", "group:everyone allow read,file_inherit,directory_inherit", str(parent)], check=True
        )
        return parent

    def test_a_secret_file_is_not_written_where_it_would_inherit_a_read_entry(self) -> None:
        parent = self.readable_parent()
        written: list[bytes] = []
        real_write = os.write
        with mock.patch(
            "os.write", side_effect=lambda fd, data: (written.append(bytes(data)), real_write(fd, data))[1]
        ):
            with self.assertRaises(DashboardError):
                util.write_file(parent / "ntfy-topic", "a-long-enough-topic-1234")
        self.assertEqual(
            [w for w in written if b"a-long" in w],
            [],
            "the secret must not be written before the ACL is known to be private",
        )
        self.assertEqual(
            sorted(p.name for p in parent.iterdir()), [], "no secret bytes and no temporary file are left behind"
        )

    def test_a_new_private_directory_that_inherited_an_entry_is_refused(self) -> None:
        parent = self.readable_parent()
        with self.assertRaises(DashboardError):
            util.ensure_private_dir(parent / "secrets")


class R7Delete(ScratchTestCase):
    def test_clearing_the_topic_walks_the_ancestors_like_every_other_change(self) -> None:
        s = self.make()
        loose = s.root / "loose"
        loose.mkdir()
        os.chmod(loose, 0o777)
        inner = loose / "t"
        inner.mkdir(mode=0o700)
        topic = inner / "ntfy-topic"
        topic.write_text("a-long-enough-topic-1234\n")
        os.chmod(topic, 0o600)
        s.write_config({"alerts": {"topicFile": str(topic)}})
        r = cli(s, "config", "clear-ntfy-topic")
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertTrue(topic.exists())


# R8 / R9 ----------------------------------------------------------------------------------------------------------------------


class R8Profile(ScratchTestCase):
    def test_a_configured_ranking_directory_does_not_excuse_a_profile_variable(self) -> None:
        s = self.make()
        s.write_config(
            {"omp": {"bin": s.env["OMP_BIN"]}, "listen": {"port": s.port}, "projects": {"sessionsDir": "/x/sessions"}}
        )
        env = {k: v for k, v in s.env.items() if k != "OMP_DASHBOARD_PORT"}
        r = subprocess.run(
            [sys.executable, str(MAIN), "dropins", "--write"],
            env={**env, "PI_CODING_AGENT_DIR": "/x/agent", "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            text=True,
            timeout=60,
            input="",
        )
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("PI_CODING_AGENT_DIR", r.stderr)


class R9Lexical(ScratchTestCase):
    def test_home_and_the_fallback_variables_refuse_dot_dot_like_the_rest(self) -> None:
        s = self.make()
        with self.assertRaises(DashboardError):
            from_env({**s.env, "HOME": "/safe/link/../home"})
        for name in ("OMP_SESSION_CONTROL_DIR", "PI_CODING_AGENT_DIR"):
            env = {**s.env, name: "/a/../b"}
            self.assertTrue([p for p in load_config(from_env(env), env).problems if name in p], name)


# R10 --------------------------------------------------------------------------------------------------------------------------


class R10Cache(Tmp):
    def test_a_store_with_devices_is_parsed_once_until_the_file_changes(self) -> None:
        store = PairingStore(self.root / "store", "c")
        store.writer.redeem(store.writer.create_token(), "x")
        reader = PairingReader(self.root / "store")
        first = reader.read()
        self.assertTrue(first.hashes)
        self.assertIs(reader.read(), first, "the cache key must be the file's fingerprint, not the last JSON key")


# R11 --------------------------------------------------------------------------------------------------------------------------


class R11Audit(ScratchTestCase):
    def lines(self, hub) -> list[tuple[str, str]]:
        p = hub.config.audit_file
        return [(x["action"], x["outcome"]) for x in map(json.loads, p.read_text().splitlines())] if p.exists() else []

    def test_the_refusals_the_documents_promise_are_audited(self) -> None:
        s = self.make([host_entry(1)])
        hub = self.hub(
            s, {"relay": {"enabled": True, "hostPort": 17997, "guestPort": 17996, "webUrl": "http://127.0.0.1:17996"}}
        )
        c = hub.pair()
        c.post("/api/hub/link", {"instanceId": "inst0001", "allowPublic": True})
        c.post("/api/hub/start", {"path": "/x"})
        Client(s.port).request("POST", "/api/hub/pair", raw_body=b"not json")
        got = self.lines(hub)
        for want in (
            ("link", "refused:public_requested"),
            ("start", "refused:start_off"),
            ("pair", "refused:malformed"),
        ):
            self.assertIn(want, got)

    def test_a_failed_command_line_revoke_is_audited(self) -> None:
        s = self.make()
        s.write_config({"audit": {"file": str(s.root / "audit.jsonl")}})
        r = cli(s, "revoke", "zz")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("refused:", (s.root / "audit.jsonl").read_text())

    def test_the_documents_say_what_the_code_does(self) -> None:
        readme = (PLUGIN / "README.md").read_text()
        self.assertNotIn("set-ntfy-topic [TOPIC]", readme)
        api = (PLUGIN / "docs/api.md").read_text()
        self.assertIn("force_exited", api)
        evidence = (PLUGIN / "docs/release-evidence.md").read_text()
        self.assertNotIn("stats proof", evidence)
        self.assertNotIn("24 guards", evidence)
        threat = (PLUGIN / "docs/threat-model.md").read_text()
        self.assertIn("is in that browser's history", threat)


if __name__ == "__main__":
    unittest.main()
