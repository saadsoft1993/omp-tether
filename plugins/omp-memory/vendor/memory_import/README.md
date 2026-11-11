# memory_import

Imports other memory systems into omp's local mnemopi banks (`~/.omp/agent/memories/mnemopi/banks/<bank>/mnemopi.db`),
through the Mnemopi library omp itself uses (the published npm package `@oh-my-pi/pi-mnemopi`, at exactly omp's version; no omp source checkout is involved). Three sources, one command line:

| `--source` | What is read | Where it goes |
|---|---|---|
| `claude-memory` | `~/.claude*/projects/<dir>/memory/*.md` (MEMORY.md indexes skipped). A symlink inside the project's memory directory is followed and counted once; a link that leads outside that directory, a memory directory that is a link out of its project, and anything that is not a regular file (FIFO, device) are skipped with a reason in `excluded` and `warnings`. A file over 4,000,000 bytes is not read | the bank of the project's real folder |
| `hindsight` | the remainder of a Hindsight export (`--hindsight-export FILE`): world/experience facts in state valid that did **not** come from mnemopi | the bank of the `project:<name>` folder from a reviewed project map, else `unassigned` |
| `claude-mem-summaries` | `session_summaries` of `~/.claude-mem/claude-mem.db` (copied with sqlite's online backup first into a private 0700 temp directory that is removed on exit, SIGINT and SIGTERM, not on SIGKILL; only `session_summaries` is read as text, but the snapshot copy holds the whole database while the command runs; observations and user_prompts are never read) | the bank of the summary's session folder |

Run with Bun 1.4.4 or newer (the version omp embeds): `bun cli.ts <cmd> --source <source> [flags]`. Once, before the first command that opens a bank: `bun cli.ts setup`.

| cmd | does |
|---|---|
| `setup [--dry-run] [--no-embeddings]` | the only command that uses the network, and only because you ran it: installs `@oh-my-pi/pi-mnemopi@<omp version>` (and the dependencies it pins) into `~/.cache/omp-memory/lib/<version>` with `bun add --exact --registry=<url>`, after asking the registry that exactly that version exists (it refuses, installing nothing, when it does not), then runs the library self-test (below) and, unless `--no-embeddings`, embeds one sentence so that the local embedding runtime and the model are fetched now, not in the middle of an import, and repeats the self-test with that real model. It prints what it will fetch first; `--dry-run` prints that and stops, also when a matching library is already there: it then runs no self-test, writes no cache and downloads nothing. Nothing is fetched when a matching library is already there. `bun add` also keeps the packages it downloads in Bun's own install cache (`~/.bun/install/cache`, or `$BUN_INSTALL_CACHE_DIR`), and the self-test result is cached under `~/.cache/omp-memory`; nothing else is written outside the install directory. A registry URL (`--registry`, `OMP_MEMORY_NPM_REGISTRY`) must be https, or http on localhost, and must not carry a user name, password, query or fragment (refused, never echoed). The install directory is 0700 and its `package.json` and marker 0600. The child process inherits the whole environment of the command (except `BUN_BE_BUN`). When the SQL check or the self-test fails, what this run installed is removed again (nothing half-installed is left; a library that was already there is only reported on) and the exit status is 1. A failed model download alone keeps the library. The embedding runtime directory (in omp's cache, shared with omp) is claimed before the embedding step by creating it, one level at a time with a plain `mkdir` (no `-p`), with a token file `.omp-memory-owner-<random>` in it; the fingerprint (path, inode, size, modification time) of the token and of each directory this run made is recorded. A directory that is already there, or that another process makes first, is not claimed and is never touched. After a failure this run removes only its own token and the directories it made, each only while it is still empty and still matches its recorded fingerprint (checked again right before it is removed); every other file in the runtime directory, whatever wrote it, including a half-finished install, is preserved, and the message says how to remove the directory by hand. After a success only the token is removed. Residual: between the check and the `unlink`/`rmdir` call another process can still act on the same path; the token has a random name this run chose, and `rmdir` refuses a directory that is not empty, so the exposure is an empty directory another process was about to fill. The embedding model files are never removed. An interrupt during the download removes the partial install too (a failure inside that cleanup is reported and ends with status 3). `setup` never replaces or removes a directory it did not make (`--lib-dir` must be empty, absent, or a previous setup's). |
| `library` | read-only: which Bun, which omp version, where the library was found or why each place was refused, and `selfTest`: what an earlier `setup`, `doctor` or write command cached for exactly this library (`passed`, `failed` with the step, or `not-run`). Exit 1 when none matches. |
| `scan` | counts per folder and type, parse errors, unresolved folders, binary and oversize items. No redaction, no ledger, opens no bank, so every `wouldImport` in its report is an **upper bound** (`extra.wouldImportIsUpperBound`): held items, duplicates, items already imported and banks left out by `--only-new-banks` are only found by `dry-run`, whose `counts.wouldImport` is the real number. |
| `dry-run` | the full plan per bank: would import / duplicate / unchanged / changed / tombstoned / held back / binary / too large, redaction counts, `counts.wouldImport` (what the apply would write), `planDigest` (sha256 of the sorted `bank, originKey, originHash, decision` lines of the items it would write, report as changed or hold back: the same tree and state give the same digest whatever the run id, time or output files; it carries no text), plus `<out>.samples.json` (at most 3 redacted 80-character samples per source and type; an item with any finding, a redaction error or a provenance hold is never sampled, whatever the ledger says about it). Writes nothing but `--out` and the samples file. |
| `import --apply` | writes. Refuses without `--apply`. Needs exactly one of `--expect-plan-digest HEX` / `--no-plan-check` and one of `--backed-up-banks LIST` / `--no-backup-check` (see Guards). Per bank, the embeddings are made first and then the rows, annotations and embeddings are written in one transaction; nothing is written when an embedding is missing. |
| `verify [--run ID]` | per bank: the ledger's rows against the bank's rows by row id and provenance (missing, unexpected, provenance differs), every row embedded with the bank's model, in FTS, has originKey + importRunId + originHash + source, has its `occurred_on` annotation, is consolidated, `PRAGMA quick_check`; and the source's current parse errors, which fail verify. A run found in the banks (rows with this tool's provenance and a run id) that the ledger has no record of is rebuilt from those rows, in memory, and verified too (`recoveredRuns` lists it); this needs no source file. Exit 1 on any problem. |
| `undo --run ID [--apply]` | removes the rows of a run (found by their metadata, not the ledger) that are provably the importer's; reports others and every ledger-backed row that is still in its bank by row id (also when its run id was removed or changed). A run whose record is not in the ledger is rebuilt from the banks' rows first. Without `--apply` it only reports. Exit 1 when a row stays or is not the importer's. |
| `doctor` | read-only on the banks: which Bun, omp and library are used and where the library came from; the SQL the importer relies on in the package and in the omp binary; the library self-test, run now in a scratch bank and cached; every bank's schema vs a fresh library database; every bank id vs `projectBankSegment(cwd)` of the cwd its own rows record (this proves the importer's local copy of that function against your banks). Without a matching library it still checks the bank ids and says the schemas were not checked. |
| `project-map` | draft `project:<name> -> folder` map from a Hindsight export (`--hindsight-export`, `--out`). Review it, then pass it with `--project-map`. A tag name or folder that carries a secret or a control character (judged on the raw value, see "Redaction") appears as `project#N` or `folder#N` on stdout and in the file, which carries `"format": "omp-memory-project-map/1"`. |

Flags: `--banks-dir DIR` (default the real banks dir; use a scratch copy for tests), `--ledger FILE`
(default `~/.config/omp-memory-import/ledger.json`, mode 0600; not inside the banks directory), `--out report.json` (and `<out>.samples.json` for a dry run): never inside the banks directory, never the ledger or another input of the run, and an existing destination must be a regular file (not a link) that carries this tool's format marker (`"format": "omp-memory-report/1"`, `omp-memory-samples/1` or `omp-memory-project-map/1`; every file the tool writes has one; a file without it, such as unrelated JSON, is refused with a message to remove it first), otherwise the command is refused before it reads or writes anything; files are written through an exclusive random-named temp file (`O_EXCL`, no links followed) and renamed, `--run-id ID` (import) / `--run ID`
(undo, verify), `--force-closed`, `--global-allowlist FILE`, `--skip-unassigned`, `--claude-home DIR`, `--claude-mem-db FILE`,
`--only-new-banks`, `--banks A,B,...`, `--release-held KEY[,KEY...]|FILE` (read as a file only when written as a path: `/...`, `~/...`, `./...` or `../...`, and a regular file; anything else is a key list), `--hindsight-export FILE`, `--hindsight-ledger FILE`, `--project-map FILE`, `--embedding-model`,
`--lib-dir DIR` (env `OMP_MEMORY_LIB_DIR`: an install root with `node_modules/@oh-my-pi/pi-mnemopi`, used instead of the lookup below; still must equal omp's version), `--omp-bin` (env `OMP_BIN`; default `omp`),
`--conf FILE` (env `OMP_MEMORY_BANKS_CONF`) and `--folder-roots DIR,DIR` (directories whose sub-folders are known project folders): both only help `claude-mem-summaries` and `project-map` find a folder from a project name.

The library is looked for, in this order, at an explicit `--lib-dir`; omp's plugin directory (`$OMP_PLUGINS_DIR`, else `~/.omp/plugins`); `~/.cache/omp-memory/lib/<version>`
(`$OMP_MEMORY_CACHE_DIR`, else `$XDG_CACHE_HOME/omp-memory`) made by `setup`. A place is used only when its package version equals `omp --version` and every dependency
the package pins is installed at that version. Anything else is refused with the exact `setup` command; there is no way to accept a different version.

Exit status: 0 success; 1 a refusal (a gate, a failed check, a library that is missing or fails its test, an open bank, an unreachable registry, a lock held by another operation, a plan that changed since the dry run) or a failed `verify`/`doctor`/`undo`; 2 wrong usage (unknown flag, a flag given twice, a value given to a flag that takes none such as `--apply=false`, missing value, `import --apply` without one of each of `--expect-plan-digest`/`--no-plan-check` and `--backed-up-banks`/`--no-backup-check`, `import` without `--apply`, a reused `--run-id`) or an unusable environment (no omp version to work with); 3 an unexpected failure.

## What a written row is

The row is what omp's own `retain` / `learn` leaves, so recall and the consolidation code treat it as a
normal note: session = author = channel = the bank, scope `bank`, veracity `tool`, memory type `fact` (summaries
`episode`), importance 0.75 / 0.7 / 0.6, FTS through the library's triggers, the `occurred_on` and `has_source`
annotations, an embedding by the bank's own model (made by `embed_worker.ts`, a child process that runs the library's own
`embed()` with fastembed, one per bank; the importer process never loads onnxruntime). Source column `import-<source>`.
Differences, on purpose:

* the original timestamp is kept in `timestamp` (file `modified` / mtime, Hindsight `date`, summary `created_at`);
* trust tier `IMPORTED`, and `consolidated_at` is stamped by the library's own import path. omp's start-up
  `promoteEligibleWorkingMemory` and `sleep` only pick rows with `consolidated_at IS NULL`, so imported rows are neither
  trimmed (the 24 h working-memory trim) nor re-summarised;
* fact and triple extraction never run (nothing is sent to an LLM); the importer writes with `beam.importFromDict`
  (the library's restore path), `beam.annotations.add`, the library's embedding-store statement for the vectors and `forgetWorking`:
  no other hand-written SQL writes;
* `metadata_json` carries the provenance: `source` (claude-code / hindsight / claude-mem), `sourceKind`, `origin`,
  `originKey`, `originHash`, `importRunId`, `importedAt`, `claudeType`, `name`, `description`, `cwd`, `tool: "import"`
  and source extras (`projectDir`, `hindsightBank`, `factType`, `documentId`, `tags`, `project`, `memorySessionId`).

Row ids are the first 16 hex of `sha256("import\0<source>\0<originKey>")`. Content shown to recall is
`<name>\n<description>\n\n<body>` for Claude memories, the text for Hindsight facts, and a titled field list for summaries.
Redaction: the Hindsight proxy's rules (`redact_bridge.py` calls `hindsight_proxy.redact`), then omp's own
`redactMemorySecrets`. After redaction two checks run: the proxy import's residual scan and the HOLD rules in `hold.ts`. An item with a finding is held back
(`skippedHeld`, `held` in the report, printed by `dry-run`; listed by originKey and finding kind, never by text). The same checks run over the strings that come from the source and are stored or printed: origin, originKey, folder, front matter type and source extras (file names, tags, document ids). They are judged below the home directory (the home prefix, as written and by real path, and the project directory name Claude derives from the folder, are not scanned), and the high-entropy residual of the redaction step does not count there: a hash-like folder name is ordinary; a long token holds only next to a key, token or secret word (`hold:long-token`) or in a known token shape. Every such string is judged RAW, never in a sanitised form (the bank id turns `password=1234` into `password-1234` and loses the context): as written, with its percent escapes decoded (twice), and each of those in lower case, the whole string and each path segment through the hold rules and omp's own redaction, and each segment for a key word (`password`, `passwd`, `pwd`, `passphrase`, `secret`, `token`, `api key`) followed by a value whatever separates them (`=`, `:`, `-`, `_`, `.`, a space, `%3D`): three or more characters with a digit, or four or more with a capital after the first letter (`token-service` and `Secret Santa` are not findings; `password-73915` and `Token_aB3x` are). An item with a finding, or a control character, is held with kind `hold:provenance-secret` or `hold:provenance-unprintable` and listed as `held#N`; `--release-held` cannot release it: rename or remove what carries the secret. A bank id derived from a folder with such a finding is named `bank#N` in the reports (`perBank`, `changed`, `tombstoned`) and nothing is imported into it; project and folder labels of `project-map` are `project#N` and `folder#N`. N counts the distinct values of its kind in the order the run meets them: a label carries no hash, length or characters of the value, and means something only inside one run. The plan digest names such items by their content hash only. Warnings and errors in a report never carry raw source values (a secret-like string is withheld, control characters are removed, the length is limited); the front matter `unknown type` warning does not show the value.
Hold kinds: `hold:password-value` (password/passwd/pwd/passphrase/secret/token/api-key, then within 40 characters `:` `=` `is` `->` and a value of 4+ characters that has a digit, a symbol or mixed case and is not a placeholder or URL), `hold:credential-pair` (`user@host / value` after login/ssh/user/username/root/admin), `hold:private-key`, `hold:bearer-token` (`Bearer` + 20 characters), `hold:token-prefix` (`sk-`, `ghp_`, `glpat-`, `xoxb-`, `AKIA`...), `hold:url-credentials` (`user:pass@`), `hold:long-token` (24+ characters with a digit or mixed case within 30 characters after key/token/secret). Prose ("the password is required") and markdown links do not hold.
After a human review, `--release-held KEY,KEY` (or a file with one originKey per line, given as a path) imports exactly those held items; everything else stays held. Provenance holds are not releasable.

## Output: one sink

What the importer prints, and the files it writes for a person to read, go through one sink (`sink.ts`, with `outputs.ts` for report files): stdout, stderr, progress lines, the message of a failure (shown as its message only, never as a stack), JSON reports, samples files, project-map drafts, and the self-test result it caches. The sink applies the label policy to every string it is given, whatever put it there (a file name, a project key, a path flag, a system error that quotes a path, the output of `bun add`): a string that carries a recognised secret (the raw string, its percent-decoded and lower-case forms, each path segment and each word of it) is replaced by an index label (`secret#N`, `line#N`, `bank#N`, `held#N`, `project#N`, `folder#N`), control characters (terminal escape sequences) are removed, and a failure built by the importer names its category and the labels of what it is about (`fail(category, ...labels)`), never raw source-controlled text. Exceptions the importer does not write itself (a JSON parser, SQLite, a child process) are shown through the same filter, and the redaction bridge's and sqlite's own messages are not shown at all, because they can quote what they were given. A secret in a form the rules do not recognise (a bare word with no key word or known shape) is not seen, as for the text.

The entry (`cli.ts`) loads nothing before its last-resort handlers exist: it installs `uncaughtException` and `unhandledRejection` handlers first and loads the application (`main.ts`) with a dynamic import inside a try, so that a failure while a module loads, an exception nobody catches, or a rejected promise nobody awaits prints one line `unexpected failure (<kind>): <message>` (the message through the sink when the sink loads, withheld when it does not) and exits with status 3. The callbacks that run on a signal or at exit (the claude-mem snapshot cleanup, `setup`'s interrupt cleanup) catch their own failure and report it the same way.

Fields that keep the real names, on purpose: the ledger (`ledger.json`, mode 0600) records `entries` keys (`<source>|<originKey>`) and `entries[*].originKey`, `entries[*].bank`, `entries[*].rowId`, `entries[*].runId`, `runs[*].runId`, `runs[*].banks` and the keys of `banksCreated`. These are operational identities: `verify` and `undo` find the rows and banks by those exact names, so they cannot be labels. Only items that passed the provenance hold are recorded (a held item is never imported), so a name the rules recognise as a secret is not in the ledger; a name that carries a secret in a form they do not recognise would be recorded as it is. The lock file beside the ledger holds a pid, a random token and a time. Everything else the importer persists (reports, samples files, project-map drafts, the self-test cache) is written through the label policy. `test/output-sink.test.ts` reads every source file and fails on any use of the `console` object, a `process.stdout`/`stderr` or stream `write`, a file write, copy or rename, a `process.on`/`once`/`off` registration, a `.catch(` or `new Promise` rejection path, a child process with inherited stdio or uncaptured stderr, or `throw new Error(...)` with anything but a plain string, outside an explicit allow-list in which every line has its reason; `test/secret-matrix.test.ts` places a recognised secret (a PIN assignment, a token with a known prefix, a percent-encoded and a case-changed form) in every position the command line can carry one and checks stdout, stderr, the failure message and the report, samples, project-map and undo/verify files; `test/entry.test.ts` runs the entry with a module that throws at load, an uncaught exception, an unhandled rejection, a sink that cannot load and a signal cleanup that throws.

## Content that is never imported

Decided on the text as read, before redaction, so that nothing which is not text a person wrote reaches the redaction step or a bank:

* **Binary** (`skippedBinary`, listed in `binary` with the key and the reason): the text contains a NUL byte, or more than 2% of its characters are U+FFFD (what decoding invalid UTF-8 leaves).
* **Too large** (`skippedTooLarge`, listed in `tooLarge` with the key, the length and the limit): more than 1,000,000 characters (counted as code points), the longest text the redaction step accepts (`hindsight_proxy.redact.MAX_TEXT_CHARS`; a test compares the two numbers). The reason reads `N characters; the limit is 1,000,000 characters (the redaction step refuses longer text)`. A Claude memory file over 4,000,000 bytes (more than that many characters whatever it holds) is not read at all: it shows in `excluded` and `warnings`, not in `tooLarge`.

Neither is an error (`errors` stays 0, the exit status is unaffected), neither is redacted, written to a bank or put in a sample, and `dry-run` prints one `skipped (binary): KEY<TAB>reason` or `skipped (too large): KEY<TAB>reason` line per item on stderr. Split a large file, or leave it out of the memory directory, to import it.

## Re-running, tombstones, changed sources

An item is identified by its originKey (Claude: `<real folder>#<file name>`, a second root's same-named file gets
`@<root>`; Hindsight: `<bank>#<id>`; claude-mem: `summaries#<id>`), never by content. Per item, first match wins:
tombstoned -> `skippedTombstone`; known and the source hash is unchanged -> `skippedUnchanged`; known and the source changed
-> `changedSource` (reported, **never applied**); same redacted content already in that bank or earlier in the run ->
`skippedDuplicate`; residual findings -> `skippedHeld`; else imported. The hash ignores the volatile front matter fields
`originSessionId` / `modified` and line-ending / trailing-space noise.

The ledger (outside the banks) records each imported item (`bank`, `rowId`, `hash`, `runId`) and a state. A ledger entry
whose row has disappeared from its bank was deleted by the user: it becomes a tombstone and the item is never imported
again. `undo` removes rows and drops the entries of rows it confirmed gone, except entries whose rows were already gone: those stay tombstoned. Entries of rows that are still present are kept.
The ledger also records the banks whose database this tool created (`banksCreated`: bank, run id, time). A bank listed there that holds no rows (its rows were undone) counts as new again for `--only-new-banks`; one that holds any row does not. The `omp-memory` launcher uses the same record to skip the backup requirement for a bank that holds only rows this tool wrote.
Every row also carries its originKey and run id, so a lost ledger is rebuilt from the rows on the next `import --apply`
(tombstones cannot be rebuilt, because their rows are gone).

## Folders and bank ids

Claude project directory -> folder: (1) the `cwd` in the project's newest session `.jsonl` files (the cwd the directory
name encodes wins a vote), (2) the directory name decoded into paths that exist on disk, (3) unresolved. The path is
normalised with realpath. The bank is omp's own `projectBankSegment(path)`, copied into `bank_id.ts` (it lives in omp's coding agent, which the published
library package does not include; test vectors were made by omp's function). omp hashes the path it was started in without resolving symlinks, so for
a folder reached through a symlink (`/tmp/x`) the bank omp already uses wins when the realpath form has none.
`doctor` proves this for every existing bank. Unresolved or ambiguous items go to the `unassigned` bank (never recalled by omp,
listed under `unresolved`); `--skip-unassigned` holds them back instead. Nothing goes to the global bank (`default`) unless
its originKey is in the reviewed `--global-allowlist` file (one originKey per line, `#` comments).

claude-mem summaries find their folder through `sdk_sessions.content_session_id` -> the session's `.jsonl` -> project
directory; without a session file the `project` name decides only when exactly one known folder has that name or the
project map names it.

Hindsight: `project:<name>` is looked up in `--project-map` (`{"projects": {"sms": "/abs/path", "other": null}}`;
`project-map` writes a draft). An item with no project tag, several project tags, an unmapped or null name is unassigned.

### Hindsight export file

`{"exportedAt": "...", "banks": {"<bank>": [item, ...]}}` (or `{"bank": "<bank>", "items": [...]}`, or a bare list whose items carry
`bank`). An item has the shape of Hindsight's `/memories/list` rows: `id`, `text`, `fact_type`, `state`, `tags`, `context`,
`document_id`, `date` (or `mentioned_at`), optional `metadata`.

Items that originated from mnemopi are excluded; any one signal is enough: `document_id` is a key of the Hindsight import
ledger (`--hindsight-ledger FILE`, default `~/.config/omp-memory-import/hindsight-ledger.json`), `document_id` starts with `mnemopi:`,
`context` starts with `mnemopi import:`, or the metadata has `mnemopi_table` (all four are what `hindsight_import` sent; Hindsight rewrites
the text of facts, so a content hash cannot be used). Observations and invalidated rows are never imported.

## Targeting banks

`--only-new-banks` writes only to banks whose database does not exist yet, or that this tool created and that hold no rows now (after an `undo`); `--banks A,B` writes only to the named bank
directory names; together they are a union. Items whose bank is not targeted count as `skippedNotTargeted` (per bank:
`perBank[].notTargeted`), are not written and are not put in the ledger, so a later run imports them. Preflight (schema,
embedding model, open-session check) runs only on targeted banks, and `dry-run` honours the filter, so its plan is what
`import --apply` does. A targeted existing bank that another process has open still refuses (`--force-closed` is the only
bypass and is unchanged). Names in `--banks` that no bank or item maps to are refused. `verify` and `undo` ignore the filters.

## Guards (all enforced before the first write)

* **Library = omp, exactly.** `omp --version` must equal the library package's version, and the package's pinned
  dependencies must be installed at those versions; any other version (a patch release too) is refused, naming the
  `setup` command. There is no override. Before a write the SQL the importer relies on (the restore insert, embedding
  and annotation stores, forget, the `working_memory` DDL, the trim exemption for durable rows) must be found in the
  package's source files and in the omp binary (an `omp` smaller than 5 MiB is a launcher script, not scanned: `doctor` and the
  report's `library.warnings` say that parity with omp's own SQL is not verified, `parity.ompParityVerified` is false). The match is structural: letter case and layout do not matter (`VALUES (?, ?, ?)` and a multi-line
  `SELECT ?, ?, ?` are the same statement), and a column list only has to contain the columns the importer writes.
* **Behavioural self-test.** What decides whether a library may be written through is `selftest.ts`: through the loaded library, in a
  scratch bank under the system temp directory, the importer performs its exact write sequence (`applyBank`: restore insert, annotation,
  embedding) and then checks the row, its full-text entry, both annotations, its embedding row (model and vector), `consolidated_at`, the trim
  exemption (the library's own `remember` trims an old native row, the restored rows survive) and `forgetRows` (nothing of the rows is left in
  any table). Any failed step refuses the write and names the step. `setup` and `doctor` run it (`setup` also once with the real embedding
  model); `import --apply` and `undo --apply` run it with a stand-in embedder when no passing result is cached. The result is cached in
  `<cache>/selftest/<version>-<fingerprint>.json` (mode 0600) for that library version, a fingerprint of the whole installed package (every file below its directory, nested dependencies aside),
  the Bun version and the sequence version; plain `doctor` reads the cache and says "not yet verified" when there is none. `verify_versions.ts`
  runs the checks (and, with `--suite`, this folder's tests) against published releases, one install per version.
* **Schema.** Each existing bank's core tables (columns and types), its full-text table and every trigger (name and complete
  definition, layout aside) must equal a fresh library database, and the bank must have no trigger or view the library does not
  make (one could rewrite rows as they are written or deleted); otherwise refuse. A bank made by an older omp whose trigger text
  differs is refused too. The embedding model of a bank must be single (omp's default `BAAI/bge-base-en-v1.5` for a new one).
  `undo --apply` runs the same checks as an import, for every bank, before it deletes anything.
* **Bank paths.** A bank directory, its database or its `-wal`/`-shm`/`-journal` file that is a symbolic link or not a regular
  file, a database with several hard links, and a bank directory that is not directly inside the banks directory are refused
  (import and undo). `--banks-dir` itself may be a link. The check is made right before the library opens the bank; a link made in
  the instants after it is not excluded.
* **Sessions (best effort).** A bank that another process has open (`lsof`, or on Linux without it a scan of `/proc/<pid>/fd`,
  which only sees processes of the same user) is refused, unless `--force-closed`. The check is made at the preflight, again right
  before each bank is written (after its embeddings are made), and again before each bank's rows are deleted by `undo`; it cannot
  exclude a process that opens the bank in the instants between the check and the write. An `lsof` that fails (exit status 1 with a
  message on stderr) counts as "cannot tell", not as "nobody has it open".
* **One operation at a time.** `import --apply` and `undo --apply` hold a lock file next to the ledger (`<ledger>.lock`, mode 0600,
  with the pid, a token and the start time) from before the ledger is read until they finish. It is published with `link()` of a
  fully written file. A lock held by a live process refuses the operation. A lock whose process is gone is reclaimed by exactly one
  process: the one that creates the marker `<ledger>.lock.reclaimed-<hash of that record>` (`O_EXCL`; a record is reclaimed once),
  moves the stale file away with `rename()` and checks it is the record it looked at; the others wait and look again, so nobody
  removes a live replacement. Markers and leftovers are removed after a day. Nothing is ever signalled or killed. An unreadable lock
  file, or an interrupted reclaim, refuses and names the files; remove them only when no import or undo is running.
* **Native rows.** The write is rolled back if the bank's other rows change (their count and a digest of every column are compared
  inside the transaction); the importer never calls the library's `remember` on a real bank (its TTL trim would delete unconsolidated
  rows); only the self-test does, in its scratch bank.
* **Embeddings.** The embeddings are made first, with the bank's model, and stored in the same transaction as the rows and their
  annotations (with the library's own embedding-store statement, vectors as 32-bit floats). A row without an embedding means nothing
  is written and the bank is not even created; a process stopped at any point leaves the bank without the rows or with every row
  complete. A row without an embedding is found by `verify`, and `undo --run` removes it.
* **Run ids.** `import --apply` refuses (usage error) a `--run-id` that is already in the ledger or on rows in any bank. `undo` removes only
  rows that are provably the importer's (trust tier `IMPORTED`, source `import-<source>`, `tool` `import`, origin key and hash in the
  metadata) and that the run id in their metadata selects; another row that carries the run id is left in place and reported. What is
  left is judged by row id, not by that selection: a ledger-backed row that is still in its bank (its run id removed or changed, its
  provenance altered) is a survivor, reported by id (never by content), and keeps its ledger entry. The ledger changes only for rows that are confirmed
  gone afterwards; a row the library did not delete is reported (exit 1) and keeps its entry. A run is marked undone only when no
  ledger-backed row of it is left in a bank (a survivor, also one whose provenance was changed, keeps the run open, so `verify` still
  covers it). A run whose record never reached the ledger (a stop after the commit) is rebuilt from its rows by the next
  `import --apply`, so that `verify` covers it. `import --apply` prints its run id on stderr before the first write.
* **The approved plan and the backup.** `import --apply` is given exactly one of `--expect-plan-digest HEX` (the `planDigest` of the
  dry run that was reviewed) or `--no-plan-check`, and exactly one of `--backed-up-banks a,b` (the banks a verified backup covers; `--backed-up-banks=`
  is the empty list) or `--no-backup-check`; anything else is a usage error. Under the lock, after the importer has made its own plan
  and before the library is opened for writing, it recomputes the digest with the dry run's function and refuses (nothing is written)
  when it differs. Right before each bank is written (after the open-bank and schema checks), a bank directory that exists then
  and is not in `--backed-up-banks` is refused with `bank X exists but is not covered by a verified backup`, whether it existed at the
  start or someone made it since; that is a per-bank error in the report (exit 1) and the other banks are still written. `--exempt-banks c,d`
  (optional; `--exempt-banks=` is the empty list) names banks that need no backup **only while they hold nothing but this tool's rows**:
  it is judged again right before that bank's write, and holds only when the ledger's `banksCreated` says this tool made the bank and
  the bank is provably empty of anything of the user's (`holdsOnlyImporterContent`, rules in `EXEMPTION_RULES`, mirrored in the
  launcher's `safety.py`): every `working_memory` row carries this tool's provenance; `annotations` and `memory_embeddings` have rows only
  for those rows' ids; `fts_working`, the shadow tables of FTS5 tables and `sqlite_*` tables are not looked at; **every other table**
  (episodic memory, scratchpad, gists, facts, triples, graph edges, consolidation log, `fts_episodes`, `fts_facts`, any table this list
  does not know) has zero rows. A non-empty `-wal` or `-journal` file, an unreadable or locked database, or any error means not
  exempt (fail closed). The importer empties the log after each of its writes (`wal_checkpoint(TRUNCATE)`), so a bank it just wrote
  qualifies; while another process has the bank open the log may stay and the bank does not. A native row added meanwhile, in any table,
  ends the exemption (the bank is refused and not touched). An exemption names its bank only and is used up by its write: a bank is written at most once per invocation. `banksCreated`
  is recorded only for a bank that did not exist when the plan was read and still did not exist right before this run's own successful
  write (never for a refused or failed write, never for a bank another process made in between). The command line says on stderr when
  either escape is used. The library is opened for writing only after these repeated checks.

## Tests

`bun test` in this folder (Bun 1.4.4 or newer). `bun verify_versions.ts [--root DIR] [--suite] VERSION...` runs the structural check and the self-test (and with `--suite` the whole test suite) against each published `@oh-my-pi/pi-mnemopi` version and prints a table. `selftest.test.ts` changes one behaviour at a time in a copy of the library and expects the self-test to fail at that step. Scratch banks are built with the real library: the first run installs
the published `@oh-my-pi/pi-mnemopi` (default 18.18.3, `OMP_MEMORY_TEST_VERSION`) into
`<tmp>/omp-memory-test-lib/<version>` with the importer's own `setup` code (network once; `OMP_MEMORY_TEST_LIB_DIR`
points at an existing install root); `bunfig.toml` runs `test/preload.ts`, which installs it before the first test file and points `OMP_MEMORY_CACHE_DIR` at a scratch directory (the self-test cache of the tests never touches yours). Every test file sets its own 120 s test timeout with `useLongTimeouts()` (`test/timeout.ts`; Bun keeps the limit per file, so a preload cannot set it for all, and `test/timeouts.test.ts` fails when a file forgets): several tests start real processes, and Bun's default of 5 s is too short for them on a busy machine. No omp source tree is used. Types: in a copy of this folder,
`bun add -d typescript @types/bun && node_modules/.bin/tsc -p tsconfig.json`.
