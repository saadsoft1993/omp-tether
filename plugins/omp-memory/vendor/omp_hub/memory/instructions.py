"""Instruction files (CLAUDE.md / AGENTS.md), read-only: which exist and which apply to a folder.

Discovery (cached for 60 s) finds
- the global files: ~/.claude*/CLAUDE.md, ~/.codex/AGENTS.md (kind `global`);
- every CLAUDE.md / AGENTS.md under the scan roots, at most 4 directory levels down, never descending into dot
  directories (.git, .claude/worktrees ...), `node_modules`, `worktrees`, vendored or build directories or a
  symlinked directory. The scan roots are `roots` when the caller gives them; otherwise the group roots (a conf
  file's, and `groups`) plus the first directory below $HOME of every bank folder (`banks_fn` lists those).
  A file directly in a scan root, in one of its direct children or in a group root is `workspace`, any other `repo`.
A file reached by two paths (a symlink, AGENTS.md -> CLAUDE.md) is listed once, by the path that is not a symlink
(the global path wins over a project path). That set is the ONLY thing the hub reads: /instruction answers a path
only when it is exactly a path of the set and still resolves to the same file; anything else is a 404. Nothing is
ever written. Text is capped at 64 KiB.
"""

import glob
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .folders import group_roots, top_level_dir
from .model import MemoryError

NAMES = ("CLAUDE.md", "AGENTS.md")
MAX_DEPTH = 4
MAX_DIRS = 20000  # directories visited by one discovery, a stop for a runaway tree
TEXT_MAX = 64 * 1024
TTL_S = 60
PATH_MAX = 4096
SKIP_DIRS = frozenset(
	{
		"node_modules",
		"worktrees",
		"vendor",
		"vendored",
		"third_party",
		"third-party",
		"site-packages",
		"__pycache__",
		"dist",
		"build",
		"target",
		"venv",
		"Pods",
	}
)


def _iso(mtime):
	return datetime.fromtimestamp(mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Instructions:
	def __init__(
		self,
		home=None,
		conf_path=None,
		resolve_bank=None,
		ttl_s=TTL_S,
		clock=time.monotonic,
		roots=None,
		groups=None,
		group_names=None,
		banks_fn=None,
	):
		"""resolve_bank(bank) -> the bank's folder path | None; raises MemoryError 404 for an unknown bank.
		roots: the directories to scan (default: see the module docstring); groups, group_names: as for Folders;
		banks_fn() -> the folder paths of the banks, from which the default scan roots are derived."""
		self.home = Path(home) if home else Path.home()
		self.roots = roots
		self.groups = groups
		self.group_names = group_names
		self.banks_fn = banks_fn
		self.conf_path = conf_path
		self.resolve_bank = resolve_bank
		self.ttl_s = ttl_s
		self._clock = clock
		self._lock = threading.Lock()
		self._found = None  # {path: {path, real, kind, size, mtime}}
		self._at = 0.0

	# -- discovery ---------------------------------------------------------------------------------------------
	def _globals(self):
		claude = sorted(glob.glob(os.path.join(glob.escape(str(self.home)), ".claude*", "CLAUDE.md")))
		return [*map(Path, claude), self.home / ".codex" / "AGENTS.md"]

	def _scan_roots(self, conf_roots):
		"""The existing, distinct (by realpath) directories to scan; one that lies inside another is dropped."""
		if self.roots is not None:
			wanted = list(self.roots)
		else:
			wanted = [path for _group, path in conf_roots]
			for folder in self.banks_fn() if self.banks_fn else ():
				top = top_level_dir(folder, self.home) if folder else None
				if top:
					wanted.append(top)
		real = sorted({os.path.realpath(path) for path in wanted if os.path.isdir(path)})
		return [r for r in real if not any(r != o and r.startswith(o.rstrip("/") + "/") for o in real)]

	def _entry(self, path, kind):
		try:
			real = os.path.realpath(path)
			st = os.stat(real)
		except OSError:
			return None
		if not os.path.isfile(real):
			return None
		return {"path": str(path), "real": real, "kind": kind, "size": st.st_size, "mtime": _iso(st.st_mtime)}

	def _scan(self):
		found, by_real = {}, {}

		def add(entry):
			prior = by_real.get(entry["real"])
			if prior is not None:
				# one file, several paths: keep the one that is not a symlink, else the first found
				if (
					prior["kind"] == "global"
					or entry["path"] != entry["real"]
					or prior["path"] == prior["real"]
				):
					return
				del found[prior["path"]]
			found[entry["path"]] = by_real[entry["real"]] = entry

		for path in self._globals():
			entry = self._entry(path, "global")
			if entry:
				add(entry)
		conf_roots = group_roots(self.conf_path, self.home, self.groups, self.group_names)
		scan_roots = self._scan_roots(conf_roots)
		root_set = set(scan_roots)
		workspaces = {os.path.realpath(p) for _group, p in conf_roots} | root_set
		visited = 0
		stack = [(root, 0) for root in reversed(scan_roots)]
		while stack and visited < MAX_DIRS:
			directory, depth = stack.pop()
			visited += 1
			try:
				entries = sorted(os.scandir(directory), key=lambda e: e.name)
			except OSError:
				continue
			kind = (
				"workspace" if directory in workspaces or os.path.dirname(directory) in root_set else "repo"
			)
			for e in entries:
				if e.name in NAMES:
					entry = self._entry(os.path.join(directory, e.name), kind)
					if entry:
						add(entry)
			if depth < MAX_DEPTH:
				for e in reversed(entries):
					if e.name.startswith(".") or e.name in SKIP_DIRS:
						continue
					try:
						if e.is_dir(follow_symlinks=False):
							stack.append((e.path, depth + 1))
					except OSError:
						continue
		return found

	def discovered(self, refresh=False):
		"""{path: {path, real, kind, size, mtime}} of every instruction file the hub may show (cached)."""
		with self._lock:
			if self._found is None or refresh or self._clock() - self._at >= self.ttl_s:
				self._found = self._scan()
				self._at = self._clock()
			return self._found

	def counts(self, refresh=False):
		found = self.discovered(refresh)
		out = {"files": len(found)}
		for kind in ("global", "workspace", "repo"):
			out[kind] = sum(1 for e in found.values() if e["kind"] == kind)
		return out

	# -- the two reads -----------------------------------------------------------------------------------------
	def _folder_path(self, folder):
		"""The absolute folder an /instructions request names: a path, or a mnemopi bank (its folder, maybe None)."""
		if not isinstance(folder, str) or not folder or len(folder) > PATH_MAX or "\0" in folder:
			raise MemoryError(400, "folder must be an absolute path or a bank name")
		if folder.startswith("/"):
			return os.path.realpath(folder)
		if self.resolve_bank is None:
			raise MemoryError(404, "no such bank")
		return self.resolve_bank(folder)

	def listing(self, folder):
		"""{"files": [{path, kind, size, mtime}]}: the files that apply to the folder, global first, then from the
		outermost directory down to the folder itself."""
		path = self._folder_path(folder)
		found = self.discovered()
		files = [e for e in found.values() if e["kind"] == "global"]
		files.sort(key=lambda e: e["path"])
		if path:
			chain = [path]
			while os.path.dirname(chain[-1]) != chain[-1]:
				chain.append(os.path.dirname(chain[-1]))
			for directory in reversed(chain):
				for name in NAMES:
					entry = found.get(os.path.join(directory, name))
					if entry and entry["kind"] != "global":
						files.append(entry)
		return {"files": [{k: e[k] for k in ("path", "kind", "size", "mtime")} for e in files]}

	def read(self, path):
		"""{path, size, text, truncated} of a discovered file; MemoryError 404 for anything else."""
		missing = MemoryError(404, "not an instruction file the hub lists")
		if not isinstance(path, str) or len(path) > PATH_MAX or "\0" in path:
			raise missing
		entry = self.discovered().get(path)
		if entry is None:
			raise missing
		try:
			if (
				os.path.realpath(path) != entry["real"]
			):  # swapped for a link to somewhere else since discovery
				raise missing
			with open(entry["real"], "rb") as f:
				data = f.read(TEXT_MAX + 1)
				size = os.fstat(f.fileno()).st_size
		except OSError:
			raise missing from None
		return {
			"path": path,
			"size": size,
			"text": data[:TEXT_MAX].decode("utf-8", "replace"),
			"truncated": len(data) > TEXT_MAX,
		}
