"""Small shared helpers: errors, home-relative display, versions, files that are written atomically, open-file checks."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

EXIT_OK = 0
EXIT_FINDING = 1  # a refusal, a failed check, a failed child command
EXIT_USAGE = 2  # bad arguments or configuration


class CliError(Exception):
    """A refusal or a usage error that ends the command with a one-line message (never a traceback)."""

    def __init__(self, message: str, code: int = EXIT_FINDING):
        super().__init__(message)
        self.message = message
        self.code = code


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def printable(text: str) -> str:
    """`text` with terminal control characters (escape sequences, C0 and C1 controls except tab and newline) replaced by `?`,
    so that a file name or a folder from a bank cannot drive the user's terminal."""
    return _CONTROL.sub("?", text)


def tilde(path: str | Path | None) -> str | None:
    """`~/...` for a path below the home directory, so that output does not carry the account name. A home that is a
    symbolic link (macOS's /tmp, a home on another mount) is recognised both as written in $HOME and by its real path:
    a folder the importer reports by realpath (`/private/tmp/x/work`) is shown under `~` all the same."""
    if path is None:
        return None
    text = printable(str(path))
    home = str(Path.home())
    for prefix in dict.fromkeys((home, os.path.realpath(home))):
        if text == prefix:
            return "~"
        if text.startswith(prefix.rstrip("/") + "/"):
            return "~" + text[len(prefix.rstrip("/")) :]
    return text


def parse_semver(text: str | None) -> tuple[int, int, int] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def version_text(parts: tuple[int, int, int] | None) -> str | None:
    return ".".join(map(str, parts)) if parts else None


def clean_env() -> dict[str, str]:
    """The environment for a child process. BUN_BE_BUN would make the `omp` the importer runs behave as plain Bun."""
    env = dict(os.environ)
    env.pop("BUN_BE_BUN", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


MAX_OWN_FILE = 8 * 1024 * 1024
SQLITE_MAGIC = b"SQLite format 3\0"


STATE_KIND = "omp-memory-state"
CONFIG_KIND = "omp-memory-config"


def _is_state(data: object) -> bool:
    """The safety-state file: its own kind marker, and nothing else's keys."""
    return (
        isinstance(data, dict)
        and data.get("kind") == STATE_KIND
        and isinstance(data.get("schemaVersion"), int)
        and set(data) <= {"kind", "schemaVersion", "backups", "dryRuns"}
    )


def _is_config(data: object) -> bool:
    """The configuration file: its own marker (or none, as a hand-written file), a schema version, and only setting keys."""
    from .config import GROUPS_KEY, KEYS

    return (
        isinstance(data, dict)
        and data.get("kind", CONFIG_KIND) == CONFIG_KIND
        and isinstance(data.get("schemaVersion"), int)
        and set(data) <= {"kind", "schemaVersion", GROUPS_KEY, *KEYS}
    )


def _is_labels(data: object) -> bool:
    """The label store's file: {"version": 1, "labels": {bank: {...}}} and nothing that belongs to another kind."""
    return (
        isinstance(data, dict)
        and isinstance(data.get("labels"), dict)
        and "version" in data
        and not ({"kind", "schemaVersion"} & set(data))
    )


OWN_FILE_KINDS = {
    "state": ("state file", _is_state),
    "config": ("configuration file", _is_config),
    "labels": ("labels file", _is_labels),
}


def path_inside(path: Path, directory: Path) -> bool:
    """True when `path` is `directory` or below it, by real path (a not yet existing tail is resolved as far as it goes)."""
    real_dir = Path(os.path.realpath(directory))
    real = Path(os.path.realpath(path))
    return real == real_dir or real_dir in real.parents


def check_own_file(path: Path, kind: str, banks_dir: Path | None = None) -> None:
    """Refuse, before anything is written, a destination that is not this command line's own kind of file.

    The file must be absent, empty, or a regular file (never a link, a directory or a database) holding the JSON of its
    kind (`state`, `config`: an object with a `schemaVersion`; `labels`: an object of strings), and must not lie inside
    the banks directory. Raises CliError; nothing is created or changed."""
    name, valid = OWN_FILE_KINDS[kind]
    shown = tilde(path)
    if banks_dir is not None and path_inside(path, banks_dir):
        raise CliError(f"the {name} {shown} is inside the banks directory; refusing to write there", EXIT_USAGE)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as err:
        raise CliError(f"cannot read the {name} {shown}: {err.strerror}", EXIT_USAGE) from None
    if not stat.S_ISREG(info.st_mode):
        raise CliError(
            f"{shown} is not an omp-memory {name}: it is a link, a directory or another kind of file", EXIT_USAGE
        )
    if info.st_size == 0:
        return
    if info.st_size > MAX_OWN_FILE:
        raise CliError(f"{shown} is not an omp-memory {name}: it is too large", EXIT_USAGE)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        raw = os.read(fd, MAX_OWN_FILE + 1)
    finally:
        os.close(fd)
    try:
        ok = not raw.startswith(SQLITE_MAGIC) and valid(json.loads(raw.decode("utf-8")))
    except (ValueError, UnicodeDecodeError):
        ok = False
    if not ok:
        raise CliError(
            f"{shown} is not an omp-memory {name} (not the JSON of one); it is left as it is. Remove it yourself or point the setting elsewhere",
            EXIT_USAGE,
        )


def atomic_write_json(path: Path, data: object, kind: str, banks_dir: Path | None = None) -> None:
    """Write JSON to `path` through an exclusive temporary file in the same directory: mode 0600, directory 0700 when
    created. The destination is checked first (`check_own_file`), so a file of another kind is never replaced."""
    check_own_file(path, kind, banks_dir)
    parent = path.parent
    created = not parent.exists()
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if created:
        parent.chmod(0o700)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, ensure_ascii=False, indent="\t") + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _proc_holders(owner: dict[str, str], holders: dict[str, list[int]]) -> dict[str, list[int]] | None:
    """The same answer from /proc/<pid>/fd, for Linux without lsof. Only processes whose fd directory is readable are
    seen (your own, or all when running as root); None where /proc is not available."""
    proc = Path("/proc")
    if not (sys.platform.startswith("linux") and proc.is_dir()):
        return None
    wanted = {os.path.realpath(path): bank for path, bank in owner.items()}
    try:
        entries = [p for p in proc.iterdir() if p.name.isdigit()]
    except OSError:
        return None
    for entry in entries:
        pid = int(entry.name)
        if pid == os.getpid():
            continue
        try:
            fds = list((entry / "fd").iterdir())
        except OSError:
            continue  # another user's process, or one that just ended
        for fd in fds:
            try:
                bank = wanted.get(os.readlink(fd))
            except OSError:
                continue
            if bank is not None and pid not in holders[bank]:
                holders[bank].append(pid)
    return holders


def open_holders(banks: dict[str, list[Path]]) -> dict[str, list[int]] | None:
    """{bank: pids of other processes that have one of its files open}; None when neither lsof nor /proc can tell.

    `banks` maps a bank name to its database files (the database and its -wal and -shm files, when they exist).
    """
    owner: dict[str, str] = {}
    for bank, files in banks.items():
        for file in files:
            if file.exists():
                # lsof and /proc name a file by its real path, so a symlinked directory must not hide a holder
                owner[os.path.realpath(file)] = bank
    holders: dict[str, list[int]] = {bank: [] for bank in banks}
    if not owner:
        return holders
    lsof = shutil.which("lsof") or ("/usr/sbin/lsof" if os.access("/usr/sbin/lsof", os.X_OK) else None)
    if lsof is None:
        return _proc_holders(owner, holders)
    try:
        ran = subprocess.run([lsof, "-F", "pn", "--", *owner], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if ran.returncode > 1:
        return None
    if ran.returncode == 1 and not ran.stdout.strip() and ran.stderr.strip():
        return (
            None  # lsof exits 1 both for "nothing found" and for most errors; an error with no answer is not "nobody"
        )
    pid = None
    for line in ran.stdout.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None and pid != os.getpid():
            bank = owner.get(os.path.realpath(line[1:]))
            if bank is not None and pid not in holders[bank]:
                holders[bank].append(pid)
    return holders
