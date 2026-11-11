"""The one place omp-memory prints: every byte of stdout and stderr passes through here.

The names a command shows (bank directories, folders, labels, file names, paths, project keys) come from the user's own
data, and a PIN assignment or an API key pasted into one is a secret that must not be echoed into a terminal, a log or a
pasted bug report. This module applies one policy to all output, the same one the importer applies to its reports
(vendor/memory_import/outputs.ts): a name that carries a recognised secret (detect.py) is replaced by an index label,
`bank#1`, `folder#2`, `name#3`, numbered by first appearance in this process, with nothing of the value in it.

What goes through it:
  * `out` / `err` / `write_out` / `write_err`: every line a command prints, and the lines of a child process (the importer, the
    bundled scripts) that runtime.py forwards;
  * `emit_json`: JSON documents, with the keys and values that are names labelled before they are serialised;
  * `name`: one name labelled by its kind (for a table cell or a sentence built from a bank name);
  * `ArgumentParser`: argparse writes usage text and error messages that echo the user's argument, so commands use this
    subclass, which hands them to the sink;
  * `install`: Python warnings and an uncaught exception are printed here too, as one categorical line without a traceback.

`tests/python/test_output_sink.py` fails when another module prints, writes to a standard stream, or starts a child
process whose output would reach the terminal unfiltered.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import threading
import warnings

from .detect import carries_secret
from .util import printable

WITHHELD = "(withheld: it looks like it carries a secret)"

_lock = threading.RLock()
_indexes: dict[str, dict[str, int]] = {}


# -- labels ---------------------------------------------------------------------------------------------------------
def reset_labels() -> None:
    """Forget the numbering (tests only: a process numbers the names it meets from 1 and never reuses a number)."""
    with _lock:
        _indexes.clear()


def label(kind: str, value: str) -> str:
    """`<kind>#N`: N is the order in which this process first met `value` among the names of that kind. Nothing of the value is in it."""
    with _lock:
        index = _indexes.setdefault(kind, {})
        number = index.get(value)
        if number is None:
            number = index[value] = len(index) + 1
        return f"{kind}#{number}"


def single_line(text: str) -> str:
    """`text` with every control character (a line break and a tab too) replaced by `?`: a name is one line."""
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", "?", text)


def name(value: object, kind: str = "name") -> str:
    """`value` as a name may be shown: an index label when it carries a secret, else itself (control characters made `?`)."""
    text = single_line(str(value))
    return label(kind, text) if carries_secret(text) else text


# -- text -----------------------------------------------------------------------------------------------------------
_QUOTED = re.compile(r"\"((?:[^\"\\\n]|\\.)*)\"|'([^'\n]*)'")
_TOKEN = re.compile(r"\S+")
_EDGE = re.compile(r"^([(\[{<\"'`]*)(.*?)([)\]}>\"'`.,;:!?]*)$", re.DOTALL)
_CELL_GAP = re.compile(r"(\t| {2,})")


def _kind_of(span: str) -> str:
    return "folder" if "/" in span or span.startswith("~") else "name"


def _quoted(line: str) -> str:
    def replace(match: re.Match) -> str:
        inner = match.group(1) if match.group(1) is not None else match.group(2)
        if inner and carries_secret(inner):
            quote = match.group(0)[0]
            return f"{quote}{label(_kind_of(inner), inner)}{quote}"
        return match.group(0)

    return _QUOTED.sub(replace, line)


def _tokens(line: str) -> str:
    def replace(match: re.Match) -> str:
        lead, core, trail = _EDGE.match(match.group(0)).groups()
        if core and carries_secret(core):
            return lead + label(_kind_of(core), core) + trail
        return match.group(0)

    return _TOKEN.sub(replace, line)


def _cells(line: str) -> str:
    parts = _CELL_GAP.split(line)
    for index in range(0, len(parts), 2):
        if parts[index] and carries_secret(parts[index]):
            parts[index] = label(_kind_of(parts[index]), parts[index])
    return "".join(parts)


def _scrub_body(line: str) -> str:
    """One line, narrowed step by step until it no longer carries a secret: quoted strings, then words, then table cells,
    then the whole line."""
    if not carries_secret(line):
        return line
    for step in (_quoted, _tokens, _cells):
        line = step(line)
        if not carries_secret(line):
            return line
    indent = line[: len(line) - len(line.lstrip())]
    return indent + WITHHELD


def scrub(text: str) -> str:
    """`text` as it may be shown: line by line, what carries a secret is replaced by an index label (or the line is withheld
    when it cannot be narrowed down), and terminal control characters are made `?`. Ordinary text is returned unchanged."""
    return "\n".join(printable(_scrub_body(line)) for line in text.replace("\r\n", "\n").split("\n"))


# -- JSON -----------------------------------------------------------------------------------------------------------
BANK_KEYS = frozenset({"bank", "banks", "emptyBanks", "stale", "bankIds", "perBank", "backed_up", "exempt"})
PATH_WORDS = ("path", "dir", "file", "folder", "label", "root", "cwd", "home")


def _kind_for_key(key: str) -> str:
    if key in BANK_KEYS:
        return "bank"
    lowered = key.lower()
    return "folder" if any(word in lowered for word in PATH_WORDS) else "name"


def _string(value: str, key: str) -> str:
    kind = _kind_for_key(key)
    if kind == "name" and re.search(r"\s", value):
        return scrub(value)  # a sentence (a note, an error): only the part that carries the secret goes
    return name(value, kind)


def clean(value: object, key: str = "") -> object:
    """A copy of JSON-like data in which every string that carries a secret (a value or a key) is an index label."""
    if isinstance(value, str):
        return _string(value, key)
    if isinstance(value, dict):
        key_kind = "bank" if key in BANK_KEYS else "name"
        return {name(k, key_kind): clean(v, str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [clean(item, key) for item in value]
    return value


def emit_json(data: object) -> None:
    """Print a JSON document: its strings are labelled one by one, never judged as lines (a line such as `"key": "<a path>"`
    reads like an assignment, and withholding it would break the document)."""
    _write(sys.stdout, json.dumps(clean(data), indent=2, ensure_ascii=False) + "\n", lines=False)


_COMPACT_START = ('{"', '["', "[{")


def starts_document(line: str) -> bool:
    """Whether a line of a child's stdout begins a JSON document: a lone `{` or `[` (the pretty-printed form), or an object or array
    that opens straight into a key or an element (the compact form)."""
    text = line.strip()
    return text in ("{", "[") or text.startswith(_COMPACT_START)


def _indent_of(block: str):
    """The indentation a pretty-printed document uses (a tab, or the number of spaces), or None when it is on one line."""
    rows = block.split("\n")
    if len(rows) < 2:
        return None
    lead = rows[1][: len(rows[1]) - len(rows[1].lstrip())]
    return "\t" if lead.startswith("\t") else len(lead) or 2


def write_document(text: str, show=None) -> None:
    """Write the stdout of a child that begins with a JSON document. The document is parsed, every string value and key in it is
    labelled when it carries a secret, and it is written again with the indentation it had, so it stays a document. Whatever
    does not parse, and whatever follows the document, is written line by line."""
    body = text.lstrip()
    try:
        document, end = json.JSONDecoder().raw_decode(body)
    except ValueError:
        document = None
    if document is None:
        for line in text.splitlines(keepends=True):
            write_out(show(line) if show else line)
        return
    block = body[:end]
    indent = _indent_of(block)
    spaced = indent is None and '": ' in block  # a one-line document keeps the spacing it had
    shown = json.dumps(
        clean(document),
        indent=indent,
        ensure_ascii=False,
        separators=((", ", ": ") if spaced else (",", ":")) if indent is None else None,
    )
    _write(sys.stdout, shown + "\n", lines=False)
    for line in body[end:].lstrip("\r\n").splitlines(keepends=True):
        write_out(show(line) if show else line)


# -- writing --------------------------------------------------------------------------------------------------------
def _write(stream, text: str, lines: bool = True) -> None:
    data = scrub(text) if lines else printable(text)
    with _lock:
        stream.write(data)
        stream.flush()


def write_out(text: str) -> None:
    """Write `text` (already complete lines, or a partial one) to stdout, scrubbed."""
    _write(sys.stdout, text)


def write_err(text: str) -> None:
    _write(sys.stderr, text)


def out(*parts: object, sep: str = " ", end: str = "\n") -> None:
    write_out(sep.join(str(part) for part in parts) + end)


def err(*parts: object, sep: str = " ", end: str = "\n") -> None:
    write_err(sep.join(str(part) for part in parts) + end)


# -- argparse, warnings, uncaught errors ----------------------------------------------------------------------------
class ArgumentParser(argparse.ArgumentParser):
    """argparse that writes its usage text, help and error messages (which echo the user's arguments) through the sink."""

    def __init__(self, *args, **kwargs):
        if sys.version_info >= (3, 14):
            kwargs.setdefault("color", False)
        super().__init__(*args, **kwargs)

    def _print_message(self, message, file=None):
        if message:
            (write_out if file is sys.stdout else write_err)(message)


EXIT_INTERNAL = (
    3  # a bug: an error nobody handled, in the main thread, a thread, a finaliser, or while loading a command
)

_state = {"crashed": False, "signal": 0}


def crashed() -> bool:
    """Whether an internal error has been reported in this process (the exit status is then 3)."""
    return _state["crashed"]


def signalled() -> int:
    """The number of the signal (SIGINT or SIGTERM) that stopped the command, or 0."""
    return _state["signal"]


def reset_state() -> None:
    _state.update(crashed=False, signal=0)


def internal_error(category: str) -> None:
    """The one line an internal error becomes: a category (an exception's type name, where it happened) and nothing else. The
    exception's message is never printed: it can carry anything the command was working on, a path, a name, a value."""
    _state["crashed"] = True
    try:
        err(f"omp-memory: internal error ({category})")
    except (OSError, ValueError):
        pass  # the terminal is gone; there is nobody to tell


def unexpected_error(exc: BaseException) -> None:
    internal_error(type(exc).__name__)


def _show_warning(message, category, filename, lineno, file=None, line=None):
    try:
        err(f"omp-memory: warning ({category.__name__}; details withheld)")
    except (OSError, ValueError):
        pass


def _unexpected(exc_type, exc, tb):
    internal_error(exc_type.__name__)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError):
            pass
    os._exit(
        EXIT_INTERNAL
    )  # an exception that escaped everything: the interpreter would exit with 1 after printing a traceback


def _thread_unexpected(args) -> None:
    if args.exc_type is not SystemExit:
        internal_error(f"thread: {args.exc_type.__name__}")


def _unraisable(hook_args) -> None:
    kind = hook_args.exc_type.__name__ if hook_args.exc_type is not None else "error"
    internal_error(f"unraisable: {kind}")


def _on_signal(signum, frame) -> None:
    _state["signal"] = signum
    raise KeyboardInterrupt


def install() -> None:
    """Route Python warnings, uncaught errors (main thread, other threads, finalisers) and SIGINT and SIGTERM through the sink: no
    traceback, no message of the exception, a categorical line, and an exit status the caller can tell (3, or 128 + the signal)."""
    warnings.showwarning = _show_warning
    sys.excepthook = _unexpected
    threading.excepthook = _thread_unexpected
    sys.unraisablehook = _unraisable
    try:
        signal.signal(signal.SIGINT, _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)
    except (ValueError, OSError):
        pass  # not the main thread (a test harness): the default handlers stay
