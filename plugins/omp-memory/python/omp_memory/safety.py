"""What `omp-memory import --apply` checks before it lets the importer write: a recent verified backup and a recent dry run.

Two small records are kept in the state file (`config show` names it, mode 0600):

* `backups`: written by `omp-memory backup --apply` after it has re-read every copy and compared its SHA-256 with the
  backup's own SHA256SUMS: when, where, which banks directory, and each copy's checksum.
* `dryRuns`: written by `omp-memory import dry-run` when it succeeds: when, a fingerprint of the options, and which
  banks the plan would write to.

`import ... --apply` then refuses unless (a) a backup of the banks directory younger than 24 hours is recorded, still on
disk and still matching its checksums, and covers every existing bank the import would write to, except a bank that
does not exist yet and a bank an earlier import of this tool created that still holds only that import's rows (the
ledger records which banks it created; a backup cannot protect what the importer itself wrote), so a first import
into a machine without banks needs no backup. Which banks the import would write to is asked of the importer again
when `--apply` runs (a plan made a moment ago), not read from the recorded dry run, which can be a day old; and (b) a
dry run with the same options (source, target flags, banks directory, ledger and the content of any file a flag names;
not --apply, --out or --run-id) is recorded and younger than 24 hours. `--no-backup` skips (a) and
`--skip-dry-run-check` skips (b); both say so on stderr. Neither check changes what the importer itself guards (open
banks, held credentials, library version).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from .util import EXIT_FINDING, EXIT_USAGE, STATE_KIND, CliError, atomic_write_json, check_own_file, tilde

STATE_VERSION = 1
MAX_AGE_S = 24 * 3600
KEEP = 20
# Importer flags that do not change what is written, so they do not change the fingerprint.
IGNORED_FLAGS = frozenset({"apply", "out", "run-id", "run", "force-closed", "python", "omp-bin", "lib-dir"})
# Importer flags that name a file whose content decides what is written: the fingerprint holds the file's hash too, so a
# plan read before the file changed does not authorise the write after.
FILE_FLAGS = frozenset({"project-map", "global-allowlist", "release-held", "conf", "hindsight-export"})


def now() -> float:
    return time.time()


def load_state(path: Path) -> dict:
    """The state file's content (no records when there is none or it cannot be read). Reading never writes: recording
    refuses a file that is not a state file (`check_own_file`)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"kind": STATE_KIND, "schemaVersion": STATE_VERSION, "backups": [], "dryRuns": []}
    if not isinstance(data, dict):
        data = {}
    return {
        "kind": STATE_KIND,
        "schemaVersion": STATE_VERSION,
        "backups": [b for b in data.get("backups", []) if isinstance(b, dict)],
        "dryRuns": [d for d in data.get("dryRuns", []) if isinstance(d, dict)],
    }


def save_state(path: Path, state: dict, banks_dir: Path | None = None) -> None:
    state = {**state, "backups": state["backups"][-KEEP:], "dryRuns": state["dryRuns"][-KEEP:]}
    atomic_write_json(path, state, "state", banks_dir)


def sha256_file(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def parse_sums(text: str) -> dict[str, str]:
    """{file name: sha256} from a `sha256sum`/`shasum -a 256` listing."""
    sums = {}
    for line in text.splitlines():
        digest, _, name = line.partition(" ")
        name = name.lstrip(" *").strip()
        if len(digest) == 64 and name:
            sums[name] = digest
    return sums


def verify_backup_dir(directory: Path) -> tuple[dict[str, str], list[str]]:
    """({file: sha256} of the copies, problems) after re-hashing every copy against the backup's SHA256SUMS."""
    problems: list[str] = []
    try:
        sums = parse_sums((directory / "SHA256SUMS").read_text(encoding="utf-8"))
    except OSError:
        return {}, [f"{tilde(directory)} has no SHA256SUMS"]
    if not sums:
        return {}, [f"{tilde(directory)}/SHA256SUMS lists no files"]
    actual: dict[str, str] = {}
    for name, expected in sums.items():
        found = sha256_file(directory / name)
        if found is None:
            problems.append(f"{name} is missing")
        elif found != expected:
            problems.append(f"{name} does not match its checksum")
        else:
            actual[name] = found
    return actual, problems


def record_backup(state_file: Path, banks_dir: Path, backup_dir: Path) -> dict:
    """Verify the backup in `backup_dir` and record it; raises CliError when a copy does not match its checksum."""
    files, problems = verify_backup_dir(backup_dir)
    if problems:
        raise CliError(
            f"the backup in {tilde(backup_dir)} did not verify, so it is not recorded: " + "; ".join(problems)
        )
    entry = {
        "at": now(),
        "dir": str(backup_dir),
        "banksDir": str(banks_dir),
        "files": files,
    }
    state = load_state(state_file)
    state["backups"].append(entry)
    save_state(state_file, state, banks_dir)
    return entry


def file_digest(value: str) -> str:
    """The SHA-256 of the file a flag value names (`~/` and relative paths as the importer reads them), "none" when the
    value is not an existing file (`--release-held` also takes a plain list of keys), "unreadable" when it cannot be read."""
    path = Path(value).expanduser()
    if not path.is_file():
        return "none"
    digest = sha256_file(path)
    return digest or "unreadable"


def flag_pairs(argv: list[str]) -> list[tuple[str, str]]:
    """Sorted (flag, value) pairs of importer flags (`--name value`, `--name=value`, or a bare `--name` -> value "").
    A flag given twice with different values is refused: the importer would use one of them and this fingerprint both."""
    pairs: list[tuple[str, str]] = []
    seen: dict[str, str] = {}
    index = 0
    while index < len(argv):
        arg = argv[index]
        index += 1
        if not arg.startswith("--"):
            continue
        name, eq, inline = arg[2:].partition("=")
        if eq:
            value = inline
        elif index < len(argv) and not argv[index].startswith("--"):
            value = argv[index]
            index += 1
        else:
            value = ""
        if name in seen and seen[name] != value:
            raise CliError(f"--{name} was given more than once with different values; give it once", EXIT_USAGE)
        seen[name] = value
        if name in IGNORED_FLAGS:
            continue
        if name == "banks":  # the order of a list does not matter
            value = ",".join(sorted(part.strip() for part in value.split(",") if part.strip()))
        if any(n == name for n, _ in pairs):  # the same flag again with the same value
            continue
        pairs.append((name, value))
        if name in FILE_FLAGS and value:
            pairs.append((f"{name}-sha256", file_digest(value)))
    return sorted(pairs)


def fingerprint(importer_flags: list[str]) -> str:
    """A hash of the options that decide what an import writes. `importer_flags` is what follows the subcommand,
    including the wrapper's resolved --banks-dir and --ledger; `--source` defaults to claude-memory."""
    pairs = flag_pairs(importer_flags)
    if not any(name == "source" for name, _ in pairs):
        pairs = sorted([*pairs, ("source", "claude-memory")])
    return hashlib.sha256(json.dumps(pairs, separators=(",", ":")).encode()).hexdigest()


def check_state_destination(state_file: Path, banks_dir: Path) -> None:
    """Refuse a state file that is not one, or lies in the banks directory, before anything runs."""
    check_own_file(state_file, "state", banks_dir)


def record_dry_run(state_file: Path, print_fingerprint: str, report: dict, banks_dir: Path | None = None) -> None:
    per_bank = [b for b in report.get("perBank", []) if isinstance(b, dict)]
    banks = sorted({b["bank"] for b in per_bank if b.get("wouldImport", 0) > 0})
    counts = report.get("counts", {})
    would = counts.get("wouldImport")
    state = load_state(state_file)
    state["dryRuns"].append(
        {
            "at": now(),
            "fingerprint": print_fingerprint,
            "source": report.get("source"),
            # what the plan would write: the importer's own number, else the sum over the banks (never `found`, which also counts skips)
            "wouldImport": would if isinstance(would, int) else sum(int(b.get("wouldImport", 0)) for b in per_bank),
            "targetBanks": banks,
            # what the plan would write, as the importer digests it: `--apply` compares a plan made then with this
            "planDigest": report.get("planDigest") if isinstance(report.get("planDigest"), str) else None,
        }
    )
    save_state(state_file, state, banks_dir)


def age_text(seconds: float) -> str:
    return f"{int(seconds // 3600)} h {int(seconds % 3600 // 60)} min"


def existing_banks(banks_dir: Path) -> list[str]:
    try:
        return sorted(p.name for p in banks_dir.iterdir() if (p / "mnemopi.db").is_file())
    except OSError:
        return []


def fresh_dry_run(state: dict, print_fingerprint: str) -> dict | None:
    cutoff = now() - MAX_AGE_S
    found = [d for d in state["dryRuns"] if d.get("fingerprint") == print_fingerprint and d.get("at", 0) >= cutoff]
    return max(found, key=lambda d: d["at"], default=None)


def fresh_backup(state: dict, banks_dir: Path, needed: list[str]) -> tuple[dict | None, list[str]]:
    """(the newest recorded backup of `banks_dir` younger than 24 h that is intact and covers `needed`, why not)."""
    cutoff = now() - MAX_AGE_S
    candidates = [b for b in state["backups"] if b.get("banksDir") == str(banks_dir) and b.get("at", 0) >= cutoff]
    if not candidates:
        older = [b for b in state["backups"] if b.get("banksDir") == str(banks_dir)]
        if older:
            return None, [f"the newest recorded backup is {age_text(now() - max(b['at'] for b in older))} old"]
        return None, ["no backup of this banks directory is recorded"]
    reasons: list[str] = []
    for entry in sorted(candidates, key=lambda b: b["at"], reverse=True):
        directory = Path(entry.get("dir", ""))
        files = entry.get("files", {})
        gone = [n for n, digest in files.items() if sha256_file(directory / n) != digest]
        if gone:
            reasons.append(f"{tilde(directory)}: {', '.join(sorted(gone)[:3])} changed or is missing")
            continue
        missing = [bank for bank in needed if f"{bank}.db" not in files]
        if missing:
            reasons.append(f"{tilde(directory)} does not cover {', '.join(missing[:5])}")
            continue
        return entry, []
    return None, reasons


FTS_SHADOW = ("config", "data", "docsize", "idx", "content")


def holds_only_importer_rows(database: Path) -> bool:
    """True only when the bank provably holds nothing of the user's. The same rules as the importer's own check
    (`EXEMPTION_RULES` in vendor/memory_import/banks.ts), failing closed on any doubt:

    * a non-empty `-wal` or `-journal` file next to the database means unmerged content: not exempt (`-shm` is ignored);
    * a missing database file is an empty bank; otherwise it is opened read-only and any error means not exempt;
    * `working_memory` must exist and every row must be this tool's (trust tier `IMPORTED`, source `import-<source>`
      matching the metadata's source, `tool` `import`, string `originKey` and `originHash`);
    * `annotations` and `memory_embeddings` may only refer to those working rows;
    * `fts_working` and the shadow tables of FTS5 tables are ignored;
    * every other table (episodic memory, scratchpad, gists, facts, triples, ...) must hold no row at all.
    """
    for side in ("-wal", "-journal"):
        extra = Path(f"{database}{side}")
        try:
            if extra.exists() and extra.stat().st_size > 0:
                return False
        except OSError:
            return False
    if not database.exists():
        return True
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        tables = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite!_%' ESCAPE '!'"
        ).fetchall()
        names = {name for name, _ in tables}
        if "working_memory" not in names:
            return False
        fts = {
            name
            for name, sql in tables
            if sql and "create virtual table" in sql.lower() and "using fts5" in sql.lower()
        }
        shadows = {f"{table}_{part}" for table in fts for part in FTS_SHADOW}
        # rows for which "this tool wrote it" is not TRUE: a missing or NULL field makes the predicate NULL, which is not true
        foreign = connection.execute(
            "SELECT COUNT(*) FROM working_memory WHERE COALESCE(json_valid(metadata_json), 0) = 0"
            " OR COALESCE(trust_tier = 'IMPORTED', 0) = 0"
            " OR COALESCE(json_type(metadata_json, '$.source') = 'text'"
            "  AND json_extract(metadata_json, '$.source') != ''"
            "  AND source = 'import-' || json_extract(metadata_json, '$.source'), 0) = 0"
            " OR COALESCE(json_extract(metadata_json, '$.tool') = 'import', 0) = 0"
            " OR COALESCE(json_type(metadata_json, '$.originKey') = 'text', 0) = 0"
            " OR COALESCE(json_type(metadata_json, '$.originHash') = 'text', 0) = 0"
        ).fetchone()[0]
        if foreign:
            return False
        for name in sorted(names - {"working_memory", "fts_working"} - shadows):
            quoted = '"' + name.replace('"', '""') + '"'
            if name in ("annotations", "memory_embeddings"):
                count = connection.execute(
                    f"SELECT COUNT(*) FROM {quoted} WHERE memory_id IS NULL OR memory_id NOT IN (SELECT id FROM working_memory)"
                ).fetchone()[0]
            else:
                count = connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
            if count:
                return False
        return True
    except sqlite3.Error:
        return False
    finally:
        connection.close()


def importer_only_banks(ledger_file: Path | None, banks_dir: Path, names: list[str]) -> set[str]:
    """The banks among `names` that an earlier import of this tool created (the ledger records them) and that still hold
    only what it wrote, or nothing (after an undo). A backup cannot protect what the importer itself put there."""
    if ledger_file is None:
        return set()
    try:
        created = json.loads(ledger_file.read_text(encoding="utf-8")).get("banksCreated", {})
    except (OSError, ValueError, AttributeError):
        return set()
    if not isinstance(created, dict):
        return set()
    return {bank for bank in names if bank in created and holds_only_importer_rows(banks_dir / bank / "mnemopi.db")}


def planned_banks_of(report: dict) -> set[str]:
    """The banks a dry-run report says would receive rows."""
    per_bank = [b for b in report.get("perBank", []) if isinstance(b, dict)]
    return {str(b["bank"]) for b in per_bank if isinstance(b.get("bank"), str) and (b.get("wouldImport") or 0) > 0}


def check_import_allowed(
    state_file: Path,
    banks_dir: Path,
    print_fingerprint: str,
    *,
    no_backup: bool,
    skip_dry_run_check: bool,
    explain: Callable[[str], None],
    ledger_file: Path | None = None,
    plan: Callable[[], dict] | None = None,
) -> dict:
    """Raise CliError unless the import may write. `explain` prints a warning line for a skipped check. Returns what the
    importer must enforce itself against the plan it executes: `digest` (the plan digest of the dry run, None when that check
    was skipped), `backed_up` (the banks a verified backup covers; None when the backup check was skipped) and `exempt`
    (banks this tool created that held only its rows when this was looked at: the importer re-evaluates that for each bank
    just before it writes, and a bank is exempt once).
    `plan()` asks the importer for a plan made just now (its dry-run report). It is asked once, and only when the recorded
    dry run carries a plan digest to compare or a backup has to be judged (None: no plan, every existing bank counts)."""
    state = load_state(state_file)
    dry = None
    fresh: dict | None = None
    digest: str | None = None

    def fresh_plan() -> dict | None:
        nonlocal fresh
        if fresh is None and plan is not None:
            fresh = plan()
        return fresh

    if skip_dry_run_check:
        explain("warning: --skip-dry-run-check: no dry run for these options was required")
    else:
        dry = fresh_dry_run(state, print_fingerprint)
        if dry is None:
            raise CliError(
                "refusing to write: no dry run with these exact options was made in the last 24 hours. Run "
                "`omp-memory import dry-run` with the same flags (everything except --apply, --out and --run-id), "
                "read the plan, then repeat the import. (--skip-dry-run-check skips this check.)",
                EXIT_FINDING,
            )
        recorded = dry.get("planDigest")
        if not recorded:
            raise CliError(
                "refusing to write: the recorded dry run carries no plan digest (it was made by an older build). Run "
                "`omp-memory import dry-run` again with the same flags. (--skip-dry-run-check skips this check.)",
                EXIT_FINDING,
            )
        digest = recorded
        if plan is not None:
            now_digest = (fresh_plan() or {}).get("planDigest")
            if now_digest != recorded:
                raise CliError(
                    "refusing to write: what this import would write is not what your dry run showed (a memory file, "
                    "a held item or a bank changed since). Run `omp-memory import dry-run` again with the same flags, read "
                    "the plan, then repeat the import. (--skip-dry-run-check skips this check.)",
                    EXIT_FINDING,
                )
    if no_backup:
        explain(
            "warning: --no-backup: no verified backup was required; the rows this writes can only be removed with `import undo`"
        )
        return {"digest": digest, "backed_up": None, "exempt": None}
    # Banks the import would write to, from a plan made now (the recorded dry run can be a day old and the banks and
    # the memory files can have changed since). A bank that does not exist yet has nothing to protect, and neither has
    # one this tool created that holds only its own rows.
    existing = existing_banks(banks_dir)
    report = fresh_plan()
    planned = None if report is None else planned_banks_of(report)
    needed = existing if planned is None else [b for b in existing if b in planned]
    ours_all = importer_only_banks(ledger_file, banks_dir, existing)
    ours = {bank for bank in needed if bank in ours_all}
    needed = [bank for bank in needed if bank not in ours]
    if not needed:
        explain(
            "no backup needed: no bank that holds anything of yours is written to"
            + (f" ({len(ours)} bank(s) were created by an earlier import and hold only its rows)" if ours else "")
        )
        newest, _ = fresh_backup(state, banks_dir, [])
        covered = {name[:-3] for name in (newest or {}).get("files", {}) if name.endswith(".db")}
        return {"digest": digest, "backed_up": sorted(covered), "exempt": sorted(ours_all)}
    backup, why = fresh_backup(state, banks_dir, needed)
    if backup is None:
        raise CliError(
            "refusing to write: no verified backup of the banks directory younger than 24 hours ("
            + "; ".join(why)
            + "). Run `omp-memory backup --apply` first. (--no-backup skips this check.)",
            EXIT_FINDING,
        )
    explain(
        f"backup {tilde(backup['dir'])} ({age_text(now() - backup['at'])} old, {len(backup['files'])} database(s)) verified"
    )
    covered = {name[:-3] for name in backup["files"] if name.endswith(".db")}
    return {"digest": digest, "backed_up": sorted(covered), "exempt": sorted(ours_all)}


def state_summary(state_file: Path, banks_dir: Path) -> dict:
    """What `doctor` shows: the newest backup of the banks directory and the newest dry run."""
    state = load_state(state_file)
    backups = [b for b in state["backups"] if b.get("banksDir") == str(banks_dir)]
    dry = state["dryRuns"]
    return {
        "backupAgeS": now() - max(b["at"] for b in backups) if backups else None,
        "dryRunAgeS": now() - max(d["at"] for d in dry) if dry else None,
    }
