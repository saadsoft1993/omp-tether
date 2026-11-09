"""Where everything lives. One place, so the CLI, the units and the tests agree.

    config   $XDG_CONFIG_HOME/omp-services   (default ~/.config/omp-services)   what you edit
             config.json, health.d/, services.d/, secrets/ (0700)
    data     $XDG_DATA_HOME/omp-services     (default ~/.local/share/omp-services)   what is installed
             releases/<id>/, current -> releases/<id>, bin/ (wrappers), state.json
    state    $XDG_STATE_HOME/omp-services    (default ~/.local/state/omp-services)    what the services write
             logs/, heartbeat-status.json, watchdog.json

The XDG defaults are used on macOS too (the other plugins of this family do the same). Each directory can be moved with
OMP_SERVICES_CONFIG_DIR, OMP_SERVICES_DATA_DIR and OMP_SERVICES_STATE_DIR; OMP_SERVICES_UNIT_DIR moves the directory
the launchd or systemd unit files are written to (for a scratch run).
"""

from __future__ import annotations

import os
import pwd
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .util import ServicesError

APP = "omp-services"


def _plain(value: str) -> bool:
    """No control characters: a path that reaches a unit file must not be able to start a new line in it."""
    return all(ord(c) >= 0x20 and ord(c) != 0x7F for c in value)


def _abs_env(env: Mapping[str, str], name: str) -> Path | None:
    value = env.get(name, "")
    if not value:
        return None
    if not os.path.isabs(value) or not _plain(value):
        raise ServicesError(f"{name} must be an absolute path without control characters")
    return Path(os.path.normpath(value))


def home_dir(env: Mapping[str, str]) -> Path:
    value = env.get("HOME", "")
    if value and not _plain(value):
        raise ServicesError("HOME contains control characters")
    if value and os.path.isabs(value):
        return Path(os.path.normpath(value))
    try:
        return Path(pwd.getpwuid(os.geteuid()).pw_dir)
    except KeyError as exc:  # pragma: no cover - a user without a passwd entry and without HOME
        raise ServicesError("cannot find your home directory: set HOME") from exc


def _xdg(env: Mapping[str, str], name: str, default: str, home: Path) -> Path:
    value = env.get(name, "")
    if value and not _plain(value):
        raise ServicesError(f"{name} contains control characters")
    # The XDG specification says a relative value is invalid and must be ignored.
    return Path(os.path.normpath(value)) if value and os.path.isabs(value) else home / default


@dataclass(frozen=True)
class Paths:
    home: Path
    config_dir: Path
    data_dir: Path
    state_dir: Path
    unit_dir: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.json"

    @property
    def secrets_dir(self) -> Path:
        return self.config_dir / "secrets"

    @property
    def health_d(self) -> Path:
        return self.config_dir / "health.d"

    @property
    def services_d(self) -> Path:
        return self.config_dir / "services.d"

    @property
    def default_url_file(self) -> Path:
        return self.secrets_dir / "heartbeat-url"

    @property
    def releases_dir(self) -> Path:
        return self.data_dir / "releases"

    @property
    def current(self) -> Path:
        return self.data_dir / "current"

    @property
    def bin_dir(self) -> Path:
        return self.data_dir / "bin"

    @property
    def state_file(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def logs_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def heartbeat_status(self) -> Path:
        return self.state_dir / "heartbeat-status.json"

    @property
    def watchdog_state(self) -> Path:
        return self.state_dir / "watchdog.json"

    @property
    def wrapper(self) -> Path:
        return self.bin_dir / APP

    def private_dirs(self) -> list[Path]:
        """Every directory this tool creates, parents first. All are mode 0700."""
        return [
            self.config_dir,
            self.secrets_dir,
            self.health_d,
            self.services_d,
            self.data_dir,
            self.releases_dir,
            self.bin_dir,
            self.state_dir,
            self.logs_dir,
        ]

    def roots(self) -> list[Path]:
        """The three directories `purge` may remove."""
        return [self.config_dir, self.data_dir, self.state_dir]

    @property
    def lock_file(self) -> Path:
        """The lock every mutating command takes: a file next to the data directory, never inside a directory purge may
        remove (see layout_problem)."""
        return self.data_dir.parent / f"{self.data_dir.name}.lock"

    def layout_problem(self) -> str | None:
        """Why this set of directories is unsafe, or None. The three roots must not contain one another (a purge of one
        would take another with it), and the lock file must not lie inside any of them (a purge would delete the very
        file that excludes other commands while it runs)."""
        real = [Path(os.path.realpath(r)) for r in self.roots()]
        names = ("config", "data", "state")
        for i, a in enumerate(real):
            for j in range(i + 1, len(real)):
                b = real[j]
                if a == b or a in b.parents or b in a.parents:
                    return f"the {names[i]} directory and the {names[j]} directory overlap ({a} and {b}): they must be separate"
        lock = Path(os.path.realpath(self.lock_file.parent)) / self.lock_file.name
        for name, root in zip(names, real, strict=False):
            if root == lock or root in lock.parents:
                return f"the lock file {lock} would lie inside the {name} directory, which purge may delete"
        return None

    def env_overrides(self, env: Mapping[str, str]) -> dict[str, str]:
        """What a service must carry so that it finds the same directories and the same effective settings we do: the
        resolved absolute roots (not the variables they came from), and the override variables in effect."""
        carried = {
            "OMP_SERVICES_CONFIG_DIR": str(self.config_dir),
            "OMP_SERVICES_DATA_DIR": str(self.data_dir),
            "OMP_SERVICES_STATE_DIR": str(self.state_dir),
            "OMP_SERVICES_UNIT_DIR": str(self.unit_dir),
        }
        if env.get("OMP_SERVICES_PLATFORM") in ("darwin", "linux"):
            carried["OMP_SERVICES_PLATFORM"] = env["OMP_SERVICES_PLATFORM"]
        return carried


def default_unit_dir(platform: str, home: Path, env: Mapping[str, str]) -> Path:
    if platform == "darwin":
        return home / "Library" / "LaunchAgents"
    return _xdg(env, "XDG_CONFIG_HOME", ".config", home) / "systemd" / "user"


def from_env(env: Mapping[str, str] | None = None, platform: str | None = None) -> Paths:
    env = os.environ if env is None else env
    home = home_dir(env)
    plat = platform or ("darwin" if sys.platform == "darwin" else "linux")
    return Paths(
        home=home,
        config_dir=_abs_env(env, "OMP_SERVICES_CONFIG_DIR") or _xdg(env, "XDG_CONFIG_HOME", ".config", home) / APP,
        data_dir=_abs_env(env, "OMP_SERVICES_DATA_DIR") or _xdg(env, "XDG_DATA_HOME", ".local/share", home) / APP,
        state_dir=_abs_env(env, "OMP_SERVICES_STATE_DIR") or _xdg(env, "XDG_STATE_HOME", ".local/state", home) / APP,
        unit_dir=_abs_env(env, "OMP_SERVICES_UNIT_DIR") or default_unit_dir(plat, home, env),
    )
