"""Unit rendering: launchd property lists and systemd user units, labels, quoting, environment rules."""

from __future__ import annotations

import os
import plistlib
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from omp_services import paths as paths_mod
from omp_services import units
from omp_services.util import ServicesError

GOLDEN = Path(__file__).parent / "golden"
PATHS = paths_mod.from_env({"HOME": "/home/example", "XDG_CONFIG_HOME": "/home/example/.config"}, "linux")


def sample_specs() -> list[units.ServiceSpec]:
    drop = {
        "protocol": "services/1",
        "plugin": "omp-example",
        "services": [
            {
                "name": "hub",
                "description": 'Example hub 100% sure $HOME "quoted"',
                "program": {
                    "type": "omp-script",
                    "script": "/opt/example dir/hub.ts",
                    "args": ["--port", "8123", "a b"],
                },
                "env": {"EXAMPLE_MODE": "x=1 %h $y"},
                "protected": True,
                "throttleSeconds": 20,
            },
            {"name": "tool", "program": {"type": "exec", "argv": ["/usr/bin/example", "serve"]}},
        ],
    }
    out = [units.heartbeat_spec("omp-tether", PATHS, "/usr/bin/python3.12", "/home/example/.local/bin/omp", {})]
    out += units.specs_from_dropin(drop, "omp-tether", PATHS, "/usr/bin/python3.12", "/home/example/.local/bin/omp", {})
    return out


class RenderTests(unittest.TestCase):
    def test_golden_files(self):
        for spec in sample_specs():
            for kind, suffix in (("launchd", "plist"), ("systemd", "service")):
                golden = GOLDEN / f"{spec.label}.{suffix}"
                text = units.render(spec, kind)
                if os.environ.get("OMP_SERVICES_UPDATE_GOLDEN"):
                    GOLDEN.mkdir(exist_ok=True)
                    golden.write_text(text)
                self.assertEqual(golden.read_text(), text, f"{golden.name} drifted: review the change, then regenerate")

    def test_launchd_plist_parses_and_has_the_safety_keys(self):
        for spec in sample_specs():
            data = plistlib.loads(units.render_launchd(spec).encode())
            self.assertEqual(data["Label"], spec.label)
            self.assertEqual(data["ProgramArguments"], list(spec.argv))
            self.assertIs(data["KeepAlive"], True)
            self.assertEqual(data["Umask"], 0o077)
            self.assertEqual(data["StandardOutPath"], spec.log)
            self.assertTrue(data["StandardOutPath"].startswith(str(PATHS.state_dir)), "logs live under the state dir")
            self.assertEqual(data["EnvironmentVariables"], dict(spec.env))

    @unittest.skipUnless(shutil.which("plutil"), "plutil is macOS only")
    def test_plutil_accepts_every_plist(self):
        with tempfile.TemporaryDirectory() as d:
            for spec in sample_specs():
                path = Path(d) / f"{spec.label}.plist"
                path.write_text(units.render_launchd(spec))
                done = subprocess.run(["plutil", "-lint", str(path)], capture_output=True, text=True)
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_systemd_unit_shape(self):
        hub = next(s for s in sample_specs() if s.name == "hub")
        text = units.render_systemd(hub)
        self.assertIn("[Unit]", text)
        self.assertIn("UMask=0077", text)
        self.assertIn("Restart=always", text)
        self.assertIn("RestartSec=20", text)
        self.assertIn("NoNewPrivileges=yes", text)
        self.assertIn("WantedBy=default.target", text)
        self.assertIn(f"StandardOutput=append:{PATHS.logs_dir}/omp-example.hub.log", text)
        # a space in a path or argument stays inside one quoted word; % and $ are doubled so nothing expands
        self.assertIn('"/opt/example dir/hub.ts"', text)
        self.assertIn('"a b"', text)
        self.assertIn("Description=Example hub 100%% sure $HOME", text)
        self.assertIn('Environment="EXAMPLE_MODE=x=1 %%h $y"', text)  # Environment= expands specifiers, not variables
        self.assertIn('Environment="BUN_BE_BUN=1"', text)

    def test_systemd_quote(self):
        self.assertEqual(units.systemd_quote('a"b'), '"a\\"b"')
        self.assertEqual(units.systemd_quote("a\\b"), '"a\\\\b"')
        self.assertEqual(units.systemd_quote("50%$x"), '"50%%$$x"')

    def test_omp_scripts_run_under_embedded_bun_and_a_pinned_omp(self):
        hub = next(s for s in sample_specs() if s.name == "hub")
        self.assertEqual(hub.argv[0], "/home/example/.local/bin/omp")
        self.assertEqual(dict(hub.env)["BUN_BE_BUN"], "1")
        self.assertTrue(dict(hub.env)["PATH"].startswith(str(PATHS.bin_dir)))
        tool = next(s for s in sample_specs() if s.name == "tool")
        self.assertNotIn("BUN_BE_BUN", dict(tool.env))

    def test_labels_and_the_allowlist(self):
        self.assertEqual(units.label_for("p", "plug", "svc"), "p.plug.svc")
        self.assertEqual(units.heartbeat_label("p"), "p.heartbeat")
        self.assertTrue(units.owns_label("p", "p.heartbeat"))
        self.assertFalse(units.owns_label("p", "p"))
        self.assertFalse(units.owns_label("p", "px.heartbeat"))
        self.assertFalse(units.owns_label("p", "com.apple.thing"))
        self.assertFalse(units.owns_label("p", "p.UPPER"))
        self.assertFalse(units.owns_label("p", "p../x"))

    def test_the_heartbeat_unit_runs_the_current_release_link(self):
        hb = sample_specs()[0]
        self.assertEqual(
            hb.argv,
            ("/usr/bin/python3.12", f"{PATHS.data_dir}/current/python/omp_services_main.py", "heartbeat", "run"),
        )
        self.assertTrue(re.fullmatch(r"[a-z0-9.-]+", hb.label))

    def rejects(self, drop: dict, needle: str, omp: str | None = "/usr/bin/omp") -> None:
        with self.assertRaises(ServicesError) as ctx:
            units.specs_from_dropin(drop, "p", PATHS, "/usr/bin/python3", omp, {})
        self.assertIn(needle, str(ctx.exception))

    def test_what_the_schema_cannot_say(self):
        def one(program, **extra):
            return {"protocol": "services/1", "plugin": "pl", "services": [{"name": "a", "program": program, **extra}]}

        self.rejects(one({"type": "exec", "argv": ["relative/bin"]}), "absolute")
        self.rejects(one({"type": "omp-script", "script": "/a.ts"}), "no omp binary", omp=None)
        for name in ("PATH", "BUN_BE_BUN", "LD_PRELOAD", "OMP_SERVICES_DATA_DIR"):
            self.rejects(one({"type": "exec", "argv": ["/bin/x"]}, env={name: "x"}), "reserved")
        self.rejects(one({"type": "exec", "argv": ["/bin/x"]}, env={"lower": "x"}), "must match")
        dup = {
            "protocol": "services/1",
            "plugin": "pl",
            "services": [
                {"name": "a", "program": {"type": "exec", "argv": ["/bin/x"]}},
                {"name": "a", "program": {"type": "exec", "argv": ["/bin/y"]}},
            ],
        }
        self.rejects(dup, "declared twice")

    def test_needs_lists_the_files_a_service_requires(self):
        specs = {s.name: s for s in sample_specs()}
        self.assertEqual(specs["hub"].needs, ("/home/example/.local/bin/omp", "/opt/example dir/hub.ts"))
        self.assertEqual(specs["tool"].needs, ("/usr/bin/example",))


if __name__ == "__main__":
    unittest.main()
