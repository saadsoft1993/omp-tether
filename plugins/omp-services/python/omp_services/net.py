"""Bounded network helpers for the checks and the heartbeat ping.

What they guarantee: a hard overall deadline (a timer closes the connection if the peer drips bytes), TLS with
certificate and host name verification for https, no proxy from the environment, no redirect ever followed, no cookies,
no request body, a capped response, and errors reduced to a short code that never contains the URL.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit

from .util import ServicesError, call_with_deadline

USER_AGENT = "omp-services"


@dataclass(frozen=True)
class Target:
    scheme: str
    host: str
    port: int
    path: str  # path and query, always starting with '/'


@dataclass(frozen=True)
class HttpResult:
    status: int | None
    body: bytes
    error: str | None  # a short code (see error_code), never the URL


def parse_http_url(url: str, *, https_only: bool = False) -> Target:
    """Split an http(s) URL into its parts. Rejects credentials, fragments and anything else odd; the message never
    repeats the URL."""
    try:
        parts: SplitResult = urlsplit(url)
        port = parts.port
        host = parts.hostname
    except ValueError:
        raise ServicesError("not a valid URL") from None
    allowed = ("https",) if https_only else ("http", "https")
    if parts.scheme not in allowed:
        raise ServicesError("the URL must start with https://" if https_only else "the URL must be http:// or https://")
    if not host:
        raise ServicesError("the URL has no host")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise ServicesError("the URL must not contain a user name or password")
    if parts.fragment or any(ord(c) < 0x21 or ord(c) == 0x7F for c in url):
        raise ServicesError("the URL contains a fragment, a space or a control character")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return Target(parts.scheme, host, port or (443 if parts.scheme == "https" else 80), path)


def is_loopback_host(host: str) -> bool:
    if host.lower() in {"localhost", "localhost."}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def error_code(exc: BaseException) -> str:
    """A short, URL-free code for a network failure."""
    if isinstance(exc, ConnectionRefusedError):
        return "connection-refused"
    if isinstance(exc, TimeoutError | socket.timeout):
        return "timeout"
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "tls-certificate"
    if isinstance(exc, ssl.SSLError):
        return "tls-error"
    if isinstance(exc, socket.gaierror):
        return "dns-error"
    if isinstance(exc, http.client.HTTPException):
        return "protocol-error"
    if isinstance(exc, ConnectionError):
        return "connection-error"
    if isinstance(exc, OSError):
        return "network-error"
    return "error"


def open_socket(host: str, port: int, deadline: float, cancel: threading.Event) -> socket.socket:
    """A connected TCP socket, within one absolute `deadline` and one `cancel` flag for every stage. Name resolution
    itself cannot be interrupted, so its result is checked before any connection is attempted: a lookup that finishes
    after the caller gave up opens nothing. Each address is tried only while there is time and no cancellation."""
    if cancel.is_set() or time.monotonic() >= deadline:
        raise TimeoutError("deadline")
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    last: OSError = OSError("no address")
    for family, kind, proto, _, address in infos:
        remaining = deadline - time.monotonic()
        if cancel.is_set() or remaining <= 0:
            raise TimeoutError("deadline")
        sock = socket.socket(family, kind, proto)
        try:
            sock.settimeout(remaining)
            sock.connect(address)
            if cancel.is_set():
                raise TimeoutError("deadline")
            return sock
        except OSError as exc:
            sock.close()
            last = exc
            if isinstance(exc, TimeoutError):
                raise
    raise last


class _Plain(http.client.HTTPConnection):
    def __init__(self, target: Target, deadline: float, cancel: threading.Event, held: dict):
        super().__init__(target.host, target.port, timeout=max(0.05, deadline - time.monotonic()))
        self._deadline, self._cancel, self._held = deadline, cancel, held

    def connect(self) -> None:
        self.sock = open_socket(self.host, self.port, self._deadline, self._cancel)
        self._held["sock"] = self.sock


class _Secure(http.client.HTTPSConnection):
    def __init__(self, target: Target, deadline: float, cancel: threading.Event, held: dict):
        super().__init__(
            target.host,
            target.port,
            timeout=max(0.05, deadline - time.monotonic()),
            context=ssl.create_default_context(),
        )
        self._deadline, self._cancel, self._held = deadline, cancel, held

    def connect(self) -> None:
        raw = open_socket(self.host, self.port, self._deadline, self._cancel)
        self._held["sock"] = raw
        try:
            raw.settimeout(max(0.05, self._deadline - time.monotonic()))
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)  # TLS is verified (default context)
        except BaseException:
            raw.close()
            raise
        self._held["sock"] = self.sock
        if self._cancel.is_set():
            raise TimeoutError("deadline")


def http_get(target: Target, timeout: float, *, max_body: int = 0) -> HttpResult:
    """GET `target` once. Reads at most `max_body` bytes of the response (0: none) and ignores the rest. One absolute
    deadline covers name resolution, connecting, the TLS handshake, the request and the body; the caller is released at
    that deadline (resolution runs in a thread that cannot be interrupted, so it is abandoned, but it opens no
    connection and sends nothing once the deadline or a cancellation has passed)."""
    held: dict[str, socket.socket | None] = {"sock": None}
    cancel = threading.Event()
    deadline = time.monotonic() + timeout
    finished, value = call_with_deadline(lambda: _http_get(target, deadline, max_body, held, cancel), timeout, cancel)
    if not finished:
        sock = held["sock"]
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        return HttpResult(None, b"", "timeout")
    return value if isinstance(value, HttpResult) else HttpResult(None, b"", "error")


def _http_get(target: Target, deadline: float, max_body: int, held: dict, cancel: threading.Event) -> HttpResult:
    conn: http.client.HTTPConnection = (
        _Secure(target, deadline, cancel, held) if target.scheme == "https" else _Plain(target, deadline, cancel, held)
    )

    def cut_off() -> None:
        cancel.set()
        # close() alone does not wake a thread blocked in recv(); shutdown() does.
        sock = held["sock"] or conn.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        conn.close()

    timer = threading.Timer(max(0.0, deadline - time.monotonic()), cut_off)
    timer.daemon = True
    timer.start()
    try:
        conn.connect()
        if cancel.is_set() or time.monotonic() >= deadline:  # the caller gave up: send nothing
            return HttpResult(None, b"", "timeout")
        conn.request("GET", target.path, headers={"User-Agent": USER_AGENT, "Accept": "*/*", "Connection": "close"})
        response = conn.getresponse()
        body = b""
        if max_body > 0:
            while len(body) < max_body and time.monotonic() < deadline:
                chunk = response.read(min(8192, max_body - len(body)))
                if not chunk:
                    break
                body += chunk
        return HttpResult(response.status, body, None)
    except (OSError, http.client.HTTPException, ValueError) as exc:
        return HttpResult(None, b"", error_code(exc))
    finally:
        timer.cancel()
        conn.close()


def tcp_connect(host: str, port: int, timeout: float, deadline: float | None = None) -> str | None:
    """None when a TCP connection succeeds, else a short error code. `deadline` (absolute, monotonic) is the caller's
    own budget when it has one that is already running; `timeout` otherwise."""
    cancel = threading.Event()
    end = deadline if deadline is not None else time.monotonic() + timeout
    wait = max(0.0, end - time.monotonic())

    def attempt() -> str | None:
        try:
            open_socket(host, port, end, cancel).close()
            return None
        except (OSError, ValueError) as exc:
            return error_code(exc)

    finished, value = call_with_deadline(attempt, wait, cancel)
    if not finished:
        return "timeout"
    return value if value is None or isinstance(value, str) else "error"
