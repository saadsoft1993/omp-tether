"""A recognised secret in a name never reaches the terminal or a file this tool writes.

The names a user's data carries (a bank directory, a folder, a label, a file name, a path flag, a configuration value, a
mistyped argument) are not secret by intent, but a PIN assignment or an API key pasted into one is. Every command is run
with such a name in each of those places, and neither stdout nor stderr may show the secret, in any of its variants
(percent-encoded, upper or lower case), and no file the command writes may newly contain it. The secrets are built
here at run time, never written out in the source."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import unittest
import warnings
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import ClassVar
from unittest import mock
from urllib.parse import quote

from support import LAUNCHER, PLUGIN, PYTHON_DIR, Result, Scratch, ScratchCase

PIN = "493817"


def token_shaped() -> list[tuple[str, str]]:
    """(carrier, the part of it that must never appear) for secrets that have a recognised prefix."""
    github = "ghp_" + secrets.token_hex(18)
    secret_key = "sk-" + secrets.token_hex(14)
    return [(github, github), (secret_key.upper(), secret_key[3:])]


# Names a bank directory may have (letters, digits, `.`, `_`, `-`), and free text for everything else.
BANK_FORMS = [("password-" + PIN, PIN), ("PASSWORD." + PIN, PIN), ("Token_" + PIN, PIN), *token_shaped()]
FREE_FORMS = [
    ("password=" + PIN, PIN),
    ("Password%3D" + PIN, PIN),
    ("PASSWORD_" + PIN, PIN),
    ("api key " + PIN, PIN),
    *token_shaped(),
]


def needles(carrier: str, core: str) -> list[str]:
    """Every spelling of the secret that must not be found (compared in lower case)."""
    out = {carrier, core, quote(carrier, safe=""), quote(quote(carrier, safe=""), safe="")}
    return sorted(n.lower() for n in out)


# A stand-in for Bun that behaves like an importer which prints what it was given: a name with a secret on stdout and on
# stderr (defence in depth: the wrapper filters what it forwards).
LEAKY_BUN = """#!/bin/sh
if [ "$1" = "--version" ]; then echo 1.4.4; exit 0; fi
if [ -n "${LEAK_OUT_FILE:-}" ]; then cat "$LEAK_OUT_FILE"; else printf '%s\\n' "${LEAK_OUT:-}"; fi
printf '%s\\n' "${LEAK_ERR:-}" >&2
exit "${LEAK_EXIT:-0}"
"""

# Where a command may write besides the banks (databases are binary and hold the user's own data).
SKIP_PARTS = ("Backups", "stubs", ".claude", ".codex", ".claude-mem", ".omp")
# Files that record the real paths the user pointed at (the state file's backup directory and banks directory, the backup
# manifest's source, the configuration's folders) cannot hold an index instead: they are how those paths are found again.
# What this test is about is what is printed, and the labels file and the ledger, which only ever hold names.
PATH_RECORDS = ("state.json", "config.json", "MANIFEST.tsv", "SHA256SUMS")


class SecretCase(ScratchCase):
    def setUp(self):
        super().setUp()
        self.leaky_bun = self.s._script("bun-leaky", LEAKY_BUN)
        self.ran: list[str] = []

    def fresh(self) -> None:
        """A new scratch home for the next form of the secret (the banks, labels and state of the last one are gone)."""
        self.s.close()
        self.s = Scratch()
        self.addCleanup(self.s.close)
        self.leaky_bun = self.s._script("bun-leaky", LEAKY_BUN)

    # -- running -------------------------------------------------------------------------------------------------
    def snapshot(self) -> dict[Path, bytes]:
        files = {}
        for path in self.s.home.rglob("*"):
            if path.is_file() and not any(part in SKIP_PARTS for part in path.relative_to(self.s.home).parts):
                if not path.name.endswith((".db", ".db-wal", ".db-shm")) and path.name not in PATH_RECORDS:
                    files[path] = path.read_bytes()
        return files

    def env(self, carrier: str, **extra: str) -> dict[str, str]:
        return self.s.wired(
            OMP_MEMORY_BUN=str(self.leaky_bun),
            LEAK_OUT=f"report for {carrier}",
            LEAK_ERR=f"warning: {carrier}",
            **extra,
        )

    def check(self, args: tuple[str, ...], carrier: str, core: str, **env_extra: str) -> None:
        before = self.snapshot()
        r = self.s.run(*args, env=self.env(carrier, **env_extra))
        shown = " ".join(args).replace(carrier, "<carrier>")
        self.assertIn(r.code, (0, 1, 2), f"{shown}: unexpected exit status {r.code}")
        text = (r.out + "\n" + r.err).lower()
        for needle in needles(carrier, core):
            self.assertNotIn(needle, text, f"{shown}: the output shows the secret")
        self.assertNotIn("traceback", text, f"{shown}: a traceback reached the user")
        if "--json" in args and r.out.strip():
            json.loads(r.out)  # withholding a name keeps the document valid
        after = self.snapshot()
        for path, data in after.items():
            if data != before.get(path):
                written = data.decode("utf-8", "replace").lower()
                for needle in needles(carrier, core):
                    if needle not in (before.get(path) or b"").decode("utf-8", "replace").lower():
                        self.assertNotIn(needle, written, f"{shown}: wrote the secret to {path.name}")
        self.ran.append(shown)

    def everywhere(self, carrier: str, core: str, bank: str, label_bank: str | None = None) -> None:
        """Every command that reads banks, with `bank` and a plain bank on disk."""
        c, k = carrier, core
        for args in (
            ("banks",),
            ("banks", "--json"),
            ("sources",),
            ("sources", "--json"),
            ("labels", "list"),
            ("labels", "list", "--json"),
            ("labels", "set", bank, "Work"),
            ("labels", "clear", bank),
            ("labels", "set", "plain-1abc", "Plain"),
            ("doctor",),
            ("doctor", "--json"),
            ("doctor", "--deep"),
            ("config", "show"),
            ("config", "show", "--json"),
            ("config", "path"),
            ("backup",),
            ("backup", "--apply"),
            ("restore-drill",),
            (
                "export-hindsight",
                "--banks",
                "one",
                "--overlay-dir",
                str(self.s.root / "ov"),
                "--out",
                str(self.s.root / "hs"),
            ),
            ("import", "scan"),
            ("import", "dry-run"),
            ("import", "verify"),
            ("import", "undo", "--run", "r1"),
            ("setup", "--dry-run"),
        ):
            self.check(args, c, k)


class BankNameTest(SecretCase):
    def test_a_secret_in_a_bank_directory_name_reaches_no_output(self):
        for carrier, core in BANK_FORMS:
            with self.subTest(carrier=carrier[:14]):
                self.fresh()
                self.s.bank("plain-1abc", "work/plain")
                self.s.bank(carrier, "work/other")
                self.everywhere(carrier, core, carrier)

    def test_banks_shows_an_index_for_the_bank_and_keeps_ordinary_names(self):
        self.s.bank("plain-1abc", "work/plain")
        self.s.bank("password-" + PIN, "work/other")
        r = self.s.run("banks")
        self.assertEqual(r.code, 0, r.err)
        self.assertNotIn(PIN, r.out + r.err)
        self.assertIn("bank#1", r.out)
        self.assertIn("plain-1abc", r.out)
        again = self.s.run("banks", "--json")
        data = json.loads(again.out)
        names = sorted(b["bank"] for b in data["banks"])
        self.assertEqual(names, ["bank#1", "plain-1abc"])

    def test_the_index_follows_first_appearance_in_the_process_and_hides_the_value(self):
        for index, name in enumerate(("password-" + PIN, "Token_" + PIN, "ghp_" + secrets.token_hex(18)), start=1):
            self.s.bank(name, f"work/f{index}")
        data = json.loads(self.s.run("banks", "--json").out)
        self.assertEqual(sorted(b["bank"] for b in data["banks"]), ["bank#1", "bank#2", "bank#3"])


class FolderTest(SecretCase):
    def test_a_secret_in_a_folder_name_reaches_no_output(self):
        for carrier, core in FREE_FORMS:
            with self.subTest(carrier=carrier[:14]):
                self.fresh()
                self.s.bank("plain-1abc", f"work/{carrier}/app")
                self.s.bank("other-2def", "work/plain")
                self.s.claude_memory("-" + carrier.replace("/", "-"), {"MEMORY.md": "- [x](x.md)"})
                self.everywhere(carrier, core, "plain-1abc")

    def test_the_folder_column_shows_an_index_and_ordinary_folders_stay(self):
        self.s.bank("plain-1abc", f"work/password={PIN}/app")
        self.s.bank("other-2def", "work/plain")
        r = self.s.run("banks")
        self.assertEqual(r.code, 0, r.err)
        self.assertNotIn(PIN, r.out + r.err)
        self.assertIn("folder#1", r.out)
        self.assertIn("~/work/plain", r.out)


class LabelTest(SecretCase):
    def write_labels(self, bank: str, label: str) -> None:
        path = self.s.home / ".config" / "omp-memory" / "labels.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 1, "labels": {bank: {"label": label}}}))
        path.chmod(0o600)

    def test_a_secret_in_a_label_reaches_no_output(self):
        for carrier, core in FREE_FORMS:
            with self.subTest(carrier=carrier[:14]):
                self.fresh()
                self.s.bank("plain-1abc", "work/plain")
                self.write_labels("plain-1abc", carrier[:60])
                self.everywhere(carrier[:60], core, "plain-1abc")

    def test_labels_list_shows_an_index_for_the_label(self):
        self.s.bank("plain-1abc", "work/plain")
        self.write_labels("plain-1abc", "password=" + PIN)
        for command in (("labels", "list"), ("banks",), ("sources",)):
            r = self.s.run(*command)
            self.assertNotIn(PIN, r.out + r.err, command)
        listed = self.s.run("labels", "list")
        self.assertIn("folder#1", listed.out)

    def test_a_label_that_carries_a_secret_is_refused_and_not_stored(self):
        self.s.bank("plain-1abc", "work/plain")
        r = self.s.run("labels", "set", "plain-1abc", "password=" + PIN)
        self.assertEqual(r.code, 2, r.out + r.err)
        self.assertNotIn(PIN, r.out + r.err)
        self.assertFalse((self.s.home / ".config" / "omp-memory" / "labels.json").exists())

    def test_a_label_for_a_bank_whose_name_is_a_secret_is_refused_and_not_stored(self):
        self.s.bank("password-" + PIN, "work/plain")
        r = self.s.run("labels", "set", "password-" + PIN, "Work")
        self.assertEqual(r.code, 2, r.out + r.err)
        self.assertNotIn(PIN, r.out + r.err)
        self.assertFalse((self.s.home / ".config" / "omp-memory" / "labels.json").exists())


class FileNameTest(SecretCase):
    def test_a_secret_in_a_claude_memory_file_name_reaches_no_output(self):
        for carrier, core in FREE_FORMS:
            with self.subTest(carrier=carrier[:14]):
                self.fresh()
                self.s.claude_memory("-proj", {f"{carrier}.md": "text", "MEMORY.md": "- [x](x.md)"})
                self.s.bank("plain-1abc", "work/plain")
                for args in (
                    ("sources",),
                    ("sources", "--json"),
                    ("banks",),
                    ("doctor",),
                    ("import", "scan"),
                    ("import", "dry-run"),
                ):
                    self.check(args, carrier, core)


class PathFlagTest(SecretCase):
    def test_a_secret_in_a_path_flag_reaches_no_output(self):
        for carrier, core in FREE_FORMS:
            with self.subTest(carrier=carrier[:14]):
                self.fresh()
                banks = self.s.root / f"x-{carrier}" / "banks"
                self.s.banks = banks
                self.s.bank("plain-1abc", "work/plain")
                missing = self.s.root / f"missing-{carrier}"
                for args in (
                    ("banks", "--banks-dir", str(banks)),
                    ("banks", "--json", "--banks-dir", str(banks)),
                    ("sources", "--banks-dir", str(banks)),
                    ("labels", "list", "--banks-dir", str(banks)),
                    ("labels", "list", "--json", "--banks-dir", str(banks)),
                    ("doctor", "--banks-dir", str(banks)),
                    ("doctor", "--json", "--banks-dir", str(banks)),
                    ("doctor", "--deep", "--banks-dir", str(banks)),
                    ("backup", "--banks-dir", str(banks)),
                    ("backup", "--apply", "--banks-dir", str(banks), "--dest", str(self.s.root / f"dest-{carrier}")),
                    ("restore-drill", "--banks-dir", str(banks), "--backup", str(missing)),
                    ("restore-drill", "--banks-dir", str(banks), "--scratch", str(self.s.root / f"scratch-{carrier}")),
                    ("export-hindsight", "--banks", "one", "--overlay-dir", str(missing), "--out", str(missing)),
                    (
                        "export-hindsight",
                        "--apply",
                        "--banks",
                        "one",
                        "--overlay-dir",
                        str(missing),
                        "--out",
                        str(self.s.root / f"out-{carrier}"),
                    ),
                    (
                        "import",
                        "dry-run",
                        "--banks-dir",
                        str(banks),
                        "--out",
                        str(self.s.root / f"report-{carrier}.json"),
                    ),
                    ("import", "scan", "--banks-dir", str(missing)),
                    ("import", "verify", "--ledger", str(missing)),
                    ("setup", "--dry-run", "--lib-dir", str(missing)),
                    ("config", "set", "banksDir", str(missing)),
                ):
                    self.check(args, carrier, core)


class ConfigValueTest(SecretCase):
    def test_a_secret_in_a_configuration_value_reaches_no_output(self):
        for carrier, core in FREE_FORMS:
            with self.subTest(carrier=carrier[:14]):
                self.fresh()
                banks = self.s.home / carrier / "banks"
                self.s.banks = banks
                self.s.bank("plain-1abc", "work/plain")
                config = {
                    "kind": "omp-memory-config",
                    "schemaVersion": 1,
                    "banksDir": str(banks),
                    "backupsDir": str(self.s.home / carrier / "backups"),
                    "groups": {carrier[:20]: [f"~/work/{carrier}"]},
                }
                path = self.s.home / ".config" / "omp-memory" / "config.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(config))
                path.chmod(0o600)
                self.everywhere(carrier, core, "plain-1abc")
                for args in (("config", "show"), ("config", "show", "--json"), ("config", "path")):
                    self.check(args, carrier, core, OMP_MEMORY_LEDGER=str(self.s.home / carrier / "ledger.json"))
                self.check(("config", "show"), carrier, core, OMP_MEMORY_CONFIG=str(path))
                path.unlink()

    def test_a_secret_in_an_environment_setting_reaches_no_output(self):
        for carrier, core in FREE_FORMS:
            with self.subTest(carrier=carrier[:14]):
                self.fresh()
                self.s.bank("plain-1abc", "work/plain")
                for args in (("config", "show"), ("config", "show", "--json"), ("doctor",), ("banks",)):
                    self.check(args, carrier, core, OMP_MEMORY_LABELS_FILE=str(self.s.home / carrier / "labels.json"))


class ErrorCauseTest(SecretCase):
    def test_a_secret_in_a_mistyped_argument_reaches_no_output(self):
        for carrier, core in FREE_FORMS:
            with self.subTest(carrier=carrier[:14]):
                self.fresh()
                self.s.bank("plain-1abc", "work/plain")
                for args in (
                    (carrier,),
                    ("labels", carrier),
                    ("labels", "set", carrier, "x"),
                    ("labels", "clear", carrier),
                    ("labels", "list", carrier),
                    ("banks", carrier),
                    ("banks", "--" + carrier),
                    ("sources", "--banks-dir", carrier),
                    ("config", carrier),
                    ("config", "set", carrier, "/x"),
                    ("config", "set", "banksDir", carrier),
                    ("config", "unset", carrier),
                    ("config", "unset-group", carrier),
                    ("config", "set-group", "g", carrier),
                    ("doctor", "--" + carrier),
                    ("doctor", "--banks-dir", carrier),
                    ("import", carrier),
                    ("import", "scan", "--apply=" + carrier),
                    ("import", "scan", "--out"),
                    ("import", "import", "--apply", "--banks", carrier),
                    ("backup", carrier),
                    ("backup", "--dest", carrier),
                    ("restore-drill", "--backup", "/nonexistent/" + carrier),
                    ("restore-drill", "--backup", carrier),
                    ("export-hindsight", "--" + carrier),
                    ("export-hindsight", "--banks", carrier, "--out", str(self.s.root / "o"), "--overlay-dir", "/x"),
                    ("setup", carrier),
                    ("setup", "--registry", carrier, "--dry-run"),
                ):
                    self.check(args, carrier, core)


class ForwardedOutputTest(SecretCase):
    """What the importer or a script prints is filtered line by line, on both streams, whatever the command."""

    def run_leaky(self, *args: str, exit_code: int = 0):
        env = self.s.wired(
            OMP_MEMORY_BUN=str(self.leaky_bun),
            LEAK_OUT=f"bank password={PIN} found\n    folder: ~/work/Token_{PIN}/app\nplain line",
            LEAK_ERR=f"warning: password={PIN}",
            LEAK_EXIT=str(exit_code),
        )
        return self.s.run(*args, env=env)

    def test_both_streams_of_the_importer_are_filtered_and_ordinary_lines_pass(self):
        self.s.bank("plain-1abc", "work/plain")
        for args in (("import", "scan"), ("import", "dry-run"), ("import", "verify"), ("setup", "--dry-run")):
            with self.subTest(args=args):
                r = self.run_leaky(*args)
                self.assertNotIn(PIN, r.out + r.err)
                self.assertIn("plain line", r.out)
                self.assertIn("found", r.out)

    def test_the_exit_status_of_the_child_is_unchanged(self):
        self.s.bank("plain-1abc", "work/plain")
        self.assertEqual(self.run_leaky("import", "scan", exit_code=3).code, 3)
        self.assertEqual(self.run_leaky("import", "scan", exit_code=0).code, 0)

    def test_the_dry_run_report_still_parses_when_it_is_json(self):
        self.s.bank("plain-1abc", "work/plain")
        report = json.dumps({"command": "dry-run", "source": "claude-memory", "counts": {"found": 0}, "perBank": []})
        env = self.s.wired(OMP_MEMORY_BUN=str(self.leaky_bun), LEAK_OUT=report, LEAK_ERR="")
        r = self.s.run("import", "dry-run", env=env)
        self.assertEqual(r.code, 0, r.err)
        self.assertIn("dry run recorded", r.err)
        self.assertIn('"command": "dry-run"', r.out)


class StructuredOutputTest(SecretCase):
    """A JSON document the importer prints stays a JSON document: only the strings that carry a secret are replaced."""

    # A path like a temporary directory's (mixed case and digits) under a key named `originKey` is what a report holds for a
    # held item; read as a line of text it looks like `key = secret value`, read as the string it is, it is a path.
    KEY = "/private/var/T/tmp.S7QBnmQ2Ch/omp-memory-smoke.Psf5px/home/work/smoke-b#secret"

    def report(self) -> dict:
        return {
            "command": "dry-run",
            "counts": {"found": 5, "skippedHeld": 1},
            "held": [{"originKey": self.KEY, "kinds": ["hold:password-value", "secret-assignment"]}],
            "perBank": [{"bank": "work-1abc", "wouldImport": 3, "folder": "~/work/x"}],
            "nested": {"list": [{"deep": [1, 2.5, None, True, "plain text"]}], "empty": {}, "none": []},
            f"password-{PIN}": 1,
            "note": f"the line says password={PIN} here",
            "folder": f"~/work/Token_{PIN}/app",
        }

    def expected(self) -> dict:
        expected = self.report()
        del expected[f"password-{PIN}"]
        expected["name#1"] = 1
        expected["note"] = "the line says name#2 here"
        expected["folder"] = "folder#1"
        return expected

    def forward(self, text: str, *args: str):
        path = self.s.root / "report.txt"
        path.write_text(text)
        env = self.s.wired(OMP_MEMORY_BUN=str(self.leaky_bun), LEAK_OUT="", LEAK_ERR="")
        env["LEAK_OUT_FILE"] = str(path)
        return self.s.run(*(args or ("import", "scan")), env=env)

    def test_a_pretty_printed_report_is_forwarded_as_the_same_document(self):
        for indent in (2, 4, "\t"):
            with self.subTest(indent=indent):
                text = json.dumps(self.report(), indent=indent, ensure_ascii=False) + "\n"
                r = self.forward(text)
                self.assertEqual(r.code, 0, r.err)
                shown = json.loads(r.out)  # still a document
                self.assertEqual(shown["held"], self.report()["held"])  # an ordinary key and the kinds are untouched
                self.assertEqual(shown["nested"], self.report()["nested"])
                self.assertNotIn(PIN, r.out + r.err)
                self.assertEqual(r.out.splitlines()[:3], text.splitlines()[:3])  # same layout, same indentation
                self.assertIn(self.KEY, r.out)
                self.assertTrue(r.out.endswith("}\n"))

    def test_only_the_strings_that_carry_a_secret_are_replaced(self):
        r = self.forward(json.dumps(self.report(), indent=2) + "\n")
        shown = json.loads(r.out)
        untouched = ("held", "nested")
        self.assertEqual(
            {k: v for k, v in shown.items() if k not in untouched},
            {k: v for k, v in self.expected().items() if k not in untouched},
        )

    def test_a_compact_document_and_a_document_after_text_lines_are_handled_too(self):
        compact = json.dumps(self.report(), separators=(",", ":"))
        r = self.forward(compact + "\n")
        self.assertEqual(json.loads(r.out)["held"], self.report()["held"])
        self.assertNotIn(PIN, r.out)
        self.assertEqual(r.out.count("\n"), 1)
        lines = "progress: reading\n" + json.dumps(self.report(), indent=2) + "\nfooter: done\n"
        r = self.forward(lines)
        self.assertTrue(r.out.startswith("progress: reading\n{\n"))
        self.assertTrue(r.out.endswith("}\nfooter: done\n"))
        document = r.out[len("progress: reading\n") : -len("footer: done\n")]
        self.assertEqual(json.loads(document)["held"], self.report()["held"])
        self.assertNotIn(PIN, r.out)

    def test_text_that_only_looks_like_the_start_of_a_document_is_forwarded_line_by_line(self):
        r = self.forward("[info] starting\n[1/2] reading\n{\nnot json at all\n")
        self.assertEqual(r.out, "[info] starting\n[1/2] reading\n{\nnot json at all\n")
        r = self.forward(f"{{\nbank password={PIN}\n")
        self.assertNotIn(PIN, r.out)

    def test_the_wrappers_own_json_stays_valid_whatever_its_keys_say(self):
        import io
        from contextlib import redirect_stdout

        from omp_memory import sink

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            sink.emit_json({"password": "x9", "token": ["a1B2c3"], "api key": f"password={PIN}", "n": [1, 2]})
        shown = json.loads(buffer.getvalue())
        self.assertEqual(shown["n"], [1, 2])
        self.assertNotIn(PIN, buffer.getvalue())


CRASHING_SITE = """
import importlib.abc, importlib.machinery, sys

SECRET = %(secret)r

class Finder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    # a stand-in for a command module that fails while it loads, or that crashes a thread and a finaliser
    def find_spec(self, name, path=None, target=None):
        if name == "omp_memory.doctor":
            return importlib.machinery.ModuleSpec(name, self)
        return None
    def create_module(self, spec):
        return None
    def exec_module(self, module):
        mode = %(mode)r
        if mode == "import":
            raise RuntimeError("cannot load " + SECRET)
        import threading
        def boom():
            raise RuntimeError("thread says " + SECRET)
        thread = threading.Thread(target=boom)
        thread.start()
        thread.join()
        class Late:
            def __del__(self):
                raise RuntimeError("finaliser says " + SECRET)
        Late()
        module.run_doctor = lambda args, config: 0

sys.meta_path.insert(0, Finder())
"""


class BootstrapTest(ScratchCase):
    """Nothing the launcher or the entry point prints names a path or a value; a failure before the commands load is categorical."""

    SECRET_NAMES = ("password=123456", "ghp_" + secrets.token_hex(18))

    def assertNoSecret(self, r, *values: str):
        for value in (*values, *self.SECRET_NAMES):
            self.assertNotIn(value, r.out + r.err)
        self.assertNotIn("Traceback", r.out + r.err)

    def test_a_bad_python_override_is_reported_without_its_value(self):
        for name in self.SECRET_NAMES:
            with self.subTest(name=name[:8]):
                missing = f"/nonexistent/{name}/python"
                relative = f"{name}/python"
                stand_in = self.s.bin / name
                stand_in.write_text("#!/bin/sh\nexit 1\n")  # a program that is not a recent Python
                stand_in.chmod(0o755)
                for value in (missing, relative, str(stand_in)):
                    r = self.s.run("--version", env=self.s.env(OMP_MEMORY_PYTHON=value))
                    self.assertEqual(r.code, 2, r.err)
                    self.assertNoSecret(r, value, str(self.s.bin))
                    self.assertIn("OMP_MEMORY_PYTHON", r.err)
                    self.assertIn("absolute path to Python 3.10 or newer", r.err)

    def test_an_old_python_is_reported_without_its_path_or_version_of_the_installation(self):
        link_dir = self.s.root / self.SECRET_NAMES[0]
        link_dir.mkdir()
        link = link_dir / "python3"
        link.symlink_to(sys.executable)
        main = PYTHON_DIR / "omp_memory_main.py"
        code = (
            "import sys, runpy; sys.version_info = (3, 9, 0, 'final', 0); "
            f"sys.argv = ['m', '--version']; runpy.run_path({str(main)!r}, run_name='__main__')"
        )
        proc = subprocess.run([str(link), "-c", code], capture_output=True, text=True, check=False, env=self.s.env())
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("needs Python 3.10 or newer", proc.stderr)
        self.assertNoSecret(Result(proc), str(link), str(link_dir), str(main))

    def copy_plugin(self, name: str, *, vendor: bool, main: bool = True) -> Path:
        root = self.s.root / name / "plugin"
        shutil.copytree(PLUGIN / "bin", root / "bin")
        shutil.copytree(PLUGIN / "python", root / "python", ignore=shutil.ignore_patterns("__pycache__"))
        if vendor:
            shutil.copytree(PLUGIN / "vendor", root / "vendor", ignore=shutil.ignore_patterns("__pycache__"))
        if not main:
            (root / "python" / "omp_memory_main.py").unlink()
        return root

    def test_an_incomplete_installation_is_reported_without_its_location(self):
        for name in self.SECRET_NAMES:
            with self.subTest(name=name[:8]):
                for vendor, main in ((True, False), (False, True)):
                    root = self.copy_plugin(f"{name}/{vendor}", vendor=vendor, main=main)
                    proc = subprocess.run(
                        [str(root / "bin" / "omp-memory"), "--version"],
                        capture_output=True,
                        text=True,
                        check=False,
                        env=self.s.env(),
                    )
                    self.assertEqual(proc.returncode, 3 if not vendor else 2, proc.stderr)
                    self.assertNoSecret(Result(proc), str(root), name)
                    self.assertIn("installation is incomplete", proc.stderr)

    def run_crashing(self, mode: str, *args: str):
        site = self.s.root / "site"
        site.mkdir(exist_ok=True)
        (site / "sitecustomize.py").write_text(CRASHING_SITE % {"secret": self.SECRET_NAMES[0], "mode": mode})
        if not (self.s.banks / "plain-1abc").exists():
            self.s.bank("plain-1abc", "work/plain")
        return self.s.run(*args, env=self.s.wired(PYTHONPATH=str(site)))

    def test_a_command_module_that_fails_to_load_is_an_internal_error_with_status_3(self):
        r = self.run_crashing("import", "banks")
        self.assertEqual(r.code, 3, r.out + r.err)
        self.assertNoSecret(r)
        self.assertIn("omp-memory: internal error (", r.err)
        # what needs no command module still works
        self.assertEqual(self.run_crashing("import", "--version").code, 0)

    def test_a_crashing_thread_or_finaliser_is_an_internal_error_with_status_3(self):
        r = self.run_crashing("threads", "banks")
        self.assertEqual(r.code, 3, r.out + r.err)
        self.assertNoSecret(r)
        self.assertIn("internal error (thread: RuntimeError)", r.err)
        self.assertIn("internal error (unraisable: RuntimeError)", r.err)

    def test_the_sink_hooks_are_installed_before_the_commands_are_loaded(self):
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys, threading; sys.path.insert(0, sys.argv[1]); from omp_memory import sink; "
                "assert 'omp_memory.cli' not in sys.modules and 'omp_memory.ops' not in sys.modules; "
                "sink.install(); t = threading.Thread(target=lambda: 1 / 0); t.start(); t.join(); "
                "sys.exit(0 if sink.crashed() else 1)",
                str(PYTHON_DIR),
            ],
            capture_output=True,
            text=True,
            check=False,
            env=self.s.env(),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("internal error (thread: ZeroDivisionError)", proc.stderr)

    def test_sigterm_ends_a_command_quietly(self):
        import signal
        import time

        self.s.bank("plain-1abc", "work/plain")
        hang = self.s._script("bun-hang", '#!/bin/sh\nif [ "$1" = --version ]; then echo 1.4.4; exit 0; fi\nsleep 30\n')
        child = subprocess.Popen(
            [str(LAUNCHER), "import", "scan"],
            env=self.s.wired(OMP_MEMORY_BUN=str(hang)),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(3)
        child.send_signal(signal.SIGTERM)
        out, err = child.communicate(timeout=60)
        self.assertEqual(child.returncode, 143, err)
        self.assertNotIn("Traceback", out + err)


class InProcessTest(unittest.TestCase):
    """The top-level handler and the hooks: no traceback, no raw exception text, argparse and warnings included."""

    def run_main(self, argv, **env):
        import support  # noqa: F401  (sys.path)
        from omp_memory import cli

        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err), mock.patch.dict(os.environ, env):
            try:
                code = cli.main(argv)
            except SystemExit as exit_:
                code = exit_.code
        return code, out.getvalue(), err.getvalue()

    def test_an_unexpected_exception_prints_a_categorical_message_and_no_traceback(self):
        import support  # noqa: F401
        from omp_memory import core

        with mock.patch.object(core, "overview", side_effect=RuntimeError(f"password={PIN} broke")):
            with mock.patch.dict(os.environ, {"HOME": "/nonexistent-home"}):
                code, out, err = self.run_main(["banks", "--banks-dir", "/tmp"])
        self.assertEqual(code, 3)
        self.assertNotIn("Traceback", out + err)
        self.assertNotIn(PIN, out + err)
        self.assertIn("internal error (RuntimeError)", err)

    def test_a_python_warning_goes_through_the_sink(self):
        import support  # noqa: F401
        from omp_memory import sink

        err = StringIO()
        with redirect_stderr(err), warnings.catch_warnings():
            sink.install()
            warnings.warn(f"password={PIN} deprecated", DeprecationWarning, stacklevel=1)
        self.assertNotIn(PIN, err.getvalue())

    def test_argparse_errors_and_help_go_through_the_sink(self):
        import support  # noqa: F401
        from omp_memory import info

        err = StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            info.parser("banks", "x").parse_args([f"--password={PIN}"])
        self.assertEqual(caught.exception.code, 2)
        self.assertNotIn(PIN, err.getvalue())
        self.assertIn("usage", err.getvalue())


class DetectorTest(unittest.TestCase):
    """The Python mirror of the importer's `carriesSecret`: the same positives, and the same names left alone."""

    CARRIES: ClassVar[list[str]] = [
        "password=" + PIN,
        "password-" + PIN,
        "Password_" + PIN,
        "PASSWORD." + PIN,
        "password " + PIN,
        "password%3D" + PIN,
        "password%253D" + PIN,
        "Password%3a" + PIN,
        "token-a1B2c3",
        "api key 9f8e12",
        "api-key-9f8e12",
        "secret.2024",
        "password" + PIN,
        "credential-9f8e12",
        "auth.1234",
        "work/password=" + PIN + "/app",
        "~/work/Token_" + PIN + "/proj",
        "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2",
        "GHP_" + "a1B2c3D4e5F6g7H8i9J0k1L2",
        "sk-" + "a1B2c3D4e5F6g7H8i9J0",
        "github_pat_" + "a1B2c3D4e5F6g7H8i9J0k1L2",
        "glpat-" + "a1B2c3D4e5F6g7H8i9J0",
        "xoxb-" + "1234567890-abcdef",
        "AKIA" + "ABCDEFGH12345678",
        "Bearer " + "abcdefghijklmnopqrstuvwxyz0123456789",
        "https://user:hunter2xx@example.test/x",
        "-----BEGIN RSA PRIVATE KEY-----",
        "login admin / Pa55word!",
    ]
    CLEAN: ClassVar[list[str]] = [
        "plain-1abc",
        "sms-1qfllt3yk702w",
        "token-service",
        "Secret Santa",
        "password-policy",
        "tokenizer-2",
        "author-notes",
        "work/alpha",
        "~/projects/demo",
        "Work",
        "My Project",
        "auth-service",
        "README.md",
        "unknown command 'frobnicate'",
        "--banks-dir",
    ]

    def setUp(self):
        import support  # noqa: F401
        from omp_memory import detect

        self.detect = detect

    def test_secrets_are_recognised_in_every_form(self):
        for text in self.CARRIES:
            with self.subTest(text=text[:20]):
                self.assertTrue(self.detect.carries_secret(text))

    def test_ordinary_names_are_left_alone(self):
        for text in self.CLEAN:
            with self.subTest(text=text):
                self.assertFalse(self.detect.carries_secret(text))

    def test_labels_are_indexes_by_first_appearance_per_kind(self):
        from omp_memory import sink

        sink.reset_labels()
        self.assertEqual(sink.label("bank", "password-" + PIN), "bank#1")
        self.assertEqual(sink.label("bank", "Token_" + PIN), "bank#2")
        self.assertEqual(sink.label("bank", "password-" + PIN), "bank#1")
        self.assertEqual(sink.label("folder", "work/password=" + PIN), "folder#1")
        self.assertNotRegex(sink.label("bank", f"Token_{PIN}"), r"[0-9a-f]{6,}")

    def test_the_help_texts_are_not_mistaken_for_secrets(self):
        from omp_memory import cli, info, ops, sink

        texts = [cli.__doc__, ops.IMPORT_HELP, ops.SETUP_HELP]
        for prog in ("banks", "sources", "labels", "doctor"):
            texts.append(info.parser(prog, "x").format_help())
        for text in texts:
            self.assertEqual(sink.scrub(text), text)

    def test_scrub_keeps_ordinary_text_byte_for_byte_and_withholds_the_value_only(self):
        from omp_memory import sink

        sink.reset_labels()
        plain = "BANK        ROWS  FOLDER\nplain-1abc  4     ~/work/plain\n"
        self.assertEqual(sink.scrub(plain), plain)
        line = f"unreadable: password={PIN}: not a database"
        scrubbed = sink.scrub(line)
        self.assertNotIn(PIN, scrubbed)
        self.assertTrue(scrubbed.startswith("unreadable: "))
        quoted = sink.scrub(f'  "bank": "password-{PIN}",\n')
        self.assertNotIn(PIN, quoted)
        self.assertRegex(quoted, r'^  "bank": "[a-z]+#\d+",\n$')


def stat_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


if __name__ == "__main__":
    unittest.main()
