#!/usr/bin/env python3
"""Allowlist check for a tree that is about to be published.

Checks, for a git repository:
  1. every path of the candidate commit's tree (read from the commit, not the index) and of every commit
     reachable from any ref matches an allowlist glob and no deny glob, and is itself free of the content rules;
  2. every distinct file version in those trees (merge resolutions included) is free of the content rules (tool
     names, trailers, private hosts, home paths, emails); binary or non-UTF-8 files fail unless listed in
     --binary-allow, because their content cannot be checked;
  3. every commit message, tag message and ref name is free of the same rules, and every author/committer
     identity is on the identity allowlist;
  4. submodule entries and unsupported file modes fail.
Findings show the rule and the location but not the matched text, unless --show-matches is given.

Exit codes: 0 clean, 1 findings, 2 usage or configuration error.
Standard library only (Python 3.10+).
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Words are assembled from fragments so this file does not trip its own rules.
_NAMES = [
    "Cl" + "aude",
    "Anthr" + "opic",
    "Cod" + "ex",
    "Open" + "AI",
    "Chat" + "GPT",
    "Co" + "pilot",
    "Gem" + "ini",
    "Gr" + "ok",
]
_TRAILER = "Co-" + "Authored-" + "By"
_GENERATED = "Gener" + "ated (?:with|by)"
_ROBOT = "\U0001f916"

# Phrases that reveal how work or reviews were done. Public files say what a thing is and does, never who or what wrote
# or reviewed it ("independent security review" is fine). Assembled from fragments so this file passes its own check.
_PROVENANCE = [
    r"\bmodel" + r" famil",
    r"\bdifferent" + r" model",
    r"\breview" + r"ers of\b",
    r"\breviewed" + r" by (?:an? )?(?:ai|llm|model|agent)\b",
    r"\bai" + r"-assisted\b",
    r"\bai" + r"-generated\b",
    r"\bgenerated" + r" (?:by|with)\b",
    r"\bwritten" + r" by (?:an? )?(?:ai|llm|model|agent)\b",
    r"\bco" + r"-authored\b",
    r"\bllm" + r"-written\b",
    r"\bas an" + r" ai\b",
]
# The one product-subject phrase of the same family. It fails everywhere unless a committed exemption names the file
# (--exempt, rule "language-model"), for a plugin whose subject really is an LLM.
_LANGUAGE_MODEL = r"\blanguage" + r" models?\b"

# Paths that never go public, whatever the allowlist says. Deny beats allow.
DENY_GLOBS = [
    "**/" + "CLAU" + "DE.md",
    "**/" + "AGE" + "NTS.md",
    "**/.cl" + "aude/**",
    ".omc/**",
    ".graphify/**",
    "**/.cursorrules",
    ".omp/**",
    "docs/research/**",
    "**/.env",
    "**/.env.local",
    "**/.env.*.local",
    "**/*.pem",
    "**/*.key",
    "**/__pycache__/**",
    "**/*.pyc",
    "**/.DS_Store",
    "**/node_modules/**",
    "**/*.jsonl",
    "**/*.sqlite",
    "**/*.db",
]


@dataclass(frozen=True)
class Rule:
    rule_id: str
    pattern: re.Pattern
    severity: str  # "fail" or "warn"
    why: str


def build_rules(private_terms: list[str]) -> list[Rule]:
    names = "|".join(_NAMES)
    rules = [
        Rule("ai-name", re.compile(rf"\b(?:{names})\b", re.I), "fail", "names an AI tool or vendor"),
        Rule("ai-trailer", re.compile(_TRAILER + ":", re.I), "fail", "agent co-author trailer"),
        Rule("ai" + "-generated", re.compile(_GENERATED + r"\b", re.I), "fail", "generated-with line"),
        Rule("ai-emoji", re.compile(_ROBOT), "fail", "robot emoji"),
        Rule("provenance", re.compile("|".join(_PROVENANCE), re.I), "fail", "says how work or reviews were done"),
        Rule(
            "language-model",
            re.compile(_LANGUAGE_MODEL, re.I),
            "fail",
            "names a language" + " model; allowed only in files an exemption lists",
        ),
        Rule("tailnet-host", re.compile(r"\b(?:[a-z0-9-]+\.)+ts\.net\b", re.I), "fail", "Tailscale MagicDNS host"),
        Rule("home-path", re.compile(r"/Users/(?!Shared\b)[A-Za-z0-9._-]+"), "fail", "absolute macOS home path"),
        Rule("agent-word", re.compile(r"\bagents?\b", re.I), "warn", "the word 'agent' (check context)"),
    ]
    for term in private_terms:
        rules.append(Rule("private-term", re.compile(term, re.I), "fail", "private term list"))
    email_re = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
    rules.append(Rule("email", email_re, "fail", "email address not on the allowlist"))
    rules.append(
        Rule("cgnat-ip", re.compile(r"\b100\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"), "fail", "Tailscale (CGNAT) address")
    )
    return rules


def rule_hits(rule: Rule, line: str, allowed_emails: set[str]):
    for m in rule.pattern.finditer(line):
        text = m.group(0)
        if rule.rule_id == "email":
            low = text.lower()
            reserved = ("@example.com", "@example.org", "@example.net", "@users.noreply.github.com")
            if (
                low in allowed_emails
                or low.endswith(reserved)
                or low.rsplit(".", 1)[-1] in {"example", "invalid", "test"}
            ):
                continue
        if rule.rule_id == "cgnat-ip":
            try:
                addr = ipaddress.ip_address(text)
            except ValueError:
                continue
            # the network's own base address is how docs and code name the range
            if addr not in ipaddress.ip_network("100.64.0.0/10") or text == "100.64.0.0":
                continue
        yield text


def glob_to_re(glob: str) -> re.Pattern:
    out, i = [], 0
    while i < len(glob):
        c = glob[i]
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def load_lines(path: str | None) -> list[str]:
    if not path:
        return []
    return [
        ln.strip()
        for ln in Path(path).expanduser().read_text().splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]


def git(repo: str, *args: str) -> bytes:
    res = subprocess.run(["git", "-C", repo, *args], capture_output=True)
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {res.stderr.decode(errors='replace').strip()}")
    return res.stdout


def short_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:8]


@dataclass(frozen=True)
class Entry:
    mode: str
    kind: str
    sha: str
    path: str


def ls_tree(repo: str, commit: str) -> list[Entry]:
    """Every entry of one commit's tree, recursively, exactly as recorded (modes, types, blob ids)."""
    out = git(repo, "ls-tree", "-r", "-z", "--full-tree", commit)
    entries = []
    for raw in out.split(b"\0"):
        if not raw:
            continue
        meta, _, path = raw.partition(b"\t")
        mode, kind, sha = meta.decode().split(" ")
        entries.append(Entry(mode, kind, sha, path.decode("utf-8", "surrogateescape")))
    return entries


class BlobReader:
    """Reads blobs through one `git cat-file --batch` process."""

    def __init__(self, repo: str) -> None:
        self.proc = subprocess.Popen(
            ["git", "-C", repo, "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE
        )

    def read(self, sha: str) -> bytes:
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(sha.encode() + b"\n")
        self.proc.stdin.flush()
        header = self.proc.stdout.readline().split()
        if len(header) != 3:
            raise RuntimeError(f"git cat-file could not read {sha}")
        data = self.proc.stdout.read(int(header[2]))
        self.proc.stdout.read(1)  # the newline after the object
        return data

    def close(self) -> None:
        if self.proc.stdin:
            self.proc.stdin.close()
        self.proc.wait()


class Report:
    """Findings. By default a finding shows its rule and location but not the text that matched: CI logs and shared
    transcripts must not become a publication channel for the very names the check exists to keep private."""

    def __init__(self, show_matches: bool) -> None:
        self.show = show_matches
        self.fails: list[str] = []
        self.warns: dict[str, int] = {}

    def fail(self, where: str, rule_id: str, detail: str, secret: str = "") -> None:
        shown = f"{detail}: {secret!r}" if (secret and self.show) else detail
        if secret and not self.show:
            shown = f"{detail} [match hidden; rerun locally with --show-matches]"
        self.fails.append(f"FAIL {rule_id:<14} {where}  {shown}")

    def warn(self, where: str) -> None:
        self.warns[where] = self.warns.get(where, 0) + 1


def scan_text(
    report: Report, where: str, text: str, rules: list[Rule], allowed_emails: set[str], line_offset: int = 0
) -> None:
    for n, line in enumerate(text.splitlines(), 1):
        for rule in rules:
            for hit in rule_hits(rule, line, allowed_emails):
                if rule.severity == "warn":
                    report.warn(where)
                else:
                    report.fail(f"{where}:{n + line_offset}", rule.rule_id, rule.why, secret=hit)


def has_hits(text: str, rules: list[Rule], emails: set[str]) -> bool:
    """Does a content rule that fails the check match this text?"""
    probe = Report(True)
    scan_text(probe, "x", text, [r for r in rules if r.severity == "fail"], emails)
    return bool(probe.fails)


def shown_name(report: Report, name: str, rules: list[Rule], emails: set[str], kind: str = "path") -> str:
    """How a path or ref name appears in a finding: as it is, unless it matches a content rule itself (the name
    may be the very thing kept private), in which case only its hash, unless --show-matches was given."""
    if report.show or not has_hits(name, rules, emails):
        return name
    return f"{kind}#{short_hash(name)}"


def check_path(
    report: Report, where: str, path: str, allow: list[re.Pattern], deny: list[re.Pattern], shown: str | None = None
) -> None:
    shown = path if shown is None else shown
    if any(p.match(path) for p in deny):
        report.fail(where, "path-denied", f"{shown} matches a deny glob")
    elif not any(p.match(path) for p in allow):
        report.fail(where, "path-not-allowed", f"{shown} is not on the allowlist")


def scan_path_string(report: Report, where: str, path: str, rules: list[Rule], emails: set[str]) -> None:
    """The content rules apply to file names too. The location is the path's hash: the name may be the private part."""
    probe = Report(True)
    scan_text(probe, "x", path, [r for r in rules if r.severity == "fail"], emails)
    for line in probe.fails:
        rule_id = line.split()[1]
        report.fail(f"{where} path#{short_hash(path)}", rule_id, "a file name matches a content rule", secret=path)


def scan_blob(
    report: Report,
    where: str,
    path: str,
    data: bytes,
    rules: list[Rule],
    emails: set[str],
    binary_ok: list[re.Pattern],
) -> None:
    try:
        if b"\0" in data:
            raise ValueError("NUL byte")
        text = data.decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        if not any(p.match(path) for p in binary_ok):
            report.fail(
                where,
                "binary-blob",
                "this file is binary or not UTF-8, so its content cannot be checked; list it in --binary-allow after review",
            )
        return
    scan_text(report, where, text, rules, emails)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo", help="path to the git repository to check")
    ap.add_argument("--allowlist", required=True, help="file with one allowed path glob per line")
    ap.add_argument("--identities", required=True, help="file with one allowed 'Name <email>' per line")
    ap.add_argument(
        "--private-terms", help="file of private regexes (hostnames, tailnet name); keep it outside the repo"
    )
    ap.add_argument("--allow-email", action="append", default=[], help="email that may appear in content (repeatable)")
    ap.add_argument(
        "--exempt", help="JSON: {rule_id: [path globs]} with reasons in a sibling key; skips a rule in those paths"
    )
    ap.add_argument("--binary-allow", help="file with one path glob per line of binary files reviewed and accepted")
    ap.add_argument(
        "--commit", default="HEAD", help="the commit whose tree is the publication candidate (default HEAD)"
    )
    ap.add_argument("--show-matches", action="store_true", help="print the matched text (local runs only; never in CI)")
    ap.add_argument("--no-history", action="store_true", help="only check the candidate commit's tree")
    args = ap.parse_args(argv)

    try:
        allow = [glob_to_re(g) for g in load_lines(args.allowlist)]
        if not allow:
            print("error: allowlist is empty", file=sys.stderr)
            return 2
        deny = [glob_to_re(g) for g in DENY_GLOBS]
        binary_ok = [glob_to_re(g) for g in load_lines(args.binary_allow)]
        identities = set(load_lines(args.identities))
        if not identities:
            print("error: identity allowlist is empty", file=sys.stderr)
            return 2
        emails = {a.lower() for a in args.allow_email} | {
            m.group(1).lower() for i in identities if (m := re.search(r"<([^>]+)>", i))
        }
        rules = build_rules(load_lines(args.private_terms))
        exempt: dict[str, list[re.Pattern]] = {}
        if args.exempt:
            for rid, globs in json.loads(Path(args.exempt).read_text()).get("rules", {}).items():
                exempt[rid] = [glob_to_re(g) for g in globs]
        git(args.repo, "rev-parse", "--git-dir")
        candidate = git(args.repo, "rev-parse", "--verify", f"{args.commit}^{{commit}}").decode().strip()
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    def rules_for(path: str) -> list[Rule]:
        return [r for r in rules if not any(p.match(path) for p in exempt.get(r.rule_id, []))]

    report = Report(args.show_matches)
    reader = BlobReader(args.repo)
    scanned: set[tuple[str, frozenset[str]]] = set()  # (blob id, rules skipped for its path): each version once
    paths_seen: set[str] = set()

    def check_entries(where: str, entries: list[Entry]) -> None:
        for e in entries:
            shown = shown_name(report, e.path, rules_for(e.path), emails)
            if e.path not in paths_seen:
                paths_seen.add(e.path)
                check_path(report, f"{where}", e.path, allow, deny, shown)
                scan_path_string(report, where, e.path, rules_for(e.path), emails)
            if e.kind == "commit" or e.mode == "160000":
                report.fail(where, "unsupported-entry", f"{shown} is a submodule entry, which is not published")
                continue
            if e.kind != "blob":
                report.fail(where, "unsupported-entry", f"{shown} has unsupported type {e.kind}")
                continue
            if e.mode not in ("100644", "100755", "120000"):
                report.fail(where, "unsupported-entry", f"{shown} has unsupported mode {e.mode}")
                continue
            skipped = frozenset(r.rule_id for r in rules if r not in rules_for(e.path))
            key = (e.sha, skipped)
            if key in scanned:
                continue
            scanned.add(key)
            scan_blob(report, f"{where}:{shown}", e.path, reader.read(e.sha), rules_for(e.path), emails, binary_ok)

    # 1 and 2: the candidate commit's tree, read from the commit (never the index) with every entry's recorded mode
    head_entries = ls_tree(args.repo, candidate)
    check_entries("HEAD", head_entries)

    # 3: every commit reachable from any ref: each distinct file version is scanned once, whatever commit (merge
    # resolutions included) introduced it, instead of parsing human-readable patches
    n_commits = 1
    if not args.no_history:
        log = git(args.repo, "log", "--all", "--format=%x01%H%x00%an <%ae>%x00%cn <%ce>%x00%B%x02").decode(
            "utf-8", "replace"
        )
        commits = [c for c in log.split("\x01") if c.strip("\0 \n")]
        n_commits = len(commits)
        for c in commits:
            head, _, _ = c.partition("\x02")
            sha, author, committer, body = [*head.split("\0", 3), "", "", ""][:4]
            short = sha[:10]
            for who in {author, committer}:
                if who not in identities:
                    report.fail(
                        f"commit {short}",
                        "identity",
                        f"identity #{short_hash(who)} is not on the allowlist",
                        secret=who,
                    )
            scan_text(report, f"commit {short} message", body, rules, emails)
            check_entries(f"history {short}", ls_tree(args.repo, sha))
        for ref in (
            git(args.repo, "for-each-ref", "--format=%(refname)%00%(contents)%02")
            .decode("utf-8", "replace")
            .split("\x02")
        ):
            name, _, contents = ref.strip().partition("\0")
            if name:
                body_text = contents if name.startswith("refs/tags/") else ""
                label = shown_name(report, name, rules, emails, "ref")
                scan_text(report, f"ref {label}", name + "\n" + body_text, rules, emails)
    reader.close()

    for line in sorted(set(report.fails)):
        print(line)
    for where, n in sorted(report.warns.items()):
        print(f"warn agent-word     {where}  {n} occurrence(s), review in context")
    print(
        f"checked commit {candidate[:10]}: {len(head_entries)} tracked files, {n_commits} commit(s), "
        f"{len(scanned)} distinct file versions; {len(set(report.fails))} failing finding(s)"
    )
    return 1 if report.fails else 0


if __name__ == "__main__":
    sys.exit(main())
