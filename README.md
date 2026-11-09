# omp-tether

Small plugins for [omp](https://github.com/can1357/oh-my-pi) that let you reach **your own** sessions from other places
on your own machines. Each plugin is installed on its own from this repository's catalog; none of them does anything
until you turn it on.

| Plugin | What it does | Status |
|---|---|---|
| [`omp-session-control`](plugins/omp-session-control) | An extension that lets a local program stop the running turn of an interactive session and rename it, over a private Unix socket | 0.1.0 (beta) |
| [`omp-services`](plugins/omp-services) | Sets up the background services other plugins need (launchd and `systemd --user` units), checks them (`doctor`), and runs a heartbeat that pings a URL of your choosing with the result of the health checks plugins contribute | 0.1.0 (beta) |
| [`omp-private-relay`](plugins/omp-private-relay) | A Collab relay that runs on your own machine, a patched browser client for it, and per-browser pairing, so session links never go through omp's public relay | 0.1.0 (beta) |
| [`omp-dashboard`](plugins/omp-dashboard) | A local dashboard for your own sessions: list, open (through your private relay), stop, rename, kill and start them, with per-browser pairing; its own hub (Python, standard library only) and page | 0.1.0 (beta) |

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
- **Nothing leaves your machine unless you configure it or ask for it.** No plugin here reports usage or checks for
  updates. `omp-session-control` makes no network connection. `omp-services` can contact a network in three ways: the
  heartbeat ping to the URL you store (off until you do), `url` and `port` health checks that a plugin's drop-in declares
  (loopback only unless you allow more; `doctor` runs them too), and, on request, an `omp` marketplace refresh that
  `omp-services doctor --refresh-catalog` starts through `omp`. `omp-private-relay` makes exactly two kinds, both when you
  ask: the download of its web client during `init` (checked against a hash that ships with the plugin), and `bun install`
  during `rebuild-web`. `omp-dashboard` listens on loopback only and contacts a network only to send
  push alerts to the ntfy server (default `ntfy.sh`) once you create a topic file; with no topic file it contacts nothing.
- **Reviewed, not certified.** Each plugin has had independent reviews and their findings are fixed or answered in
  writing (`omp-private-relay`: fixed, but the fixes themselves have not been re-reviewed); no third-party audit of any of
  them has been done. Treat the first releases as beta software.

## Contents of this repository

| Path | What |
|---|---|
| `.omp-plugin/marketplace.json` | The catalog omp reads |
| `plugins/<name>/` | One directory per plugin: `package.json`, `src/`, `tests/`, its own README, SECURITY.md, CHANGELOG and threat model |
| `docs/protocol/` | Wire and file protocols, one document per protocol and version ([`control-1.md`](docs/protocol/control-1.md), [`health-1.md`](docs/protocol/health-1.md), [`services-1.md`](docs/protocol/services-1.md), [`pairing-store-1.md`](docs/protocol/pairing-store-1.md), [`relay-check-1.md`](docs/protocol/relay-check-1.md)) |
| `docs/compat.md`, `compat.json` | Which omp, Bun and platforms are supported and what each plugin uses from omp |
| `tools/extension-load-gate/` | Installs every plugin in a scratch home and loads it in a real omp |
| `tools/public-tree-check/` | The pre-publish check: allowlisted paths, author identities, wording, private names |
| `tools/testkit/` | A fake model provider and a scratch relay for tests: no external service and no model calls (the scratch relay listens on loopback), no cost |

## Develop

```
make check-contrib   # lint, shellcheck, unit, omp-services suites, integration, load gate (what a pull request needs)
make check           # the same plus the maintainer's publication gate (clean tree, one author identity)
make help            # the individual targets
```

Needs Bun 1.4.4 or newer, Python 3.10 or newer, ruff, shellcheck, and omp (18.18.x) for the integration test and the gate. Tests run
in scratch homes on random ports and never touch your real omp profile. How to set up, what a pull request needs and how to report
a problem: [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Licence

MIT, see [`LICENSE`](LICENSE). Report security problems as described in [`SECURITY.md`](SECURITY.md).
