"""`doctor`: what is installed, what is running, and whether what answers is really yours. Nothing here changes anything
and nothing leaves this machine (no ntfy message is sent, no remote host is asked).
"""

from __future__ import annotations

import http.client
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass

from . import __version__, relay
from .alerts import read_topic
from .auth import TAILSCALE_APP, TailscaleServe, check_loopback_bind
from .compat import MIN_OMP, TESTED_OMP, parse_omp_version
from .config import Config
from .control import ControlDir, ControlError
from .dropins import dropin_paths, dropin_text, health_dropin, services_dropin, stray_overrides
from .hostplat import Platform, PlatformError
from .omp import Omp, OmpError, find_tool
from .server import HubApp
from .ui import UiBundleError, load_bundle
from .util import DashboardError, Deadline, connect_bounded, one_line, read_text, run_bounded, safe_env

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass(frozen=True)
class Finding:
    id: str
    level: str
    message: str
    fix: str | None = None

    def as_dict(self) -> dict[str, str]:
        out = {"id": self.id, "level": self.level, "message": self.message}
        if self.fix:
            out["fix"] = self.fix
        return out


def _f(id_: str, level: str, message: str, fix: str | None = None) -> Finding:
    return Finding(id_, level, message, fix)


def parse_serve_status(text: str, port: int, target: str) -> tuple[str, str | bool | None]:
    """Reads `tailscale serve status --json`: is there a route from https://<this machine>:<port> to `target`, and is
    Funnel (the public internet) switched on for it? ('ok', funnel) | ('absent', None) | ('elsewhere', where) | ('unreadable', None)."""
    try:
        status = json.loads(text)
    except ValueError:
        return "unreadable", None
    if not isinstance(status, dict):
        return "unreadable", None
    web = status.get("Web") or {}
    funnel = status.get("AllowFunnel") or {}
    for host_port, entry in web.items() if isinstance(web, dict) else []:
        if not str(host_port).endswith(f":{port}"):
            continue
        proxy = ((entry or {}).get("Handlers") or {}).get("/", {}).get("Proxy") if isinstance(entry, dict) else None
        if not isinstance(proxy, str):
            return "elsewhere", "(not a proxy)"
        if proxy.rstrip("/") != target.rstrip("/"):
            return "elsewhere", proxy
        return "ok", bool(isinstance(funnel, dict) and funnel.get(host_port) is True)
    return "absent", None


def _probe_hub(host: str, port: int) -> str | None:
    """None when a hub answers /healthz on 127.0.0.1:<port>, else why not."""
    conn = http.client.HTTPConnection(host, port, timeout=2)
    deadline = Deadline(2)
    try:
        connect_bounded(conn, host, port, deadline)
        conn.request("GET", "/healthz")
        resp = conn.getresponse()
        body = resp.read(1024)
        deadline.check()
        if resp.status == 200 and b'"ok"' in body:
            return None
        return f"answered {resp.status}"
    except (OSError, http.client.HTTPException):
        return "no answer"
    finally:
        deadline.cancel()
        conn.close()


def run_doctor(config: Config, env: Mapping[str, str], platform: Platform | None = None) -> list[Finding]:
    out: list[Finding] = []
    platform = platform or Platform()
    uid = os.getuid()
    # --- python and configuration ---------------------------------------------------------------------------------
    out.append(
        _f(
            "python",
            OK,
            f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}, omp-dashboard {__version__}",
        )
    )
    if config.problems:
        for problem in config.problems:
            out.append(
                _f(
                    "config",
                    FAIL,
                    one_line(problem, 300),
                    "omp-dashboard config validate shows every problem; fix the file or the variable",
                )
            )
        return out  # nothing below can be trusted with an invalid configuration
    where = f"{config.paths.config_file}" if config.file_present else "none (the defaults are in use)"
    out.append(
        _f(
            "config",
            OK,
            f"configuration valid; file: {where}"
            + (f"; {len(config.overrides)} environment override(s)" if config.overrides else ""),
        )
    )
    # --- the page ---------------------------------------------------------------------------------------------------
    try:
        bundle = load_bundle()
        out.append(_f("ui-bundle", OK, f"the dashboard page matches its pinned hashes ({bundle.tree_sha256[:12]})"))
    except UiBundleError as exc:
        out.append(
            _f("ui-bundle", FAIL, str(exc), "reinstall the plugin (omp plugin upgrade) or rebuild with make build-ui")
        )
    # --- bind, port, sign-in ----------------------------------------------------------------------------------------
    host, port = config.get("listen.host"), int(config.get("listen.port"))
    try:
        check_loopback_bind(host, config.get("auth.adapter"))
        out.append(_f("bind", OK, f"the hub listens on {host}:{port} (loopback only)"))
    except DashboardError as exc:
        out.append(_f("bind", FAIL, str(exc)))
    try:
        owners = platform.listener_uids(port)
    except PlatformError:
        owners = None
    answer = _probe_hub(host, port)
    if answer is None:
        out.append(_f("hub", OK, f"a hub answers on {host}:{port}"))
    elif owners and owners != {uid}:
        out.append(
            _f(
                "hub",
                FAIL,
                f"port {port} is held by another account's process",
                "choose another listen.port, or stop what holds it",
            )
        )
    else:
        out.append(
            _f(
                "hub",
                WARN,
                f"no hub answers on {host}:{port} ({answer})",
                "start it: omp-dashboard run (or omp-services, see the README)",
            )
        )
    if answer is None and owners is not None and owners != {uid}:
        out.append(
            _f(
                "hub-owner",
                FAIL,
                f"the listener on port {port} is not owned by your account (or its owner cannot be seen)",
            )
        )
    adapter = config.get("auth.adapter")
    if adapter == "tailscale-serve":
        out.extend(_tailscale(config, env, platform))
    else:
        out.append(_f("auth", OK, "sign-in: loopback-only (the pairing cookie alone, on a loopback Host)"))
    # --- pairing ----------------------------------------------------------------------------------------------------
    app = HubApp(config, dict(env), platform)
    view = app.pairing.view()
    if view.refusal == "store-missing":
        out.append(_f("pairing", WARN, "no browser is paired yet", "omp-dashboard pair"))
    elif view.refusal:
        out.append(
            _f(
                "pairing",
                FAIL,
                f"the pairing store is refused ({view.refusal}); nobody is paired",
                f"check {config.pairing_dir}: it must be a 0700 directory of yours under directories nobody else can write",
            )
        )
    else:
        out.append(_f("pairing", OK, f"{len(view.hashes)} paired browser(s) in {config.pairing_dir}"))
    # --- omp --------------------------------------------------------------------------------------------------------
    out.extend(_omp(app.omp, config, env))
    # --- control ----------------------------------------------------------------------------------------------------
    out.append(_control(config))
    # --- relay ------------------------------------------------------------------------------------------------------
    out.extend(_relay(config, app, platform, uid))
    # --- start ------------------------------------------------------------------------------------------------------
    out.extend(_start(config, app, env))
    # --- alerts -----------------------------------------------------------------------------------------------------
    out.append(_alerts(config))
    # --- stats ------------------------------------------------------------------------------------------------------
    if config.get("stats.url"):
        out.append(_f("stats", OK, "the Usage page links to stats.url (the hub never talks to it)"))
    # --- service drop-ins -------------------------------------------------------------------------------------------
    out.extend(_dropins(config, env))
    return out


def _tailscale(config: Config, env: Mapping[str, str], platform: Platform) -> list[Finding]:
    out: list[Finding] = []
    adapter = TailscaleServe(
        config.get("auth.tailscale.bin"),
        config.serve_port,
        config.get("auth.tailscale.owner"),
        config.get("auth.tailscale.publicHost"),
        env=dict(env),
    )
    names, owner = adapter.identity()
    configured = config.get("auth.tailscale.bin")
    binary = find_tool("tailscale", configured, dict(env)) or (
        TAILSCALE_APP if not configured and os.access(TAILSCALE_APP, os.X_OK) else None
    )
    if not names or not owner:
        if binary is None and not (config.get("auth.tailscale.owner") and config.get("auth.tailscale.publicHost")):
            looked = (
                configured or "PATH, ~/.local/bin, /opt/homebrew/bin, /usr/local/bin, /usr/bin and the Tailscale app"
            )
            out.append(
                _f(
                    "tailscale",
                    FAIL,
                    f"the tailscale command was not found (looked at {looked}), so this machine's owner and name are not known: every request gets 503",
                    "set auth.tailscale.bin in the config file (or OMP_DASHBOARD_TAILSCALE_BIN) to the absolute path of the tailscale command, "
                    "for example /Applications/Tailscale.app/Contents/MacOS/Tailscale; or set auth.tailscale.owner and auth.tailscale.publicHost",
                )
            )
            return out
        out.append(
            _f(
                "tailscale",
                FAIL,
                "this machine's Tailscale owner and name are not known: every request gets 503",
                "start Tailscale, or set auth.tailscale.owner and auth.tailscale.publicHost",
            )
        )
        return out
    out.append(_f("tailscale", OK, f"owner and machine name known ({len(names)} name(s))"))
    if binary is None:
        out.append(
            _f(
                "serve-route",
                WARN,
                "the tailscale command was not found, so the Serve route cannot be checked",
                "set auth.tailscale.bin",
            )
        )
        return out
    result = run_bounded([binary, "serve", "status", "--json"], 10.0, env=safe_env(), max_output=1024 * 1024)
    if not result.ok:
        out.append(_f("serve-route", WARN, "`tailscale serve status` did not answer", None))
        return out
    port = int(config.get("listen.port"))
    host = config.get("listen.host")
    target = f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"
    state, detail = parse_serve_status(result.stdout, config.serve_port, target)
    if state == "ok":
        if detail:
            out.append(
                _f(
                    "serve-route",
                    FAIL,
                    f"Funnel is switched on for port {config.serve_port}: the hub is open to the internet's requests (they are refused, but they should not arrive)",
                    f"tailscale funnel --https={config.serve_port} off",
                )
            )
        else:
            out.append(
                _f("serve-route", OK, f"Tailscale Serve forwards https port {config.serve_port} to the hub (no Funnel)")
            )
    elif state == "elsewhere":
        out.append(
            _f(
                "serve-route",
                FAIL,
                f"Serve port {config.serve_port} forwards to {one_line(str(detail), 80)}, not to this hub",
                f"tailscale serve --bg --https={config.serve_port} {target}",
            )
        )
    elif state == "absent":
        out.append(
            _f(
                "serve-route",
                WARN,
                f"no Serve route on port {config.serve_port}: other devices cannot reach the hub",
                f"tailscale serve --bg --https={config.serve_port} {target}",
            )
        )
    else:
        out.append(_f("serve-route", WARN, "`tailscale serve status` printed something unreadable"))
    return out


def _omp(omp: Omp, config: Config, env: Mapping[str, str]) -> list[Finding]:
    out: list[Finding] = []
    binary = find_tool("omp", config.get("omp.bin"), dict(env), "OMP_BIN")
    if binary is None:
        return [_f("omp", FAIL, "omp was not found", "set omp.bin in the configuration, or put omp on PATH")]
    from .omp import child_env

    result = run_bounded(
        [binary, "--version"],
        10.0,
        env=child_env({**safe_env(), **{k: v for k, v in env.items() if k.startswith(("PI_", "OMP_"))}}),
    )
    version = parse_omp_version(result.stdout) if result.ok else None
    if version is None:
        out.append(_f("omp", FAIL, "`omp --version` printed nothing this dashboard recognises"))
    elif version < MIN_OMP:
        out.append(
            _f(
                "omp",
                FAIL,
                f"omp {'.'.join(map(str, version))} is older than the supported {'.'.join(map(str, MIN_OMP))}",
                "upgrade omp",
            )
        )
    elif ".".join(map(str, version)) not in TESTED_OMP:
        out.append(
            _f(
                "omp",
                WARN,
                f"omp {'.'.join(map(str, version))} is supported but not one of the versions this dashboard was tested with ({', '.join(TESTED_OMP)})",
            )
        )
    else:
        out.append(_f("omp", OK, f"omp {'.'.join(map(str, version))}"))
    try:
        hosts = omp.list_hosts()
        out.append(_f("collab", OK, f"`omp collab list --json` works ({len(hosts)} live session(s))"))
    except OmpError as exc:
        out.append(_f("collab", FAIL, f"`omp collab list --json` failed: {exc}"))
    return out


def _control(config: Config) -> Finding:
    directory = config.control_dir
    try:
        regs = ControlDir(directory).registries()
    except ControlError as exc:
        return _f("control", FAIL, str(exc), f"chmod 700 {directory}")
    if not directory.is_dir():
        return _f(
            "control",
            WARN,
            f"{directory} does not exist: Stop and Rename are off",
            "install and turn on omp-session-control (its README)",
        )
    return _f("control", OK, f"{len(regs)} session(s) with a control socket in {directory}")


def _relay(config: Config, app: HubApp, platform: Platform, uid: int) -> list[Finding]:
    if not config.get("relay.enabled"):
        return [_f("relay", OK, "no private relay configured: sessions are listed, and no link is handed out")]
    out: list[Finding] = []
    ports = (int(config.get("relay.guestPort")), int(config.get("relay.hostPort")))
    try:
        relay.check_ports(ports, config.relay_check_dir, platform, uid)
        out.append(_f("relay", OK, f"both relay listeners ({ports[0]}, {ports[1]}) prove to be your relay"))
    except DashboardError as exc:
        out.append(
            _f("relay", FAIL, str(exc), "start the relay (omp-private-relay run); check relay.checkDir and the ports")
        )
    web, relay_url = app.web_url(), app.relay_url()
    if web is None:
        out.append(_f("relay-web", WARN, "the relay's web address is not known yet (Tailscale?)"))
        return out
    # omp's own settings must point at the same relay, or sessions keep using the public one.
    for key, want in (("collab.relayUrl", relay_url), ("collab.webUrl", web)):
        binary = find_tool("omp", config.get("omp.bin"), app.env, "OMP_BIN")
        if binary is None:
            break
        result = run_bounded(
            [binary, "config", "get", key], 10.0, env={k: v for k, v in app.env.items() if k != "BUN_BE_BUN"}
        )
        got = result.stdout.strip() if result.ok else ""
        if got.rstrip("/").lower() != str(want).rstrip("/").lower():
            out.append(
                _f(
                    f"omp-{key.split('.')[1]}",
                    WARN,
                    f"omp's {key} is not {want}: sessions will not use this relay",
                    f"omp config set {key} {want}",
                )
            )
        else:
            out.append(_f(f"omp-{key.split('.')[1]}", OK, f"omp's {key} points at the relay"))
    return out


def _start(config: Config, app: HubApp, env: Mapping[str, str]) -> list[Finding]:
    backend = config.get("start.backend")
    if backend == "off":
        return [_f("start", OK, "Start is off (start.backend); sessions are opened from a terminal")]
    out: list[Finding] = []
    tool = find_tool(
        backend, config.get(f"start.{backend}.bin"), dict(env), "HERDR_BIN" if backend == "herdr" else None
    )
    if tool is None:
        out.append(_f("start", FAIL, f"{backend} was not found", f"install {backend} or set start.{backend}.bin"))
    else:
        out.append(_f("start", OK, f"Start opens sessions with {backend}"))
    if not config.get("start.roots"):
        out.append(
            _f(
                "start-roots",
                WARN,
                "start.roots is empty: nothing can be started",
                "list the folders Start may use in start.roots",
            )
        )
    else:
        out.append(
            _f(
                "start-roots",
                OK,
                f"{len(app.projects())} project(s) offered from {len(config.get('start.roots'))} root(s)",
            )
        )
    return out


def _alerts(config: Config) -> Finding:
    try:
        topic = read_topic(config.ntfy_topic_file)
    except DashboardError as exc:
        return _f(
            "alerts",
            FAIL,
            one_line(str(exc), 250),
            f"chmod 600 {config.ntfy_topic_file}, or run omp-dashboard config set-ntfy-topic",
        )
    if topic is None:
        return _f("alerts", OK, "alerts are off (no ntfy topic set)")
    return _f(
        "alerts",
        OK,
        f"alerts are on: messages go to {config.get('alerts.server')} (a third party) and name the project folder and model",
    )


def _dropins(config: Config, env: Mapping[str, str]) -> list[Finding]:
    out: list[Finding] = []
    wanted = {"services": services_dropin(config, env), "health": health_dropin(config, env)}
    for kind, path in dropin_paths(config).items():
        try:
            have = read_text(path, limit=64 * 1024)
        except FileNotFoundError:
            out.append(
                _f(
                    f"dropin-{kind}",
                    OK,
                    f"no {kind} drop-in at {path} (omp-dashboard dropins --write makes it, for omp-services)",
                )
            )
            continue
        except DashboardError as exc:
            out.append(_f(f"dropin-{kind}", FAIL, one_line(str(exc), 250)))
            continue
        if have != dropin_text(wanted[kind]):
            out.append(
                _f(
                    f"dropin-{kind}",
                    WARN,
                    f"the {kind} drop-in at {path} is not what this configuration would write",
                    "omp-dashboard dropins --write, then omp-services init",
                )
            )
        else:
            out.append(_f(f"dropin-{kind}", OK, f"the {kind} drop-in matches this configuration"))
    stray = stray_overrides(env)
    if stray:
        out.append(
            _f(
                "env-overrides",
                WARN,
                f"{len(stray)} OMP_DASHBOARD_* variable(s) are set in this shell; a service started by omp-services does not see them",
                "put them in config.json",
            )
        )
    return out


def render(findings: list[Finding]) -> str:
    marks = {OK: "ok  ", WARN: "warn", FAIL: "FAIL"}
    lines = []
    for f in findings:
        lines.append(f"[{marks[f.level]}] {f.id}: {f.message}")
        if f.fix and f.level != OK:
            lines.append(f"       fix: {f.fix}")
    failed = sum(f.level == FAIL for f in findings)
    warned = sum(f.level == WARN for f in findings)
    lines.append("")
    lines.append(f"{failed} failing, {warned} warning(s), {len(findings) - failed - warned} ok")
    return "\n".join(lines) + "\n"
