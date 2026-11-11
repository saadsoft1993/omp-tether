"""pairing-store/1 (docs/protocol/pairing-store-1.md): the writer the dashboard uses to pair and revoke browsers, and
the reader it uses to decide whether a request comes from a paired browser. The same files are read by
omp-private-relay in its `shared` mode, and the conformance suite of that plugin checks this module (see
tests/conformance/adapter.py).

Everything here fails closed: a store that is missing, corrupt, a link, owned by someone else or open to other accounts
means that nobody is paired. A store holds only SHA-256 hashes: reading it lets nobody in.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .trustwalk import TrustError, check_path
from .util import CLOEXEC, NOFOLLOW, NONBLOCK, DashboardError, ensure_private_dir

COOKIE_VALUE_MAX = 512
STORE_MAX_BYTES = 4 * 1024 * 1024
PAIR_TTL_S = 300
LABEL_MAX = 80
DEVICES_FILE = "devices.json"
PENDING_FILE = "pending.json"
LOCK_FILE = ".lock"
LOCK_WAIT_S = 5.0
ANCESTRY_TTL_S = 5.0
HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
ACCESS_KINDS = (
    "local",
    "tailnet",
)  # how a device was paired; an optional field of its record (pairing-store/1 readers ignore it)
PREFIX_RE = re.compile(r"[0-9a-f]{4,64}\Z")

# Machine-readable reasons a store is refused (specification section 4); the last one is this plugin's addition: the
# directory is fine, but a directory above it could be replaced by another account.
REFUSALS = (
    "store-missing",
    "store-symlink",
    "store-foreign-owner",
    "store-open-to-others",
    "store-not-regular",
    "store-too-large",
    "store-invalid-json",
    "store-not-object",
    "store-unreadable",
    "store-untrusted-path",
)


class PairingError(DashboardError):
    """A writer refused to change the store. `code` is one of: unsafe, corrupt, locked, bad-request, ambiguous."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


def cookie_hash(value: str) -> str:
    """The stored form of a cookie value or pairing token: hex(sha256(utf8(value))), lowercase."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def cookie_values(header: str | None, name: str) -> list[str]:
    """Every value the header carries for cookie `name`, in order, ignoring empty and over-long values."""
    if not header:
        return []
    values: list[str] = []
    for part in header.split(";"):
        key, eq, value = part.partition("=")
        if not eq or key.strip() != name:
            continue
        value = value.strip()
        if value and len(value) <= COOKIE_VALUE_MAX:
            values.append(value)
    return values


def clean_label(label: str | None) -> str:
    """Drops control and invisible format characters and caps the length: a label is shown in terminals and web pages."""
    text = "".join(
        " " if unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") else c
        for c in (label if isinstance(label, str) else "")
    ).strip()
    return text[:LABEL_MAX].strip() or "browser"


# ---------------------------------------------------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------------------------------------------------


def _dir_problem(st: os.stat_result) -> str | None:
    if stat.S_ISLNK(st.st_mode):
        return "store-symlink"
    if not stat.S_ISDIR(st.st_mode):
        return "store-not-regular"
    if st.st_uid != os.geteuid():
        return "store-foreign-owner"
    if st.st_mode & 0o077:
        return "store-open-to-others"
    return None


def _file_problem(st: os.stat_result) -> str | None:
    if not stat.S_ISREG(st.st_mode):
        return "store-not-regular"
    if st.st_uid != os.geteuid():
        return "store-foreign-owner"
    if st.st_mode & 0o077:
        return "store-open-to-others"
    return None


@dataclass(frozen=True)
class StoreView:
    hashes: frozenset[str]
    refusal: str | None
    paired_at: dict[str, int] | None = None  # hash -> seconds since the epoch, for the age limit
    access: dict[str, str] | None = None  # hash -> "local" or "tailnet", for the devices that have a recorded way in


_NOBODY: frozenset[str] = frozenset()


class PairingReader:
    """The hashes of the paired browsers, re-read only when the file changes."""

    def __init__(self, directory: Path | str, on_refusal: Callable[[str], None] | None = None):
        self.dir = Path(directory)
        self.on_refusal = on_refusal
        self._cache: tuple[object, StoreView] | None = None

    def _refuse(self, reason: str) -> StoreView:
        if (self._cache is None or self._cache[0] != ("refused", reason)) and self.on_refusal:
            self.on_refusal(reason)
        view = StoreView(_NOBODY, reason)
        self._cache = (("refused", reason), view)
        return view

    def read(self) -> StoreView:
        try:
            dir_stat = os.lstat(self.dir)
        except FileNotFoundError:
            return self._refuse("store-missing")
        except OSError:
            return self._refuse("store-unreadable")
        bad = _dir_problem(dir_stat)
        if bad:
            return self._refuse(bad)
        try:
            fd = os.open(self.dir / DEVICES_FILE, os.O_RDONLY | NOFOLLOW | NONBLOCK | CLOEXEC)
        except FileNotFoundError:
            return self._refuse("store-missing")
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EMLINK):
                return self._refuse("store-symlink")
            return self._refuse("store-unreadable")
        try:
            st = os.fstat(fd)
            bad = _file_problem(st)
            if bad:
                return self._refuse(bad)
            if st.st_size > STORE_MAX_BYTES:
                return self._refuse("store-too-large")
            fingerprint = (st.st_ino, st.st_size, st.st_mtime_ns)
            if self._cache is not None and self._cache[0] == fingerprint:
                return self._cache[1]
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > STORE_MAX_BYTES:  # grew after it was measured
                    return self._refuse("store-too-large")
                chunks.append(chunk)
            try:
                parsed = json.loads(b"".join(chunks).decode("utf-8"))
            except ValueError:  # includes bad UTF-8
                return self._refuse("store-invalid-json")
            except RecursionError:
                return self._refuse("store-invalid-json")
            if not isinstance(parsed, dict):
                return self._refuse("store-not-object")
            ages: dict[str, int] = {}
            ways: dict[str, str] = {}
            for digest, value in parsed.items():
                if not HASH_RE.match(digest) or not isinstance(value, dict):
                    continue
                stamp = value.get("paired_at")
                if isinstance(stamp, int | float) and not isinstance(stamp, bool):
                    ages[digest] = int(stamp)
                if value.get("access") in ACCESS_KINDS:
                    ways[digest] = value["access"]
            view = StoreView(frozenset(k for k in parsed if HASH_RE.match(k)), None, ages, ways)
            self._cache = (fingerprint, view)
            return view
        finally:
            os.close(fd)

    def match(self, cookie_header: str | None, name: str, access: str | None = None) -> str | None:
        """The hash of the first cookie named `name` whose value is paired, or None. Every cookie of that name is tried.

        With `access` ("local" or "tailnet") only a device that was paired that way counts; a device with no recorded way in
        counts for none. A cookie of another way in is skipped, not fatal: a valid one after it is still found."""
        view = self.read()
        if not view.hashes:
            return None
        for value in cookie_values(cookie_header, name):
            digest = cookie_hash(value)
            if digest in view.hashes and (access is None or (view.access or {}).get(digest) == access):
                return digest
        return None


# ---------------------------------------------------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DeviceInfo:
    id: str
    hash: str
    label: str
    paired_at: int
    access: str | None = None  # "local" or "tailnet": the way the browser was paired; None when none was recorded


class PairingWriter:
    def __init__(self, directory: Path | str, now: Callable[[], float] = time.time):
        self.dir = Path(directory)
        self.now = now

    def ensure_dir(self) -> None:
        """Creates the directory (0700) or checks the one that exists; tightens a too-open mode on our own directory."""
        try:
            ensure_private_dir(self.dir)
        except DashboardError as exc:
            raise PairingError(str(exc), "unsafe") from None

    def _locked(self):
        return _StoreLock(self)

    def _load(self, name: str) -> dict[str, object]:
        try:
            fd = os.open(self.dir / name, os.O_RDONLY | NOFOLLOW | NONBLOCK | CLOEXEC)
        except FileNotFoundError:
            return {}
        except OSError:
            raise PairingError(f"{name} cannot be opened (a link or unreadable file)", "unsafe") from None
        try:
            st = os.fstat(fd)
            bad = _file_problem(st)
            if bad:
                raise PairingError(
                    f"{name} is not safe to use ({bad}); check who could read it, then chmod 600", "unsafe"
                )
            try:
                # ACLs too: another principal could write the hash of a cookie it chose
                check_path(self.dir / name, secret=True)
            except TrustError as exc:
                raise PairingError(f"{name} is not safe to use ({exc})", "unsafe") from None
            if st.st_size > STORE_MAX_BYTES:
                raise PairingError(f"{name} is too large", "corrupt")
            data = b""
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                data += chunk
                if len(data) > STORE_MAX_BYTES:
                    raise PairingError(f"{name} is too large", "corrupt")
            try:
                parsed = json.loads(data.decode("utf-8"))  # a zero-byte file is a truncated store, not an empty one
            except (ValueError, RecursionError):
                raise PairingError(f"{name} is not valid JSON; refusing to overwrite it", "corrupt") from None
            if not isinstance(parsed, dict):
                raise PairingError(f"{name} is not a JSON object; refusing to overwrite it", "corrupt")
            return parsed
        finally:
            os.close(fd)

    def _save(self, name: str, data: dict[str, object]) -> None:
        final = self.dir / name
        tmp = self.dir / f".{name}.{os.getpid()}.{os.urandom(4).hex()}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW | CLOEXEC, 0o600)
        try:
            try:
                os.write(fd, json.dumps(data).encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.rename(tmp, final)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def create_token(self) -> str:
        """A single-use pairing token, valid for five minutes. Only its hash is stored."""
        token = secrets.token_urlsafe(32)
        with self._locked():
            now = self.now()
            pending = {
                h: e
                for h, e in self._load(PENDING_FILE).items()
                if isinstance(e, int | float) and not isinstance(e, bool) and e > now
            }
            pending[cookie_hash(token)] = now + PAIR_TTL_S
            self._save(PENDING_FILE, pending)
        return token

    def redeem(self, token: object, label: str | None, access: str | None = None) -> str | None:
        """Spends a token. Returns the new browser's session id (its cookie value), or None for an unknown or expired one.
        `access` ("local" or "tailnet") is recorded with the device: the way the browser was paired (see PairingStore.paired_device)."""
        if access is not None and access not in ACCESS_KINDS:
            raise ValueError(f"unknown way in: {access!r}")
        if not isinstance(token, str) or token == "" or len(token) > COOKIE_VALUE_MAX:
            return None
        with self._locked():
            now = self.now()
            pending = self._load(PENDING_FILE)
            digest = cookie_hash(token)
            expiry = pending.pop(digest, None)
            live = {
                h: e for h, e in pending.items() if isinstance(e, int | float) and not isinstance(e, bool) and e > now
            }
            # A wrong guess writes nothing; the token is consumed before the session exists, so a failure cannot spend it twice.
            valid = isinstance(expiry, int | float) and not isinstance(expiry, bool)
            if valid or len(live) != len(pending):
                self._save(PENDING_FILE, live)
            if not valid or expiry <= now:  # type: ignore[operator]
                return None
            session = secrets.token_urlsafe(32)
            devices = self._load(DEVICES_FILE)
            record: dict[str, object] = {"label": clean_label(label), "paired_at": int(now)}
            if access is not None:
                record["access"] = access
            devices[cookie_hash(session)] = record
            self._save(DEVICES_FILE, devices)
            return session

    def token_valid(self, token: object) -> bool:
        """Whether `token` is an unused, unexpired pairing token. Read-only: it takes no lock and writes nothing, so a wrong
        guess costs the same as a missing file, and a right one cannot be guessed (256 bits)."""
        if not isinstance(token, str) or token == "" or len(token) > COOKIE_VALUE_MAX:
            return False
        try:
            expiry = self._load(PENDING_FILE).get(cookie_hash(token))
        except PairingError:
            return False
        return isinstance(expiry, int | float) and not isinstance(expiry, bool) and expiry > self.now()

    def revoke_everything(self) -> tuple[int, int]:
        """Unpairs every browser AND cancels every unused link under ONE hold of the store lock, so that a redemption cannot land between
        the two (it waits for the lock, then finds neither). Returns (browsers unpaired, links cancelled). pairing-store/1's `revoke`
        leaves pending tokens alone and stays as it is."""
        with self._locked():
            devices, pending = self._load(DEVICES_FILE), self._load(PENDING_FILE)
            self._save(DEVICES_FILE, {})
            if pending:
                self._save(PENDING_FILE, {})
            return len(devices), len(pending)

    def clear_pending(self) -> int:
        """Cancels every unused pairing token. pairing-store/1's `revoke` leaves them alone (its conformance suite checks that);
        the command line calls this too when it unpairs everyone, so a link made before cannot pair a browser after."""
        with self._locked():
            pending = self._load(PENDING_FILE)
            if pending:
                self._save(PENDING_FILE, {})
            return len(pending)

    def devices(self) -> list[DeviceInfo]:
        """Paired browsers, oldest first. Read-only, takes no lock (writers replace the file atomically)."""
        out: list[DeviceInfo] = []
        for digest, value in self._load(DEVICES_FILE).items():
            if not HASH_RE.match(digest):
                continue
            info = value if isinstance(value, dict) else {}
            paired = info.get("paired_at")
            out.append(
                DeviceInfo(
                    id=digest[:8],
                    hash=digest,
                    label=clean_label(info.get("label") if isinstance(info.get("label"), str) else None),
                    paired_at=int(paired) if isinstance(paired, int | float) and not isinstance(paired, bool) else 0,
                    access=info.get("access") if info.get("access") in ACCESS_KINDS else None,
                )
            )
        return sorted(out, key=lambda d: d.paired_at)

    def revoke(self, prefix: str | None) -> int:
        """Unpairs the one browser whose id starts with `prefix` (4 or more hex characters), or every browser for None."""
        if prefix is not None and not PREFIX_RE.match(prefix):
            raise PairingError("give at least 4 hex characters of the id from `devices`", "bad-request")
        with self._locked():
            devices = self._load(DEVICES_FILE)
            gone = [k for k in devices if prefix is None or k.startswith(prefix)]
            if prefix is not None and len(gone) > 1:
                raise PairingError("that id prefix matches more than one browser; give more characters", "ambiguous")
            for k in gone:
                del devices[k]
            if gone or prefix is None:
                self._save(DEVICES_FILE, devices)
            return len(gone)


class _StoreLock:
    """The exclusive advisory lock on <dir>/.lock, held for a whole read-modify-write (section 6)."""

    def __init__(self, writer: PairingWriter):
        self.writer = writer
        self.fd: int | None = None

    def __enter__(self) -> _StoreLock:
        self.writer.ensure_dir()
        try:
            fd = os.open(self.writer.dir / LOCK_FILE, os.O_RDWR | os.O_CREAT | NOFOLLOW | NONBLOCK | CLOEXEC, 0o600)
        except OSError:
            raise PairingError("the pairing store lock file cannot be opened (is it a link?)", "unsafe") from None
        try:
            if _file_problem(os.fstat(fd)):
                raise PairingError(
                    "the pairing store lock file is not a private plain file of ours (check who could write it, then chmod 600)",
                    "unsafe",
                )
            try:
                check_path(self.writer.dir / LOCK_FILE, secret=True)
            except TrustError as exc:
                raise PairingError(f"the pairing store lock file is not safe to use ({exc})", "unsafe") from None
            deadline = time.monotonic() + LOCK_WAIT_S
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise PairingError("the pairing store is locked by another process", "locked") from None
                    time.sleep(0.02)
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd
        return self

    def __exit__(self, *exc: object) -> None:
        if self.fd is not None:
            os.close(self.fd)  # releases the flock
            self.fd = None


# ---------------------------------------------------------------------------------------------------------------------
# What the hub and the command line use
# ---------------------------------------------------------------------------------------------------------------------


class PairingStore:
    """Reader and writer of one store, plus the check this plugin adds: the chain of directories above the store must be
    one nobody else can replace (trustwalk), or nobody is paired. The walk is cached for a few seconds."""

    def __init__(
        self,
        directory: Path | str,
        cookie_name: str,
        on_refusal: Callable[[str], None] | None = None,
        max_age_days: int | None = None,
        local_max_age_days: int | None = None,
        now: Callable[[], float] = time.time,
    ):
        self.dir = Path(directory)
        self.cookie_name = cookie_name
        self.max_age_days = max_age_days
        self.local_max_age_days = local_max_age_days
        self.now = now
        self.reader = PairingReader(self.dir, on_refusal)
        self.writer = PairingWriter(self.dir)
        self._walk: tuple[float, str | None, tuple[object, ...]] | None = None

    def _fingerprint(self) -> tuple[object, ...]:
        """Identity and security metadata of everything the verdict rests on: every directory from the root down to the store, the
        store directory, and each file in it (inode, times, mode, owner; an ACL change moves ctime). Missing parts are None."""
        parts: list[object] = []
        # Above the store only who and what the directory is (a chmod, a chown, a replacement): their times move whenever anybody
        # creates a file in /tmp or in a home directory. The store directory and its files carry their times too (a change of ACL
        # moves ctime); an ACL change on a directory above is picked up when the verdict expires (ANCESTRY_TTL_S).
        for path in (
            *reversed(self.dir.parents),
            self.dir,
            *(self.dir / n for n in (DEVICES_FILE, PENDING_FILE, LOCK_FILE)),
        ):
            try:
                st = os.lstat(path)
            except OSError:
                parts.append(None)
                continue
            own = path == self.dir or path.parent == self.dir
            parts.append(
                (st.st_dev, st.st_ino, st.st_mode, st.st_uid, *((st.st_mtime_ns, st.st_ctime_ns) if own else ()))
            )
        return tuple(parts)

    def ancestry_problem(self) -> str | None:
        """Why the store, a file in it, or a directory above it can be changed by another account, or None. The verdict is reused
        only while nothing it rests on has changed (`_fingerprint`) and for at most ANCESTRY_TTL_S (for what a fingerprint cannot
        see: the targets of links on the way); a replaced or re-permissioned file is judged again before it is used."""
        now = time.monotonic()
        fingerprint = self._fingerprint()
        cached = self._walk
        if cached is not None and now - cached[0] < ANCESTRY_TTL_S and cached[2] == fingerprint:
            return cached[1]
        try:
            check_path(self.dir)
            for name in (
                DEVICES_FILE,
                PENDING_FILE,
                LOCK_FILE,
            ):  # the files too: an ACL on one is as good as a write to the store
                if (self.dir / name).exists() or (self.dir / name).is_symlink():
                    check_path(self.dir / name, secret=True)
            problem = None
        except TrustError as exc:
            problem = str(exc)
        except OSError as exc:
            problem = f"cannot inspect {self.dir} ({exc.strerror})"
        self._walk = (now, problem, fingerprint)
        return problem

    def view(self) -> StoreView:
        if self.ancestry_problem():
            return StoreView(_NOBODY, "store-untrusted-path")
        return self.reader.read()

    def max_age_days_for(self, access: str | None) -> int | None:
        """How many days a browser paired that way stays paired: the general limit, or the shorter local one when it is set
        (and then never longer than the general one). None: no limit."""
        limit = self.max_age_days
        if access == "local" and self.local_max_age_days is not None:
            limit = self.local_max_age_days if limit is None else min(limit, self.local_max_age_days)
        return limit

    def paired_device(self, cookie_header: str | None, access: str | None = None) -> str | None:
        """The paired browser's short id (as `devices` lists it), or None.

        With `access` only a device paired that way counts: a cookie is scoped by host and not by port, so the one of the local
        address travels to every other service on 127.0.0.1 and must not open the tailnet path, where a program that holds it
        could write the Tailscale headers itself; and the reverse. A device with no recorded way in counts for no way."""
        if self.ancestry_problem():
            return None
        digest = self.reader.match(cookie_header, self.cookie_name, access)
        if digest is None:
            return None
        limit = self.max_age_days_for(access)
        if limit is not None:
            paired = (self.reader.read().paired_at or {}).get(digest)
            if paired is None or self.now() - paired > limit * 86400:
                return None  # paired longer ago than the configured age: no longer paired
        return digest[:8]
