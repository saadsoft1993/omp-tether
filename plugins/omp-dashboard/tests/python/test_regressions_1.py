"""Regression tests: restart, trust walk, kill timing, duplicate headers, pairing budget and expiry, ntfy topics, request contract, ACLs, bounded reads, paths, enumeration, environment."""

from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from omp_dashboard import alerts, audit, control, start, trustwalk, util
from omp_dashboard.config import load_config
from omp_dashboard.hostplat import Platform, ProcessInfo
from omp_dashboard.kill import Killer
from omp_dashboard.pairing import PairingStore, PairingWriter
from omp_dashboard.paths import from_env
from omp_dashboard.trustwalk import TrustError, check_path
from omp_dashboard.util import DashboardError, printable, run_bounded
from support import (
    MAIN,
    PLUGIN,
    Client,
    InProcessHub,
    Scratch,
    ScratchTestCase,
    host_entry,
)
from test_auth import tailscale_script

REPO = PLUGIN.parents[1]


def cli(s: Scratch, *args: str, stdin: str | None = None, env: dict | None = None) -> subprocess.CompletedProcess[str]:
    e = {**s.env, "PYTHONDONTWRITEBYTECODE": "1", **(env or {})}
    return subprocess.run(
        [sys.executable, str(MAIN), *args],
        env=e,
        capture_output=True,
        text=True,
        timeout=60,
        input=stdin if stdin is not None else "",
    )


class Tmp(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="odq", dir="/tmp")).resolve()
        os.chmod(self.root, 0o700)
        self.addCleanup(shutil.rmtree, self.root, True)


# hub restart ------------------------------------------------------------------------------------------------------------------


class HubRestart(ScratchTestCase):
    def test_a_hub_that_just_stopped_can_be_started_again_on_the_same_port_at_once(self) -> None:
        s = self.make()
        first = InProcessHub(s)
        for _ in range(3):
            self.assertEqual(
                first.client.get("/healthz").status, 200
            )  # each served connection leaves a TIME_WAIT behind
        first.close()
        second = InProcessHub(s)  # raised "Address already in use"
        self.addCleanup(second.close)
        self.assertEqual(second.client.get("/healthz").status, 200)

    def test_a_second_hub_on_a_port_that_is_in_use_is_still_refused(self) -> None:
        s = self.make()
        hub = self.hub(s)
        with self.assertRaises(OSError):
            InProcessHub(s)
        self.assertEqual(hub.client.get("/healthz").status, 200)

    def test_the_socket_option_is_reuse_address_and_not_reuse_port(self) -> None:
        text = (PLUGIN / "python/omp_dashboard/server.py").read_text()
        self.assertIn("allow_reuse_address = True", text)
        self.assertNotIn("SO_REUSEPORT", text)
        self.assertNotIn("allow_reuse_port", text)


# trust walk -----------------------------------------------------------------------------------------------------------------------


class GroupWritableDirectories(Tmp):
    def test_a_group_writable_directory_is_accepted_only_for_a_group_with_no_other_member(self) -> None:
        d = self.root / "g"
        d.mkdir()
        os.chmod(d, 0o775)
        gid = os.stat(d).st_gid
        with (
            mock.patch.object(sys, "platform", "linux"),
            mock.patch.object(trustwalk, "_acl_problem", return_value=None),
        ):
            with mock.patch.object(trustwalk, "group_is_private", return_value=True):
                check_path(d / "x")
            with (
                mock.patch.object(trustwalk, "group_is_private", return_value=False),
                self.assertRaisesRegex(TrustError, "writable by group"),
            ):
                check_path(d / "x")
        self.assertTrue(isinstance(gid, int))

    def test_on_macos_a_group_writable_directory_is_never_accepted(self) -> None:
        d = self.root / "g"
        d.mkdir()
        os.chmod(d, 0o775)
        with (
            mock.patch.object(sys, "platform", "darwin"),
            mock.patch.object(trustwalk, "group_is_private", return_value=True),
            mock.patch.object(trustwalk, "_acl_problem", return_value=None),
        ):
            with self.assertRaisesRegex(TrustError, "writable by group"):
                check_path(d / "x")

    def test_others_writable_is_refused_whatever_the_group_and_a_file_too(self) -> None:
        d = self.root / "o"
        d.mkdir()
        os.chmod(d, 0o757)
        with (
            mock.patch.object(sys, "platform", "linux"),
            mock.patch.object(trustwalk, "group_is_private", return_value=True),
            self.assertRaises(TrustError),
        ):
            check_path(d / "x")

    @unittest.skipUnless(
        sys.platform.startswith("linux") and trustwalk.group_is_private(os.getgid(), os.getuid()),
        "needs Linux and a private primary group",
    )
    def test_with_a_private_group_a_group_writable_directory_and_a_0664_file_are_trusted_for_real(self) -> None:
        d = self.root / "real"
        d.mkdir()
        os.chmod(d, 0o775)  # what umask 002 makes of a directory
        f = d / "config.json"
        f.write_text("{}")
        os.chmod(f, 0o664)
        check_path(f)
        self.assertEqual(util.read_trusted_file(f), b"{}")

    def test_a_group_is_private_only_when_every_member_is_us_or_root(self) -> None:
        passwd = "me:x:1000:100:Me:/home/me:/bin/sh\nroot:x:0:0::/root:/bin/sh\nother:x:1001:100::/home/o:/bin/sh\nalone:x:1002:1002::/home/a:/bin/sh\n"
        group = "users:x:100:me\nalone:x:1002:\nshared:x:1003:me,bob\nstaff:x:1004:root,me\n"
        files = {"/etc/passwd": passwd, "/etc/group": group}
        read = files.get
        self.assertFalse(trustwalk.group_is_private(100, 1000, read), "another user has it as primary group")
        self.assertFalse(trustwalk.group_is_private(1003, 1000, read), "bob is a member")
        self.assertTrue(trustwalk.group_is_private(1004, 1000, read))
        self.assertFalse(trustwalk.group_is_private(1002, 1000, read), "1002 belongs to another user's own group")
        self.assertFalse(
            trustwalk.group_is_private(4242, 1000, read), "a group the files do not describe is not private"
        )
        self.assertFalse(trustwalk.group_is_private(100, 1000, lambda f: None), "unreadable files are not private")


@unittest.skipUnless(sys.platform == "darwin", "macOS ACLs")
class InheritedAcls(Tmp):
    def test_an_inherited_write_grant_on_a_file_is_refused_after_its_source_is_gone(self) -> None:
        parent = self.root / "p"
        parent.mkdir(mode=0o700)
        subprocess.run(
            ["/bin/chmod", "+a", "group:everyone allow write,file_inherit,directory_inherit", str(parent)], check=True
        )
        d = parent / "d"
        d.mkdir(mode=0o700)
        f = d / "f"
        f.write_text("x")
        os.chmod(f, 0o600)
        subprocess.run(
            ["/bin/chmod", "-a#", "0", str(parent)], check=True
        )  # the source is gone; the copies stay, marked `inherited`
        for path in (parent, d, f):
            self.assertNotIn(
                "everyone", subprocess.run(["/bin/ls", "-lde", str(path)], capture_output=True, text=True).stdout
            ) if path == parent else None
        listing = subprocess.run(["/bin/ls", "-lde", str(f)], capture_output=True, text=True).stdout
        self.assertIn("inherited", listing)
        with self.assertRaises(TrustError):
            check_path(f)

    def test_an_inherited_read_entry_is_understood_and_does_not_make_a_plain_file_untrusted(self) -> None:
        parent = self.root / "q"
        parent.mkdir(mode=0o700)
        subprocess.run(
            ["/bin/chmod", "+a", "group:everyone allow read,file_inherit,directory_inherit", str(parent)], check=True
        )
        d = parent / "d"
        d.mkdir(mode=0o700)
        f = d / "f"
        f.write_text("x")
        os.chmod(f, 0o600)
        subprocess.run(["/bin/chmod", "-a#", "0", str(parent)], check=True)
        self.assertIn("inherited", subprocess.run(["/bin/ls", "-lde", str(f)], capture_output=True, text=True).stdout)
        check_path(f)  # an inherited entry that grants no write is read, and harmless for a file that is not a secret

    def test_an_unrecognised_acl_line_is_not_trusted(self) -> None:
        d = self.root / "w"
        d.mkdir(mode=0o700)

        def fake(argv, **kw):
            lines = []
            for operand in argv[3:]:
                lines.append(f"drwx------+ 2 me staff 64 Nov 10 12:00 {operand}")
                lines.append(" 0: something odd that is not an entry")
            return subprocess.CompletedProcess(argv, 0, stdout="\n".join(lines) + "\n", stderr="")

        with mock.patch.object(trustwalk, "_run", side_effect=fake), self.assertRaises(TrustError):
            check_path(d / "x")


# kill group timing ------------------------------------------------------------------------------------------------------------------


class KillGroupTiming(unittest.TestCase):
    def test_the_group_is_signalled_only_at_the_deadline_never_after_the_child_exited_and_was_reaped(self) -> None:
        calls: list[int] = []
        real = os.killpg
        with mock.patch.object(os, "killpg", side_effect=lambda pid, sig: (calls.append(pid), real(pid, sig))):
            run_bounded([sys.executable, "-c", "pass"], 10)
            self.assertEqual(calls, [], "a finished child's pid may already belong to someone else")
            r = run_bounded([sys.executable, "-c", "import time; time.sleep(30)"], 0.5)
        self.assertTrue(r.timed_out)
        self.assertEqual(len(calls), 1)

    def test_a_process_a_backend_left_in_the_group_is_not_killed_after_a_normal_exit(self) -> None:
        marker = tempfile.mktemp(prefix="odq", dir="/tmp")
        inner = f"import time,os;open({marker!r},'w').write(str(os.getpid()));time.sleep(20)"
        code = f"import subprocess,sys;subprocess.Popen([sys.executable,'-c',{inner!r}], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)"
        r = run_bounded([sys.executable, "-c", code], 10)
        self.assertEqual(r.returncode, 0)
        deadline = time.monotonic() + 5
        while not os.path.exists(marker) and time.monotonic() < deadline:
            time.sleep(0.05)
        pid = int(Path(marker).read_text())
        os.unlink(marker)
        try:
            os.kill(pid, 0)  # still alive: a daemon a backend started is the backend's
        finally:
            os.kill(pid, 9)


# tailscale duplicate headers -----------------------------------------------------------------------------------------------------


class DuplicateHeaders(ScratchTestCase):
    def setUp(self) -> None:
        self.s = self.make()
        ts = tailscale_script(self.s.bin)
        self.hub_ = self.hub(
            self.s, {"auth": {"adapter": "tailscale-serve", "tailscale": {"bin": str(ts), "servePort": 8443}}}
        )
        self.host = "box.tail1234.ts.example:8443"

    def raw(self, *headers: str) -> int:
        with socket.create_connection(("127.0.0.1", self.s.port), timeout=5) as sock:
            sock.sendall(("GET /api/hub/machine HTTP/1.0\r\n" + "\r\n".join(headers) + "\r\n\r\n").encode())
            return int(sock.recv(100).split(b" ")[1])

    def test_one_login_and_one_host_pass_the_adapter_and_two_of_either_are_refused(self) -> None:
        good = [f"Host: {self.host}", "Tailscale-User-Login: me@example.test"]
        self.assertEqual(self.raw(*good), 401, "past the adapter: now it wants a cookie")
        self.assertEqual(self.raw(*good, "Tailscale-User-Login: me@example.test"), 403)
        self.assertEqual(self.raw("Tailscale-User-Login: intruder@example.test", *good), 403)
        self.assertEqual(self.raw(*good, f"Host: {self.host}"), 403)
        self.assertEqual(self.raw(*good, "Tailscale-Funnel-Request: ?1", "Tailscale-Funnel-Request: ?0"), 403)

    def test_a_login_with_control_characters_is_refused(self) -> None:
        from omp_dashboard.auth import TailscaleServe

        a = TailscaleServe(
            None, 8443, "me@example.test\x07", "box.example.test"
        )  # the owner as Tailscale reported it, bell included
        headers = {"Host": "box.example.test:8443", "Tailscale-User-Login": "me@example.test\x07"}

        class H(dict):
            def get_all(self, name, default=None):
                return [self[name]] if name in self else default

        self.assertEqual(a.check(H(headers)).status, 403)


# pairing budget ---------------------------------------------------------------------------------------------------------------------


class PairingBudget(ScratchTestCase):
    def test_a_valid_link_still_works_while_wrong_guesses_are_being_refused(self) -> None:
        s = self.make()
        hub = self.hub(s)
        fresh = Client(s.port)
        for _ in range(10):
            fresh.post("/api/hub/pair", {"token": "wrong"})
        self.assertEqual(fresh.post("/api/hub/pair", {"token": "wrong"}).status, 429)
        token = hub.app.pairing.writer.create_token()
        owner = Client(s.port)
        self.assertEqual(
            owner.pair_with(token).status, 200, "ten wrong guesses by someone else must not lock the owner out"
        )
        self.assertEqual(owner.get("/api/hub/machine").status, 200)
        self.assertEqual(fresh.post("/api/hub/pair", {"token": "wrong"}).status, 429, "and wrong guesses stay limited")

    def test_a_token_that_was_already_used_or_has_expired_is_not_valid_for_the_bypass(self) -> None:
        s = self.make()
        hub = self.hub(s)
        for _ in range(10):
            Client(s.port).post("/api/hub/pair", {"token": "wrong"})
        token = hub.app.pairing.writer.create_token()
        self.assertEqual(Client(s.port).pair_with(token).status, 200)
        self.assertEqual(Client(s.port).post("/api/hub/pair", {"token": token}).status, 429)


# pairing expiry -----------------------------------------------------------------------------------------------------------------------


class PairingExpiry(ScratchTestCase):
    def test_a_browser_older_than_the_configured_age_is_no_longer_paired(self) -> None:
        s = self.make()
        s.write_config({"pairing": {"maxAgeDays": 30}})
        hub = self.hub(s)
        c = hub.pair()
        self.assertIn(
            "Max-Age=2592000", Client(s.port).pair_with(hub.app.pairing.writer.create_token()).headers["Set-Cookie"]
        )
        self.assertEqual(c.get("/api/hub/machine").status, 200)
        devices = Path(hub.config.pairing_dir / "devices.json")
        data = json.loads(devices.read_text())
        for entry in data.values():
            entry["paired_at"] = int(time.time()) - 31 * 86400
        devices.write_text(json.dumps(data))
        os.chmod(devices, 0o600)
        self.assertEqual(c.get("/api/hub/machine").status, 401)

    def test_the_default_is_ninety_days_and_null_means_never(self) -> None:
        s = self.make()
        self.assertEqual(s.config().get("pairing.maxAgeDays"), 90)
        s.write_config({"pairing": {"maxAgeDays": None}})
        self.assertIsNone(s.config().get("pairing.maxAgeDays"))
        s.write_config({"pairing": {"maxAgeDays": 0}})
        self.assertTrue(s.config().problems)

    def test_revoke_all_on_the_command_line_also_cancels_unused_pairing_links(self) -> None:
        s = self.make()
        w = PairingWriter(s.paths().pairing_dir)
        token = w.create_token()
        r = cli(s, "revoke", "all")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(w.redeem(token, "x"), "a link made before `revoke all` must not pair a browser after it")

    def test_the_writer_still_leaves_pending_tokens_alone_as_the_protocol_says(self) -> None:
        s = self.make()
        w = PairingWriter(s.paths().pairing_dir)
        token = w.create_token()
        w.revoke(None)
        self.assertTrue(w.redeem(token, "x"))


# ntfy topic and payloads -------------------------------------------------------------------------------------------------------------


class NtfyTopic(ScratchTestCase):
    def test_a_supplied_topic_must_be_long_and_never_comes_from_the_command_line(self) -> None:
        s = self.make()
        self.assertEqual(
            cli(s, "config", "set-ntfy-topic", "my-long-enough-topic-name-1").returncode,
            2,
            "a positional topic would be visible in ps and shell history",
        )
        short = cli(s, "config", "set-ntfy-topic", "--stdin", stdin="short-topic\n")
        self.assertEqual(short.returncode, 2)
        ok = cli(s, "config", "set-ntfy-topic", "--stdin", stdin="a-long-enough-topic-1234\n")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual((s.paths().secrets_dir / "ntfy-topic").read_text().strip(), "a-long-enough-topic-1234")
        self.assertEqual(stat.S_IMODE(os.stat(s.paths().secrets_dir / "ntfy-topic").st_mode), 0o600)

    def test_the_random_topic_is_long_and_the_reader_refuses_a_short_file(self) -> None:
        s = self.make()
        self.assertEqual(cli(s, "config", "set-ntfy-topic").returncode, 0)
        self.assertGreaterEqual(len((s.paths().secrets_dir / "ntfy-topic").read_text().strip()), 16)
        f = s.paths().secrets_dir / "ntfy-topic"
        f.write_text("short\n")
        with self.assertRaises(DashboardError):
            alerts.read_topic(f)


class ReplyPayload(unittest.TestCase):
    def test_the_payload_has_no_tags_at_all(self) -> None:
        self.assertNotIn("tags", alerts.ntfy_payload("t" * 16, "T", "B"))
        self.assertNotIn("robot", json.dumps(alerts.ntfy_payload("t" * 16, "T", "B")))


# request contract ---------------------------------------------------------------------------------------------------------------------


class PostContract(ScratchTestCase):
    def setUp(self) -> None:
        self.s = self.make([host_entry(1)])
        self.hub_ = self.hub(self.s)
        self.hub_.pair()
        self.c = self.hub_.client

    def audit_lines(self) -> list[dict]:
        p = self.hub_.config.audit_file
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def test_a_post_body_must_be_declared_json(self) -> None:
        for ctype in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x", ""):
            r = self.c.request("POST", "/api/hub/alerts/test", raw_body=b"{}", headers={"Content-Type": ctype})
            self.assertEqual(r.status, 415, ctype)
        ok = self.c.request(
            "POST", "/api/hub/alerts/test", raw_body=b"{}", headers={"Content-Type": "application/json; charset=utf-8"}
        )
        self.assertNotEqual(ok.status, 415)

    def test_other_methods_answer_405_or_501_as_the_api_page_says(self) -> None:
        self.assertEqual(self.c.request("PROPFIND", "/").status, 501)
        self.assertEqual(self.c.request("PUT", "/").status, 405)
        text = (PLUGIN / "docs/api.md").read_text()
        self.assertIn("501", text)
        self.assertIn("415", text)

    def test_refused_links_failed_pairing_and_alert_tests_are_audited_without_secrets(self) -> None:
        self.c.post("/api/hub/link", {"instanceId": "inst0001", "view": True})  # no relay: refused
        Client(self.s.port).post("/api/hub/pair", {"token": "wrong-token-value"})
        self.c.post("/api/hub/alerts/test", {})
        for _ in range(10):
            Client(self.s.port).post("/api/hub/pair", {"token": "x"})
        lines = {(x["action"], x["outcome"]) for x in self.audit_lines()}
        for want in (
            ("link", "refused:relay_off"),
            ("pair", "refused"),
            ("alerts-test", "refused:no_topic"),
            ("pair", "rate_limited"),
        ):
            self.assertIn(want, lines)
        text = self.hub_.config.audit_file.read_text()
        self.assertNotIn("wrong-token-value", text)

    def test_the_command_line_audits_its_own_pairing_changes(self) -> None:
        s = self.make()
        s.write_config({"audit": {"file": str(s.root / "audit.jsonl")}})
        w = PairingWriter(s.paths().pairing_dir)
        w.redeem(w.create_token(), "x")
        r = cli(s, "revoke", "all")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('"action": "revoke"', (s.root / "audit.jsonl").read_text())


# names, replies, tmux ------------------------------------------------------------------------------------------------------------


class PrintableNames(unittest.TestCase):
    def test_bidi_and_invisible_format_characters_are_stripped_from_names(self) -> None:
        self.assertEqual(printable("prod\u202eved\u200b\ufeff\u2066x"), "prodvedx")
        self.assertEqual(printable("normal é ✓"), "normal é ✓")

    def test_a_session_name_with_such_characters_reaches_the_page_without_them(self) -> None:
        from omp_dashboard.omp import _host

        h = _host({"instanceId": "inst0001", "sessionName": "re\u202eadme", "cwd": "/a/b\u200bc"})
        self.assertEqual((h["sessionName"], h["cwd"]), ("readme", "/a/bc"))


class RefusalReplies(Tmp):
    def test_a_refusal_code_must_be_a_short_lowercase_word_and_a_result_only_has_the_known_keys(self) -> None:
        from test_control import SESSION, ControlCase, FakeExtension

        class Case(ControlCase):
            def runTest(self) -> None:
                pass

        case = Case()
        case.setUp()
        try:
            e = FakeExtension(case.dir)
            case.exts.append(e)
            e.behaviour = "refuse_snapshot"
            original = e.reply

            def evil(req):
                r = original(req)
                if r.get("code"):
                    r["code"] = "<script>" + "x" * 300
                return r

            e.reply = evil  # type: ignore[method-assign]
            with self.assertRaises(control.ControlError) as ctx:
                control.control(case.cdir(), SESSION, e.pid, "abort")
            self.assertEqual(ctx.exception.code, "refused")
            self.assertNotIn("script", str(ctx.exception))
            e.behaviour = "normal"

            def extra(req):
                r = original(req)
                if r.get("ok") and req["op"] == "abort":
                    r["result"] = {"aborted": True, "junk": "x" * 1000, "html": "<b>"}
                return r

            e.reply = extra  # type: ignore[method-assign]
            self.assertEqual(control.control(case.cdir(), SESSION, e.pid, "abort"), {"aborted": True})
        finally:
            case.doCleanups()


class TmuxStart(unittest.TestCase):
    def test_tmux_formats_and_separators_in_a_folder_name_are_neutralised(self) -> None:
        d = Path(tempfile.mkdtemp(prefix="odq", dir="/tmp"))
        self.addCleanup(shutil.rmtree, d, True)
        from test_start import FakeCli, HostSequence

        cli_ = FakeCli(d, "tmux", {"new-window": "%7", "new-session": "%7"}, fail=[])
        tmux = start.Tmux(lambda: str(cli_.path), "omp", Platform(), {}, wait_s=1, sleep=lambda s: None)
        folder = d / "a#{session_name}b"
        folder.mkdir()
        tmux.start(str(folder), HostSequence(0, str(folder)))
        argv = next(c for c in cli_.calls() if c[0] == "new-window")
        self.assertEqual(argv[argv.index("-n") + 1], "a##{session_name}b")
        self.assertEqual(argv[argv.index("-c") + 1], str(folder).replace("#", "##"))
        for bad in ("semi;", "a;b"):
            p = d / bad
            p.mkdir()
            with self.assertRaises(start.StartError):
                tmux.start(str(p), lambda: [])


@unittest.skipUnless(shutil.which("tmux"), "tmux is not installed here")
class RealTmux(Tmp):
    def test_a_folder_with_a_format_in_its_name_is_the_directory_the_window_opens_in(self) -> None:
        stub = self.root / "stub"
        stub.mkdir()
        (stub / "omp").write_text(f'#!/bin/sh\npwd -P > "{self.root}/cwd"\nexec sleep 30\n')
        os.chmod(stub / "omp", 0o700)
        (self.root / "t").mkdir()
        env = {
            "PATH": f"{stub}:/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin",
            "TMUX_TMPDIR": str(self.root / "t"),
            "SHELL": "/bin/sh",
            "HOME": str(self.root),
        }
        tmux_bin = shutil.which("tmux") or ""
        self.addCleanup(lambda: subprocess.run([tmux_bin, "kill-server"], env=env, capture_output=True))
        folder = self.root / "w#{session_name}x"
        folder.mkdir()
        backend = start.Tmux(lambda: tmux_bin, "odq", Platform(), env, wait_s=1)
        backend.start(str(folder), lambda: [])
        deadline = time.monotonic() + 10
        while not (self.root / "cwd").exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(os.path.realpath((self.root / "cwd").read_text().strip()), os.path.realpath(folder))


# file modes, ACLs and writers -----------------------------------------------------------------------------------------------------------


@unittest.skipUnless(sys.platform == "darwin", "macOS ACLs")
class PairingAcl(Tmp):
    def test_a_write_acl_on_devices_json_means_nobody_is_paired(self) -> None:
        store = PairingStore(self.root / "store", "c")
        session = store.writer.redeem(store.writer.create_token(), "x")
        self.assertTrue(store.paired_device(f"c={session}"))
        subprocess.run(
            ["/bin/chmod", "+a", "group:everyone allow write,append", str(self.root / "store" / "devices.json")],
            check=True,
        )
        fresh = PairingStore(self.root / "store", "c")
        self.assertIsNone(
            fresh.paired_device(f"c={session}"), "another principal could have inserted the hash of a cookie it chose"
        )
        self.assertEqual(fresh.view().refusal, "store-untrusted-path")
        from omp_dashboard.pairing import PairingError

        with self.assertRaises(PairingError):
            PairingWriter(self.root / "store").devices()  # the writer refuses it too, not only the reader


class LeafFiles(Tmp):
    def test_the_walk_covers_the_files_and_not_only_the_directory(self) -> None:
        store = PairingStore(self.root / "store", "c")
        store.writer.redeem(store.writer.create_token(), "x")
        seen: list[tuple[str, bool]] = []
        real = check_path

        def spy(p, secret=False):
            seen.append((Path(p).name, secret))
            return real(p, secret=secret)

        with mock.patch("omp_dashboard.pairing.check_path", spy):
            PairingStore(self.root / "store", "c").view()
            store.writer.devices()
            store.writer.create_token()
        names = {n for n, _ in seen}
        self.assertTrue({"devices.json", "pending.json"} <= names, names)


class WriterParents(Tmp):
    def loose(self) -> Path:
        d = self.root / "loose"
        if not d.exists():
            d.mkdir()
            os.chmod(d, 0o777)
        return d

    def test_nothing_trusted_is_created_below_a_directory_others_can_write(self) -> None:
        under = self.loose() / "state"
        with self.assertRaises(DashboardError):
            util.ensure_private_dir(under / "x")
        self.assertFalse(under.exists())
        with self.assertRaises(DashboardError):
            util.write_file(self.loose() / "f", "x")
        with self.assertRaises(DashboardError):
            PairingWriter(self.loose() / "pairing").create_token()
        self.assertFalse((self.loose() / "pairing").exists())
        with self.assertRaises(DashboardError):
            alerts.write_topic(self.loose() / "secrets" / "ntfy-topic", "a-long-enough-topic-1234")
        self.assertFalse((self.loose() / "secrets").exists())

    def test_the_command_line_refuses_the_same_way(self) -> None:
        s = Scratch()
        self.addCleanup(s.close)
        os.chmod(s.home / ".config" if (s.home / ".config").exists() else s.home, 0o700)
        loose = self.loose()
        r = cli(s, "config", "init", env={"XDG_CONFIG_HOME": str(loose / "cfg")})
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((loose / "cfg").exists())


class ParentDirectories(Tmp):
    def test_directories_created_above_a_private_directory_are_private_whatever_the_umask(self) -> None:
        old = os.umask(0o002)
        try:
            util.ensure_private_dir(self.root / "a" / "b" / "c")
        finally:
            os.umask(old)
        for part in ("a", "a/b", "a/b/c"):
            self.assertEqual(stat.S_IMODE(os.stat(self.root / part).st_mode), 0o700, part)


class AuditFifo(Tmp):
    def test_a_fifo_at_the_audit_path_is_refused_promptly_and_does_not_block_the_next_call(self) -> None:
        p = self.root / "state" / "audit.jsonl"
        p.parent.mkdir(mode=0o700)
        os.mkfifo(p, 0o600)
        started = time.monotonic()
        with self.assertRaises((DashboardError, OSError)):
            audit.audit(p, "stop", None, "x", None)
        self.assertLess(time.monotonic() - started, 3)
        other = self.root / "state" / "other.jsonl"
        started = time.monotonic()
        audit.audit(other, "stop", None, "x", None)
        self.assertLess(time.monotonic() - started, 3)

    def test_an_existing_file_that_is_not_ours_is_not_rotated_away(self) -> None:
        p = self.root / "a.jsonl"
        p.write_text("x" * 5000)
        os.chmod(p, 0o600)
        with (
            mock.patch.object(os, "geteuid", return_value=os.geteuid() + 1),
            self.assertRaises((DashboardError, OSError)),
        ):
            audit.audit(p, "stop", None, "x", None, max_bytes=100)
        self.assertFalse(p.with_name("a.jsonl.1").exists())


# force kill -----------------------------------------------------------------------------------------------------------------------


class FakePlatform:
    kind = "linux"

    def __init__(self) -> None:
        self.infos: dict[int, ProcessInfo | None] = {}
        self.tree: list[tuple[int, float]] = []

    def process_info(self, pid: int):
        return self.infos.get(pid)

    def descendants_info(self, pid: int, uid: int, root_start: float | None = None):
        return list(self.tree)

    def descendants(self, pid: int, uid: int):
        return [p for p, _ in self.tree]


class ForceKillTree(unittest.TestCase):
    def setUp(self) -> None:
        self.host = host_entry(1, pid=4242, startedAt=1_000_000_000_000)
        self.plat = FakePlatform()
        uid = os.getuid()
        self.plat.infos[4242] = ProcessInfo(uid, 1_000_000_000.5, "/usr/local/bin/omp")
        self.sent: list[tuple[int, int]] = []
        self.now = [0.0]

        def send(pid: int, sig: int) -> None:
            self.sent.append((pid, sig))
            if pid == 4242:
                self.plat.infos[4242] = None
            else:
                self.plat.infos[pid] = None if pid not in self.survivors else self.plat.infos.get(pid)

        self.survivors: set[int] = set()
        self.killer = Killer(
            lambda: [self.host],
            self.plat,
            send=send,
            sleep=lambda s: self.now.__setitem__(0, self.now[0] + s),
            clock=lambda: self.now[0],
            uid=uid,
        )  # type: ignore[arg-type]
        self.sid = self.host["sessionId"]
        # a failed Kill first
        self.killer.failed_term[self.sid] = (4242, 1_000_000_000.5, 0.0)
        self.uid = uid

    def test_a_descendant_whose_pid_now_belongs_to_another_process_is_not_signalled(self) -> None:
        self.plat.tree = [(5001, 1000.0), (5002, 1001.0)]
        self.plat.infos[5001] = ProcessInfo(self.uid, 1000.0, "tool")  # still the same process
        self.plat.infos[5002] = ProcessInfo(self.uid, 9999.0, "newcomer")  # the pid was reused after the snapshot
        result = self.killer.force(self.sid, "session 1", wait_s=1)
        signalled = {p for p, _ in self.sent}
        self.assertIn(5001, signalled)
        self.assertNotIn(5002, signalled)
        self.assertEqual(result["descendants"], 1)

    def test_a_descendant_that_is_still_running_is_reported_not_hidden(self) -> None:
        self.plat.tree = [(5001, 1000.0)]
        self.plat.infos[5001] = ProcessInfo(self.uid, 1000.0, "tool")
        self.survivors = {5001}
        result = self.killer.force(self.sid, "session 1", wait_s=1)
        self.assertEqual((result["outcome"], result["remaining"]), ("still_running", 1))

    def test_everything_gone_is_exited(self) -> None:
        self.plat.tree = [(5001, 1000.0)]
        self.plat.infos[5001] = ProcessInfo(self.uid, 1000.0, "tool")
        result = self.killer.force(self.sid, "session 1", wait_s=1)
        self.assertEqual((result["outcome"], result["remaining"]), ("exited", 0))


class ForceKillRealTable(unittest.TestCase):
    def test_the_platform_reports_start_times_with_the_children(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            time.sleep(0.3)
            rows = Platform().descendants_info(os.getpid(), os.getuid())
            mine = [r for r in rows if r[0] == child.pid]
            self.assertEqual(len(mine), 1)
            self.assertLess(abs(mine[0][1] - time.time()), 5)
        finally:
            child.kill()
            child.wait()


# link order -----------------------------------------------------------------------------------------------------------------------


class LinkOrder(ScratchTestCase):
    def test_the_relay_is_proven_after_the_link_is_fetched_and_just_before_it_is_returned(self) -> None:
        s = self.make([host_entry(1)])
        hub = self.hub(
            s, {"relay": {"enabled": True, "hostPort": 17997, "guestPort": 17996, "webUrl": "http://127.0.0.1:17996"}}
        )
        hub.pair()
        order: list[str] = []
        real_link = hub.app.omp.link
        hub.app.omp.link = lambda *a, **k: (order.append("link"), real_link(*a, **k))[1]  # type: ignore[method-assign]
        s.fake_state["links"] = {
            "inst0001": {
                "control": "http://127.0.0.1:17996/#ws://127.0.0.1:17997/r/room.key",
                "view": "http://127.0.0.1:17996/#ws://127.0.0.1:17997/r/room.key",
            }
        }
        s.save_fake()
        import omp_dashboard.server as server

        with mock.patch.object(server.relay, "check_ports", side_effect=lambda *a, **k: order.append("proof")):
            r = hub.client.post("/api/hub/link", {"instanceId": "inst0001", "view": False})
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual(order[-2:], ["link", "proof"], order)


# probe deadlines ------------------------------------------------------------------------------------------------------------------


class Trickle:
    """A server that answers one byte at a time, forever."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.stop = False
        self.conns: list[socket.socket] = []
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
            for byte in b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n" + b"x" * 100000:
                conn.sendall(bytes([byte]))
                time.sleep(0.2)
        except OSError:
            pass

    def close(self) -> None:
        self.stop = True
        self.sock.close()
        for conn in self.conns:
            conn.close()


class ProbeDeadlines(unittest.TestCase):
    def test_a_relay_that_drips_bytes_is_cut_at_the_overall_deadline(self) -> None:
        from omp_dashboard import relay

        t = Trickle()
        self.addCleanup(t.close)
        started = time.monotonic()
        with self.assertRaises(relay.RelayCheckError):
            relay.prove_listener(t.port, b"\x00" * 32, timeout=1.0)
        self.assertLess(time.monotonic() - started, 3.5)

    def test_an_ntfy_server_that_drips_bytes_cannot_hold_the_watcher(self) -> None:
        t = Trickle()
        self.addCleanup(t.close)
        started = time.monotonic()
        with self.assertRaises(OSError):
            alerts.ntfy_send(f"http://127.0.0.1:{t.port}", "a-long-enough-topic-1234", "T", "B", timeout=1.0)
        self.assertLess(time.monotonic() - started, 3.5)

    def test_the_doctor_probe_has_an_overall_deadline_too(self) -> None:
        from omp_dashboard import doctor

        t = Trickle()
        self.addCleanup(t.close)
        started = time.monotonic()
        self.assertIsNotNone(doctor._probe_hub("127.0.0.1", t.port))
        self.assertLess(time.monotonic() - started, 5)


# dot-dot paths --------------------------------------------------------------------------------------------------------------------


class DotDotPaths(ScratchTestCase):
    def test_a_path_setting_with_dot_dot_is_refused_not_silently_collapsed(self) -> None:
        s = self.make()
        for key, value in (
            ("pairing", {"dir": "/safe/link/../pairing"}),
            ("control", {"dir": "/a/../b"}),
            ("audit", {"file": "/a/b/../c.jsonl"}),
            ("relay", {"checkDir": "/a/../b"}),
        ):
            s.write_config({key: value})
            self.assertTrue(any("'..'" in p or ".." in p for p in s.config().problems), (key, s.config().problems))

    def test_the_environment_variables_too(self) -> None:
        s = self.make()
        e = {**s.env, "OMP_DASHBOARD_PAIRING_DIR": "/a/../b"}
        self.assertTrue(load_config(from_env(e), e).problems)
        with self.assertRaises(DashboardError):
            from_env({**s.env, "XDG_CONFIG_HOME": "/a/../b"})
        with self.assertRaises(DashboardError):
            from_env({**s.env, "OMP_DASHBOARD_CONFIG": "/a/../b.json"})


# project enumeration --------------------------------------------------------------------------------------------------------------


class ProjectEnumeration(Tmp):
    def test_one_huge_directory_is_not_enumerated_without_bound(self) -> None:
        from omp_dashboard import projects

        big = self.root / "home" / "big"
        big.mkdir(parents=True)
        for i in range(300):
            (big / f"d{i:04d}").mkdir()
        seen = [0]
        real = os.scandir

        class Counting:
            def __init__(self, it):
                self.it = it

            def __enter__(self):
                self.it.__enter__()
                return self

            def __exit__(self, *a):
                return self.it.__exit__(*a)

            def __iter__(self):
                return self

            def __next__(self):
                seen[0] += 1
                return next(self.it)

        with (
            mock.patch.object(projects, "MAX_VISITED", 50),
            mock.patch.object(projects.os, "scandir", lambda p: Counting(real(p))),
        ):
            projects.discover_projects(["~/big"], self.root / "home")
        self.assertLessEqual(seen[0], 60)

    def test_ranking_looks_the_found_projects_up_and_never_lists_the_session_directory(self) -> None:
        from omp_dashboard import projects

        sessions = self.root / "sessions"
        sessions.mkdir()
        for i in range(120):
            (sessions / f"-x{i}").mkdir()
        (self.root / "home" / "p" / ".git").mkdir(parents=True)
        (sessions / "-p").mkdir()
        listed: list[str] = []
        real = os.scandir

        def spy(path):
            listed.append(str(path))
            return real(path)

        with mock.patch.object(projects.os, "scandir", spy):
            found = projects.discover_projects(["~/p"], self.root / "home", sessions_dir=sessions)
        self.assertNotIn(str(sessions), listed)
        self.assertIsNotNone(found[0]["last_used"], "the session folder of the project is located by name")


# environment ----------------------------------------------------------------------------------------------------------------------


class DropinEnvironment(ScratchTestCase):
    def test_dropins_write_refuses_while_a_fallback_variable_decides_something_the_service_would_not_see(self) -> None:
        s = self.make()
        env = {k: v for k, v in s.env.items() if k != "OMP_DASHBOARD_PORT"}
        for name, value in (
            ("OMP_BIN", "/x/omp"),
            ("HERDR_BIN", "/x/herdr"),
            ("OMP_SESSION_CONTROL_DIR", "/x/ctl"),
            ("PI_CODING_AGENT_DIR", "/x/agent"),
        ):
            r = subprocess.run(
                [sys.executable, str(MAIN), "dropins", "--write"],
                env={**env, name: value, "PYTHONDONTWRITEBYTECODE": "1"},
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(r.returncode, 1, name)
            self.assertIn(name, r.stderr)
        self.assertFalse(s.paths().services_dropin.exists())

    def test_a_configured_value_wins_over_the_fallback_so_the_variable_does_not_matter(self) -> None:
        s = self.make()
        s.write_config({"omp": {"bin": s.env["OMP_BIN"]}, "listen": {"port": s.port}})
        env = {k: v for k, v in s.env.items() if k != "OMP_DASHBOARD_PORT"}
        r = subprocess.run(
            [sys.executable, str(MAIN), "dropins", "--write"],
            env={**env, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(r.returncode, 0, r.stderr)


# stats proxy removal --------------------------------------------------------------------------------------------------------------


class StatsProxyIsGone(ScratchTestCase):
    def test_no_proxy_code_flag_schema_key_or_route_remains(self) -> None:
        self.assertFalse((PLUGIN / "python/omp_dashboard/statsproxy.py").exists())
        for path in list((PLUGIN / "python").rglob("*.py")) + list((PLUGIN / "python").rglob("*.json")):
            text = path.read_text()
            for word in ("statsproxy", "stats.proxy", "StatsProxy", "stats-proxy", "X-Omp-" + "Hub-Auth"):
                self.assertNotIn(word, text, f"{word} in {path.name}")
        s = self.make()
        s.write_config({"stats": {"proxy": {"enabled": True}}})
        self.assertTrue(s.config().problems, "the key is unknown now")
        hub = self.hub(self.make())
        hub.pair()
        self.assertEqual(hub.client.get("/api/stats/overview").status, 404)
        self.assertEqual(hub.client.post("/api/stats/close", {}).status, 404)

    def test_the_stats_link_out_stays(self) -> None:
        s = self.make()
        s.write_config({"stats": {"url": "http://127.0.0.1:3847/"}})
        hub = self.hub(s)
        hub.pair()
        self.assertEqual(hub.client.get("/api/hub/about").json()["statsUrl"], "http://127.0.0.1:3847/")


# relay classifier caches -------------------------------------------------------------------------------------------------------------


class RelayClassifierFailures(unittest.TestCase):
    def test_a_failing_link_lookup_is_not_repeated_on_every_poll(self) -> None:
        from omp_dashboard.omp import OmpError, RelayClassifier

        calls = []

        class FailingOmp:
            def link(self, *a, **k):
                calls.append(1)
                raise OmpError("down")

        c = RelayClassifier(FailingOmp())  # type: ignore[arg-type]
        hosts = [{"instanceId": "inst0001", "generation": 1}]
        for _ in range(5):
            c.annotate([dict(h) for h in hosts], "http://127.0.0.1:1", "ws://127.0.0.1:2")
        self.assertEqual(len(calls), 1)


class Docs(unittest.TestCase):
    def test_the_documents_do_not_claim_what_the_evidence_does_not_show(self) -> None:
        texts = {
            p.name: p.read_text()
            for p in [PLUGIN / "README.md", PLUGIN / "docs/threat-model.md", PLUGIN / "docs/release-evidence.md"]
        }
        readme = texts["README.md"]
        self.assertNotIn(
            "herdr 0.9.5 and tmux 3.x were checked; herdr on Linux was checked in a container only", readme
        )
        self.assertNotIn("Tailscale Serve's header behaviour is verified on macOS only", readme)
        self.assertNotIn("(verified on macOS only)", texts["threat-model.md"])
        self.assertIn("Out of scope", texts["threat-model.md"])


if __name__ == "__main__":
    unittest.main()
