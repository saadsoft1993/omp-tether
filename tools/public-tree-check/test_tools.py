"""Tests for the publication tools: the tree checker, the fresh-history exporter and the release gate.

Every scenario builds a throwaway git repository. Forbidden words and secrets are assembled at run time, so this file
passes the checks it tests. Standard library only; Python 3.10 or newer.
Run: python3 -m unittest discover -s tools/public-tree-check
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

HERE = Path(__file__).resolve().parent
CHECK = HERE / "public_tree_check.py"
EXPORT = HERE / "fresh_history.py"
GATE = HERE / "release_gate.py"

AI_NAME = "Cl" + "aude"
HOST = "box.tail" + "abc123.t" + "s.net"
IDENTITY = "Example Author <author@example.com>"
ALLOWLIST = "README.md\nLICENSE\nplugins/**\ndocs/**\nrun.sh\n"
GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Example Author",
    "GIT_AUTHOR_EMAIL": "author@example.com",
    "GIT_COMMITTER_NAME": "Example Author",
    "GIT_COMMITTER_EMAIL": "author@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


class Repo:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True)
        self.git("init", "-q", "-b", "main")
        (root / "README.md").write_text("Session control for omp.\n")
        (root / "LICENSE").write_text("MIT\n")
        self.commit("Initial import")

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(self.root), *args], env=GIT_ENV, capture_output=True, text=True, check=check
        )

    def write(self, rel: str, data: bytes | str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
        return path

    def commit(self, message: str) -> None:
        self.git("add", "-A", "-f")
        self.git("commit", "-q", "-m", message)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ptc-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = Repo(self.tmp / "repo")
        self.allow = self.tmp / "allow.txt"
        self.allow.write_text(ALLOWLIST)
        self.idents = self.tmp / "idents.txt"
        self.idents.write_text(IDENTITY + "\n")

    def check(self, *extra: str) -> tuple[int, str]:
        r = subprocess.run(
            [
                sys.executable,
                str(CHECK),
                str(self.repo.root),
                "--allowlist",
                str(self.allow),
                "--identities",
                str(self.idents),
                *extra,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        return r.returncode, r.stdout + r.stderr


class CheckerBlindSpots(Base):
    def test_a_blob_with_a_nul_byte_is_not_skipped(self) -> None:
        self.repo.write("plugins/p/src/a.ts", b"ok\0 pasted from " + AI_NAME.encode() + b"\n")
        self.repo.commit("Add")
        rc, out = self.check()
        self.assertEqual(rc, 1, out)

    def test_a_line_that_starts_with_plus_plus_is_scanned_in_history(self) -> None:
        self.repo.write("plugins/p/src/a.ts", f"++ pasted from {AI_NAME}\n")
        self.repo.commit("Add")
        self.repo.write("plugins/p/src/a.ts", "clean\n")
        self.repo.commit("Remove")
        rc, out = self.check()
        self.assertEqual(rc, 1, out)
        self.assertIn("ai-name", out)

    def test_content_added_only_in_a_merge_resolution_is_scanned(self) -> None:
        self.repo.write("plugins/p/src/a.ts", "base\n")
        self.repo.commit("Base")
        self.repo.git("checkout", "-q", "-b", "side")
        self.repo.write("plugins/p/src/a.ts", "side change\n")
        self.repo.commit("Side")
        self.repo.git("checkout", "-q", "main")
        self.repo.write("plugins/p/src/a.ts", "main change\n")
        self.repo.commit("Main")
        self.repo.git("merge", "-q", "side", check=False)  # conflicts
        self.repo.write("plugins/p/src/a.ts", f"resolved with {AI_NAME}\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "Merge side")
        self.repo.write("plugins/p/src/a.ts", "clean again\n")
        self.repo.commit("Clean")
        rc, out = self.check()
        self.assertEqual(rc, 1, out)
        self.assertIn("ai-name", out)

    def test_a_private_term_in_a_file_name_is_caught(self) -> None:
        terms = self.tmp / "terms.txt"
        terms.write_text("bigbox-\\d+\n")
        self.repo.write("plugins/p/src/bigbox-7.ts", "export const ok = true;\n")
        self.repo.commit("Add")
        rc, out = self.check("--private-terms", str(terms))
        self.assertEqual(rc, 1, out)
        self.assertIn("private-term", out)
        self.assertNotIn("bigbox-7", out)  # and the name is not printed back

    def test_binary_content_is_rejected_unless_allowed_explicitly(self) -> None:
        self.repo.write("plugins/p/src/blob.bin", bytes([0xFF, 0xFE, 0x00, 0x80, 0x81]))
        self.repo.commit("Add")
        rc, out = self.check()
        self.assertEqual(rc, 1, out)
        self.assertIn("binary-blob", out)
        allowed = self.tmp / "binary.txt"
        allowed.write_text("plugins/p/src/blob.bin\n")
        rc, out = self.check("--binary-allow", str(allowed))
        self.assertEqual(rc, 0, out)

    def test_the_tree_is_read_from_the_commit_not_the_index(self) -> None:
        self.repo.write("README.md", f"staged but never committed: {AI_NAME}\n")
        self.repo.git("add", "README.md")
        rc, out = self.check("--no-history")
        self.assertEqual(rc, 0, out)

    def test_matches_are_not_printed_unless_asked(self) -> None:
        self.repo.write("README.md", f"Open https://{HOST}/ to pair.\n")
        self.repo.commit("Docs")
        rc, out = self.check()
        self.assertEqual(rc, 1, out)
        self.assertIn("tailnet-host", out)
        self.assertNotIn(HOST, out)
        rc, out = self.check("--show-matches")
        self.assertIn(HOST, out)

    def test_a_clean_repository_passes(self) -> None:
        self.repo.write("plugins/p/src/a.ts", "export const ok = true;\n")
        self.repo.commit("Add")
        rc, out = self.check()
        self.assertEqual(rc, 0, out)


class ProvenanceRules(Base):
    """Public files must not say how work or reviews were done; a per-file exemption covers one subject word."""

    PHRASES: ClassVar[list[str]] = [
        "model" + " family",
        "review" + "ers of different kinds",
        "a different" + " model checked it",
        "reviewed" + " by an AI",
        "reviewed" + " by a model",
        "AI" + "-assisted",
        "AI" + "-generated",
        "generated" + " by a tool",
        "generated" + " with a tool",
        "written" + " by an LLM",
        "Co" + "-authored in the open",
        "LLM" + "-written",
        "as an" + " AI",
        "MODEL" + " FAMILIES",
    ]

    def scan(self, text: str, *extra: str, path: str = "docs/note.md") -> tuple[int, str]:
        self.repo.write(path, text + "\n")
        self.repo.commit("Docs")
        return self.check("--no-history", *extra)

    def test_each_phrase_is_a_failing_finding(self) -> None:
        for phrase in self.PHRASES:
            with self.subTest(phrase=phrase):
                rc, out = self.scan(f"This section was {phrase} today.")
                self.assertEqual(rc, 1, out)
                self.assertIn("provenance", out)
                self.assertNotIn(phrase, out)  # the match is not echoed either

    def test_ordinary_review_wording_passes(self) -> None:
        text = (
            "Please review the dry run before you apply. The model name is shown in the status line; "
            "generated files are not committed."
        )
        rc, out = self.scan(text)
        self.assertEqual(rc, 0, out)

    def test_a_description_of_how_the_work_was_reviewed_fails(self) -> None:
        for phrase in (
            "Two independent " + "review" + "s were done",
            "a second " + "review" + "er read the code",
            "each fix has a " + "failing-" + "first test",
            "the fixes were " + "re-" + "review" + "ed",
            "all findings " + "were fixed",
        ):
            with self.subTest(phrase=phrase):
                rc, out = self.scan(f"The plugin: {phrase}.")
                self.assertEqual(rc, 1, out)
                self.assertIn("process-narrative", out)

    def test_every_plugin_path_is_scanned_for_it(self) -> None:
        for name in ("omp-services", "omp-session-control", "omp-private-relay", "omp-dashboard", "omp-memory"):
            with self.subTest(plugin=name):
                rc, out = self.scan(
                    "Two independent " + "reviews were done.", path=f"plugins/{name}/docs/threat-model.md"
                )
                self.assertEqual(rc, 1, out)

    NARRATIVES: ClassVar[list[str]] = [
        "the " + "pre" + "decessor of this tool",
        "a " + "pre" + "cursor that ran at home",
        "It " + "general" + "ises a hub",
        "It " + "general" + "izes a relay",
        "this " + "grew " + "out of a script",
        "It was " + "original" + "ly built for one machine",
        "The " + "first " + "design was different",
        "An " + "earlier " + "tool of the author's",
        "it " + "ran for " + "one person",
        "used " + "before this " + "plugin existed",
        "This " + "came from a " + "relay that ran at home",
        "Notes " + "written" + " with a helper",
        "As " + "we " + "found",
        "the bug " + "was " + "found in the lock",
        "changed " + "after " + "testing found a race",
        "fixed " + "while " + "porting",
        "a " + "regression " + "for a " + "bug",
        "see " + "F1" + "/" + "H2",
        "the " + "second " + "round",
        "the " + "first " + "round of checks",
        "kept from the " + "previous " + "round",
        "added " + "in a later " + "round",
        "this " + "round",
        "an " + "earlier " + "debugging failure",
        "an " + "earlier " + "version of this test",
        "locks (" + "F07" + ")",
        "R6 " + "(second " + "round)",
        "R3 " + "fix",
        "R3 " + "finding",
    ]

    def test_each_origin_and_process_phrase_fails_in_docs_comments_and_test_titles(self) -> None:
        for phrase in self.NARRATIVES:
            for path, text in (
                ("plugins/p/docs/notes.md", f"{phrase}."),
                ("plugins/p/tests/unit/a.test.ts", f"// {phrase}"),
                ("plugins/p/tests/unit/b.test.ts", f'test("{phrase}", () => {{}});'),
            ):
                with self.subTest(phrase=phrase, path=path):
                    rc, out = self.scan(text, path=path)
                    self.assertEqual(rc, 1, out)
                    self.assertIn("process-narrative", out)
                    self.assertNotIn(phrase, out)

    def test_factual_sentences_pass(self) -> None:
        for text in (
            "Limitation: only a single account is supported.",
            "Tested on macOS 27.2 and Ubuntu 24.04.",
            "Ported to Linux: lsof is replaced by /proc.",
            "First run: init asks before each step.",
            "The first version of the protocol is session-control/1.",
            "The file is found by name in the project directory.",
            "Fixed width columns are used in the table.",
            "Reads are bounded, after which the connection is closed.",
            "A bank is recorded only when it is written without an error.",
            "Rounds the value up to the next multiple.",
            "A round trip through the codec is lossless, and this round-trip is tested.",
            "R2 of the retry table is the backoff row.",
            "Protocol checks M1 to M3 cover abort, rename and ordering (see control-1).",
            "Protocol check M1: abort on an idle session returns aborted false.",
        ):
            with self.subTest(text=text):
                rc, out = self.scan(text)
                self.assertEqual(rc, 0, out)

    def test_protocol_check_ids_in_conformance_code_pass(self) -> None:
        text = (
            'await check("M1", "abort on an idle session", () => run());\n'
            'test("rename collapses runs of spaces", () => {});\n'
            'describe("M1 to M3: ordered mutations", () => {});'
        )
        rc, out = self.scan(text, path="plugins/p/tests/conformance/suite.ts")
        self.assertEqual(rc, 0, out)

    def test_a_finding_id_opening_a_test_title_fails(self) -> None:
        for opener in ("test", "it", "describe"):
            for ident in ("F" + "05", "R" + "6", "H" + "1", "L" + "12", "M" + "4"):
                with self.subTest(opener=opener, ident=ident):
                    text = f'{opener}("{ident}: a lock is refused", () => {{}});'
                    rc, out = self.scan(text, path="plugins/p/tests/unit/a.test.ts")
                    self.assertEqual(rc, 1, out)
                    self.assertIn("process-narrative", out)

    def test_a_commit_message_with_a_narrative_phrase_fails(self) -> None:
        self.repo.write("README.md", "Session control for omp.\nMore.\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "Docs: " + "an earlier " + "design of the tool")
        rc, out = self.check()
        self.assertEqual(rc, 1, out)
        self.assertIn("process-narrative", out)

    def test_an_older_file_version_in_the_history_is_not_checked_against_it(self) -> None:
        self.repo.write("docs/a.md", "It " + "general" + "ises a hub.\n")
        self.repo.commit("Add")
        self.repo.write("docs/a.md", "A hub.\n")
        self.repo.commit("Edit")
        rc, out = self.check()
        self.assertEqual(rc, 0, out)

    def test_the_phrase_in_a_commit_message_fails_too(self) -> None:
        self.repo.write("README.md", "Session control for omp.\nMore.\n")
        self.repo.git("add", "-A")
        self.repo.git("commit", "-q", "-m", "Fix docs " + "AI" + "-assisted")
        rc, out = self.check()
        self.assertEqual(rc, 1, out)
        self.assertIn("provenance", out)

    def test_language_model_fails_unless_that_exact_file_is_exempted(self) -> None:
        subject = "plugins/memory/docs/guide.md"
        other = "plugins/memory/docs/other.md"
        text = "The memory plugin's own " + "language" + " model summarises the memories."
        self.repo.write(subject, text + "\n")
        self.repo.write(other, text + "\n")
        self.repo.commit("Docs")
        rc, out = self.check("--no-history")
        self.assertEqual(rc, 1, out)
        self.assertIn("language-model", out)
        exempt = self.tmp / "exempt.json"
        exempt.write_text(
            json.dumps({"rules": {"language-model": [subject]}, "reasons": {"language-model": "subject"}})
        )
        rc, out = self.check("--no-history", "--exempt", str(exempt))
        self.assertEqual(rc, 1, out)  # the other file still fails
        self.assertIn("other.md", out)
        self.assertNotIn("guide.md", out)
        exempt.write_text(json.dumps({"rules": {"language-model": [subject, other]}}))
        rc, out = self.check("--no-history", "--exempt", str(exempt))
        self.assertEqual(rc, 0, out)

    def test_an_exemption_for_language_model_does_not_excuse_a_provenance_phrase(self) -> None:
        path = "plugins/memory/docs/guide.md"
        exempt = self.tmp / "exempt.json"
        exempt.write_text(json.dumps({"rules": {"language-model": [path]}}))
        rc, out = self.scan(
            "A " + "language" + " model. Notes written" + " by an LLM.", "--exempt", str(exempt), path=path
        )
        self.assertEqual(rc, 1, out)
        self.assertIn("provenance", out)
        self.assertNotIn("language-model", out)


class Exporter(Base):
    def export(self, *extra: str) -> tuple[subprocess.CompletedProcess, Path]:
        dest = self.tmp / "fresh"
        r = subprocess.run(
            [
                sys.executable,
                str(EXPORT),
                str(self.repo.root),
                str(dest),
                "--allowlist",
                str(self.allow),
                "--name",
                "Example Author",
                "--email",
                "author@example.com",
                *extra,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        return r, dest

    def test_it_exports_the_commit_not_the_index_and_keeps_modes(self) -> None:
        script = self.repo.write("run.sh", "#!/bin/sh\necho hi\n")
        script.chmod(0o755)
        os.symlink("README.md", self.repo.root / "docs-link")
        self.repo.write("docs/a.md", "committed\n")
        self.repo.commit("Add files")
        # staged and untracked changes made after the commit must not reach the export
        self.repo.write("docs/a.md", f"staged: {AI_NAME}\n")
        self.repo.git("add", "docs/a.md")
        self.repo.write("docs/b.md", "untracked\n")
        r, dest = self.export("--commit", "HEAD")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((dest / "docs" / "a.md").read_text(), "committed\n")
        self.assertFalse((dest / "docs" / "b.md").exists())
        self.assertTrue(os.access(dest / "run.sh", os.X_OK), "the executable bit is lost")
        listing = subprocess.run(
            ["git", "-C", str(dest), "ls-tree", "-r", "HEAD"], capture_output=True, text=True, env=GIT_ENV
        ).stdout
        self.assertIn("100755", listing)
        self.assertNotIn("docs-link", listing)  # not allowlisted: dropped, and said to be dropped
        self.assertIn("dropped", r.stdout)
        count = subprocess.run(
            ["git", "-C", str(dest), "rev-list", "--count", "HEAD"], capture_output=True, text=True, env=GIT_ENV
        )
        self.assertEqual(count.stdout.strip(), "1")

    def test_symlinks_that_are_allowlisted_stay_symlinks(self) -> None:
        self.allow.write_text(ALLOWLIST + "docs-link\n")
        os.symlink("README.md", self.repo.root / "docs-link")
        self.repo.commit("Add link")
        r, dest = self.export()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((dest / "docs-link").is_symlink())
        self.assertEqual(os.readlink(dest / "docs-link"), "README.md")

    def test_a_submodule_entry_is_refused(self) -> None:
        sha = "1234567890123456789012345678901234567890"
        self.repo.git("update-index", "--add", "--cacheinfo", f"160000,{sha},plugins/sub")
        self.repo.git("commit", "-q", "-m", "Add gitlink")
        r, _ = self.export()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("unsupported", r.stdout + r.stderr)


def have_gitleaks() -> bool:
    return shutil.which("gitleaks") is not None


class ReleaseGate(Base):
    def gate(self, path_override: str | None = None) -> tuple[int, str]:
        env = {**os.environ}
        if path_override is not None:
            env["PATH"] = path_override
        r = subprocess.run(
            [
                sys.executable,
                str(GATE),
                "--repo",
                str(self.repo.root),
                "--allowlist",
                str(self.allow),
                "--identity",
                IDENTITY,
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=300,
        )
        return r.returncode, r.stdout + r.stderr

    def path_without_gitleaks(self) -> str:
        bindir = self.tmp / "bin"
        bindir.mkdir()
        for tool in ("git", "bash", "sh", "env"):
            found = shutil.which(tool)
            if found:
                (bindir / tool).symlink_to(found)
        return str(bindir)

    def test_a_missing_credential_scanner_fails_the_gate(self) -> None:
        rc, out = self.gate(self.path_without_gitleaks())
        self.assertNotEqual(rc, 0, out)
        self.assertIn("gitleaks", out)
        self.assertNotIn("public-tree check passed", out)

    @unittest.skipUnless(have_gitleaks(), "gitleaks is required for this scenario")
    def test_a_credential_removed_from_original_history_fails_the_gate(self) -> None:
        alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        token = "gh" + "p_" + "".join(secrets.choice(alphabet) for _ in range(36))
        self.repo.write("plugins/p/src/a.ts", f'export const key = "{token}";\n')
        self.repo.commit("Add")
        self.repo.write("plugins/p/src/a.ts", "export const key = process.env.KEY;\n")
        self.repo.commit("Remove")
        rc, out = self.gate()
        self.assertNotEqual(rc, 0, out)
        self.assertIn("gitleaks", out)
        self.assertNotIn(token, out)

    @unittest.skipUnless(have_gitleaks(), "gitleaks is required for this scenario")
    def test_a_clean_repository_passes_the_gate(self) -> None:
        script = self.repo.write("run.sh", "#!/bin/sh\necho hi\n")
        script.chmod(0o755)
        self.repo.write("plugins/p/src/a.ts", "export const ok = true;\n")
        self.repo.commit("Add")
        rc, out = self.gate()
        self.assertEqual(rc, 0, out)
        self.assertIn("public-tree check passed", out)

    @unittest.skipUnless(have_gitleaks(), "gitleaks is required for this scenario")
    def test_staged_changes_do_not_enter_or_fail_the_gate(self) -> None:
        self.repo.write("README.md", f"staged only: {AI_NAME}\n")
        self.repo.git("add", "README.md")
        rc, out = self.gate()
        self.assertEqual(rc, 0, out)


class PrivateNamesStayOutOfDiagnostics(Base):
    """A private term in a path or ref name is hidden in every diagnostic, not only in the one about the name."""

    def setUp(self) -> None:
        super().setUp()
        self.terms = self.tmp / "terms.txt"
        self.terms.write_text("bigbox-\\d+\n")

    def test_a_denied_or_unlisted_path_is_not_printed(self) -> None:
        self.repo.write("elsewhere/bigbox-7.ts", "export const ok = true;\n")
        self.repo.commit("Add")
        rc, out = self.check("--private-terms", str(self.terms))
        self.assertEqual(rc, 1, out)
        self.assertIn("path-not-allowed", out)
        self.assertNotIn("bigbox-7", out)
        rc, out = self.check("--private-terms", str(self.terms), "--show-matches")
        self.assertIn("bigbox-7", out)

    def test_a_finding_inside_a_privately_named_file_does_not_print_the_file_name(self) -> None:
        self.repo.write("plugins/p/src/bigbox-7.ts", f"// pasted from {AI_NAME}\n")
        self.repo.commit("Add")
        rc, out = self.check("--private-terms", str(self.terms))
        self.assertEqual(rc, 1, out)
        self.assertIn("ai-name", out)
        self.assertNotIn("bigbox-7", out)

    def test_a_binary_blob_with_a_private_name_is_reported_without_the_name(self) -> None:
        self.repo.write("plugins/p/src/bigbox-7.bin", bytes([0xFF, 0xFE, 0x00, 0x80]))
        self.repo.commit("Add")
        rc, out = self.check("--private-terms", str(self.terms))
        self.assertEqual(rc, 1, out)
        self.assertIn("binary-blob", out)
        self.assertNotIn("bigbox-7", out)

    def test_a_private_ref_name_is_not_printed(self) -> None:
        self.repo.git("branch", "bigbox-7-wip")
        rc, out = self.check("--private-terms", str(self.terms))
        self.assertEqual(rc, 1, out)
        self.assertIn("private-term", out)
        self.assertNotIn("bigbox-7", out)


if __name__ == "__main__":
    unittest.main()
