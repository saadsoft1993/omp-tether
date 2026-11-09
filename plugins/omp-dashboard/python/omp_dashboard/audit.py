"""The audit log: one line for every action that changes something, whatever its outcome.

A line holds the time, the short id of the paired browser, the action, a target (a session id, an instance id, or
nothing) and the outcome. It never holds a session name, a prompt, a project path or a link. The file is created 0600 in
a 0700 directory, opened without following a symbolic link, and moved to `<file>.1` when it passes `max_bytes` (one
previous file is kept), so it cannot grow without bound.
"""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .util import CLOEXEC, NOFOLLOW, NONBLOCK, DashboardError, ensure_private_dir, printable, trust_ancestors

DEFAULT_MAX_BYTES = 1024 * 1024
_lock = threading.Lock()


def audit(
    path: Path,
    action: str,
    target: str | None,
    outcome: str,
    browser: str | None,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    now: float | None = None,
) -> None:
    """Append one line. Raises OSError or DashboardError when it cannot; the caller logs that and moves on."""
    line = json.dumps(
        {
            "time": datetime.fromtimestamp(now if now is not None else time.time(), timezone.utc).isoformat(),
            "browser": printable(browser, 16) if browser else None,
            "action": printable(action, 40),
            "target": printable(target, 80) if target else None,
            "outcome": printable(outcome, 80),
        }
    )
    path = Path(path)
    with _lock:
        if not path.parent.is_dir():  # created 0700; an existing directory (a configured location) is never changed
            ensure_private_dir(path.parent)
        else:
            _walk(path.parent)
        try:
            st = path.lstat()
        except FileNotFoundError:
            st = None
        if st is not None:
            # Before anything is rotated or opened: a FIFO would block the open (and every later caller, which waits on the lock),
            # and a file that is not ours is not ours to move.
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():
                raise DashboardError(f"{path} is not a plain file of yours; the audit line was not written")
            if st.st_size > max_bytes:
                os.replace(path, path.with_name(path.name + ".1"))
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | NOFOLLOW | NONBLOCK | CLOEXEC, 0o600)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():
                raise DashboardError(f"{path} is not a plain file of yours; the audit line was not written")
            if st.st_mode & 0o077:
                os.fchmod(fd, 0o600)
            os.write(fd, (line + "\n").encode())
        finally:
            os.close(fd)


_walked: dict[str, float] = {}


def _walk(directory: Path) -> None:
    """The chain of directories above the log is walked (trustwalk), at most every few seconds per directory."""
    key = str(directory)
    now = time.monotonic()
    if now - _walked.get(key, -1e9) < 5:
        return
    trust_ancestors(directory)
    _walked[key] = now
