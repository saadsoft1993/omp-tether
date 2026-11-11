"""Fallback Hindsight export: page `GET memories/list` through the redacting proxy into JSONL.

    python3 -m memory_ops.hindsight_pages --out DIR --banks a,b (--overlay-dir DIR | --api-url URL --token-file FILE)
        [--page 500] [--timeout 30] [--pause 0.2] [--dry-run]

Why this exists next to `hindsight-admin export-bank`: the archive export-bank writes omits database ids,
embeddings, entities/links and (by default) the consolidated observations, so it cannot feed the importer's
`<bank>#<itemId>` ledger keys. This path keeps every field the list returns (id, text, fact_type, state,
tags, dates, context, entities, document_id ...), verbatim, one JSON object per line.

Connection: each bank's overlay (`--overlay-dir DIR`, one <bank>.yml per bank, parsed with the hub's own
parse_overlay) gives apiUrl + apiToken, exactly as the hub adapter uses them. The token is only ever put in the
Authorization header: never printed, logged, written to the output or echoed in an error. `--api-url` +
`--token-file` replace the overlay (tests; one bank); plain http is accepted only for 127.0.0.1/localhost.

Safety: the bearer token is only sent to the configured api url (no environment proxy is used, and a url with
user:password in it is refused). A token file readable by group or others is refused (chmod 600). Existing
output files are never overwritten or followed through symlinks: every file is created exclusively, 0600 (also
set on the descriptor), and a second export within the same second gets a stamp with a counter suffix
(`<stamp>-2`, `<stamp>-3` ...). The `--out` directory is made 0700 only when this run creates it; an existing one keeps its mode.

Output: when every bank succeeded, `hindsight-export-<stamp>.json` = {"exportedAt": ISO, "banks": {bank: [row,...]}}
(the memory_import hindsight-mode input, every list row verbatim). Per bank, in DIR (0700), files 0600: `<bank>-<UTC stamp>.jsonl` and `<bank>-<UTC stamp>.meta.json`
({bank, stamp, rows, total, sha256, pages, seconds}). The JSONL is written to a `.partial` file and renamed only
when the row count equals the server's `total` and no id repeats (a repeat means the bank changed while paging:
stop writers first). Any failure removes the partial file and exits 1; other banks still run. Exit 2 = bad
arguments. One bank at a time, one request at a time, with a pause between pages, because the server may be slow
or busy. Nothing is written to Hindsight.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from hindsight_proxy.redact import redact_tokens
from omp_hub.memory.hindsight import parse_overlay, tls_context

BANK_RE = re.compile(r"[A-Za-z0-9_.-]{1,80}")
MAX_RETRIES = 3

# A bank name or a path is not secret by intent, but a PIN assignment (`password=1234`) or an API key pasted into one must
# not be echoed into a terminal or a log: `shown` gives such a name as an index label (`bank#1`, `folder#2`, numbered by first
# appearance in this run, nothing of the value in it). The short form of the rule in omp-memory's python/omp_memory/detect.py.
_KEYWORDS = ("password", "passwd", "pwd", "passphrase", "secret", "token", "apikey", "credentials", "credential", "auth")
_FILLER = ("is", "are", "was", "equals", "eq")
_labels = {}


def _value_in_name(value):
	return (len(value) >= 3 and re.search(r"[0-9]", value) is not None) or (
		len(value) >= 4 and re.search(r"[a-z]", value) is not None and re.search(r"[A-Z]", value[1:]) is not None
	)


def _keyword_with_value(text):
	words = [w for w in re.split(r"[\W_]+", text) if w]
	for i, raw in enumerate(words):
		word, nxt = raw.lower(), i + 1
		if word == "api" and nxt < len(words) and words[nxt].lower() == "key":
			word, nxt = "apikey", nxt + 1
		if word in _KEYWORDS:
			if nxt < len(words) and words[nxt].lower() in _FILLER:
				nxt += 1
			if nxt < len(words) and _value_in_name(words[nxt]):
				return True
			continue
		key = next((k for k in _KEYWORDS if word.startswith(k)), None)
		if key and re.fullmatch(r"[0-9]{3,}", word[len(key) :]):
			return True
	return False


def _carries_secret(text):
	decoded = urllib.parse.unquote(urllib.parse.unquote(text))
	return any(redact_tokens(form)[0] != form or _keyword_with_value(form) for form in {text, decoded, text.lower(), decoded.lower()})


def shown(kind, text):
	"""`text` as it may be printed: control characters made `?`, or an index label when it carries a secret."""
	text = re.sub(r"[\x00-\x1f\x7f-\x9f]", "?", str(text))
	if not _carries_secret(text):
		return text
	index = _labels.setdefault(kind, {})
	return f"{kind}#{index.setdefault(text, len(index) + 1)}"


class ExportError(Exception):
	pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
	"""A redirect would carry the bearer token to wherever it points."""

	def redirect_request(self, *args, **kwargs):
		return None


def check_url(url):
	parts = urllib.parse.urlsplit(url)
	if parts.username is not None or parts.password is not None or "@" in parts.netloc:
		raise ValueError("api url must not contain user:password")
	local = parts.hostname in ("127.0.0.1", "localhost")
	if parts.scheme == "https" or (parts.scheme == "http" and local):
		return url.rstrip("/")
	raise ValueError("api url must be https (http only for 127.0.0.1/localhost)")


def check_token_file(path):
	mode = Path(path).expanduser().stat().st_mode
	if mode & 0o077:
		raise ValueError(f"token file {shown('folder', path)} is readable by group or others: chmod 600 it")


def read_token_file(path):
	check_token_file(path)
	text = Path(path).expanduser().read_text().strip()
	if not text:
		raise ValueError(f"empty token file: {shown('folder', path)}")
	return text


def connection(args, bank):
	"""(api url, token) for a bank, from --api-url/--token-file or the bank's overlay."""
	if args.api_url:
		return check_url(args.api_url), read_token_file(args.token_file)
	path = Path(args.overlay_dir).expanduser() / f"{bank}.yml"
	try:
		cfg = parse_overlay(path.read_text())
	except OSError as e:
		raise ExportError(f"cannot read overlay {shown('folder', path)}: {e.strerror}")
	if cfg.get("bankId") != bank or not cfg.get("apiUrl") or not cfg.get("apiToken"):
		raise ExportError(f"overlay {shown('folder', path)} has no hindsight apiUrl/apiToken for bank {shown('bank', bank)}")
	return check_url(cfg["apiUrl"]), cfg["apiToken"]


class Pager:
	def __init__(self, url, token, bank, timeout, opener=None):
		self.base = f"{url}/v1/default/banks/{urllib.parse.quote(bank, safe='')}/memories/list"
		self._token = token
		self.timeout = timeout
		# ProxyHandler({}): never route a request (and so the token) through an environment proxy
		self._opener = opener or urllib.request.build_opener(
			urllib.request.ProxyHandler({}), _NoRedirect, urllib.request.HTTPSHandler(context=tls_context())
		)

	def get(self, offset, limit):
		url = f"{self.base}?{urllib.parse.urlencode({'limit': limit, 'offset': offset})}"
		req = urllib.request.Request(
			url, headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
		)
		last = "no attempt"
		for attempt in range(MAX_RETRIES):
			try:
				with self._opener.open(req, timeout=self.timeout) as resp:
					data = json.loads(resp.read())
				if not isinstance(data, dict) or not isinstance(data.get("items"), list):
					raise ExportError("unexpected list response shape")
				return data
			except urllib.error.HTTPError as e:
				if e.code in (400, 401, 403, 404):  # not transient
					raise ExportError(f"HTTP {e.code} from proxy at offset {offset}")
				last = f"HTTP {e.code}"
			except (
				urllib.error.URLError,
				OSError,
				ValueError,
			) as e:  # URLError text may name the host, never the token
				last = type(e).__name__
			time.sleep(min(2**attempt, 8))
		raise ExportError(f"offset {offset}: gave up after {MAX_RETRIES} attempts ({last})")


def create_private(path):
	"""Create `path` as a new 0600 file and return its descriptor. Never opens an existing file or follows a symlink
	(FileExistsError), and sets the mode on the descriptor so the umask cannot loosen it."""
	fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
	os.fchmod(fd, 0o600)
	return fd


def refuse_existing(path):
	if os.path.lexists(path):
		raise FileExistsError(errno.EEXIST, "refusing to overwrite an existing export file", str(path))


def export_names(bank, stamp):
	return (f"{bank}-{stamp}.jsonl", f".{bank}-{stamp}.jsonl.partial", f"{bank}-{stamp}.meta.json")


def unique_stamp(out_dir, banks):
	"""The UTC second as a stamp, with a `-2`, `-3` ... suffix while any output name for it is already taken."""
	base = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
	stamp, n = base, 1
	while os.path.lexists(out_dir / f"hindsight-export-{stamp}.json") or any(
		os.path.lexists(out_dir / name) for bank in banks for name in export_names(bank, stamp)
	):
		n += 1
		stamp = f"{base}-{n}"
	return stamp


def export_bank(pager, bank, out_dir, stamp, page, pause):
	start = time.monotonic()
	final_name, part_name, meta_name = export_names(bank, stamp)
	final, part, meta_path = out_dir / final_name, out_dir / part_name, out_dir / meta_name
	seen, pages, total, digest = set(), 0, None, hashlib.sha256()
	refuse_existing(final)
	refuse_existing(meta_path)
	fd = create_private(part)  # outside the try: a partial file this run did not create is never deleted
	try:
		with os.fdopen(fd, "wb") as fh:
			offset = 0
			while True:
				data = pager.get(offset, page)
				pages += 1
				total = data.get("total", total)
				items = data["items"]
				for item in items:
					ident = item.get("id") if isinstance(item, dict) else None
					if not ident:
						raise ExportError("a row has no id")
					if ident in seen:
						raise ExportError(
							"an id repeated while paging: the bank changed, stop writers and rerun"
						)
					seen.add(ident)
					line = (json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n").encode()
					digest.update(line)
					fh.write(line)
				offset += len(items)
				if not items or (total is not None and offset >= total):
					break
				time.sleep(pause)
		if total is None or len(seen) != int(total):
			raise ExportError(f"row count {len(seen)} != server total {total}")
		os.link(part, final)  # publishes without replacing: fails if `final` appeared meanwhile
	finally:
		part.unlink(missing_ok=True)
	meta = {
		"bank": bank,
		"stamp": stamp,
		"rows": len(seen),
		"total": int(total),
		"sha256": digest.hexdigest(),
		"pages": pages,
		"seconds": round(time.monotonic() - start, 2),
	}
	with os.fdopen(create_private(meta_path), "w") as fh:
		json.dump(meta, fh, indent=1)
	return meta


class _Parser(argparse.ArgumentParser):
	def error(self, message):  # argparse echoes the argument it did not understand
		super().error(shown("name", message))


def parse_args(argv):
	p = _Parser(prog="hindsight_pages", description="Page memories/list into JSONL.")
	p.add_argument("--out", required=True, help="directory for the export files")
	p.add_argument("--banks", required=True, help="comma list of Hindsight bank names")
	p.add_argument("--overlay-dir", help="directory of <bank>.yml overlay files (api url and token per bank)")
	p.add_argument("--api-url")
	p.add_argument("--token-file")
	p.add_argument("--page", type=int, default=500)
	p.add_argument("--timeout", type=float, default=30)
	p.add_argument("--pause", type=float, default=0.2)
	p.add_argument("--dry-run", action="store_true", help="validate arguments and print the plan; no network")
	args = p.parse_args(argv)
	args.bank_list = [b.strip() for b in args.banks.split(",") if b.strip()]
	if not args.bank_list or not all(BANK_RE.fullmatch(b) and b not in (".", "..") for b in args.bank_list):
		p.error("--banks must be a comma list of bank names")
	if not 1 <= args.page <= 1000:
		p.error("--page must be 1..1000")
	if args.timeout <= 0 or args.pause < 0:
		p.error("--timeout must be > 0 and --pause >= 0")
	if bool(args.api_url) != bool(args.token_file):
		p.error("--api-url and --token-file go together")
	if not args.api_url and not args.overlay_dir:
		p.error("give --overlay-dir, or --api-url with --token-file")
	if args.api_url:
		if len(args.bank_list) != 1:
			p.error("--api-url serves exactly one bank (--banks NAME)")
		try:
			check_url(args.api_url)
		except ValueError as e:
			p.error(str(e))
		if not Path(args.token_file).expanduser().is_file():
			p.error("--token-file is not a file")
		try:
			check_token_file(args.token_file)
		except ValueError as e:
			p.error(str(e))
	return args


def main(argv=None, opener=None):
	try:
		args = parse_args(argv if argv is not None else sys.argv[1:])
	except SystemExit as e:
		return int(e.code or 0) if e.code in (0, None) else 2
	out_dir = Path(args.out).expanduser()
	if args.dry_run:
		source = args.api_url or f"overlays in {args.overlay_dir}"
		banks = ",".join(shown("bank", b) for b in args.bank_list)
		print(f"dry-run: banks={banks} page={args.page} out={shown('folder', out_dir)} source={shown('folder', source)}")
		return 0
	try:
		out_dir.mkdir(mode=0o700, parents=True)
		out_dir.chmod(0o700)  # only a directory this run made: an existing one keeps its mode
	except FileExistsError:
		pass
	stamp = unique_stamp(out_dir, args.bank_list)
	failed = False
	done = []
	for bank in args.bank_list:
		try:
			url, token = connection(args, bank)
			meta = export_bank(
				Pager(url, token, bank, args.timeout, opener), bank, out_dir, stamp, args.page, args.pause
			)
			done.append(bank)
			print(
				f"ok   {shown('bank', bank)}: {meta['rows']} rows, {meta['pages']} pages, {meta['seconds']}s -> {shown('name', f'{bank}-{stamp}.jsonl')}"
			)
		except (ExportError, ValueError, OSError) as e:
			print(f"FAIL {shown('bank', bank)}: {shown('name', e)}", file=sys.stderr)
			failed = True
	if not failed:
		try:
			combined = write_combined(out_dir, stamp, done)
		except OSError as e:
			print(f"FAIL combined: {shown('name', e)}", file=sys.stderr)
			return 1
		print(f"ok   combined: {combined.name}")
	return 1 if failed else 0


def write_combined(out_dir, stamp, banks):
	"""`hindsight-export-<stamp>.json` = {"exportedAt": ISO, "banks": {bank: [row, ...]}}: the importer's input."""
	data = {}
	for bank in banks:
		with open(out_dir / f"{bank}-{stamp}.jsonl", encoding="utf-8") as fh:
			data[bank] = [json.loads(line) for line in fh]
	path = out_dir / f"hindsight-export-{stamp}.json"
	with os.fdopen(create_private(path), "w", encoding="utf-8") as fh:
		json.dump(
			{"exportedAt": datetime.now(timezone.utc).isoformat(), "banks": data}, fh, ensure_ascii=False
		)
	return path


if __name__ == "__main__":
	sys.exit(main())
