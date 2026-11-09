"""The control/1 client (docs/protocol/control-1.md): registry checks, the conversation with a session's socket, and what a hostile or
broken peer on that socket can and cannot do. The peer is a stand-in extension that speaks the protocol on a real Unix socket."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from omp_dashboard import control as control_module
from omp_dashboard.control import Conn, ControlDir, ControlError, control, has_control, negotiate
from support import PLUGIN  # noqa: F401

SESSION = "01a1b65b-93f3-7000-8000-000000000001"


class FakeExtension:
    """One session's control socket and registry entry, in a private directory."""

    def __init__(
        self, directory: Path, session: str = SESSION, pid: int | None = None, versions: list[int] | None = None
    ):
        self.dir = directory
        self.session = session
        self.pid = pid if pid is not None else os.getpid()
        self.versions = versions if versions is not None else [1]
        self.nonce = secrets.token_hex(16)
        self.generation = 0
        self.name: str | None = None
        self.requests: list[dict[str, Any]] = []
        self.behaviour = "normal"  # normal | oversize | trickle | garbage | array | spam | closing | stale_once | invalid | refuse_snapshot | other_session | silent
        self.stale_sent = False
        self.sock_path = directory / f"{secrets.token_hex(8)}.sock"
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.sock_path))
        os.chmod(self.sock_path, 0o600)
        self.server.listen(4)
        self.registry = directory / f"{self.nonce}.json"
        self.write_registry()
        self.stop = False
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def write_registry(self, **over: Any) -> None:
        doc = {
            "v": 1,
            "versions": self.versions,
            "nonce": self.nonce,
            "pid": self.pid,
            "socket": str(self.sock_path),
            "sessionId": self.session,
            "generation": self.generation,
            "plugin": {"name": "omp-session-control", "version": "0.1.0"},
            "writtenAt": "2026-11-10T00:00:00.000Z",
        }
        doc.update(over)
        self.registry.write_text(json.dumps(doc))
        os.chmod(self.registry, 0o600)

    def serve(self) -> None:
        while not self.stop:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(conn,), daemon=True).start()

    def reply(self, req: dict[str, Any]) -> dict[str, Any]:
        op = req.get("op")
        base = {"v": 1, "id": req.get("id")}
        if req.get("nonce") != self.nonce:
            return {**base, "ok": False, "code": "bad_nonce", "message": "nonce"}
        if op == "snapshot":
            if self.behaviour == "refuse_snapshot":
                return {**base, "ok": False, "code": "disabled", "message": "control is off\x1b[31m"}
            session = "someone-else-entirely" if self.behaviour == "other_session" else self.session
            return {
                **base,
                "ok": True,
                "result": {
                    "sessionId": session,
                    "generation": self.generation,
                    "name": self.name,
                    "busy": True,
                    "pid": self.pid,
                    "protocol": 1,
                    "protocols": [1],
                    "ops": ["snapshot", "abort", "rename"],
                },
            }
        if self.behaviour == "stale_once" and not self.stale_sent:
            self.stale_sent = True
            self.generation += 1
            return {
                **base,
                "ok": False,
                "code": "stale_generation",
                "message": "moved",
                "detail": {"sessionId": self.session, "generation": self.generation},
            }
        if self.behaviour == "invalid":
            return {**base, "ok": False, "code": "invalid_args", "message": "name has invisible characters"}
        if req.get("sessionId") != self.session or req.get("generation") != self.generation:
            return {
                **base,
                "ok": False,
                "code": "stale_generation",
                "message": "stale",
                "detail": {"sessionId": self.session, "generation": self.generation},
            }
        if op == "abort":
            return {**base, "ok": True, "result": {"aborted": True}}
        if op == "rename":
            before, self.name = self.name, req["args"]["name"]
            return {**base, "ok": True, "result": {"before": before, "name": self.name}}
        return {**base, "ok": False, "code": "unknown_op", "message": "?"}

    def handle(self, conn: socket.socket) -> None:
        conn.settimeout(10)
        buf = b""
        try:
            while True:
                while b"\n" not in buf:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    buf += chunk
                line, buf = buf.split(b"\n", 1)
                req = json.loads(line)
                self.requests.append(req)
                b = self.behaviour
                if b == "silent":
                    time.sleep(30)
                elif b == "oversize":
                    conn.sendall(b"x" * 200_000)
                elif b == "trickle":
                    for _ in range(100):
                        conn.sendall(b" ")
                        time.sleep(0.1)
                elif b == "garbage":
                    conn.sendall(b"not json\n")
                elif b == "array":
                    conn.sendall(b"[1,2]\n")
                elif b == "spam":
                    for i in range(100):
                        conn.sendall(json.dumps({"v": 1, "id": f"other{i}", "ok": True, "result": {}}).encode() + b"\n")
                elif b == "closing":
                    conn.sendall(
                        json.dumps({"v": 1, "id": None, "ok": False, "code": "closing", "message": "bye"}).encode()
                        + b"\n"
                    )
                else:
                    conn.sendall(json.dumps(self.reply(req)).encode() + b"\n")
        except (OSError, ValueError):
            pass
        finally:
            conn.close()

    def close(self) -> None:
        self.stop = True
        self.server.close()


class ControlCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="odc", dir="/tmp")).resolve()
        os.chmod(self.root, 0o700)
        self.dir = self.root / "c"
        self.dir.mkdir(mode=0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.exts: list[FakeExtension] = []
        self.addCleanup(lambda: [e.close() for e in self.exts])

    def ext(self, **kw: Any) -> FakeExtension:
        e = FakeExtension(self.dir, **kw)
        self.exts.append(e)
        return e

    def cdir(self, timeout: float = 5.0) -> ControlDir:
        return ControlDir(self.dir, connect=lambda reg: Conn(reg, timeout=timeout))

    def refusal(self, fn: Any, status: int | None = None, code: str | None = None) -> ControlError:
        with self.assertRaises(ControlError) as ctx:
            fn()
        if status is not None:
            self.assertEqual(ctx.exception.status, status)
        if code is not None:
            self.assertEqual(ctx.exception.code, code)
        return ctx.exception


class Registry(ControlCase):
    def test_a_live_session_is_found_by_session_and_pid(self) -> None:
        e = self.ext()
        regs = self.cdir().registries()
        self.assertEqual([r["nonce"] for r in regs], [e.nonce])
        self.assertTrue(has_control(regs, SESSION, os.getpid()))
        self.assertFalse(has_control(regs, SESSION, os.getpid() + 1))
        self.assertFalse(has_control(regs, "other", os.getpid()))
        self.assertFalse(has_control(regs, None, None))

    def test_a_missing_directory_means_control_is_off_not_an_error(self) -> None:
        self.assertEqual(ControlDir(self.root / "nope").registries(), [])

    def test_a_directory_that_is_not_private_is_refused_with_a_clear_message(self) -> None:
        self.ext()
        os.chmod(self.dir, 0o755)
        self.refusal(self.cdir().registries, 503, "dir")
        os.chmod(self.dir, 0o700)
        link = self.root / "link"
        link.symlink_to(self.dir)
        self.refusal(ControlDir(link).registries, 503, "dir")

    def test_a_directory_of_another_account_is_refused(self) -> None:
        self.refusal(ControlDir(self.dir, uid=os.getuid() + 1).registries, 503, "dir")

    def test_a_directory_below_one_others_can_write_is_refused(self) -> None:
        loose = self.root / "loose"
        loose.mkdir()
        os.chmod(loose, 0o777)
        inner = loose / "c"
        inner.mkdir(mode=0o700)
        self.refusal(ControlDir(inner).registries, 503, "dir")

    def test_registry_files_that_are_not_ours_alone_are_skipped(self) -> None:
        e = self.ext()
        os.chmod(e.registry, 0o644)
        self.assertEqual(self.cdir().registries(), [])
        os.chmod(e.registry, 0o600)
        self.assertEqual(len(self.cdir().registries()), 1)
        real = self.root / "elsewhere.json"
        real.write_text(e.registry.read_text())
        os.chmod(real, 0o600)
        e.registry.unlink()
        e.registry.symlink_to(real)
        self.assertEqual(self.cdir().registries(), [], "a registry that is a symbolic link is never followed")

    def test_a_fifo_named_like_a_registry_cannot_stall_the_scan(self) -> None:
        self.ext()
        os.mkfifo(self.dir / "fifo.json", 0o600)
        started = time.monotonic()
        self.assertEqual(len(self.cdir().registries()), 1)
        self.assertLess(time.monotonic() - started, 3)

    def test_oversized_malformed_and_odd_registries_are_skipped(self) -> None:
        e = self.ext()
        (self.dir / "big.json").write_text(json.dumps({"pad": "x" * 5000}))
        (self.dir / "bad.json").write_text("{not json")
        (self.dir / "arr.json").write_text("[1]")
        for name in ("big.json", "bad.json", "arr.json"):
            os.chmod(self.dir / name, 0o600)
        for over in (
            {"nonce": "XYZ"},
            {"nonce": "A" * 32},
            {"pid": "12"},
            {"pid": True},
            {"sessionId": 5},
            {"socket": 5},
            {"versions": "1"},
        ):
            e.write_registry(**over)
            self.assertEqual(self.cdir().registries(), [], over)
        e.write_registry()
        self.assertEqual(len(self.cdir().registries()), 1)

    def test_a_socket_outside_the_directory_or_open_to_others_is_not_used(self) -> None:
        e = self.ext()
        e.write_registry(socket=str(self.root / "elsewhere.sock"))
        self.assertEqual(self.cdir().registries(), [])
        e.write_registry()
        os.chmod(e.sock_path, 0o666)
        self.assertEqual(self.cdir().registries(), [])
        os.chmod(e.sock_path, 0o600)
        plain = self.dir / "plain.sock"
        plain.write_text("")
        os.chmod(plain, 0o600)
        e.write_registry(socket=str(plain))
        self.assertEqual(self.cdir().registries(), [], "a regular file is not a socket")

    def test_versions_are_negotiated_and_an_unknown_one_means_no_contact(self) -> None:
        self.assertEqual(negotiate([1, 2, 3]), 1)
        self.assertIsNone(negotiate([2, 3]))
        self.assertIsNone(negotiate("1"))
        self.assertIsNone(negotiate([True]))
        self.ext(versions=[2])
        self.assertEqual(self.cdir().registries(), [])


class Conversation(ControlCase):
    def run_op(
        self, e: FakeExtension, op: str, args: dict[str, Any] | None = None, timeout: float = 5.0
    ) -> dict[str, Any]:
        return control(self.cdir(timeout), SESSION, e.pid, op, args)

    def test_stop_and_rename_pin_the_session_and_the_generation(self) -> None:
        e = self.ext()
        self.assertEqual(self.run_op(e, "abort"), {"aborted": True})
        self.assertEqual(self.run_op(e, "rename", {"name": "New name"}), {"before": None, "name": "New name"})
        ops = [r for r in e.requests if r["op"] != "snapshot"]
        for r in ops:
            self.assertEqual((r["v"], r["nonce"], r["sessionId"], r["generation"]), (1, e.nonce, SESSION, 0))
        self.assertEqual([r["op"] for r in e.requests], ["snapshot", "abort", "snapshot", "rename"])

    def test_a_generation_that_moved_is_retried_once_with_the_one_the_session_names(self) -> None:
        e = self.ext()
        e.behaviour = "stale_once"
        self.assertEqual(self.run_op(e, "abort"), {"aborted": True})
        self.assertEqual([r.get("generation") for r in e.requests if r["op"] == "abort"], [0, 1])

    def test_a_session_that_is_not_the_one_asked_for_is_refused_and_nothing_is_changed(self) -> None:
        e = self.ext()
        e.behaviour = "other_session"
        self.refusal(lambda: self.run_op(e, "abort"), 409, "stale_session")
        self.assertEqual([r["op"] for r in e.requests], ["snapshot"])

    def test_the_sessions_refusals_become_codes_and_the_text_is_cleaned(self) -> None:
        e = self.ext()
        e.behaviour = "invalid"
        self.refusal(lambda: self.run_op(e, "rename", {"name": "x"}), 400, "invalid_args")
        e.behaviour = "refuse_snapshot"
        err = self.refusal(lambda: self.run_op(e, "abort"), 409, "disabled")
        self.assertNotIn("\x1b", str(err))

    def test_a_session_without_a_live_socket_or_not_listed_is_no_control(self) -> None:
        e = self.ext()
        e.close()
        e.sock_path.unlink()
        self.refusal(
            lambda: self.run_op(e, "abort"), 409, "no_control"
        )  # a crashed session's leftover registry is skipped
        self.refusal(lambda: control(self.cdir(), "unknown-session", 1, "abort"), 409, "no_control")

    def test_hostile_replies_end_in_a_502_and_never_hang(self) -> None:
        for behaviour in ("oversize", "garbage", "array", "spam", "closing"):
            e = self.ext()
            e.behaviour = behaviour
            started = time.monotonic()
            err = self.refusal(lambda e=e: self.run_op(e, "abort", timeout=3), None)
            self.assertLess(time.monotonic() - started, 6, behaviour)
            if behaviour != "closing":
                self.assertEqual((err.status, err.code), (502, "socket"), behaviour)
            e.close()
            for p in self.dir.iterdir():
                p.unlink()

    def test_a_peer_that_trickles_bytes_is_cut_at_the_overall_deadline(self) -> None:
        e = self.ext()
        e.behaviour = "trickle"
        started = time.monotonic()
        self.refusal(lambda: self.run_op(e, "abort", timeout=1.0), 502, "socket")
        self.assertLess(time.monotonic() - started, 4)

    def test_a_silent_peer_times_out(self) -> None:
        e = self.ext()
        e.behaviour = "silent"
        started = time.monotonic()
        self.refusal(lambda: self.run_op(e, "abort", timeout=1.0), 502, "socket")
        self.assertLess(time.monotonic() - started, 4)

    def test_a_reply_over_64_kib_is_refused_by_bytes(self) -> None:
        self.assertEqual(control_module.MAX_REPLY_BYTES, 65536)
        with mock.patch.object(control_module, "MAX_REPLY_BYTES", 100):
            e = self.ext()
            e.behaviour = "oversize"
            self.refusal(lambda: self.run_op(e, "abort"), 502, "socket")

    def test_the_socket_path_is_inside_the_private_directory_even_if_the_registry_says_otherwise(self) -> None:
        e = self.ext()
        decoy = self.root / "decoy"
        decoy.mkdir(mode=0o700)
        other = FakeExtension(decoy)
        self.exts.append(other)
        e.write_registry(socket=str(other.sock_path))
        self.refusal(lambda: self.run_op(e, "abort"), 409, "no_control")
        self.assertEqual(other.requests, [], "nothing was sent to a socket outside the control directory")


class Modes(unittest.TestCase):
    def test_the_fake_peer_really_is_private(self) -> None:
        d = Path(tempfile.mkdtemp(prefix="odc", dir="/tmp"))
        self.addCleanup(shutil.rmtree, d, True)
        os.chmod(d, 0o700)
        e = FakeExtension(d)
        self.addCleanup(e.close)
        self.assertEqual(stat.S_IMODE(os.stat(e.sock_path).st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
