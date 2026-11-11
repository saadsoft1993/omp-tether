"""Test kit: a scratch HOME with fake banks (the real mnemopi schema), stand-in `bun` and `omp`, and the launcher run as a
child process. Nothing here touches the real home directory, a real bank, the network or a fixed port."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2]
PYTHON_DIR = PLUGIN / "python"
VENDOR = PLUGIN / "vendor"
LAUNCHER = PLUGIN / "bin" / "omp-memory"
sys.dont_write_bytecode = True
sys.path.insert(0, str(PYTHON_DIR))
sys.path.insert(0, str(VENDOR))

from omp_hub.test_memory_schema import make_bank  # the real schema, copied from the upstream checkout

FAKE_BUN = """#!/bin/sh
if [ "$1" = "--version" ]; then echo "${FAKE_BUN_VERSION:-1.4.4}"; exit 0; fi
printf '%s\\n' "$@" > "$FAKE_BUN_LOG"
printf '%s ' "$@" >> "$FAKE_BUN_LOG.calls"; echo >> "$FAKE_BUN_LOG.calls"
pwd -P > "$FAKE_BUN_LOG.cwd"
env > "$FAKE_BUN_LOG.env"
if [ -n "${FAKE_BUN_STDOUT:-}" ]; then cat "$FAKE_BUN_STDOUT"; fi
exit "${FAKE_BUN_EXIT:-0}"
"""
FAKE_OMP = """#!/bin/sh
echo "omp/${FAKE_OMP_VERSION:-18.18.0}"
"""
# Variables of the developer's own environment that would change what the commands see.
SCRUBBED = ("OMP_", "PI_", "BUN_", "XDG_", "OMP_MEMORY_")


class Result:
    def __init__(self, proc: subprocess.CompletedProcess):
        self.code = proc.returncode
        self.out = proc.stdout
        self.err = proc.stderr

    def json(self):
        return json.loads(self.out)


class Scratch:
    """A throwaway HOME. Use as a context manager (or call close())."""

    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="ompmem-test-", dir="/tmp")).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.banks = self.home / ".omp" / "agent" / "memories" / "mnemopi" / "banks"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.fake_bun = self._script("bun", FAKE_BUN)
        self.fake_omp = self._script("omp", FAKE_OMP)
        self.bun_log = self.root / "bun-argv.txt"

    def _script(self, name: str, body: str) -> Path:
        path = self.bin / name
        path.write_text(body)
        path.chmod(0o755)
        return path

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)

    # -- data ------------------------------------------------------------------------------------------------------
    def bank(self, name: str, folder: str | None = "work/alpha", wm=(("w1", "note one", "bank"),), **kw) -> Path:
        cwd = str(self.home / folder) if folder else ""
        make_bank(self.banks, name, wm=wm, cwd=cwd, **kw)
        return self.banks / name / "mnemopi.db"

    def claude_memory(self, project: str, files: dict[str, str], root: str = ".claude") -> Path:
        directory = self.home / root / "projects" / project / "memory"
        directory.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            (directory / name).write_text(text)
        return directory

    def codex_db(self, rows: int) -> Path:
        path = self.home / ".codex" / "memories_1.sqlite"
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE stage1_outputs (id INTEGER PRIMARY KEY, body TEXT)")
        db.executemany("INSERT INTO stage1_outputs (body) VALUES (?)", [("x",)] * rows)
        db.commit()
        db.close()
        return path

    def claude_mem_db(self, observations=2, summaries=1, prompts=3) -> Path:
        path = self.home / ".claude-mem" / "claude-mem.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path)
        for table, rows in (
            ("observations", observations),
            ("session_summaries", summaries),
            ("user_prompts", prompts),
        ):
            db.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, body TEXT)")
            db.executemany(f"INSERT INTO {table} (body) VALUES (?)", [("x",)] * rows)
        db.commit()
        db.close()
        return path

    # -- running ---------------------------------------------------------------------------------------------------
    def env(self, **extra: str) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith(SCRUBBED)}
        env.update(HOME=str(self.home), FAKE_BUN_LOG=str(self.bun_log), PYTHONDONTWRITEBYTECODE="1")
        env.update(extra)
        return env

    def run(
        self, *args: str, env: dict[str, str] | None = None, stdin: str | None = None, cwd: Path | None = None
    ) -> Result:
        proc = subprocess.run(
            [str(LAUNCHER), *args],
            env=env if env is not None else self.env(),
            capture_output=True,
            text=True,
            input=stdin,
            timeout=120,
            check=False,
            cwd=cwd,
        )
        return Result(proc)

    def wired(self, **extra: str) -> dict[str, str]:
        """An environment in which `import` finds the stand-in bun and omp."""
        wired = {"OMP_MEMORY_BUN": str(self.fake_bun), "OMP_BIN": str(self.fake_omp)}
        return self.env(**{**wired, **extra})

    def bun_stdout(self, text: str) -> dict[str, str]:
        """Make the stand-in bun print `text` on stdout; returns the environment entry that does it."""
        path = self.root / "bun-stdout.txt"
        path.write_text(text)
        return {"FAKE_BUN_STDOUT": str(path)}

    @property
    def state_file(self) -> Path:
        return self.home / ".config" / "omp-memory" / "state.json"

    def bun_argv(self) -> list[str]:
        return self.bun_log.read_text().splitlines()

    def bun_cwd(self) -> str:
        """The working directory the stand-in Bun was started in."""
        return Path(f"{self.bun_log}.cwd").read_text().strip()

    def forget_bun_calls(self) -> None:
        Path(f"{self.bun_log}.calls").unlink(missing_ok=True)

    def bun_calls(self) -> list[str]:
        """One line per time the stand-in Bun ran (its arguments, space separated), oldest first."""
        path = Path(f"{self.bun_log}.calls")
        return path.read_text().splitlines() if path.exists() else []

    def bun_env(self) -> dict[str, str]:
        return dict(line.split("=", 1) for line in Path(f"{self.bun_log}.env").read_text().splitlines() if "=" in line)


class ScratchCase(unittest.TestCase):
    def setUp(self):
        self.s = Scratch()
        self.addCleanup(self.s.close)
