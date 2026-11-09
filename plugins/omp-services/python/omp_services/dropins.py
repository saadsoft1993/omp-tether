"""Reading the drop-in files other plugins write: `health.d/<plugin>.json` and `services.d/<plugin>.json`.

A drop-in can make this tool run a command or start a service as the user, so each file is held to the same standard
as a secret: a plain file of this user that nobody else can write, in a directory that nobody else can write. A file
that fails any rule is reported with the reason and is **not used**; nothing is silently skipped, and an unusable
health drop-in counts as a failed check, because a monitor that quietly ignores a broken file is worse than none.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import schema
from .util import ServicesError, dir_problem, file_problem, read_text

MAX_DROPIN_BYTES = 64 * 1024
MAX_DROPINS = 64
PLUGIN_STEM = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$")
KINDS = {"health": "health-1", "services": "services-1"}
PROTOCOLS = {"health": "health/1", "services": "services/1"}


@dataclass
class Dropin:
    kind: str
    path: Path
    plugin: str
    data: dict[str, Any] | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.data is not None and not self.errors


def load_one(kind: str, path: Path) -> Dropin:
    """Read and validate one drop-in file. Never raises: problems are collected on the result."""
    stem = path.name[: -len(".json")]
    drop = Dropin(kind=kind, path=path, plugin=stem)
    if not PLUGIN_STEM.fullmatch(stem):
        drop.errors.append(
            "the file name must be <plugin>.json with a plugin name of lowercase letters, digits and hyphens"
        )
        return drop
    problem = file_problem(path, forbid=0o022)
    if problem:
        drop.errors.append(f"the file {problem} (it must be a plain file of yours that others cannot write)")
        return drop
    try:
        parsed = json.loads(read_text(path, MAX_DROPIN_BYTES, forbid=0o022))
    except ServicesError as exc:
        drop.errors.append(str(exc).split(": ", 1)[-1])
        return drop
    except ValueError as exc:
        drop.errors.append(f"not valid JSON ({exc})")
        return drop
    errors = schema.validate(parsed, schema.load_schema(KINDS[kind]))
    if errors:
        drop.errors.extend(errors)
        return drop
    if parsed["plugin"] != stem:
        drop.errors.append(f"$.plugin is {parsed['plugin']!r} but the file is named {stem}.json: they must match")
        return drop
    ids = [c["id"] for c in parsed["checks"]] if kind == "health" else [s["name"] for s in parsed["services"]]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        drop.errors.append(f"duplicate {'check ids' if kind == 'health' else 'service names'}: {', '.join(duplicates)}")
        return drop
    drop.data = parsed
    return drop


def load_standalone(kind: str, path: Path) -> Dropin:
    """A drop-in given by path (`health --file`): held to the same rule as one in the directory, including the
    directory it sits in."""
    problem = dir_problem(path.parent, forbid=0o022)
    if problem:
        return Dropin(kind=kind, path=path, plugin=path.name.removesuffix(".json"), errors=[f"its directory {problem}"])
    return load_one(kind, path)


def load_dir(kind: str, directory: Path) -> tuple[list[Dropin], list[str]]:
    """(drop-ins sorted by plugin name, problems with the directory itself). A missing directory is not a problem."""
    if not directory.exists() and not directory.is_symlink():
        return [], []
    problem = dir_problem(directory, forbid=0o022)
    if problem:
        return [], [f"{directory} {problem}: its drop-ins are not used"]
    names = sorted(p for p in directory.iterdir() if p.name.endswith(".json") and not p.name.startswith("."))
    problems: list[str] = []
    if len(names) > MAX_DROPINS:
        problems.append(f"{directory} holds more than {MAX_DROPINS} drop-ins: only the first {MAX_DROPINS} are read")
        names = names[:MAX_DROPINS]
    return [load_one(kind, p) for p in names], problems
