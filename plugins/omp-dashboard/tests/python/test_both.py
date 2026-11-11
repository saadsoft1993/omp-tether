"""The `both` sign-in adapter: one hub that answers on its own loopback address (the pairing cookie alone) and, at the same
time, through Tailscale Serve (owner login, name and cookie). Every request is judged by exactly one of the two rule sets,
chosen by whether it carries any sign of a proxy, and never by both or by a mixture."""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import unittest
from email.message import Message
from typing import ClassVar
from unittest import mock

from omp_dashboard import doctor
from omp_dashboard.auth import Both, LoopbackOnly, TailscaleServe, build_adapter, proxy_sign
from omp_dashboard.dropins import health_dropin
from omp_dashboard.omp import RelayClassifier, link_relay
from support import Client, Scratch, ScratchTestCase, host_entry, run_hub_process, stop_process
from test_auth import STATUS, tailscale_script
from test_cli import FAKE_TAILSCALE, SERVICES, cli, validate

NAME = "box.tail1234.ts.example"
OWNER = "me@example.test"
SERVE = 8443


def both(port: int = 4000, hosts: list[str] | None = None, **ts) -> Both:
    ts.setdefault("owner", OWNER)
    ts.setdefault("public_host", NAME)
    return Both(LoopbackOnly("127.0.0.1", port, hosts or []), TailscaleServe(None, SERVE, **ts))


def headers(host: str | None, *pairs: tuple[str, str]) -> Message:
    """A real header block (names and repeats exactly as sent), like the one the hub's handler holds."""
    msg = http.client.HTTPMessage()
    if host is not None:
        msg["Host"] = host
    for name, value in pairs:
        msg[name] = value  # Message.__setitem__ appends: a name may repeat
    return msg


TAILNET_HOST = f"{NAME}:{SERVE}"
LOGIN = ("Tailscale-User-Login", OWNER)
# Every header the hub reads as "a proxy is in front": none of them may sit on a request that is judged as local.
PROXY_SIGNS = [
    ("Tailscale-User-Login", OWNER),
    ("tailscale-user-login", OWNER),
    ("TAILSCALE-USER-LOGIN", OWNER),
    ("Tailscale-User-Name", "Me"),
    ("Tailscale-User-Profile-Pic", "https://x.example/p.png"),
    ("Tailscale-Headers-Info", "https://tailscale.com/s/serve-headers"),
    ("Tailscale-Funnel-Request", "?1"),
    ("Tailscale-Something-New", "x"),
    ("X-Forwarded-For", "203.0.113.9"),
    ("x-forwarded-for", "203.0.113.9"),
    ("X-Forwarded-Host", NAME),
    ("X-Forwarded-Proto", "https"),
    ("X-Forwarded-Port", "443"),
    ("X-Forwarded-Anything", "x"),
    ("Forwarded", "for=203.0.113.9"),
    ("FORWARDED", "for=203.0.113.9"),
    ("Via", "1.1 proxy"),
    ("via", "1.1 proxy"),
    ("X-Real-IP", "203.0.113.9"),
    ("x-real-ip", "203.0.113.9"),
]


class Classification(unittest.TestCase):
    """Which rules judge a request, and what they say, with no hub in between."""

    def setUp(self) -> None:
        self.a = both(hosts=["fwd.example.test:4999"])

    def test_the_registered_name_and_the_default_for_a_build_from_config(self) -> None:
        self.assertEqual(self.a.name, "both")
        self.assertIsInstance(self.a, Both)

    def test_a_request_with_no_sign_of_a_proxy_and_a_loopback_host_is_local(self) -> None:
        for host in ("127.0.0.1:4000", "localhost:4000", "[::1]:4000", "LOCALHOST:4000", "fwd.example.test:4999"):
            req = headers(host)
            self.assertIsNone(self.a.check(req), host)
            self.assertEqual(self.a.access(req), "local", host)

    def test_a_request_with_no_sign_of_a_proxy_is_never_judged_by_the_tailnet_rules(self) -> None:
        for host in (
            TAILNET_HOST,
            NAME,
            f"box:{SERVE}",
            "127.0.0.1",
            "127.0.0.1:4001",
            "[::1]",
            "[::1]:4001",
            "localhost",
            "evil.example:4000",
            "127.0.0.1.evil.example:4000",
            "0.0.0.0:4000",
            "localhost.:4000",
            "",
            None,
        ):
            denied = self.a.check(headers(host))
            self.assertIsNotNone(denied, host)
            self.assertEqual(denied.status, 403, host)

    def test_every_sign_of_a_proxy_makes_the_tailnet_rules_the_only_ones(self) -> None:
        for name, value in PROXY_SIGNS:
            is_login = name.lower() == "tailscale-user-login"
            on_loopback = headers("127.0.0.1:4000", (name, value))
            # a loopback Host never satisfies the tailnet rules, whatever else is claimed
            self.assertEqual(self.a.access(on_loopback), "tailnet", name)
            self.assertEqual(self.a.check(on_loopback).status, 403, name)
            self.assertEqual(self.a.check(headers("127.0.0.1:4000", LOGIN, (name, value))).status, 403, name)
            # a tailnet Host without the owner's login is a tagged device or a forgery
            if not is_login:
                self.assertEqual(self.a.check(headers(TAILNET_HOST, (name, value))).status, 403, name)
            # with the owner's login and this machine's name it is the tailnet path (Funnel excepted)
            ok = headers(TAILNET_HOST, (name, value)) if is_login else headers(TAILNET_HOST, LOGIN, (name, value))
            self.assertEqual(self.a.access(ok), "tailnet", name)
            denied = self.a.check(ok)
            if name.lower() == "tailscale-funnel-request":
                self.assertEqual(denied.status, 403, "Funnel is refused")
            else:
                self.assertIsNone(denied, name)

    def test_the_tailnet_rules_are_exactly_the_tailscale_serve_rules(self) -> None:
        serve = TailscaleServe(None, SERVE, OWNER, NAME)
        cases = [
            headers(TAILNET_HOST, LOGIN),
            headers(f"box:{SERVE}", LOGIN),
            headers(TAILNET_HOST, ("Tailscale-User-Login", "intruder@example.test")),
            headers(TAILNET_HOST, ("Tailscale-User-Login", "ME@example.test")),
            headers(TAILNET_HOST, ("Tailscale-User-Login", "")),
            headers(TAILNET_HOST, ("Tailscale-User-Login", " " + OWNER)),
            headers(TAILNET_HOST, LOGIN, ("Tailscale-Funnel-Request", "?1")),
            headers("evil.example:8443", LOGIN),
            headers(f"{NAME}:9999", LOGIN),
            headers(f"{NAME}:8443.evil", LOGIN),
            headers("127.0.0.1:4000", LOGIN),
            headers("", LOGIN),
            headers(TAILNET_HOST, LOGIN, LOGIN),
            headers(TAILNET_HOST, LOGIN, ("Tailscale-User-Login", "x@y.z")),
        ]
        for req in cases:
            self.assertEqual(self.a.check(req), serve.check(req), dict(req.items()))

    def test_a_header_that_must_appear_once_but_appears_twice_is_refused_on_both_paths(self) -> None:
        for req in (
            headers("127.0.0.1:4000", ("Host", "127.0.0.1:4000")),
            headers("127.0.0.1:4000", ("Host", "evil.example")),
            headers(TAILNET_HOST, LOGIN, ("Host", TAILNET_HOST)),
            headers(TAILNET_HOST, LOGIN, LOGIN),
            headers(TAILNET_HOST, LOGIN, ("Tailscale-User-Login", "x@y.z")),
            headers(TAILNET_HOST, LOGIN, ("Tailscale-Funnel-Request", "?1"), ("Tailscale-Funnel-Request", "?1")),
            headers("127.0.0.1:4000", LOGIN, LOGIN),
        ):
            denied = self.a.check(req)
            self.assertEqual(denied.status, 403, req.items())
            self.assertIn("more than once", denied.message)

    def test_an_unknown_owner_does_not_touch_the_local_path_and_stops_the_tailnet_one(self) -> None:
        s = Scratch()
        self.addCleanup(s.close)
        for ts in (str(tailscale_script(s.bin, fail=True)), str(s.root / "no-such-tailscale")):
            a = Both(LoopbackOnly("127.0.0.1", 4000, []), TailscaleServe(ts, SERVE, None, None, env=s.env))
            self.assertIsNone(a.check(headers("127.0.0.1:4000")))
            self.assertIsNone(a.check(headers("localhost:4000")))
            self.assertEqual(a.check(headers(TAILNET_HOST, LOGIN)).status, 503)
            self.assertEqual(a.check(headers("127.0.0.1:4000", LOGIN)).status, 503)
            self.assertEqual(a.check(headers("127.0.0.1:4000", ("X-Forwarded-For", "1.2.3.4"))).status, 503)
            self.assertIsNone(a.tailnet_url())
            self.assertEqual(a.public_url(), "http://127.0.0.1:4000/")

    def test_origins_and_the_secure_flag_follow_the_path_of_the_request(self) -> None:
        local, tailnet = headers("127.0.0.1:4000"), headers(TAILNET_HOST, LOGIN)
        self.assertIn("http://127.0.0.1:4000", self.a.origins(local))
        self.assertIn("http://fwd.example.test:4999", self.a.origins(local))
        self.assertFalse(any(o.startswith("https://") for o in self.a.origins(local)))
        self.assertEqual(self.a.origins(tailnet), {f"https://{NAME}:{SERVE}", f"https://box:{SERVE}"})
        self.assertFalse(self.a.secure_cookie(local))
        self.assertTrue(self.a.secure_cookie(tailnet))
        # a request that merely carries a proxy header is on the tailnet path for these too
        self.assertTrue(self.a.secure_cookie(headers("127.0.0.1:4000", ("Via", "x"))))
        self.assertFalse(self.a.origins(headers("127.0.0.1:4000", ("Via", "x"))) & self.a.origins(local))

    def test_urls_the_hub_prints_or_links_to(self) -> None:
        self.assertEqual(self.a.public_url(), "http://127.0.0.1:4000/")
        self.assertEqual(self.a.tailnet_url(), f"https://{NAME}:{SERVE}/")
        self.assertEqual(self.a.web_origin(8444), f"https://{NAME}:8444")
        self.assertEqual(both(443).tailnet_url(), f"https://{NAME}:{SERVE}/", "the Serve port, not the hub's")

    def test_the_other_adapters_have_one_path_each(self) -> None:
        loop = LoopbackOnly("127.0.0.1", 4000, [])
        serve = TailscaleServe(None, SERVE, OWNER, NAME)
        self.assertEqual(loop.access(headers("127.0.0.1:4000")), "local")
        self.assertEqual(serve.access(headers(TAILNET_HOST, LOGIN)), "tailnet")
        self.assertIsNone(loop.tailnet_url())
        self.assertEqual(serve.tailnet_url(), serve.public_url())

    def test_the_build_from_configuration(self) -> None:
        s = Scratch()
        self.addCleanup(s.close)
        s.write_config(
            {
                "auth": {
                    "adapter": "both",
                    "loopbackOnly": {"allowedHosts": ["fwd.example.test:4999"]},
                    "tailscale": {"servePort": SERVE, "owner": OWNER, "publicHost": NAME},
                },
            }
        )
        config = s.config()
        self.assertEqual(config.problems, [])
        a = build_adapter(config, s.env)
        self.assertIsInstance(a, Both)
        self.assertIsNone(a.check(headers(f"127.0.0.1:{s.port}")))
        self.assertIsNone(a.check(headers("fwd.example.test:4999")))
        self.assertIsNone(a.check(headers(f"{NAME}:{SERVE}", LOGIN)))
        self.assertEqual(a.check(headers("127.0.0.1:4000")).status, 403)

    def test_proxy_sign_sees_header_names_in_any_case_and_prefix(self) -> None:
        self.assertIsNone(proxy_sign(headers("127.0.0.1:1", ("Accept", "*/*"), ("User-Agent", "x"), ("Cookie", "a=b"))))
        for name, value in PROXY_SIGNS:
            self.assertEqual(proxy_sign(headers("127.0.0.1:1", (name, value))), name)
        # look-alikes are not signs
        for name in ("X-Forward", "Xtailscale-User", "Forwarded-By", "Tailscale", "Viaduct", "X-Real-IPv6"):
            self.assertIsNone(proxy_sign(headers("127.0.0.1:1", (name, "x"))), name)

        # an object without a header list falls back to the names it can be asked for
        class AskOnly:
            def __init__(self, **found: str):
                self.found = {k.lower(): v for k, v in found.items()}

            def get(self, name: str, default=None):
                return self.found.get(name.lower(), default)

        self.assertEqual(proxy_sign(AskOnly(Host="x", Via="1")), "Via")
        self.assertIsNone(proxy_sign(AskOnly(Host="x")))


def raw_request(port: int, lines: list[str], body: bytes = b"") -> tuple[int, bytes]:
    """Sends the header lines exactly as given (so a name can repeat) and returns (status, body)."""
    with socket.create_connection(("127.0.0.1", port), timeout=10) as conn:
        conn.sendall(("\r\n".join(lines) + "\r\n\r\n").encode() + body)
        data = b""
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    return int(head.split()[1]), rest


class HubFixture(ScratchTestCase):
    """A hub with `both`, the owner and the machine name fixed (Tailscale is not asked), on a scratch port."""

    def setUp(self) -> None:
        self.s = self.make([host_entry(1)])
        self.hub_ = self.hub(
            self.s,
            {"auth": {"adapter": "both", "tailscale": {"servePort": SERVE, "owner": OWNER, "publicHost": NAME}}},
        )
        self.local = self.hub_.client
        self.tailnet = Client(self.s.port, host=TAILNET_HOST, origin=f"https://{NAME}:{SERVE}")
        self.login = {"Tailscale-User-Login": OWNER}

    def token(self) -> str:
        return self.hub_.app.pairing.writer.create_token()

    def pair_tailnet(self, client: Client | None = None) -> Client:
        client = client or self.tailnet
        resp = client.post("/api/hub/pair", {"token": self.token()}, headers=self.login)
        self.assertEqual(resp.status, 200, resp.body)
        client.cookie = resp.headers["Set-Cookie"].split(";", 1)[0]
        return client


class ThroughTheHub(HubFixture):
    def test_the_page_and_the_pair_screen_load_on_both_paths_unpaired(self) -> None:
        self.assertEqual(self.local.get("/").status, 200)
        self.assertEqual(self.tailnet.get("/", headers=self.login).status, 200)
        self.assertEqual(self.local.get("/api/hub/machine").status, 401)
        self.assertEqual(self.tailnet.get("/api/hub/machine", headers=self.login).status, 401)

    def test_pairing_locally_works_and_the_cookie_is_not_secure(self) -> None:
        resp = self.local.post("/api/hub/pair", {"token": self.token()})
        self.assertEqual(resp.status, 200, resp.body)
        cookie = resp.headers["Set-Cookie"]
        self.assertNotIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.local.cookie = cookie.split(";", 1)[0]
        self.assertEqual(self.local.get("/api/hub/machine").status, 200)

    def test_pairing_through_serve_works_and_the_cookie_is_secure(self) -> None:
        resp = self.tailnet.post("/api/hub/pair", {"token": self.token()}, headers=self.login)
        self.assertEqual(resp.status, 200, resp.body)
        self.assertIn("; Secure", resp.headers["Set-Cookie"])
        self.tailnet.cookie = resp.headers["Set-Cookie"].split(";", 1)[0]
        self.assertEqual(self.tailnet.get("/api/hub/machine", headers=self.login).status, 200)

    def test_a_proxy_header_on_the_local_address_gets_no_cookie(self) -> None:
        resp = self.local.post("/api/hub/pair", {"token": self.token()}, headers={"Via": "1.1 x"})
        self.assertEqual(resp.status, 403)
        self.assertNotIn("Set-Cookie", resp.headers)

    def test_a_token_is_accepted_once_whichever_origin_redeems_it_first(self) -> None:
        first = self.token()
        self.assertEqual(self.local.post("/api/hub/pair", {"token": first}).status, 200)
        again = self.tailnet.post("/api/hub/pair", {"token": first}, headers=self.login)
        self.assertEqual(again.status, 403)
        self.assertNotIn("Set-Cookie", again.headers)
        second = self.token()
        self.assertEqual(self.tailnet.post("/api/hub/pair", {"token": second}, headers=self.login).status, 200)
        self.assertEqual(self.local.post("/api/hub/pair", {"token": second}).status, 403)

    def test_one_token_pairs_one_browser_on_one_address_and_each_address_pairs_separately(self) -> None:
        local = Client(self.s.port)
        self.hub_.pair(local)
        self.pair_tailnet()
        self.assertEqual(len(self.hub_.app.pairing.writer.devices()), 2)
        # a device is let in only the way it was paired: the local cookie is no way in through Serve, with or without the owner's login
        stray = Client(self.s.port, host=TAILNET_HOST, origin=self.tailnet.origin)
        stray.cookie = local.cookie
        self.assertEqual(stray.get("/api/hub/machine").status, 403, "no login: the tailnet rules refuse")
        self.assertEqual(stray.get("/api/hub/machine", headers=self.login).status, 401, "owner login, wrong way in")

    def test_a_forged_tailscale_header_on_a_direct_looking_request_is_handled_as_proxied(self) -> None:
        self.hub_.pair()
        for extra in (self.login, {"Tailscale-User-Login": "intruder@example.test"}, {"Tailscale-User-Name": "x"}):
            self.assertEqual(self.local.get("/api/hub/machine", headers=extra).status, 403, extra)
        self.assertEqual(self.local.get("/api/hub/machine").status, 200, "without it the paired browser is let in")

    def test_every_proxy_sign_on_the_local_address_is_refused_even_for_a_paired_browser(self) -> None:
        self.hub_.pair()
        for name, value in PROXY_SIGNS:
            self.assertEqual(self.local.get("/api/hub/machine", headers={name: value}).status, 403, name)
            self.assertEqual(self.local.get("/", headers={name: value}).status, 403, name)

    def test_the_tailnet_path_still_needs_the_owner_the_name_and_the_port(self) -> None:
        self.pair_tailnet()
        c = self.tailnet
        self.assertEqual(c.get("/api/hub/machine").status, 403, "cookie alone is not enough on the tailnet path")
        self.assertEqual(c.get("/api/hub/machine", headers={"Tailscale-User-Login": "x@y.z"}).status, 403)
        self.assertEqual(
            c.get("/api/hub/machine", headers={**self.login, "Tailscale-Funnel-Request": "?1"}).status, 403
        )
        for host in ("evil.example", f"{NAME}:9999", "127.0.0.1", f"127.0.0.1:{self.s.port}"):
            c.host = host
            self.assertEqual(c.get("/api/hub/machine", headers=self.login).status, 403, host)

    def test_dns_rebinding_to_the_local_address_is_stopped_by_the_host_check(self) -> None:
        self.hub_.pair()
        for host in (f"evil.example:{self.s.port}", "evil.example", f"127.0.0.1.evil.example:{self.s.port}", NAME):
            self.local.host = host
            self.assertEqual(self.local.get("/api/hub/machine").status, 403, host)
            self.assertEqual(self.local.get("/").status, 403, host)

    def test_a_post_needs_the_custom_header_and_the_origin_of_its_own_path(self) -> None:
        self.hub_.pair()
        c = self.local
        self.assertEqual(
            c.post("/api/hub/alerts/test", {}, post_headers=False).status, 403, "no custom header, no Origin"
        )
        self.assertEqual(
            c.post("/api/hub/link", {"instanceId": "inst0001"}, headers={"Origin": "https://evil.example"}).status, 403
        )
        # the tailnet origin does not do on the local address, and the local one does not do through Serve
        self.assertEqual(c.post("/api/hub/link", {}, headers={"Origin": self.tailnet.origin}).status, 403)
        self.assertEqual(c.post("/api/hub/link", {}, headers={"Origin": "null"}).status, 403)
        self.assertNotEqual(c.post("/api/hub/link", {}).status, 403)
        self.pair_tailnet()
        t = self.tailnet
        self.assertEqual(t.post("/api/hub/link", {}, headers={**self.login, "Origin": self.local.origin}).status, 403)
        self.assertEqual(
            t.post("/api/hub/link", {}, headers={**self.login, "Origin": "https://evil.example"}).status, 403
        )
        self.assertNotEqual(t.post("/api/hub/link", {}, headers=self.login).status, 403)

    def test_the_cookie_is_checked_after_the_sign_in_on_both_paths(self) -> None:
        self.assertEqual(self.local.post("/api/hub/link", {}).status, 401)
        self.assertEqual(self.tailnet.post("/api/hub/link", {}, headers=self.login).status, 401)

    def test_healthz_is_for_direct_local_clients_only(self) -> None:
        self.assertEqual(self.local.get("/healthz").status, 200)
        for name, value in PROXY_SIGNS:
            self.assertEqual(self.local.get("/healthz", headers={name: value}).status, 403, name)
        self.assertEqual(self.tailnet.get("/healthz", headers=self.login).status, 403)
        self.assertEqual(Client(self.s.port, host="evil.example").get("/healthz").status, 403)

    def test_duplicate_headers_are_refused_over_the_wire(self) -> None:
        port = self.s.port
        status, _ = raw_request(
            port, ["GET / HTTP/1.1", f"Host: 127.0.0.1:{port}", "Host: evil.example", "Connection: close"]
        )
        self.assertEqual(status, 403)
        status, _ = raw_request(
            port,
            [
                "GET / HTTP/1.1",
                f"Host: {TAILNET_HOST}",
                f"Tailscale-User-Login: {OWNER}",
                "Tailscale-User-Login: x@y.z",
                "Connection: close",
            ],
        )
        self.assertEqual(status, 403)
        status, _ = raw_request(
            port, ["GET / HTTP/1.1", f"Host: {TAILNET_HOST}", f"tailscale-user-login: {OWNER}", "Connection: close"]
        )
        self.assertEqual(status, 200, "a lower-case name is the same header")

    def test_about_says_which_adapter(self) -> None:
        self.hub_.pair()
        self.assertEqual(self.local.get("/api/hub/about").json()["adapter"], "both")

    def test_the_hub_banner_names_the_adapter(self) -> None:
        self.assertEqual(self.hub_.app.adapter.name, "both")


class WithTailscaleOff(ScratchTestCase):
    """Tailscale is not running: the local path is untouched, the tailnet one answers 503."""

    def setUp(self) -> None:
        self.s = self.make([host_entry(1)])
        self.ts = tailscale_script(self.s.bin, fail=True)
        self.hub_ = self.hub(
            self.s, {"auth": {"adapter": "both", "tailscale": {"bin": str(self.ts), "servePort": SERVE}}}
        )

    def test_local_pairing_and_use_work_and_the_tailnet_path_is_unavailable(self) -> None:
        c = self.hub_.pair()
        self.assertEqual(c.get("/api/hub/machine").status, 200)
        self.assertEqual(c.get("/api/hub/sessions").status, 200)
        self.assertEqual(c.get("/healthz").status, 200)
        tailnet = Client(self.s.port, host=TAILNET_HOST, origin=f"https://{NAME}:{SERVE}")
        self.assertEqual(tailnet.get("/", headers={"Tailscale-User-Login": OWNER}).status, 503)
        self.assertEqual(c.get("/api/hub/machine", headers={"Tailscale-User-Login": OWNER}).status, 503)
        self.assertIsNone(self.hub_.app.adapter.tailnet_url())
        self.assertIsNone(self.hub_.app.sessions_url())

    def test_the_hub_starts_when_the_tailscale_command_cannot_be_run_at_all(self) -> None:
        self.assertEqual(self.hub_.client.get("/").status, 200)


class RelayLinks(ScratchTestCase):
    """Open and Watch with `both`. The bundled relay admits one way (token auth: direct browsers; tailscale-serve auth: browsers through
    Serve), named by relay.admits: a link is offered to the browsers of that way, with the host name they used, and the others are told why not."""

    GUEST, HOST = 17996, 17997
    REMOTE = f"https://{NAME}:{SERVE}"
    LOGIN: ClassVar[dict[str, str]] = {"Tailscale-User-Login": OWNER}

    def setUp(self) -> None:
        self.s = self.make([host_entry(1)])
        self.fragment = f"ws://127.0.0.1:{self.HOST}/r/room.key"

    def start(
        self,
        admits: str = "local",
        omp_web: str | None = None,
        relay: dict | None = None,
        tailscale: dict | None = None,
        hosts=None,
    ):
        tailscale = tailscale or {"owner": OWNER, "publicHost": NAME}
        auth: dict = {"adapter": "both", "tailscale": {"servePort": SERVE, **tailscale}}
        if hosts:
            auth["loopbackOnly"] = {"allowedHosts": hosts}
        self.hub_ = self.hub(
            self.s,
            {
                "auth": auth,
                "relay": {
                    "enabled": True,
                    "hostPort": self.HOST,
                    "guestPort": self.GUEST,
                    "servePort": SERVE,
                    "admits": admits,
                    **(relay or {}),
                },
            },
        )
        base = omp_web or (f"http://127.0.0.1:{self.GUEST}" if admits == "local" else self.REMOTE)
        self.s.fake_state["links"] = {
            "inst0001": {"control": f"{base}/#{self.fragment}", "view": f"{base}/#{self.fragment}"}
        }
        self.s.save_fake()
        self.local = self.hub_.pair()
        port = self.s.port
        self.localhost = self.hub_.pair(Client(port, host=f"localhost:{port}", origin=f"http://localhost:{port}"))
        self.tailnet = Client(port, host=TAILNET_HOST, origin=self.REMOTE)
        resp = self.tailnet.post(
            "/api/hub/pair", {"token": self.hub_.app.pairing.writer.create_token()}, headers=self.LOGIN
        )
        if resp.status == 200:  # not while Tailscale is off
            self.tailnet.cookie = resp.headers["Set-Cookie"].split(";", 1)[0]
        return self.hub_

    def open(self, client: Client, **headers: str) -> tuple[int, dict]:
        import omp_dashboard.server as server

        with mock.patch.object(server.relay, "check_ports", return_value=None):
            r = client.post("/api/hub/link", {"instanceId": "inst0001", "view": False}, headers=headers or None)
        return r.status, r.json()

    def test_a_relay_for_the_local_way_gives_local_browsers_a_link_at_the_name_they_used(self) -> None:
        self.start("local")
        status, body = self.open(self.local)
        self.assertEqual((status, body["url"]), (200, f"http://127.0.0.1:{self.GUEST}/#{self.fragment}"))
        status, body = self.open(
            self.localhost
        )  # omp's link names 127.0.0.1: the browser's own name is kept, so its cookie is sent
        self.assertEqual((status, body["url"]), (200, f"http://localhost:{self.GUEST}/#{self.fragment}"))

    def test_a_relay_for_the_local_way_gives_a_tailnet_browser_a_message_and_no_link(self) -> None:
        self.start("local")
        status, body = self.open(self.tailnet, **self.LOGIN)
        self.assertEqual((status, body["code"]), (409, "relay_other_path"))
        self.assertNotIn("url", body)
        self.assertIn("own address", body["error"])

    def test_a_relay_for_the_tailnet_way_gives_serve_browsers_a_link_and_local_browsers_a_message(self) -> None:
        self.start("tailnet")
        status, body = self.open(self.tailnet, **self.LOGIN)
        self.assertEqual((status, body["url"]), (200, f"{self.REMOTE}/#{self.fragment}"))
        for client in (self.local, self.localhost):
            status, body = self.open(client)
            self.assertEqual((status, body["code"]), (409, "relay_other_path"))
            self.assertNotIn("url", body)
        self.assertIn("Tailscale Serve", body["error"])

    def test_a_configured_web_url_names_the_serve_address_of_a_tailnet_relay(self) -> None:
        self.start("tailnet", relay={"webUrl": "https://relay.example.test"}, omp_web="https://relay.example.test")
        self.assertEqual(
            self.open(self.tailnet, **self.LOGIN)[1]["url"], f"https://relay.example.test/#{self.fragment}"
        )

    def test_a_local_address_that_is_not_the_machines_own_name_is_refused_not_rewritten(self) -> None:
        # a port forward (an allowedHosts entry) or [::1]: the relay's address would be the browser machine's, not this one's
        self.start("local", hosts=["fwd.example.test:4999"])
        port = self.s.port
        for host in (f"[::1]:{port}", "fwd.example.test:4999"):
            client = self.hub_.pair(Client(port, host=host, origin=f"http://{host}"))
            status, body = self.open(client)
            self.assertEqual((status, body["code"]), (409, "relay_address"), host)
            self.assertNotIn("url", body)
            self.assertIn("cannot be told apart", body["error"])

    def test_a_forward_that_keeps_the_hubs_port_number_is_indistinguishable_from_a_direct_request(self) -> None:
        # `ssh -L <port>:127.0.0.1:<port>` sends Host 127.0.0.1:<port> and no proxy header: the hub cannot know the browser is on another
        # machine, offers the link, and the documents say so instead of claiming a refusal.
        self.start("local")
        forwarded = Client(self.s.port)  # what such a browser sends: this hub's own Host and port
        self.hub_.pair(forwarded)
        status, body = self.open(forwarded)
        self.assertEqual((status, body["url"]), (200, f"http://127.0.0.1:{self.GUEST}/#{self.fragment}"))
        from support import PLUGIN

        for name in ("README.md", "docs/threat-model.md"):
            self.assertIn("cannot be told apart", (PLUGIN / name).read_text().replace("\n", " "), name)

    def test_a_link_for_another_address_or_another_relay_is_still_refused(self) -> None:
        self.start("local", omp_web="https://other.example")
        self.assertEqual(self.open(self.local)[0], 409)
        self.s.fake_state["links"]["inst0001"]["control"] = (
            f"http://127.0.0.1:{self.GUEST}/#ws://127.0.0.1:1/r/room.key"
        )
        self.s.save_fake()
        status, body = self.open(self.local)
        self.assertEqual((status, body.get("relay")), (409, "public"))

    def test_with_tailscale_off_a_local_relay_still_gives_local_links(self) -> None:
        down = tailscale_script(self.s.bin, fail=True)
        self.start("local", tailscale={"bin": str(down)})
        status, body = self.open(self.local)
        self.assertEqual((status, body["url"]), (200, f"http://127.0.0.1:{self.GUEST}/#{self.fragment}"))

    def test_with_tailscale_off_a_tailnet_relay_has_no_address_and_says_so(self) -> None:
        down = tailscale_script(self.s.bin, fail=True)
        self.start("tailnet", tailscale={"bin": str(down)})
        status, body = self.open(self.local)
        self.assertEqual((status, body["code"]), (409, "relay_other_path"))
        rows = self.local.get("/api/hub/sessions").json()["sessions"]
        self.assertEqual(rows[0]["relay"], "unknown", "its own web address is not known")

    def test_the_session_list_marks_the_relay_for_any_loopback_name_omp_uses(self) -> None:
        self.start("local", omp_web=f"http://localhost:{self.GUEST}")
        rows = self.local.get("/api/hub/sessions").json()["sessions"]
        self.assertEqual(rows[0]["relay"], "private")

    def test_the_default_is_a_relay_for_the_local_way_and_the_setting_is_validated(self) -> None:
        self.assertEqual(self.make().config().get("relay.admits"), "local")
        self.assertTrue(self.make(OMP_DASHBOARD_RELAY_ADMITS="everyone").config().problems)
        self.assertEqual(self.make(OMP_DASHBOARD_RELAY_ADMITS="tailnet").config().get("relay.admits"), "tailnet")

    def test_link_relay_and_the_classifier_take_several_addresses(self) -> None:
        relay = "ws://127.0.0.1:2"
        url = f"https://b.example/#{relay}/r/x.y"
        self.assertEqual(link_relay(url, ("http://127.0.0.1:1", "https://b.example"), relay), "private")
        self.assertEqual(link_relay(url, ("http://127.0.0.1:1",), relay), "public")
        self.assertEqual(link_relay(url, "https://b.example", relay), "private", "a single address still works")
        self.assertEqual(link_relay(url, None, relay), "public")
        self.assertEqual(link_relay(url, (), relay), "public")

        class Omp:
            def link(self, *_a, **_k):
                return {"url": url}

        classifier = RelayClassifier(Omp())  # type: ignore[arg-type]
        hosts = [{"instanceId": "inst0001", "generation": 1}]
        self.assertEqual(
            classifier.annotate([dict(h) for h in hosts], ("https://b.example",), relay)[0]["relay"], "private"
        )


class Pairing(ScratchTestCase):
    """`omp-dashboard pair` for each path."""

    def hub_process(self, state: str = "Running", pinned: bool = True, missing: bool = False):
        """A hub with a stand-in `tailscale` that reports `state` as its BackendState ("down": it cannot be run at all); `pinned` fixes the
        owner and the machine name in the config, so that Tailscale is never asked for them."""
        s = self.make()
        if missing:
            ts = s.root / "no-such-tailscale"
        elif state == "down":
            ts = tailscale_script(s.bin, fail=True)
        else:
            ts = tailscale_script(s.bin, {**STATUS, "BackendState": state})
        s.write_config(
            {
                "auth": {
                    "adapter": "both",
                    "tailscale": {
                        "bin": str(ts),
                        "servePort": SERVE,
                        **({"owner": OWNER, "publicHost": NAME} if pinned else {}),
                    },
                }
            }
        )
        proc = run_hub_process(s)
        self.addCleanup(stop_process, proc)
        return s

    def test_the_default_link_is_the_local_one_and_pairs_a_local_browser(self) -> None:
        s = self.hub_process()
        r = cli(s, "pair")
        self.assertEqual(r.returncode, 0, r.stderr)
        link = r.stdout.strip()
        self.assertTrue(link.startswith(f"http://127.0.0.1:{s.port}/#pair="), link)
        self.assertEqual(Client(s.port).pair_with(link.split("#pair=")[1]).status, 200)

    def test_tailnet_prints_the_serve_link_and_its_token_pairs_through_serve(self) -> None:
        s = self.hub_process()
        r = cli(s, "pair", "--tailnet")
        self.assertEqual(r.returncode, 0, r.stderr)
        link = r.stdout.strip()
        self.assertTrue(link.startswith(f"https://{NAME}:{SERVE}/#pair="), link)
        c = Client(s.port, host=TAILNET_HOST, origin=f"https://{NAME}:{SERVE}")
        resp = c.post("/api/hub/pair", {"token": link.split("#pair=")[1]}, headers={"Tailscale-User-Login": OWNER})
        self.assertEqual(resp.status, 200)
        self.assertIn("Secure", resp.headers["Set-Cookie"])

    def test_with_tailscale_off_tailnet_says_so_plainly_and_still_prints_the_local_link(self) -> None:
        s = self.hub_process("down", pinned=False)
        r = cli(s, "pair", "--tailnet")
        self.assertEqual(r.returncode, 1, "the tailnet link was asked for and could not be made")
        self.assertIn("Tailscale did not answer", r.stderr)
        self.assertIn("local link", r.stderr)
        link = r.stdout.strip()
        self.assertTrue(link.startswith(f"http://127.0.0.1:{s.port}/#pair="), link)
        self.assertEqual(Client(s.port).pair_with(link.split("#pair=")[1]).status, 200)
        plain = cli(s, "pair")
        self.assertEqual(plain.returncode, 0, "the local link does not depend on Tailscale")
        self.assertTrue(plain.stdout.strip().startswith(f"http://127.0.0.1:{s.port}/#pair="))

    def test_a_pinned_owner_and_name_do_not_make_a_stopped_tailscale_look_running(self) -> None:
        # With the owner and the name in the config the hub never asks Tailscale for them, so the link's address is always known:
        # whether Tailscale is up has to be asked on its own.
        for state, words in (
            ("Stopped", "Tailscale is stopped"),
            ("NeedsLogin", "needs a login"),
            ("NeedsMachineAuth", "approved"),
            ("Starting", "state: Starting"),
        ):
            s = self.hub_process(state, pinned=True)
            r = cli(s, "pair", "--tailnet")
            self.assertEqual(r.returncode, 1, state)
            self.assertIn(words, r.stderr, state)
            self.assertTrue(r.stdout.strip().startswith(f"http://127.0.0.1:{s.port}/#pair="), state)
            self.assertEqual(json.loads(cli(s, "pair", "--json", "--tailnet").stdout)["access"], "local", state)

    def test_unpinned_identity_and_a_missing_command_are_told_apart_from_a_stopped_tailscale(self) -> None:
        stopped = self.hub_process("Stopped", pinned=False)
        self.assertIn("Tailscale is stopped", cli(stopped, "pair", "--tailnet").stderr)
        missing = self.hub_process(missing=True, pinned=True)
        r = cli(missing, "pair", "--tailnet")
        self.assertIn("command was not found", r.stderr)
        self.assertEqual(r.returncode, 1)
        self.assertTrue(r.stdout.strip().startswith(f"http://127.0.0.1:{missing.port}/#pair="))

    def test_a_running_tailscale_with_a_pinned_identity_gives_the_serve_link(self) -> None:
        s = self.hub_process("Running", pinned=True)
        self.assertTrue(cli(s, "pair", "--tailnet").stdout.startswith(f"https://{NAME}:{SERVE}/#pair="))

    def test_json_says_which_address_the_link_is_for(self) -> None:
        s = self.hub_process()
        local = json.loads(cli(s, "pair", "--json").stdout)
        tailnet = json.loads(cli(s, "pair", "--json", "--tailnet").stdout)
        self.assertEqual((local["access"], tailnet["access"]), ("local", "tailnet"))
        self.assertTrue(tailnet["url"].startswith(f"https://{NAME}"))

    def test_the_other_adapters_have_their_one_link(self) -> None:
        s = self.make()
        s.write_config({})
        proc = run_hub_process(s)
        self.addCleanup(stop_process, proc)
        r = cli(s, "pair", "--tailnet")
        self.assertEqual(r.returncode, 2)
        self.assertIn("no tailnet link", r.stderr)
        self.assertEqual(r.stdout, "", "no token is made and nothing is printed")
        self.assertEqual(cli(s, "pair").returncode, 0)


class Doctor(ScratchTestCase):
    def findings(self, s: Scratch) -> dict[str, doctor.Finding]:
        return {f.id: f for f in doctor.run_doctor(s.config(), s.env)}

    def stand_in(self, s: Scratch, serve: dict | None, status: dict | None = STATUS, down: bool = False):
        script = s.bin / "tailscale"
        if down:
            script.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(1)\n")
        else:
            script.write_text(
                FAKE_TAILSCALE.format(
                    python=sys.executable, serve=json.dumps(serve), status=json.dumps(status)
                ).replace("{serve!r}", repr(json.dumps(serve)))
            )
        os.chmod(script, 0o700)
        return script

    def configure(self, s: Scratch, script) -> None:
        s.write_config({"auth": {"adapter": "both", "tailscale": {"bin": str(script), "servePort": SERVE}}})

    def test_tailscale_off_is_a_warning_for_the_tailnet_path_and_the_local_path_is_fine(self) -> None:
        s = self.make()
        proc = None
        self.configure(s, self.stand_in(s, None, down=True))
        proc = run_hub_process(s)
        self.addCleanup(stop_process, proc)
        f = self.findings(s)
        self.assertFalse([x.message for x in f.values() if x.level == "fail"], "nothing fails")
        self.assertEqual(f["access-local"].level, "ok")
        self.assertIn(f"127.0.0.1:{s.port}", f["access-local"].message)
        self.assertEqual(f["tailscale"].level, "warn")
        self.assertIn("tailnet", f["tailscale"].message)
        self.assertIn("local", f["tailscale"].message)
        self.assertEqual(f["auth"].level, "ok")

    def test_a_missing_tailscale_command_is_a_warning_in_this_mode(self) -> None:
        s = self.make()
        s.write_config({"auth": {"adapter": "both", "tailscale": {"bin": str(s.root / "nowhere" / "tailscale")}}})
        f = self.findings(s)
        self.assertEqual(f["tailscale"].level, "warn")
        self.assertIn("auth.tailscale.bin", f["tailscale"].fix)
        self.assertFalse([x for x in f.values() if x.level == "fail"])

    def test_the_two_paths_are_reported_separately_with_a_stand_in_tailscale(self) -> None:
        s = self.make()
        target = f"http://127.0.0.1:{s.port}"
        route = {"Web": {f"{NAME}:{SERVE}": {"Handlers": {"/": {"Proxy": target}}}}}
        status = {"Self": {"DNSName": f"{NAME}.", "UserID": 1}, "User": {"1": {"LoginName": OWNER}}}
        expected = {
            "ok": (route, "ok"),
            "funnel": ({**route, "AllowFunnel": {f"{NAME}:{SERVE}": True}}, "fail"),
            "absent": ({"Web": {}}, "warn"),
            "elsewhere": ({"Web": {f"{NAME}:{SERVE}": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:1"}}}}}, "fail"),
        }
        for label, (serve, level) in expected.items():
            self.configure(s, self.stand_in(s, serve, status))
            f = self.findings(s)
            self.assertEqual(f["tailscale"].level, "ok", label)
            self.assertEqual(f["serve-route"].level, level, label)
            self.assertIn("access-local", f)
            self.assertEqual(f["access-local"].level, "warn", "no hub runs here, said as a warning about the hub")
            self.assertNotEqual(f["access-local"].level, "fail")

    def test_a_route_that_points_elsewhere_and_funnel_stay_failures_with_both_even_together(self) -> None:
        s = self.make()
        status = {"Self": {"DNSName": f"{NAME}.", "UserID": 1}, "User": {"1": {"LoginName": OWNER}}}
        elsewhere = {f"{NAME}:{SERVE}": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:1"}}}}
        self.configure(s, self.stand_in(s, {"Web": elsewhere, "AllowFunnel": {f"{NAME}:{SERVE}": True}}, status))
        f = self.findings(s)
        self.assertEqual((f["serve-route"].level, f["serve-funnel"].level), ("fail", "fail"))
        self.assertIn("Funnel is switched on", f["serve-funnel"].message)

    def test_funnel_fails_when_the_web_route_is_absent_and_serve_is_read_when_the_identity_is_unknown(self) -> None:
        s = self.make()
        status = {"Self": {"DNSName": f"{NAME}.", "UserID": 1}, "User": {"1": {"LoginName": OWNER}}}
        funnel = {f"{NAME}:{SERVE}": True}
        elsewhere = {f"{NAME}:{SERVE}": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:1"}}}}
        # the route is missing, Funnel is on for the port: a warning for the route, a failure for Funnel
        self.configure(s, self.stand_in(s, {"Web": {}, "AllowFunnel": funnel}, status))
        f = self.findings(s)
        self.assertEqual((f["serve-route"].level, f["serve-funnel"].level), ("warn", "fail"))
        # Tailscale cannot say who the owner is (no Self), yet `serve status` answers: what it shows is still judged
        for serve, expected in (
            ({"Web": elsewhere}, {"serve-route": "fail"}),
            ({"Web": {}, "AllowFunnel": funnel}, {"serve-route": "warn", "serve-funnel": "fail"}),
            ({"Web": elsewhere, "AllowFunnel": funnel}, {"serve-route": "fail", "serve-funnel": "fail"}),
        ):
            self.configure(s, self.stand_in(s, serve, {"Self": {}}))
            f = self.findings(s)
            self.assertEqual(f["tailscale"].level, "warn", serve)
            self.assertEqual({k: f[k].level for k in expected}, expected, serve)

    def test_serve_check_fails_only_for_funnel_or_a_route_elsewhere(self) -> None:
        s = self.make()
        target = f"http://127.0.0.1:{s.port}"
        mine = {f"{NAME}:{SERVE}": {"Handlers": {"/": {"Proxy": target}}}}
        other = {f"{NAME}:{SERVE}": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:1"}}}}
        funnel = {f"{NAME}:{SERVE}": True}
        for serve, code in (
            ({"Web": mine}, 0),
            ({"Web": {}}, 0),  # absent: availability, not integrity
            ({"Web": other}, 1),
            ({"Web": mine, "AllowFunnel": funnel}, 1),
            ({"Web": other, "AllowFunnel": funnel}, 1),
            ({"Web": {}, "AllowFunnel": funnel}, 1),
        ):
            self.configure(s, self.stand_in(s, serve, STATUS))
            self.assertEqual(cli(s, "serve-check").returncode, code, serve)
        self.configure(s, self.stand_in(s, None, down=True))  # Tailscale off or not answering: nothing to fail on
        self.assertEqual(cli(s, "serve-check").returncode, 0)
        self.configure(s, s.root / "no-such-tailscale")
        self.assertEqual(cli(s, "serve-check").returncode, 0)

    def test_access_local_is_asked_of_the_sign_in_not_of_healthz(self) -> None:
        # a hub that is alive but still runs the tailscale-serve sign-in answers /healthz, and refuses a direct local request
        s = self.make()
        s.write_config(
            {
                "auth": {
                    "adapter": "tailscale-serve",
                    "tailscale": {"owner": OWNER, "publicHost": NAME, "servePort": SERVE},
                }
            }
        )
        proc = run_hub_process(s)
        self.addCleanup(stop_process, proc)
        s.write_config(
            {"auth": {"adapter": "both", "tailscale": {"owner": OWNER, "publicHost": NAME, "servePort": SERVE}}}
        )
        f = self.findings(s)
        self.assertEqual(f["hub"].level, "ok", "it is alive")
        self.assertEqual(f["access-local"].level, "fail")
        self.assertIn("answered 403", f["access-local"].message)

    def test_the_serve_route_check_does_not_need_a_running_hub_to_be_reported_and_loopback_only_is_unchanged(
        self,
    ) -> None:
        s = self.make()
        f = self.findings(s)
        self.assertNotIn("access-local", f)
        self.assertEqual(f["auth"].level, "ok")


class Dropins(ScratchTestCase):
    def test_the_serve_route_check_is_a_note_and_the_hub_checks_are_unchanged(self) -> None:
        s = self.make()
        s.write_config({"auth": {"adapter": "both", "tailscale": {"servePort": SERVE}}})
        doc = health_dropin(s.config(), s.env)
        if SERVICES.exists():  # a mutation copy of the plugin has no omp-services next to it
            self.assertEqual(validate(doc, "health-1"), [])
        checks = {c["id"]: c for c in doc["checks"]}
        self.assertEqual(
            set(checks), {"hub-health", "hub-port", "hub-owner", "hub-service", "serve-route", "serve-integrity"}
        )
        self.assertEqual(checks["serve-route"]["severity"], "note")
        # the note cannot tell a stopped Tailscale from a route that points elsewhere, so integrity is a check of its own that counts
        self.assertNotIn("severity", checks["serve-integrity"])
        self.assertEqual(checks["serve-integrity"]["type"], "command")
        argv = checks["serve-integrity"]["argv"]
        self.assertTrue(os.path.isabs(argv[0]))
        self.assertEqual(argv[2:], ["serve-check", "--config-file", str(s.paths().config_file)])
        self.assertEqual(
            (checks["serve-route"]["port"], checks["serve-route"]["target"]), (SERVE, f"http://127.0.0.1:{s.port}")
        )
        for hub_check in ("hub-health", "hub-port", "hub-owner", "hub-service"):
            self.assertNotIn("severity", checks[hub_check], hub_check)

    def test_tailscale_serve_alone_keeps_its_fatal_check(self) -> None:
        s = self.make()
        s.write_config({"auth": {"adapter": "tailscale-serve", "tailscale": {"servePort": SERVE}}})
        checks = {c["id"]: c for c in health_dropin(s.config(), s.env)["checks"]}
        self.assertNotIn("severity", checks["serve-route"])

    def test_omp_services_health_runner_never_counts_the_serve_route_against_the_hub(self) -> None:
        from support import MAIN
        from test_cli import SERVICES

        if not SERVICES.exists():
            self.skipTest("omp-services is not next to this copy of the plugin")

        s = self.make()
        env = {k: v for k, v in s.env.items() if k != "OMP_DASHBOARD_PORT"}
        s.write_config(
            {
                "listen": {"port": s.port},
                "omp": {"bin": str(s.omp_path)},
                "auth": {"adapter": "both", "tailscale": {"bin": str(s.root / "no-tailscale"), "servePort": 18499}},
            }
        )
        proc = run_hub_process(s)
        self.addCleanup(stop_process, proc)
        w = subprocess.run(
            [sys.executable, str(MAIN), "dropins", "--write"],
            env={**env, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(w.returncode, 0, w.stderr)
        r = subprocess.run(
            [
                str(SERVICES / "bin" / "omp-services"),
                "health",
                "--json",
                "--no-actions",
                "--file",
                str(s.paths().health_dropin),
            ],
            env={
                **env,
                "PYTHONDONTWRITEBYTECODE": "1",
                "OMP_SERVICES_PYTHON": sys.executable,
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            },
            capture_output=True,
            text=True,
            timeout=90,
        )
        results = {x["id"]: x for x in json.loads(r.stdout)}
        self.assertEqual(results["hub-health"]["status"], "ok", r.stdout)
        self.assertNotEqual(results["serve-route"]["status"], "ok", "Tailscale is not there to route")
        self.assertEqual(results["serve-route"]["severity"], "note")
        # no unit is installed in this test, so the watchdog fails; the serve route is reported and never counted
        counted = {i for i, x in results.items() if x["status"] != "ok" and x["severity"] == "fail"}
        self.assertEqual(counted, {"hub-service"}, r.stdout)


class DevicesAreBoundToTheirAccess(HubFixture):
    """A browser paired on the local address is a device of that address only, and one paired through Serve a device of Serve only.

    A cookie is scoped by host and not by port, so the local one travels to every other service on 127.0.0.1 (and to whatever holds
    the port while the hub is down); if the same value also opened the tailnet path, a program that got it and wrote a Tailscale
    header itself would control the sessions through it. The way in is recorded with the device, and each path asks for its own."""

    def devices_file(self):
        return json.loads((self.hub_.app.config.pairing_dir / "devices.json").read_text())

    def pair_local(self) -> Client:
        client = Client(self.s.port)
        self.hub_.pair(client)
        return client

    def age(self, access: str, days: float) -> None:
        """Makes every device of that way in `days` days old."""
        path = self.hub_.app.config.pairing_dir / "devices.json"
        devices = json.loads(path.read_text())
        for value in devices.values():
            if value.get("access") == access:
                value["paired_at"] = int(__import__("time").time() - days * 86400)
        path.write_text(json.dumps(devices))
        os.chmod(path, 0o600)

    def test_the_way_in_is_recorded_with_the_device_and_nothing_else_changes_in_the_store(self) -> None:
        self.pair_local()
        self.pair_tailnet()
        devices = self.devices_file().values()
        self.assertEqual(sorted(v["access"] for v in devices), ["local", "tailnet"])
        for value in devices:
            self.assertEqual(set(value), {"label", "paired_at", "access"})
        self.assertEqual({d.access for d in self.hub_.app.pairing.writer.devices()}, {"local", "tailnet"})

    def test_a_cookie_paired_locally_is_refused_through_serve_even_with_the_owners_headers_forged(self) -> None:
        local = self.pair_local()
        forged = Client(self.s.port, host=TAILNET_HOST, origin=self.tailnet.origin)
        forged.cookie = local.cookie
        self.assertEqual(forged.get("/api/hub/machine", headers=self.login).status, 401)
        self.assertEqual(forged.get("/api/hub/sessions", headers=self.login).status, 401)
        self.assertEqual(forged.post("/api/hub/link", {}, headers=self.login).status, 401)
        self.assertEqual(forged.post("/api/hub/start", {"path": "/x"}, headers=self.login).status, 401)
        self.assertEqual(local.get("/api/hub/machine").status, 200, "on its own path the cookie still works")

    def test_a_cookie_paired_through_serve_is_refused_on_the_local_address(self) -> None:
        tailnet = self.pair_tailnet()
        for extra, status in ((None, 401), ({"Via": "1.1 x"}, 403), (self.login, 403)):
            stray = Client(self.s.port)
            stray.cookie = tailnet.cookie
            self.assertEqual(stray.get("/api/hub/machine", headers=extra).status, status, extra)
        self.assertEqual(tailnet.get("/api/hub/machine", headers=self.login).status, 200)

    def test_a_device_without_a_recorded_way_in_is_refused_on_both_paths(self) -> None:
        session = self.hub_.app.pairing.writer.redeem(self.token(), "paired by something that records no way in")
        self.assertTrue(session)
        cookie = f"omp_dashboard={session}"
        local = Client(self.s.port)
        local.cookie = cookie
        tailnet = Client(self.s.port, host=TAILNET_HOST, origin=self.tailnet.origin)
        tailnet.cookie = cookie
        self.assertEqual(local.get("/api/hub/machine").status, 401)
        self.assertEqual(tailnet.get("/api/hub/machine", headers=self.login).status, 401)

    def test_a_stale_cookie_of_the_other_way_does_not_hide_a_valid_one(self) -> None:
        local, tailnet = self.pair_local(), self.pair_tailnet()
        both_cookies = Client(self.s.port)
        both_cookies.cookie = f"{tailnet.cookie}; {local.cookie}"
        self.assertEqual(
            both_cookies.get("/api/hub/machine").status, 200, "the local one is found behind the tailnet one"
        )

    def test_the_local_cookie_and_the_local_device_expire_sooner(self) -> None:
        local = Client(self.s.port)
        token = self.token()
        resp = local.post("/api/hub/pair", {"token": token})
        self.assertIn(f"Max-Age={7 * 86400};", resp.headers["Set-Cookie"] + ";")
        local.cookie = resp.headers["Set-Cookie"].split(";", 1)[0]
        tailnet = self.pair_tailnet()
        resp = self.tailnet.post("/api/hub/pair", {"token": self.token()}, headers=self.login)
        self.assertIn(f"Max-Age={90 * 86400};", resp.headers["Set-Cookie"] + ";")
        self.age("local", 8)
        self.age("tailnet", 8)
        self.assertEqual(
            local.get("/api/hub/machine").status, 401, "a local device older than a week is no longer paired"
        )
        self.assertEqual(
            tailnet.get("/api/hub/machine", headers=self.login).status, 200, "a tailnet one is, for 90 days"
        )
        self.age("tailnet", 91)
        self.assertEqual(tailnet.get("/api/hub/machine", headers=self.login).status, 401)

    def test_the_local_age_is_a_setting_and_never_longer_than_the_general_one(self) -> None:
        s = self.make()
        hub = self.hub(
            s,
            {
                "auth": {"adapter": "both", "tailscale": {"servePort": SERVE, "owner": OWNER, "publicHost": NAME}},
                "pairing": {"localMaxAgeDays": 2, "maxAgeDays": 1},
            },
        )
        resp = hub.client.post("/api/hub/pair", {"token": hub.app.pairing.writer.create_token()})
        self.assertIn(f"Max-Age={86400};", resp.headers["Set-Cookie"] + ";")
        self.assertEqual(s.config().get("pairing.localMaxAgeDays"), 2)
        self.assertEqual(self.make().config().get("pairing.localMaxAgeDays"), 7)
        self.assertTrue(self.make(OMP_DASHBOARD_PAIRING_LOCAL_MAX_AGE_DAYS="0").config().problems)

    def test_the_other_adapters_keep_one_age_and_record_their_one_way_in(self) -> None:
        for config, expected in (
            ({}, "local"),
            (
                {
                    "auth": {
                        "adapter": "tailscale-serve",
                        "tailscale": {"owner": OWNER, "publicHost": NAME, "servePort": SERVE},
                    }
                },
                "tailnet",
            ),
        ):
            s = self.make()
            hub = self.hub(s, config)
            if expected == "local":
                resp = hub.client.post("/api/hub/pair", {"token": hub.app.pairing.writer.create_token()})
            else:
                client = Client(s.port, host=TAILNET_HOST, origin=f"https://{NAME}:{SERVE}")
                resp = client.post(
                    "/api/hub/pair", {"token": hub.app.pairing.writer.create_token()}, headers=self.login
                )
            self.assertEqual(resp.status, 200)
            self.assertIn(f"Max-Age={90 * 86400};", resp.headers["Set-Cookie"] + ";")
            (device,) = json.loads((hub.app.config.pairing_dir / "devices.json").read_text()).values()
            self.assertEqual(device["access"], expected)

    def test_a_device_paired_before_switching_to_both_keeps_working_on_its_own_path_only(self) -> None:
        # a loopback-only pairing recorded `local`: the same value is a local device after the switch and nothing else
        session = self.hub_.app.pairing.writer.redeem(self.token(), "paired earlier", "local")
        client = Client(self.s.port)
        client.cookie = f"omp_dashboard={session}"
        self.assertEqual(client.get("/api/hub/machine").status, 200)
        forged = Client(self.s.port, host=TAILNET_HOST, origin=self.tailnet.origin)
        forged.cookie = client.cookie
        self.assertEqual(forged.get("/api/hub/machine", headers=self.login).status, 401)

    def test_revoking_a_device_ends_it_on_its_path(self) -> None:
        local, tailnet = self.pair_local(), self.pair_tailnet()
        self.hub_.app.pairing.writer.revoke(None)
        self.assertEqual(local.get("/api/hub/machine").status, 401)
        self.assertEqual(tailnet.get("/api/hub/machine", headers=self.login).status, 401)

    def test_doctor_and_devices_say_which_way_each_browser_came(self) -> None:
        self.pair_local()
        self.pair_tailnet()
        self.hub_.app.pairing.writer.redeem(self.token(), "no way recorded")
        findings = {f.id: f for f in doctor.run_doctor(self.s.config(), self.s.env)}
        message = findings["pairing"].message
        self.assertIn("1 local", message)
        self.assertIn("1 tailnet", message)
        self.assertIn("1 without a recorded", message)
        self.assertEqual(findings["pairing"].level, "warn")


class Configuration(ScratchTestCase):
    def test_the_adapter_name_is_accepted_in_the_file_and_in_the_environment(self) -> None:
        s = self.make(OMP_DASHBOARD_AUTH="both")
        self.assertEqual(s.config().problems, [])
        self.assertEqual(s.config().get("auth.adapter"), "both")
        s2 = self.make()
        s2.write_config({"auth": {"adapter": "both"}})
        self.assertEqual(s2.config().problems, [])
        s3 = self.make(OMP_DASHBOARD_AUTH="everyone")
        self.assertTrue(s3.config().problems)

    def test_the_hub_still_binds_loopback_only(self) -> None:
        from omp_dashboard.util import DashboardError

        with self.assertRaises(DashboardError):
            both().check_bind("0.0.0.0")
        both().check_bind("::1")


class DocumentsMatchTheCode(unittest.TestCase):
    """What the API reference says about cookie lifetimes and relay refusals is what the code does."""

    def test_the_api_reference_states_both_cookie_lifetimes_and_the_relay_refusals(self) -> None:
        from omp_dashboard.config import DEFAULTS
        from support import PLUGIN

        api = (PLUGIN / "docs" / "api.md").read_text()
        general, local = DEFAULTS["pairing"]["maxAgeDays"], DEFAULTS["pairing"]["localMaxAgeDays"]
        self.assertIn(f"`pairing.maxAgeDays` (default {general} days)", api)
        self.assertIn(f"`pairing.localMaxAgeDays` instead (default {local} days", api)
        for code in ("relay_other_path", "relay_address"):
            self.assertIn(code, api)
        # and the code's own numbers agree: a local cookie set under both lasts the local limit
        s = Scratch()
        self.addCleanup(s.close)
        s.write_config({"auth": {"adapter": "both"}})
        store_config = s.config()
        self.assertEqual(store_config.get("pairing.localMaxAgeDays"), local)
        self.assertLess(local, general)


if __name__ == "__main__":
    unittest.main()
