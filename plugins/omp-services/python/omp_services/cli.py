"""The `omp-services` command line.

    omp-services init        show what setup would do and, after a yes, do it (units, code release, wrappers)
    omp-services doctor      check everything and say what is wrong; changes nothing
    omp-services uninstall   remove what init installed; keep your configuration, secrets and logs
    omp-services purge       remove everything, including configuration and secrets (asks separately)
    omp-services heartbeat   run | set-url | clear-url | status
    omp-services health      run a health drop-in once (also the entry point of the health/1 conformance suite)
    omp-services config      path | show | validate
    omp-services render      print the unit files a setup would install (any platform)

Exit status: 0 success, 1 a finding or failure, 2 usage or configuration error, 3 not confirmed (nothing was changed).
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import os
import signal
import sys
import time
from collections.abc import Callable
from pathlib import Path

from . import config, console, heartbeat, net, runtime
from .checks import run_dropins
from .doctor import run_doctor
from .dropins import load_standalone
from .plan import Setup, desired_units, plan_init, plan_purge, plan_uninstall
from .units import render
from .util import (
    FileLock,
    ServicesError,
    ensure_private_dir,
    file_problem,
    kill_active_groups,
    read_text,
    write_file,
)

EXIT_NOT_CONFIRMED = 3
EXIT_PLAN_CHANGED = 5


def _say(text: str) -> None:
    print(text, flush=True)


def _ask_yes(args: argparse.Namespace, question: str) -> bool:
    return bool(getattr(args, "yes", False)) or console.confirm(question)


def _not_confirmed() -> int:
    _say("not confirmed: nothing was changed. Run again and answer y, or add --yes to skip the question.")
    return EXIT_NOT_CONFIRMED


# ---------------------------------------------------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------------------------------------------------


def _locked(setup: Setup, *, mutating: bool = True):
    """One mutating command at a time (init, uninstall, purge). The lock file sits next to the data directory
    (`<data>.lock`), not inside it: purge deletes the data directory, and a lock inside it would be replaced by a new
    inode while the purge still held the old one. It exists before the first init creates anything, so two first inits
    exclude each other too. A read-only preview (--plan) takes no lock and creates no file."""
    problem = setup.paths.layout_problem()
    if problem:
        raise ServicesError(problem)
    if not mutating:
        return contextlib.nullcontext()
    lock = setup.paths.lock_file
    lock.parent.mkdir(parents=True, exist_ok=True)
    return FileLock(lock)


def cmd_init(args: argparse.Namespace) -> int:
    setup = Setup.load()
    with _locked(setup, mutating=not args.plan):
        state = runtime.load_state(setup.paths)
        plan = plan_init(setup, state, repin=args.repin, restart_protected=set(args.restart_protected or []))
        if args.json:
            _say(json.dumps({"plan": plan.render(), "digest": plan.digest(), "changes": plan.changes}))
            return 0 if args.plan else 2
        _say(plan.render())
        if args.plan:
            return 0
        if args.expect_plan and args.expect_plan != plan.digest():
            _say(
                "the plan changed since it was shown: nothing was applied. Run `omp-services init` again to see the new plan."
            )
            return EXIT_PLAN_CHANGED
        if not plan.changes:
            return 0
        if not _ask_yes(args, "Apply this plan?"):
            return _not_confirmed()
        plan.apply(_say)
        _say("\nDone. `omp-services doctor` checks the result.")
        return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    setup = Setup.load()
    report = run_doctor(
        setup,
        health=not args.no_health,
        refresh_catalog=args.refresh_catalog,
        offline_catalog=not args.no_catalog,
    )
    if args.json:
        _say(json.dumps(report.as_dict(), indent=1))
    else:
        sys.stdout.write(report.render())
    return 1 if report.failed else 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    setup = Setup.load()
    with _locked(setup, mutating=not args.plan):
        state = runtime.load_state(setup.paths)
        plan = plan_uninstall(setup, state, include_protected=args.include_protected)
        _say(plan.render())
        if args.plan or not plan.changes:
            return 0
        if not _ask_yes(args, "Remove these?"):
            return _not_confirmed()
        plan.apply(_say)
        _say(
            "\nUninstalled. Your configuration, drop-ins, secrets and logs are still there; `omp-services purge` removes them."
        )
        return 0


def cmd_purge(args: argparse.Namespace) -> int:
    setup = Setup.load()
    with _locked(setup, mutating=not args.plan):
        state = runtime.load_state(setup.paths)
        plan = plan_purge(setup, state, include_protected=args.include_protected)
        _say(plan.render())
        if args.plan:
            return 0
        if not plan.changes:
            _say("nothing to purge")
            return 0
        # Its own confirmation: --yes does not count. Interactive: type the word. Scripts: --confirm-purge.
        if not (
            args.confirm_purge
            or console.confirm_word("This permanently deletes the folders above, including your secrets.", "purge")
        ):
            return _not_confirmed()
        plan.apply(_say)
        return 0


def cmd_heartbeat(args: argparse.Namespace) -> int:
    setup = Setup.load()
    paths, cfg = setup.paths, setup.cfg
    if args.action == "run":
        if args.once:
            return heartbeat.run_once(paths, setup.env, send=not args.no_ping, allow_actions=not args.no_ping)
        return heartbeat.run_loop(paths, setup.env)
    cfg.require_valid()
    if args.action == "status":
        status = heartbeat.read_status(paths)
        if status is None:
            _say("no heartbeat run has been recorded yet")
            return 1
        ping = status["ping"]
        age = time.time() - float(status["at"])
        interval = float(status.get("intervalSeconds") or cfg.heartbeat["intervalSeconds"])
        stale = age > heartbeat.MAX_STATUS_AGE_FACTOR * interval
        _say(
            f"last run: {int(age)} s ago, checks {'ok' if status['ok'] else 'FAILING'}, ping "
            f"{('sent' if ping['ok'] else 'FAILED') if ping['attempted'] else ('not sent' if ping['ok'] else 'NOT ATTEMPTED (unusable URL)')}"
            f" ({ping['detail']})"
        )
        if stale:
            _say(
                f"STALE: the record is older than {heartbeat.MAX_STATUS_AGE_FACTOR} intervals ({int(interval)} s each): the heartbeat is not running"
            )
        for r in status["results"]:
            _say(f"  {r['status']:<13} {r['plugin']}/{r['id']} ({r['type']}): {r['reason']}")
        return 0 if status["ok"] and ping["ok"] and not stale else 1
    if args.action == "set-url":
        if sys.stdin.isatty():
            text = getpass.getpass("Heartbeat URL (https://, input is hidden): ")
        else:
            text = sys.stdin.readline()
        url = text.strip()
        if not url:
            raise ServicesError("no URL given")

        try:
            net.parse_http_url(url, https_only=True)
        except ServicesError as exc:
            raise ServicesError(f"the URL is not usable: {exc}") from None
        ensure_private_dir(paths.config_dir)
        ensure_private_dir(paths.secrets_dir)
        target = _managed_url_file(setup)
        write_file(target, url + "\n", 0o600)
        _set_heartbeat_enabled(setup, True)
        _say(
            f"stored in {setup.show(target)} (mode 600). The heartbeat is enabled; run `omp-services init` to install its unit."
        )
        return 0
    if args.action == "clear-url":
        target = _managed_url_file(setup)
        existed = target.exists()
        if existed:
            target.unlink()
        _set_heartbeat_enabled(setup, False)
        _say(
            "the URL was removed and the heartbeat is disabled; run `omp-services init` to remove its unit."
            if existed
            else "no URL was stored; the heartbeat is disabled."
        )
        return 0
    raise ServicesError(f"unknown heartbeat action {args.action}")


def _managed_url_file(setup: Setup) -> Path:
    """The URL file this command may write or delete: one inside the secrets directory. A `heartbeat.urlFile` that
    points anywhere else is the person's own file, and these commands never overwrite or unlink it."""
    target = setup.cfg.url_file(setup.paths)
    if target.parent != setup.paths.secrets_dir:
        raise ServicesError(
            "heartbeat.urlFile points outside the secrets directory, so omp-services will not write or delete it: "
            "manage that file by hand, or remove the setting"
        )
    return target


def _set_heartbeat_enabled(setup: Setup, enabled: bool) -> None:
    """Change one key in config.json and keep everything else the person wrote."""
    paths = setup.paths
    ensure_private_dir(paths.config_dir)
    values = (
        json.loads(read_text(paths.config_file))
        if paths.config_file.exists()
        else json.loads(config.default_config_text())
    )
    values.setdefault("heartbeat", {})["enabled"] = enabled
    write_file(paths.config_file, json.dumps(values, indent="\t") + "\n", 0o600)


def cmd_health(args: argparse.Namespace) -> int:
    setup = Setup.load()
    setup.cfg.require_valid()
    path = Path(args.file)
    drop = load_standalone("health", path)
    if not drop.ok:
        _say(json.dumps({"invalid": drop.errors}, indent=1) if args.json else "invalid: " + "; ".join(drop.errors))
        return 2
    # A diagnostic run reports and never acts: a watchdog check here cannot restart anything (`--no-actions` is
    # accepted for the conformance contract, and changes nothing because report-only is the only mode).
    ctx = heartbeat.build_context(setup.cfg, setup.paths, setup.env, allow_actions=False)
    results = run_dropins([drop], ctx, float(setup.cfg.heartbeat["cycleTimeoutSeconds"]))
    if args.json:
        _say(json.dumps([r.as_dict() for r in results], indent=1))
    else:
        for r in results:
            _say(f"{r.status:<13} {r.plugin}/{r.id} ({r.type}): {r.reason}")
    return 0 if all(not r.failing for r in results) else 1


def cmd_config(args: argparse.Namespace) -> int:
    setup = Setup.load()
    if args.action == "path":
        _say(str(setup.paths.config_file))
        return 0
    if args.action == "show":
        _say(json.dumps(setup.cfg.values, indent=1))
        if setup.cfg.overrides:
            _say("environment overrides in effect: " + ", ".join(setup.cfg.overrides))
        return 0 if not setup.cfg.problems else 1
    if setup.cfg.problems:
        for problem in setup.cfg.problems:
            _say(f"invalid: {problem}")
        return 1
    problem = file_problem(setup.paths.config_file, forbid=0o022) if setup.cfg.file_present else None
    if problem:
        _say(f"invalid: config.json {problem}")
        return 1
    _say("valid" + (" (no file: defaults)" if not setup.cfg.file_present else ""))
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    env = dict(os.environ)
    if args.platform:
        env["OMP_SERVICES_PLATFORM"] = {"launchd": "darwin", "systemd": "linux"}.get(args.platform, args.platform)
    setup = Setup.load(env)
    setup.cfg.require_valid()
    state = runtime.load_state(setup.paths)

    python_path = runtime.choose_python(setup.cfg, state)
    omp_path = runtime.choose_omp(setup.cfg, state, setup.env, setup.paths.home)
    desired, warnings = desired_units(setup, python_path, omp_path)
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        for item in desired:
            write_file(out / item.path.name, item.text, 0o644)
            _say(str(out / item.path.name))
        return 0
    for item in desired:
        _say(f"# {item.path.name}\n{render(item.spec, setup.manager.kind)}")
    return 0


# ---------------------------------------------------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="omp-services", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--version", action="version", version=runtime.version_line())
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    def add(name: str, fn: Callable[[argparse.Namespace], int], help: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help)
        p.set_defaults(fn=fn)
        return p

    p = add("init", cmd_init, "set up units, code release and wrappers (shows a plan, asks first)")
    p.add_argument("--plan", action="store_true", help="show the plan and stop")
    p.add_argument("--yes", action="store_true", help="apply without asking")
    p.add_argument("--json", action="store_true", help="with --plan: print the plan and its digest as JSON")
    p.add_argument("--expect-plan", metavar="DIGEST", help="apply only if the plan still has this digest (as shown)")
    p.add_argument("--repin", action="store_true", help="look for omp again instead of keeping the pinned one")
    p.add_argument(
        "--restart-protected",
        action="append",
        metavar="NAME",
        help="allow changing this protected service (repeatable)",
    )

    p = add("doctor", cmd_doctor, "check everything and say what is wrong")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-health", action="store_true", help="skip running the health checks")
    p.add_argument("--no-catalog", action="store_true", help="skip the installed-versus-catalog comparison")
    p.add_argument(
        "--refresh-catalog",
        action="store_true",
        help="refetch the marketplace catalog first (uses the network, through omp)",
    )

    p = add("uninstall", cmd_uninstall, "remove what init installed; keeps configuration, secrets and logs")
    p.add_argument("--plan", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--include-protected", action="store_true", help="also stop and remove protected services")

    p = add("purge", cmd_purge, "remove everything, including configuration and secrets")
    p.add_argument("--plan", action="store_true")
    p.add_argument(
        "--confirm-purge", action="store_true", help="the separate confirmation for scripts (--yes is not enough)"
    )
    p.add_argument("--include-protected", action="store_true")

    p = add("heartbeat", cmd_heartbeat, "run the heartbeat, or store/clear its URL")
    p.add_argument("action", choices=["run", "set-url", "clear-url", "status"])
    p.add_argument("--once", action="store_true", help="run: one round, then exit")
    p.add_argument("--no-ping", action="store_true", help="run --once: check only, send nothing, restart nothing")

    p = add("health", cmd_health, "run one health drop-in file once")
    p.add_argument("--file", required=True, metavar="FILE")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-actions", action="store_true", help="a watchdog check only reports")

    p = add("config", cmd_config, "path, show or validate the configuration")
    p.add_argument("action", choices=["path", "show", "validate"])

    p = add("render", cmd_render, "print the unit files a setup would install")
    p.add_argument(
        "--platform", choices=["launchd", "systemd", "darwin", "linux"], help="render for the other platform"
    )
    p.add_argument("--out", metavar="DIR", help="write the files here instead of printing them")
    return parser


def _terminate(signum: int, frame: object) -> None:
    """Stopping the command line stops the commands its checks started, then ends with the conventional status. The
    handler takes no lock, so it cannot wait for the code it interrupted."""
    kill_active_groups()
    raise SystemExit(128 + signum)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, _terminate)
    try:
        return int(args.fn(args))
    except ServicesError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
