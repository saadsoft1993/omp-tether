# omp-tether

Small plugins for [omp](https://github.com/can1357/oh-my-pi) that let you reach **your own** sessions from other places
on your own machines, and look after omp's local memory. Each plugin is installed on its own from this repository's
catalog; none of them does anything until you turn it on.

| Plugin | What it does | Status |
|---|---|---|
| [`omp-session-control`](plugins/omp-session-control) | An extension that lets a local program stop the running turn of an interactive session and rename it, over a private Unix socket | 0.1.0 (beta) |
| [`omp-services`](plugins/omp-services) | Sets up the background services other plugins need (launchd and `systemd --user` units), checks them (`doctor`), and runs a heartbeat that pings a URL of your choosing with the result of the health checks plugins contribute | 0.1.0 (beta) |
| [`omp-private-relay`](plugins/omp-private-relay) | A Collab relay that runs on your own machine, a patched browser client for it, and per-browser pairing, so session links never go through omp's public relay | 0.1.0 (beta) |
| [`omp-dashboard`](plugins/omp-dashboard) | A local dashboard for your own sessions: list, open (through your private relay), stop, rename, kill and start them, with per-browser pairing; its own hub (Python, standard library only) and page | 0.1.0 (beta) |
| [`omp-memory`](plugins/omp-memory) | A command line for omp's local memory banks: list banks (with folder labels) and what the memory files of other coding tools hold, import them into the banks, back them up and check them. Read-only until you pass `--apply`; ships a skill that tells an agent how to use it safely | 0.1.0 (beta) |

## Install a plugin

```
omp plugin marketplace add saadsoft1993/omp-tether
omp plugin install omp-session-control@omp-tether
```

Restart omp sessions that were already running: omp loads a newly installed extension in new sessions only. Then follow
the plugin's own README to turn it on. To install from a local checkout instead, give `omp plugin marketplace add` the
path of the checkout.

Remove it with `omp plugin uninstall omp-session-control@omp-tether`; each plugin's README says what data it leaves behind
and how to purge it.

## What to know first

- **One person, one machine, one account.** These tools assume that whoever runs omp is trusted and that nobody else has
  your account or root. The threat models say precisely what that means and what is out of scope:
  [`docs/threat-model.md`](docs/threat-model.md).
- **Nothing is on by default.** Installing a plugin creates no files outside omp's plugin folders and opens nothing.
  `omp-memory` never writes a memory bank unless you pass `--apply` to an import (or undo) command.
- **Nothing leaves your machine unless you configure it or ask for it.** No plugin here reports usage or checks for
  updates. `omp-session-control` makes no network connection. `omp-services` can contact a network in three ways: the
  heartbeat ping to the URL you store (off until you do), `url` and `port` health checks that a plugin's drop-in declares
  (loopback only unless you allow more; `doctor` runs them too), and, on request, an `omp` marketplace refresh that
  `omp-services doctor --refresh-catalog` starts through `omp`. `omp-private-relay` makes exactly two kinds, both when you
  ask: the download of its web client during `init` (checked against a hash that ships with the plugin), and `bun install`
  during `rebuild-web`. `omp-dashboard` listens on loopback only and contacts a network only to send
  push alerts to the ntfy server (default `ntfy.sh`) once you create a topic file; with no topic file it contacts nothing.
  `omp-memory` contacts a network only when you run `omp-memory setup` (the npm registry for omp's Mnemopi library at your omp's version, then omp's embedding runtime and model, once),
  when `import import --apply` has to fetch that embedding runtime or model because `setup` was skipped, and with the optional `export-hindsight --apply` (your own Hindsight); your memory text is never sent anywhere else.
- **Not certified.** No third-party audit of any of them has been done. Treat the first releases as beta software.

## Contents of this repository

| Path | What |
|---|---|
| `.omp-plugin/marketplace.json` | The catalog omp reads |
| `plugins/<name>/` | One directory per plugin: `package.json`, `src/` or `python/`, `tests/`, its own README, SECURITY.md, CHANGELOG and threat model (`omp-memory` also ships `skills/` and a `vendor/` tree copied from another repository, see its `docs/vendoring.md`) |
| `docs/protocol/` | Wire and file protocols, one document per protocol and version ([`control-1.md`](docs/protocol/control-1.md), [`health-1.md`](docs/protocol/health-1.md), [`services-1.md`](docs/protocol/services-1.md), [`pairing-store-1.md`](docs/protocol/pairing-store-1.md), [`relay-check-1.md`](docs/protocol/relay-check-1.md)) |
| `docs/compat.md`, `compat.json` | Which omp, Bun and platforms are supported and what each plugin uses from omp |
| `tools/extension-load-gate/` | Installs every plugin in a scratch home and loads it in a real omp |
| `tools/public-tree-check/` | The pre-publish check: allowlisted paths, author identities, wording, private names |
| `tools/testkit/` | A fake model provider and a scratch relay for tests: no external service and no model calls (the scratch relay listens on loopback), no cost |

## Develop

```
make check-contrib   # lint, shellcheck, unit, omp-services and omp-memory suites, integration, load gate (what a pull request needs)
make check           # the same plus the maintainer's publication gate (clean tree, one author identity)
make help            # the individual targets
```

Needs Bun 1.4.4 or newer, Python 3.10 or newer, ruff, shellcheck, and omp (18.18.x) for the integration test and the gate. `make test-memory` installs the published Mnemopi library (`@oh-my-pi/pi-mnemopi`) into a scratch directory once (network) and compares `vendor/` with the upstream development checkout when `OMP_MEMORY_SOURCE_DIR` names one, and says what it skipped when not. Tests run
in scratch homes on random ports and never touch your real omp profile. How to set up, what a pull request needs and how to report
a problem: [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Licence

MIT, see [`LICENSE`](LICENSE). Report security problems as described in [`SECURITY.md`](SECURITY.md).
