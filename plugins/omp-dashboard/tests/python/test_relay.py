"""relay-check/1 from the verifying side. The relay is a stand-in HTTP server on a free loopback port that answers the way the real one does
(HMAC under a key file) or the way an impostor would; an impostor must never be believed."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from omp_dashboard import relay
from omp_dashboard.hostplat import PlatformError
from omp_dashboard.relay import RelayCheckError
from support import PLUGIN, free_port  # noqa: F401


class FakePlatform:
    kind = "linux"

    def __init__(self, uids: dict[int, set[int]] | Exception):
        self.uids = uids

    def listener_uids(self, port: int) -> set[int]:
        if isinstance(self.uids, Exception):
            raise self.uids
        return self.uids.get(port, set())


class FakeRelay:
    def __init__(self, key: bytes, mode: str = "good") -> None:
        self.key = key
        self.mode = mode
        self.port = free_port()
        self.seen: list[tuple[str, dict[str, str]]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: object) -> None:
                pass

            def do_GET(self) -> None:
                outer.seen.append((self.path, dict(self.headers)))
                path, _, query = self.path.partition("?")
                nonce = query.partition("nonce=")[2]
                port = outer.port
                if outer.mode == "redirect":
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{port}/__relay_check?nonce={nonce}&again=1")
                    self.end_headers()
                    return
                if outer.mode == "status":
                    self.send_response(503)
                    self.end_headers()
                    return
                if path == "/__relay_check":
                    body = self.check_body(port, nonce)
                elif path == "/__relay_stats":
                    body = self.stats_body(port, nonce)
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def check_body(self, port: int, nonce: str) -> bytes:
                key = outer.key if outer.mode != "wrong-key" else secrets.token_bytes(32)
                mac = hmac.new(key, f"omp-private-relay-check/1:{port}:{nonce}".encode(), hashlib.sha256).hexdigest()
                if outer.mode == "lookalike":
                    mac = "0" * 64
                if outer.mode == "other-port":  # an answer for the other listener, replayed
                    mac = hmac.new(
                        key, f"omp-private-relay-check/1:{port + 1}:{nonce}".encode(), hashlib.sha256
                    ).hexdigest()
                if outer.mode == "not-hex":
                    mac = "z" * 64
                if outer.mode == "uppercase":
                    mac = mac.upper()
                if outer.mode == "oversize":
                    return json.dumps({"mac": mac, "pad": "x" * 5000}).encode()
                if outer.mode == "not-json":
                    return b"hello"
                if outer.mode == "array":
                    return b"[1]"
                return json.dumps({"mac": mac}).encode()

            def stats_body(self, port: int, nonce: str) -> bytes:
                text = json.dumps(
                    {
                        "rooms": 3,
                        "guests": 1,
                        "uptimeS": 90,
                        "staticReady": True,
                        "ready": True,
                        "build": {"ompTag": "v18.18.3"},
                    }
                )
                if outer.mode == "wide-stats":  # 40 000 two-byte characters: under 65 536 characters, over 65 536 bytes
                    text = json.dumps({"rooms": 1, "pad": "é" * 40_000}, ensure_ascii=False)
                mac = hmac.new(
                    outer.key, f"omp-private-relay-check/1:stats:{port}:{nonce}:{text}".encode(), hashlib.sha256
                ).hexdigest()
                if outer.mode == "stats-wrong-mac":
                    mac = "1" * 64
                if outer.mode == "stats-tampered":
                    text = text.replace('"rooms": 3', '"rooms": 99')
                return json.dumps({"body": text, "mac": mac}, ensure_ascii=False).encode()

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class RelayCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="odr", dir="/tmp")).resolve()
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.key = secrets.token_bytes(32)
        self.check = self.root / "check"
        self.check.mkdir(mode=0o700)
        (self.check / "check.key").write_text(self.key.hex() + "\n")
        os.chmod(self.check / "check.key", 0o600)

    def relay(self, mode: str = "good") -> FakeRelay:
        r = FakeRelay(self.key, mode)
        self.addCleanup(r.close)
        return r


class KeyFile(RelayCase):
    def test_a_private_key_is_read(self) -> None:
        self.assertEqual(relay.read_check_key(self.check), self.key)

    def test_every_way_the_key_can_be_unsafe_is_refused(self) -> None:
        key = self.check / "check.key"
        for mode in (0o640, 0o604, 0o666):
            os.chmod(key, mode)
            with self.assertRaises(RelayCheckError):
                relay.read_check_key(self.check)
        os.chmod(key, 0o600)
        os.chmod(self.check, 0o755)
        with self.assertRaises(RelayCheckError):
            relay.read_check_key(self.check)
        os.chmod(self.check, 0o700)
        link = self.root / "linkdir"
        link.symlink_to(self.check)
        with self.assertRaises(RelayCheckError):
            relay.read_check_key(link)
        key.unlink()
        real = self.root / "real.key"
        real.write_text(self.key.hex())
        os.chmod(real, 0o600)
        key.symlink_to(real)
        with self.assertRaises(RelayCheckError):
            relay.read_check_key(self.check)

    def test_a_fifo_malformed_or_oversized_key_is_refused_without_blocking(self) -> None:
        key = self.check / "check.key"
        key.unlink()
        os.mkfifo(key, 0o600)
        with self.assertRaises(RelayCheckError):
            relay.read_check_key(self.check)
        key.unlink()
        for text in ("zz" * 32, "ab" * 31, "AB" * 32, "ab" * 32 + "\n" + "x" * 2000):
            key.write_text(text)
            os.chmod(key, 0o600)
            with self.assertRaises(RelayCheckError, msg=text[:10]):
                relay.read_check_key(self.check)
            key.unlink()

    def test_a_missing_directory_or_key_says_so(self) -> None:
        with self.assertRaisesRegex(RelayCheckError, "no check directory"):
            relay.read_check_key(self.root / "absent")
        (self.check / "check.key").unlink()
        with self.assertRaisesRegex(RelayCheckError, "no check key"):
            relay.read_check_key(self.check)


class Proof(RelayCase):
    def test_the_real_relay_proves_itself_and_the_request_is_a_plain_direct_one(self) -> None:
        r = self.relay()
        relay.prove_listener(r.port, self.key)
        path, headers = r.seen[0]
        nonce = path.split("nonce=")[1]
        self.assertRegex(nonce, r"^[0-9a-f]{32}$")
        self.assertEqual(headers["Host"], f"127.0.0.1:{r.port}")
        for forbidden in ("Origin", "Cookie", "Authorization", "X-Forwarded-For", "Tailscale-User-Login"):
            self.assertNotIn(forbidden, headers)

    def test_every_nonce_is_fresh(self) -> None:
        r = self.relay()
        relay.prove_listener(r.port, self.key)
        relay.prove_listener(r.port, self.key)
        self.assertNotEqual(r.seen[0][0], r.seen[1][0])

    def test_an_impostor_is_never_believed(self) -> None:
        for mode in (
            "lookalike",
            "wrong-key",
            "other-port",
            "not-hex",
            "uppercase",
            "oversize",
            "not-json",
            "array",
            "status",
            "redirect",
        ):
            r = self.relay(mode)
            with self.assertRaises(RelayCheckError, msg=mode):
                relay.prove_listener(r.port, self.key)
        self.assertEqual(len([s for s in r.seen if "again=1" in s[0]]), 0, "a redirect is never followed")

    def test_nobody_listening_is_an_error_not_a_pass(self) -> None:
        with self.assertRaises(RelayCheckError):
            relay.prove_listener(free_port(), self.key, timeout=1)

    def test_an_answer_over_the_bound_is_refused_by_bytes(self) -> None:
        self.assertEqual((relay.MAX_PROOF_BYTES, relay.MAX_STATS_BYTES), (1024, 65536))
        r = self.relay("wide-stats")
        with self.assertRaisesRegex(RelayCheckError, "counts could not be read"):
            relay.fetch_stats(r.port, self.key)  # 40 000 characters, but more than 65 536 bytes


class Stats(RelayCase):
    def test_proven_counts_are_shown(self) -> None:
        r = self.relay()
        stats = relay.fetch_stats(r.port, self.key)
        self.assertEqual((stats["rooms"], stats["guests"]), (3, 1))

    def test_counts_without_the_proof_or_altered_are_not_believed(self) -> None:
        for mode in ("stats-wrong-mac", "stats-tampered", "status", "redirect"):
            r = self.relay(mode)
            with self.assertRaises(RelayCheckError, msg=mode):
                relay.fetch_stats(r.port, self.key)

    def test_health_reports_a_problem_instead_of_raising(self) -> None:
        r = self.relay()
        platform = FakePlatform({r.port: {os.getuid()}})
        ok = relay.health(r.port, self.check, platform)
        self.assertEqual(
            (ok["ok"], ok["rooms"], ok["guests"], ok["uptime_s"], ok["build"]), (True, 3, 1, 90, "v18.18.3")
        )
        impostor = self.relay("stats-wrong-mac")
        bad = relay.health(impostor.port, self.check, FakePlatform({impostor.port: {os.getuid()}}))
        self.assertFalse(bad["ok"])
        self.assertNotIn("rooms", bad)
        self.assertFalse(relay.health(r.port, self.check, FakePlatform({r.port: {os.getuid() + 1}}))["ok"])
        self.assertFalse(relay.health(r.port, self.check, FakePlatform({}))["ok"])
        self.assertFalse(relay.health(r.port, self.check, FakePlatform(PlatformError("no lsof")))["ok"])
        self.assertFalse(relay.health(r.port, self.root / "absent", platform)["ok"])


class Ports(RelayCase):
    def two(self, mode_guest: str = "good", mode_host: str = "good") -> tuple[FakeRelay, FakeRelay]:
        return self.relay(mode_guest), self.relay(mode_host)

    def test_both_listeners_ours_and_proven_passes(self) -> None:
        guest, host = self.two()
        platform = FakePlatform({guest.port: {os.getuid()}, host.port: {os.getuid()}})
        relay.check_ports((guest.port, host.port), self.check, platform)

    def test_a_listener_of_another_account_or_one_we_cannot_see_is_refused_before_anything_is_asked(self) -> None:
        guest, host = self.two()
        for uids in (
            {guest.port: {os.getuid() + 1}, host.port: {os.getuid()}},
            {guest.port: set(), host.port: {os.getuid()}},
            {guest.port: {os.getuid(), os.getuid() + 1}, host.port: {os.getuid()}},
        ):
            with self.assertRaisesRegex(RelayCheckError, "not held by our relay"):
                relay.check_ports((guest.port, host.port), self.check, FakePlatform(uids))
        self.assertEqual(guest.seen, [], "an owner that is not us is refused without sending the challenge")
        with self.assertRaisesRegex(RelayCheckError, "could not check"):
            relay.check_ports((guest.port, host.port), self.check, FakePlatform(PlatformError("x")))

    def test_the_right_owner_with_a_look_alike_on_either_port_is_refused(self) -> None:
        for bad_guest in (True, False):
            guest, host = self.two("lookalike" if bad_guest else "good", "good" if bad_guest else "lookalike")
            platform = FakePlatform({guest.port: {os.getuid()}, host.port: {os.getuid()}})
            with self.assertRaisesRegex(RelayCheckError, "not our relay"):
                relay.check_ports((guest.port, host.port), self.check, platform)

    def test_a_missing_key_refuses_everything(self) -> None:
        guest, host = self.two()
        (self.check / "check.key").unlink()
        with self.assertRaises(RelayCheckError):
            relay.check_ports(
                (guest.port, host.port), self.check, FakePlatform({guest.port: {os.getuid()}, host.port: {os.getuid()}})
            )


if __name__ == "__main__":
    unittest.main()
