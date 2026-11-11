#!/usr/bin/env python3
"""Entry point for `python omp_memory_main.py <command> ...` (the launcher and the tests use it).

This file is deliberately written in syntax that every Python 3 can parse, so that an old interpreter gets a clear
message instead of a SyntaxError from the package. It runs before the output filter can be loaded, so everything it
prints is fixed text: never a path, an interpreter name or an exception's message. It loads the filter (omp_memory/sink.py,
which installs the hooks for uncaught errors, warnings and signals) first, and only then the commands.
"""

import os
import sys

if sys.version_info < (3, 10):
    sys.stderr.write(
        "omp-memory needs Python 3.10 or newer.\n"
        "Install a newer Python (Homebrew, uv, python.org, your package manager) and run it with that,\n"
        "or set OMP_MEMORY_PYTHON to the absolute path of one. On macOS, /usr/bin/python3 is too old.\n"
    )
    sys.exit(2)

sys.dont_write_bytecode = True


def start():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        from omp_memory import sink

        sink.install()
    except Exception:
        sys.stderr.write("omp-memory: internal error (the plugin installation is incomplete)\n")
        return 3
    try:
        from omp_memory import cli
    except Exception:
        sink.internal_error("start-up")
        return sink.EXIT_INTERNAL
    code = cli.main(sys.argv[1:])
    if sink.crashed():
        return sink.EXIT_INTERNAL
    if sink.signalled():
        return 128 + sink.signalled()
    return code


if __name__ == "__main__":
    sys.exit(start())
