"""Which shared memory bank a directory belongs to, from a memory-banks.conf.

The zsh hook reads the same file with the same rules: resolve symlinks, compare
case-insensitively, longest root wins, no match means no shared memory.
"""

from __future__ import annotations

import dataclasses
import os
import re
from collections.abc import Sequence
from pathlib import Path

DEFAULT_CONF = Path(__file__).resolve().parent.parent / "mini" / "memory-banks.conf"
_BANK = re.compile(r"[A-Za-z0-9_-]+")

Banks = Sequence[tuple[str, str]]


@dataclasses.dataclass(frozen=True)
class Route:
	bank: str | None
	missing_cwd: bool


def load_banks(conf: Path | str = DEFAULT_CONF) -> list[tuple[str, str]]:
	"""`(bank, root relative to $HOME)` pairs; `#` comments and blank lines skipped, extra fields ignored."""
	banks = []
	for line in Path(conf).read_text().splitlines():
		fields = line.split()
		if len(fields) < 2 or fields[0].startswith("#"):
			continue
		bank, root = fields[:2]
		if not _BANK.fullmatch(bank):
			raise ValueError(f"{conf}: bad bank name {bank!r}")
		banks.append((bank, root))
	return banks


def _norm(path: Path | str) -> str:
	return os.path.realpath(os.path.expanduser(str(path))).rstrip("/").casefold() + "/"


def route(path: Path | str, banks: Banks | None = None, home: Path | str | None = None) -> Route:
	"""Bank for path, and whether path no longer exists (it is then routed by its normalised string)."""
	banks = load_banks() if banks is None else banks
	home = Path.home() if home is None else Path(home)
	target = _norm(path)
	best: tuple[int, str] | None = None
	for bank, root in banks:
		prefix = _norm(home / root)
		if target.startswith(prefix) and (best is None or len(prefix) > best[0]):
			best = (len(prefix), bank)
	return Route(best[1] if best else None, not os.path.exists(path))


def bank_for_path(path: Path | str, banks: Banks | None = None, home: Path | str | None = None) -> str | None:
	return route(path, banks, home).bank
