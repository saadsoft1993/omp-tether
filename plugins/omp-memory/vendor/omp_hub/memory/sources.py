"""The Sources page: where memories come from and how many each source holds. Read-only, cached for 60 s.

Nothing here runs on the overview path: GET sources builds (or reuses) the answer, `refresh=1` rescans. Every
foreign database is opened read-only with a short timeout and a failure becomes a note, never an exception:
- claude-code: `~/.claude*/projects/*/memory/*.md` (MEMORY.md, the index, is not a memory), a file or folder reached
  through a symlink counts once;
- codex: rows of `stage1_outputs` in `~/.codex/memories_*.sqlite`;
- claude-mem: observations / session_summaries / user_prompts in `~/.claude-mem/claude-mem.db`;
- hindsight: what the Hindsight adapter reports from its cache (absent when that backend is switched off);
- instructions: the CLAUDE.md / AGENTS.md files of instructions.py.
`importedRows` is how many rows of the mnemopi banks carry that `source` (the importer wrote them), `lastImportAt`
the newest `importedAt` among them.
"""

import glob
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

from .model import utc_now

log = logging.getLogger("omp_hub.memory")
TTL_S = 60
DB_TIMEOUT_S = 1.0
INDEX_FILE = "MEMORY.md"


def _tilde(path, home):
	text = str(path)
	return "~" + text[len(str(home)) :] if text == str(home) or text.startswith(str(home) + "/") else text


def _open_ro(path):
	db = sqlite3.connect(f"{Path(path).as_uri()}?mode=ro", uri=True, timeout=DB_TIMEOUT_S)
	db.execute("PRAGMA query_only = ON")
	return db


def _db_note(error):
	"""A short, text-free reason for a database that could not be read."""
	message = str(error).lower()
	if "locked" in message or "busy" in message:
		return "database is locked; try again later"
	if "no such table" in message or "no such column" in message or "not a database" in message:
		return "schema not recognised"
	return "database cannot be read"


class SourceScanner:
	def __init__(
		self, home=None, mnemopi=None, hindsight=None, instructions=None, ttl_s=TTL_S, clock=time.monotonic
	):
		self.home = Path(home) if home else Path.home()
		self.mnemopi = mnemopi
		self.hindsight = hindsight
		self.instructions = instructions
		self.ttl_s = ttl_s
		self._clock = clock
		self._lock = threading.Lock()
		self._cached = None
		self._at = 0.0

	def scan(self, refresh=False):
		with self._lock:
			if self._cached is None or refresh or self._clock() - self._at >= self.ttl_s:
				self._cached = self._build(refresh)
				self._at = self._clock()
			return self._cached

	# -- one source each -----------------------------------------------------------------------------------------
	def _entry(self, source_id, name, available, path, counts, note=None):
		return {
			"id": source_id,
			"name": name,
			"available": available,
			"path": path,
			"counts": counts,
			"importedRows": None,
			"note": note,
			"lastImportAt": None,
		}

	def _mnemopi(self):
		path = _tilde(self.mnemopi.banks_dir, self.home) if self.mnemopi else None
		if self.mnemopi is None:
			return self._entry(
				"omp-mnemopi", "omp memory (mnemopi)", False, path, {}, "backend is switched off"
			)
		try:
			banks = self.mnemopi.overview()["banks"]
		except Exception as e:
			log.warning("sources: mnemopi overview failed (%s)", type(e).__name__)
			return self._entry("omp-mnemopi", "omp memory (mnemopi)", False, path, {}, "banks cannot be read")
		counts = {
			"banks": len(banks),
			"memories": sum(b["counts"]["total"] for b in banks),
			"facts": sum(b.get("factCount", 0) for b in banks),
		}
		return self._entry(
			"omp-mnemopi", "omp memory (mnemopi)", self.mnemopi.banks_dir.is_dir(), path, counts
		)

	def _claude_code(self):
		files, dirs, per_root = set(), set(), {}
		roots = sorted(glob.glob(os.path.join(glob.escape(str(self.home)), ".claude*", "projects")))
		homes = {os.path.realpath(r) + "/": Path(r).parent.name for r in roots}

		def root_of(real, fallback):
			"""The ~/.claude* root a file really lives in (a link from another root does not take it over)."""
			return next((name for prefix, name in homes.items() if real.startswith(prefix)), fallback)

		for projects in roots:
			for memory_dir in sorted(glob.glob(os.path.join(glob.escape(projects), "*", "memory"))):
				real_dir = os.path.realpath(memory_dir)
				if not os.path.isdir(real_dir):
					continue
				mine = set()
				for md in glob.glob(os.path.join(glob.escape(memory_dir), "*.md")):
					real = os.path.realpath(md)
					if os.path.basename(md) != INDEX_FILE and real not in files and os.path.isfile(real):
						mine.add(real)
				if mine:
					dirs.add(real_dir)
					files |= mine
					for real in mine:
						name = root_of(real, Path(projects).parent.name)
						per_root[name] = per_root.get(name, 0) + 1
		counts = {"memory files": len(files), "project folders": len(dirs)}
		counts.update({f"~/{root}": n for root, n in sorted(per_root.items())})
		return self._entry(
			"claude-code",
			"Claude Code memory",
			bool(roots),
			"~/.claude*/projects/*/memory",
			counts,
		)

	def _codex(self):
		paths = sorted(glob.glob(os.path.join(glob.escape(str(self.home)), ".codex", "memories_*.sqlite")))
		entry = self._entry("codex", "Codex memory", bool(paths), "~/.codex/memories_*.sqlite", {})
		if not paths:
			entry["note"] = "no Codex memory database found"
			return entry
		rows, notes = 0, []
		for path in paths:
			try:
				db = _open_ro(path)
				try:
					rows += db.execute("SELECT COUNT(*) FROM stage1_outputs").fetchone()[0]
				finally:
					db.close()
			except sqlite3.Error as e:
				notes.append(_db_note(e))
		if notes:
			entry["note"] = notes[0]
		else:
			entry["counts"] = {"stage1 outputs": rows}
		return entry

	def _claude_mem(self):
		path = self.home / ".claude-mem" / "claude-mem.db"
		entry = self._entry("claude-mem", "claude-mem archive", path.is_file(), _tilde(path, self.home), {})
		if not path.is_file():
			entry["note"] = "claude-mem database not found"
			return entry
		try:
			db = _open_ro(path)
		except sqlite3.Error as e:
			entry["note"] = _db_note(e)
			return entry
		try:
			notes = []
			for table in ("observations", "session_summaries", "user_prompts"):
				try:
					entry["counts"][table] = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
				except sqlite3.Error as e:
					notes.append(_db_note(e))
			if notes and not entry["counts"]:
				entry["note"] = notes[0]
			elif notes:
				entry["note"] = "some tables are missing: schema not recognised"
		finally:
			db.close()
		return entry

	def _hindsight(self):
		info = self.hindsight.source_info()
		return self._entry(
			"hindsight", "Hindsight (shared banks)", info["available"], None, info["counts"], info["note"]
		)

	def _instructions(self, refresh):
		counts = self.instructions.counts(refresh) if self.instructions else {}
		return self._entry(
			"instructions",
			"Instruction files",
			self.instructions is not None,
			"CLAUDE.md, AGENTS.md in the groups and project folders",
			counts,
		)

	def _build(self, refresh):
		sources = [self._mnemopi(), self._claude_code(), self._codex(), self._claude_mem()]
		if self.hindsight is not None:
			sources.append(self._hindsight())
		sources.append(self._instructions(refresh))
		if self.mnemopi is not None:
			try:
				stats = self.mnemopi.import_stats()
			except Exception as e:
				log.warning("sources: import counts failed (%s)", type(e).__name__)
				stats = {}
			for entry in sources:
				stat = stats.get(entry["id"])
				if stat is not None:
					entry["importedRows"] = stat["rows"]
					entry["lastImportAt"] = stat["lastImportAt"]
		return {"sources": sources, "generatedAt": utc_now()}
