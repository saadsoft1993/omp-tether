"""Which omp this dashboard was written for. `compat.json` at the root of the repository is the machine-readable version
of the same facts, and a test fails when the two disagree (the installed plugin does not carry the repository root)."""

from __future__ import annotations

import re

MIN_OMP = (18, 18, 0)
TESTED_OMP = ("18.18.0", "18.18.3")
_VERSION = re.compile(r"(?:^|[^0-9.])(\d{1,4})\.(\d{1,4})\.(\d{1,4})(?![0-9])")


def parse_omp_version(text: str) -> tuple[int, int, int] | None:
    """(major, minor, patch) from the first line of `omp --version` ('omp/18.18.3'), or None."""
    match = _VERSION.search(text.strip().splitlines()[0] if text.strip() else "")
    return (int(match.group(1)), int(match.group(2)), int(match.group(3))) if match else None
