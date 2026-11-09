"""Drop-in loading rules, and the launchd and systemd managers against recording fakes."""

from __future__ import annotations

import os
import unittest

import support
from omp_services import dropins
from omp_services.manager import Launchd, Systemd
from omp_services.util import ServicesError

GOOD_CHECK = {"id": "a", "type": "port", "port": 8080}


class DropinTests(support.Case):
    def test_a_valid_drop_in(self):
        path = self.s.health("plug-in", GOOD_CHECK)
        drop = dropins.load_one("health", path)
        self.assertTrue(drop.ok, drop.errors)
        self.assertEqual(drop.plugin, "plug-in")

    def test_every_refusal_is_reported_with_a_reason(self):
        cases = {
            "writable by others": (lambda: self.s.health("aa", GOOD_CHECK, mode=0o666), "cannot write"),
            "wrong plugin": (
                lambda: self.s.dropin("health", "bb", {"protocol": "health/1", "plugin": "cc", "checks": [GOOD_CHECK]}),
                "must match",
            ),
            "bad json": (lambda: self.s.dropin("health", "dd", "{"), "not valid JSON"),
            "unknown field": (
                lambda: self.s.dropin(
                    "health", "ee", {"protocol": "health/1", "plugin": "ee", "checks": [GOOD_CHECK], "x": 1}
                ),
                "unknown field",
            ),
            "bad type": (
                lambda: self.s.dropin(
                    "health", "ff", {"protocol": "health/1", "plugin": "ff", "checks": [{"id": "a", "type": "magic"}]}
                ),
                "unknown type",
            ),
            "duplicate ids": (
                lambda: self.s.dropin(
                    "health", "gg", {"protocol": "health/1", "plugin": "gg", "checks": [GOOD_CHECK, GOOD_CHECK]}
                ),
                "duplicate",
            ),
            "too big": (lambda: self.s.dropin("health", "hh", " " * 70000), "larger than"),
        }
        for label, (make, needle) in cases.items():
            with self.subTest(label):
                drop = dropins.load_one("health", make())
                self.assertFalse(drop.ok)
                self.assertIn(needle, " ".join(drop.errors), drop.errors)

    def test_a_symlink_is_refused(self):
        real = self.s.root / "real.json"
        real.write_text("{}")
        os.chmod(real, 0o600)
        self.s.health("zz", GOOD_CHECK)
        link = self.s.config_dir / "health.d" / "link.json"
        link.symlink_to(real)
        drop = dropins.load_one("health", link)
        self.assertFalse(drop.ok)
        self.assertIn("symbolic link", " ".join(drop.errors))

    def test_a_file_name_that_is_not_a_plugin_name(self):
        directory = self.s.config_dir / "health.d"
        directory.mkdir(parents=True)
        path = directory / "Bad_Name.json"
        path.write_text("{}")
        os.chmod(path, 0o600)
        self.assertFalse(dropins.load_one("health", path).ok)

    def test_a_writable_directory_disables_its_drop_ins(self):
        self.s.health("aa", GOOD_CHECK)
        os.chmod(self.s.config_dir / "health.d", 0o777)
        drops, problems = dropins.load_dir("health", self.s.config_dir / "health.d")
        self.assertEqual(drops, [])
        self.assertIn("not used", problems[0])

    def test_load_dir_ignores_temporary_files_and_sorts(self):
        self.s.health("bb", GOOD_CHECK)
        self.s.health("aa", GOOD_CHECK)
        d = self.s.config_dir / "health.d"
        (d / ".aa.json.tmp").write_text("x")
        (d / "notes.txt").write_text("x")
        drops, problems = dropins.load_dir("health", d)
        self.assertEqual([x.plugin for x in drops], ["aa", "bb"])
        self.assertEqual(problems, [])
        self.assertEqual(dropins.load_dir("health", self.s.root / "missing"), ([], []))

    def test_services_drop_ins_use_their_own_schema(self):
        path = self.s.services("plug", {"name": "hub", "program": {"type": "exec", "argv": ["/bin/true"]}})
        self.assertTrue(dropins.load_one("services", path).ok)
        self.assertFalse(dropins.load_one("health", path).ok)


class LaunchdTests(support.Case):
    def manager(self) -> Launchd:
        return Launchd(self.s.env, sleep=lambda s: None)

    def test_load_unload_and_state(self):
        m = self.manager()
        plist = self.s.units / "osvtest.x.plist"
        plist.write_text("<plist><dict><key>Label</key><string>osvtest.x</string></dict></plist>")
        self.assertFalse(m.is_loaded("osvtest.x"))
        m.load("osvtest.x", plist)
        self.assertTrue(m.is_loaded("osvtest.x"))
        self.assertTrue(m.is_running("osvtest.x"))
        self.s.set_state("osvtest.x", "waiting")
        self.assertFalse(m.is_running("osvtest.x"))
        m.unload("osvtest.x", plist)
        self.assertFalse(m.is_loaded("osvtest.x"))
        m.unload("osvtest.x", plist)  # not loaded: nothing to do, no error
        self.assertEqual(sum(1 for c in self.s.calls() if c.startswith("bootout")), 1)

    def test_a_failing_bootstrap_is_retried_then_reported(self):
        (self.s.fake / "fail-bootstrap").write_text("x")
        plist = self.s.units / "osvtest.y.plist"
        plist.write_text("x")
        with self.assertRaises(ServicesError) as ctx:
            self.manager().load("osvtest.y", plist)
        self.assertIn("could not load osvtest.y", str(ctx.exception))
        self.assertEqual(sum(1 for c in self.s.calls() if c.startswith("bootstrap")), 5)

    def test_problem_and_restart(self):
        m = self.manager()
        self.assertIsNone(m.problem())
        with self.assertRaises(ServicesError):
            m.restart("osvtest.nothing")
        self.s.set_state("osvtest.z", "stopped")
        m.restart("osvtest.z")
        self.assertEqual(self.s.restarts()[-1], "osvtest.z")
        bad = Launchd({**self.s.env, "OMP_SERVICES_LAUNCHCTL": "/nonexistent/launchctl"})
        self.assertIn("not available", bad.problem())


class SystemdTests(support.Case):
    kind = "systemd"

    def manager(self) -> Systemd:
        return Systemd(self.s.env, sleep=lambda s: None)

    def test_load_unload_restart(self):
        m = self.manager()
        unit = self.s.units / "osvtest.x.service"
        unit.write_text("x")
        m.load("osvtest.x", unit)
        self.assertEqual(self.s.calls()[-2:], ["--user daemon-reload", "--user enable --now osvtest.x.service"])
        self.assertTrue(m.is_loaded("osvtest.x") and m.is_running("osvtest.x"))
        m.restart("osvtest.x")
        self.assertEqual(self.s.restarts(), ["osvtest.x"])
        m.unload("osvtest.x", unit)
        self.assertFalse(m.is_loaded("osvtest.x"))

    def test_linger_note_and_problem(self):
        m = self.manager()
        self.assertIsNone(m.problem())
        self.assertIn("loginctl enable-linger", m.linger_note() or "")
        self.assertFalse(m.lingering())
        self.assertIsNotNone(Systemd({**self.s.env, "OMP_SERVICES_SYSTEMCTL": "/nonexistent/systemctl"}).problem())


if __name__ == "__main__":
    unittest.main()
