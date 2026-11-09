"""Regression tests: protection records, transitional manager states, damaged metadata, lost state, exact prune and
record binding, lock layout, service-manager answers, network deadlines, installer provenance and the release gates."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
import unittest
from contextlib import redirect_stdout

import support
import test_hardening_more as hm
from omp_services import checks, heartbeat, hostplat, net, runtime
from omp_services import paths as paths_mod
from omp_services.manager import make_manager
from omp_services.units import ServiceSpec
from omp_services.util import ServicesError

HUB = hm.HUB
TOKEN = "transaction-token"
SHA = "0" * 64


# ---------------------------------------------------------------------------------------------------------------------
# parent cancellation
# ---------------------------------------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------------------------------------
# watchdog and lost state; service manager answers
# ---------------------------------------------------------------------------------------------------------------------


class LostState(hm.Base):
    def setUp(self):
        super().setUp()
        self.s.services("plug", {**HUB, "protected": True})
        self.s.health("plug", {"id": "w", "type": "watchdog", "service": "hub", "restart": {}})
        self.init()
        self.s.services("plug", HUB)  # the flag is dropped from the declaration

    def round(self):
        with self.s.patched():
            paths = paths_mod.from_env(os.environ, "darwin")
            with open(os.devnull, "w") as sink, redirect_stdout(sink):
                heartbeat.run_once(paths, os.environ, send=False, allow_actions=True)

    def test_a_missing_state_file_is_lost_state_not_a_machine_with_nothing_protected(self):
        (self.s.data_dir / "state.json").unlink()
        self.s.set_state("osvtest.plug.hub", "waiting")
        self.s.clear_calls()
        self.round()
        self.assertEqual(self.s.restarts(), [])

    def test_a_dangling_state_link_is_lost_state_too(self):
        state = self.s.data_dir / "state.json"
        state.unlink()
        state.symlink_to(self.s.root / "nowhere.json")
        self.s.set_state("osvtest.plug.hub", "waiting")
        self.s.clear_calls()
        self.round()
        self.assertEqual(self.s.restarts(), [])


class _ManagerAnswers:
    def test_a_manager_that_cannot_answer_does_not_let_uninstall_delete_the_unit_file(self):
        self.s.services("plug", {"name": "svc", "program": HUB["program"]})
        self.init()
        file = self.unit_file("osvtest.plug.svc")
        (self.s.fake / "manager-down").write_text("")
        rc, out, err = self.s.cli("uninstall", "--yes")
        self.assertTrue(file.exists(), "a manager that cannot answer has not said that the unit stopped")
        self.assertNotEqual(rc, 0)


class ManagerAnswersLaunchd(_ManagerAnswers, hm.Base):
    kind = "launchd"


class ManagerAnswersSystemd(_ManagerAnswers, hm.Base):
    kind = "systemd"

    def test_unrecognised_answers_are_errors_and_real_negative_answers_are_not(self):
        manager = make_manager("systemd", self.s.env, sleep=lambda s: None)
        (self.s.fake / "manager-down").write_text("")
        with self.assertRaises(ServicesError):
            manager.is_enabled("osvtest.x")
        with self.assertRaises(ServicesError):
            manager.is_running("osvtest.x")
        (self.s.fake / "manager-down").unlink()
        self.assertFalse(manager.is_enabled("osvtest.x"), "no such unit file is a real answer")
        self.assertFalse(manager.is_running("osvtest.x"))


# ---------------------------------------------------------------------------------------------------------------------
# network deadlines
# ---------------------------------------------------------------------------------------------------------------------


class NetworkDeadlines(hm.Base):
    def listener(self):
        lst = socket.socket()
        lst.bind(("127.0.0.1", 0))
        lst.listen(8)
        lst.settimeout(0.2)
        count = [0]
        stop = threading.Event()

        def accept():
            while not stop.is_set():
                try:
                    conn, _ = lst.accept()
                    count[0] += 1
                    conn.close()
                except OSError:
                    pass

        threading.Thread(target=accept, daemon=True).start()
        self.addCleanup(lambda: (stop.set(), lst.close()))
        return lst.getsockname()[1], count

    def slow_resolver(self, seconds):
        original = socket.getaddrinfo

        def slow(*a, **k):
            time.sleep(seconds)
            return original(*a, **k)

        socket.getaddrinfo = slow
        self.addCleanup(setattr, socket, "getaddrinfo", original)

    def test_no_connection_is_opened_by_an_abandoned_lookup(self):
        port, count = self.listener()
        self.slow_resolver(1.2)
        self.assertEqual(net.tcp_connect("127.0.0.1", port, 0.3), "timeout")
        self.assertEqual(net.http_get(net.parse_http_url(f"http://127.0.0.1:{port}/"), 0.3).error, "timeout")
        time.sleep(2.5)
        self.assertEqual(count[0], 0, "a lookup that finishes after the caller gave up opened a connection")

    def test_the_caller_is_released_at_the_deadline_not_half_a_second_later(self):
        self.slow_resolver(3)
        started = time.monotonic()
        net.tcp_connect("127.0.0.1", 9, 0.3)
        self.assertLess(time.monotonic() - started, 0.45)

    def test_the_owner_fallback_connection_spends_what_is_left_of_the_check_budget(self):
        seen = {}
        original = net.tcp_connect

        def spy(host, port, timeout, deadline=None):
            seen["deadline"] = deadline
            return "connection-refused"

        net.tcp_connect = spy
        self.addCleanup(setattr, net, "tcp_connect", original)
        real = hostplat.listener_uids
        hostplat.listener_uids = lambda *a, **k: set()
        self.addCleanup(setattr, hostplat, "listener_uids", real)
        started = time.monotonic()
        checks.check_uid_owner("plug", {"id": "o", "type": "uid-owner", "port": 1}, self.ctx(), 0.5)
        self.assertIsNotNone(seen.get("deadline"), "the caller's budget is passed on as an absolute deadline")
        self.assertLessEqual(seen["deadline"], started + 0.55)

    def test_the_watchdog_never_sleeps_past_its_budget(self):
        spec = ServiceSpec("plug", "svc", "osvtest.plug.svc", ("/bin/x",), (), "/", "d", False, 10, "/l")
        manager = make_manager("launchd", self.s.env)
        (self.s.units / "osvtest.plug.svc.plist").write_text("<!-- Managed by omp-services. -->")
        self.s.set_state("osvtest.plug.svc", "stopped")
        calls = iter([False] + [False] * 100)
        manager.is_running = lambda label: next(calls)  # it never comes up: the grace loop runs to the deadline
        ctx = self.ctx(
            manager=manager,
            services={"plug": {"svc": spec}},
            allow_actions=True,
            protection=lambda: {"osvtest.plug.svc": False},
        )
        check = {"severity": "fail", "id": "w", "type": "watchdog", "service": "svc", "restart": {"graceSeconds": 30}}
        started = time.monotonic()
        checks.run_check("plug", check, ctx, 0.6)
        self.assertLess(time.monotonic() - started, 0.85)


# ---------------------------------------------------------------------------------------------------------------------
# the plan binds pruning and recorded state
# ---------------------------------------------------------------------------------------------------------------------


class PlanBinding(hm.Base):
    def owned_release(self, name):
        d = self.s.data_dir / "releases" / name
        (d / "python").mkdir(parents=True)
        (d / runtime.RELEASE_MARKER).write_text(runtime.release_marker_text(name))
        stamp = 1000 + int(name[0], 16)
        os.utime(d, (stamp, stamp))
        return d

    def test_pruning_is_part_of_the_plan_and_of_its_digest(self):
        self.init()
        before = self.digest()
        for i in range(1, 5):
            self.owned_release(f"{i}.0.0-" + f"{i:x}" * 32)
        rc, out, err = self.s.cli("init", "--plan")
        self.assertIn("old code release", out)
        self.assertNotEqual(self.digest(), before)
        digest = self.digest()
        extra = self.owned_release("5.0.0-" + "5" * 32)
        rc, out, err = self.s.cli("init", "--yes", "--expect-plan", digest)
        self.assertEqual(rc, 5, "a release appeared after the plan was shown")
        self.assertTrue(extra.exists())

    def test_only_the_listed_releases_are_removed(self):
        self.init()
        listed = self.owned_release("1.0.0-" + "1" * 32)
        later = self.owned_release("2.0.0-" + "2" * 32)
        with self.s.patched():
            paths = paths_mod.from_env(os.environ, "darwin")
            removed = runtime.remove_releases(paths, [listed.name], set())
        self.assertEqual(removed, [listed.name])
        self.assertTrue(later.exists(), "what the plan did not list is not looked for")

    def test_remembered_protection_is_part_of_the_digest(self):
        program = {"type": "exec", "argv": ["/bin/sleep", "1"]}
        self.s.services("plug", {"name": "hub", "program": program})
        a = self.digest()
        self.s.services("plug", {"name": "hub", "protected": True, "program": program})
        b = self.digest()
        self.assertNotEqual(a, b, "the same unit file, but a different protection record")


# ---------------------------------------------------------------------------------------------------------------------
# lock layout and live proof
# ---------------------------------------------------------------------------------------------------------------------


class LockLayout(hm.Base):
    def test_a_data_directory_inside_another_managed_root_is_refused(self):
        inside = self.s.config_dir / "data"
        for cmd in (["init", "--plan"], ["init", "--yes"], ["uninstall", "--plan"], ["purge", "--plan"]):
            rc, out, err = self.s.cli(*cmd, extra_env={"OMP_SERVICES_DATA_DIR": str(inside)})
            self.assertEqual(rc, 2, cmd)
            self.assertIn("overlap", err)
        self.assertFalse(inside.exists())


# ---------------------------------------------------------------------------------------------------------------------
# installer provenance, documents, gates
# ---------------------------------------------------------------------------------------------------------------------


class InstallerProvenance(hm.Base):
    def test_an_existing_installation_is_not_run_when_a_checkout_was_named(self):
        self.s.omp_install("omp-services", "0.1.0", tree=support.PLUGIN)
        checkout = self.s.root / "checkout"
        (checkout / ".omp-plugin").mkdir(parents=True)
        done = subprocess.run(
            ["/bin/sh", str(support.PLUGIN / "install.sh")],
            env={**self.s.env, "OMP_TETHER_SOURCE_DIR": str(checkout)},
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            timeout=60,
        )
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn("already installed", done.stderr)
        self.assertNotIn("Running: omp-services init", done.stdout)


# ---------------------------------------------------------------------------------------------------------------------
# protection records and unknown history
# ---------------------------------------------------------------------------------------------------------------------

PROGRAM_2 = {"type": "exec", "argv": ["/bin/sleep", "2"]}


class ProtectionRecords(hm.Base):
    def setUp(self):
        super().setUp()
        self.s.services("plug", {**HUB, "protected": True})
        self.init()
        self.label = "osvtest.plug.hub"

    def change_program(self, **extra):
        self.s.services("plug", {"name": "hub", "program": PROGRAM_2, **extra})

    def test_a_retained_protected_unit_keeps_its_record_when_the_declaration_goes_away(self):
        self.s.services("plug", {"name": "other", "program": HUB["program"]})  # hub is no longer declared
        rc, out, err = self.init()
        self.assertEqual(rc, 0, out + err)
        self.assertIn(self.label, self.s.loaded())
        self.assertTrue(self.state()["units"][self.label]["protected"], "the record outlives the declaration")
        self.change_program()  # declared again, without the flag
        self.s.clear_calls()
        rc, out, err = self.init()
        self.assertIn(self.label, self.s.loaded())
        self.assertNotIn("--", self.unit_file(self.label).read_text().split("sleep")[-1][:6])
        self.assertEqual(
            [c for c in self.s.calls() if c.startswith("bootout") and self.label in c],
            [],
            "still protected: never reloaded",
        )

    def test_a_live_unit_that_the_state_file_does_not_mention_is_treated_as_protected_by_init(self):
        state = self.state()
        del state["units"][self.label]  # the record is lost; the unit is still ours and still running
        (self.s.data_dir / "state.json").write_text(json.dumps(state))
        self.change_program()  # and the declaration no longer says protected
        before = self.unit_file(self.label).read_text()
        self.s.clear_calls()
        rc, out, err = self.init()
        self.assertEqual(self.unit_file(self.label).read_text(), before, "history unknown: not rewritten")
        self.assertEqual([c for c in self.s.calls() if c.startswith("bootout")], [])
        self.assertIn("no record of it", out)

    def test_naming_it_overrides_the_unknown_history(self):
        state = self.state()
        del state["units"][self.label]
        (self.s.data_dir / "state.json").write_text(json.dumps(state))
        self.change_program()
        rc, out, err = self.init("--restart-protected", "plug.hub")
        self.assertIn("2", self.unit_file(self.label).read_text())

    def test_uninstall_leaves_a_live_unit_the_state_file_does_not_mention(self):
        state = self.state()
        del state["units"][self.label]
        (self.s.data_dir / "state.json").write_text(json.dumps(state))
        self.s.services("plug", {"name": "hub", "program": HUB["program"]})
        rc, out, err = self.s.cli("uninstall", "--yes")
        self.assertIn(self.label, self.s.loaded())
        self.assertTrue(self.unit_file(self.label).exists())

    def test_the_watchdog_does_not_restart_a_service_the_state_file_does_not_record(self):
        self.s.health("plug", {"id": "w", "type": "watchdog", "service": "hub", "restart": {}})
        self.s.services("plug", {"name": "hub", "program": HUB["program"]})
        state = self.state()
        del state["units"][self.label]
        (self.s.data_dir / "state.json").write_text(json.dumps(state))
        self.s.set_state(self.label, "waiting")
        self.s.clear_calls()
        with self.s.patched():
            paths = paths_mod.from_env(os.environ, "darwin")
            with open(os.devnull, "w") as sink, redirect_stdout(sink):
                heartbeat.run_once(paths, os.environ, send=False, allow_actions=True)
        self.assertEqual(self.s.restarts(), [])


# ---------------------------------------------------------------------------------------------------------------------
# systemd states between up and down
# ---------------------------------------------------------------------------------------------------------------------


class TransitionalStates(hm.Base):
    kind = "systemd"

    def setUp(self):
        super().setUp()
        self.s.services("plug", {"name": "hub", "program": HUB["program"]})
        self.init()
        self.label = "osvtest.plug.hub"
        self.manager = make_manager("systemd", self.s.env, sleep=lambda s: None)

    def stop_calls(self):
        return [c for c in self.s.calls() if "stop" in c or "disable" in c]

    def test_reloading_and_activating_units_are_live_not_stopped(self):
        for word in ("reloading-disabled", "activating-disabled", "deactivating-disabled"):
            self.s.set_state(self.label, word)
            self.assertTrue(self.manager.is_running(self.label), word)
            self.assertTrue(self.manager.is_loaded(self.label), word)
            self.assertTrue(self.manager.transitional(self.label), word)

    def test_a_transitional_unit_is_never_unloaded(self):
        self.s.set_state(self.label, "reloading-disabled")
        self.s.clear_calls()
        with self.assertRaises(ServicesError):
            self.manager.unload(self.label, self.unit_file(self.label))
        self.assertEqual(self.stop_calls(), [])

    def test_init_leaves_a_transitional_unit_alone_and_says_so(self):
        self.s.set_state(self.label, "reloading-disabled")
        self.s.services("plug", {"name": "hub", "program": PROGRAM_2})
        before = self.unit_file(self.label).read_text()
        self.s.clear_calls()
        rc, out, err = self.init()
        self.assertEqual(self.unit_file(self.label).read_text(), before)
        self.assertEqual(self.stop_calls(), [])
        self.assertIn("changing state", out)

    def test_uninstall_keeps_the_file_of_a_transitional_unit(self):
        self.s.set_state(self.label, "reloading-disabled")
        self.s.clear_calls()
        rc, out, err = self.s.cli("uninstall", "--yes")
        self.assertTrue(self.unit_file(self.label).exists())
        self.assertEqual(self.stop_calls(), [])
        self.assertIn("changing state", out)

    def test_the_watchdog_reports_and_does_not_restart_a_transitional_service(self):
        self.s.health("plug", {"id": "w", "type": "watchdog", "service": "hub", "restart": {}})
        self.s.set_state(self.label, "reloading-disabled")
        self.s.clear_calls()
        with self.s.patched():
            paths = paths_mod.from_env(os.environ, "linux")
            with open(os.devnull, "w") as sink, redirect_stdout(sink):
                heartbeat.run_once(paths, os.environ, send=False, allow_actions=True)
        self.assertEqual(self.s.restarts(), [])
        self.assertEqual(self.stop_calls(), [])
        spec = ServiceSpec("plug", "hub", self.label, ("/bin/x",), (), "/", "d", False, 10, "/l")
        ctx = self.ctx(
            manager=self.manager,
            services={"plug": {"hub": spec}},
            allow_actions=True,
            protection=lambda: {self.label: False},
        )
        result = checks.run_check(
            "plug", {"severity": "fail", "id": "w", "type": "watchdog", "service": "hub", "restart": {}}, ctx, 5
        )
        self.assertEqual(result.reason, "transitional-state")


# ---------------------------------------------------------------------------------------------------------------------
# damaged metadata is refused; unreadable is not absent
# ---------------------------------------------------------------------------------------------------------------------


class DamagedMetadata(hm.Base):
    def setUp(self):
        super().setUp()
        self.s.services("plug", {**HUB, "protected": True})
        self.init()
        self.label = "osvtest.plug.hub"

    def write_state(self, mutate):
        state = self.state()
        mutate(state["units"][self.label])
        (self.s.data_dir / "state.json").write_text(json.dumps(state))

    def test_a_unit_record_without_its_protection_flag_is_refused_not_read_as_unprotected(self):
        self.write_state(lambda record: record.pop("protected"))
        rc, out, err = self.s.cli("init", "--plan")
        self.assertEqual(rc, 2, out + err)
        self.assertIn("invalid fields", err)

    def test_a_protection_flag_of_the_wrong_type_is_refused(self):
        self.write_state(lambda record: record.update(protected="no"))
        rc, out, err = self.s.cli("uninstall", "--plan")
        self.assertEqual(rc, 2, out + err)

    def test_a_record_without_a_hash_is_refused(self):
        self.write_state(lambda record: record.pop("sha256"))
        rc, out, err = self.s.cli("init", "--plan")
        self.assertEqual(rc, 2, out + err)

    def test_a_damaged_state_means_the_watchdog_restarts_nothing(self):
        self.s.services("plug", {"name": "hub", "program": HUB["program"]})
        self.s.health("plug", {"id": "w", "type": "watchdog", "service": "hub", "restart": {}})
        self.write_state(lambda record: record.pop("protected"))
        self.s.set_state(self.label, "waiting")
        self.s.clear_calls()
        with self.s.patched():
            paths = paths_mod.from_env(os.environ, "darwin")
            with open(os.devnull, "w") as sink, redirect_stdout(sink):
                heartbeat.run_once(paths, os.environ, send=False, allow_actions=True)
        self.assertEqual(self.s.restarts(), [])

    def test_a_wrapper_that_cannot_be_read_is_left_alone_not_replaced(self):
        wrapper = self.s.data_dir / "bin" / "omp-services"
        elsewhere = self.s.root / "elsewhere.sh"
        elsewhere.write_text("#!/bin/sh\n")
        wrapper.unlink()
        wrapper.symlink_to(elsewhere)
        rc, out, err = self.init()
        self.assertTrue(wrapper.is_symlink(), "an unreadable target is not an absent one")
        self.assertIn("cannot be read", out)

    def test_a_directory_where_a_wrapper_belongs_is_left_alone(self):
        wrapper = self.s.data_dir / "bin" / "omp-services"
        wrapper.unlink()
        wrapper.mkdir()
        (wrapper / "keep.txt").write_text("x")
        rc, out, err = self.init()
        self.assertTrue((wrapper / "keep.txt").exists())

    def test_an_unreadable_unit_file_is_foreign_not_absent(self):
        self.s.services("plug", {"name": "hub", "program": PROGRAM_2})
        file = self.unit_file(self.label)
        os.chmod(file, 0)
        self.addCleanup(os.chmod, file, 0o644)
        rc, out, err = self.init()
        os.chmod(file, 0o644)
        self.assertNotIn("2", file.read_text().split("sleep")[-1][:6], "an unreadable unit was not overwritten")


class Gates(unittest.TestCase):
    repo = support.PLUGIN.parents[1]

    def test_the_release_target_loads_the_exact_export(self):
        self.assertIn("export-gate", (self.repo / "Makefile").read_text())
        script = self.repo / "scripts" / "export-gate.sh"
        self.assertTrue(os.access(script, os.X_OK))
        self.assertIn("fresh_history.py", script.read_text())
        self.assertIn("--repo", script.read_text())

    def test_documents_describe_the_scope_that_exists(self):
        threat = (support.PLUGIN / "docs" / "threat-model.md").read_text()
        readme = (support.PLUGIN / "README.md").read_text()
        changelog = (support.PLUGIN / "CHANGELOG.md").read_text()
        self.assertIn("## Out of scope", threat)
        for phrase in ("same user", "root", "setsid()", "No live systemd"):
            self.assertIn(phrase, threat.split("## Out of scope")[1])
        self.assertIn("omp plugin upgrade", readme)
        self.assertIn("omp-services doctor", readme)
        self.assertIn("Scope decision", changelog)
        for text in (threat, readme):
            self.assertNotIn("journal", text)
            self.assertNotIn("snapshot", text)

    def test_there_is_no_upgrade_command_left_anywhere(self):
        for path in (support.PLUGIN / "python" / "omp_services").rglob("*.py"):
            self.assertNotIn("upgrade-finish", path.read_text(), path)
        self.assertFalse((support.PLUGIN / "python" / "omp_services" / "upgrade.py").exists())
        done = subprocess.run(
            [str(support.PLUGIN / "bin" / "omp-services"), "--help"], capture_output=True, text=True, timeout=30
        )
        self.assertNotIn("upgrade", done.stdout.lower().replace("omp plugin upgrade", ""))
        self.assertNotEqual(
            subprocess.run([str(support.PLUGIN / "bin" / "omp-services"), "upgrade"], capture_output=True).returncode, 0
        )


if __name__ == "__main__":
    unittest.main()
