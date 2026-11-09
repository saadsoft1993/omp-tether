"""The health/1 checks: what each does, how a failure is classified, and that every one is bounded."""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import unittest
from pathlib import Path

import support
from omp_services import checks, config, dropins, hostplat, net
from omp_services import paths as paths_mod
from omp_services.manager import make_manager
from omp_services.units import ServiceSpec
from omp_services.util import ServicesError


class CheckCase(support.Case):
    def ctx(self, **kw) -> checks.Context:
        env = {**self.s.env}
        paths = paths_mod.from_env(env, "darwin")
        cfg = config.load_config(paths, env)
        paths.state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(paths.state_dir, 0o700)
        # the owner lookup talks to the real operating system, so use the real platform, not the scratch one
        return checks.Context(cfg=cfg, paths=paths, plat=hostplat.system({}), env=env, **kw)

    def run_one(self, check: dict, timeout: float = 5, **kw) -> checks.CheckResult:
        return checks.run_check("plug", {"severity": "fail", **check}, self.ctx(**kw), timeout)

    def server(self) -> support.Server:
        srv = support.Server()
        self.addCleanup(srv.stop)
        return srv


class UrlCheckTests(CheckCase):
    def test_ok_and_status_handling(self):
        srv = self.server()
        srv.route("/health", 200)
        srv.route("/down", 503)
        srv.route("/teapot", 418)
        base = {"id": "a", "type": "url"}
        self.assertEqual(self.run_one({**base, "url": f"{srv.base}/health"}).status, "ok")
        bad = self.run_one({**base, "url": f"{srv.base}/down"})
        self.assertEqual((bad.status, bad.reason), ("failed", "status-503"))
        self.assertEqual(self.run_one({**base, "url": f"{srv.base}/teapot", "expectStatus": [418]}).status, "ok")
        self.assertEqual(self.run_one({**base, "url": f"{srv.base}/health", "expectStatus": [204]}).status, "failed")

    def test_nothing_listening_is_unreachable(self):
        r = self.run_one({"id": "a", "type": "url", "url": f"http://127.0.0.1:{support.closed_port()}/"})
        self.assertEqual((r.status, r.reason), ("unreachable", "connection-refused"))

    def test_a_redirect_is_not_followed(self):
        other = self.server()
        other.route("/landing", 200)
        srv = self.server()
        srv.route("/old", 302, "", {"Location": f"{other.base}/landing"})
        r = self.run_one({"id": "a", "type": "url", "url": f"{srv.base}/old"})
        self.assertEqual((r.status, r.reason), ("failed", "status-302"))
        self.assertEqual(other.requests, [], "the redirect target was never contacted")

    def test_slow_server_is_cut_off_at_the_deadline(self):
        srv = self.server()
        srv.route("/slow", 200, delay=3)
        started = time.monotonic()
        r = self.run_one({"id": "a", "type": "url", "url": f"{srv.base}/slow"}, timeout=1)
        self.assertEqual(r.status, "unreachable")
        self.assertLess(time.monotonic() - started, 2.5)

    def test_a_server_that_drips_bytes_is_still_cut_off(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)

        def drip():
            conn, _ = listener.accept()
            try:
                for _ in range(20):
                    conn.sendall(b"H")
                    time.sleep(0.2)
            except OSError:
                pass
            finally:
                conn.close()

        import threading

        threading.Thread(target=drip, daemon=True).start()
        started = time.monotonic()
        r = self.run_one({"id": "a", "type": "url", "url": f"http://127.0.0.1:{listener.getsockname()[1]}/"}, timeout=1)
        self.assertNotEqual(r.status, "ok")
        self.assertLess(time.monotonic() - started, 2.5)

    def test_freshness(self):
        srv = self.server()
        now = time.time()
        srv.route("/fresh", 200, json.dumps({"data": {"at": now - 5}}))
        srv.route("/old", 200, json.dumps({"data": {"at": now - 500}}))
        srv.route("/iso", 200, json.dumps({"data": {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 5))}}))
        srv.route("/future", 200, json.dumps({"data": {"at": now + 9999}}))
        srv.route("/junk", 200, "not json")
        srv.route("/missing", 200, json.dumps({"other": 1}))
        f = {"jsonPointer": "/data/at", "maxAgeSeconds": 60}
        base = {"id": "a", "type": "url"}
        self.assertEqual(self.run_one({**base, "url": f"{srv.base}/fresh", "freshness": f}).status, "ok")
        old = self.run_one({**base, "url": f"{srv.base}/old", "freshness": f})
        self.assertEqual((old.status, old.reason), ("stale", "older-than-60s"))
        self.assertEqual(
            self.run_one({**base, "url": f"{srv.base}/iso", "freshness": {**f, "format": "iso8601"}}).status, "ok"
        )
        self.assertEqual(
            self.run_one({**base, "url": f"{srv.base}/future", "freshness": f}).reason, "timestamp-in-future"
        )
        self.assertEqual(
            self.run_one({**base, "url": f"{srv.base}/junk", "freshness": f}).reason, "freshness-unreadable"
        )
        self.assertEqual(
            self.run_one({**base, "url": f"{srv.base}/missing", "freshness": f}).reason, "freshness-unreadable"
        )

    def test_remote_hosts_are_refused_unless_allowed(self):
        r = self.run_one({"id": "a", "type": "url", "url": "http://example.com/"})
        self.assertEqual((r.status, r.reason), ("invalid", "remote-host-not-allowed"))
        r = self.run_one({"id": "a", "type": "port", "host": "example.com", "port": 80})
        self.assertEqual(r.status, "invalid")

    def test_the_result_never_contains_the_url(self):
        srv = self.server()
        token = "tok-" + os.urandom(6).hex()
        srv.route(f"/{token}", 500)
        r = self.run_one({"id": "a", "type": "url", "url": f"{srv.base}/{token}?k={token}"})
        self.assertNotIn(token, json.dumps(r.as_dict()))


class PortAndOwnerTests(CheckCase):
    def test_port(self):
        srv = self.server()
        self.assertEqual(self.run_one({"id": "p", "type": "port", "port": srv.port}).status, "ok")
        r = self.run_one({"id": "p", "type": "port", "port": support.closed_port()})
        self.assertEqual((r.status, r.reason), ("unreachable", "connection-refused"))

    def test_uid_owner_self_and_mismatch(self):
        srv = self.server()
        r = self.run_one({"id": "o", "type": "uid-owner", "port": srv.port})
        self.assertEqual(r.status, "ok", r.reason)
        other = self.run_one({"id": "o", "type": "uid-owner", "port": srv.port, "uid": os.geteuid() + 1})
        self.assertEqual((other.status, other.reason), ("failed", "owner-mismatch"))
        gone = self.run_one({"id": "o", "type": "uid-owner", "port": support.closed_port()})
        self.assertEqual((gone.status, gone.reason), ("unreachable", "no-listener"))

    def test_proc_net_tcp_parsing(self):
        text = (
            "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
            "   0: 0100007F:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 11111 1 0 100 0 0 10 0\n"
            "   1: 00000000:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 22222 1 0 100 0 0 10 0\n"
            "   2: 0100007F:0050 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 33333 1 0 100 0 0 10 0\n"
            "   3: 0100007F:1F90 0100007F:9999 01 00000000:00000000 00:00000000 00000000  1234        0 44444 1 0 100 0 0 10 0\n"
            "garbage line\n"
        )
        self.assertEqual(hostplat.parse_proc_net_tcp(text, 8080), {0, 1000})  # 0x1F90; the ESTABLISHED row is skipped
        self.assertEqual(hostplat.parse_proc_net_tcp(text, 80), {1000})
        self.assertEqual(hostplat.parse_proc_net_tcp(text, 81), set())

    def test_lsof_parsing(self):
        self.assertEqual(hostplat.parse_lsof_uids("p123\nu501\np456\nu0\n"), {0, 501})
        self.assertEqual(hostplat.parse_lsof_uids(""), set())

    def test_linux_listener_lookup_reads_proc(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "net").mkdir()
            (Path(d) / "net" / "tcp").write_text(
                "header\n   0: 0100007F:1F90 00000000:0000 0A 0:0 0:0 0  42 0 1 1 0 100 0 0 10 0\n"
            )
            self.assertEqual(hostplat.listener_uids(8080, "linux", proc_root=Path(d)), {42})
            with self.assertRaises(ServicesError):
                hostplat.listener_uids(8080, "linux", proc_root=Path(d) / "nope")


class CommandAndFileTests(CheckCase):
    def test_command_exit_status_and_output(self):
        py = sys.executable
        ok = self.run_one(
            {"id": "c", "type": "command", "argv": [py, "-c", "print('hello world')"], "expectOutputContains": "hello"}
        )
        self.assertEqual(ok.status, "ok")
        miss = self.run_one(
            {"id": "c", "type": "command", "argv": [py, "-c", "print('x')"], "expectOutputContains": "hello"}
        )
        self.assertEqual((miss.status, miss.reason), ("failed", "output-mismatch"))
        bad = self.run_one({"id": "c", "type": "command", "argv": [py, "-c", "raise SystemExit(7)"]})
        self.assertEqual((bad.status, bad.reason), ("failed", "exit-7"))
        want = self.run_one({"id": "c", "type": "command", "argv": [py, "-c", "raise SystemExit(7)"], "expectExit": 7})
        self.assertEqual(want.status, "ok")

    def test_command_rules(self):
        self.assertEqual(
            self.run_one({"id": "c", "type": "command", "argv": ["python3", "-c", "1"]}).reason, "argv0-not-absolute"
        )
        self.assertEqual(
            self.run_one({"id": "c", "type": "command", "argv": ["/nonexistent/x"]}).reason, "not-executable"
        )
        # no shell: metacharacters are plain arguments
        r = self.run_one(
            {
                "id": "c",
                "type": "command",
                "argv": [
                    sys.executable,
                    "-c",
                    "import sys; sys.exit(0 if sys.argv[1] == '$(id); echo' else 9)",
                    "$(id); echo",
                ],
            }
        )
        self.assertEqual(r.status, "ok")

    def test_command_deadline_kills_the_whole_process_group(self):
        marker = self.s.root / "grandchild-survived"
        child = self.s.root / "grandchild.py"
        child.write_text(f"import pathlib, time\ntime.sleep(2.5)\npathlib.Path({str(marker)!r}).write_text('x')\n")
        parent = self.s.root / "parent.py"
        parent.write_text(
            f"import subprocess, sys, time\nsubprocess.Popen([sys.executable, {str(child)!r}])\ntime.sleep(60)\n"
        )
        started = time.monotonic()
        r = self.run_one({"id": "c", "type": "command", "argv": [sys.executable, str(parent)]}, timeout=1)
        self.assertEqual((r.status, r.reason), ("failed", "deadline"))
        self.assertLess(time.monotonic() - started, 3)
        time.sleep(3)
        self.assertFalse(marker.exists(), "the grandchild was killed with the group")

    def test_command_output_is_capped_and_never_in_the_result(self):
        code = "import sys; sys.stdout.write('A' * 5_000_000); sys.stderr.write('SECRET-OUT' * 10)"
        r = self.run_one({"id": "c", "type": "command", "argv": [sys.executable, "-c", code]})
        self.assertEqual(r.status, "ok")
        self.assertNotIn("SECRET-OUT", json.dumps(r.as_dict()))

    def test_command_environment_is_small(self):
        os.environ["OSV_LEAK_TEST"] = "leaked"
        self.addCleanup(os.environ.pop, "OSV_LEAK_TEST", None)
        code = "import os, sys; sys.exit(1 if 'OSV_LEAK_TEST' in os.environ else 0)"
        self.assertEqual(
            self.run_one({"id": "c", "type": "command", "argv": [sys.executable, "-c", code]}).status, "ok"
        )

    def test_file_age(self):
        d = self.s.root / "backups"
        d.mkdir()
        self.assertEqual(
            self.run_one({"id": "f", "type": "file-age", "path": f"{d}/*.dump", "maxAgeSeconds": 60}).reason, "no-match"
        )
        new, old = d / "new.dump", d / "old.dump"
        new.write_text("x")
        old.write_text("x")
        os.utime(old, (time.time() - 1000, time.time() - 1000))
        self.assertEqual(
            self.run_one({"id": "f", "type": "file-age", "path": str(old), "maxAgeSeconds": 60}).status, "stale"
        )
        self.assertEqual(
            self.run_one({"id": "f", "type": "file-age", "path": f"{d}/*.dump", "maxAgeSeconds": 60}).status, "ok"
        )  # newest wins
        r = self.run_one({"id": "f", "type": "file-age", "path": str(old), "maxAgeSeconds": 60})
        self.assertEqual(r.reason, "older-than-60s")


class ServeRouteTests(CheckCase):
    ROUTE = {"Web": {"host.example:8443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:9000"}}}}}  # noqa: RUF012

    def test_route_present(self):
        self.s.set_tailscale(self.ROUTE)
        c = {"id": "s", "type": "serve-route", "port": 8443, "path": "/", "target": "http://127.0.0.1:9000/"}
        self.assertEqual(self.run_one(c).status, "ok")

    def test_missing_route_is_not_on_relay(self):
        self.s.set_tailscale({})
        r = self.run_one({"id": "s", "type": "serve-route", "port": 8443, "target": "http://127.0.0.1:9000"})
        self.assertEqual((r.status, r.reason), ("not-on-relay", "no-route"))
        self.s.set_tailscale(self.ROUTE)
        r = self.run_one({"id": "s", "type": "serve-route", "port": 8443, "target": "http://127.0.0.1:9999"})
        self.assertEqual((r.status, r.reason), ("not-on-relay", "route-points-elsewhere"))
        r = self.run_one({"id": "s", "type": "serve-route", "port": 443, "target": "http://127.0.0.1:9000"})
        self.assertEqual(r.reason, "no-route")

    def test_tailscale_trouble(self):
        self.s.set_tailscale("", rc=1)
        self.assertEqual(
            self.run_one({"id": "s", "type": "serve-route", "target": "http://127.0.0.1:1"}).status, "unreachable"
        )
        self.s.set_tailscale("not json")
        self.assertEqual(
            self.run_one({"id": "s", "type": "serve-route", "target": "http://127.0.0.1:1"}).reason, "unreadable-output"
        )
        ctx = self.ctx()
        ctx.cfg.values["tools"]["tailscale"] = "/nonexistent/tailscale"
        r = checks.run_check("plug", {"id": "s", "type": "serve-route", "target": "http://127.0.0.1:1"}, ctx, 5)
        self.assertEqual(r.reason, "tailscale-not-found")

    def test_route_parsing_ignores_odd_shapes(self):
        self.assertEqual(checks.routes_in({"Web": {"h:443": {"Handlers": {"/": {"Text": "hi"}}}}}), {})
        self.assertEqual(checks.routes_in("nope"), {})
        self.assertEqual(checks.routes_in({"Web": {"noport": {"Handlers": {"/": {"Proxy": "x"}}}}}), {})


class WatchdogTests(CheckCase):
    def setUp(self):
        super().setUp()
        self.spec = ServiceSpec("plug", "svc", "osvtest.plug.svc", ("/bin/x",), (), "/", "d", False, 10, "/l", needs=())
        self.manager = make_manager("launchd", self.s.env, sleep=lambda s: None)
        (self.s.units / "osvtest.plug.svc.plist").write_text("<!-- Managed by omp-services. -->")  # a unit of ours
        self.check = {
            "id": "w",
            "type": "watchdog",
            "service": "svc",
            "restart": {"maxRestarts": 2, "cooldownSeconds": 10, "windowSeconds": 3600, "graceSeconds": 2},
        }
        self.clock = [1000.0]

    def wd(self, check=None, spec=None, actions=True):
        ctx = self.ctx(
            manager=self.manager,
            services={"plug": {"svc": spec or self.spec}},
            allow_actions=actions,
            now=lambda: self.clock[0],
        )
        return checks.run_check("plug", {"severity": "fail", **(check or self.check)}, ctx, 5)

    def test_running_is_ok_and_unknown_service_is_invalid(self):
        self.s.set_state("osvtest.plug.svc", "running")
        self.assertEqual(self.wd().reason, "running")
        r = checks.run_check(
            "plug", {"severity": "fail", **self.check, "service": "other"}, self.ctx(manager=self.manager), 5
        )
        self.assertEqual((r.status, r.reason), ("invalid", "unknown-service"))

    def test_restart_is_bounded(self):
        self.s.set_state("osvtest.plug.svc", "stopped")
        # kickstart makes the fake running again
        self.assertEqual(self.wd().reason, "restarted")
        self.assertEqual(self.s.restarts(), ["osvtest.plug.svc"])
        self.s.set_state("osvtest.plug.svc", "stopped")
        self.clock[0] += 5
        self.assertEqual(self.wd().reason, "restart-cooldown")
        self.clock[0] += 20
        self.assertEqual(self.wd().reason, "restarted")
        self.s.set_state("osvtest.plug.svc", "stopped")
        self.clock[0] += 20
        r = self.wd()
        self.assertEqual((r.status, r.reason), ("failed", "restart-limit"))
        self.assertEqual(len(self.s.restarts()), 2, "never more than maxRestarts in the window")
        self.clock[0] += 4000  # the window passes
        self.assertEqual(self.wd().reason, "restarted")

    def test_report_only_without_a_policy_or_without_actions(self):
        self.s.set_state("osvtest.plug.svc", "stopped")
        no_policy = {k: v for k, v in self.check.items() if k != "restart"}
        self.assertEqual(self.wd(no_policy).reason, "not-running")
        self.assertEqual(self.wd(actions=False).reason, "not-running-report-only")
        self.assertEqual(self.s.restarts(), [])

    def test_a_protected_service_is_never_restarted_by_a_watchdog(self):
        protected = ServiceSpec("plug", "svc", "osvtest.plug.svc", ("/bin/x",), (), "/", "d", True, 10, "/l")
        self.s.set_state("osvtest.plug.svc", "stopped")
        self.assertEqual(self.wd(spec=protected).reason, "protected-not-restarted")
        self.assertEqual(self.s.restarts(), [])

    def test_an_unloaded_service_is_not_loaded_by_the_watchdog(self):
        self.assertEqual(self.wd().reason, "not-loaded")
        self.assertEqual(self.s.restarts(), [])


class RunnerTests(CheckCase):
    def test_note_severity_does_not_count(self):
        r = self.run_one({"id": "p", "type": "port", "port": support.closed_port(), "severity": "note"})
        self.assertEqual(r.status, "unreachable")
        self.assertFalse(r.failing)
        self.assertTrue(self.run_one({"id": "p", "type": "port", "port": support.closed_port()}).failing)

    def test_the_cycle_deadline_and_invalid_drop_ins(self):
        path = self.s.health(
            "plug",
            *[
                {
                    "id": f"c{i}",
                    "type": "command",
                    "argv": [sys.executable, "-c", "import time; time.sleep(3)"],
                    "timeoutSeconds": 3,
                }
                for i in range(3)
            ],
        )
        drop = dropins.load_one("health", path)
        self.assertTrue(drop.ok, drop.errors)
        started = time.monotonic()
        results = checks.run_dropins([drop], self.ctx(), cycle_seconds=1.5)
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual([r.reason for r in results][-1], "cycle-deadline")
        bad = self.s.dropin("health", "broken", "{not json")
        results = checks.run_dropins([dropins.load_one("health", bad)], self.ctx(), 5)
        self.assertEqual((results[0].status, results[0].id), ("invalid", "dropin"))
        self.assertTrue(results[0].failing)

    def test_a_crashing_check_becomes_a_failed_result(self):
        original = checks.RUNNERS["port"]

        def boom(*a):
            raise RuntimeError("secret detail")

        checks.RUNNERS["port"] = boom
        self.addCleanup(checks.RUNNERS.__setitem__, "port", original)
        r = self.run_one({"id": "p", "type": "port", "port": 1})
        self.assertEqual((r.status, r.reason), ("failed", "internal-RuntimeError"))

    def test_url_parsing_rejects_credentials_and_odd_forms(self):
        for bad in ("http://u:p@host/", "ftp://h/", "http:///x", "http://h/a b", "http://h/#frag", "https://user@h/"):
            with self.assertRaises(ServicesError):
                net.parse_http_url(bad)
        with self.assertRaises(ServicesError):
            net.parse_http_url("http://h/", https_only=True)
        t = net.parse_http_url("https://h.example:8443/a/b?x=1")
        self.assertEqual((t.scheme, t.host, t.port, t.path), ("https", "h.example", 8443, "/a/b?x=1"))
        self.assertTrue(
            net.is_loopback_host("127.0.0.5") and net.is_loopback_host("::1") and net.is_loopback_host("localhost")
        )
        self.assertFalse(net.is_loopback_host("10.0.0.1") or net.is_loopback_host("example.com"))


if __name__ == "__main__":
    unittest.main()
