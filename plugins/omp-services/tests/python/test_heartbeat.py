"""The heartbeat: ping on success, <URL>/fail on failure, no redirects, verified TLS, and the URL is never leaked."""

from __future__ import annotations

import json
import os
import sys
import time
import unittest

import support
from omp_services import checks, config, heartbeat, net
from omp_services import paths as paths_mod
from omp_services.util import ServicesError

TOKEN = "tok-" + os.urandom(6).hex()  # built at run time so secret scanners have nothing to find


class HeartbeatCase(support.Case):
    def setUp(self):
        super().setUp()
        self.certs = support.make_certs(self.s.root)
        self.server = None

    def tls_server(self) -> support.Server:
        if not self.certs:
            self.skipTest("the openssl command is not available")
        srv = support.Server(cert=(self.certs[1], self.certs[2]))
        self.addCleanup(srv.stop)
        self.server = srv
        return srv

    def trust(self) -> dict[str, str]:
        assert self.certs
        return {"SSL_CERT_FILE": self.certs[0]}

    def configure(self, url: str | None, **heartbeat_cfg) -> None:
        heartbeat_cfg.setdefault("allowNoChecks", True)  # these tests are about pings; "no checks" has its own tests
        self.s.write_config(heartbeat={"enabled": True, **heartbeat_cfg})
        self.s.state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.s.state_dir, 0o700)
        if url is not None:
            secrets = self.s.config_dir / "secrets"
            secrets.mkdir(exist_ok=True)
            os.chmod(secrets, 0o700)
            (secrets / "heartbeat-url").write_text(url + "\n")
            os.chmod(secrets / "heartbeat-url", 0o600)

    def run_once(self, extra_env=None, **kw):
        with self.s.patched(extra_env):
            paths = paths_mod.from_env(os.environ, "darwin")
            import contextlib
            import io

            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = heartbeat.run_once(paths, os.environ, **kw)
        return rc, out.getvalue(), err.getvalue()

    def everything_written(self) -> str:
        """Every byte under the scratch home and state, as one string, for leak checks."""
        chunks = []
        for p in self.s.root.rglob("*"):
            if (
                p.is_file()
                and not p.is_symlink()
                and p.name != "heartbeat-url"
                and p.suffix not in {".crt", ".key", ".srl", ".csr"}
            ):
                try:
                    chunks.append(p.read_text(errors="replace"))
                except OSError:
                    pass
        return "\n".join(chunks)


class PingTests(HeartbeatCase):
    def test_success_pings_the_url_and_failure_pings_slash_fail(self):
        srv = self.tls_server()
        self.configure(f"{srv.base}/ping/{TOKEN}")
        port = support.closed_port()
        rc, out, err = self.run_once(self.trust())
        self.assertEqual(rc, 0, out + err)
        self.assertEqual([r["path"] for r in srv.requests], [f"/ping/{TOKEN}"])
        self.assertEqual(srv.requests[0]["method"], "GET")
        self.assertEqual(srv.requests[0]["body"], b"", "no body")
        self.s.health("plug", {"id": "down", "type": "port", "port": port})
        rc, out, err = self.run_once(self.trust())
        self.assertEqual(rc, 1)
        self.assertEqual(srv.requests[-1]["path"], f"/ping/{TOKEN}/fail")
        self.assertEqual(srv.requests[-1]["body"], b"")
        self.assertIn("plug/down unreachable connection-refused", out)

    def test_a_note_only_failure_still_pings_success(self):
        srv = self.tls_server()
        self.configure(f"{srv.base}/ping/{TOKEN}")
        self.s.health("plug", {"id": "meh", "type": "port", "port": support.closed_port(), "severity": "note"})
        rc, out, err = self.run_once(self.trust())
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(srv.requests[-1]["path"], f"/ping/{TOKEN}")

    def test_an_unusable_health_drop_in_sends_fail(self):
        srv = self.tls_server()
        self.configure(f"{srv.base}/ping/{TOKEN}")
        self.s.dropin("health", "broken", "{oops")
        rc, out, _ = self.run_once(self.trust())
        self.assertEqual(rc, 1)
        self.assertEqual(srv.requests[-1]["path"], f"/ping/{TOKEN}/fail")

    def test_query_strings_survive_the_fail_suffix(self):
        self.assertEqual(heartbeat.ping_target("https://h.example/ping/x?create=1", True).path, "/ping/x/fail?create=1")
        self.assertEqual(heartbeat.ping_target("https://h.example/ping/x/", True).path, "/ping/x/fail")
        self.assertEqual(heartbeat.ping_target("https://h.example", True).path, "/fail")
        self.assertEqual(heartbeat.ping_target("https://h.example/ping/x", False).path, "/ping/x")

    def test_a_redirect_is_not_followed_and_counts_as_a_failed_ping(self):
        other = self.tls_server()
        other.route("/landing", 200)
        srv = support.Server(cert=(self.certs[1], self.certs[2]))
        self.addCleanup(srv.stop)
        srv.route(f"/ping/{TOKEN}", 301, "", {"Location": f"{other.base}/landing"})
        self.configure(f"{srv.base}/ping/{TOKEN}")
        rc, out, err = self.run_once(self.trust())
        self.assertEqual(rc, 1)
        self.assertEqual(other.requests, [], "the redirect target was never contacted")
        self.assertIn("ping failed (status 301", out)

    def test_tls_is_verified(self):
        srv = self.tls_server()
        self.configure(f"{srv.base}/ping/{TOKEN}")
        rc, out, err = self.run_once()  # the test CA is not trusted
        self.assertEqual(rc, 1)
        self.assertEqual(srv.requests, [], "no request was made to a server whose certificate is not trusted")
        self.assertIn("tls-certificate", out)

    def test_the_url_is_never_logged_or_stored(self):
        srv = self.tls_server()
        self.configure(f"{srv.base}/ping/{TOKEN}")
        for env, _port_ok in ((self.trust(), True), ({}, False)):  # a good ping, then a certificate failure
            rc, out, err = self.run_once(env)
            self.assertNotIn(TOKEN, out + err)
        dead = f"https://127.0.0.1:{support.closed_port()}/ping/{TOKEN}"
        self.configure(dead)
        rc, out, err = self.run_once()
        self.assertNotIn(TOKEN, out + err)
        self.assertIn("connection-refused", out)
        written = self.everything_written()
        self.assertNotIn(TOKEN, written)
        status = json.loads((self.s.state_dir / "heartbeat-status.json").read_text())
        self.assertNotIn(TOKEN, json.dumps(status))

    def test_no_url_means_nothing_is_sent(self):
        self.configure(None)
        rc, out, _ = self.run_once()
        self.assertEqual(rc, 0)
        self.assertIn("no heartbeat URL is configured", out)

    def test_disabled_heartbeat_sends_nothing(self):
        srv = self.tls_server()
        self.configure(f"{srv.base}/ping/{TOKEN}")
        self.s.write_config(heartbeat={"enabled": False})
        self.run_once(self.trust())
        self.assertEqual(srv.requests, [])

    def test_no_ping_flag_checks_only(self):
        srv = self.tls_server()
        self.configure(f"{srv.base}/ping/{TOKEN}")
        rc, out, _ = self.run_once(self.trust(), send=False)
        self.assertEqual(rc, 0)
        self.assertEqual(srv.requests, [])
        self.assertTrue((self.s.state_dir / "heartbeat-status.json").exists())


class UrlRulesTests(HeartbeatCase):
    def read(self, text: str | None, mode: int = 0o600):
        self.configure(text, enabled=True)
        paths = paths_mod.from_env(self.s.env, "darwin")
        cfg = config.load_config(paths, self.s.env)
        if text is not None:
            os.chmod(paths.default_url_file, mode)
        return heartbeat.read_url(cfg, paths)

    def test_only_https_without_credentials(self):
        self.assertIsNone(self.read(None))
        self.assertEqual(self.read("https://hc.example/ping/abc"), "https://hc.example/ping/abc")
        for bad in ("http://hc.example/ping/abc", "https://user:pw@hc.example/x", "ftp://x/y", "not a url"):
            with self.assertRaises(ServicesError) as ctx:
                self.read(bad)
            self.assertNotIn("hc.example", str(ctx.exception))
            self.assertNotIn("pw", str(ctx.exception))

    def test_the_url_file_must_be_private(self):
        with self.assertRaises(ServicesError) as ctx:
            self.read("https://hc.example/ping/abc", mode=0o644)
        self.assertIn("mode", str(ctx.exception))
        self.assertNotIn("abc", str(ctx.exception))

    def test_a_symlinked_url_file_is_refused(self):
        self.configure(None)
        secrets = self.s.config_dir / "secrets"
        secrets.mkdir(exist_ok=True)
        os.chmod(secrets, 0o700)
        target = self.s.root / "elsewhere"
        target.write_text("https://hc.example/x\n")
        os.chmod(target, 0o600)
        (secrets / "heartbeat-url").symlink_to(target)
        paths = paths_mod.from_env(self.s.env, "darwin")
        with self.assertRaises(ServicesError):
            heartbeat.read_url(config.load_config(paths, self.s.env), paths)


class LogTests(HeartbeatCase):
    def test_trim_logs_keeps_one_generation_and_truncates_in_place(self):
        logs = self.s.state_dir / "logs"
        logs.mkdir(parents=True)
        big, small = logs / "big.log", logs / "small.log"
        big.write_text("x" * 5000)
        small.write_text("y" * 10)
        inode = big.stat().st_ino
        self.assertEqual(heartbeat.trim_logs(logs, 1000), ["big.log"])
        self.assertEqual(big.read_text(), "")
        self.assertEqual(big.stat().st_ino, inode, "truncated in place: the service manager keeps the file open")
        self.assertEqual((logs / "big.log.1").read_text(), "x" * 1000, "the newest max_bytes are kept")
        self.assertEqual(small.read_text(), "y" * 10)
        self.assertEqual(support.mode_of(logs / "big.log.1"), 0o600)


class LoopTests(HeartbeatCase):
    def test_the_service_loop_runs_rounds_and_stops_on_sigterm(self):
        srv = self.tls_server()
        self.configure(f"{srv.base}/ping/{TOKEN}", intervalSeconds=30)
        env = {**self.s.env, **self.trust()}
        proc = __import__("subprocess").Popen(
            [sys.executable, str(support.PLUGIN / "python" / "omp_services_main.py"), "heartbeat", "run"],
            env=env,
            stdout=__import__("subprocess").PIPE,
            stderr=__import__("subprocess").PIPE,
            text=True,
        )
        try:
            deadline = time.time() + 20
            while time.time() < deadline and not srv.requests:
                time.sleep(0.1)
            self.assertTrue(srv.requests, "the loop pinged on its first pass")
        finally:
            proc.terminate()
        out, err = proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, 0, err)
        self.assertIn("heartbeat started", out)
        self.assertIn("heartbeat stopped", out)
        self.assertNotIn(TOKEN, out + err)


class CheckUnavailable(unittest.TestCase):
    def test_http_get_error_codes_have_no_urls(self):
        got = net.http_get(net.parse_http_url(f"http://127.0.0.1:{support.closed_port()}/{TOKEN}"), 2)
        self.assertEqual(got.error, "connection-refused")
        self.assertNotIn(TOKEN, got.error or "")
        self.assertIs(checks.OK, "ok")


if __name__ == "__main__":
    unittest.main()
