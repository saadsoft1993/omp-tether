# Vendored code

`vendor/` holds code that is **copied byte for byte** from a separate *source tree*, where the importer and the scripts are developed. That tree is not part of this repository and is not needed to use, build, test or review this plugin; only the maintainer refreshes `vendor/` from it.
It is never edited here: the change is made upstream, then the sync is run. The layout under `vendor/` is the upstream layout, because the code imports its neighbours by that layout. A contributor who wants to change vendored code sends the change as a patch against the files here; the maintainer applies it upstream and re-syncs.

| Under `vendor/` | What | Used by |
|---|---|---|
| `memory_import/` | The importer (TypeScript on Bun), its README and its tests; `tsconfig.json` is not copied | `omp-memory import ...` runs `memory_import/cli.ts`; `make test-memory` runs its tests |
| `memory_import/redact_bridge.py`, `hindsight_proxy/redact.py`, `hindsight_import/` | The redaction rules and the residual scan the importer calls through a Python child process | the importer |
| `memory_ops/` | `backup_mnemopi.sh`, `restore_drill.sh`, `export_hindsight.sh`, `hindsight_pages.py` | `backup`, `restore-drill`, `export-hindsight` |
| `omp_hub/memory/{model,graph,folders,mnemopi,sources,instructions,hindsight}.py` | The standalone memory modules: bank listing, folder labels and groups, the label store, source scanning, instruction-file discovery. They import only each other | `banks`, `sources`, `labels` (`python/omp_memory/core.py` is the only file of this plugin that imports them) |
| `omp_hub/test_memory_schema.py` | The real mnemopi schema and a bank builder | the Python tests |
| `SOURCE.sha256` | `sha256  path` of every vendored file, written by the sync | the drift test and `doctor` |

The upstream package (`omp_hub/memory/__init__.py`, its service, revision store and HTTP routes) is **not** copied; the modules above are used as a namespace package, so nothing from the hub is imported.

## Refresh

```
export OMP_MEMORY_SOURCE_DIR=/path/to/the/upstream/checkout     # there is no default; or pass --source DIR
scripts/sync-vendor.sh                  # copy from it; rewrites vendor/SOURCE.sha256
scripts/sync-vendor.sh --check          # write nothing; exit 1 and list every difference
git diff plugins/omp-memory/vendor      # review, then commit vendor/ together with SOURCE.sha256
```

The files to copy are listed in `scripts/vendor_manifest.txt` (`file <path>` and `tree <dir> [exclude <name> ...]`). A new file in a listed tree is drift until it is synced; a file that left the source is removed by the sync.
The sync writes only below `vendor/`.

## What the tests say

- `tests/python/test_vendor.py` always checks that every vendored file matches `vendor/SOURCE.sha256` (an edit made here and not upstream fails), that no unrecorded file sits in `vendor/`, and (against a small fake source checkout) that
  the comparison catches a changed file, a new file, a removed file, a stray file, a missing or unnamed source and a manifest path that leaves the source.
- Against the real upstream checkout (`$OMP_MEMORY_SOURCE_DIR`) it checks that nothing drifted. When the variable is unset or the directory does not exist the two tests are **skipped, with a message that says why**; they do not pass silently.
- `omp-memory doctor` re-checks the recorded checksums on your machine.

## Known differences from running the importer upstream

- The vendored modules are generic: they take the group roots (`groups`, an optional conf file), the scan roots for instruction files and the display names of groups as input. The upstream hub passes its own conf and names; the plugin passes
  the user's `groups` setting and `banksConf` (only when the user sets it). The importer takes `--conf FILE` and `--folder-roots DIR,...` the same way.
- The importer loads `@oh-my-pi/pi-mnemopi` from npm at omp's exact version (`omp-memory setup`); its tests install that package into a scratch directory (`bun test` runs `setup`'s own code once, network needed) and use no omp source tree.
- `export_hindsight.sh` has no defaults: banks, output folder and connection come from flags or `OMP_MEMORY_HINDSIGHT_*` variables, and the archive mode runs a remote command that you name, over ssh. It is optional: nothing else uses it.
