"""The hub: one local HTTP server that serves the dashboard page and its own API under /api/hub/.

It listens on one loopback address only. Every request first passes the sign-in adapter (auth.py: loopback-only or
tailscale-serve); every API call except pairing itself then also needs a paired browser's cookie (pairing.py). POSTs
need a custom header (which forces a CORS preflight that this server never approves) and a matching Origin.

Everything that reads from a client is bounded before anything else is decided: the header block (bytes and time), the
body (bytes and time), the number of connections. Answers are fixed JSON texts or the fixed page; nothing a client sent
is ever reflected in a header or in an error text.
"""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar
from urllib.parse import unquote

from . import __version__, hostplat, relay
from .alerts import Alerts
from .audit import audit
from .auth import Denied, build_adapter, host_header, proxy_sign
from .config import Config
from .control import ControlDir, ControlError, control, has_control
from .hostplat import Platform
from .kill import Killer
from .omp import (
    SESSION_ID,
    Omp,
    OmpError,
    PublicRelayError,
    RelayClassifier,
    find_tool,
    machine_name,
    session_link,
)
from .pairing import PairingStore, cookie_hash
from .projects import discover_projects
from .start import StartError, build_backend
from .ui import ROUTES, UiBundleError, load_bundle
from .util import DashboardError

MAX_BODY = 4096
BODY_DEADLINE_S = 10.0  # a whole request body, however slowly it trickles in
HEADER_BYTES = 16 * 1024  # the request line and every header together
HEADER_DEADLINE_S = 10.0  # the whole header block, however slowly it trickles in
MAX_THREADS = 64  # connections served at once; more are closed at once
PLACES_TTL_S = 5.0
MACHINE_TTL_S = 60.0
PAIR_FAILURES = 10  # failed pairing attempts per PAIR_WINDOW_S, all clients together
PAIR_WINDOW_S = 300.0
SESSION_ACTION = re.compile(r"/api/hub/sessions/([^/]+)/(stop|rename|kill|force-kill)\Z")
# Characters a request target may hold: what browsers send unescaped, minus `#`. Anything else is refused.
TARGET_CHARS = re.compile(r"[A-Za-z0-9\-._~!$&'()*+,;=:@/%?]*\Z")
FIXED_ERRORS = {
    400: "bad request",
    403: "forbidden",
    404: "not found",
    405: "method not allowed",
    408: "request timeout",
    413: "request too large",
    414: "request too long",
    415: "send application/json",
    429: "too many attempts",
    431: "request headers too large",
    500: "internal error; see the hub's error log",
    501: "not supported",
    503: "unavailable",
}
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; style-src-attr 'unsafe-inline'; img-src 'self' data:; "
    "font-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; "
    "form-action 'none'"
)
UI_UNAVAILABLE = "the dashboard page is missing or damaged; run `omp-dashboard doctor`"
LOOPBACK_NAMES = ("127.0.0.1", "localhost")  # the names a browser may open this machine's relay at, with sign-in both
RELAY_OTHER_PATH = {
    "local": "The relay admits only browsers on this machine's own address (its auth is token), so Open and Watch are offered there only. "
    "Open the dashboard at http://127.0.0.1:<port>/ on this machine, or run the relay with auth tailscale-serve and set relay.admits to tailnet.",
    "tailnet": "The relay admits only browsers that come through Tailscale Serve (its auth is tailscale-serve), so Open and Watch are offered there only. "
    "Open the dashboard through Serve, or run the relay with auth token and set relay.admits to local.",
}
RELAY_BAD_ADDRESS = (
    "Open and Watch are offered only at 127.0.0.1 and localhost on the hub's own port: at another name or port the relay's address "
    "would be the browser machine's, not the hub machine's. A forward that keeps the hub's port number cannot be told apart from a "
    "direct request, so there the link is offered and may name the wrong machine: forward the relay's guest port too, or do not use Open and Watch."
)


def clean_target(target: str) -> tuple[str, str] | None:
    """(decoded path to route on, the target) or None when the target is refused.

    Refused: absolute-form targets, characters outside TARGET_CHARS (`#`, spaces, controls, non-ASCII), and after one
    decode backslashes, control characters and `.`/`..` segments, so that a path is routed the way a browser reads it.
    """
    if not target.startswith("/") or target.startswith("//") or not TARGET_CHARS.match(target):
        return None
    raw_path = target.partition("?")[0]
    path = unquote(raw_path)
    if "\\" in path or any(ord(c) < 0x21 or ord(c) == 0x7F for c in path):
        return None
    if any(seg in (".", "..") for seg in path.split("/")):
        return None
    return path, target


class FailureBudget:
    """At most `limit` failures per `window` seconds, all clients together. A slot is reserved BEFORE the work and given
    back when the work succeeds, so simultaneous attempts cannot exceed the limit."""

    def __init__(self, limit: int, window: float, clock: Any = time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self.stamps: list[float] = []
        self.lock = threading.Lock()

    def reserve(self) -> float | None:
        """A token for the reserved slot, or None when the budget is spent."""
        with self.lock:
            now = self.clock()
            self.stamps = [t for t in self.stamps if now - t < self.window]
            if len(self.stamps) >= self.limit:
                return None
            self.stamps.append(now)
            return now

    def release(self, token: float) -> None:
        with self.lock:
            if token in self.stamps:
                self.stamps.remove(token)


class HeaderBudgetExceeded(Exception):
    pass


class BudgetReader:
    """The request stream, with a byte budget for the header block. Over budget it RAISES: a reader that returned EOF
    would let a truncated header block parse as a complete one."""

    def __init__(self, raw: Any):
        self.raw = raw
        self.budget: int | None = HEADER_BYTES

    def _take(self, data: bytes) -> bytes:
        if self.budget is not None:
            self.budget -= len(data)
            if self.budget < 0:
                raise HeaderBudgetExceeded
        return data

    def readline(self, size: int = -1) -> bytes:
        limit = size if self.budget is None else (self.budget + 1 if size < 0 else min(size, self.budget + 1))
        return self._take(self.raw.readline(limit))

    def read(self, size: int = -1) -> bytes:
        return self._take(self.raw.read(size if self.budget is None or size >= 0 else self.budget + 1))

    def read1(self, size: int = -1) -> bytes:
        return self._take(self.raw.read1(size))

    def __getattr__(self, name: str) -> Any:
        return getattr(self.raw, name)


class HubApp:
    """Everything the handlers share, built once from the configuration."""

    def __init__(self, config: Config, env: dict[str, str] | None = None, platform: Platform | None = None):
        self.config = config
        self.env = dict(os.environ if env is None else env)
        self.platform = platform or Platform()
        self.adapter = build_adapter(config, self.env)
        self.pairing = PairingStore(
            config.pairing_dir,
            config.get("pairing.cookieName"),
            on_refusal=self._refusal,
            max_age_days=config.get("pairing.maxAgeDays"),
            local_max_age_days=config.get("pairing.localMaxAgeDays") if self.adapter.name == "both" else None,
        )
        self.omp = Omp(lambda: find_tool("omp", config.get("omp.bin"), self.env, "OMP_BIN"), self.env)
        self.control = ControlDir(config.control_dir)
        self.killer = Killer(self.omp.list_hosts, self.platform)
        self.backend = build_backend(config, self.platform, self.env)
        self.classifier = RelayClassifier(self.omp)
        self.alerts = Alerts(
            config.get("alerts.server"),
            config.ntfy_topic_file,
            config.get("alerts.minTurnSeconds"),
            self.omp.list_hosts,
            self.sessions_url,
        )
        self.start_lock = threading.Lock()
        self.pair_budget = FailureBudget(PAIR_FAILURES, PAIR_WINDOW_S)
        self._places = (0.0, {})
        self._machine = (0.0, "")
        self._cache_lock = threading.Lock()
        self.stop = threading.Event()
        try:
            self.bundle = load_bundle()
            self.bundle_error: str | None = None
        except UiBundleError as exc:
            self.bundle = None
            self.bundle_error = str(exc)

    # --- derived values ---------------------------------------------------------------------------------------------

    def _refusal(self, reason: str) -> None:
        if reason == "store-missing":  # normal until the first browser is paired
            return
        print(
            f"omp-dashboard: the pairing store is refused ({reason}); nobody is paired until that is fixed",
            file=sys.stderr,
            flush=True,
        )

    @property
    def relay_enabled(self) -> bool:
        return bool(self.config.get("relay.enabled"))

    def relay_url(self) -> str | None:
        """The relay's host listener as omp sessions use it (collab.relayUrl), or None when the relay is off."""
        return f"ws://127.0.0.1:{self.config.get('relay.hostPort')}" if self.relay_enabled else None

    def relay_admits(self) -> str | None:
        """With `both`: "local" or "tailnet", the one way the running relay admits browsers (relay.admits); else None."""
        return str(self.config.get("relay.admits")) if self.adapter.name == "both" else None

    def web_url(self) -> str | None:
        """The relay's web client as a browser reaches it (collab.webUrl), or None while it is not known.

        With `both` it is the address of the one way the relay admits: this machine's own address, or the Serve one."""
        if not self.relay_enabled:
            return None
        guest = int(self.config.get("relay.guestPort"))
        if self.relay_admits() == "local":
            return f"http://127.0.0.1:{guest}"
        configured = self.config.get("relay.webUrl")
        if configured:
            return str(configured)
        if self.adapter.name in ("tailscale-serve", "both"):
            return self.adapter.web_origin(int(self.config.get("relay.servePort") or guest))
        return f"http://127.0.0.1:{guest}"

    def web_addresses(self) -> tuple[str, ...]:
        """Every web address a link of ours may carry: one, or with `both` and a relay for the local way, the loopback names of this machine."""
        if self.relay_admits() == "local" and self.relay_enabled:
            guest = int(self.config.get("relay.guestPort"))
            return tuple(f"http://{name}:{guest}" for name in LOOPBACK_NAMES)
        url = self.web_url()
        return (url,) if url else ()

    def local_open_address(self, host_header_value: str | None) -> str | None:
        """Where a browser that reached the hub at this `Host` opens the relay: the same host name, at the relay's guest port. Only for
        127.0.0.1 and localhost at this hub's own port: another name (`[::1]`, an allowedHosts entry for a forward) is not one this
        relay is known to listen on, and the address would be the browser machine's, not this one's. None then."""
        name, sep, port = (host_header_value or "").strip().lower().rpartition(":")
        if not sep or name not in LOOPBACK_NAMES or port != str(self.config.get("listen.port")):
            return None
        return f"http://{name}:{int(self.config.get('relay.guestPort'))}"

    def sessions_url(self) -> str | None:
        """Where an alert points: the dashboard's Sessions page, only when a phone could reach it."""
        url = self.adapter.tailnet_url()
        return f"{url}#/sessions" if url else None

    def machine(self) -> str:
        with self._cache_lock:
            at, name = self._machine
            if time.monotonic() - at < MACHINE_TTL_S and name:
                return name
        name = machine_name(self.config.get("machine.name"), self.omp)
        with self._cache_lock:
            self._machine = (time.monotonic(), name)
        return name

    def places(self, hosts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        if self.backend is None:
            return {}
        with self._cache_lock:
            at, value = self._places
            if time.monotonic() - at < PLACES_TTL_S:
                return value
        value = self.backend.locations(hosts)
        with self._cache_lock:
            self._places = (time.monotonic(), value)
        return value

    def projects(self) -> list[dict[str, Any]]:
        if self.backend is None:
            return []
        sessions = self.config.sessions_dir if self.config.get("projects.rankBySessions") else None
        return discover_projects(
            self.config.get("start.roots"), self.paths_home, int(self.config.get("start.maxDepth")), sessions
        )

    @property
    def paths_home(self):
        return self.config.paths.home

    def audit(self, action: str, target: str | None, outcome: str, browser: str | None) -> None:
        """Write the audit line; a failure to write it is logged and never changes the answer."""
        try:
            audit(
                self.config.audit_file,
                action,
                target,
                outcome,
                browser,
                max_bytes=int(self.config.get("audit.maxBytes")),
            )
        except (OSError, DashboardError):
            print("omp-dashboard: the audit line could not be written", file=sys.stderr, flush=True)


class HubServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with at most MAX_THREADS connections in flight; the rest are closed at once."""

    daemon_threads = True
    # SO_REUSEADDR, so a restart does not wait out TIME_WAIT; a second hub on a port in use must still fail to bind.
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], app: HubApp, max_threads: int = MAX_THREADS):
        self.app = app
        self.address_family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
        self._slots = threading.BoundedSemaphore(max_threads)
        super().__init__(address, Handler)

    def handle_error(self, request: Any, client_address: Any) -> None:
        """A browser that leaves mid-answer is normal and silent; anything else is logged (the traceback only)."""
        if isinstance(sys.exc_info()[1], BrokenPipeError | ConnectionResetError | TimeoutError):
            return
        traceback.print_exc()

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def _cut(connection: socket.socket) -> None:
    try:
        connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


class Handler(BaseHTTPRequestHandler):
    timeout = 15  # an idle client must not pin a thread
    server: HubServer  # type: ignore[assignment]
    server_version = "omp-dashboard"
    sys_version = ""
    command: str | None = None
    cache_control = "no-store"
    SECURITY_HEADERS: ClassVar[dict[str, str]] = {
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": CSP,
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    }

    @property
    def app(self) -> HubApp:
        return self.server.app

    # --- framing ----------------------------------------------------------------------------------------------------

    def setup(self) -> None:
        super().setup()
        self.rfile = BudgetReader(self.rfile)  # type: ignore[assignment]

    def handle_one_request(self) -> None:
        timer = threading.Timer(HEADER_DEADLINE_S, _cut, (self.connection,))
        timer.daemon = True
        self._header_timer = timer
        timer.start()
        try:
            super().handle_one_request()
        except HeaderBudgetExceeded:
            self.close_connection = True
            self.command, self.request_version, self.requestline = "", "HTTP/1.0", ""  # parse_request never ran
            self.reply(431, {"error": FIXED_ERRORS[431]})
        finally:
            timer.cancel()

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """Fixed texts only: the default error page reflects parts of the request."""
        self.close_connection = True
        self.reply(code, {"error": FIXED_ERRORS.get(code, "error")})

    def end_headers(self) -> None:
        self.send_header("Cache-Control", self.cache_control)
        for key, value in self.SECURITY_HEADERS.items():
            self.send_header(key, value)
        super().end_headers()

    def log_message(self, *args: Any) -> None:
        pass

    def reply(self, code: int, obj: Any, extra_headers: dict[str, str] | None = None) -> None:
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def do_GET(self) -> None:
        self.safely(self.route)

    do_HEAD = do_GET

    def do_POST(self) -> None:
        self.safely(self.route)

    def _not_allowed(self) -> None:
        self.reply(405, {"error": FIXED_ERRORS[405]})

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _not_allowed

    def safely(self, handler: Any) -> None:
        try:
            handler()
        except (BrokenPipeError, ConnectionResetError):
            pass  # the browser left (navigated away mid-request): nothing to answer
        except (ValueError, RecursionError):
            self.reply(400, {"error": FIXED_ERRORS[400]})
        except Exception:  # details go to the log only
            traceback.print_exc()
            self.reply(500, {"error": FIXED_ERRORS[500]})

    # --- gates ------------------------------------------------------------------------------------------------------

    def paired_device(self) -> str | None:
        """The paired browser behind this request's cookie, or None. With `both` the device must have been paired the way
        this request came (local or through Serve): a cookie of the other way is not a paired browser here."""
        access = self.app.adapter.access(self.headers) if self.app.adapter.name == "both" else None
        return self.app.pairing.paired_device(self.headers.get("Cookie"), access)

    def same_origin_post(self) -> str | None:
        """None when this POST came from the dashboard page itself, else why not.

        A custom header forces a CORS preflight, which this server never approves: no cross-site posts. And Origin
        must be this hub's own origin, which every fetch POST from the page carries."""
        if self.headers.get("X-Omp-Dashboard") != "1":
            return "missing X-Omp-Dashboard header"
        if self.headers.get("Origin") not in self.app.adapter.origins(self.headers):
            return "wrong Origin"
        return None

    def route(self) -> None:
        self.rfile.budget = None  # the header block is complete; the body is bounded by read_body
        self._header_timer.cancel()
        cleaned = clean_target(self.path)
        if cleaned is None:
            return self.reply(400, {"error": "bad path"})
        path, _ = cleaned
        if path == "/healthz":
            return self.healthz()
        denied: Denied | None = self.app.adapter.check(self.headers)
        if denied is not None:
            return self.reply(denied.status, {"error": denied.message})
        if path.startswith("/api/hub/"):
            return self.hub_post(path) if self.command == "POST" else self.hub_get(path)
        if path == "/api" or path.startswith("/api/"):
            return self.reply(404, {"error": FIXED_ERRORS[404]})
        # The page holds no data: the owner gets it unpaired, so the Pair screen renders.
        if self.command == "POST":
            return self.reply(405, {"error": "GET only"})
        return self.static(path)

    def healthz(self) -> None:
        """Liveness for a heartbeat on this machine: only a direct loopback client with this hub's own port gets it."""
        port = self.app.config.get("listen.port")
        direct = host_header(self.headers) in {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
        proxied = proxy_sign(self.headers) is not None or self.headers.get("Origin") is not None
        if self.command == "POST" or not direct or proxied:
            return self.reply(403, {"error": FIXED_ERRORS[403]})
        return self.reply(200, {"ok": True, "updatedAt": int(time.time())})

    # --- the page ---------------------------------------------------------------------------------------------------

    def static(self, path: str) -> None:
        name = ROUTES.get(path)
        if name is None:
            return self.reply(404, {"error": FIXED_ERRORS[404]})
        if self.app.bundle is None:
            return self.reply(503, {"error": UI_UNAVAILABLE})
        asset = self.app.bundle.assets[name]
        self.cache_control = "no-cache"  # revalidate (ETag), never serve a stale build
        if self.headers.get("If-None-Match") == asset.etag:
            self.send_response(304)
            self.send_header("ETag", asset.etag)
            self.end_headers()
            return None
        self.send_response(200)
        self.send_header("Content-Type", asset.content_type)
        self.send_header("Content-Length", str(len(asset.data)))
        self.send_header("ETag", asset.etag)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(asset.data)
        return None

    # --- API: reads -------------------------------------------------------------------------------------------------

    def hub_get(self, path: str) -> None:
        if self.paired_device() is None:
            return self.reply(401, {"error": "not paired", "pair": "omp-dashboard pair"})
        app = self.app
        if path == "/api/hub/sessions":
            return self.sessions()
        if path == "/api/hub/machine":
            return self.reply(200, {"name": app.machine(), "noun": hostplat.noun(app.platform.kind)})
        if path == "/api/hub/about":
            return self.reply(
                200,
                {
                    "version": __version__,
                    "adapter": app.adapter.name,
                    "start": app.backend.name if app.backend else "off",
                    "relay": app.relay_enabled,
                    "statsUrl": app.config.get("stats.url"),
                },
            )
        if path == "/api/hub/relay":
            if not app.relay_enabled:
                return self.reply(200, {"enabled": False, "ok": False, "error": "no private relay is configured"})
            return self.reply(
                200,
                {
                    "enabled": True,
                    **relay.health(int(app.config.get("relay.hostPort")), app.config.relay_check_dir, app.platform),
                },
            )
        if path == "/api/hub/projects":
            return self.reply(200, {"backend": app.backend.name if app.backend else "off", "projects": app.projects()})
        if path == "/api/hub/alerts":
            return self.reply(200, app.alerts.status())
        return self.reply(404, {"error": FIXED_ERRORS[404]})

    def sessions(self) -> None:
        app = self.app
        try:
            hosts = app.omp.list_hosts()
        except OmpError as exc:
            return self.reply(503, {"error": f"could not list sessions: {exc}"})
        control_problem: str | None = None
        try:
            regs = app.control.registries()
        except ControlError as exc:
            regs, control_problem = [], str(exc)
        places = app.places(hosts)
        app.classifier.annotate(hosts, app.web_addresses(), app.relay_url(), app.relay_enabled)
        for h in hosts:
            h["control"] = has_control(regs, h.get("sessionId"), h.get("pid"))
            h["place"] = places.get(h.get("sessionId"))
        return self.reply(200, {"sessions": hosts, "now": int(time.time() * 1000), "controlProblem": control_problem})

    # --- API: writes ------------------------------------------------------------------------------------------------

    def read_body(self) -> bytes | None:
        """The request body (at most MAX_BODY bytes, within BODY_DEADLINE_S), or None after replying."""
        if self.headers.get("Transfer-Encoding"):
            self.reply(400, {"error": "send a Content-Length body"})
            return None
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.reply(400, {"error": "bad Content-Length"})
            return None
        if length < 0 or length > MAX_BODY:
            self.reply(400 if length < 0 else 413, {"error": f"body must be 0-{MAX_BODY} bytes"})
            return None
        deadline = time.monotonic() + BODY_DEADLINE_S
        chunks: list[bytes] = []
        remaining = length
        try:
            while remaining:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError
                self.connection.settimeout(left)
                chunk = self.rfile.read1(remaining)
                if not chunk:
                    self.reply(400, {"error": "body ended early"})
                    return None
                chunks.append(chunk)
                remaining -= len(chunk)
        except TimeoutError:
            self.reply(408, {"error": FIXED_ERRORS[408]})
            return None
        finally:
            self.connection.settimeout(self.timeout)
        return b"".join(chunks)

    def hub_post(self, path: str) -> None:
        app = self.app
        problem = self.same_origin_post()
        if problem:
            return self.reply(403, {"error": problem})
        content_type = (self.headers.get("Content-Type") or "").partition(";")[0].strip().lower()
        if content_type != "application/json":  # the page always says so; a form or text post is not the page
            self.close_connection = True
            return self.reply(415, {"error": FIXED_ERRORS[415]})
        if path == "/api/hub/pair":
            return self.pair()
        browser = self.paired_device()
        if browser is None:
            return self.reply(401, {"error": "not paired"})
        raw = self.read_body()
        if raw is None:
            return None
        try:
            body = json.loads(raw or b"{}")
        except (ValueError, RecursionError):
            return self.reply(400, {"error": "body must be JSON"})
        if not isinstance(body, dict):
            return self.reply(400, {"error": "body must be a JSON object"})
        if path == "/api/hub/link":
            return self.link(body, browser)
        if path == "/api/hub/start":
            return self.start(body, browser)
        if path == "/api/hub/alerts/test":
            try:
                app.alerts.send_test()
            except DashboardError as exc:
                app.audit("alerts-test", None, "refused:no_topic", browser)
                return self.reply(400, {"error": str(exc)})
            except OSError as exc:
                app.audit("alerts-test", None, f"failed:{type(exc).__name__}", browser)
                return self.reply(502, {"error": f"the ntfy server did not take the message ({type(exc).__name__})"})
            app.audit("alerts-test", None, "sent", browser)
            return self.reply(200, {"ok": True})
        action = SESSION_ACTION.match(path)
        if action:
            return self.session_action(action.group(1), action.group(2), body, browser)
        return self.reply(404, {"error": FIXED_ERRORS[404]})

    def pair(self) -> None:
        app = self.app
        raw = self.read_body()
        if raw is None:
            return None
        try:
            body = json.loads(raw or b"{}")
        except (ValueError, RecursionError):
            app.audit("pair", None, "refused:malformed", None)
            return self.reply(400, {"error": "body must be JSON"})
        if not isinstance(body, dict):
            app.audit("pair", None, "refused:malformed", None)
            return self.reply(400, {"error": "body must be a JSON object"})
        token = body.get("token")
        # A token that is on file, unused and unexpired cannot be a guess (256 random bits), so it does not need a failure slot:
        # ten wrong guesses by anyone on this machine (every local client is 127.0.0.1) must not stop the owner pairing.
        # Wrong guesses still spend slots, and a token that was used or has expired is a wrong guess.
        known = app.pairing.writer.token_valid(token)
        slot = None if known else app.pair_budget.reserve()
        if not known and slot is None:
            app.audit("pair", None, "rate_limited", None)
            return self.reply(429, {"error": "too many failed pairing attempts; try again in a few minutes"})
        access = app.adapter.access(self.headers)
        session = app.pairing.writer.redeem(token, self.headers.get("User-Agent"), access)
        if session is None:
            if known:
                app.pair_budget.reserve()  # lost a race with another redeem: that was a failure after all
            app.audit("pair", None, "refused", None)
            return self.reply(
                403, {"error": "the pairing link was already used or expired; run `omp-dashboard pair` again"}
            )
        if slot is not None:
            app.pair_budget.release(slot)
        secure = "; Secure" if app.adapter.secure_cookie(self.headers) else ""
        days = app.pairing.max_age_days_for(access if app.adapter.name == "both" else None)
        max_age = 31536000 if days is None else int(days) * 86400
        cookie = f"{app.config.get('pairing.cookieName')}={session}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Strict{secure}"
        app.audit("pair", None, "paired", cookie_hash(session)[:8])
        return self.reply(200, {"ok": True}, extra_headers={"Set-Cookie": cookie})

    def link(self, body: dict[str, Any], browser: str) -> None:
        app = self.app
        if "allowPublic" in body:  # there is no way to ask for a public-relay link
            app.audit("link", None, "refused:public_requested", browser)
            return self.reply(400, {"error": "public-relay links are never handed out"})
        target = body.get("instanceId") if isinstance(body.get("instanceId"), str) else None
        if not app.relay_enabled:
            app.audit("link", target, "refused:relay_off", browser)
            return self.reply(409, {"error": "no private relay is configured (relay.enabled)", "code": "relay_off"})
        addresses = app.web_addresses()
        relay_url = app.relay_url()
        open_at = None
        if app.adapter.name == "both":
            # The bundled relay admits one way (token auth: direct browsers; tailscale-serve auth: browsers through Serve), so a link is
            # offered only to browsers of that way, and says so to the others instead of handing out a link that would be refused.
            access, admits = app.adapter.access(self.headers), app.relay_admits()
            if access != admits:
                app.audit("link", target, "refused:relay_other_path", browser)
                return self.reply(409, {"error": RELAY_OTHER_PATH[admits or "local"], "code": "relay_other_path"})
            if access == "local":
                open_at = app.local_open_address(host_header(self.headers))
                if open_at is None:
                    app.audit("link", target, "refused:relay_address", browser)
                    return self.reply(409, {"error": RELAY_BAD_ADDRESS, "code": "relay_address"})
            else:
                open_at = app.web_url()
        if not addresses or not relay_url or (app.adapter.name == "both" and not open_at):
            app.audit("link", target, "refused:no_address", browser)
            return self.reply(503, {"error": "the relay's web address is not known yet (is Tailscale running?)"})
        instance, view = body.get("instanceId"), bool(body.get("view"))
        try:
            link = session_link(
                app.omp,
                instance if isinstance(instance, str) else "",
                view,
                addresses,
                relay_url,
                open_at=open_at,
            )
        except PublicRelayError as exc:
            app.audit("link", target, "refused:public_relay", browser)
            return self.reply(409, {"error": str(exc), "relay": "public"})
        except LookupError as exc:
            app.audit("link", target, "refused:unknown_session", browser)
            return self.reply(404, {"error": str(exc)})
        try:  # the proof comes last, as close to handing the link over as it can: a listener swapped while omp was asked fails it
            relay.check_ports(
                (int(app.config.get("relay.guestPort")), int(app.config.get("relay.hostPort"))),
                app.config.relay_check_dir,
                app.platform,
            )
        except DashboardError as exc:
            app.audit("link", target, "refused:relay_proof", browser)
            return self.reply(503, {"error": str(exc)})
        app.audit("link", target, "view" if view else "control", browser)
        return self.reply(200, link)

    def start(self, body: dict[str, Any], browser: str) -> None:
        app = self.app
        if app.backend is None:
            app.audit("start", None, "refused:start_off", browser)
            return self.reply(409, {"error": "Start is off (start.backend)", "code": "start_off"})
        if not app.start_lock.acquire(blocking=False):
            app.audit("start", None, "refused:busy", browser)
            return self.reply(429, {"error": "another session is starting"})
        try:
            path = body.get("path")
            if not isinstance(path, str) or path not in {p["path"] for p in app.projects()}:
                app.audit("start", None, "refused:unknown_project", browser)
                return self.reply(400, {"error": "unknown project"})
            try:
                started = app.backend.start(path, app.omp.list_hosts)
            except StartError as exc:
                app.audit("start", None, f"refused:{exc.status}", browser)
                return self.reply(exc.status, {"error": str(exc)})
            except Exception:  # unexpected: still one audit line and a JSON answer, details in the log
                traceback.print_exc()
                app.audit("start", None, "error", browser)
                return self.reply(500, {"error": "Start failed unexpectedly; see the hub's error log"})
            app.audit(
                "start",
                started.get("instanceId"),
                "started" if started.get("instanceId") else "no-session-seen",
                browser,
            )
            return self.reply(200, started)
        finally:
            app.start_lock.release()

    def session_action(self, session_id: str, action: str, body: dict[str, Any], browser: str) -> None:
        """Stop, Rename, Kill, Force kill; one audit line each, whatever the outcome."""
        app = self.app
        if not SESSION_ID.match(session_id):
            return self.reply(400, {"error": "bad session id"})
        try:
            result, outcome = self._run_session_action(session_id, action, body)
        except ControlError as exc:
            app.audit(action, session_id, f"refused:{exc.code}", browser)
            return self.reply(exc.status, {"error": str(exc), "code": exc.code})
        except Exception:  # unexpected: still one audit line and a JSON answer, details in the log
            traceback.print_exc()
            app.audit(action, session_id, "error", browser)
            return self.reply(500, {"error": f"{action} failed unexpectedly; see the hub's error log", "code": "error"})
        app.audit(action, session_id, outcome, browser)
        return self.reply(200, {**result, "outcome": outcome})

    def _run_session_action(self, session_id: str, action: str, body: dict[str, Any]) -> tuple[dict[str, Any], str]:
        app = self.app
        if action == "kill":
            result = app.killer.terminate(session_id, body.get("confirm"))
            return result, result["outcome"]
        if action == "force-kill":
            result = app.killer.force(session_id, body.get("confirm"))
            return result, f"force_{result['outcome']}"
        try:
            hosts = app.omp.list_hosts()
        except OmpError:
            raise ControlError(503, "could not list sessions (omp collab list failed)", "list") from None
        host = next((h for h in hosts if h.get("sessionId") == session_id), None)
        if host is None:
            raise ControlError(404, "that session is not running any more; refresh the list", "gone")
        if action == "stop":
            result = control(app.control, session_id, host.get("pid"), "abort")
            return result, "stopped" if result.get("aborted") is True else "was_idle"
        name = body.get("name")
        if not isinstance(name, str):
            raise ControlError(400, "send the new name as a string", "invalid_args")
        return control(app.control, session_id, host.get("pid"), "rename", {"name": name}), "renamed"


def serve(config: Config, env: dict[str, str] | None = None) -> int:
    """Run the hub until SIGINT or SIGTERM."""
    config.require_valid()
    app = HubApp(config, env)
    host, port = config.get("listen.host"), int(config.get("listen.port"))
    app.adapter.check_bind(host)
    try:
        server = HubServer((host, port), app)
    except OSError as exc:
        print(
            f"omp-dashboard: cannot listen on {host}:{port} ({exc.strerror}); is another hub running? Choose listen.port.",
            file=sys.stderr,
        )
        return 1
    if app.bundle_error:
        print(f"omp-dashboard: {app.bundle_error}; the page will not be served", file=sys.stderr, flush=True)
    thread = threading.Thread(target=app.alerts.run_forever, args=(app.stop,), daemon=True)
    thread.start()

    def shutdown(*_: Any) -> None:
        app.stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    shown = f"[{host}]" if ":" in host else host
    print(f"omp-dashboard {__version__} listening on http://{shown}:{port} (sign-in: {app.adapter.name})", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0
