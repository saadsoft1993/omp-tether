"""What this plugin hands to omp-services: a services/1 drop-in (how to keep the hub running) and a health/1 drop-in (what
to watch). This plugin never installs a unit itself. See docs/protocol/services-1.md and health-1.md for the two formats;
both documents are the contract, and omp-services owns the renderer.

The drop-ins hold paths and port numbers only (no secrets), and are written only when you ask for them. Without
omp-services, run the hub by hand (`omp-dashboard run`) or write a unit yourself: the README shows both.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .config import ENV_OVERRIDES, Config
from .paths import APP, CONFIG_FILE_ENV
from .trustwalk import TrustError, check_path
from .ui import plugin_root
from .util import DashboardError, ensure_private_dir, write_file


def stable_script(file: str, env: Mapping[str, str]) -> str:
    """The path a unit should name: the stable plugin link omp keeps up to date when it exists, else the real tree."""
    real = plugin_root()
    home = env.get("HOME", "")
    if home and os.path.isabs(home):
        link = Path(home) / ".omp" / "plugins" / "node_modules" / APP
        try:
            if link.resolve() == real.resolve():
                return str(link / file)
        except OSError:
            pass
    return str(real / file)


def pinned_env(config: Config, env: Mapping[str, str]) -> dict[str, str]:
    """The environment that makes the service read the SAME configuration as the one shown to the person: the absolute
    path of the config file, and the resolved XDG roots (the defaults inside the config derive from them)."""
    paths = config.paths
    return {
        CONFIG_FILE_ENV: str(paths.config_file),
        "XDG_CONFIG_HOME": str(paths.config_dir.parent),
        "XDG_STATE_HOME": str(paths.state_dir.parent),
    }


# Variables other tools define that the hub falls back on when its own setting is empty: (variable, the setting it stands in for).
FALLBACKS = (
    ("OMP_BIN", ("omp", "bin")),
    ("HERDR_BIN", ("start", "herdr", "bin")),
    ("OMP_SESSION_CONTROL_DIR", ("control", "dir")),
    ("PI_CODING_AGENT_DIR", ("projects", "sessionsDir")),
)


ALWAYS = {"PI_CODING_AGENT_DIR"}


def stray_overrides(env: Mapping[str, str], config: Config | None = None) -> list[str]:
    """Names of variables set in `env` that change what the hub does and that a service would not see: the OMP_DASHBOARD_*
    settings, and (given `config`) the fallback variables whose setting is empty, so that they would decide something."""
    names = [name for name in ENV_OVERRIDES if env.get(name)]
    if config is not None:
        for name, keys in FALLBACKS:
            # PI_CODING_AGENT_DIR also selects omp's profile for every omp the hub starts, whatever dashboard setting
            # (projects.sessionsDir) is configured, and a service would not have it: it is refused even then.
            if env.get(name) and (name in ALWAYS or not config.get(".".join(keys))):
                names.append(name)
    return names


def services_dropin(config: Config, env: Mapping[str, str]) -> dict[str, Any]:
    return {
        "protocol": "services/1",
        "plugin": APP,
        "services": [
            {
                "name": "hub",
                "description": "omp dashboard hub",
                "program": {
                    "type": "python",
                    "script": stable_script("python/omp_dashboard_main.py", env),
                    "args": ["run"],
                },
                "env": pinned_env(config, env),
                "protected": False,
                "throttleSeconds": 10,
            }
        ],
    }


def health_dropin(config: Config, env: Mapping[str, str]) -> dict[str, Any]:
    host = config.get("listen.host")
    port = int(config.get("listen.port"))
    url_host = f"[{host}]" if ":" in host else host
    checks: list[dict[str, Any]] = [
        {
            "id": "hub-health",
            "type": "url",
            "url": f"http://{url_host}:{port}/healthz",
            "freshness": {"jsonPointer": "/updatedAt", "maxAgeSeconds": 60, "format": "epoch-seconds"},
        },
        {"id": "hub-port", "type": "port", "host": host, "port": port},
        {"id": "hub-owner", "type": "uid-owner", "port": port},
        {"id": "hub-service", "type": "watchdog", "service": "hub"},
    ]
    if config.get("auth.adapter") == "tailscale-serve":
        checks.append(
            {
                "id": "serve-route",
                "type": "serve-route",
                "port": config.serve_port,
                "path": "/",
                "target": f"http://{url_host}:{port}",
            }
        )
    return {"protocol": "health/1", "plugin": APP, "checks": checks}


def dropin_paths(config: Config) -> dict[str, Path]:
    return {"services": config.paths.services_dropin, "health": config.paths.health_dropin}


def dropin_text(doc: dict[str, Any]) -> str:
    return json.dumps(doc, indent="\t") + "\n"


def write_dropin(file: Path, doc: dict[str, Any]) -> Path:
    """Writes one drop-in atomically (0600) into its directory (0700 when created; another tool's existing directory is
    never chmod-ed and must not be writable by others). Refuses a path that is a symbolic link."""
    ensure_private_dir(file.parent, tighten=False)
    try:
        check_path(file.parent)
    except TrustError as exc:
        raise DashboardError(str(exc)) from None
    if file.is_symlink():
        raise DashboardError(f"{file} is a symbolic link; remove it first")
    write_file(file, dropin_text(doc), 0o600)
    return file
