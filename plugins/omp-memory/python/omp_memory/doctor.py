"""`omp-memory doctor`: what is ready, what is not, and the command that fixes it. It changes nothing."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from . import core, safety, sink
from .config import Config
from .info import emit, parser, table
from .runtime import IMPORTER_DIR, find_bun, find_omp, importer_env, library_status
from .util import EXIT_FINDING, EXIT_OK, CliError, open_holders, tilde, version_text

OK, WARN, FAIL = "ok", "warn", "fail"


def check(name: str, status: str, detail: str, fix: str | None = None) -> dict:
    return {"name": name, "status": status, "detail": detail, **({"fix": fix} if fix else {})}


def vendor_check() -> dict:
    """The vendored copies still match the checksums recorded when they were copied from the upstream checkout."""
    record = core.VENDOR / "SOURCE.sha256"
    try:
        lines = [line.partition("  ") for line in record.read_text().splitlines() if line.strip()]
    except OSError:
        return check("vendored code", FAIL, "vendor/SOURCE.sha256 is missing", "reinstall the plugin")
    recorded = {rel for _, _, rel in lines}
    changed = [rel for digest, _, rel in lines if _digest(core.VENDOR / rel) != digest]
    added = sorted(
        str(path.relative_to(core.VENDOR))
        for path in core.VENDOR.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.name != "SOURCE.sha256"
        and str(path.relative_to(core.VENDOR)) not in recorded
    )
    if changed or added:
        first = (changed or added)[0]
        return check(
            "vendored code",
            FAIL,
            f"{len(changed)} file(s) differ from the recorded checksums and {len(added)} are not recorded, first: {first}",
            "reinstall the plugin (or run scripts/sync-vendor.sh in a checkout)",
        )
    return check("vendored code", OK, f"{len(lines)} files match their recorded checksums")


def _digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def python_check() -> dict:
    """Older interpreters never get this far (omp_memory_main.py refuses them), so this only reports which one runs."""
    version = ".".join(map(str, sys.version_info[:3]))
    return check("python", OK, f"{version} ({tilde(sys.executable)})")


def bun_check(config: Config) -> dict:
    try:
        bun = find_bun(config)
    except CliError as err:
        return check("bun", FAIL, err.message.split(". ")[0], "install Bun 1.4.4 or newer, or set OMP_MEMORY_BUN")
    return check("bun", OK, f"{version_text(bun.version)} ({tilde(bun.path)}, from {bun.source})")


def omp_checks(config: Config) -> list[dict]:
    """Which omp and which Mnemopi library the importer will use, and whether they are the same release."""
    omp = find_omp(config)
    out = []
    if omp is None:
        out.append(
            check(
                "omp",
                WARN,
                "no omp binary found; the importer cannot tell which library version it needs",
                "put omp on PATH or set OMP_BIN",
            )
        )
        return out
    out.append(
        check(
            "omp",
            OK if omp.version else WARN,
            f"{version_text(omp.version) or 'version unreadable'} ({tilde(omp.path)}, from {omp.source})",
        )
    )
    status = library_status(config, omp)
    if status is None:
        out.append(
            check(
                "library",
                WARN,
                "could not ask the importer which library it would use (no usable Bun, or no omp version)",
                "fix the bun check above",
            )
        )
        return out
    location = status.get("location")
    if location:
        found = (
            f"@oh-my-pi/pi-mnemopi {location['version']} = omp {status.get('ompVersion')}, found in "
            f"{_kind(location['kind'])}: {tilde(location['pkgDir'])}"
        )
        out.append(_library_with_self_test(found, status.get("selfTest")))
        return out
    looked = "; ".join(f"{tilde(c['pkgDir'])}: {c['why']}" for c in status.get("candidates", []))
    out.append(
        check(
            "library",
            FAIL,
            f"no @oh-my-pi/pi-mnemopi at omp's exact version {status.get('ompVersion')} (looked at: {looked})",
            f"{status.get('setupCommand', 'omp-memory setup')}   (downloads that exact version once; add --dry-run to see what first)",
        )
    )
    return out


def _library_with_self_test(found: str, self_test: dict | None) -> dict:
    """The library check, OK only when the importer's behavioural self-test has passed for exactly this library (a result
    cached by `setup` or `doctor --deep`); a library nobody has tested is a WARN, one that failed is a FAIL."""
    state = (self_test or {}).get("status", "not-run")
    if state == "passed":
        return check("library", OK, f"{found}; self-test {self_test['detail']}, cached")
    if state == "failed":
        return check(
            "library",
            FAIL,
            f"{found}; self-test failed, {self_test.get('detail')}. Writing is refused",
            "omp-memory doctor --deep repeats the test; see README, Compatibility, for the omp versions this was verified against",
        )
    return check(
        "library",
        WARN,
        f"{found}; not yet verified: the importer's write test has not run for this library",
        "omp-memory doctor --deep   (runs it in a scratch bank, nothing of yours is touched; `setup` does it too)",
    )


def _kind(kind: str) -> str:
    return {
        "omp-plugins": "omp's plugin directory",
        "cache": "the omp-memory cache (made by setup)",
        "explicit": "libDir",
    }.get(kind, kind)


def safety_check(config: Config) -> dict:
    """The records `import --apply` checks: a verified backup and a dry run, each of the last 24 hours."""
    try:
        summary = safety.state_summary(config.require("stateFile"), config.require("banksDir"))
    except CliError:
        return check("safety records", WARN, "state file not readable")
    backup, dry = summary["backupAgeS"], summary["dryRunAgeS"]

    def age(value):
        return "none recorded" if value is None else f"{safety.age_text(value)} ago"

    stale = backup is None or backup > safety.MAX_AGE_S
    return check(
        "safety records",
        OK if not stale else WARN,
        f"verified backup: {age(backup)}; dry run: {age(dry)} (`import --apply` needs a dry run within 24 h, and a backup unless it only writes new banks)",
        "omp-memory backup --apply, then omp-memory import dry-run ... with the flags you will apply"
        if stale
        else None,
    )


def banks_checks(config: Config) -> list[dict]:
    banks_dir = config.require("banksDir")
    if not banks_dir.is_dir():
        return [
            check(
                "banks dir",
                WARN,
                f"{tilde(banks_dir)} does not exist (no omp memory yet, or a different location)",
                "omp-memory config set banksDir <path>",
            )
        ]
    names = core.all_bank_names(banks_dir)
    out = [check("banks dir", OK, f"{tilde(banks_dir)}: {len(names)} bank(s)")]
    files = {name: [banks_dir / name / f"mnemopi.db{suffix}" for suffix in ("", "-wal", "-shm")] for name in names}
    holders = open_holders(files)
    if holders is None:
        out.append(
            check(
                "open banks",
                WARN,
                "lsof is not available: cannot tell which banks omp sessions have open",
                "install lsof, or close every omp session before a write",
            )
        )
    else:
        busy = {bank: pids for bank, pids in holders.items() if pids}
        if busy:
            listing = ", ".join(
                f"{sink.name(bank, 'bank')} (pid {', '.join(map(str, pids))})"
                for bank, pids in sorted(busy.items())[:5]
            )
            out.append(
                check(
                    "open banks",
                    WARN,
                    f"{len(busy)} bank(s) are open in other processes: {listing}",
                    "close those omp sessions before `import import --apply` or `import undo --apply`; the importer refuses these banks",
                )
            )
        else:
            out.append(check("open banks", OK, "no omp session has a bank open"))
    overview = core.overview(banks_dir, config.require("labelsFile"), config.path("banksConf"), config.groups())
    if overview["unreadable"]:
        out.append(
            check(
                "bank files",
                WARN,
                f"{len(overview['unreadable'])} bank(s) cannot be read: "
                + ", ".join(sink.name(i["bank"], "bank") for i in overview["unreadable"][:5]),
            )
        )
    return out


def files_checks(config: Config) -> list[dict]:
    out = []
    labels = config.require("labelsFile")
    if labels.exists():
        try:
            data = json.loads(labels.read_text())
            mode = labels.stat().st_mode & 0o777
            if not isinstance(data, dict) or not isinstance(data.get("labels", {}), dict):
                out.append(check("labels file", WARN, f"{tilde(labels)} is not a labels file; labels are ignored"))
            elif mode & 0o077:
                out.append(
                    check(
                        "labels file",
                        WARN,
                        f"{tilde(labels)} has mode {mode:o}; labels are not secret but keep it 600",
                        f"chmod 600 {labels}",
                    )
                )
            else:
                out.append(check("labels file", OK, f"{tilde(labels)}: {len(data.get('labels', {}))} custom label(s)"))
        except (OSError, ValueError):
            out.append(check("labels file", WARN, f"{tilde(labels)} cannot be read; labels are ignored"))
    else:
        out.append(check("labels file", OK, f"{tilde(labels)} not created yet (no custom labels)"))
    ledger = config.require("ledger")
    out.append(
        check(
            "ledger",
            OK,
            f"{tilde(ledger)} " + ("exists" if ledger.exists() else "not created yet (no import has been applied)"),
        )
    )
    conf = config.path("banksConf")
    groups = config.groups()
    if conf is not None and not conf.is_file():
        out.append(check("groups", WARN, f"banksConf {tilde(conf)} is set but not a file; it is ignored"))
    parts = []
    if conf is not None and conf.is_file():
        parts.append(f"{len(core.load_conf_roots(conf, Path.home()))} root(s) from {tilde(conf)}")
    if groups:
        parts.append(f"{len(groups)} group(s) set with `config set-group`: {', '.join(sorted(groups))}")
    out.append(
        check(
            "groups",
            OK,
            "; ".join(parts) or "none set: a folder's group is the first directory below your home that contains it",
        )
    )
    backups = config.require("backupsDir")
    out.append(
        check(
            "backups dir",
            OK,
            f"{tilde(backups)} " + ("exists" if backups.is_dir() else "not created yet (`backup --apply` creates it)"),
        )
    )
    return out


def sources_check(config: Config) -> dict:
    scan = core.scan_sources(
        config.require("banksDir"), config.require("labelsFile"), config.path("banksConf"), config.groups()
    )
    found = [s["id"] for s in scan["sources"] if s["available"]]
    notes = [f"{s['id']}: {s['note']}" for s in scan["sources"] if s["note"] and s["id"] != "omp-mnemopi"]
    detail = "found: " + (", ".join(found) or "none") + ("; " + "; ".join(notes) if notes else "")
    return check("sources", OK if found else WARN, detail)


def run_doctor(args: list[str], config: Config) -> int:
    ap = parser(
        "doctor",
        "Check which bun, omp and Mnemopi library the importer uses, the banks directory, sessions that hold banks open and source discovery. Read-only.",
    )
    ap.add_argument(
        "--deep",
        action="store_true",
        help="also run the importer's own `doctor` (SQL parity with omp, bank schemas, bank ids); needs Bun and the library",
    )
    ns = ap.parse_args(args)
    cfg = config.derive(banksDir=ns.banks_dir)
    # --deep runs the importer's doctor (which includes the library write test) FIRST, so that the library line below is
    # read from the result it just cached: one run, one consistent answer.
    deep_code, deep_report, deep_stderr = (
        (EXIT_OK, None, "") if not ns.deep else deep_importer_doctor(config, ns.banks_dir)
    )
    checks = [
        python_check(),
        bun_check(cfg),
        *omp_checks(cfg),
        *banks_checks(cfg),
        sources_check(cfg),
        *files_checks(cfg),
        safety_check(cfg),
        vendor_check(),
    ]
    failed = any(c["status"] == FAIL for c in checks)
    code = EXIT_FINDING if failed or deep_code != EXIT_OK else EXIT_OK
    if ns.json:
        # one JSON document, with the importer's report under "deep" when --deep was given
        emit({"ok": code == EXIT_OK, "checks": checks, **({"deep": deep_report} if ns.deep else {})})
        if deep_stderr:
            sink.write_err(deep_stderr)
        return code
    sink.out(table([[c["status"].upper(), c["name"], c["detail"]] for c in checks], ["", "CHECK", "DETAIL"]))
    for c in checks:
        if c.get("fix") and c["status"] != OK:
            sink.out(f"  fix ({c['name']}): {c['fix']}")
    sink.out(
        "\n"
        + ("not ready: fix the failures above" if failed else "ready (warnings are about writes or optional parts)")
    )
    if ns.deep:
        sink.out("\n-- importer doctor --")
        if deep_report is not None:
            emit(deep_report)
        else:
            sink.out("(the importer's doctor gave no report)")
        if deep_stderr:
            sink.write_err(deep_stderr)
    return code


def deep_importer_doctor(config: Config, banks_dir: str | None) -> tuple[int, dict | None, str]:
    """Run the importer's own `doctor` (SQL, write test, schemas, bank ids): (exit status, its JSON report or None, stderr)."""
    from .ops import import_argv

    try:
        argv = import_argv(["doctor", *(["--banks-dir", banks_dir] if banks_dir else [])], config)
        ran = subprocess.run(
            argv, capture_output=True, text=True, env=importer_env(), timeout=900, cwd=IMPORTER_DIR, check=False
        )
    except CliError as err:
        return EXIT_FINDING, {"error": err.message}, ""
    except (OSError, subprocess.SubprocessError) as err:
        return EXIT_FINDING, {"error": str(err)}, ""
    try:
        report = json.loads(ran.stdout)
    except ValueError:
        report = None
    return ran.returncode, report if isinstance(report, dict) else None, ran.stderr
