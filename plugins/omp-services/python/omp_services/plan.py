"""Plans: what `init`, `uninstall` and `purge` would do, shown first and applied only after a yes.

A plan is a list of steps, each with a one-line description and an action. Building a plan reads the machine and
changes nothing. Applying runs the steps in order and stops at the first failure; every step is safe to run again, so
the cure for a failed run is to run it again.

The rules that matter, kept in one place:
  * Only units whose label starts with the configured prefix and that this tool wrote (recorded in state.json, or
    carrying its marker comment) are ever loaded, reloaded or removed.
  * A unit that is already loaded and whose rendered text is unchanged is left running.
  * A changed unit is reloaded (stop, rewrite, start), except one flagged `protected`: that one is never reloaded,
    restarted or removed unless the person names it (`--restart-protected NAME`).
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shlex
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config as config_mod
from . import console, dropins, heartbeat, runtime, units
from .config import Config, load_config
from .hostplat import manager_kind, system
from .manager import ServiceManager, make_manager
from .paths import Paths
from .paths import from_env as paths_from_env
from .util import (
    ServicesError,
    dir_problem,
    ensure_private_dir,
    read_text,
    remove_file,
    sha256_text,
    write_file,
)

MANAGED_MARKER = units.MANAGED_MARKER


@dataclass
class Step:
    verb: str
    subject: str
    run: Callable[[], str | None]
    outside: bool = False  # touches something outside omp-services' own config, data and state directories
    payload: str = ""  # the exact content this step writes or the exact target it acts on (bound into the plan digest)
    # the unit record this step leaves in the state file (None: the record is removed): bound into the digest
    unit_record: tuple[str, dict[str, Any] | None] | None = None


@dataclass
class Plan:
    title: str
    steps: list[Step] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [self.title, ""]
        own = [s for s in self.steps if not s.outside]
        outside = [s for s in self.steps if s.outside]
        if own:
            lines.append("In omp-services' own folders (config, data, state):")
            lines += [f"  {s.verb:<9} {s.subject}" for s in own]
            lines.append("")
        if outside:
            lines.append("Outside them (your service manager):")
            lines += [f"  {s.verb:<9} {s.subject}" for s in outside]
            lines.append("")
        if not own and not outside:
            lines += ["Nothing to do: everything is already as it should be.", ""]
        for warning in self.warnings:
            lines.append(f"warning: {warning}")
        for note in self.notes:
            lines.append(f"note: {note}")
        return "\n".join(lines).rstrip() + "\n"

    @property
    def changes(self) -> bool:
        return bool(self.steps)

    def digest(self) -> str:
        """A digest of the plan as displayed *and* of every payload behind it: the exact unit files, wrapper scripts,
        configuration text, release id and deletion targets that will be written or acted on. Two plans that read the
        same but would write different bytes have different digests, so `--expect-plan` approves actions, not prose."""
        manifest = json.dumps(
            [[s.verb, s.subject, s.payload, s.outside, s.unit_record] for s in self.steps], sort_keys=True
        )
        return hashlib.sha256((self.render() + "\0" + manifest).encode("utf-8")).hexdigest()

    def apply(self, say: Callable[[str], None]) -> None:
        for step in self.steps:
            result = step.run()
            say(f"  done: {step.verb} {step.subject}" + (f" ({result})" if result else ""))


@dataclass
class Setup:
    """Everything a command needs: where things are, the configuration, the service manager."""

    env: Mapping[str, str]
    paths: Paths
    cfg: Config
    plat: str
    manager: ServiceManager
    root: Path  # the code being installed (the plugin tree, or a release directory)

    @classmethod
    def load(
        cls, env: Mapping[str, str] | None = None, *, root: Path | None = None, manager: ServiceManager | None = None
    ) -> Setup:
        env = os.environ if env is None else env
        plat = system(env)
        paths = paths_from_env(env, plat)
        cfg = load_config(paths, env)
        return cls(
            env, paths, cfg, plat, manager or make_manager(manager_kind(plat), env), root or runtime.plugin_root()
        )

    def show(self, path: Path | str) -> str:
        return console.tidy(path, self.paths.home)


@dataclass
class DesiredUnit:
    spec: units.ServiceSpec
    path: Path
    text: str


def desired_units(setup: Setup, python_path: str, omp_path: str | None) -> tuple[list[DesiredUnit], list[str]]:
    """The units that should exist, and why some that a drop-in asks for cannot."""
    cfg, paths = setup.cfg, setup.paths
    carried = units.carried_env(paths, cfg, setup.env)
    specs: list[units.ServiceSpec] = []
    warnings: list[str] = []
    if cfg.heartbeat["enabled"]:
        specs.append(units.heartbeat_spec(cfg.label_prefix, paths, python_path, omp_path, carried))
    drops, problems = dropins.load_dir("services", paths.services_d)
    warnings += problems
    for drop in drops:
        if not drop.ok:
            warnings.append(f"services.d/{drop.plugin}.json is not used: {drop.errors[0]}")
            continue
        assert drop.data is not None
        try:
            specs += units.specs_from_dropin(drop.data, cfg.label_prefix, paths, python_path, omp_path, carried)
        except ServicesError as exc:
            warnings.append(f"{exc}; its services are not installed")
    labels = [s.label for s in specs]
    for label in sorted({x for x in labels if labels.count(x) > 1}):
        raise ServicesError(f"two services render to the label {label}: names must be unique within a plugin")
    kind = setup.manager.kind
    return [
        DesiredUnit(s, setup.manager.unit_file(paths.unit_dir, s.label), units.render(s, kind)) for s in specs
    ], warnings


def _read_if_file(path: Path) -> str | None:
    try:
        return read_text(path)
    except ServicesError:
        return None


def unit_path(setup: Setup, label: str) -> Path:
    """Where the unit of `label` lives: derived from the label and the unit directory, never read from a file."""
    return setup.manager.unit_file(setup.paths.unit_dir, label)


def classify_unit(setup: Setup, state: Mapping[str, Any], label: str) -> tuple[str, str | None]:
    """('absent' | 'ours' | 'foreign', text). A unit file is ours when it carries our marker comment, or when it is
    byte for byte what the state file recorded for that label. Called again at the moment of every action."""
    path = unit_path(setup, label)
    if not os.path.lexists(path):
        return "absent", None
    try:
        text = read_text(path)
    except ServicesError:
        return "foreign", None
    if units.is_managed_text(text):
        return "ours", text
    info = state.get("units", {}).get(label)
    if isinstance(info, dict) and info.get("sha256") == sha256_text(text):
        return "ours", text
    return "foreign", text


def owned_labels(setup: Setup, state: Mapping[str, Any]) -> dict[str, Path]:
    """Every label of ours: those recorded in the state file plus files in the unit directory that carry both our prefix
    and our marker, minus any whose unit file is no longer ours. The path is derived from the label (a path stored in
    the state file is ignored). Returns {label: unit file}."""
    prefix = setup.cfg.label_prefix
    candidates: set[str] = {label for label in state.get("units", {}) if units.owns_label(prefix, label)}
    unit_dir = setup.paths.unit_dir
    suffix = setup.manager.suffix
    if unit_dir.is_dir():
        for entry in unit_dir.iterdir():
            if entry.name.startswith(prefix + ".") and entry.name.endswith(suffix) and not entry.is_symlink():
                label = entry.name[: -len(suffix)]
                if units.owns_label(prefix, label):
                    candidates.add(label)
    return {
        label: unit_path(setup, label)
        for label in sorted(candidates)
        if classify_unit(setup, state, label)[0] in {"ours", "absent"}
    }


def foreign_labels(setup: Setup, state: Mapping[str, Any]) -> list[str]:
    """Labels the state file records whose unit file is no longer ours."""
    return sorted(
        label
        for label in state.get("units", {})
        if units.owns_label(setup.cfg.label_prefix, label) and classify_unit(setup, state, label)[0] == "foreign"
    )


def protected_names(setup: Setup, labels: set[str]) -> set[str]:
    """Which of `labels` are flagged protected in services.d (the state file remembers it too)."""
    flagged: set[str] = set()
    drops, _ = dropins.load_dir("services", setup.paths.services_d)
    for drop in drops:
        if drop.ok and drop.data:
            for svc in drop.data["services"]:
                label = units.label_for(setup.cfg.label_prefix, drop.plugin, svc["name"])
                if label in labels and svc.get("protected"):
                    flagged.add(label)
    return flagged


def _guarded_labels(setup: Setup, state: Mapping[str, Any]) -> set[str]:
    """The labels that are never stopped, reloaded or removed unless the user names them: those declared `protected`, those
    the state file remembers as protected, and those it has no record of at all. A unit of ours that the state file does
    not mention (the state was lost, or the unit is older than the record) has an unknown history, and unknown is not the
    same as unprotected."""
    owned = set(owned_labels(setup, state))
    remembered = {label for label, info in state.get("units", {}).items() if info.get("protected")}
    unrecorded = owned - set(state.get("units", {}))
    return protected_names(setup, owned) | remembered | unrecorded


def _matches(label: str, prefix: str, names: set[str]) -> bool:
    return bool(names) and (label in names or label[len(prefix) + 1 :] in names)


# ---------------------------------------------------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------------------------------------------------


def plan_init(
    setup: Setup, state: dict[str, Any], *, repin: bool = False, restart_protected: set[str] | None = None
) -> Plan:
    """What `init` would do now. `state` is mutated by the steps as they run and saved after each unit."""
    before_state = copy.deepcopy(state)
    cfg, paths, manager = setup.cfg.require_valid(), setup.paths, setup.manager
    allowed = restart_protected or set()
    shown = setup.show
    plan = Plan(f"omp-services init: {setup.plat}, {manager.kind}, label prefix {cfg.label_prefix}")

    # Interpreters and omp
    python_path = runtime.choose_python(cfg, state)
    omp_path = runtime.choose_omp(cfg, state, setup.env, paths.home, repin=repin)
    omp_info: dict[str, Any] = {"path": omp_path}
    if omp_path is None:
        plan.warnings.append(
            "no omp binary was found (OMP_BIN, PATH, ~/.local/bin/omp): the omp and bun wrappers are not written and "
            "services that run omp scripts cannot be installed. Install omp, then run init again."
        )
    else:
        version = runtime.omp_version(omp_path)
        omp_info["version"] = ".".join(map(str, version)) if version else None
        if version is None:
            plan.warnings.append(f"{shown(omp_path)} did not report a version; check that it runs")
        elif not runtime.at_least(version):
            plan.warnings.append(
                f"omp {'.'.join(map(str, version))} is older than the supported minimum "
                f"{'.'.join(map(str, runtime.MIN_OMP))}"
            )

    # Directories
    roots = set(paths.roots())
    for directory in paths.private_dirs():
        problem = dir_problem(directory)
        if problem == "does not exist":
            plan.steps.append(
                Step(
                    "create",
                    f"{shown(directory)} (mode 0700)",
                    _make_dir(directory, roots),
                    payload=f"{directory}:0700",
                )
            )
        elif problem and problem.startswith("has mode"):
            plan.steps.append(
                Step(
                    "tighten",
                    f"{shown(directory)} to mode 0700",
                    _make_dir(directory, roots),
                    payload=f"{directory}:0700",
                )
            )
        elif problem:
            raise ServicesError(f"{directory} {problem}; fix or remove it, then run init again")
        elif directory in roots and not runtime.has_marker(directory):
            plan.notes.append(
                f"{shown(directory)} already existed, so it is left unmarked: `purge` deletes only the entries init creates "
                "in it (recorded in .omp-services-entries) and leaves the directory and everything else in it"
            )

    if not paths.config_file.exists():
        plan.steps.append(
            Step(
                "write",
                f"{shown(paths.config_file)} (all defaults; nothing is on)",
                _write_default_config(paths),
                payload=config_mod.default_config_text(),
            )
        )

    # Code release, wrappers
    rid = runtime.release_id(setup.root)
    before = runtime.current_release(paths)
    if before == rid and not runtime.verify_release(paths.current / "", rid):
        raise ServicesError(
            f"the active release {rid} does not match its name (changed or damaged): remove {paths.releases_dir / rid} and run init again"
        )
    if before != rid:
        plan.steps.append(
            Step(
                "install",
                f"code release {rid} into {shown(paths.releases_dir)}",
                _install_release(setup, state, rid),
                payload=f"{setup.root}:{rid}",
            )
        )
    wanted = runtime.wrapper_texts(paths, python_path, omp_path)
    for name, text in sorted(wanted.items()):
        target = paths.bin_dir / name
        exists = os.path.lexists(target)
        current = _read_if_file(target) if exists else None
        if exists and current is None:  # unreadable is not absent: a link, a directory, a file we cannot read
            plan.warnings.append(f"{shown(target)} cannot be read as a file: left alone, the wrapper is not installed")
        elif current is not None and runtime.WRAPPER_HEADER not in current[:200]:
            plan.warnings.append(
                f"{shown(target)} was not written by omp-services: left alone, the wrapper is not installed"
            )
        elif current != text:
            plan.steps.append(
                Step(
                    "write",
                    f"wrapper {shown(target)} (runs {_wrapper_summary(name, python_path, omp_path)})",
                    _write_wrapper(target, text),
                    payload=text,
                )
            )

    # Units
    desired, warnings = desired_units(setup, python_path, omp_path)
    plan.warnings += warnings
    wanted_labels = {d.spec.label for d in desired}
    flagged = _guarded_labels(setup, state)
    for label in foreign_labels(setup, state):
        plan.warnings.append(
            f"{label} is recorded as installed, but its unit file is not managed by omp-services any more: left alone"
        )
    if desired or owned_labels(setup, state):
        trouble = manager.problem()
        if trouble:
            plan.warnings.append(f"the service manager is not usable here: {trouble}")
    for item in desired:
        _plan_unit(setup, plan, state, item, allowed)
    for label, file in sorted(owned_labels(setup, state).items()):
        if label in wanted_labels:
            continue
        if label in flagged and not _matches(label, cfg.label_prefix, allowed):
            plan.warnings.append(
                f"{label} is no longer wanted but is protected: it stays loaded. "
                f"Remove it with `uninstall --include-protected` or `init --restart-protected {label[len(cfg.label_prefix) + 1 :]}`."
            )
            continue
        if manager.transitional(label):
            plan.warnings.append(f"{label} is changing state: left alone. Run init again once it has settled.")
            continue
        plan.steps.append(
            Step(
                "remove",
                f"unit {label} (stop it, delete {shown(file)})",
                _remove_unit(setup, state, label, file),
                outside=True,
                payload=f"{label}:{file}:{sha256_text(classify_unit(setup, state, label)[1] or '')}",
                unit_record=(label, None),
            )
        )

    over = heartbeat.oversized_logs(paths.logs_dir, cfg.log_max_bytes)
    if over:
        plan.steps.append(
            Step(
                "trim",
                f"{len(over)} service log(s) above {cfg.log_max_bytes} bytes",
                _trim(paths, cfg.log_max_bytes),
                payload=",".join(str(p) for p in over),
            )
        )
    fields = {
        "release": rid,
        "version": runtime.read_version(setup.root),
        "python": python_path,
        "omp": omp_info,
        "manager": manager.kind,
        "unitDir": str(paths.unit_dir),
        "labelPrefix": cfg.label_prefix,
    }
    changed = state.get("units") != before_state.get("units") or any(
        before_state.get(k) != v for k, v in fields.items()
    )
    if plan.steps or changed:
        # The complete unit records the state file will hold afterwards (not only the fields), so that a change to what
        # is remembered, such as protection, changes the digest.
        projected = {label: dict(info) for label, info in state.get("units", {}).items()}
        for step in plan.steps:
            if step.unit_record:
                label, record = step.unit_record
                if record is None:
                    projected.pop(label, None)
                else:
                    projected[label] = dict(record)
        plan.steps.append(
            Step(
                "record",
                f"what was installed in {shown(paths.state_file)}",
                _record(setup, state, fields),
                payload=json.dumps({"fields": fields, "units": projected}, sort_keys=True),
            )
        )
    # Old releases: the exact list is part of the plan (nothing is enumerated again when it runs).
    previous = before if before and before != rid else state.get("previousRelease")
    keep = sorted(r for r in (rid, previous) if r)
    pruned = runtime.prune_targets(paths, set(keep))
    if pruned:
        plan.steps.append(
            Step(
                "prune",
                f"{len(pruned)} old code release(s) in {shown(paths.releases_dir)}: {', '.join(pruned)}",
                _prune(setup, pruned, set(keep)),
                payload=json.dumps({"remove": pruned, "keep": keep}),
            )
        )
    if manager.kind == "systemd":
        note = manager.linger_note()
        if note:
            plan.notes.append(note)
    if plat_note := _launchd_note(setup):
        plan.notes.append(plat_note)
    return plan


def _launchd_note(setup: Setup) -> str | None:
    if setup.manager.kind == "launchd":
        return "LaunchAgents run while you are logged in to the desktop; they are not started by an ssh session alone."
    return None


def _plan_unit(setup: Setup, plan: Plan, state: dict[str, Any], item: DesiredUnit, allowed: set[str]) -> None:
    manager, prefix, shown = setup.manager, setup.cfg.label_prefix, setup.show
    spec = item.spec
    kind, existing = classify_unit(setup, state, spec.label)
    loaded = manager.is_loaded(spec.label)
    prior = state.get("units", {}).get(spec.label)
    named = _matches(spec.label, prefix, allowed)
    if kind == "foreign" or (kind == "absent" and loaded and prior is None):
        plan.warnings.append(
            f"{spec.label} already exists and is not managed by omp-services: it was not touched, and the service "
            "the plugin declared is not installed. Remove or rename the existing unit, then run init again."
        )
        return
    # Protection is remembered from what was installed: dropping the flag from a declaration does not lift it.
    if loaded and manager.transitional(spec.label):
        plan.warnings.append(
            f"{spec.label} is changing state (activating, reloading or deactivating): left alone. Run init again once it has settled."
        )
        return
    protected = spec.protected or bool(prior and prior.get("protected"))
    unrecorded = prior is None and kind == "ours" and loaded  # a live unit of ours with no record: history unknown
    if unrecorded:
        plan.notes.append(
            f"{spec.label} is running but the state file has no record of it: it is treated as protected (not reloaded) "
            f"unless you name it with `init --restart-protected {spec.label[len(prefix) + 1 :]}`"
        )
    if existing == item.text:
        if loaded:
            _remember(state, item, protected)  # keep the record complete even when nothing changes
            plan.notes.append(f"{spec.label} is unchanged and already loaded: left running")
            return
        plan.steps.append(
            Step(
                "load",
                f"unit {spec.label} (already written; runs {_runs(spec)})",
                _install_unit(setup, state, item, reload=False, named=named),
                outside=True,
                payload=item.text,
                unit_record=_unit_record(item, spec.protected if named else protected),
            )
        )
        return
    if loaded and (protected or unrecorded) and not named:
        plan.warnings.append(
            f"{spec.label} changed (or its file is missing) but is protected, so it was not rewritten or reloaded. "
            f"Run `init --restart-protected {spec.label[len(prefix) + 1 :]}` when stopping it is acceptable."
        )
        return
    verb = "update" if existing is not None else "install"
    suffix = "reload (stop, rewrite, start)" if loaded else "write and load"
    plan.steps.append(
        Step(
            verb,
            f"unit {spec.label}: {suffix}, file {shown(item.path)}; runs {_runs(spec)}",
            _install_unit(setup, state, item, reload=loaded, named=named),
            outside=True,
            payload=item.text + "\0" + sha256_text(existing or ""),
            unit_record=_unit_record(item, spec.protected if named else protected),
        )
    )


def _unit_record(item: DesiredUnit, protected: bool) -> tuple[str, dict[str, Any]]:
    spec = item.spec
    record = {"sha256": sha256_text(item.text), "plugin": spec.plugin, "name": spec.name, "protected": protected}
    return spec.label, record


def _runs(spec: units.ServiceSpec) -> str:
    """What a unit will run, shown in the plan: the program and its arguments, and a digest of its environment."""
    env_digest = sha256_text(json.dumps(spec.env))[:10]
    return (
        " ".join(shlex.quote(a) for a in spec.argv) + f" (environment {env_digest}, working directory {spec.workdir})"
    )


def _wrapper_summary(name: str, python_path: str, omp_path: str | None) -> str:
    if name == "omp-services":
        return f"{python_path} on the installed code"
    return omp_path or "omp"


def _remember(state: dict[str, Any], item: DesiredUnit, protected: bool) -> None:
    state.setdefault("units", {})[item.spec.label] = {
        "sha256": sha256_text(item.text),
        "plugin": item.spec.plugin,
        "name": item.spec.name,
        "protected": protected,
    }


def _make_dir(directory: Path, roots: set[Path]) -> Callable[[], str | None]:
    def run() -> str | None:
        existed = os.path.lexists(directory)
        ensure_private_dir(directory)
        # The marker means "created by omp-services": it is written in the step that creates the directory, never
        # into one that was already there.
        if directory in roots and not existed:
            runtime.write_marker(directory)
        elif not existed:
            for root in roots:
                runtime.note_created(root, directory)
        return None

    return run


def _write_default_config(paths: Paths) -> Callable[[], str | None]:
    def run() -> str | None:
        created = not os.path.lexists(paths.config_file)
        write_file(paths.config_file, config_mod.default_config_text(), 0o600)
        if created:
            runtime.note_created(paths.config_dir, paths.config_file)
        return None

    return run


def _install_release(setup: Setup, state: dict[str, Any], rid: str) -> Callable[[], str | None]:
    def run() -> str | None:
        runtime.install_release(setup.paths, setup.root)
        before = runtime.current_release(setup.paths)
        runtime.activate_release(setup.paths, rid)
        if before and before != rid:
            state["previousRelease"] = before
        state["release"] = rid
        return None

    return run


def _write_wrapper(target: Path, text: str) -> Callable[[], str | None]:
    def run() -> str | None:
        runtime.write_wrapper(target, text)
        return None

    return run


def _install_unit(
    setup: Setup, state: dict[str, Any], item: DesiredUnit, *, reload: bool, named: bool = False
) -> Callable[[], str | None]:
    def run() -> str | None:
        manager = setup.manager
        label = item.spec.label
        # Ownership is established again here, at the moment of action, not trusted from when the plan was made.
        if classify_unit(setup, state, label)[0] == "foreign":
            raise ServicesError(f"{label} is not managed by omp-services; refusing to replace it")
        prior = state.get("units", {}).get(label)
        protected = item.spec.protected if named else bool(item.spec.protected or (prior and prior.get("protected")))
        if reload:
            manager.unload(label, item.path)
        item.path.parent.mkdir(parents=True, exist_ok=True)
        write_file(item.path, item.text, 0o644)
        manager.load(label, item.path)
        _remember(state, item, protected)
        runtime.save_state(setup.paths, state)
        return "reloaded" if reload else "loaded"

    return run


def _remove_unit(setup: Setup, state: dict[str, Any], label: str, file: Path) -> Callable[[], str | None]:
    def run() -> str | None:
        if not units.owns_label(setup.cfg.label_prefix, label):  # belt and braces: never touch a foreign label
            raise ServicesError(f"refusing to touch {label}: it does not carry the prefix {setup.cfg.label_prefix}")
        file = unit_path(setup, label)  # derived from the label, whatever the plan was built with
        if classify_unit(setup, state, label)[0] == "foreign":
            raise ServicesError(f"{label} is not managed by omp-services; refusing to remove it")
        setup.manager.unload(label, file)
        remove_file(file)
        refresh = getattr(setup.manager, "refresh", None)
        if refresh:
            refresh()
        state.get("units", {}).pop(label, None)
        runtime.save_state(setup.paths, state)
        return None

    return run


def _trim(paths: Paths, max_bytes: int) -> Callable[[], str | None]:
    def run() -> str | None:
        done = heartbeat.trim_logs(paths.logs_dir, max_bytes)
        return ", ".join(done) if done else None

    return run


def _record(setup: Setup, state: dict[str, Any], fields: dict[str, Any]) -> Callable[[], str | None]:
    def run() -> str | None:
        state.update(fields)
        state["release"] = runtime.current_release(setup.paths) or fields["release"]
        runtime.save_state(setup.paths, state)
        return None

    return run


def _prune(setup: Setup, names: list[str], keep: set[str]) -> Callable[[], str | None]:
    """Remove exactly the releases the plan listed: nothing is enumerated again at apply time."""

    def run() -> str | None:
        removed = runtime.remove_releases(setup.paths, names, keep)
        return f"removed {len(removed)} of {len(names)}"

    return run


# ---------------------------------------------------------------------------------------------------------------------
# uninstall and purge
# ---------------------------------------------------------------------------------------------------------------------


def plan_uninstall(
    setup: Setup, state: dict[str, Any], *, include_protected: bool = False, title: str = "omp-services uninstall"
) -> Plan:
    """Remove everything that was installed (units, wrappers, code releases, state). The configuration, the drop-ins,
    the secrets, the logs and the heartbeat record stay: that is your data. Nothing is followed through a link, and
    only files and directories that look like this tool's own are deleted."""
    paths, shown = setup.paths, setup.show
    for directory in (paths.releases_dir, paths.bin_dir):
        problem = dir_problem(directory) if os.path.lexists(directory) else None
        if problem:
            raise ServicesError(f"{directory} {problem}: refusing to delete anything inside it")
    plan = Plan(f"{title}: keeps your configuration, drop-ins, secrets and logs")
    owned = owned_labels(setup, state)
    flagged = _guarded_labels(setup, state)
    for label in foreign_labels(setup, state):
        plan.warnings.append(f"{label} is not managed by omp-services any more: left in place")
    survivors = 0
    for label, file in sorted(owned.items()):
        if label in flagged and not include_protected:
            plan.warnings.append(
                f"{label} is protected: left loaded and in place. Add --include-protected to remove it too."
            )
            survivors += 1
            continue
        if setup.manager.transitional(label):
            plan.warnings.append(f"{label} is changing state: left in place. Run uninstall again once it has settled.")
            survivors += 1
            continue
        plan.steps.append(
            Step(
                "remove",
                f"unit {label} (stop it, delete {shown(file)})",
                _remove_unit(setup, state, label, file),
                outside=True,
            )
        )
    if survivors:
        # What a surviving protected unit may still need (the wrappers, the code release) and what remembers its
        # protection (the state file) stays.
        plan.notes.append(
            "wrappers, code releases and the state file stay: protected units that are left still need them"
        )
        return plan
    for name in ("omp-services", "omp", "bun"):
        target = paths.bin_dir / name
        text = _read_if_file(target) if target.is_file() and not target.is_symlink() else None
        if text is not None and runtime.WRAPPER_HEADER in text[:200]:
            plan.steps.append(Step("delete", f"wrapper {shown(target)}", _delete_file(target)))
        elif target.exists() or target.is_symlink():
            plan.notes.append(f"{shown(target)} was not written by omp-services: kept")
    if paths.current.is_symlink():
        plan.steps.append(Step("delete", f"code link {shown(paths.current)}", _delete_file(paths.current)))
    directory = paths.releases_dir
    mine = [e for e in directory.iterdir() if runtime.is_owned_release(e)] if os.path.lexists(directory) else []
    if mine:
        plan.steps.append(
            Step(
                "empty",
                f"{shown(directory)} ({len(mine)} created by omp-services)",
                _remove_matching(directory, runtime.is_owned_release),
                payload=",".join(sorted(e.name for e in mine)),
            )
        )
    if paths.state_file.exists():
        plan.steps.append(Step("delete", shown(paths.state_file), _delete_file(paths.state_file)))
    if not paths.config_file.exists() and not owned:
        plan.notes.append("no configuration was ever written here")
    return plan


def _delete_file(path: Path) -> Callable[[], str | None]:
    def run() -> str | None:
        remove_file(path)
        return None

    return run


def _remove_matching(directory: Path, owned: Callable[[Path], bool]) -> Callable[[], str | None]:
    """Delete the entries of `directory` that this tool created (name and marker file); leave everything else."""

    def run() -> str | None:
        if dir_problem(directory):
            raise ServicesError(f"{directory} changed since the plan was made")
        for entry in directory.iterdir():
            if owned(entry):
                shutil.rmtree(entry)
        return None

    return run


def purge_problems(setup: Setup) -> list[str]:
    """Reasons `purge` must refuse: a directory it did not create, a link, a directory that contains your home."""
    problems: list[str] = []
    home = setup.paths.home
    for root in setup.paths.roots():
        if not root.exists() and not root.is_symlink():
            continue
        problem = dir_problem(root, forbid=0o000)
        if problem:
            problems.append(f"{root} {problem}")
        elif root == home or root in home.parents or root == Path("/"):
            problems.append(f"{root} is, or contains, your home directory")
        elif not runtime.has_marker(root):
            try:
                recorded = runtime.recorded_entries(root)
            except ServicesError as exc:
                problems.append(str(exc))
                continue
            if not (root / runtime.ENTRIES_NAME).exists():
                problems.append(
                    f"{root} was not created by omp-services (it has no {'.omp-services-dir'} marker) and init recorded nothing in it: remove it by hand if you want it gone"
                )
            elif not recorded:
                problems.append(f"{root} has an empty record of what omp-services created in it: remove it by hand")
    return problems


def plan_purge(setup: Setup, state: dict[str, Any], *, include_protected: bool = False) -> Plan:
    problems = purge_problems(setup)
    if problems:
        raise ServicesError("purge refused:\n  " + "\n  ".join(problems))
    plan = plan_uninstall(setup, state, include_protected=include_protected, title="omp-services purge")
    if plan.warnings and any("protected" in w for w in plan.warnings):
        raise ServicesError(
            "purge refused: protected units are still installed; add --include-protected to stop and remove them too"
        )
    for root in setup.paths.roots():
        if not root.exists():
            continue
        if runtime.has_marker(root):
            plan.steps.append(
                Step("delete", f"{setup.show(root)} and everything in it (including secrets)", _delete_tree(root))
            )
        else:
            names = runtime.recorded_entries(root)
            plan.steps.append(
                Step(
                    "delete",
                    f"from {setup.show(root)}, which omp-services did not create, only what init created in it: "
                    f"{', '.join(names)} (the directory and everything else in it stay)",
                    _delete_recorded(root, names),
                    payload=f"{root}:{','.join(names)}",
                )
            )
    return plan


def _delete_recorded(root: Path, names: list[str]) -> Callable[[], str | None]:
    """Delete exactly the recorded entries of a directory this tool did not create, then the record, and the directory
    only when that leaves it empty."""

    def run() -> str | None:
        if dir_problem(root, forbid=0o000) or runtime.has_marker(root):
            raise ServicesError(f"refusing to delete inside {root}: it changed since the plan was made")
        if runtime.recorded_entries(root) != names:
            raise ServicesError(f"refusing to delete inside {root}: the record of what omp-services created changed")
        for name in names:
            target = root / name
            if os.path.islink(target) or target.is_file():
                os.unlink(target)
            elif target.is_dir():
                shutil.rmtree(target)
        os.unlink(root / runtime.ENTRIES_NAME)
        try:
            root.rmdir()
        except OSError:
            pass  # something that is not ours is in it: it stays
        return None

    return run


def _delete_tree(root: Path) -> Callable[[], str | None]:
    def run() -> str | None:
        # Re-checked at the moment of deletion: nothing was swapped for a link or someone else's directory.
        if dir_problem(root, forbid=0o000) or not runtime.has_marker(root):
            raise ServicesError(f"refusing to delete {root}: it changed since the plan was made")
        shutil.rmtree(root)
        return None

    return run
