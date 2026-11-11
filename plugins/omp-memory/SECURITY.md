# Security policy: omp-memory

## Reporting a vulnerability

Please report it privately, not in a public issue: use GitHub's **Report a vulnerability** button on the repository's Security tab
(private vulnerability reporting). Include the omp version, your operating system, the plugin version, the command you ran, and the smallest steps
that reproduce the problem. Please do not attach memory text, bank files, backups or your ledger; a description of the shape of the data is enough.

This is a one-person project. I will read every report and answer as soon as I can; I cannot promise a fixed response time. Please give me
reasonable time to fix a problem before you publish it, and tell me if you intend to.

## What counts

In scope: any command that writes a bank, a ledger, a backup or a report without the explicit flag that is documented for it (`--apply`, or the `labels`/`config` subcommand itself);
a write to a bank that another process has open, or to a bank the importer's guards (library version, schema, embedding model) should have refused; a secret that the redaction and HOLD stages
should have kept out of a bank and the dry run's report, but did not (give the *kind* of secret and the shape of the text, not the secret); memory text appearing in a command's output, an error message
or a file beyond what the README says (at most three redacted 80-character samples per source and type in the dry run and its samples file; a name that carries a secret (a bank, folder, label, file name, path or project key) appears in a command's output, in `--json`, in error messages and in forwarded importer or script output only as a process-local index such as `bank#3`, `folder#2` or `name#1`, and no traceback is ever printed: an internal error is one categorical line, `omp-memory: internal error (<category>)`, with exit status 3, and the launcher and entry point print fixed text only (never a path, an interpreter name or an exception's message)); a file with a mode wider than `0600` (directory `0700`) among those this plugin writes; a way to make
a command follow a symbolic link into a place it should not write, or to write outside the directories it documents; a configuration file that is a link, not yours or writable by others being used; a path with `..` accepted;
a drift between `vendor/` and what the tests say it is.

Out of scope, by design (see [`docs/threat-model.md`](docs/threat-model.md)): files that must hold the real name to work (the state file's backup and banks directories and bank file names, backup copy names, `MANIFEST.tsv`, folders you put in the configuration) keep it; a process that already runs as your user or as root (it can read the banks, the memory files of the tools the README names, the ledger and the backups anyway);
other accounts on a machine you share (not supported); secrets with no recognisable shape that redaction cannot see (the README says so and asks you to review the dry run); flaws in omp, Bun, SQLite, `lsof`, ssh, npm or the
npm packages `omp-memory setup` downloads (report those upstream, and tell me if this plugin makes one worse).

## Supported versions

The latest release only, while the version is below 1.0.

## What is not claimed

Nobody has audited this plugin. What exists instead: the importer's own suite (scratch banks built with the real Mnemopi library: guards, holds, tombstones, undo, targeting, the three sources); a Python suite that runs
the command line against scratch homes and fake banks with the real schema (labels rules, configuration refusals, argument handling for the importer, backups and restore drill, a real second process holding a bank open, vendor drift);
and `scripts/smoke.sh`, a whole install, import, verify and undo cycle on synthetic data in a throwaway home (run on macOS arm64 and in an Ubuntu 24.04 arm64 container; other platforms are not tested, see `compat.json`). These show that the guards that exist work, not that no guard is missing. The redaction and HOLD rules are patterns: they reduce the chance
that a secret is imported, they do not remove it. Treat early releases as beta software, back your banks up before any write, and read the dry run.
