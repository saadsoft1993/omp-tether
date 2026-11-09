# Contributing to omp-tether

Thanks for helping. This repository holds four small plugins for [omp](https://github.com/can1357/oh-my-pi) that let you
reach your own sessions from other places on your own machines. It is maintained by one person, so small, focused changes
that come with tests are the easiest to review.

## Before you start

- **Bugs and ideas:** open an issue and pick the matching template. For a larger change (a new plugin, a new protocol
  version, a new network connection), please open an issue first and agree on the approach before you write code.
- **Security problems:** do not open an issue. Use the **Report a vulnerability** button on the repository's Security tab
  (private vulnerability reporting), as described in [`SECURITY.md`](SECURITY.md).
- **Design rules these plugins keep:** nothing is on by default; nothing leaves the machine unless the user configures it or
  asks for it; one trusted person on one account. A change that breaks one of these needs a written reason in the issue and
  an update of the threat model (`docs/threat-model.md` and the plugin's own `docs/threat-model.md`).

## Set up

You need:

- **Bun 1.4.4 or newer.** The Makefile refuses to run with an older Bun. omp 18.18 embeds Bun 1.4.4, which is what the
  extensions run on, and the page of `omp-dashboard` and the web client of `omp-private-relay` are built and pinned with
  exactly 1.4.4 (`plugins/omp-dashboard/ui-build.json`, `plugins/omp-private-relay/web-builds.json`), so use 1.4.4 when you
  rebuild either one.
- **Python 3.10 or newer**, [`ruff`](https://docs.astral.sh/ruff/) and [`shellcheck`](https://www.shellcheck.net/).
- **omp 18.18.x** on your `PATH`, for the integration tests and the extension-load gate (they start a real omp in a scratch
  home with a fake model; no model calls are made).
- **gitleaks**, only if you run the maintainer's publication gate (`make check`; see below).

```
git clone https://github.com/saadsoft1993/omp-tether
cd omp-tether
make ui-install      # once: downloads the build packages of the omp-dashboard page (needs the network)
make check-contrib
```

All tests run in scratch homes on random free ports and never touch your real omp profile (`~/.omp`) or your real
configuration. If you see a test touching either, that is a bug: please report it.

### Environment variables the Makefile understands

Set them in your shell or on the command line (`make BUN_BIN=/path/to/bun check-contrib`).

| Variable | Meaning |
|---|---|
| `BUN_BIN` | The Bun to use instead of the first `bun` on `PATH` (for example a pinned 1.4.4 build) |
| `OMP_BIN` | The omp binary the integration tests and the gate use instead of the first `omp` on `PATH` |
| `PYTHON` | The Python for the Python suites (default `python3`; must be 3.10 or newer) |
| `OMP_SRC` | A checkout of omp with its dependencies installed. When set, `make check-contrib` also type-checks the extensions against omp's types (`make typecheck`); when not set, that step is skipped |
| `WEB_OMP_SRC`, `OMP_TAG`, `ASSET`, `SERVICES_SCHEMAS` | Only for `make release-check-relay` and `make verify-web`, which rebuild the relay's web client from an omp tag and compare it with its pin (maintainers; needs the network once) |

Two rules: `BUN_BIN` and `OMP_BIN` are exported to every recipe, so give absolute paths; and do **not** export `BUN_BE_BUN`
in your shell: with it set, the omp binary behaves as Bun and prints no version, and the scripts that need it set it only
for the one command that does.

## What to run

```
make check-contrib   # everything a pull request needs: lint, shellcheck, unit, relay, dashboard, services suites,
                     # mutation checks, conformance suites, integration, extension-load gate
make help            # the individual targets, for a quicker loop while you work
```

`make check` runs `check-contrib` and then the maintainer's publication gate (`make public-check`). That gate needs a clean
working tree and requires every commit to carry one maintainer identity, so a branch with your commits fails it by design:
run `check-contrib`, not `check`. The maintainer runs the gate before every release. If you add a file, the gate's path
allowlist (`tools/public-tree-check/allowlist.txt`) has to list it; add the path to your pull request.

There is no continuous-integration workflow in this repository yet; the maintainer runs `make check` before merging.

## Pull requests

- **One logical change per pull request.** A bug fix, a feature, or a refactor: not a mix. Unrelated tidying goes in its own
  pull request.
- **Tests are required.** A bug fix comes with a test that fails without the fix. A new feature comes with tests of its
  behaviour and of what it refuses. Changes to a security guard of `omp-private-relay` or `omp-dashboard` must keep its
  mutation check meaningful (`make test-mutation`, `make test-dashboard-mutation`: each guard is switched off in a copy and
  a test must fail).
- **Protocols change by specification.** A change to a wire or file protocol updates its document in `docs/protocol/`, its
  schema and its conformance suite in the same pull request, and bumps the protocol number when it is not compatible.
- **Update the plugin's `CHANGELOG.md`** under the next version, and its README or threat model when behaviour or the trust
  model changes.
- **Keep the commits plain:** an imperative subject line that says what changed, a body that says why when it is not obvious.
- **Style:** `ruff` for Python (`make lint`), `shellcheck` for shell, and the existing style of the TypeScript next to your
  change. Match the code around it; do not reformat files you are not changing.
- **No new runtime dependencies** in the extensions, the hub or the relay without discussing it in an issue first: they are
  deliberately dependency-free.
- Write the pull request description with the template; say how you tested the change and on which operating system.

## Licence

By contributing you agree that your contribution is licensed under the MIT licence of this repository (see
[`LICENSE`](LICENSE)).
