"""The configuration file: defaults, environment overrides, schema, and how the file itself is read."""

from __future__ import annotations

import json
import os
import re
import unittest
from pathlib import Path

from omp_dashboard import schema
from omp_dashboard.config import DEFAULTS, ENV_OVERRIDES, default_config_text, load_config
from omp_dashboard.paths import CONFIG_FILE_ENV, from_env
from omp_dashboard.util import DashboardError
from support import PLUGIN, Scratch


class ConfigCase(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = Scratch()
        self.addCleanup(self.scratch.close)

    def load(self, **env: str):
        e = dict(self.scratch.env)
        e.pop("OMP_DASHBOARD_PORT", None)
        e.update(env)
        return load_config(from_env(e), e)

    def write(self, text: str, mode: int = 0o600) -> Path:
        paths = self.scratch.paths()
        paths.config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        paths.config_file.write_text(text)
        os.chmod(paths.config_file, mode)
        return paths.config_file


class Defaults(ConfigCase):
    def test_no_file_means_safe_defaults(self) -> None:
        c = self.load()
        self.assertEqual(c.problems, [])
        self.assertFalse(c.file_present)
        self.assertEqual(c.get("listen.host"), "127.0.0.1")
        self.assertEqual(c.get("auth.adapter"), "loopback-only")
        self.assertEqual(c.get("start.backend"), "off")
        self.assertEqual(c.get("start.roots"), [])
        self.assertFalse(c.get("relay.enabled"))
        self.assertIsNone(c.get("machine.name"))
        self.assertEqual(c.get("alerts.server"), "https://ntfy.sh")  # a default server, but off: no topic, no messages

    def test_the_defaults_pass_their_own_schema_and_the_default_file_text_parses_back(self) -> None:
        self.assertEqual(schema.validate(DEFAULTS, schema.load_schema("config-1")), [])
        self.assertEqual(json.loads(default_config_text()), DEFAULTS)

    def test_every_default_matches_the_schema_default_and_every_schema_key_has_a_default(self) -> None:
        def walk(sch: dict, node: dict, where: str) -> None:
            for key, sub in sch.get("properties", {}).items():
                self.assertIn(key, node, f"{where}.{key} is in the schema but has no default")
                if "properties" in sub:
                    walk(sub, node[key], f"{where}.{key}")
                elif "default" in sub:
                    self.assertEqual(node[key], sub["default"], f"{where}.{key}")
            for key in node:
                if key != "schemaVersion":
                    self.assertIn(key, sch["properties"], f"{where}.{key} has a default but is not in the schema")

        walk(schema.load_schema("config-1"), DEFAULTS, "$")

    def test_paths_default_under_xdg_roots_and_never_under_a_fixed_home(self) -> None:
        c = self.load()
        home = self.scratch.home
        self.assertEqual(c.pairing_dir, home / ".config" / "omp-dashboard" / "pairing")
        self.assertEqual(c.ntfy_topic_file, home / ".config" / "omp-dashboard" / "secrets" / "ntfy-topic")
        self.assertEqual(c.audit_file, home / ".local" / "state" / "omp-dashboard" / "audit.jsonl")
        self.assertEqual(c.control_dir, home / ".config" / "omp-session-control")
        self.assertEqual(c.relay_check_dir, home / ".config" / "omp-private-relay" / "check")
        self.assertEqual(c.sessions_dir, home / ".omp" / "agent" / "sessions")

    def test_xdg_variables_move_the_defaults_and_a_relative_value_is_ignored(self) -> None:
        other = self.scratch.root / "cfg"
        c = self.load(XDG_CONFIG_HOME=str(other), XDG_STATE_HOME="relative/state")
        self.assertEqual(c.pairing_dir, other / "omp-dashboard" / "pairing")
        self.assertEqual(c.audit_file, self.scratch.home / ".local/state" / "omp-dashboard" / "audit.jsonl")

    def test_the_session_control_dir_follows_the_extensions_own_variable(self) -> None:
        c = self.load(OMP_SESSION_CONTROL_DIR=str(self.scratch.root / "ctl"))
        self.assertEqual(c.control_dir, self.scratch.root / "ctl")
        c = self.load(OMP_SESSION_CONTROL_DIR="relative")
        self.assertEqual(c.control_dir, self.scratch.home / ".config" / "omp-session-control")
        c = self.load(
            OMP_SESSION_CONTROL_DIR=str(self.scratch.root / "ctl"),
            OMP_DASHBOARD_CONTROL_DIR=str(self.scratch.root / "mine"),
        )
        self.assertEqual(c.control_dir, self.scratch.root / "mine")

    def test_no_default_names_a_host_a_home_directory_or_a_fixed_port_in_use_elsewhere(self) -> None:
        schema_text = json.dumps(
            {k: v for k, v in schema.load_schema("config-1").items() if k != "$id"}
        )  # $id is this repository's address
        text = json.dumps(DEFAULTS) + schema_text
        # Names of people and organisations are kept out by the publication check, which reads them from a file outside the repository.
        for word in (".ts.net", "/users/", "/home/", "homebrew"):
            self.assertNotIn(word, text.lower(), word)
        ports = {DEFAULTS["listen"]["port"], DEFAULTS["relay"]["hostPort"], DEFAULTS["relay"]["guestPort"]}
        live = (
            set(range(8790 + 3, 8790 + 9)) | {3800 + 50} | set(range(8800 + 88, 8899))
        )  # ports other tools commonly use
        self.assertFalse(ports & live)


class File(ConfigCase):
    def test_a_valid_file_overrides_defaults_and_is_reported_as_present(self) -> None:
        self.write(
            json.dumps(
                {"schemaVersion": 1, "listen": {"port": 4321}, "start": {"backend": "tmux", "roots": ["~/code"]}}
            )
        )
        c = self.load()
        self.assertEqual(c.problems, [])
        self.assertTrue(c.file_present)
        self.assertEqual(
            (c.get("listen.port"), c.get("start.backend"), c.get("start.roots")), (4321, "tmux", ["~/code"])
        )
        self.assertEqual(c.get("start.maxDepth"), 3)  # untouched keys keep their defaults

    def test_schema_version_is_required(self) -> None:
        self.write("{}")
        self.assertTrue(any("schemaVersion" in p for p in self.load().problems))
        self.write(json.dumps({"schemaVersion": 2}))
        self.assertTrue(any("schemaVersion" in p for p in self.load().problems))

    def test_unknown_keys_and_wrong_types_are_refused_with_where_they_are(self) -> None:
        self.write(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "listen": {"port": "80", "colour": 1},
                    "typo": True,
                    "alerts": {"server": "http://evil.example"},
                }
            )
        )
        problems = "\n".join(self.load().problems)
        for needle in ("$.listen.port", "colour", "typo", "$.alerts.server"):
            self.assertIn(needle, problems)

    def test_the_hub_cannot_be_told_to_bind_beyond_loopback(self) -> None:
        for host in ("0.0.0.0", "::", "192.168.1.5", "localhost", "example.com"):
            self.write(json.dumps({"schemaVersion": 1, "listen": {"host": host}}))
            self.assertTrue(any("listen.host" in p for p in self.load().problems), host)
        self.write(json.dumps({"schemaVersion": 1, "listen": {"host": "::1"}}))
        self.assertEqual(self.load().problems, [])

    def test_invalid_json_duplicates_and_a_non_object_are_refused(self) -> None:
        for text in ("{", '{"schemaVersion": 1, "schemaVersion": 1}', "[]", "null", "", '{"schemaVersion": 1} x'):
            self.write(text)
            self.assertTrue(self.load().problems, text)

    def test_control_characters_in_a_value_are_refused(self) -> None:
        self.write(json.dumps({"schemaVersion": 1, "machine": {"name": "Laptop\u0007"}}))
        self.assertTrue(self.load().problems)
        self.write(json.dumps({"schemaVersion": 1, "start": {"roots": ["/a\nb"]}}))
        self.assertTrue(self.load().problems)

    def test_a_file_that_group_or_others_can_write_is_not_used(self) -> None:
        self.write(json.dumps({"schemaVersion": 1, "listen": {"port": 4321}}), mode=0o666)
        c = self.load()
        self.assertTrue(any("chmod go-w" in p for p in c.problems))
        self.assertEqual(c.get("listen.port"), 17795)  # the defaults, never a partly-read file

    def test_a_file_in_a_directory_others_can_write_is_not_used(self) -> None:
        self.write(json.dumps({"schemaVersion": 1}))
        os.chmod(self.scratch.paths().config_dir, 0o777)
        self.assertTrue(any("writable by group or other" in p for p in self.load().problems))

    def test_a_fifo_named_like_the_file_cannot_stall_the_hub(self) -> None:
        paths = self.scratch.paths()
        paths.config_dir.mkdir(parents=True, mode=0o700)
        os.mkfifo(paths.config_file, 0o600)
        c = self.load()
        self.assertTrue(any("not a regular file" in p for p in c.problems))

    def test_a_link_to_a_file_of_ours_is_followed(self) -> None:
        real = self.scratch.root / "real.json"
        real.write_text(json.dumps({"schemaVersion": 1, "listen": {"port": 4242}}))
        os.chmod(real, 0o600)
        paths = self.scratch.paths()
        paths.config_dir.mkdir(parents=True, mode=0o700)
        paths.config_file.symlink_to(real)
        self.assertEqual(self.load().get("listen.port"), 4242)

    def test_an_oversized_file_is_refused(self) -> None:
        self.write(json.dumps({"schemaVersion": 1, "machine": {"name": "x"}}) + " " * (2 * 1024 * 1024))
        self.assertTrue(any("larger than" in p for p in self.load().problems))

    def test_config_env_names_another_file_and_must_be_absolute(self) -> None:
        other = self.scratch.root / "other.json"
        other.write_text(json.dumps({"schemaVersion": 1, "listen": {"port": 4545}}))
        os.chmod(other, 0o600)
        self.assertEqual(self.load(**{CONFIG_FILE_ENV: str(other)}).get("listen.port"), 4545)
        with self.assertRaises(DashboardError):
            self.load(**{CONFIG_FILE_ENV: "relative.json"})


class Environment(ConfigCase):
    def test_environment_beats_the_file_and_the_file_beats_the_defaults(self) -> None:
        self.write(json.dumps({"schemaVersion": 1, "listen": {"port": 4321}, "start": {"backend": "herdr"}}))
        c = self.load(OMP_DASHBOARD_PORT="5555")
        self.assertEqual((c.get("listen.port"), c.get("start.backend")), (5555, "herdr"))
        self.assertEqual(c.overrides, ["OMP_DASHBOARD_PORT"])
        self.assertEqual(c.effective_overrides(), {"OMP_DASHBOARD_PORT": "5555"})

    def test_a_bad_value_is_reported_and_names_the_variable(self) -> None:
        for name, value in (
            ("OMP_DASHBOARD_PORT", "abc"),
            ("OMP_DASHBOARD_RELAY_ENABLED", "maybe"),
            ("OMP_DASHBOARD_PORT", "70000"),
            ("OMP_DASHBOARD_AUTH", "none"),
        ):
            problems = self.load(**{name: value}).problems
            self.assertTrue(
                any(name in p or "listen.port" in p or "auth.adapter" in p for p in problems), (name, value, problems)
            )

    def test_roots_are_colon_separated_and_booleans_accept_the_usual_spellings(self) -> None:
        c = self.load(
            OMP_DASHBOARD_START_ROOTS="~/a:/b/c", OMP_DASHBOARD_RELAY_ENABLED="Yes", OMP_DASHBOARD_RANK_BY_SESSIONS="0"
        )
        self.assertEqual(c.get("start.roots"), ["~/a", "/b/c"])
        self.assertTrue(c.get("relay.enabled"))
        self.assertFalse(c.get("projects.rankBySessions"))

    def test_every_override_names_a_real_key_and_a_valid_sample_round_trips(self) -> None:
        samples = {
            "OMP_DASHBOARD_PORT": ("7000", 7000),
            "OMP_DASHBOARD_AUTH": ("tailscale-serve", "tailscale-serve"),
            "OMP_DASHBOARD_TAILSCALE_BIN": ("/usr/bin/ts", "/usr/bin/ts"),
            "OMP_DASHBOARD_TAILSCALE_SERVE_PORT": ("8443", 8443),
            "OMP_DASHBOARD_TAILSCALE_OWNER": ("me@example.test", "me@example.test"),
            "OMP_DASHBOARD_TAILSCALE_HOST": ("box.example.test", "box.example.test"),
            "OMP_DASHBOARD_PAIRING_DIR": ("/p", "/p"),
            "OMP_DASHBOARD_PAIRING_MAX_AGE_DAYS": ("30", 30),
            "OMP_DASHBOARD_PAIRING_LOCAL_MAX_AGE_DAYS": ("3", 3),
            "OMP_DASHBOARD_COOKIE_NAME": ("cookie_x", "cookie_x"),
            "OMP_DASHBOARD_OMP_BIN": ("/bin/omp", "/bin/omp"),
            "OMP_DASHBOARD_CONTROL_DIR": ("/c", "/c"),
            "OMP_DASHBOARD_RELAY_ENABLED": ("true", True),
            "OMP_DASHBOARD_RELAY_HOST_PORT": ("7001", 7001),
            "OMP_DASHBOARD_RELAY_GUEST_PORT": ("7002", 7002),
            "OMP_DASHBOARD_RELAY_CHECK_DIR": ("/k", "/k"),
            "OMP_DASHBOARD_RELAY_WEB_URL": ("http://127.0.0.1:7002", "http://127.0.0.1:7002"),
            "OMP_DASHBOARD_RELAY_SERVE_PORT": ("7003", 7003),
            "OMP_DASHBOARD_RELAY_ADMITS": ("tailnet", "tailnet"),
            "OMP_DASHBOARD_START_BACKEND": ("tmux", "tmux"),
            "OMP_DASHBOARD_START_ROOTS": ("/r1:/r2", ["/r1", "/r2"]),
            "OMP_DASHBOARD_HERDR_BIN": ("/h", "/h"),
            "OMP_DASHBOARD_TMUX_BIN": ("/t", "/t"),
            "OMP_DASHBOARD_TMUX_SESSION": ("sess", "sess"),
            "OMP_DASHBOARD_RANK_BY_SESSIONS": ("true", True),
            "OMP_DASHBOARD_SESSIONS_DIR": ("/s", "/s"),
            "OMP_DASHBOARD_NTFY_SERVER": ("https://ntfy.example.test", "https://ntfy.example.test"),
            "OMP_DASHBOARD_NTFY_TOPIC_FILE": ("/n", "/n"),
            "OMP_DASHBOARD_ALERT_MIN_TURN_SECONDS": ("90", 90),
            "OMP_DASHBOARD_STATS_URL": ("http://127.0.0.1:3000/", "http://127.0.0.1:3000/"),
            "OMP_DASHBOARD_AUDIT_FILE": ("/a.jsonl", "/a.jsonl"),
            "OMP_DASHBOARD_MACHINE_NAME": ("Box", "Box"),
        }
        self.assertEqual(
            set(samples), set(ENV_OVERRIDES), "a variable was added or removed: update this table and the README"
        )
        for name, (raw, want) in samples.items():
            c = self.load(**{name: raw})
            self.assertEqual(c.problems, [], name)
            node = c.values
            for key in ENV_OVERRIDES[name][0]:
                node = node[key]
            self.assertEqual(node, want, name)

    def test_relative_and_empty_values_do_not_apply(self) -> None:
        self.assertEqual(self.load(OMP_DASHBOARD_PORT="").overrides, [])
        self.assertTrue(self.load(OMP_DASHBOARD_PAIRING_DIR="relative").problems)


class Semantics(ConfigCase):
    def test_relay_ports_must_differ_from_each_other_and_from_the_hub(self) -> None:
        self.write(json.dumps({"schemaVersion": 1, "relay": {"enabled": True, "hostPort": 7000, "guestPort": 7000}}))
        self.assertTrue(any("must differ" in p for p in self.load().problems))
        self.write(
            json.dumps({"schemaVersion": 1, "listen": {"port": 7000}, "relay": {"enabled": True, "hostPort": 7000}})
        )
        self.assertTrue(any("hub's own port" in p for p in self.load().problems))

    def test_serve_port_defaults_to_the_listen_port(self) -> None:
        self.write(json.dumps({"schemaVersion": 1, "listen": {"port": 7000}}))
        self.assertEqual(self.load().serve_port, 7000)
        self.write(
            json.dumps({"schemaVersion": 1, "listen": {"port": 7000}, "auth": {"tailscale": {"servePort": 443}}})
        )
        self.assertEqual(self.load().serve_port, 443)


class SchemaFile(unittest.TestCase):
    def test_only_keywords_the_validator_implements_are_used(self) -> None:
        schema.check_schema_keywords(schema.load_schema("config-1"))

    def test_the_schema_has_no_secret_keys(self) -> None:
        text = (PLUGIN / "python/omp_dashboard/schemas/config-1.schema.json").read_text()
        for word in ('"topic"', "password", "token", "cookieValue", '"key"'):
            self.assertNotIn(word, text)
        self.assertRegex(text, re.compile(r'"topicFile"'))


if __name__ == "__main__":
    unittest.main()
