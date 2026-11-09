"""The folders Start may open a session in: each configured root and every git repository below it.

Nothing outside the configured roots is ever offered, and the roots default to none. Discovery is bounded (depth, number
of folders looked at, number of projects) and never follows a symbolic link into another place.

With `projects.rankBySessions` the list is ordered by recent use. That reads only the names and the modification times of
the folders in omp's session directory (omp names a project's folder after the project's path), never what is in them.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

SKIP_DIRS = {"node_modules", ".git", "venv", ".venv", "dist", "build", "__pycache__"}
MAX_VISITED = 20_000
MAX_PROJECTS = 500


def session_dir_name(path: str | Path, home: Path) -> str:
    """omp names a project's session folder after its path relative to home: ~/a/b -> -a-b."""
    try:
        rel = Path(path).relative_to(home)
    except ValueError:
        rel = Path(str(path).lstrip("/"))
    return "-" + str(rel).replace(os.sep, "-")


def expand_root(root: str, home: Path) -> Path:
    """`~/x` means x in the home directory; everything else must already be absolute (the schema checks both)."""
    return home / root[2:] if root.startswith("~/") else Path(root)


def discover_projects(
    roots: list[str], home: Path, max_depth: int = 3, sessions_dir: Path | None = None
) -> list[dict[str, Any]]:
    """Each root plus every git repo below it, most recently used with omp first when `sessions_dir` is given."""
    found: dict[str, Path] = {}
    visited = 0
    for raw in roots:
        root = expand_root(raw, home)
        if not root.is_dir():
            continue
        found[str(root)] = root
        stack = [(root, 0)]
        while stack and visited < MAX_VISITED and len(found) < MAX_PROJECTS:
            directory, depth = stack.pop()
            if depth >= max_depth:
                continue
            try:
                scan = os.scandir(directory)
            except OSError:
                continue
            with scan:
                # One entry at a time with the bound checked on each: a folder with millions of children costs MAX_VISITED
                # entries, not a list of all of them.
                while visited < MAX_VISITED:
                    try:
                        entry = next(scan, None)
                    except OSError:
                        break
                    if entry is None:
                        break
                    visited += 1
                    try:
                        if (
                            entry.name.startswith(".")
                            or entry.name in SKIP_DIRS
                            or not entry.is_dir(follow_symlinks=False)
                        ):
                            continue
                    except OSError:
                        continue
                    child = directory / entry.name
                    if len(found) < MAX_PROJECTS and os.path.lexists(child / ".git"):
                        found[str(child)] = child
                    stack.append((child, depth + 1))
    last_used: dict[str, float] = {}
    if sessions_dir is not None:
        # Looked up by name for the projects found (at most MAX_PROJECTS), never by listing the session directory, which can be
        # as large as the user's history.
        for folder in found.values():
            name = session_dir_name(folder, home)
            try:
                last_used[name] = os.lstat(sessions_dir / name).st_mtime
            except OSError:
                continue
    projects = []
    for path, folder in found.items():
        used = last_used.get(session_dir_name(folder, home))
        try:
            label = "~/" + str(folder.relative_to(home))
        except ValueError:
            label = path  # a root outside home keeps its absolute path
        projects.append(
            {"path": path, "name": folder.name, "label": label, "last_used": int(used * 1000) if used else None}
        )
    projects.sort(key=lambda p: (-(p["last_used"] or 0), p["label"]))
    return projects
