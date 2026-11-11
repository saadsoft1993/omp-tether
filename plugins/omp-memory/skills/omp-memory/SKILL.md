---
name: omp-memory
description: Use when the user asks about omp's local memory banks: which banks and folders exist, what Claude Code, Codex or claude-mem hold, labelling a folder, importing Claude Code memory into omp, backing the banks up, or checking that memory is healthy. Drives the `omp-memory` command line safely; never writes a bank without the user's say-so.
---

# omp-memory

`omp-memory` is a command line (installed with the `omp-memory` plugin) for omp's local mnemopi memory banks
(`~/.omp/agent/memories/mnemopi/banks/<bank>/mnemopi.db`, one per working folder). Memory text is private: it can hold
credentials, customer data and personal notes. Work from counts, bank names and keys; never paste memory text into a
chat, a ticket or a commit, and never print more than a short redacted sample.

If the command is not on `PATH`, run it from the plugin: `~/.omp/plugins/node_modules/omp-memory/bin/omp-memory`.

## Safe by default

Everything here only reads, except the commands named in the table. Run read commands freely.

| Command | Writes |
|---|---|
| `omp-memory sources`, `banks`, `labels list`, `config show`, `import scan`, `import verify` | nothing |
| `omp-memory doctor`, `import doctor` | nothing in a bank; `doctor --deep` and `import doctor` run the importer's write test in a scratch bank under `/tmp` and keep its result in a cache file |
| `omp-memory import dry-run` | a record that the plan was made, in `~/.config/omp-memory/state.json`; no bank. `--out FILE` also writes that report file (0600) |
| `omp-memory labels set <bank> <label>`, `labels clear <bank>` | the labels file only (`~/.config/omp-memory/labels.json`); never a bank |
| `omp-memory config set <setting> <path>`, `config set-group <name> <folder>...`, `config unset-group <name>` | `~/.config/omp-memory/config.json` |
| `omp-memory setup` | downloads `@oh-my-pi/pi-mnemopi` at the version of omp (and omp's embedding runtime and model) into `~/.cache/omp-memory` and omp's cache; `--dry-run` shows what first. Run it only when the user agrees to the download |
| `omp-memory backup --apply` | a new `<UTC stamp>` folder under the backups directory, and a record of it in `~/.config/omp-memory/state.json`; banks are only read |
| `omp-memory restore-drill` | a scratch folder under `/tmp`, deleted afterwards |
| `omp-memory import import --apply`, `import undo --run ID --apply` | **the banks.** Only after the steps below, and only when the user says so |
| `omp-memory export-hindsight --apply` | optional, only for Hindsight users: export files; contacts the user's Hindsight (read-only there); needs `--banks`, `--out` and a connection, there are no defaults |

## Rules

1. **Never pass `--apply` to `import import` or `import undo` unless the user has told you to in this conversation.** A
   plan, a green dry run, or "go ahead and look at it" is not that. `import import` without `--apply` is refused.
2. **Dry run first**, and show the user the counts: `omp-memory import dry-run --source claude-memory --out /tmp/plan.json`.
   Report found / would import / duplicates / held back / unresolved folders. Never print the plan's samples file.
3. **Held items stay held.** The importer holds back any item that still looks like a secret after redaction. List them by
   key and kind only. Pass `--release-held KEY[,KEY]` only for keys the user has reviewed and named.
4. **Close omp sessions first.** The importer refuses a bank another process has open. `omp-memory doctor` lists them
   (pids). Do not pass `--force-closed` to get around that; ask the user to close the session.
5. **Back up before a write**: `omp-memory backup --apply`, then `omp-memory restore-drill`. Do both and show the
   `RESULT:` line before an `--apply` import. `import import --apply` refuses without a dry run of the same flags (made
   in the last 24 h) and, when it writes into a bank that already holds anything of the user's, without that backup (also
   within 24 h). On a machine with no banks `backup` says `nothing to back up` and the first import needs no backup.
   Never pass `--no-backup` or `--skip-dry-run-check` unless the user has asked for exactly that.
6. **Prefer the narrowest write**: `--only-new-banks` (writes only banks that do not exist yet) or `--banks A,B`.
7. **Verify after a write**: `omp-memory import verify`; it must exit 0. If something is wrong the run can be undone:
   `omp-memory import undo --run <id>` (a report), then the same with `--apply` only if the user agrees. After an
   undo the banks the import created are new again for `--only-new-banks`.
8. The importer needs the Mnemopi library at exactly omp's version. If `doctor` says the library is missing or not omp's
   version, tell the user and offer `omp-memory setup --dry-run` (what it would download); run `omp-memory setup` only
   when they agree. There is no way to use another version, and none should be suggested. `doctor` shows the library as
   `OK` only when it has passed the importer's write test; a `WARN ... not yet verified` means `omp-memory doctor --deep`
   (or `setup`) has to run it first. A failing test names its step: report that line, do not work around it.
9. Use a scratch copy (`--banks-dir /tmp/scratch-banks`, made with `sqlite3 SRC .backup DEST`) for anything experimental.

## Typical requests

- "What memory do I have?" -> `omp-memory sources`, then `omp-memory banks`.
- "Name this folder's memory" -> `omp-memory labels set <bank> "<label>"` (1 to 60 characters).
- "Import my Claude Code memory" -> `doctor` (and `setup` if it says the library is missing), `backup --apply`, `restore-drill`, `import dry-run`, show the plan, wait for
  the user, then `import import --apply --only-new-banks` (or the scope they choose), then `import verify`.
- "Is memory healthy?" -> `omp-memory doctor` (add `--deep` to run the importer's write test and its SQL-parity, schema and bank-id checks).

`omp-memory --help` and `omp-memory import --help` list every command and flag. The plugin's README has the full safety model.
