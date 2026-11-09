"""The schema validator, the schema files, and the configuration loader."""

from __future__ import annotations

import json
import os
import unittest

import support
from omp_services import config, schema
from omp_services import paths as paths_mod
from omp_services.util import ServicesError


class ValidatorTests(unittest.TestCase):
    def test_types_and_bool_is_not_a_number(self):
        self.assertEqual(schema.validate(1, {"type": "integer"}), [])
        self.assertTrue(schema.validate(True, {"type": "integer"}))
        self.assertTrue(schema.validate(1.5, {"type": "integer"}))
        self.assertEqual(schema.validate(None, {"type": ["string", "null"]}), [])
        self.assertTrue(schema.validate({}, {"type": "array"}))

    def test_object_rules(self):
        s = {
            "type": "object",
            "additionalProperties": False,
            "required": ["a"],
            "properties": {
                "a": {"type": "string", "pattern": "^x"},
                "b": {"type": "integer", "minimum": 1, "maximum": 3},
            },
        }
        self.assertEqual(schema.validate({"a": "xy", "b": 2}, s), [])
        errors = schema.validate({"b": 9, "c": 1}, s)
        self.assertTrue(any("missing required field 'a'" in e for e in errors))
        self.assertTrue(any("above the maximum" in e for e in errors))
        self.assertTrue(any("unknown field 'c'" in e for e in errors))
        self.assertTrue(schema.validate({"a": "no"}, s))

    def test_arrays_and_enums(self):
        s = {"type": "array", "minItems": 1, "maxItems": 2, "uniqueItems": True, "items": {"enum": ["a", "b"]}}
        self.assertEqual(schema.validate(["a"], s), [])
        self.assertTrue(schema.validate([], s))
        self.assertTrue(schema.validate(["a", "b", "a"], s))
        self.assertTrue(schema.validate(["c"], s))

    def test_one_of_names_the_alternative_the_type_field_points_at(self):
        s = {
            "oneOf": [
                {
                    "type": "object",
                    "required": ["type", "x"],
                    "properties": {"type": {"const": "one"}, "x": {"type": "integer"}},
                },
                {"type": "object", "required": ["type"], "properties": {"type": {"const": "two"}}},
            ]
        }
        self.assertEqual(schema.validate({"type": "two"}, s), [])
        errors = schema.validate({"type": "one", "x": "no"}, s)
        self.assertEqual(len(errors), 1)
        self.assertIn("$.x", errors[0])
        self.assertIn("unknown type", schema.validate({"type": "three"}, s)[0])

    def test_ref_and_unknown_keywords(self):
        root = {"$defs": {"n": {"type": "integer"}}, "properties": {"v": {"$ref": "#/$defs/n"}}}
        self.assertTrue(schema.validate({"v": "x"}, root))
        with self.assertRaises(schema.SchemaError):
            schema.check_schema_keywords({"format": "uri"})

    def test_every_schema_file_uses_only_supported_keywords(self):
        for name in ("config-1", "health-1", "services-1"):
            schema.check_schema_keywords(schema.load_schema(name))


class ConfigTests(support.Case):
    def load(self, extra_env=None):
        env = {**self.s.env, **(extra_env or {})}
        return config.load_config(paths_mod.from_env(env, "darwin"), env)

    def test_defaults_match_the_schema_defaults(self):
        spec = schema.load_schema("config-1")["properties"]

        def check(defaults, props):
            for key, value in defaults.items():
                if key == "schemaVersion":
                    continue
                self.assertIn(key, props, key)
                if isinstance(value, dict):
                    check(value, props[key]["properties"])
                else:
                    self.assertEqual(props[key]["default"], value, key)

        check(config.DEFAULTS, spec)
        self.assertEqual(schema.validate(config.DEFAULTS, schema.load_schema("config-1")), [])
        self.assertEqual(json.loads(config.default_config_text()), config.DEFAULTS)

    def test_nothing_is_on_by_default(self):
        cfg = self.load({"OMP_SERVICES_LABEL_PREFIX": ""})
        self.assertFalse(cfg.heartbeat["enabled"])
        self.assertIsNone(cfg.heartbeat["urlFile"])
        self.assertFalse(cfg.allow_remote_urls)
        self.assertEqual(cfg.problems, [])

    def test_file_then_environment(self):
        self.s.write_config(heartbeat={"intervalSeconds": 60}, labelPrefix="fromfile")
        cfg = self.load({"OMP_SERVICES_LABEL_PREFIX": ""})
        self.assertEqual(cfg.label_prefix, "fromfile")
        self.assertEqual(cfg.heartbeat["intervalSeconds"], 60)
        self.assertEqual(cfg.heartbeat["pingTimeoutSeconds"], 15)  # untouched default
        cfg = self.load({"OMP_SERVICES_LABEL_PREFIX": "fromenv", "OMP_SERVICES_HEARTBEAT_INTERVAL_SECONDS": "90"})
        self.assertEqual((cfg.label_prefix, cfg.heartbeat["intervalSeconds"]), ("fromenv", 90))
        self.assertIn("OMP_SERVICES_LABEL_PREFIX", cfg.overrides)

    def test_problems_are_reported_not_hidden(self):
        self.s.write_config(heartbeat={"intervalSeconds": 5}, surprise=True)
        cfg = self.load()
        self.assertEqual(len(cfg.problems), 2, cfg.problems)
        with self.assertRaises(ServicesError):
            cfg.require_valid()
        bad = self.load({"OMP_SERVICES_HEARTBEAT_ENABLED": "maybe", "OMP_SERVICES_LABEL_PREFIX": "UPPER"})
        self.assertTrue(any("HEARTBEAT_ENABLED" in p for p in bad.problems))
        self.assertTrue(any("labelPrefix" in p for p in bad.problems))

    def test_invalid_json_and_wrong_top_level(self):
        self.s.config_dir.mkdir(parents=True)
        os.chmod(
            self.s.config_dir, 0o700
        )  # whatever the umask: a group-writable directory is refused before the JSON is read
        (self.s.config_dir / "config.json").write_text("{nope")
        os.chmod(self.s.config_dir / "config.json", 0o600)
        self.assertTrue(any("not valid JSON" in p for p in self.load().problems))
        (self.s.config_dir / "config.json").write_text("[]")
        self.assertTrue(any("top level" in p for p in self.load().problems))

    def test_relative_paths_are_refused(self):
        self.s.write_config(omp={"path": "omp"})
        self.assertTrue(self.load().problems)


class PathsTests(unittest.TestCase):
    def test_xdg_roots_and_overrides(self):
        env = {"HOME": "/h", "XDG_CONFIG_HOME": "/x/c", "XDG_DATA_HOME": "/x/d", "XDG_STATE_HOME": "/x/s"}
        p = paths_mod.from_env(env, "linux")
        self.assertEqual(str(p.config_dir), "/x/c/omp-services")
        self.assertEqual(str(p.data_dir), "/x/d/omp-services")
        self.assertEqual(str(p.state_dir), "/x/s/omp-services")
        self.assertEqual(str(p.unit_dir), "/x/c/systemd/user")
        mac = paths_mod.from_env({"HOME": "/h"}, "darwin")
        self.assertEqual(str(mac.unit_dir), "/h/Library/LaunchAgents")
        self.assertEqual(str(mac.config_dir), "/h/.config/omp-services")

    def test_relative_xdg_values_are_ignored_and_relative_overrides_refused(self):
        p = paths_mod.from_env({"HOME": "/h", "XDG_CONFIG_HOME": "relative"}, "linux")
        self.assertEqual(str(p.config_dir), "/h/.config/omp-services")
        with self.assertRaises(ServicesError):
            paths_mod.from_env({"HOME": "/h", "OMP_SERVICES_DATA_DIR": "rel"}, "linux")


if __name__ == "__main__":
    unittest.main()
