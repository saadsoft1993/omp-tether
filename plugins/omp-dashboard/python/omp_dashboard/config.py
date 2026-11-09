"""The configuration file, the defaults and the environment overrides.

Order, lowest to highest: built-in defaults, `config.json`, environment variables. The merged result is validated
against `omp_dashboard/schemas/config-1.schema.json`; whatever is wrong is reported with where it comes from, never
silently corrected. Secrets are never in this file. The file decides which programs the hub runs, so it is read like
a secret is: through one descriptor, owned by you, not writable by others, in a chain of directories nobody else can
replace (see util.read_trusted_file).
"""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import schema
from .paths import Paths
from .util import DashboardError, read_text

DEFAULTS: dict[str, Any] = {
    "schemaVersion": 1,
    "listen": {"host": "127.0.0.1", "port": 17795},
    "auth": {
        "adapter": "loopback-only",
        "loopbackOnly": {"allowedHosts": []},
        "tailscale": {"bin": None, "servePort": None, "owner": None, "publicHost": None},
    },
    "pairing": {"dir": None, "cookieName": "omp_dashboard", "maxAgeDays": 90},
    "omp": {"bin": None},
    "control": {"dir": None},
    "relay": {
        "enabled": False,
        "hostPort": 17797,
        "guestPort": 17796,
        "checkDir": None,
        "webUrl": None,
        "servePort": None,
    },
    "start": {
        "backend": "off",
        "roots": [],
        "maxDepth": 3,
        "herdr": {"bin": None},
        "tmux": {"bin": None, "session": "omp"},
    },
    "projects": {"rankBySessions": False, "sessionsDir": None},
    "alerts": {"server": "https://ntfy.sh", "topicFile": None, "minTurnSeconds": 60},
    "stats": {"url": None},
    "audit": {"file": None, "maxBytes": 1048576},
    "machine": {"name": None},
}


def _bool(text: str) -> bool:
    low = text.strip().lower()
    if low in {"1", "true", "yes", "on"}:
        return True
    if low in {"0", "false", "no", "off"}:
        return False
    raise ValueError("expected true or false")


def _int(text: str) -> int:
    return int(text.strip(), 10)


def _roots(text: str) -> list[str]:
    return [part for part in text.split(":") if part]


# environment variable -> (path in the config, parser). A service sees none of these (the drop-in pins only the
# configuration file and the XDG roots), so `dropins` refuses settings that exist only as environment variables.
ENV_OVERRIDES: dict[str, tuple[tuple[str, ...], Callable[[str], Any]]] = {
    "OMP_DASHBOARD_PORT": (("listen", "port"), _int),
    "OMP_DASHBOARD_AUTH": (("auth", "adapter"), str),
    "OMP_DASHBOARD_TAILSCALE_BIN": (("auth", "tailscale", "bin"), str),
    "OMP_DASHBOARD_TAILSCALE_SERVE_PORT": (("auth", "tailscale", "servePort"), _int),
    "OMP_DASHBOARD_TAILSCALE_OWNER": (("auth", "tailscale", "owner"), str),
    "OMP_DASHBOARD_TAILSCALE_HOST": (("auth", "tailscale", "publicHost"), str),
    "OMP_DASHBOARD_PAIRING_DIR": (("pairing", "dir"), str),
    "OMP_DASHBOARD_COOKIE_NAME": (("pairing", "cookieName"), str),
    "OMP_DASHBOARD_PAIRING_MAX_AGE_DAYS": (("pairing", "maxAgeDays"), _int),
    "OMP_DASHBOARD_OMP_BIN": (("omp", "bin"), str),
    "OMP_DASHBOARD_CONTROL_DIR": (("control", "dir"), str),
    "OMP_DASHBOARD_RELAY_ENABLED": (("relay", "enabled"), _bool),
    "OMP_DASHBOARD_RELAY_HOST_PORT": (("relay", "hostPort"), _int),
    "OMP_DASHBOARD_RELAY_GUEST_PORT": (("relay", "guestPort"), _int),
    "OMP_DASHBOARD_RELAY_CHECK_DIR": (("relay", "checkDir"), str),
    "OMP_DASHBOARD_RELAY_WEB_URL": (("relay", "webUrl"), str),
    "OMP_DASHBOARD_RELAY_SERVE_PORT": (("relay", "servePort"), _int),
    "OMP_DASHBOARD_START_BACKEND": (("start", "backend"), str),
    "OMP_DASHBOARD_START_ROOTS": (("start", "roots"), _roots),
    "OMP_DASHBOARD_HERDR_BIN": (("start", "herdr", "bin"), str),
    "OMP_DASHBOARD_TMUX_BIN": (("start", "tmux", "bin"), str),
    "OMP_DASHBOARD_TMUX_SESSION": (("start", "tmux", "session"), str),
    "OMP_DASHBOARD_RANK_BY_SESSIONS": (("projects", "rankBySessions"), _bool),
    "OMP_DASHBOARD_SESSIONS_DIR": (("projects", "sessionsDir"), str),
    "OMP_DASHBOARD_NTFY_SERVER": (("alerts", "server"), str),
    "OMP_DASHBOARD_NTFY_TOPIC_FILE": (("alerts", "topicFile"), str),
    "OMP_DASHBOARD_ALERT_MIN_TURN_SECONDS": (("alerts", "minTurnSeconds"), _int),
    "OMP_DASHBOARD_STATS_URL": (("stats", "url"), str),
    "OMP_DASHBOARD_AUDIT_FILE": (("audit", "file"), str),
    "OMP_DASHBOARD_MACHINE_NAME": (("machine", "name"), str),
}


def _set(tree: dict[str, Any], keys: tuple[str, ...], value: Any) -> None:
    node = tree
    for key in keys[:-1]:
        node = node.setdefault(key, {})
    node[keys[-1]] = value


def _merge(base: dict[str, Any], top: Mapping[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in top.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _control_chars(value: Any, where: str = "$") -> list[str]:
    """Strings that hold a control character: a path or a name in a log line or a unit file must stay on one line."""
    if isinstance(value, str):
        return [f"{where}: contains a control character"] if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value) else []
    if isinstance(value, list):
        return [p for i, v in enumerate(value) for p in _control_chars(v, f"{where}[{i}]")]
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in _control_chars(v, f"{where}.{k}")]
    return []


def _dot_dot(value: Any, where: str = "$") -> list[str]:
    """A path setting with a `..` component. It is refused, not collapsed: collapsing `a/link/..` to `a` names another directory than
    the kernel would reach, and the trust walk and the code that uses the path must agree on what the path is."""
    if isinstance(value, str):
        return (
            [f"{where}: a path with '..' in it is not accepted; write the path without it"]
            if value.startswith(("/", "~/")) and ".." in value.split("/")
            else []
        )
    if isinstance(value, list):
        return [p for i, v in enumerate(value) for p in _dot_dot(v, f"{where}[{i}]")]
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in _dot_dot(v, f"{where}.{k}")]
    return []


def _semantic_problems(values: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    listen = values["listen"]["port"]
    relay = values["relay"]
    if relay["enabled"]:
        if relay["hostPort"] == relay["guestPort"]:
            problems.append("relay.hostPort and relay.guestPort must differ")
        for name in ("hostPort", "guestPort"):
            if relay[name] == listen:
                problems.append(f"relay.{name} is the hub's own port ({listen}); choose different ports")
    return problems


@dataclass
class Config:
    values: dict[str, Any]
    paths: Paths
    problems: list[str] = field(default_factory=list)
    overrides: list[str] = field(default_factory=list)  # names of the environment variables that took effect
    file_present: bool = False
    env: dict[str, str] = field(default_factory=dict)

    def require_valid(self) -> Config:
        if self.problems:
            raise DashboardError("the configuration is invalid:\n  " + "\n  ".join(self.problems))
        return self

    def get(self, dotted: str) -> Any:
        node: Any = self.values
        for key in dotted.split("."):
            node = node[key]
        return node

    # --- resolved locations ---------------------------------------------------------------------------------------

    def _path(self, dotted: str, default: Path) -> Path:
        value = self.get(dotted)
        return Path(os.path.normpath(value)) if value else default

    @property
    def pairing_dir(self) -> Path:
        return self._path("pairing.dir", self.paths.pairing_dir)

    @property
    def control_dir(self) -> Path:
        configured = self.get("control.dir")
        if configured:
            return Path(os.path.normpath(configured))
        env = self.env.get("OMP_SESSION_CONTROL_DIR", "")
        if env and os.path.isabs(env) and ".." not in env.split("/"):
            return Path(os.path.normpath(env))
        return self.paths.control_dir

    @property
    def relay_check_dir(self) -> Path:
        return self._path("relay.checkDir", self.paths.relay_check_dir)

    @property
    def ntfy_topic_file(self) -> Path:
        return self._path("alerts.topicFile", self.paths.ntfy_topic_file)

    @property
    def audit_file(self) -> Path:
        return self._path("audit.file", self.paths.audit_file)

    @property
    def sessions_dir(self) -> Path:
        configured = self.get("projects.sessionsDir")
        if configured:
            return Path(os.path.normpath(configured))
        agent = self.env.get("PI_CODING_AGENT_DIR", "")
        usable = agent and os.path.isabs(agent) and ".." not in agent.split("/")
        base = Path(agent) if usable else self.paths.home / ".omp" / "agent"
        return base / "sessions"

    @property
    def serve_port(self) -> int:
        return int(self.get("auth.tailscale.servePort") or self.get("listen.port"))

    def effective_overrides(self) -> dict[str, str]:
        """The environment overrides that took effect, as validated values (never the raw text from the environment)."""
        out: dict[str, str] = {}
        for name in self.overrides:
            node: Any = self.values
            for key in ENV_OVERRIDES[name][0]:
                node = node[key]
            out[name] = ("true" if node else "false") if isinstance(node, bool) else str(node)
        return out


def default_config_text() -> str:
    """What `config init` would write: the defaults, so the file documents every key."""
    return json.dumps(DEFAULTS, indent="\t") + "\n"


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key {key!r}")
        seen[key] = value
    return seen


def load_config(paths: Paths, env: Mapping[str, str]) -> Config:
    """Read the file and the environment. Never raises for a bad value: the problems are listed on the result."""
    config_schema = schema.load_schema("config-1")
    problems: list[str] = []
    file_values: dict[str, Any] = {}
    present = False
    text: str | None = None
    try:
        text = read_text(paths.config_file)
    except FileNotFoundError:
        text = None  # no file: the defaults, which are safe
    except DashboardError as exc:
        problems.append(str(exc))
    if text is not None:
        present = True
        try:
            parsed = json.loads(text, object_pairs_hook=_no_duplicates)
        except ValueError as exc:
            problems.append(f"{paths.config_file}: not valid JSON ({exc})")
        else:
            if isinstance(parsed, dict):
                for message in schema.validate(parsed, config_schema):
                    problems.append(f"{paths.config_file}: {message}")
                problems.extend(f"{paths.config_file}: {m}" for m in _control_chars(parsed) + _dot_dot(parsed))
                file_values = parsed
            else:
                problems.append(f"{paths.config_file}: the top level must be an object")
    merged = _merge(DEFAULTS, file_values) if not problems else copy.deepcopy(DEFAULTS)
    overrides: list[str] = []
    for name, (keys, parse) in ENV_OVERRIDES.items():
        raw = env.get(name)
        if raw is None or raw == "":
            continue
        try:
            _set(merged, keys, parse(raw))
        except ValueError as exc:
            problems.append(f"environment {name}: {exc}")
            continue
        overrides.append(name)
    for name in (
        "OMP_SESSION_CONTROL_DIR",
        "PI_CODING_AGENT_DIR",
    ):  # fallbacks other tools define: refused, not silently replaced by a default
        value = env.get(name, "")
        if value and ".." in value.split("/"):
            problems.append(f"environment {name}: a path with '..' in it is not accepted; write the path without it")
    if overrides:
        for message in schema.validate(merged, config_schema):
            problems.append(f"after environment overrides ({', '.join(overrides)}): {message}")
        problems.extend(f"after environment overrides: {m}" for m in _control_chars(merged) + _dot_dot(merged))
    if not problems:
        problems.extend(_semantic_problems(merged))
    return Config(
        values=merged, paths=paths, env=dict(env), problems=problems, overrides=overrides, file_present=present
    )
