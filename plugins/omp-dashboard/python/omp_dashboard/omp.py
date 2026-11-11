"""omp's own command line: which sessions are live (`omp collab list --json`), a fresh browser link for one of them
(`omp collab link`), and the display name of this machine (`omp config get collab.displayName`).

Nothing here reads omp's private files. Every call is an argv list (no shell) with a deadline, a capped output, its own
process group, and an environment without BUN_BE_BUN: that variable turns omp into a plain Bun, so it must never reach
the omp we start.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .util import DashboardError, RunResult, printable, run_bounded

# The version of `omp collab list --json` / `omp collab link --json` this dashboard reads (omp's own `version` field).
COLLAB_JSON_VERSIONS = (1,)
INSTANCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{3,63}\Z")  # must not start with "-": omp would read it as a flag
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{7,63}\Z")
MAX_JSON_BYTES = 4 * 1024 * 1024
EXTRA_DIRS = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin")

STILL_PUBLIC = "still on the public relay: in that session run /collab stop, then /collab"


class OmpError(DashboardError):
    """omp could not be run, did not answer in time, or answered something this dashboard does not understand."""


class PublicRelayError(DashboardError):
    """The session is still hosted on the public relay; the dashboard never hands out such links."""


def find_tool(name: str, configured: str | None, env: Mapping[str, str], env_var: str | None = None) -> str | None:
    """An executable's absolute path: the configured one, else the environment variable's, else PATH, else the usual
    places (a service starts with a very small PATH). None when it cannot be found."""
    candidates: list[str] = []
    if configured:
        candidates.append(configured)
    elif env_var and env.get(env_var):
        candidates.append(env[env_var])
    else:
        found = shutil.which(name, path=env.get("PATH"))
        if found:
            candidates.append(found)
        home = env.get("HOME", "")
        for directory in EXTRA_DIRS:
            base = os.path.join(home, directory[2:]) if directory.startswith("~/") else directory
            candidates.append(os.path.join(base, name))
    for candidate in candidates:
        if os.path.isabs(candidate) and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def child_env(env: Mapping[str, str]) -> dict[str, str]:
    """The environment of a child omp: ours without BUN_BE_BUN."""
    return {k: v for k, v in env.items() if k != "BUN_BE_BUN"}


def _host(raw: Any) -> dict[str, Any] | None:
    """A host from `collab list`, reduced to the fields the dashboard knows, each checked for its type."""
    if not isinstance(raw, dict):
        return None
    instance = raw.get("instanceId")
    if not isinstance(instance, str) or not INSTANCE_ID.match(instance):
        return None
    host: dict[str, Any] = {"instanceId": instance}

    def number(key: str) -> None:
        value = raw.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool):
            host[key] = value

    def text(key: str, limit: int) -> None:
        value = raw.get(key)
        if isinstance(value, str):
            host[key] = printable(value, limit)

    def flag(key: str) -> None:
        value = raw.get(key)
        if isinstance(value, bool):
            host[key] = value

    for key in ("generation", "pid", "startedAt", "participants"):
        number(key)
    text("sessionId", 64)
    text("sessionName", 200)
    text("cwd", 1024)
    for key in ("relayConnected", "inputRequired", "busy"):
        flag(key)
    model = raw.get("model")
    if isinstance(model, dict):
        host["model"] = {k: printable(model[k], 200) for k in ("provider", "id") if isinstance(model.get(k), str)}
    return host


class Omp:
    """Runs omp. `binary` is a callable so that a configuration reload or a late install is picked up."""

    def __init__(
        self,
        binary: Callable[[], str | None],
        env: Mapping[str, str] | None = None,
        run: Callable[..., RunResult] = run_bounded,
    ):
        self._binary = binary
        self._env = child_env(os.environ if env is None else env)
        self._run = run

    def _argv(self, *args: str) -> list[str]:
        binary = self._binary()
        if not binary:
            raise OmpError("omp was not found: set omp.bin in the configuration, or put omp on PATH")
        return [binary, *args]

    def _json(self, args: list[str], timeout: float = 15.0) -> dict[str, Any]:
        result = self._run(self._argv(*args), timeout, env=self._env, max_output=MAX_JSON_BYTES)
        if result.error:
            raise OmpError("omp could not be started")
        if result.timed_out:
            raise OmpError("omp did not answer in time")
        if result.returncode != 0:
            raise OmpError(f"omp exited with status {result.returncode}")
        if result.truncated:
            raise OmpError("omp's answer was larger than the dashboard reads")
        try:
            parsed = json.loads(result.stdout) if result.stdout.strip() else {}
        except ValueError:
            raise OmpError("omp did not print JSON") from None
        if not isinstance(parsed, dict):
            raise OmpError("omp did not print a JSON object")
        version = parsed.get("version")
        if parsed and version not in COLLAB_JSON_VERSIONS:
            raise OmpError(f"omp printed JSON version {version!r}; this dashboard reads version 1")
        return parsed

    def list_hosts(self) -> list[dict[str, Any]]:
        """Live omp sessions that publish a Collab room (every interactive session), reduced to known fields."""
        hosts = self._json(["collab", "list", "--json"]).get("hosts") or []
        if not isinstance(hosts, list):
            raise OmpError("omp's host list is not a list")
        return [h for h in (_host(raw) for raw in hosts) if h is not None]

    def link(self, instance_id: str, view: bool) -> dict[str, Any]:
        """The Collab link of a session's current room: full control, or view-only with `view`."""
        if not INSTANCE_ID.match(instance_id or ""):
            raise ValueError("bad session id")
        args = ["collab", "link", instance_id, "--json"]
        if view:
            args.insert(-1, "--view")
        return self._json(args)

    def display_name(self) -> str | None:
        """omp's `collab.displayName`, or None when it is not set."""
        result = self._run(self._argv("config", "get", "collab.displayName"), 10.0, env=self._env)
        if not result.ok:
            return None
        name = " ".join(printable(result.stdout).split())[:60]
        return name if name and name != "undefined" else None


def machine_name(configured: str | None, omp: Omp | None) -> str:
    """This machine's dashboard name: the configured one, else omp's `collab.displayName`, else the host name; never
    empty, one line, short."""
    if configured:
        return " ".join(printable(configured).split())[:60] or "this machine"
    name = None
    if omp is not None:
        try:
            name = omp.display_name()
        except OmpError:
            name = None
    return name or (socket.gethostname().split(".")[0] or "this machine")


# ---------------------------------------------------------------------------------------------------------------------
# Which relay a room uses
# ---------------------------------------------------------------------------------------------------------------------


def link_relay(url: str | None, web_url: str | Sequence[str] | None, relay_url: str | None) -> str:
    """'private' for a link served by our collab-web and routed through our relay, else 'public'.

    omp web links look like `<webUrl>/#<relay origin>/r/<room>.<key>`; both halves must be ours. `web_url` is the address
    of our collab-web, or every address it has (with `both`, the loopback names of this machine: 127.0.0.1 and localhost).
    """
    web_urls = [web_url] if isinstance(web_url, str) else list(web_url or ())
    if not any(web_urls) or not relay_url:
        return "public"
    base, sep, fragment = (url or "").partition("/#")
    if not sep or not any(base.rstrip("/").lower() == w.rstrip("/").lower() for w in web_urls if w):
        return "public"
    return "private" if fragment.lower().startswith(relay_url.rstrip("/").lower() + "/r/") else "public"


FAIL_BACKOFF_S = 30.0


class RelayClassifier:
    """Which relay each live room uses, cached per (instanceId, generation).

    `omp collab list` does not say which relay a room is on, so the first sight of a generation asks for its view-only
    link and keeps only the verdict; the link itself is dropped at once.
    """

    def __init__(self, omp: Omp):
        self.omp = omp
        self.cache: dict[tuple[Any, Any], str] = {}
        self.failed: dict[tuple[Any, Any], float] = {}  # key -> when asking failed; not asked again for FAIL_BACKOFF_S
        self.lock = threading.Lock()

    def annotate(
        self,
        hosts: list[dict[str, Any]],
        web_url: str | Sequence[str] | None,
        relay_url: str | None,
        enabled: bool = True,
    ) -> list[dict[str, Any]]:
        if not enabled or not relay_url:
            for h in hosts:
                h["relay"] = "off"
            return hosts
        if not web_url:  # our own web address is not known: our links cannot be told from public ones
            for h in hosts:
                h["relay"] = "unknown"
            return hosts
        live = set()
        for h in hosts:
            key = (h.get("instanceId"), h.get("generation"))
            live.add(key)
            with self.lock:
                verdict = self.cache.get(key)
            with self.lock:
                failed_at = self.failed.get(key)
            if verdict is None and failed_at is not None and time.monotonic() - failed_at < FAIL_BACKOFF_S:
                verdict = "unknown"  # every poll would otherwise spawn `omp collab link` per room while omp is failing
            if verdict is None:
                try:
                    url = self.omp.link(h["instanceId"], True)["url"]
                except (OmpError, KeyError, TypeError, ValueError):
                    verdict = "unknown"  # asked again after FAIL_BACKOFF_S
                    with self.lock:
                        self.failed[key] = time.monotonic()
                else:
                    verdict = link_relay(url if isinstance(url, str) else None, web_url, relay_url)
                    with self.lock:
                        self.cache[key] = verdict
            h["relay"] = verdict
        with self.lock:
            for gone in set(self.cache) - live:
                del self.cache[gone]
            for gone in set(self.failed) - live:
                del self.failed[gone]
        return hosts


def retarget(url: str, web_url: str) -> str:
    """The same link at another web address: `<web_url>/#<everything after the old address>`."""
    _, _, fragment = url.partition("/#")
    return f"{web_url.rstrip('/')}/#{fragment}"


def session_link(
    omp: Omp,
    instance_id: str,
    view: bool,
    web_url: str | Sequence[str],
    relay_url: str,
    attempts: int = 3,
    open_at: str | None = None,
) -> dict[str, Any]:
    """A fresh browser link for the session's current room, fetched at click time.

    Rooms rotate (session switch, /collab restart). `omp collab link` lists the hosts, then asks for the generation it
    just saw and fails if the room moved in between; the retry lists again and follows the new generation. Full-control
    links steer the agent on this machine: never log or store them. Only links through our own relay are ever returned.
    `open_at` (one of our web addresses) is the address the returned link opens at, whichever of ours omp put in it.
    """
    if not INSTANCE_ID.match(instance_id or ""):
        raise ValueError("bad session id")
    out: dict[str, Any] = {}
    for attempt in range(attempts):
        try:
            out = omp.link(instance_id, view)
            break
        except OmpError:
            if attempt == attempts - 1:
                raise LookupError("that session ended or its room keeps changing; refresh the list") from None
    url = out.get("url")
    relay = link_relay(url if isinstance(url, str) else None, web_url, relay_url)
    if relay == "public":
        raise PublicRelayError(STILL_PUBLIC)
    if open_at and isinstance(url, str):
        url = retarget(url, open_at)
    return {"url": url, "generation": out.get("generation"), "relay": relay}
