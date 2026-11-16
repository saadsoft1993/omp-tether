"""omp-session-control in a real omp TUI: scratch HOME, fake model provider, scratch relay on a random port.

The plugin is installed the way a user installs it: from the repository's own marketplace catalog, with
`omp plugin marketplace add <repo>` and `omp plugin install`. Nothing touches the real account, the network or a
running service. Needs omp and Bun (OMP_BIN and BUN_BIN, or both on PATH); skipped otherwise. Takes about two minutes.

Run: python3 -m unittest discover -s plugins/omp-session-control/tests/integration -v
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
PLUGIN = ROOT / "plugins" / "omp-session-control"
sys.path.insert(0, str(ROOT / "tools" / "testkit"))

import omp_scratch as kit

OMP = kit.find_omp()
BUN = kit.find_bun()
IDLE_MS = 3000
SECOND_IDLE_MS = 5000
SUITE = PLUGIN / "tests" / "conformance" / "suite.ts"
NEEDS = unittest.skipUnless(OMP and BUN, "omp and bun are required (OMP_BIN, BUN_BIN)")


def control_dir(scratch: kit.ScratchHome) -> Path:
    return scratch.home / ".config" / "omp-session-control"


def registries(directory: Path, pid: int | None = None) -> list[dict]:
    out = []
    for f in directory.glob("*.json") if directory.exists() else []:
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if pid is None or data.get("pid") == pid:
            out.append(data)
    return out


def request(reg: dict, frame: dict | str) -> dict:
    """One request on a fresh connection, one reply."""
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(10)
        s.connect(reg["socket"])
        s.sendall((frame if isinstance(frame, str) else json.dumps(frame)).encode() + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf.split(b"\n")[0])


def conformance(directory: Path, pid: int, *flags: str) -> subprocess.CompletedProcess:
    assert BUN
    return subprocess.run(
        [BUN, "--no-install", str(SUITE), "--dir", str(directory), "--pid", str(pid), *flags],
        capture_output=True,
        text=True,
        cwd=PLUGIN,
        timeout=180,
    )


@NEEDS
class SessionControlInRealOmp(unittest.TestCase):
    relay: kit.ScratchRelay
    scratch: kit.ScratchHome
    omp: kit.PtyOmp
    install_log: str

    @classmethod
    def setUpClass(cls) -> None:
        assert BUN
        cls.relay = kit.ScratchRelay(BUN)
        cls.scratch = kit.ScratchHome(cls.relay.url)
        try:
            cls.install_log = cls.scratch.install_plugin(ROOT, "omp-session-control", "omp-tether")
            # The idle timeout comes from omp's plugin settings (no environment variable): the suite's idle check
            # below only passes when the extension read the stored value, because the default is 60 s.
            cls.scratch.cli("plugin", "config", "set", "omp-session-control", "idleTimeoutMs", str(IDLE_MS))
            cls.omp = kit.PtyOmp(cls.scratch)
            # A hosted session shows up in `omp collab list` once the (scratch) relay answered: the TUI is up.
            kit.wait_for(cls.omp.collab_hosts, timeout=90, what="the session in the Collab listing")
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        if getattr(cls, "omp", None):
            cls.omp.stop()
        if getattr(cls, "relay", None):
            cls.relay.stop()
        if getattr(cls, "scratch", None):
            cls.scratch.cleanup()

    # -- helpers ----------------------------------------------------------------------------------------------------

    @property
    def ctl_dir(self) -> Path:
        return control_dir(self.scratch)

    def reg(self) -> dict | None:
        regs = registries(self.ctl_dir, self.omp.pid)
        return regs[0] if len(regs) == 1 else None

    def raw_op(self, op: str, args: dict | None = None) -> dict:
        reg = self.reg()
        assert reg, "no registry"
        return request(
            reg,
            {
                "v": 1,
                "id": "t",
                "nonce": reg["nonce"],
                "sessionId": reg["sessionId"],
                "generation": reg["generation"],
                "op": op,
                **({"args": args} if args is not None else {}),
            },
        )

    def op(self, op: str, args: dict | None = None) -> dict:
        reply = self.raw_op(op, args)
        self.assertTrue(reply["ok"], reply)
        return reply["result"]

    def read_plan(self) -> dict:
        """The open review's metadata and its whole plan text, read through the cursor."""
        offset, text, meta = 0, "", None
        for _ in range(50):
            part = self.op("plan-review", {"offset": offset})
            if not part["open"]:
                return part
            meta = meta or part
            self.assertEqual(
                (part["reviewId"], part["total"], part["offset"]), (meta["reviewId"], meta["total"], offset)
            )
            text += part["chunk"]
            offset = part["nextOffset"]
            if part["done"]:
                self.assertEqual(len(text.encode("utf-16-le")) // 2, meta["total"])
                return {**meta, "planContent": text}
        raise AssertionError("the plan cursor never finished")

    def open_review(self, previous: str | None = None) -> dict:
        """Opens the plan review in the terminal (/plan-review) and waits until the registry shows a new one."""
        self.omp.type("/plan-review")
        return kit.wait_for(
            lambda: (
                lambda r: r["planReview"]
                if r and r.get("planReview") and r["planReview"]["reviewId"] != previous
                else None
            )(self.reg()),
            timeout=45,
            what="the plan review in the registry",
        )

    def snapshot(self) -> dict:
        reg = self.reg()
        assert reg
        return request(reg, {"v": 1, "id": "s", "nonce": reg["nonce"], "op": "snapshot"})["result"]

    # -- tests, in order --------------------------------------------------------------------------------------------

    def test_00_installed_from_the_marketplace_and_inert(self) -> None:
        self.assertIn("Installed omp-session-control from omp-tether", self.install_log)
        time.sleep(3)  # long enough for session_start and a first reconcile
        self.assertTrue(self.omp.alive())
        self.assertFalse((self.scratch.home / ".config").exists(), "installing must not create anything outside ~/.omp")
        self.omp.type("/session-control status")
        kit.wait_for(lambda: "inert" in self.omp.text(), timeout=20, what="the status text")
        self.assertIn("omp-session-control 0.3.0", self.omp.text())
        self.assertFalse((self.scratch.home / ".config").exists(), "status must not create anything either")

    def test_01_on_command_enables_this_session(self) -> None:
        self.omp.type("/session-control on")
        reg = kit.wait_for(self.reg, timeout=30, what="the registry after /session-control on")
        self.assertEqual(stat.S_IMODE(self.ctl_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.ctl_dir / "enabled").stat().st_mode), 0o600)
        self.assertEqual(reg["pid"], self.omp.pid)
        self.assertEqual(reg["sessionId"], self.omp.collab_hosts()[0]["sessionId"])
        self.omp.type("/session-control status")
        kit.wait_for(lambda: "listening" in self.omp.text(), timeout=20, what="the listening status")

    def test_10_conformance_read_only_and_limits(self) -> None:
        r = conformance(self.ctl_dir, self.omp.pid, "--exclusive", "--idle-ms", str(IDLE_MS))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("0 failed", r.stdout)

    def test_20_stop_interrupts_a_running_tool_call(self) -> None:
        self.assertEqual(self.op("abort"), {"aborted": False})  # idle: nothing to stop
        self.omp.type("RUN_SLEEP please")
        # the `sleep` is not a separate process we can see, so the proof is in the session file
        kit.wait_for(
            lambda: any(
                e.get("customType") == "tool_execution_start" and e["data"].get("args", {}).get("command") == "sleep 60"
                for e in self.omp.session_entries()
            ),
            timeout=30,
            what="the sleep 60 tool call to start",
        )
        time.sleep(1)
        self.assertTrue(self.snapshot()["busy"])
        started = time.monotonic()
        self.assertEqual(self.op("abort"), {"aborted": True})
        kit.wait_for(lambda: self.snapshot()["busy"] is False, timeout=15, what="the turn to end")
        self.assertLess(time.monotonic() - started, 20)  # far less than the 60 s the tool would have run
        results = [
            e["message"] for e in self.omp.session_entries() if (e.get("message") or {}).get("role") == "toolResult"
        ]
        self.assertEqual(results[-1]["toolName"], "bash")
        self.assertTrue(results[-1]["isError"])
        self.assertEqual(results[-1]["content"][0]["text"], "Command aborted")
        self.assertTrue(self.omp.alive())

    def test_21_rename_changes_the_listing_and_the_session_file(self) -> None:
        result = self.op("rename", {"name": "  Rename  test ✓ "})
        self.assertEqual(result["name"], "Rename test ✓")
        kit.wait_for(
            lambda: (self.omp.collab_hosts() or [{}])[0].get("sessionName") == "Rename test ✓",
            timeout=20,
            what="omp's own listing to show the new name",
        )
        kit.wait_for(
            lambda: any(
                e.get("type") == "title_change" and e.get("title") == "Rename test ✓"
                for e in self.omp.session_entries()
            ),
            timeout=20,
            what="the title_change entry",
        )
        self.assertEqual(self.snapshot()["name"], "Rename test ✓")

    def test_30_conformance_mutations_and_off_switch(self) -> None:
        r = conformance(self.ctl_dir, self.omp.pid, "--mutate", "--off-switch")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(self.omp.alive())
        self.assertIsNotNone(kit.wait_for(self.reg, timeout=15, what="control to be back after the off switch"))

    def test_40_hostile_clients_do_not_hurt_the_session(self) -> None:
        reg = self.reg()
        assert reg
        for i in range(60):
            with socket.socket(socket.AF_UNIX) as s:
                s.connect(reg["socket"])
                s.sendall([b'{"v":1,"id":"x","nonce":"', b"\xff\xfe\x00\n", b"[" * 5000 + b"\n", b"{bad\n"][i % 4])
                if i % 2:
                    s.shutdown(socket.SHUT_RDWR)
        self.assertTrue(self.omp.alive())
        time.sleep(IDLE_MS / 1000 + 1)  # let abandoned connections time out
        self.assertEqual(self.snapshot()["pid"], self.omp.pid)
        self.assertTrue(self.omp.alive())

    def test_50_live_disabled_extensions_edit_closes_and_reopens_control(self) -> None:
        old = self.reg()
        assert old
        self.scratch.write_config("disabledExtensions:\n  - extension-module:omp-session-control\n")
        kit.wait_for(lambda: self.reg() is None, timeout=20, what="control to close after disabledExtensions")
        self.assertFalse(Path(old["socket"]).exists())
        self.scratch.write_config()
        new = kit.wait_for(self.reg, timeout=20, what="control to come back")
        self.assertNotEqual(new["nonce"], old["nonce"])

    def test_55_a_replaced_control_directory_is_picked_up_again(self) -> None:
        old = self.reg()
        assert old
        directory = self.ctl_dir
        # The session removes its own socket and registry the moment it sees them go, so it can unlink an entry between
        # rmtree listing it and deleting it. That is the extension behaving correctly, not an error: delete until gone.
        for _ in range(20):
            shutil.rmtree(directory, ignore_errors=True)
            if not directory.exists():
                break
        self.assertFalse(directory.exists())
        directory.mkdir(mode=0o700)
        (directory / "enabled").write_text("")
        new = kit.wait_for(self.reg, timeout=20, what="a new registry in the recreated directory")
        self.assertNotEqual(new["nonce"], old["nonce"])
        self.assertEqual(self.snapshot()["pid"], self.omp.pid)
        self.assertTrue(self.omp.alive())

    def test_60_a_second_session_removes_what_a_dead_one_left(self) -> None:
        directory = self.ctl_dir
        dead = subprocess.Popen(["/usr/bin/true"])
        dead.wait()
        stale_sock = directory / "deaddeaddeaddead.sock"
        with socket.socket(socket.AF_UNIX) as s:
            s.bind(str(stale_sock))
        stale_reg = directory / ("0" * 32 + ".json")
        stale_reg.write_text(json.dumps({"v": 1, "pid": dead.pid, "socket": str(stale_sock), "sessionId": "x"}))
        stale_reg.chmod(0o600)
        # the environment beats the stored setting
        second = kit.PtyOmp(self.scratch, {"OMP_SESSION_CONTROL_IDLE_MS": str(SECOND_IDLE_MS)})
        try:
            kit.wait_for(lambda: registries(directory, second.pid), timeout=60, what="the second session's registry")
            self.assertFalse(stale_reg.exists() or stale_sock.exists(), "the dead session's files are removed")
            self.assertEqual(len(registries(directory)), 2)
            r = conformance(directory, second.pid, "--idle-ms", str(SECOND_IDLE_MS))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        finally:
            second.stop()
        # a clean exit removes only that session's files
        kit.wait_for(
            lambda: not registries(directory, second.pid), timeout=15, what="the second session's registry to go"
        )
        self.assertEqual([r["pid"] for r in registries(directory)], [self.omp.pid])

    def test_70_a_plan_review_is_read_and_answered_over_the_socket(self) -> None:
        reg = self.reg()
        assert reg
        self.assertEqual(
            reg["ops"],
            [
                "snapshot",
                "abort",
                "rename",
                "plan-review",
                "plan-answer",
                "session-info",
                "compact",
                "set-model",
                "set-thinking",
                "set-mode",
            ],
        )
        self.assertIsNone(reg["planReview"])
        self.assertEqual(self.op("plan-review", {"offset": 0}), {"open": False})
        self.omp.type("/plan")
        kit.wait_for(
            lambda: "Plan mode" in self.omp.text() or "plan mode" in self.omp.text(), timeout=30, what="plan mode"
        )
        # omp reviews the newest local://*plan.md of the session: write one where the session keeps its local files.
        files = list((self.scratch.home / ".omp" / "agent" / "sessions").rglob("*.jsonl"))
        self.assertEqual(len(files), 1)
        local = files[0].with_suffix("") / "local"
        local.mkdir(parents=True, exist_ok=True)
        plan = "# Probe plan\n\n" + "".join(
            f"{i}. step with \u201cquotes\u201d and \u8ba1\u5212 \U0001f600\n" for i in range(6000)
        )
        (local / "probe-plan.md").write_text(plan, encoding="utf-8")

        first = self.open_review()
        self.assertTrue(first["title"])  # omp derives it (here from the file name)
        read = self.read_plan()
        self.assertEqual(read["reviewId"], first["reviewId"])
        self.assertEqual(read["planContent"], plan)
        self.assertEqual(read["planFilePath"], "local://probe-plan.md")
        self.assertEqual([o["id"] for o in read["options"]], ["execute", "compact", "keep", "refine", "save"])
        self.assertTrue(all(isinstance(o["label"], str) and not o["disabled"] for o in read["options"]))
        self.assertGreater(len(plan), 150_000)

        # refusals first: nothing may change in the terminal
        self.assertEqual(
            self.raw_op("plan-answer", {"reviewId": first["reviewId"], "choice": "refine"})["code"], "invalid_args"
        )
        self.assertEqual(
            self.raw_op("plan-answer", {"reviewId": "not-the-review", "choice": "execute"})["code"], "not_accepted"
        )
        self.assertEqual(self.reg()["planReview"]["reviewId"], first["reviewId"])

        # refine sends the feedback as the next planning prompt: the fake provider is asked again
        calls = self.scratch.root / "provider-calls.jsonl"
        before = len(calls.read_text().splitlines()) if calls.exists() else 0
        self.assertEqual(
            self.op(
                "plan-answer",
                {"reviewId": first["reviewId"], "choice": "refine", "feedback": "PROBE-FEEDBACK make it shorter"},
            ),
            {"accepted": True},
        )
        kit.wait_for(lambda: self.reg() and self.reg()["planReview"] is None, timeout=30, what="the review to close")
        kit.wait_for(
            lambda: "PROBE-FEEDBACK" in (calls.read_text() if calls.exists() else "")
            and len(calls.read_text().splitlines()) > before,
            timeout=45,
            what="the feedback to reach the model",
        )
        kit.wait_for(lambda: self.snapshot()["busy"] is False, timeout=30, what="the refine turn to end")

        # a second review, answered with execute
        second = self.open_review(previous=first["reviewId"])
        self.assertNotEqual(second["reviewId"], first["reviewId"])
        self.assertEqual(self.read_plan()["reviewId"], second["reviewId"])
        self.assertEqual(
            self.op("plan-answer", {"reviewId": second["reviewId"], "choice": "execute"}), {"accepted": True}
        )
        kit.wait_for(lambda: self.reg() and self.reg()["planReview"] is None, timeout=30, what="the review to close")
        # answering again is refused: the terminal no longer has that review
        self.assertEqual(
            self.raw_op("plan-answer", {"reviewId": second["reviewId"], "choice": "execute"})["code"], "not_accepted"
        )
        self.assertTrue(self.omp.alive())
        self.assertNotIn("PROBE-FEEDBACK", (self.ctl_dir / "events.log").read_text())

    def live_entries(self) -> list[dict]:
        """The entries of the live session's file (after test_70 the scratch home holds several session files)."""
        sid = self.snapshot()["sessionId"]
        files = [f for f in (self.scratch.home / ".omp" / "agent" / "sessions").rglob("*.jsonl") if sid in f.name]
        if len(files) != 1:
            return []
        return [json.loads(line) for line in files[0].read_text().splitlines() if line.strip()]

    def test_75_session_commands_read_and_change_the_session(self) -> None:
        self.assertTrue(kit.wait_for(lambda: self.snapshot()["busy"] is False, timeout=30, what="an idle session"))
        # (the new session's registry follows a moment after the switch, so wait until it has been stable for a while)
        stable: list = []

        def settled() -> bool:
            reg, snap = self.reg() or {}, self.snapshot()
            now = (reg.get("sessionId"), reg.get("generation"))
            if now != (snap["sessionId"], snap["generation"]) or snap["busy"]:
                stable.clear()
                return False
            stable.append(time.monotonic())
            return stable[-1] - stable[0] >= 3

        kit.wait_for(settled, timeout=60, what="the registry to settle on the live session")
        info = self.op("session-info")
        self.assertEqual(info["model"], {"provider": "tethertest", "id": "lite"})
        self.assertEqual(info["thinkingLevels"], [])  # the first fake model has no reasoning
        self.assertEqual(
            sorted((m["provider"], m["id"]) for m in info["models"]), [("tethertest", "deep"), ("tethertest", "lite")]
        )
        # start from off, whatever plan mode state the earlier test left
        self.assertIn(self.op("set-mode", {"mode": "plan", "action": "exit"})["changed"], (True, False))
        self.assertEqual(self.op("session-info")["mode"], {"plan": False, "goal": False, "loop": False, "vibe": False})
        self.assertEqual(self.raw_op("set-thinking", {"level": "high"})["code"], "invalid_args")

        # switch to the model that has a thinking ladder; the footer of the terminal shows it
        self.assertEqual(
            self.op("set-model", {"provider": "tethertest", "id": "deep"}),
            {"model": {"provider": "tethertest", "id": "deep"}},
        )
        info = self.op("session-info")
        self.assertEqual(info["model"]["id"], "deep")
        self.assertEqual(info["thinkingLevels"], ["off", "low", "medium", "high"])
        self.assertEqual(self.raw_op("set-model", {"provider": "tethertest", "id": "nope"})["code"], "invalid_args")
        self.assertEqual(self.raw_op("set-thinking", {"level": "auto"})["code"], "invalid_args")
        self.assertEqual(self.op("set-thinking", {"level": "medium"}), {"thinkingLevel": "medium"})
        self.assertEqual(self.op("session-info")["thinkingLevel"], "medium")
        self.assertEqual(self.op("set-thinking", {"level": "off"}), {"thinkingLevel": "off"})
        kit.wait_for(
            lambda: any(
                e.get("type") == "thinking_level_change" and e.get("thinkingLevel") == "medium"
                for e in self.live_entries()
            ),
            timeout=20,
            what="the thinking level change in the session file",
        )
        self.assertEqual(self.op("set-model", {"provider": "tethertest", "id": "lite"})["model"]["id"], "lite")

        # plan mode on and off; the registry and the terminal agree
        self.assertEqual(self.op("set-mode", {"mode": "plan", "action": "enter"}), {"changed": True})
        self.assertTrue(self.op("session-info")["mode"]["plan"])
        self.assertEqual(self.op("set-mode", {"mode": "plan", "action": "enter"}), {"changed": False})
        self.assertEqual(self.op("set-mode", {"mode": "plan", "action": "exit"}), {"changed": True})
        self.assertFalse(self.op("session-info")["mode"]["plan"])
        for args in (
            {"mode": "loop", "action": "enter"},
            {"mode": "plan", "action": "pause"},
            {"mode": "goal", "action": "enter"},
        ):
            self.assertEqual(self.raw_op("set-mode", args)["code"], "invalid_args", args)

        # compaction returns when it has finished: the session file then holds a compaction entry
        self.assertEqual(self.raw_op("compact", {"instructions": "x" * 2001})["code"], "invalid_args")
        started = time.monotonic()
        reply = self.raw_op("compact", {"instructions": "keep the probe details"})
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["result"], {"ok": True})
        self.assertLess(time.monotonic() - started, 120)
        self.assertTrue(
            any(e.get("type") == "compaction" for e in self.live_entries()),
            "the compaction must be in the session file by the time compact answered",
        )
        self.assertFalse(self.snapshot()["busy"])
        self.assertIs(self.snapshot()["compacting"], False)
        self.assertIs(self.op("session-info")["compacting"], False)
        self.assertTrue(self.omp.alive())
        # no argument text in the log
        self.assertNotIn("keep the probe details", (self.ctl_dir / "events.log").read_text())

    def test_90_exit_closes_the_socket_and_removes_the_registry(self) -> None:
        reg = self.reg()
        assert reg
        self.omp.stop()
        self.assertFalse(self.omp.alive())
        self.assertEqual(registries(self.ctl_dir), [])
        self.assertFalse(Path(reg["socket"]).exists())
        with socket.socket(socket.AF_UNIX) as s, self.assertRaises(OSError):
            s.connect(reg["socket"])
        log = (self.ctl_dir / "events.log").read_text()
        self.assertIn('"kind":"listening"', log)
        self.assertIn('"kind":"closed"', log)
        self.assertNotIn(str(self.scratch.home), log)  # the log holds kinds, pids and session ids, no paths
        self.assertEqual(stat.S_IMODE((self.ctl_dir / "events.log").stat().st_mode), 0o600)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    unittest.main()
