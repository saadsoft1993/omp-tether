"""Does a name carry a secret? The Python mirror of the importer's `carriesSecret` (vendor/memory_import/hold.ts).

A bank directory, a folder, a label, a file name or a path is not secret by intent, but a PIN assignment (`password=493817`)
or an API key pasted into one is. The importer withholds such names from its reports; this module lets the rest of
omp-memory's output do the same (see sink.py).

A string is judged on the raw text and in each form a separator or an escape could hide it in: as written, with its
percent escapes decoded (twice at most), and each of those in lower case; the whole string and each path segment by itself.
In each piece the rules are the importer's HOLD rules (ported below), the vendored proxy redactor (`redact_text`, and
the part of `residual_findings` that is about assignments and URL credentials), and a key word followed by a value in a
name. Names such as `token-service`, `Secret Santa`, `password-policy` and `tokenizer-2` are not findings.
"""

from __future__ import annotations

import re
import sys
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote

# The same vendor directory core.py puts on sys.path (not imported from there: core loads the whole memory library).
VENDOR = Path(__file__).resolve().parents[2] / "vendor"
if str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))

from hindsight_proxy.redact import RejectBody, redact_text, residual_findings

# -- the HOLD rules of hold.ts --------------------------------------------------------------------------------------
PASSWORD_WORD = re.compile(r"\b(?:password|passwd|pwd|passphrase|secret|token|api[_-]?key)\b", re.IGNORECASE)
# a separator within 40 characters: `:` `=` `is` `->`; the gap itself may not contain link or call punctuation
SEPARATOR = re.compile(r"^([^\n\]()]{0,40}?)(?::|=|\bis\b|->)\s*(\S{4,})", re.IGNORECASE)
PAIR = re.compile(r"\b(?:login|ssh|username|user|root|admin)\b[^\n]{0,40}?[\w.@+-]+\s+/\s+(\S{4,})", re.IGNORECASE)
PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY(?: BLOCK)?-----")
BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}")
PREFIX = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{10,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{10,}"
    r"|xox[bp]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{12,})"
)
URL_CREDENTIALS = re.compile(r":\/\/[^\s/@:]+:([^\s/@]{3,})@")
KEY_WORD = re.compile(r"\b(?:key|token|secret)\b", re.IGNORECASE)
LONG_TOKEN = re.compile(r"^[^\n]{0,30}?(?<![A-Za-z0-9_-])([A-Za-z0-9_-]{24,})(?![A-Za-z0-9_-])")
EXAMPLE_PASSWORDS = {"password", "pass", "passwd", "pwd", "secret", "xxxx", "xxxxx", "****"}
LEADING = re.compile(r"^[`'\"([{*_~]+")
TRAILING = re.compile(r"[`'\")\]}*_~,.;:?]+$")
PLACEHOLDER_START = re.compile(r"^(<|\$|\{|//|https?:)", re.IGNORECASE)


def looks_like_secret(raw: str) -> bool:
    """A value that could be a secret: not a placeholder or URL, and with a digit, a symbol or mixed case."""
    value = TRAILING.sub("", LEADING.sub("", raw))
    if len(value) < 4:
        return False
    if raw.startswith("[REDACTED") or PLACEHOLDER_START.match(re.sub(r"^[`'\"*_~]+", "", raw)):
        return False
    return bool(
        re.search(r"[0-9]", value)
        or re.search(r"[^A-Za-z0-9]", value)
        or (re.search(r"[a-z]", value) and re.search(r"[A-Z]", value))
    )


def find_holds(text: str) -> bool:
    """Whether any HOLD rule of the importer matches `text`."""
    for match in PASSWORD_WORD.finditer(text):
        parsed = SEPARATOR.match(text[match.end() :])
        if parsed and looks_like_secret(parsed.group(2)):
            return True
    for match in PAIR.finditer(text):
        if looks_like_secret(match.group(1)):
            return True
    if PRIVATE_KEY.search(text) or BEARER.search(text) or PREFIX.search(text):
        return True
    url = URL_CREDENTIALS.search(text)
    if url and url.group(1).lower() not in EXAMPLE_PASSWORDS and not url.group(1).startswith("[REDACTED"):
        return True
    for match in KEY_WORD.finditer(text):
        token = LONG_TOKEN.match(text[match.end() :])
        if token:
            value = token.group(1)
            if (re.search(r"[a-z]", value) and re.search(r"[A-Z]", value)) or re.search(r"[0-9]", value):
                return True
    return False


# -- a key word and a value in a name -------------------------------------------------------------------------------
# The importer's list, plus `credential` and `auth`.
KEYWORDS = ("password", "passwd", "pwd", "passphrase", "secret", "token", "apikey", "credentials", "credential", "auth")
FILLER = ("is", "are", "was", "equals", "eq")
SPLIT = re.compile(r"[\W_]+")


def value_in_name(value: str) -> bool:
    """Three or more characters with a digit, or four or more with a capital after the first letter."""
    return (len(value) >= 3 and re.search(r"[0-9]", value) is not None) or (
        len(value) >= 4 and re.search(r"[a-z]", value) is not None and re.search(r"[A-Z]", value[1:]) is not None
    )


def keyword_with_value(text: str) -> bool:
    """A key word followed by a value, whatever separates them (`=`, `:`, `-`, `_`, `.`, a space, any other character):
    `password-123456`, `Token_a1B2c3`, `api key 9f8e`, `secret.2024`, and a number glued to the word (`password123456`).
    A word that only begins like a key word, or is followed by a plain word (`token-service`), is not a finding."""
    tokens = [t for t in SPLIT.split(text) if t]
    for index, token in enumerate(tokens):
        word = token.lower()
        nxt = index + 1
        if word == "api" and nxt < len(tokens) and tokens[nxt].lower() == "key":
            word = "apikey"
            nxt += 1
        if word in KEYWORDS:
            if nxt < len(tokens) and tokens[nxt].lower() in FILLER:
                nxt += 1
            if nxt < len(tokens) and value_in_name(tokens[nxt]):
                return True
            continue
        glued = next((key for key in KEYWORDS if word.startswith(key)), None)
        if glued is not None and re.fullmatch(r"[0-9]{3,}", word[len(glued) :]):
            return True
    return False


# -- the whole judgement --------------------------------------------------------------------------------------------
def url_decoded(text: str) -> str:
    """`text` with its percent escapes decoded (twice at most; one escape at a time when the whole is malformed)."""
    out = text
    for _ in range(2):
        try:
            following = unquote(out, errors="strict")
        except UnicodeDecodeError:
            following = re.sub(r"%([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), out)
        if following == out:
            break
        out = following
    return out


def redaction_changes(piece: str) -> bool:
    """The proxy redactor would rewrite `piece`, or it still carries an assignment or URL credentials after redaction."""
    try:
        redacted, _ = redact_text(piece)
    except RejectBody:
        return True  # too large to judge: withheld
    if redacted != piece:
        return True
    return any(kind != "high-entropy" for kind, _ in residual_findings(piece))


@lru_cache(maxsize=8192)
def carries_secret(raw: str) -> bool:
    """Whether a string that is going to be shown (a bank, a folder, a label, a file name, a path, a project key) carries a
    secret, judged on the raw string, its percent-decoded form and the lower case of both, whole and per path segment."""
    decoded = url_decoded(raw)
    forms = dict.fromkeys((raw, decoded, raw.lower(), decoded.lower()))
    for form in forms:
        for piece in (form, *form.split("/")):
            if piece and (find_holds(piece) or redaction_changes(piece)):
                return True
    for form in dict.fromkeys((raw, decoded)):
        for segment in form.split("/"):
            if keyword_with_value(segment):
                return True
    return False
