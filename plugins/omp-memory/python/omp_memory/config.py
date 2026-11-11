"""Where omp-memory looks for things: the banks, the ledger, backups, the safety state, and how folders are grouped.

Every setting has a default; the configuration file overrides the default, an environment variable overrides the file,
and a command line flag overrides everything. The file is `~/.config/omp-memory/config.json` (or under
$XDG_CONFIG_HOME, or the file named by $OMP_MEMORY_CONFIG), mode 0600, written by `omp-memory config set`.

Besides the path settings the file may hold `groups`: {group name: [root folder, ...]}, which sorts the banks'
folders into groups (see `omp-memory config set-group`). Without it a folder's group is the first directory below
your home that contains it (`~/projects/demo` -> `projects`).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .util import CONFIG_KIND, EXIT_USAGE, CliError, atomic_write_json, tilde

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Key:
    name: str
    env: tuple[str, ...]
    help: str
    executable: bool = False


KEYS: dict[str, Key] = {
    k.name: k
    for k in (
        Key(
            "banksDir",
            ("OMP_MEMORY_BANKS_DIR",),
            "omp's mnemopi banks: one directory per project folder, each with mnemopi.db",
        ),
        Key(
            "libDir",
            ("OMP_MEMORY_LIB_DIR",),
            "an install root (a directory with node_modules/@oh-my-pi/pi-mnemopi) to use instead of the automatic "
            "lookup; it must still be exactly omp's version (default lookup: omp's plugin directory, then the cache `setup` fills)",
        ),
        Key(
            "ledger",
            ("OMP_MEMORY_LEDGER",),
            "the importer's ledger (what was imported, tombstones); kept outside the banks",
        ),
        Key("backupsDir", ("OMP_MEMORY_BACKUPS_DIR",), "where `backup` writes <UTC stamp>/<bank>.db copies"),
        Key(
            "banksConf",
            ("OMP_MEMORY_BANKS_CONF",),
            "optional: a conf file of `<group> <root relative to home>` lines that sort folders into groups (used only when you set it)",
        ),
        Key(
            "stateFile",
            ("OMP_MEMORY_STATE_FILE",),
            "where `backup --apply` and `import dry-run` record what `import --apply` checks before it writes",
        ),
        Key(
            "labelsFile",
            ("OMP_MEMORY_LABELS_FILE",),
            "the folder labels file (bank label per folder; written by `labels set`)",
        ),
        Key("bun", ("OMP_MEMORY_BUN", "BUN_BIN"), "the Bun that runs the importer (1.4.4 or newer)", executable=True),
        Key(
            "ompBin",
            ("OMP_BIN",),
            "the omp binary whose version the importer checks the library against",
            executable=True,
        ),
    )
}
GROUPS_KEY = "groups"
GROUP_NAME_MAX = 60


def home() -> Path:
    return Path.home()


def config_path() -> Path:
    override = os.environ.get("OMP_MEMORY_CONFIG")
    if override:
        if not os.path.isabs(override):
            raise CliError("OMP_MEMORY_CONFIG must be an absolute path", EXIT_USAGE)
        return Path(override)
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base and os.path.isabs(base) else home() / ".config"
    return root / "omp-memory" / "config.json"


def default_banks_dir() -> Path:
    agent = os.environ.get("PI_CODING_AGENT_DIR")
    root = Path(agent) if agent and os.path.isabs(agent) else home() / ".omp" / "agent"
    return root / "memories" / "mnemopi" / "banks"


def defaults() -> dict[str, Path | None]:
    return {
        "banksDir": default_banks_dir(),
        "libDir": None,
        "ledger": home() / ".config" / "omp-memory-import" / "ledger.json",
        "backupsDir": home() / "Backups" / "omp-memory",
        "banksConf": None,
        "stateFile": home() / ".config" / "omp-memory" / "state.json",
        "labelsFile": home() / ".config" / "omp-memory" / "labels.json",
        "bun": None,
        "ompBin": None,
    }


def expand_path(name: str, value: str) -> Path:
    """An absolute path from `~/x` or `/x`; refuses a relative path, a `..` component and a NUL."""
    if not isinstance(value, str) or not value or "\0" in value:
        raise CliError(f"{name}: a path is required", EXIT_USAGE)
    text = str(home() / value[2:]) if value.startswith("~/") else value
    if not os.path.isabs(text):
        raise CliError(f"{name}: {value!r} is not an absolute path (use /abs/path or ~/path)", EXIT_USAGE)
    if ".." in Path(text).parts:
        raise CliError(f"{name}: a path with a '..' component is refused", EXIT_USAGE)
    return Path(text)


def flag_path(name: str, value: str) -> Path:
    """A path given on the command line: `~/x`, `/x`, or relative to the current directory."""
    if value and "\0" not in value and ".." in Path(value).parts:
        raise CliError(f"--{name}: a path with a '..' component is refused", EXIT_USAGE)  # before it is normalised away
    if value and "\0" not in value and not value.startswith("~/") and not os.path.isabs(value):
        return Path(os.path.abspath(value))
    return expand_path(f"--{name}", value)


def check_group(name: object, roots: object) -> list[str]:
    """What is wrong with one group entry ([] when it is fine): a name and a non-empty list of root folders."""
    problems = []
    if not isinstance(name, str) or not name.strip() or len(name) > GROUP_NAME_MAX:
        problems.append(f"group name {name!r} must be 1 to {GROUP_NAME_MAX} characters")
    elif any(ord(c) < 32 or ord(c) == 127 for c in name):
        problems.append(f"group name {name!r} has control characters")
    if not isinstance(roots, list) or not roots:
        problems.append(f"group {name!r} must be a non-empty list of folders")
        return problems
    for root in roots:
        if not isinstance(root, str) or not root.strip() or "\0" in root:
            problems.append(f"group {name!r} has an empty or invalid folder")
        elif ".." in Path(root).parts:
            problems.append(f"group {name!r}: a folder with a '..' component is refused")
    return problems


def read_file(path: Path | None = None) -> tuple[dict[str, str], dict[str, list[str]]]:
    """(settings, groups) stored in the configuration file (both empty when there is none); every problem is listed at once."""
    path = path or config_path()
    try:
        st = path.lstat()
    except FileNotFoundError:
        return {}, {}
    except OSError as err:
        raise CliError(f"cannot read {tilde(path)}: {err.strerror}", EXIT_USAGE) from None
    problems = []
    if path.is_symlink():
        problems.append("it is a symbolic link")
    if st.st_uid != os.getuid():
        problems.append("it is not owned by you")
    if st.st_mode & 0o022:
        problems.append("it is writable by group or others (chmod 600)")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise CliError(f"{tilde(path)} is not valid JSON ({type(err).__name__})", EXIT_USAGE) from None
    if not isinstance(data, dict):
        problems.append("the top level must be an object")
        data = {}
    if data.get("schemaVersion") != SCHEMA_VERSION:
        problems.append(f"schemaVersion must be {SCHEMA_VERSION}")
    out: dict[str, str] = {}
    groups: dict[str, list[str]] = {}
    for key, value in data.items():
        if key == "schemaVersion":
            continue
        if key == "kind":
            if value != CONFIG_KIND:
                problems.append(f"this is not an omp-memory configuration file (kind {value!r})")
            continue
        if key == GROUPS_KEY:
            if not isinstance(value, dict):
                problems.append(f"{GROUPS_KEY} must be an object of group name -> list of folders")
                continue
            for name, roots in value.items():
                found = check_group(name, roots)
                problems.extend(found)
                if not found:
                    groups[name] = [r.strip() for r in roots]
        elif key not in KEYS:
            problems.append(f"unknown key {key!r}")
        elif not isinstance(value, str) or not value:
            problems.append(f"{key} must be a non-empty string")
        else:
            out[key] = value
    if problems:
        raise CliError(f"{tilde(path)} is refused: " + "; ".join(problems), EXIT_USAGE)
    return out, groups


@dataclass(frozen=True)
class Setting:
    key: str
    value: Path | None
    source: str  # flag | env | config | default | unset
    env: str | None = None


class Config:
    """The resolved settings. `flags` maps a key to a value given on the command line (a path string)."""

    def __init__(self, flags: dict[str, str] | None = None):
        self.flags = {k: v for k, v in (flags or {}).items() if v is not None}
        self.file, self.file_groups = read_file()
        self.defaults = defaults()
        self._cache: dict[str, Setting] = {}

    def derive(self, **flags: str | None) -> Config:
        """A copy with more command line flags (None values are ignored); flags already set are kept unless replaced."""
        other = Config.__new__(Config)
        other.flags = {**self.flags, **{k: v for k, v in flags.items() if v is not None}}
        other.file, other.file_groups, other.defaults, other._cache = self.file, self.file_groups, self.defaults, {}
        return other

    def get(self, key: str) -> Setting:
        if key in self._cache:
            return self._cache[key]
        spec = KEYS[key]
        setting: Setting | None = None
        if key in self.flags:
            setting = Setting(key, flag_path(key, self.flags[key]), "flag")
        if setting is None:
            for name in spec.env:
                value = os.environ.get(name)
                if value:
                    setting = Setting(key, expand_path(f"${name}", value), "env", name)
                    break
        if setting is None and key in self.file:
            setting = Setting(key, expand_path(key, self.file[key]), "config")
        if setting is None:
            fallback = self.defaults[key]
            setting = Setting(key, fallback, "default" if fallback is not None else "unset")
        self._cache[key] = setting
        return setting

    def path(self, key: str) -> Path | None:
        return self.get(key).value

    def require(self, key: str, hint: str = "") -> Path:
        value = self.path(key)
        if value is None:
            raise CliError(f"{key} is not set. {hint}".strip(), EXIT_USAGE)
        return value

    def all(self) -> list[Setting]:
        return [self.get(key) for key in KEYS]

    def groups(self) -> dict[str, list[str]]:
        """The user's `groups` ({} when none): {group name: [root folders]}."""
        return dict(self.file_groups)


def write_file(file: Path, values: dict[str, str], groups: dict[str, list[str]]) -> None:
    data: dict[str, object] = {"kind": CONFIG_KIND, "schemaVersion": SCHEMA_VERSION, **dict(sorted(values.items()))}
    if groups:
        data[GROUPS_KEY] = dict(sorted(groups.items()))
    check_destinations(config_file=file)
    atomic_write_json(file, data, "config", banks_dir=Config().path("banksDir"))


def set_value(key: str, value: str) -> Path:
    """Store `key` in the configuration file (atomic, 0600); returns the file."""
    if key not in KEYS:
        raise CliError(f"unknown setting {key!r}; one of: {', '.join(KEYS)}", EXIT_USAGE)
    path = expand_path(key, value)
    if KEYS[key].executable and not (path.is_file() and os.access(path, os.X_OK)):
        raise CliError(f"{key}: {tilde(path)} is not an executable file", EXIT_USAGE)
    file = config_path()
    current, groups = read_file(file)
    current[key] = str(path)
    write_file(file, current, groups)
    return file


def unset_value(key: str) -> Path:
    if key not in KEYS:
        raise CliError(f"unknown setting {key!r}; one of: {', '.join(KEYS)}", EXIT_USAGE)
    file = config_path()
    current, groups = read_file(file)
    if key in current:
        del current[key]
        write_file(file, current, groups)
    return file


def set_group(name: str, roots: list[str]) -> Path:
    """Define (or replace) the group `name` with these root folders (`~/x`, `/x` or a path relative to your home)."""
    problems = check_group(name, roots)
    if problems:
        raise CliError("; ".join(problems), EXIT_USAGE)
    file = config_path()
    current, groups = read_file(file)
    groups[name.strip()] = [r.strip() for r in roots]
    write_file(file, current, groups)
    return file


def unset_group(name: str) -> Path:
    file = config_path()
    current, groups = read_file(file)
    if name not in groups:
        raise CliError(f"no group named {name!r}; defined: {', '.join(groups) or 'none'}", EXIT_USAGE)
    del groups[name]
    write_file(file, current, groups)
    return file


def check_destinations(config_file: Path | None = None, **extra: Path | None) -> None:
    """Refuse a setup in which two files this command line writes (state, labels, ledger, configuration) are the same file,
    by real path: one would be written over by the other."""
    cfg = Config()
    named = {
        "the configuration file": config_file or config_path(),
        "stateFile": cfg.path("stateFile"),
        "labelsFile": cfg.path("labelsFile"),
        "ledger": cfg.path("ledger"),
    }
    for label, value in extra.items():
        if value is not None:
            named[label] = value
    seen: dict[str, str] = {}
    for label, path in named.items():
        if path is None:
            continue
        real = os.path.realpath(path)
        if real in seen:
            raise CliError(
                f"{label} and {seen[real]} are the same file ({tilde(path)}); give them different paths", EXIT_USAGE
            )
        seen[real] = label
