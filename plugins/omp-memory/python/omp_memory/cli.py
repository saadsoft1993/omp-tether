"""The `omp-memory` command line.

    omp-memory setup [--dry-run]               install the Mnemopi library at your omp's exact version (the command that downloads it)
    omp-memory sources                         what Claude Code, Codex, claude-mem, omp and instruction files hold
    omp-memory banks                           every local mnemopi bank: folder label, group, path, counts, sources
    omp-memory labels list|set <bank> <label>|clear <bank>
    omp-memory import <scan|dry-run|import|verify|undo|doctor|project-map> [flags]
    omp-memory backup [--apply]                VACUUM INTO copies of every bank, verified (prints the plan without --apply)
    omp-memory restore-drill                   restore the newest backup into a scratch directory and compare
    omp-memory export-hindsight [--apply]      read-only export of the Hindsight banks (plan only without --apply)
    omp-memory doctor [--deep]                 what is ready and what is not
    omp-memory config show|set|unset|path|set-group|unset-group

Exit status: 0 success, 1 a finding or a refusal, 2 usage or configuration error.
Nothing writes to a bank unless `import import --apply` or `import undo --apply` is given. `import import --apply`
also needs a verified backup (`backup --apply`) and a dry run with the same flags, both younger than 24 hours.
"""

from __future__ import annotations

from . import __version__, sink
from .util import EXIT_USAGE, CliError


def commands() -> dict:
    """The commands, loaded on first use: the output filter and the error hooks are installed before any command module runs."""
    from .doctor import run_doctor
    from .info import cmd_banks, cmd_config, cmd_labels, cmd_sources
    from .ops import cmd_backup, cmd_export_hindsight, cmd_import, cmd_restore_drill, cmd_setup

    return {
        "setup": cmd_setup,
        "sources": cmd_sources,
        "banks": cmd_banks,
        "labels": cmd_labels,
        "import": cmd_import,
        "backup": cmd_backup,
        "restore-drill": cmd_restore_drill,
        "export-hindsight": cmd_export_hindsight,
        "doctor": run_doctor,
        "config": cmd_config,
    }


def main(argv: list[str]) -> int:
    sink.install()
    try:
        return run(argv)
    except CliError as err:
        sink.err(f"omp-memory: {err.message}")
        return err.code
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as err:
        sink.unexpected_error(err)
        return sink.EXIT_INTERNAL


def run(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        sink.out(__doc__.strip())
        return 0 if argv else EXIT_USAGE
    if argv[0] in ("-V", "--version", "version"):
        sink.out(f"omp-memory {__version__}")
        return 0
    command = commands().get(argv[0])
    if command is None:
        sink.err(f"omp-memory: unknown command {argv[0]!r}\n\n{__doc__.strip()}")
        return EXIT_USAGE
    from .config import Config

    return command(argv[1:], Config())
