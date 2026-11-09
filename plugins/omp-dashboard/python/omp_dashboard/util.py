"""Careful file, process and text helpers shared by every module.

Rules kept here so the other modules cannot forget them: private files are created with an exclusive, no-follow open
and replaced atomically; a file that matters (configuration, a secret) is read through ONE open descriptor whose type,
owner, mode and size are checked on the descriptor, and the path to it is walked afterwards (trustwalk.py); child
processes run without a shell, with a small environment, with a hard deadline, in their own process group, and with
their output capped.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import selectors
import signal
import socket
import stat
import subprocess
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .trustwalk import TrustError, check_path, others_can_write, private_acl_problem

NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
NONBLOCK = getattr(os, "O_NONBLOCK", 0)
CLOEXEC = getattr(os, "O_CLOEXEC", 0)
MAX_TEXT_BYTES = 1024 * 1024


class DashboardError(Exception):
    """An error whose message is safe to show to the user (it never carries a secret)."""


# ---------------------------------------------------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------------------------------------------------


def printable(text: object, limit: int | None = None) -> str:
    """`text` without control characters (C0, DEL, C1) and without line or paragraph separators, so that a stored label
    or an outside value cannot drive a terminal or break a log line."""
    kept = "".join(
        c
        for c in str(text if text is not None else "")
        if not (ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F or c in "\u2028\u2029" or unicodedata.category(c) == "Cf")
    )
    return kept if limit is None else kept[:limit]


def one_line(text: str, limit: int = 200) -> str:
    """Collapse whitespace and control characters, then cut: for putting outside text into one log or report line."""
    cleaned = "".join(ch if ch.isprintable() else " " for ch in text)
    cleaned = " ".join(cleaned.split())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "…"


# ---------------------------------------------------------------------------------------------------------------------
# Files and directories
# ---------------------------------------------------------------------------------------------------------------------


class Deadline:
    """One overall deadline for an HTTP exchange. A timer shuts down the socket that was actually connected (kept here, not read from
    the `http.client` connection, which lets go of its socket when the answer says `Connection: close`), so a peer that sends headers
    at once and then the body a byte at a time, in any framing, is cut at the deadline. After it, `expired` is true: a read that
    ended because of it looks like the end of an answer that has no length, and the caller must check `expired` and treat it as a failure.
    (Name resolution is outside it: the system resolver has its own limits.)"""

    def __init__(self, seconds: float):
        self.end = time.monotonic() + seconds
        self.expired = False
        self._sock: Any = None
        self._lock = threading.Lock()
        self._timer = threading.Timer(seconds, self._fire)
        self._timer.daemon = True
        self._timer.start()

    def remaining(self) -> float:
        return max(0.001, self.end - time.monotonic())

    def _cut(self) -> None:
        with contextlib.suppress(OSError, AttributeError):
            self._sock.shutdown(socket.SHUT_RDWR)

    def _fire(self) -> None:
        with self._lock:
            self.expired = True
            self._cut()

    def watch(self, sock: Any) -> None:
        with self._lock:
            self._sock = sock
            if self.expired:
                self._cut()

    def check(self) -> None:
        if self.expired:
            raise TimeoutError("the overall deadline passed")

    def cancel(self) -> None:
        self._timer.cancel()


def connect_bounded(conn: Any, host: str, port: int, deadline: Deadline, tls_context: Any = None) -> None:
    """Connect `conn` (an http.client connection) to host:port under `deadline`, TLS handshake included when `tls_context` is given."""
    raw = socket.create_connection((host, port), timeout=deadline.remaining())
    try:
        if tls_context is None:
            deadline.watch(raw)
            raw.settimeout(deadline.remaining())
            conn.sock = raw
        else:
            wrapped = tls_context.wrap_socket(raw, server_hostname=host, do_handshake_on_connect=False)
            deadline.watch(wrapped)
            wrapped.settimeout(deadline.remaining())
            wrapped.do_handshake()
            conn.sock = wrapped
        deadline.check()
    except BaseException:
        raw.close()
        raise


def trust_ancestors(directory: Path) -> None:
    """Refuse unless every existing directory on the way to `directory` is one nobody else can replace (trustwalk). Called
    before anything is created, changed or written below it."""
    try:
        check_path(directory)
    except TrustError as exc:
        raise DashboardError(str(exc)) from None


def dir_problem(path: Path, *, forbid: int = 0o077) -> str | None:
    """Why `path` is not an acceptable private directory, or None. Never creates or changes anything."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return "does not exist"
    except OSError as exc:
        return f"cannot be inspected ({exc.strerror})"
    if stat.S_ISLNK(st.st_mode):
        return "is a symbolic link"
    if not stat.S_ISDIR(st.st_mode):
        return "is not a directory"
    if st.st_uid != os.geteuid():
        return "is owned by another user"
    if st.st_mode & forbid:
        return f"has mode {stat.S_IMODE(st.st_mode):04o}, expected {0o777 & ~forbid:04o} or stricter"
    return None


def _make_missing_parents(directory: Path) -> None:
    """Create the directories above a new private directory that do not exist yet, each with mode 0700 (Path.mkdir(parents=True) would give
    them the default mode, which a umask of 002 makes group-writable)."""
    missing: list[Path] = []
    current = directory
    while True:
        try:
            os.lstat(current)
            break
        except FileNotFoundError:
            missing.append(current)
            current = current.parent
    for each in reversed(missing):
        with contextlib.suppress(FileExistsError):
            os.mkdir(each, 0o700)


def ensure_private_dir(path: Path, *, refuse_open: bool = False, tighten: bool = True) -> bool:
    """Make `path` a real directory of this user with mode 0700. Returns True when something was created or changed.

    The checks and the mode change go through one open directory handle, so swapping the path for a symlink in between
    cannot make us change another directory. Only the final component is created with mode 0700 (whatever the umask:
    the mode is applied again through the handle); parents that have to be created get mode 0700 too, and parents that exist are never changed. With
    `refuse_open` a directory that exists and is open to others is refused instead of tightened (a directory that
    holds a key somebody else wrote). With `tighten=False` (another tool's drop-in directory) an existing directory is
    never chmod-ed, and is refused when group or others can write to it.
    """
    trust_ancestors(path.parent)
    changed = False
    try:
        os.lstat(path)
        existed = True
    except FileNotFoundError:
        existed = False
        _make_missing_parents(path.parent)
        os.mkdir(path, 0o700)
        changed = True
        _private_acl(path)  # a new directory can inherit ACL entries from its parent
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | NOFOLLOW | CLOEXEC
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise DashboardError(f"{path}: not a usable directory ({exc.strerror}); is it a symbolic link?") from exc
    try:
        st = os.fstat(fd)
        if st.st_uid != os.geteuid():
            raise DashboardError(f"{path}: owned by another user")
        if not tighten and existed:
            if others_can_write(st):
                raise DashboardError(f"{path}: is writable by group or others")
        elif stat.S_IMODE(st.st_mode) != 0o700:
            if existed and refuse_open:
                raise DashboardError(f"{path}: can be read or written by group or others; it must be mode 700")
            os.fchmod(fd, 0o700)
            changed = True
    finally:
        os.close(fd)
    return changed


def read_fd_bounded(fd: int, limit: int) -> bytes | None:
    """Everything readable from `fd`, or None when there is more than `limit` bytes (a file can grow after it was
    measured). The bound is in bytes."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            return None
        chunks.append(chunk)


def read_trusted_file(path: Path, *, limit: int = MAX_TEXT_BYTES, secret: bool = False) -> bytes:
    """The bytes of a file that must be trusted: ours, not writable by anyone else (not readable either when `secret`),
    in a chain of directories nobody else can replace (trustwalk.check_path).

    The file is opened ONCE, without blocking (a FIFO named like the file cannot stall us), and everything is read from
    that descriptor; type, owner, mode and size are checked on the descriptor, and the path is walked after the open,
    so a file swapped in afterwards is never the one that was read. Symbolic links are followed as the kernel does, and
    the walk refuses a link that is not yours or root's. Raises DashboardError; `FileNotFoundError` is raised as such
    so a caller can treat "absent" differently from "untrusted"."""
    try:
        fd = os.open(path, os.O_RDONLY | NONBLOCK | CLOEXEC)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise DashboardError(f"{path}: cannot be read ({exc.strerror})") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise DashboardError(f"{path}: not a regular file")
        if st.st_uid != os.geteuid():
            raise DashboardError(f"{path}: is owned by another user")
        if (st.st_mode & 0o077) if secret else others_can_write(st):
            fix = "chmod 600" if secret else "chmod go-w"
            raise DashboardError(
                f"{path}: has mode {stat.S_IMODE(st.st_mode):04o}, which others can {'read or ' if secret else ''}write; "
                f"run {fix} on it"
            )
        if st.st_size > limit:
            raise DashboardError(f"{path}: larger than {limit} bytes")
        try:
            check_path(path, secret=secret)
        except TrustError as exc:
            raise DashboardError(str(exc)) from None
        data = read_fd_bounded(fd, limit)
    finally:
        os.close(fd)
    if data is None:
        raise DashboardError(f"{path}: larger than {limit} bytes")
    return data


def read_text(path: Path, *, limit: int = MAX_TEXT_BYTES, secret: bool = False) -> str:
    data = read_trusted_file(path, limit=limit, secret=secret)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DashboardError(f"{path}: not valid UTF-8") from exc


def _private_acl(path: Path) -> None:
    """Refuse unless `path` has no ACL entry for another principal (trustwalk, secret=True)."""
    try:
        if path.is_dir() and not path.is_symlink():
            problem = private_acl_problem(path)  # check_path walks a directory as a directory, not as the secret leaf
            if problem:
                raise TrustError(problem)
        else:
            check_path(path, secret=True)
    except TrustError as exc:
        raise DashboardError(str(exc)) from None


def write_file(path: Path, content: str | bytes, mode: int = 0o600) -> None:
    """Write atomically: an exclusive no-follow temporary file in the same directory, fsync, rename over the target."""
    data = content.encode("utf-8") if isinstance(content, str) else content
    trust_ancestors(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW | CLOEXEC
    fd = os.open(tmp, flags, mode)
    try:
        try:
            os.fchmod(fd, mode)
            if not mode & 0o077:
                # Before the secret goes in: an inherited ACL entry can let another principal read what is written.
                _private_acl(tmp)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
            if not mode & 0o077:
                _private_acl(tmp)  # and again once written
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


class FileLock:
    """An exclusive advisory lock on a file, held for the length of a `with` block. Not re-entrant."""

    def __init__(self, path: Path, wait: float = 0.0, what: str = "another command"):
        self.path = path
        self.wait = wait
        self.what = what
        self.fd: int | None = None

    def __enter__(self) -> FileLock:
        flags = os.O_CREAT | os.O_RDWR | NOFOLLOW | CLOEXEC | NONBLOCK
        try:
            fd = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise DashboardError(f"{self.path}: cannot be opened for locking ({exc.strerror})") from exc
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
            os.close(fd)
            raise DashboardError(f"{self.path}: is not a private regular file of yours: refusing to use it as a lock")
        deadline = time.monotonic() + self.wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise DashboardError(f"{self.what} holds the lock on {self.path}; try again in a moment") from None
                time.sleep(0.02)
        self.fd = fd
        return self

    def __exit__(self, *exc: object) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


# ---------------------------------------------------------------------------------------------------------------------
# Child processes
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RunResult:
    returncode: int | None  # None: it did not start, or it was killed at the deadline
    stdout: str
    stderr: str
    timed_out: bool = False
    error: str | None = None  # why it did not start (the error class and errno text, no argv)
    truncated: bool = False  # more than `max_output` bytes of stdout were produced

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def safe_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """A small, fixed environment for child processes: nothing of ours leaks into a command we run."""
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": os.environ.get("HOME", "/"),
        "LANG": "C",
        "LC_ALL": "C",
    }
    if extra:
        env.update(extra)
    return env


def run_bounded(
    argv: list[str],
    timeout: float,
    *,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    max_output: int = 64 * 1024,
) -> RunResult:
    """Run `argv` (no shell) and wait at most `timeout` seconds. A child still running then is killed with its whole
    process group. At most `max_output` bytes of each stream are kept; the rest is read and thrown away."""
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env if env is not None else safe_env(),
            cwd=cwd,
            start_new_session=True,
            close_fds=True,
        )
    except (OSError, ValueError) as exc:
        return RunResult(None, "", "", error=f"{type(exc).__name__}: {getattr(exc, 'strerror', None) or 'failed'}")
    deadline = time.monotonic() + timeout
    chunks: dict[str, bytearray] = {"out": bytearray(), "err": bytearray()}
    selector = selectors.DefaultSelector()
    assert proc.stdout is not None and proc.stderr is not None
    selector.register(proc.stdout, selectors.EVENT_READ, "out")
    selector.register(proc.stderr, selectors.EVENT_READ, "err")
    timed_out = False
    truncated = False
    exited_at: float | None = None
    try:
        while True:
            now = time.monotonic()
            running = proc.poll() is None
            if not selector.get_map() and not running:
                break
            if now >= deadline:
                timed_out = True
                break
            if not running:
                exited_at = exited_at or now
                if now - exited_at > 0.5:
                    break  # the child is gone; a grandchild that keeps the pipes open is not waited for
            if not selector.get_map():
                time.sleep(min(0.05, max(0.0, deadline - now)))
                continue
            for key, _ in selector.select(timeout=min(0.2, max(0.0, deadline - now))):
                data = os.read(key.fileobj.fileno(), 65536)  # type: ignore[union-attr]
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                buf = chunks[key.data]
                room = max_output - len(buf)
                if len(data) > room and key.data == "out":
                    truncated = True
                if room > 0:
                    buf.extend(data[:room])
    finally:
        selector.close()
        # Only at the deadline, and only while the leader has NOT been reaped (poll() reaps it, so `returncode` is the test). After it
        # has been reaped its pid and group may belong to somebody else by now; a process a backend left running (a server it
        # started) is the backend's, not ours to kill, and one that holds our pipes after the leader exited is left alone and closed on.
        # (An unreaped leader: its zombie still holds the pid, so the group is ours.)
        if timed_out and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - SIGKILL cannot be ignored
            pass
        proc.stdout.close()
        proc.stderr.close()
    return RunResult(
        None if timed_out else proc.returncode,
        chunks["out"].decode("utf-8", "replace"),
        chunks["err"].decode("utf-8", "replace"),
        timed_out=timed_out,
        truncated=truncated,
    )
