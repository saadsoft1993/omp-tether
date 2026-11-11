"""The commands that hand over to the bundled importer and scripts: setup, import, backup, restore-drill, export-hindsight.

None of them writes anything by default: `import` plans unless the importer's own subcommand writes AND `--apply` is
given, `backup` and `export-hindsight` print their plan until `--apply` is given, `setup` is the one command that
fetches anything (and only because you ran it), and `restore-drill` only ever writes to a scratch directory it deletes.
`import ... --apply` additionally refuses without a recent verified backup and a recent dry run (see safety.py).
Whether another process has a bank open is checked by the importer itself, before its first write.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from . import core, safety, sink
from .config import Config, check_destinations, flag_path
from .runtime import (
    IMPORTER,
    IMPORTER_DIR,
    OPS,
    find_bun,
    find_omp,
    importer_env,
    run,
    run_script,
    run_script_tee,
    run_tee,
)
from .util import EXIT_FINDING, EXIT_USAGE, CliError, tilde

IMPORT_COMMANDS = ("scan", "dry-run", "import", "verify", "undo", "doctor", "project-map")

IMPORT_HELP = """usage: omp-memory import <command> [flags]

commands (everything is read-only until a write command gets --apply):
  scan         counts per folder and type, parse errors, unresolved folders; opens no bank (its wouldImport is an upper bound)
  dry-run      the full plan per bank (what would be imported, skipped, held back; binary and over-1,000,000-character files are listed and skipped); --out writes the report
  import       writes rows into banks: refuses without --apply
  verify       expected vs actual counts, embeddings, FTS integrity; exit 1 on any problem
  undo         --run ID: removes the rows of one run; reports only, unless --apply
  doctor       the importer's own checks: library vs omp, the library's write test, bank schemas, bank ids
  project-map  draft folder map for a Hindsight export (--hindsight-export FILE --out FILE)

flags are passed to the importer unchanged; see vendor/memory_import/README.md. Frequently used:
  --source claude-memory|hindsight|claude-mem-summaries   (default claude-memory)
  --only-new-banks  --banks A,B  --release-held KEY[,KEY]|FILE  --apply  --out FILE  --run-id ID
  --force-closed    skip the check that no omp session has the bank open (only after closing them yourself)
before `import --apply` writes, omp-memory also requires (each with an explicit escape):
  a verified backup of the banks younger than 24 h   (`omp-memory backup --apply`; skip with --no-backup); not needed when it
                                                     writes only banks that do not exist yet or that an earlier import of this tool made
  a dry run with the same flags younger than 24 h    (`omp-memory import dry-run ...`; skip with --skip-dry-run-check)
omp-memory fills in --banks-dir, --ledger, --lib-dir, --omp-bin, --conf, --folder-roots and --python from its
configuration (`omp-memory config show`); a flag you give wins. It never adds --apply."""

SETUP_HELP = """usage: omp-memory setup [--dry-run] [--no-embeddings] [--omp-version X.Y.Z] [--registry URL]

Installs the Mnemopi library (`@oh-my-pi/pi-mnemopi`) at exactly the version of your omp into
~/.cache/omp-memory/lib/<version>, once. It is the omp-memory command that downloads the library, and it prints what it
will fetch first. It asks the npm registry whether that exact version exists and refuses (installing nothing) when it does
not. It then runs the importer's write test on the library (a scratch bank under /tmp) and, unless --no-embeddings, embeds
one sentence so that the local embedding runtime and model are fetched now, and repeats the test with that model. When the
library fails a check, setup removes what it installed and exits 1. Nothing is fetched when omp's own plugin directory or
an earlier setup already has the right version.
  --dry-run          print what would be fetched and where, without any network use
  --no-embeddings    skip the embedding runtime and model download
  --omp-version V    install for this version instead of the one `omp --version` prints
  --registry URL     another npm registry (https, or http on this machine; env OMP_MEMORY_NPM_REGISTRY)"""


# Importer flags that take no value: `--apply=false` would still switch the flag on, so a value is refused.
BOOLEAN_FLAGS = frozenset(
    {
        "apply",
        "force-closed",
        "skip-unassigned",
        "only-new-banks",
        "dry-run",
        "no-embeddings",
        "json",
        "help",
        "no-backup",
        "skip-dry-run-check",
    }
)
# Importer flags whose value is a path: made absolute against the caller's directory, because the importer starts in its own.
# `--release-held` names a file only in these written forms (the importer reads anything else as a list of keys).
EXPLICIT_PATH = ("/", "~/", "./", "../")
PATH_FLAGS = (
    "out",
    "project-map",
    "global-allowlist",
    "hindsight-export",
    "hindsight-ledger",
    "claude-mem-db",
    "claude-home",
    "conf",
    "folder-roots",
)


# Importer flags that name a program: a value with a path separator is made absolute (against the caller's directory,
# because the importer starts in its own); a bare name is left for the PATH lookup.
EXECUTABLE_FLAGS = ("python", "sqlite3")


def executable_path(flag: str, value: str) -> str:
    return str(flag_path(flag, value)) if "/" in value else value


def refuse_boolean_values(args: list[str]) -> None:
    for arg in args:
        name, eq, _ = arg.partition("=")
        if eq and name.startswith("--") and name[2:] in BOOLEAN_FLAGS:
            raise CliError(f"{name} takes no value (it is on when given, off when left out)", EXIT_USAGE)


def absolute_paths(
    args: list[str], flags: tuple[str, ...] = PATH_FLAGS, if_file: tuple[str, ...] = ("release-held",)
) -> list[str]:
    """`args` with the value of every flag in `flags` made absolute (against the caller's directory), and likewise the
    value of a flag in `if_file` when it names an existing file (`--release-held` also takes a plain list of keys)."""
    out: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        name, eq, inline = arg.partition("=")
        flag = name[2:] if name.startswith("--") else None
        if flag is not None and (
            flag in flags or flag in if_file or (flag in EXECUTABLE_FLAGS and flags is PATH_FLAGS)
        ):
            if eq:
                value, consumed = inline, 1
            elif i + 1 < len(args):
                value, consumed = args[i + 1], 2
            else:
                out.append(arg)
                i += 1
                continue
            if flag in EXECUTABLE_FLAGS:
                value = executable_path(flag, value)
            elif flag == "folder-roots":
                value = ",".join(str(flag_path(flag, part.strip())) for part in value.split(",") if part.strip())
            elif value and (flag in flags or (value.startswith(EXPLICIT_PATH) and Path(value).expanduser().is_file())):
                value = str(flag_path(flag, value))
            out.extend([f"{name}={value}"] if eq else [name, value])
            i += consumed
            continue
        out.append(arg)
        i += 1
    return out


def take_flags(args: list[str], names: tuple[str, ...]) -> tuple[dict[str, str], list[str]]:
    """Remove `--name value` / `--name=value` for the given names; returns ({name: value}, the remaining arguments)."""
    found: dict[str, str] = {}
    rest: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        name, eq, inline = arg.partition("=")
        if name.startswith("--") and name[2:] in names:
            if name[2:] in found:
                raise CliError(f"{name} was given more than once; give it once", EXIT_USAGE)
            if eq:
                found[name[2:]] = inline
            elif i + 1 < len(args):
                i += 1
                found[name[2:]] = args[i]
            else:
                raise CliError(f"{name} needs a value", EXIT_USAGE)
        else:
            rest.append(arg)
        i += 1
    return found, rest


def import_argv(args: list[str], config: Config) -> list[str]:
    """The full command line for the bundled importer. Raises CliError for what the importer would refuse anyway."""
    if not args or args[0] in ("-h", "--help", "help"):
        sink.out(IMPORT_HELP)
        raise SystemExit(0 if args else EXIT_USAGE)
    sub, rest = args[0], args[1:]
    if sub not in IMPORT_COMMANDS:
        raise CliError(f"unknown import command {sub!r}; one of: {', '.join(IMPORT_COMMANDS)}", EXIT_USAGE)
    if "-h" in rest or "--help" in rest:
        sink.out(IMPORT_HELP)
        raise SystemExit(0)
    if sub == "import" and "--apply" not in rest:
        raise CliError("import writes rows: pass --apply (use `import dry-run` to see the plan first)", EXIT_USAGE)
    refuse_boolean_values(rest)
    rest = absolute_paths(rest)
    given, rest = take_flags(rest, ("banks-dir", "ledger", "lib-dir", "omp-bin", "python"))
    cfg = config.derive(
        banksDir=given.get("banks-dir"),
        ledger=given.get("ledger"),
        libDir=given.get("lib-dir"),
        ompBin=given.get("omp-bin"),
    )
    bun = find_bun(cfg)
    argv: list[str] = [
        str(bun.path),
        str(IMPORTER),
        sub,
        *rest,
        "--banks-dir",
        str(cfg.require("banksDir")),
        "--ledger",
        str(cfg.require("ledger")),
    ]
    lib_dir = cfg.path("libDir")
    if lib_dir is not None:
        argv += ["--lib-dir", str(lib_dir)]
    conf = cfg.path("banksConf")
    if conf is not None and not any(a == "--conf" or a.startswith("--conf=") for a in rest):
        argv += ["--conf", str(conf)]
    roots = group_roots(cfg)
    if roots and not any(a.startswith("--folder-roots") for a in rest):
        argv += ["--folder-roots", ",".join(roots)]
    omp = find_omp(cfg)
    if omp is not None:
        argv += ["--omp-bin", str(omp.path)]
    argv += ["--python", executable_path("python", given["python"]) if "python" in given else sys.executable]
    if "--force-closed" in rest:
        sink.err(
            "warning: --force-closed skips the check for omp sessions that have the bank open; make sure they are closed"
        )
    return argv


def group_roots(cfg: Config) -> list[str]:
    """The absolute root folders of the user's `groups`: where their projects live (the importer uses them to find a
    folder from a project name)."""
    home = Path.home()
    roots = []
    for paths in cfg.groups().values():
        for path in paths:
            roots.append(
                str(Path(path) if path.startswith("/") else home / (path[2:] if path.startswith("~/") else path))
            )
    return sorted(set(roots))


def split_safety_flags(rest: list[str]) -> tuple[list[str], bool, bool]:
    """(the other arguments, --no-backup given, --skip-dry-run-check given): flags of omp-memory, not of the importer."""
    kept = [a for a in rest if a not in ("--no-backup", "--skip-dry-run-check")]
    return kept, "--no-backup" in rest, "--skip-dry-run-check" in rest


def cmd_import(args: list[str], config: Config) -> int:
    refuse_boolean_values(args)
    no_backup = skip_dry_run = False
    if args:
        rest, no_backup, skip_dry_run = split_safety_flags(args[1:])
        if (no_backup or skip_dry_run) and not (args[0] == "import" and "--apply" in rest):
            raise CliError("--no-backup and --skip-dry-run-check only apply to `import import --apply`", EXIT_USAGE)
        args = [args[0], *rest]
    argv = import_argv(args, config)
    state_file = config.require("stateFile")
    banks_dir = Path(argv[argv.index("--banks-dir") + 1])
    sub = args[0] if args else ""
    if sub in ("dry-run", "import"):
        check_destinations(ledger=Path(argv[argv.index("--ledger") + 1]))
        safety.check_state_destination(state_file, banks_dir)  # before anything runs
    # Everything that decides what is written: the importer's flags after the subcommand, minus the interpreter and omp paths.
    options = argv[3:]
    print_fingerprint = safety.fingerprint(
        options
    )  # refuses a flag given twice with different values, before anything runs
    if sub == "import" and "--apply" in argv:
        allowed = safety.check_import_allowed(
            state_file,
            banks_dir,
            print_fingerprint,
            no_backup=no_backup,
            skip_dry_run_check=skip_dry_run,
            explain=sink.err,
            ledger_file=Path(argv[argv.index("--ledger") + 1]),
            plan=lambda: fresh_plan(argv),
        )
    if sub == "import" and "--apply" in argv:
        # The importer enforces both against the plan it executes (under its lock, before any bank is opened for writing).
        argv += ["--expect-plan-digest", allowed["digest"]] if allowed["digest"] else ["--no-plan-check"]
        argv += (
            [f"--backed-up-banks={','.join(allowed['backed_up'])}", f"--exempt-banks={','.join(allowed['exempt'])}"]
            if allowed["backed_up"] is not None
            else ["--no-backup-check"]
        )
    if "--apply" in argv and sub in ("import", "undo"):
        sink.err(f"note: --apply is set: {sub} will write to the banks in {tilde(banks_dir)}")
    if sub == "dry-run":
        return dry_run(argv, options, state_file, banks_dir)
    return run(argv, env=importer_env(), cwd=IMPORTER_DIR)


def fresh_plan(argv: list[str]) -> dict:
    """The importer's dry-run report for these arguments, made now: nothing is written and no report file is made.
    Raises CliError when the importer cannot plan, because then what it would write is unknown."""
    plan = list(argv)
    plan[2] = "dry-run"
    kept: list[str] = []
    skip = False
    for arg in plan:
        if skip:
            skip = False
        elif arg == "--apply" or arg.startswith("--out="):
            continue
        elif arg == "--out":
            skip = True
        else:
            kept.append(arg)
    try:
        ran = subprocess.run(kept, env=importer_env(), cwd=IMPORTER_DIR, capture_output=True, text=True, check=False)
    except OSError as err:
        raise CliError(f"cannot run {kept[0]}: {err.strerror}", EXIT_USAGE) from None
    try:
        report = json.loads(ran.stdout)
    except ValueError:
        report = None
    if ran.returncode != 0 or not isinstance(report, dict):
        raise CliError(
            "refusing to write: the importer could not plan this import just now, so what it would write is unknown"
            + (f" (exit status {ran.returncode}): {ran.stderr.strip()[-300:]}" if ran.returncode else ""),
            EXIT_FINDING,
        )
    return report


def dry_run(argv: list[str], options: list[str], state_file: Path, banks_dir: Path | None = None) -> int:
    """Run the importer's dry run; when it succeeds, record that this plan exists (what `import --apply` checks)."""
    code, out = run_tee(argv, env=importer_env(), cwd=IMPORTER_DIR)
    if code != 0:
        return code
    try:
        report = json.loads(out)
    except ValueError:
        report = None
    if not isinstance(report, dict) or report.get("command") != "dry-run":
        sink.err("note: the dry run's report could not be read, so it is not recorded")
        return code
    safety.record_dry_run(state_file, safety.fingerprint(options), report, banks_dir)
    sink.err(
        f"dry run recorded in {tilde(state_file)}: `import import --apply` with the same flags is allowed for 24 hours (after `backup --apply`)"
    )
    return code


def cmd_setup(args: list[str], config: Config) -> int:
    if any(a in ("-h", "--help") for a in args):
        sink.out(SETUP_HELP)
        return 0
    given, rest = take_flags(args, ("omp-version", "registry", "omp-bin", "lib-dir"))
    cfg = config.derive(ompBin=given.get("omp-bin"), libDir=given.get("lib-dir"))
    allowed = {"--dry-run", "--no-embeddings"}
    stray = [a for a in rest if a not in allowed]
    if stray:
        raise CliError(f"setup: unexpected argument {stray[0]!r}\n\n{SETUP_HELP}", EXIT_USAGE)
    bun = find_bun(cfg)
    argv = [str(bun.path), str(IMPORTER), "setup", *rest]
    for flag in ("omp-version", "registry"):
        if flag in given:
            argv += [f"--{flag}", given[flag]]
    lib_dir = cfg.path("libDir")
    if lib_dir is not None:
        argv += ["--lib-dir", str(lib_dir)]
    omp = find_omp(cfg)
    if omp is not None:
        argv += ["--omp-bin", str(omp.path)]
    return run(argv, env=importer_env(), cwd=IMPORTER_DIR)


def show_home_relative(line: str) -> str:
    """What `backup` prints of the script's `BACKUP_DIR=<path>` line: the path under `~` (the parsed copy keeps the real one)."""
    return f"BACKUP_DIR={tilde(line[len('BACKUP_DIR=') :].rstrip())}\n" if line.startswith("BACKUP_DIR=") else line


def newest_backup(root: Path) -> Path | None:
    """The newest <UTC stamp> directory of a backups directory that holds a SHA256SUMS file."""
    if not root.is_dir():
        return None
    stamps = sorted((p for p in root.iterdir() if p.is_dir() and (p / "SHA256SUMS").is_file()), key=lambda p: p.name)
    return stamps[-1] if stamps else None


def cmd_backup(args: list[str], config: Config) -> int:
    given, rest = take_flags(args, ("dest", "banks-dir"))
    apply = "--apply" in rest
    rest = [a for a in rest if a != "--apply"]
    if rest:
        raise CliError(
            f"backup: unexpected argument {rest[0]!r}; usage: omp-memory backup [--dest DIR] [--banks-dir DIR] [--apply]",
            EXIT_USAGE,
        )
    cfg = config.derive(banksDir=given.get("banks-dir"), backupsDir=given.get("dest"))
    banks_dir, dest = cfg.require("banksDir"), cfg.require("backupsDir")
    names = core.all_bank_names(banks_dir)
    if not names and not (banks_dir.parent / "mnemopi.db").is_file():
        sink.out(
            f"nothing to back up: there is no bank in {tilde(banks_dir)} (omp has not written memory there yet, or banksDir points "
            "elsewhere: see `omp-memory config show`). The first import needs no backup: it only creates new banks."
        )
        return 0
    if not apply:
        sink.out(
            f"plan only, nothing written: would copy {len(names)} bank(s) from {tilde(banks_dir)} to {tilde(dest)}/<UTC stamp>/ (VACUUM INTO, read-only on the banks)"
        )
        sink.out("run again with --apply to make the backup")
        return 0
    safety.check_state_destination(
        cfg.require("stateFile"), banks_dir
    )  # before the script makes a backup nobody can record
    code, out = run_script_tee(
        OPS / "backup_mnemopi.sh", ["--dest", str(dest), "--src", str(banks_dir)], show=show_home_relative
    )
    marker = [line[len("BACKUP_DIR=") :] for line in out.splitlines() if line.startswith("BACKUP_DIR=")]
    if code != 0 or not marker:
        return code or 1
    entry = safety.record_backup(cfg.require("stateFile"), banks_dir, Path(marker[-1]))
    sink.err(
        f"backup verified ({len(entry['files'])} database(s), checksums re-read) and recorded in "
        f"{tilde(cfg.require('stateFile'))}: `import --apply` accepts it for 24 hours"
    )
    return 0


def cmd_restore_drill(args: list[str], config: Config) -> int:
    given, rest = take_flags(args, ("backup", "banks-dir"))
    cfg = config.derive(banksDir=given.get("banks-dir"))
    backup = flag_path("backup", given["backup"]) if "backup" in given else newest_backup(cfg.require("backupsDir"))
    if backup is None:
        raise CliError(
            f"no backup found in {tilde(cfg.path('backupsDir'))}; make one with `omp-memory backup --apply` or pass --backup DIR",
        )
    sink.err(f"restore drill on {tilde(backup)} (writes only to a scratch directory that is deleted afterwards)")
    return run_script(
        OPS / "restore_drill.sh",
        ["--backup", str(backup), "--src", str(cfg.require("banksDir")), *absolute_paths(rest, ("scratch",), ())],
    )


def cmd_export_hindsight(args: list[str], config: Config) -> int:
    del config
    apply = "--apply" in args
    rest = [a for a in args if a != "--apply"]
    if not apply and "--dry-run" not in rest and not any(a in ("-h", "--help") for a in rest):
        rest.append("--dry-run")
        sink.err(
            "plan only (--dry-run): nothing is fetched or written. Pass --apply to export; the export is read-only on Hindsight."
        )
    return run_script(OPS / "export_hindsight.sh", absolute_paths(rest, ("out", "overlay-dir", "token-file"), ()))
