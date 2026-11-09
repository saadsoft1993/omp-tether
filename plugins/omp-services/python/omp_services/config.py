"""The configuration file, the defaults and the environment overrides.

Order, lowest to highest: built-in defaults, `config.json`, environment variables. The merged result is validated
against `omp_services/schemas/config-1.schema.json`; whatever is wrong is reported with where it comes from, never silently
corrected. Secrets are never in this file.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import schema
from .paths import Paths
from .util import ServicesError, dir_problem, read_text

DEFAULTS: dict[str, Any] = {
    "schemaVersion": 1,
    "labelPrefix": "omp-tether",
    "omp": {"path": None},
    "python": {"path": None},
    "heartbeat": {
        "enabled": False,
        "allowNoChecks": False,
        "intervalSeconds": 300,
        "urlFile": None,
        "pingTimeoutSeconds": 15,
        "checkTimeoutSeconds": 10,
        "cycleTimeoutSeconds": 120,
    },
    "health": {"allowRemoteUrls": False},
    "tools": {"tailscale": None},
    "catalog": {"marketplace": "omp-tether"},
    "logs": {"maxBytes": 1048576},
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


# environment variable -> (path in the config, parser)
ENV_OVERRIDES: dict[str, tuple[tuple[str, ...], Callable[[str], Any]]] = {
    "OMP_SERVICES_LABEL_PREFIX": (("labelPrefix",), str),
    "OMP_SERVICES_OMP_PATH": (("omp", "path"), str),
    "OMP_SERVICES_PYTHON": (("python", "path"), str),
    "OMP_SERVICES_HEARTBEAT_ENABLED": (("heartbeat", "enabled"), _bool),
    "OMP_SERVICES_HEARTBEAT_ALLOW_NO_CHECKS": (("heartbeat", "allowNoChecks"), _bool),
    "OMP_SERVICES_HEARTBEAT_INTERVAL_SECONDS": (("heartbeat", "intervalSeconds"), _int),
    "OMP_SERVICES_HEARTBEAT_URL_FILE": (("heartbeat", "urlFile"), str),
    "OMP_SERVICES_ALLOW_REMOTE_URLS": (("health", "allowRemoteUrls"), _bool),
    "OMP_SERVICES_TAILSCALE": (("tools", "tailscale"), str),
    "OMP_SERVICES_MARKETPLACE": (("catalog", "marketplace"), str),
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


@dataclass
class Config:
    values: dict[str, Any]
    problems: list[str] = field(default_factory=list)
    overrides: list[str] = field(default_factory=list)  # names of the environment variables that took effect
    file_present: bool = False

    def require_valid(self) -> Config:
        if self.problems:
            raise ServicesError("the configuration is invalid:\n  " + "\n  ".join(self.problems))
        return self

    @property
    def label_prefix(self) -> str:
        return str(self.values["labelPrefix"])

    @property
    def omp_path(self) -> str | None:
        return self.values["omp"]["path"]

    @property
    def python_path(self) -> str | None:
        return self.values["python"]["path"]

    @property
    def heartbeat(self) -> dict[str, Any]:
        return self.values["heartbeat"]

    @property
    def allow_remote_urls(self) -> bool:
        return bool(self.values["health"]["allowRemoteUrls"])

    @property
    def tailscale(self) -> str | None:
        return self.values["tools"]["tailscale"]

    @property
    def marketplace(self) -> str:
        return str(self.values["catalog"]["marketplace"])

    @property
    def log_max_bytes(self) -> int:
        return int(self.values["logs"]["maxBytes"])

    def effective_overrides(self) -> dict[str, str]:
        """The environment overrides that took effect, as validated values (never the raw text from the environment: a
        value with a trailing newline or spaces is accepted by the parser and must not be copied into a unit as is)."""
        out: dict[str, str] = {}
        for name in self.overrides:
            node: Any = self.values
            for key in ENV_OVERRIDES[name][0]:
                node = node[key]
            out[name] = ("true" if node else "false") if isinstance(node, bool) else str(node)
        return out

    def url_file(self, paths: Paths) -> Path:
        configured = self.heartbeat["urlFile"]
        return Path(configured) if configured else paths.default_url_file


def default_config_text() -> str:
    """What `init` writes when there is no config file: the defaults, so the file documents every key."""
    return json.dumps(DEFAULTS, indent="\t") + "\n"


def load_config(paths: Paths, env: Mapping[str, str]) -> Config:
    """Read the file and the environment. Never raises for a bad value: the problems are listed on the result."""
    config_schema = schema.load_schema("config-1")
    problems: list[str] = []
    file_values: dict[str, Any] = {}
    present = False
    try:
        # The file decides which programs run, so it is held to the same standard as a drop-in: ours, not writable by
        # others, in a directory that others cannot write. Checked on the opened descriptor.
        trouble = dir_problem(paths.config_dir, forbid=0o022) if paths.config_file.exists() else None
        if trouble:
            raise ServicesError(f"{paths.config_dir} {trouble}: the configuration in it is not used")
        text = read_text(paths.config_file, forbid=0o022)
    except ServicesError as exc:
        if paths.config_file.exists() or paths.config_file.is_symlink():
            problems.append(str(exc))
        text = None
    if text is not None:
        present = True
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            problems.append(f"{paths.config_file}: not valid JSON ({exc})")
            parsed = None
        if isinstance(parsed, dict):
            for message in schema.validate(parsed, config_schema):
                problems.append(f"{paths.config_file}: {message}")
            file_values = parsed
        elif parsed is not None:
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
    if overrides:
        for message in schema.validate(merged, config_schema):
            problems.append(f"after environment overrides ({', '.join(overrides)}): {message}")
    return Config(values=merged, problems=problems, overrides=overrides, file_present=present)
