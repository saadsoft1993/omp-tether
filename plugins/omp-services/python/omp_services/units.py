"""Service definitions and the unit files rendered from them (launchd property lists, systemd user units).

A service is a long-running program the service manager keeps alive. `services.d/<plugin>.json` drop-ins (protocol
services/1) declare them; the built-in heartbeat is one more. Rendering is a pure function of the definition and the
pinned paths, so "has this unit changed?" is a string comparison and a rerun of `init` changes nothing that did not
change.

Every unit gets: the pinned absolute program, a small fixed PATH, no shell, Umask 077, keep-alive with a throttle, and
its output in a log file under the state directory (never inside a checkout).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from .paths import Paths
from .util import ServicesError

HEARTBEAT = "heartbeat"
ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]*")
MANAGED_MARKER = "Managed by omp-services"
RESERVED_ENV = {
    "PATH",
    "HOME",
    "BUN_BE_BUN",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH",
}
SYSTEM_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
UMASK_077 = 0o077


@dataclass(frozen=True)
class ServiceSpec:
    plugin: str
    name: str
    label: str
    argv: tuple[str, ...]
    env: tuple[tuple[str, str], ...]
    workdir: str
    description: str
    protected: bool
    throttle: int
    log: str
    builtin: bool = False
    needs: tuple[str, ...] = ()  # files that must exist for the service to start: the program and its script


def label_for(prefix: str, plugin: str, name: str) -> str:
    return f"{prefix}.{plugin}.{name}"


def heartbeat_label(prefix: str) -> str:
    return f"{prefix}.{HEARTBEAT}"


def is_managed_text(text: str) -> bool:
    """True when a unit file carries the marker comment this tool writes into every unit."""
    return MANAGED_MARKER in text[:600]


def owns_label(prefix: str, label: str) -> bool:
    """True for a label this tool may manage: it starts with '<prefix>.' and has a plain remainder."""
    return label.startswith(prefix + ".") and re.fullmatch(r"[a-z0-9][a-z0-9.-]*", label[len(prefix) + 1 :]) is not None


def service_path(paths: Paths, omp_path: str | None) -> str:
    """PATH for a service: our wrappers first (a `bun` that is omp's embedded Bun, an `omp` pinned by path), then the
    directory of the pinned omp, then the system directories."""
    parts = [str(paths.bin_dir)]
    if omp_path:
        parts.append(str(Path(omp_path).parent))
    parts.append(SYSTEM_PATH)
    seen: list[str] = []
    for part in parts:
        if part not in seen:
            seen.append(part)
    return ":".join(seen)


def _check_env(extra: Mapping[str, str], where: str) -> None:
    for name in extra:
        if not ENV_NAME.fullmatch(name):
            raise ServicesError(f"{where}: environment name {name!r} must match [A-Z_][A-Z0-9_]*")
        if name in RESERVED_ENV or name.startswith("OMP_SERVICES_"):
            raise ServicesError(f"{where}: environment name {name} is reserved")


def heartbeat_spec(
    prefix: str, paths: Paths, python_path: str, omp_path: str | None, carried: Mapping[str, str]
) -> ServiceSpec:
    env = {"PATH": service_path(paths, omp_path), **carried}
    return ServiceSpec(
        plugin="omp-services",
        name=HEARTBEAT,
        label=heartbeat_label(prefix),
        argv=(python_path, str(paths.current / "python" / "omp_services_main.py"), "heartbeat", "run"),
        env=tuple(sorted(env.items())),
        workdir=str(paths.state_dir),
        description="omp-services heartbeat: runs the health checks and pings the configured URL",
        protected=False,
        throttle=10,
        log=str(paths.logs_dir / f"{HEARTBEAT}.log"),
        builtin=True,
        needs=(python_path, str(paths.current / "python" / "omp_services_main.py")),
    )


def specs_from_dropin(
    data: Mapping[str, object],
    prefix: str,
    paths: Paths,
    python_path: str,
    omp_path: str | None,
    carried: Mapping[str, str],
) -> list[ServiceSpec]:
    """Turn a validated services/1 drop-in into service definitions. Raises ServicesError for what the schema cannot
    say: a relative program path, a reserved environment name, an omp script without a pinned omp."""
    plugin = str(data["plugin"])
    specs: list[ServiceSpec] = []
    seen: set[str] = set()
    for svc in data["services"]:  # type: ignore[attr-defined]
        name = svc["name"]
        where = f"services.d/{plugin}.json service {name}"
        if name in seen:
            raise ServicesError(f"{where}: declared twice")
        seen.add(name)
        program = svc["program"]
        kind = program["type"]
        env = {"PATH": service_path(paths, omp_path), **carried}
        if kind == "omp-script":
            if not omp_path:
                raise ServicesError(
                    f"{where}: needs omp, but no omp binary is pinned (install omp, then run init again)"
                )
            argv = (omp_path, program["script"], *program.get("args", []))
            env["BUN_BE_BUN"] = "1"
        elif kind == "python":
            argv = (python_path, program["script"], *program.get("args", []))
        else:
            argv = tuple(program["argv"])
            if not argv[0].startswith("/"):
                raise ServicesError(f"{where}: argv[0] must be an absolute path")
        extra = svc.get("env", {})
        _check_env(extra, where)
        env.update(extra)
        label = label_for(prefix, plugin, name)
        specs.append(
            ServiceSpec(
                plugin=plugin,
                name=name,
                label=label,
                argv=argv,
                env=tuple(sorted(env.items())),
                workdir=svc.get("workingDirectory", str(paths.state_dir)),
                description=svc.get("description") or f"{plugin} {name}",
                protected=bool(svc.get("protected", False)),
                throttle=int(svc.get("throttleSeconds", 10)),
                log=str(paths.logs_dir / f"{plugin}.{name}.log"),
                needs=argv[:2] if kind in {"omp-script", "python"} else argv[:1],
            )
        )
    return specs


# ---------------------------------------------------------------------------------------------------------------------
# launchd
# ---------------------------------------------------------------------------------------------------------------------


def _xml(value: object, indent: int) -> str:
    pad = "\t" * indent
    if isinstance(value, bool):
        return f"{pad}<{'true' if value else 'false'}/>\n"
    if isinstance(value, int):
        return f"{pad}<integer>{value}</integer>\n"
    if isinstance(value, str):
        return f"{pad}<string>{escape(value)}</string>\n"
    if isinstance(value, list):
        inner = "".join(_xml(v, indent + 1) for v in value)
        return f"{pad}<array>\n{inner}{pad}</array>\n"
    if isinstance(value, dict):
        inner = "".join(f"{pad}\t<key>{escape(k)}</key>\n{_xml(v, indent + 1)}" for k, v in sorted(value.items()))
        return f"{pad}<dict>\n{inner}{pad}</dict>\n"
    raise TypeError(f"cannot render {type(value).__name__} in a property list")


def render_launchd(spec: ServiceSpec) -> str:
    """A LaunchAgent property list. Always KeepAlive, Umask 077, output in the service's log file."""
    body = {
        "Label": spec.label,
        "ProgramArguments": list(spec.argv),
        "EnvironmentVariables": dict(spec.env),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": spec.throttle,
        "Umask": UMASK_077,
        "WorkingDirectory": spec.workdir,
        "StandardOutPath": spec.log,
        "StandardErrorPath": spec.log,
    }
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        "<!-- Managed by omp-services. Changes are overwritten by `omp-services init`. -->\n"
        '<plist version="1.0">\n' + _xml(body, 0) + "</plist>\n"
    )


# ---------------------------------------------------------------------------------------------------------------------
# systemd
# ---------------------------------------------------------------------------------------------------------------------


def systemd_quote(text: str, *, variables: bool = True) -> str:
    """One word of an ExecStart= or Environment= line: double-quoted, with the escapes systemd expects, and with `%`
    doubled so that no specifier is expanded. In ExecStart= `$` is doubled too (systemd expands $VARIABLE there); in
    Environment= nothing is expanded but specifiers, so a `$` stays as written (`variables=False`)."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if variables:
        escaped = escaped.replace("$", "$$")
    return f'"{escaped}"'


def _unit_value(text: str) -> str:
    return text.replace("%", "%%")


def render_systemd(spec: ServiceSpec) -> str:
    """A `systemd --user` service unit: Restart=always, UMask=0077, output appended to the service's log file."""
    lines = [
        "# Managed by omp-services. Changes are overwritten by `omp-services init`.",
        "[Unit]",
        f"Description={_unit_value(spec.description)}",
        "",
        "[Service]",
        "Type=simple",
        "ExecStart=" + " ".join(systemd_quote(a) for a in spec.argv),
    ]
    lines += [f"Environment={systemd_quote(f'{k}={v}', variables=False)}" for k, v in spec.env]
    lines += [
        f"WorkingDirectory={_unit_value(spec.workdir)}",
        "UMask=0077",
        "Restart=always",
        f"RestartSec={spec.throttle}",
        "NoNewPrivileges=yes",
        f"StandardOutput=append:{_unit_value(spec.log)}",
        f"StandardError=append:{_unit_value(spec.log)}",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


def check_plain(spec: ServiceSpec) -> None:
    """One invariant for everything that reaches a unit file: no control characters in any field, whatever its source
    (a drop-in, the environment, a discovered or persisted interpreter path)."""
    fields = [spec.label, spec.workdir, spec.description, spec.log, *spec.argv, *spec.needs]
    fields += [part for pair in spec.env for part in pair]
    for text in fields:
        if any(ord(c) < 0x20 or ord(c) == 0x7F for c in text):
            raise ServicesError(
                f"{spec.label}: a control character in a field that would be written into its unit file"
            )


def carried_env(paths: Paths, cfg: Any, env: Mapping[str, str]) -> dict[str, str]:
    """The non-secret settings a service must carry to see the same directories and the same effective configuration as
    the command that installed it: the resolved roots, and the validated values of the overrides in effect."""
    carried = paths.env_overrides(env)
    carried.update(cfg.effective_overrides())
    return carried


def render(spec: ServiceSpec, kind: str) -> str:
    check_plain(spec)
    return render_launchd(spec) if kind == "launchd" else render_systemd(spec)
