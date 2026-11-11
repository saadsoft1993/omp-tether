"""Folder model of the memory banks: which working folder a bank belongs to, how it is labelled and grouped.

A bank's folder is a dict `{path, label, group, labelCustom}`:
- `path`: the working folder the bank's rows were retained from (its most common `metadata_json.cwd`), or None
  when the bank has none (the global bank, an empty one);
- `label`: the user's own label (the labels file) when there is one, else the default: the last two segments
  of the path (`demo/api`), or the bank name minus its hash without a path. Two banks with the same
  default label get the last three segments, and what still collides gets ` (2)`, ` (3)`... in bank-name order,
  so a default label is unique and never depends on a custom one;
- `group`: the first directory below $HOME that contains the folder (`~/projects/demo` -> `projects`), unless the
  folder is inside a root the user named: in `groups` ({group name: [root paths]}) or in a conf file of
  `<group> <root relative to $HOME>` lines. The longest root containing the path wins (compared
  case-insensitively after resolving symlinks; an explicit `groups` entry beats a conf line of the same length).
  The caller may map a conf word to a display name (`group_names`) and may turn the first-directory default off
  (`top_level=False`: a folder outside every root is then `Other`). A bank with no path, or a folder that is not
  below $HOME, is `Other`;
- `labelCustom`: True when `label` is the user's.

Custom labels live in one JSON file whose path the caller chooses (dir 0700, file 0600, written atomically). A label is
1-60 characters, normalised to NFC, with no control or format characters (no newline, no bidi override).
"""

import contextlib
import json
import logging
import os
import re
import tempfile
import threading
import unicodedata
from pathlib import Path

from .model import MemoryError

log = logging.getLogger("omp_hub.memory")
LABEL_MAX = 60
OTHER = "Other"
_CONF_BANK = re.compile(r"[A-Za-z0-9_-]+")
_HASH_SUFFIX = re.compile(r"-[0-9a-z]{6,13}$")


def group_name(conf_group, names=None):
	"""The group a conf line's first word stands for: `names` ({lowercase word: display name}, supplied by the caller)
	maps it, otherwise it is the word itself."""
	word = str(conf_group)
	return (names or {}).get(word.lower(), word)


def validate_label(value):
	"""The label to store: NFC, trimmed, 1-60 characters, no control/format characters; MemoryError 400 otherwise."""
	if not isinstance(value, str):
		raise MemoryError(400, "label must be a string or null")
	label = unicodedata.normalize("NFC", value).strip()
	if not label or len(label) > LABEL_MAX:
		raise MemoryError(400, f"label must be 1 to {LABEL_MAX} characters")
	if any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp") for c in label):
		raise MemoryError(400, "label must not contain control characters")
	return label


def load_conf_roots(conf_path, home):
	"""[(group, absolute root path)] from a conf file: `<group> <root relative to $HOME>` per line, `#` comments, a
	group word of [A-Za-z0-9_-]+, extra fields ignored. [] when there is no file or it is unreadable."""
	if not conf_path:
		return []
	try:
		lines = Path(conf_path).read_text().splitlines()
	except OSError:
		return []
	roots = []
	for line in lines:
		parts = line.split()
		if len(parts) >= 2 and not parts[0].startswith("#") and _CONF_BANK.fullmatch(parts[0]):
			roots.append((parts[0], str(Path(home) / parts[1])))
	return roots


def parse_groups(groups, home):
	"""[(group name, absolute root path)] from {name: [root, ...]}: `~/x` and a relative root are below $HOME,
	an absolute root stays. Names and roots that are not strings, and empty ones, are skipped."""
	roots = []
	for name, paths in (groups or {}).items():
		if not isinstance(name, str) or not name.strip():
			continue
		for path in [paths] if isinstance(paths, str) else (paths or []):
			if not isinstance(path, str) or not path.strip():
				continue
			if path.startswith("~/"):
				path = path[2:]
			roots.append((name.strip(), os.path.normpath(os.path.join(str(home), path))))
	return roots


def group_roots(conf_path, home, groups=None, group_names=None):
	"""Every [(group, absolute root)]: the conf file's lines (words mapped by `group_names`), then `groups`."""
	conf = [(group_name(g, group_names), r) for g, r in load_conf_roots(conf_path, home)]
	return conf + parse_groups(groups, home)


def top_level_dir(path, home):
	"""The first directory below $HOME (by realpath) that contains `path`; None when it is $HOME itself or elsewhere."""
	real_home = os.path.realpath(home)
	first = os.path.relpath(os.path.realpath(path), real_home).split(os.sep)[0]
	return None if first in (".", "..") else os.path.join(real_home, first)


def top_level_group(path, home):
	"""The name of `top_level_dir`: the default group of a folder."""
	top = top_level_dir(path, home)
	return os.path.basename(top) if top else None


class LabelStore:
	"""{bank: custom label}, kept in one JSON file (in memory only when `path` is None)."""

	def __init__(self, path=None):
		self.path = Path(path) if path else None
		self._lock = threading.RLock()
		self._labels = {}
		self._sig = None
		self.version = (
			0  # bumps whenever the labels change, for callers that cache what they derive from them
		)

	def _signature(self):
		try:
			st = self.path.stat()
		except OSError:
			return None
		return (st.st_mtime_ns, st.st_size)

	def _refresh(self):
		if self.path is None:
			return
		sig = self._signature()
		if sig == self._sig:
			return
		labels = {}
		if sig is not None:
			try:
				data = json.loads(self.path.read_text())
				entries = data.get("labels") if isinstance(data, dict) else None
				for bank, entry in (entries if isinstance(entries, dict) else {}).items():
					with contextlib.suppress(MemoryError):
						labels[bank] = validate_label(entry.get("label") if isinstance(entry, dict) else None)
			except (OSError, ValueError):
				log.warning("memory labels file %s is unreadable; no custom labels", self.path.name)
				labels = {}
		self._labels, self._sig = labels, sig
		self.version += 1

	def all(self):
		with self._lock:
			self._refresh()
			return dict(self._labels)

	def set(self, bank, label):
		"""Store `label` for the bank (None clears it) and write the file atomically."""
		with self._lock:
			self._refresh()
			labels = dict(self._labels)
			if label is None:
				labels.pop(bank, None)
			else:
				labels[bank] = label
			if self.path is not None:
				self._write(labels)
				self._sig = self._signature()
			self._labels = labels
			self.version += 1

	def _write(self, labels):
		parent = self.path.parent
		try:
			parent.mkdir(parents=True, mode=0o700)
			parent.chmod(0o700)  # only a directory this write created: an existing one keeps its mode
		except FileExistsError:
			if not parent.is_dir():
				raise
		body = json.dumps(
			{"version": 1, "labels": {b: {"label": labels[b]} for b in sorted(labels)}},
			ensure_ascii=False,
			indent="\t",
		)
		fd, tmp = tempfile.mkstemp(prefix=".memory-labels-", suffix=".tmp", dir=parent)
		try:
			os.fchmod(fd, 0o600)
			with os.fdopen(fd, "w", encoding="utf-8") as f:
				f.write(body + "\n")
				f.flush()
				os.fsync(f.fileno())
			os.replace(tmp, self.path)
		except BaseException:
			with contextlib.suppress(OSError):
				os.unlink(tmp)
			raise


def _segments(path):
	return [s for s in path.split("/") if s]


def _default_label(path, bank, depth):
	if not path:
		return _HASH_SUFFIX.sub("", bank) or bank
	parts = _segments(path)
	return "/".join(parts[-depth:]) if parts else "/"


class Folders:
	"""Resolves {bank: cwd} into folder dicts (see the module docstring)."""

	def __init__(self, conf_path=None, home=None, labels=None, groups=None, group_names=None, top_level=True):
		self.labels = labels if labels is not None else LabelStore()
		self._home = Path(home) if home else Path.home()
		self._conf_path = Path(conf_path) if conf_path else None
		self._groups = groups
		self._group_names = group_names
		self._top_level = top_level
		self._roots = None  # [(lowercase realpath prefix with a trailing "/", group, explicit)]
		self._group_cache = {}
		self._plain_cache = None  # (cwds key, default labels) of the last resolve

	def _load_roots(self):
		conf = len(load_conf_roots(self._conf_path, self._home))
		return [
			(os.path.realpath(path).lower().rstrip("/") + "/", group, index >= conf)
			for index, (group, path) in enumerate(
				group_roots(self._conf_path, self._home, self._groups, self._group_names)
			)
		]

	def group_of(self, path):
		"""The group of a folder path (see the module docstring); Other when it is None."""
		if not path:
			return OTHER
		if self._roots is None:
			self._roots = self._load_roots()
		hit = self._group_cache.get(path)
		if hit is None:
			key = os.path.realpath(path).lower().rstrip("/") + "/"
			best = max(
				(r for r in self._roots if key.startswith(r[0])),
				key=lambda r: (len(r[0]), r[2]),
				default=None,
			)
			if best:
				hit = best[1]
			else:
				hit = (top_level_group(path, self._home) if self._top_level else None) or OTHER
			self._group_cache[path] = hit
		return hit

	def _defaults(self, cwds):
		"""{bank: default label}, unique across the banks."""
		key = tuple(sorted(cwds.items(), key=lambda kv: kv[0]))
		if self._plain_cache and self._plain_cache[0] == key:
			return self._plain_cache[1]
		labels = {b: _default_label(p, b, 2) for b, p in cwds.items()}
		by_label = {}
		for bank, label in labels.items():
			by_label.setdefault(label, []).append(bank)
		for banks in by_label.values():
			if len(banks) > 1:
				for bank in banks:
					labels[bank] = _default_label(cwds[bank], bank, 3)
		taken, final = set(), {}
		for bank in sorted(labels):  # still equal: the first keeps the label, the next ones get (2), (3)...
			label, n = labels[bank], 1
			while label in taken:
				n += 1
				label = f"{labels[bank]} ({n})"
			taken.add(label)
			final[bank] = label
		self._plain_cache = (key, final)
		return final

	def resolve(self, cwds):
		"""{bank: folder dict} for {bank: cwd | None} (every bank that exists: a label depends on the others)."""
		defaults = self._defaults(cwds)
		custom = self.labels.all()
		out = {}
		for bank, path in cwds.items():
			label = custom.get(bank)
			out[bank] = {
				"path": path,
				"label": label or defaults[bank],
				"group": self.group_of(path),
				"labelCustom": label is not None,
			}
		return out


def named(label, group=OTHER):
	"""A folder dict that is not a mnemopi bank (a Hindsight bank or project): no path, never custom."""
	return {"path": None, "label": label, "group": group, "labelCustom": False}
