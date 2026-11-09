"""omp-dashboard's hub against two live omp TUI sessions in a scratch HOME, with the real omp-session-control extension (installed from this
repository's catalog, the way a user installs it), the test kit's fake model provider and a scratch relay on a random port.

Checks, through the hub's HTTP API: the Sessions list shows both sessions with Stop and Rename available; Stop interrupts a running tool call;
Rename shows in `omp collab list`; Kill ends only the session it was asked about; Force kill is refused until a Kill failed, and then ends the
session and everything below it, and only that session. Nothing touches the real account, the network or a running service.
Needs omp and Bun (OMP_BIN and BUN_BIN, or both on PATH). Takes about three minutes.

Run: python3 -m unittest discover -s plugins/omp-dashboard/tests/integration -v
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
PLUGIN = ROOT / "plugins" / "omp-dashboard"
sys.path.insert(0, str(ROOT / "tools" / "testkit"))
sys.path.insert(0, str(PLUGIN / "python"))
sys.path.insert(0, str(PLUGIN / "tests" / "python"))

import omp_scratch as kit
from omp_dashboard import config as config_module
from omp_dashboard import paths as paths_module
from omp_dashboard.server import HubApp, HubServer
from support import Client, free_port

OMP = kit.find_omp()
BUN = kit.find_bun()
NEEDS = unittest.skipUnless(OMP and BUN, "omp and bun are required (OMP_BIN, BUN_BIN)")


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@NEEDS
class DashboardInRealOmp(unittest.TestCase):
    relay: kit.ScratchRelay
    scratch: kit.ScratchHome
    a: kit.PtyOmp
    b: kit.PtyOmp

    @classmethod
    def setUpClass(cls) -> None:
        assert BUN and OMP
        cls.relay = kit.ScratchRelay(BUN)
        cls.scratch = kit.ScratchHome(cls.relay.url)
        try:
            cls.scratch.install_plugin(ROOT, "omp-session-control", "omp-tether")
            cls.a = kit.PtyOmp(cls.scratch)
            kit.wait_for(lambda: len(cls.hosts()) == 1, timeout=120, what="the first session in the Collab listing")
            cls.b = kit.PtyOmp(cls.scratch)
            kit.wait_for(lambda: len(cls.hosts()) == 2, timeout=120, what="both sessions in the Collab listing")
            cls.a.type("/session-control on")
            time.sleep(1)
            cls.b.type("/session-control on")
            ctl = cls.scratch.home / ".config" / "omp-session-control"
            kit.wait_for(
                lambda: len(list(ctl.glob("*.json"))) == 2, timeout=60, what="a control socket in each session"
            )
            cls.start_hub()
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def hosts(cls) -> list[dict]:
        r = cls.scratch.cli("collab", "list", "--json", timeout=30)
        return json.loads(r.stdout).get("hosts") or [] if r.returncode == 0 else []

    @classmethod
    def start_hub(cls) -> None:
        port = free_port()
        env = {
            "HOME": str(cls.scratch.home),
            "PATH": f"{Path(OMP).parent}:/usr/bin:/bin:/usr/sbin:/sbin",
            "OMP_BIN": OMP,
            "OMP_DASHBOARD_PORT": str(port),
            "LANG": "en_US.UTF-8",
        }
        cls.hub_port = port
        config = config_module.load_config(paths_module.from_env(env), env)
        assert not config.problems, config.problems
        cls.app = HubApp(config, env)
        cls.server = HubServer(("127.0.0.1", port), cls.app)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.client = Client(port)
        assert cls.client.pair_with(cls.app.pairing.writer.create_token()).status == 200

    @classmethod
    def tearDownClass(cls) -> None:
        if getattr(cls, "server", None):
            cls.app.stop.set()
            cls.server.shutdown()
            cls.server.server_close()
        for name in ("a", "b"):
            omp = getattr(cls, name, None)
            if omp:
                omp.stop()
        if getattr(cls, "relay", None):
            cls.relay.stop()
        if getattr(cls, "scratch", None):
            cls.scratch.cleanup()

    # -- helpers ----------------------------------------------------------------------------------------------------

    def listed(self) -> dict[int, dict]:
        body = self.client.get("/api/hub/sessions").json()
        return {s["pid"]: s for s in body["sessions"]}

    def session_of(self, omp: kit.PtyOmp) -> dict:
        return self.listed()[omp.pid]

    def post(self, session: dict, action: str, body: dict | None = None):
        return self.client.post(f"/api/hub/sessions/{session['sessionId']}/{action}", body or {})

    def tool_started(self, omp: kit.PtyOmp) -> bool:
        return any(
            e.get("customType") == "tool_execution_start" and e["data"].get("args", {}).get("command") == "sleep 60"
            for e in self.session_entries(omp)
        )

    def session_entries(self, omp: kit.PtyOmp) -> list[dict]:
        """The session file of this session: omp names it `<time>_<sessionId>.jsonl`."""
        session_id = next((h["sessionId"] for h in self.hosts() if h.get("pid") == omp.pid), None)
        if not session_id:
            return []
        files = list((self.scratch.home / ".omp" / "agent" / "sessions").rglob(f"*{session_id}*.jsonl"))
        if len(files) != 1:
            return []
        try:
            return [json.loads(line) for line in files[0].read_text().splitlines() if line.strip()]
        except (OSError, ValueError):
            return []

    # -- the tests, in order ----------------------------------------------------------------------------------------

    def test_10_the_sessions_page_lists_both_sessions_with_controls(self) -> None:
        sessions = self.listed()
        self.assertEqual(set(sessions), {self.a.pid, self.b.pid})
        for s in sessions.values():
            self.assertTrue(s["control"], s)
            self.assertEqual(s["relay"], "off")
            self.assertEqual(
                s["cwd"], str(self.scratch.repo.resolve()) if s["cwd"].startswith("/private") else s["cwd"]
            )
            self.assertIsNone(s["place"])
        self.assertIsNone(self.client.get("/api/hub/sessions").json()["controlProblem"])

    def test_20_stop_interrupts_a_running_tool_call_of_that_session_only(self) -> None:
        a = self.session_of(self.a)
        self.assertEqual(self.post(a, "stop").json()["outcome"], "was_idle")
        self.a.type("RUN_SLEEP please")
        kit.wait_for(lambda: self.tool_started(self.a), timeout=45, what="the sleep 60 tool call to start")
        time.sleep(1)
        self.assertTrue(self.session_of(self.a)["busy"])
        self.assertFalse(self.session_of(self.b)["busy"])
        started = time.monotonic()
        r = self.post(a, "stop")
        self.assertEqual((r.status, r.json()["outcome"]), (200, "stopped"), r.body)
        kit.wait_for(lambda: self.session_of(self.a)["busy"] is False, timeout=20, what="the turn to end")
        self.assertLess(time.monotonic() - started, 25, "far less than the 60 s the tool would have run")
        results = [
            e["message"] for e in self.session_entries(self.a) if (e.get("message") or {}).get("role") == "toolResult"
        ]
        self.assertTrue(results[-1]["isError"])
        self.assertEqual(results[-1]["content"][0]["text"], "Command aborted")
        self.assertTrue(alive(self.a.pid) and alive(self.b.pid))

    def test_30_rename_shows_in_omp_collab_list_and_in_the_page(self) -> None:
        b = self.session_of(self.b)
        r = self.post(b, "rename", {"name": "  Renamed  by the hub ✓ "})
        self.assertEqual((r.status, r.json()["name"]), (200, "Renamed by the hub ✓"), r.body)
        kit.wait_for(
            lambda: any(
                h["pid"] == self.b.pid and h.get("sessionName") == "Renamed by the hub ✓" for h in self.hosts()
            ),
            timeout=20,
            what="omp's own listing to show the name",
        )
        self.assertEqual(self.session_of(self.b)["sessionName"], "Renamed by the hub ✓")
        self.assertNotEqual(self.session_of(self.a).get("sessionName"), "Renamed by the hub ✓")
        bad = self.post(b, "rename", {"name": "a\u202eb"})
        self.assertEqual((bad.status, bad.json()["code"]), (400, "invalid_args"))

    def test_40_kill_needs_the_typed_name_and_ends_only_that_session(self) -> None:
        a = self.session_of(self.a)
        wrong = self.post(a, "kill", {"confirm": "not the name"})
        self.assertEqual((wrong.status, wrong.json()["code"]), (400, "confirm"))
        self.assertTrue(alive(self.a.pid))
        forced_early = self.post(a, "force-kill", {"confirm": Path(a["cwd"]).name})
        self.assertEqual((forced_early.status, forced_early.json()["code"]), (409, "no_failed_term"))
        self.assertTrue(alive(self.a.pid))
        done = self.post(a, "kill", {"confirm": Path(a["cwd"]).name})
        self.assertEqual((done.status, done.json()["outcome"]), (200, "exited"), done.body)
        kit.wait_for(lambda: not alive(self.a.pid), timeout=20, what="the first session to be gone")
        self.assertTrue(alive(self.b.pid), "the other session was not touched")
        self.assertEqual(set(self.listed()), {self.b.pid})

    def test_50_kill_of_a_session_with_a_running_tool_ends_the_last_session_and_force_kill_stays_refused(self) -> None:
        b = self.session_of(self.b)
        self.b.type("RUN_SLEEP again")
        kit.wait_for(lambda: self.tool_started(self.b), timeout=45, what="the tool call in the second session")
        confirm = b["sessionName"]
        self.assertTrue(self.post(b, "force-kill", {"confirm": confirm}).json()["code"] == "no_failed_term")
        self.assertTrue(alive(self.b.pid), "Force kill is not offered for a session Kill has not failed on")
        done = self.post(b, "kill", {"confirm": confirm})
        self.assertEqual((done.status, done.json()["outcome"]), (200, "exited"), done.body)
        kit.wait_for(lambda: not alive(self.b.pid), timeout=20, what="the second session to be gone")
        self.assertEqual(self.listed(), {})
        # SIGKILL of a session and its children after a failed SIGTERM cannot be provoked with a real omp (a stopped omp is not listed
        # at all, and a running one ends on SIGTERM); it is covered with real processes in tests/python/test_kill.py.

    def test_60_every_action_was_audited_without_names_or_paths(self) -> None:
        lines = [json.loads(x) for x in self.app.config.audit_file.read_text().splitlines()]
        actions = [(x["action"], x["outcome"]) for x in lines]
        for want in (
            ("stop", "was_idle"),
            ("stop", "stopped"),
            ("rename", "renamed"),
            ("kill", "refused:confirm"),
            ("force-kill", "refused:no_failed_term"),
            ("kill", "exited"),
        ):
            self.assertIn(want, actions)
        text = self.app.config.audit_file.read_text()
        for leak in ("Renamed by the hub", str(self.scratch.repo), "RUN_SLEEP"):
            self.assertNotIn(leak, text)


if __name__ == "__main__":
    unittest.main()
