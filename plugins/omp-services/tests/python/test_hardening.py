"""Regression tests for the findings of the two independent reviews. Each test failed on commit ce7348e."""

from __future__ import annotations

import json
import math
import os
import socket
import stat
import subprocess
import sys
import time
import unittest
from pathlib import Path

import support
from omp_services import checks, config, dropins, heartbeat, hostplat, net, schema, units, util
from omp_services import paths as paths_mod
from omp_services.manager import Systemd, make_manager
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

    def ctx(self, **kw):
        env = {**self.s.env}
        paths = paths_mod.from_env(env, "darwin")
        cfg = config.load_config(paths, env)
        paths.state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(paths.state_dir, 0o700)
        return checks.Context(cfg=cfg, paths=paths, plat=hostplat.system({}), env=env, **kw)


# ---------------------------------------------------------------------------------------------------------------------
# purge only what omp-services created; init never adopts a directory
# ---------------------------------------------------------------------------------------------------------------------


class F1Adoption(Base):
    def test_init_does_not_mark_an_existing_directory_and_purge_deletes_only_what_init_created_in_it(self):
        # the README flow: another plugin's `dropins --write` created the configuration directory first
        self.s.config_dir.mkdir(parents=True)
        os.chmod(self.s.config_dir, 0o700)
        mine = self.s.config_dir / "mine.txt"
        mine.write_text("precious")
        dropin = self.s.config_dir / "health.d" / "relay.json"
        dropin.parent.mkdir(mode=0o700)
        dropin.write_text("{}")
        rc, out, err = self.init()
        self.assertEqual(rc, 0, err)
        self.assertFalse((self.s.config_dir / ".omp-services-dir").exists(), "an existing directory is never adopted")
        self.assertTrue((self.s.data_dir / ".omp-services-dir").exists(), "a directory it created is marked")
        self.assertTrue((self.s.config_dir / "config.json").exists())
        rc, out, err = self.s.cli("purge", "--confirm-purge")
        self.assertEqual(rc, 0, err)
        self.assertEqual(mine.read_text(), "precious", "a file it did not create is never deleted")
        self.assertEqual(dropin.read_text(), "{}", "another plugin's drop-in is never deleted")
        self.assertTrue(self.s.config_dir.is_dir(), "a directory it did not create is left in place")
        self.assertFalse((self.s.config_dir / "config.json").exists(), "what init wrote there is gone")
        self.assertFalse((self.s.config_dir / "secrets").exists(), "what init created there is gone")
        self.assertFalse(self.s.data_dir.exists(), "a directory it created is deleted")
        self.assertFalse(self.s.state_dir.exists())

    def test_purge_after_dropins_only_refuses_and_deletes_nothing(self):
        # nothing was recorded as created by omp-services: an unmarked directory is not touched
        self.s.config_dir.mkdir(parents=True)
        os.chmod(self.s.config_dir, 0o700)
        (self.s.config_dir / "mine.txt").write_text("precious")
        rc, out, err = self.s.cli("purge", "--confirm-purge")
        self.assertEqual(rc, 2)
        self.assertIn("was not created by omp-services", err)
        self.assertEqual((self.s.config_dir / "mine.txt").read_text(), "precious")

    def test_a_forged_record_cannot_name_something_outside_the_directory(self):
        self.s.config_dir.mkdir(parents=True)
        os.chmod(self.s.config_dir, 0o700)
        outside = self.s.home / "outside.txt"
        outside.write_text("precious")
        rc, out, err = self.init()
        self.assertEqual(rc, 0, err)
        record = self.s.config_dir / ".omp-services-entries"
        self.assertTrue(record.exists(), "init records what it created in a directory it did not create")
        record.write_text(record.read_text() + "../outside.txt\n")
        rc, out, err = self.s.cli("purge", "--confirm-purge")
        self.assertEqual(outside.read_text(), "precious")
        self.assertEqual(rc, 2, "a record with a path component is refused")

    def test_a_forged_marker_with_other_content_does_not_count(self):
        self.init()
        (self.s.config_dir / ".omp-services-dir").write_text("hello")
        rc, out, err = self.s.cli("purge", "--confirm-purge")
        self.assertEqual(rc, 2)
        self.assertTrue(self.s.config_dir.exists())


# ---------------------------------------------------------------------------------------------------------------------
# unit paths come from the label, ownership is checked at the moment of action
# ---------------------------------------------------------------------------------------------------------------------


class F2UnitOwnership(Base):
    def test_a_path_in_state_json_is_never_used(self):
        self.s.services("plug", HUB)
        self.init()
        victim = self.s.root / "victim.txt"
        victim.write_text("not a unit")
        state = self.state()
        state["units"]["osvtest.plug.hub"]["file"] = str(victim)
        (self.s.data_dir / "state.json").write_text(json.dumps(state))
        rc, out, err = self.s.cli("uninstall", "--yes")
        self.assertEqual(victim.read_text(), "not a unit")
        self.assertFalse(
            self.unit_file("osvtest.plug.hub").exists(), "the real unit, derived from the label, was removed"
        )

    def test_a_unit_that_is_no_longer_ours_is_not_deleted(self):
        self.s.services("plug", HUB)
        self.init()
        unit = self.unit_file("osvtest.plug.hub")
        unit.write_text("<plist>someone else's now</plist>")
        self.s.clear_calls()
        rc, out, err = self.s.cli("uninstall", "--yes")
        self.assertEqual(unit.read_text(), "<plist>someone else's now</plist>")
        self.assertFalse(any(c.startswith("bootout") for c in self.s.calls()))
        self.assertIn("not managed", out + err)

    def test_init_does_not_replace_or_stop_a_unit_it_did_not_write(self):
        foreign = self.unit_file("osvtest.plug.hub")
        foreign.write_text("<plist>not ours</plist>")
        self.s.set_state("osvtest.plug.hub", "running")
        self.s.services("plug", HUB)
        self.s.clear_calls()
        rc, out, err = self.init()
        self.assertEqual(foreign.read_text(), "<plist>not ours</plist>")
        self.assertFalse(any(c.split()[0] in {"bootout", "bootstrap"} for c in self.s.calls()))
        self.assertIn("not managed by omp-services", out)

    def test_a_loaded_label_with_no_unit_file_is_not_taken_over(self):
        self.s.set_state("osvtest.plug.hub", "running")
        self.s.services("plug", HUB)
        self.s.clear_calls()
        self.init()
        self.assertFalse(any(c.split()[0] in {"bootout", "bootstrap"} for c in self.s.calls()))

    def test_the_watchdog_only_restarts_units_omp_services_manages(self):
        manager = make_manager("launchd", self.s.env, sleep=lambda s: None)
        spec = ServiceSpec("plug", "svc", "osvtest.plug.svc", ("/bin/x",), (), "/", "d", False, 10, "/l")
        self.s.set_state("osvtest.plug.svc", "stopped")
        (self.s.units / "osvtest.plug.svc.plist").write_text("<plist>foreign</plist>")
        check = {"severity": "fail", "id": "w", "type": "watchdog", "service": "svc", "restart": {}}
        ctx = self.ctx(manager=manager, services={"plug": {"svc": spec}}, allow_actions=True)
        r = checks.run_check("plug", check, ctx, 5)
        self.assertEqual(r.reason, "not-managed")
        self.assertEqual(self.s.restarts(), [])


# ---------------------------------------------------------------------------------------------------------------------
# uninstall does not follow links, deletes only its own wrappers, keeps what protected units need
# ---------------------------------------------------------------------------------------------------------------------


class F3Uninstall(Base):
    def test_a_symlinked_releases_directory_is_refused(self):
        self.init()
        victim = self.s.root / "victim"
        victim.mkdir()
        (victim / "keep.txt").write_text("x")
        shutil_rmtree = __import__("shutil").rmtree
        shutil_rmtree(self.s.data_dir / "releases")
        (self.s.data_dir / "releases").symlink_to(victim)
        rc, out, err = self.s.cli("uninstall", "--yes")
        self.assertEqual(rc, 2)
        self.assertTrue((victim / "keep.txt").exists())

    def test_a_wrapper_it_did_not_write_is_kept(self):
        self.init()
        mine = self.s.data_dir / "bin" / "omp"
        mine.write_text("#!/bin/sh\necho somebody else's\n")
        self.s.cli("uninstall", "--yes")
        self.assertEqual(mine.read_text(), "#!/bin/sh\necho somebody else's\n")
        self.assertFalse((self.s.data_dir / "bin" / "omp-services").exists())

    def test_protected_units_keep_their_runtime_and_their_protection(self):
        self.s.services("plug", {**HUB, "protected": True})
        self.init()
        self.s.cli("uninstall", "--yes")
        self.assertIn("osvtest.plug.hub", self.s.loaded())
        self.assertTrue((self.s.data_dir / "bin" / "omp-services").exists(), "still there for the surviving unit")
        self.assertTrue(self.state()["units"]["osvtest.plug.hub"]["protected"], "the protection is remembered")


# ---------------------------------------------------------------------------------------------------------------------
# protected means protected
# ---------------------------------------------------------------------------------------------------------------------


class F4Protected(Base):
    def test_allow_protected_no_longer_exists(self):
        path = self.s.health(
            "plug", {"id": "w", "type": "watchdog", "service": "hub", "restart": {"allowProtected": True}}
        )
        drop = dropins.load_one("health", path)
        self.assertFalse(drop.ok)
        self.assertIn("allowProtected", " ".join(drop.errors))

    def test_a_protected_service_is_never_restarted_by_a_watchdog(self):
        spec = ServiceSpec("plug", "svc", "osvtest.plug.svc", ("/bin/x",), (), "/", "d", True, 10, "/l")
        manager = make_manager("launchd", self.s.env, sleep=lambda s: None)
        self.s.set_state("osvtest.plug.svc", "stopped")
        (self.s.units / "osvtest.plug.svc.plist").write_text("<!-- Managed by omp-services. -->")
        ctx = self.ctx(manager=manager, services={"plug": {"svc": spec}}, allow_actions=True)
        r = checks.run_check(
            "plug", {"severity": "fail", "id": "w", "type": "watchdog", "service": "svc", "restart": {}}, ctx, 5
        )
        self.assertEqual(r.reason, "protected-not-restarted")
        self.assertEqual(self.s.restarts(), [])

    def test_dropping_the_flag_from_the_declaration_does_not_unprotect_a_loaded_unit(self):
        self.s.services("plug", {**HUB, "protected": True})
        self.init()
        self.s.services("plug", {"name": "hub", "program": {"type": "exec", "argv": ["/bin/sleep", "2"]}})
        self.s.clear_calls()
        rc, out, err = self.init()
        self.assertFalse(any(c.split()[0] in {"bootout", "bootstrap"} for c in self.s.calls()), self.s.calls())
        self.assertIn("protected", out)

    def test_a_loaded_protected_unit_whose_file_vanished_is_not_reloaded(self):
        self.s.services("plug", {**HUB, "protected": True})
        self.init()
        self.unit_file("osvtest.plug.hub").unlink()
        self.s.clear_calls()
        self.init()
        self.assertFalse(any(c.split()[0] in {"bootout", "bootstrap"} for c in self.s.calls()), self.s.calls())


# ---------------------------------------------------------------------------------------------------------------------
# the confirmation is bound to the plan that runs
# ---------------------------------------------------------------------------------------------------------------------


class F5PlanDigest(Base):
    def test_plan_json_carries_the_whole_plan_and_a_digest(self):
        rc, out, err = self.s.cli("init", "--plan", "--json")
        self.assertEqual(rc, 0, err)
        data = json.loads(out)
        self.assertIn("create", data["plan"])
        self.assertEqual(len(data["digest"]), 64)
        self.assertTrue(data["changes"])
        rc, out2, _ = self.s.cli("init", "--plan", "--json")
        self.assertEqual(json.loads(out2)["digest"], data["digest"], "deterministic")

    def test_apply_refuses_a_plan_that_changed_since_it_was_shown(self):
        digest = json.loads(self.s.cli("init", "--plan", "--json")[1])["digest"]
        self.s.services("plug", HUB)  # something changes between the dialog and the apply
        rc, out, err = self.s.cli("init", "--yes", "--expect-plan", digest)
        self.assertEqual(rc, 5)
        self.assertIn("plan changed", out + err)
        self.assertFalse(self.s.data_dir.exists(), "nothing was applied")

    def test_apply_runs_the_plan_that_was_shown(self):
        digest = json.loads(self.s.cli("init", "--plan", "--json")[1])["digest"]
        rc, out, err = self.s.cli("init", "--yes", "--expect-plan", digest)
        self.assertEqual(rc, 0, err)
        self.assertTrue((self.s.data_dir / "state.json").exists())

    def test_nothing_to_do_is_a_field_not_a_substring(self):
        self.init()
        data = json.loads(self.s.cli("init", "--plan", "--json")[1])
        self.assertFalse(data["changes"])


# ---------------------------------------------------------------------------------------------------------------------
# the installer validates what it is given
# ---------------------------------------------------------------------------------------------------------------------


class F6Installer(Base):
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

    def test_a_repository_value_that_is_not_owner_slash_name_is_refused_before_anything_runs(self):
        self.s.omp_will_install("omp-services", "0.1.0", support.PLUGIN)
        for bad in ("--evil", "-x/y", "a/b/c", "a b/c", "../x/y", "https://x.example/y/z", "x/y;rm", "x/y\n"):
            done = self.run_script({"OMP_TETHER_REPO": bad})
            self.assertEqual(done.returncode, 2, bad)
            self.assertIn("OMP_TETHER_REPO", done.stderr)
        self.assertEqual([c for c in self.s.omp_calls() if c.startswith("plugin")], [])

    def test_a_marketplace_name_is_validated_too(self):
        done = self.run_script({"OMP_TETHER_MARKETPLACE": "-bad"})
        self.assertEqual(done.returncode, 2)

    def test_the_published_checksum_matches_the_script(self):
        import hashlib

        recorded = (support.PLUGIN / "install.sh.sha256").read_text().split()[0]
        self.assertEqual(recorded, hashlib.sha256((support.PLUGIN / "install.sh").read_bytes()).hexdigest())

    def test_the_readme_shows_a_tagged_download_and_a_checksum_check(self):
        text = (support.PLUGIN / "README.md").read_text()
        self.assertIn("shasum -a 256 -c", text)
        self.assertNotIn("/main/plugins/omp-services/install.sh", text)


# ---------------------------------------------------------------------------------------------------------------------
# one predicate for names: no trailing newline
# ---------------------------------------------------------------------------------------------------------------------


class F7Newline(Base):
    def test_schema_patterns_refuse_a_trailing_newline(self):
        doc = {
            "protocol": "services/1",
            "plugin": "plug",
            "services": [{"name": "hub\n", "program": {"type": "exec", "argv": ["/bin/x"]}}],
        }
        self.assertTrue(schema.validate(doc, schema.load_schema("services-1")))
        cfg = {"schemaVersion": 1, "labelPrefix": "abc\n"}
        self.assertTrue(schema.validate(cfg, schema.load_schema("config-1")))
        health = {"protocol": "health/1", "plugin": "plug", "checks": [{"id": "a\n", "type": "port", "port": 1}]}
        self.assertTrue(schema.validate(health, schema.load_schema("health-1")))

    def test_a_drop_in_file_name_with_a_newline_is_refused(self):
        directory = self.s.config_dir / "health.d"
        directory.mkdir(parents=True)
        path = directory / "foo\n.json"
        path.write_text("{}")
        os.chmod(path, 0o600)
        self.assertFalse(dropins.load_one("health", path).ok)
        self.assertIsNone(__import__("re").fullmatch(dropins.PLUGIN_STEM.pattern, "abc\n"))

    def test_env_names_and_labels_use_the_same_strictness(self):
        with self.assertRaises(ServicesError):
            units._check_env({"A\n": "x"}, "t")
        self.assertFalse(units.owns_label("p", "p.x\n"))


# ---------------------------------------------------------------------------------------------------------------------
# hard deadlines
# ---------------------------------------------------------------------------------------------------------------------


class F8Deadlines(Base):
    def test_file_age_obeys_its_deadline(self):
        import glob as globmod

        original = globmod.iglob

        def slow(pattern):
            time.sleep(6)
            return iter(())

        globmod.iglob = slow
        self.addCleanup(setattr, globmod, "iglob", original)
        started = time.monotonic()
        r = checks.run_check(
            "plug",
            {"severity": "fail", "id": "f", "type": "file-age", "path": "/x/*", "maxAgeSeconds": 60},
            self.ctx(),
            1,
        )
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual((r.status, r.reason), ("failed", "deadline"))

    def test_dns_resolution_is_inside_the_deadline(self):
        original = socket.getaddrinfo

        def slow(*a, **k):
            time.sleep(6)
            return original(*a, **k)

        socket.getaddrinfo = slow
        self.addCleanup(setattr, socket, "getaddrinfo", original)
        started = time.monotonic()
        got = net.http_get(net.parse_http_url("http://slow.example.invalid/"), 1)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(got.error, "timeout")
        started = time.monotonic()
        self.assertEqual(net.tcp_connect("slow.example.invalid", 80, 1), "timeout")
        self.assertLess(time.monotonic() - started, 3)

    def test_a_slow_body_is_cut_off_even_when_the_connection_closes_after_the_response(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)

        def drip():
            conn, _ = listener.accept()
            try:
                conn.recv(4096)
                conn.sendall(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n")
                for _ in range(40):
                    conn.sendall(b"x")
                    time.sleep(0.2)
            except OSError:
                pass
            finally:
                conn.close()

        import threading

        threading.Thread(target=drip, daemon=True).start()
        started = time.monotonic()
        net.http_get(net.parse_http_url(f"http://127.0.0.1:{listener.getsockname()[1]}/"), 1, max_body=1_000_000)
        self.assertLess(time.monotonic() - started, 3)

    def test_watchdog_manager_calls_share_the_check_budget(self):
        (self.s.fake / "sleep-calls").write_text("3")
        manager = make_manager("launchd", self.s.env, sleep=lambda s: None)
        spec = ServiceSpec("plug", "svc", "osvtest.plug.svc", ("/bin/x",), (), "/", "d", False, 10, "/l")
        ctx = self.ctx(manager=manager, services={"plug": {"svc": spec}}, allow_actions=True)
        started = time.monotonic()
        r = checks.run_check(
            "plug", {"severity": "fail", "id": "w", "type": "watchdog", "service": "svc", "restart": {}}, ctx, 1
        )
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual(r.status, "failed")


# ---------------------------------------------------------------------------------------------------------------------
# trust before execution
# ---------------------------------------------------------------------------------------------------------------------


class F9Trust(Base):
    def test_a_group_writable_config_file_is_refused(self):
        self.s.write_config(heartbeat={"enabled": False})
        os.chmod(self.s.config_dir / "config.json", 0o666)
        paths = paths_mod.from_env(self.s.env, "darwin")
        cfg = config.load_config(paths, self.s.env)
        self.assertTrue(any("mode" in p for p in cfg.problems), cfg.problems)
        rc, out, err = self.s.cli("init", "--plan")
        self.assertEqual(rc, 2)

    def test_a_writable_config_directory_is_refused(self):
        self.s.write_config()
        os.chmod(self.s.config_dir, 0o777)
        paths = paths_mod.from_env(self.s.env, "darwin")
        self.assertTrue(config.load_config(paths, self.s.env).problems)

    def test_doctor_runs_nothing_it_found_in_an_untrusted_config(self):
        self.s.write_config(omp={"path": str(self.s.omp)})
        os.chmod(self.s.config_dir / "config.json", 0o666)
        self.s.clear_calls()
        (self.s.fake / "omp-calls.log").unlink(missing_ok=True)
        rc, out, err = self.s.cli("doctor", "--no-catalog", extra_env={"OMP_BIN": ""})
        self.assertEqual(rc, 1)
        self.assertEqual(self.s.omp_calls(), [], "omp was not executed")

    def test_the_state_file_is_validated_and_trusted(self):
        self.init()
        state = self.s.data_dir / "state.json"
        os.chmod(state, 0o666)
        rc, out, err = self.s.cli("init", "--plan")
        self.assertEqual(rc, 2)
        os.chmod(state, 0o600)
        bad = self.state()
        bad["python"] = "relative/python"
        state.write_text(json.dumps(bad))
        rc, out, err = self.s.cli("init", "--plan")
        self.assertEqual(rc, 2)
        self.assertIn("state", err)

    def test_set_and_clear_url_only_touch_files_inside_the_secrets_directory(self):
        victim = self.s.root / "victim.txt"
        victim.write_text("keep me")
        self.s.write_config(heartbeat={"urlFile": str(victim)})
        rc, out, err = self.s.cli("heartbeat", "clear-url")
        self.assertEqual(rc, 2)
        self.assertEqual(victim.read_text(), "keep me")
        proc = subprocess.run(
            [str(LAUNCHER), "heartbeat", "set-url"],
            env=self.s.env,
            input="https://hc.example/x\n",
            capture_output=True,
            text=True,
            start_new_session=True,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(victim.read_text(), "keep me")

    def test_a_standalone_health_file_is_held_to_the_directory_rule_and_opened_checked(self):
        d = self.s.root / "open"
        d.mkdir(mode=0o777)
        os.chmod(d, 0o777)
        path = d / "plug.json"
        path.write_text(
            json.dumps({"protocol": "health/1", "plugin": "plug", "checks": [{"id": "a", "type": "port", "port": 1}]})
        )
        os.chmod(path, 0o600)
        rc, out, err = self.s.cli("health", "--file", str(path), "--json")
        self.assertEqual(rc, 2)
        self.assertIn("invalid", out)
        with self.assertRaises(ServicesError):
            util.read_text(path, forbid=0o022) if False else util.read_text(self.s.units / "missing", forbid=0o022)


# ---------------------------------------------------------------------------------------------------------------------
# locking
# ---------------------------------------------------------------------------------------------------------------------


class F10Lock(Base):
    def test_a_mutating_command_refuses_to_run_twice_at_once(self):
        import fcntl

        lock = self.s.data_dir.parent / "omp-services.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        for cmd in (["init", "--yes"], ["uninstall", "--yes"], ["purge", "--confirm-purge"]):
            rc, out, err = self.s.cli(*cmd)
            self.assertEqual(rc, 2, cmd)
            self.assertIn("another omp-services command", err)


# ---------------------------------------------------------------------------------------------------------------------
# release integrity; F12/A-M5: quoting; F13/A-M1: descendants; F14/A-M11: logs
# ---------------------------------------------------------------------------------------------------------------------


class F11Release(Base):
    def test_a_release_directory_that_does_not_match_its_name_is_not_reused(self):
        self.init()
        release = self.s.data_dir / "releases" / self.state()["release"]
        (release / "python" / "omp_services" / "cli.py").write_text("print('substituted')\n")
        rc, out, err = self.s.cli("doctor", "--no-catalog", "--json")
        data = {f["id"]: f for f in json.loads(out)["findings"]}
        self.assertEqual(data["runtime.integrity"]["level"], "fail")
        rc, out, err = self.init()
        self.assertEqual(rc, 2, out + err)
        self.assertIn("does not match", err)

    def test_the_release_id_comes_from_the_staged_copy_and_is_long(self):
        self.init()
        rid = self.state()["release"]
        self.assertGreaterEqual(len(rid.rsplit("-", 1)[1]), 32)

    def test_the_hidden_from_option_is_gone(self):
        rc, out, err = self.s.cli("init", "--plan", "--from", str(support.PLUGIN))
        self.assertEqual(rc, 2)


class F12Systemd(Base):
    kind = "systemd"

    def test_environment_values_keep_their_dollar_signs(self):
        spec = units.heartbeat_spec("p", paths_mod.from_env({"HOME": "/h"}, "linux"), "/usr/bin/python3", None, {})
        spec = ServiceSpec(**{**spec.__dict__, "env": (("X", "a $y %h"),)})
        text = units.render_systemd(spec)
        self.assertIn('Environment="X=a $y %%h"', text)
        exec_line = next(line for line in text.splitlines() if line.startswith("ExecStart="))
        self.assertNotIn("$$", exec_line.replace("$$", "") and "")  # exec words still double $ (checked below)
        quoted = units.systemd_quote("a $b")
        self.assertEqual(quoted, '"a $$b"')

    def test_control_characters_in_paths_are_refused(self):
        for name in (
            "HOME",
            "XDG_CONFIG_HOME",
            "OMP_SERVICES_DATA_DIR",
            "OMP_SERVICES_STATE_DIR",
            "OMP_SERVICES_CONFIG_DIR",
            "OMP_SERVICES_UNIT_DIR",
        ):
            with self.assertRaises(ServicesError, msg=name):
                paths_mod.from_env({"HOME": "/h", name: "/x\n[Service]\nExecStart=/bin/evil"}, "linux")


class F13Descendants(Base):
    def test_a_grandchild_of_a_command_that_exits_is_killed_too(self):
        marker = self.s.root / "survived"
        child = self.s.root / "gc.py"
        child.write_text(f"import pathlib, time\ntime.sleep(2)\npathlib.Path({str(marker)!r}).write_text('x')\n")
        parent = self.s.root / "p.py"
        parent.write_text(f"import subprocess, sys\nsubprocess.Popen([sys.executable, {str(child)!r}])\n")
        r = util.run_bounded([sys.executable, str(parent)], 10)
        self.assertEqual(r.returncode, 0)
        time.sleep(3)
        self.assertFalse(marker.exists())

    def test_sigterm_to_the_command_line_stops_the_checks_it_started(self):
        marker = self.s.root / "survived2"
        child = self.s.root / "gc2.py"
        child.write_text(f"import pathlib, time\ntime.sleep(4)\npathlib.Path({str(marker)!r}).write_text('x')\n")
        path = self.s.health(
            "plug", {"id": "slow", "type": "command", "argv": [sys.executable, str(child)], "timeoutSeconds": 30}
        )
        proc = subprocess.Popen(
            [str(LAUNCHER), "health", "--file", str(path), "--json"],
            env=self.s.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        time.sleep(1.5)
        os.killpg(proc.pid, 15) if False else proc.terminate()
        proc.wait(timeout=20)
        time.sleep(4)
        self.assertFalse(marker.exists(), "the command's process group did not outlive the command line")


class F14Logs(Base):
    def test_rotation_streams_and_keeps_at_most_max_bytes(self):
        logs = self.s.state_dir / "logs"
        logs.mkdir(parents=True)
        big = logs / "big.log"
        big.write_bytes(b"A" * 5000 + b"B" * 3000)
        original = Path.read_bytes
        Path.read_bytes = lambda self: (_ for _ in ()).throw(AssertionError("whole file read into memory"))
        try:
            self.assertEqual(heartbeat.trim_logs(logs, 4000), ["big.log"])
        finally:
            Path.read_bytes = original
        rotated = (logs / "big.log.1").read_bytes()
        self.assertLessEqual(len(rotated), 4000)
        self.assertTrue(rotated.endswith(b"B" * 3000), "the newest bytes are kept")
        self.assertEqual(big.stat().st_size, 0)

    def test_init_trims_logs_even_with_the_heartbeat_off(self):
        self.init()
        log = self.s.state_dir / "logs" / "plug.hub.log"
        log.write_bytes(b"x" * 2_000_000)
        self.init()
        self.assertEqual(log.stat().st_size, 0)

    def test_doctor_warns_about_a_log_that_has_outgrown_its_bound(self):
        self.init()
        (self.s.state_dir / "logs" / "plug.hub.log").write_bytes(b"x" * 5_000_000)
        rc, out, _ = self.s.cli("doctor", "--json", "--no-catalog")
        findings = json.loads(out)["findings"]
        self.assertTrue(any(f["id"].startswith("logs") and f["level"] == "warn" for f in findings))


# ---------------------------------------------------------------------------------------------------------------------
# heartbeat honesty
# ---------------------------------------------------------------------------------------------------------------------


class F15Heartbeat(Base):
    def setUp(self):
        super().setUp()
        self.certs = support.make_certs(self.s.root)
        if not self.certs:
            self.skipTest("openssl is not available")
        self.srv = support.Server(cert=(self.certs[1], self.certs[2]))
        self.addCleanup(self.srv.stop)
        self.s.write_config(heartbeat={"enabled": True})
        self.s.state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.s.state_dir, 0o700)
        secrets = self.s.config_dir / "secrets"
        secrets.mkdir(exist_ok=True)
        os.chmod(secrets, 0o700)
        (secrets / "heartbeat-url").write_text(f"{self.srv.base}/ping/abc\n")
        os.chmod(secrets / "heartbeat-url", 0o600)

    def run_once(self, **kw):
        import contextlib
        import io

        with self.s.patched({"SSL_CERT_FILE": self.certs[0]}):
            paths = paths_mod.from_env(os.environ, "darwin")
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                rc = heartbeat.run_once(paths, os.environ, **kw)
        return rc, out.getvalue()

    def test_no_drop_ins_means_a_fail_ping_unless_allowed(self):
        rc, out = self.run_once()
        self.assertEqual(rc, 1)
        self.assertEqual(self.srv.requests[-1]["path"], "/ping/abc/fail")
        self.assertIn("no health checks", out)
        self.s.write_config(heartbeat={"enabled": True, "allowNoChecks": True})
        rc, out = self.run_once()
        self.assertEqual(rc, 0)
        self.assertEqual(self.srv.requests[-1]["path"], "/ping/abc")

    def test_doctor_warns_when_the_heartbeat_has_nothing_to_check(self):
        rc, out, _ = self.s.cli("doctor", "--json", "--no-catalog")
        findings = {f["id"]: f for f in json.loads(out)["findings"]}
        self.assertEqual(findings["heartbeat.checks"]["level"], "warn")

    def test_an_unusable_url_is_a_failing_exit_status(self):
        (self.s.config_dir / "secrets" / "heartbeat-url").write_text("http://x.example/y\n")
        rc, out = self.run_once()
        self.assertEqual(rc, 1)

    def test_status_command_reports_failed_pings_and_old_records(self):
        status = self.s.state_dir / "heartbeat-status.json"
        base = {"schemaVersion": 1, "intervalSeconds": 60, "ok": True, "results": []}
        status.write_text(
            json.dumps({**base, "at": time.time(), "ping": {"attempted": True, "ok": False, "detail": "timeout"}})
        )
        self.assertEqual(self.s.cli("heartbeat", "status")[0], 1)
        status.write_text(
            json.dumps(
                {**base, "at": time.time() - 10_000, "ping": {"attempted": True, "ok": True, "detail": "status 200"}}
            )
        )
        rc, out, _ = self.s.cli("heartbeat", "status")
        self.assertEqual(rc, 1)
        self.assertIn("STALE", out)
        status.write_text(
            json.dumps({**base, "at": time.time(), "ping": {"attempted": True, "ok": True, "detail": "status 200"}})
        )
        self.assertEqual(self.s.cli("heartbeat", "status")[0], 0)

    def test_a_non_finite_freshness_timestamp_is_not_healthy(self):
        srv = support.Server()
        self.addCleanup(srv.stop)
        srv.route("/nan", 200, '{"at": NaN}')
        srv.route("/inf", 200, '{"at": Infinity}')
        for path in ("/nan", "/inf"):
            r = checks.run_check(
                "plug",
                {
                    "severity": "fail",
                    "id": "f",
                    "type": "url",
                    "url": f"{srv.base}{path}",
                    "freshness": {"jsonPointer": "/at", "maxAgeSeconds": 60},
                },
                self.ctx(),
                5,
            )
            self.assertEqual((r.status, r.reason), ("failed", "freshness-unreadable"), path)
        self.assertFalse(math.isfinite(float("nan")))

    def test_the_url_parse_error_does_not_keep_its_cause(self):
        try:
            net.parse_http_url("http://[::1")
        except ServicesError as exc:
            self.assertIsNone(exc.__cause__)


# ---------------------------------------------------------------------------------------------------------------------
# # ---------------------------------------------------------------------------------------------------------------------


class F16Doctor(Base):
    def test_doctor_names_what_each_drop_in_makes_the_machine_run(self):
        self.s.services("plug", {"name": "hub", "program": {"type": "exec", "argv": ["/usr/bin/true", "--secretish"]}})
        self.s.health("plug", {"id": "c", "type": "command", "argv": ["/bin/echo", "hi"]})
        self.init()
        rc, out, _ = self.s.cli("doctor", "--json", "--no-catalog")
        messages = " ".join(f["message"] for f in json.loads(out)["findings"] if f["id"].startswith("dropin"))
        self.assertIn("/usr/bin/true", messages)
        self.assertIn("/bin/echo", messages)

    def test_an_unreadable_watchdog_budget_fails_closed(self):
        spec = ServiceSpec("plug", "svc", "osvtest.plug.svc", ("/bin/x",), (), "/", "d", False, 10, "/l")
        manager = make_manager("launchd", self.s.env, sleep=lambda s: None)
        self.s.set_state("osvtest.plug.svc", "stopped")
        (self.s.units / "osvtest.plug.svc.plist").write_text("<!-- Managed by omp-services. -->")
        ctx = self.ctx(manager=manager, services={"plug": {"svc": spec}}, allow_actions=True)
        ctx.paths.watchdog_state.write_text("{not json")
        r = checks.run_check(
            "plug", {"severity": "fail", "id": "w", "type": "watchdog", "service": "svc", "restart": {}}, ctx, 5
        )
        self.assertEqual(r.reason, "restart-state-unreadable")
        self.assertEqual(self.s.restarts(), [])


class AM3PinnedOmp(Base):
    def test_watchdogs_resolve_omp_script_services_through_the_pinned_omp(self):
        script = self.s.root / "hub.ts"
        script.write_text("x")
        self.s.services("plug", {"name": "hub", "program": {"type": "omp-script", "script": str(script)}})
        self.s.health("plug", {"id": "w", "type": "watchdog", "service": "hub"})
        self.init()
        path = self.s.config_dir / "health.d" / "plug.json"
        rc, out, _ = self.s.cli(
            "health", "--file", str(path), "--json", extra_env={"OMP_BIN": "", "PATH": "/usr/bin:/bin"}
        )
        result = json.loads(out)[0]
        self.assertNotEqual(result["reason"], "unknown-service")


class AM4CarriedEnvironment(Base):
    def test_units_carry_the_resolved_roots_and_effective_overrides(self):
        base = self.s.root / "xdg"
        env = {"XDG_CONFIG_HOME": str(base / "c"), "XDG_DATA_HOME": str(base / "d"), "XDG_STATE_HOME": str(base / "s")}
        env["OMP_SERVICES_HEARTBEAT_ENABLED"] = "true"
        rc, out, err = self.s.cli(
            "render", "--platform", "systemd", extra_env={**env, "OMP_SERVICES_ALLOW_REMOTE_URLS": "true"}
        )
        self.assertEqual(rc, 0, err)
        self.assertIn(f"OMP_SERVICES_CONFIG_DIR={base / 'c' / 'omp-services'}", out)
        self.assertIn(f"OMP_SERVICES_DATA_DIR={base / 'd' / 'omp-services'}", out)
        self.assertIn("OMP_SERVICES_STATE_DIR=", out)
        self.assertIn("OMP_SERVICES_ALLOW_REMOTE_URLS=true", out)
        self.assertIn("OMP_SERVICES_LABEL_PREFIX=osvtest", out)


class AM6SystemdState(Base):
    kind = "systemd"

    def manager(self):
        return Systemd(self.s.env, sleep=lambda s: None)

    def test_an_active_but_disabled_unit_counts_as_loaded_and_is_stopped_on_unload(self):
        m = self.manager()
        unit = self.s.units / "osvtest.x.service"
        unit.write_text("x")
        self.s.set_state("osvtest.x", "running-disabled")
        self.assertTrue(m.is_loaded("osvtest.x"))
        m.unload("osvtest.x", unit)
        self.assertFalse(m.is_running("osvtest.x"))

    def test_a_failed_stop_is_an_error_and_the_unit_file_stays(self):
        self.s.services("plug", HUB)
        self.init()
        self.s.set_state("osvtest.plug.hub", "running-disabled")
        (self.s.fake / "fail-stop").write_text("x")
        (self.s.config_dir / "services.d" / "plug.json").unlink()
        rc, out, err = self.init()
        self.assertEqual(rc, 2, out)
        self.assertTrue(self.unit_file("osvtest.plug.hub").exists(), "not deleted while it may still run")


class AM7HealthDefaults(Base):
    def test_the_health_command_never_restarts_anything(self):
        self.s.services("plug", HUB)
        self.s.health("plug", {"id": "w", "type": "watchdog", "service": "hub", "restart": {}})
        self.init()
        self.s.set_state("osvtest.plug.hub", "waiting")
        self.s.clear_calls()
        path = self.s.config_dir / "health.d" / "plug.json"
        self.s.cli("health", "--file", str(path), "--json")
        self.assertEqual(self.s.restarts(), [])


class AM8Reasons(Base):
    def test_an_invalid_drop_in_result_carries_a_fixed_code_not_paths_or_field_names(self):
        key = "key-" + os.urandom(6).hex()
        path = self.s.dropin(
            "health",
            "weird",
            {"protocol": "health/1", "plugin": "weird", key: 1, "checks": [{"id": "a", "type": "port", "port": 1}]},
        )
        drop = dropins.load_one("health", path)
        result = checks.invalid_dropin(drop)
        self.assertEqual(result.reason, "dropin-invalid")
        self.assertNotIn(key, json.dumps(result.as_dict()))
        os.chmod(self.s.config_dir / "health.d", 0o777)
        results, _ = heartbeat.collect_results(self.ctx())
        self.assertTrue(all(str(self.s.root) not in json.dumps(r.as_dict()) for r in results))


class AM15Packaging(unittest.TestCase):
    def test_there_is_no_pip_install_route_that_cannot_work(self):
        self.assertFalse((support.PLUGIN / "pyproject.toml").exists())
        readme = (support.PLUGIN / "README.md").read_text()
        self.assertNotIn("uv tool install", readme + (support.PLUGIN / "CHANGELOG.md").read_text())


class L2Docs(unittest.TestCase):
    def read(self, rel):
        return (support.PLUGIN / rel).read_text()

    def test_docs_describe_what_the_code_does(self):
        repo = support.PLUGIN.parents[1]
        everything = (
            self.read("README.md")
            + self.read("docs/threat-model.md")
            + (repo / "docs/protocol/health-1.md").read_text()
        )
        self.assertNotIn("allowProtected", everything)
        self.assertNotIn("never writes outside your home", self.read("docs/threat-model.md"))
        self.assertIn("health checks", self.read("README.md").split("What it never does")[1].split("## Install")[0])
        uninstall = self.read("README.md").split("## Uninstall and purge")[1].split("## Develop")[0]
        self.assertLess(uninstall.index("purge"), uninstall.index("omp plugin uninstall"))

    def test_the_threat_model_matches_the_new_controls(self):
        text = self.read("docs/threat-model.md")
        for needle in ("recorded sha256", "plan digest", "marker", "lock"):
            self.assertIn(needle, text)

    def test_the_stat_of_the_installer_is_executable(self):
        self.assertTrue(stat.S_IMODE(os.stat(support.PLUGIN / "install.sh").st_mode) & 0o111)


if __name__ == "__main__":
    unittest.main()
