"""The read commands (sources, banks) and the two that keep small files (labels, config)."""

from __future__ import annotations

from . import core, sink
from .config import KEYS, Config, check_destinations, config_path, set_group, set_value, unset_group, unset_value
from .detect import carries_secret
from .util import EXIT_USAGE, CliError, check_own_file, printable, tilde

# The columns of a table that hold one name (a bank, a folder, a label, a path): a name that carries a secret becomes an index
# label of that kind. Every other column is text, of which only the part that carries a secret is withheld.
NAME_COLUMNS = {"BANK": "bank", "LABEL": "folder", "FOLDER": "folder", "PATH": "folder", "VALUE": "folder"}


def table(rows: list[list[str]], header: list[str]) -> str:
    kinds = [NAME_COLUMNS.get(column) for column in header]
    rows = [
        [sink.name(cell, kinds[i]) if kinds[i] else sink.scrub(printable(str(cell))) for i, cell in enumerate(row)]
        for row in rows
    ]
    widths = [max(len(str(r[i])) for r in [header, *rows]) for i in range(len(header))]
    lines = ["  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)).rstrip() for row in [header, *rows]]
    return "\n".join(lines)


def emit(data: object) -> None:
    sink.emit_json(data)


def parser(prog: str, description: str) -> sink.ArgumentParser:
    ap = sink.ArgumentParser(prog=f"omp-memory {prog}", description=description)
    ap.add_argument("--json", action="store_true", help="print JSON")
    ap.add_argument("--banks-dir", metavar="DIR", help="the banks directory (default: see `omp-memory config show`)")
    return ap


def sources_text(scan: dict) -> str:
    rows = []
    for entry in scan["sources"]:
        counts = ", ".join(f"{k} {v}" for k, v in entry["counts"].items()) or "-"
        imported = "-" if entry["importedRows"] is None else str(entry["importedRows"])
        note = entry["note"] or ""
        rows.append([entry["id"], "yes" if entry["available"] else "no", counts, imported, entry["path"] or "-", note])
    return table(rows, ["SOURCE", "FOUND", "COUNTS", "IMPORTED ROWS", "PATH", "NOTE"])


def cmd_sources(args: list[str], config: Config) -> int:
    ns = parser(
        "sources", "What Claude Code, Codex, claude-mem, omp and the instruction files hold. Read-only."
    ).parse_args(args)
    cfg = config.derive(banksDir=ns.banks_dir)
    scan = core.scan_sources(cfg.require("banksDir"), cfg.require("labelsFile"), cfg.path("banksConf"), cfg.groups())
    if ns.json:
        emit(scan)
    else:
        sink.out(sources_text(scan))
        sink.out(
            "\nIMPORTED ROWS counts rows in the omp banks that an import wrote from that source; claude-mem observations and prompts are never imported."
        )
    return 0


def sources_summary(row: dict[str, int]) -> str:
    return ", ".join(f"{k} {v}" for k, v in row.items() if v) or "-"


def banks_text(result: dict) -> str:
    rows = []
    for bank in sorted(result["banks"], key=lambda b: (b["folder"]["group"], b["folder"]["label"].casefold())):
        folder = bank["folder"]
        label = folder["label"] + (" *" if folder["labelCustom"] else "")
        rows.append(
            [
                bank["bank"],
                label,
                folder["group"],
                str(bank["counts"]["total"]),
                str(bank["factCount"]),
                sources_summary(bank["sources"]),
                tilde(folder["path"]) or "-",
            ]
        )
    out = (
        table(rows, ["BANK", "LABEL", "GROUP", "ROWS", "FACTS", "SOURCES", "FOLDER"])
        if rows
        else "no banks with memories"
    )
    if result["emptyBanks"]:
        out += f"\n\n{len(result['emptyBanks'])} empty bank(s) not shown (use --json to list them)"
    for item in result["unreadable"]:
        out += f"\nunreadable: {sink.name(item['bank'], 'bank')}: {item['error']}"
    if any(b["folder"]["labelCustom"] for b in result["banks"]):
        out += "\n* custom label (omp-memory labels)"
    return out


def cmd_banks(args: list[str], config: Config) -> int:
    ns = parser(
        "banks",
        "Every local mnemopi bank: folder label, group, folder path, row counts and where rows came from. Read-only.",
    ).parse_args(args)
    cfg = config.derive(banksDir=ns.banks_dir)
    banks_dir = cfg.require("banksDir")
    result = core.overview(banks_dir, cfg.require("labelsFile"), cfg.path("banksConf"), cfg.groups())
    if ns.json:
        emit({"banksDir": str(banks_dir), **result})
    else:
        sink.out(f"banks in {tilde(banks_dir)}\n")
        sink.out(banks_text(result))
    return 0


def cmd_labels(args: list[str], config: Config) -> int:
    ap = parser(
        "labels", "Folder labels. `set` and `clear` write the labels file (0600, atomic); nothing else is written."
    )
    ap.add_argument("action", choices=("list", "set", "clear"))
    ap.add_argument("bank", nargs="?")
    ap.add_argument("label", nargs="*")
    ns = ap.parse_intermixed_args(args)
    cfg = config.derive(banksDir=ns.banks_dir)
    banks_dir, labels_file, conf = cfg.require("banksDir"), cfg.require("labelsFile"), cfg.path("banksConf")
    adapter = core.build_adapter(banks_dir, labels_file, conf, groups=cfg.groups())
    try:
        if ns.action == "list":
            if ns.bank or ns.label:
                raise CliError("labels list takes no further arguments", EXIT_USAGE)
            return labels_list(adapter, banks_dir, ns.json)
        if not ns.bank:
            raise CliError(f"labels {ns.action} needs a bank name (see `omp-memory banks`)", EXIT_USAGE)
        check_destinations()
        check_own_file(labels_file, "labels", banks_dir)
        if ns.action == "set":
            text = " ".join(ns.label)
            if not text:
                raise CliError("labels set needs a label: omp-memory labels set <bank> <label>", EXIT_USAGE)
            if carries_secret(text):
                raise CliError(
                    "that label looks like it carries a secret (a password, a token or a key); labels are shown in every "
                    "listing and kept in a file, so it is not stored. Choose a plain name",
                    EXIT_USAGE,
                )
            if carries_secret(ns.bank):
                raise CliError(
                    "that bank's name looks like it carries a secret, so it is not written into the labels file. "
                    "Rename the bank or its folder, then label it",
                    EXIT_USAGE,
                )
            folder = adapter.set_label(ns.bank, text)
        else:
            if ns.label:
                raise CliError("labels clear takes only a bank name", EXIT_USAGE)
            try:
                folder = adapter.set_label(ns.bank, None)
            except core.MemoryError as err:
                if err.status != 404 or ns.bank not in adapter.folders.labels.all():
                    raise
                adapter.folders.labels.set(ns.bank, None)  # the bank is gone; drop its stale label
                folder = None
    except core.MemoryError as err:
        raise CliError(err.message) from None
    shown = sink.name(ns.bank, "bank")
    if ns.json:
        emit({"bank": ns.bank, "folder": folder})
    elif folder is None:
        sink.out(f"cleared the label of {shown} (the bank no longer exists)")
    else:
        sink.out(
            f"{shown}: {sink.name(folder['label'], 'folder')}"
            + (" (custom)" if folder["labelCustom"] else " (default)")
        )
    return 0


def labels_list(adapter, banks_dir, as_json: bool) -> int:
    folders = adapter.folder_map()
    custom = adapter.folders.labels.all()
    stale = sorted(b for b in custom if b not in folders)
    if as_json:
        emit({"labelsFile": str(adapter.folders.labels.path), "banks": folders, "stale": stale})
        return 0
    rows = [
        [
            bank,
            folder["label"],
            "custom" if folder["labelCustom"] else "default",
            folder["group"],
            tilde(folder["path"]) or "-",
        ]
        for bank, folder in sorted(folders.items(), key=lambda kv: kv[1]["label"].casefold())
    ]
    sink.out(table(rows, ["BANK", "LABEL", "KIND", "GROUP", "FOLDER"]) if rows else f"no banks in {tilde(banks_dir)}")
    for bank in stale:
        shown = sink.name(bank, "bank")
        sink.out(
            f"stale label for a bank that no longer exists: {shown} (remove with: omp-memory labels clear {shown})"
        )
    return 0


def cmd_config(args: list[str], config: Config) -> int:
    ap = sink.ArgumentParser(
        prog="omp-memory config",
        description="Show or change where omp-memory looks for things and how folders are grouped.",
        epilog="groups: `config set-group NAME FOLDER...` makes banks whose folder is inside one of those folders "
        "(`~/x`, `/x` or relative to your home) part of group NAME; `config unset-group NAME` removes it. "
        "Without groups a folder's group is the first directory below your home that contains it.",
    )
    ap.add_argument("action", choices=("show", "set", "unset", "path", "set-group", "unset-group"))
    ap.add_argument(
        "key", nargs="?", help="a setting (" + ", ".join(KEYS) + ") or, for set-group/unset-group, a group name"
    )
    ap.add_argument("value", nargs="*", help="the value (a path), or the folders of a group")
    ap.add_argument("--json", action="store_true")
    ns = ap.parse_args(args)
    if ns.action == "path":
        sink.out(config_path())
        return 0
    if ns.action == "show":
        settings = config.all()
        groups = config.groups()
        if ns.json:
            emit(
                {
                    "file": str(config_path()),
                    "settings": {
                        s.key: {"value": str(s.value) if s.value else None, "source": s.source, "env": s.env}
                        for s in settings
                    },
                    "groups": groups,
                }
            )
            return 0
        sink.out(f"configuration file: {tilde(config_path())}\n")
        rows = [
            [s.key, tilde(s.value) or "(not set)", s.source + (f" ${s.env}" if s.env else ""), KEYS[s.key].help]
            for s in settings
        ]
        sink.out(table(rows, ["SETTING", "VALUE", "FROM", "MEANING"]))
        sink.out("\nprecedence: command line flag, environment variable, configuration file, default")
        if groups:
            sink.out("\ngroups (config set-group / unset-group):")
            for group, roots in sorted(groups.items()):
                sink.out(f"  {sink.name(group)}: {', '.join(sink.name(root, 'folder') for root in roots)}")
        else:
            sink.out("\ngroups: none set; a folder's group is the first directory below your home that contains it")
        return 0
    if not ns.key:
        raise CliError(f"config {ns.action} needs a name; see `omp-memory config --help`", EXIT_USAGE)
    if ns.action == "set-group":
        if not ns.value:
            raise CliError("config set-group needs folders: omp-memory config set-group <name> <folder>...", EXIT_USAGE)
        file = set_group(ns.key, ns.value)
        sink.out(f"group {sink.name(ns.key)} stored in {tilde(file)}")
        return 0
    if ns.action == "unset-group":
        if ns.value:
            raise CliError("config unset-group takes only a group name", EXIT_USAGE)
        file = unset_group(ns.key)
        sink.out(f"group {sink.name(ns.key)} removed from {tilde(file)}")
        return 0
    if ns.action == "set":
        if len(ns.value) != 1:
            raise CliError("config set needs exactly one value: omp-memory config set <setting> <path>", EXIT_USAGE)
        file = set_value(ns.key, ns.value[0])
        sink.out(f"{sink.name(ns.key)} stored in {tilde(file)}")
    else:
        if ns.value:
            raise CliError("config unset takes only a setting name", EXIT_USAGE)
        file = unset_value(ns.key)
        sink.out(f"{sink.name(ns.key)} removed from {tilde(file)}")
    return 0
