"""`omp-services doctor`: look at everything and say what is wrong, in words, without changing anything.

Every finding has a stable id, a level (ok, info, warn, fail) and, when there is something to do, a hint. A check that
cannot run reports why instead of stopping the others. Health checks from the drop-ins are run live with their
restart actions switched off, and each failure is named by what kind it is:

  unreachable   nothing answered (the service is down, or the port is wrong)
  stale         it answered but its data or its file is too old (it is up but not doing its job)
  not-on-relay  it works locally but the Tailscale Serve route that forwards it is missing
  failed        anything else (wrong status, wrong owner, non-zero exit)

The exit status is 1 when any finding is `fail`, so a script can use doctor as a gate.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import TESTED_OMP, __version__, catalog, dropins, heartbeat, runtime
from .checks import CheckResult
from .plan import Setup, desired_units, owned_labels
from .util import ServicesError, dir_problem, file_problem, read_text

LEVELS = ("ok", "info", "warn", "fail")


@dataclass
class Finding:
    id: str
    level: str
    message: str
    hint: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "level": self.level, "message": self.message}
        if self.hint:
            out["hint"] = self.hint
        return out


@dataclass
class Report:
    version: str
    platform: str
    manager: str
    findings: list[Finding] = field(default_factory=list)
    health: list[CheckResult] = field(default_factory=list)

    def add(self, id: str, level: str, message: str, hint: str | None = None) -> None:
        self.findings.append(Finding(id, level, message, hint))

    @property
    def counts(self) -> dict[str, int]:
        return {level: sum(1 for f in self.findings if f.level == level) for level in LEVELS}

    @property
    def failed(self) -> bool:
        return self.counts["fail"] > 0

    def fail_ids(self) -> set[str]:
        return {f.id for f in self.findings if f.level == "fail"}

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "platform": self.platform,
            "manager": self.manager,
            "summary": self.counts,
            "findings": [f.as_dict() for f in self.findings],
            "health": [r.as_dict() for r in self.health],
        }

    def render(self) -> str:
        lines = [f"omp-services doctor {self.version} ({self.platform}, {self.manager})", ""]
        width = max((len(f.id) for f in self.findings), default=10)
        for f in self.findings:
            lines.append(f"  {f.level:<5} {f.id:<{width}}  {f.message}")
            if f.hint:
                lines.append(f"        {'':<{width}}  -> {f.hint}")
        c = self.counts
        lines += ["", f"{c['ok']} ok, {c['info']} info, {c['warn']} warning(s), {c['fail']} failure(s)"]
        return "\n".join(lines) + "\n"


def _guard(report: Report, id: str, fn: Callable[[], None]) -> None:
    try:
        fn()
    except Exception as exc:  # doctor never stops half way
        report.add(id, "fail", f"this check crashed ({type(exc).__name__}); that is a bug in omp-services")


def run_doctor(
    setup: Setup, *, health: bool = True, refresh_catalog: bool = False, offline_catalog: bool = True
) -> Report:
    report = Report(__version__, setup.plat, setup.manager.kind)
    try:
        state = runtime.load_state(setup.paths)
        state_problem = None
    except ServicesError as exc:
        state, state_problem = runtime.empty_state(), str(exc)
    initialised = bool(state.get("release")) or setup.paths.config_dir.exists()
    ctx: dict[str, Any] = {"state": state, "omp": None}

    _guard(report, "python", lambda: _python(report, setup, state))
    if state_problem:
        report.add("state", "fail", state_problem)
    if not initialised:
        report.add("setup", "warn", "omp-services has not been set up on this machine", "run `omp-services init`")
    _guard(report, "config", lambda: _config(report, setup))
    _guard(report, "directories", lambda: _directories(report, setup, initialised))
    _guard(report, "omp", lambda: _omp(report, setup, state, ctx))
    if initialised:
        _guard(report, "runtime", lambda: _runtime(report, setup, state, ctx))
        _guard(report, "manager", lambda: _manager(report, setup, state))
        _guard(report, "units", lambda: _units(report, setup, state, ctx))
    _guard(report, "logs", lambda: _logs(report, setup))
    _guard(report, "dropins", lambda: _dropins(report, setup))
    _guard(report, "heartbeat", lambda: _heartbeat(report, setup))
    if health:
        _guard(report, "health", lambda: _health(report, setup))
    if ctx["omp"] and (refresh_catalog or offline_catalog):
        _guard(report, "catalog", lambda: _catalog(report, setup, ctx["omp"], refresh_catalog))
    return report


# ---------------------------------------------------------------------------------------------------------------------


def _python(report: Report, setup: Setup, state: dict[str, Any]) -> None:
    here = ".".join(map(str, sys.version_info[:3]))
    report.add("python.version", "ok", f"Python {here} at {setup.show(sys.executable)}")
    pinned = state.get("python")
    if pinned and not os.access(pinned, os.X_OK):
        report.add(
            "python.pinned",
            "fail",
            f"the pinned Python {setup.show(pinned)} is gone",
            "run `omp-services init` to pin a new one",
        )
    elif pinned:
        version = runtime.python_version(pinned)
        if version is None or version[:2] < (3, 10):
            report.add(
                "python.pinned",
                "fail",
                f"the pinned Python {setup.show(pinned)} cannot run or is older than 3.10",
                "run `omp-services init --repin`",
            )


def _config(report: Report, setup: Setup) -> None:
    cfg = setup.cfg
    if cfg.problems:
        for problem in cfg.problems:
            report.add(
                "config.valid",
                "fail",
                problem,
                "fix the file or unset the variable; `omp-services config validate` repeats this check",
            )
    elif cfg.file_present:
        report.add("config.valid", "ok", f"{setup.show(setup.paths.config_file)} is valid (schemaVersion 1)")
    else:
        report.add("config.valid", "info", "no config file: the defaults apply")
    if cfg.overrides:
        report.add("config.env", "info", "environment overrides in effect: " + ", ".join(cfg.overrides))
    if cfg.file_present:
        problem = file_problem(setup.paths.config_file, forbid=0o022)
        if problem:
            report.add("config.mode", "fail", f"config.json {problem}", "chmod 600 it")


def _directories(report: Report, setup: Setup, initialised: bool) -> None:
    paths = setup.paths
    bad = 0
    for directory in paths.private_dirs():
        problem = dir_problem(directory)
        if problem == "does not exist":
            if initialised and directory in (paths.config_dir, paths.data_dir, paths.state_dir):
                report.add("directories", "warn", f"{setup.show(directory)} does not exist", "run `omp-services init`")
                bad += 1
            continue
        if problem:
            level = "fail" if directory == paths.secrets_dir else "warn"
            report.add("directories", level, f"{setup.show(directory)} {problem}", f"chmod 700 {directory}")
            bad += 1
    if paths.secrets_dir.is_dir():
        for entry in sorted(paths.secrets_dir.iterdir()):
            problem = file_problem(entry)
            if problem:
                report.add("secrets.mode", "fail", f"secrets/{entry.name} {problem}", f"chmod 600 {entry}")
                bad += 1
    if bad == 0:
        report.add("directories", "ok", "config, data and state directories are private (0700) and secrets are 0600")


def _omp(report: Report, setup: Setup, state: dict[str, Any], ctx: dict[str, Any]) -> None:
    if setup.cfg.problems:
        # The configuration names the program to run; with a configuration that cannot be trusted nothing is executed.
        report.add("omp.path", "info", "not checked: the configuration is not trusted, so no program from it is run")
        return
    path = runtime.choose_omp(setup.cfg, state, setup.env, setup.paths.home)
    if not path:
        report.add(
            "omp.path",
            "fail",
            "no omp binary found (OMP_BIN, PATH, ~/.local/bin/omp)",
            "install omp, then run `omp-services init`",
        )
        return
    if not os.access(path, os.X_OK):
        report.add(
            "omp.path",
            "fail",
            f"the pinned omp {setup.show(path)} is gone or not executable",
            "run `omp-services init --repin`",
        )
        return
    ctx["omp"] = path
    version = runtime.omp_version(path)
    if version is None:
        report.add("omp.version", "fail", f"{setup.show(path)} did not report a version")
        return
    text = ".".join(map(str, version))
    if not runtime.at_least(version):
        report.add(
            "omp.version",
            "fail",
            f"omp {text} at {setup.show(path)} is older than the supported minimum {'.'.join(map(str, runtime.MIN_OMP))}",
            "upgrade omp",
        )
    elif text not in TESTED_OMP:
        report.add(
            "omp.version",
            "info",
            f"omp {text} at {setup.show(path)} (supported, but not one of the tested versions: {', '.join(TESTED_OMP)})",
        )
    else:
        report.add("omp.version", "ok", f"omp {text} at {setup.show(path)}")
    pinned = (state.get("omp") or {}).get("path")
    if pinned and pinned != path:
        report.add(
            "omp.pinned",
            "warn",
            f"the pinned omp is {setup.show(pinned)} but {setup.show(path)} is what the configuration selects",
        )


def _runtime(report: Report, setup: Setup, state: dict[str, Any], ctx: dict[str, Any]) -> None:
    paths = setup.paths
    current = runtime.current_release(paths)
    link_problem = runtime.current_link_problem(paths)
    if link_problem:
        report.add(
            "runtime.current",
            "fail",
            f"the code release link {setup.show(paths.current)} {link_problem}",
            "run `omp-services init`",
        )
        return
    if not current or not (paths.current / "python" / "omp_services_main.py").is_file():
        report.add("runtime.current", "fail", "the installed code release is missing", "run `omp-services init`")
        return
    report.add("runtime.current", "ok", f"code release {current} is active")
    if runtime.verify_release(paths.releases_dir / current, current):
        report.add("runtime.integrity", "ok", "the active code release matches the digest in its name")
    else:
        report.add(
            "runtime.integrity",
            "fail",
            f"the active code release {current} does not match the digest in its name (changed or damaged)",
            "remove it and run `omp-services init`",
        )
    if state.get("release") and state["release"] != current:
        report.add("runtime.state", "warn", f"the state file says release {state['release']} but {current} is active")
    python_path = state.get("python") or sys.executable
    omp_path = ctx.get("omp")
    for name, text in sorted(runtime.wrapper_texts(paths, python_path, omp_path).items()):
        target = paths.bin_dir / name
        try:
            actual = read_text(target)
        except ServicesError:
            report.add(f"wrapper.{name}", "fail", f"{setup.show(target)} is missing", "run `omp-services init`")
            continue
        if actual != text:
            report.add(
                f"wrapper.{name}",
                "warn",
                f"{setup.show(target)} differs from what init would write",
                "run `omp-services init`",
            )
        elif not os.access(target, os.X_OK):
            report.add(f"wrapper.{name}", "fail", f"{setup.show(target)} is not executable", f"chmod 700 {target}")


def _manager(report: Report, setup: Setup, state: dict[str, Any]) -> None:
    problem = setup.manager.problem()
    has_units = bool(state.get("units"))
    if problem:
        report.add("manager", "fail" if has_units else "warn", f"{setup.manager.kind}: {problem}")
    else:
        report.add("manager", "ok", f"{setup.manager.kind} answers")
    if setup.manager.kind == "systemd":
        note = setup.manager.linger_note()
        if note and has_units:
            report.add(
                "linger", "warn", "lingering is off: services stop at logout", "run once: loginctl enable-linger"
            )
        elif not note:
            report.add("linger", "ok", "lingering is on: services keep running after logout")


def _units(report: Report, setup: Setup, state: dict[str, Any], ctx: dict[str, Any]) -> None:
    python_path = state.get("python") or sys.executable
    desired, warnings = desired_units(setup, python_path, ctx.get("omp"))
    for warning in warnings:
        report.add("units.plan", "warn", warning)
    wanted = {d.spec.label for d in desired}
    protected = {lab for lab, i in state.get("units", {}).items() if i.get("protected")}
    for item in desired:
        label = item.spec.label
        try:
            existing = read_text(item.path)
        except ServicesError:
            existing = None
        suffix = " (protected)" if item.spec.protected else ""
        missing_files = [p for p in item.spec.needs if not os.path.exists(p)]
        if missing_files:
            report.add(
                f"unit.{label}",
                "fail",
                f"cannot start: {setup.show(missing_files[0])} does not exist{suffix}",
                "reinstall the plugin that declared it, or fix its services.d file",
            )
            continue
        if existing is None:
            report.add(
                f"unit.{label}",
                "fail",
                f"the unit file {setup.show(item.path)} is missing{suffix}",
                "run `omp-services init`",
            )
            continue
        if existing != item.text:
            report.add(
                f"unit.{label}",
                "warn",
                f"the unit file differs from what init would write{suffix}",
                "run `omp-services init` (a protected unit needs --restart-protected)",
            )
        if not setup.manager.is_loaded(label):
            report.add(
                f"unit.{label}", "fail", f"not loaded in {setup.manager.kind}{suffix}", "run `omp-services init`"
            )
        elif not setup.manager.is_running(label):
            report.add(
                f"unit.{label}",
                "fail",
                f"loaded but not running{suffix}",
                f"look at the log: {setup.show(item.spec.log)}",
            )
        elif existing == item.text:
            report.add(f"unit.{label}", "ok", f"loaded and running{suffix}")
    for label in sorted(set(owned_labels(setup, state)) - wanted):
        note = " (protected: left running)" if label in protected else ""
        report.add(f"unit.{label}", "warn", f"installed but no longer wanted{note}", "run `omp-services init`")
    if not desired and not owned_labels(setup, state):
        report.add("units", "info", "no services are installed (the heartbeat is off and no plugin declared one)")


def _dropins(report: Report, setup: Setup) -> None:
    for kind in ("health", "services"):
        directory = setup.paths.health_d if kind == "health" else setup.paths.services_d
        drops, problems = dropins.load_dir(kind, directory)
        for problem in problems:
            report.add(f"dropin.{kind}", "fail", problem)
        for drop in drops:
            if drop.ok:
                assert drop.data is not None
                count = len(drop.data["checks"] if kind == "health" else drop.data["services"])
                runs = _what_runs(kind, drop.data)
                report.add(
                    f"dropin.{kind}.{drop.plugin}",
                    "ok",
                    f"{count} {'check' if kind == 'health' else 'service'}(s)"
                    + (f"; makes this machine run: {runs}" if runs else ""),
                )
            else:
                report.add(
                    f"dropin.{kind}.{drop.plugin}",
                    "fail",
                    f"not used: {drop.errors[0]}",
                    f"fix {setup.show(drop.path)}",
                )


def _what_runs(kind: str, data: dict[str, Any]) -> str:
    """The programs a drop-in makes this machine run, by path (arguments are not shown)."""
    programs: list[str] = []
    if kind == "health":
        programs = [c["argv"][0] for c in data["checks"] if c["type"] == "command"]
    else:
        for svc in data["services"]:
            program = svc["program"]
            programs.append(
                program["argv"][0] if program["type"] == "exec" else f"{program['type']} {program['script']}"
            )
    return ", ".join(programs)


def _logs(report: Report, setup: Setup) -> None:
    limit = setup.cfg.log_max_bytes
    big = heartbeat.oversized_logs(setup.paths.logs_dir, 2 * limit)
    for log in big:
        report.add(
            f"logs.{log.name}",
            "warn",
            f"{log.name} has grown past twice the {limit}-byte bound ({log.stat().st_size} bytes)",
            "run `omp-services init` (it trims logs); the heartbeat also trims them every round",
        )


def _heartbeat(report: Report, setup: Setup) -> None:
    cfg, paths = setup.cfg, setup.paths
    hb = cfg.heartbeat
    url_file = cfg.url_file(paths)
    if not hb["enabled"]:
        report.add("heartbeat", "info", "off (nothing is pinged until you run `omp-services heartbeat set-url`)")
        return
    try:
        url = heartbeat.read_url(cfg, paths)
    except ServicesError as exc:
        report.add("heartbeat.url", "fail", str(exc), f"check {setup.show(url_file)} (mode 600, one https:// URL)")
        url = None
    else:
        if url is None:
            report.add(
                "heartbeat.url",
                "warn",
                f"enabled, but {setup.show(url_file)} holds no URL: nothing is pinged",
                "run `omp-services heartbeat set-url`",
            )
        else:
            report.add(
                "heartbeat.url", "ok", f"an https URL is stored in {setup.show(url_file)} (mode 600; never printed)"
            )
    drops, _ = dropins.load_dir("health", paths.health_d)
    if not any(d.ok for d in drops) and not hb["allowNoChecks"]:
        report.add(
            "heartbeat.checks",
            "warn",
            "the heartbeat is enabled but no health checks are configured: every round sends the failure ping",
            "install a plugin that writes a health.d drop-in, or set heartbeat.allowNoChecks to true",
        )
    status = heartbeat.read_status(paths)
    if status is None:
        report.add(
            "heartbeat.last-run",
            "warn",
            "the heartbeat has not recorded a run yet",
            "is its unit running? see the unit findings above",
        )
        return
    interval = float(status.get("intervalSeconds") or hb["intervalSeconds"])
    age = time.time() - float(status.get("at", 0))
    if age > heartbeat.MAX_STATUS_AGE_FACTOR * interval:
        report.add(
            "heartbeat.last-run",
            "fail",
            f"stale: the last run was {int(age)} s ago, the interval is {int(interval)} s",
            "start the unit or run `omp-services heartbeat run --once`",
        )
    else:
        ping = status.get("ping", {})
        detail = ping.get("detail", "?")
        if ping.get("attempted") and not ping.get("ok"):
            report.add(
                "heartbeat.last-run", "fail", f"the last ping failed ({detail})", "check the URL and the network"
            )
        else:
            report.add(
                "heartbeat.last-run",
                "ok",
                f"last run {int(age)} s ago; ping {'sent' if ping.get('attempted') else 'not sent'}",
            )


def _health(report: Report, setup: Setup) -> None:
    ctx = heartbeat.build_context(setup.cfg, setup.paths, setup.env, allow_actions=False)
    results, _ = heartbeat.collect_results(ctx)
    report.health = results
    if not results:
        report.add("health", "info", "no health checks are configured (no drop-ins in health.d)")
        return
    for result in results:
        id = f"health.{result.plugin}.{result.id}"
        if result.status == "ok":
            report.add(id, "ok", f"{result.type}: ok ({result.reason})")
        else:
            level = "fail" if result.severity == "fail" else "warn"
            report.add(id, level, f"{result.type}: {result.status} ({result.reason})")


def _catalog(report: Report, setup: Setup, omp: str, refresh: bool) -> None:
    cfg = setup.cfg
    try:
        if refresh:
            catalog.refresh_catalog(omp, setup.env, cfg.marketplace)
        rows = catalog.plugin_rows(omp, setup.env, cfg.marketplace)
    except ServicesError as exc:
        report.add("catalog", "warn", f"could not compare versions: {exc}")
        return
    if not rows:
        report.add("catalog", "info", f"no plugin from the marketplace {cfg.marketplace} is installed through omp")
        return
    newer = [r for r in rows if r.outdated]
    for row in newer:
        report.add(
            f"update.{row.plugin}",
            "info",
            (
                f"newer version available: {row.plugin} {row.installed} -> {row.available}"
                if row.kind == "newer"
                else f"a different version is available (not comparable, so not called newer): {row.plugin} {row.installed} -> {row.available}"
            ),
            "run `omp plugin upgrade <id>`, then the plugin's own `bin/omp-services init`",
        )
    if not newer:
        report.add(
            "catalog",
            "ok",
            "installed plugins match the catalog: " + ", ".join(f"{r.plugin} {r.installed}" for r in rows),
        )
    mine = next((r for r in rows if r.plugin == "omp-services"), None)
    if mine and mine.installed != __version__:
        report.add(
            "runtime.version", "info", f"omp installed omp-services {mine.installed}, this code is {__version__}"
        )
