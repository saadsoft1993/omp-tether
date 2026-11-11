"""The plugin's files agree with each other and with the repository: one version everywhere, the catalog entry, the license
and notice, the files `package.json` ships. The checks against the repository's files are skipped, with the reason, when
the plugin is not inside the repository (an installed copy)."""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PLUGIN / "python"))
sys.dont_write_bytecode = True

import omp_memory

REPO = PLUGIN.parents[1] if len(PLUGIN.parents) > 1 else PLUGIN  # an installed copy may sit at a shallow path
IN_REPO = (REPO / ".omp-plugin" / "marketplace.json").is_file() and (REPO / "compat.json").is_file()
NAME = "omp-memory"


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class PluginFiles(unittest.TestCase):
    def setUp(self) -> None:
        self.package = load(PLUGIN / "package.json")

    def test_package_json_names_the_plugin_and_ships_what_exists(self):
        self.assertEqual(self.package["name"], NAME)
        self.assertEqual(self.package["license"], "MIT")
        self.assertIn("omp-package", self.package["keywords"])
        self.assertEqual(self.package["bin"], {NAME: "./bin/omp-memory"})
        for entry in self.package["files"]:
            self.assertTrue((PLUGIN / entry).exists(), f"package.json lists {entry}, which is missing")
        for required in ("LICENSE", "NOTICE", "README.md", "SECURITY.md", "CHANGELOG.md"):
            self.assertIn(required, self.package["files"])

    def test_files_lists_every_top_level_entry_the_install_carries(self):
        """omp installs the whole plugin directory, so `files` must name everything in it (the tests included)."""
        installed = {
            entry.name
            for entry in PLUGIN.iterdir()
            if not entry.name.startswith(".") and entry.name not in ("__pycache__", "node_modules", "package.json")
        }
        self.assertEqual(installed, set(self.package["files"]))

    def test_the_code_the_changelog_and_the_package_state_one_version(self):
        version = self.package["version"]
        self.assertEqual(omp_memory.__version__, version)
        top = re.search(r"^## (\d+\.\d+\.\d+) \((\d{4}-\d{2}-\d{2})\)$", (PLUGIN / "CHANGELOG.md").read_text(), re.M)
        self.assertIsNotNone(top, "CHANGELOG.md has no '## X.Y.Z (date)' heading")
        self.assertEqual(top.group(1), version)

    def test_notice_accounts_for_the_code_copied_from_omp_and_the_packages_setup_fetches(self):
        notice = (PLUGIN / "NOTICE").read_text(encoding="utf-8")
        for copied in ("omp_redact.ts", "bank_id.ts", "test_memory_schema.py"):
            self.assertIn(copied, notice)
            self.assertTrue(list((PLUGIN / "vendor").rglob(copied)), f"{copied} is named in NOTICE but not in vendor/")
        for fetched in ("@oh-my-pi/pi-mnemopi", "fastembed", "onnxruntime-node", "bge-base-en-v1.5"):
            self.assertIn(fetched, notice)
        self.assertIn("Permission is hereby granted", notice)
        self.assertIn("Stencil Labs", notice)

    def test_every_file_derived_from_omp_says_so_in_its_header(self):
        for name in ("omp_redact.ts", "bank_id.ts"):
            head = next((PLUGIN / "vendor").rglob(name)).read_text(encoding="utf-8")[:1200]
            self.assertIn("MIT License", head)
            self.assertIn("Stencil Labs", head)


@unittest.skipUnless(IN_REPO, "not inside the omp-tether repository: the catalog and compat.json are not here")
class RepositoryFiles(unittest.TestCase):
    def setUp(self) -> None:
        self.package = load(PLUGIN / "package.json")
        catalog = load(REPO / ".omp-plugin" / "marketplace.json")
        self.entry = next(p for p in catalog["plugins"] if p["name"] == NAME)
        self.compat = load(REPO / "compat.json")["plugins"][NAME]

    def test_catalog_compat_and_package_agree(self):
        self.assertEqual(self.entry["version"], self.package["version"])
        self.assertEqual(self.compat["version"], self.package["version"])
        self.assertEqual(self.entry["source"], "./plugins/omp-memory")
        self.assertEqual(self.entry["license"], self.package["license"])
        self.assertEqual(self.entry["description"], self.package["description"])

    def test_the_license_is_the_siblings_license(self):
        mine = (PLUGIN / "LICENSE").read_text()
        self.assertEqual(mine, (REPO / "LICENSE").read_text())
        for sibling in (REPO / "plugins").glob("omp-*/LICENSE"):
            self.assertEqual(mine, sibling.read_text(), str(sibling))

    def test_compat_json_is_honest_about_what_was_run(self):
        self.assertEqual(self.compat["ompTested"], ["18.18.0", "18.18.3"])  # the real omp binaries that were run
        self.assertEqual(self.compat["ompVerifiedRange"], {"first": "18.18.0", "last": "18.18.8"})
        self.assertEqual(self.compat["platforms"], ["darwin", "linux"])
        tested = " ".join(self.compat["platformsTested"]).lower()
        self.assertIn("darwin-arm64", tested)
        self.assertIn("linux-arm64", tested)
        not_tested = " ".join(self.compat["platformsNotTested"]).lower()
        self.assertIn("linux-x64", not_tested)
        self.assertIn("musl", not_tested)
        self.assertNotIn("win32", self.compat["platforms"])

    def test_the_verified_omp_range_is_the_same_in_the_importer_compat_json_and_the_readme(self):
        """The refusal message of the write test names the range; compat.json and the README state the same one."""
        source = (PLUGIN / "vendor" / "memory_import" / "selftest.ts").read_text(encoding="utf-8")
        found = re.search(r'VERIFIED_OMP_RANGE = \{ first: "([\d.]+)", last: "([\d.]+)" \}', source)
        self.assertIsNotNone(found)
        self.assertEqual({"first": found.group(1), "last": found.group(2)}, self.compat["ompVerifiedRange"])
        readme = (PLUGIN / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"{found.group(1)} to {found.group(2)}", readme)
        self.assertIn(
            f"{found.group(1)} to {found.group(2)}", (REPO / "docs" / "compat.md").read_text(encoding="utf-8")
        )

    def test_issue_templates_offer_the_plugin(self):
        for name in ("bug_report.yml", "feature_request.yml"):
            self.assertIn(f"- {NAME}\n", (REPO / ".github" / "ISSUE_TEMPLATE" / name).read_text())

    def test_the_repository_documents_list_the_plugin(self):
        for relative in ("README.md", "SECURITY.md", "docs/threat-model.md", "docs/compat.md"):
            self.assertIn(NAME, (REPO / relative).read_text(), relative)
        self.assertIn(NAME, (REPO / "tools" / "extension-load-gate" / "expectations.json").read_text())


if __name__ == "__main__":
    unittest.main()


class UninstallDocTest(unittest.TestCase):
    def test_the_uninstall_section_does_not_contradict_itself_about_the_labels(self):
        """`rm -r ~/.config/omp-memory` removes the labels file (its default is inside), and the README says so."""
        from omp_memory import config

        self.assertEqual(config.defaults()["labelsFile"].parent.name, "omp-memory")
        section = (PLUGIN / "README.md").read_text(encoding="utf-8").split("## Uninstall")[1].split("## FAQ")[0]
        self.assertIn("rm -r ~/.config/omp-memory ", section)
        self.assertIn("labels.json", section)
        kept = section.split("does **not** remove these")[1]
        self.assertNotIn("(your labels)", kept)
        self.assertIn("deletes them", kept)
