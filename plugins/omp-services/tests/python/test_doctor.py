"""doctor: findings, classification of health failures, file modes, stale heartbeat, the newer-version notice."""

from __future__ import annotations

import json
import os
import time
import unittest

import support

HUB_SCRIPT = {"name": "hub", "program": {"type": "exec", "argv": ["/bin/sleep", "1"]}}


class DoctorCase(support.Case):
    def doctor(self, *args):
        rc, out, err = self.s.cli("doctor", "--json", "--no-catalog", *args)
        self.assertIn(rc, (0, 1), err)
        data = json.loads(out)
        return rc, data, {f["id"]: f for f in data["findings"]}

    def init(self):
        rc, out, err = self.s.cli("init", "--yes")
        self.assertEqual(rc, 0, out + err)


class FreshAndHealthy(DoctorCase):
    def test_fresh_machine_says_to_run_init_and_does_not_fail(self):
        rc, data, f = self.doctor()
        self.assertEqual(rc, 0)
        self.assertEqual(f["setup"]["level"], "warn")
        self.assertIn("omp-services init", f["setup"]["hint"])
        self.assertEqual(f["omp.version"]["level"], "ok")

    def test_healthy_machine(self):
        self.s.write_config(heartbeat={"enabled": True})
        self.s.services("plug", HUB_SCRIPT)
        self.init()
        rc, data, f = self.doctor()
        self.assertEqual(rc, 0, data["findings"])
        self.assertEqual(f["unit.osvtest.plug.hub"]["level"], "ok")
        self.assertEqual(f["directories"]["level"], "ok")
        self.assertEqual(f["runtime.current"]["level"], "ok")

    def test_text_output_and_exit_status(self):
        self.init()
        rc, out, _ = self.s.cli("doctor", "--no-catalog")
        self.assertEqual(rc, 0)
        self.assertIn("omp-services doctor 0.1.1", out)
        self.assertIn("0 failure(s)", out)
        os.chmod(self.s.config_dir / "config.json", 0o666)
        rc, out, _ = self.s.cli("doctor", "--no-catalog")
        self.assertEqual(rc, 1)
        self.assertIn("mode", out)  # the configuration is no longer trusted: reported, and nothing from it is run


class UnitsAndFiles(DoctorCase):
    def setUp(self):
        super().setUp()
        self.s.services("plug", HUB_SCRIPT)
        self.init()

    def test_stopped_missing_and_drifted_units(self):
        self.s.set_state("osvtest.plug.hub", "waiting")
        _, _, f = self.doctor()
        self.assertEqual(f["unit.osvtest.plug.hub"]["level"], "fail")
        self.assertIn("not running", f["unit.osvtest.plug.hub"]["message"])
        (self.s.fake / "loaded" / "osvtest.plug.hub").unlink()
        _, _, f = self.doctor()
        self.assertIn("not loaded", f["unit.osvtest.plug.hub"]["message"])
        unit = self.s.units / "osvtest.plug.hub.plist"
        unit.write_text(unit.read_text() + "<!-- edited -->\n")
        self.s.set_state("osvtest.plug.hub", "running")
        _, _, f = self.doctor()
        self.assertEqual(f["unit.osvtest.plug.hub"]["level"], "warn")
        unit.unlink()
        _, _, f = self.doctor()
        self.assertEqual(f["unit.osvtest.plug.hub"]["level"], "fail")
        self.assertIn("missing", f["unit.osvtest.plug.hub"]["message"])

    def test_orphaned_unit(self):
        (self.s.config_dir / "services.d" / "plug.json").unlink()
        _, _, f = self.doctor()
        self.assertIn("no longer wanted", f["unit.osvtest.plug.hub"]["message"])

    def test_a_service_whose_program_is_gone(self):
        self.s.services("plug", {"name": "hub", "program": {"type": "exec", "argv": ["/nonexistent/program"]}})
        _, _, f = self.doctor()
        self.assertEqual(f["unit.osvtest.plug.hub"]["level"], "fail")
        self.assertIn("/nonexistent/program", f["unit.osvtest.plug.hub"]["message"])

    def test_file_and_directory_modes(self):
        secrets = self.s.config_dir / "secrets"
        (secrets / "heartbeat-url").write_text("https://hc.example/x\n")
        os.chmod(secrets / "heartbeat-url", 0o644)
        _, _, f = self.doctor()
        self.assertEqual(f["secrets.mode"]["level"], "fail")
        self.assertNotIn("hc.example", json.dumps(f))
        os.chmod(secrets / "heartbeat-url", 0o600)
        os.chmod(secrets, 0o755)
        os.chmod(self.s.state_dir, 0o750)
        rc, data, f = self.doctor()
        self.assertTrue(
            any(
                x["id"] == "directories" and x["level"] == "fail" and "secrets" in x["message"]
                for x in data["findings"]
            )
        )
        self.assertTrue(any(x["level"] == "warn" and "mode 0750" in x["message"] for x in data["findings"]))

    def test_config_problems_and_env_overrides(self):
        self.s.write_config(heartbeat={"intervalSeconds": 1})
        rc, data, f = self.doctor()
        self.assertEqual(rc, 1)
        self.assertEqual(f["config.valid"]["level"], "fail")
        self.assertIn("intervalSeconds", f["config.valid"]["message"])

    def test_invalid_drop_ins_are_named(self):
        self.s.dropin(
            "health", "broken", {"protocol": "health/1", "plugin": "broken", "checks": [{"id": "x", "type": "nope"}]}
        )
        self.s.health("loose", {"id": "a", "type": "port", "port": 1}, mode=0o666)
        _, _, f = self.doctor()
        self.assertEqual(f["dropin.health.broken"]["level"], "fail")
        self.assertIn("unknown type", f["dropin.health.broken"]["message"])
        self.assertIn("cannot write", f["dropin.health.loose"]["message"])

    def test_the_pinned_omp_disappearing(self):
        self.s.omp.unlink()
        rc, _, f = self.doctor()
        self.assertEqual(rc, 1)
        self.assertIn("omp.path", f)

    def test_omp_version_rules(self):
        (self.s.fake / "omp.version").write_text("18.0.0")
        _, _, f = self.doctor()
        self.assertEqual(f["omp.version"]["level"], "fail")
        (self.s.fake / "omp.version").write_text("19.2.1")
        _, _, f = self.doctor()
        self.assertEqual(f["omp.version"]["level"], "info")
        self.assertIn("not one of the tested", f["omp.version"]["message"])

    def test_a_damaged_state_file_is_a_failure_with_a_way_out(self):
        (self.s.data_dir / "state.json").write_text("{nope")
        rc, _, f = self.doctor()
        self.assertEqual(rc, 1)
        self.assertIn("damaged", f["state"]["message"])


class HealthClassification(DoctorCase):
    def test_unreachable_stale_and_not_on_relay_are_told_apart(self):
        self.init()
        srv = support.Server()
        self.addCleanup(srv.stop)
        srv.route("/status", 200, json.dumps({"updatedAt": time.time() - 3600}))
        self.s.set_tailscale({"Web": {}})
        self.s.health(
            "plug",
            {"id": "down", "type": "port", "port": support.closed_port()},
            {
                "id": "old",
                "type": "url",
                "url": f"{srv.base}/status",
                "freshness": {"jsonPointer": "/updatedAt", "maxAgeSeconds": 60},
            },
            {"id": "route", "type": "serve-route", "port": 8443, "target": f"http://127.0.0.1:{srv.port}"},
            {"id": "fine", "type": "url", "url": f"{srv.base}/status"},
            {"id": "meh", "type": "port", "port": support.closed_port(), "severity": "note"},
        )
        rc, data, f = self.doctor()
        self.assertEqual(rc, 1)
        self.assertIn("unreachable", f["health.plug.down"]["message"])
        self.assertIn("stale", f["health.plug.old"]["message"])
        self.assertIn("not-on-relay", f["health.plug.route"]["message"])
        self.assertEqual(f["health.plug.fine"]["level"], "ok")
        self.assertEqual(f["health.plug.meh"]["level"], "warn", "a note-only check is a warning, not a failure")
        statuses = {r["id"]: r["status"] for r in data["health"]}
        self.assertEqual(
            statuses,
            {"down": "unreachable", "old": "stale", "route": "not-on-relay", "fine": "ok", "meh": "unreachable"},
        )

    def test_doctor_never_restarts_anything(self):
        self.s.services("plug", HUB_SCRIPT)
        self.init()
        self.s.set_state("osvtest.plug.hub", "waiting")
        self.s.health("plug", {"id": "w", "type": "watchdog", "service": "hub", "restart": {}})
        self.s.clear_calls()
        _, _, f = self.doctor()
        self.assertEqual(self.s.restarts(), [])
        self.assertIn("not-running-report-only", f["health.plug.w"]["message"])

    def test_no_health_flag_skips_checks(self):
        self.init()
        self.s.health("plug", {"id": "down", "type": "port", "port": support.closed_port()})
        rc, data, f = self.doctor("--no-health")
        self.assertEqual(rc, 0)
        self.assertEqual(data["health"], [])


class HeartbeatFindings(DoctorCase):
    def setUp(self):
        super().setUp()
        self.s.write_config(heartbeat={"enabled": True, "intervalSeconds": 60})
        self.init()
        secrets = self.s.config_dir / "secrets"
        (secrets / "heartbeat-url").write_text("https://hc.example/ping/x\n")
        os.chmod(secrets / "heartbeat-url", 0o600)

    def record(self, age: float, ok: bool = True, ping_ok: bool = True) -> None:
        (self.s.state_dir / "heartbeat-status.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "at": time.time() - age,
                    "intervalSeconds": 60,
                    "ok": ok,
                    "ping": {"attempted": True, "ok": ping_ok, "detail": "status 200" if ping_ok else "timeout"},
                    "results": [],
                }
            )
        )

    def test_missing_fresh_stale_and_failed_ping(self):
        _, _, f = self.doctor()
        self.assertEqual(f["heartbeat.last-run"]["level"], "warn")
        self.assertEqual(f["heartbeat.url"]["level"], "ok")
        self.record(10)
        _, _, f = self.doctor()
        self.assertEqual(f["heartbeat.last-run"]["level"], "ok")
        self.record(1000)
        rc, _, f = self.doctor()
        self.assertEqual((rc, f["heartbeat.last-run"]["level"]), (1, "fail"))
        self.assertIn("stale", f["heartbeat.last-run"]["message"])
        self.record(5, ping_ok=False)
        _, _, f = self.doctor()
        self.assertIn("ping failed", f["heartbeat.last-run"]["message"])

    def test_the_url_is_never_printed(self):
        rc, out, _ = self.s.cli("doctor", "--no-catalog")
        self.assertNotIn("hc.example", out)
        self.assertIn("never printed", out)

    def test_an_unusable_url_is_a_failure_without_echoing_it(self):
        (self.s.config_dir / "secrets" / "heartbeat-url").write_text("http://hc.example/ping/x\n")
        rc, out, _ = self.s.cli("doctor", "--no-catalog")
        self.assertEqual(rc, 1)
        self.assertNotIn("hc.example", out)
        self.assertIn("https", out)


class CatalogNotice(DoctorCase):
    def test_newer_version_available(self):
        self.init()
        tree = self.s.plugin_tree("omp-services", "0.1.0")
        self.s.omp_install("omp-services", "0.1.0", tree=tree)
        self.s.omp_install("other-plugin", "1.0.0")
        self.s.omp_catalog({"omp-services": "0.2.0", "other-plugin": "1.0.0"})
        rc, out, err = self.s.cli("doctor", "--json")
        data = {f["id"]: f for f in json.loads(out)["findings"]}
        self.assertEqual(rc, 0)
        self.assertIn("newer version available: omp-services 0.1.0 -> 0.2.0", data["update.omp-services"]["message"])
        self.assertNotIn("update.other-plugin", data)
        self.assertEqual(data["update.omp-services"]["level"], "info")

    def test_up_to_date_and_unreadable_catalog(self):
        self.init()
        self.s.omp_install("omp-services", "0.1.0", tree=self.s.plugin_tree("omp-services", "0.1.0"))
        self.s.omp_catalog({"omp-services": "0.1.0"})
        _, out, _ = self.s.cli("doctor", "--json")
        data = {f["id"]: f for f in json.loads(out)["findings"]}
        self.assertEqual(data["catalog"]["level"], "ok")
        (self.s.root / "omp" / "cache" / "marketplaces" / "omp-tether" / "marketplace.json").unlink()
        _, out, _ = self.s.cli("doctor", "--json")
        data = {f["id"]: f for f in json.loads(out)["findings"]}
        self.assertNotIn("update.omp-services", data)

    def test_no_network_unless_asked_to_refresh(self):
        self.init()
        self.s.omp_install("omp-services", "0.1.0", tree=self.s.plugin_tree("omp-services", "0.1.0"))
        self.s.omp_catalog({"omp-services": "0.1.0"})
        self.s.cli("doctor")
        self.assertFalse(any("marketplace update" in c for c in self.s.omp_calls()))
        state = self.s._omp_state()
        state["refresh"]["omp-tether"] = {
            "file": str(self.s.root / "omp" / "cache" / "marketplaces" / "omp-tether" / "marketplace.json"),
            "plugins": {"omp-services": "0.3.0"},
        }
        self.s._save_omp_state(state)
        _, out, _ = self.s.cli("doctor", "--refresh-catalog", "--json")
        self.assertTrue(any("marketplace update omp-tether" in c for c in self.s.omp_calls()))
        self.assertIn("0.1.0 -> 0.3.0", out)

    def test_the_omp_child_never_sees_bun_be_bun(self):
        self.init()
        self.s.omp_install("omp-services", "0.1.0", tree=self.s.plugin_tree("omp-services", "0.1.0"))
        self.s.omp_catalog({"omp-services": "0.1.0"})
        self.s.cli("doctor", extra_env={"BUN_BE_BUN": "1"})
        self.assertTrue(self.s.omp_calls())
        self.assertTrue(
            all(c.endswith("BUN_BE_BUN=") for c in self.s.omp_calls() if c.startswith("plugin")), self.s.omp_calls()
        )


if __name__ == "__main__":
    unittest.main()
