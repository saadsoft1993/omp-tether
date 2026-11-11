"""Who may even ask: the owner-login adapters.

An adapter answers these questions for the hub, and the pairing cookie is checked after it, always:

    check_bind(host)       may the hub listen on this address? (loopback only, for every adapter)
    access(headers)        which way did this request come, "local" (direct, on loopback) or "tailnet" (through a proxy)?
    check(headers)         does this request come from the right place? None, or a Denied(status, message)
    origins(headers)       the Origin values a browser's POST by that way may carry
    secure_cookie(headers) whether the pairing cookie for a browser that came that way is marked Secure
    public_url()           where a browser opens the dashboard (pairing links), or None while unknown
    tailnet_url()          where a browser on the tailnet opens it (alert clicks), or None when there is no such way or it is unknown

`loopback-only` (the default): the request must name a loopback address as its Host and carry no sign of a proxy; the
pairing cookie alone is the credential. `tailscale-serve`: the request must come through Tailscale Serve (which sets
Tailscale-User-Login to the caller's login, replacing anything the caller sent, and passes the caller's Host through),
the login must be this machine's owner, the Host must name this machine, and a request through Funnel (the internet) or
from a tagged device (no login) is refused. `both`: every request is judged by exactly one of those two rule sets: by
tailscale-serve's when it carries any sign of a proxy, by loopback-only's when it carries none. Neither adapter stops
another process on this machine that connects to the port and writes the headers itself: the paired-browser cookie does that.
"""

from __future__ import annotations

import ipaddress
import json
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .omp import find_tool
from .util import DashboardError, RunResult, run_bounded, safe_env

IDENTITY_TTL_S = 60.0
IDENTITY_RETRY_S = 10.0  # while Tailscale is unknown, do not run `tailscale status` on every request
TAILSCALE_APP = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
# What says a proxy sits in front of the hub: every `Tailscale-*` and `X-Forwarded-*` header, and these. Looked for by the names
# the request carries, in any case, so that a header Serve or another proxy adds later is a sign too. A request with any of them
# is never judged as a direct local one.
PROXY_PREFIXES = ("tailscale-", "x-forwarded-")
PROXY_NAMES = ("forwarded", "via", "x-real-ip")
# What can be asked for by name when a headers object cannot be listed.
PROXY_HEADERS = (
    "Tailscale-User-Login",
    "Tailscale-Funnel-Request",
    "X-Forwarded-For",
    "X-Forwarded-Host",
    "Forwarded",
    "Via",
    "X-Real-Ip",
)


@dataclass(frozen=True)
class Denied:
    status: int
    message: str


class Headers(Protocol):
    def get(self, name: str, default: Any = None) -> Any: ...


def proxy_sign(headers: Any) -> str | None:
    """The name of the first header that shows a proxy in front of the hub, or None. See PROXY_PREFIXES and PROXY_NAMES."""
    names = getattr(headers, "keys", None)
    if names is None:
        return next((h for h in PROXY_HEADERS if headers.get(h) is not None), None)
    for name in names():
        low = str(name).lower()
        if low.startswith(PROXY_PREFIXES) or low in PROXY_NAMES:
            return str(name)
    return None


def loopback_address(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_loopback_bind(host: str, adapter: str) -> None:
    if not loopback_address(host):
        raise DashboardError(
            f"refusing to listen on {host!r}: the hub speaks plain HTTP and the {adapter} sign-in only works behind a "
            "loopback address. Use listen.host 127.0.0.1 (or ::1) and reach the hub from other devices through "
            "tailscale serve or an SSH forward"
        )


SINGLE_HEADERS = ("Host", "Tailscale-User-Login", "Tailscale-Funnel-Request")


def repeated_header(headers: Any) -> str | None:
    """The name of a header the adapters read that arrived more than once, or None. `get` would answer with the first and a proxy
    in front might have used the last: two copies are never a request the adapters will judge."""
    get_all = getattr(headers, "get_all", None)
    if get_all is None:
        return None
    for name in SINGLE_HEADERS:
        if len(get_all(name) or []) > 1:
            return name
    return None


def host_header(headers: Headers) -> str:
    return (headers.get("Host") or "").strip().lower()


class LoopbackOnly:
    name = "loopback-only"
    scheme = "http"

    def __init__(self, host: str, port: int, allowed_hosts: list[str]):
        self.host = host
        self.port = port
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"} | {h.lower() for h in allowed_hosts}

    def check_bind(self, host: str) -> None:
        check_loopback_bind(host, self.name)

    def access(self, headers: Headers) -> str:
        return "local"

    def check(self, headers: Headers) -> Denied | None:
        if repeated_header(headers):
            return Denied(403, "a header that must appear once appeared more than once")
        if proxy_sign(headers):
            return Denied(403, "this hub is set to loopback-only sign-in, which does not accept proxied requests")
        if host_header(headers) not in self.hosts:
            return Denied(403, "this hub answers only requests addressed to its own loopback address")
        return None

    def origins(self, headers: Headers | None = None) -> set[str]:
        return {f"http://{h}" for h in self.hosts}

    def public_url(self) -> str | None:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}/"

    def tailnet_url(self) -> str | None:
        return None

    def secure_cookie(self, headers: Headers | None = None) -> bool:
        return False


class TailscaleServe:
    name = "tailscale-serve"
    scheme = "https"

    def __init__(
        self,
        bin_path: str | None,
        serve_port: int,
        owner: str | None,
        public_host: str | None,
        env: Mapping[str, str] | None = None,
        run: Callable[..., RunResult] = run_bounded,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.bin_path = bin_path
        self.serve_port = serve_port
        self.fixed_owner = owner
        self.fixed_host = public_host.lower().rstrip(".") if public_host else None
        self.env = dict(os.environ if env is None else env)
        self.run = run
        self.clock = clock
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {"names": set(), "owner": None, "at": None, "refreshing": False}

    def check_bind(self, host: str) -> None:
        check_loopback_bind(host, self.name)

    # --- identity --------------------------------------------------------------------------------------------------

    def _lookup(self) -> tuple[set[str], str | None]:
        """(MagicDNS names of this machine, login of its owner) from `tailscale status --json`, or (set(), None)."""
        binary = find_tool("tailscale", self.bin_path, self.env)
        if binary is None and not self.bin_path and os.access(TAILSCALE_APP, os.X_OK):
            binary = TAILSCALE_APP
        if binary is None:
            return set(), None
        result = self.run([binary, "status", "--json"], 5.0, env=safe_env(), max_output=2 * 1024 * 1024)
        if not result.ok or result.truncated:
            return set(), None
        try:
            status = json.loads(result.stdout)
            me = status.get("Self") or {}
            dns = (me.get("DNSName") or "").rstrip(".")
            names = {dns, dns.split(".")[0]} - {""}
            user = (status.get("User") or {}).get(str(me.get("UserID"))) or {}
            login = user.get("LoginName")
        except (ValueError, AttributeError, TypeError):
            return set(), None
        return {n.lower() for n in names}, login if isinstance(login, str) and login else None

    def backend_state(self) -> str:
        """Whether Tailscale is up, asked now and never cached: the `BackendState` of `tailscale status --json` ("Running", "Stopped",
        "NeedsLogin", ...), "not-found" when there is no tailscale command, "unknown" when it does not answer in time or prints nothing
        usable. Identity (the owner and the machine's names) is a different question: it can be configured, or cached, while Tailscale is off."""
        binary = find_tool("tailscale", self.bin_path, self.env)
        if binary is None and not self.bin_path and os.access(TAILSCALE_APP, os.X_OK):
            binary = TAILSCALE_APP
        if binary is None:
            return "not-found"
        result = self.run([binary, "status", "--json"], 5.0, env=safe_env(), max_output=2 * 1024 * 1024)
        if not result.ok or result.truncated:
            return "unknown"
        try:
            state = json.loads(result.stdout).get("BackendState")
        except (ValueError, AttributeError):
            return "unknown"
        return state if isinstance(state, str) and state.isalnum() else "unknown"

    def identity(self) -> tuple[set[str], str | None]:
        """Names and owner, cached; one caller refreshes while the rest keep the cached answer. Configured values win;
        with both set, Tailscale is not asked at all."""
        if self.fixed_host and self.fixed_owner:
            return {self.fixed_host, self.fixed_host.split(".")[0]}, self.fixed_owner
        with self._lock:
            state = self._state
            ttl = IDENTITY_TTL_S if state["owner"] else IDENTITY_RETRY_S
            if state["refreshing"] or (state["at"] is not None and self.clock() - state["at"] < ttl):
                return self._merged(state["names"], state["owner"])
            state["refreshing"] = True
        try:
            names, owner = self._lookup()
        finally:
            with self._lock:
                self._state["refreshing"] = False
                self._state["at"] = self.clock()
        with self._lock:
            self._state.update(names=names, owner=owner)
        return self._merged(names, owner)

    def _merged(self, names: set[str], owner: str | None) -> tuple[set[str], str | None]:
        if self.fixed_host:
            names = {self.fixed_host, self.fixed_host.split(".")[0]}
        return names, self.fixed_owner or owner

    # --- the adapter -----------------------------------------------------------------------------------------------

    def access(self, headers: Headers) -> str:
        return "tailnet"

    def check(self, headers: Headers) -> Denied | None:
        if repeated_header(headers):
            return Denied(403, "a header that must appear once appeared more than once")
        names, owner = self.identity()
        if not owner or not names:
            return Denied(503, "the hub does not know this machine's Tailscale owner yet; is Tailscale running?")
        if headers.get("Tailscale-Funnel-Request") is not None:
            return Denied(403, "requests through Tailscale Funnel are refused")
        login = headers.get("Tailscale-User-Login")
        if not login or login != owner or not login.isprintable() or login != login.strip():
            return Denied(403, "this hub only answers its owner's tailnet login")
        host = host_header(headers)
        name, sep, port = host.rpartition(":")
        if not sep or not port.isdigit():  # no port in the Host header: the default https port
            name, port = host, "443"
        if name not in names or port != str(self.serve_port):
            return Denied(403, "this hub only answers requests addressed to this machine's tailnet name")
        return None

    def _name(self) -> str | None:
        names, _ = self.identity()
        return max(names, key=len) if names else None

    def origins(self, headers: Headers | None = None) -> set[str]:
        names, _ = self.identity()
        origins = {f"https://{n}:{self.serve_port}" for n in names}
        if self.serve_port == 443:
            origins |= {f"https://{n}" for n in names}
        return origins

    def public_url(self) -> str | None:
        name = self._name()
        if not name:
            return None
        return f"https://{name}/" if self.serve_port == 443 else f"https://{name}:{self.serve_port}/"

    def tailnet_url(self) -> str | None:
        return self.public_url()

    def secure_cookie(self, headers: Headers | None = None) -> bool:
        return True

    def web_origin(self, port: int) -> str | None:
        """https origin of another Serve port on this machine (the relay's guest listener), or None while unknown."""
        name = self._name()
        if not name:
            return None
        return f"https://{name}" if port == 443 else f"https://{name}:{port}"


class Both:
    """`loopback-only` and `tailscale-serve` at once, each request judged by exactly one of them.

    A request that carries any sign of a proxy (see `proxy_sign`) is judged by tailscale-serve's rules and only by them: the
    owner's login, this machine's name and Serve's port, no Funnel, no tagged device. A request that carries none is judged
    by loopback-only's rules and only by them: a loopback Host of this hub (or one in allowedHosts). So a request that looks
    direct but carries one forged Tailscale header is a proxied one and needs the owner's login, and a request with no sign
    of a proxy can never use the tailnet rules, whatever Host it names. What follows a request (the Origin its POSTs may
    carry, the Secure flag of its cookie) follows the same choice. The tailnet side answers 503 while the owner and the machine's
    names are unknown (a configured or recently looked-up identity is still used); whether Tailscale is running is asked
    separately (`TailscaleServe.backend_state`). The local side is not affected by either.
    """

    name = "both"

    def __init__(self, local: LoopbackOnly, tailnet: TailscaleServe):
        self.local = local
        self.tailnet = tailnet

    def check_bind(self, host: str) -> None:
        check_loopback_bind(host, self.name)

    def access(self, headers: Headers) -> str:
        return "tailnet" if proxy_sign(headers) else "local"

    def _by(self, headers: Headers) -> LoopbackOnly | TailscaleServe:
        return self.tailnet if self.access(headers) == "tailnet" else self.local

    def check(self, headers: Headers) -> Denied | None:
        return self._by(headers).check(headers)

    def origins(self, headers: Headers) -> set[str]:
        return self._by(headers).origins()

    def secure_cookie(self, headers: Headers) -> bool:
        return self._by(headers).secure_cookie()

    def public_url(self) -> str | None:
        return self.local.public_url()

    def tailnet_url(self) -> str | None:
        return self.tailnet.public_url()

    def backend_state(self) -> str:
        return self.tailnet.backend_state()

    def web_origin(self, port: int) -> str | None:
        return self.tailnet.web_origin(port)


def build_adapter(config: Any, env: Mapping[str, str] | None = None) -> LoopbackOnly | TailscaleServe | Both:
    """The adapter the configuration names."""
    host = config.get("listen.host")
    port = config.get("listen.port")
    adapter = config.get("auth.adapter")
    local = LoopbackOnly(host, port, config.get("auth.loopbackOnly.allowedHosts"))
    if adapter == "loopback-only":
        return local
    tailnet = TailscaleServe(
        config.get("auth.tailscale.bin"),
        config.serve_port,
        config.get("auth.tailscale.owner"),
        config.get("auth.tailscale.publicHost"),
        env=env,
    )
    return tailnet if adapter == "tailscale-serve" else Both(local, tailnet)
