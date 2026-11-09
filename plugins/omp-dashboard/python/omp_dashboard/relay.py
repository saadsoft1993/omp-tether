"""relay-check/1 (docs/protocol/relay-check-1.md), client side: prove that the program listening on a loopback port is
the omp-private-relay this user started, before any link or pairing token is handed to it.

While the relay is down, another account on the same machine could bind its port; anything that then sends a browser
there would hand that account room links and cookies. So before each link both relay ports must (1) be held only by
processes running as us, where the platform shows who holds them (`unknown` is never `ours`), and (2) answer a fresh
nonce with an HMAC under the key the relay wrote to a 0600 file that other accounts cannot read.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import re
import secrets
import stat
from pathlib import Path
from typing import Any

from .hostplat import Platform, PlatformError
from .trustwalk import TrustError, check_path
from .util import CLOEXEC, NOFOLLOW, NONBLOCK, DashboardError, Deadline, connect_bounded

# Domain separator of the proof: HMAC-SHA256(key, f"{CHECK_PREFIX}:{port}:{nonce}").
CHECK_PREFIX = "omp-private-relay-check/1"
KEY_RE = re.compile(r"[0-9a-f]{64}\Z")
MAC_RE = re.compile(r"[0-9a-f]{64}\Z")
MAX_PROOF_BYTES = 1024
MAX_STATS_BYTES = 65536


class RelayCheckError(DashboardError):
    """The relay could not be proven; the message is safe to show."""


def read_check_key(check_dir: Path) -> bytes:
    """The key file, read only when the directory and the file are private and ours."""
    try:
        st = os.lstat(check_dir)
    except OSError:
        raise RelayCheckError("no check directory (is the relay installed and started?)") from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
        raise RelayCheckError("the relay's check directory is not a private directory of ours")
    try:
        fd = os.open(check_dir / "check.key", os.O_RDONLY | NOFOLLOW | NONBLOCK | CLOEXEC)
    except OSError:
        raise RelayCheckError("no check key (the relay has not started with this directory)") from None
    try:
        key_stat = os.fstat(fd)
        if (
            not stat.S_ISREG(key_stat.st_mode)
            or key_stat.st_uid != os.geteuid()
            or key_stat.st_mode & 0o077
            or key_stat.st_size > 1024
        ):
            raise RelayCheckError("the relay's check key file is not private")
        try:
            check_path(check_dir / "check.key", secret=True)
        except TrustError:
            raise RelayCheckError("the relay's check key is below a directory another account could replace") from None
        text = os.read(fd, 1025).decode("ascii", "replace").strip()
    finally:
        os.close(fd)
    if not KEY_RE.match(text):
        raise RelayCheckError("the relay's check key file is malformed")
    return bytes.fromhex(text)


def _get(port: int, target: str, limit: int, timeout: float) -> tuple[int, bytes | None]:
    """One GET to 127.0.0.1:<port>, no redirect followed, no proxy, the body bounded in bytes. (status, body or None
    when it is larger than `limit`)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    deadline = Deadline(timeout)  # `timeout` is the whole exchange, not one read
    try:
        connect_bounded(conn, "127.0.0.1", port, deadline)
        conn.request("GET", target, headers={"Accept": "application/json"})
        resp = conn.getresponse()
        body = resp.read(limit + 1)
        deadline.check()  # a read cut by the deadline can look like the end of an answer
        return resp.status, (None if len(body) > limit else body)
    finally:
        deadline.cancel()
        conn.close()


def prove_listener(port: int, key: bytes, timeout: float = 3.0) -> None:
    """Challenge the listener on 127.0.0.1:<port> with a fresh nonce; raises RelayCheckError unless it proves itself."""
    nonce = secrets.token_hex(16)
    try:
        status, body = _get(port, f"/__relay_check?nonce={nonce}", MAX_PROOF_BYTES, timeout)
    except (OSError, http.client.HTTPException):
        raise RelayCheckError(f"the listener on port {port} did not answer the relay check") from None
    if status != 200:
        raise RelayCheckError(f"the listener on port {port} answered {status} to the relay check")
    if body is None:
        raise RelayCheckError(f"the listener on port {port} sent an answer that is too large")
    try:
        mac = json.loads(body).get("mac")
    except (ValueError, AttributeError):
        mac = None
    if not isinstance(mac, str) or not MAC_RE.match(mac):
        raise RelayCheckError(f"the listener on port {port} sent a malformed answer")
    want = hmac.new(key, f"{CHECK_PREFIX}:{port}:{nonce}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, want):
        raise RelayCheckError(f"the listener on port {port} is not our relay; links stay closed")


def fetch_stats(port: int, key: bytes, timeout: float = 3.0) -> dict[str, Any]:
    """The relay's counts, believed only because they carry a MAC over exactly those bytes under a fresh nonce."""
    nonce = secrets.token_hex(16)
    try:
        status, body = _get(port, f"/__relay_stats?nonce={nonce}", MAX_STATS_BYTES, timeout)
    except (OSError, http.client.HTTPException):
        raise RelayCheckError("the relay did not answer") from None
    if status != 200 or body is None:
        raise RelayCheckError("the relay's counts could not be read")
    try:
        outer = json.loads(body)
        text, mac = outer["body"], outer["mac"]
    except (ValueError, KeyError, TypeError):
        raise RelayCheckError("the relay's counts are malformed") from None
    if not isinstance(text, str) or not isinstance(mac, str) or not MAC_RE.match(mac):
        raise RelayCheckError("the relay's counts are malformed")
    want = hmac.new(key, f"{CHECK_PREFIX}:stats:{port}:{nonce}:{text}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, want):
        raise RelayCheckError("the relay's counts do not carry its proof; ignoring them")
    try:
        stats = json.loads(text)
    except ValueError:
        raise RelayCheckError("the relay's counts are malformed") from None
    if not isinstance(stats, dict):
        raise RelayCheckError("the relay's counts are malformed")
    return stats


def check_ports(
    ports: tuple[int, int], check_dir: Path, platform: Platform, uid: int | None = None, timeout: float = 3.0
) -> None:
    """Raises RelayCheckError unless both relay listeners are the relay this user started."""
    uid = os.getuid() if uid is None else uid
    key = read_check_key(check_dir)
    for port in ports:
        try:
            uids = platform.listener_uids(port)
        except PlatformError:
            raise RelayCheckError(f"could not check who is listening on port {port}") from None
        if uids != {uid}:
            raise RelayCheckError(
                f"port {port} is not held by our relay (is it running?); links stay closed until it is"
            )
        prove_listener(port, key, timeout)


def health(host_port: int, check_dir: Path, platform: Platform, uid: int | None = None) -> dict[str, Any]:
    """The Relay page's status: proven counts, or why they cannot be shown. Never raises."""
    uid = os.getuid() if uid is None else uid
    try:
        key = read_check_key(check_dir)
        try:
            uids = platform.listener_uids(host_port)
        except PlatformError:
            return {"ok": False, "error": "could not check who is listening on the relay's port"}
        if uids != {uid}:
            return {"ok": False, "error": "the relay is not running (its port is not held by our account)"}
        stats = fetch_stats(host_port, key)
    except RelayCheckError as exc:
        return {"ok": False, "error": str(exc)}
    problems = []
    if not stats.get("staticReady"):
        problems.append("the relay's web client is not installed (run omp-private-relay init)")
    if stats.get("ready") is False:
        problems.append("the relay is not ready to admit browsers (its sign-in check is still starting)")

    def count(name: str) -> int | None:
        value = stats.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    build = stats.get("build")
    tag = build.get("ompTag") if isinstance(build, dict) else None
    return {
        "ok": not problems,
        "error": "; ".join(problems) or None,
        "rooms": count("rooms"),
        "guests": count("guests"),
        "uptime_s": count("uptimeS"),
        "build": tag if isinstance(tag, str) and len(tag) <= 40 else None,
    }
