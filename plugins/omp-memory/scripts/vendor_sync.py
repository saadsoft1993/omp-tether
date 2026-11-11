#!/usr/bin/env python3
"""Copy the files listed in vendor_manifest.txt from the source tree into vendor/, or check that
nothing drifted.

    vendor_sync.py [--source DIR] [--check]

Without --check the vendored files are replaced by the source's (byte for byte, mode kept), files that are no longer in
the manifest are removed, and vendor/SOURCE.sha256 is rewritten. With --check nothing is written: the exit status is 1
when a vendored file differs from the source's, is missing, is not in the manifest, or when the source has files the
manifest's `tree` entries would now pick up. The source is --source DIR, else $OMP_MEMORY_SOURCE_DIR; there is no default.

Standard library only; Python 3.10 or newer. Nothing is ever written outside vendor/.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
VENDOR = PLUGIN / "vendor"
MANIFEST = Path(__file__).resolve().parent / "vendor_manifest.txt"
RECORD = VENDOR / "SOURCE.sha256"
SKIP_PARTS = {"__pycache__", "node_modules", ".DS_Store", ".pytest_cache"}


def default_source() -> Path | None:
    """The source tree named by $OMP_MEMORY_SOURCE_DIR, or None."""
    value = os.environ.get("OMP_MEMORY_SOURCE_DIR")
    return Path(value).expanduser() if value else None


def parse_manifest(text: str) -> list[tuple[str, str, set[str]]]:
    """[(kind, path, excluded names)] from the manifest text; raises ValueError on a malformed line."""
    entries = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        words = line.split()
        if words[0] == "file" and len(words) == 2:
            entries.append(("file", words[1], set()))
        elif words[0] == "tree" and (len(words) == 2 or (len(words) > 3 and words[2] == "exclude")):
            entries.append(("tree", words[1], set(words[3:])))
        else:
            raise ValueError(f"vendor_manifest.txt line {number}: cannot parse {raw!r}")
        if words[1].startswith("/") or ".." in Path(words[1]).parts:
            raise ValueError(f"vendor_manifest.txt line {number}: path must be relative and stay inside the source")
    return entries


def expand(root: Path, entries: list[tuple[str, str, set[str]]]) -> list[str]:
    """Sorted relative paths of every file the manifest selects below `root` (files that do not exist are kept)."""
    found: set[str] = set()
    for kind, rel, excluded in entries:
        if kind == "file":
            found.add(rel)
            continue
        base = root / rel
        for path in sorted(base.rglob("*")) if base.is_dir() else []:
            if not path.is_file() or path.is_symlink():
                continue
            parts = path.relative_to(base).parts
            if SKIP_PARTS.intersection(parts) or path.name in excluded or path.suffix == ".pyc":
                continue
            found.add(str(path.relative_to(root)))
    return sorted(found)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def vendored_files() -> list[str]:
    return sorted(
        str(p.relative_to(VENDOR))
        for p in VENDOR.rglob("*")
        if p.is_file() and p.name != RECORD.name and not SKIP_PARTS.intersection(p.relative_to(VENDOR).parts)
    )


def read_record() -> dict[str, str]:
    """{relative path: sha256} recorded by the last sync (empty when there is no record)."""
    if not RECORD.is_file():
        return {}
    out = {}
    for line in RECORD.read_text().splitlines():
        digest_, _, rel = line.partition("  ")
        if digest_ and rel:
            out[rel] = digest_
    return out


def compare(source: Path) -> list[str]:
    """Human-readable differences between vendor/ and the source checkout; [] when they agree."""
    problems = []
    entries = parse_manifest(MANIFEST.read_text())
    wanted = expand(source, entries)
    have = vendored_files()
    for rel in wanted:
        origin, copy = source / rel, VENDOR / rel
        if not origin.is_file():
            problems.append(f"{rel}: listed in the manifest but missing in the source")
        elif not copy.is_file():
            problems.append(f"{rel}: not vendored yet")
        elif digest(origin) != digest(copy):
            problems.append(f"{rel}: vendored copy differs from the source's")
        elif os.access(origin, os.X_OK) != os.access(copy, os.X_OK):
            problems.append(f"{rel}: executable bit differs")
    for rel in have:
        if rel not in wanted:
            problems.append(f"{rel}: vendored but no longer selected by the manifest")
    return problems


def sync(source: Path) -> int:
    entries = parse_manifest(MANIFEST.read_text())
    wanted = expand(source, entries)
    missing = [rel for rel in wanted if not (source / rel).is_file()]
    if missing:
        print(f"error: {len(missing)} file(s) are missing in the source:", file=sys.stderr)
        for rel in missing:
            print(rel, file=sys.stderr)
        return 2
    VENDOR.mkdir(parents=True, exist_ok=True)
    for rel in vendored_files():
        if rel not in wanted:
            (VENDOR / rel).unlink()
    for rel in wanted:
        target = VENDOR / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / rel, target)
    for directory in sorted((p for p in VENDOR.rglob("*") if p.is_dir()), reverse=True):
        if not any(directory.iterdir()):
            directory.rmdir()
    RECORD.write_text("".join(f"{digest(VENDOR / rel)}  {rel}\n" for rel in wanted))
    print(f"vendored {len(wanted)} files")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", type=Path, default=None, help="the source tree (default: $OMP_MEMORY_SOURCE_DIR)")
    ap.add_argument("--check", action="store_true", help="write nothing; exit 1 when vendor/ differs from the source")
    args = ap.parse_args(argv)
    source = (args.source or default_source() or Path()).expanduser()
    if args.source is None and default_source() is None:
        print("error: no source checkout given (set OMP_MEMORY_SOURCE_DIR or pass --source DIR)", file=sys.stderr)
        return 2
    if not source.is_dir():
        print("error: source checkout not found (set OMP_MEMORY_SOURCE_DIR or pass --source)", file=sys.stderr)
        return 2
    if args.check:
        problems = compare(source)
        for line in problems:
            print(line)
        if problems:
            print(f"{len(problems)} difference(s): run scripts/sync-vendor.sh")
            return 1
        print("vendor/ matches the source")
        return 0
    return sync(source)


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    sys.exit(main(sys.argv[1:]))
