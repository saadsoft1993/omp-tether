# Release evidence

What was run against the **archive that is released**, by which method, and what was not. Source tests and socket tests do not
show that the distributed browser client behaves correctly, so a release needs the browser scenarios below, run on that
archive, recorded here.

## Procedure for a release

1. `make release-check-relay WEB_OMP_SRC=<omp checkout at the tag> OMP_TAG=<tag> ASSET=<archive> SERVICES_SCHEMAS=<omp-services schemas>`: rebuilds
   the web client from the tag and compares it with the pin (archive sha256, content hash, NOTICE hash), then runs the whole
   suite with the archive and the schemas required (a missing one fails; nothing is skipped).
2. `scripts/browser-check.py --asset <archive>`: a scratch HOME, random ports, `init --asset-file` of that archive, an omp
   session hosting through the relay. It prints a pairing link and a session link and takes commands (`restart`,
   `revoke-all`, `pair`, `stop`) from a control file. Drive a real browser through the scenarios below.
3. Fill in the record: the archive's sha256, the omp, Bun and browser versions, the platform, and each scenario's outcome.

## Scenarios

| # | Scenario | Expected |
|---|---|---|
| 1 | Open the pairing link, then the session link in the same browser | "Paired", then the session shows **Live** with the host's model and an enabled prompt box |
| 2 | `restart` the relay while the page is open | host and browser reconnect within the grace period; the page shows **Live** again |
| 3 | `revoke-all` while the page is open | within a few seconds the page shows **Ended** with "this browser is not paired with the relay; pair it again, then reload" |
| 4 | Open the session link in a browser that was never paired | the 4401 text; no session content |

Scenario 4 and the adapter and slow-consumer behaviours below are exercised by the test suite (sockets), not by a browser.

## Record: 0.1.0 candidate

| | |
|---|---|
| Archive | `omp-private-relay-web-18.18.tar`, sha256 `8702cb37df80852b6b9869f2ad56b90f9fe5a48a26cf5c8dad975a3e66030af9` (the pin in `web-builds.json`, rebuilt from tag `v18.18.3` by `scripts/verify-web.sh`) |
| omp | 18.18.3 (embedded Bun 1.4.4); the relay ran under it through `bin/omp-private-relay` |
| Platform | macOS, arm64 |
| Browser | Chrome 156 (driven through its DevTools protocol) |
| Adapter | `token` (loopback) |
| Date | 2026-11-09 |

| # | Outcome |
|---|---|
| 1 | Passed: "Paired. You can close this tab…", then the session page showed **Live** ("Tether Test Lite", prompt box enabled) |
| 2 | Passed: after the relay was restarted the relay log shows the host and the browser reconnecting about one second later (room opened by the host, then the guest with the same device), and the page showed **Live** |
| 3 | Passed: `revoke all` reported one browser unpaired; the page changed from **Live** to **Ended** with the not-paired text within the 15 s observed |
| 4 | Not run in a browser (covered by `tests/unit/relay-token.test.ts`) |

## Not exercised in a browser, and why

- **Slow-consumer cut (4008) and reconnect:** needs a stalled browser; covered by the relay's socket tests.
- **`tailscale-serve` adapter:** a real Tailscale Serve (1.104.5, macOS) was tested on a second machine with a real browser over the tailnet: pair, open a session ("Live"),
  revoke (the page closed with 4401 within 15 s); a forged `Tailscale-User-Login` was replaced by the real owner's login, a forged Funnel header was removed. The test tailnet had one user, so a request from a real
  non-owner identity was not tried (proven only with forged headers on loopback), and the header behaviour is **unverified on Linux**.
- **Linux and x86_64 browsers:** the suite and the conformance checks run in an Ubuntu 24.04 container (aarch64) as a non-root
  user; no browser was run there.
- **A different omp minor:** each minor needs its own archive, pin and record.
