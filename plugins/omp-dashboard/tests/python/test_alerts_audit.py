"""Phone alerts (off by default, the topic a secret) and the audit log (no secrets, bounded, private)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from omp_dashboard import alerts
from omp_dashboard.alerts import (
    Alerts,
    AlertTracker,
    alert_message,
    ntfy_payload,
    ntfy_send,
    ntfy_send_retrying,
    read_topic,
    write_topic,
)
from omp_dashboard.audit import audit
from omp_dashboard.util import DashboardError
from support import PLUGIN, Scratch, ScratchTestCase, free_port, host_entry  # noqa: F401

TOPIC = "omp-supersecret-topic-1234"


class FakeNtfy:
    def __init__(self, behaviour: str = "ok") -> None:
        self.port = free_port()
        self.posts: list[dict] = []
        self.redirected_to_hits = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: object) -> None:
                pass

            def do_GET(self) -> None:
                if self.path == "/followed":
                    outer.redirected_to_hits += 1
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.path == "/followed":
                    outer.redirected_to_hits += 1
                if behaviour == "redirect":
                    self.send_response(303)
                    self.send_header("Location", f"http://127.0.0.1:{outer.port}/followed")
                    self.end_headers()
                    return
                outer.posts.append({"path": self.path, "headers": dict(self.headers), "body": json.loads(body)})
                self.send_response(500 if behaviour == "error" else 200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class Topic(unittest.TestCase):
    def setUp(self) -> None:
        self.s = Scratch()
        self.addCleanup(self.s.close)
        self.file = self.s.home / "secrets" / "ntfy-topic"

    def test_no_file_means_off_and_a_written_topic_is_private(self) -> None:
        self.assertIsNone(read_topic(self.file))
        write_topic(self.file, TOPIC)
        self.assertEqual(read_topic(self.file), TOPIC)
        self.assertEqual(stat.S_IMODE(os.stat(self.file).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.file.parent).st_mode), 0o700)

    def test_an_unsafe_or_malformed_topic_file_is_an_error_never_silently_off(self) -> None:
        write_topic(self.file, TOPIC)
        os.chmod(self.file, 0o644)
        with self.assertRaises(DashboardError):
            read_topic(self.file)
        os.chmod(self.file, 0o600)
        for bad in ("", "a b", "x" * 65, "abc", "t/../x", "tòpic1234"):
            self.file.write_text(bad)
            with self.assertRaises(DashboardError, msg=bad):
                read_topic(self.file)
        with self.assertRaises(DashboardError):
            write_topic(self.file, "no good!")

    def test_a_directory_open_to_others_is_refused_for_writing_a_topic(self) -> None:
        self.file.parent.mkdir(mode=0o755)
        os.chmod(self.file.parent, 0o755)
        with self.assertRaisesRegex(DashboardError, "mode 700"):
            write_topic(self.file, TOPIC)


class Tracker(unittest.TestCase):
    def test_a_long_turn_that_finishes_and_a_question_alert_but_short_turns_and_first_sights_do_not(self) -> None:
        t = AlertTracker(60_000)
        h = host_entry(1, busy=False)
        self.assertEqual(t.observe([h], 0), [], "first sight")
        h["busy"] = True
        self.assertEqual(t.observe([h], 1_000), [])
        h["busy"] = False
        self.assertEqual(t.observe([h], 5_000), [], "a short turn")
        h["busy"] = True
        t.observe([h], 10_000)
        h["busy"] = False
        self.assertEqual([k for k, _ in t.observe([h], 100_000)], ["finished"])
        h["inputRequired"] = True
        self.assertEqual([k for k, _ in t.observe([h], 101_000)], ["needs_input"])
        self.assertEqual(t.observe([h], 102_000), [], "the same question twice is one alert")

    def test_gone_sessions_are_forgotten(self) -> None:
        t = AlertTracker()
        t.observe([host_entry(1)], 0)
        t.observe([], 1)
        self.assertEqual(t.state, {})

    def test_messages_hold_the_folder_the_model_and_a_state_and_nothing_else(self) -> None:
        h = host_entry(1, cwd="/home/someone/private-client/api", sessionName="secret prompt title", inputRequired=True)
        h["url"] = "https://relay.example/#room.key"
        for kind in ("needs_input", "finished"):
            title, body, priority = alert_message(kind, h)
            text = f"{title} {body}"
            self.assertIn("api", text)
            self.assertIn("tether-lite", text)
            for leak in ("secret prompt title", "/home/someone", "private-client", "relay.example", "room.key"):
                self.assertNotIn(leak, text)
            self.assertIn(priority, (3, 4))
        self.assertEqual(alert_message("finished", {})[0], "omp session finished")

    def test_the_payload_has_no_robot_tag_and_a_click_only_when_given(self) -> None:
        p = ntfy_payload("t", "T", "B")
        self.assertNotIn("click", p)
        self.assertEqual(ntfy_payload("t", "T", "B", click="https://x.example/")["click"], "https://x.example/")


class Sending(unittest.TestCase):
    def setUp(self) -> None:
        self.ntfy = FakeNtfy()
        self.addCleanup(self.ntfy.close)

    def test_a_message_is_a_json_post_to_the_server_root(self) -> None:
        self.assertEqual(ntfy_send(self.ntfy.url, TOPIC, "Title é", "Body", 4), 200)
        post = self.ntfy.posts[0]
        self.assertEqual(
            (post["path"], post["body"]["topic"], post["body"]["title"], post["body"]["priority"]),
            ("/", TOPIC, "Title é", 4),
        )
        self.assertEqual(post["headers"]["Content-Type"], "application/json")

    def test_a_redirect_is_an_error_and_the_message_is_not_sent_anywhere_else(self) -> None:
        redirect = FakeNtfy("redirect")
        self.addCleanup(redirect.close)
        with self.assertRaises(OSError):
            ntfy_send(redirect.url, TOPIC, "T", "B")
        self.assertEqual(redirect.redirected_to_hits, 0)

    def test_retries_back_off_then_give_up(self) -> None:
        calls, sleeps = [], []

        def failing(*a, **k):
            calls.append(1)
            raise OSError("reset")

        with self.assertRaises(OSError):
            ntfy_send_retrying("s", "t", "T", "B", send=failing, sleep=sleeps.append)
        self.assertEqual((len(calls), sleeps), (3, [2, 5]))
        results = iter([OSError("x"), 200])

        def flaky(*a, **k):
            r = next(results)
            if isinstance(r, Exception):
                raise r
            return r

        self.assertEqual(ntfy_send_retrying("s", "t", "T", "B", send=flaky, sleep=lambda s: None), 200)


class Watcher(ScratchTestCase):
    def build(self, **over) -> tuple[Alerts, Path, list]:
        s = self.make([host_entry(1, busy=True)])
        topic_file = s.home / "secrets" / "ntfy-topic"
        asked: list[int] = []
        sent: list[tuple] = []

        def list_hosts():
            asked.append(1)
            return s.hosts

        def send(server, topic, title, body, priority=3, click=None):
            sent.append((server, topic, title, body, priority, click))
            return 200

        a = Alerts("https://ntfy.example.test", topic_file, 60, list_hosts, lambda: over.get("click"), send=send)
        a.asked = asked  # type: ignore[attr-defined]
        return a, topic_file, sent

    def test_off_by_default_and_while_off_omp_is_not_even_asked(self) -> None:
        a, _, sent = self.build()
        for _ in range(3):
            a.poll_once(1_000)
        self.assertEqual((a.asked, sent), ([], []))  # type: ignore[attr-defined]
        self.assertFalse(a.status()["enabled"])
        with self.assertRaisesRegex(DashboardError, "no ntfy topic"):
            a.send_test()

    def test_with_a_topic_a_finished_long_turn_is_sent_and_counted(self) -> None:
        a, topic_file, sent = self.build(click="https://box.example/#/sessions")
        write_topic(topic_file, TOPIC)
        a.poll_once(1_000)
        a.list_hosts()[0]["busy"] = False  # type: ignore[index]
        a.poll_once(120_000)
        self.assertEqual(len(sent), 1)
        self.assertEqual(
            (sent[0][0], sent[0][1], sent[0][5]), ("https://ntfy.example.test", TOPIC, "https://box.example/#/sessions")
        )
        status = a.status()
        self.assertEqual((status["enabled"], status["sent"], status["last_error"]), (True, 1, None))

    def test_the_topic_is_never_in_the_status_the_page_gets_nor_in_anything_printed(self) -> None:
        a, topic_file, sent = self.build()
        write_topic(topic_file, TOPIC)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            a.poll_once(1_000)
            a.list_hosts()[0]["busy"] = False  # type: ignore[index]
            a.poll_once(120_000)
            a.send_test()
            text = json.dumps(a.status())
        self.assertNotIn(TOPIC, text + out.getvalue() + err.getvalue())

    def test_a_failing_server_is_reported_without_the_topic_and_the_watcher_goes_on(self) -> None:
        a, topic_file, _ = self.build()
        write_topic(topic_file, TOPIC)

        def boom(*args, **kwargs):
            raise OSError(f"connection reset talking to {args[1]}")

        a.send = boom
        a.poll_once(1_000)
        a.list_hosts()[0]["busy"] = False  # type: ignore[index]
        a.poll_once(120_000)
        status = a.status()
        self.assertIn("OSError", status["last_error"])
        self.assertNotIn(TOPIC, json.dumps(status))

    def test_an_unsafe_topic_file_is_shown_as_a_problem_and_nothing_is_sent(self) -> None:
        a, topic_file, sent = self.build()
        write_topic(topic_file, TOPIC)
        os.chmod(topic_file, 0o644)
        a.poll_once(1_000)
        status = a.status()
        self.assertFalse(status["enabled"])
        self.assertIn("topic file cannot be used", status["last_error"])
        self.assertNotIn(TOPIC, json.dumps(status))
        self.assertEqual(sent, [])

    def test_a_listing_failure_is_reported_not_raised(self) -> None:
        a, topic_file, _ = self.build()
        write_topic(topic_file, TOPIC)

        def failing():
            raise DashboardError("omp failed")

        a.list_hosts = failing
        a.poll_once(1_000)
        self.assertEqual(a.status()["last_error"], "watching sessions: DashboardError")

    def test_the_server_setting_only_accepts_https_or_loopback_http(self) -> None:
        from omp_dashboard import schema

        sch = schema.load_schema("config-1")
        for url, ok in (
            ("https://ntfy.sh", True),
            ("http://127.0.0.1:8080", True),
            ("http://localhost", True),
            ("http://ntfy.sh", False),
            ("https://ntfy.sh/path", False),
            ("ftp://x", False),
            ("http://127.0.0.1.evil.example", False),
        ):
            problems = schema.validate({"schemaVersion": 1, "alerts": {"server": url}}, sch)
            self.assertEqual(problems == [], ok, url)
        self.assertTrue(alerts.TOPIC_RE)


class AuditLog(unittest.TestCase):
    def setUp(self) -> None:
        self.s = Scratch()
        self.addCleanup(self.s.close)
        self.path = self.s.home / "state" / "audit.jsonl"

    def lines(self) -> list[dict]:
        return [json.loads(line) for line in self.path.read_text().splitlines()]

    def test_a_line_holds_time_browser_action_target_and_outcome_only(self) -> None:
        audit(self.path, "kill", "01a1b65b-93f3", "exited", "ab12cd34", now=1_700_000_000)
        (line,) = self.lines()
        self.assertEqual(set(line), {"time", "browser", "action", "target", "outcome"})
        self.assertEqual(
            (line["time"], line["browser"], line["action"], line["target"], line["outcome"]),
            ("2023-11-14T22:13:20+00:00", "ab12cd34", "kill", "01a1b65b-93f3", "exited"),
        )

    def test_the_directory_and_file_are_private_and_a_loose_file_is_fixed(self) -> None:
        audit(self.path, "stop", None, "stopped", None)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.path.parent).st_mode), 0o700)
        os.chmod(self.path, 0o644)
        audit(self.path, "stop", None, "stopped", None)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertIsNone(self.lines()[0]["browser"])

    def test_hostile_text_cannot_break_a_line_or_grow_one(self) -> None:
        audit(self.path, "ac\ntion", "t\r\narget" + "x" * 500, "out\x1b[31mcome", "br\nowser" + "y" * 100)
        (line,) = self.lines()
        for key in ("action", "target", "outcome", "browser"):
            self.assertNotRegex(line[key] or "", r"[\x00-\x1f]")
        self.assertLessEqual(len(line["target"]), 80)
        self.assertLessEqual(len(line["browser"]), 16)
        self.assertEqual(len(self.path.read_text().splitlines()), 1)

    def test_it_rotates_at_the_bound_and_keeps_one_previous_file(self) -> None:
        for i in range(60):
            audit(self.path, "stop", f"s{i}", "stopped", "ab12cd34", max_bytes=2000)
        self.assertTrue(self.path.with_name("audit.jsonl.1").exists())
        total = self.path.stat().st_size
        self.assertLess(total, 2000 + 300)
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ["audit.jsonl", "audit.jsonl.1"])

    def test_a_planted_link_is_never_followed(self) -> None:
        victim = self.s.root / "victim"
        victim.write_text("keep")
        self.path.parent.mkdir(mode=0o700)
        self.path.symlink_to(victim)
        with self.assertRaises((OSError, DashboardError)):
            audit(self.path, "stop", None, "x", None)
        self.assertEqual(victim.read_text(), "keep")

    def test_concurrent_writers_never_interleave_a_line(self) -> None:
        def run(i: int) -> None:
            for j in range(20):
                audit(self.path, "stop", f"s{i}-{j}", "stopped", "ab12cd34")

        threads = [threading.Thread(target=run, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(self.lines()), 120)

    def test_an_existing_configured_directory_is_never_chmoded(self) -> None:
        d = self.s.root / "shared"
        d.mkdir()
        os.chmod(d, 0o755)
        audit(d / "a.jsonl", "stop", None, "x", None)
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o755)


if __name__ == "__main__":
    unittest.main()
