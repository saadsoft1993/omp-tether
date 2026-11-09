"""Stop and Rename for live omp sessions, through the omp-session-control extension's Unix socket (session-control/1,
docs/protocol/control-1.md). A session is found by session id and process id in the extension's registry directory.

The directory must be a real 0700 directory of ours under a chain of directories nobody else can replace; each registry
file and its socket must be ours, not symbolic links, with no group or other permissions, and the socket must sit in that
directory. Registry files are read through one descriptor, at most 4 KiB. Everything that talks to a socket has a
deadline, and a reply is at most 64 KiB.
"""

from __future__ import annotations

import json
import os
import re
import socket
import stat
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .trustwalk import TrustError, check_path
from .util import CLOEXEC, NOFOLLOW, NONBLOCK, printable

# The wire protocol versions this client speaks; it picks the highest one a session lists.
SUPPORTED_VERSIONS = (1,)
SOCKET_TIMEOUT_S = 10.0
NO_CONTROL = "restart this session to enable Stop and Rename"
MAX_REGISTRY_BYTES = 4096
MAX_REPLY_BYTES = 64 * 1024
MAX_REPLIES_PER_REQUEST = 16
WALK_TTL_S = 5.0
NONCE = re.compile(r"[0-9a-f]{32}\Z")


class ControlError(Exception):
    """A refusal with the HTTP status the hub answers with."""

    def __init__(self, status: int, message: str, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code or "refused"


def _private(st: os.stat_result, uid: int, kind: int) -> bool:
    return stat.S_IFMT(st.st_mode) == kind and st.st_uid == uid and not st.st_mode & 0o077


def _read_registry(entry: Path, uid: int) -> Any:
    """A registry file's JSON: opened without following links or blocking, a private regular file, at most 4 KiB."""
    fd = os.open(entry, os.O_RDONLY | NOFOLLOW | NONBLOCK | CLOEXEC)
    try:
        if not _private(os.fstat(fd), uid, stat.S_IFREG):
            raise ValueError("not a private regular file")
        data = os.read(fd, MAX_REGISTRY_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_REGISTRY_BYTES:
        raise ValueError("registry too large")
    return json.loads(data)


def negotiate(versions: Any) -> int | None:
    """The highest protocol version both sides speak, or None."""
    if not isinstance(versions, list):
        return None
    common = [v for v in versions if isinstance(v, int) and not isinstance(v, bool) and v in SUPPORTED_VERSIONS]
    return max(common) if common else None


class Conn:
    """One connection to a session's control socket."""

    def __init__(self, reg: dict[str, Any], timeout: float = SOCKET_TIMEOUT_S):
        self.reg = reg
        self.version = reg["_version"]
        self.deadline = time.monotonic() + timeout
        self.counter = 0
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        try:
            self.sock.connect(reg["socket"])
        except OSError:
            self.sock.close()
            raise
        self.buf = b""

    def _recv(self) -> dict[str, Any]:
        """The next reply line as a dict; a line over 64 KiB or anything but a JSON object is a protocol error."""
        while b"\n" not in self.buf:
            if len(self.buf) > MAX_REPLY_BYTES:
                raise ValueError("reply too large")
            left = self.deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError
            self.sock.settimeout(left)
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("the session closed the control socket")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        if len(line) > MAX_REPLY_BYTES:
            raise ValueError("reply too large")
        reply = json.loads(line)
        if not isinstance(reply, dict):
            raise ValueError("reply is not an object")
        return reply

    def request(
        self, op: str, args: dict[str, Any] | None = None, session_id: str | None = None, generation: Any = None
    ) -> dict[str, Any]:
        self.counter += 1
        frame: dict[str, Any] = {"v": self.version, "id": f"d{self.counter}", "nonce": self.reg["nonce"], "op": op}
        if session_id is not None:
            frame["sessionId"] = session_id
        if generation is not None:
            frame["generation"] = generation
        if args is not None:
            frame["args"] = args
        self.sock.sendall((json.dumps(frame) + "\n").encode())
        for _ in range(MAX_REPLIES_PER_REQUEST):
            reply = self._recv()
            if reply.get("id") == frame["id"]:
                return reply
            if reply.get("id") is None and not reply.get("ok"):
                return reply  # closing/disabled notices are addressed to every request
        raise ValueError("too many unrelated replies")

    def close(self) -> None:
        self.sock.close()


class ControlDir:
    """The control directory of omp-session-control, read safely."""

    def __init__(
        self,
        directory: Path,
        uid: int | None = None,
        connect: Callable[[dict[str, Any]], Conn] = Conn,
        walk: Callable[[Path], None] = check_path,
    ):
        self.dir = Path(directory)
        self.uid = os.getuid() if uid is None else uid
        self.connect = connect
        self._walk = walk
        self._walked: tuple[float, str | None] | None = None

    def _ancestry(self) -> str | None:
        now = time.monotonic()
        if self._walked is not None and now - self._walked[0] < WALK_TTL_S:
            return self._walked[1]
        try:
            self._walk(self.dir)
            problem = None
        except TrustError as exc:
            problem = str(exc)
        except OSError as exc:
            problem = f"cannot inspect it ({exc.strerror})"
        self._walked = (now, problem)
        return problem

    def registries(self) -> list[dict[str, Any]]:
        """Every usable registry entry of a live extension socket, safest checks first. Raises ControlError when the
        directory exists but is not private; returns [] when it does not exist (control is off)."""
        try:
            st = self.dir.lstat()
        except FileNotFoundError:
            return []
        except OSError:
            raise ControlError(503, f"{self.dir} cannot be inspected; Stop and Rename stay off", "dir") from None
        if not _private(st, self.uid, stat.S_IFDIR):
            raise ControlError(503, f"{self.dir} is not private to this account; Stop and Rename stay off", "dir")
        problem = self._ancestry()
        if problem:
            raise ControlError(503, f"{self.dir}: {problem}; Stop and Rename stay off", "dir")
        found: list[dict[str, Any]] = []
        try:
            names = sorted(os.listdir(self.dir))
        except OSError:
            return []
        for name in names:
            if not name.endswith(".json") or name.startswith("."):
                continue
            try:
                reg = _read_registry(self.dir / name, self.uid)
                if not isinstance(reg, dict):
                    continue
                version = negotiate(reg.get("versions"))
                nonce, sock_path, pid, session = (
                    reg.get("nonce"),
                    reg.get("socket"),
                    reg.get("pid"),
                    reg.get("sessionId"),
                )
                if (
                    version is None
                    or not isinstance(nonce, str)
                    or not NONCE.match(nonce)
                    or not isinstance(sock_path, str)
                    or not isinstance(pid, int)
                    or isinstance(pid, bool)
                    or not isinstance(session, str)
                ):
                    continue
                sock = Path(sock_path)
                if sock.parent != self.dir or not _private(sock.lstat(), self.uid, stat.S_IFSOCK):
                    continue
            except (OSError, ValueError, KeyError, TypeError):
                continue
            found.append({**reg, "_version": version})
        return found


def has_control(regs: list[dict[str, Any]], session_id: Any, pid: Any) -> bool:
    return bool(session_id and pid and any(r.get("sessionId") == session_id and r.get("pid") == pid for r in regs))


_CODE = re.compile(r"[a-z_]{1,32}\Z")


def _code(value: Any) -> str:
    """A refusal code from the session's extension: a short lowercase word, else the generic `refused`. It reaches the page and the
    audit log, so nothing else the extension sends gets through."""
    return value if isinstance(value, str) and _CODE.match(value) else "refused"


def _clean_result(result: dict[str, Any]) -> dict[str, Any]:
    """Only the fields the hub knows from control/1's replies: aborted (bool), before and name (short text)."""
    out: dict[str, Any] = {}
    if isinstance(result.get("aborted"), bool):
        out["aborted"] = result["aborted"]
    for key in ("before", "name"):
        if result.get(key) is None and key in result:
            out[key] = None  # `before` is null when the session had no name
        elif isinstance(result.get(key), str):
            out[key] = printable(result[key], 200)
    return out


def _refused(reply: dict[str, Any], code: str) -> str:
    message = reply.get("message")
    text = printable(message, 160) if isinstance(message, str) else ""
    return f"the session refused ({code})" + (f": {text}" if text else "")


def control(cdir: ControlDir, session_id: str, pid: Any, op: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run a mutating op on the session's extension; returns its result or raises ControlError.

    Reads the live generation with a snapshot first (and checks the socket answers for this session and process), then
    sends the op pinned to that session and generation; a generation that moved in between is retried once.
    """
    regs = [r for r in cdir.registries() if r.get("sessionId") == session_id and r.get("pid") == pid]
    for reg in regs:
        try:
            conn = cdir.connect(reg)
        except OSError:
            continue  # a registry left behind by a session that crashed
        try:
            snap = conn.request("snapshot")
            if not snap.get("ok"):
                code = _code(snap.get("code"))
                raise ControlError(409, _refused(snap, code), code)
            live = snap.get("result")
            if not isinstance(live, dict) or live.get("sessionId") != session_id or live.get("pid") != pid:
                raise ControlError(409, "that socket now serves another session; refresh the list", "stale_session")
            generation = live.get("generation")
            for attempt in range(2):
                reply = conn.request(op, args, session_id, generation)
                if reply.get("ok"):
                    if not isinstance(reply.get("result"), dict):
                        raise ValueError("result is not an object")
                    return _clean_result(reply["result"])
                code = _code(reply.get("code"))
                detail = reply.get("detail") if isinstance(reply.get("detail"), dict) else {}
                if code == "stale_generation" and attempt == 0 and detail.get("sessionId") == session_id:
                    generation = detail.get("generation")
                    continue
                status = 400 if code == "invalid_args" else 409
                # The reply's own message is for people at a terminal and is not parsed; it is shown shortened.
                raise ControlError(status, _refused(reply, code), code)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ControlError(502, f"the session's control socket failed ({type(exc).__name__})", "socket") from None
        finally:
            conn.close()
    raise ControlError(409, NO_CONTROL, "no_control")
