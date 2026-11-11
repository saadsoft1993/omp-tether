# Changelog

All notable changes to `omp-services`. The protocols have their own numbers (`health/1`, `services/1`, `config/1`) and
change only by spec; see `docs/protocol/` in the repository.

## 0.1.1 (2026-11-11)

- `purge` works after another plugin's `dropins --write` created `~/.config/omp-services` before `init` (the order the plugins' READMEs use): it used to refuse every directory because that one had no marker. `init` now records the entries it creates inside a directory that already existed (`.omp-services-entries`), and `purge` deletes exactly those, never the directory itself unless that leaves it empty, and never a drop-in or anything else in it. A directory with neither its marker nor a record is still refused, a record that names anything but a direct child is refused, and the health and services drop-in directories are never recorded as ours.

## 0.1.0 (2026-11-11)

First release.

**Scope decision: there is no `upgrade` command in 0.1.** An earlier design had `omp-services upgrade` with a snapshot,
a write-ahead journal and a rollback. It was removed, with everything that existed only for it, to keep the first release
small enough to verify: omp has its own `omp plugin upgrade`; `omp-services doctor` reports "newer version available" and
drift; `omp-services init` re-renders the units (changed units only, protected units never touched). A rollback may come
back in a later release if there is a need for it.

- Command line `omp-services`: `init` (plan, confirmation, apply; safe to repeat), `doctor`, `uninstall` (keeps configuration, secrets, logs), `purge` (its own
  confirmation, only directories it created), `heartbeat`, `health`, `config`, `render`.
- Unit renderer for launchd (macOS) and `systemd --user` (Linux): generic label prefix, logs under the state directory,
  `Umask 077`, keep-alive, a fixed small `PATH`; the rule "reload only changed units, never reload a protected one unless
  named"; a label allowlist so that only units it wrote are ever touched.
- Private code release (`releases/<version>-<digest>`, `current` link, previous releases kept) and wrappers that pin the
  absolute omp path (`omp`, and `bun` as omp's embedded Bun) and the Python 3.10+ in use.
- Heartbeat: runs the checks, pings `<URL>` or `<URL>/fail`, no body, never follows a redirect, verifies TLS, never logs the
  URL, bounded checks, log trimming, status record that `doctor` reads.
- `health/1`: drop-in checks `url`, `port`, `uid-owner`, `serve-route`, `command`, `file-age`, `watchdog` with the result
  classes ok, unreachable, stale, not-on-relay, failed, invalid; specification, JSON Schema and a conformance suite.
- `services/1`: drop-in service declarations (omp script on the embedded Bun, Python script, executable).
- `doctor`: configuration and file-mode validation, per-unit state, drop-in problems, stale heartbeat, live health with the
  failure kinds above, "newer version available" from omp's cached catalog.
- Extension `omp-services` (entry file `src/omp-services.ts`, id `extension-module:omp-services`): `/omp-services doctor` and
  `/omp-services setup` (plan, confirmation, then apply). One file, no dependencies, no events, no background work.
- `install.sh` one-line bootstrap with a published SHA-256 (it verifies the installer; `OMP_TETHER_SOURCE_DIR` installs the
  plugin from a local checkout at a commit you chose).
- Plan digest: `--expect-plan` binds the exact payloads (unit files, wrapper scripts, configuration text, release id,
  deletion targets), not only the displayed text; the plan shows what each unit and wrapper will run.
- Only release directories carrying this tool's marker file are ever pruned or deleted, and pruning is part of the approved
  plan.
- Protection: the watchdog honours the protection recorded at install time and refuses to restart when that state is missing,
  damaged or has no record of the service; a unit of ours the state file has no record of is treated as protected by `init`
  and `uninstall`; a unit record without its protection flag is refused as damaged state. systemd units that are activating,
  reloading or deactivating are live, never stopped, unloaded, restarted or removed while they change state. A wrapper that
  cannot be read as a file is left alone, not replaced.
- One lock file next to the data directory covers `init`, `uninstall` and `purge`, including the first `init`
  (overlapping directory layouts are refused).
- Deadlines: one absolute deadline for resolution, connection, TLS, request and body; nothing is connected or sent once the
  caller has given up; at most 16 abandoned workers; the service manager's answers are parsed, and a failure to answer is an
  error, not "no"; a FIFO as configuration cannot hang; log trimming copies a bounded range.
- Version ordering follows semantic versioning (pre-releases below their release); versions that cannot be ordered are
  reported as "different", not "newer".

