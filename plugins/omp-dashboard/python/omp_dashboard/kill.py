"""Kill and Force kill for a live omp session.

Kill sends SIGTERM to the session's omp process (the pid `omp collab list` reports) after checking that the pid still
is that session; it never escalates by itself. Force kill (SIGKILL, to the process and everything below it that runs as
you) is offered only for a while after a Kill that did not end the session, and only for the same process.

The checks, in order, every time: the session is still listed by omp; the person typed its exact name or its project
folder; omp reported a pid; the process with that pid belongs to this user, is an `omp` process, and started no later
than the room did (a pid reused by a newer process cannot be the one that opened the room). After the signal, "gone"
means the pid vanished or the process now at that pid started at another time.
"""

from __future__ import annotations

import contextlib
import os
import signal
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .control import ControlError
from .hostplat import Platform, PlatformError, ProcessInfo
from .omp import OmpError

# A process whose start is later than the Collab room's start (plus clock rounding: ps reports whole seconds) cannot be
# the process that opened the room: its pid was reused.
START_TOLERANCE_S = 2.0
TERM_WAIT_S = 10.0
FORCE_WINDOW_S = 600.0  # Force kill is offered for this long after a SIGTERM that did not end the session
COMMAND_NAME = "omp"


def target_problem(host: dict[str, Any], info: ProcessInfo | None, uid: int) -> str | None:
    """None when `info` (the process at host's pid) is still the omp process hosting this session, else why not."""
    if info is None:
        return "that session's process is already gone"
    if info.uid != uid:
        return "that pid belongs to another account; nothing was sent"
    if Path(info.command).name != COMMAND_NAME:
        return "that pid is no longer an omp process; nothing was sent"
    started = host.get("startedAt")
    if (
        not isinstance(started, int | float)
        or isinstance(started, bool)
        or info.start > started / 1000 + START_TOLERANCE_S
    ):
        return "that pid was reused by a newer process; nothing was sent"
    return None


def confirm_matches(host: dict[str, Any], confirm: object) -> bool:
    """The typed confirmation is the session's exact name or its project folder name."""
    if not isinstance(confirm, str) or not confirm.strip():
        return False
    names = {(host.get("sessionName") or "").strip(), Path(host.get("cwd") or "").name}
    return confirm.strip() in names - {""}


class Killer:
    """SIGTERM with identity checks; SIGKILL only on request after a SIGTERM that did not end the session."""

    def __init__(
        self,
        list_hosts: Callable[[], list[dict[str, Any]]],
        platform: Platform,
        send: Callable[[int, int], None] = os.kill,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        uid: int | None = None,
    ):
        self.list_hosts = list_hosts
        self.platform = platform
        self.send = send
        self.sleep = sleep
        self.clock = clock
        self.uid = os.getuid() if uid is None else uid
        self.failed_term: dict[str, tuple[int, float, float]] = {}  # sessionId -> (pid, process start, when)
        self.lock = threading.Lock()

    def _info(self, pid: int) -> ProcessInfo | None:
        try:
            return self.platform.process_info(pid)
        except PlatformError:
            raise ControlError(503, "could not read the process table; nothing was sent", "ps") from None

    def _target(self, session_id: str, confirm: object) -> tuple[int, ProcessInfo]:
        try:
            hosts = self.list_hosts()
        except (OmpError, OSError, ValueError):
            raise ControlError(
                503, "could not list sessions (omp collab list failed); nothing was sent", "list"
            ) from None
        host = next((h for h in hosts if isinstance(h, dict) and h.get("sessionId") == session_id), None)
        if host is None:
            raise ControlError(404, "that session is not running any more; refresh the list", "gone")
        if not confirm_matches(host, confirm):
            raise ControlError(400, "type the session's name (or its project folder) exactly to confirm", "confirm")
        pid = host.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
            raise ControlError(409, "omp did not report that session's process", "no_pid")
        info = self._info(pid)
        problem = target_problem(host, info, self.uid)
        if problem:
            raise ControlError(409, problem, "identity")
        assert info is not None
        return pid, info

    def _gone(self, pid: int, info: ProcessInfo) -> bool:
        now = self._info(pid)
        return now is None or now.start != info.start

    def _wait(self, pid: int, info: ProcessInfo, wait_s: float) -> bool:
        deadline = self.clock() + wait_s
        while self.clock() < deadline:
            if self._gone(pid, info):
                return True
            self.sleep(0.25)
        return self._gone(pid, info)

    def terminate(self, session_id: str, confirm: object, wait_s: float = TERM_WAIT_S) -> dict[str, Any]:
        with self.lock:
            pid, info = self._target(session_id, confirm)
            try:
                self.send(pid, signal.SIGTERM)
            except ProcessLookupError:  # it ended between the check and the signal
                self.failed_term.pop(session_id, None)
                return {"outcome": "exited", "pid": pid}
        if self._wait(pid, info, wait_s):
            self.failed_term.pop(session_id, None)
            return {"outcome": "exited", "pid": pid}
        with self.lock:
            self.failed_term[session_id] = (pid, info.start, self.clock())
        return {"outcome": "still_running", "pid": pid}

    def force(self, session_id: str, confirm: object, wait_s: float = TERM_WAIT_S) -> dict[str, Any]:
        """SIGKILL the omp process and every process below it that runs as us (tool commands, MCP servers).

        The tree is read before the signal (once omp is gone its children are re-parented and cannot be found), with each
        process's start time. Just before a signal is sent the pid is looked at again: one whose start time changed was reused by
        another process and is left alone. Afterwards every signalled process is checked gone; `remaining` counts what is not.
        (A pidfd would close the remaining gap between the check and the signal; it is not portable to macOS, so this is the check.)
        """
        with self.lock:
            pid, info = self._target(session_id, confirm)
            failed = self.failed_term.get(session_id)
            if failed is None or failed[:2] != (pid, info.start) or self.clock() - failed[2] > FORCE_WINDOW_S:
                raise ControlError(
                    409, "Force kill is only offered after Kill did not end the session", "no_failed_term"
                )
            try:
                tree = self.platform.descendants_info(pid, self.uid, info.start)
            except PlatformError:
                raise ControlError(503, "could not read the process table; nothing was sent", "ps") from None
            below = [(p, s) for p, s in tree if p > 1 and p != os.getpid()]
            if self._gone(pid, info):
                # It ended (or its pid was reused) while the table was read. What was listed below `pid` may belong to a replacement
                # process, and a child's own start time says nothing about whose child it is: nothing below it is signalled.
                below = []
                parent_signalled = False
            else:
                parent_signalled = True
                with contextlib.suppress(ProcessLookupError):
                    self.send(pid, signal.SIGKILL)
            signalled: list[tuple[int, float]] = []
            for target, start in below:
                current = self._info(target)
                if current is None or current.start != start or current.uid != self.uid:
                    continue  # gone already, or the pid now names another process
                with contextlib.suppress(ProcessLookupError):
                    self.send(target, signal.SIGKILL)
                signalled.append((target, start))
        remaining, parent_running = self._wait_all(pid, info, signalled, wait_s, parent_signalled)
        if remaining == 0:
            with self.lock:
                self.failed_term.pop(session_id, None)
        return {
            "outcome": "exited" if remaining == 0 else "still_running",
            "pid": pid,
            "descendants": len(signalled),
            "remaining": remaining,
            "parent_running": parent_running,
        }

    def _wait_all(
        self, pid: int, info: ProcessInfo, signalled: list[tuple[int, float]], wait_s: float, parent: bool
    ) -> tuple[int, bool]:
        """(how many of the parent and the signalled descendants are still running, whether the parent is one of them) after up to
        `wait_s` seconds."""

        def alive() -> tuple[int, bool]:
            running = parent and not self._gone(pid, info)
            count = 1 if running else 0
            for target, start in signalled:
                now = self._info(target)
                if now is not None and now.start == start:
                    count += 1
            return count, running

        deadline = self.clock() + wait_s
        left = alive()
        while left[0] and self.clock() < deadline:
            self.sleep(0.25)
            left = alive()
        return left
