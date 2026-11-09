# Changelog

All notable changes to `omp-dashboard`. The protocols it speaks (`pairing-store/1`, `session-control/1`, `relay-check/1`, `services/1`,
`health/1`) have their own numbers and change only by spec; see `docs/protocol/` in the repository.

## 0.1.0 (2026-11-11)

First release. It generalises a hub that ran for one person on two machines.

- The hub (Python 3.10+, standard library only): serves the page and `/api/hub/*` on one loopback address; sessions from
  `omp collab list --json`; links from `omp collab link`, only for rooms on your private relay and only after `relay-check/1` proves the relay;
  Stop and Rename over `session-control/1`; Kill and Force kill with identity checks (owner, `omp` command, start time not after the room's,
  typed confirmation, Force only after a Kill that failed); Start through `herdr`, `tmux` or `off` (default); audit log; ntfy alerts (off until
  a topic is set); the machine banner; a link-out to a stock `omp stats`.
- Sign-in adapters `loopback-only` (default) and `tailscale-serve`; per-browser pairing (`pairing-store/1`, conformance suite passes; readable by
  `omp-private-relay`); a limit on failed pairing attempts; POSTs need a custom header and the hub's own Origin.
- Every read from a client is bounded before anything is decided (header bytes and time, body bytes and time, connections); answers are fixed texts.
- Every file that matters is read through one open descriptor and its path is walked component by component (links followed, owners, writable
  bits, ACLs through `ls -le` or the POSIX attribute); an unreadable ACL is not trusted.
- The page: built from `ui/` with Bun, committed in `ui-dist/`, pinned by `ui-build.json`, verified at start and by `make verify-ui`.
- Command line: `run`, `pair`, `devices`, `revoke`, `doctor`, `config`, `dropins`. Drop-ins for `omp-services` (`services/1`, `health/1`).
- Platforms: macOS and Linux (`lsof`/`ps` on macOS; `/proc` with an `ss` fallback on Linux, so no `ps` is needed and busybox is not a problem).

- Tests: every module has its own suite; an integration test runs the hub against two live omp sessions with the real session-control extension; a mutation check covers 24 guards; the suite also passes in an Ubuntu 24.04 container as a non-root user. See `docs/release-evidence.md`.
- After review: no stats proxy (only the `stats.url` link-out); a pairing link that is on file bypasses the failure limit; pairings expire (`pairing.maxAgeDays`, default 90) and `revoke all` cancels unused links; POSTs must be `application/json`; a second `Host`/`Tailscale-User-Login` header is refused; ntfy topics are 16+ characters and never an argument; Force kill re-checks each process below the session by start time and reports what is left; every writer checks the directories above what it creates; a group-writable directory is trusted only on Linux and only when the group has no other member; macOS ACL entries marked `inherited` are read; the hub can be restarted on its port at once; overall deadlines on the relay check, ntfy and the doctor probe.
- Fixed while testing: the project list was not capped inside one directory; a browser that left mid-answer printed a traceback.
