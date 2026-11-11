"""Redact recognisable secrets from text before it reaches Hindsight.

Used by the proxy (every write body, including omp's automatic transcript retains, which omp does not redact)
and by the mnemopi import. Each match becomes `[REDACTED:<kind>]`; `redact_text` also returns how many of each
kind it replaced, so callers can log counts without ever logging the text.

Two families of rules:
- Token shapes that are secrets wherever they appear (AWS key ids, GitHub/GitLab/Slack/Google/npm/Hugging Face/
  Stripe/Twilio/SendGrid/Mailgun/Linear/Notion/DigitalOcean tokens, `sk-` style API keys, JWTs, Azure account
  keys, `Bearer <token>`, `Basic <base64 user:pass>`, PEM private keys, credentials inside URLs). `redact_tokens`
  applies only these, so identifiers (document ids, tags) are never rewritten by a name-based guess.
- Assignments whose *name* says secret (key, secret, token, password, credential, dsn, auth, ...) and whose
  *value* looks like one: `.env` lines, quoted assignments in code/JSON/YAML (also JSON escaped inside a string),
  unquoted YAML values, `Authorization:` and other secret-named HTTP headers, secret URL query parameters and
  `--password`/`--token`/`--secret`/`--api-key` CLI flags. Code such as `token = get_token()`, `api_key: str` or
  `KEY_PREFIX = "project:"` is left alone: calls, types, short or single-character-class values and
  placeholders are not secrets.

Every pattern runs in linear time: unbounded runs either start only where the run itself starts (a lookbehind
excludes the run's own characters) or are bounded, so adversarial input cannot stall the proxy. Strings longer
than MAX_TEXT_CHARS are refused (TextTooLarge) instead of scanned.

Pattern redaction cannot catch a secret that has no recognisable shape (a bare password in prose), nor one
split across fields or hidden with zero-width characters, homoglyphs or an extra layer of encoding.
`residual_findings` reports what still looks secret after redaction, for a human to review.
"""

import base64
import binascii
import itertools
import math
import re
from collections import Counter
from collections.abc import Callable, Iterable

MARK = "[REDACTED:{}]"
MAX_TEXT_CHARS = 1_000_000


class RejectBody(ValueError):
	"""The body cannot be redacted safely and must not be forwarded."""


class TextTooLarge(RejectBody):
	"""A single string is longer than MAX_TEXT_CHARS."""


# Name segments that make an assignment secret-bearing (matched per segment, so `tokenizer`, `monkey`,
# `author` and `keyboard` do not count).
_SECRET_SEGMENTS = {
	"key",
	"apikey",
	"secret",
	"secrets",
	"token",
	"password",
	"passwd",
	"pwd",
	"pass",
	"passphrase",
	"credential",
	"credentials",
	"dsn",
	"auth",
	"privatekey",
	"accesskey",
	"clientsecret",
}
_PLACEHOLDER = re.compile(
	r"^(?:\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|<[^>]*>|\*+|x{3,}|\.\.\.|…|null|none|nil|true|false|undefined"
	r"|changeme|change[_-]?me|example\w*|dummy\w*|placeholder\w*|your[_-]\w*|redacted\w*)$",
	re.IGNORECASE,
)
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
# Code references rather than literals: settings.OPENAI_API_KEY, os.environ, my_api_key, OPENAI_API_KEY.
_IDENTIFIER = re.compile(
	r"^(?:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+|[a-z][a-z0-9]*(?:_[a-z0-9]+)+|[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)$"
)
_HEX = re.compile(r"[0-9a-fA-F]+")


def _secret_name(name):
	parts = [p.lower() for p in re.split(r"[_.\-]+", _CAMEL.sub("_", name)) if p]
	return any(p in _SECRET_SEGMENTS for p in parts) or name.lower().replace("_", "") in _SECRET_SEGMENTS


def _secret_value(value):
	"""A value that looks like a secret rather than code, a type, a word or a placeholder."""
	if value.startswith("[REDACTED") or _PLACEHOLDER.match(value):
		return False
	if any(c in value for c in "()[]{}") or len(value) < 6 or _IDENTIFIER.match(value):
		return False
	classes = sum(
		(
			any(c.islower() for c in value),
			any(c.isupper() for c in value),
			any(c.isdigit() for c in value),
		)
	)
	if classes >= 2:
		return True
	# One character class: only long unbroken runs (a random all-lowercase or all-digit key).
	return len(value) >= 24 and re.fullmatch(r"[A-Za-z0-9+/=_\-]+", value) is not None


def _literal(value, min_len):
	"""A concrete value rather than a placeholder, template, variable or code reference."""
	return (
		len(value) >= min_len
		and not _PLACEHOLDER.match(value)
		and not any(c in value for c in "{}$<>()")
		and not _IDENTIFIER.match(value)
	)


def _has_digit(value):
	return any(c.isdigit() for c in value)


# Accept callbacks take (match, value) and decide whether the matched value is a secret.
def _digit(_m, value):
	return _has_digit(value)


def _hex_mix(_m, value):
	# 32 hex chars after SK/AC: requiring both a digit and a hex letter keeps words such as SKIDDADDLE... out.
	return _has_digit(value) and any(c.isalpha() for c in value[2:])


def _notion_secret(_m, value):
	# `secret_` + 43 chars could be a long snake_case identifier; real tokens are base62 with digits and capitals.
	return _has_digit(value) and any(c.isupper() for c in value)


def _basic_credentials(_m, value):
	"""`Basic <x>` outside an Authorization header only when <x> is base64 of printable `user:pass`."""
	try:
		raw = base64.b64decode(value + "=" * (-len(value) % 4), validate=True)
	except (binascii.Error, ValueError):
		return False
	return b":" in raw and all(32 <= b < 127 for b in raw)


def _url_password(_m, value):
	# `http://host:8080/a/@b` is a port and a path, not a password.
	return not _PLACEHOLDER.match(value) and re.match(r"\d{1,5}(?:/|$)", value) is None


def _url_userinfo(_m, value):
	return (_HEX.fullmatch(value) is not None and len(value) >= 16) or _secret_value(value)


def _named_secret(m, value):
	return _secret_name(m.group("name")) and _secret_value(value)


_AUTH_SCHEMES = {"basic", "bearer", "token", "digest", "apikey", "api-key", "key", "sso-key", "negotiate"}


def _authorization(_m, value):
	return (
		value.lower() not in _AUTH_SCHEMES
		and _literal(value, 8)
		and (len(value) >= 20 or _secret_value(value))
	)


def _query_param(_m, value):
	return _literal(value, 8) and (len(value) >= 16 or _secret_value(value))


def _cli_value(_m, value):
	return not value.startswith("-") and _literal(value, 6)


def _mysql_password(_m, value):
	return _literal(value, 1)


Accept = Callable[[re.Match, str], bool] | None
Rule = tuple[str, re.Pattern[str], Accept]

_NAME = r"[A-Za-z_][A-Za-z0-9_.\-]{0,127}"
_NB = r"(?<![A-Za-z0-9_.\-])"  # a name starts only where its run starts

# Token shapes. A rule with a `val` group replaces only that group; otherwise the whole match.
_TOKEN_RULES: list[Rule] = [
	(
		"private-key",
		re.compile(
			r"-----BEGIN [A-Z0-9 ]{0,64}PRIVATE KEY(?: BLOCK)?-----[\s\S]*?"
			r"(?:-----END [A-Z0-9 ]{0,64}PRIVATE KEY(?: BLOCK)?-----|\Z)"
		),
		None,
	),
	(
		"aws-access-key",
		re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16}(?![A-Za-z0-9])"),
		None,
	),
	(
		"github-token",
		re.compile(r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
		None,
	),
	(
		"gitlab-token",
		re.compile(
			r"(?<![A-Za-z0-9_])(?:(?:glpat|gldt|glrt|glptt|glft|glsoat|glcbt|gloas|glimt|glagent)-[A-Za-z0-9_\-]{20,}"
			r"|GR1348941[A-Za-z0-9_\-]{20,})"
		),
		None,
	),
	("slack-token", re.compile(r"(?<![A-Za-z0-9])xox[abeoprs]-[A-Za-z0-9-]{10,}"), None),
	("google-api-key", re.compile(r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_\-]{35}"), None),
	("npm-token", re.compile(r"(?<![A-Za-z0-9])npm_[A-Za-z0-9]{36}"), None),
	("huggingface-token", re.compile(r"(?<![A-Za-z0-9])hf_[A-Za-z0-9]{30,}"), None),
	("stripe-key", re.compile(r"(?<![A-Za-z0-9])(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}"), None),
	("twilio-key", re.compile(r"(?<![A-Za-z0-9])(?:SK|AC)[0-9a-f]{32}(?![A-Za-z0-9])"), _hex_mix),
	(
		"sendgrid-key",
		re.compile(r"(?<![A-Za-z0-9_\-])SG\.[A-Za-z0-9_\-]{22}\.[A-Za-z0-9_\-]{43}(?![A-Za-z0-9_\-])"),
		None,
	),
	("mailgun-key", re.compile(r"(?<![A-Za-z0-9_\-])key-[0-9a-f]{32}(?![A-Za-z0-9])"), None),
	("linear-key", re.compile(r"(?<![A-Za-z0-9_])lin_(?:api|oauth)_[A-Za-z0-9]{32,}"), None),
	("notion-token", re.compile(r"(?<![A-Za-z0-9_])ntn_[A-Za-z0-9]{30,}"), None),
	("notion-token", re.compile(r"(?<![A-Za-z0-9_])secret_[A-Za-z0-9]{43}(?![A-Za-z0-9_])"), _notion_secret),
	("digitalocean-token", re.compile(r"(?<![A-Za-z0-9_])do[opr]_v1_[0-9a-f]{64}(?![A-Za-z0-9])"), None),
	(
		"azure-key",
		re.compile(
			r"(?i)(?<![A-Za-z0-9])(?:AccountKey|SharedAccessKey)=(?P<val>[A-Za-z0-9+/]{20,512}={0,2})"
		),
		None,
	),
	(
		"jwt",
		re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
		None,
	),
	# `sk-...` keys (OpenAI, Anthropic, DeepSeek, ...) and `Bearer <token>`: the token must contain a digit, so
	# prose such as "sk-learn-compatible-estimators" or "Bearer authentication-scheme-description" (and words
	# like task-runner or disk-usage) never match. A real key without a single digit is vanishingly unlikely.
	("api-key", re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_\-]{20,}"), _digit),
	("bearer-token", re.compile(r"(?i)\bbearer[ \t]{1,8}(?P<val>[A-Za-z0-9._~+/=\-]{20,4096})"), _digit),
	(
		"basic-auth",
		re.compile(
			r"(?i)(?<![A-Za-z0-9])basic[ \t]{1,8}(?P<val>[A-Za-z0-9+/]{8,4096}={0,2})(?![A-Za-z0-9+/=])"
		),
		_basic_credentials,
	),
	# scheme://user:password@ (user may be empty; the password may contain '/').
	(
		"url-credentials",
		re.compile(
			r"(?i)(?<![a-z0-9+.\-])[a-z][a-z0-9+.\-]{0,30}://[^\s:/@\"'<>\[\]]{0,256}:(?P<val>[^\s@\"'<>]{1,256})@"
		),
		_url_password,
	),
	# scheme://<key>@host (Sentry DSNs and other key-only userinfo).
	(
		"url-credentials",
		re.compile(r"(?i)(?<![a-z0-9+.\-])[a-z][a-z0-9+.\-]{0,30}://(?P<val>[A-Za-z0-9_\-]{16,256})@"),
		_url_userinfo,
	),
]

_VALUE = r"[^\s\"'\\]{1,1024}"

# Name-based rules: run by redact_text only.
_NAME_RULES: list[Rule] = [
	(
		"authorization",
		re.compile(
			r"(?i)(?<![A-Za-z0-9_])(?:proxy-)?authorization\\{0,7}[\"']?[ \t]{0,8}[:=][ \t]{0,8}\\{0,7}[\"']?"
			r"[ \t]{0,8}(?:(?:basic|bearer|token|digest|apikey|api-key|key|sso-key|negotiate)[ \t]{1,8})?"
			r"(?P<val>[^\s\"'\\,;]{1,4096})"
		),
		_authorization,
	),
	# HTTP-style headers with a dashed name (X-Api-Key: v, Private-Token: v), at line start or after a quote.
	(
		"header",
		re.compile(
			r"(?<![^\s\"'])(?P<name>[A-Za-z][A-Za-z0-9]{0,63}(?:-[A-Za-z0-9]{1,63}){1,8}):[ \t]{0,8}"
			rf"(?P<val>{_VALUE})"
		),
		_named_secret,
	),
	# curl -H "apikey: v" / --header 'token: v'.
	(
		"header",
		re.compile(
			r"(?<![A-Za-z0-9_\-])(?:-H|--header)[ \t=]{1,8}[\"']?(?P<name>[A-Za-z][A-Za-z0-9_\-]{0,63}):[ \t]{0,8}"
			rf"(?P<val>{_VALUE})"
		),
		_named_secret,
	),
	(
		"url-param",
		re.compile(
			r"(?i)[?&;](?:access_token|api_key|apikey|token|key|secret|signature|sig|password|auth|client_secret)="
			r"(?P<val>[^&#\s\"'<>\\]{1,2048})"
		),
		_query_param,
	),
	(
		"cli-flag",
		re.compile(
			r"(?<![A-Za-z0-9_\-])--(?:password|passwd|pass|token|secret|api-key|apikey|api-token|access-token"
			r"|auth-token|client-secret)(?:[ \t]{1,8}|=)(?P<q>[\"']?)(?P<val>[^\s\"'\\]{1,1024})(?P=q)"
		),
		_cli_value,
	),
	# mysql -pSECRET (password glued to -p). psql/ssh `-p` is a port and is not matched.
	(
		"cli-flag",
		re.compile(
			r"(?<![A-Za-z0-9_\-])(?:mysql|mysqldump|mysqladmin|mariadb|mariadb-dump)\b[^\n|;&]{0,256}?"
			r"[ \t]-p(?P<q>[\"']?)(?P<val>[^\s\"'\\]{1,256})(?P=q)"
		),
		_mysql_password,
	),
	# KEY=value at line start, after a space/semicolon or after an escaped newline (.env, shell export,
	# `API_KEY=x npm start`); no spaces around '=' so `token = x` is not this rule, and `f(token=x)` (after '(')
	# is not either. An unquoted value runs to whitespace and may contain '#', ';' and quotes; it stops at an
	# escaped newline/quote and at `;NAME=` (the next assignment).
	(
		"env-assignment",
		re.compile(
			rf"(?m)(?:^|(?<=[ \t;])|(?<=\\n))(?:export[ \t]{{1,8}})?(?P<name>{_NAME})="
			r"(?:(?P<q>[\"'])(?P<val>[^\"'\r\n\\]{1,1024})(?P=q)"
			r"|(?P<uval>(?:[^\s\\;]|\\(?![nrt\"'])|;(?![A-Za-z_][A-Za-z0-9_]{0,127}=)){1,1024}))"
		),
		_named_secret,
	),
	# name = "value" / name: 'value' / "name": "value" (code, JSON, YAML, TOML), also JSON-escaped inside a
	# string (\"name\":\"value\", any escaping depth).
	(
		"assignment",
		re.compile(
			rf"{_NB}(?P<name>{_NAME})\\{{0,7}}[\"']?[ \t]{{0,8}}[:=][ \t]{{0,8}}(?P<e>\\{{0,7}})(?P<q>[\"'])"
			r"(?P<val>[^\"'\s\\]{1,1024})(?P=e)(?P=q)"
		),
		_named_secret,
	),
	# YAML `name: value` with an unquoted scalar, alone on its line.
	(
		"assignment",
		re.compile(
			rf"(?m)^[ \t]{{0,64}}(?:-[ \t]{{1,8}})?(?P<name>{_NAME}):[ \t]{{1,64}}"
			r"(?P<val>[^\s\"'#{}\[\](),]{1,1024})[ \t]{0,64}$"
		),
		_named_secret,
	),
]


def _check_size(text):
	if len(text) > MAX_TEXT_CHARS:
		raise TextTooLarge(f"string longer than {MAX_TEXT_CHARS} characters")


def _apply(text, rules, counts):
	for kind, pattern, accept in rules:
		groups = [g for g in ("val", "uval") if g in pattern.groupindex]

		def repl(m, kind=kind, accept=accept, groups=groups):
			g = next((g for g in groups if m.group(g) is not None), None)
			value = m.group(g) if g else m.group(0)
			if value.startswith("[REDACTED") or (accept is not None and not accept(m, value)):
				return m.group(0)
			counts[kind] += 1
			if g is None:
				return MARK.format(kind)
			whole, base = m.group(0), m.start()
			return whole[: m.start(g) - base] + MARK.format(kind) + whole[m.end(g) - base :]

		text = pattern.sub(repl, text)
	return text


def _redact_shapes(text, extra_secrets, counts):
	for secret in extra_secrets:
		if secret and len(secret) >= 8 and secret in text:
			counts["proxy-key"] += text.count(secret)
			text = text.replace(secret, MARK.format("proxy-key"))
	return _apply(text, _TOKEN_RULES, counts)


def redact_tokens(text: str, extra_secrets: Iterable[str] = ()) -> tuple[str, Counter]:
	"""Token-shape rules only (no name-based rules): for identifier fields such as tags and document ids."""
	_check_size(text)
	counts: Counter = Counter()
	if not text:
		return text, counts
	return _redact_shapes(text, extra_secrets, counts), counts


def redact_text(text: str, extra_secrets: Iterable[str] = ()) -> tuple[str, Counter]:
	"""Return `text` with secrets replaced by `[REDACTED:<kind>]`, and a Counter of replacements per kind."""
	_check_size(text)
	counts: Counter = Counter()
	if not text:
		return text, counts
	text = _redact_shapes(text, extra_secrets, counts)
	return _apply(text, _NAME_RULES, counts), counts


_RUN = re.compile(r"(?<![A-Za-z0-9+/_=\-])[A-Za-z0-9+/_=\-]{24,}")
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_SEGMENTS = re.compile(r"[/_\-+=]+")
_RESIDUAL_URL = re.compile(r"://[^/\s@:]{0,256}:(?P<val>[^@\s/]{1,256})@")
_RESIDUAL_ASSIGN = re.compile(
	r"(?i)(?:secret|token|password|passwd|api[_-]?key|private[_-]?key)(?P<sep>\W{0,3})(?P<val>\S{8,256})"
)
_ASSIGN_PREFIX = re.compile(r"[\w.\-]{0,128}[=:]{1,2}")
# Literal words used in documentation examples such as scheme://user:password@host.
_EXAMPLE_PASSWORDS = {"password", "pass", "passwd", "pwd", "pw", "secret"}


def _alnum_mix(value):
	return _has_digit(value) and any(c.isalpha() for c in value)


def _key_piece(piece):
	"""A long piece that mixes letters and digits often, as random keys do; words such as SchoolManagementEC2
	or hex ids such as i-082c37fe509909a9d do not."""
	if len(piece) < 16 or _HEX.fullmatch(piece) or not _alnum_mix(piece):
		return False
	kinds = ["d" if c.isdigit() else "u" if c.isupper() else "l" for c in piece]
	changes = sum(a != b for a, b in itertools.pairwise(kinds))
	return changes * 3 >= len(piece)


def _entropy(s):
	n = len(s)
	return -sum(c / n * math.log2(c / n) for c in Counter(s).values())


def residual_findings(text: str) -> list[tuple[str, int]]:
	"""What still looks secret in (already redacted) text: a list of (kind, character offset)."""
	found = []
	for m in _RUN.finditer(text):
		run = m.group(0)
		if text.endswith("[REDACTED:", 0, m.start()):
			continue
		if (_HEX.fullmatch(run) and len(run) <= 64) or _UUID.fullmatch(run):
			continue
		# Names, paths, URLs, settings and cloud ids (OMP_SOME_FLAG=1, blog/13878/web-push, eipalloc-0c89…)
		# are words or short/hex pieces between separators; a key has at least one long random-looking piece.
		if not any(_key_piece(p) for p in _SEGMENTS.split(run)):
			continue
		if _entropy(run) >= 4.0:
			found.append(("high-entropy", m.start()))
	for m in _RESIDUAL_URL.finditer(text):
		value = m.group("val")
		if "[REDACTED" in m.group(0) or value.lower() in _EXAMPLE_PASSWORDS or not _url_password(m, value):
			continue
		found.append(("url-credentials", m.start()))
	for m in _RESIDUAL_ASSIGN.finditer(text):
		value = m.group("val").strip("\"'`,;!\\()[]{}")
		prefix = _ASSIGN_PREFIX.match(value)
		if prefix:
			value = value[prefix.end() :].strip("\"'`\\()[]{}")
		elif not any(c in m.group("sep") for c in ":="):
			continue  # prose ("the token, base64/...") rather than an assignment
		if "[REDACTED" in value or not (_secret_value(value) and _alnum_mix(value)):
			continue
		found.append(("secret-assignment", m.start()))
	return sorted(found, key=lambda f: f[1])


# Identifier positions (document ids, tags, top-level mental model id) must match this; anything else is
# refused rather than forwarded, so an id or tag can never carry free text past the name-based rules.
_ID = re.compile(r"[A-Za-z0-9:_.\-/@ ]{1,200}")
_MEDIA_TYPE = re.compile(r"image/[A-Za-z0-9.+\-]{1,64}")
_BASE64_DATA = re.compile(r"[A-Za-z0-9+/=_\-\s]*")


def redact_json(value, extra_secrets: Iterable[str] = (), counts: Counter | None = None):
	"""Redact every string in a decoded JSON request body. Returns (value, counts).

	Every string gets `redact_text`, except identifier positions of the Hindsight request shapes, which get the
	token-shape rules only (`redact_tokens`) so document ids stay stable and a resend replaces rather than
	duplicates: top-level async, budget, tags_match, update_mode, types[*], tags[*], document_tags[*], id,
	trigger.mode; items[*].document_id, timestamp, tags[*], update_mode, entities[*].type. Base64 image blocks in
	items[*].content keep `type`, `source.data` and `source.media_type` byte-identical. Ids and tags that do not
	match `_ID` raise RejectBody.
	"""
	counts = Counter() if counts is None else counts
	extra = tuple(extra_secrets)

	def full(s):
		out, c = redact_text(s, extra)
		counts.update(c)
		return out

	def tokens(v):
		if not isinstance(v, str):
			return walk(v)
		out, c = redact_tokens(v, extra)
		counts.update(c)
		return out

	def ident(v):
		if isinstance(v, str) and not _ID.fullmatch(v):
			raise RejectBody("invalid identifier")
		return tokens(v)

	def tag(v):
		if not isinstance(v, str):
			raise RejectBody("tags must be strings")
		return ident(v)

	def tag_list(v):
		if v is None:
			return v
		if not isinstance(v, list):
			raise RejectBody("tags must be a list")
		return [tag(t) for t in v]

	def token_list(v):
		return [tokens(x) for x in v] if isinstance(v, list) else walk(v)

	def keep(v):
		return v

	def obj(d, special):
		out = {}
		for k, x in d.items():
			fn = special.get(k)
			if fn is not None:
				out[k] = fn(x)
			elif isinstance(x, str) and _secret_name(k):
				_check_size(x)
				if _secret_value(x):
					counts["assignment"] += 1
					out[k] = MARK.format("assignment")
				else:
					out[k] = full(x)
			else:
				out[k] = walk(x)
		return out

	def walk(v):
		if isinstance(v, str):
			return full(v)
		if isinstance(v, list):
			return [walk(x) for x in v]
		if isinstance(v, dict):
			return obj(v, {})
		return v

	def is_image(b):
		if not isinstance(b, dict):
			return False
		source = b.get("source")
		return b.get("type") == "image" and isinstance(source, dict) and source.get("type") == "base64"

	def image(b):
		source = b["source"]
		media, data = source.get("media_type"), source.get("data")
		if not (isinstance(media, str) and _MEDIA_TYPE.fullmatch(media)):
			raise RejectBody("invalid image media type")
		if not (isinstance(data, str) and _BASE64_DATA.fullmatch(data)):
			raise RejectBody("image data is not base64")
		return obj(b, {"type": keep, "source": lambda s: obj(s, {"data": keep, "media_type": keep})})

	def content(v):
		if not isinstance(v, list):
			return walk(v)
		return [image(b) if is_image(b) else walk(b) for b in v]

	def entities(v):
		if not isinstance(v, list):
			return walk(v)
		return [obj(e, {"type": tokens}) if isinstance(e, dict) else walk(e) for e in v]

	item_fields = {
		"document_id": ident,
		"timestamp": tokens,
		"update_mode": tokens,
		"tags": tag_list,
		"entities": entities,
		"content": content,
	}

	def items(v):
		if not isinstance(v, list):
			return walk(v)
		return [obj(i, item_fields) if isinstance(i, dict) else walk(i) for i in v]

	def trigger(v):
		return obj(v, {"mode": tokens}) if isinstance(v, dict) else walk(v)

	top_fields = {
		"async": tokens,
		"budget": tokens,
		"tags_match": tokens,
		"update_mode": tokens,
		"types": token_list,
		"tags": tag_list,
		"document_tags": tag_list,
		"id": ident,
		"trigger": trigger,
		"items": items,
	}
	if isinstance(value, dict):
		return obj(value, top_fields), counts
	return walk(value), counts
