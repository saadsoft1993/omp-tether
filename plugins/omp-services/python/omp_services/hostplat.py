"""What differs between macOS and Linux: which service manager, and who owns a listening TCP port."""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path

from .util import ServicesError, run_bounded

LSOF = "/usr/sbin/lsof"
TCP_LISTEN = "0A"


def system(env: Mapping[str, str] | None = None) -> str:
    """'darwin' or 'linux'. OMP_SERVICES_PLATFORM forces one of them (to render the other platform's units)."""
    forced = (env or os.environ).get("OMP_SERVICES_PLATFORM", "")
    if forced:
        if forced not in {"darwin", "linux"}:
            raise ServicesError("OMP_SERVICES_PLATFORM must be darwin or linux")
        return forced
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    raise ServicesError(f"unsupported platform {sys.platform!r}: macOS and Linux only")


def manager_kind(plat: str) -> str:
    return "launchd" if plat == "darwin" else "systemd"


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


def listener_uids(
    port: int, plat: str | None = None, timeout: float = 5.0, proc_root: Path = Path("/proc")
) -> set[int]:
    """The uids that own a TCP listener on `port` (any address). Empty: nothing listens, or this user may not see it.

    Raises ServicesError when the platform tool is missing or fails, so the caller can say 'cannot determine'.
    """
    plat = plat or system()
    if plat == "linux":
        uids: set[int] = set()
        found = False
        for name in ("tcp", "tcp6"):
            try:
                uids |= parse_proc_net_tcp((proc_root / "net" / name).read_text(), port)
                found = True
            except OSError:
                continue
        if not found:
            raise ServicesError("cannot read /proc/net/tcp")
        return uids
    result = run_bounded([LSOF, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fu"], timeout)
    if result.error or result.timed_out:
        raise ServicesError("lsof did not answer")
    # lsof exits 1 when nothing matches, with no output: that is 'no listener', not an error.
    if result.returncode not in (0, 1):
        raise ServicesError(f"lsof failed with exit status {result.returncode}")
    return parse_lsof_uids(result.stdout)
