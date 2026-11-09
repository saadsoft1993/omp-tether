#!/usr/bin/env python3
"""A scratch environment to drive the dashboard page by hand or from a browser: the real-omp setup of test_real_omp.py (two live omp sessions with
the control extension, a scratch relay, a hub on a free port), left running. Writes {"port", "pair"} to <dir>/env.json, reads one command per
line from <dir>/cmd (`type a TEXT` / `type b TEXT` types TEXT into session a or b), and tears everything down when <dir>/stop appears.

    python3 browser_env.py <dir>
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_real_omp as t

directory = Path(sys.argv[1])
directory.mkdir(parents=True, exist_ok=True)
cls = t.DashboardInRealOmp
cls.setUpClass()
try:
    token = cls.app.pairing.writer.create_token()
    (directory / "env.json").write_text(
        json.dumps(
            {
                "port": cls.hub_port,
                "pair": f"http://127.0.0.1:{cls.hub_port}/#pair={token}",
                "repo": str(cls.scratch.repo),
            }
        )
    )
    done = 0
    while not (directory / "stop").exists():
        cmd = directory / "cmd"
        lines = cmd.read_text().splitlines() if cmd.exists() else []
        for line in lines[done:]:
            kind, which, _, text = (
                line.partition(" ")[2].partition(" ")[0],
                line.split(" ")[1],
                "",
                " ".join(line.split(" ")[2:]),
            )
            (cls.a if which == "a" else cls.b).type(text)
        done = len(lines)
        time.sleep(0.5)
finally:
    cls.tearDownClass()
