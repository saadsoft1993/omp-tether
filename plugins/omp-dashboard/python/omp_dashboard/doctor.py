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


def serve_funnel(text: str, port: int) -> bool:
    """Whether `tailscale serve status --json` says Funnel (the public internet) is on for any route on `port`, whatever the route points at."""
    try:
        status = json.loads(text)
    except ValueError:
        return False
    funnel = status.get("AllowFunnel") if isinstance(status, dict) else None
    return isinstance(funnel, dict) and any(v is True and str(k).endswith(f":{port}") for k, v in funnel.items())


def serve_verdict(config: Config, binary: str) -> tuple[str, str | bool | None, bool, str] | None:
    """(state, detail, funnel, the route's target) from `tailscale serve status --json`, or None when it did not answer. See parse_serve_status."""
    result = run_bounded([binary, "serve", "status", "--json"], 10.0, env=safe_env(), max_output=1024 * 1024)
    if not result.ok:
        return None
    port, host = int(config.get("listen.port")), config.get("listen.host")
    target = f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"
    state, detail = parse_serve_status(result.stdout, config.serve_port, target)
    return state, detail, serve_funnel(result.stdout, config.serve_port), target


def serve_integrity(config: Config, env: Mapping[str, str]) -> tuple[bool, str]:
    """(ok, what) for the route's integrity alone: false only for Funnel on, or a route that points elsewhere. Tailscale being off, the
    tool missing or an absent route are availability, not integrity, and are ok here (`doctor` reports them as warnings)."""
    configured = config.get("auth.tailscale.bin")
    binary = find_tool("tailscale", configured, dict(env)) or (
        TAILSCALE_APP if not configured and os.access(TAILSCALE_APP, os.X_OK) else None
    )
    if binary is None:
        return True, "the tailscale command was not found: the route was not checked"
    verdict = serve_verdict(config, binary)
    if verdict is None:
        return True, "tailscale serve status did not answer: the route was not checked"
    state, detail, funnel, _ = verdict
    if funnel:
        return False, f"Funnel is switched on for port {config.serve_port}"
    if state == "elsewhere":
        return False, f"Serve port {config.serve_port} forwards to {one_line(str(detail), 80)}, not to this hub"
    return True, f"route state: {state}"


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


def _probe_local_access(host: str, port: int) -> int | str:
    """The status a direct, unpaired request for the sign-in's own gate gets (`GET /api/hub/about`: 401 once the sign-in has let it in), or why not."""
    conn = http.client.HTTPConnection(host, port, timeout=2)
    deadline = Deadline(2)
    try:
        connect_bounded(conn, host, port, deadline)
        conn.request("GET", "/api/hub/about", headers={"Host": f"{'[' + host + ']' if ':' in host else host}:{port}"})
        resp = conn.getresponse()
        resp.read(1024)
        deadline.check()
        return resp.status
    except (OSError, http.client.HTTPException):
        return "no answer"
    finally:
        deadline.cancel()
        conn.close()


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
    if adapter == "both":
        out.append(
            _f(
                "auth",
                OK,
                "sign-in: both. This machine's own address (loopback Host, the pairing cookie) and Tailscale Serve (the owner's login, "
                "this machine's name and the cookie) are judged by their own rules",
            )
        )
        # /healthz is liveness only and ignores the sign-in; ask a path the sign-in guards, with no cookie: 401 means it let a direct request in.
        status = _probe_local_access(host, port) if answer is None else None
        if answer is not None:
            out.append(
                _f(
                    "access-local",
                    WARN,
                    f"local access is not available: no hub answers on {host}:{port}",
                    "start it: omp-dashboard run (or omp-services, see the README)",
                )
            )
        elif status == 401:
            out.append(_f("access-local", OK, f"local access works: http://{host}:{port}/ (no Tailscale needed)"))
        else:
            out.append(
                _f(
                    "access-local",
                    FAIL,
                    f"the hub on {host}:{port} is alive but refuses a direct local request (answered {status}): it is running with another sign-in than the configuration says",
                    "restart the hub so it reads auth.adapter: both",
                )
            )
    if adapter in ("tailscale-serve", "both"):
        out.extend(_tailscale(config, env, platform, soft=adapter == "both"))
    elif adapter == "loopback-only":
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
        message = f"{len(view.hashes)} paired browser(s) in {config.pairing_dir}"
        if config.get("auth.adapter") == "both":
            ways = view.access or {}
            local = sum(1 for h in view.hashes if ways.get(h) == "local")
            tailnet = sum(1 for h in view.hashes if ways.get(h) == "tailnet")
            unknown = len(view.hashes) - local - tailnet
            message += f" ({local} local, {tailnet} tailnet, {unknown} without a recorded way in)"
            if unknown:
                out.append(
                    _f(
                        "pairing",
                        WARN,
                        message
                        + ": with sign-in both a browser is let in only the way it was paired, so those are refused on both",
                        "pair them again: omp-dashboard pair (local) or omp-dashboard pair --tailnet; `omp-dashboard devices` lists them",
                    )
                )
                message = ""
        if message:
            out.append(_f("pairing", OK, message))
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


def _tailscale(config: Config, env: Mapping[str, str], platform: Platform, soft: bool = False) -> list[Finding]:
    """The tailnet side. With `soft` (sign-in `both`) a problem with it is a warning, because the local address works without it
    and a doctor that fails for Tailscale being off would call a working hub broken; Funnel being on stays a failure."""
    bad = WARN if soft else FAIL
    gone = "the tailnet path answers 503" if soft else "every request gets 503"
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
                    bad,
                    f"the tailscale command was not found (looked at {looked}), so this machine's owner and name are not known: {gone}",
                    "set auth.tailscale.bin in the config file (or OMP_DASHBOARD_TAILSCALE_BIN) to the absolute path of the tailscale command, "
                    "for example /Applications/Tailscale.app/Contents/MacOS/Tailscale; or set auth.tailscale.owner and auth.tailscale.publicHost",
                )
            )
            if soft:
                out.append(
                    _f("serve-route", WARN, "the Serve route was not checked: the tailscale command was not found")
                )
            return out
        out.append(
            _f(
                "tailscale",
                bad,
                f"this machine's Tailscale owner and name are not known (is Tailscale running?): {gone}"
                + ("; the local address is not affected" if soft else ""),
                "start Tailscale, or set auth.tailscale.owner and auth.tailscale.publicHost",
            )
        )
        # The identity is unknown, not the Serve configuration: a route that points elsewhere or Funnel being on can be seen without it.
        if binary is not None:
            out.extend(_serve_findings(config, binary))
        elif soft:
            out.append(_f("serve-route", WARN, "the Serve route was not checked: the tailscale command was not found"))
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
    out.extend(_serve_findings(config, binary))
    return out


def _serve_findings(config: Config, binary: str) -> list[Finding]:
    """The Serve route and Funnel for the hub's port, from `tailscale serve status`. A route that points elsewhere and Funnel being on
    are failures whatever else is true (sign-in, whether the identity is known, whether the Web route exists); only an absent route,
    or a Tailscale that does not answer, is a warning."""
    verdict = serve_verdict(config, binary)
    if verdict is None:
        return [_f("serve-route", WARN, "`tailscale serve status` did not answer", None)]
    out: list[Finding] = []
    state, detail, funnel, target = verdict
    funnel_message = (
        f"Funnel is switched on for port {config.serve_port}: the hub is open to the internet's requests (they are refused, but they should not arrive)",
        f"tailscale funnel --https={config.serve_port} off",
    )
    if state == "ok" and funnel:
        out.append(_f("serve-route", FAIL, *funnel_message))
    elif state == "ok":
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
    if funnel and state != "ok":  # Funnel is on for the port whether or not the Web route there is ours, or exists
        out.append(_f("serve-funnel", FAIL, *funnel_message))
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
    addresses = app.web_addresses()
    relay_url = app.relay_url()
    if not addresses:
        out.append(_f("relay-web", WARN, "the relay's web address is not known yet (Tailscale?)"))
        return out
    admits = app.relay_admits()
    if admits:
        out.append(
            _f(
                "relay-admits",
                OK,
                f"relay.admits is {admits}: Open and Watch are offered to browsers "
                + (
                    "on this machine's own address only (the relay's auth must be token)"
                    if admits == "local"
                    else "through Tailscale Serve only (the relay's auth must be tailscale-serve)"
                )
                + "; the other way is told why there is no link",
            )
        )
    # omp's own settings must point at the same relay, or sessions keep using the public one. With `both` and a relay for the
    # local way, omp's web address may be any of this machine's loopback names.
    for key, choices in (("collab.relayUrl", (relay_url,)), ("collab.webUrl", addresses)):
        binary = find_tool("omp", config.get("omp.bin"), app.env, "OMP_BIN")
        if binary is None:
            break
        result = run_bounded(
            [binary, "config", "get", key], 10.0, env={k: v for k, v in app.env.items() if k != "BUN_BE_BUN"}
        )
        got = result.stdout.strip() if result.ok else ""
        if not any(got.rstrip("/").lower() == str(want).rstrip("/").lower() for want in choices):
            want = choices[0]
            out.append(
                _f(
                    f"omp-{key.split('.')[1]}",
                    WARN,
                    f"omp's {key} is not {' or '.join(str(c) for c in choices)}: sessions will not use this relay",
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
