"""Regression tests: plan digest, protection, ownership, rollback, cancellation, locks, deadlines, releases, installer."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

import support
from omp_services import catalog, checks, config, heartbeat, net, runtime, units, util
from omp_services import paths as paths_mod
from omp_services.manager import Launchd, Systemd, make_manager
from omp_services.units import ServiceSpec
from omp_services.util import ServicesError

HUB = {"name": "hub", "program": {"type": "exec", "argv": ["/bin/sleep", "1"]}}
LAUNCHER = support.PLUGIN / "bin" / "omp-services"


class Base(support.Case):
    def init(self, *args, **kw):
        return self.s.cli("init", "--yes", *args, **kw)

    def unit_file(self, label):
        return self.s.units / (label + (".plist" if self.kind == "launchd" else ".service"))

    def state(self):
        return json.loads((self.s.data_dir / "state.json").read_text())

    def digest(self, **kw):
        rc, out, err = self.s.cli("init", "--plan", "--json", **kw)
        self.assertEqual(rc, 0, err)
        return json.loads(out)["digest"]

    def ctx(self, **kw):
        env = {**self.s.env}
        paths = paths_mod.from_env(env, "darwin")
        cfg = config.load_config(paths, env)
        paths.state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(paths.state_dir, 0o700)
        from omp_services import hostplat

        return checks.Context(cfg=cfg, paths=paths, plat=hostplat.system({}), env=env, **kw)


# ---------------------------------------------------------------------------------------------------------------------
# HIGH-1: the digest identifies the actions, not their description
# ---------------------------------------------------------------------------------------------------------------------


class Digest(Base):
    def test_a_listed_unit_that_would_run_something_else_has_another_digest(self):
        self.s.services("plug", HUB)
        self.init()
        self.s.services("plug", {"name": "hub", "program": {"type": "exec", "argv": ["/bin/sleep", "2"]}})
        first = self.digest()
        self.s.services("plug", {"name": "hub", "program": {"type": "exec", "argv": ["/bin/echo", "other program"]}})
        second = self.digest()
        self.assertNotEqual(first, second, "the same label and the same file name, but a different program")
        rc, out, err = self.s.cli("init", "--yes", "--expect-plan", first)
        self.assertEqual(rc, 5, out + err)
        self.assertNotIn("other program", self.unit_file("osvtest.plug.hub").read_text(), "nothing was applied")

    def test_the_plan_shows_what_the_unit_will_run(self):
        self.s.services("plug", {"name": "hub", "program": {"type": "exec", "argv": ["/bin/echo", "visible-argument"]}})
        rc, out, err = self.s.cli("init", "--plan")
        self.assertIn("/bin/echo visible-argument", out)

    def test_a_different_pinned_interpreter_changes_the_digest(self):
        link = self.s.root / "python-alt"
        link.symlink_to(sys.executable)
        a = self.digest()
        b = self.digest(extra_env={"OMP_SERVICES_PYTHON": str(link)})
        self.assertNotEqual(a, b)

    def test_the_digest_still_depends_on_the_text_and_is_stable_for_an_unchanged_machine(self):
        self.assertEqual(self.digest(), self.digest())
        self.s.services("plug", HUB)
        self.assertNotEqual(
            self.digest(),
            json.loads(self.s.cli("init", "--plan", "--json", extra_env={"OMP_SERVICES_LABEL_PREFIX": "osvother"})[1])[
                "digest"
            ],
        )


# ---------------------------------------------------------------------------------------------------------------------
# HIGH-2 / HIGH-3: protection remembered by the watchdog; heartbeat restarts check ownership
# ---------------------------------------------------------------------------------------------------------------------


class WatchdogProtection(Base):
    def setUp(self):
        super().setUp()
        self.s.services("plug", {**HUB, "protected": True})
        self.s.health("plug", {"id": "w", "type": "watchdog", "service": "hub", "restart": {}})
        self.init()

    def round(self):
        with self.s.patched():
            paths = paths_mod.from_env(os.environ, "darwin")
            with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink):
                heartbeat.run_once(paths, os.environ, send=False, allow_actions=True)

    def test_a_declaration_that_drops_the_flag_does_not_unprotect_the_unit_for_the_watchdog(self):
        self.s.services("plug", HUB)  # the same service, no longer declared protected
        self.s.set_state("osvtest.plug.hub", "waiting")
        self.s.clear_calls()
        self.round()
        self.assertEqual(self.s.restarts(), [], "the installed state still says protected")

    def test_an_unreadable_installed_state_means_no_restart(self):
        self.s.services("plug", HUB)
        self.s.set_state("osvtest.plug.hub", "waiting")
        (self.s.data_dir / "state.json").write_text("{damaged")
        self.s.clear_calls()
        self.round()
        self.assertEqual(self.s.restarts(), [])

    def test_protection_is_asked_again_at_the_moment_of_the_restart(self):
        manager = make_manager("launchd", self.s.env, sleep=lambda s: None)
        spec = ServiceSpec("plug", "hub", "osvtest.plug.hub", ("/bin/x",), (), "/", "d", False, 10, "/l")
        answers = iter(
            [{"osvtest.plug.hub": False}, {"osvtest.plug.hub": True}]
        )  # not protected at the start of the check, protected a moment later
        ctx = self.ctx(
            manager=manager, services={"plug": {"hub": spec}}, allow_actions=True, protection=lambda: next(answers)
        )
        self.s.set_state("osvtest.plug.hub", "waiting")
        r = checks.run_check(
            "plug", {"severity": "fail", "id": "w", "type": "watchdog", "service": "hub", "restart": {}}, ctx, 5
        )
        self.assertEqual(r.reason, "protected-not-restarted")
        self.assertEqual(self.s.restarts(), [])


# ---------------------------------------------------------------------------------------------------------------------
# HIGH-4: pruning deletes only what this tool created
# ---------------------------------------------------------------------------------------------------------------------


class Pruning(Base):
    def make_release(self, name, owned):
        d = self.s.data_dir / "releases" / name
        d.mkdir(parents=True)
        (d / "python").mkdir()
        if owned:
            (d / runtime.RELEASE_MARKER).write_text(runtime.release_marker_text(name))
        return d

    def test_releases_that_this_tool_did_not_create_are_never_pruned(self):
        self.init()
        stranger_named_like_one = self.make_release("0.0.1-" + "a" * 32, owned=False)
        stranger = self.make_release("my-own-notes", owned=False)
        for i in range(5):
            self.make_release(f"0.0.{i + 2}-" + f"{i:x}" * 32, owned=True)
        with self.s.patched():
            paths = paths_mod.from_env(os.environ, "darwin")
            removed = runtime.remove_releases(paths, runtime.prune_targets(paths, set()), set())
        self.assertTrue(stranger_named_like_one.exists() and stranger.exists())
        self.assertTrue(removed, "its own old releases are still pruned")
        self.assertTrue(all(runtime.RELEASE_NAME.fullmatch(r) for r in removed))

    def test_uninstall_keeps_a_directory_that_only_looks_like_a_release(self):
        self.init()
        lookalike = self.make_release("0.0.9-" + "b" * 32, owned=False)
        own = self.s.data_dir / "releases" / self.state()["release"]
        self.s.cli("uninstall", "--yes")
        self.assertTrue(lookalike.exists())
        self.assertFalse(own.exists())


# ---------------------------------------------------------------------------------------------------------------------
# HIGH-5: rollback restores exact manager state and reports what it could not
# ---------------------------------------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------------------------------------
# MEDIUM-1: cancellation
# ---------------------------------------------------------------------------------------------------------------------


class Cancellation(Base):
    def test_the_signal_helper_does_not_wait_for_a_lock_the_interrupted_code_holds(self):
        done = threading.Event()

        def call():
            util.kill_active_groups()
            done.set()

        with util._ACTIVE_LOCK:  # the main thread is inside run_bounded's bookkeeping when the signal arrives
            thread = threading.Thread(target=call, daemon=True)
            thread.start()
            self.assertTrue(done.wait(3), "kill_active_groups waited for _ACTIVE_LOCK")

    def test_the_limit_of_group_cleanup_is_documented(self):
        text = (support.PLUGIN / "docs" / "threat-model.md").read_text()
        self.assertIn("setsid()", text)
        self.assertNotIn("a daemonised descendant does not outlive its check;", text)

    def test_sigterm_to_a_running_command_line_ends_it_promptly(self):
        path = self.s.health(
            "plug",
            {
                "id": "slow",
                "type": "command",
                "argv": [sys.executable, "-c", "import time; time.sleep(30)"],
                "timeoutSeconds": 30,
            },
        )
        proc = subprocess.Popen(
            [str(LAUNCHER), "health", "--file", str(path)],
            env=self.s.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        time.sleep(1.5)
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
        proc.stdout.close()
        proc.stderr.close()
        self.assertEqual(proc.returncode, 128 + signal.SIGTERM)


# ---------------------------------------------------------------------------------------------------------------------
# MEDIUM-2: locks
# ---------------------------------------------------------------------------------------------------------------------


class Locks(Base):
    @property
    def lock(self) -> Path:
        return self.s.data_dir.parent / "omp-services.lock"

    def test_two_first_inits_exclude_each_other(self):
        self.assertFalse(self.s.data_dir.exists())
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock, os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        rc, out, err = self.init()
        self.assertEqual(rc, 2, out + err)
        self.assertIn("another omp-services command", err)
        self.assertFalse(self.s.data_dir.exists())

    def test_a_preview_takes_no_lock_and_creates_no_file(self):
        for cmd in (["init", "--plan"], ["init", "--plan", "--json"], ["uninstall", "--plan"], ["purge", "--plan"]):
            self.s.cli(*cmd)
        self.assertFalse(self.lock.exists())

    def test_a_lock_file_that_is_not_private_is_refused(self):
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        self.lock.write_text("")
        os.chmod(self.lock, 0o666)
        rc, out, err = self.init()
        self.assertEqual(rc, 2)
        self.assertIn("lock", err)

    def test_the_lock_outlives_a_purge(self):
        self.init()
        self.assertTrue(self.lock.exists())
        self.s.cli("purge", "--confirm-purge")
        self.assertFalse(self.s.data_dir.exists())
        self.assertTrue(self.lock.exists(), "a purge never deletes the lock it holds")


# ---------------------------------------------------------------------------------------------------------------------
# MEDIUM-3: deadlines
# ---------------------------------------------------------------------------------------------------------------------


class Deadlines(Base):
    def test_a_request_is_not_sent_after_its_caller_gave_up(self):
        srv = support.Server()
        self.addCleanup(srv.stop)
        original = socket.getaddrinfo

        def slow(*a, **k):
            time.sleep(1.5)
            return original(*a, **k)

        socket.getaddrinfo = slow
        self.addCleanup(setattr, socket, "getaddrinfo", original)
        got = net.http_get(net.parse_http_url(f"{srv.base}/late"), 0.4)
        self.assertEqual(got.error, "timeout")
        time.sleep(2.5)
        self.assertEqual(srv.requests, [], "the abandoned worker must not send the obsolete request")

    def test_abandoned_workers_are_capped(self):
        release = threading.Event()
        original = socket.getaddrinfo
        socket.getaddrinfo = lambda *a, **k: release.wait(20) and original(*a, **k)
        self.addCleanup(setattr, socket, "getaddrinfo", original)
        self.addCleanup(release.set)
        before = threading.active_count()
        started = time.monotonic()
        results = [net.tcp_connect("blocked.example.invalid", 80, 0.05) for _ in range(40)]
        self.assertEqual(set(results), {"timeout"})
        self.assertLessEqual(threading.active_count() - before, 17)
        self.assertLess(time.monotonic() - started, 40 * 0.7)

    def test_the_watchdog_lock_wait_comes_out_of_the_check_budget(self):
        spec = ServiceSpec("plug", "svc", "osvtest.plug.svc", ("/bin/x",), (), "/", "d", False, 10, "/l")
        manager = make_manager("launchd", self.s.env, sleep=lambda s: None)
        (self.s.units / "osvtest.plug.svc.plist").write_text("<!-- Managed by omp-services. -->")
        self.s.set_state("osvtest.plug.svc", "stopped")
        ctx = self.ctx(
            manager=manager,
            services={"plug": {"svc": spec}},
            allow_actions=True,
            protection=lambda: {"osvtest.plug.svc": False},
        )
        lock = ctx.paths.state_dir / "watchdog.lock"
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        (self.s.fake / "sleep-calls").write_text("0.4")
        started = time.monotonic()
        r = checks.run_check(
            "plug", {"severity": "fail", "id": "w", "type": "watchdog", "service": "svc", "restart": {}}, ctx, 1.5
        )
        self.assertLess(
            time.monotonic() - started, 2.2, "the lock wait used what was left of the 1.5 s, not a fresh allowance"
        )
        self.assertEqual(r.status, "failed")

    def test_no_manager_call_starts_after_the_deadline_has_passed(self):
        manager = Launchd(self.s.env, sleep=lambda s: None)
        manager.deadline = time.monotonic() - 1
        self.s.clear_calls()
        with self.assertRaises(ServicesError):
            manager.is_running("osvtest.x")
        self.assertEqual(self.s.calls(), [], "not even a process was started")

    def test_a_fifo_where_the_configuration_should_be_does_not_block(self):
        self.s.config_dir.mkdir(parents=True)
        os.chmod(self.s.config_dir, 0o700)
        os.mkfifo(self.s.config_dir / "config.json", 0o600)
        box = {}

        def load():
            box["cfg"] = config.load_config(paths_mod.from_env(self.s.env, "darwin"), self.s.env)

        thread = threading.Thread(target=load, daemon=True)
        thread.start()
        thread.join(3)
        self.assertFalse(thread.is_alive(), "opening a FIFO blocked the loader")
        self.assertTrue(box["cfg"].problems)

    def test_a_failed_question_to_the_service_manager_is_not_an_answer(self):
        broken = Launchd({**self.s.env, "OMP_SERVICES_LAUNCHCTL": "/nonexistent/launchctl"})
        with self.assertRaises(ServicesError):
            broken.is_loaded("osvtest.x")
        broken = Systemd({**self.s.env, "OMP_SERVICES_SYSTEMCTL": "/nonexistent/systemctl"})
        with self.assertRaises(ServicesError):
            broken.is_running("osvtest.x")


# ---------------------------------------------------------------------------------------------------------------------
# MEDIUM-4 / MEDIUM-5 / MEDIUM-6
# ---------------------------------------------------------------------------------------------------------------------


class LogRotation(Base):
    def test_a_writer_that_appends_while_the_copy_runs_cannot_push_the_copy_past_the_bound(self):
        log = self.s.root / "busy.log"
        log.write_bytes(b"A" * 20000)
        stop = threading.Event()

        def writer():
            with open(log, "ab") as f:
                while not stop.is_set():
                    f.write(b"B" * 500)
                    f.flush()
                    time.sleep(0.0005)

        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        try:
            heartbeat._rotate(log, 5000, chunk=100)
        finally:
            stop.set()
            thread.join(2)
        self.assertLessEqual(len(log.with_name("busy.log.1").read_bytes()), 5000)


class Normalisation(Base):
    def test_an_accepted_override_is_carried_as_its_validated_value(self):
        rc, out, err = self.s.cli(
            "render",
            "--platform",
            "systemd",
            extra_env={"OMP_SERVICES_HEARTBEAT_ENABLED": "true\n", "OMP_SERVICES_HEARTBEAT_INTERVAL_SECONDS": " 120 "},
        )
        self.assertEqual(rc, 0, err)
        self.assertIn('Environment="OMP_SERVICES_HEARTBEAT_ENABLED=true"', out)
        self.assertIn('Environment="OMP_SERVICES_HEARTBEAT_INTERVAL_SECONDS=120"', out)
        self.assertNotIn('true\n"', out)

    def test_one_invariant_covers_every_rendered_field(self):
        paths = paths_mod.from_env({"HOME": "/h"}, "linux")
        spec = units.heartbeat_spec("p", paths, "/usr/bin/python3", None, {})
        bad = ServiceSpec(**{**spec.__dict__, "argv": ("/usr/bin/python3\n[Service]", "x")})
        with self.assertRaises(ServicesError):
            units.render(bad, "systemd")

    def test_a_discovered_or_persisted_interpreter_path_with_a_control_character_is_refused(self):
        self.assertIsNone(runtime.find_omp({"OMP_BIN": "/tmp/omp\n[Service]", "PATH": ""}, Path("/nonexistent")))
        state = {"schemaVersion": 1, "units": {}, "python": "/usr/bin/python3\nx"}
        with self.assertRaises(ServicesError):
            runtime._validate_state(state, Path("state.json"))


class ReleaseLink(Base):
    def test_a_link_that_points_elsewhere_is_not_reported_healthy(self):
        self.init()
        rid = self.state()["release"]
        elsewhere = self.s.root / "elsewhere" / rid
        shutil.copytree(self.s.data_dir / "releases" / rid, elsewhere)
        (elsewhere / "python" / "omp_services" / "cli.py").write_text("print('different code')\n")
        link = self.s.data_dir / "current"
        link.unlink()
        link.symlink_to(elsewhere)
        rc, out, err = self.s.cli("doctor", "--json", "--no-catalog")
        findings = {f["id"]: f for f in json.loads(out)["findings"]}
        self.assertEqual(findings["runtime.current"]["level"], "fail")
        self.assertIsNone(runtime.current_release(paths_mod.from_env(self.s.env, "darwin")))

    def test_activation_refuses_a_release_that_does_not_match_its_name(self):
        self.init()
        rid = self.state()["release"]
        (self.s.data_dir / "releases" / rid / "python" / "omp_services" / "cli.py").write_text("tampered\n")
        with self.assertRaises(ServicesError):
            runtime.activate_release(paths_mod.from_env(self.s.env, "darwin"), rid)

    def test_an_installation_made_with_the_shorter_digest_keeps_working_and_migrates(self):
        self.init()
        rid = self.state()["release"]
        legacy = rid.rsplit("-", 1)[0] + "-" + rid.rsplit("-", 1)[1][:10]
        releases = self.s.data_dir / "releases"
        (releases / rid).rename(releases / legacy)
        link = self.s.data_dir / "current"
        link.unlink()
        link.symlink_to(f"releases/{legacy}")
        state = self.state()
        state["release"] = legacy
        (self.s.data_dir / "state.json").write_text(json.dumps(state))
        rc, out, err = self.s.cli("doctor", "--json", "--no-catalog")
        findings = {f["id"]: f for f in json.loads(out)["findings"]}
        self.assertEqual(findings["runtime.current"]["level"], "ok", findings)
        rc, out, err = self.init()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.state()["release"].rsplit("-", 1)[1], rid.rsplit("-", 1)[1])


# ---------------------------------------------------------------------------------------------------------------------
# MEDIUM-7, catalog, extension, docs, gates
# ---------------------------------------------------------------------------------------------------------------------


class Installer(Base):
    def run_script(self, extra):
        return subprocess.run(
            ["/bin/sh", str(support.PLUGIN / "install.sh")],
            env={**self.s.env, **extra},
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            timeout=60,
        )

    def test_a_local_checkout_source_is_validated_and_named_in_the_plan(self):
        self.s.omp_will_install("omp-services", "0.1.0", support.PLUGIN)
        for bad in ("relative/dir", str(self.s.root / "nothing-here"), str(self.s.root)):
            self.assertEqual(self.run_script({"OMP_TETHER_SOURCE_DIR": bad}).returncode, 2, bad)
        checkout = self.s.root / "checkout"
        (checkout / ".omp-plugin").mkdir(parents=True)
        done = self.run_script({"OMP_TETHER_SOURCE_DIR": str(checkout)})
        self.assertEqual(done.returncode, 3, done.stderr)
        self.assertIn(f"marketplace add {checkout}", done.stdout)
        self.assertNotIn("follows the repository's current default branch", done.stdout)

    def test_the_github_shorthand_says_it_is_not_tied_to_a_commit(self):
        self.s.omp_will_install("omp-services", "0.1.0", support.PLUGIN)
        done = self.run_script({})
        self.assertIn("not tied to the commit", done.stdout)

    def test_the_readme_does_not_claim_a_tag_cannot_move(self):
        self.assertNotIn("a tag does not move", (support.PLUGIN / "README.md").read_text())
        self.assertIn("OMP_TETHER_SOURCE_DIR", (support.PLUGIN / "README.md").read_text())

    def test_the_checksum_is_current(self):
        import hashlib

        self.assertEqual(
            (support.PLUGIN / "install.sh.sha256").read_text().split()[0],
            hashlib.sha256((support.PLUGIN / "install.sh").read_bytes()).hexdigest(),
        )


class Versions(unittest.TestCase):
    def test_precedence_follows_semantic_versioning(self):
        self.assertEqual(catalog.compare("1.0.0", "1.0.0-rc.1"), "newer")
        self.assertIsNone(catalog.compare("1.0.0-rc.1", "1.0.0"))
        self.assertEqual(catalog.compare("1.0.0-rc.2", "1.0.0-rc.1"), "newer")
        self.assertEqual(catalog.compare("1.0.0-rc.10", "1.0.0-rc.9"), "newer")
        self.assertEqual(catalog.compare("1.0.0-beta", "1.0.0-alpha"), "newer")
        self.assertIsNone(catalog.compare("1.2.3", "1.2.3"))
        self.assertIsNone(catalog.compare("1.2.3+build5", "1.2.3+build6"))
        self.assertEqual(catalog.compare("1.10.0", "1.9.9"), "newer")

    def test_a_version_that_cannot_be_ordered_is_different_not_newer(self):
        self.assertEqual(catalog.compare("nightly-2", "nightly-1"), "different")
        self.assertFalse(catalog.is_newer("nightly-2", "nightly-1"))
        row = catalog.PluginRow("p", "m", "nightly-1", "nightly-2", Path("/x"))
        self.assertEqual(row.kind, "different")
        self.assertTrue(row.outdated, "omp itself would reinstall it, so doctor still reports it")


class Docs(unittest.TestCase):
    def test_the_root_documents_describe_the_network_use_that_exists(self):
        root = (support.PLUGIN.parents[1] / "README.md").read_text()
        self.assertIn("health checks", root)
        self.assertNotIn("The one thing in this repository that can contact a network is", root)

    def test_the_uninstall_sequence_does_not_use_a_wrapper_it_has_just_removed(self):
        text = (support.PLUGIN / "README.md").read_text().split("## Uninstall and purge")[1].split("## Develop")[0]
        block = text.split("```")[1]
        self.assertLess(block.index("purge"), block.index("omp plugin uninstall"))
        self.assertIn("INSTEAD of `uninstall`", text)

    def test_the_review_status_is_not_stale(self):
        self.assertNotIn(
            "no independent review has been done", (support.PLUGIN / "docs" / "threat-model.md").read_text().lower()
        )

    def test_release_check_requires_the_type_check(self):
        makefile = (support.PLUGIN.parents[1] / "Makefile").read_text()
        self.assertIn("release-check", makefile)
        done = subprocess.run(
            ["make", "release-check"],
            cwd=support.PLUGIN.parents[1],
            env={**os.environ, "OMP_SRC": ""},
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("OMP_SRC", done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main()
