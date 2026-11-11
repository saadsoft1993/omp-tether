"""Shared vocabulary of the memory package: limits, the error type, ids, etags and the Adapter protocol."""

import hashlib
import re
from datetime import datetime, timezone
from typing import Protocol

REVISION_KEEP = 4  # prior versions kept per memory; the one place this number lives
RECALL_WINDOW_DAYS = (
	30  # how far back "recalled" looks: the proxy's recall log retention, and the Optimize rule
)
MAX_TEXT_BYTES = 64 * 1024  # UTF-8 bytes of memory text per edit
LIST_PREVIEW_CHARS = 600  # text kept per item in a list response
KINDS = ("memory", "episode", "fact")
SCOPES = ("global", "project", "folder")
# Where a memory came from (`metadata_json.source` written by the importer); a row without one is native omp memory.
SOURCES = ("omp", "claude-code", "hindsight", "claude-mem")
SOURCE_KINDS = ("memory", "summary")
TAG_MAX_PER_ITEM = 12  # derived + given tags an item shows
TAG_MAX_CHARS = 60
TAG_FILTER_MAX = 3  # `tag=` filters one items request may carry (AND)
TAGS_TOP = 30  # tags a bank's overview entry lists
TAGS_ENDPOINT_TOP = 100  # tags GET /tags lists


class MemoryError(Exception):
	"""A request the memory package refuses or cannot serve. status: 400/403/404/409/502."""

	def __init__(self, status, message, current=None, retryable=False):
		super().__init__(message)
		self.status = status
		self.message = message
		self.current = current  # on a 409 etag mismatch: the item as it is now
		self.retryable = retryable  # on a 409 for a busy database: the same request may simply be sent again


_ID_PART = re.compile(r"[A-Za-z0-9._@-]{1,200}")


def make_id(backend, bank, kind, native):
	return f"{backend}:{bank}:{kind}:{native}"


def parse_id(item_id):
	"""(backend, bank, kind, native) from "<backend>:<bank>:<kind>:<native>"; MemoryError 400 when malformed."""
	parts = item_id.split(":", 3) if isinstance(item_id, str) else []
	if len(parts) != 4 or not all(parts):
		raise MemoryError(400, "malformed memory id")
	backend, bank, kind, native = parts
	if (
		kind not in KINDS
		or not _ID_PART.fullmatch(bank)
		or not _ID_PART.fullmatch(native)
		or bank in (".", "..")
	):
		raise MemoryError(400, "malformed memory id")
	return backend, bank, kind, native


def top_tags(counter, limit):
	"""[{tag, count}] of a Counter, most used first, ties by name, at most `limit`."""
	ranked = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
	return [{"tag": tag, "count": n} for tag, n in ranked]


def etag(text, tags):
	"""sha256(text + "\\n" + the sorted tags, one per line)[:16]: changes iff the editable content changes."""
	body = text + "\n" + "\n".join(sorted(tags or ()))
	return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def size_bytes(text):
	return len(text.encode("utf-8"))


def validate_text(text):
	"""The edit text as given (never trimmed); MemoryError 400 when empty or over MAX_TEXT_BYTES."""
	if not isinstance(text, str):
		raise MemoryError(400, "text must be a string")
	if not text.strip():
		raise MemoryError(400, "text must not be empty")
	if size_bytes(text) > MAX_TEXT_BYTES:
		raise MemoryError(400, f"text is over the {MAX_TEXT_BYTES // 1024} KiB limit")
	return text


def utc_now():
	return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def to_iso(value):
	"""An ISO-8601 UTC string from the timestamp shapes the backends use, or None.

	mnemopi writes "2026-11-05T13:00:11.621Z" and, in created_at, SQLite's "2026-11-05 13:00:11" (UTC).
	"""
	if not value or not isinstance(value, str):
		return None
	if re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", value):
		return value.replace(" ", "T") + "Z"
	return value


def item_dict(
	*,
	backend,
	bank,
	kind,
	native,
	scope,
	scope_label,
	folder,
	text,
	tags,
	created_at,
	updated_at,
	edited_at,
	editable,
	read_only_reason,
	deletable,
	meta,
	delete_reason=None,
	delete_label="Delete",
	title=None,
	source="omp",
	source_kind=None,
):
	"""A normalised item (the API shape); revisionCount is filled in by the service.

	`folder` is the folder dict of folders.py ({path, label, group, labelCustom}) of the bank the item lives in;
	`title`, `source` and `source_kind` say where an imported memory came from (see SOURCES)."""
	tags = sorted(tags or ())
	return {
		"id": make_id(backend, bank, kind, native),
		"backend": backend,
		"bank": bank,
		"kind": kind,
		"scope": scope,
		"scopeLabel": scope_label,
		"folder": folder,
		"title": title,
		"source": source,
		"sourceKind": source_kind,
		"text": text,
		"tags": tags,
		"createdAt": created_at,
		"updatedAt": updated_at,
		"editedAt": edited_at,
		"editable": editable,
		"readOnlyReason": read_only_reason,
		"deletable": deletable,
		"deleteReason": None if deletable else delete_reason,
		"deleteLabel": delete_label,
		"revisionCount": 0,
		"sizeBytes": size_bytes(text),
		"etag": etag(text, tags),
		"meta": meta,
	}


class Adapter(Protocol):
	"""One memory backend. Items are the normalised dicts of item_dict(); failures are MemoryError."""

	name: str

	def overview(self) -> dict:
		"""{"banks": [{bank, folder, counts: {global, project, folder, total}, bytes, sources?}]}; `folder` is the
		folder dict of folders.py, `sources` {omp, claude-code, hindsight, claude-mem: rows} (mnemopi)."""

	def list_items(self, bank, scope, kind, q, cursor, limit, source=None, group=None, tags=()):
		"""(items, next_cursor | None, total); items carry the FULL text (the service truncates).

		`source` keeps the rows of one SOURCES value, `group` those whose folder group matches, `tags` (up to
		TAG_FILTER_MAX exact tags) the rows that carry all of them."""

	def tags(self, bank, limit):
		"""[{tag, count}] over one bank (None: every bank), most used first, at most `limit`."""

	def get(self, bank, kind, native_id):
		"""One item with full text; MemoryError 404 when it is gone."""

	def write(self, item, text, tags):
		"""Replace text (and tags where the backend has them) in place; 409 when the stored etag is no longer item's."""

	def delete(self, item, save):
		"""Remove the item (mnemopi: the row and everything derived from it, in one transaction; Hindsight:
		invalidate, reversible); 409 when the stored etag moved.

		`save(native)` is called after the backend-native record is read and before anything is changed (mnemopi:
		inside the same transaction); it stores the trash snapshot and returns it.
		"""

	def recreate(self, snapshot):
		"""Put a deleted item back (mnemopi: re-insert the saved rows; Hindsight: mark it valid again, same id).

		snapshot = {id, text, tags, meta}. Returns the item."""

	def scan(self, bank):
		"""{"backend", "bank", "truncated", "items": [full-text items], "recalls": {...}} for the Optimize analysis.

		`recalls` = {"available": bool, "windowDays": int, "coveredDays": float | None}: whether the items' recall
		counters mean something (Hindsight: only with the proxy's recall log), and how many days the log spans."""

	def map(self, bank, limit):
		"""The memory map of one bank: {"nodes", "edges", "stats", "source", "generatedAt"} (see graph.build)."""

	def recalls(self, bank, limit):
		"""The recall activity of one bank: {"events", "top", "neverRecalled", "windowDays", "available", "note"}."""
