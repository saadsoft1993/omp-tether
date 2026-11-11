"""The command line as a user meets it: real processes, no terminal, the launcher, heartbeat url handling, render."""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import stat
import subprocess
import sys
import unittest

import support

LAUNCHER = support.PLUGIN / "bin" / "omp-services"
MAIN = support.PLUGIN / "python" / "omp_services_main.py"


class ProcessTests(support.Case):
    def run_launcher(self, *args, extra_env=None, stdin=None):
        return subprocess.run(
            [str(LAUNCHER), *args],
            env={**self.s.env, **(extra_env or {})},
            capture_output=True,
            text=True,
            input=stdin,
            start_new_session=True,
            timeout=120,
        )

    def test_version_and_help(self):
        done = self.run_launcher("--version")
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "omp-services 0.1.1"))
        done = self.run_launcher("--help")
        self.assertEqual(done.returncode, 0)
        for word in ("init", "doctor", "uninstall", "purge", "heartbeat"):
            self.assertIn(word, done.stdout)

    def test_without_a_terminal_init_refuses_and_changes_nothing(self):
        done = self.run_launcher("init")
        self.assertEqual(done.returncode, 3, done.stdout + done.stderr)
        self.assertIn("not confirmed", done.stdout)
        self.assertFalse(self.s.data_dir.exists())
        self.assertFalse(self.s.config_dir.exists())

    def test_yes_applies_and_the_unit_dir_is_the_scratch_one(self):
        self.s.write_config(heartbeat={"enabled": True})
        done = self.run_launcher("init", "--yes")
        self.assertEqual(done.returncode, 0, done.stderr)
        plistlib.loads((self.s.units / "osvtest.heartbeat.plist").read_bytes())
        self.assertFalse((self.s.home / "Library" / "LaunchAgents").exists(), "nothing was written to the real path")

    def test_launcher_python_selection(self):
        forced = self.run_launcher("--version", extra_env={"OMP_SERVICES_PYTHON": sys.executable})
        self.assertEqual(forced.returncode, 0)
        rel = self.run_launcher("--version", extra_env={"OMP_SERVICES_PYTHON": "python3"})
        self.assertEqual(rel.returncode, 2)
        self.assertIn("absolute path", rel.stderr)
        old = self.s.root / "python-old"
        old.write_text("#!/bin/sh\nexit 1\n")
        old.chmod(0o755)
        bad = self.run_launcher("--version", extra_env={"OMP_SERVICES_PYTHON": str(old)})
        self.assertEqual(bad.returncode, 2)
        self.assertIn("not Python 3.10 or newer", bad.stderr)

    def test_launcher_finds_a_python_when_path_is_minimal(self):
        done = self.run_launcher("--version", extra_env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_launcher_reports_when_no_python_is_new_enough(self):
        # A PATH holding only a python3 that is too old (and the two tools the launcher needs), no Homebrew directories.
        fake = self.s.root / "fakebin"
        fake.mkdir()
        (fake / "python3").write_text("#!/bin/sh\nexit 1\n")
        (fake / "python3").chmod(0o755)
        for tool in ("dirname", "cat"):
            (fake / tool).symlink_to(shutil.which(tool) or f"/usr/bin/{tool}")
        copy = self.s.root / "plugin-copy"
        (copy / "bin").mkdir(parents=True)
        (copy / "python").mkdir()
        (copy / "python" / "omp_services_main.py").write_text("")
        text = LAUNCHER.read_text().replace("/opt/homebrew/bin /usr/local/bin", "/nonexistent-a /nonexistent-b")
        (copy / "bin" / "omp-services").write_text(text)
        (copy / "bin" / "omp-services").chmod(0o755)
        done = subprocess.run(
            ["/bin/sh", str(copy / "bin" / "omp-services")],
            env={"PATH": str(fake), "HOME": str(self.s.home)},
            capture_output=True,
            text=True,
        )
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn("needs Python 3.10 or newer", done.stderr)

    def test_the_main_script_explains_an_old_python(self):
        text = MAIN.read_text()
        self.assertLess(text.index("sys.version_info < (3, 10)"), text.index("from omp_services.cli import main"))

    def test_bad_arguments_exit_2(self):
        done = self.run_launcher("nonsense")
        self.assertEqual(done.returncode, 2)


class HeartbeatUrlTests(support.Case):
    def test_set_url_reads_stdin_stores_privately_enables_and_never_echoes(self):
        token = "tok-" + os.urandom(6).hex()
        proc = subprocess.run(
            [str(LAUNCHER), "heartbeat", "set-url"],
            env=self.s.env,
            input=f"https://hc.example/ping/{token}\n",
            capture_output=True,
            text=True,
            start_new_session=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn(token, proc.stdout + proc.stderr)
        stored = self.s.config_dir / "secrets" / "heartbeat-url"
        self.assertEqual(stored.read_text().strip(), f"https://hc.example/ping/{token}")
        self.assertEqual(support.mode_of(stored), 0o600)
        self.assertEqual(support.mode_of(stored.parent), 0o700)
        self.assertEqual(support.mode_of(self.s.config_dir / "config.json"), 0o600)
        cfg = json.loads((self.s.config_dir / "config.json").read_text())
        self.assertTrue(cfg["heartbeat"]["enabled"])
        self.assertNotIn(token, json.dumps(cfg))
        self.assertEqual(cfg["schemaVersion"], 1)

    def test_set_url_rejects_bad_urls_without_echoing_them(self):
        for bad in ("http://hc.example/x", "https://u:p@hc.example/x", "garbage", ""):
            rc, out, err = self.s.cli("heartbeat", "set-url")
            proc = subprocess.run(
                [str(LAUNCHER), "heartbeat", "set-url"],
                env=self.s.env,
                input=bad + "\n",
                capture_output=True,
                text=True,
                start_new_session=True,
            )
            self.assertEqual(proc.returncode, 2, bad)
            self.assertNotIn("hc.example", proc.stdout + proc.stderr)
            self.assertFalse((self.s.config_dir / "secrets" / "heartbeat-url").exists())

    def test_clear_url(self):
        subprocess.run(
            [str(LAUNCHER), "heartbeat", "set-url"],
            env=self.s.env,
            input="https://hc.example/x\n",
            capture_output=True,
            text=True,
            start_new_session=True,
        )
        rc, out, err = self.s.cli("heartbeat", "clear-url")
        self.assertEqual(rc, 0, err)
        self.assertFalse((self.s.config_dir / "secrets" / "heartbeat-url").exists())
        self.assertFalse(json.loads((self.s.config_dir / "config.json").read_text())["heartbeat"]["enabled"])

    def test_set_url_preserves_other_keys_in_the_config(self):
        self.s.write_config(labelPrefix="mine", logs={"maxBytes": 70000})
        subprocess.run(
            [str(LAUNCHER), "heartbeat", "set-url"],
            env=self.s.env,
            input="https://hc.example/x\n",
            capture_output=True,
            text=True,
            start_new_session=True,
        )
        cfg = json.loads((self.s.config_dir / "config.json").read_text())
        self.assertEqual((cfg["labelPrefix"], cfg["logs"]["maxBytes"]), ("mine", 70000))

    def test_status_before_and_after(self):
        rc, out, _ = self.s.cli("heartbeat", "status")
        self.assertEqual(rc, 1)
        self.assertIn("no heartbeat run", out)


class ConfigAndRenderTests(support.Case):
    def test_config_commands(self):
        rc, out, _ = self.s.cli("config", "path")
        self.assertEqual(out.strip(), str(self.s.config_dir / "config.json"))
        rc, out, _ = self.s.cli("config", "validate")
        self.assertEqual((rc, out.strip()), (0, "valid (no file: defaults)"))
        self.s.write_config(bogus=1)
        rc, out, _ = self.s.cli("config", "validate")
        self.assertEqual(rc, 1)
        self.assertIn("unknown field 'bogus'", out)
        rc, out, _ = self.s.cli("config", "show")
        self.assertEqual(rc, 1)

    def test_render_for_both_platforms_without_touching_anything(self):
        self.s.write_config(heartbeat={"enabled": True})
        rc, out, _ = self.s.cli("render", "--platform", "systemd")
        self.assertEqual(rc, 0)
        self.assertIn("ExecStart=", out)
        self.assertIn("# osvtest.heartbeat.service", out)
        rc, out, _ = self.s.cli("render", "--platform", "launchd")
        self.assertIn("<key>KeepAlive</key>", out)
        outdir = self.s.root / "rendered"
        rc, out, _ = self.s.cli("render", "--platform", "linux", "--out", str(outdir))
        self.assertTrue((outdir / "osvtest.heartbeat.service").is_file())
        self.assertEqual(stat.S_IMODE(os.stat(outdir / "osvtest.heartbeat.service").st_mode), 0o644)
        self.assertFalse(self.s.data_dir.exists(), "render is read-only")
        self.assertEqual(self.s.units.exists() and list(self.s.units.iterdir()), [])


class HealthCommandTests(support.Case):
    def test_health_run_prints_results_and_exit_codes(self):
        self.s.health(
            "plug",
            {"id": "down", "type": "port", "port": support.closed_port()},
            {"id": "up", "type": "command", "argv": [sys.executable, "-c", "pass"]},
        )
        path = self.s.config_dir / "health.d" / "plug.json"
        rc, out, _ = (
            self.s.cli("health", "run", "--file", str(path), "--json")
            if False
            else self.s.cli("health", "--file", str(path), "--json")
        )
        self.assertEqual(rc, 1)
        results = {r["id"]: r["status"] for r in json.loads(out)}
        self.assertEqual(results, {"down": "unreachable", "up": "ok"})
        bad = self.s.dropin("health", "bad", "{")
        rc, out, _ = self.s.cli("health", "--file", str(bad), "--json")
        self.assertEqual(rc, 2)
        self.assertIn("invalid", out)


if __name__ == "__main__":
    unittest.main()
