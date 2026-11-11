# Changelog

All notable changes to `omp-dashboard`. The protocols it speaks (`pairing-store/1`, `session-control/1`, `relay-check/1`, `services/1`,
`health/1`) have their own numbers and change only by spec; see `docs/protocol/` in the repository.

## 0.2.0 (2026-11-11)

- New sign-in adapter `both` (`auth.adapter: "both"`, `OMP_DASHBOARD_AUTH=both`): the hub answers on its own loopback address and through Tailscale Serve at the same time, so the local address keeps working with Tailscale switched off. Every request is judged by exactly one of the two existing rule sets: a request with any sign of a proxy (a `Tailscale-*` or `X-Forwarded-*` header, `Forwarded`, `Via`, `X-Real-IP`; any letter case) by the `tailscale-serve` rules, one with none by the `loopback-only` rules, and a request with no sign of a proxy can never use the tailnet rules. The POST `Origin` and the cookie's `Secure` flag follow the way the request came (`Secure` only through Serve, because a browser may refuse a `Secure` cookie from plain `http://127.0.0.1`); a browser pairs once per address; a pairing link works once on either address. See `docs/threat-model.md`.
- With `both` a device is let in only the way it was paired. The local cookie is not `Secure` and is sent to every service on `127.0.0.1`, so a copy of it must not open the tailnet path to a program that writes `Tailscale-User-Login` itself, and the reverse. Each pairing records its way (`local` or `tailnet`) in an optional `access` field of the device's record in `devices.json` (`pairing-store/1` is unchanged: readers use only the keys, and the spec now names the field); the hub accepts a cookie only on a request of its device's way. A device with no recorded way in (made by `omp-private-relay`'s own `pair` or by an older hub) is refused on both ways and has to be paired again. The other adapters record their one way and are unchanged. `doctor` counts devices by way; `devices` shows it.
- New setting `pairing.localMaxAgeDays` (`OMP_DASHBOARD_PAIRING_LOCAL_MAX_AGE_DAYS`, default 7, never more than `pairing.maxAgeDays`): with `both`, the local device and its cookie expire sooner than a tailnet one.
- Documented: `ssh -L`, Docker port mappings, VS Code forwarded ports and `tailscale serve --tcp` carry no proxy header and keep a loopback `Host`, so they are served as direct (local) requests; the `Host` check and the cookie protect that path.
- `omp-dashboard pair` prints the local link when the adapter is `both`; `pair --tailnet` prints the Serve link and, when Tailscale is off, says so, prints the local link and exits 1. `pair --json` adds `access` (`local` or `tailnet`).
- Open and Watch with `both`: the bundled relay admits one way (its sign-in is `token` for direct browsers or `tailscale-serve` for browsers through Serve), named by the new setting `relay.admits` (`local`, the default, or `tailnet`). Browsers of that way get a link; browsers of the other way get 409 `relay_other_path` and a message instead of a link the relay would refuse. A direct browser's link opens at the host name it used (`127.0.0.1` or `localhost`) at the relay's guest port; a port forward, `[::1]` or another name gets 409 `relay_address`. One relay serving both ways at once needs a change in `omp-private-relay` and is not part of this release.
- `doctor` with `both` reports the local address (`access-local`, asked of the sign-in itself with an unpaired request, not of `/healthz`), Tailscale and the Serve route separately. Tailscale off or not found and an absent route are warnings for the tailnet path; a route that points elsewhere and Funnel being on are failures, together or alone.
- The health drop-in with `both`: the `serve-route` check is a `note` (a stopped Tailscale or an absent route never makes the hub unhealthy) and a separate `serve-integrity` command check (`omp-dashboard serve-check`) fails when Funnel is on or the route points elsewhere.
- `pair --tailnet` asks Tailscale for its `BackendState` (bounded `tailscale status --json`), also when the owner and name are in the config, and prints the Serve link only when it is `Running`; otherwise it says whether Tailscale is stopped, needs a login, did not answer or is missing, prints the local link and exits 1.
- `loopback-only` and `/healthz` now treat every `Tailscale-*` and `X-Forwarded-*` header, `Via` and `X-Real-IP` as a sign of a proxy (before: a fixed list of six names). Alert clicks open the tailnet address with `both` as with `tailscale-serve`.
- Config schema: `auth.adapter` accepts `both`; new `relay.admits` (`local` or `tailnet`) and `pairing.localMaxAgeDays`; `relay.webUrl` and `relay.servePort` apply to a `tailnet` relay with `both`. `/api/hub/about` reports `adapter: "both"`.
- Tests: `tests/python/test_both.py` (classification matrix, cookie and Origin per path, pairing on both addresses, forged and duplicated headers over the wire, Tailscale off, relay links, `pair`, `doctor`, the drop-in and `omp-services`' health runner); 12 new mutation rows for the guards.
- Tested for real on macOS (Tailscale 1.104.4): both addresses, a real Serve route on a scratch port, the route removed, and Tailscale stopped; pairing in a real browser on both addresses. Not tested: a real non-owner tailnet identity, Serve on Linux, Safari.

## 0.1.1 (2026-11-11)

- `doctor` no longer calls an omp that is older than the newest tested version "newer than the versions tested"; it says the version is supported but not one of the tested ones.
- Tested omp versions are 18.18.0 and 18.18.3, stated once in `compat.json` and checked against the code, the README and `docs/compat.md` by a test (the code and `compat.json` disagreed about the tested list).
- `doctor` names `auth.tailscale.bin` (and `OMP_DASHBOARD_TAILSCALE_BIN`) when the `tailscale` command cannot be found and the owner and machine name are not fixed in the config; the macOS app-bundle fallback applies only when no path is configured, the same rule the sign-in uses.
- Documents: a real Tailscale Serve (macOS, Tailscale 1.104.5) was tested: it replaced a caller-sent `Tailscale-User-Login`, removed a caller-sent Funnel header and replaced `X-Forwarded-For`; herdr 0.9.5 Start was tested for real. Not tested: a non-owner tailnet identity, Serve and herdr on Linux.

## 0.1.0 (2026-11-11)

First release.

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
- Behaviour: no stats proxy (only the `stats.url` link-out); a pairing link that is on file bypasses the failure limit; pairings expire (`pairing.maxAgeDays`, default 90) and `revoke all` cancels unused links; POSTs must be `application/json`; a second `Host`/`Tailscale-User-Login` header is refused; ntfy topics are 16+ characters and never an argument; Force kill re-checks each process below the session by start time and reports what is left; every writer checks the directories above what it creates; a group-writable directory is trusted only on Linux and only when the group has no other member; macOS ACL entries marked `inherited` are read; the hub can be restarted on its port at once; overall deadlines on the relay check, ntfy and the doctor probe.
- The project list is capped inside one directory; a browser that leaves mid-answer prints no traceback.
