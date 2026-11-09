#!/usr/bin/env python3
"""Entry point for `python omp_dashboard_main.py <command> ...` (the launcher, omp-services' unit and the tests use it).

This file is deliberately written in syntax that every Python 3 can parse, so that an old interpreter gets a clear
message instead of a SyntaxError from the package.
"""

import os
import sys

if sys.version_info < (3, 10):
    sys.stderr.write(
        "omp-dashboard needs Python 3.10 or newer; this is Python %d.%d (%s).\n"
        "Install a newer Python (Homebrew, uv, python.org, your package manager) and run it with that,\n"
        "or set OMP_DASHBOARD_PYTHON to its absolute path. On macOS, /usr/bin/python3 is too old.\n"
        % (sys.version_info[0], sys.version_info[1], sys.executable)
    )
    sys.exit(2)

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from omp_dashboard.cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
