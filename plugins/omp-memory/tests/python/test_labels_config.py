"""`labels` (the label store file and rules) and `config` (paths, environment overrides, refusals)."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import unittest

from support import ScratchCase


class LabelsTest(ScratchCase):
    def setUp(self):
        super().setUp()
        self.db = self.s.bank("sms-1qfllt3yk702w", "projects/shop-site/sms")
        self.s.bank("tools-2guil9dlpgfxr", "elsewhere/tools")
        self.file = self.s.home / ".config" / "omp-memory" / "labels.json"

    def test_set_list_clear_round_trip_in_the_shared_file_format(self):
        r = self.s.run("labels", "set", "sms-1qfllt3yk702w", "SMS app")
        self.assertEqual(r.code, 0, r.err)
        self.assertEqual(
            json.loads(self.file.read_text()), {"version": 1, "labels": {"sms-1qfllt3yk702w": {"label": "SMS app"}}}
        )
        listing = self.s.run("labels", "list", "--json").json()["banks"]
        self.assertEqual(listing["sms-1qfllt3yk702w"]["label"], "SMS app")
        self.assertTrue(listing["sms-1qfllt3yk702w"]["labelCustom"])
        self.assertIn("custom", self.s.run("labels", "list").out)
        self.assertEqual(self.s.run("banks", "--json").json()["banks"][0]["folder"]["label"], "SMS app")
        self.assertEqual(self.s.run("labels", "clear", "sms-1qfllt3yk702w").code, 0)
        self.assertEqual(json.loads(self.file.read_text())["labels"], {})
        self.assertEqual(
            self.s.run("labels", "list", "--json").json()["banks"]["sms-1qfllt3yk702w"]["label"],
            "shop-site/sms",
        )

    def test_the_file_is_private_and_written_atomically(self):
        self.s.run("labels", "set", "sms-1qfllt3yk702w", "SMS")
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.file.parent.stat().st_mode), 0o700)
        self.assertEqual([p.name for p in self.file.parent.iterdir()], [self.file.name])  # no temporary file left

    def test_a_label_may_have_spaces_without_quotes(self):
        self.assertEqual(self.s.run("labels", "set", "sms-1qfllt3yk702w", "Billing", "service").code, 0)
        self.assertEqual(json.loads(self.file.read_text())["labels"]["sms-1qfllt3yk702w"]["label"], "Billing service")

    def test_length_rules_one_to_sixty_characters(self):
        ok = self.s.run("labels", "set", "sms-1qfllt3yk702w", "x" * 60)
        self.assertEqual(ok.code, 0, ok.err)
        for bad in ("x" * 61, "   "):
            r = self.s.run("labels", "set", "sms-1qfllt3yk702w", bad)
            self.assertNotEqual(r.code, 0, bad)
            self.assertIn("1 to 60", r.err)
        self.assertEqual(json.loads(self.file.read_text())["labels"]["sms-1qfllt3yk702w"]["label"], "x" * 60)

    def test_control_and_bidi_characters_are_refused(self):
        for bad in ("a\nb", "a\u202eb", "a\x07b"):
            r = self.s.run("labels", "set", "sms-1qfllt3yk702w", bad)
            self.assertEqual(r.code, 1, repr(bad))
            self.assertIn("control characters", r.err)
        self.assertFalse(self.file.exists())

    def test_unknown_bank_and_duplicate_label_are_refused_and_change_nothing(self):
        r = self.s.run("labels", "set", "nope-1abc", "X")
        self.assertEqual(r.code, 1)
        self.assertIn("no mnemopi bank", r.err)
        self.assertEqual(self.s.run("labels", "set", "sms-1qfllt3yk702w", "Shared").code, 0)
        before = self.file.read_text()
        r = self.s.run("labels", "set", "tools-2guil9dlpgfxr", "shared")  # case-insensitive clash
        self.assertEqual(r.code, 1)
        self.assertIn("already has that label", r.err)
        self.assertEqual(self.file.read_text(), before)

    def test_malformed_bank_name_cannot_escape_the_banks_directory(self):
        r = self.s.run("labels", "set", "../x", "X")
        self.assertEqual(r.code, 1)
        self.assertFalse(self.file.exists())

    def test_clear_of_a_vanished_bank_drops_the_stale_label(self):
        self.s.run("labels", "set", "tools-2guil9dlpgfxr", "Tools")
        (self.s.banks / "tools-2guil9dlpgfxr" / "mnemopi.db").unlink()
        self.assertIn("stale label", self.s.run("labels", "list").out)
        self.assertEqual(self.s.run("labels", "clear", "tools-2guil9dlpgfxr").code, 0)
        self.assertEqual(json.loads(self.file.read_text())["labels"], {})
        self.assertEqual(self.s.run("labels", "clear", "tools-2guil9dlpgfxr").code, 1)

    def test_usage_errors(self):
        self.assertEqual(self.s.run("labels").code, 2)
        self.assertEqual(self.s.run("labels", "set").code, 2)
        self.assertEqual(self.s.run("labels", "set", "sms-1qfllt3yk702w").code, 2)
        self.assertEqual(self.s.run("labels", "clear").code, 2)
        self.assertEqual(self.s.run("labels", "rename", "a", "b").code, 2)

    def test_a_label_never_touches_the_bank(self):
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        self.s.run("labels", "set", "sms-1qfllt3yk702w", "SMS")
        self.s.run("labels", "clear", "sms-1qfllt3yk702w")
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)

    def test_labels_file_can_be_moved_with_the_environment(self):
        target = self.s.root / "elsewhere" / "labels.json"
        env = self.s.env(OMP_MEMORY_LABELS_FILE=str(target))
        self.assertEqual(self.s.run("labels", "set", "sms-1qfllt3yk702w", "SMS", env=env).code, 0)
        self.assertTrue(target.exists())
        self.assertFalse(self.file.exists())


class ConfigTest(ScratchCase):
    def settings(self, env=None):
        return self.s.run("config", "show", "--json", env=env).json()["settings"]

    def test_the_help_text_does_not_describe_a_browser_page_this_plugin_does_not_have(self):
        shown = self.s.run("config", "show")
        self.assertEqual(shown.code, 0, shown.err)
        self.assertNotIn("dashboard", (shown.out + shown.err).lower())
        self.assertNotIn("dashboard", self.s.run("config", "show", "--json").out.lower())

    def test_defaults(self):
        got = self.settings()
        home = str(self.s.home)
        self.assertEqual(got["banksDir"]["value"], f"{home}/.omp/agent/memories/mnemopi/banks")
        self.assertEqual(got["ledger"]["value"], f"{home}/.config/omp-memory-import/ledger.json")
        self.assertEqual(got["backupsDir"]["value"], f"{home}/Backups/omp-memory")
        self.assertEqual(got["labelsFile"]["value"], f"{home}/.config/omp-memory/labels.json")
        self.assertEqual(got["stateFile"]["value"], f"{home}/.config/omp-memory/state.json")
        # nothing is assumed about where your projects live or which omp source or conf file you have
        for key in ("libDir", "banksConf", "bun", "ompBin"):
            self.assertEqual(got[key], {"value": None, "source": "unset", "env": None}, key)
        self.assertNotIn("ompSrcDir", got)
        self.assertTrue(all(v["source"] in ("default", "unset") for v in got.values()))

    def test_agent_dir_moves_the_default_banks_dir(self):
        got = self.settings(self.s.env(PI_CODING_AGENT_DIR=str(self.s.root / "agent")))
        self.assertEqual(got["banksDir"]["value"], f"{self.s.root}/agent/memories/mnemopi/banks")

    def test_precedence_flag_over_env_over_file_over_default(self):
        file = self.s.root / "cfg.json"
        file.write_text(json.dumps({"schemaVersion": 1, "backupsDir": "/tmp/from-file"}))
        file.chmod(0o600)
        env = self.s.env(OMP_MEMORY_CONFIG=str(file))
        self.assertEqual(self.settings(env)["backupsDir"], {"value": "/tmp/from-file", "source": "config", "env": None})
        env["OMP_MEMORY_BACKUPS_DIR"] = "/tmp/from-env"
        self.assertEqual(
            self.settings(env)["backupsDir"],
            {"value": "/tmp/from-env", "source": "env", "env": "OMP_MEMORY_BACKUPS_DIR"},
        )
        self.s.bank("sms-1qfllt3yk702w")  # a backup plan needs a bank to plan for
        plan = self.s.run("backup", "--dest", "/tmp/from-flag", env=env)
        self.assertIn("/tmp/from-flag", plan.out)

    def test_set_and_unset_write_a_private_file(self):
        r = self.s.run("config", "set", "backupsDir", "~/mybackups")
        self.assertEqual(r.code, 0, r.err)
        file = self.s.home / ".config" / "omp-memory" / "config.json"
        self.assertEqual(
            json.loads(file.read_text()),
            {"kind": "omp-memory-config", "schemaVersion": 1, "backupsDir": f"{self.s.home}/mybackups"},
        )
        self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(file.parent.stat().st_mode), 0o700)
        self.assertEqual(self.settings()["backupsDir"]["source"], "config")
        self.assertEqual(self.s.run("config", "unset", "backupsDir").code, 0)
        self.assertEqual(self.settings()["backupsDir"]["source"], "default")

    def test_set_refuses_bad_values(self):
        for args in (
            ("nope", "/tmp/x"),
            ("banksDir", "relative/path"),
            ("banksDir", "/tmp/a/../b"),
            ("bun", str(self.s.root / "missing")),
            ("banksDir",),
        ):
            r = self.s.run("config", "set", *args)
            self.assertEqual(r.code, 2, args)
        self.assertFalse((self.s.home / ".config" / "omp-memory").exists())

    def test_set_library_directory_and_bun_accept_real_ones(self):
        self.assertEqual(self.s.run("config", "set", "libDir", str(self.s.root / "lib")).code, 0)
        self.assertEqual(self.s.run("config", "set", "bun", str(self.s.fake_bun)).code, 0)
        got = self.settings()
        self.assertEqual((got["libDir"]["source"], got["bun"]["source"]), ("config", "config"))

    def test_a_config_file_that_others_can_write_is_refused(self):
        file = self.s.home / ".config" / "omp-memory" / "config.json"
        file.parent.mkdir(parents=True)
        file.write_text(json.dumps({"schemaVersion": 1, "bun": "/tmp/evil"}))
        file.chmod(0o666)
        r = self.s.run("config", "show")
        self.assertEqual(r.code, 2)
        self.assertIn("writable by group or others", r.err)

    def test_a_config_file_with_unknown_keys_lists_every_problem(self):
        file = self.s.home / ".config" / "omp-memory" / "config.json"
        file.parent.mkdir(parents=True)
        file.write_text(json.dumps({"schemaVersion": 2, "zzz": "a", "banksDir": 3}))
        file.chmod(0o600)
        r = self.s.run("config", "show")
        self.assertEqual(r.code, 2)
        for fragment in ("schemaVersion must be 1", "unknown key 'zzz'", "banksDir must be a non-empty string"):
            self.assertIn(fragment, r.err)

    def test_a_symlinked_config_file_is_refused(self):
        real = self.s.root / "real.json"
        real.write_text(json.dumps({"schemaVersion": 1}))
        real.chmod(0o600)
        file = self.s.home / ".config" / "omp-memory" / "config.json"
        file.parent.mkdir(parents=True)
        os.symlink(real, file)
        r = self.s.run("config", "show")
        self.assertEqual(r.code, 2)
        self.assertIn("symbolic link", r.err)

    def test_relative_env_paths_are_refused(self):
        r = self.s.run("banks", env=self.s.env(OMP_MEMORY_BANKS_DIR="relative"))
        self.assertEqual(r.code, 2)
        self.assertIn("absolute", r.err)


class GroupsTest(ScratchCase):
    def run_config(self, *args):
        return self.s.run("config", *args)

    def stored(self):
        file = self.s.home / ".config" / "omp-memory" / "config.json"
        return json.loads(file.read_text())

    def test_set_group_stores_folders_and_show_lists_them(self):
        r = self.run_config("set-group", "Shop", "work/shop", "~/clients", "/srv/x")
        self.assertEqual(r.code, 0, r.err)
        self.assertEqual(
            self.stored(),
            {"kind": "omp-memory-config", "schemaVersion": 1, "groups": {"Shop": ["work/shop", "~/clients", "/srv/x"]}},
        )
        shown = self.run_config("show", "--json").json()
        self.assertEqual(shown["groups"], {"Shop": ["work/shop", "~/clients", "/srv/x"]})
        self.assertIn("Shop: work/shop, ~/clients, /srv/x", self.run_config("show").out)
        self.assertEqual(stat.S_IMODE((self.s.home / ".config" / "omp-memory" / "config.json").stat().st_mode), 0o600)

    def test_a_group_is_replaced_by_setting_it_again_and_removed_by_unset_group(self):
        self.run_config("set-group", "Shop", "a")
        self.run_config("set-group", "Shop", "b")
        self.run_config("set-group", "Other-group", "c")
        self.assertEqual(self.stored()["groups"], {"Other-group": ["c"], "Shop": ["b"]})
        self.assertEqual(self.run_config("unset-group", "Shop").code, 0)
        self.assertEqual(self.stored()["groups"], {"Other-group": ["c"]})
        self.assertEqual(self.run_config("unset-group", "Shop").code, 2)  # no longer there
        self.run_config("unset-group", "Other-group")
        self.assertNotIn("groups", self.stored())
        self.assertIn("groups: none set", self.run_config("show").out)

    def test_bad_groups_are_refused(self):
        for args in (
            ("set-group",),
            ("set-group", "Shop"),
            ("set-group", "", "x"),
            ("set-group", "Shop", "a/../b"),
            ("set-group", "x" * 61, "a"),
        ):
            self.assertEqual(self.run_config(*args).code, 2, args)
        self.assertFalse((self.s.home / ".config" / "omp-memory").exists())

    def test_a_config_file_with_bad_groups_lists_the_problems(self):
        file = self.s.home / ".config" / "omp-memory" / "config.json"
        file.parent.mkdir(parents=True)
        file.write_text(json.dumps({"schemaVersion": 1, "groups": {"A": "not-a-list", "B": [], "C": ["x", 3]}}))
        file.chmod(0o600)
        r = self.run_config("show")
        self.assertEqual(r.code, 2)
        for fragment in (
            "group 'A' must be a non-empty list",
            "group 'B' must be a non-empty list",
            "group 'C' has an empty or invalid folder",
        ):
            self.assertIn(fragment, r.err)

    def test_banks_use_the_first_directory_below_home_unless_a_group_names_the_folder(self):
        self.s.bank("api-1aaa", "projects/demo/api")
        self.s.bank("site-1bbb", "work/team/site")
        self.s.bank("docs-1ccc", "projects/demo/docs")
        self.s.bank("far-1ddd", "elsewhere/x")
        groups = {b["bank"]: b["folder"]["group"] for b in self.s.run("banks", "--json").json()["banks"]}
        self.assertEqual(
            groups, {"api-1aaa": "projects", "site-1bbb": "work", "docs-1ccc": "projects", "far-1ddd": "elsewhere"}
        )
        self.run_config("set-group", "Demo", "projects/demo")
        self.run_config("set-group", "Docs", "~/projects/demo/docs")
        groups = {b["bank"]: b["folder"]["group"] for b in self.s.run("banks", "--json").json()["banks"]}
        self.assertEqual(
            groups, {"api-1aaa": "Demo", "site-1bbb": "work", "docs-1ccc": "Docs", "far-1ddd": "elsewhere"}
        )

    def test_a_conf_file_is_used_only_when_the_user_names_it(self):
        self.s.bank("api-1aaa", "projects/demo/api")
        conf = self.s.home / ".config" / "omp-memory" / "memory-banks.conf"
        conf.parent.mkdir(parents=True)
        conf.write_text("shop projects/demo\n")
        before = self.s.run("banks", "--json").json()["banks"][0]["folder"]["group"]
        self.assertEqual(before, "projects")  # the old default location of the file is not read
        env = self.s.env(OMP_MEMORY_BANKS_CONF=str(conf))
        self.assertEqual(self.s.run("banks", "--json", env=env).json()["banks"][0]["folder"]["group"], "shop")


if __name__ == "__main__":
    unittest.main()
