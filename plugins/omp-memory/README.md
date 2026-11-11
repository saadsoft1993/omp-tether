# omp-memory

A command line for **your own** [omp](https://github.com/can1357/oh-my-pi) memory. omp keeps one local memory bank per working
folder (`~/.omp/agent/memories/mnemopi/banks/<bank>/mnemopi.db`). `omp-memory` lists those banks with a readable folder label,
shows what other coding tools have stored about the same projects (Claude Code, claude-mem, Codex), **imports** Claude Code memory
into the right banks, backs the banks up, and checks that they are healthy.

> **Beta.** `omp-memory` 0.1.0 writes your real omp memory banks. It has been tested (the suites, a smoke cycle on synthetic data on macOS and Linux, and a write test against nine omp library releases), but its most recent changes are verified by those tests only, and no third-party audit has been done. **Make a verified backup of your banks (`omp-memory backup --apply`, then `omp-memory restore-drill`) before any `--apply`, and read the dry run first.** Known limits are listed under [Known limits of this beta](#known-limits-of-this-beta).

- **Read-only until you say otherwise.** Only `import import --apply` and `import undo --apply` write a bank, and nothing adds `--apply` for you.
  Even then the import refuses without a dry run from the last 24 hours and, when it writes into a bank that already holds anything of yours, a verified backup from the last 24 hours ([Safety gates](#safety-gates)).
- **Nothing happens on install.** Installing the plugin writes no file outside omp's plugin folders, runs nothing and contacts nothing.
- **Your memory stays on your machine.** No telemetry, no update check. The network is used by `omp-memory setup`, which you run, by `import import --apply` only when `setup` was skipped and omp's library has to fetch its embedding runtime and model, and by the optional
  Hindsight export, which you start ([Network and privacy](#network-and-privacy)).
- **This is the command line and a skill, nothing else.** There is no browser page for these banks.

## Prerequisites

| You need | Version | Notes |
|---|---|---|
| [omp](https://github.com/can1357/oh-my-pi) | 18.18.0 to 18.18.8 (the library releases are verified; the real omp was run at 18.18.0 and 18.18.3); other releases only if they pass the write test | The importer writes through omp's own Mnemopi library at **exactly** your omp's version, so it follows your omp (see [`setup`](#4-fetch-the-library-once-setup)) |
| [Bun](https://bun.sh) | 1.4.4 or newer | Runs the importer. omp's built-in Bun is not used. Found on `PATH`, in `~/.bun/bin`, in `~/.cache/omp-relay/bun-*/` (the one `omp-private-relay` downloads) or in Homebrew; or set `OMP_MEMORY_BUN`. `curl -fsSL https://bun.sh/install \| bash -s bun-v1.4.4` installs one (it needs `curl`, `unzip` and `ca-certificates`; on Debian or Ubuntu: `apt install curl unzip ca-certificates`) |
| Python | 3.10 or newer | Standard library only. macOS's own `/usr/bin/python3` is 3.9 and too old; set `OMP_MEMORY_PYTHON` to an absolute path to force one |
| `sqlite3`, and `shasum` or `sha256sum` | any | Used by the backup scripts (Debian or Ubuntu: `apt install sqlite3`) |
| `lsof` | optional | On Linux without it, the check for open banks reads `/proc/<pid>/fd` instead. On macOS it is built in |
| Disk and network, once | about 130 MB for the library, about 450 MB for the embedding runtime and model | Only for `omp-memory setup` ([what it fetches](#network-and-privacy)); the embedding part is shared with omp's own cache |

Supported: macOS and Linux, one user account on one machine. Not supported: Windows; a machine several people share. What was actually run is in [Compatibility](#compatibility).

## Quickstart

About ten minutes. Every step says what it writes. Steps 1 to 5 only read (step 4 downloads); the first write to a memory bank is step 8.

### 1. Install

```
omp plugin marketplace add saadsoft1993/omp-tether
omp plugin install omp-memory@omp-tether
ln -s ~/.omp/plugins/node_modules/omp-memory/bin/omp-memory ~/.local/bin/omp-memory   # optional: puts the command on your PATH
omp-memory --version
```

Expected: `omp-memory 0.1.0`. (To install from a local checkout, give `omp plugin marketplace add` the checkout's path.) Writes: omp's plugin folder only.

### 2. Ask `doctor` what is missing

```
omp-memory doctor
```

`doctor` changes nothing. It lists what is ready and what is not, and prints the command that fixes each failure. A library is only `OK` once the importer's write test has passed for it (see [Safety gates](#safety-gates)); before that it is a `WARN` that names `omp-memory doctor --deep`. On a bare machine the first failure is Bun, and the library line is a warning until Bun is there:

```
      CHECK           DETAIL
OK    python          3.14.7 (/opt/homebrew/opt/python@3.14/bin/python3.14)
FAIL  bun             no Bun 1.4.4 or newer
OK    omp             18.18.8 (~/.local/bin/omp, from PATH)
WARN  library         could not ask the importer which library it would use (no usable Bun, or no omp version)
WARN  banks dir       ~/.omp/agent/memories/mnemopi/banks does not exist (no omp memory yet, or a different location)
OK    sources         found: instructions; codex: no Codex memory database found; claude-mem: claude-mem database not found
…
  fix (bun): install Bun 1.4.4 or newer, or set OMP_MEMORY_BUN
  fix (library): fix the bun check above
  fix (banks dir): omp-memory config set banksDir <path>

not ready: fix the failures above
```

With Bun installed, the library line becomes the next thing to fix (`FAIL library  no @oh-my-pi/pi-mnemopi at omp's exact version 18.18.8 (looked at: …)`, with the fix `omp-memory setup`). (Sample output from a bare scratch home; paths shortened.)

A `WARN` on `safety records` is normal before your first backup: it only says that `import --apply` has nothing recorded yet.

### 3. Look around (read-only)

```
omp-memory banks       # every bank: folder label, group, rows, rows per source, folder
omp-memory sources     # what each source holds, and how many rows an import wrote from it
```

```
BANK                   LABEL         GROUP  ROWS  FACTS  SOURCES  FOLDER
smoke-c-27yi2sipe0x84  work/smoke-c  work   1     0      omp 1    ~/work/smoke-c

SOURCE        FOUND  COUNTS                                          IMPORTED ROWS  PATH                                                    NOTE
omp-mnemopi   yes    banks 1, memories 1, facts 0                    -              ~/.omp/agent/memories/mnemopi/banks
claude-code   yes    memory files 8, project folders 4, ~/.claude 8  0              ~/.claude*/projects/*/memory
codex         no     -                                               -              ~/.codex/memories_*.sqlite                              no Codex memory database found
claude-mem    no     -                                               0              ~/.claude-mem/claude-mem.db                             claude-mem database not found
instructions  yes    files 0, global 0, workspace 0, repo 0          -              CLAUDE.md, AGENTS.md in the groups and project folders
```

(Sample output from a scratch home with synthetic data.) Neither command prints memory text.

### 4. Fetch the library once: `setup`

The importer writes through the library omp itself uses, the published npm package `@oh-my-pi/pi-mnemopi`, at exactly the version `omp --version` prints.
Run `setup` once per omp version. It is the command that downloads the library, and `--dry-run` shows what it would do first (and, with a library already installed, runs and fetches nothing):

```
omp-memory setup --dry-run
omp-memory setup
```

```
omp-memory setup: omp 18.18.3
will fetch from https://registry.npmjs.org: @oh-my-pi/pi-mnemopi@18.18.3 and its exactly pinned dependencies (pi-ai, pi-utils, pi-catalog, pi-natives and its binary for this platform; about 130 MB)
into ~/.cache/omp-memory/lib/18.18.3; bun add also keeps the packages it downloads in Bun's own install cache (~/.bun/install/cache), and the self-test result is cached under ~/.cache/omp-memory
then it embeds one sentence, which downloads the local embedding runtime (fastembed, onnxruntime) and the model BAAI/bge-base-en-v1.5 into omp's cache directory unless they are already there (about 450 MB)
  bun: bun add v1.4.4 (38afee6c8)
  bun: installed @oh-my-pi/pi-mnemopi@18.18.3 with binaries:
  bun: - mnemopi
  bun: 8 packages installed [81.00ms]
  bun: Saved lockfile
installed @oh-my-pi/pi-mnemopi@18.18.3 into ~/.cache/omp-memory/lib/18.18.3
running the library self-test (the importer's write sequence in a scratch bank)...
embedding one sentence to prepare the local embedding runtime...
repeating the self-test with the real embedding model...
setup complete
{
  "ok": true,
  "ompVersion": "18.18.3",
  "action": "installed",
  "library": { "kind": "cache", "path": "~/.cache/omp-memory/lib/18.18.3/node_modules/@oh-my-pi/pi-mnemopi", "version": "18.18.3" },
  "embeddings": "ready",
  "selfTest": "passed (174 ms with a stand-in embedder, 7049 ms with BAAI/bge-base-en-v1.5)",
  "problems": []
}
```

`setup complete` is followed by the JSON report (the progress lines go to stderr, the report to stdout; `"ok": false` and exit status 1 when anything failed). The `bun:` lines are the installer's own.
**It leaves nothing half-installed.** When the library fails its SQL check or the write test, `setup` removes what it just installed, says so (`removed <dir>: the library was not left installed`) and exits 1; an interrupted download is removed too, and a directory `setup` did not make is never replaced. (A failed embedding step keeps the complete library. After it, `setup` removes only its own ownership token and the folders it created itself, each only while it is empty and unchanged. It never deletes anything else in omp's runtime cache, including a half-finished install; it tells you how to remove that by hand.)

Writes: `~/.cache/omp-memory/lib/<version>/`, and omp's own embedding cache (`~/.omp/cache/fastembed*`) if the model was not there. If npm has no package at your omp's exact version, `setup`
refuses and installs nothing. Packages and licenses: [`NOTICE`](NOTICE). After you update omp, run `setup` again.
(`--no-embeddings` skips the embedding download; the first `import --apply` then fetches it.) Run `omp-memory doctor` again: the `library` line is now `OK`.

### 5. Close omp sessions that use the banks you will write

The importer refuses a bank another process has open. `omp-memory doctor` lists the holders (`WARN  open banks  1 bank(s) are open in other processes: <bank> (pid 4242)`). Quit those omp
sessions. The simplest plan is to import only into banks that do not exist yet (`--only-new-banks` below), which no session can have open.

### 6. Back up, and prove the backup restores

```
omp-memory backup --apply        # a verified copy of every bank, recorded for the import's check
omp-memory restore-drill         # restores it into a scratch folder and compares
```

```
databases: 1, bytes: 372736, seconds: 0
BACKUP_DIR=~/Backups/omp-memory/20261111T114520Z
backup verified (1 database(s), checksums re-read) and recorded in ~/.config/omp-memory/state.json: `import --apply` accepts it for 24 hours
…
RESULT: OK, identical to source
```

Writes: `~/Backups/omp-memory/<UTC stamp>/` (`0700`; files `0600`) and `~/.config/omp-memory/state.json`. A backup is a full copy of your memory: as sensitive as the banks. Without `--apply`, `backup` only prints its plan.

**No banks yet?** On a machine where omp has not written any memory, `backup` says `nothing to back up` and exits 0, and the first `import --apply` does not ask for a backup either: it only creates new banks, so there is nothing a backup could restore. The dry run is still required.

### 7. Dry-run the import and read the plan

```
omp-memory import dry-run --only-new-banks --out ~/omp-memory-plan.json
```

It prints a JSON plan, one entry per bank, and a line per **held** item. Check these:

- `counts`: `found`, `wouldImport` (what the apply would write; the number the state file records), `skippedHeld` (items that still look like a secret after redaction), `skippedBinary` and `skippedTooLarge` (below), `errors`; and the `unresolved` list: project folders the importer could not match to a real folder (their items go to an `unassigned` bank that omp never recalls, or are held back with `--skip-unassigned`).
- `held`: items listed by key and kinds only (`hold:password-value,secret-assignment`: one item can match several rules; a kind without the `hold:` prefix comes from the redaction step's own residual scan), never by text. They are **not** imported; see [held credentials](#faq). An item whose origin, file name, folder or source extras (tags, document ids) look like a secret or contain a control character is held too: it is listed as `held#N` (a number by first appearance in that run, derived from nothing in the value) with kind `hold:provenance-secret` or `hold:provenance-unprintable`, and `--release-held` cannot release it; rename or remove what carries the secret. A bank whose name would be derived from such a folder is shown as `bank#N` in every report, and no bank is created under that name; `project-map` shows such a project tag or folder as `project#N` or `folder#N`.
- `skippedBinary` / `skippedTooLarge`: files that are not text (a NUL byte, or more than 2% undecodable characters) or longer than **1,000,000 characters** (the longest text the redaction step accepts). They are listed by key and reason (`binary`, `tooLarge`; one `skipped (...)` line each on stderr), are never imported, and are not errors. Split a large file to import it. A file over 4,000,000 bytes is not read at all; it appears only in `excluded` and `warnings`. A memory file that is a link leading outside its project's memory directory, a FIFO or a device is skipped with a reason in the same places.
- `perBank`: which banks would receive rows. `--only-new-banks` leaves every bank that already exists untouched, and counts as new a bank that an earlier import of this tool created and that holds no rows now (after an undo).

`import scan` gives the same shape without redaction or banks, so its `wouldImport` is an upper bound (the report says so); only `dry-run` gives the real number.

```
held: ~/work/demo-b#secret	hold:password-value,secret-assignment
skipped (binary): ~/work/demo-c#random	it contains a NUL byte
skipped (too large): ~/work/demo-c#big	1,000,105 characters; the limit is 1,000,000 characters (the redaction step refuses longer text)
{ "runId": "imp-20261111T114616Z-577078", "command": "dry-run", "source": "claude-memory", "apply": false,
  "counts": { "found": 5, "imported": 0, …, "skippedHeld": 1, …, "skippedNotTargeted": 1 },
  "perBank": [ { "bank": "demo-a-zxzfvpki3svx", …, "wouldImport": 2 }, { "bank": "demo-b-1l9a0wbesjf9h", …, "wouldImport": 1 }, … ], … }
dry run recorded in ~/.config/omp-memory/state.json: `import import --apply` with the same flags is allowed for 24 hours (after `backup --apply`)
```

The plan also carries at most three redacted 80-character samples per source and type (never of an item with a finding, a redaction error or a provenance hold, whatever the ledger says); treat the plan file (`0600`) and `<plan>.samples.json` as private. The plan carries a `planDigest` too: `--apply` makes a plan again and refuses when its digest differs from the one your dry run recorded (a memory file was added, edited or removed, or an item became held), so what is written is what you read.
Writes: the `--out` file and the state file. `--out` and the samples file must be outside the banks directory, must not be the ledger or an input of the run, and, when they already exist, must be regular files holding an earlier report, samples file or project-map draft; otherwise the command is refused. They are written through an exclusive, randomly named temporary file.

### 8. Import

```
omp-memory import import --only-new-banks --apply
```

Use **exactly the flags of the dry run** (plus `--apply`; `--out` and `--run-id` may differ; a flag given twice, or a value on a flag that takes none, is refused). The importer re-checks every gate (including the library write test, once, cached), makes the embeddings of the new rows with the bank's own model first, then writes rows, annotations and embeddings in one transaction per bank:

```
backup ~/Backups/omp-memory/20261111T114730Z (0 h 0 min old, 1 database(s)) verified
note: --apply is set: import will write to the banks in ~/.omp/agent/memories/mnemopi/banks
{ "runId": "imp-20261111T114735Z-383282", "command": "import", "apply": true,
  "counts": { "found": 5, "imported": 3, …, "errors": 0, "skippedHeld": 1, … }, … }
```

Note the `runId`. Writes: the banks it names, and `~/.config/omp-memory-import/ledger.json` (what came from where, so a re-run skips it, and which banks this tool created).

**Running it again** is safe and needs no new backup for banks this tool created and that still hold only what it wrote: the gate exempts them (the ledger records which banks they are), because a backup cannot protect rows the importer put there itself. Once omp has written anything of its own into such a bank, or when the run targets a bank that existed before, the backup requirement applies as before (`omp-memory backup --apply`, then the dry run, then the import).

### 9. Verify

```
omp-memory import verify
```

Expected: `"ok": true` and `"problems": []`, with per-bank counts of what was expected, found, and any missing embedding, full-text entry or provenance. Exit status 1 on any problem. Run it right after an import: `verify` checks the runs the ledger still holds, so after an undo it says `no runs to verify`.

### 10. Undo, if you want to

```
omp-memory import undo --run imp-20261111T114735Z-383282            # a report; nothing is removed
omp-memory import undo --run imp-20261111T114735Z-383282 --apply    # removes exactly that run's rows
```

Other rows, including those omp wrote itself, are never touched. After an undo the same files can be imported again, with or without `--only-new-banks`: a bank directory the import created stays on disk, empty, and counts as new again for as long as it holds no row. omp has nothing to recall from an empty bank; delete the directory yourself if you want it gone.

## Safety gates

| Gate | What it stops | Escape (use only when you know why) |
|---|---|---|
| No `--apply`, no write | A plan or a report is all `import`, `undo`, `backup` and `export-hindsight` give you without `--apply` | none: pass `--apply` yourself |
| Verified recent backup | An import with nothing to go back to. `import --apply` needs a backup made by `omp-memory backup --apply` in the last 24 hours, still on disk, matching its checksums, and covering every existing bank the import would write. Which banks it would write is asked of the importer again when `--apply` runs (a plan made a moment ago), not read from the day-old dry run. A bank that does not exist yet, and one an earlier import of this tool created that holds only rows it wrote (or none, after an undo), needs none: a first import on a machine without banks asks for no backup | `--no-backup` (says so on stderr) |
| Dry run of the same options | An import you never looked at. Needs `import dry-run` with the same source and target flags (`--only-new-banks`, `--banks`), banks directory and ledger, and the same content of any file a flag names (`--release-held`, `--project-map`, `--global-allowlist`, ...), in the last 24 hours. At `--apply` a plan is made again and its digest (`planDigest`: every item that would be written or held, by bank, origin key and content hash) must equal the dry run's: a memory file added, edited or removed since, or a changed held item, makes it refuse until you dry-run again | `--skip-dry-run-check` (says so on stderr) |
| Open banks | A write into a bank an omp session is using. **Best effort**: made before the plan, again right before each bank's write and before each undo deletion, with `lsof` or, on Linux without it, `/proc/<pid>/fd` (which sees only processes of your own user). `lsof` reporting an error counts as unknown and refuses. It cannot exclude a process that opens the bank in the instants between the check and the write | `--force-closed`, only after you closed the sessions yourself (`omp-memory` passes it on with a warning) |
| Exact library version, and a write test | Writing with a different Mnemopi than your omp runs. Also refuses when the SQL the importer relies on (compared by structure, so layout and letter case do not matter) is missing from the package or the omp binary; when the library fails the **write test**: through the loaded library, in a scratch bank under `/tmp`, the importer performs its exact write sequence (restore insert, annotation, embedding) and checks the row, its full-text entry, its annotations, its embedding, `consolidated_at` and the trim exemption, then deletes it again and checks that nothing is left; when a bank's tables, triggers or full-text table differ from a fresh library database; and when a bank uses more than one embedding model. The refusal names the failing step. `setup` and `doctor --deep` run the test; it is cached per library version | none: run `omp-memory setup` (and see [Compatibility](#compatibility)) |
| One transaction per bank | A half-written bank. The embeddings are made before the write; rows, annotations and embeddings then go in one transaction, so a failure or a kill leaves nothing or everything. Native rows are never touched (the write rolls back if their count or a digest of their content changes); if any embedding is missing nothing is written. Another importer run at the same time is refused (a lock file beside the ledger). A bank directory, database or side file that is a symbolic link, or a hard-linked database, is refused | none |
| Held credentials | A secret reaching a bank. After redaction, an item that still looks like a password, key, token or credential URL is held back and is **never** imported by default | `--release-held KEY[,KEY]` or a file of keys, for items you reviewed one by one |
| Nothing global by default | Rows landing in the global bank | a reviewed `--global-allowlist FILE` |

Everything else is read-only. `labels set`, `config set` and the state file write only their own small files (`0600`, atomic, through an exclusive temporary file); each refuses a destination that is not a file of its own kind (a database, a link, a directory, another JSON file) or lies inside the banks directory, and leaves it untouched. Opening a WAL-mode bank read-only can create (and later remove) the `-shm` and `-wal` files next to it, as any SQLite reader does.

## What it reads, writes and where data goes

**Reads**

| What | Where | Used by |
|---|---|---|
| omp's banks | `~/.omp/agent/memories/mnemopi/banks/<bank>/mnemopi.db` (or under `$PI_CODING_AGENT_DIR`; the `banksDir` setting) | everything; read-only except the two write commands |
| Claude Code memory files | `~/.claude*/projects/<dir>/memory/*.md` (`MEMORY.md` indexes are skipped) | `sources`, `import` |
| claude-mem | `~/.claude-mem/claude-mem.db`: `sources` runs `COUNT(*)` on `session_summaries`, `observations` and `user_prompts` and shows `schema not recognised` when those tables are missing; `import --source claude-mem-summaries` reads **only** `session_summaries` as text (observations and prompts are counted, never read as text), but through a snapshot copy of the whole database in a `0700` folder under `/tmp`, which exists for the duration of the command and is removed when it ends or on SIGINT or SIGTERM (not on SIGKILL) | `sources`, `import --source claude-mem-summaries` |
| Codex memory | `~/.codex/memories_*.sqlite`, counted only | `sources` |
| Instruction files | `CLAUDE.md`, `AGENTS.md` in your groups and project folders, counted only | `sources` |
| A Hindsight export file you made | the file you name with `--hindsight-export` | `import --source hindsight` |

**Writes**

| Where | What | Created by |
|---|---|---|
| the banks | rows, annotations and embeddings, written through omp's own library; each row carries its origin and run id | `import import --apply`, `import undo --apply` |
| `~/.config/omp-memory/config.json` (`0600`, dir `0700`) | paths only, and your `groups` | `config set`, `config set-group` |
| `~/.config/omp-memory/state.json` (`0600`) | when each verified backup and dry run was made, where the backup is, its checksums, a hash of the options; no memory text | `backup --apply`, `import dry-run` |
| `~/.config/omp-memory-import/ledger.json` (`0600`) and `ledger.json.lock` while an `--apply` import or undo runs | for each imported item: bank, row id, hash, run id, state; and tombstones. Origin keys name folders and file names; no memory text. The lock file holds a pid and a start time and is removed when the command ends; a second `--apply` import or undo is refused while it exists and its process is alive | `import import --apply`, `import undo --apply` |
| `~/.config/omp-memory/labels.json` (`0600`, dir `0700`) | `{bank: label}`. Same format as the other label stores of this kind, so a tool that keeps its file elsewhere can point `labelsFile` at it | `labels set` |
| `~/Backups/omp-memory/<UTC stamp>/` (`0700`; files `0600`) | a full copy of every bank, `SHA256SUMS`, `MANIFEST.tsv` | `backup --apply` |
| `~/.cache/omp-memory/lib/<version>/` | the Mnemopi library and its dependencies (about 130 MB). `setup` removes what it installed when the library fails its checks; it never leaves a half install | `setup` |
| `~/.cache/omp-memory/selftest/<version>-<fingerprint>.json` (`0600`) | the result of the write test for that library: steps and timing, no memory text | `setup`, `doctor --deep`, the first `import --apply` |
| `~/.omp/cache/fastembed*` | the local embedding runtime and model; omp's own cache, shared with omp | `setup`, or the first `import --apply` |
| the file given as `--out` (`0600`) and `<out>.samples.json` | an importer report; the samples file holds at most three redacted 80-character samples per source and type. Written through an exclusive, randomly named temporary file; never inside the banks directory; carries a `format` field, and an existing file at that path is replaced only when it carries one (an older or unrelated file is refused and left as it is) | `import … --out` |
| temporary folders under `/tmp` | a scratch restore, a one-line `python3` link, a snapshot copy of the claude-mem database (`memory-import-snapshot-*`) and the write test's scratch bank (`memory-import-selftest-*`, synthetic rows); each deleted when the command ends | `restore-drill`, `backup`, `import` of claude-mem, the write test |

Nothing else: no logs, no audit trail, no cache. The banks themselves are omp's.

## Commands

| Command | What it does | Writes |
|---|---|---|
| `omp-memory doctor [--deep] [--json]` | Python, which Bun, omp and library are used (versions and paths), whether the library has passed the write test (`OK` only then; otherwise `WARN: not yet verified` or `FAIL` with the step), the banks directory, which sessions hold banks open, sources, groups, labels, safety records, vendored code. `--deep` runs the write test now and adds the importer's SQL, schema and bank-id checks | the test's cache file, `--deep` only |
| `omp-memory setup [--dry-run] [--no-embeddings]` | Installs `@oh-my-pi/pi-mnemopi@<your omp's version>`, runs the write test on it, warms the embedding model and repeats the test with it. Fails with exit 1, and removes what it installed, when the library does not pass | the cache directories |
| `omp-memory banks [--json]` | Every bank: label, group, folder, rows, facts, rows per source. Empty banks are counted, unreadable ones named | nothing |
| `omp-memory sources [--json]` | What each source holds, and how many bank rows an import wrote from each | nothing |
| `omp-memory labels list` / `set <bank> <label>` / `clear <bank>` | Folder labels (1 to 60 characters, unique) | the labels file |
| `omp-memory import <scan\|dry-run\|import\|verify\|undo\|doctor\|project-map> [flags]` | The bundled importer, flags passed through ([Importing](#importing-in-detail)) | banks, only `import --apply` and `undo --apply` |
| `omp-memory backup [--dest DIR] [--apply]` | `VACUUM INTO` copy of every bank with checksums, re-verified and recorded. Prints its plan until `--apply` | backups directory, state file |
| `omp-memory restore-drill [--backup DIR] [--strict]` | Restores a backup (newest by default) into a scratch folder and compares with the backup and the live banks | `/tmp` scratch, deleted afterwards |
| `omp-memory export-hindsight [--apply] [flags]` | **Optional**, only if you keep memories in a Hindsight server: a read-only export. A plan until `--apply`; no defaults (`--banks`, `--out`, and a connection are required; also as `OMP_MEMORY_HINDSIGHT_*` variables) | the export folder, with `--apply` |
| `omp-memory config show\|set\|unset\|path\|set-group\|unset-group` | Where it looks for things and how folders are grouped ([Configuration](#folder-labels-groups-configuration)) | the configuration file |

Exit status, the same for every command (and for the importer under `import`): **0** success; **1** a refusal or a failed check: a gate said no (no dry run, no backup, an open bank, a library that fails its test, a registry that cannot be reached), `verify` or `doctor` found a problem, an undo left a row behind; **2** a usage or environment error: an unknown flag or command, a missing value, a flag given twice, a value on a flag that takes none, `import import` without `--apply`, no usable Bun, an unreadable or refused configuration file; **3** an internal error or an unexpected failure inside the importer (a bug: report it with the category shown); **128 + n** stopped by signal n (130 for Ctrl-C, 143 for SIGTERM). `--json` is available on the read commands. The commands print counts, bank names, folder paths and item keys, not memory text. A dry run and an import report also print what a file name or a front-matter value looks like only after control characters are removed and anything secret-like is withheld; it is not a promise that a file name holds nothing private.

## Importing in detail

The importer ([full reference](vendor/memory_import/README.md)) reads `~/.claude*/projects/<dir>/memory/*.md` and writes each file into the bank of the project's real folder, through the same library omp uses.
The rows look like what omp's own `retain` leaves, keep their original timestamps, carry their origin (`source`, `origin`, `originKey`, `originHash`, run id) and are embedded with the bank's model.

| `--source` | Reads | Notes |
|---|---|---|
| `claude-memory` (default) | Claude Code memory files | Imported. A file reached through a symbolic link counts once |
| `claude-mem-summaries` | claude-mem's `session_summaries` | Imported. Its observations and prompts are never read. A summary's folder comes from its session file; without one, the project name decides when exactly one known folder has that name. Use `--project-map FILE` for any folder that stays unresolved |
| `hindsight` | the remainder of a Hindsight export file (`--hindsight-export FILE`) | Imported. Needs an export you made with `export-hindsight` or your own |
| (none) | Codex, instruction files | counted by `sources`, not imported |

Flags you will use: `--only-new-banks` (write only banks that do not exist yet), `--banks A,B` (only these banks), `--release-held KEY[,KEY]|FILE`, `--global-allowlist FILE`, `--skip-unassigned`, `--project-map FILE`, `--run-id`, `--out FILE`.
`omp-memory` fills in `--banks-dir`, `--ledger`, `--omp-bin` and `--python` (and `--lib-dir`, `--conf`, `--folder-roots` when you configured them); it never adds `--apply`. A flag you give twice is refused, and so is a value on a flag that takes none (`--apply=false` is an error, not "off"). The importer is started in the plugin's own directory, never in the directory you are in, and a path you give is made absolute first (relative to where you are).
The library lookup order is your `libDir` setting, omp's own plugin directory (`$OMP_PLUGINS_DIR`, else `~/.omp/plugins`), then `~/.cache/omp-memory/lib/<version>`; a place is used only when its version equals omp's.

Re-running is safe. An item is identified by its origin key, not by its content: one already imported and unchanged is skipped; one whose source changed is **reported, never overwritten**; one you deleted from a bank
is tombstoned and never imported again; the same text already in a bank is skipped. Claude Code memory is meant to be imported once; a later run reports what changed. Folders that cannot be resolved go to an `unassigned` bank omp never recalls.
Files that are not text, or longer than 1,000,000 characters, are skipped and listed with the reason (see step 7). What a re-run needs: a fresh dry run (24 hours); a new backup only if it writes into a bank that is not one this tool created and left holding only its own rows. After an `undo`, the banks it emptied are new again for `--only-new-banks`.

## Folder labels, groups, configuration

`omp-memory labels set demo-a-zxzfvpki3svx "Demo A"` gives a bank a label. The default label is the last two segments of the bank's folder. A bank's **group** is the first directory below your home that contains its folder
(`~/projects/demo` → `projects`; `Other` outside your home), unless you name roots yourself: `omp-memory config set-group Demo projects/demo ~/clients`. Bank directories are never renamed.

Precedence: command line flag, environment variable, configuration file (`~/.config/omp-memory/config.json`, or `$XDG_CONFIG_HOME/omp-memory/config.json`, or `$OMP_MEMORY_CONFIG`), default. `omp-memory config show` prints each value and where it came from.
A configuration file that is a symbolic link, not yours, writable by group or others, or has an unknown key is **refused**, because it decides which programs run.

| Setting | Environment | Default | Meaning |
|---|---|---|---|
| `banksDir` | `OMP_MEMORY_BANKS_DIR` | `$PI_CODING_AGENT_DIR/memories/mnemopi/banks`, else `~/.omp/agent/memories/mnemopi/banks` | omp's banks |
| `libDir` | `OMP_MEMORY_LIB_DIR` | none (lookup above) | an install root with `node_modules/@oh-my-pi/pi-mnemopi`, used instead of the lookup; must still equal omp's version |
| `ledger` | `OMP_MEMORY_LEDGER` | `~/.config/omp-memory-import/ledger.json` | what was imported, tombstones (outside the banks, `0600`) |
| `backupsDir` | `OMP_MEMORY_BACKUPS_DIR` | `~/Backups/omp-memory` | where `backup` writes `<UTC stamp>/` |
| `banksConf` | `OMP_MEMORY_BANKS_CONF` | none | optional conf file of `<group> <folder relative to home>` lines; read only when you set it |
| `stateFile` | `OMP_MEMORY_STATE_FILE` | `~/.config/omp-memory/state.json` | the recorded backups and dry runs that `import --apply` checks |
| `groups` (file only) | none | none | `{group name: [folders]}`, set with `config set-group` |
| `labelsFile` | `OMP_MEMORY_LABELS_FILE` | `~/.config/omp-memory/labels.json` | the labels store |
| `bun` | `OMP_MEMORY_BUN`, `BUN_BIN` | searched | the Bun that runs the importer; a configured Bun is used or refused, never replaced |
| `ompBin` | `OMP_BIN` | `omp` on `PATH` | the omp whose version the importer compares with the library |

Paths must be absolute or start with `~/`; one with a `..` component is refused. `HOME` decides what `~` and the locations of the other tools' files mean.

## Network and privacy

`banks`, `sources`, `labels`, `config`, `doctor`, `backup`, `restore-drill` and `import scan|dry-run|verify|undo|doctor` open no connection. Memory text is never sent anywhere. The cases that use a network:

- **`omp-memory setup`** (you run it). It asks the npm registry (`registry.npmjs.org`) whether `@oh-my-pi/pi-mnemopi@<your omp's version>` exists and downloads it and its pinned dependencies with `bun add`; then it embeds one sentence,
  which makes omp's library fetch the embedding runtime (`fastembed`, `onnxruntime-node` and their dependencies, from npm) and the model (from `huggingface.co`) into omp's cache. On Linux x64 the `onnxruntime-node` install script also downloads CUDA binaries from GitHub
  unless `ONNXRUNTIME_NODE_INSTALL_CUDA=skip` is set (read from that package's script; that platform has not been run). `setup --dry-run` prints what it would fetch and uses no network.
- **`import import --apply`** embeds rows with omp's local model. If `setup` was skipped and the runtime or model was never downloaded on this machine, the library downloads them then.
- **`export-hindsight --apply`** (optional; only if you use Hindsight) reads your own Hindsight banks over the HTTP API, and over `ssh` in its archive mode, as you configure it by flags. It writes nothing to Hindsight.

No analytics, no crash reports, no update check. Nothing is fetched at install time. What `setup` downloads, with names, versions and licenses, is in [`NOTICE`](NOTICE).

## Troubleshooting

| You see | Why | What to do |
|---|---|---|
| `no Bun 1.4.4 or newer; found: …` | No usable Bun on the usual paths | Install Bun 1.4.4 or newer, or `omp-memory config set bun /abs/path/bun` (or `OMP_MEMORY_BUN`) |
| `omp-memory needs Python 3.10 or newer` | Only macOS's 3.9 or an old Python was found | `brew install python` (or your package manager), or set `OMP_MEMORY_PYTHON` to an absolute path |
| `OMP_MEMORY_PYTHON is not a usable interpreter` | The variable names something that is not an absolute path to Python 3.10 or newer | Set it to the absolute path of a Python 3.10 or newer, or unset it |
| `FAIL library  no @oh-my-pi/pi-mnemopi at omp's exact version …` | The Mnemopi library for your omp is not installed, or omp was updated | `omp-memory setup` |
| `WARN library  … not yet verified` | The library is there, but its write test has not run for it (a library from omp's own plugin directory, or after a Bun update) | `omp-memory doctor --deep`, or `omp-memory setup`. Not an error: the first `import --apply` runs the test too |
| `the library self-test failed at step "…"` (from `setup`, `doctor --deep` or an import) | The library does not behave the way the importer needs at that step (see [Compatibility](#compatibility)) | Nothing was written and `setup` removed what it had installed. Use an omp version in the verified range, or open an issue with the line (no memory text) |
| `nothing to back up` | `backup` found no bank: omp has not written memory yet, or `banksDir` points elsewhere | Nothing to do: the first import needs no backup. Check `omp-memory config show` if you expected banks |
| `skipped (binary)` / `skipped (too large)` on stderr of a dry run | A memory file that is not text, or longer than 1,000,000 characters (a file over 4,000,000 bytes is not read; it is in `excluded` and `warnings`) | Expected. The key and the reason are in the report; split or remove the file |
| `setup` refuses: npm has no such version | Your omp is newer (or older) than any published library | Nothing to override: wait for the package, or use an omp version that has one |
| `refusing to write: no dry run with these exact options …` | The apply's flags differ from the dry run's, or the dry run is older than 24 hours | Repeat `import dry-run` with the apply's flags (everything except `--apply`, `--out`, `--run-id`) |
| `refusing to write: no verified backup … younger than 24 hours` | No backup, an old one, or one that no longer matches its checksums | `omp-memory backup --apply` |
| `refused: bank <name>: open in process <pid>; close the omp session or pass --force-closed` (exit 1) | An omp session (or any process) has that bank open | Quit the session and run again, or limit the import to new banks with `--only-new-banks` |
| `cannot tell whether omp has it open (no lsof)` | Neither `lsof` nor `/proc` is available, or `lsof` reported an error | Install `lsof`, or close every omp session and pass `--force-closed` once |
| `schema differs from the library` for a bank | That bank was made by a different omp version or edited | There is no override, by design. `omp-memory doctor --deep` says which bank and why; open an issue with that line (no memory text) |
| `WARN safety records` | No backup or dry run recorded yet | Expected before your first backup; steps 6 and 7 |
| `skippedHeld` is not 0 | Items that still look like a secret after redaction | Review them (they are listed by key and kind); release only what is safe, with `--release-held KEY` |
| `unresolved` folders in the plan | The project folder no longer exists or cannot be matched to one | Their items go to an `unassigned` bank omp never recalls. Add `--skip-unassigned` to hold them back, or name the folders in a `--project-map FILE` ([importer reference](vendor/memory_import/README.md)) |
| `embedding: … returned no vectors` (from `setup`), or the first import cannot embed | The embedding runtime that omp's library installs on first use (fastembed and `onnxruntime-node`, into `~/.omp/cache/fastembed-runtime/`) failed half way. The usual cause is an old `node` first on `PATH` (before 18): `onnxruntime-node`'s install script runs with it. `setup` says so and leaves a half-finished runtime in place (it removes only its own token and empty folders it made) | Put a current `node` first on `PATH` (or remove the old one) and run `omp-memory setup` again. By hand: `rm -r ~/.omp/cache/fastembed-runtime` (omp downloads it again; only when nothing else is using it); this is also what to do after a failed `setup` embedding step, and `rm -r ~/.cache/omp-memory/lib/<version>` only if the library itself is damaged, then `omp-memory setup` |
| `no runs to verify` | `verify` found no run in the ledger (nothing imported yet, or the run was undone) | Run it right after an import, or pass `--run ID` |
| `doctor` says `vendored code … differ` | A file under `vendor/` was edited after the plugin was installed | Reinstall the plugin; do not edit `vendor/` |

## Known limits of this beta

- **Names in files.** Files that have to hold the real name to work keep it: the state file's directories and bank file names, backup copy names and `MANIFEST.tsv`, and folders you type into the configuration. The ledger (`0600`) keeps the real origin keys, bank names, row ids and run ids of what was imported, because `verify` and `undo` find rows by them; items held back are never recorded. They are `0600` files in `0700` directories. A bank, folder or file name that carries a recognised secret is shown as an index (`bank#3`) in command output, but it is in those files.
- **Error messages.** About 55 refusal and usage messages inside the importer are built from names and are scrubbed when they reach the importer's top-level handler, not written as fixed text at their source.
- **Secrets without a recognisable shape.** A bare key of 24 or more characters with no prefix or key word in a name is not detected; neither is a password written in prose. `auth` and `credential` count as key words in the command line but not in the importer.
- **`setup` and the shared embedding cache.** On a failed embedding step `setup` removes only its own token file and the empty folders it created itself, after re-checking each, and keeps everything else in omp's embedding runtime folder. It can leave a partly filled runtime folder behind; remove it by hand if omp cannot embed afterwards.
- **Open banks.** The check that no omp session has a bank open is best effort (see the safety gates).
- **Not tested:** Linux x86-64, musl or busybox userlands, Windows (not supported), omp versions other than 18.18.0 and 18.18.3 with the real binary.

## Uninstall

```
omp plugin uninstall omp-memory@omp-tether
rm -f ~/.local/bin/omp-memory                  # if you made the link
rm -r ~/.cache/omp-memory                      # the library (about 130 MB)
rm -r ~/.config/omp-memory                     # configuration, the recorded backups/dry runs, AND your labels (labels.json lives here)
```

Uninstalling the plugin does **not** remove these, on purpose; keep or delete them as you like:

- `~/.config/omp-memory-import/` (the ledger). **It holds your tombstones: delete it and an item you removed from a bank may be imported again**, and a later run no longer knows what was imported.
- `~/Backups/omp-memory/` (full copies of your memory; delete what you no longer need).
- `~/.omp/cache/fastembed*` (omp's own embedding cache; omp uses it). Your labels are in `~/.config/omp-memory/labels.json`, so the second `rm` above deletes them: copy that file first to keep them.
- Everything an import wrote into the banks. Use `import undo --run ID --apply` first if you want those rows gone.

## FAQ

**Does it close or kill my omp sessions?** No. It never touches a process. It refuses to write a bank that a session has open and tells you the pid; you close the session.

**What are "held" credentials?** After redaction, an item that still contains something shaped like a password, private key, bearer token, `sk-…` / `ghp_…` style token, a credential inside a URL or a long token next to a key name is held back.
It is not imported and not printed: the plan lists the origin key and the kind (`hold:password-value`). Nothing releases it except you naming that key with `--release-held` (a file of keys must be written as `/…`, `~/…` or `./…`), after you looked at the item in its source file. An item whose file name, folder or tags look like a secret is held as `held#N` and cannot be released: rename the file or remove what carries the secret.
Pattern matching cannot catch a secret with no recognisable shape (a bare password in prose), so read the dry run.

**Can a command print a secret that is in a name?** It is built not to, for secrets of known shapes (a `password=…` assignment, a `ghp_…`/`sk-…` key, URL credentials, a private-key header; any case, percent-encoded). What the importer prints, and the reports, samples files, project-map drafts and self-test result it writes, are passed through a filter that replaces a string carrying a recognised secret with an index label (`secret#N`, `bank#N`, `held#N`), as is the command line's own output, `--json` and forwarded output; `labels set` refuses a label or bank name that carries one. Memory text appears only as at most three redacted 80-character samples per source and type in the dry-run samples file. Some files keep real names, see [Known limits of this beta](#known-limits-of-this-beta).

**Is Hindsight required?** No. `export-hindsight` and `--source hindsight` are for people who keep memories in a Hindsight server. Everything else works without it, and nothing connects to one unless you run the export with `--apply`.

**How does this relate to [`omp-hindsight-guard`](https://github.com/saadsoft1993/omp-hindsight-guard)?** They are separate projects that do not depend on each other. `omp-memory` works on the local banks omp keeps on this machine (SQLite files, one per folder); `omp-hindsight-guard` is for people who run their own remote Hindsight server and sit between omp and that server (a redacting proxy, banks per folder on the server, a one-time move of local memories to it). Install either, both or neither. The redaction rules `omp-memory` applies before an import follow the same approach as that project's proxy; the optional `export-hindsight` here only reads from such a server.

**Which sources are supported?** Imported: Claude Code memory files, claude-mem session summaries, and the remainder of a Hindsight export. Counted only: Codex memory databases and instruction files (`CLAUDE.md`, `AGENTS.md`).
omp's own banks are listed, labelled, backed up and verified.

**Is there a browser page for this?** No. The plugin is the command line and a skill.

**Will an import change what omp recalls?** Yes, that is its point: after an import the rows are ordinary omp memory, recalled into prompts and subject to omp's own consolidation and deletion rules. Import into new banks first, read what lands, and keep the backup.

**Why Bun, when omp has one built in?** The importer is TypeScript that links the Mnemopi library; `omp-memory` runs it on a Bun you control and can pin. omp's embedded runtime is not used.

**What if omp updates?** The library must equal omp's version, so `doctor` will say so and `omp-memory setup` fetches the matching one. There is no option to use a different version.

## Compatibility

| | Status |
|---|---|
| macOS arm64, MacBook (Bun 1.4.4, Python 3.14 and 3.10, the real omp 18.18.3) and Mac mini (the real omp 18.18.0, Homebrew Python 3.14) | tested: importer tests, plugin Python suite, shell scripts, the full smoke cycle on synthetic data with the real embedding model |
| Linux arm64, Ubuntu 24.04 container as a non-root user (Bun 1.4.4, Python 3.12, `sqlite3` 3.45, with and without `lsof`) | tested: importer tests, plugin Python suite, the full smoke cycle on synthetic data |
| Linux x64, other distributions, musl or busybox userland | **not tested**. Expected to work (the scripts use only POSIX tools plus `sqlite3` and a SHA-256 tool), but unverified; on x64 see the CUDA note under [Network](#network-and-privacy) |
| Windows | **not supported** |
| omp 18.18.0 and 18.18.3 | **tested** with the real binaries and the matching `@oh-my-pi/pi-mnemopi` releases (structural SQL check, write test, importer tests, smoke cycle). The library releases 18.18.0 to 18.18.8 (all nine) pass the structural SQL check, the write test and the importer's Bun suite |
| any other omp version | not tested. The write test decides at run time: a library that passes is used, one that fails is refused with the failing step |

The machine-readable record is [`compat.json`](https://github.com/saadsoft1993/omp-tether/blob/main/compat.json) and [`docs/compat.md`](https://github.com/saadsoft1993/omp-tether/blob/main/docs/compat.md) in the repository.

## The code under `vendor/`

The importer (TypeScript on Bun, with its tests), the backup, restore-drill and export scripts, and the standalone Python modules for banks, sources and labels are **copied byte for byte** from a source tree,
listed in [`scripts/vendor_manifest.txt`](scripts/vendor_manifest.txt). `scripts/sync-vendor.sh` refreshes them from the source tree named by `OMP_MEMORY_SOURCE_DIR` (and `--check` compares without writing); tests fail when a vendored file differs from its source or from the checksum recorded at
the last sync; `doctor` re-checks the checksums on your machine. See [`docs/vendoring.md`](docs/vendoring.md). The files that contain or derive from omp's code (MIT) are listed in [`NOTICE`](NOTICE).

## Develop

```
make test-memory          # from the repository root: the importer's Bun tests (from this copy) and the Python suite
make memory-vendor-check  # vendor/ against the upstream checkout it was copied from (OMP_MEMORY_SOURCE_DIR); skipped when unset
make check-contrib        # everything, including lint, shellcheck and the extension-load gate that installs this plugin
scripts/smoke.sh          # a whole install-to-undo cycle on synthetic data in a throwaway HOME (--bun PATH, --keep)
```

The importer's Bun tests build scratch banks with the real Mnemopi library: the published `@oh-my-pi/pi-mnemopi` (default 18.18.3, `OMP_MEMORY_TEST_VERSION`), installed once by the importer's own `setup` code into
`<tmp>/omp-memory-test-lib/<version>` (network once; `OMP_MEMORY_TEST_LIB_DIR` points at an existing install root). No omp source tree is used. Every test file raises Bun's 5-second per-test default itself (Bun keeps that limit per file), because
several tests start real processes. The Python suite needs neither omp nor Bun: it uses a scratch `HOME`, fake banks with the real schema, and a stand-in `bun`; one module runs the real importer through the launcher, against the same installed library
and a bank a second process holds open, and is skipped, with the reason, without Bun 1.4.4 or the library.

## Who this protects against

One person, one machine, one account: you, and root, are trusted. It is meant to stop a half-finished import from damaging a bank (best-effort open-bank refusal, exact-version and schema parity, a recent verified backup and a dry run that must match the apply, embeddings made before one transaction per bank, verify and undo),
a secret that survives redaction from reaching a bank by accident (the HOLD stage), and another account on the machine from reading what these commands write (modes `0600`/`0700`). It does **not** stop a process running as you, and it does not make a shared machine safe:
see [`docs/threat-model.md`](docs/threat-model.md). Report a vulnerability as [`SECURITY.md`](SECURITY.md) describes.
