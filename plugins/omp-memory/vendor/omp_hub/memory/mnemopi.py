"""mnemopi adapter: the local SQLite memory banks of omp (~/.omp/agent/memories/mnemopi/banks/<bank>/mnemopi.db).

Bank naming (omp coding-agent/src/mnemopi/config.ts, verified against omp's own banks): omp keeps one bank
per working folder, named `<basename of cwd, sanitised>-<Bun.hash(cwd) in base 36>` (e.g. `api-i6vpjra2mruk` for
/home/alice/projects/demo/api). The folder shown to the user is the cwd the rows were retained
from (`cwd` in metadata_json, the most common one); without one it is the bank name minus the hash. folders.py
turns that into the folder dict every item and bank carries (label, group, custom label).

Provenance: a row the importer wrote carries `source` (claude-code | hindsight | claude-mem), `sourceKind`, `name`
and the origin keys in metadata_json; the item shows them as `source`, `sourceKind`, `title` and `meta.import`, a row
without them is `source: "omp"`. Bank source counts and the `source` filter read them in SQL (json_extract behind a
cheap LIKE and json_valid), so no row is parsed in Python.

Scope mapping: rows with scope `global` (the shared bank) -> "global"; every other row (omp writes `bank` for
retained transcripts and learned notes, `session` for consolidated episodes) lives in its folder's bank ->
"folder". mnemopi has no project level, so "project" is always empty here.

Kinds: `memory` = working_memory rows, `episode` = episodic_memory rows, `fact` = the auto-extracted `facts`
triples (listed and deletable, never editable). A listing without a `kind` returns memory + episode only; facts
(hundreds of mostly auto-extracted rows) come back only for kind=fact.

Writes (omp sessions keep these databases open in WAL mode): one short `BEGIN IMMEDIATE` transaction with a busy
timeout, the stored etag re-checked inside it. An edit does what omp's own updateWorking()/degradeEpisodic() do:
- working_memory: `content = ?, embed_text = NULL`; the `wm_au` trigger rewrites the fts_working row from the new
  content (it indexes COALESCE(embed_text, content), which is why embed_text must be cleared);
- episodic_memory: `content = ?, binary_vector = NULL`; the `em_au` trigger rewrites fts_episodes;
- the stale `memory_embeddings` row is deleted: omp re-embeds any live memory without an active-model embedding
  when it next opens the bank (store.ts "resuming interrupted embedding rebuild"), so recall is FTS-only for the
  edited row until then, never wrong;
- `metadata_json` gets `editedAt` / `editedBy` ("local-editor") merged in.
A delete of a working memory does what omp's forgetWorking() does (mnemopi core/beam/store.ts
purgeWorkingMemoryArtifacts), in the same BEGIN IMMEDIATE transaction as the row delete and the snapshot read: the
row, its embedding, `facts` by source_msg_id, `annotations`, the memoria_* projections by source_memory_id, `gists`
and the `graph_edges` tied to the memory / its gist / its facts. Those derived rows (bounded by MAX_DERIVED_ROWS;
a memory with more is refused rather than half-snapshotted) are stored in the trash snapshot next to the row, and
undelete puts them all back. An episode or a fact is deleted as the row alone (the triggers clean FTS); omp itself has no episode forget
(forgetWorking handles working memories only, memory_edit answers not_found for episodes), so there is no cascade
to mirror. Undelete
re-inserts the saved row under its old id; the triggers rebuild FTS and omp re-embeds it.
Search folds case with Python's str.lower() on both sides (a SQL function), so it is case-insensitive beyond ASCII.

The memory map (map()) uses working_memory + episodic_memory rows as nodes. Edges: `graph_edges` rows between two
memories ("link", labelled with edge_type; an endpoint that is a gist id `gist_<memory>` or an extracted fact id
counts as the memory it belongs to, via `gists.memory_id` / `facts.source_msg_id`), memories whose extracted `facts`
share a subject or object ("fact") and memories whose `annotations` share a (kind, value) ("entity"). A term shared
by more than MAP_SHARED_MAX memories is a hub word, not a relation, and makes no edge. Recall activity is the row's
`recall_count` / `last_recalled`; mnemopi keeps no per-recall log.
"""

import base64
import contextlib
import json
import logging
import os
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from . import graph
from .folders import Folders, named, validate_label
from .model import (
	RECALL_WINDOW_DAYS,
	SOURCE_KINDS,
	SOURCES,
	TAG_FILTER_MAX,
	TAG_MAX_CHARS,
	TAG_MAX_PER_ITEM,
	TAGS_TOP,
	MemoryError,
	etag,
	item_dict,
	make_id,
	to_iso,
	top_tags,
	utc_now,
	validate_text,
)

log = logging.getLogger("omp_hub.memory")
BUSY_TIMEOUT_MS = 5000
SCAN_MAX_ROWS = 20000
MAP_SHARED_MAX = 8  # a fact term / annotation shared by more memories than this makes no map edge
MAP_MAX_EDGES = 20000
MAP_MAX_GRAPH_EDGES = 100000  # graph_edges rows read
MAP_TERM_MIN_CHARS = 3
RECALL_TOP = 20
MAX_DERIVED_ROWS = 2000  # rows derived from one deleted working memory that a trash snapshot may hold
_BANK = re.compile(r"[A-Za-z0-9_-]{1,80}")
TITLE_MAX = 200
DESCRIPTION_MAX = 500
# The `source` an importer wrote into metadata_json; NULL for anything else (no "source" key, not JSON), so a row
# without provenance is native. json_valid() first: json_extract raises on a malformed blob.
_SRC_SQL = (
	"CASE WHEN metadata_json LIKE '%\"source\"%' AND json_valid(metadata_json)"
	" THEN json_extract(metadata_json, '$.source') END"
)
_IMPORTED_AT_SQL = (
	"CASE WHEN metadata_json LIKE '%\"importedAt\"%' AND json_valid(metadata_json)"
	" THEN json_extract(metadata_json, '$.importedAt') END"
)
_FOREIGN = tuple(s for s in SOURCES if s != "omp")
_CURSOR = re.compile(r"[0-9]{1,9}")
_HASH_SUFFIX = re.compile(r"-[0-9a-z]{6,13}$")
# Tables whose rows point at a working_memory id (omp purgeWorkingMemoryArtifacts), with the column that does.
_DERIVED = (
	("facts", "source_msg_id"),
	("annotations", "memory_id"),
	("memoria_facts", "source_memory_id"),
	("memoria_instructions", "source_memory_id"),
	("memoria_kg", "source_memory_id"),
	("memoria_preferences", "source_memory_id"),
	("memoria_timelines", "source_memory_id"),
	("gists", "memory_id"),
)
EDITOR = "local-editor"

_TABLES = {"memory": "working_memory", "episode": "episodic_memory", "fact": "facts"}
_ID_COLUMN = {"memory": "id", "episode": "id", "fact": "fact_id"}
_READ_ONLY_FACT = "auto-extracted facts can only be deleted, not edited"


def _like_text(kind):
	return "subject || ' ' || predicate || ' ' || object" if kind == "fact" else "content"


def _pylower(value):
	return value.lower() if isinstance(value, str) else value


def _enc(value):
	"""A column value that survives JSON: bytes become {"$b64": ...}."""
	return {"$b64": base64.b64encode(value).decode()} if isinstance(value, bytes) else value


def _dec(value):
	if isinstance(value, dict) and set(value) == {"$b64"}:
		return base64.b64decode(value["$b64"])
	return value


def _clean(value, limit):
	"""A short display string from a metadata value (control characters dropped), or None."""
	if not isinstance(value, str):
		return None
	text = "".join(c for c in value if c >= " " and c != "\x7f").strip()
	return text[:limit] or None


def _provenance(meta):
	"""(source, sourceKind, title, import info | None) of a row's metadata_json dict (see the importer contract)."""
	source = meta.get("source")
	if source not in SOURCES or source == "omp":
		return "omp", None, None, None
	kind = meta.get("sourceKind")
	info = {
		"origin": _clean(meta.get("origin"), 500),
		"originKey": _clean(meta.get("originKey"), 500),
		"importRunId": _clean(meta.get("importRunId"), 80),
		"importedAt": _clean(meta.get("importedAt"), 40),
		"claudeType": _clean(meta.get("claudeType"), 40),
		"name": _clean(meta.get("name"), TITLE_MAX),
		"description": _clean(meta.get("description"), DESCRIPTION_MAX),
	}
	return source, kind if kind in SOURCE_KINDS else None, info["name"], info


_TAG_VALUE = re.compile(r"[a-z0-9][a-z0-9._-]{0,39}")


def _tag_value(value):
	"""A provenance value as a tag suffix: stripped, lower-cased, [a-z0-9._-] only; None when it is anything else."""
	if not isinstance(value, str):
		return None
	value = value.strip().lower()
	return value if _TAG_VALUE.fullmatch(value) else None


def derive_tags(meta):
	"""The read-only tags of a row's metadata_json dict: the importer's `tags` (a list, or a comma-joined string split on commas) as given, then
	`type:<claudeType>`, `fact:<factType>`, `kind:<sourceKind>` and `source:<source>` (imported rows only).

	Strings only, 1-TAG_MAX_CHARS characters after stripping (anything else is ignored), no duplicates, at most
	TAG_MAX_PER_ITEM. The one definition: the item view, the `tag` filter and the tag counts (as the SQL function
	row_tags) all use it, so they can never disagree."""
	if not isinstance(meta, dict):
		return []
	out = {}  # ordered set
	given = meta.get("tags")
	if isinstance(given, str):  # the importer writes a comma-joined string
		given = given.split(",")
	if isinstance(given, list):
		for tag in given:
			if isinstance(tag, str) and not any(c < " " or c == "\x7f" for c in tag):
				tag = tag.strip()
				if 0 < len(tag) <= TAG_MAX_CHARS:
					out[tag] = None
					if len(out) >= TAG_MAX_PER_ITEM:
						return list(out)
	imported = meta.get("source") in SOURCES and meta.get("source") != "omp"
	for prefix, key in (("type", "claudeType"), ("fact", "factType"), ("kind", "sourceKind")):
		value = _tag_value(meta.get(key))
		if value and (key != "sourceKind" or imported):
			out.setdefault(f"{prefix}:{value}", None)
	if imported:
		out.setdefault(f"source:{meta['source']}", None)
	return [t for t in out if len(t) <= TAG_MAX_CHARS][:TAG_MAX_PER_ITEM]


def _row_tags_sql(blob):
	"""SQL function row_tags(metadata_json): the derive_tags of the blob as a JSON array ('[]' for anything else)."""
	if not isinstance(blob, str) or not blob or blob[0] != "{":
		return "[]"
	try:
		return json.dumps(derive_tags(json.loads(blob)))
	except ValueError:
		return "[]"


_HAS_TAG_SQL = "EXISTS (SELECT 1 FROM json_each(row_tags(metadata_json)) WHERE value = ?)"


def _record(row, skip=()):
	return {k: _enc(row[k]) for k in row.keys() if k not in skip}


class MnemopiAdapter:
	name = "mnemopi"
	tags_writable = False  # item tags are a derived view of the row's provenance (derive_tags), never written
	local = True  # a file on this Mac: the service never makes a page wait on a budget for it

	def __init__(self, banks_dir, busy_timeout_ms=BUSY_TIMEOUT_MS, folders=None):
		self.banks_dir = Path(banks_dir)
		self.busy_timeout_ms = busy_timeout_ms
		self.folders = folders or Folders()
		self._cwd_cache = {}  # bank -> (file signature, cwd): reading it costs a query, and it only changes on a write

	# -- connections -------------------------------------------------------------------------------------------
	def _db_path(self, bank):
		if not isinstance(bank, str) or not _BANK.fullmatch(bank):
			raise MemoryError(400, "malformed bank name")
		path = self.banks_dir / bank / "mnemopi.db"
		if not path.is_file():
			raise MemoryError(404, f"no mnemopi bank {bank}")
		return path

	@contextlib.contextmanager
	def _read(self, bank):
		path = self._db_path(bank)
		try:
			db = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=self.busy_timeout_ms / 1000)
			db.row_factory = sqlite3.Row
			db.create_function("pylower", 1, _pylower, deterministic=True)
			db.create_function("row_tags", 1, _row_tags_sql, deterministic=True)
		except sqlite3.Error:
			log.exception("cannot open mnemopi bank %s", bank)
			raise MemoryError(502, f"cannot open mnemopi bank {bank}") from None
		try:
			yield db
		except sqlite3.OperationalError:
			log.exception("mnemopi bank %s is not readable", bank)
			raise MemoryError(502, f"mnemopi bank {bank} is not readable") from None
		finally:
			db.close()

	@contextlib.contextmanager
	def _write(self, bank):
		"""A write connection inside one BEGIN IMMEDIATE transaction (committed on exit, rolled back on error)."""
		path = self._db_path(bank)
		db = sqlite3.connect(path, timeout=self.busy_timeout_ms / 1000, isolation_level=None)
		db.row_factory = sqlite3.Row
		try:
			db.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_ms)}")
			try:
				db.execute("BEGIN IMMEDIATE")
			except sqlite3.OperationalError:
				raise MemoryError(409, f"mnemopi bank {bank} is busy; try again", retryable=True)
			try:
				yield db
			except BaseException:
				db.execute("ROLLBACK")
				raise
			db.execute("COMMIT")
		except sqlite3.OperationalError as e:
			busy = "locked" in str(e) or "busy" in str(e)
			if not busy:
				log.exception("mnemopi write failed in bank %s", bank)
			if busy:
				raise MemoryError(409, f"mnemopi bank {bank} is busy; try again", retryable=True) from None
			raise MemoryError(502, f"mnemopi write failed in bank {bank}") from None
		finally:
			db.close()

	def _banks(self):
		if not self.banks_dir.is_dir():
			return []
		return sorted(
			p.parent.name for p in self.banks_dir.glob("*/mnemopi.db") if _BANK.fullmatch(p.parent.name)
		)

	# -- bank facts --------------------------------------------------------------------------------------------
	@staticmethod
	def _tables(db):
		return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}

	def _signature(self, bank):
		"""(db, wal) (mtime, size) of a bank: what changes when anything is written to it."""
		base = f"{self.banks_dir / bank / 'mnemopi.db'}"
		sig = []
		for suffix in ("", "-wal"):
			try:
				st = os.stat(base + suffix)
				sig.append((st.st_mtime_ns, st.st_size))
			except OSError:
				sig.append(None)
		return tuple(sig)

	@staticmethod
	def _read_cwd(db, tables):
		"""The bank's most common recent `metadata_json.cwd` (working memories, newest 50 that have one), or None."""
		cwds = Counter()
		if "working_memory" in tables:
			for (blob,) in db.execute(
				"SELECT metadata_json FROM working_memory WHERE metadata_json LIKE '%\"cwd\"%'"
				" ORDER BY timestamp DESC LIMIT 50"
			):
				with contextlib.suppress(ValueError, AttributeError):
					cwd = json.loads(blob).get("cwd")
					if isinstance(cwd, str) and cwd:
						cwds[cwd] += 1
		return cwds.most_common(1)[0][0] if cwds else None

	def _bank_cwd(self, bank, db=None):
		"""The working folder path of a bank (cached until the bank is written), None when it has none."""
		sig = self._signature(bank)
		hit = self._cwd_cache.get(bank)
		if hit and hit[0] == sig:
			return hit[1]
		try:
			if db is not None:
				cwd = self._read_cwd(db, self._tables(db))
			else:
				with self._read(bank) as ro:
					cwd = self._read_cwd(ro, self._tables(ro))
		except (MemoryError, sqlite3.Error):
			cwd = (
				None  # a bank that cannot be read is labelled by its name; its own request reports the error
			)
		self._cwd_cache[bank] = (sig, cwd)
		return cwd

	def folder_map(self, db=None, bank=None):
		"""{bank: folder dict} of every bank (a label depends on the other banks); `db` is `bank`'s open connection."""
		cwds = {b: self._bank_cwd(b, db if b == bank else None) for b in self._banks()}
		return self.folders.resolve(cwds)

	def _folder(self, db, bank):
		"""The folder dict of `bank` (see folders.py); `db` is its open connection."""
		return self.folder_map(db, bank).get(bank) or named(_HASH_SUFFIX.sub("", bank) or bank)

	def bank_path(self, bank):
		"""The working folder path of a bank (None when it has none); 400/404 for a malformed/unknown bank."""
		self._db_path(bank)
		return self.folder_map()[bank]["path"]

	def labels(self):
		"""{bank: {"label"}} of the custom labels."""
		return {bank: {"label": label} for bank, label in self.folders.labels.all().items()}

	def set_label(self, bank, label):
		"""Set (or, with None, clear) a bank's custom label; the bank's new folder dict. 404 for an unknown bank,
		409 when another bank already shows that label."""
		self._db_path(bank)
		if label is not None:
			label = validate_label(label)
			taken = {f["label"].casefold() for b, f in self.folder_map().items() if b != bank}
			if label.casefold() in taken:
				raise MemoryError(409, "another folder already has that label")
		self.folders.labels.set(bank, label)
		return self.folder_map()[bank]

	@staticmethod
	def _tag_counts(db, tables, limit):
		"""[{tag, count}] over working and episodic memories: one query, the tags of a row counted once each."""
		parts = [
			f"SELECT row_tags(metadata_json) AS t FROM {table}"
			for table in ("working_memory", "episodic_memory")
			if table in tables
		]
		if not parts:
			return []
		rows = db.execute(
			f"SELECT j.value, COUNT(*) AS n FROM ({' UNION ALL '.join(parts)}) AS r, json_each(r.t) AS j"
			" GROUP BY j.value ORDER BY n DESC, j.value LIMIT ?",
			(limit,),
		).fetchall()
		return [{"tag": tag, "count": n} for tag, n in rows]

	def tags(self, bank, limit=TAGS_TOP):
		"""The most used tags of one bank (None: all banks together), see derive_tags."""
		if bank:
			self._db_path(bank)
		banks = [bank] if bank else self._banks()
		counts = {}
		for b in banks:
			try:
				with self._read(b) as db:
					found = self._tag_counts(db, self._tables(db), 10**6)
			except MemoryError:
				if bank:
					raise
				continue  # one unreadable bank must not hide the rest
			for entry in found:
				counts[entry["tag"]] = counts.get(entry["tag"], 0) + entry["count"]
		return top_tags(counts, limit)

	@staticmethod
	def _source_stats(db, tables):
		"""{source: {"rows", "lastImportAt"}} over working and episodic memories: one grouped query per table."""
		out = {s: {"rows": 0, "lastImportAt": None} for s in SOURCES}
		for table in ("working_memory", "episodic_memory"):
			if table not in tables:
				continue
			for src, n, imported in db.execute(
				f"SELECT {_SRC_SQL} AS s, COUNT(*), MAX({_IMPORTED_AT_SQL}) FROM {table} GROUP BY s"
			):
				entry = out[src if src in SOURCES else "omp"]
				entry["rows"] += n
				if isinstance(imported, str) and imported > (entry["lastImportAt"] or ""):
					entry["lastImportAt"] = imported
		return out

	def overview(self):
		banks = []
		for bank in self._banks():
			with self._read(bank) as db:
				tables = self._tables(db)
				counts = {"global": 0, "project": 0, "folder": 0}
				size = 0
				for table in ("working_memory", "episodic_memory"):
					if table not in tables:
						continue
					for scope, n, nbytes in db.execute(
						f"SELECT scope = 'global', COUNT(*), COALESCE(SUM(LENGTH(CAST(content AS BLOB))), 0) FROM {table}"
						" GROUP BY scope = 'global'"
					):
						counts["global" if scope else "folder"] += n
						size += nbytes
				facts = db.execute("SELECT COUNT(*) FROM facts").fetchone()[0] if "facts" in tables else 0
				if not sum(counts.values()) and not facts:
					continue  # an empty bank (smoke tests, an unused folder) is noise
				sources = {src: v["rows"] for src, v in self._source_stats(db, tables).items()}
				tags = self._tag_counts(db, tables, TAGS_TOP)
				self._bank_cwd(bank, db)
			banks.append(
				{
					"bank": bank,
					"counts": {**counts, "total": counts["global"] + counts["folder"]},
					"factCount": facts,
					"bytes": size,
					"sources": sources,
					"tags": tags,
				}
			)
		folders = self.folder_map()
		for entry in banks:
			entry["folder"] = folders[entry["bank"]]
		return {"banks": banks}

	def import_stats(self):
		"""{source: {"rows", "lastImportAt"}} summed over every bank, for the Sources page."""
		out = {s: {"rows": 0, "lastImportAt": None} for s in SOURCES}
		for bank in self._banks():
			try:
				with self._read(bank) as db:
					stats = self._source_stats(db, self._tables(db))
			except (MemoryError, sqlite3.Error):
				continue  # one unreadable bank must not hide the rest
			for src, v in stats.items():
				out[src]["rows"] += v["rows"]
				if (v["lastImportAt"] or "") > (out[src]["lastImportAt"] or ""):
					out[src]["lastImportAt"] = v["lastImportAt"]
		return out

	# -- rows -> items -----------------------------------------------------------------------------------------
	def _item(self, bank, kind, row, folder):
		name = (Path(folder["path"]).name if folder["path"] else folder["label"]) or None
		if kind == "fact":
			text = f"{row['subject']} | {row['predicate']} | {row['object']}"
			return item_dict(
				backend=self.name,
				bank=bank,
				kind="fact",
				native=row["fact_id"],
				scope="folder",
				scope_label=f"folder:{name}" if name else "folder",
				folder=folder,
				text=text,
				tags=(),
				created_at=to_iso(row["timestamp"]) or to_iso(row["created_at"]),
				updated_at=None,
				edited_at=None,
				editable=False,
				read_only_reason=_READ_ONLY_FACT,
				deletable=True,
				meta={
					"confidence": row["confidence"],
					"subject": row["subject"],
					"predicate": row["predicate"],
					"object": row["object"],
					"sessionId": row["session_id"],
					"sourceId": row["source_msg_id"],
					"state": "active",
					"recallCount": None,
					"lastRecalledAt": None,
				},
			)
		try:
			meta_json = json.loads(row["metadata_json"] or "{}")
			if not isinstance(meta_json, dict):
				meta_json = {}
		except ValueError:
			meta_json = {}
		is_global = row["scope"] == "global"
		cwd = meta_json.get("cwd")
		source, source_kind, title, imported = _provenance(meta_json)
		tags = derive_tags(meta_json)
		edited_at = meta_json.get("editedAt")
		now = utc_now()
		if row["superseded_by"]:
			state = "superseded"
		elif row["valid_until"] and to_iso(row["valid_until"]) <= now:
			state = "invalid"
		else:
			state = "active"
		meta = {
			"importance": row["importance"],
			"recallCount": row["recall_count"],
			"lastRecalledAt": to_iso(row["last_recalled"]),
			"source": row["source"],
			"memoryType": row["memory_type"],
			"sessionId": row["session_id"],
			"mnemopiScope": row["scope"],
			"state": state,
			"supersededBy": row["superseded_by"],
			"cwd": cwd if isinstance(cwd, str) else None,
		}
		if kind == "episode":
			meta["tier"] = row["tier"]
		if imported:
			meta["import"] = imported
		for key, limit in (("factType", 40), ("claudeType", 40), ("documentId", 200), ("hindsightBank", 100)):
			value = _clean(meta_json.get(key), limit)
			if value:
				meta[key] = value
		return item_dict(
			backend=self.name,
			bank=bank,
			kind=kind,
			native=row["id"],
			scope="global" if is_global else "folder",
			scope_label="global" if is_global else (f"folder:{name}" if name else "folder"),
			folder=folder,
			title=title,
			source=source,
			source_kind=source_kind,
			text=row["content"],
			tags=tags,
			created_at=to_iso(row["timestamp"]) or to_iso(row["created_at"]),
			updated_at=edited_at,
			edited_at=edited_at,
			editable=True,
			read_only_reason=None,
			deletable=True,
			meta=meta,
		)

	@staticmethod
	def _where(kind, scope, q, source=None, tags=()):
		clauses, params = [], []
		for tag in tags:  # facts carry no provenance, so no tag matches them
			clauses.append("0" if kind == "fact" else _HAS_TAG_SQL)
			if kind != "fact":
				params.append(tag)
		if source == "omp":  # native: no importer wrote a foreign source (facts are always native)
			if kind != "fact":
				clauses.append(f"COALESCE({_SRC_SQL}, 'omp') NOT IN ({', '.join('?' * len(_FOREIGN))})")
				params.extend(_FOREIGN)
		elif source:
			clauses.append("0" if kind == "fact" else f"{_SRC_SQL} = ?")
			if kind != "fact":
				params.append(source)
		if scope == "project":
			clauses.append("0")
		elif scope == "global":
			clauses.append("0" if kind == "fact" else "scope = 'global'")
		elif scope == "folder" and kind != "fact":
			clauses.append("COALESCE(scope, '') != 'global'")
		if q:
			clauses.append(f"instr(pylower({_like_text(kind)}), ?) > 0")
			params.append(q.lower())
		return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

	def _kinds(self, kind):
		if kind in _TABLES:
			return [kind]
		return ["memory", "episode"]

	def _select(self, db, tables, bank, kind, scope, q, limit, source=None, tags=()):
		"""(rows, total) of one kind in one bank, newest first, at most `limit` rows."""
		table = _TABLES[kind]
		if table not in tables:
			return [], 0
		where, params = self._where(kind, scope, q, source, tags)
		total = db.execute(f"SELECT COUNT(*) FROM {table}{where}", params).fetchone()[0]
		order = "COALESCE(timestamp, created_at) DESC, " + _ID_COLUMN[kind]
		rows = db.execute(
			f"SELECT *, COALESCE(timestamp, created_at) AS _sort_ts FROM {table}{where} ORDER BY {order} LIMIT ?",
			[*params, limit],
		).fetchall()
		return rows, total

	def list_items(self, bank, scope, kind, q, cursor, limit, source=None, group=None, tags=()):
		if cursor and not _CURSOR.fullmatch(cursor):
			raise MemoryError(400, "malformed cursor")
		tags = tuple(tags or ())
		if len(tags) > TAG_FILTER_MAX or not all(isinstance(t, str) and t for t in tags):
			raise MemoryError(400, f"at most {TAG_FILTER_MAX} tag filters")
		if source is not None and source not in SOURCES:
			raise MemoryError(400, f"source must be one of {', '.join(SOURCES)}")
		offset = int(cursor) if cursor else 0
		banks = [bank] if bank else self._banks()
		if bank:
			self._db_path(bank)
		folders = self.folder_map()
		if group:
			banks = [b for b in banks if folders.get(b, {}).get("group") == group]
		merged, total = [], 0
		for b in banks:
			with self._read(b) as db:
				tables = self._tables(db)
				folder = folders.get(b) or self._folder(db, b)
				for k in self._kinds(kind):
					rows, n = self._select(db, tables, b, k, scope, q, offset + limit, source, tags)
					total += n
					merged.extend((r["_sort_ts"] or "", self._item(b, k, r, folder)) for r in rows)
		# the SQL order, newest first with the id ascending inside a tie, so the per-table window cut above and
		# this merge agree: sort by id first, then (stable) by the raw SQL timestamp, newest first
		merged.sort(key=lambda pair: pair[1]["id"])
		merged.sort(key=lambda pair: pair[0], reverse=True)
		page = [item for _, item in merged[offset : offset + limit]]
		nxt = str(offset + limit) if offset + limit < total else None
		return page, nxt, total

	def get(self, bank, kind, native_id):
		return self._get_in(bank, kind, native_id)

	def _get_in(self, bank, kind, native_id, db=None):
		table = _TABLES.get(kind)
		if table is None:
			raise MemoryError(400, "malformed memory id")
		if db is None:
			with self._read(bank) as ro:
				return self._get_in(bank, kind, native_id, ro)
		if table not in self._tables(db):
			raise MemoryError(404, "memory not found")
		row = db.execute(f"SELECT * FROM {table} WHERE {_ID_COLUMN[kind]} = ?", (native_id,)).fetchone()
		if row is None:
			raise MemoryError(404, "memory not found")
		return self._item(bank, kind, row, self._folder(db, bank))

	# -- writes ------------------------------------------------------------------------------------------------
	def _locked_row(self, db, item):
		"""The stored row of an item inside the write transaction; 404 when gone, 409 when its etag moved on."""
		kind, native = item["kind"], item["id"].split(":", 3)[3]
		try:
			current = self._get_in(item["bank"], kind, native, db)
		except MemoryError as e:
			raise MemoryError(404, "memory not found") if e.status == 404 else e
		if current["etag"] != item["etag"]:
			raise MemoryError(409, "the memory changed since it was loaded", current=current)
		return current, native

	def write(self, item, text, tags):
		if item["kind"] == "fact":
			raise MemoryError(403, _READ_ONLY_FACT)
		validate_text(text)
		if tags is not None and sorted(set(tags)) != item["tags"]:
			raise MemoryError(400, "mnemopi memories have no tags")
		table = _TABLES[item["kind"]]
		with self._write(item["bank"]) as db:
			current, native = self._locked_row(db, item)
			row = db.execute(f"SELECT metadata_json FROM {table} WHERE id = ?", (native,)).fetchone()
			try:
				meta = json.loads(row["metadata_json"] or "{}")
				meta = meta if isinstance(meta, dict) else {}
			except ValueError:
				meta = {}
			meta["editedAt"] = utc_now()
			meta["editedBy"] = EDITOR
			if item["kind"] == "memory":
				db.execute(
					"UPDATE working_memory SET content = ?, embed_text = NULL, metadata_json = ? WHERE id = ?",
					(text, json.dumps(meta), native),
				)
			else:
				db.execute(
					"UPDATE episodic_memory SET content = ?, binary_vector = NULL, metadata_json = ? WHERE id = ?",
					(text, json.dumps(meta), native),
				)
			self._drop_vectors(db, native)
			return self._get_in(item["bank"], item["kind"], native, db)

	@staticmethod
	def _drop_vectors(db, native):
		"""Forget the stored embedding of a changed row; omp re-embeds it on its next open of the bank."""
		if "memory_embeddings" in MnemopiAdapter._tables(db):
			db.execute("DELETE FROM memory_embeddings WHERE memory_id = ?", (native,))

	@staticmethod
	def _graph_refs(native, derived):
		"""Node ids graph_edges uses for a working memory: the memory, its gist and its extracted facts."""
		return {native, f"gist_{native}", *(r["fact_id"] for r in derived.get("facts", ()))}

	def _collect_derived(self, db, native):
		"""{table: [row records]} derived from one working memory (see _DERIVED), refused above MAX_DERIVED_ROWS."""
		tables, derived, total = self._tables(db), {}, 0

		def take(table, where, params):
			nonlocal total
			rows = db.execute(f"SELECT * FROM {table} WHERE {where}", params).fetchall()
			total += len(rows)
			if total > MAX_DERIVED_ROWS:
				raise MemoryError(
					400,
					f"this memory has more than {MAX_DERIVED_ROWS} derived rows; delete it in omp instead",
				)
			if rows:
				derived[table] = [_record(r) for r in rows]

		for table, column in _DERIVED:
			if table in tables:
				take(table, f"{column} = ?", (native,))
		if "graph_edges" in tables:
			refs = sorted(self._graph_refs(native, derived))
			marks = ", ".join("?" * len(refs))
			take("graph_edges", f"source IN ({marks}) OR target IN ({marks})", [*refs, *refs])
		return derived

	def _purge_derived(self, db, native, derived):
		"""omp's purgeWorkingMemoryArtifacts for one id; `derived` is what _collect_derived read a moment ago."""
		tables = self._tables(db)
		for table, column in _DERIVED:
			if table in tables:
				db.execute(f"DELETE FROM {table} WHERE {column} = ?", (native,))
		if "graph_edges" in tables:
			refs = sorted(self._graph_refs(native, derived))
			marks = ", ".join("?" * len(refs))
			db.execute(
				f"DELETE FROM graph_edges WHERE source IN ({marks}) OR target IN ({marks})", [*refs, *refs]
			)

	def delete(self, item, save):
		kind = item["kind"]
		table = _TABLES[kind]
		with self._write(item["bank"]) as db:
			_current, native = self._locked_row(db, item)
			row = db.execute(f"SELECT * FROM {table} WHERE {_ID_COLUMN[kind]} = ?", (native,)).fetchone()
			snapshot = {"table": table, "row": _record(row, skip=("rowid", "binary_vector"))}
			derived = self._collect_derived(db, native) if kind == "memory" else {}
			if derived:
				snapshot["derived"] = derived
			revision = save(snapshot)  # inside the transaction: what is stored is exactly what is removed
			db.execute(f"DELETE FROM {table} WHERE {_ID_COLUMN[kind]} = ?", (native,))
			if kind != "fact":
				self._drop_vectors(db, native)
			if kind == "memory":
				self._purge_derived(db, native, derived)
			return revision

	def recreate(self, snapshot):
		backend, bank, kind, native = snapshot["id"].split(":", 3)
		meta = snapshot["meta"] or {}
		table, row = meta.get("table"), dict(meta.get("row") or {})
		if table != _TABLES.get(kind) or not row:
			raise MemoryError(400, "this snapshot cannot be restored")
		derived = meta.get("derived") or {}
		allowed = {t for t, _ in _DERIVED} | {"graph_edges"}
		if not isinstance(derived, dict) or not set(derived) <= allowed:
			raise MemoryError(400, "this snapshot cannot be restored")
		with self._write(bank) as db:
			if db.execute(f"SELECT 1 FROM {table} WHERE {_ID_COLUMN[kind]} = ?", (native,)).fetchone():
				raise MemoryError(409, "a memory with this id already exists")
			self._insert(db, table, row)
			existing = self._tables(db)
			for dtable, records in derived.items():
				if (
					dtable in existing
				):  # a derived table the bank no longer has is skipped, like omp's own guards
					for record in records:
						self._insert(db, dtable, record, ignore=True)
			return self._get_in(bank, kind, native, db)

	@staticmethod
	def _insert(db, table, record, ignore=False):
		columns = {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}
		if set(record) - columns:
			raise MemoryError(409, "the bank's schema no longer matches this snapshot")
		names = list(record)
		db.execute(
			f"INSERT {'OR IGNORE ' if ignore else ''}INTO {table} ({', '.join(names)})"
			f" VALUES ({', '.join('?' * len(names))})",
			[_dec(record[n]) for n in names],
		)

	# -- the memory map and recall activity --------------------------------------------------------------------
	def _node_items(self, db, tables, bank, folder):
		"""({native id: item}, rows the bank has) of every memory and episode, at most SCAN_MAX_ROWS."""
		items, total = {}, 0
		for kind in ("memory", "episode"):
			if _TABLES[kind] not in tables:
				continue
			rows, n = self._select(db, tables, bank, kind, None, None, max(SCAN_MAX_ROWS - len(items), 0))
			total += n
			for row in rows:
				items[row["id"]] = self._item(bank, kind, row, folder)
		return items, total

	@staticmethod
	def _term(value):
		term = " ".join(value.lower().split()) if isinstance(value, str) else ""
		return term if len(term) >= MAP_TERM_MIN_CHARS else None

	def map(self, bank, limit):
		with self._read(bank) as db:
			tables = self._tables(db)
			items, total = self._node_items(db, tables, bank, self._folder(db, bank))
			owner = {}  # a gist id or fact id -> the memory it hangs off
			if "gists" in tables:
				owner.update(
					(gist, memory)
					for gist, memory in db.execute("SELECT id, memory_id FROM gists")
					if memory in items
				)
			shared = defaultdict(set)  # (kind, term) -> memories
			if "facts" in tables:
				for fact_id, source, subject, obj in db.execute(
					"SELECT fact_id, source_msg_id, subject, object FROM facts"
				):
					if source not in items:
						continue
					owner[fact_id] = source
					for term in (self._term(subject), self._term(obj)):
						if term:
							shared["fact", term].add(source)
			if "annotations" in tables:
				for memory, kind, value in db.execute("SELECT memory_id, kind, value FROM annotations"):
					if memory in items and isinstance(value, str) and value.strip():
						shared["entity", f"{kind}: {value.strip()}"].add(memory)
			links = []
			if "graph_edges" in tables:
				links = db.execute(
					"SELECT source, target, edge_type, weight FROM graph_edges LIMIT ?",
					(MAP_MAX_GRAPH_EDGES,),
				).fetchall()

		def node_of(ref):
			if ref in items:
				return ref
			if ref in owner:
				return owner[ref]
			return ref[5:] if ref.startswith("gist_") and ref[5:] in items else None

		edges = []
		for source, target, edge_type, weight in links:
			a, b = node_of(source), node_of(target)
			if a and b and a != b:
				edges.append(
					{
						"source": items[a]["id"],
						"target": items[b]["id"],
						"kind": "link",
						"weight": weight,
						"label": edge_type,
					}
				)
		pairs = defaultdict(list)  # (kind, a, b) -> the shared terms
		for (kind, term), members in shared.items():
			if not 2 <= len(members) <= MAP_SHARED_MAX:
				continue
			ordered = sorted(members)
			for i, a in enumerate(ordered):
				for b in ordered[i + 1 :]:
					pairs[kind, a, b].append(term)
			if len(pairs) > MAP_MAX_EDGES:
				break
		for (kind, a, b), terms in pairs.items():
			terms.sort()
			label = terms[0] if len(terms) == 1 else f"{terms[0]} (+{len(terms) - 1})"
			edges.append(
				{
					"source": items[a]["id"],
					"target": items[b]["id"],
					"kind": kind,
					"weight": float(len(terms)),
					"label": label[:80],
				}
			)
		nodes = [
			{
				"id": it["id"],
				"text": it["text"],
				"kind": it["kind"],
				"scope": it["scope"],
				"state": it["meta"]["state"],
				"recallCount": it["meta"]["recallCount"],
				"lastRecalledAt": it["meta"]["lastRecalledAt"],
				"createdAt": it["createdAt"],
			}
			for it in items.values()
		]
		return graph.build(nodes, edges, limit, "mnemopi", total)

	def recalls(self, bank, limit):
		"""No event log in mnemopi: `top` and `neverRecalled` come from the rows' recall_count / last_recalled."""
		top, never = [], 0
		with self._read(bank) as db:
			tables = self._tables(db)
			for kind in ("memory", "episode"):
				table = _TABLES[kind]
				if table not in tables:
					continue
				never += db.execute(
					f"SELECT COUNT(*) FROM {table} WHERE COALESCE(recall_count, 0) = 0"
				).fetchone()[0]
				for row in db.execute(
					f"SELECT id, content, recall_count, last_recalled FROM {table} WHERE COALESCE(recall_count, 0) > 0"
					" ORDER BY recall_count DESC, last_recalled DESC LIMIT ?",
					(RECALL_TOP,),
				):
					top.append(
						{
							"id": make_id(self.name, bank, kind, row["id"]),
							"label": graph.label_of(row["content"]),
							"count": row["recall_count"],
							"lastAt": to_iso(row["last_recalled"]),
						}
					)
		top.sort(key=lambda entry: entry["lastAt"] or "", reverse=True)
		top.sort(key=lambda entry: entry["count"], reverse=True)
		return {
			"events": [],
			"top": top[:RECALL_TOP],
			"neverRecalled": never,
			"windowDays": None,
			"available": True,
			"note": "mnemopi keeps no recall log: counts come from each memory's recall counter (events are tracked for shared memory only)",
		}

	# -- optimize ----------------------------------------------------------------------------------------------
	def scan(self, bank):
		items, truncated = [], False
		with self._read(bank) as db:
			tables = self._tables(db)
			folder = self._folder(db, bank)
			for kind in ("memory", "episode", "fact"):
				if _TABLES[kind] not in tables:
					continue
				room = SCAN_MAX_ROWS - len(items)
				if room <= 0:
					truncated = True
					break
				rows, total = self._select(db, tables, bank, kind, None, None, room)
				truncated = truncated or total > len(rows)
				items.extend(self._item(bank, kind, r, folder) for r in rows)
		recalls = {
			"available": True,
			"windowDays": RECALL_WINDOW_DAYS,
			"coveredDays": float(RECALL_WINDOW_DAYS),
		}
		return {
			"backend": self.name,
			"bank": bank,
			"truncated": truncated,
			"items": items,
			"recalls": recalls,
		}
