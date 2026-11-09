"""The heartbeat: run every health check, then ping a dead-man URL (a healthchecks.io-style check) with the result.

    all checks pass  ->  GET <URL>
    any check fails  ->  GET <URL>/fail

The request has no body; only the URL's own path carries the check id. The URL is a secret: it lives in its own 0600
file, is never logged, never written to the status record, never echoed in an error, and a redirect is never followed
(a 3xx counts as a failed ping). TLS is verified. The checks come from `health.d/*.json` drop-ins (health/1), so every
plugin contributes its own; with no drop-ins the ping simply says "this machine and this service are alive".
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import net
from . import units as units_mod
from .checks import CheckResult, Context, invalid_dropin, run_dropins
from .config import Config, load_config
from .dropins import Dropin, load_dir
from .hostplat import manager_kind, system
from .manager import make_manager
from .paths import Paths
from .runtime import choose_omp, choose_python, load_state
from .units import ServiceSpec, specs_from_dropin
from .util import ServicesError, file_problem, kill_active_groups, one_line, read_text, write_file

STATUS_SCHEMA = 1
MAX_STATUS_AGE_FACTOR = 3  # doctor calls the record stale after this many intervals


@dataclass
class PingResult:
    attempted: bool
    ok: bool
    detail: str  # a status code or a short error code; never the URL


@dataclass
class CycleResult:
    at: float
    results: list[CheckResult]
    ping: PingResult
    notes: list[str] = field(default_factory=list)

    @property
    def failing(self) -> list[CheckResult]:
        return [r for r in self.results if r.failing]

    @property
    def ok(self) -> bool:
        return not self.failing


# ---------------------------------------------------------------------------------------------------------------------
# The ping
# ---------------------------------------------------------------------------------------------------------------------


def read_url(cfg: Config, paths: Paths) -> str | None:
    """The configured ping URL, or None when none is configured. Raises ServicesError (without the URL) when the file
    or the URL is unacceptable."""
    path = cfg.url_file(paths)
    if not path.exists() and not path.is_symlink():
        return None
    problem = file_problem(path)
    if problem:
        raise ServicesError(f"the heartbeat URL file {problem}")
    text = read_text(path, 4096, forbid=0o077).strip()
    if not text:
        return None
    try:
        net.parse_http_url(text, https_only=True)
    except ServicesError as exc:
        raise ServicesError(f"the heartbeat URL is not usable: {exc}") from None
    return text


def ping_target(url: str, failed: bool) -> net.Target:
    target = net.parse_http_url(url, https_only=True)
    if not failed:
        return target
    path, sep, query = target.path.partition("?")
    return net.Target(
        target.scheme, target.host, target.port, path.rstrip("/") + "/fail" + (sep + query if sep else "")
    )


def send_ping(url: str, failed: bool, timeout: float) -> PingResult:
    got = net.http_get(ping_target(url, failed), timeout)
    if got.status is None:
        return PingResult(True, False, got.error or "no-answer")
    if 200 <= got.status < 300:
        return PingResult(True, True, f"status {got.status}")
    if 300 <= got.status < 400:
        return PingResult(True, False, f"status {got.status} (a redirect; redirects are not followed)")
    return PingResult(True, False, f"status {got.status}")


# ---------------------------------------------------------------------------------------------------------------------
# One round
# ---------------------------------------------------------------------------------------------------------------------


def _services_by_plugin(
    cfg: Config, paths: Paths, env: Mapping[str, str]
) -> tuple[dict[str, dict[str, ServiceSpec]], dict[str, str]]:
    """(plugin -> name -> service, plugin -> why its services could not be built). Interpreters are resolved the way
    `init` resolves them: configured, else the ones pinned in the state file, so an omp script is found."""
    try:
        state = load_state(paths)
    except ServicesError:
        state = {}
    try:
        python_path = choose_python(cfg, state)
    except ServicesError:
        python_path = sys.executable
    omp_path = choose_omp(cfg, state, env, paths.home)
    drops, _ = load_dir("services", paths.services_d)
    out: dict[str, dict[str, ServiceSpec]] = {}
    errors: dict[str, str] = {}
    for drop in drops:
        if not drop.ok:
            errors[drop.plugin] = "unusable drop-in"
            continue
        assert drop.data is not None
        try:
            specs = specs_from_dropin(
                drop.data, cfg.label_prefix, paths, python_path, omp_path, units_mod.carried_env(paths, cfg, env)
            )
        except ServicesError as exc:
            errors[drop.plugin] = str(exc)
            continue
        out[drop.plugin] = {s.name: s for s in specs}
    return out, errors


def _installed_protection(paths: Paths) -> dict[str, bool] | None:
    """The labels the installed state remembers as protected; None when the state cannot be read *or is missing*: a
    watchdog that runs has been installed, so a missing state file (or a dangling link in its place) is lost state, not a
    machine with nothing protected, and nothing may be restarted on that reading."""
    if not os.path.lexists(paths.state_file):
        return None
    try:
        state = load_state(paths)
    except ServicesError:
        return None
    return {label: bool(info.get("protected")) for label, info in state.get("units", {}).items()}


def build_context(cfg: Config, paths: Paths, env: Mapping[str, str], *, allow_actions: bool) -> Context:
    plat = system(env)
    services, service_errors = _services_by_plugin(cfg, paths, env)
    return Context(
        cfg=cfg,
        paths=paths,
        plat=plat,
        manager=make_manager(manager_kind(plat), env),
        services=services,
        service_errors=service_errors,
        protection=lambda: _installed_protection(paths),
        allow_actions=allow_actions,
        env=env,
    )


def collect_results(ctx: Context) -> tuple[list[CheckResult], list[str]]:
    """Run every usable drop-in's checks. Unusable drop-ins and directories become failing results."""
    drops, problems = load_dir("health", ctx.paths.health_d)
    results = run_dropins(drops, ctx, float(ctx.cfg.heartbeat["cycleTimeoutSeconds"]))
    for problem in problems:
        drop = Dropin("health", ctx.paths.health_d, "health-d", errors=[problem])
        results.append(invalid_dropin(drop, "dropin-directory-unsafe"))
    return results, problems


def run_cycle(ctx: Context, *, send: bool) -> CycleResult:
    results, _ = collect_results(ctx)
    notes: list[str] = []
    if send and not results and not ctx.cfg.heartbeat["allowNoChecks"]:
        # Nothing to check is not the same as everything fine: a heartbeat that has no checks says so loudly, so that
        # a plugin whose drop-in never arrived, or a typo in a file name, does not turn into a green light.
        results.append(CheckResult("omp-services", "checks", "checks", "failed", "no-health-checks-configured"))
        notes.append(
            "no health checks are configured: sending /fail (set heartbeat.allowNoChecks to true to change this)"
        )
    now = ctx.now()
    failing = [r for r in results if r.failing]
    ping = PingResult(False, True, "not sent")
    if send:
        try:
            url = read_url(ctx.cfg, ctx.paths)
        except ServicesError as exc:
            url = None
            notes.append(str(exc))
            ping = PingResult(False, False, "url-unusable")
        else:
            if url is None:
                notes.append("no heartbeat URL is configured, nothing sent")
                ping = PingResult(False, True, "no-url")
            else:
                ping = send_ping(url, bool(failing), float(ctx.cfg.heartbeat["pingTimeoutSeconds"]))
    cycle = CycleResult(now, results, ping, notes)
    write_status(ctx, cycle)
    return cycle


def write_status(ctx: Context, cycle: CycleResult) -> None:
    """The record `doctor` reads: when it ran, what each check said, whether the ping got through. No URL."""
    record = {
        "schemaVersion": STATUS_SCHEMA,
        "at": cycle.at,
        "intervalSeconds": ctx.cfg.heartbeat["intervalSeconds"],
        "ok": cycle.ok,
        "ping": {"attempted": cycle.ping.attempted, "ok": cycle.ping.ok, "detail": cycle.ping.detail},
        "results": [r.as_dict() for r in cycle.results],
    }
    try:
        write_file(ctx.paths.heartbeat_status, json.dumps(record, indent=1) + "\n")
    except OSError:
        pass


def read_status(paths: Paths) -> dict[str, Any] | None:
    try:
        data = json.loads(read_text(paths.heartbeat_status))
    except (ServicesError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("schemaVersion") == STATUS_SCHEMA else None


def log_line(cycle: CycleResult) -> str:
    stamp = datetime.fromtimestamp(cycle.at).strftime("%Y-%m-%d %H:%M:%S")
    state = (
        "ok" if cycle.ok else "fail: " + ", ".join(f"{r.plugin}/{r.id} {r.status} {r.reason}" for r in cycle.failing)
    )
    if cycle.ping.attempted:
        sent = "ping sent" if cycle.ping.ok else f"ping failed ({cycle.ping.detail})"
        if cycle.notes:
            sent += "; " + "; ".join(cycle.notes)
    else:
        sent = "; ".join(cycle.notes) if cycle.notes else "no ping"
    return one_line(f"{stamp} {state}; {sent}", 600)


# ---------------------------------------------------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------------------------------------------------


def trim_logs(logs_dir: Path, max_bytes: int) -> list[str]:
    """Keep each `*.log` under `max_bytes`: the old content moves to `<name>.1` (one generation, replaced each time)
    and the file is truncated in place, because the service manager keeps it open for appending."""
    trimmed: list[str] = []
    if not logs_dir.is_dir():
        return trimmed
    for log in sorted(logs_dir.glob("*.log")):
        try:
            if log.is_symlink() or not log.is_file() or log.stat().st_size <= max_bytes:
                continue
            _rotate(log, max_bytes)
            trimmed.append(log.name)
        except OSError:
            continue
    return trimmed


def oversized_logs(logs_dir: Path, max_bytes: int) -> list[Path]:
    if not logs_dir.is_dir():
        return []
    out = []
    for log in sorted(logs_dir.glob("*.log")):
        try:
            if not log.is_symlink() and log.is_file() and log.stat().st_size > max_bytes:
                out.append(log)
        except OSError:
            continue
    return out


def _rotate(log: Path, max_bytes: int, chunk: int = 1024 * 1024) -> None:
    """Keep the newest `max_bytes` of `log` in `<name>.1`, copied in chunks (never the whole file in memory), then
    truncate `log` in place. Bytes appended between the copy and the truncate are lost: the price of not renaming a
    file the service manager holds open."""
    old = log.with_name(log.name + ".1")
    tmp = log.with_name(f".{log.name}.1.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    with open(log, "rb") as src:
        size = os.fstat(src.fileno()).st_size
        start = max(0, size - max_bytes)
        src.seek(start)
        budget = size - start  # exactly the range decided now: bytes a writer appends while we copy are not followed
        fd = os.open(tmp, flags, 0o600)
        try:
            with os.fdopen(fd, "wb") as dst:
                while budget > 0 and (data := src.read(min(chunk, budget))):
                    dst.write(data)
                    budget -= len(data)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    os.replace(tmp, old)
    os.truncate(log, 0)


# ---------------------------------------------------------------------------------------------------------------------
# The service loop
# ---------------------------------------------------------------------------------------------------------------------


def run_once(paths: Paths, env: Mapping[str, str], *, send: bool = True, allow_actions: bool = True) -> int:
    """One round, printed as one log line. Exit status: 0 healthy and (pinged or no URL), 1 anything else."""
    cfg = load_config(paths, env)
    if cfg.problems:
        print("configuration problems, nothing checked or sent:", file=sys.stderr)
        for problem in cfg.problems:
            print(f"  {one_line(problem, 300)}", file=sys.stderr)
        return 1
    ctx = build_context(cfg, paths, env, allow_actions=allow_actions)
    cycle = run_cycle(ctx, send=send and bool(cfg.heartbeat["enabled"]))
    print(log_line(cycle), flush=True)
    trimmed = trim_logs(paths.logs_dir, cfg.log_max_bytes)
    if trimmed:
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S} trimmed {', '.join(trimmed)}", flush=True)
    return 0 if cycle.ok and cycle.ping.ok else 1


def run_loop(paths: Paths, env: Mapping[str, str]) -> int:
    """The long-running service: a round, then wait `intervalSeconds`, until SIGTERM or SIGINT. A round that raises is
    logged by exception class and does not stop the loop (the service manager would only restart it)."""
    stop = threading.Event()

    def on_signal(signum: int, frame: object) -> None:
        stop.set()
        kill_active_groups()  # a check command still running goes down with the loop

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} heartbeat started", flush=True)
    while not stop.is_set():
        started = time.monotonic()
        interval = 300.0
        try:
            cfg = load_config(paths, env)
            interval = float(cfg.heartbeat["intervalSeconds"])
            if cfg.problems:
                print(
                    f"{datetime.now():%Y-%m-%d %H:%M:%S} configuration problems, nothing sent: {one_line(cfg.problems[0], 300)}",
                    flush=True,
                )
            else:
                run_once(paths, env, send=True, allow_actions=True)
        except Exception as exc:
            print(f"{datetime.now():%Y-%m-%d %H:%M:%S} round failed: {type(exc).__name__}", flush=True)
        stop.wait(max(1.0, interval - (time.monotonic() - started)))
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} heartbeat stopped", flush=True)
    return 0
