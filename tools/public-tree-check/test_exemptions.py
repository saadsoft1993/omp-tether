"""The committed exemptions of the publication checker stay narrow: exact paths, one reason each, product names only,
and no entry that no longer needs to be there."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))

from public_tree_check import build_rules, rule_hits

FILE = HERE / "exemptions.json"
ALLOWED_RULES = {"ai-name"}
GLOB_CHARS = set("*?[]{}")


class CommittedExemptions(unittest.TestCase):
    def setUp(self) -> None:
        self.data = json.loads(FILE.read_text())
        self.rules = self.data["rules"]
        self.reasons = self.data["reasons"]

    def test_only_the_product_name_rule_is_exempted(self) -> None:
        self.assertEqual(set(self.rules), ALLOWED_RULES)
        self.assertEqual(set(self.reasons), ALLOWED_RULES)

    def test_every_entry_is_an_exact_path_of_an_existing_file(self) -> None:
        for rule, paths in self.rules.items():
            self.assertEqual(len(paths), len(set(paths)), f"{rule}: duplicate entries")
            for path in paths:
                with self.subTest(rule=rule, path=path):
                    self.assertFalse(GLOB_CHARS & set(path), "an exemption names one file, never a pattern")
                    self.assertFalse(path.startswith("/") or ".." in Path(path).parts)
                    self.assertTrue((ROOT / path).is_file(), "no such file in the repository")

    def test_every_entry_has_a_one_line_reason_and_every_reason_an_entry(self) -> None:
        for rule, paths in self.rules.items():
            self.assertEqual(set(self.reasons[rule]), set(paths))
            for path, reason in self.reasons[rule].items():
                with self.subTest(rule=rule, path=path):
                    self.assertIsInstance(reason, str)
                    self.assertTrue(reason.strip(), "a reason is required")
                    self.assertNotIn("\n", reason)
                    self.assertLessEqual(len(reason), 160)

    def test_no_stale_entry_each_file_still_triggers_the_rule_it_is_exempted_from(self) -> None:
        rules = {r.rule_id: r for r in build_rules([])}
        for rule_id, paths in self.rules.items():
            for path in paths:
                with self.subTest(rule=rule_id, path=path):
                    text = (ROOT / path).read_text(encoding="utf-8")
                    hit = any(list(rule_hits(rules[rule_id], line, set())) for line in [*text.splitlines(), path])
                    self.assertTrue(hit, "the file no longer matches the rule: remove its exemption")

    def test_the_gate_wrapper_passes_the_file_to_the_checker(self) -> None:
        script = (ROOT / "scripts" / "check-public.sh").read_text()
        self.assertIn("exemptions.json", script)
        self.assertIn("--exempt", script)


if __name__ == "__main__":
    unittest.main()
