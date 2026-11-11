"""Finding the programs omp-memory runs (Bun, omp) and running the bundled importer and scripts."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import sink
from .config import Config
from .core import VENDOR
from .util import EXIT_USAGE, CliError, clean_env, parse_semver, tilde, version_text

MIN_BUN = (1, 4, 4)
IMPORTER = VENDOR / "memory_import" / "cli.ts"
# Bun reads bunfig.toml and .env from the directory it starts in: it always starts in the plugin's own, never the caller's.
IMPORTER_DIR = IMPORTER.parent
OPS = VENDOR / "memory_ops"


@dataclass(frozen=True)
class Program:
    path: Path
    version: tuple[int, int, int] | None
    source: str


def probe(path: str | Path, args: Sequence[str] = ("--version",)) -> tuple[int, int, int] | None:
    """The x.y.z a program prints for --version, or None when it does not run or prints none."""
    try:
        ran = subprocess.run(
            [str(path), *args], capture_output=True, text=True, timeout=20, env=clean_env(), check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_semver(ran.stdout) if ran.returncode == 0 else None


def bun_candidates(config: Config) -> Iterator[tuple[Path, str]]:
    configured = config.get("bun")
    if configured.value is not None:
        yield configured.value, f"{configured.source} {configured.env or 'bun'}"
        return
    on_path = shutil.which("bun")
    if on_path:
        yield Path(on_path), "PATH"
    home = Path.home()
    yield home / ".bun" / "bin" / "bun", "~/.bun"
    cache = home / ".cache" / "omp-relay"
    if cache.is_dir():
        for entry in sorted(cache.glob("bun-*/bun"), reverse=True):
            yield entry, "~/.cache/omp-relay"
    yield Path("/opt/homebrew/bin/bun"), "/opt/homebrew"
    yield Path("/usr/local/bin/bun"), "/usr/local"


def find_bun(config: Config) -> Program:
    """The first Bun that is 1.4.4 or newer. A configured Bun is used or refused, never replaced by another."""
    seen: list[str] = []
    for path, source in bun_candidates(config):
        if not (path.is_file() and os.access(path, os.X_OK)):
            continue
        version = probe(path)
        if version is not None and version >= MIN_BUN:
            return Program(path, version, source)
        seen.append(f"{tilde(path)} ({version_text(version) or 'no version'})")
    found = "; found: " + ", ".join(seen) if seen else ""
    raise CliError(
        f"no Bun {version_text(MIN_BUN)} or newer{found}. Install one (https://bun.sh: `curl -fsSL https://bun.sh/install | bash -s bun-v1.4.4`, "
        "or brew install oven-sh/bun/bun) or point to it with OMP_MEMORY_BUN / `omp-memory config set bun /abs/path/bun`.",
        EXIT_USAGE,
    )


def find_omp(config: Config) -> Program | None:
    configured = config.get("ompBin")
    candidate: Path | None = configured.value
    source = f"{configured.source} {configured.env or 'ompBin'}"
    if candidate is None:
        found = shutil.which("omp")
        candidate = Path(found) if found else Path.home() / ".local" / "bin" / "omp"
        source = "PATH" if found else "~/.local/bin"
    if not (candidate.is_file() and os.access(candidate, os.X_OK)):
        return None
    return Program(candidate, probe(candidate), source)


def importer_env() -> dict[str, str]:
    """The environment of the importer: clean, plus the command it tells the user to run when the library is missing."""
    return {**clean_env(), "OMP_MEMORY_SETUP_COMMAND": "omp-memory setup"}


def library_status(config: Config, omp: Program | None) -> dict | None:
    """What the importer's `library` command reports: which Bun and omp, and where the Mnemopi library was found or why
    each place was refused. None when it cannot be asked (no Bun, no omp, or no JSON came back). Read-only, no network."""
    if omp is None:
        return None
    try:
        bun = find_bun(config)
    except CliError:
        return None
    argv = [str(bun.path), str(IMPORTER), "library", "--omp-bin", str(omp.path)]
    lib_dir = config.path("libDir")
    if lib_dir is not None:
        argv += ["--lib-dir", str(lib_dir)]
    try:
        ran = subprocess.run(
            argv, capture_output=True, text=True, timeout=60, env=importer_env(), cwd=IMPORTER_DIR, check=False
        )
        status = json.loads(ran.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return status if isinstance(status, dict) else None


@contextlib.contextmanager
def python_on_path(env: dict[str, str]) -> Iterator[dict[str, str]]:
    """Put this interpreter first on PATH as `python3`, so scripts that call `python3` get a Python 3.10 or newer."""
    with tempfile.TemporaryDirectory(prefix="omp-memory-bin-") as tmp:
        os.symlink(sys.executable, os.path.join(tmp, "python3"))
        yield {**env, "PATH": tmp + os.pathsep + env.get("PATH", "")}


TERM_GRACE_S = 15


def stop_child(child: subprocess.Popen) -> None:
    """Ask a child to stop with a signal it can handle (it may have a private copy or a lock to remove), wait for it, and
    only then, when it does not stop, kill it."""
    if child.poll() is None:
        with contextlib.suppress(OSError):
            child.send_signal(signal.SIGTERM)
        try:
            child.wait(timeout=TERM_GRACE_S)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def _forward(stream, write: Callable[[str], None], seen: list[str] | None, show: Callable[[str], str] | None) -> None:
    """Pass a child's output on line by line through the sink, remembering the raw lines when asked. A reader that cannot write
    any more (the terminal closed) keeps draining, so the child never blocks on a full pipe."""
    broken = False
    for line in stream:
        if seen is not None:
            seen.append(line)
        if not broken:
            try:
                write(show(line) if show else line)
            except (BrokenPipeError, ValueError):
                broken = True


def _forward_stdout(stream, seen: list[str], show: Callable[[str], str] | None) -> None:
    """Like `_forward` for stdout, except that a JSON document in it (the importer's report) is held back until it is complete,
    then written as the same document with only the strings that carry a secret replaced: line by line it would not stay valid
    JSON. Text before and after the document is still passed on line by line, as it arrives."""
    broken = False
    document: list[str] = []

    def write(text: str) -> None:
        nonlocal broken
        if not broken:
            try:
                sink.write_out(text)
            except (BrokenPipeError, ValueError):
                broken = True

    def flush_document() -> None:
        nonlocal broken
        if document and not broken:
            try:
                sink.write_document("".join(document), show)
            except (BrokenPipeError, ValueError):
                broken = True
        document.clear()

    for line in stream:
        seen.append(line)
        if document or sink.starts_document(line):
            document.append(line)
        else:
            write(show(line) if show else line)
    flush_document()


def _start(argv: Sequence[str | Path], env: dict[str, str] | None, cwd: Path | None) -> subprocess.Popen:
    try:
        return subprocess.Popen(
            [str(a) for a in argv],
            env=env or clean_env(),
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as err:
        raise CliError(f"cannot run {argv[0]}: {err.strerror}", EXIT_USAGE) from None


def run_tee(
    argv: Sequence[str | Path],
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    show: Callable[[str], str] | None = None,
) -> tuple[int, str]:
    """Run a child with this process's stdin. Its stdout and stderr are passed on line by line through the sink (which
    withholds what carries a secret); `show` may rewrite a stdout line first. Returns (exit status, the raw stdout)."""
    child = _start(argv, env, cwd)
    assert child.stdout is not None and child.stderr is not None
    seen: list[str] = []
    errors = threading.Thread(target=_forward, args=(child.stderr, sink.write_err, None, None), daemon=True)
    errors.start()
    try:
        _forward_stdout(child.stdout, seen, show)
        code = child.wait()
    except KeyboardInterrupt:
        stop_child(child)
        code = 130
    errors.join(timeout=TERM_GRACE_S)
    return code, "".join(seen)


def run(argv: Sequence[str | Path], env: dict[str, str] | None = None, cwd: Path | None = None) -> int:
    """Run a child with this process's stdin; its stdout and stderr go through the sink. Its exit status (or 2 when it cannot start)."""
    return run_tee(argv, env=env, cwd=cwd)[0]


def _checked_script(script: Path) -> Path:
    if not script.is_file():
        raise CliError(f"bundled script missing: {tilde(script)}; run scripts/sync-vendor.sh", EXIT_USAGE)
    return script


def run_script(script: Path, args: Sequence[str]) -> int:
    """Run one of the bundled memory_ops scripts from the vendor directory, with a modern python3 first on PATH."""
    with python_on_path(clean_env()) as env:
        return run([_checked_script(script), *args], env=env, cwd=VENDOR)


def run_script_tee(script: Path, args: Sequence[str], show: Callable[[str], str] | None = None) -> tuple[int, str]:
    """`run_script`, returning what the script printed to stdout as well as showing it."""
    with python_on_path(clean_env()) as env:
        return run_tee([_checked_script(script), *args], env=env, cwd=VENDOR, show=show)
