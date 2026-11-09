# Changelog

All notable changes to `omp-private-relay`. The protocols it implements have their own numbers (`pairing-store/1`,
`relay-check/1`) and change only by spec; see `docs/protocol/` in the repository.

## 0.1.0 (2026-11-11)

Review fixes (independent security reviews, every finding fixed or answered; tests in `tests/unit/review-fixes.test.ts`, and
`tests/mutation` reverts each fix and requires a test to fail):

- `rebuild-web` creates the data directory's parent (it crashed on a fresh install). `pair` first proves that the guest port
  is this relay and creates no token and prints no link otherwise (and no orphan token when the address is unknown).
- The pairing endpoint requires a Content-Length (no chunked bodies), counts bytes as they arrive, the server bounds request
  bodies, and the failure budget is reserved before any await, so simultaneous attempts cannot exceed it.
- The listener proof key is published only after both listeners are bound, and a second relay can no longer share a port
  (`reusePort` off), so a failed duplicate start no longer invalidates the running relay's proof.
- The config file is read through one descriptor and refused in a directory others can write; `init` validates the whole
  configuration before it runs the omp the file names. The Tailscale lookup no longer blocks the event loop.
- `status` believes the relay's numbers only from a listener that proved itself, follows no redirect and prints only numbers.
- Drop-ins pin the same config file (and XDG roots) for the service and the proof command (which no longer needs a shell),
  and refuse settings that exist only as environment overrides.
- A zero-byte store file and a loose-mode lock file are refused by the writer (conformance checks added); `doctor` uses the
  reader's checks and the real Bun check; hashed assets are cached `private`; the restart grace is capped at 120 s; a cap
  on all open sockets (`limits.maxConnections`); logged paths are bounded; another tool's directories are never chmod-ed.
- The web asset: loopback http is accepted only as the starting URL, `NOTICE` is pinned separately from the content hash,
  an install interrupted between its renames is recovered, `--asset-file` is size-bounded, and `scripts/verify-web.sh` was run
  against the pin (`make verify-web`).
- Verification pass: the web client's whole path is checked (and refused when another account could replace it), directories
  are created 0700 whatever the umask and a configured key directory is refused rather than chmod-ed; the connection cap is
  enforced before the upgrade; `status` believes only counts that carry a MAC (`/__relay_stats`, relay-check/1 section 5) and
  reads them bounded; installs are serialised and recorded so recovery touches only its own directories; the drop-ins pin the
  resolved XDG roots; the installed `NOTICE` is verified and a notice-only release is installed; the mutation harness requires
  the designated test itself to fail; `make release-check` requires the archive and omp-services' schemas.
- Second verification pass: the web path is walked component by component (links followed and their targets walked, `..`
  taken from the real directory, owners, writable bits, sticky, ACLs through `ls -le` / `getfacl`, loops refused), a
  group-writable directory is accepted only for a group shown to have no member but you or root (never on macOS), and the
  relay serves from the resolved directory whose identity it re-checks; error pages and health answers never open the web tree
  and an index that is a FIFO or a directory cannot stall or fail them; the install lock is an advisory `flock` on a validated
  descriptor held across the whole install, staging directories have unpredictable names created exclusively, a damaged
  record stops the install; the signed-answer bound is in bytes; an install recorded without a NOTICE hash is "legacy", not
  verified; `--asset-file` is read with a bound; the mutation harness runs each case in a sandbox and checks that nothing
  outside it changed, and selects the one designated test.
- Documentation: the cookie is not port-scoped, a local process can forge the Tailscale header and spend the pairing budget,
  the listener proof detects but does not prevent navigation to an impostor, install is two renames.

First release. It generalises a relay that ran for one person on two machines, and carries over the protections of that relay that its tests can check (see the threat model for what remains accepted).

- The Collab relay (`src/relay.ts`): host listener for omp sessions and guest listener for browsers, both on `127.0.0.1`
  only; room contract of omp's stand-in relay; caps on rooms, guests, frame sizes and queued bytes; slow-consumer cut
  (4008); 60 s restart grace; revocation of open pages (4401) with a forced cut after 5 s and nothing relayed after the
  close; static web client with a strict Content-Security-Policy; `/healthz`, a counts-only `/stats`, and the listener
  proof (`relay-check/1`).
- Swappable sign-in adapters: `token` (default; loopback only) and `tailscale-serve` (owner login, machine name, Origin,
  no Funnel, owner and name refreshed every minute, fail closed while unknown).
- Pairing: `pairing-store/1` reader and writer with a conformance suite that also runs against deliberately flawed
  implementations; one-time pairing links redeemed by the relay itself (`own` mode) or a store written by another program
  (`shared` mode); a failure limit on redemption; `pair`, `devices`, `revoke`.
- Command line: `init` (asks before each step, never edits omp's settings silently), `status`, `doctor`, `check`,
  `rebuild-web`, `dropins`, `run`.
- Configuration: one JSON file with a JSON Schema, `OMP_PRIVATE_RELAY_*` overrides, refusal of unknown keys and of files
  other accounts can write. Ports are configuration; tests use free ports.
- Runs on the Bun embedded in omp (`BUN_BE_BUN` scoped to the one command that needs it), macOS and Linux; listener
  ownership through `lsof` (macOS) or `/proc/net/tcp` and `ss` (Linux), and an owner that cannot be seen is unknown, never
  assumed to be you.
- The patched web client for omp 18.18, a release asset pinned by two hashes and verified before installation; the patch
  for omp 18.18 also connects over `ws://` on loopback http, and shows a relay-specific text for close code 4401.
- Drop-ins for `omp-services`: a `services/1` unit description and a `health/1` check list.
- Fixed while following the README on a clean machine: `doctor` called the pairing store of an `own` setup a failure until the first browser had been paired; it is now a warning ("no browser is paired yet", fix `omp-private-relay pair`). A missing store in `shared` mode still fails.
