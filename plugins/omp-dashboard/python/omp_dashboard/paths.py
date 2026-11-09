"""Where everything lives. One place, so the command line, the hub and the tests agree.

    config   $XDG_CONFIG_HOME/omp-dashboard  (default ~/.config/omp-dashboard)   what you edit
             config.json, pairing/ (0700: the pairing store), secrets/ (0700: the ntfy topic)
    state    $XDG_STATE_HOME/omp-dashboard   (default ~/.local/state/omp-dashboard)   what the hub writes
             audit.jsonl (and audit.jsonl.1)

The XDG defaults are used on macOS too (the other plugins of this family do the same). The configuration file can be
moved with OMP_DASHBOARD_CONFIG (an absolute path); everything else moves with the XDG variables or with the settings
in the file.
"""

from __future__ import annotations

import os
import pwd
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .util import DashboardError

APP = "omp-dashboard"
CONFIG_FILE_ENV = "OMP_DASHBOARD_CONFIG"


def lexical(value: str, name: str) -> str:
    """A path from the environment as it was written: refused when it has a `..` component (see config._dot_dot)."""
    if ".." in value.split("/"):
        raise DashboardError(f"{name} must not contain '..'")
    return value


def plain(value: str) -> bool:
    """No control characters: a path that reaches a unit file or a log line must not be able to start a new line."""
    return all(ord(c) >= 0x20 and ord(c) != 0x7F for c in value)


def home_dir(env: Mapping[str, str]) -> Path:
    if env.get("HOME"):
        lexical(env["HOME"], "HOME")  # before anything is normalised: `a/link/..` is not `a`
    value = env.get("HOME", "")
    if value and not plain(value):
        raise DashboardError("HOME contains control characters")
    if value and os.path.isabs(value):
        return Path(os.path.normpath(value))
    try:
        return Path(pwd.getpwuid(os.geteuid()).pw_dir)
    except KeyError as exc:  # pragma: no cover - a user without a passwd entry and without HOME
        raise DashboardError("cannot find your home directory: set HOME") from exc


def _xdg(env: Mapping[str, str], name: str, default: str, home: Path) -> Path:
    value = env.get(name, "")
    if value and not plain(value):
        raise DashboardError(f"{name} contains control characters")
    if value:
        lexical(value, name)
    # The XDG specification says a relative value is invalid and must be ignored.
    return Path(os.path.normpath(value)) if value and os.path.isabs(value) else home / default


@dataclass(frozen=True)
class Paths:
    home: Path
    config_dir: Path
    config_file: Path
    state_dir: Path
    services_dir: Path  # omp-services' configuration directory: where the drop-ins go
    relay_check_dir: Path  # the default of relay.checkDir: omp-private-relay's check directory
    control_dir: Path  # the default of control.dir: omp-session-control's directory

    @property
    def pairing_dir(self) -> Path:
        return self.config_dir / "pairing"

    @property
    def secrets_dir(self) -> Path:
        return self.config_dir / "secrets"

    @property
    def ntfy_topic_file(self) -> Path:
        return self.secrets_dir / "ntfy-topic"

    @property
    def audit_file(self) -> Path:
        return self.state_dir / "audit.jsonl"

    @property
    def services_dropin(self) -> Path:
        return self.services_dir / "services.d" / f"{APP}.json"

    @property
    def health_dropin(self) -> Path:
        return self.services_dir / "health.d" / f"{APP}.json"


def from_env(env: Mapping[str, str] | None = None) -> Paths:
    env = os.environ if env is None else env
    home = home_dir(env)
    config_dir = _xdg(env, "XDG_CONFIG_HOME", ".config", home) / APP
    override = env.get(CONFIG_FILE_ENV, "")
    if override:
        if not os.path.isabs(override) or not plain(override):
            raise DashboardError(f"{CONFIG_FILE_ENV} must be an absolute path without control characters")
        lexical(override, CONFIG_FILE_ENV)
        config_file = Path(os.path.normpath(override))
    else:
        config_file = config_dir / "config.json"
    xdg_config = _xdg(env, "XDG_CONFIG_HOME", ".config", home)
    return Paths(
        home=home,
        config_dir=config_dir,
        config_file=config_file,
        state_dir=_xdg(env, "XDG_STATE_HOME", ".local/state", home) / APP,
        services_dir=xdg_config / "omp-services",
        relay_check_dir=xdg_config / "omp-private-relay" / "check",
        control_dir=home / ".config" / "omp-session-control",
    )
