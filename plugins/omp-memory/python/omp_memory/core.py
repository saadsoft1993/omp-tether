"""Reads the banks, the sources and the labels through the vendored memory modules (see docs/vendoring.md).

The modules under vendor/omp_hub/memory are copies of the upstream memory modules, so the folder labels, groups and
source counts here follow their rules. A folder's group is the first directory below your home that contains
it, unless it lies in a root of the user's `groups` setting or of a banks conf file the user named. Nothing here writes to a bank: banks are opened read-only.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
VENDOR = PLUGIN_ROOT / "vendor"
if str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))

from omp_hub.memory.folders import Folders, LabelStore, load_conf_roots
from omp_hub.memory.instructions import Instructions
from omp_hub.memory.mnemopi import MnemopiAdapter
from omp_hub.memory.model import SOURCES, MemoryError
from omp_hub.memory.sources import SourceScanner

__all__ = [
    "SOURCES",
    "VENDOR",
    "Adapter",
    "Folders",
    "Instructions",
    "LabelStore",
    "MemoryError",
    "SourceScanner",
    "build_adapter",
    "load_conf_roots",
    "overview",
    "scan_sources",
]


class Adapter(MnemopiAdapter):
    """The bank adapter, minus the banks named in `skip` (so that one unreadable bank hides nothing else)."""

    skip: frozenset[str] = frozenset()

    def _banks(self):
        return [bank for bank in super()._banks() if bank not in self.skip]


def build_adapter(
    banks_dir: Path,
    labels_file: Path,
    banks_conf: Path | None,
    skip: frozenset[str] = frozenset(),
    groups: dict[str, list[str]] | None = None,
):
    folders = Folders(conf_path=banks_conf, home=Path.home(), labels=LabelStore(labels_file), groups=groups)
    adapter = Adapter(banks_dir, folders=folders)
    adapter.skip = skip
    return adapter


def all_bank_names(banks_dir: Path) -> list[str]:
    """Every bank directory that holds a mnemopi.db, empty or not."""
    return Adapter(banks_dir)._banks()


def overview(
    banks_dir: Path, labels_file: Path, banks_conf: Path | None, groups: dict[str, list[str]] | None = None
) -> dict:
    """{"banks": [...], "emptyBanks": [...], "unreadable": [{"bank", "error"}]}: the overview, per bank.

    A bank that cannot be opened is reported and left out; it does not hide the others. A bank with no rows and no
    facts is listed under `emptyBanks` (left out of the main list as noise).
    """
    names = all_bank_names(banks_dir)
    unreadable: list[dict[str, str]] = []
    probe = build_adapter(banks_dir, labels_file, banks_conf, groups=groups)
    for bank in names:
        try:
            with probe._read(bank) as db:
                db.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        except MemoryError as err:
            unreadable.append({"bank": bank, "error": err.message})
        except sqlite3.Error:
            unreadable.append({"bank": bank, "error": "not a readable SQLite database"})
    skip = frozenset(item["bank"] for item in unreadable)
    adapter = build_adapter(banks_dir, labels_file, banks_conf, skip, groups)
    banks = adapter.overview()["banks"] if banks_dir.is_dir() else []
    shown = {entry["bank"] for entry in banks}
    empty = [bank for bank in names if bank not in shown and bank not in skip]
    return {"banks": banks, "emptyBanks": empty, "unreadable": unreadable}


def scan_sources(
    banks_dir: Path, labels_file: Path, banks_conf: Path | None, groups: dict[str, list[str]] | None = None
) -> dict:
    """The sources answer for this machine: omp banks, Claude Code, Codex, claude-mem, instruction files."""
    home = Path.home()
    adapter = build_adapter(banks_dir, labels_file, banks_conf, groups=groups)
    instructions = Instructions(
        home=home,
        conf_path=banks_conf,
        groups=groups,
        banks_fn=lambda: [folder["path"] for folder in adapter.folder_map().values() if folder["path"]],
    )
    return SourceScanner(home=home, mnemopi=adapter, hindsight=None, instructions=instructions).scan(refresh=True)
