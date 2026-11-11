"""Import mnemopi working and episodic memories into the shared Hindsight banks.

Reads every `<label>-<hash>/mnemopi.db` under the mnemopi banks root through a read-only online backup, routes
each row by its cwd with a memory-banks.conf (routing.py; the zsh hook reads the same file), tags it
`project:<name>`, redacts it with the proxy's own code, scans what is left for residual secrets and dedupes
identical content within a bank.

Not sent: rows outside every bank root (`skip`), rows without a cwd whose mnemopi bank's cwd rows do not all
route to one bank (`review`, unless --include-review), superseded or expired rows (unless
--include-superseded) and --exclude-label labels.

Dry run by default: prints ids, routing, lengths and redaction/residual counts (never contents) and a digest of
the exact items to send. `--send --confirm <digest>` writes those items to a 0600 file, refuses on residual
findings or on rows the ledger says went to another bank, then posts each batch with its bank's overlay key
and records it in the ledger. `--only-new` limits the items to documents with a row not yet in the ledger with
the same bank and content; `--count-unsynced` only prints `unsynced=<n>`, the number of such rows.
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any, TextIO

if __package__ in (None, ""):
	sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hindsight_import import routing

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROOT = Path.home() / ".omp/agent/memories/mnemopi/banks"
DEFAULT_OVERLAY_DIR = Path.home() / ".omp/agent/memory-overlays"
LEDGER_NAME = "import-ledger.json"
TABLES = {"working_memory": "w", "episodic_memory": "e"}
COLUMNS = (
	"id",
	"content",
	"source",
	"timestamp",
	"created_at",
	"session_id",
	"importance",
	"metadata_json",
	"superseded_by",
	"valid_until",
)
BATCH_SIZE = 16
TIMEOUT = 60
MAX_RESPONSE = 1 << 20
SKIP_REASONS = ("skip", "review", "excluded-label", "superseded", "expired", "empty")
# Characters the proxy accepts in document ids and tags.
_UNSAFE_IDENT = re.compile(r"[^A-Za-z0-9:_.\-/@ ]")
_OPERATION_ID = re.compile(r"[A-Za-z0-9_.:\-]{1,100}")
# Decision (2026-11-09): the id segment of a claude.ai artifact link is a public share id, not a
# secret. Only this exact host and path, and only for the import's residual scan; the proxy is unchanged.
_ARTIFACT_ID = re.compile(r"https://claude\.ai/artifacts?/([A-Za-z0-9_-]{8,128})(?![A-Za-z0-9_-])")


def import_scan(text: str, scan: Scanner) -> list[tuple[str, int]]:
	"""`scan` with the ids of https://claude.ai/artifact/<id> links blanked out (same length, so offsets hold)."""
	return scan(
		_ARTIFACT_ID.sub(lambda m: m.group(0)[: m.start(1) - m.start(0)] + "x" * len(m.group(1)), text)
	)


EXIT_LEDGER = 3
EXIT_RESIDUAL = 4
EXIT_CONFIRM = 5

Redactor = Callable[[str], "tuple[str, collections.Counter[str]]"]
Scanner = Callable[[str], "list[tuple[str, int]]"]
Resolver = Callable[[Path], "Path | None"]


@dataclasses.dataclass
class Row:
	label: str
	table: str
	id: str
	bank: str | None
	tag: str
	content: str
	source: str | None
	session_id: str | None
	importance: str | None
	timestamp: str | None
	superseded: bool
	expired: bool
	redactions: collections.Counter[str]
	has_cwd: bool = True
	review: bool = False
	missing_cwd: bool = False
	residual: list[tuple[str, str, int]] = dataclasses.field(default_factory=list)

	@property
	def key(self) -> tuple[str, str, str]:
		return (self.label, self.table, self.id)

	@property
	def source_key(self) -> str:
		return f"{self.label}:{TABLES[self.table]}:{self.id}"


@dataclasses.dataclass
class Doc:
	row: Row
	tags: list[str]
	merged: int
	members: list[Row] = dataclasses.field(default_factory=list)
	document_id: str | None = None

	@property
	def sha(self) -> str:
		return content_hash(self.row.content)


@dataclasses.dataclass
class Ledger:
	"""What earlier runs sent: document_id -> bank/sha, content sha -> {bank: document_id}, source row -> bank."""

	documents: dict[str, dict[str, str]] = dataclasses.field(default_factory=dict)
	contents: dict[str, dict[str, str]] = dataclasses.field(default_factory=dict)
	sources: dict[str, str] = dataclasses.field(default_factory=dict)


Request = tuple[str, tuple[str, ...], list[Doc]]


def label_base(label: str) -> str:
	"""`customer-frontend-3tntylzrz3coh` -> `customer-frontend`."""
	return label.rsplit("-", 1)[0] if "-" in label else label


def safe_ident(text: str) -> str:
	return _UNSAFE_IDENT.sub("_", text)[:200]


def git_primary_root(cwd: Path) -> Path | None:
	"""Primary checkout root of the repo containing cwd (worktrees map to their main checkout)."""
	try:
		proc = subprocess.run(
			["git", "-C", str(cwd), "rev-parse", "--path-format=absolute", "--git-common-dir"],
			capture_output=True,
			text=True,
			timeout=10,
			check=False,
		)
	except (OSError, subprocess.SubprocessError):
		return None
	common = proc.stdout.strip()
	if proc.returncode != 0 or not common:
		return None
	path = Path(common)
	return path.parent if path.name == ".git" else path


def project_tag(cwd: str | None, label: str, resolver: Resolver) -> str:
	name = label_base(label)
	if cwd:
		path = Path(cwd)
		root = resolver(path) if path.is_dir() else None
		if root is not None and root.name:
			name = root.name
		elif path.name:
			name = path.name
	return safe_ident(f"project:{name.lower()}")


def label_bank(label: str, banks: routing.Banks) -> str | None:
	"""Bank whose root directory has the label's name (`alpha-39wp...` -> alpha), if exactly one."""
	base = label_base(label).casefold()
	found = {bank for bank, root in banks if Path(root).name.casefold() == base}
	return found.pop() if len(found) == 1 else None


def parse_time(value: Any) -> dt.datetime | None:
	"""Epoch seconds/milliseconds or ISO-8601 text -> aware UTC datetime; None if unusable."""
	if value is None or value == "":
		return None
	if isinstance(value, str):
		text = value.strip()
		try:
			value = float(text)
		except ValueError:
			if text.endswith(("Z", "z")):
				text = text[:-1] + "+00:00"
			try:
				parsed = dt.datetime.fromisoformat(text)
			except ValueError:
				return None
			return parsed.replace(tzinfo=dt.timezone.utc) if parsed.tzinfo is None else parsed
	if isinstance(value, (int, float)) and not isinstance(value, bool):
		seconds = float(value) / 1000 if abs(value) > 1e11 else float(value)
		try:
			return dt.datetime.fromtimestamp(seconds, dt.timezone.utc)
		except (OverflowError, OSError, ValueError):
			return None
	return None


def iso(moment: dt.datetime) -> str:
	return moment.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def read_tables(db: Path) -> list[dict[str, Any]]:
	"""All rows of both tables, from a private snapshot taken with SQLite's online backup of a read-only handle."""
	rows: list[dict[str, Any]] = []
	with tempfile.TemporaryDirectory(prefix="mnemopi-import-") as tmp:
		os.chmod(tmp, 0o700)
		copy = Path(tmp) / "snapshot.db"
		src = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
		try:
			dst = sqlite3.connect(copy)
			try:
				src.backup(dst)
			finally:
				dst.close()
		finally:
			src.close()
		conn = sqlite3.connect(f"{copy.as_uri()}?mode=ro", uri=True)
		try:
			for table in TABLES:
				present = {r[1] for r in conn.execute(f"PRAGMA table_info('{table}')")}
				if not present:
					continue
				select = ", ".join(c if c in present else f"NULL AS {c}" for c in COLUMNS)
				cursor = conn.execute(f"SELECT {select} FROM {table}")
				for values in cursor:
					rows.append({"table": table, **dict(zip(COLUMNS, values, strict=True))})
		finally:
			conn.close()
	return rows


def text_or_none(value: Any) -> str | None:
	return None if value is None else str(value)


def build_row(
	label: str,
	raw: dict[str, Any],
	home: Path,
	banks: routing.Banks,
	resolver: Resolver,
	redact: Redactor,
	scan: Scanner,
	now: dt.datetime,
) -> Row:
	try:
		meta = json.loads(raw["metadata_json"] or "{}")
	except (TypeError, ValueError):
		meta = {}
	cwd = meta.get("cwd") if isinstance(meta, dict) else None
	cwd = cwd if isinstance(cwd, str) and cwd else None
	counts: collections.Counter[str] = collections.Counter()

	def clean(value: Any) -> str | None:
		text = text_or_none(value)
		if text is None:
			return None
		redacted, found = redact(text)
		counts.update(found)
		return redacted

	route = routing.route(cwd, banks, home) if cwd else routing.Route(None, False)
	moment = parse_time(raw["created_at"]) or parse_time(raw["timestamp"])
	valid_until = parse_time(raw["valid_until"])
	importance = raw["importance"]
	row = Row(
		label=label,
		table=raw["table"],
		id=str(raw["id"]),
		bank=route.bank,
		tag=project_tag(cwd, label, resolver),
		content=clean(raw["content"]) or "",
		source=clean(raw["source"]),
		session_id=clean(raw["session_id"]),
		importance=None if importance is None else str(importance),
		timestamp=iso(moment) if moment else None,
		superseded=bool(raw["superseded_by"]),
		expired=valid_until is not None and valid_until < now,
		redactions=counts,
		has_cwd=cwd is not None,
		missing_cwd=route.missing_cwd,
	)
	for field in ("content", "source", "session_id", "tag"):
		text = getattr(row, field)
		if text:
			row.residual.extend((field, kind, offset) for kind, offset in scan(text))
	return row


def collect(
	root: Path,
	home: Path,
	banks: routing.Banks,
	resolver: Resolver,
	redact: Redactor,
	scan: Scanner,
	now: dt.datetime | None = None,
) -> tuple[list[Row], int]:
	"""Rows with content from every bank under root, and the number skipped for empty content."""
	now = now or dt.datetime.now(dt.timezone.utc)
	cache: dict[Path, Path | None] = {}

	def cached(path: Path) -> Path | None:
		if path not in cache:
			cache[path] = resolver(path)
		return cache[path]

	rows: list[Row] = []
	empty = 0
	for bank_dir in sorted(p for p in root.iterdir() if (p / "mnemopi.db").is_file()):
		bank_rows = []
		for raw in read_tables(bank_dir / "mnemopi.db"):
			row = build_row(bank_dir.name, raw, home, banks, cached, redact, scan, now)
			if row.content.strip():
				bank_rows.append(row)
			else:
				empty += 1
		inherit_routing(bank_rows, banks)
		rows.extend(bank_rows)
	return rows, empty


def inherit_routing(bank_rows: list[Row], banks: routing.Banks) -> None:
	"""Rows without a cwd follow their mnemopi bank's cwd rows when those all route to one (non-None) bank.

	They take that bank and the most common tag among those rows (ties: alphabetically first). Otherwise they
	are flagged for review with a tentative bank: the one bank among the cwd rows, or with no cwd rows at all
	the bank whose root has the label's name; never a default.
	"""
	with_cwd = [r for r in bank_rows if r.has_cwd]
	orphans = [r for r in bank_rows if not r.has_cwd]
	targets = {r.bank for r in with_cwd}
	if len(targets) == 1 and None not in targets:
		tags = collections.Counter(r.tag for r in with_cwd)
		tag = min(tags, key=lambda t: (-tags[t], t))
		for row in orphans:
			row.bank, row.tag = next(iter(targets)), tag
		return
	known = targets - {None}
	for row in orphans:
		if with_cwd:
			row.bank = next(iter(known)) if len(known) == 1 else None
		else:
			row.bank = label_bank(row.label, banks)
		row.review = True


def skip_reason(
	row: Row, include_review: bool, include_superseded: bool, exclude: Sequence[str]
) -> str | None:
	if row.bank is None and not row.review:
		return "skip"
	base = label_base(row.label).casefold()
	if any(base.startswith(prefix.casefold()) for prefix in exclude):
		return "excluded-label"
	if row.review and not include_review:
		return "review"
	if row.bank is None:
		return "skip"
	if not include_superseded:
		if row.superseded:
			return "superseded"
		if row.expired:
			return "expired"
	return None


def select(
	rows: Iterable[Row], include_review: bool, include_superseded: bool, exclude: Sequence[str]
) -> tuple[list[Row], list[tuple[Row, str]]]:
	"""Rows to send, and the others with the reason they are not sent."""
	chosen, skipped = [], []
	for row in rows:
		reason = skip_reason(row, include_review, include_superseded, exclude)
		if reason is None:
			chosen.append(row)
		else:
			skipped.append((row, reason))
	return chosen, skipped


def content_hash(text: str) -> str:
	return hashlib.sha256(text.encode("utf-8")).hexdigest()


def dedupe(rows: Iterable[Row]) -> list[Doc]:
	"""One document per (bank, sha256 of redacted content): lowest (label, table, id) wins, tags unioned."""
	groups: dict[tuple[str | None, str], list[Row]] = {}
	for row in rows:
		groups.setdefault((row.bank, content_hash(row.content)), []).append(row)
	docs = []
	for members in groups.values():
		members.sort(key=lambda r: r.key)
		docs.append(Doc(members[0], sorted({m.tag for m in members}), len(members), members))
	docs.sort(key=lambda d: (d.row.bank or "", d.row.key))
	return docs


def default_document_id(row: Row) -> str:
	return safe_ident(f"mnemopi:{row.bank}:{row.label}:{TABLES[row.table]}:{row.id}")


def load_ledger(path: Path) -> Ledger:
	if not path.exists():
		return Ledger()
	data = json.loads(path.read_text())
	if not isinstance(data, dict):
		raise ValueError(f"{path}: not a ledger")
	ledger = Ledger(data.get("documents", {}), data.get("contents", {}), data.get("sources", {}))
	if not all(isinstance(v, dict) for v in (ledger.documents, ledger.contents, ledger.sources)):
		raise ValueError(f"{path}: not a ledger")
	return ledger


def save_ledger(path: Path, ledger: Ledger) -> None:
	"""Atomic 0600 write: temp file in the same directory, then rename."""
	path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
	fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
	try:
		os.fchmod(fd, 0o600)
		with os.fdopen(fd, "w") as fh:
			json.dump(dataclasses.asdict(ledger), fh, sort_keys=True, indent=1)
			fh.flush()
			os.fsync(fh.fileno())
		os.replace(tmp, path)
	except BaseException:
		Path(tmp).unlink(missing_ok=True)
		raise


def record(ledger: Ledger, docs: Iterable[Doc]) -> None:
	for doc in docs:
		bank, sha = doc.row.bank or "", doc.sha
		doc_id = doc.document_id or default_document_id(doc.row)
		ledger.documents[doc_id] = {"bank": bank, "sha": sha}
		ledger.contents.setdefault(sha, {})[bank] = doc_id
		for member in doc.members or [doc.row]:
			ledger.sources[member.source_key] = bank


def synced(row: Row, ledger: Ledger) -> bool:
	"""The ledger has this source row in its bank, and its (redacted) content in that bank."""
	bank = row.bank or ""
	return ledger.sources.get(row.source_key) == bank and bank in ledger.contents.get(
		content_hash(row.content), {}
	)


def apply_ledger(
	docs: list[Doc], ledger: Ledger, skipped: Iterable[tuple[Row, str]] = ()
) -> list[tuple[Row, str, str]]:
	"""Reuse earlier document ids per (content, bank); return (row, earlier bank, why) for cross-bank moves.

	A row sent earlier that now routes to no bank (`skip`) is a move too: its copy stays in the earlier bank.
	"""
	defaults = collections.Counter(default_document_id(d.row) for d in docs)
	for doc in docs:
		own = default_document_id(doc.row)
		earlier = ledger.contents.get(doc.sha, {}).get(doc.row.bank or "")
		taken = defaults[earlier] - (earlier == own) if earlier else 0
		doc.document_id = earlier if earlier and not taken else own
	conflicts: list[tuple[Row, str, str]] = []
	used = collections.Counter(d.document_id for d in docs)
	run_banks: dict[str, set[str]] = collections.defaultdict(set)
	for doc in docs:
		run_banks[doc.sha].add(doc.row.bank or "")
	for doc in docs:
		bank = doc.row.bank or ""
		for member in doc.members or [doc.row]:
			before = ledger.sources.get(member.source_key)
			if before is not None and before != bank:
				conflicts.append((member, before, "source"))
		before_banks = set(ledger.contents.get(doc.sha, {}))
		if before_banks - run_banks[doc.sha] and run_banks[doc.sha] - before_banks:
			conflicts.append((doc.row, ",".join(sorted(before_banks)), "content"))
		previous = ledger.documents.get(doc.document_id or "")
		if previous is not None and previous.get("bank") != bank:
			conflicts.append((doc.row, previous.get("bank", "?"), "document"))
		if used[doc.document_id] > 1:
			conflicts.append((doc.row, bank, "duplicate-id"))
	for row, reason in skipped:
		before = ledger.sources.get(row.source_key)
		if reason == "skip" and before is not None:
			conflicts.append((row, before, "unrouted"))
	return conflicts


def to_item(doc: Doc) -> dict[str, Any]:
	row = doc.row
	metadata = {
		"source": row.source,
		"importance": row.importance,
		"session_id": row.session_id,
		"mnemopi_table": row.table,
		"superseded": "true" if row.superseded else "false",
	}
	item: dict[str, Any] = {
		"content": row.content,
		"context": f"mnemopi import: {row.source or 'unknown'}",
		"document_id": doc.document_id or default_document_id(row),
		"tags": list(doc.tags),
		"metadata": {k: str(v) for k, v in metadata.items() if v is not None},
	}
	if row.timestamp:
		item["timestamp"] = row.timestamp
	return item


def batches(docs: Iterable[Doc]) -> list[Request]:
	"""One request per (bank, tag set), at most BATCH_SIZE documents each, in a stable order."""
	groups: dict[tuple[str, tuple[str, ...]], list[Doc]] = {}
	for doc in docs:
		groups.setdefault((doc.row.bank or "", tuple(doc.tags)), []).append(doc)
	out = []
	for (bank, tags), members in sorted(groups.items()):
		for start in range(0, len(members), BATCH_SIZE):
			out.append((bank, tags, members[start : start + BATCH_SIZE]))
	return out


def payload(requests: list[Request]) -> list[dict[str, Any]]:
	return [{"bank": bank, "items": [to_item(d) for d in docs]} for bank, _, docs in requests]


def digest(requests: list[Request]) -> str:
	"""sha256 of the canonical JSON of every item to send, with its bank."""
	canonical = json.dumps(payload(requests), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
	return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def write_redacted(path: Path, requests: list[Request], force: bool) -> None:
	"""JSONL, one `{"bank", "item"}` per line, created 0600; refuses an existing file unless force."""
	path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
	flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | (os.O_TRUNC if force else os.O_EXCL)
	fd = os.open(path, flags, 0o600)
	os.fchmod(fd, 0o600)
	with os.fdopen(fd, "w") as fh:
		for entry in payload(requests):
			for item in entry["items"]:
				fh.write(
					json.dumps({"bank": entry["bank"], "item": item}, sort_keys=True, ensure_ascii=False)
				)
				fh.write("\n")


def fmt_counts(counts: collections.Counter[str]) -> str:
	return ",".join(f"{k}={v}" for k, v in sorted(counts.items()) if v) or "none"


def residual_counts(rows: Iterable[Row]) -> collections.Counter[str]:
	return collections.Counter(kind for row in rows for _, kind, _ in row.residual)


def report(
	rows: list[Row],
	docs: list[Doc],
	skipped: list[tuple[Row, str]],
	empty: int,
	conflicts: list[tuple[Row, str, str]],
	requests: list[Request],
	out: TextIO,
	sync: tuple[int, int],
) -> None:
	reasons = {r.key: reason for r, reason in skipped}
	for row in sorted(rows, key=lambda r: r.key):
		flags = ",".join(f for f, on in (("missing-cwd", row.missing_cwd), ("review", row.review)) if on)
		print(
			f"{row.label}\t{row.table}\t{row.id}\t-> {row.bank or '-'}\t{row.tag}\t"
			f"len={len(row.content)}\tredactions={fmt_counts(row.redactions)}\t"
			f"residual={fmt_counts(residual_counts([row]))}\t{reasons.get(row.key, 'send')}"
			+ (f"\tflags={flags}" if flags else ""),
			file=out,
		)
	for row in sorted((r for r in rows if r.review), key=lambda r: r.key):
		print(f"review:\t{row.label}\t{row.table}\t{row.id}\t-> {row.bank or '-'}\t{row.tag}", file=out)
	for row, reason in sorted(skipped, key=lambda s: s[0].key):
		print(f"skip:\t{row.label}\t{row.table}\t{row.id}\t{reason}", file=out)
	for row in sorted(rows, key=lambda r: r.key):
		for field, kind, offset in row.residual:
			print(f"residual:\t{row.label}\t{row.table}\t{row.id}\t{kind}\t{field}:{offset}", file=out)
	for row, before, why in conflicts:
		print(
			f"ledger-conflict:\t{row.label}\t{row.table}\t{row.id}\t-> {row.bank or '-'}\t"
			f"earlier={before}\t{why}",
			file=out,
		)
	print("== summary ==", file=out)
	per_source = collections.Counter((r.label, r.table) for r in rows)
	for (label, table), n in sorted(per_source.items()):
		print(f"source\t{label}\t{table}\t{n}", file=out)
	per_target = collections.Counter((d.row.bank, ",".join(d.tags)) for d in docs)
	for (bank, tags), n in sorted(per_target.items()):
		print(f"target\t{bank}\t{tags}\t{n}", file=out)
	total: collections.Counter[str] = collections.Counter()
	for row in rows:
		total.update(row.redactions)
	per_table = collections.Counter(r.table for r in rows)
	sent_rows = [m for d in docs for m in d.members or [d.row]]
	print(
		f"rows={len(rows)} (working_memory={per_table['working_memory']}, "
		f"episodic_memory={per_table['episodic_memory']}) to_send={len(sent_rows)} after_dedupe={len(docs)} "
		f"superseded={sum(r.superseded for r in rows)} expired={sum(r.expired for r in rows)} "
		f"review={sum(r.review for r in rows)} missing_cwd={sum(r.missing_cwd for r in rows)}",
		file=out,
	)
	print(f"already_synced={sync[0]} unsynced={sync[1]}", file=out)
	why = collections.Counter(reason for _, reason in skipped)
	why["empty"] += empty
	print("skipped: " + " ".join(f"{reason}={why[reason]}" for reason in SKIP_REASONS), file=out)
	print(f"redactions: {fmt_counts(total)}", file=out)
	print(f"residual: {fmt_counts(residual_counts(sent_rows))}", file=out)
	print(f"residual_not_sent: {fmt_counts(residual_counts(r for r, _ in skipped))}", file=out)
	print(f"ledger_conflicts={len(conflicts)}", file=out)
	print(f"requests={len(requests)} items={sum(len(d) for _, _, d in requests)}", file=out)
	print(f"digest={digest(requests)}", file=out)


def check_api_url(url: str) -> None:
	parts = urllib.parse.urlsplit(url)
	if parts.scheme == "https" and parts.hostname:
		return
	if parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost"):
		return
	raise ValueError("hindsight.apiUrl must be https:// or http://127.0.0.1 / http://localhost")


def load_overlay(path: Path) -> tuple[str, str]:
	import yaml

	fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
	with os.fdopen(fd) as fh:
		st = os.fstat(fh.fileno())
		if st.st_uid != os.getuid():
			raise ValueError(f"{path}: not owned by the current user")
		if st.st_mode & 0o077:
			raise ValueError(f"{path}: mode {st.st_mode & 0o777:o}, must be 0600")
		text = fh.read()
	try:
		data = yaml.safe_load(text)
	except yaml.YAMLError:
		raise ValueError(f"{path}: not valid YAML") from None
	hindsight = data.get("hindsight") if isinstance(data, dict) else None
	url = hindsight.get("apiUrl") if isinstance(hindsight, dict) else None
	token = hindsight.get("apiToken") if isinstance(hindsight, dict) else None
	if not isinstance(url, str) or not url or not isinstance(token, str) or not token:
		raise ValueError(f"{path}: hindsight.apiUrl and hindsight.apiToken are required")
	try:
		check_api_url(url)
	except ValueError as err:
		raise ValueError(f"{path}: {err}") from None
	return url.rstrip("/"), token


class NoRedirect(urllib.request.HTTPRedirectHandler):
	"""Never follow a redirect: the 3xx surfaces as an HTTPError, so the bearer is never re-sent elsewhere."""

	def redirect_request(self, req, fp, code, msg, headers, newurl):
		return None


def build_opener() -> urllib.request.OpenerDirector:
	return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def operation_ids(body: bytes) -> list[str]:
	"""`operation_id` / `operation_ids` values from a JSON response, id-shaped strings only."""
	try:
		data = json.loads(body)
	except ValueError:
		return []
	found: list[str] = []

	def walk(value: Any) -> None:
		if isinstance(value, dict):
			for key, inner in value.items():
				if key in ("operation_id", "operation_ids"):
					for op in inner if isinstance(inner, list) else [inner]:
						if isinstance(op, str) and _OPERATION_ID.fullmatch(op):
							found.append(op)
				else:
					walk(inner)
		elif isinstance(value, list):
			for inner in value:
				walk(inner)

	walk(data)
	return found


def send(
	requests: list[Request],
	overlays: dict[str, tuple[str, str]],
	opener: Any,
	out: TextIO,
	on_success: Callable[[list[Doc]], None] = lambda docs: None,
) -> int:
	"""POST each batch; stop at the first failure (any non-2xx, redirects included). Returns the exit code."""
	for bank, tags, docs in requests:
		url, token = overlays[bank]
		req = urllib.request.Request(
			f"{url}/v1/default/banks/{bank}/memories",
			data=json.dumps({"async": True, "items": [to_item(d) for d in docs]}).encode("utf-8"),
			method="POST",
			headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
		)
		label = f"POST bank={bank} tags={','.join(tags)} items={len(docs)}"
		body = b""
		try:
			resp = opener.open(req, timeout=TIMEOUT)
			try:
				status = resp.status
				if 200 <= status < 300:
					body = resp.read(MAX_RESPONSE)
			finally:
				resp.close()
		except urllib.error.HTTPError as err:
			status = err.code
			err.close()
		except (urllib.error.URLError, OSError) as err:
			print(f"{label} -> error {type(err).__name__}", file=out)
			return 1
		if not 200 <= status < 300:
			print(f"{label} -> {status}", file=out)
			return 1
		print(f"{label} -> {status} operations={','.join(operation_ids(body)) or 'none'}", file=out)
		on_success(docs)
	print(f"sent requests={len(requests)} items={sum(len(d) for _, _, d in requests)}", file=out)
	return 0


def main(
	argv: list[str] | None = None,
	*,
	resolver: Resolver = git_primary_root,
	redact: Redactor | None = None,
	scan: Scanner | None = None,
	opener: Any = None,
	home: Path | None = None,
	now: dt.datetime | None = None,
	out: TextIO = sys.stdout,
) -> int:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument("--send", action="store_true", help="post to Hindsight (default: dry run)")
	parser.add_argument(
		"--confirm", metavar="DIGEST", help="digest printed by the dry run (required by --send)"
	)
	parser.add_argument("--overlay-dir", type=Path, default=DEFAULT_OVERLAY_DIR)
	parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
	parser.add_argument("--ledger", type=Path, help=f"default: <overlay-dir>/{LEDGER_NAME}")
	parser.add_argument("--include-review", action="store_true", help="also send rows flagged review")
	parser.add_argument("--include-superseded", action="store_true", help="also send superseded/expired rows")
	parser.add_argument(
		"--exclude-label",
		action="append",
		default=[],
		metavar="LABEL",
		help="skip mnemopi labels with this prefix",
	)
	parser.add_argument("--write-redacted", type=Path, metavar="FILE", help="write the items as 0600 JSONL")
	parser.add_argument("--force", action="store_true", help="overwrite an existing --write-redacted file")
	parser.add_argument(
		"--only-new",
		action="store_true",
		help="only documents with a row not yet in the ledger with the same bank and content",
	)
	parser.add_argument(
		"--count-unsynced",
		action="store_true",
		help="print only unsynced=<n> (rows to send that the ledger lacks) and exit",
	)
	args = parser.parse_args(argv)
	if args.count_unsynced and (args.send or args.write_redacted):
		parser.error("--count-unsynced takes no --send or --write-redacted")
	home = home or Path.home()
	root = args.root.expanduser()
	if not root.is_dir():
		print(f"no mnemopi banks root at {root}", file=sys.stderr)
		return 2
	if redact is None or scan is None:
		sys.path.insert(0, str(REPO_ROOT))
		from hindsight_proxy.redact import redact_text, residual_findings

		redact = redact or redact_text
		scan = scan or (lambda text: import_scan(text, residual_findings))
	if args.count_unsynced:
		# Counting needs the redacted content's hash only: no residual scan, no git lookups for tags.
		scan = lambda text: []  # noqa: E731
		resolver = lambda path: None  # noqa: E731
	overlay_dir = args.overlay_dir.expanduser()
	ledger_path = (args.ledger or overlay_dir / LEDGER_NAME).expanduser()
	try:
		ledger = load_ledger(ledger_path)
	except (OSError, ValueError) as err:
		print(f"ledger: {type(err).__name__}: {err}", file=sys.stderr)
		return 2
	banks = routing.load_banks()
	rows, empty = collect(root, home, banks, resolver, redact, scan, now)
	chosen, skipped = select(rows, args.include_review, args.include_superseded, args.exclude_label)
	unsynced = sum(not synced(row, ledger) for row in chosen)
	if args.count_unsynced:
		print(f"unsynced={unsynced}", file=out)
		return 0
	docs = dedupe(chosen)
	conflicts = apply_ledger(docs, ledger, skipped)
	if args.only_new:
		docs = [d for d in docs if not all(synced(m, ledger) for m in d.members or [d.row])]
	requests = batches(docs)
	report(rows, docs, skipped, empty, conflicts, requests, out, (len(chosen) - unsynced, unsynced))
	current = digest(requests)

	redacted_path = args.write_redacted
	if not args.send:
		if redacted_path is None:
			return 0
	elif args.confirm != current:
		print("refused: --send needs --confirm equal to the digest above; nothing sent", file=out)
		return EXIT_CONFIRM
	elif redacted_path is None:
		stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
		redacted_path = home / "Backups" / f"hindsight-import-{stamp}.jsonl"
	try:
		write_redacted(redacted_path.expanduser(), requests, args.force)
	except FileExistsError:
		print(f"refused: {redacted_path} exists (use --force); nothing sent", file=sys.stderr)
		return 2
	print(f"redacted items written: {redacted_path}", file=out)
	if not args.send:
		return 0
	if conflicts:
		print(f"refused: {len(conflicts)} ledger conflict(s) above; nothing sent", file=out)
		return EXIT_LEDGER
	if residual_counts(m for d in docs for m in d.members or [d.row]):
		print("refused: residual findings in items to send; nothing sent", file=out)
		return EXIT_RESIDUAL
	overlays = {}
	for bank in sorted({bank for bank, _, _ in requests}):
		try:
			overlays[bank] = load_overlay(overlay_dir / f"{bank}.yml")
		except (OSError, ValueError) as err:
			print(f"overlay for {bank}: {type(err).__name__}: {err}", file=sys.stderr)
			return 2

	def on_success(sent: list[Doc]) -> None:
		record(ledger, sent)
		save_ledger(ledger_path, ledger)

	return send(requests, overlays, opener or build_opener(), out, on_success)


if __name__ == "__main__":
	sys.exit(main())
