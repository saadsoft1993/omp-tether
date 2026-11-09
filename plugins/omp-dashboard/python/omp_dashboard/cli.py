"""The `omp-dashboard` command line.

    omp-dashboard run                      run the hub (in a terminal you keep open, or under omp-services / a unit you wrote)
    omp-dashboard pair                     print a single-use link that pairs one browser (valid 5 minutes)
    omp-dashboard devices                  list paired browsers
    omp-dashboard revoke <id|all>          unpair one browser (4 or more characters of its id), or every browser
    omp-dashboard doctor [--json]          check everything and say what is wrong; changes nothing
    omp-dashboard config path|show|validate|init|set-ntfy-topic [--stdin]|clear-ntfy-topic
    omp-dashboard dropins [--write]        show (or write) the drop-ins omp-services reads

Exit status: 0 success, 1 a finding or a refusal, 2 usage or configuration error.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from collections.abc import Mapping

from . import __version__, doctor
from .alerts import TOPIC_RE, read_topic, write_topic
from .audit import audit
from .auth import build_adapter
from .config import Config, default_config_text, load_config
from .dropins import dropin_paths, dropin_text, health_dropin, services_dropin, stray_overrides, write_dropin
from .hostplat import Platform, PlatformError
from .pairing import PairingError, PairingStore
from .paths import Paths, from_env
from .util import DashboardError, ensure_private_dir, trust_ancestors, write_file

PAIR_MINUTES = 5


def _say(text: str = "") -> None:
    print(text, flush=True)


def _err(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def _load(env: Mapping[str, str]) -> tuple[Paths, Config]:
    paths = from_env(env)
    return paths, load_config(paths, env)


def _valid(env: Mapping[str, str]) -> Config | None:
    try:
        _, config = _load(env)
    except DashboardError as exc:
        _err(f"omp-dashboard: {exc}")
        return None
    if config.problems:
        _err("omp-dashboard: the configuration is invalid:")
        for problem in config.problems:
            _err(f"  {problem}")
        _err("run `omp-dashboard config validate`, then fix the file or the variable")
        return None
    return config


def _store(config: Config) -> PairingStore:
    return PairingStore(config.pairing_dir, config.get("pairing.cookieName"))


# ---------------------------------------------------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    config = _valid(env)
    if config is None:
        return 2
    from .server import serve

    try:
        return serve(config, dict(env))
    except DashboardError as exc:
        _err(f"omp-dashboard: {exc}")
        return 1


def _audit(config: Config, action: str, target: str | None, outcome: str) -> None:
    """The command line's own changes to who is paired go in the audit log too (browser id `cli`); a failure to write is a warning."""
    try:
        audit(config.audit_file, action, target, outcome, "cli", max_bytes=int(config.get("audit.maxBytes")))
    except (OSError, DashboardError) as exc:
        _err(f"omp-dashboard: the audit line could not be written ({exc})")


def cmd_pair(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    config = _valid(env)
    if config is None:
        return 2
    adapter = build_adapter(config, dict(env))
    url = adapter.public_url()
    if not url:
        _err("omp-dashboard: Tailscale is not running or does not know this machine; start it and try again.")
        _audit(config, "pair-token", None, "refused:no_address")
        return 1
    port, uid = int(config.get("listen.port")), os.getuid()
    try:
        owners = Platform().listener_uids(port)
    except PlatformError as exc:
        _err(f"omp-dashboard: could not check who listens on port {port} ({exc}); no pairing token was made.")
        _audit(config, "pair-token", None, "refused:listener_unknown")
        return 1
    if owners != {uid}:
        _err(
            f"omp-dashboard: the hub is not running on port {port} (or the port is held by another account), so no pairing "
            "token was made: a link must never lead to a program that is not yours. Start it with `omp-dashboard run`."
        )
        return 1
    try:
        token = _store(config).writer.create_token()
    except PairingError as exc:
        _err(f"omp-dashboard: {exc}")
        _audit(config, "pair-token", None, f"refused:{exc.code}")
        return 1
    _audit(config, "pair-token", None, "created")
    link = f"{url}#pair={token}"
    if args.json:
        _say(json.dumps({"url": link, "expiresInSeconds": PAIR_MINUTES * 60}))
        return 0
    _err(
        f"Open this link in the browser you want to pair, within {PAIR_MINUTES} minutes. It works once: do not paste it anywhere "
        "else, and run this in a terminal, not inside an omp session (the link would land in its transcript)."
    )
    _say(link)
    return 0


def cmd_devices(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    config = _valid(env)
    if config is None:
        return 2
    try:
        devices = _store(config).writer.devices()
    except PairingError as exc:
        _err(f"omp-dashboard: {exc}")
        return 1
    if not devices:
        _say("No browser is paired.")
    for d in devices:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(d.paired_at))
        _say(f"{d.id}  paired {when}  {d.label}")
    return 0


def cmd_revoke(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    config = _valid(env)
    if config is None:
        return 2
    target = "all" if args.id == "all" else args.id[:8]
    try:
        writer = _store(config).writer
        if args.id == "all":
            # One hold of the store lock for both: a redemption cannot land between unpairing and cancelling the links.
            count, _links = writer.revoke_everything()
        else:
            count = writer.revoke(args.id)
    except PairingError as exc:
        _err(f"omp-dashboard: {exc}")
        _audit(config, "revoke", target, f"refused:{exc.code}")
        return 1
    _audit(config, "revoke", target, f"unpaired:{count}")
    _say(f"Unpaired {count} browser(s).")
    return 1 if count == 0 and args.id != "all" else 0


def cmd_doctor(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    try:
        paths, config = _load(env)
    except DashboardError as exc:
        _err(f"omp-dashboard: {exc}")
        return 2
    findings = doctor.run_doctor(config, env)
    if args.json:
        _say(json.dumps([f.as_dict() for f in findings], indent=1))
    else:
        sys.stdout.write(doctor.render(findings))
    return 1 if any(f.level == doctor.FAIL for f in findings) else 0


def cmd_config(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    try:
        paths, config = _load(env)
    except DashboardError as exc:
        _err(f"omp-dashboard: {exc}")
        return 2
    action = args.action
    if action == "path":
        _say(str(paths.config_file))
        return 0
    if action == "validate":
        if config.problems:
            for problem in config.problems:
                _err(problem)
            return 1
        _say("The configuration is valid." + ("" if config.file_present else " (No file: the defaults are in use.)"))
        return 0
    if action == "show":
        if config.problems:
            for problem in config.problems:
                _err(problem)
            return 1
        _err(
            f"# file: {paths.config_file if config.file_present else 'none (defaults)'}; overrides: {', '.join(config.overrides) or 'none'}"
        )
        _say(json.dumps(config.values, indent=2))
        return 0
    if action == "init":
        if paths.config_file.exists() or paths.config_file.is_symlink():
            _err(f"omp-dashboard: {paths.config_file} exists; it was not changed")
            return 1
        ensure_private_dir(paths.config_file.parent)
        write_file(paths.config_file, default_config_text(), 0o600)
        _say(f"Wrote {paths.config_file} with the defaults. Edit it, then run `omp-dashboard doctor`.")
        return 0
    if config.problems:
        for problem in config.problems:
            _err(problem)
        return 2
    topic_file = config.ntfy_topic_file
    if action == "clear-ntfy-topic":
        trust_ancestors(
            topic_file.parent
        )  # deleting is a change too: never through a directory somebody else can replace
        try:
            topic_file.unlink()
        except FileNotFoundError:
            _say("No topic was set.")
            return 0
        _say("Alerts are off: the topic file was removed.")
        return 0
    if action == "set-ntfy-topic":
        try:
            existing = read_topic(topic_file)
        except DashboardError:
            existing = "unusable"
        if existing is not None and not args.replace:
            _err(f"omp-dashboard: a topic is already set ({topic_file}); use --replace to change it")
            return 1
        # Never an argument: a topic given on the command line is visible to every process (ps) and kept in shell history.
        topic = sys.stdin.read(200).strip() if args.stdin else "omp-" + secrets.token_urlsafe(16)
        if not TOPIC_RE.match(topic):
            _err(
                "omp-dashboard: a topic is 16 to 64 letters, digits, - or _ (it is a secret: anyone who knows it can read your alerts)"
            )
            return 2
        if topic_file.is_symlink():
            _err(f"omp-dashboard: {topic_file} is a symbolic link; remove it first")
            return 1
        write_topic(topic_file, topic)
        server = config.get("alerts.server")
        _say(f"Alerts are on. Messages go to {server}, a third party, and name the project folder and the model.")
        _say(
            "The topic is a secret: anyone who knows it can read your alerts and send you messages. Subscribe to it in the ntfy app:"
        )
        _say(f"  {topic}")
        return 0
    return 2


def cmd_dropins(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    config = _valid(env)
    if config is None:
        return 2
    docs = {"services": services_dropin(config, env), "health": health_dropin(config, env)}
    paths = dropin_paths(config)
    if not args.write:
        for kind, doc in docs.items():
            _say(f"# {paths[kind]}")
            sys.stdout.write(dropin_text(doc))
        _say("# nothing was written; add --write to write both files, then run `omp-services init`")
        return 0
    stray = stray_overrides(env, config)
    if stray:
        _err(
            f"omp-dashboard: these variables are set here and a service would not see them: {', '.join(stray)}. "
            "Put them in config.json first, so that the service and this command read the same settings."
        )
        return 1
    try:
        for kind, doc in docs.items():
            _say(f"wrote {write_dropin(paths[kind], doc)}")
    except DashboardError as exc:
        _err(f"omp-dashboard: {exc}")
        return 1
    _say("Next: `omp-services init` shows its plan and asks before installing the unit.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="omp-dashboard", description="A local dashboard for your own omp sessions.")
    parser.add_argument("--version", action="version", version=f"omp-dashboard {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run the hub").set_defaults(fn=cmd_run)
    p = sub.add_parser("pair", help="print a single-use pairing link")
    p.add_argument("--json", action="store_true", help="print {url, expiresInSeconds} as JSON")
    p.set_defaults(fn=cmd_pair)
    sub.add_parser("devices", help="list paired browsers").set_defaults(fn=cmd_devices)
    p = sub.add_parser("revoke", help="unpair one browser, or all")
    p.add_argument("id", help="4 or more characters of a device id, or 'all'")
    p.set_defaults(fn=cmd_revoke)
    p = sub.add_parser("doctor", help="check everything; changes nothing")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_doctor)
    p = sub.add_parser("config", help="show or check the configuration")
    p.add_argument("action", choices=["path", "show", "validate", "init", "set-ntfy-topic", "clear-ntfy-topic"])
    p.add_argument(
        "--stdin",
        action="store_true",
        help="with set-ntfy-topic: read the topic from standard input (default: make a random one)",
    )
    p.add_argument("--replace", action="store_true", help="with set-ntfy-topic: replace an existing topic")
    p.set_defaults(fn=cmd_config)
    p = sub.add_parser("dropins", help="show or write the omp-services drop-ins")
    p.add_argument("--write", action="store_true")
    p.set_defaults(fn=cmd_dropins)
    return parser


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    args = build_parser().parse_args(argv)
    if args.command == "config" and args.action != "set-ntfy-topic" and (args.stdin or args.replace):
        _err("omp-dashboard: --stdin and --replace belong to set-ntfy-topic")
        return 2
    try:
        return int(args.fn(args, env))
    except DashboardError as exc:
        _err(f"omp-dashboard: {exc}")
        return 1
    except KeyboardInterrupt:
        return 130
