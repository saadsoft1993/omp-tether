"""Careful file and process helpers shared by every module.

Rules kept here so the other modules cannot forget them: private files are created with an exclusive, no-follow open
and replaced atomically; private directories are checked and tightened through an open handle; child processes run
without a shell, with a small environment, with a hard deadline, and with their output capped.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import selectors
import signal
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

MAX_TEXT_BYTES = 1024 * 1024
MARKER_NAME = ".omp-services-dir"


class ServicesError(Exception):
    """An error whose message is safe to show to the user (it never carries a secret)."""


# ---------------------------------------------------------------------------------------------------------------------
# Files and directories
# ---------------------------------------------------------------------------------------------------------------------


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


def ensure_private_dir(path: Path) -> bool:
    """Make `path` a real directory of this user with mode 0700. Returns True when something was created or changed.

    The checks and the mode change go through one open directory handle, so swapping the path for a symlink in between
    cannot make us change another directory. Only the final component is created with mode 0700; parents get the
    normal default mode and are never changed.
    """
    changed = False
    try:
        os.lstat(path)
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.mkdir(path, 0o700)
        changed = True
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ServicesError(f"{path}: not a usable directory ({exc.strerror}); is it a symbolic link?") from exc
    try:
        st = os.fstat(fd)
        if st.st_uid != os.geteuid():
            raise ServicesError(f"{path}: owned by another user")
        if stat.S_IMODE(st.st_mode) != 0o700:
            os.fchmod(fd, 0o700)
            changed = True
    finally:
        os.close(fd)
    return changed


def file_problem(path: Path, *, forbid: int = 0o077) -> str | None:
    """Why `path` is not an acceptable plain file of this user (symlinks are not followed), or None."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return "does not exist"
    except OSError as exc:
        return f"cannot be inspected ({exc.strerror})"
    if stat.S_ISLNK(st.st_mode):
        return "is a symbolic link"
    if not stat.S_ISREG(st.st_mode):
        return "is not a regular file"
    if st.st_uid != os.geteuid():
        return "is owned by another user"
    if st.st_mode & forbid:
        return f"has mode {stat.S_IMODE(st.st_mode):04o}, expected {0o666 & ~forbid:04o} or stricter"
    return None


def read_text(path: Path, max_bytes: int = MAX_TEXT_BYTES, *, forbid: int | None = None) -> str:
    """Read a regular file without following a symlink, refusing anything larger than `max_bytes`.

    With `forbid`, the opened descriptor itself must be owned by this user and carry none of those mode bits, so a
    file swapped in between a check by path and the read cannot be accepted."""
    # O_NONBLOCK: opening a FIFO that nobody writes to would otherwise block here, before the type check below can
    # refuse it. For a regular file the flag changes nothing.
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ServicesError(f"{path}: cannot be read ({exc.strerror})") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ServicesError(f"{path}: not a regular file")
        if forbid is not None:
            if st.st_uid != os.geteuid():
                raise ServicesError(f"{path}: is owned by another user")
            if st.st_mode & forbid:
                raise ServicesError(f"{path}: has mode {stat.S_IMODE(st.st_mode):04o}, which others can write")
        if st.st_size > max_bytes:
            raise ServicesError(f"{path}: larger than {max_bytes} bytes")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(max_bytes + 1)
    finally:
        os.close(fd)
    if len(data) > max_bytes:
        raise ServicesError(f"{path}: larger than {max_bytes} bytes")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ServicesError(f"{path}: not valid UTF-8") from exc


def write_file(path: Path, content: str | bytes, mode: int = 0o600) -> None:
    """Write atomically: an exclusive no-follow temporary file in the same directory, fsync, rename over the target."""
    data = content.encode("utf-8") if isinstance(content, str) else content
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    with contextlib.suppress(FileNotFoundError):
        os.unlink(tmp)
    fd = os.open(tmp, flags, mode)
    try:
        try:
            os.fchmod(fd, mode)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def replace_symlink(link: Path, target: str) -> None:
    """Point `link` at `target` atomically (a temporary link renamed over the old one)."""
    tmp = link.with_name(f".{link.name}.{os.getpid()}.tmp")
    with contextlib.suppress(FileNotFoundError):
        os.unlink(tmp)
    os.symlink(target, tmp)
    try:
        os.replace(tmp, link)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def remove_file(path: Path) -> bool:
    """Unlink a file or symlink (never a directory). True when something was removed."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        return False
    return True


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def tree_digest(root: Path, names: tuple[str, ...]) -> str:
    """A digest of the regular files below the named subdirectories of `root` (paths and contents)."""
    digest = hashlib.sha256()
    for name in names:
        base = root / name
        for path in sorted(p for p in base.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
            digest.update(str(path.relative_to(root)).encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


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


_ACTIVE: set[int] = set()
_ACTIVE_LOCK = threading.Lock()  # serialises writers only; readers (signal handlers) never take it
_WORKERS = threading.BoundedSemaphore(16)  # abandoned workers that are still running; the cap keeps them bounded


def kill_active_groups() -> None:
    """SIGKILL every process group run_bounded has started and not yet finished. Called from signal handlers, so it takes
    no lock (a handler that interrupted the code holding the lock would wait for itself forever): it copies the set,
    retrying if the main thread changes it while it is being copied."""
    pids: list[int] = []
    for _ in range(5):
        try:
            pids = list(_ACTIVE)
            break
        except RuntimeError:  # the set changed size during the copy
            continue
    for pid in pids:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGKILL)


def call_with_deadline(fn, timeout: float, cancel: threading.Event | None = None):
    """Run `fn()` in a daemon thread and wait at most `timeout` seconds. Returns (finished, value); `value` is the
    result, or the exception `fn` raised. For work that cannot be interrupted (name resolution, a directory walk): the
    caller moves on at the deadline and the thread is left to finish on its own, but `cancel` (an Event the work should
    check before it does anything with side effects, such as sending a request) is set at that moment. At most 16 such
    workers exist at once; beyond that the call does not start and reports "not finished"."""
    if not _WORKERS.acquire(blocking=False):
        return False, None
    box: dict[str, object] = {}

    def work() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # reported to the caller, never raised in the thread
            box["value"] = exc
        finally:
            _WORKERS.release()

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        if cancel is not None:
            cancel.set()
        return False, None
    return True, box.get("value")


class FileLock:
    """An exclusive advisory lock on a file, held for the length of a `with` block. Not re-entrant."""

    def __init__(self, path: Path, wait: float = 0.0):
        self.path = path
        self.wait = wait
        self.fd: int | None = None

    def __enter__(self) -> FileLock:
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(self.path, flags | os.O_NONBLOCK, 0o600)
        except OSError as exc:
            raise ServicesError(f"{self.path}: cannot be opened for locking ({exc.strerror})") from exc
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
            os.close(fd)
            raise ServicesError(f"{self.path}: is not a private regular file of yours: refusing to use it as a lock")
        deadline = time.monotonic() + self.wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise ServicesError(
                        "another omp-services command is running; try again when it has finished"
                    ) from None
                time.sleep(0.05)
        self.fd = fd
        return self

    def __exit__(self, *exc: object) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


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
    with _ACTIVE_LOCK:
        _ACTIVE.add(proc.pid)
    deadline = time.monotonic() + timeout
    chunks: dict[str, bytearray] = {"out": bytearray(), "err": bytearray()}
    selector = selectors.DefaultSelector()
    assert proc.stdout is not None and proc.stderr is not None
    selector.register(proc.stdout, selectors.EVENT_READ, "out")
    selector.register(proc.stderr, selectors.EVENT_READ, "err")
    timed_out = False
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
                if len(buf) < max_output:
                    buf.extend(data[: max_output - len(buf)])
    finally:
        selector.close()
        # Always: a command that forks a descendant and exits leaves the group alive, and the descendant must not
        # outlive the check that started it. (A descendant that starts a session of its own with setsid() is outside
        # the group and is not reached: that limit is documented, not claimed away.)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        with _ACTIVE_LOCK:
            _ACTIVE.discard(proc.pid)
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
    )


def one_line(text: str, limit: int = 200) -> str:
    """Collapse whitespace and control characters, then cut: for putting outside text into one log or report line."""
    cleaned = "".join(ch if ch.isprintable() else " " for ch in text)
    cleaned = " ".join(cleaned.split())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "…"
