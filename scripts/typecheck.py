#!/usr/bin/env python3
"""Type-checks the extension and the test kit against omp's own types, and fails closed.

Needs a checkout of omp at the tag you target, with dependencies installed:
    OMP_SRC=/path/to/oh-my-pi scripts/typecheck.py            (TSC=... overrides the compiler path)

omp's own sources are checked too, because the files import them, and some of them need build-time shims we do not
have; their diagnostics are tolerated *only* when the compiler ran, listed both of our files, reported at least one
well-formed diagnostic, and none of the diagnostics is in this repository's files. Everything else is a failure: a
compiler that cannot start, crashes, is killed or times out, a configuration error, a failure with no diagnostics, an
unrecognised diagnostic line, or one of our files missing from the compiler's file list.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELAY = "plugins/omp-private-relay"
TARGETS = [
    "plugins/omp-session-control/src/omp-session-control.ts",
    "plugins/omp-services/src/omp-services.ts",
    "tools/testkit/fake-provider.ts",
    f"{RELAY}/src/cli.ts",
    f"{RELAY}/src/main.ts",
    f"{RELAY}/scripts/web-asset.ts",
    f"{RELAY}/tests/conformance/pairing-store-suite.ts",
    f"{RELAY}/tests/conformance/adapter.ts",
    f"{RELAY}/tests/mutation/mutation.test.ts",
    *sorted(str(p.relative_to(ROOT)) for p in (ROOT / RELAY / "tests" / "unit").rglob("*.ts")),
]
DIAGNOSTIC = re.compile(r"^(?P<file>.+?)\((?P<line>\d+),(?P<col>\d+)\): error (?P<code>TS\d+): ")
TIMEOUT_SECONDS = 600


def classify(returncode: int, output: str, cwd: Path, root: Path, targets: list[str]) -> tuple[bool, str]:
    """(ok, message) for one compiler run. `output` is stdout and stderr, produced with --listFiles --pretty false."""
    listed = {
        Path(line.strip()).resolve() for line in output.splitlines() if line.strip().endswith(".ts") and "(" not in line
    }
    missing = [t for t in targets if (root / t).resolve() not in listed]
    if missing:
        return False, f"the compiler did not list our file(s): {', '.join(missing)}"
    if returncode == 0:
        return True, "no diagnostics"
    if returncode not in (1, 2):
        return False, f"the compiler exited with status {returncode}, which is not a type-check result"
    error_lines = [line for line in output.splitlines() if re.search(r"(?:^|\s)error TS\d+", line)]
    diagnostics = []
    for line in error_lines:
        m = DIAGNOSTIC.match(line)
        if not m:
            return False, f"unrecognised compiler error (configuration or invocation problem): {line[:200]}"
        diagnostics.append(((cwd / m["file"]).resolve(), m["code"]))
    if not diagnostics:
        return False, f"the compiler failed (status {returncode}) without reporting a diagnostic"
    ours = [(p, c) for p, c in diagnostics if root.resolve() in p.parents]
    if ours:
        shown = "; ".join(f"{p.relative_to(root.resolve())} {c}" for p, c in ours[:5])
        return False, f"{len(ours)} diagnostic(s) in this repository's files: {shown}"
    return True, f"{len(diagnostics)} diagnostic(s), all in omp's own sources"


def main() -> int:
    omp_src = os.environ.get("OMP_SRC")
    if not omp_src:
        print("error: set OMP_SRC to a checkout of omp with dependencies installed", file=sys.stderr)
        return 2
    omp = Path(omp_src).resolve()
    tsc = os.environ.get("TSC") or str(omp / "node_modules" / ".bin" / "tsc")
    if not (os.path.isfile(tsc) and os.access(tsc, os.X_OK)):
        print(f"error: tsc not found or not executable at {tsc} (set TSC)", file=sys.stderr)
        return 2
    config = {
        "compilerOptions": {
            "target": "ESNext",
            "module": "ESNext",
            "moduleResolution": "bundler",
            "strict": True,
            "noEmit": True,
            "skipLibCheck": True,
            "allowImportingTsExtensions": True,
            "types": ["bun"],
            "typeRoots": [str(omp / "node_modules" / "@types")],
            "paths": {
                "@oh-my-pi/pi-coding-agent": [str(omp / "packages/coding-agent/src/index.ts")],
                "@oh-my-pi/pi-coding-agent/*": [str(omp / "packages/coding-agent/src/*")],
                "@oh-my-pi/pi-ai": [str(omp / "packages/ai/src/index.ts")],
                "@oh-my-pi/pi-utils": [str(omp / "packages/utils/src/index.ts")],
                "@oh-my-pi/pi-utils/*": [str(omp / "packages/utils/src/*")],
            },
        },
        "files": [str(ROOT / t) for t in TARGETS],
    }
    with tempfile.TemporaryDirectory(prefix="omp-tether-tsc.") as tmp:
        cfg = Path(tmp) / "tsconfig.json"
        cfg.write_text(json.dumps(config))
        try:
            run = subprocess.run(
                [tsc, "-p", str(cfg), "--pretty", "false", "--listFiles"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"type check FAILED: the compiler did not run to completion ({type(exc).__name__})", file=sys.stderr)
            return 1
    ok, message = classify(run.returncode, run.stdout + run.stderr, ROOT, ROOT, TARGETS)
    if not ok:
        print(f"type check FAILED: {message}", file=sys.stderr)
        return 1
    print(f"type check passed ({message})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
