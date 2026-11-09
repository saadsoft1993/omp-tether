#!/usr/bin/env python3
"""The publication gate: everything that must hold before a repository is published, in one command.

It fails closed. A missing tool, an error, an unsupported entry or a finding anywhere makes it exit non-zero; it never
prints success after skipping a step.

  1. one candidate commit is resolved (default HEAD) and used for everything below; the index and the working tree are
     ignored, and uncommitted changes are reported but never part of anything;
  2. public_tree_check.py on the original repository: the candidate tree and every commit reachable from any ref;
  3. gitleaks on the original repository's complete history (every ref), because the allowlisted-path checks do not
     look for credentials;
  4. a fresh single-commit export of the candidate commit (modes and symlinks kept), compared entry by entry (paths,
     blob ids, modes) against the allowlisted entries of the candidate;
  5. public_tree_check.py, gitleaks (history) and gitleaks (the exported files, without git) on that exact artifact.

gitleaks is required. Findings show rule and location but never the matched text.

Usage: release_gate.py --repo DIR --allowlist FILE (--identity "Name <email>" | uses git user.name/user.email)
                       [--commit REF] [--private-terms FILE] [--binary-allow FILE] [--exempt FILE] [--keep-export DIR]
Exit codes: 0 every step passed, 1 a step failed, 2 usage or missing tool.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from public_tree_check import DENY_GLOBS, glob_to_re, load_lines, ls_tree


def run(cmd: list[str], label: str) -> bool:
    print(f"== {label}", flush=True)
    r = subprocess.run(cmd)
    if r.returncode != 0:
        print(f"FAIL {label} (exit {r.returncode})", flush=True)
        return False
    return True


def gitleaks(repo_or_dir: Path, label: str, *, history: bool) -> bool:
    cmd = ["gitleaks", "detect", "--source", str(repo_or_dir), "--no-banner", "--redact", "--exit-code", "1"]
    if history:
        cmd += ["--log-opts", "--all"]
    else:
        cmd += ["--no-git"]
    return run(cmd, f"gitleaks: {label}")


def entries_of(repo: Path, commit: str, allow: list, deny: list) -> dict[str, tuple[str, str]]:
    out = {}
    for e in ls_tree(str(repo), commit):
        if any(a.match(e.path) for a in allow) and not any(d.match(e.path) for d in deny):
            out[e.path] = (e.mode, e.sha)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", type=Path, required=True)
    ap.add_argument("--allowlist", type=Path, required=True)
    ap.add_argument("--identity", help="the one author and committer identity, as 'Name <email>'")
    ap.add_argument("--commit", default="HEAD")
    ap.add_argument("--private-terms", type=Path)
    ap.add_argument("--binary-allow", type=Path)
    ap.add_argument("--exempt", type=Path)
    ap.add_argument("--keep-export", type=Path, help="write the export here instead of a temporary directory")
    args = ap.parse_args()

    missing = [t for t in ("git", "gitleaks") if shutil.which(t) is None]
    if missing:
        print(
            f"FAIL required tool(s) not found: {', '.join(missing)}. The gate does not skip a credential scan: install "
            "gitleaks and run it again.",
            file=sys.stderr,
        )
        return 2

    repo = args.repo.resolve()

    def git(*a: str) -> str:
        r = subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"git {' '.join(a)}: {r.stderr.strip()}")
        return r.stdout.strip()

    try:
        commit = git("rev-parse", "--verify", f"{args.commit}^{{commit}}")
        identity = args.identity
        if not identity:
            name, email = git("config", "user.name"), git("config", "user.email")
            identity = f"{name} <{email}>"
        name, _, rest = identity.partition(" <")
        email = rest.rstrip(">")
        dirty = git("status", "--porcelain")
    except RuntimeError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 2
    if not name or not email:
        print("FAIL --identity must look like 'Name <email>'", file=sys.stderr)
        return 2
    print(f"candidate commit {commit[:10]}")
    if dirty:
        print("note: uncommitted changes exist; they are not part of the candidate and are not checked")

    tmp = Path(tempfile.mkdtemp(prefix="omp-public-gate."))
    try:
        idents = tmp / "identities.txt"
        idents.write_text(f"{identity}\n")
        check_opts = ["--allowlist", str(args.allowlist), "--identities", str(idents)]
        for flag, value in (
            ("--private-terms", args.private_terms),
            ("--binary-allow", args.binary_allow),
            ("--exempt", args.exempt),
        ):
            if value:
                check_opts += [flag, str(value)]
        checker = [sys.executable, str(HERE / "public_tree_check.py")]

        ok = run([*checker, str(repo), "--commit", commit, *check_opts], "tree and history of the repository")
        ok = gitleaks(repo, "every ref of the repository", history=True) and ok

        export = args.keep_export or tmp / "fresh"
        ok_export = run(
            [
                sys.executable,
                str(HERE / "fresh_history.py"),
                str(repo),
                str(export),
                "--allowlist",
                str(args.allowlist),
                "--name",
                name,
                "--email",
                email,
                "--commit",
                commit,
            ],
            "fresh single-commit export",
        )
        ok = ok and ok_export
        if ok_export:
            allow = [glob_to_re(g) for g in load_lines(str(args.allowlist))]
            deny = [glob_to_re(g) for g in DENY_GLOBS]
            want = entries_of(repo, commit, allow, deny)
            got = {e.path: (e.mode, e.sha) for e in ls_tree(str(export), "HEAD")}
            everything = {e.path for e in ls_tree(str(repo), commit)}
            problems = []
            if want != got:
                problems.append(
                    f"the export differs from the candidate at {len(set(want) ^ set(got))} path(s) or in modes/blob ids"
                )
            if set(want) != everything:
                problems.append(
                    f"{len(everything - set(want))} tracked path(s) of the candidate are not exported (not allowlisted)"
                )
            count = subprocess.run(
                ["git", "-C", str(export), "rev-list", "--count", "HEAD"], capture_output=True, text=True
            ).stdout.strip()
            if count != "1":
                problems.append(f"the export has {count} commits, expected 1")
            print("== export equals the candidate (paths, blob ids, modes)")
            for p in problems:
                print(f"FAIL {p}")
            ok = ok and not problems
            ok = run([*checker, str(export), *check_opts], "tree and history of the export") and ok
            ok = gitleaks(export, "the export's history", history=True) and ok
            ok = gitleaks(export, "the exported files", history=False) and ok
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if not ok:
        print("public-tree check FAILED")
        return 1
    print("public-tree check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
