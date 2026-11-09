"""The sign-in adapters, alone and through the hub: loopback-only, tailscale-serve (with a stand-in `tailscale` command), forged headers,
and the refusal to bind anywhere but a loopback address."""

from __future__ import annotations

import json
import os
import stat
import sys
import unittest
from pathlib import Path

from omp_dashboard.auth import LoopbackOnly, TailscaleServe, check_loopback_bind, loopback_address
from omp_dashboard.server import HubApp, HubServer
from omp_dashboard.util import DashboardError
from support import PLUGIN, Client, ScratchTestCase, host_entry  # noqa: F401

STATUS = {
    "Self": {"DNSName": "box.tail1234.ts.example.", "UserID": 7},
    "User": {"7": {"LoginName": "me@example.test"}},
}


def tailscale_script(directory: Path, status: dict | None = STATUS, fail: bool = False) -> Path:
    """A `tailscale` that prints a status and counts how often it was asked."""
    path = directory / "tailscale"
    counter = directory / "asked"
    path.write_text(
        f"#!{sys.executable}\nimport sys\nopen({str(counter)!r}, 'a').write('x')\n"
        + ("sys.exit(1)\n" if fail else f"print({json.dumps(json.dumps(status))})\n")
    )
    os.chmod(path, 0o700)
    return path


class Headers(dict):
    def get(self, name: str, default=None):  # type: ignore[override]
        return super().get(name, default)


def req(host: str, login: str | None = "me@example.test", **extra: str) -> Headers:
    h = Headers({"Host": host})
    if login is not None:
        h["Tailscale-User-Login"] = login
    h.update(extra)
    return h


class Bind(unittest.TestCase):
    def test_only_loopback_addresses_are_accepted(self) -> None:
        for good in ("127.0.0.1", "127.5.5.5", "::1"):
            self.assertTrue(loopback_address(good))
            check_loopback_bind(good, "loopback-only")
        for bad in (
            "0.0.0.0",
            "::",
            "192.168.1.5",
            "10.0.0.2",
            "100." + "64.0.1",
            "localhost",
            "example.com",
            "",
            "127.0.0.1 ",
            "::ffff:8.8.8.8",
        ):
            self.assertFalse(loopback_address(bad), bad)
            with self.assertRaisesRegex(DashboardError, "refusing to listen"):
                check_loopback_bind(bad, "loopback-only")

    def test_both_adapters_refuse_a_non_loopback_bind(self) -> None:
        with self.assertRaises(DashboardError):
            LoopbackOnly("0.0.0.0", 1, []).check_bind("0.0.0.0")
        with self.assertRaises(DashboardError):
            TailscaleServe(None, 443, "a", "b").check_bind("192.168.0.9")

    def test_the_hub_itself_will_not_start_on_such_an_address(self) -> None:
        from support import Scratch

        s = Scratch()
        self.addCleanup(s.close)
        s.write_config({"listen": {"host": "::1"}})
        config = s.config()
        self.assertEqual(config.problems, [])
        config.values["listen"]["host"] = "0.0.0.0"  # even if validation were bypassed, serve() checks the adapter
        from omp_dashboard.server import serve

        with self.assertRaises(DashboardError):
            serve(config, s.env)


class Loopback(unittest.TestCase):
    def setUp(self) -> None:
        self.a = LoopbackOnly("127.0.0.1", 4000, ["fwd.example.test:4999"])

    def test_the_hosts_it_answers_for(self) -> None:
        for host in ("127.0.0.1:4000", "localhost:4000", "[::1]:4000", "LOCALHOST:4000", "fwd.example.test:4999"):
            self.assertIsNone(self.a.check(Headers({"Host": host})), host)
        for host in (
            "127.0.0.1",
            "127.0.0.1:4001",
            "evil.example:4000",
            "127.0.0.1.evil.example:4000",
            "",
            "0.0.0.0:4000",
            "localhost.:4000",
        ):
            denied = self.a.check(Headers({"Host": host}))
            self.assertIsNotNone(denied, host)
            self.assertEqual(denied.status, 403)

    def test_any_sign_of_a_proxy_is_refused(self) -> None:
        for name in (
            "Tailscale-User-Login",
            "Tailscale-Funnel-Request",
            "X-Forwarded-For",
            "X-Forwarded-Host",
            "Forwarded",
            "X-Real-Ip",
        ):
            self.assertEqual(self.a.check(Headers({"Host": "127.0.0.1:4000", name: "x"})).status, 403, name)

    def test_origins_and_url(self) -> None:
        self.assertIn("http://127.0.0.1:4000", self.a.origins())
        self.assertIn("http://fwd.example.test:4999", self.a.origins())
        self.assertNotIn("https://127.0.0.1:4000", self.a.origins())
        self.assertEqual(self.a.public_url(), "http://127.0.0.1:4000/")
        self.assertEqual(LoopbackOnly("::1", 4000, []).public_url(), "http://[::1]:4000/")
        self.assertFalse(self.a.secure_cookie())


class Tailscale(unittest.TestCase):
    def setUp(self) -> None:
        from support import Scratch

        self.s = Scratch()
        self.addCleanup(self.s.close)
        self.asked = self.s.bin / "asked"
        self.ts = tailscale_script(self.s.bin)
        self.now = [0.0]

    def adapter(self, **kw) -> TailscaleServe:
        kw.setdefault("bin_path", str(self.ts))
        return TailscaleServe(
            kw.pop("bin_path"),
            kw.pop("serve_port", 8443),
            kw.pop("owner", None),
            kw.pop("public_host", None),
            env=self.s.env,
            clock=lambda: self.now[0],
            **kw,
        )

    def good(self) -> Headers:
        return req("box.tail1234.ts.example:8443")

    def test_the_owner_through_serve_is_let_through(self) -> None:
        a = self.adapter()
        self.assertIsNone(a.check(self.good()))
        self.assertIsNone(a.check(req("box:8443")), "the short name is a name of this machine too")
        self.assertEqual(a.public_url(), "https://box.tail1234.ts.example:8443/")
        self.assertTrue(a.secure_cookie())
        self.assertEqual(a.origins(), {"https://box.tail1234.ts.example:8443", "https://box:8443"})

    def test_forged_and_missing_headers_are_refused(self) -> None:
        a = self.adapter()
        for headers in (
            req("box.tail1234.ts.example:8443", login=None),
            req("box.tail1234.ts.example:8443", login="intruder@example.test"),
            req("box.tail1234.ts.example:8443", login=""),
            req("box.tail1234.ts.example:8443", login="ME@example.test"),
            req("box.tail1234.ts.example:8443", **{"Tailscale-Funnel-Request": "?1"}),
            req("evil.example:8443"),
            req("box.tail1234.ts.example:9999"),
            req("box.tail1234.ts.example:8443.evil"),
            req("127.0.0.1:8443"),
            req(""),
        ):
            denied = a.check(headers)
            self.assertIsNotNone(denied, dict(headers))
            self.assertEqual(denied.status, 403, dict(headers))

    def test_the_default_https_port_may_omit_the_port_in_host(self) -> None:
        a = self.adapter(serve_port=443)
        self.assertIsNone(a.check(req("box.tail1234.ts.example")))
        self.assertIsNone(a.check(req("box.tail1234.ts.example:443")))
        self.assertEqual(a.public_url(), "https://box.tail1234.ts.example/")
        self.assertIsNotNone(self.adapter().check(req("box.tail1234.ts.example")), "port 8443 is not the default port")

    def test_while_the_owner_is_unknown_every_request_gets_503_and_nothing_is_guessed(self) -> None:
        for kwargs in (
            {"bin_path": str(tailscale_script(self.s.root, fail=True))},
            {"bin_path": str(tailscale_script(self.s.root / ".", {"Self": {}}))},
            {"bin_path": str(self.s.root / "no-such-tailscale")},
        ):
            a = self.adapter(**kwargs)
            denied = a.check(self.good())
            self.assertEqual(denied.status, 503)
            self.assertIsNone(a.public_url())

    def test_the_answer_is_cached_and_retried_sooner_while_unknown(self) -> None:
        a = self.adapter()
        for _ in range(5):
            a.check(self.good())
        self.assertEqual(self.asked.read_text(), "x")
        self.now[0] += 61
        a.check(self.good())
        self.assertEqual(self.asked.read_text(), "xx")
        broken = self.adapter(bin_path=str(tailscale_script(self.s.root, fail=True)))
        broken.check(self.good())
        broken.check(self.good())
        self.assertEqual((self.s.root / "asked").read_text(), "x", "within the retry interval it is not asked again")
        self.now[0] += 11
        broken.check(self.good())
        self.assertEqual((self.s.root / "asked").read_text(), "xx")

    def test_configured_owner_and_host_mean_tailscale_is_not_asked_at_all(self) -> None:
        a = self.adapter(owner="me@example.test", public_host="Box.Example.Test")
        self.assertIsNone(a.check(req("box.example.test:8443")))
        self.assertIsNone(a.check(req("box:8443")))
        self.assertEqual(a.check(req("box.example.test:8443", login="x@y.z")).status, 403)
        self.assertFalse(self.asked.exists())

    def test_a_configured_owner_alone_still_needs_the_machine_name_from_tailscale(self) -> None:
        a = self.adapter(owner="someone@example.test")
        self.assertIsNone(a.check(req("box.tail1234.ts.example:8443", login="someone@example.test")))
        self.assertEqual(a.check(self.good()).status, 403, "the configured owner replaces the one Tailscale reports")

    def test_a_tagged_machine_without_an_owner_is_unknown(self) -> None:
        status = {"Self": {"DNSName": "box.tail1234.ts.example.", "UserID": 99}, "User": {}}
        self.assertEqual(
            self.adapter(bin_path=str(tailscale_script(self.s.root, status))).check(self.good()).status, 503
        )


class ThroughTheHub(ScratchTestCase):
    def setUp(self) -> None:
        self.s = self.make([host_entry(1)])
        ts = tailscale_script(self.s.bin)
        self.hub_ = self.hub(
            self.s, {"auth": {"adapter": "tailscale-serve", "tailscale": {"bin": str(ts), "servePort": 8443}}}
        )
        self.name = "box.tail1234.ts.example"
        self.c = self.hub_.client
        self.c.host = f"{self.name}:8443"
        self.c.origin = f"https://{self.name}:8443"
        self.login = {"Tailscale-User-Login": "me@example.test"}

    def test_with_the_right_headers_pairing_works_and_the_cookie_is_secure(self) -> None:
        self.c.get("/api/hub/machine", headers=self.login)
        token = self.hub_.app.pairing.writer.create_token()
        resp = self.c.post("/api/hub/pair", {"token": token}, headers=self.login)
        self.assertEqual(resp.status, 200)
        self.assertIn("; Secure", resp.headers["Set-Cookie"])
        self.c.cookie = resp.headers["Set-Cookie"].split(";", 1)[0]
        self.assertEqual(self.c.get("/api/hub/machine", headers=self.login).status, 200)

    def test_forged_headers_cannot_replace_the_cookie_and_the_cookie_cannot_replace_the_headers(self) -> None:
        self.assertEqual(self.c.get("/api/hub/machine", headers=self.login).status, 401, "right headers, no cookie")
        token = self.hub_.app.pairing.writer.create_token()
        self.c.post("/api/hub/pair", {"token": token}, headers=self.login)
        resp = self.c.post("/api/hub/pair", {"token": self.hub_.app.pairing.writer.create_token()}, headers=self.login)
        self.c.cookie = resp.headers["Set-Cookie"].split(";", 1)[0]
        self.assertEqual(self.c.get("/api/hub/machine").status, 403, "a cookie without the owner's login")
        self.assertEqual(self.c.get("/api/hub/machine", headers={"Tailscale-User-Login": "x@y.z"}).status, 403)
        self.assertEqual(
            self.c.get("/api/hub/machine", headers={**self.login, "Tailscale-Funnel-Request": "?1"}).status, 403
        )
        self.assertEqual(self.c.get("/", headers={"Tailscale-User-Login": "x@y.z"}).status, 403, "the page too")
        wrong_origin = self.c.post("/api/hub/alerts/test", {}, headers={**self.login, "Origin": "https://evil.example"})
        self.assertEqual(wrong_origin.status, 403)

    def test_healthz_does_not_depend_on_tailscale_headers(self) -> None:
        resp = Client(self.s.port).get("/healthz")
        self.assertEqual(resp.status, 200)


class AppCase(unittest.TestCase):
    def test_the_hub_app_picks_the_adapter_the_configuration_names(self) -> None:
        from support import Scratch

        s = Scratch()
        self.addCleanup(s.close)
        self.assertIsInstance(HubApp(s.config(), s.env).adapter, LoopbackOnly)
        s.write_config(
            {"auth": {"adapter": "tailscale-serve", "tailscale": {"owner": "a@b.c", "publicHost": "h.example"}}}
        )
        self.assertIsInstance(HubApp(s.config(), s.env).adapter, TailscaleServe)
        self.assertTrue(stat.S_ISREG(os.stat(s.paths().config_file).st_mode))
        self.assertTrue(HubServer)
