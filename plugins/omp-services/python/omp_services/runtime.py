"""The installed runtime: a private copy of this tool's code, the wrapper scripts, the pinned omp, the state file.

Services must not point into omp's plugin cache: `omp plugin upgrade` deletes the old version's directory while a
service may still be running from it. So `init` copies the code (about 200 KB, no dependencies) into
`<data>/releases/<version>-<digest>/` and points `<data>/current` at it. Running `init` again after the plugin was
upgraded adds a release and moves the link; the previous releases stay (the newest few) until the plan prunes them.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import MIN_OMP, MIN_PYTHON, __version__
from .config import Config
from .paths import APP, Paths
from .util import (
    ServicesError,
    dir_problem,
    read_text,
    replace_symlink,
    run_bounded,
    safe_env,
    tree_digest,
    write_file,
)

SOURCE_DIRS = ("python", "bin")
KEEP_RELEASES = 3
STATE_SCHEMA = 1
MARKER_TEXT = "Created by omp-services. `omp-services purge` removes only directories that carry this file.\n"


def plugin_root() -> Path:
    """The directory this code runs from: the plugin tree, or a release directory (same layout)."""
    return Path(__file__).resolve().parents[2]


DIGEST_CHARS = 32
RELEASE_NAME = re.compile(r"\d+\.\d+\.\d+[0-9A-Za-z.+-]*-[0-9a-f]{10,64}")  # 10 hex: releases made before 32 hex ones
WRAPPER_HEADER = "# Written by omp-services."


def release_id(root: Path) -> str:
    return f"{read_version(root)}-{tree_digest(root, SOURCE_DIRS)[:DIGEST_CHARS]}"


RELEASE_MARKER = ".omp-services-release"


def release_marker_text(rid: str) -> str:
    return f"omp-services release {rid}\n"


def verify_release(directory: Path, rid: str) -> bool:
    """True when the release directory's content hashes to the digest its name carries (a name made before the digest was
    lengthened carries fewer characters; they are compared as far as they go)."""
    try:
        suffix = rid.rsplit("-", 1)[1]
        return bool(suffix) and tree_digest(directory, SOURCE_DIRS).startswith(suffix)
    except (OSError, IndexError):
        return False


def is_owned_release(directory: Path) -> bool:
    """A directory under releases/ that this tool created: a real directory (not a link) with a name that looks like a
    release and the marker file install_release writes, naming that same release."""
    if directory.is_symlink() or not directory.is_dir() or not RELEASE_NAME.fullmatch(directory.name):
        return False
    try:
        return read_text(directory / RELEASE_MARKER, 512) == release_marker_text(directory.name)
    except ServicesError:
        return False


def read_version(root: Path) -> str:
    text = (root / "python" / "omp_services" / "__init__.py").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise ServicesError(f"{root}: no version found")


def _plain(text: str) -> bool:
    """No control characters (a path that reaches a unit file or a wrapper must not be able to start a new line)."""
    return all(ord(c) >= 0x20 and ord(c) != 0x7F for c in text)


def sh_quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------------------------------------------------------------
# Releases
# ---------------------------------------------------------------------------------------------------------------------


def _copy_tree(src: Path, dst: Path) -> None:
    dst.mkdir(mode=0o700)
    for entry in sorted(src.iterdir()):
        if entry.name == "__pycache__" or entry.name.endswith(".pyc"):
            continue
        target = dst / entry.name
        if entry.is_symlink():
            raise ServicesError(f"{entry}: a symbolic link inside the code to install")
        if entry.is_dir():
            _copy_tree(entry, target)
        else:
            shutil.copyfile(entry, target)
            os.chmod(target, 0o700 if os.access(entry, os.X_OK) else 0o600)


def install_release(paths: Paths, root: Path) -> str:
    """Copy the code under `root` into releases/<id>. A no-op when that release already exists. Returns the id."""
    rid = release_id(root)
    target = paths.releases_dir / rid
    if os.path.lexists(target):
        if dir_problem(target, forbid=0o022) or not verify_release(target, rid):
            raise ServicesError(
                f"release {rid} exists but its content does not match its name: remove {target} and run init again"
            )
        return rid
    staging = paths.releases_dir / f".{rid}.{os.getpid()}.tmp"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(mode=0o700)
    try:
        for name in SOURCE_DIRS:
            _copy_tree(root / name, staging / name)
        if not verify_release(
            staging, rid
        ):  # the source changed while it was being copied, or the copy is not faithful
            raise ServicesError("the code to install changed while it was being copied; run init again")
        write_file(staging / RELEASE_MARKER, release_marker_text(rid))
        os.rename(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return rid


def current_release(paths: Paths) -> str | None:
    """The id the `current` link points at, only when the link has exactly the form `releases/<id>` that this tool
    writes; any other target (a path elsewhere, a different directory with the same base name) is not a release."""
    try:
        target = os.readlink(paths.current)
    except OSError:
        return None
    head, _, rid = target.partition("/")
    if head != "releases" or "/" in rid or not RELEASE_NAME.fullmatch(rid):
        return None
    return rid


def current_link_problem(paths: Paths) -> str | None:
    """Why `current` is not a usable link to an installed release, or None."""
    if not os.path.lexists(paths.current):
        return "is missing"
    if not paths.current.is_symlink():
        return "is not a symbolic link"
    rid = current_release(paths)
    if rid is None:
        return f"points to {os.readlink(paths.current)!r}, not to releases/<id>"
    return None


def activate_release(paths: Paths, rid: str) -> None:
    target = paths.releases_dir / rid
    if not RELEASE_NAME.fullmatch(rid) or target.is_symlink() or not target.is_dir():
        raise ServicesError(f"release {rid} is not installed")
    if not verify_release(target, rid):  # never point `current` at a release whose content does not match its name
        raise ServicesError(f"release {rid} does not match its name: not activated")
    replace_symlink(paths.current, f"releases/{rid}")


def prune_targets(paths: Paths, keep: set[str]) -> list[str]:
    """The releases a prune would remove: all but the newest KEEP_RELEASES, never one in `keep` (current, previous).
    Only directories this tool created (name and marker) are ever listed: anything else is not ours to delete."""
    if not paths.releases_dir.is_dir():
        return []
    releases = sorted(
        (p for p in paths.releases_dir.iterdir() if is_owned_release(p)),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return sorted(r.name for index, r in enumerate(releases) if index >= KEEP_RELEASES and r.name not in keep)


def remove_releases(paths: Paths, names: list[str], keep: set[str]) -> list[str]:
    """Remove exactly `names` (the list that was planned and approved): each must still be a release this tool created
    and must not have become one in `keep`. Nothing else is looked for. Returns the ones removed."""
    removed: list[str] = []
    for name in names:
        target = paths.releases_dir / name
        if name in keep or not RELEASE_NAME.fullmatch(name) or not is_owned_release(target):
            continue
        shutil.rmtree(target, ignore_errors=True)
        removed.append(name)
    return removed


# ---------------------------------------------------------------------------------------------------------------------
# Interpreters
# ---------------------------------------------------------------------------------------------------------------------


def python_version(path: str) -> tuple[int, int, int] | None:
    """The version of the interpreter at `path`, or None when it cannot be run."""
    if path == sys.executable:
        return sys.version_info[:3]
    ran = run_bounded([path, "-c", "import sys; print(*sys.version_info[:3])"], 10.0, env=safe_env())
    try:
        major, minor, micro = (int(x) for x in ran.stdout.split())
    except ValueError:
        return None
    return (major, minor, micro) if ran.ok else None


def choose_python(cfg: Config, state: Mapping[str, Any]) -> str:
    """The Python the services run: configured, else the one pinned before, else the one running now. It must be an
    absolute path to an interpreter of 3.10 or newer; an older one is refused with the reason."""
    for candidate in (cfg.python_path, state.get("python"), sys.executable):
        if candidate and os.path.isabs(candidate) and os.access(candidate, os.X_OK):
            version = python_version(candidate)
            if version is None or version[:2] < MIN_PYTHON:
                if candidate == cfg.python_path:
                    shown = ".".join(map(str, version)) if version else "not runnable"
                    raise ServicesError(
                        f"python.path {candidate} is {shown}; Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer is required"
                    )
                continue
            return candidate
    raise ServicesError("no Python 3.10 or newer is available")


# ---------------------------------------------------------------------------------------------------------------------
# omp
# ---------------------------------------------------------------------------------------------------------------------


def find_omp(env: Mapping[str, str], home: Path) -> str | None:
    """OMP_BIN, then `omp` on PATH, then ~/.local/bin/omp: the first that is an executable file."""
    candidates = [env.get("OMP_BIN")]
    for directory in env.get("PATH", "").split(os.pathsep):
        if directory and os.path.isabs(directory):
            candidates.append(os.path.join(directory, "omp"))
    candidates.append(str(home / ".local" / "bin" / "omp"))
    for candidate in candidates:
        if candidate and not _plain(candidate):
            continue
        if candidate and os.path.isabs(candidate) and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return os.path.normpath(candidate)
    return None


def omp_version(path: str) -> tuple[int, int, int] | None:
    """The version `omp --version` reports ('omp/18.18.0'), run without BUN_BE_BUN so it answers as omp."""
    ran = run_bounded([path, "--version"], 20.0, env=safe_env())
    if not ran.ok:
        return None
    text = ran.stdout.strip().split("/")[-1]
    try:
        parts = tuple(int(p) for p in text.split(".")[:3])
    except ValueError:
        return None
    return parts if len(parts) == 3 else None


def choose_omp(
    cfg: Config, state: Mapping[str, Any], env: Mapping[str, str], home: Path, *, repin: bool = False
) -> str | None:
    """The omp the services run: configured, else the one pinned before (while it still works), else found now."""
    if cfg.omp_path:
        return cfg.omp_path
    pinned = (state.get("omp") or {}).get("path")
    if pinned and not repin and os.access(pinned, os.X_OK):
        return str(pinned)
    return find_omp(env, home)


def at_least(version: tuple[int, ...], minimum: tuple[int, ...] = MIN_OMP) -> bool:
    return version >= minimum


# ---------------------------------------------------------------------------------------------------------------------
# Wrappers
# ---------------------------------------------------------------------------------------------------------------------


def wrapper_texts(paths: Paths, python_path: str, omp_path: str | None) -> dict[str, str]:
    """The scripts in <data>/bin. `omp-services` pins our code and our Python; `omp` pins the omp binary and clears
    BUN_BE_BUN (a child omp must behave as omp); `bun` is the embedded Bun of that omp, for build scripts that call
    `bun`."""
    head = "#!/bin/sh\n# Written by omp-services. Changes are overwritten by `omp-services init`.\n"
    scripts = {
        APP: f'{head}exec {sh_quote(python_path)} {sh_quote(str(paths.current / "python" / "omp_services_main.py"))} "$@"\n'
    }
    if omp_path:
        scripts["omp"] = f'{head}unset BUN_BE_BUN\nexec {sh_quote(omp_path)} "$@"\n'
        scripts["bun"] = f'{head}BUN_BE_BUN=1\nexport BUN_BE_BUN\nexec {sh_quote(omp_path)} "$@"\n'
    return scripts


def write_wrapper(path: Path, text: str) -> None:
    write_file(path, text, 0o700)


# ---------------------------------------------------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------------------------------------------------


def empty_state() -> dict[str, Any]:
    return {"schemaVersion": STATE_SCHEMA, "release": None, "previousRelease": None, "units": {}}


def _validate_state(data: object, where: Path) -> dict[str, Any]:
    """The state file chooses interpreters and units to act on, so its fields are checked, not just its shape."""
    if (
        not isinstance(data, dict)
        or data.get("schemaVersion") != STATE_SCHEMA
        or not isinstance(data.get("units"), dict)
    ):
        raise ServicesError(f"{where} (state) has an unexpected shape; move it away and run init again")
    problems = []
    value = data.get("python")
    if value is not None and not (isinstance(value, str) and os.path.isabs(value)):
        problems.append("python")
    omp = data.get("omp")
    if omp is not None:
        path = omp.get("path") if isinstance(omp, dict) else False
        if path is False or (path is not None and not (isinstance(path, str) and os.path.isabs(path))):
            problems.append("omp.path")
    for key in ("release", "previousRelease"):
        value = data.get(key)
        if value is not None and not (isinstance(value, str) and RELEASE_NAME.fullmatch(value)):
            problems.append(key)
    for text in (
        data.get("python"),
        (data.get("omp") or {}).get("path") if isinstance(data.get("omp"), dict) else None,
    ):
        if isinstance(text, str) and not _plain(text):
            problems.append("control characters in a path")
    for label, info in data["units"].items():
        if (
            not isinstance(label, str)
            or not isinstance(info, dict)
            or not isinstance(info.get("sha256"), str)
            or not isinstance(info.get("protected"), bool)  # missing is not False: a damaged record is refused
        ):
            problems.append(f"units.{str(label)[:40]}")
    if problems:
        raise ServicesError(
            f"{where} (state) has invalid fields ({', '.join(problems)}); move it away and run init again"
        )
    return data


def load_state(paths: Paths) -> dict[str, Any]:
    """The state file, or a fresh state when there is none. A damaged file raises: guessing could lose track of units."""
    if not os.path.lexists(paths.state_file):
        return empty_state()
    # exists() follows links: a dangling link would look like "no state", which a caller may read as a fresh machine
    trouble = dir_problem(paths.data_dir, forbid=0o022)
    if trouble:
        raise ServicesError(f"{paths.data_dir} {trouble}: its state is not used")
    try:
        data = json.loads(read_text(paths.state_file, forbid=0o022))
    except ValueError as exc:
        raise ServicesError(f"{paths.state_file} is damaged ({exc}); move it away and run init again") from exc
    return _validate_state(data, paths.state_file)


def save_state(paths: Paths, state: dict[str, Any]) -> None:
    state["updatedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    write_file(paths.state_file, json.dumps(state, indent=1, sort_keys=True) + "\n")


def write_marker(directory: Path) -> None:
    write_file(directory / ".omp-services-dir", MARKER_TEXT)


def has_marker(directory: Path) -> bool:
    marker = directory / ".omp-services-dir"
    try:
        st = os.lstat(marker)
        return stat.S_ISREG(st.st_mode) and st.st_uid == os.geteuid() and read_text(marker, 1024) == MARKER_TEXT
    except (OSError, ServicesError):
        return False


def version_line() -> str:
    return f"omp-services {__version__}"
