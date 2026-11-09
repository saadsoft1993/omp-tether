# Release evidence: omp-dashboard 0.1.0

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

## Not verified, and why

- **Tailscale Serve on a real tailnet.** Only a stand-in `tailscale` command was used; whether Serve replaces a caller-supplied `Tailscale-User-Login` is untested.
- **herdr.** It was not installed; the backend is tested against a stand-in command only. tmux 3.4 was exercised for real.
- **Open and Watch in a browser** need a running `omp-private-relay` with its web client; the page's refusal states are what the real-omp test exercises.
- **Force kill after a Kill that failed, with a real omp:** a stopped omp is not listed and a running one ends on SIGTERM, so the sequence cannot be provoked; it is tested with real processes.
- **`make release-check`'s relay half** belongs to omp-private-relay and needs an omp checkout at its tag and the built web archive.
- **Pid and group reuse between a check and a signal** is narrowed (start-time checks, signalling a process group only while its leader is unreaped), not closed: a pidfd is not available on macOS.
- **Name resolution** is not covered by the overall HTTP deadlines.

## Last recorded run

See the final report that accompanies the candidate commit; the candidate is the tip of the `dashboard` branch.
