"""The dashboard page: a fixed set of built files, checked against the hashes in `ui-build.json`.

The built page is committed in the plugin (`ui-dist/`), together with `ui-build.json`, which pins the SHA-256 of every
file and of the set. At start the hub reads each file once through one descriptor, refuses the whole page unless every
hash matches and no other file is present, and serves from memory: a file replaced afterwards changes nothing until the
next start. The directory is also walked (trustwalk.check_path), because a browser runs the scripts it serves.

`ui-build.json` sits in the same tree, so the hashes catch damage and mistakes, and a build that does not match its
sources (`make verify-ui` rebuilds and compares); they do not defend against someone who can already write the plugin's
directory, which the trust walk is for.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .trustwalk import TrustError, check_path, others_can_write
from .util import CLOEXEC, NOFOLLOW, NONBLOCK, DashboardError, read_fd_bounded

MAX_FILE_BYTES = 2 * 1024 * 1024
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
}
# What the hub serves, and the file each address maps to.
ROUTES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/theme.js": "theme.js",
    "/index.js": "index.js",
    "/index.css": "index.css",
    "/favicon.svg": "favicon.svg",
}


class UiBundleError(DashboardError):
    """The page is missing, damaged or not trusted; the hub answers 503 for it."""


@dataclass(frozen=True)
class Asset:
    data: bytes
    etag: str
    content_type: str


@dataclass(frozen=True)
class Bundle:
    assets: dict[str, Asset]  # file name -> asset
    tree_sha256: str


def plugin_root() -> Path:
    return Path(__file__).resolve().parents[2]


def tree_digest(files: dict[str, str]) -> str:
    """sha256 over `<name>\\0<sha256 of the file>\\n` for every file, sorted by name."""
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(f"{name}\0{files[name]}\n".encode())
    return digest.hexdigest()


def _read_file(directory: Path, name: str, limit: int) -> bytes:
    fd = os.open(directory / name, os.O_RDONLY | NOFOLLOW | NONBLOCK | CLOEXEC)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid not in (os.geteuid(), 0) or others_can_write(st):
            raise UiBundleError(f"{name} is not a plain file that only you can change")
        data = read_fd_bounded(fd, limit)
    finally:
        os.close(fd)
    if data is None:
        raise UiBundleError(f"{name} is larger than {limit} bytes")
    return data


def load_bundle(root: Path | None = None) -> Bundle:
    """The verified page, or UiBundleError saying what is wrong."""
    root = root or plugin_root()
    pin_file, directory = root / "ui-build.json", root / "ui-dist"
    try:
        pin = json.loads(_read_file(root, "ui-build.json", 1024 * 1024))
    except (OSError, ValueError, UiBundleError) as exc:
        raise UiBundleError(f"ui-build.json cannot be read ({type(exc).__name__})") from None
    try:
        pinned = {name: str(entry["sha256"]) for name, entry in pin["files"].items()}
        tree = str(pin["treeSha256"])
        if pin["schema"] != 1 or not all(
            NAME_RE.match(n) and re.fullmatch(r"[0-9a-f]{64}", h) for n, h in pinned.items()
        ):
            raise ValueError
        if not re.fullmatch(r"[0-9a-f]{64}", tree) or set(pinned) != set(ROUTES.values()):
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError):
        raise UiBundleError(f"{pin_file} is not a ui-build/1 file for this dashboard") from None
    if tree_digest(pinned) != tree:
        raise UiBundleError("ui-build.json does not agree with itself (its tree hash is wrong)")
    try:
        check_path(directory)
        check_path(pin_file)
    except TrustError as exc:
        raise UiBundleError(f"the page's directory is not trusted: {exc}") from None
    try:
        present = set(os.listdir(directory))
    except OSError:
        raise UiBundleError("ui-dist is missing") from None
    if present != set(pinned):
        extra, missing = sorted(present - set(pinned)), sorted(set(pinned) - present)
        raise UiBundleError(
            "ui-dist does not hold exactly the pinned files"
            + (f" (extra: {', '.join(extra[:5])})" if extra else "")
            + (f" (missing: {', '.join(missing[:5])})" if missing else "")
        )
    assets: dict[str, Asset] = {}
    for name, want in sorted(pinned.items()):
        try:
            data = _read_file(directory, name, MAX_FILE_BYTES)
        except OSError:
            raise UiBundleError(f"{name} cannot be read") from None
        got = hashlib.sha256(data).hexdigest()
        if got != want:
            raise UiBundleError(f"{name} does not match its pinned hash; rebuild it or reinstall the plugin")
        content_type = TYPES.get(Path(name).suffix)
        if content_type is None:
            raise UiBundleError(f"{name}: unknown file type")
        assets[name] = Asset(data, f'"{got[:32]}"', content_type)
    return Bundle(assets, tree)


def pin_document(files: dict[str, bytes], bun: str) -> dict[str, Any]:
    """The ui-build/1 document for built files (what scripts/build-ui.ts writes; kept here so tests can compare)."""
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    return {
        "schema": 1,
        "bun": bun,
        "files": {name: {"sha256": hashes[name], "size": len(files[name])} for name in sorted(files)},
        "treeSha256": tree_digest(hashes),
    }
