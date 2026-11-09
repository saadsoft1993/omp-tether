"""What differs between macOS and Linux: who owns a listening TCP port, and the process table.

    macOS   `lsof` for listeners; `ps -o uid=,lstart=,comm=` (times parsed as UTC) for a process, `ps -A` for the tree
    Linux   /proc/net/tcp and /proc/net/tcp6 for listeners (`ss` when those cannot be read);
            /proc/<pid>/{stat,status,comm} and /proc/stat for processes, so no `ps` is needed (a minimal container's
            busybox `ps` has no start time, which is why `ps` is not used there)

Anything that cannot be determined raises PlatformError: callers treat "cannot see" as unknown and never as "ours".
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .util import DashboardError, RunResult, run_bounded

LSOF = "/usr/sbin/lsof"
PS = "/bin/ps"
SS_CANDIDATES = ("/usr/bin/ss", "/usr/sbin/ss", "/bin/ss", "/sbin/ss")
TCP_LISTEN = "0A"
PS_ENV = {"LC_ALL": "C", "TZ": "UTC0", "PATH": "/usr/bin:/bin"}  # times in UTC, whatever the hub's own TZ


class PlatformError(DashboardError):
    """The platform tool is missing, failed or answered something unreadable."""


def system(forced: str | None = None) -> str:
    """'darwin' or 'linux'. `forced` is for tests."""
    if forced:
        if forced not in {"darwin", "linux"}:
            raise PlatformError("platform must be darwin or linux")
        return forced
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    raise PlatformError(f"unsupported platform {sys.platform!r}: macOS and Linux only")


def noun(plat: str) -> str:
    """What the page calls this machine: 'Mac' on macOS, 'machine' elsewhere."""
    return "Mac" if plat == "darwin" else "machine"


# ---------------------------------------------------------------------------------------------------------------------
# Parsers (pure)
# ---------------------------------------------------------------------------------------------------------------------


def parse_proc_net_tcp(text: str, port: int) -> set[int]:
    """Owner uids of the listening sockets on `port` in the text of /proc/net/tcp or /proc/net/tcp6."""
    uids: set[int] = set()
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 8 or fields[3] != TCP_LISTEN:
            continue
        local = fields[1].rpartition(":")[2]
        try:
            if int(local, 16) == port:
                uids.add(int(fields[7]))
        except ValueError:
            continue
    return uids


def parse_lsof_uids(text: str) -> set[int]:
    """Owner uids from `lsof -F u` output (lines 'u501')."""
    return {int(m.group(1)) for m in re.finditer(r"^u(\d+)$", text, re.M)}


_SS_UID = re.compile(r"\buid:(\d+)\b")


def parse_ss_uids(text: str, port: int) -> set[int]:
    """Owner uids from `ss -H -l -t -n -e` output for the listeners on `port`."""
    uids: set[int] = set()
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 5 or not fields[0].startswith("LISTEN"):
            continue
        if fields[3].rpartition(":")[2] != str(port):
            continue
        match = _SS_UID.search(line)
        if match:
            uids.add(int(match.group(1)))
    return uids


def parse_ps_info(text: str) -> dict[str, object] | None:
    """{uid, start (epoch s), command} from `ps -o uid=,lstart=,comm=` for one pid, or None."""
    parts = text.split(None, 6)
    if len(parts) < 7:
        return None
    try:
        start = datetime.strptime(" ".join(parts[1:6]), "%a %b %d %H:%M:%S %Y").replace(tzinfo=timezone.utc)
        return {"uid": int(parts[0]), "start": start.timestamp(), "command": parts[6].strip()}
    except ValueError:
        return None


def parse_ps_table(text: str) -> dict[int, tuple[int, int]]:
    """pid -> (ppid, uid) from `ps -A -o pid=,ppid=,uid=`."""
    table: dict[int, tuple[int, int]] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            pid, ppid, uid = map(int, parts)
            table[pid] = (ppid, uid)
    return table


def parse_proc_stat(text: str) -> tuple[int, int] | None:
    """(ppid, starttime in clock ticks) from the text of /proc/<pid>/stat. The command name is between the first '('
    and the LAST ')', and may itself hold spaces and parentheses."""
    end = text.rfind(")")
    if end < 0:
        return None
    fields = text[end + 1 :].split()
    if len(fields) < 20:
        return None
    try:
        return int(fields[1]), int(fields[19])
    except ValueError:
        return None


def parse_proc_status_uid(text: str) -> int | None:
    """The effective uid from the text of /proc/<pid>/status ('Uid:\\treal\\teffective\\tsaved\\tfs')."""
    for line in text.splitlines():
        if line.startswith("Uid:"):
            fields = line.split()
            if len(fields) >= 3 and fields[2].isdigit():
                return int(fields[2])
    return None


# ---------------------------------------------------------------------------------------------------------------------
# The platform
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ProcessInfo:
    uid: int
    start: float  # epoch seconds
    command: str


Runner = Callable[..., RunResult]


class Platform:
    """The platform tools, with the process runner and the /proc root injectable for tests."""

    def __init__(self, kind: str | None = None, proc_root: Path = Path("/proc"), run: Runner = run_bounded):
        self.kind = system(kind)
        self.proc_root = proc_root
        self.run = run

    # --- listeners -----------------------------------------------------------------------------------------------

    def listener_uids(self, port: int, timeout: float = 5.0) -> set[int]:
        """The uids that own a TCP listener on `port` (any address). Empty: nothing listens, or this user may not see
        it (lsof cannot see other accounts' sockets). Raises PlatformError when the tool is missing or fails."""
        port = int(port)
        if self.kind == "linux":
            uids: set[int] = set()
            found = False
            for name in ("tcp", "tcp6"):
                try:
                    uids |= parse_proc_net_tcp((self.proc_root / "net" / name).read_text(), port)
                    found = True
                except OSError:
                    continue
            if found:
                return uids
            return self._ss_uids(port, timeout)
        result = self.run([LSOF, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fu"], timeout)
        if result.error or result.timed_out:
            raise PlatformError("lsof did not answer")
        # lsof exits 1 when nothing matches, with no output: that is 'no listener', not an error.
        if result.returncode not in (0, 1):
            raise PlatformError(f"lsof failed with exit status {result.returncode}")
        return parse_lsof_uids(result.stdout)

    def _ss_uids(self, port: int, timeout: float) -> set[int]:
        for candidate in SS_CANDIDATES:
            if os.access(candidate, os.X_OK):
                result = self.run([candidate, "-H", "-l", "-t", "-n", "-e"], timeout, max_output=1024 * 1024)
                if result.error or result.timed_out or result.returncode != 0:
                    raise PlatformError("ss did not answer")
                return parse_ss_uids(result.stdout, port)
        raise PlatformError("cannot read /proc/net/tcp and there is no ss command")

    # --- processes -----------------------------------------------------------------------------------------------

    def process_info(self, pid: int) -> ProcessInfo | None:
        """{uid, start, command} of a live pid, or None when it is gone. Raises PlatformError when the process table
        cannot be read at all."""
        pid = int(pid)
        if self.kind == "linux":
            return self._linux_info(pid)
        result = self.run([PS, "-o", "uid=,lstart=,comm=", "-p", str(pid)], 5.0, env=PS_ENV)
        if result.error or result.timed_out:
            raise PlatformError("could not read the process table (ps failed)")
        if result.returncode != 0:
            return None
        parsed = parse_ps_info(result.stdout)
        return (
            None if parsed is None else ProcessInfo(int(parsed["uid"]), float(parsed["start"]), str(parsed["command"]))
        )  # type: ignore[arg-type]

    def _linux_info(self, pid: int) -> ProcessInfo | None:
        base = self.proc_root / str(pid)
        try:
            stat_text = (base / "stat").read_text(errors="replace")
        except FileNotFoundError:
            if not (self.proc_root / "stat").exists():
                raise PlatformError("cannot read /proc") from None
            return None
        except OSError as exc:
            if not (self.proc_root / "stat").exists():
                raise PlatformError("cannot read /proc") from exc
            return None  # a process that went away between listing and reading
        parsed = parse_proc_stat(stat_text)
        if parsed is None:
            raise PlatformError("unexpected /proc/<pid>/stat format")
        try:
            uid = parse_proc_status_uid((base / "status").read_text(errors="replace"))
            command = (base / "comm").read_text(errors="replace").strip()
            boot = self._boot_time()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise PlatformError("cannot read /proc") from exc
        if uid is None:
            raise PlatformError("unexpected /proc/<pid>/status format")
        return ProcessInfo(uid, boot + parsed[1] / _clock_ticks(), command)

    def _boot_time(self) -> float:
        for line in (self.proc_root / "stat").read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
        raise PlatformError("no btime in /proc/stat")

    def process_table(self) -> dict[int, tuple[int, int]]:
        """pid -> (ppid, uid) of every process, from one snapshot."""
        if self.kind == "linux":
            table: dict[int, tuple[int, int]] = {}
            try:
                names = os.listdir(self.proc_root)
            except OSError as exc:
                raise PlatformError("cannot read /proc") from exc
            for name in names:
                if not name.isdigit():
                    continue
                base = self.proc_root / name
                try:
                    parsed = parse_proc_stat((base / "stat").read_text(errors="replace"))
                    uid = parse_proc_status_uid((base / "status").read_text(errors="replace"))
                except OSError:
                    continue
                if parsed is not None and uid is not None:
                    table[int(name)] = (parsed[0], uid)
            return table
        result = self.run([PS, "-A", "-o", "pid=,ppid=,uid="], 5.0, env=PS_ENV, max_output=4 * 1024 * 1024)
        if result.error or result.timed_out or result.returncode != 0:
            raise PlatformError("could not read the process table (ps failed)")
        return parse_ps_table(result.stdout)

    def _table_with_start(self) -> dict[int, tuple[int, int, float]]:
        """pid -> (ppid, uid, start in epoch seconds) from one snapshot; a process whose start cannot be read is left out."""
        table: dict[int, tuple[int, int, float]] = {}
        if self.kind == "linux":
            try:
                names, boot = os.listdir(self.proc_root), self._boot_time()
            except OSError as exc:
                raise PlatformError("cannot read /proc") from exc
            for name in names:
                if not name.isdigit():
                    continue
                base = self.proc_root / name
                try:
                    parsed = parse_proc_stat((base / "stat").read_text(errors="replace"))
                    uid = parse_proc_status_uid((base / "status").read_text(errors="replace"))
                except OSError:
                    continue
                if parsed is not None and uid is not None:
                    table[int(name)] = (parsed[0], uid, boot + parsed[1] / _clock_ticks())
            return table
        result = self.run([PS, "-A", "-o", "pid=,ppid=,uid=,lstart="], 5.0, env=PS_ENV, max_output=4 * 1024 * 1024)
        if result.error or result.timed_out or result.returncode != 0:
            raise PlatformError("could not read the process table (ps failed)")
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) != 8 or not all(p.isdigit() for p in parts[:3]):
                continue
            try:
                start = datetime.strptime(" ".join(parts[3:8]), "%a %b %d %H:%M:%S %Y").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            table[int(parts[0])] = (int(parts[1]), int(parts[2]), start.timestamp())
        return table

    def descendants_info(self, pid: int, uid: int, root_start: float | None = None) -> list[tuple[int, float]]:
        """(pid, start) of the processes below `pid` that run as `uid`, from one snapshot. The start time travels with the pid so
        that a caller can check, just before signalling, that the pid still names the process that was listed (a pid is reused
        once its process is gone)."""
        table = self._table_with_start()
        if root_start is not None:
            root = table.get(pid)
            if root is None or abs(root[2] - root_start) > 1.0:
                return []  # `pid` now names another process (or none): the children below it are not the ones that were meant
        children: dict[int, list[tuple[int, int, float]]] = {}
        for child, (parent, owner, start) in table.items():
            children.setdefault(parent, []).append((child, owner, start))
        found: dict[int, float] = {}
        todo = [pid]
        while todo:
            for child, owner, start in children.get(todo.pop(), []):
                if child > 1 and owner == uid and child != pid and child not in found:
                    found[child] = start
                    todo.append(child)
        return list(found.items())

    def descendants(self, pid: int, uid: int) -> list[int]:
        """Pids below `pid` (children, grandchildren, ...) that run as `uid`, from one snapshot. A process of another
        account breaks its branch: nothing below it is returned. pid <= 1 never is."""
        children: dict[int, list[tuple[int, int]]] = {}
        for child, (parent, owner) in self.process_table().items():
            children.setdefault(parent, []).append((child, owner))
        found: list[int] = []
        todo = [pid]
        while todo:
            for child, owner in children.get(todo.pop(), []):
                if child > 1 and owner == uid and child != pid and child not in found:
                    found.append(child)
                    todo.append(child)
        return found

    def ancestors(self, pid: int, limit: int = 64) -> list[int]:
        """The parents of `pid`, nearest first, up to (not including) pid 1."""
        table = self.process_table()
        chain: list[int] = []
        current = pid
        while len(chain) < limit:
            entry = table.get(current)
            if entry is None or entry[0] <= 1 or entry[0] in chain:
                break
            chain.append(entry[0])
            current = entry[0]
        return chain


def _clock_ticks() -> int:
    try:
        return int(os.sysconf("SC_CLK_TCK"))
    except (ValueError, OSError, AttributeError):  # pragma: no cover - every Linux has it
        return 100
