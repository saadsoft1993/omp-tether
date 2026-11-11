# Release evidence: omp-dashboard 0.2.0

What a release candidate must have run, what was not possible, and where the results of the last run are recorded. This file does not carry
numbers from earlier trees: counts and results belong to one revision, and are recorded for that revision (see "Last recorded run").

## Checks (all from the repository root; `BUN_BIN` and `OMP_SRC` are environment variables, never `make` arguments)

| Check | What it covers |
|---|---|
| `make check` | lint (ruff, shellcheck), the type check, every plugin's unit tests, the relay and dashboard mutation checks, the conformance suites (including `pairing-store/1` against this plugin's reader and writer, and the cross-check with the relay's TypeScript reader and writer), the page's tests, type check and rebuild comparison, the tool tests, the services tests, the integration tests (below), `gate` and the publication checker |
| `make test-dashboard-mutation` | every row of `tests/mutation/run_mutations.py`: one guard is switched off in a copy and a test must fail (a hang counts) |
| `tests/integration/test_real_omp.py` (part of `make test-integration`) | the hub against two live omp TUI sessions with the real session-control extension, the test kit's fake model provider and a scratch relay: list, Stop, Rename, Kill needing the typed name, Force kill refused until a Kill failed, the audit log |
| Ubuntu 24.04 container, non-root user, Python 3.10 and 3.14, umask 022 and 002 | the whole Python suite, a first run under umask 002 with a group-writable home, the `pairing-store/1` conformance suite |
| `make export-gate` | each plugin installed from a fresh single-commit export and loaded in a headless omp |
| `scripts/check-public.sh` with `PRIVATE_TERMS_FILE` set | the publication checker on the tree, the history and a fresh export, and gitleaks |
| A real browser against the real-omp test environment | pairing, Rename, Stop, Kill and Force kill through the page's buttons, the Pair screen, a phone-width layout, and that the page refuses a link that is not http(s) |
| `both` against a real Serve route (macOS, scratch home and ports) | the local address and the tailnet name answer, a pairing link works once on either, forged `Tailscale-*`, `Via`, repeated and Funnel headers sent straight to the hub's port, the route removed, Tailscale stopped, `doctor` at each step, and a real browser paired on both addresses |

## Not verified, and why

- **Tailscale Serve with a second tailnet identity.** A real Serve (Tailscale 1.104.5, macOS) was tested on a second machine: a forged `Tailscale-User-Login` was replaced by the real owner's login, a forged `Tailscale-Funnel-Request` was removed and a forged `X-Forwarded-For` was replaced. The test tailnet had one user, so a request from a real non-owner identity was not tried; that refusal is proven only with forged headers on loopback. Serve on Linux was not tested.
- **`both` with Safari, and with a real non-owner identity.** The real run used one account on macOS (Tailscale 1.104.4) and a Chromium browser; the local cookie's missing `Secure` flag is not tested in Safari.
- **herdr on Linux.** herdr 0.9.5 was tested for real on macOS (Start opened a workspace, a tab labelled `omp` and a pane running `omp`); on Linux the backend is tested against a stand-in command only. tmux 3.4 was exercised for real.
- **Open and Watch in a browser** need a running `omp-private-relay` with its web client; the page's refusal states are what the real-omp test exercises. With `both` the bundled relay admits one way (`relay.admits`): the links the hub returns are tested against a stand-in `omp`, with the relay proof mocked, and were not followed through a real relay on either way.
- **Force kill after a Kill that failed, with a real omp:** a stopped omp is not listed and a running one ends on SIGTERM, so the sequence cannot be provoked; it is tested with real processes.
- **`make release-check`'s relay half** belongs to omp-private-relay and needs an omp checkout at its tag and the built web archive.
- **Pid and group reuse between a check and a signal** is narrowed (start-time checks, signalling a process group only while its leader is unreaped), not closed: a pidfd is not available on macOS.
- **Name resolution** is not covered by the overall HTTP deadlines.

## Last recorded run

The candidate is the commit at the tip of the `both` branch (one commit on top of the 0.1.1 release). The run below was made on that tree, on macOS (Apple silicon, Python 3.14, Bun 1.4.4), from the repository root; the Ubuntu run used the `odb-ubuntu` image (Ubuntu 24.04, Python 3.12, user `tester`, not root).

| Check | Command | Result |
|---|---|---|
| Lint | `ruff check .` and `ruff format --check .` | clean (93 files formatted) |
| Python suite, macOS | `cd plugins/omp-dashboard/tests/python && PYTHONPATH=../../python BUN_BIN=<bun 1.4.4> python3 -m unittest discover -p 'test_*.py'` | 448 tests OK, 1 skipped |
| Python suite, Ubuntu 24.04 non-root | the same, in a container with the repository mounted read-only | 448 tests OK, 15 skipped (platform-specific) |
| `pairing-store/1` conformance | `bun plugins/omp-private-relay/tests/conformance/pairing-store-suite.ts` with this plugin's `tests/conformance/adapter.py` as reader and writer | 30 passed, 0 failed |
| Relay metadata test (reads `docs/protocol`) | `bun test tests/unit/metadata.test.ts` in `plugins/omp-private-relay` | 16 pass, 0 fail |
| Mutation check | `python3 tests/mutation/run_mutations.py --only <row>`, once per row | 65 rows, 65 caught, none missing an anchor |
| Publication gate | `PRIVATE_TERMS_FILE=<file outside the repository> scripts/check-public.sh --commit HEAD` | tree, history and a fresh single-commit export: 0 failing findings; gitleaks: no leaks |
| Real Serve, macOS, scratch home and ports | a hub with `both`, one Serve route added and removed at a time; `doctor`, `serve-check`, `pair --tailnet`, a real browser paired on both addresses | run on the previous revision of this candidate (the code since changed only in the `doctor` branches for an unknown identity and for Funnel with no Web route, in the wording of messages and in the documents): a route pointed at a dead port made `doctor` and `serve-check` fail; both addresses paired in a real browser. Not repeated on this revision; Funnel was never switched on for it |

Not run: `make check-contrib` as a whole and `make export-gate`; their parts are the rows above. The real relay was not run, so Open and Watch were not followed through it (see above).
