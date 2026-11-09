"""Installed versus catalog versions: the "newer version available" notice that `doctor` prints.

omp's own `notify` mode only writes a debug line, so nobody is told about updates. Here the installed versions come
from `omp plugin list --json` and the catalog from the copy omp cached when the marketplace was last refreshed. No
network is used unless the caller asks for a refresh, which runs `omp plugin marketplace update`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .util import ServicesError, one_line, run_bounded

SEMVER = re.compile(
    r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)


@dataclass(frozen=True)
class PluginRow:
    plugin: str
    marketplace: str
    installed: str
    available: str | None
    install_path: Path

    @property
    def id(self) -> str:
        return f"{self.plugin}@{self.marketplace}"

    @property
    def kind(self) -> str | None:
        """'newer' (a higher semantic version), 'different' (the two cannot be ordered, as with a name that is not a
        semantic version, but they are not equal: omp itself would reinstall), or None."""
        return None if self.available is None else compare(self.available, self.installed)

    @property
    def outdated(self) -> bool:
        return self.kind is not None


def parse_semver(text: str) -> tuple[tuple[int, int, int], tuple[Any, ...] | None] | None:
    match = SEMVER.fullmatch(text.strip())
    if not match:
        return None
    core = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    pre = match.group(4)
    ids = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split(".")) if pre else None
    return core, ids


def compare(candidate: str, current: str) -> str | None:
    """'newer', 'different' or None for `candidate` against `current`, by semantic-version precedence (a release is
    higher than its own pre-releases; pre-release identifiers compare numerically or alphabetically). Anything that is
    not a semantic version is never called newer: it is 'different' when unequal."""
    a, b = parse_semver(candidate), parse_semver(current)
    if a is None or b is None:
        return None if candidate == current else "different"
    (core_a, pre_a), (core_b, pre_b) = a, b
    if core_a != core_b:
        return "newer" if core_a > core_b else None
    if pre_a == pre_b:
        return None
    if pre_a is None:
        return "newer"
    if pre_b is None:
        return None
    return "newer" if pre_a > pre_b else None


def is_newer(candidate: str, current: str) -> bool:
    return compare(candidate, current) == "newer"


def omp_env(env: Mapping[str, str]) -> dict[str, str]:
    """The environment for running omp itself: ours, without BUN_BE_BUN (a child omp must behave as omp)."""
    clean = {k: v for k, v in env.items() if k != "BUN_BE_BUN"}
    clean.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    return clean


def run_omp(omp: str, args: list[str], env: Mapping[str, str], timeout: float):
    return run_bounded([omp, *args], timeout, env=omp_env(env), max_output=512 * 1024)


def list_installed(omp: str, env: Mapping[str, str]) -> list[dict[str, Any]]:
    """The marketplace plugins `omp plugin list --json` reports (user scope), as {id, version, installPath}."""
    ran = run_omp(omp, ["plugin", "list", "--json"], env, 60.0)
    if not ran.ok:
        raise ServicesError(
            f"`omp plugin list --json` failed ({'timed out' if ran.timed_out else one_line(ran.stderr or ran.error or 'no answer', 120)})"
        )
    try:
        data = json.loads(ran.stdout)
    except ValueError as exc:
        raise ServicesError("`omp plugin list --json` printed something that is not JSON") from exc
    rows: list[dict[str, Any]] = []
    for item in data.get("marketplace", []) if isinstance(data, dict) else []:
        entries = item.get("entries") or []
        user = next((e for e in entries if e.get("scope") == "user"), entries[0] if entries else None)
        if user and isinstance(item.get("id"), str) and "@" in item["id"]:
            rows.append(
                {"id": item["id"], "version": str(user.get("version", "")), "installPath": user.get("installPath", "")}
            )
    return rows


def catalog_file(install_path: Path, marketplace: str) -> Path:
    """Where omp cached the catalog: <plugins root>/cache/marketplaces/<name>/marketplace.json, found from the
    plugin's install path <plugins root>/cache/plugins/<marketplace>___<plugin>___<version>."""
    return install_path.parent.parent / "marketplaces" / marketplace / "marketplace.json"


def catalog_versions(file: Path) -> dict[str, str]:
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ServicesError(f"cannot read the cached catalog {file}: {type(exc).__name__}") from exc
    out: dict[str, str] = {}
    for entry in data.get("plugins", []) if isinstance(data, dict) else []:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str) and isinstance(entry.get("version"), str):
            out[entry["name"]] = entry["version"]
    return out


def plugin_rows(omp: str, env: Mapping[str, str], marketplace: str) -> list[PluginRow]:
    """Every plugin installed from `marketplace`, with the catalog's current version next to it."""
    rows: list[PluginRow] = []
    catalogs: dict[Path, dict[str, str]] = {}
    for item in list_installed(omp, env):
        name, _, market = item["id"].partition("@")
        if market != marketplace:
            continue
        path = Path(item["installPath"])
        file = catalog_file(path, market)
        if file not in catalogs:
            try:
                catalogs[file] = catalog_versions(file)
            except ServicesError:
                catalogs[file] = {}
        rows.append(PluginRow(name, market, item["version"], catalogs[file].get(name), path))
    return sorted(rows, key=lambda r: r.plugin)


def refresh_catalog(omp: str, env: Mapping[str, str], marketplace: str) -> None:
    """Ask omp to refetch the marketplace catalog. This is the only call here that can use the network (omp does)."""
    ran = run_omp(omp, ["plugin", "marketplace", "update", marketplace], env, 180.0)
    if not ran.ok:
        raise ServicesError(
            f"`omp plugin marketplace update {marketplace}` failed: {one_line(ran.stderr or ran.stdout or ran.error or 'timed out', 160)}"
        )
