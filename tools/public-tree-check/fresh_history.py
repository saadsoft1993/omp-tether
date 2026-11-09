#!/usr/bin/env python3
"""Build a one-commit repository from the allowlisted entries of one commit of another repository.

The source is a single immutable commit (default HEAD), read with `git ls-tree` and `git cat-file`: the index and the
working tree are never consulted, so staged or uncommitted changes cannot reach the export. Each entry keeps its
recorded mode (executable bit) and type (regular file or symlink). Submodules and any other entry type are refused.
The result is verified against the source: the same paths, blob ids and modes, or the export fails.

The new repository has no history of the source, so nothing that was ever committed and removed can leak. Run
public_tree_check.py on the result before publishing. Standard library only.

Usage: fresh_history.py SRC DEST --allowlist FILE --name NAME --email EMAIL [--commit REF] [--message TEXT]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from public_tree_check import DENY_GLOBS, Entry, git, glob_to_re, load_lines, ls_tree

SUPPORTED = {"100644", "100755", "120000"}


def blob(src: str, sha: str) -> bytes:
    return git(src, "cat-file", "blob", sha)


def selected(entries: list[Entry], allow: list, deny: list) -> tuple[list[Entry], list[str]]:
    keep, dropped = [], []
    for e in entries:
        if any(a.match(e.path) for a in allow) and not any(d.match(e.path) for d in deny):
            keep.append(e)
        else:
            dropped.append(e.path)
    return keep, dropped


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src")
    ap.add_argument("dest")
    ap.add_argument("--allowlist", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--email", required=True)
    ap.add_argument("--commit", default="HEAD", help="the commit to export (default HEAD)")
    ap.add_argument("--message", default="Initial import")
    args = ap.parse_args()

    dest = Path(args.dest)
    if dest.exists() and any(dest.iterdir()):
        print(f"error: {dest} exists and is not empty", file=sys.stderr)
        return 2
    allow = [glob_to_re(g) for g in load_lines(args.allowlist)]
    deny = [glob_to_re(g) for g in DENY_GLOBS]
    commit = git(args.src, "rev-parse", "--verify", f"{args.commit}^{{commit}}").decode().strip()
    keep, dropped = selected(ls_tree(args.src, commit), allow, deny)
    bad = [e for e in keep if e.kind != "blob" or e.mode not in SUPPORTED]
    if bad:
        for e in bad:
            print(f"error: unsupported entry {e.path} (mode {e.mode}, type {e.kind})", file=sys.stderr)
        return 1

    dest.mkdir(parents=True, exist_ok=True)
    for e in keep:
        target = dest / e.path
        target.parent.mkdir(parents=True, exist_ok=True)
        data = blob(args.src, e.sha)
        if e.mode == "120000":
            os.symlink(data.decode("utf-8", "surrogateescape"), target)
        else:
            target.write_bytes(data)
            target.chmod(0o755 if e.mode == "100755" else 0o644)
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": args.name,
        "GIT_AUTHOR_EMAIL": args.email,
        "GIT_COMMITTER_NAME": args.name,
        "GIT_COMMITTER_EMAIL": args.email,
    }
    for cmd in (
        ["init", "-q", "-b", "main"],
        ["config", "core.fileMode", "true"],
        ["add", "-A"],
        ["commit", "-q", "-m", args.message],
    ):
        subprocess.run(["git", "-C", str(dest), *cmd], check=True, env=env)

    # verify: what was written is exactly what the source commit records for the kept entries
    want = {e.path: (e.mode, e.sha) for e in keep}
    got = {e.path: (e.mode, e.sha) for e in ls_tree(str(dest), "HEAD")}
    if want != got:
        diff = sorted(set(want) ^ set(got)) or sorted(p for p in want if want[p] != got.get(p))
        print(
            f"error: the export does not match commit {commit[:10]} (differs at {len(diff)} path(s), e.g. {diff[:3]})",
            file=sys.stderr,
        )
        return 1
    print(
        f"wrote {len(keep)} files from commit {commit[:10]} to {dest} as one commit; "
        f"{len(dropped)} path(s) dropped (not allowlisted)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
