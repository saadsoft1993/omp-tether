"""Helpers for tests that run a real omp in a scratch HOME: no network, no cost, nothing of the real account.

- ScratchHome: a throwaway HOME with a minimal omp config that selects the fake provider.
- ScratchRelay: tools/testkit/scratch-relay.ts on a random loopback port.
- PtyOmp: an interactive omp in a pseudo-terminal, driven like a person would.
- find_omp / find_bun: locate the binaries from OMP_BIN / BUN_BIN or PATH.

Standard library only; Python 3.10 or newer.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import tempfile
import termios
import threading
import time
import warnings
from pathlib import Path

KIT = Path(__file__).resolve().parent
FAKE_PROVIDER = KIT / "fake-provider.ts"
SCRATCH_RELAY = KIT / "scratch-relay.ts"
MODEL = "tethertest/lite"

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Z0-9]|\x1b[=>]")


def find_omp() -> str | None:
    """The omp binary: $OMP_BIN, else `omp` on PATH, else ~/.local/bin/omp."""
    for candidate in (os.environ.get("OMP_BIN"), shutil.which("omp"), str(Path.home() / ".local" / "bin" / "omp")):
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def find_bun() -> str | None:
    """Bun for the TypeScript helpers: $BUN_BIN, else `bun` on PATH."""
    for candidate in (os.environ.get("BUN_BIN"), shutil.which("bun")):
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def wait_for(fn, timeout: float = 60, step: float = 0.2, what: str = "condition"):
    """Poll `fn` until it returns something truthy; raise AssertionError after `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(step)
    raise AssertionError(f"timed out waiting for {what}")


RELAY_START_TIMEOUT = 30.0


class ScratchRelay:
    """scratch-relay.ts on a random free loopback port."""

    def __init__(self, bun: str):
        self.proc = subprocess.Popen(
            [bun, "--no-install", str(SCRATCH_RELAY)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        line = self._ready_line(RELAY_START_TIMEOUT)
        if not line.startswith("READY "):
            self.stop()
            raise RuntimeError(f"scratch relay did not start: {line!r}")
        self.port = int(line.split()[1])
        self.url = f"ws://127.0.0.1:{self.port}"

    def _ready_line(self, timeout: float) -> str:
        """The first line the relay prints, read with a deadline: a process that prints nothing cannot hang us."""
        assert self.proc.stdout
        fd = self.proc.stdout.fileno()
        deadline = time.monotonic() + timeout
        buf = b""
        while b"\n" not in buf and len(buf) < 4096:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                return buf.decode("utf-8", "replace").strip() or "(no output before the deadline)"
            chunk = os.read(fd, 1024)
            if not chunk:
                break
            buf += chunk
        return buf.split(b"\n")[0].decode("utf-8", "replace").strip()

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.proc.wait(5)
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        if self.proc.stdout:
            self.proc.stdout.close()


class ScratchHome:
    """A throwaway HOME. Socket paths are limited to about 100 bytes, so the root is a short directory under /tmp."""

    def __init__(self, relay_url: str | None = None, prefix: str = "osc"):
        self.root = Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp")).resolve()
        self.home = self.root / "h"
        self.repo = self.root / "repo"
        self.repo.mkdir(parents=True)
        self.omp_config_dir = self.home / ".omp" / "agent"
        self.omp_config_dir.mkdir(parents=True)
        self.relay_url = relay_url
        self.write_config()

    @property
    def env(self) -> dict[str, str]:
        omp = find_omp()
        return {
            "HOME": str(self.home),
            "PATH": f"{Path(omp).parent if omp else ''}:/usr/bin:/bin",
            "TERM": "xterm-256color",
            "LANG": "en_US.UTF-8",
            "OMP_TETHER_TEST_DIR": str(self.root),
        }

    def write_config(self, extra: str = "") -> None:
        collab = f"collab:\n  autoStart: control\n  relayUrl: {self.relay_url}\n" if self.relay_url else ""
        (self.omp_config_dir / "config.yml").write_text(
            'setupVersion: 3\nupdate:\n  auto: false\nmemory:\n  backend: "off"\n'
            f"modelRoles:\n  default: {MODEL}\n  smol: {MODEL}\n{collab}{extra}"
        )

    def cli(
        self, *args: str, timeout: float = 120, extra_env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess:
        """Run `omp <args>` in the scratch HOME and return the finished process (output captured as text)."""
        omp = find_omp()
        assert omp, "omp is not installed"
        return subprocess.run(
            [omp, *args],
            capture_output=True,
            text=True,
            env={**self.env, **(extra_env or {})},
            cwd=self.repo,
            timeout=timeout,
        )

    def install_plugin(self, marketplace_dir: Path, plugin: str, marketplace_name: str) -> str:
        """Add a local marketplace directory and install one plugin from it; returns omp's combined output."""
        out = []
        for args in (
            ("plugin", "marketplace", "add", str(marketplace_dir)),
            ("plugin", "install", f"{plugin}@{marketplace_name}"),
        ):
            r = self.cli(*args)
            out.append(r.stdout + r.stderr)
            if r.returncode != 0:
                raise RuntimeError(f"omp {' '.join(args)} failed ({r.returncode}):\n{out[-1]}")
        return "\n".join(out)

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class PtyOmp:
    """An interactive omp in a pseudo-terminal, with the fake provider loaded and nothing else of the real account."""

    def __init__(self, scratch: ScratchHome, extra_env: dict[str, str] | None = None, rows: int = 40, cols: int = 120):
        omp = find_omp()
        assert omp, "omp is not installed"
        self.scratch = scratch
        self.env = {**scratch.env, **(extra_env or {})}
        self.screen = bytearray()
        argv = [omp, "-e", str(FAKE_PROVIDER), "--model", MODEL, "--auto-approve", "--no-title"]
        with warnings.catch_warnings():
            # The child only changes directory and execs; the warning is about arbitrary code running after fork.
            warnings.simplefilter("ignore", DeprecationWarning)
            self.pid, self.fd = pty.fork()
        if self.pid == 0:  # child
            os.chdir(scratch.repo)
            os.execve(omp, argv, self.env)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.exited = threading.Event()
        threading.Thread(target=self._drain, daemon=True).start()
        threading.Thread(target=self._reap, daemon=True).start()

    def _reap(self) -> None:
        """Reap at once, so an exited omp never lingers as a zombie that still looks alive to `kill -0`."""
        os.waitpid(self.pid, 0)
        self.exited.set()

    def _drain(self) -> None:
        while True:
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                return
            if not data:
                return
            self.screen += data
            if b"\x1b[6n" in data:  # the TUI asks for the cursor position at start
                with contextlib.suppress(OSError):
                    os.write(self.fd, b"\x1b[1;1R")

    def type(self, text: str) -> None:
        os.write(self.fd, text.encode())
        time.sleep(0.3)
        os.write(self.fd, b"\r")

    def text(self) -> str:
        """Everything printed so far, without terminal escape sequences, with all whitespace runs collapsed."""
        return re.sub(r"\s+", " ", _ANSI.sub("", self.screen.decode("utf8", "replace")))

    def alive(self) -> bool:
        return not self.exited.is_set()

    def collab_hosts(self) -> list[dict]:
        r = self.scratch.cli("collab", "list", "--json", timeout=30)
        if r.returncode != 0:
            return []
        return [h for h in json.loads(r.stdout).get("hosts") or [] if h.get("pid") == self.pid]

    def session_entries(self) -> list[dict]:
        files = list((self.scratch.home / ".omp" / "agent" / "sessions").rglob("*.jsonl"))
        if len(files) != 1:
            return []
        return [json.loads(line) for line in files[0].read_text().splitlines() if line.strip()]

    def stop(self) -> None:
        if self.alive():
            os.kill(self.pid, signal.SIGTERM)
            if not self.exited.wait(15):
                os.kill(self.pid, signal.SIGKILL)
                self.exited.wait(5)
        with contextlib.suppress(OSError):
            os.close(self.fd)
