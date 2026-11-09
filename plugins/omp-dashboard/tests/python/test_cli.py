"""The command line, `doctor`, and the drop-ins for omp-services, validated against omp-services' own schemas and its health runner."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import unittest

from omp_dashboard import doctor, schema
from omp_dashboard.dropins import health_dropin, services_dropin, stray_overrides
from support import MAIN, PLUGIN, Client, Scratch, ScratchTestCase, host_entry, run_hub_process, stop_process

REPO = PLUGIN.parents[1]
SERVICES = REPO / "plugins" / "omp-services"


def cli(
    scratch: Scratch, *args: str, env: dict | None = None, timeout: float = 60, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    e = {**scratch.env, "PYTHONDONTWRITEBYTECODE": "1", **(env or {})}
    return subprocess.run(
        [sys.executable, str(MAIN), *args],
        env=e,
        capture_output=True,
        text=True,
        timeout=timeout,
        input=stdin if stdin is not None else "",
    )


class Basics(ScratchTestCase):
    def setUp(self) -> None:
        self.s = self.make()

    def test_usage_errors_exit_2_and_unknown_things_are_refused(self) -> None:
        self.assertEqual(cli(self.s).returncode, 2)
        self.assertEqual(cli(self.s, "revoke").returncode, 2)
        self.assertEqual(cli(self.s, "config", "bogus").returncode, 2)
        self.assertEqual(cli(self.s, "config", "show", "sometopic").returncode, 2)
        self.assertEqual(cli(self.s, "config", "validate", "--replace").returncode, 2)

    def test_version_and_config_commands(self) -> None:
        self.assertIn("omp-dashboard 0.1.0", cli(self.s, "--version").stdout)
        self.assertEqual(cli(self.s, "config", "path").stdout.strip(), str(self.s.paths().config_file))
        self.assertIn("defaults are in use", cli(self.s, "config", "validate").stdout)
        shown = cli(self.s, "config", "show")
        self.assertEqual(json.loads(shown.stdout)["listen"]["host"], "127.0.0.1")
        self.assertIn("overrides: OMP_DASHBOARD_PORT", shown.stderr)

    def test_config_init_writes_the_defaults_private_and_never_overwrites(self) -> None:
        r = cli(self.s, "config", "init")
        self.assertEqual(r.returncode, 0, r.stderr)
        path = self.s.paths().config_file
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(path.parent).st_mode), 0o700)
        self.assertEqual(json.loads(path.read_text())["start"]["backend"], "off")
        path.write_text(path.read_text().replace('"off"', '"herdr"'))
        again = cli(self.s, "config", "init")
        self.assertEqual(again.returncode, 1)
        self.assertIn('"herdr"', path.read_text())
        self.assertEqual(cli(self.s, "config", "validate").returncode, 0)

    def test_an_invalid_configuration_stops_every_command_that_needs_one(self) -> None:
        self.s.write_config({"typo": True})
        for args in (("run",), ("pair",), ("devices",), ("revoke", "all"), ("dropins",), ("config", "set-ntfy-topic")):
            r = cli(self.s, *args)
            self.assertEqual(r.returncode, 2, args)
            self.assertIn("typo", r.stderr)
        self.assertEqual(cli(self.s, "config", "validate").returncode, 1)
        d = cli(self.s, "doctor")
        self.assertEqual(d.returncode, 1)
        self.assertIn("[FAIL] config", d.stdout)

    def test_the_ntfy_topic_is_made_private_printed_once_and_not_replaced_by_accident(self) -> None:
        r = cli(self.s, "config", "set-ntfy-topic")
        self.assertEqual(r.returncode, 0, r.stderr)
        topic_file = self.s.paths().secrets_dir / "ntfy-topic"
        topic = topic_file.read_text().strip()
        self.assertIn(topic, r.stdout)
        self.assertIn("third party", r.stdout)
        self.assertEqual(stat.S_IMODE(os.stat(topic_file).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(topic_file.parent).st_mode), 0o700)
        second = cli(self.s, "config", "set-ntfy-topic", "--stdin", stdin="other-topic-1234567\n")
        self.assertEqual(second.returncode, 1)
        self.assertEqual(topic_file.read_text().strip(), topic)
        self.assertEqual(
            cli(self.s, "config", "set-ntfy-topic", "--stdin", "--replace", stdin="other-topic-1234567\n").returncode, 0
        )
        self.assertEqual(topic_file.read_text().strip(), "other-topic-1234567")
        for shown in (cli(self.s, "config", "show"), cli(self.s, "doctor"), cli(self.s, "doctor", "--json")):
            self.assertNotIn("other-topic-1234567", shown.stdout + shown.stderr)
        self.assertEqual(
            cli(self.s, "config", "set-ntfy-topic", "--stdin", "--replace", stdin="bad topic\n").returncode, 2
        )
        self.assertEqual(cli(self.s, "config", "clear-ntfy-topic").returncode, 0)
        self.assertFalse(topic_file.exists())
        self.assertIn("No topic", cli(self.s, "config", "clear-ntfy-topic").stdout)

    def test_a_topic_file_that_is_a_link_is_not_followed(self) -> None:
        victim = self.s.root / "victim"
        victim.write_text("keep")
        os.chmod(victim, 0o600)
        d = self.s.paths().secrets_dir
        d.mkdir(parents=True, mode=0o700)
        (d / "ntfy-topic").symlink_to(victim)
        r = cli(self.s, "config", "set-ntfy-topic", "--stdin", "--replace", stdin="newtopic-1234567890\n")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(victim.read_text(), "keep")


class PairingCommands(ScratchTestCase):
    def test_pair_refuses_and_makes_no_token_while_the_hub_is_not_running(self) -> None:
        s = self.make()
        r = cli(s, "pair")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no pairing token was made", r.stderr)
        self.assertFalse((s.paths().pairing_dir / "pending.json").exists())

    def test_pair_devices_and_revoke_against_a_running_hub(self) -> None:
        s = self.make([host_entry(1)])
        proc = run_hub_process(s)
        self.addCleanup(stop_process, proc)
        r = cli(s, "pair")
        self.assertEqual(r.returncode, 0, r.stderr)
        link = r.stdout.strip()
        self.assertTrue(link.startswith(f"http://127.0.0.1:{s.port}/#pair="), link)
        self.assertIn("works once", r.stderr)
        self.assertNotIn("#pair=", r.stderr)
        token = link.split("#pair=")[1]
        c = Client(s.port)
        self.assertEqual(c.pair_with(token).status, 200)
        self.assertEqual(c.get("/api/hub/machine").status, 200)
        self.assertEqual(Client(s.port).pair_with(token).status, 403, "the link works once")
        listing = cli(s, "devices").stdout
        self.assertIn("Test Browser/1.0", listing)
        device_id = listing.split()[0]
        self.assertEqual(cli(s, "revoke", "zz").returncode, 1, "too short to name one browser")
        self.assertEqual(cli(s, "revoke", device_id[:3]).returncode, 1)
        self.assertEqual(cli(s, "revoke", device_id).returncode, 0)
        self.assertEqual(c.get("/api/hub/machine").status, 401, "a revoked browser is refused on its next request")
        self.assertIn("No browser is paired", cli(s, "devices").stdout)
        as_json = json.loads(cli(s, "pair", "--json").stdout)
        self.assertEqual(as_json["expiresInSeconds"], 300)
        self.assertEqual(cli(s, "revoke", "all").returncode, 0)

    def test_the_hub_process_shuts_down_cleanly_on_sigterm_and_prints_no_secret(self) -> None:
        s = self.make()
        proc = run_hub_process(s)
        proc.terminate()
        out = proc.stdout.read().decode() if proc.stdout else ""
        self.assertEqual(proc.wait(10), 0)
        self.assertIn("listening on http://127.0.0.1", out)
        self.assertNotIn("pair=", out)

    def test_a_second_hub_on_the_same_port_says_so_and_exits_1(self) -> None:
        s = self.make()
        proc = run_hub_process(s)
        self.addCleanup(stop_process, proc)
        r = cli(s, "run")
        self.assertEqual(r.returncode, 1)
        self.assertIn("cannot listen", r.stderr)


def validate(doc: dict, name: str) -> list[str]:
    sch = json.loads((SERVICES / "python" / "omp_services" / "schemas" / f"{name}.schema.json").read_text())
    schema.check_schema_keywords(sch)
    return schema.validate(doc, sch)


class Dropins(ScratchTestCase):
    def setUp(self) -> None:
        self.s = self.make()
        self.config = self.s.config()

    def test_the_documents_pass_omp_services_own_schemas_for_every_adapter(self) -> None:
        for values in (
            {},
            {"auth": {"adapter": "tailscale-serve", "tailscale": {"servePort": 8443}}},
            {"listen": {"host": "::1"}},
        ):
            self.s.write_config(values)
            config = self.s.config()
            self.assertEqual(validate(services_dropin(config, self.s.env), "services-1"), [], values)
            self.assertEqual(validate(health_dropin(config, self.s.env), "health-1"), [], values)

    def test_the_service_runs_python_with_the_same_config_and_nothing_secret(self) -> None:
        doc = services_dropin(self.config, self.s.env)
        (service,) = doc["services"]
        self.assertEqual(
            (doc["plugin"], service["name"], service["program"]["type"], service["program"]["args"]),
            ("omp-dashboard", "hub", "python", ["run"]),
        )
        self.assertTrue(service["program"]["script"].endswith("python/omp_dashboard_main.py"))
        self.assertEqual(service["env"]["OMP_DASHBOARD_CONFIG"], str(self.s.paths().config_file))
        self.assertEqual(service["env"]["XDG_CONFIG_HOME"], str(self.s.home / ".config"))
        text = json.dumps(doc)
        for word in ("BUN_BE_BUN", "ntfy", "topic", "cookie"):
            self.assertNotIn(word, text)

    def test_health_checks_name_the_hubs_own_port_and_a_serve_route_only_with_tailscale(self) -> None:
        checks = {c["id"]: c for c in health_dropin(self.config, self.s.env)["checks"]}
        self.assertEqual(set(checks), {"hub-health", "hub-port", "hub-owner", "hub-service"})
        self.assertEqual(checks["hub-health"]["url"], f"http://127.0.0.1:{self.s.port}/healthz")
        self.assertEqual(checks["hub-owner"]["port"], self.s.port)
        self.s.write_config({"auth": {"adapter": "tailscale-serve", "tailscale": {"servePort": 8443}}})
        checks = {c["id"]: c for c in health_dropin(self.s.config(), self.s.env)["checks"]}
        self.assertEqual(
            (checks["serve-route"]["port"], checks["serve-route"]["target"]), (8443, f"http://127.0.0.1:{self.s.port}")
        )

    def test_show_writes_nothing_and_write_makes_private_files_that_doctor_then_recognises(self) -> None:
        shown = cli(self.s, "dropins")
        self.assertEqual(shown.returncode, 0)
        self.assertIn("nothing was written", shown.stdout)
        self.assertFalse(self.s.paths().services_dir.exists())
        env = {k: v for k, v in self.s.env.items() if k != "OMP_DASHBOARD_PORT"}
        self.s.write_config({"listen": {"port": self.s.port}, "omp": {"bin": str(self.s.omp_path)}})
        r = subprocess.run(
            [sys.executable, str(MAIN), "dropins", "--write"],
            env={**env, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        paths = self.s.paths()
        for f in (paths.services_dropin, paths.health_dropin):
            self.assertEqual(stat.S_IMODE(os.stat(f).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(f.parent).st_mode), 0o700)
        findings = {f.id: f.level for f in doctor.run_doctor(self.s.config(), self.s.env)}
        self.assertEqual((findings["dropin-services"], findings["dropin-health"]), ("ok", "ok"))
        self.assertEqual(json.loads(paths.services_dropin.read_text())["plugin"], "omp-dashboard")

    def test_write_refuses_while_settings_exist_only_as_environment_variables(self) -> None:
        self.assertEqual(stray_overrides(self.s.env), ["OMP_DASHBOARD_PORT"])
        r = cli(self.s, "dropins", "--write")
        self.assertEqual(r.returncode, 1)
        self.assertIn("OMP_DASHBOARD_PORT", r.stderr)
        self.assertFalse(self.s.paths().services_dropin.exists())

    def test_write_never_follows_a_link_or_a_loose_directory(self) -> None:
        env = {k: v for k, v in self.s.env.items() if k != "OMP_DASHBOARD_PORT"}

        def run() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [sys.executable, str(MAIN), "dropins", "--write"],
                env={**env, "PYTHONDONTWRITEBYTECODE": "1"},
                capture_output=True,
                text=True,
                timeout=60,
            )

        paths = self.s.paths()
        victim = self.s.root / "victim"
        victim.write_text("keep")
        paths.services_dropin.parent.mkdir(parents=True, mode=0o700)
        paths.services_dropin.symlink_to(victim)
        self.assertEqual(run().returncode, 1)
        self.assertEqual(victim.read_text(), "keep")
        paths.services_dropin.unlink()
        os.chmod(paths.services_dropin.parent, 0o777)
        self.assertEqual(run().returncode, 1)
        self.assertFalse(paths.services_dropin.exists())

    def test_omp_services_health_runner_accepts_the_drop_in_and_finds_a_running_hub_healthy(self) -> None:
        env = {k: v for k, v in self.s.env.items() if k != "OMP_DASHBOARD_PORT"}
        self.s.write_config({"listen": {"port": self.s.port}, "omp": {"bin": str(self.s.omp_path)}})
        proc = run_hub_process(self.s)  # the hub reads the config file written above
        self.addCleanup(stop_process, proc)
        w = subprocess.run(
            [sys.executable, str(MAIN), "dropins", "--write"],
            env={**env, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(w.returncode, 0, w.stderr)
        r = subprocess.run(
            [
                str(SERVICES / "bin" / "omp-services"),
                "health",
                "--json",
                "--no-actions",
                "--file",
                str(self.s.paths().health_dropin),
            ],
            env={
                **env,
                "PYTHONDONTWRITEBYTECODE": "1",
                "OMP_SERVICES_PYTHON": sys.executable,
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            },
            capture_output=True,
            text=True,
            timeout=90,
        )
        results = {x["id"]: x["status"] for x in json.loads(r.stdout)}
        self.assertEqual(results["hub-health"], "ok", r.stdout + r.stderr)
        self.assertEqual((results["hub-port"], results["hub-owner"]), ("ok", "ok"))
        self.assertEqual(r.returncode, 1 if any(v != "ok" for v in results.values()) else 0)
        proc.terminate()
        proc.wait(10)
        down = subprocess.run(
            [
                str(SERVICES / "bin" / "omp-services"),
                "health",
                "--json",
                "--no-actions",
                "--file",
                str(self.s.paths().health_dropin),
            ],
            env={
                **env,
                "PYTHONDONTWRITEBYTECODE": "1",
                "OMP_SERVICES_PYTHON": sys.executable,
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            },
            capture_output=True,
            text=True,
            timeout=90,
        )
        stopped = {x["id"]: x["status"] for x in json.loads(down.stdout)}
        self.assertEqual((stopped["hub-health"], stopped["hub-port"]), ("unreachable", "unreachable"))


FAKE_TAILSCALE = """#!{python}
import json, sys
if sys.argv[1:3] == ["serve", "status"]:
    print({serve!r})
else:
    print({status!r})
"""


class Doctor(ScratchTestCase):
    def test_serve_status_is_read_for_the_route_and_for_funnel(self) -> None:
        target = "http://127.0.0.1:17795"
        ok = {"Web": {"box.example:8443": {"Handlers": {"/": {"Proxy": target}}}}}
        self.assertEqual(doctor.parse_serve_status(json.dumps(ok), 8443, target), ("ok", False))
        funnel = {**ok, "AllowFunnel": {"box.example:8443": True}}
        self.assertEqual(doctor.parse_serve_status(json.dumps(funnel), 8443, target), ("ok", True))
        elsewhere = {"Web": {"box.example:8443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:1"}}}}}
        self.assertEqual(
            doctor.parse_serve_status(json.dumps(elsewhere), 8443, target), ("elsewhere", "http://127.0.0.1:1")
        )
        self.assertEqual(doctor.parse_serve_status(json.dumps({"Web": {}}), 8443, target), ("absent", None))
        self.assertEqual(doctor.parse_serve_status("nope", 8443, target)[0], "unreadable")
        self.assertEqual(doctor.parse_serve_status("[]", 8443, target)[0], "unreadable")
        self.assertEqual(
            doctor.parse_serve_status(
                json.dumps({"Web": {"h:8443": {"Handlers": {"/": {"Path": "/x"}}}}}), 8443, target
            )[0],
            "elsewhere",
        )

    def findings(self, scratch: Scratch) -> dict[str, doctor.Finding]:
        return {f.id: f for f in doctor.run_doctor(scratch.config(), scratch.env)}

    def test_a_clean_default_setup_has_no_failure_and_names_what_is_off(self) -> None:
        s = self.make([host_entry(1)])
        f = self.findings(s)
        self.assertFalse(
            [x for x in f.values() if x.level == "fail"], [x.message for x in f.values() if x.level == "fail"]
        )
        self.assertEqual(f["ui-bundle"].level, "ok")
        self.assertEqual(f["omp"].level, "ok")
        self.assertEqual(f["hub"].level, "warn")
        self.assertIn("Start is off", f["start"].message)
        self.assertIn("alerts are off", f["alerts"].message)
        self.assertEqual(f["pairing"].level, "warn")

    def test_an_old_or_unknown_omp_and_a_broken_collab_are_reported(self) -> None:
        s = self.make()
        s.fake_state["version"] = "omp/17.0.0"
        s.save_fake()
        self.assertEqual(self.findings(s)["omp"].level, "fail")
        s.fake_state["version"] = "omp/19.0.0"
        s.save_fake()
        self.assertEqual(self.findings(s)["omp"].level, "warn")
        s.fake_state["fail"] = True
        s.save_fake()
        self.assertEqual(self.findings(s)["collab"].level, "fail")

    def test_a_hub_that_is_running_and_a_port_held_by_someone_else(self) -> None:
        s = self.make()
        proc = run_hub_process(s)
        self.addCleanup(stop_process, proc)
        self.assertEqual(self.findings(s)["hub"].level, "ok")

    def test_control_start_and_relay_findings(self) -> None:
        s = self.make()
        s.write_config({"start": {"backend": "tmux", "tmux": {"bin": "/nonexistent/tmux"}}, "relay": {"enabled": True}})
        f = self.findings(s)
        self.assertEqual(f["start"].level, "fail")
        self.assertEqual(f["start-roots"].level, "warn")
        self.assertEqual(f["relay"].level, "fail", "no relay is running: nothing proves itself")
        control = s.paths().control_dir
        control.mkdir(parents=True, mode=0o755)
        os.chmod(control, 0o755)
        self.assertEqual(self.findings(s)["control"].level, "fail")

    def test_tailscale_findings_with_a_stand_in_command(self) -> None:
        s = self.make()
        status = {"Self": {"DNSName": "box.t.example.", "UserID": 1}, "User": {"1": {"LoginName": "me@example.test"}}}
        target = f"http://127.0.0.1:{s.port}"
        for serve, expected in (
            ({"Web": {"box.t.example:8443": {"Handlers": {"/": {"Proxy": target}}}}}, "ok"),
            (
                {
                    "Web": {"box.t.example:8443": {"Handlers": {"/": {"Proxy": target}}}},
                    "AllowFunnel": {"box.t.example:8443": True},
                },
                "fail",
            ),
            ({"Web": {}}, "warn"),
        ):
            script = s.bin / "tailscale"
            script.write_text(
                FAKE_TAILSCALE.format(
                    python=sys.executable, serve=json.dumps(serve), status=json.dumps(status)
                ).replace("{serve!r}", repr(json.dumps(serve)))
            )
            os.chmod(script, 0o700)
            s.write_config(
                {"auth": {"adapter": "tailscale-serve", "tailscale": {"bin": str(script), "servePort": 8443}}}
            )
            f = self.findings(s)
            self.assertEqual(f["tailscale"].level, "ok")
            self.assertEqual(f["serve-route"].level, expected, serve)

    def test_the_rendering_names_the_fix_only_for_problems_and_json_is_machine_readable(self) -> None:
        text = doctor.render(
            [doctor.Finding("a", "ok", "fine", "never shown"), doctor.Finding("b", "fail", "broken", "do this")]
        )
        self.assertNotIn("never shown", text)
        self.assertIn("fix: do this", text)
        self.assertIn("1 failing, 0 warning(s), 1 ok", text)
        s = self.make()
        out = json.loads(cli(s, "doctor", "--json").stdout)
        self.assertTrue({"id", "level", "message"} <= set(out[0]))


if __name__ == "__main__":
    unittest.main()
