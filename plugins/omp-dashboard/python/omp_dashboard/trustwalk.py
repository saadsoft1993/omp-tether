"""Is this path one that only you (or root) can change? The one implementation of that question, used for the
configuration file, the secrets, the pairing store's directory, the web client and every directory the dashboard reads
or writes something that matters from.

A path is walked the way the kernel resolves it: component by component with lstat, following symbolic links (the
target is read losslessly) and `..` (taken from the real directory reached so far). Every directory passed through,
the ones that hold a symbolic link and the ones a link leads into, must belong to you or root and must not be writable
by group or other (a sticky directory such as /tmp is the exception); every symbolic link must belong to you or root.
On top of the mode bits, access control lists are inspected where the platform has them (macOS: `ls -le`; Linux: the
POSIX ACL extended attribute): an ACL entry that lets another principal write to a path (or read it, for a file that
holds a secret) makes the path untrusted, deny entries are harmless, and an ACL that cannot be inspected is never
"trusted". Standard library only.

Out of scope on purpose: root, and processes running as you. Either can change anything you can.
"""

from __future__ import annotations

import os
import pwd
import re
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from subprocess import SubprocessError
from subprocess import run as _run  # bound now: a test that stubs subprocess.run must not break the ACL check

MAX_LINKS = 40
# macOS ACL permission names. A grant of any of these to someone else lets them change or replace the path.
ACL_WRITE = {
    "write",
    "append",
    "delete",
    "delete_child",
    "writeattr",
    "writeextattr",
    "writesecurity",
    "chown",
    "add_file",
    "add_subdirectory",
}
ACL_READ = {"read", "readattr", "readextattr", "readsecurity", "list"}
_ACL_LINE = re.compile(r"^\s*\d+:\s+(\S+)\s+(?:inherited\s+)?(allow|deny)\s+(.*?)\s*$")
_ACL_ANY = re.compile(r"^\s*\d+:")
_ENTRY_LINE = re.compile(r"^[-dlbcpsw?][-rwxsStTl]{9}[@+.: ]?\s")


class TrustError(Exception):
    """The path is not trusted; the message says which part and why."""


def _owner_ok(info: os.stat_result) -> bool:
    return info.st_uid in (os.getuid(), 0)


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read(1024 * 1024)
    except OSError:
        return None


def group_is_private(gid: int, uid: int, read: Callable[[str], str | None] | None = None) -> bool:
    """Whether `gid` is a group whose every member (the primary members from /etc/passwd and the listed members from
    /etc/group) is `uid` or root. A group missing from the file (a directory service) or a member that cannot be resolved
    is not private: the answer cannot be shown, so it is no. This is the rule omp-private-relay uses for its web directory."""
    read = read or _read_text
    passwd, group = read("/etc/passwd"), read("/etc/group")
    if passwd is None or group is None:
        return False
    users: dict[str, int] = {}
    members: set[int] = set()
    for line in passwd.splitlines():
        parts = line.split(":")
        if len(parts) >= 4 and parts[2].isdigit() and parts[3].isdigit():
            users[parts[0]] = int(parts[2])
            if int(parts[3]) == gid:
                members.add(int(parts[2]))
    found = False
    for line in group.splitlines():
        parts = line.split(":")
        if len(parts) >= 4 and parts[2].isdigit() and int(parts[2]) == gid:
            found = True
            for name in filter(None, parts[3].split(",")):
                if name not in users:
                    return False
                members.add(users[name])
    return found and members <= {uid, 0}


def others_can_write(info: os.stat_result) -> bool:
    """A mode that lets somebody else change the path: any other-write bit, or a group-write bit unless the group has no
    member but you (Linux; never accepted on macOS, whose admin and staff groups have members the files do not list).
    A sticky directory is exempt: its entries cannot be renamed by others."""
    if info.st_mode & stat.S_ISVTX and stat.S_ISDIR(info.st_mode):
        return False
    if info.st_mode & 0o002:
        return True
    if info.st_mode & 0o020:
        return sys.platform == "darwin" or not group_is_private(info.st_gid, os.getuid())
    return False


def _directory(info: os.stat_result, current: Path) -> None:
    if not _owner_ok(info):
        raise TrustError(f"{current} belongs to another user")
    if others_can_write(info):
        raise TrustError(
            f"{current} is writable by group or other (a private directory below it could be replaced); "
            f"run chmod go-w {current}"
        )


def _acl_entries_macos(paths: list[str]) -> dict[str, list[tuple[str, str, set[str]]]]:
    """`ls -lde` parsed: path -> [(principal, allow|deny, permissions)]. Raises TrustError when it cannot run."""
    paths = list(dict.fromkeys(paths))  # a path reached twice (through a symlink) is listed once
    if not paths:
        return {}
    if any("\n" in p for p in paths):
        raise TrustError("a path with a newline cannot be checked for ACLs")
    try:
        proc = _run(
            ["/bin/ls", "-lde", "--", *paths],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env={"LC_ALL": "C", "PATH": "/bin:/usr/bin"},
        )
    except (OSError, SubprocessError) as err:
        raise TrustError(f"cannot inspect ACLs ({type(err).__name__})") from None
    if proc.returncode != 0:
        raise TrustError("cannot inspect ACLs (ls failed)")
    found: dict[str, list[tuple[str, str, set[str]]]] = {}
    current: str | None = None
    seen = 0
    for line in proc.stdout.splitlines():
        if _ENTRY_LINE.match(line):  # ls sorts its operands, so the entry is matched by the name it ends with
            current = max((p for p in paths if line.endswith(" " + p)), key=len, default=None)
            if current is None:
                raise TrustError("cannot inspect ACLs (unexpected ls output)")
            seen += 1
            continue
        match = _ACL_LINE.match(line)
        if current is not None and not match and _ACL_ANY.match(line):
            raise TrustError("cannot inspect ACLs (an entry that is not understood)")
        if match and current is not None:
            found.setdefault(current, []).append(
                (match.group(1), match.group(2), {p.strip() for p in match.group(3).split(",") if p.strip()})
            )
    if seen != len(set(paths)):
        raise TrustError("cannot inspect ACLs (unexpected ls output)")
    return found


def _acl_linux(path: str, secret: bool) -> str | None:
    """A problem found in the POSIX ACL extended attribute of `path`, or None."""
    try:
        names = os.listxattr(path, follow_symlinks=False)
    except OSError as err:
        if err.errno in (95, 61, 38):  # ENOTSUP, ENODATA, ENOSYS: no ACLs on this filesystem
            return None
        raise TrustError(f"cannot inspect ACLs of {path} ({err.strerror})") from None
    if "system.posix_acl_access" not in names:
        return None
    try:
        raw = os.getxattr(path, "system.posix_acl_access", follow_symlinks=False)
    except OSError as err:
        raise TrustError(f"cannot read the ACL of {path} ({err.strerror})") from None
    owner = os.lstat(path).st_uid
    for offset in range(4, len(raw) - 7, 8):
        tag = int.from_bytes(raw[offset : offset + 2], "little")
        perm = int.from_bytes(raw[offset + 2 : offset + 4], "little")
        who = int.from_bytes(raw[offset + 4 : offset + 8], "little")
        if tag == 0x02 and who == owner:  # the owner's own named entry
            continue
        if tag in (0x02, 0x08):  # a named user or a named group
            if perm & 0x02:
                return f"{path} has an ACL entry that lets another user or group write to it"
            if secret and perm & 0x04:
                return f"{path} has an ACL entry that lets another user or group read it"
    return None


def _acl_problem(directories: list[str], leaf: str | None, secret: bool) -> str | None:
    if sys.platform == "darwin":
        listing = _acl_entries_macos([*directories, *([leaf] if leaf else [])])
        try:
            me = pwd.getpwuid(os.getuid()).pw_name
        except KeyError:
            me = ""
        for path, entries in listing.items():
            for principal, verdict, perms in entries:
                if verdict != "allow" or principal == f"user:{me}":
                    continue
                bad = perms & ACL_WRITE
                if secret and path == leaf:
                    bad = bad | (perms & ACL_READ)
                if bad:
                    return f"{path} has an ACL entry ({principal} allow {','.join(sorted(bad))})"
        return None
    if sys.platform.startswith("linux"):
        for path in directories:
            problem = _acl_linux(path, False)
            if problem:
                return problem
        if leaf:
            return _acl_linux(leaf, secret)
    return None


def private_acl_problem(path: str | Path) -> str | None:
    """Why `path` (a file or a directory just made) has an ACL entry for another principal that grants read or write, or None."""
    return _acl_problem([], str(path), True)


def check_path(path: str | Path, *, secret: bool = False) -> None:
    """Raise TrustError unless `path` is trusted (see the module docstring). A missing component ends the walk: what
    exists before it has been checked. When the path names an existing non-directory, that file must also be ours or
    root's and not writable by group or other, and with `secret` not readable by them either."""
    text = os.path.expanduser(str(path))
    queue = [part for part in (text if os.path.isabs(text) else os.path.join(os.getcwd(), text)).split("/") if part]
    current = Path("/")
    links = 0
    directories = ["/"]
    leaf: str | None = None
    _directory(os.lstat("/"), current)
    while queue:
        part = queue.pop(0)
        if part == ".":
            continue
        if part == "..":  # `current` is a real directory, so its parent is exactly what the kernel would use
            current = current.parent
            continue
        candidate = current / part
        try:
            info = os.lstat(candidate)
        except FileNotFoundError:
            break
        except OSError as err:
            raise TrustError(f"cannot inspect {candidate} ({err.strerror or type(err).__name__})") from None
        if stat.S_ISLNK(info.st_mode):
            links += 1
            if links > MAX_LINKS:
                raise TrustError(f"too many symbolic links in {path}")
            if not _owner_ok(info):
                raise TrustError(f"the symbolic link {candidate} belongs to another user")
            target = os.readlink(candidate)  # exact: str, no newline trimming
            if target.startswith("/"):
                current = Path("/")
            queue = [p for p in target.split("/") if p] + queue
            continue
        if stat.S_ISDIR(info.st_mode):
            _directory(info, candidate)
            current = candidate
            directories.append(str(candidate))
            continue
        if queue:
            raise TrustError(f"{candidate} is not a directory")
        if not _owner_ok(info):
            raise TrustError(f"{candidate} belongs to another user")
        if others_can_write(info):
            raise TrustError(f"{candidate} is writable by group or other; run chmod go-w {candidate}")
        if secret and info.st_mode & 0o077:
            raise TrustError(f"{candidate} is accessible to group or other; run chmod 600 {candidate}")
        leaf = str(candidate)
    problem = _acl_problem(directories, leaf, secret)
    if problem:
        raise TrustError(problem)
