"""init, uninstall and purge: plans, idempotence, the reload rules, the label allowlist, data that must survive."""

from __future__ import annotations

import json
import os
import plistlib
import unittest

import support

HUB = {"name": "hub", "program": {"type": "omp-script", "script": "/opt/x/hub.ts"}}


class InitCase(support.Case):
    def init(self, *args, **kw):
        return self.s.cli("init", "--yes", *args, **kw)

    def unit_file(self, label: str):
        return self.s.units / (label + (".plist" if self.kind == "launchd" else ".service"))


class InitTests(InitCase):
    def test_plan_only_changes_nothing(self):
        self.s.write_config(heartbeat={"enabled": True})
        rc, out, err = self.s.cli("init", "--plan")
        self.assertEqual(rc, 0, err)
        self.assertIn("create", out)
        self.assertIn("osvtest.heartbeat", out)
        self.assertFalse(self.s.data_dir.exists())
        self.assertEqual(self.s.loaded(), set())
        self.assertEqual(list(self.s.units.iterdir()), [])

    def test_without_a_yes_and_without_a_terminal_nothing_happens(self):
        rc, out, _ = self.s.cli("init")
        self.assertEqual(rc, 3)
        self.assertIn("not confirmed", out)
        self.assertFalse(self.s.data_dir.exists())
        rc, out, _ = self.s.cli("init", answers=["n"])
        self.assertEqual(rc, 3)
        rc, out, _ = self.s.cli("init", answers=["y"])
        self.assertEqual(rc, 0)
        self.assertTrue(self.s.data_dir.exists())

    def test_a_fresh_init_installs_only_what_is_asked_for(self):
        rc, out, err = self.init()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.s.loaded(), set(), "no heartbeat, no services: no unit")
        self.assertEqual(list(self.s.units.iterdir()), [])
        for d in (
            self.s.config_dir,
            self.s.data_dir,
            self.s.state_dir,
            self.s.config_dir / "secrets",
            self.s.state_dir / "logs",
        ):
            self.assertEqual(support.mode_of(d), 0o700, d)
        self.assertEqual(support.mode_of(self.s.config_dir / "config.json"), 0o600)
        self.assertTrue((self.s.data_dir / "current" / "python" / "omp_services_main.py").is_file())
        for name in ("omp-services", "omp", "bun"):
            self.assertEqual(support.mode_of(self.s.data_dir / "bin" / name), 0o700)
        state = json.loads((self.s.data_dir / "state.json").read_text())
        self.assertEqual(state["omp"]["path"], str(self.s.omp))
        self.assertEqual(state["omp"]["version"], "18.18.0")

    def test_wrappers_pin_absolute_paths(self):
        self.init()
        wrapper = (self.s.data_dir / "bin" / "omp-services").read_text()
        self.assertIn(
            os.path.realpath(__import__("sys").executable) if False else __import__("sys").executable, wrapper
        )
        self.assertIn(str(self.s.data_dir / "current" / "python" / "omp_services_main.py"), wrapper)
        self.assertIn(str(self.s.omp), (self.s.data_dir / "bin" / "omp").read_text())
        self.assertIn("unset BUN_BE_BUN", (self.s.data_dir / "bin" / "omp").read_text())
        bun = (self.s.data_dir / "bin" / "bun").read_text()
        self.assertIn("BUN_BE_BUN=1", bun)
        done = self.s.run_process([str(self.s.data_dir / "bin" / "omp-services"), "--version"])
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("omp-services 0.1.1", done.stdout)

    def test_second_run_changes_nothing_and_leaves_units_running(self):
        self.s.write_config(heartbeat={"enabled": True})
        self.s.services("plug", HUB)
        self.init()
        self.assertEqual(self.s.loaded(), {"osvtest.heartbeat", "osvtest.plug.hub"})
        self.s.clear_calls()
        before = {p.name: p.read_text() for p in self.s.units.iterdir()}
        rc, out, _ = self.init()
        self.assertEqual(rc, 0)
        self.assertIn("Nothing to do", out)
        self.assertEqual([c for c in self.s.calls() if c.split()[0] in {"bootstrap", "bootout", "kickstart"}], [])
        self.assertEqual({p.name: p.read_text() for p in self.s.units.iterdir()}, before)

    def test_only_the_changed_unit_is_reloaded(self):
        self.s.services("plug", HUB, {"name": "other", "program": {"type": "exec", "argv": ["/bin/true"]}})
        self.init()
        self.s.clear_calls()
        self.s.services(
            "plug",
            {**HUB, "program": {"type": "omp-script", "script": "/opt/x/hub.ts", "args": ["--new"]}},
            {"name": "other", "program": {"type": "exec", "argv": ["/bin/true"]}},
        )
        rc, out, _ = self.init()
        self.assertEqual(rc, 0)
        verbs = [c.split()[0] for c in self.s.calls() if c.split()[0] in {"bootstrap", "bootout"}]
        self.assertEqual(verbs, ["bootout", "bootstrap"], self.s.calls())
        self.assertIn("osvtest.plug.hub", " ".join(self.s.calls()))
        self.assertNotIn(
            "osvtest.plug.other", " ".join(c for c in self.s.calls() if c.split()[0] in {"bootstrap", "bootout"})
        )
        self.assertIn("--new", self.unit_file("osvtest.plug.hub").read_text())

    def test_a_protected_unit_is_never_reloaded_unless_named(self):
        self.s.services("plug", {**HUB, "protected": True})
        self.init()
        self.s.clear_calls()
        changed = {
            **HUB,
            "protected": True,
            "program": {"type": "omp-script", "script": "/opt/x/hub.ts", "args": ["--v2"]},
        }
        self.s.services("plug", changed)
        old_text = self.unit_file("osvtest.plug.hub").read_text()
        rc, out, _ = self.init()
        self.assertEqual(rc, 0)
        self.assertIn("is protected", out)
        self.assertEqual([c for c in self.s.calls() if c.split()[0] in {"bootstrap", "bootout"}], [])
        self.assertEqual(self.unit_file("osvtest.plug.hub").read_text(), old_text, "not even rewritten")
        self.s.clear_calls()
        rc, out, _ = self.init("--restart-protected", "plug.hub")
        self.assertEqual(
            [c.split()[0] for c in self.s.calls() if c.split()[0] in {"bootstrap", "bootout"}], ["bootout", "bootstrap"]
        )
        self.assertIn("--v2", self.unit_file("osvtest.plug.hub").read_text())

    def test_a_protected_unit_that_is_no_longer_wanted_stays(self):
        self.s.services("plug", {**HUB, "protected": True})
        self.init()
        (self.s.config_dir / "services.d" / "plug.json").unlink()
        rc, out, _ = self.init()
        self.assertIn("protected", out)
        self.assertIn("osvtest.plug.hub", self.s.loaded())
        rc, out, _ = self.init("--restart-protected", "plug.hub")
        self.assertNotIn("osvtest.plug.hub", self.s.loaded())

    def test_an_unwanted_unit_is_removed_and_foreign_units_are_never_touched(self):
        foreign = self.unit_file("com.example.other")
        foreign.write_text("<plist>not ours</plist>")
        lookalike = self.unit_file("osvtest-lookalike")
        lookalike.write_text("<plist>not ours either</plist>")
        self.s.set_state("com.example.other", "running")
        self.s.services("plug", HUB)
        self.init()
        (self.s.config_dir / "services.d" / "plug.json").unlink()
        self.s.clear_calls()
        self.init()
        self.assertNotIn("osvtest.plug.hub", self.s.loaded())
        self.assertFalse(self.unit_file("osvtest.plug.hub").exists())
        self.assertEqual(foreign.read_text(), "<plist>not ours</plist>")
        self.assertTrue(lookalike.exists())
        self.assertIn("com.example.other", self.s.loaded())
        self.assertFalse(any("com.example.other" in c for c in self.s.calls() if "boot" in c or "kick" in c))

    def test_an_invalid_services_drop_in_is_reported_and_not_installed(self):
        self.s.dropin("services", "broken", "{oops")
        self.s.services("good", {"name": "ok", "program": {"type": "exec", "argv": ["/bin/true"]}})
        rc, out, _ = self.init()
        self.assertEqual(rc, 0)
        self.assertIn("warning: services.d/broken.json is not used", out)
        self.assertEqual(self.s.loaded(), {"osvtest.good.ok"})

    def test_an_omp_script_without_omp_is_a_warning_not_a_crash(self):
        self.s.services("plug", HUB)
        self.s.services("other", {"name": "plain", "program": {"type": "exec", "argv": ["/bin/true"]}})
        rc, out, _ = self.init(extra_env={"OMP_BIN": "/nonexistent/omp", "PATH": "/usr/bin:/bin"})
        self.assertEqual(rc, 0)
        self.assertIn("no omp binary was found", out)
        self.assertIn("its services are not installed", out)
        self.assertEqual(self.s.loaded(), {"osvtest.other.plain"})

    def test_a_loose_directory_mode_is_tightened_and_a_symlink_is_refused(self):
        self.init()
        os.chmod(self.s.config_dir / "secrets", 0o755)
        rc, out, _ = self.init()
        self.assertIn("tighten", out)
        self.assertEqual(support.mode_of(self.s.config_dir / "secrets"), 0o700)
        (self.s.config_dir / "health.d").rmdir()
        elsewhere = self.s.root / "elsewhere"
        elsewhere.mkdir()
        (self.s.config_dir / "health.d").symlink_to(elsewhere)
        rc, out, err = self.init()
        self.assertEqual(rc, 2)
        self.assertIn("symbolic link", err)

    def test_the_pinned_omp_survives_a_different_omp_appearing_first_on_path(self):
        self.init()
        other_dir = self.s.root / "other"
        other_dir.mkdir()
        other = other_dir / "omp"
        other.write_text(self.s.omp.read_text())
        other.chmod(0o755)
        self.init(extra_env={"OMP_BIN": str(other), "PATH": f"{other_dir}:/usr/bin:/bin"})
        state = json.loads((self.s.data_dir / "state.json").read_text())
        self.assertEqual(state["omp"]["path"], str(self.s.omp), "pinned, not re-detected")
        rc, out, _ = self.init("--repin", extra_env={"OMP_BIN": str(other)})
        state = json.loads((self.s.data_dir / "state.json").read_text())
        self.assertEqual(state["omp"]["path"], str(other))


class LaunchdRenderingTests(InitCase):
    def test_installed_plist_is_valid_and_private(self):
        self.s.services("plug", HUB)
        self.init()
        data = plistlib.loads(self.unit_file("osvtest.plug.hub").read_bytes())
        self.assertEqual(data["ProgramArguments"][0], str(self.s.omp))
        self.assertEqual(data["Umask"], 0o077)
        self.assertTrue(data["StandardOutPath"].startswith(str(self.s.state_dir / "logs")))
        self.assertEqual(support.mode_of(self.unit_file("osvtest.plug.hub")), 0o644)


class SystemdInitTests(InitCase):
    kind = "systemd"

    def test_units_notes_and_files(self):
        self.s.write_config(heartbeat={"enabled": True})
        rc, out, _ = self.init()
        self.assertEqual(rc, 0)
        self.assertIn("loginctl enable-linger", out)
        text = self.unit_file("osvtest.heartbeat").read_text()
        self.assertIn("UMask=0077", text)
        self.assertIn("Restart=always", text)
        self.assertIn(("--user enable --now osvtest.heartbeat.service"), self.s.calls())


class PathsWithSpacesTests(InitCase):
    spaces = True

    def test_everything_works_under_a_home_with_a_space(self):
        self.s.write_config(heartbeat={"enabled": True})
        script = self.s.root / "hub.ts"
        script.write_text("x")
        self.s.services("plug", {"name": "hub", "program": {"type": "omp-script", "script": str(script)}})
        rc, out, err = self.init()
        self.assertEqual(rc, 0, err)
        data = plistlib.loads(self.unit_file("osvtest.heartbeat").read_bytes())
        self.assertIn("my home", data["ProgramArguments"][1])
        done = self.s.run_process([str(self.s.data_dir / "bin" / "omp-services"), "config", "validate"])
        self.assertEqual(done.returncode, 0, done.stderr)
        rc, out, _ = self.s.cli("doctor", "--no-catalog")
        self.assertEqual(rc, 0, out)


class XdgRootsTests(InitCase):
    def test_xdg_variables_move_everything(self):
        base = self.s.root / "xdg"
        env = {"XDG_CONFIG_HOME": str(base / "c"), "XDG_DATA_HOME": str(base / "d"), "XDG_STATE_HOME": str(base / "s")}
        rc, out, err = self.init(extra_env=env)
        self.assertEqual(rc, 0, err)
        self.assertTrue((base / "c" / "omp-services" / "config.json").is_file())
        self.assertTrue((base / "d" / "omp-services" / "state.json").is_file())
        self.assertTrue((base / "s" / "omp-services" / "logs").is_dir())
        self.assertFalse(self.s.config_dir.exists())


class UninstallTests(InitCase):
    def setUp(self):
        super().setUp()
        self.s.write_config(heartbeat={"enabled": True})
        self.s.services(
            "plug", HUB, {"name": "keep", "program": {"type": "exec", "argv": ["/bin/true"]}, "protected": True}
        )
        self.init()
        secrets = self.s.config_dir / "secrets"
        (secrets / "heartbeat-url").write_text("https://hc.example/x\n")
        os.chmod(secrets / "heartbeat-url", 0o600)
        (self.s.state_dir / "logs" / "heartbeat.log").write_text("log line\n")

    def test_uninstall_removes_the_installation_and_keeps_data(self):
        rc, out, _ = self.s.cli("uninstall", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.s.loaded(), {"osvtest.plug.keep"}, "the protected unit is left alone")
        self.assertIn("protected", out)
        # a protected unit is left, and so is what it still needs (wrappers, code release, state with its protection)
        self.assertTrue((self.s.data_dir / "current").exists())
        self.assertTrue((self.s.data_dir / "state.json").exists())
        self.assertNotEqual(list((self.s.data_dir / "releases").iterdir()), [])
        for kept in (
            self.s.config_dir / "config.json",
            self.s.config_dir / "secrets" / "heartbeat-url",
            self.s.config_dir / "services.d" / "plug.json",
            self.s.state_dir / "logs" / "heartbeat.log",
        ):
            self.assertTrue(kept.exists(), kept)

    def test_uninstall_include_protected_and_plan_only(self):
        rc, out, _ = self.s.cli("uninstall", "--plan")
        self.assertEqual(self.s.loaded(), {"osvtest.heartbeat", "osvtest.plug.hub", "osvtest.plug.keep"})
        self.s.cli("uninstall", "--yes", "--include-protected")
        self.assertEqual(self.s.loaded(), set())
        self.assertEqual(list(self.s.units.iterdir()), [])

    def test_uninstall_asks_first(self):
        rc, out, _ = self.s.cli("uninstall")
        self.assertEqual(rc, 3)
        self.assertEqual(len(self.s.loaded()), 3)

    def test_uninstall_finds_our_units_even_if_the_state_file_is_lost(self):
        (self.s.data_dir / "state.json").unlink()
        self.s.cli("uninstall", "--yes", "--include-protected")
        self.assertEqual(self.s.loaded(), set())


class PurgeTests(InitCase):
    def setUp(self):
        super().setUp()
        self.init()
        (self.s.config_dir / "secrets" / "heartbeat-url").write_text("https://hc.example/x\n")

    def test_yes_is_not_enough_and_typing_the_word_is(self):
        rc, out, _ = self.s.cli("purge", "--yes")
        self.assertEqual(rc, 2 if "unrecognized" in _ else 3)
        self.assertTrue(self.s.config_dir.exists())
        rc, out, _ = self.s.cli("purge", answers=["y"])
        self.assertEqual(rc, 3)
        self.assertTrue(self.s.config_dir.exists())
        rc, out, _ = self.s.cli("purge", answers=["purge"])
        self.assertEqual(rc, 0, out)
        for d in (self.s.config_dir, self.s.data_dir, self.s.state_dir):
            self.assertFalse(d.exists(), d)
        self.assertTrue(self.s.home.exists(), "only our own directories")

    def test_the_flag_for_scripts(self):
        rc, out, _ = self.s.cli("purge", "--confirm-purge")
        self.assertEqual(rc, 0, out)
        self.assertFalse(self.s.config_dir.exists())

    def test_purge_removes_units_first(self):
        self.s.services("plug", HUB)
        self.init()
        self.s.cli("purge", "--confirm-purge")
        self.assertEqual(self.s.loaded(), set())
        self.assertEqual(list(self.s.units.iterdir()), [])

    def test_purge_refuses_a_directory_it_did_not_create(self):
        (self.s.config_dir / ".omp-services-dir").unlink()
        rc, out, err = self.s.cli("purge", "--confirm-purge")
        self.assertEqual(rc, 2)
        self.assertIn("was not created by omp-services", err)
        self.assertTrue(self.s.data_dir.exists(), "nothing at all was deleted")

    def test_purge_refuses_to_delete_the_home_directory_or_a_link(self):
        rc, out, err = self.s.cli("purge", "--confirm-purge", extra_env={"OMP_SERVICES_CONFIG_DIR": str(self.s.home)})
        self.assertEqual(rc, 2)
        self.assertTrue(self.s.home.exists())
        shutil_target = self.s.root / "victim"
        shutil_target.mkdir()
        (shutil_target / "keep.txt").write_text("x")
        link = self.s.root / "linkcfg"
        link.symlink_to(shutil_target)
        rc, out, err = self.s.cli("purge", "--confirm-purge", extra_env={"OMP_SERVICES_CONFIG_DIR": str(link)})
        self.assertEqual(rc, 2)
        self.assertTrue((shutil_target / "keep.txt").exists())

    def test_purge_refuses_while_protected_units_exist(self):
        self.s.services("plug", {**HUB, "protected": True})
        self.init()
        rc, out, err = self.s.cli("purge", "--confirm-purge")
        self.assertEqual(rc, 2)
        self.assertIn("protected", err)
        self.assertIn("osvtest.plug.hub", self.s.loaded())
        rc, out, err = self.s.cli("purge", "--confirm-purge", "--include-protected")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.s.loaded(), set())


if __name__ == "__main__":
    unittest.main()
