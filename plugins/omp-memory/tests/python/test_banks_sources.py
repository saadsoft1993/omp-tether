"""`banks` and `sources`: read-only views of the banks directory and of the places memory comes from."""

from __future__ import annotations

import hashlib
import json
import unittest

from support import ScratchCase


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class BanksTest(ScratchCase):
    def test_lists_label_group_path_counts_and_source_breakdown(self):
        s = self.s
        s.bank("sms-1qfllt3yk702w", "projects/shop-site/sms", wm=(("w1", "a", "bank"), ("w2", "b", "global")))
        s.bank("tools-2guil9dlpgfxr", "elsewhere/tools")
        result = s.run("banks", "--json")
        self.assertEqual(result.code, 0, result.err)
        data = result.json()
        by_bank = {b["bank"]: b for b in data["banks"]}
        sms = by_bank["sms-1qfllt3yk702w"]
        self.assertEqual(sms["folder"]["label"], "shop-site/sms")
        self.assertEqual(sms["folder"]["group"], "projects")  # the first directory below home
        self.assertEqual(sms["folder"]["path"], str(s.home / "projects/shop-site/sms"))
        self.assertEqual(sms["counts"]["total"], 2)
        self.assertEqual(sms["sources"], {"omp": 2, "claude-code": 0, "hindsight": 0, "claude-mem": 0})
        self.assertEqual(by_bank["tools-2guil9dlpgfxr"]["folder"]["group"], "elsewhere")

    def test_imported_rows_are_counted_by_source(self):
        s = self.s
        db = s.bank("sms-1qfllt3yk702w")
        import sqlite3

        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE working_memory SET metadata_json = ? WHERE id = 'w1'",
            (
                json.dumps(
                    {"source": "claude-code", "importedAt": "2026-11-10T10:00:00Z", "cwd": str(s.home / "work/alpha")}
                ),
            ),
        )
        conn.commit()
        conn.close()
        bank = s.run("banks", "--json").json()["banks"][0]
        self.assertEqual(bank["sources"]["claude-code"], 1)
        text = s.run("banks").out
        self.assertIn("claude-code 1", text)

    def test_a_home_that_is_a_symbolic_link_still_shows_folders_under_tilde(self):
        """macOS's /tmp and a home on another mount: omp records the real path, $HOME names the link."""
        s = self.s
        s.bank("sms-1qfllt3yk702w", "work/sms")  # the recorded folder is the real path below the real home
        link = s.root / "home-link"
        link.symlink_to(s.home)
        env = s.env(HOME=str(link))
        for command in (("banks",), ("labels", "list")):
            out = s.run(*command, env=env).out
            self.assertIn("~/work/sms", out, out)
            self.assertNotIn(str(s.home), out)
            self.assertNotIn(str(link), out)

    def test_text_table_has_a_header_and_the_folder(self):
        self.s.bank("sms-1qfllt3yk702w", "work/sms")
        out = self.s.run("banks").out
        self.assertIn("BANK", out.splitlines()[2])
        self.assertIn("~/work/sms", out)
        self.assertNotIn(str(self.s.home), out)  # paths are shown relative to ~

    def test_empty_banks_are_listed_separately_and_an_unreadable_one_hides_nothing(self):
        s = self.s
        s.bank("good-1abc", "work/good")
        s.bank("empty-1def", "work/empty", wm=())
        bad = s.banks / "broken-1ghi" / "mnemopi.db"
        bad.parent.mkdir(parents=True)
        bad.write_bytes(b"this is not a database" * 100)
        data = s.run("banks", "--json").json()
        self.assertEqual([b["bank"] for b in data["banks"]], ["good-1abc"])
        self.assertEqual(data["emptyBanks"], ["empty-1def"])
        self.assertEqual([u["bank"] for u in data["unreadable"]], ["broken-1ghi"])
        self.assertIn("unreadable: broken-1ghi", s.run("banks").out)

    def test_banks_dir_flag_and_missing_directory(self):
        s = self.s
        s.bank("good-1abc", "work/good")
        other = s.root / "other-banks"
        self.assertEqual(s.run("banks", "--banks-dir", str(other), "--json").json()["banks"], [])
        self.assertEqual(s.run("banks", "--banks-dir", str(s.banks), "--json").json()["banks"][0]["bank"], "good-1abc")

    def test_reading_never_changes_a_bank(self):
        db = self.s.bank("good-1abc", "work/good")
        before = digest(db)
        for args in (("banks",), ("sources",), ("labels", "list"), ("doctor",)):
            self.s.run(*args)
        self.assertEqual(digest(db), before)
        self.assertFalse(
            (self.s.home / ".config" / "omp-memory" / "labels.json").exists()
        )  # nothing created by reading


class SourcesTest(ScratchCase):
    def by_id(self, result):
        return {entry["id"]: entry for entry in result.json()["sources"]}

    def test_nothing_installed(self):
        got = self.by_id(self.s.run("sources", "--json"))
        self.assertEqual(list(got), ["omp-mnemopi", "claude-code", "codex", "claude-mem", "instructions"])
        self.assertFalse(got["claude-code"]["available"])
        self.assertFalse(got["codex"]["available"])
        self.assertFalse(got["claude-mem"]["available"])

    def test_counts_for_every_source(self):
        s = self.s
        s.bank("good-1abc", "work/good")
        s.claude_memory("-a", {"one.md": "x", "two.md": "y", "MEMORY.md": "index"})
        s.claude_memory("-b", {"three.md": "z"}, root=".claude-accounts")
        s.codex_db(rows=4)
        s.claude_mem_db(observations=5, summaries=2, prompts=7)
        (s.home / "work" / "good").mkdir(parents=True)
        (s.home / "work" / "good" / "CLAUDE.md").write_text("rules")  # below the first directory of a bank's folder
        (s.home / "elsewhere").mkdir()
        (s.home / "elsewhere" / "CLAUDE.md").write_text("not scanned: no bank and no group is there")
        got = self.by_id(s.run("sources", "--json"))
        self.assertEqual(got["omp-mnemopi"]["counts"]["banks"], 1)
        self.assertEqual(got["claude-code"]["counts"]["memory files"], 3)  # MEMORY.md is an index, not a memory
        self.assertEqual(got["claude-code"]["counts"]["project folders"], 2)
        self.assertEqual(got["codex"]["counts"], {"stage1 outputs": 4})
        self.assertEqual(got["claude-mem"]["counts"], {"observations": 5, "session_summaries": 2, "user_prompts": 7})
        self.assertEqual(got["instructions"]["counts"]["files"], 1)

    def test_instruction_files_are_looked_for_in_the_groups_roots_too(self):
        s = self.s
        (s.home / "elsewhere").mkdir()
        (s.home / "elsewhere" / "CLAUDE.md").write_text("rules")
        self.assertEqual(self.by_id(s.run("sources", "--json"))["instructions"]["counts"]["files"], 0)
        self.assertEqual(s.run("config", "set-group", "Else", "elsewhere").code, 0)
        self.assertEqual(self.by_id(s.run("sources", "--json"))["instructions"]["counts"]["files"], 1)

    def test_a_database_with_another_schema_is_a_note_not_a_crash(self):
        s = self.s
        (s.home / ".codex").mkdir()
        (s.home / ".codex" / "memories_1.sqlite").write_bytes(b"garbage" * 200)
        result = s.run("sources", "--json")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self.by_id(result)["codex"]["note"], "schema not recognised")

    def test_text_output_never_contains_memory_text(self):
        s = self.s
        s.claude_memory("-a", {"one.md": "SECRET-MEMORY-BODY"})
        out = s.run("sources").out
        self.assertIn("claude-code", out)
        self.assertNotIn("SECRET-MEMORY-BODY", out)


if __name__ == "__main__":
    unittest.main()
