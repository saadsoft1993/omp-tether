"""Hindsight adapter: the shared banks on a Hindsight server, read through the redacting proxy (stdlib urllib only).

Connection settings come from the omp overlays, ~/.omp/agent/memory-overlays/<name>.yml (0600): `hindsight.apiUrl`,
`hindsight.apiToken`, `hindsight.bankId`, `hindsight.requestTimeoutMs`. The token is only ever used as the bearer
header: it is not logged, not put in an error message and not returned.

What the proxy allows (hindsight_proxy/proxy.py ROUTES): bank PUT, POST /memories (retain), /memories/recall,
/reflect, GET /memories/list (limit, offset, type, q, consolidation_state), GET /memories/{id}, PATCH /memories/{id}
(`text`, or `state` "valid"/"invalidated" with a `reason`), GET /graph, documents, mental-models and operations,
plus GET /omp/recalls, which the proxy answers itself from its recall log. There is no DELETE route.

Editing is gated by a capability switch, `edit_enabled` (enabled by the service that uses this adapter), because the PATCH route
only exists on the proxy once it has been deployed to the server that runs it, and the proxy only routes it for the banks in its
HINDSIGHT_PROXY_EDIT_BANKS. Off (the default): items are editable=false and deletable=false with a reason. On:
world/experience memories are editable; write() sends PATCH {text} through the proxy. Derived observations and
invalidated memories stay read-only (with their own reason). Tags are fixed when a memory is retained, so an edit
that changes tags is a 400. The proxy's own "not found" answer to the PATCH is a 502 carrying the "deploy the route"
hint (any other 404 means the memory is gone or not editable); there is no silent fallback and never a delete +
re-retain (retain re-runs extraction and mints a new fact).

Delete is **invalidate** (deleteLabel "Invalidate"): PATCH {state: "invalidated", reason}. Hindsight keeps the memory
for audit and takes it out of recall; the id never changes. Undelete is the reverse, PATCH {state: "valid"}, and
returns the same id. No memory is ever removed.

An edit or invalidate whose PATCH fails in a way that does not say whether it applied (timeout, 5xx, a lost or
non-JSON answer) is resolved by re-reading the memory: an edit compares the text (requested text means applied, old
text means not applied), an invalidate or undelete compares the state. Anything else leaves the revision snapshot in
place and answers 502 "outcome uncertain". Only a definitive refusal (4xx, connection refused) drops the snapshot.

Recall counts (meta.recallCount / lastRecalledAt) come from the proxy's GET /omp/recalls, cached for RECALL_TTL_S. A
proxy without that route (or any failure) gives null, which the page shows as "n/a": it is never an error. The map
is built from the proxy-forwarded GET /graph (Cytoscape shape, see map()).

Scope mapping: an item with no tag is "global"; a `project:<name>` tag makes it "project" (folder = <name>); the
bank itself groups folders (a memory-banks.conf), so Hindsight has no separate "folder" level.
"""

import http.client
import json
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from . import graph
from .folders import group_name, named
from .model import (
	RECALL_WINDOW_DAYS,
	SOURCES,
	TAG_MAX_CHARS,
	TAGS_TOP,
	MemoryError,
	item_dict,
	make_id,
	parse_id,
	to_iso,
	top_tags,
	validate_text,
)

PAGE = 500  # rows per list request (timed against the live proxy: 500-row pages in parallel beat 1000)
CACHE_TTL_S = 120
SCAN_MAX_ROWS = 20000
DEFAULT_TIMEOUT_S = 10
WRITE_TIMEOUT_S = (
	30  # state-changing PATCH calls (edit / invalidate / restore) may be slow on a loaded server
)
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
EDIT_DISABLED_REASON = (
	"Hindsight edit is switched off (the service that uses this adapter has to enable it)"
)
PROXY_ROUTE_MISSING_REASON = (
	"Hindsight edit is not enabled: deploy the proxy PATCH route, then enable editing in the service that uses this adapter"
)
OBSERVATION_REASON = "Hindsight memory is a derived observation; edit the source facts"
INVALIDATED_REASON = "Hindsight memory is invalidated"
INVALIDATE_DISABLED_REASON = (
	"Hindsight invalidate is switched off (the service that uses this adapter has to enable it)"
)
PROXY_STATE_ROUTE_MISSING_REASON = (
	"Hindsight invalidate is not enabled: deploy the proxy PATCH route, then enable editing in the service that uses this adapter"
)
OBSERVATION_DELETE_REASON = "Hindsight memory is a derived observation; invalidate the source facts"
ALREADY_INVALID_REASON = "Hindsight memory is already invalidated"
INVALIDATE_REASON = "invalidated from the omp hub dashboard"
STATE_UNCERTAIN_REASON = (
	"Hindsight did not confirm the change and the memory's state could not be checked: the outcome is "
	"uncertain. Reload the memory before trying again"
)
RECALL_TTL_S = 60  # how long the proxy's recall summary is reused
RECALL_FAIL_TTL_S = 15  # how long "no recall data" is reused before the proxy is asked again
RECALL_EVENTS = 500  # events asked of the proxy (its maximum); counts always cover the whole log
GRAPH_LIMIT = 1000  # nodes asked of Hindsight's graph endpoint (its default); the hub picks the best <= 400
GONE_REASON = "Hindsight memory not found or no longer editable; refresh the list"
RECALL_REFRESH_AGE_S = (
	30  # a cached list older than this is refetched before recalls() calls an id "regenerated"
)
DERIVED_GONE_LABEL = "derived observation (regenerated)"
UNCERTAIN_REASON = (
	"Hindsight did not confirm the edit and the memory could not be checked: the outcome is uncertain. "
	"The previous text is kept in the revisions; reload the memory before trying again"
)
TAGS_FIXED = "Hindsight tags cannot be changed after a memory is retained; edit the text only"
SYSTEM_CA_FILE = "/etc/ssl/cert.pem"  # the macOS system bundle
_BANK = re.compile(r"[A-Za-z0-9_.-]{1,80}")
_NATIVE = re.compile(r"[A-Za-z0-9_.-]{1,200}")
_CURSOR = re.compile(r"[0-9]{1,9}")
_PROXY_NOT_FOUND = {"detail": "not found"}  # the proxy's own 404 body (hindsight_proxy/proxy.py _error)
_PROJECT_TAG = "project:"
_LOAD = object()  # "fetch the recall summary yourself" for HindsightAdapter._item


def parse_overlay(text):
	"""{"apiUrl", "apiToken", "bankId", "requestTimeoutMs"} (those present) from an overlay's `hindsight:` block.

	The overlays are two-level YAML written by hindsight/make-overlays.py: `key: value` lines, values plain or
	quoted, nested one indent deep. Anything else is ignored.
	"""
	out, inside = {}, False
	for raw in text.splitlines():
		if not raw.strip() or raw.lstrip().startswith("#"):
			continue
		indented = raw[0] in " \t"
		key, sep, value = raw.strip().partition(":")
		if not indented:
			inside = key == "hindsight" and not value.strip()
			continue
		if not inside or not sep:
			continue
		value = value.strip()
		if value[:1] in "\"'" and value.endswith(value[:1]) and len(value) >= 2:
			value = value[1:-1]
		else:
			value = value.split(" #", 1)[0].strip()
		out[key.strip()] = value
	return out


class HindsightBank:
	"""One overlay: where a bank lives and how to talk to it."""

	def __init__(self, bank, url, token, timeout_s):
		self.bank, self.url, self.token, self.timeout_s = bank, url.rstrip("/"), token, timeout_s

	def __repr__(self):  # never print the token
		return f"HindsightBank({self.bank!r})"


def load_overlays(overlay_dir):
	"""{bank: HindsightBank} for every overlay that selects the hindsight backend with a URL, token and bank."""
	banks = {}
	for path in sorted(Path(overlay_dir).glob("*.yml")):
		try:
			cfg = parse_overlay(path.read_text())
		except OSError:
			continue
		bank, url, token = cfg.get("bankId"), cfg.get("apiUrl"), cfg.get("apiToken")
		if not (bank and url and token) or not _BANK.fullmatch(bank) or bank in (".", ".."):
			continue
		try:
			timeout_s = max(1.0, int(cfg.get("requestTimeoutMs", "")) / 1000)
		except ValueError:
			timeout_s = DEFAULT_TIMEOUT_S
		banks[bank] = HindsightBank(bank, url, token, timeout_s)
	return banks


def load_bank_folders(conf_path):
	"""{bank: [folder basenames]} from a memory-banks.conf (`<bank> <root relative to $HOME>` lines)."""
	out = {}
	try:
		lines = Path(conf_path).read_text().splitlines()
	except OSError:
		return out
	for line in lines:
		parts = line.split("#", 1)[0].split()
		if len(parts) == 2:
			out.setdefault(parts[0], []).append(Path(parts[1]).name)
	return out


class _NoRedirect(urllib.request.HTTPRedirectHandler):
	"""Never follow a redirect: it would carry the bearer token to wherever the answer points."""

	def redirect_request(self, *args, **kwargs):
		return None


def _err(status, message, uncertain=False):
	"""A MemoryError; `uncertain` marks a failed write whose effect on the server is unknown."""
	error = MemoryError(status, message)
	error.uncertain = uncertain
	return error


def tls_context(default_cafile=None, system_cafile=SYSTEM_CA_FILE, certifi_where=None):
	"""The ssl context for Hindsight HTTPS; verification is always on.

	A Python that was built without a CA bundle of its own (the hub's launchd /usr/local/bin/python3 reports
	ssl.get_default_verify_paths().cafile None) cannot verify anything with the default context. In that case the
	macOS system bundle is used, else certifi's when it is installed. SSL_CERT_FILE in the environment is honoured
	by the default context itself. The arguments are only there for tests; None reads the real ones.
	"""
	if default_cafile is None:
		default_cafile = ssl.get_default_verify_paths().cafile
	if default_cafile and Path(default_cafile).is_file():
		return ssl.create_default_context()
	if Path(system_cafile).is_file():
		return ssl.create_default_context(cafile=system_cafile)
	where = certifi_where
	if where is None:
		try:
			import certifi

			where = certifi.where
		except ImportError:
			where = None
	if where is not None and Path(where()).is_file():
		return ssl.create_default_context(cafile=where())
	return ssl.create_default_context()


class HindsightAdapter:
	name = "hindsight"

	def __init__(self, banks, bank_folders=None, opener=None, edit_enabled=False, group_names=None):
		"""banks: {bank: HindsightBank}; opener: urllib opener (tests); bank_folders: {bank: [folder names]};
		edit_enabled: the proxy PATCH route is deployed (editing enabled by the caller), so items are editable;
		group_names: {lowercase bank name: group display name} (default: the bank name is the group)."""
		self.group_names = group_names
		self.edit_enabled = bool(edit_enabled)
		self.banks = banks
		self.bank_folders = bank_folders or {}
		self._opener = opener or urllib.request.build_opener(
			_NoRedirect, urllib.request.HTTPSHandler(context=tls_context())
		)
		self._cache = {}  # bank -> (fetched_at, [raw rows])
		self._cache_lock = threading.Lock()
		self._bank_locks = {}
		self._recall_cache = {}  # bank -> (valid_until, parsed recall summary | None)
		self._recall_lock = threading.Lock()

	# -- HTTP --------------------------------------------------------------------------------------------------
	def _bank(self, bank):
		cfg = self.banks.get(bank)
		if cfg is None:
			raise MemoryError(404, f"no hindsight bank {bank}")
		return cfg

	def _get_json(self, cfg, path, query):
		return self._request(cfg, "GET", path, query)

	def _request(self, cfg, method, path, query=None, body=None):
		"""One JSON request; MemoryError on any failure. For a write, `.uncertain` tells whether it may have applied."""
		write = method != "GET"
		url = f"{cfg.url}/v1/default/banks/{urllib.parse.quote(cfg.bank, safe='')}{path}"
		if query:
			url += "?" + urllib.parse.urlencode(query)
		headers = {
			"Authorization": f"Bearer {cfg.token}",
			"Accept": "application/json",
			"User-Agent": "omp-memory-adapter",
		}
		data = None
		if body is not None:
			data = json.dumps(body, ensure_ascii=True).encode()
			headers["Content-Type"] = "application/json"
		req = urllib.request.Request(url, data=data, headers=headers, method=method)
		timeout = max(cfg.timeout_s, WRITE_TIMEOUT_S) if write else cfg.timeout_s
		timed_out = _err(502, f"Hindsight did not answer within {timeout:g}s (bank {cfg.bank})", write)
		try:
			with self._opener.open(req, timeout=timeout) as resp:
				raw = resp.read(MAX_RESPONSE_BYTES + 1)
		except urllib.error.HTTPError as e:
			if e.code in (401, 403):
				raise _err(502, f"Hindsight rejected the hub's credentials for bank {cfg.bank}") from None
			if e.code == 404:
				not_found = _err(404, f"Hindsight has no such bank or memory ({cfg.bank})")
				not_found.proxy_route_missing = self._json_body(e) == _PROXY_NOT_FOUND
				raise not_found from None
			if write and e.code in (400, 413, 415, 422):
				raise _err(400, f"Hindsight refused the change (HTTP {e.code}) for bank {cfg.bank}") from None
			raise _err(
				502, f"Hindsight answered HTTP {e.code} for bank {cfg.bank}", write and e.code >= 500
			) from None
		except TimeoutError:
			raise timed_out from None
		except http.client.HTTPException:  # IncompleteRead, BadStatusLine ...: the answer was cut off
			raise _err(502, f"Hindsight's answer was cut off (bank {cfg.bank})", write) from None
		except (urllib.error.URLError, OSError) as e:
			reason = getattr(e, "reason", e)
			if isinstance(reason, TimeoutError):
				raise timed_out from None
			refused = isinstance(reason, ConnectionRefusedError)
			raise _err(
				502,
				f"Hindsight is unreachable ({type(reason).__name__}) for bank {cfg.bank}",
				write and not refused,
			) from None
		if len(raw) > MAX_RESPONSE_BYTES:
			raise _err(502, "Hindsight answered with an unexpectedly large body", write)
		try:
			parsed = json.loads(raw)
		except ValueError:
			raise _err(502, "Hindsight answered with something that is not JSON", write) from None
		if not isinstance(parsed, dict):
			raise _err(502, "Hindsight answered with an unexpected shape", write)
		return parsed

	@staticmethod
	def _json_body(error):
		try:
			return json.loads(error.read(4096))
		except (ValueError, OSError, http.client.HTTPException):
			return None

	def _page(self, cfg, offset, limit):
		data = self._get_json(cfg, "/memories/list", {"limit": limit, "offset": offset})
		items = data.get("items")
		if not isinstance(items, list):
			raise MemoryError(502, "Hindsight answered with an unexpected shape")
		return items, data.get("total", len(items))

	def _fetch_all(self, cfg):
		"""Every raw row of a bank: one cheap request tells the total, then every page is fetched in parallel."""
		_, total = self._page(cfg, 0, 1)
		offsets = list(range(0, min(int(total), SCAN_MAX_ROWS), PAGE))
		rows = []
		if offsets:
			with ThreadPoolExecutor(max_workers=min(6, len(offsets))) as pool:
				for items, _ in pool.map(lambda off: self._page(cfg, off, PAGE), offsets):
					rows.extend(items)
		return rows, int(total)

	def _rows(self, bank, fresh=False):
		"""(raw rows, total) of a bank, cached for CACHE_TTL_S so paging and filtering cost one fetch."""
		cfg = self._bank(bank)
		with self._cache_lock:
			lock = self._bank_locks.setdefault(bank, threading.Lock())
		with lock:
			hit = self._cache.get(bank)
			if hit and not fresh and time.monotonic() - hit[0] < CACHE_TTL_S:
				return hit[1], hit[2]
			rows, total = self._fetch_all(cfg)
			self._cache[bank] = (time.monotonic(), rows, total)
			return rows, total

	def warm(self):
		"""Fill the cache of every bank (a background thread at startup); failures are left for the first request."""
		for bank in self.banks:
			try:
				self._rows(bank)
			except MemoryError:
				pass

	def source_info(self):
		"""What the Sources page says about Hindsight: {available, counts, note}. Cache only, never a request."""
		with self._cache_lock:
			totals = {bank: hit[2] for bank, hit in self._cache.items() if bank in self.banks}
		counts = {"banks": len(self.banks)}
		note = None
		if len(totals) == len(self.banks):
			counts["memories"] = sum(totals.values())
		else:
			note = "memory counts appear once the banks have been loaded (open Browse)"
		return {"available": bool(self.banks), "counts": counts, "note": note}

	# -- rows -> items -----------------------------------------------------------------------------------------
	def _folder(self, bank, project=None):
		"""The folder dict of a bank (its conf folders), or of one `project:<name>` inside it. Hindsight has no paths."""
		names = self.bank_folders.get(bank)
		return named(project or (", ".join(names) if names else bank), group_name(bank, self.group_names))

	def _item(self, bank, row, recalls=_LOAD):
		"""The normalised item of a raw row; `recalls` = the bank's recall summary (None: unavailable), loaded when omitted."""
		if recalls is _LOAD:
			recalls = self._recall_data(bank)
		tags = list(dict.fromkeys(t for t in (row.get("tags") or []) if isinstance(t, str)))
		project = next((t[len(_PROJECT_TAG) :] for t in tags if t.startswith(_PROJECT_TAG)), None)
		scope = "project" if project else "global"
		state = row.get("state")
		fact_type = row.get("fact_type") or row.get(
			"type"
		)  # the list says fact_type, a single GET may say type
		invalidated = state == "invalidated" or bool(row.get("invalidated_at"))
		if not self.edit_enabled:
			editable, reason = False, EDIT_DISABLED_REASON
			deletable, delete_reason = False, INVALIDATE_DISABLED_REASON
		elif fact_type == "observation":
			editable, reason = False, OBSERVATION_REASON
			deletable, delete_reason = False, OBSERVATION_DELETE_REASON
		elif invalidated:
			editable, reason = False, INVALIDATED_REASON
			deletable, delete_reason = False, ALREADY_INVALID_REASON
		else:
			editable, reason = True, None
			deletable, delete_reason = True, None
		native = row.get("id")
		hit = recalls["counts"].get(native) if recalls is not None else None
		meta = {
			"factType": fact_type,
			"state": "invalid" if state == "invalidated" else "active",
			"invalidationReason": row.get("invalidation_reason"),
			"proofCount": row.get("proof_count"),
			"context": row.get("context"),
			"entities": row.get("entities"),
			"documentId": row.get("document_id"),
			"consolidatedAt": row.get("consolidated_at"),
			"nativeId": native,
			"recallCount": None if recalls is None else (hit["count"] if hit else 0),
			"lastRecalledAt": hit["last"] if hit else None,
		}
		if state == "invalidated" or row.get("invalidated_at"):
			meta["invalid"] = True
		return item_dict(
			backend=self.name,
			bank=bank,
			kind="memory",
			native=row["id"],
			scope=scope,
			scope_label=f"{_PROJECT_TAG}{project}" if project else "global",
			folder=self._folder(bank, project),
			text=row.get("text") or "",
			tags=tags,
			created_at=to_iso(row.get("date")) or to_iso(row.get("mentioned_at")),
			updated_at=to_iso(row.get("updated_at")),
			edited_at=to_iso(row.get("edited_at")),
			editable=editable,
			read_only_reason=reason,
			deletable=deletable,
			delete_reason=delete_reason,
			delete_label="Invalidate" if deletable else None,
			meta=meta,
		)

	def overview(self):
		banks = []
		names = sorted(self.banks)
		with ThreadPoolExecutor(max_workers=max(1, len(names))) as pool:
			fetched = dict(zip(names, pool.map(self._rows, names), strict=True))
		for bank in names:
			rows, total = fetched[bank]
			counts = {"global": 0, "project": 0, "folder": 0}
			size = 0
			tag_counts = Counter()
			for row in rows:
				tags = row.get("tags") or []
				tag_counts.update({t for t in tags if isinstance(t, str) and 0 < len(t) <= TAG_MAX_CHARS})
				counts[
					"project"
					if any(isinstance(t, str) and t.startswith(_PROJECT_TAG) for t in tags)
					else "global"
				] += 1
				size += len((row.get("text") or "").encode("utf-8"))
			banks.append(
				{
					"bank": bank,
					"folder": self._folder(bank),
					"counts": {**counts, "total": total},
					"bytes": size,
					"tags": top_tags(tag_counts, TAGS_TOP),
				}
			)
		return {"banks": banks}

	def tags(self, bank, limit):
		"""[{tag, count}] of the tags of Hindsight's memories (one bank, or all), most used first."""
		if bank:
			self._bank(bank)
		counts = Counter()
		for b in [bank] if bank else sorted(self.banks):
			rows, _ = self._rows(b)
			for row in rows:
				counts.update(
					{t for t in (row.get("tags") or []) if isinstance(t, str) and 0 < len(t) <= TAG_MAX_CHARS}
				)
		return top_tags(counts, limit)

	def list_items(self, bank, scope, kind, q, cursor, limit, source=None, group=None, tags=()):
		if cursor and not _CURSOR.fullmatch(cursor):
			raise MemoryError(400, "malformed cursor")
		offset = int(cursor) if cursor else 0
		banks = [bank] if bank else sorted(self.banks)
		if bank:
			self._bank(bank)
		if source not in (None, "omp") and source not in SOURCES:
			raise MemoryError(400, f"source must be one of {', '.join(SOURCES)}")
		banks = [b for b in banks if not group or group_name(b, self.group_names) == group]
		if kind not in (None, "memory") or scope == "folder" or source not in (None, "omp"):
			return [], None, 0  # Hindsight's own memories are native: nothing was imported into them
		needle = q.lower() if q else None
		matched = []
		for b in banks:
			rows, _ = self._rows(b)
			recalls = self._recall_data(b)
			for row in rows:
				item = self._item(b, row, recalls)
				if scope and item["scope"] != scope:
					continue
				if needle and needle not in item["text"].lower():
					continue
				if tags and not set(tags) <= set(item["tags"]):
					continue
				matched.append(item)
		matched.sort(key=lambda it: (it["createdAt"] or "", it["id"]), reverse=True)
		page = matched[offset : offset + limit]
		nxt = str(offset + limit) if offset + limit < len(matched) else None
		return page, nxt, len(matched)

	def get(self, bank, kind, native_id):
		if kind != "memory" or not _NATIVE.fullmatch(native_id):
			raise MemoryError(404, "memory not found")
		rows, _ = self._rows(bank)
		for row in rows:
			if row.get("id") == native_id:
				return self._item(bank, row)
		rows, _ = self._rows(bank, fresh=True)  # a memory retained since the cache was filled
		for row in rows:
			if row.get("id") == native_id:
				return self._item(bank, row)
		raise MemoryError(404, "memory not found")

	# -- writes: text edit through the proxy PATCH route -------------------------------------------------------
	def _fetch_row(self, bank, native_id):
		"""The server's current raw row of one memory: GET memories/{id}, else a fresh list match, else 404."""
		cfg = self._bank(bank)
		path = "/memories/" + urllib.parse.quote(native_id, safe="")
		try:
			row = self._get_json(cfg, path, None)
		except MemoryError as e:
			if e.status != 404:
				raise
			row = None
		if row is not None and row.get("id") == native_id and "text" in row and "tags" in row:
			return row
		rows, _ = self._rows(bank, fresh=True)
		for candidate in rows:
			if candidate.get("id") == native_id:
				return candidate
		raise MemoryError(404, "memory not found")

	def _store_row(self, bank, row):
		"""Upsert a re-fetched row into the cached list so lists, counts and gets show the change at once.

		A cached row is merged with the fresh one ({**old, **new}): a single-GET row may lack fields the list has.
		A row the cache does not hold (a memory the last list left out, e.g. an invalidated one that was just
		restored) is added and the bank's total grows with it.
		"""
		with self._cache_lock:
			hit = self._cache.get(bank)
			if not hit:
				return
			rows, found = [], False
			for r in hit[1]:
				if r.get("id") == row.get("id"):
					rows.append({**r, **row})
					found = True
				else:
					rows.append(r)
			total = hit[2]
			if not found:
				rows.append(row)
				total += 1
			self._cache[bank] = (hit[0], rows, total)

	def _evict(self, bank):
		with self._cache_lock:
			self._cache.pop(bank, None)

	def write(self, item, text, tags):
		if not self.edit_enabled:
			raise MemoryError(403, EDIT_DISABLED_REASON)
		if not item["editable"]:
			raise MemoryError(403, item["readOnlyReason"] or EDIT_DISABLED_REASON)
		validate_text(text)
		if tags is not None and sorted(set(tags)) != item["tags"]:
			raise MemoryError(400, TAGS_FIXED)
		bank, native = item["bank"], item["meta"]["nativeId"]
		cfg = self._bank(bank)
		try:
			return self._patch(cfg, bank, native, item, text)
		except BaseException:
			self._evict(bank)  # whatever went wrong, the cached rows can no longer be trusted
			raise

	def _patch(self, cfg, bank, native, item, text):
		pre_row = self._fetch_row(bank, native)
		fresh = self._item(bank, pre_row)
		if fresh["etag"] != item["etag"]:
			raise MemoryError(409, "the memory changed since it was loaded", current=fresh)
		if not fresh["editable"]:
			raise MemoryError(403, fresh["readOnlyReason"])
		path = "/memories/" + urllib.parse.quote(native, safe="")
		try:
			self._request(cfg, "PATCH", path, None, {"text": text})
		except MemoryError as e:
			if e.status == 404:
				self._refuse_404(e, PROXY_ROUTE_MISSING_REASON)
			if not getattr(e, "uncertain", False):
				raise
			if not self._applied(bank, native, text, pre_row):
				raise
		try:
			row = {**pre_row, **self._fetch_row(bank, native)}
		except MemoryError:
			# the edit is in; only the re-read failed. Show what was sent and let the next load refresh it.
			return self._item(bank, {**pre_row, "text": text})
		self._store_row(bank, row)
		return self._item(bank, row)

	def _applied(self, bank, native, text, pre_row):
		"""After a PATCH that failed without a verdict: True when the server holds the requested text.

		The old text means it did not apply (the original error stands and the snapshot is dropped); anything else,
		or a failed re-read, is uncertain: the snapshot stays (keep_snapshot) and the answer says so.
		"""
		try:
			current = self._fetch_row(bank, native).get("text")
		except MemoryError:
			current = None
		if current == text:
			return True
		if current is not None and current == pre_row.get("text"):
			return False
		error = _err(502, UNCERTAIN_REASON)
		error.keep_snapshot = True
		raise error from None

	# -- delete = invalidate, undelete = restore (PATCH state) ---------------------------------------------------
	@staticmethod
	def _refuse_404(error, route_message):
		"""A 404 to a PATCH: the proxy's own "not found" means its route is missing, anything else a gone memory."""
		if getattr(error, "proxy_route_missing", False):
			raise _err(502, route_message) from None
		raise MemoryError(404, GONE_REASON) from None

	@staticmethod
	def _state_of(row):
		"""\"invalidated\", \"valid\", or None when the row does not say."""
		if row.get("state") == "invalidated" or row.get("invalidated_at"):
			return "invalidated"
		return "valid" if row.get("state") == "valid" else None

	def _state_settled(self, bank, native, want):
		"""After a state PATCH that failed without a verdict: True when the server holds `want`, False when it
		holds the other state (the original error stands and the snapshot is dropped). Anything else, or a failed
		re-read, is uncertain: the snapshot stays (keep_snapshot) and the answer says so."""
		try:
			current = self._state_of(self._fetch_row(bank, native))
		except MemoryError:
			current = None
		if current == want:
			return True
		if current is not None:
			return False
		error = _err(502, STATE_UNCERTAIN_REASON)
		error.keep_snapshot = True
		raise error from None

	def _patch_state(self, cfg, bank, native, state, reason=None):
		body = {"state": state}
		if reason:
			body["reason"] = reason
		path = "/memories/" + urllib.parse.quote(native, safe="")
		try:
			self._request(cfg, "PATCH", path, None, body)
		except MemoryError as e:
			if e.status == 404:
				self._refuse_404(e, PROXY_STATE_ROUTE_MISSING_REASON)
			if not getattr(e, "uncertain", False):
				raise
			if not self._state_settled(bank, native, state):
				raise

	def delete(self, item, save):
		"""Invalidate (reversible, same id). `save(meta)` stores the trash snapshot before the PATCH."""
		if not self.edit_enabled:
			raise MemoryError(403, INVALIDATE_DISABLED_REASON)
		if not item["deletable"]:
			raise MemoryError(403, item["deleteReason"] or INVALIDATE_DISABLED_REASON)
		bank, native = item["bank"], item["meta"]["nativeId"]
		cfg = self._bank(bank)
		try:
			pre_row = self._fetch_row(bank, native)
			fresh = self._item(bank, pre_row)
			if fresh["etag"] != item["etag"]:
				raise MemoryError(409, "the memory changed since it was loaded", current=fresh)
			if not fresh["deletable"]:
				raise MemoryError(403, fresh["deleteReason"])
			save({"action": "invalidate", "nativeId": native})
			self._patch_state(cfg, bank, native, "invalidated", INVALIDATE_REASON)
		except BaseException:
			self._evict(bank)
			raise
		self._store_row(bank, {**pre_row, "state": "invalidated", "invalidation_reason": INVALIDATE_REASON})

	def recreate(self, snapshot):
		"""Undelete: mark the memory valid again. The id is the same as before the invalidate."""
		if not self.edit_enabled:
			raise MemoryError(403, INVALIDATE_DISABLED_REASON)
		_backend, bank, _kind, native = parse_id(snapshot["id"])
		cfg = self._bank(bank)
		try:
			self._patch_state(cfg, bank, native, "valid")
		except BaseException:
			self._evict(bank)
			raise
		try:
			row = self._fetch_row(bank, native)
		except MemoryError:
			# the memory is valid again; only the re-read failed. Show what was saved and let the next load refresh.
			self._evict(bank)
			row = {"id": native, "text": snapshot["text"], "tags": snapshot["tags"], "state": "valid"}
			return self._item(bank, row)
		self._store_row(bank, row)
		return self._item(bank, row)

	# -- recall data from the proxy's log ------------------------------------------------------------------------
	@staticmethod
	def _parse_recalls(raw):
		"""The proxy's omp/recalls answer reduced to what the hub uses, or None when it is not that shape."""
		counts_raw, events_raw = raw.get("counts"), raw.get("events")
		if not isinstance(counts_raw, dict) or not isinstance(events_raw, list):
			return None
		counts = {}
		for native, entry in counts_raw.items():
			count = entry.get("count") if isinstance(entry, dict) else None
			if isinstance(count, int) and not isinstance(count, bool):
				last = entry.get("last")
				counts[native] = {"count": count, "last": last if isinstance(last, str) else None}
		events = [
			{
				"t": e["t"],
				"ids": [i for i in e["ids"] if isinstance(i, str)],
				"q": e["q"] if isinstance(e.get("q"), str) else "",
			}
			for e in events_raw
			if isinstance(e, dict) and isinstance(e.get("t"), str) and isinstance(e.get("ids"), list)
		]
		window = raw.get("windowDays")
		oldest = raw.get("oldest")
		return {
			"counts": counts,
			"events": events,
			"windowDays": window if isinstance(window, int) and window > 0 else RECALL_WINDOW_DAYS,
			"oldest": oldest if isinstance(oldest, str) else None,
		}

	def _recall_data(self, bank):
		"""The bank's recall summary from the proxy, cached; None when the proxy has no such route or fails.

		Never raises: recall data is an extra, and a proxy that predates it must not break the page."""
		with self._recall_lock:
			hit = self._recall_cache.get(bank)
			if hit and time.monotonic() < hit[0]:
				return hit[1]
			data = None
			try:
				data = self._parse_recalls(
					self._get_json(self._bank(bank), "/omp/recalls", {"limit": RECALL_EVENTS})
				)
			except MemoryError:
				pass
			ttl = RECALL_TTL_S if data is not None else RECALL_FAIL_TTL_S
			self._recall_cache[bank] = (time.monotonic() + ttl, data)
			return data

	def _recall_window(self, data):
		"""scan()'s `recalls` block: whether counters mean something and how many days the log covers."""
		if data is None:
			return {"available": False, "windowDays": RECALL_WINDOW_DAYS, "coveredDays": 0.0}
		covered = 0.0
		if data["oldest"]:
			try:
				first = datetime.fromisoformat(data["oldest"].replace("Z", "+00:00"))
				first = first if first.tzinfo else first.replace(tzinfo=timezone.utc)
				covered = max(0.0, (datetime.now(timezone.utc) - first).total_seconds() / 86400)
			except ValueError:
				covered = 0.0
		return {
			"available": True,
			"windowDays": data["windowDays"],
			"coveredDays": min(covered, float(data["windowDays"])),
		}

	def _cache_age(self, bank):
		"""Seconds since the bank's list was fetched (infinite when it is not cached)."""
		with self._cache_lock:
			hit = self._cache.get(bank)
		return time.monotonic() - hit[0] if hit else float("inf")

	@staticmethod
	def _is_observation(row):
		return (row.get("fact_type") or row.get("type")) == "observation"

	def recalls(self, bank, limit):
		rows, _ = self._rows(bank)
		data = self._recall_data(bank)
		if data is None:
			return {
				"events": [],
				"top": [],
				"neverRecalled": None,
				"windowDays": None,
				"available": False,
				"note": "The Hindsight proxy has no recall log (deploy the updated proxy to see which memories are used)",
			}
		wanted = {n for e in data["events"][:limit] for n in e["ids"]} | set(data["counts"])
		by_native = {r["id"]: r for r in rows if isinstance(r.get("id"), str)}
		if not wanted <= by_native.keys() and self._cache_age(bank) > RECALL_REFRESH_AGE_S:
			# Facts are never removed (only invalidated), so an id the list lacks is a regenerated observation, or a
			# memory newer than the cached list: look at a fresh list once before saying which.
			rows, _ = self._rows(bank, fresh=True)
			by_native = {r["id"]: r for r in rows if isinstance(r.get("id"), str)}

		def ref(native):
			row = by_native.get(native)
			item_id = make_id(self.name, bank, "memory", native)
			if row is None:
				return {"id": item_id, "label": DERIVED_GONE_LABEL, "deleted": False, "derived": True}
			return {
				"id": item_id,
				"label": graph.label_of(row.get("text")),
				"deleted": self._state_of(row) == "invalidated",
				"derived": self._is_observation(row),
			}

		events = [
			{"t": e["t"], "query": e["q"], "items": [ref(n) for n in e["ids"]]}
			for e in data["events"][:limit]
		]
		ranked = sorted(data["counts"].items(), key=lambda kv: kv[1]["last"] or "", reverse=True)
		ranked.sort(key=lambda kv: kv[1]["count"], reverse=True)
		top = [
			{
				"id": r["id"],
				"label": r["label"],
				"count": entry["count"],
				"lastAt": entry["last"],
				"derived": r["derived"],
			}
			for native, entry in ranked[:20]
			for r in (ref(native),)
		]
		never = sum(
			1
			for r in rows
			if self._state_of(r) != "invalidated"
			and not self._is_observation(r)
			and r.get("id") not in data["counts"]
		)
		return {
			"events": events,
			"top": top,
			"neverRecalled": never,
			"windowDays": data["windowDays"],
			"available": True,
			"note": None,
		}

	# -- the memory map ------------------------------------------------------------------------------------------
	def map(self, bank, limit):
		"""Nodes and edges from the proxy-forwarded GET graph, keyed by this adapter's item ids.

		Upstream shape (Hindsight OpenAPI, GraphDataResponse): {"nodes": [{"data": {"id", "label", "text", ...}}],
		"edges": [{"data": {"id", "source", "target", "linkType", "weight", "entityName"}}], "table_rows": [{"id",
		"text", ...}], "total_units": n}. The node set is the graph's own: a node stays even when the cached memory
		list (up to CACHE_TTL_S old) has not seen it yet. Its text comes from the graph (node text, the matching
		table row, then the label); the cached list only adds what the graph lacks (tags for the scope, state,
		date) and the proxy's recall log the recall counts. stats are counted on the nodes returned here:
		total = the graph's nodes, shown = those kept, truncated = shown < total."""
		cfg = self._bank(bank)
		try:
			rows, _ = self._rows(bank)
		except MemoryError:
			rows = []  # the list only enriches; the graph decides what is there
		recalls = self._recall_data(bank)
		try:
			raw = self._get_json(cfg, "/graph", {"limit": GRAPH_LIMIT})
		except MemoryError as e:
			if e.status == 404:
				raise _err(
					502, "Hindsight's graph is not available: deploy the updated proxy (GET graph route)"
				) from None
			raise
		nodes_raw, edges_raw = raw.get("nodes"), raw.get("edges")
		if not isinstance(nodes_raw, list) or not isinstance(edges_raw, list):
			raise MemoryError(502, "Hindsight answered with an unexpected shape")
		by_native = {r["id"]: r for r in rows if isinstance(r.get("id"), str)}
		table = {
			r["id"]: r
			for r in (raw.get("table_rows") if isinstance(raw.get("table_rows"), list) else [])
			if isinstance(r, dict) and isinstance(r.get("id"), str)
		}
		nodes, ids = [], {}
		for entry in nodes_raw:
			data = entry.get("data", entry) if isinstance(entry, dict) else None
			native = data.get("id") if isinstance(data, dict) else None
			if not isinstance(native, str) or not _NATIVE.fullmatch(native) or native in ids:
				continue
			cached = by_native.get(native, {})
			tabled = table.get(native, {})
			text = next(
				(
					v
					for v in (data.get("text"), tabled.get("text"), data.get("label"), cached.get("text"))
					if isinstance(v, str) and v
				),
				"",
			)
			tags = next(
				(
					v
					for v in (cached.get("tags"), data.get("tags"), tabled.get("tags"))
					if isinstance(v, list)
				),
				[],
			)
			project = next(
				(t[len(_PROJECT_TAG) :] for t in tags if isinstance(t, str) and t.startswith(_PROJECT_TAG)),
				None,
			)
			hit = recalls["counts"].get(native) if recalls is not None else None
			ids[native] = make_id(self.name, bank, "memory", native)
			nodes.append(
				{
					"id": ids[native],
					"text": text,
					"kind": "memory",
					"scope": "project" if project else "global",
					"state": "invalid" if self._state_of(cached) == "invalidated" else "active",
					"recallCount": None if recalls is None else (hit["count"] if hit else 0),
					"lastRecalledAt": hit["last"] if hit else None,
					"createdAt": to_iso(cached.get("date") or tabled.get("date") or data.get("date"))
					or to_iso(cached.get("mentioned_at")),
				}
			)
		edges = []
		for entry in edges_raw:
			data = entry.get("data", entry) if isinstance(entry, dict) else None
			if not isinstance(data, dict) or data.get("source") not in ids or data.get("target") not in ids:
				continue
			is_entity = data.get("linkType") == "entity"
			label = data.get("entityName") if is_entity else data.get("linkType")
			edges.append(
				{
					"source": ids[data["source"]],
					"target": ids[data["target"]],
					"kind": "entity" if is_entity else "link",
					"weight": data.get("weight"),
					"label": label if isinstance(label, str) and label else None,
				}
			)
		return graph.build(nodes, edges, limit, "hindsight-graph")

	def scan(self, bank):
		rows, total = self._rows(bank, fresh=True)
		data = self._recall_data(bank)
		return {
			"backend": self.name,
			"bank": bank,
			"truncated": total > len(rows),
			"items": [self._item(bank, r, data) for r in rows],
			"recalls": self._recall_window(data),
		}
