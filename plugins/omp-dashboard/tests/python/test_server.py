"""The hub over HTTP: pairing, the gates in front of every API call, bounded reads, fixed answers, and the session actions."""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path

from omp_dashboard.pairing import cookie_hash
from support import PLUGIN, Client, ScratchTestCase, host_entry


def raw_exchange(port: int, data: bytes, read: bool = True, wait: float = 5.0) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=wait) as s:
        s.sendall(data)
        if not read:
            return b""
        out = b""
        try:
            while chunk := s.recv(65536):
                out += chunk
        except (TimeoutError, ConnectionResetError, OSError):
            pass
        return out


class Gates(ScratchTestCase):
    def setUp(self) -> None:
        self.s = self.make([host_entry(1), host_entry(2, busy=True)])
        self.hub = self.hub(self.s)
        self.c = self.hub.client

    def test_the_page_is_served_unpaired_with_a_strict_policy_and_the_api_is_not(self) -> None:
        page = self.c.get("/")
        self.assertEqual(page.status, 200)
        csp = page.headers["Content-Security-Policy"]
        for want in (
            "default-src 'self'",
            "script-src 'self'",
            "object-src 'none'",
            "frame-ancestors 'none'",
            "connect-src 'self'",
        ):
            self.assertIn(want, csp)
        self.assertNotIn("http", csp.replace("http-equiv", ""))  # no third-party origin anywhere
        for name, want in (
            ("X-Content-Type-Options", "nosniff"),
            ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "no-referrer"),
        ):
            self.assertEqual(page.headers[name], want)
        self.assertNotIn("Python", page.headers.get("Server", ""))
        for path in ("/api/hub/sessions", "/api/hub/machine", "/api/hub/alerts", "/api/hub/projects", "/api/hub/relay"):
            self.assertEqual(self.c.get(path).status, 401, path)
        self.assertEqual(self.c.get("/api/other").status, 404)

    def test_pairing_sets_a_cookie_that_is_httponly_samesite_and_not_secure_over_loopback_http(self) -> None:
        token = self.hub.app.pairing.writer.create_token()
        resp = self.c.pair_with(token)
        cookie = resp.headers["Set-Cookie"]
        self.assertIn("omp_dashboard=", cookie)
        for flag in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(flag, cookie)
        self.assertNotIn("Secure", cookie)
        self.assertEqual(self.c.get("/api/hub/machine").status, 200)
        self.assertEqual(
            self.c.post("/api/hub/pair", {"token": token}, headers={"Cookie": ""}).status, 403, "a token works once"
        )

    def test_a_forged_cookie_and_a_revoked_one_are_refused(self) -> None:
        self.c.cookie = "omp_dashboard=forged"
        self.assertEqual(self.c.get("/api/hub/machine").status, 401)
        self.hub.pair()
        self.assertEqual(self.c.get("/api/hub/machine").status, 200)
        self.hub.app.pairing.writer.revoke(None)
        self.assertEqual(self.c.get("/api/hub/machine").status, 401)

    def test_a_request_for_another_host_name_is_refused_even_with_a_valid_cookie(self) -> None:
        self.hub.pair()
        for host in ("evil.example", f"evil.example:{self.s.port}", "127.0.0.1:1", ""):
            r = self.c.get("/api/hub/machine", headers={"Host": host})
            self.assertEqual(r.status, 403, host)
        self.assertEqual(self.c.get("/", headers={"Host": "evil.example"}).status, 403)

    def test_proxy_headers_make_loopback_only_refuse(self) -> None:
        self.hub.pair()
        for h in ("Tailscale-User-Login", "X-Forwarded-For", "Forwarded", "Tailscale-Funnel-Request", "X-Real-Ip"):
            self.assertEqual(self.c.get("/api/hub/machine", headers={h: "x"}).status, 403, h)

    def test_posts_need_the_custom_header_and_the_hubs_own_origin(self) -> None:
        self.hub.pair()
        path = "/api/hub/alerts/test"
        self.assertEqual(self.c.request("POST", path, body={}, post_headers=False).status, 403)
        self.assertEqual(self.c.request("POST", path, body={}, headers={"Origin": "http://evil.example"}).status, 403)
        self.assertEqual(
            self.c.request(
                "POST", path, body={}, headers={"Origin": f"http://127.0.0.1:{self.s.port}", "X-Omp-Dashboard": "0"}
            ).status,
            403,
        )
        no_origin = self.c.request("POST", path, body={}, headers={"Origin": None})  # type: ignore[dict-item]
        self.assertEqual(no_origin.status, 403)
        self.assertEqual(self.c.post(path).status, 400, "with both, it reaches the handler (no topic is set)")
        self.assertEqual(self.c.request("POST", "/api/hub/pair", body={"token": "x"}, post_headers=False).status, 403)

    def test_get_only_and_unknown_methods(self) -> None:
        self.assertEqual(self.c.request("POST", "/", body={}).status, 405)
        for method in ("PUT", "DELETE", "PATCH", "OPTIONS"):
            self.assertEqual(self.c.request(method, "/api/hub/sessions").status, 405, method)

    def test_bad_paths_are_refused_before_routing(self) -> None:
        self.hub.pair()
        for target in (
            "/api/hub/../hub/machine",
            "/api/hub/%2e%2e/machine",
            "/a%5Cb",
            "/a%00b",
            "/api/hub/machine#x",
            "/\u00e9",
        ):
            r = raw_exchange(self.s.port, f"GET {target} HTTP/1.0\r\nHost: 127.0.0.1:{self.s.port}\r\n\r\n".encode())
            self.assertIn(b" 400 ", r.split(b"\r\n", 1)[0], target)

    def test_errors_are_fixed_texts_that_never_reflect_the_request(self) -> None:
        r = raw_exchange(
            self.s.port, f"GET /<script>alert(1)</script> HTTP/1.0\r\nHost: 127.0.0.1:{self.s.port}\r\n\r\n".encode()
        )
        self.assertNotIn(b"script", r.split(b"\r\n\r\n", 1)[1])
        page = self.c.get("/nope-" + "x" * 20)
        self.assertNotIn(b"nope-", page.body)
        self.assertEqual(page.json(), {"error": "not found"})


class Limits(ScratchTestCase):
    def setUp(self) -> None:
        self.s = self.make([host_entry(1)])
        self.hub = self.hub(self.s)
        self.hub.pair()
        self.c = self.hub.client

    def test_a_huge_header_block_is_cut_with_431(self) -> None:
        r = raw_exchange(self.s.port, b"GET / HTTP/1.0\r\nHost: x\r\n" + b"X-A: " + b"a" * 20000 + b"\r\n\r\n")
        self.assertIn(b" 431 ", r.split(b"\r\n", 1)[0])
        many = b"GET / HTTP/1.0\r\n" + b"".join(b"X-%d: %s\r\n" % (i, b"a" * 200) for i in range(120)) + b"\r\n"
        self.assertIn(b" 431 ", raw_exchange(self.s.port, many).split(b"\r\n", 1)[0])
        self.assertEqual(self.c.get("/api/hub/machine").status, 200, "the hub is still serving")

    def test_a_request_line_over_the_budget_is_cut(self) -> None:
        r = raw_exchange(self.s.port, b"GET /" + b"a" * 30000 + b" HTTP/1.0\r\n\r\n")
        self.assertTrue(r == b"" or b" 431 " in r.split(b"\r\n", 1)[0] or b" 414 " in r.split(b"\r\n", 1)[0])

    def test_a_client_that_trickles_its_headers_is_cut_at_the_deadline(self) -> None:
        import omp_dashboard.server as server

        old = server.HEADER_DEADLINE_S
        server.HEADER_DEADLINE_S = 0.6
        try:
            with socket.create_connection(("127.0.0.1", self.s.port), timeout=5) as sock:
                started = time.monotonic()
                try:
                    for byte in b"GET / HTTP/1.0\r\nHost: x":
                        sock.sendall(bytes([byte]))
                        time.sleep(0.15)
                    gone = False
                except OSError:
                    gone = True
                self.assertTrue(gone or time.monotonic() - started < 5)
                self.assertEqual(sock.recv(100) if not gone else b"", b"")
        finally:
            server.HEADER_DEADLINE_S = old

    def test_body_limits_and_shapes(self) -> None:
        p = "/api/hub/start"
        self.assertEqual(self.c.post(p, raw_body=b"x" * 5000, body={}).status, 413)
        self.assertEqual(self.c.request("POST", p, raw_body=b"{not json").status, 400)
        self.assertEqual(self.c.request("POST", p, raw_body=b"[1]").status, 400)
        self.assertEqual(
            self.c.request("POST", p, raw_body=b"[" * 2000 + b"]" * 2000).status,
            400,
            "deep nesting is a 400, not a crash",
        )
        self.assertEqual(self.c.request("POST", p, headers={"Content-Length": "-1"}, raw_body=b"").status, 400)
        self.assertEqual(self.c.request("POST", p, headers={"Transfer-Encoding": "chunked"}, raw_body=b"").status, 400)

    def test_a_body_that_ends_early_is_refused(self) -> None:
        head = f"POST /api/hub/start HTTP/1.0\r\nHost: 127.0.0.1:{self.s.port}\r\nOrigin: http://127.0.0.1:{self.s.port}\r\nX-Omp-Dashboard: 1\r\nContent-Type: application/json\r\nCookie: {self.c.cookie}\r\nContent-Length: 50\r\n\r\n{{".encode()
        with socket.create_connection(("127.0.0.1", self.s.port), timeout=5) as s:
            s.sendall(head)
            s.shutdown(socket.SHUT_WR)
            self.assertIn(b" 400 ", s.recv(4096).split(b"\r\n", 1)[0])

    def test_pairing_attempts_are_limited_and_a_success_gives_its_slot_back(self) -> None:
        fresh = Client(self.s.port)
        for _ in range(10):
            self.assertEqual(fresh.post("/api/hub/pair", {"token": "wrong"}).status, 403)
        self.assertEqual(fresh.post("/api/hub/pair", {"token": "wrong"}).status, 429)
        # A token that is on file cannot be a guess, so it still pairs while wrong guesses are being refused (see test_review1).
        self.assertEqual(fresh.post("/api/hub/pair", {"token": self.hub.app.pairing.writer.create_token()}).status, 200)
        self.assertEqual(fresh.post("/api/hub/pair", {"token": "wrong"}).status, 429)

    def test_healthz_is_for_a_direct_loopback_client_only(self) -> None:
        ok = Client(self.s.port).get("/healthz")
        self.assertEqual(ok.status, 200)
        self.assertLess(abs(ok.json()["updatedAt"] - time.time()), 5)
        for headers in (
            {"Host": "evil.example"},
            {"Origin": "http://x"},
            {"X-Forwarded-For": "1.1.1.1"},
            {"Tailscale-User-Login": "a@b"},
        ):
            self.assertEqual(Client(self.s.port).get("/healthz", headers=headers).status, 403, headers)
        self.assertEqual(Client(self.s.port).request("POST", "/healthz", body={}).status, 403)


class Static(ScratchTestCase):
    def test_etag_revalidation_and_only_the_pinned_files(self) -> None:
        s = self.make()
        hub = self.hub(s)
        first = hub.client.get("/index.js")
        self.assertEqual(first.status, 200)
        self.assertEqual(hub.client.get("/index.js", headers={"If-None-Match": first.headers["ETag"]}).status, 304)
        self.assertEqual(hub.client.get("/index.html").headers["Content-Type"], "text/html; charset=utf-8")
        for path in ("/ui-build.json", "/../ui-build.json", "/index.js.map", "/favicon.ico"):
            self.assertIn(hub.client.get(path).status, (400, 404), path)
        head = hub.client.request("HEAD", "/index.js")
        self.assertEqual((head.status, head.body), (200, b""))


class Sessions(ScratchTestCase):
    def setUp(self) -> None:
        self.s = self.make([host_entry(1, busy=True), host_entry(2)], OMP_DASHBOARD_RELAY_ENABLED="false")
        self.hub = self.hub(self.s)
        self.hub.pair()
        self.c = self.hub.client

    def audit_lines(self) -> list[dict]:
        path = self.hub.config.audit_file
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_the_list_has_only_known_fields_and_no_link(self) -> None:
        self.s.hosts[0]["url"] = "http://secret/#room"
        self.s.hosts[0]["token"] = "t"
        self.s.save_fake()
        body = self.c.get("/api/hub/sessions").json()
        self.assertEqual(len(body["sessions"]), 2)
        flat = json.dumps(body)
        self.assertNotIn("secret", flat)
        self.assertNotIn('"token"', flat)
        self.assertEqual({h["relay"] for h in body["sessions"]}, {"off"})
        self.assertFalse(any(h["control"] for h in body["sessions"]))

    def test_open_is_refused_without_a_relay_and_never_calls_omp_link(self) -> None:
        r = self.c.post("/api/hub/link", {"instanceId": "inst0001", "view": True})
        self.assertEqual((r.status, r.json()["code"]), (409, "relay_off"))
        self.assertEqual(self.c.post("/api/hub/link", {"instanceId": "inst0001", "allowPublic": True}).status, 400)

    def test_start_is_off_by_default_and_lists_nothing(self) -> None:
        self.assertEqual(self.c.get("/api/hub/projects").json(), {"backend": "off", "projects": []})
        self.assertEqual(self.c.post("/api/hub/start", {"path": "/tmp"}).json()["code"], "start_off")

    def test_kill_needs_the_typed_name_and_every_refusal_is_audited_without_names(self) -> None:
        sid = self.s.hosts[0]["sessionId"]
        r = self.c.post(f"/api/hub/sessions/{sid}/kill", {"confirm": "wrong"})
        self.assertEqual((r.status, r.json()["code"]), (400, "confirm"))
        self.assertEqual(self.c.post("/api/hub/sessions/nonexistent-session-id/stop").status, 404)
        self.assertEqual(self.c.post("/api/hub/sessions/x/stop").status, 400)
        self.assertEqual(self.c.post(f"/api/hub/sessions/{sid}/rename", {"name": 5}).status, 400)
        r = self.c.post(f"/api/hub/sessions/{sid}/stop")
        self.assertEqual((r.status, r.json()["code"]), (409, "no_control"))
        lines = self.audit_lines()
        self.assertEqual(lines[0]["outcome"], "paired")
        self.assertEqual([x["action"] for x in lines], ["pair", "kill", "stop", "rename", "stop"])
        text = Path(self.hub.config.audit_file).read_text()
        for secret in ("session 1", "wrong", "/work/project1"):
            self.assertNotIn(secret, text)
        self.assertTrue(all(x["browser"] == cookie_hash(self.c.cookie.split("=", 1)[1])[:8] for x in lines[1:]))
        self.assertEqual(oct(os.stat(self.hub.config.audit_file).st_mode & 0o777), "0o600")

    def test_an_omp_that_fails_is_a_503_with_a_fixed_text(self) -> None:
        self.s.fake_state["fail"] = True
        self.s.save_fake()
        r = self.c.get("/api/hub/sessions")
        self.assertEqual(r.status, 503)
        self.assertNotIn(str(self.s.omp_path), r.body.decode())

    def test_the_machine_name_comes_from_omp_then_the_host(self) -> None:
        self.assertTrue(self.c.get("/api/hub/machine").json()["name"])
        self.assertEqual(
            self.c.get("/api/hub/machine").json()["noun"], "Mac" if os.uname().sysname == "Darwin" else "machine"
        )


class UiBundle(ScratchTestCase):
    def test_the_committed_page_matches_its_pins_and_a_changed_byte_is_refused(self) -> None:
        import shutil
        import tempfile

        from omp_dashboard.ui import UiBundleError, load_bundle

        bundle = load_bundle()
        self.assertEqual(set(bundle.assets), {"index.html", "index.js", "index.css", "theme.js", "favicon.svg"})
        copy = Path(tempfile.mkdtemp(prefix="odb", dir="/tmp"))
        self.addCleanup(shutil.rmtree, copy, True)
        os.chmod(copy, 0o700)
        for name in ("ui-dist", "ui-build.json"):
            src = PLUGIN / name
            (shutil.copytree if src.is_dir() else shutil.copy)(src, copy / name)
        load_bundle(copy)
        js = copy / "ui-dist" / "index.js"
        js.write_bytes(js.read_bytes() + b" ")
        with self.assertRaisesRegex(UiBundleError, "pinned hash"):
            load_bundle(copy)
        js.write_bytes(js.read_bytes()[:-1])
        (copy / "ui-dist" / "extra.js").write_text("x")
        with self.assertRaisesRegex(UiBundleError, "exactly the pinned files"):
            load_bundle(copy)
        (copy / "ui-dist" / "extra.js").unlink()
        os.chmod(copy / "ui-dist", 0o777)
        with self.assertRaisesRegex(UiBundleError, "not trusted"):
            load_bundle(copy)

    def test_a_damaged_page_is_a_503_and_the_api_still_works(self) -> None:
        s = self.make()
        hub = self.hub(s)
        hub.app.bundle = None
        hub.app.bundle_error = "damaged"
        self.assertEqual(hub.client.get("/").status, 503)
        hub.pair()
        self.assertEqual(hub.client.get("/api/hub/machine").status, 200)
