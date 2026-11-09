"""Talking to the person at the terminal: shortening paths, asking for confirmation.

Nothing here assumes a terminal on stdin: the one-line installer pipes a script into `sh`, so a question is asked on
/dev/tty when stdin is not a terminal. With no terminal at all the answer is "no", never "yes".
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path


def tidy(path: Path | str, home: Path) -> str:
    """A path for display: `~` for the home directory."""
    text = str(path)
    prefix = str(home)
    if text == prefix:
        return "~"
    if text.startswith(prefix + os.sep):
        return "~" + text[len(prefix) :]
    return text


def ask(prompt: str) -> str | None:
    """Print `prompt` and read one line from the person. None when there is nobody to ask."""
    if sys.stdin is not None and sys.stdin.isatty():
        try:
            return input(prompt)
        except EOFError:
            return None
    try:
        tty = open("/dev/tty", "r+", encoding="utf-8")
    except OSError:
        return None
    with contextlib.closing(tty):
        try:
            tty.write(prompt)
            tty.flush()
            line = tty.readline()
        except OSError:
            return None
    return line.rstrip("\n") if line else None


def confirm(question: str) -> bool:
    answer = ask(f"{question} [y/N] ")
    return answer is not None and answer.strip().lower() in {"y", "yes"}


def confirm_word(question: str, word: str) -> bool:
    """For the one destructive action: the person has to type `word` in full."""
    answer = ask(f'{question}\nType "{word}" to continue: ')
    return answer is not None and answer.strip() == word
