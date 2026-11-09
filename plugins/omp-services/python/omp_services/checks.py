"""The health/1 checks (docs/protocol/health-1.md): what each type does and how a failure is classified.

Result statuses:
  ok            the check passed
  unreachable   nothing answered: connection refused, timeout, nothing listening, the relay tool not answering
  stale         it answered, but the data or file is older than allowed
  not-on-relay  the service is up locally but the route that forwards it (a Tailscale Serve route) is missing
  failed        anything else: wrong status, wrong owner, non-zero exit, unreadable answer, a limit reached
  invalid       the check itself cannot run: a bad definition or a policy refusal

Every check has a hard deadline. No check output, URL, path or command line is ever put into a result: only a short
reason code made of words and numbers.
"""

from __future__ import annotations

import glob
import json
import math
import os
import shutil
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import hostplat, net
from .config import Config
from .dropins import Dropin
from .manager import ServiceManager
from .paths import Paths
from .units import ServiceSpec, is_managed_text
from .util import FileLock, ServicesError, call_with_deadline, read_text, run_bounded, safe_env, write_file

OK, UNREACHABLE, STALE, NOT_ON_RELAY, FAILED, INVALID = (
    "ok",
    "unreachable",
    "stale",
    "not-on-relay",
    "failed",
    "invalid",
)
STATUSES = (OK, UNREACHABLE, STALE, NOT_ON_RELAY, FAILED, INVALID)
MAX_BODY = 64 * 1024
MAX_GLOB_MATCHES = 10000
TAILSCALE_CANDIDATES = (
    "/usr/local/bin/tailscale",
    "/opt/homebrew/bin/tailscale",
    "/usr/bin/tailscale",
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
)


@dataclass
class CheckResult:
    plugin: str
    id: str
    type: str
    status: str
    reason: str
    severity: str = "fail"
    ms: int = 0

    @property
    def failing(self) -> bool:
        """Counts toward the heartbeat's /fail: not ok, and not a note-only check."""
        return self.status != OK and self.severity == "fail"

    def as_dict(self) -> dict[str, Any]:
        return {
            "plugin": self.plugin,
            "id": self.id,
            "type": self.type,
            "status": self.status,
            "reason": self.reason,
            "severity": self.severity,
            "ms": self.ms,
        }


@dataclass
class Context:
    cfg: Config
    paths: Paths
    plat: str
    manager: ServiceManager | None = None
    services: Mapping[str, Mapping[str, ServiceSpec]] = field(default_factory=dict)  # plugin -> name -> spec
    # The labels the installed state remembers as protected, or None when that state cannot be read (then nothing is
    # restarted). Asked again immediately before a restart, so a change since the round started is seen.
    protection: Callable[[], dict[str, bool] | None] | None = None
    service_errors: Mapping[str, str] = field(default_factory=dict)  # plugin -> why its services could not be built
    allow_actions: bool = False  # only the heartbeat lets a watchdog check restart a service
    now: Callable[[], float] = time.time
    env: Mapping[str, str] = field(default_factory=lambda: os.environ)


def _result(plugin: str, check: Mapping[str, Any], status: str, reason: str) -> CheckResult:
    return CheckResult(plugin, check["id"], check["type"], status, reason, check.get("severity", "fail"))


# ---------------------------------------------------------------------------------------------------------------------
# url
# ---------------------------------------------------------------------------------------------------------------------


def _pointer(document: Any, pointer: str) -> Any:
    node = document
    for raw in pointer.split("/")[1:]:
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and key in node:
            node = node[key]
        elif isinstance(node, list) and key.isdigit() and int(key) < len(node):
            node = node[int(key)]
        else:
            raise KeyError(pointer)
    return node


def _no_constants(name: str) -> Any:
    raise ValueError(f"{name} is not allowed")


def _timestamp(value: Any, fmt: str) -> float:
    if fmt == "epoch-seconds":
        if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
            raise ValueError("not a finite number")
        return float(value)
    if not isinstance(value, str):
        raise ValueError("not a string")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def check_url(plugin: str, check: Mapping[str, Any], ctx: Context, timeout: float) -> CheckResult:
    try:
        target = net.parse_http_url(check["url"])
    except ServicesError:
        return _result(plugin, check, INVALID, "bad-url")
    if not ctx.cfg.allow_remote_urls and not net.is_loopback_host(target.host):
        return _result(plugin, check, INVALID, "remote-host-not-allowed")
    freshness = check.get("freshness")
    got = net.http_get(target, timeout, max_body=MAX_BODY if freshness else 0)
    if got.status is None:
        return _result(plugin, check, UNREACHABLE, got.error or "no-answer")
    accepted = check.get("expectStatus")
    status_ok = got.status in accepted if accepted else 200 <= got.status < 300
    if not status_ok:
        return _result(plugin, check, FAILED, f"status-{got.status}")
    if freshness:
        try:
            moment = _timestamp(
                _pointer(json.loads(got.body, parse_constant=_no_constants), freshness["jsonPointer"]),
                freshness.get("format", "epoch-seconds"),
            )
        except (ValueError, KeyError, TypeError):
            return _result(plugin, check, FAILED, "freshness-unreadable")
        age = ctx.now() - moment
        if age < -60:
            return _result(plugin, check, FAILED, "timestamp-in-future")
        if age > freshness["maxAgeSeconds"]:
            return _result(plugin, check, STALE, f"older-than-{freshness['maxAgeSeconds']}s")
    return _result(plugin, check, OK, f"status-{got.status}")


# ---------------------------------------------------------------------------------------------------------------------
# port, uid-owner
# ---------------------------------------------------------------------------------------------------------------------


def check_port(plugin: str, check: Mapping[str, Any], ctx: Context, timeout: float) -> CheckResult:
    host = check.get("host", "127.0.0.1")
    if not ctx.cfg.allow_remote_urls and not net.is_loopback_host(host):
        return _result(plugin, check, INVALID, "remote-host-not-allowed")
    error = net.tcp_connect(host, check["port"], timeout)
    if error:
        return _result(plugin, check, UNREACHABLE, error)
    return _result(plugin, check, OK, "listening")


def check_uid_owner(plugin: str, check: Mapping[str, Any], ctx: Context, timeout: float) -> CheckResult:
    started = time.monotonic()
    want = os.geteuid() if check.get("uid", "self") == "self" else check["uid"]
    try:
        owners = hostplat.listener_uids(check["port"], ctx.plat, timeout)
    except ServicesError:
        return _result(plugin, check, FAILED, "cannot-determine-owner")
    if not owners:
        # Nothing listed: either nothing listens, or the tool may not see another user's socket.
        # the fallback connection spends what is left of this check's budget (at most 2 s), not a fresh allowance
        left = started + timeout
        if net.tcp_connect("127.0.0.1", check["port"], 0, deadline=min(left, time.monotonic() + 2.0)) is None:
            return _result(plugin, check, FAILED, "owner-unknown")
        return _result(plugin, check, UNREACHABLE, "no-listener")
    if owners != {want}:
        return _result(plugin, check, FAILED, "owner-mismatch")
    return _result(plugin, check, OK, "owner-matches")


# ---------------------------------------------------------------------------------------------------------------------
# serve-route
# ---------------------------------------------------------------------------------------------------------------------


def find_tailscale(cfg: Config) -> str | None:
    if cfg.tailscale:
        return cfg.tailscale if os.access(cfg.tailscale, os.X_OK) else None
    found = shutil.which("tailscale")  # the service's own PATH first (init gives units a sensible one)
    if found:
        return found
    return next((c for c in TAILSCALE_CANDIDATES if os.access(c, os.X_OK)), None)


def routes_in(status: Any) -> dict[tuple[int, str], str]:
    """{(port, path): proxy target} from the JSON of `tailscale serve status --json`. Any host name counts."""
    found: dict[tuple[int, str], str] = {}
    web = status.get("Web") if isinstance(status, dict) else None
    if not isinstance(web, dict):
        return found
    for hostport, config in web.items():
        port = str(hostport).rpartition(":")[2]
        handlers = config.get("Handlers") if isinstance(config, dict) else None
        if not port.isdigit() or not isinstance(handlers, dict):
            continue
        for path, handler in handlers.items():
            proxy = handler.get("Proxy") if isinstance(handler, dict) else None
            if isinstance(proxy, str):
                found[(int(port), path)] = proxy
    return found


def check_serve_route(plugin: str, check: Mapping[str, Any], ctx: Context, timeout: float) -> CheckResult:
    tool = find_tailscale(ctx.cfg)
    if not tool:
        return _result(plugin, check, FAILED, "tailscale-not-found")
    ran = run_bounded([tool, "serve", "status", "--json"], timeout, env=safe_env())
    if ran.error:
        return _result(plugin, check, FAILED, "tailscale-cannot-start")
    if ran.timed_out or ran.returncode != 0:
        return _result(plugin, check, UNREACHABLE, "tailscale-not-answering")
    try:
        status = json.loads(ran.stdout or "{}")
    except ValueError:
        return _result(plugin, check, FAILED, "unreadable-output")
    have = routes_in(status).get((check.get("port", 443), check.get("path", "/")))
    if have is None:
        return _result(plugin, check, NOT_ON_RELAY, "no-route")
    if have.rstrip("/") != check["target"].rstrip("/"):
        return _result(plugin, check, NOT_ON_RELAY, "route-points-elsewhere")
    return _result(plugin, check, OK, "route-present")


# ---------------------------------------------------------------------------------------------------------------------
# command, file-age
# ---------------------------------------------------------------------------------------------------------------------


def check_command(plugin: str, check: Mapping[str, Any], ctx: Context, timeout: float) -> CheckResult:
    argv = check["argv"]
    if not argv[0].startswith("/"):
        return _result(plugin, check, INVALID, "argv0-not-absolute")
    if not os.access(argv[0], os.X_OK) or os.path.isdir(argv[0]):
        return _result(plugin, check, FAILED, "not-executable")
    ran = run_bounded(argv, timeout, env=safe_env())
    if ran.error:
        return _result(plugin, check, FAILED, "cannot-start")
    if ran.timed_out:
        return _result(plugin, check, FAILED, "deadline")
    want = check.get("expectExit", 0)
    if ran.returncode != want:
        return _result(plugin, check, FAILED, f"exit-{ran.returncode}")
    needle = check.get("expectOutputContains")
    if needle is not None and needle not in ran.stdout:
        return _result(plugin, check, FAILED, "output-mismatch")
    return _result(plugin, check, OK, f"exit-{ran.returncode}")


def check_file_age(plugin: str, check: Mapping[str, Any], ctx: Context, timeout: float) -> CheckResult:
    deadline = time.monotonic() + timeout

    def newest_match() -> float | None:
        newest: float | None = None
        for count, match in enumerate(glob.iglob(check["path"])):
            if count >= MAX_GLOB_MATCHES or time.monotonic() > deadline:
                break
            try:
                if os.path.isfile(match):
                    mtime = os.stat(match).st_mtime
                    newest = mtime if newest is None else max(newest, mtime)
            except OSError:
                continue
        return newest

    # A pattern that walks a huge tree can sit inside the glob call: the walk runs in a thread that is abandoned at the deadline.
    finished, newest = call_with_deadline(newest_match, timeout)
    if not finished or isinstance(newest, BaseException):
        return _result(plugin, check, FAILED, "deadline")
    if newest is None:
        return _result(plugin, check, FAILED, "no-match")
    if ctx.now() - newest > check["maxAgeSeconds"]:
        return _result(plugin, check, STALE, f"older-than-{check['maxAgeSeconds']}s")
    return _result(plugin, check, OK, "fresh")


# ---------------------------------------------------------------------------------------------------------------------
# watchdog
# ---------------------------------------------------------------------------------------------------------------------


def _load_watchdog_state(ctx: Context) -> dict[str, Any] | None:
    """The restart history, {} when there is none yet, None when the file exists but cannot be trusted (an unreadable
    history must not reset the budget)."""
    if not ctx.paths.watchdog_state.exists():
        return {}
    try:
        data = json.loads(read_text(ctx.paths.watchdog_state, forbid=0o022))
    except (ServicesError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _is_ours(ctx: Context, label: str) -> bool:
    """The unit file of `label` exists and carries the marker this tool writes: the watchdog restarts only what omp-services
    installed, not whatever happens to share a label."""
    assert ctx.manager is not None
    try:
        text = read_text(ctx.manager.unit_file(ctx.paths.unit_dir, label))
    except ServicesError:
        return False
    return is_managed_text(text)


def check_watchdog(plugin: str, check: Mapping[str, Any], ctx: Context, timeout: float) -> CheckResult:
    spec = (ctx.services.get(plugin) or {}).get(check["service"])
    if spec is None:
        reason = "service-unavailable" if plugin in ctx.service_errors else "unknown-service"
        return _result(plugin, check, INVALID, reason)
    manager = ctx.manager
    if manager is None:
        return _result(plugin, check, FAILED, "no-service-manager")
    started = time.monotonic()
    manager.deadline = started + timeout  # every call to the service manager shares this check's budget
    try:
        return _watchdog(plugin, check, ctx, timeout, spec, manager, started)
    except ServicesError:  # could not ask the service manager: say so, do not guess
        return _result(plugin, check, FAILED, "manager-query-failed")
    finally:
        manager.deadline = None


def _protected(ctx: Context, spec: ServiceSpec) -> bool | None:
    """Whether `spec` is protected, by its declaration or by what was installed; None when that cannot be told."""
    if spec.protected:
        return True
    if ctx.protection is None:
        return False
    records = ctx.protection()
    # No record of this service is as unknown as no state file: nothing says it is safe to restart.
    return None if records is None or spec.label not in records else records[spec.label]


def _watchdog(plugin, check, ctx, timeout, spec, manager, started) -> CheckResult:
    if manager.transitional(spec.label):  # neither up nor down: nothing is restarted while it is changing state
        return _result(plugin, check, FAILED, "transitional-state")
    if manager.is_running(spec.label):
        return _result(plugin, check, OK, "running")
    policy = check.get("restart")
    if policy is None:
        return _result(plugin, check, FAILED, "not-running")
    if not ctx.allow_actions:
        return _result(plugin, check, FAILED, "not-running-report-only")
    protected = _protected(ctx, spec)  # a protected service is restarted by a person, never by a drop-in
    if protected is None:
        return _result(plugin, check, FAILED, "protection-state-unreadable")
    if protected:
        return _result(plugin, check, FAILED, "protected-not-restarted")
    if not _is_ours(ctx, spec.label):
        return _result(plugin, check, FAILED, "not-managed")
    if not manager.is_loaded(spec.label):
        return _result(plugin, check, FAILED, "not-loaded")
    if time.monotonic() - started >= timeout:
        return _result(plugin, check, FAILED, "deadline")
    ctx.paths.state_dir.mkdir(parents=True, exist_ok=True)
    try:
        # One reservation at a time: a heartbeat round and a manual run cannot both spend the same allowance.
        with FileLock(
            ctx.paths.state_dir / "watchdog.lock", wait=max(0.0, min(5.0, timeout - (time.monotonic() - started)))
        ):
            now = ctx.now()
            state = _load_watchdog_state(ctx)
            if state is None:
                return _result(plugin, check, FAILED, "restart-state-unreadable")
            window = policy.get("windowSeconds", 3600)
            history = [t for t in state.get(spec.label, []) if isinstance(t, int | float) and now - t < window]
            if len(history) >= policy.get("maxRestarts", 3):
                return _result(plugin, check, FAILED, "restart-limit")
            if history and now - max(history) < policy.get("cooldownSeconds", 60):
                return _result(plugin, check, FAILED, "restart-cooldown")
            history.append(now)  # counted before it is tried, so a restart that fails still uses up the budget
            state[spec.label] = history
            try:
                write_file(ctx.paths.watchdog_state, json.dumps(state))
            except OSError:
                return _result(plugin, check, FAILED, "cannot-record-restart")
    except ServicesError:
        return _result(plugin, check, FAILED, "restart-busy")
    # Ownership and protection are asked once more at the moment of the restart, not only when the round began.
    if not _is_ours(ctx, spec.label):
        return _result(plugin, check, FAILED, "not-managed")
    if _protected(ctx, spec) is not False:
        return _result(plugin, check, FAILED, "protected-not-restarted")
    try:
        manager.restart(spec.label)
    except ServicesError:
        return _result(plugin, check, FAILED, "restart-failed")
    grace = min(policy.get("graceSeconds", 10), max(1.0, timeout))
    waited = 0.0
    while waited < grace and time.monotonic() - started < timeout:
        if manager.is_running(spec.label):
            return _result(plugin, check, OK, "restarted")
        step = min(0.5, timeout - (time.monotonic() - started))  # never sleep past this check's budget
        if step <= 0:
            break
        manager.sleep(step)
        waited += step
    return _result(plugin, check, FAILED, "restart-failed")


# ---------------------------------------------------------------------------------------------------------------------
# running a drop-in
# ---------------------------------------------------------------------------------------------------------------------

RUNNERS: dict[str, Callable[[str, Mapping[str, Any], Context, float], CheckResult]] = {
    "url": check_url,
    "port": check_port,
    "uid-owner": check_uid_owner,
    "serve-route": check_serve_route,
    "command": check_command,
    "file-age": check_file_age,
    "watchdog": check_watchdog,
}


def run_check(plugin: str, check: Mapping[str, Any], ctx: Context, timeout: float) -> CheckResult:
    started = time.monotonic()
    try:
        result = RUNNERS[check["type"]](plugin, check, ctx, timeout)
    except Exception as exc:  # a bug in one check must not stop the others or the heartbeat
        result = _result(plugin, check, FAILED, f"internal-{type(exc).__name__}")
    result.ms = int((time.monotonic() - started) * 1000)
    return result


def invalid_dropin(drop: Dropin, code: str = "dropin-invalid") -> CheckResult:
    """A drop-in that could not be used counts as a failing check. The result carries a fixed code only: the reasons
    (which can quote paths or field names from the file) stay with the drop-in for `doctor` to show a person."""
    return CheckResult(drop.plugin, "dropin", "dropin", INVALID, code, "fail")


def run_dropins(drops: list[Dropin], ctx: Context, cycle_seconds: float) -> list[CheckResult]:
    """Every check of every usable drop-in, one after the other, all within `cycle_seconds`."""
    results: list[CheckResult] = []
    deadline = time.monotonic() + cycle_seconds
    for drop in drops:
        if not drop.ok:
            results.append(invalid_dropin(drop))
            continue
        assert drop.data is not None
        for check in drop.data["checks"]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                results.append(_result(drop.plugin, check, FAILED, "cycle-deadline"))
                continue
            timeout = min(float(check.get("timeoutSeconds", ctx.cfg.heartbeat["checkTimeoutSeconds"])), remaining)
            results.append(run_check(drop.plugin, check, ctx, timeout))
    return results
