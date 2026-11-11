# Compatibility

`compat.json` at the repository root is the machine-readable version of this page.

## omp

| | |
|---|---|
| Minimum | 18.18.0 |
| Tested | 18.18.0 |

Only a listed version has been run. The extension uses omp's documented extension API for everything it needs, plus two
omp-internal modules that are loaded optionally (see `ompInternals` in `compat.json`): if either goes away the extension
still loads and works, and loses one feature (live self-disable) or reads only defaults and environment variables.

`make gate` loads every plugin in a real omp in a scratch home and checks that it registers what it should. Run it
against a new omp release before adding that release to `tested`.

### `omp-private-relay`

Tested with omp 18.18.0 and 18.18.3 (the web client's patch also applies, byte for byte, to the 18.18.0 sources; the pinned build is from v18.18.3). The relay uses omp
through its command line and its Collab wire contract only (see `ompInterfaces` in `compat.json`), not through the
extension API. Its web client is **built per omp minor** from omp's release tag and pinned by hash (`webBuilds` in
`compat.json`, mirrored in `plugins/omp-private-relay/web-builds.json`); a minor without a build needs a patch file
(`patches/collab-web-<minor>.patch`) and a new build before it can be listed. The build is reproducible: the same tag
built with the same Bun gives the same content hash, which a release job compares with the pinned value.

### `omp-dashboard`

Tested with omp 18.18.0 and 18.18.3. It uses omp through its command line only (`omp --version`, `omp collab list --json` and `omp collab link --json` at JSON version 1, `omp config get`), plus `session-control/1` for Stop and Rename and `relay-check/1` for the relay. `doctor` fails on an omp older than 18.18.0 and warns on a version not listed in `ompTested`. The dashboard page is built with Bun and committed, pinned by hashes (`plugins/omp-dashboard/ui-build.json`). Sign-in adapters (`auth.adapter`): `loopback-only`, `tailscale-serve` and, from 0.2.0, `both` (the local address and Tailscale Serve at once); config schema 1 is unchanged in shape, `both` is one more allowed value.

### `omp-memory`

Tested with the real binaries of **omp 18.18.0** (the Mac mini) and **18.18.3** (the MacBook). Every published `@oh-my-pi/pi-mnemopi` from **18.18.0 to 18.18.8** passes the structural SQL check, the write test and the importer's Bun suite (210 tests; `bun verify_versions.ts --suite`, run again after the last change to the write path). The importer writes through the published `@oh-my-pi/pi-mnemopi` at **exactly omp's version** (omp's plugin directory, or installed once by `omp-memory setup`; see `ompInterfaces` in `compat.json`) and refuses any other version, a patch release included, naming the `setup` command; there is no override. Before a write the library must also pass a write test in a scratch bank; a failure names the step. A version that has not been listed above is accepted only if it passes that test. The structural SQL check looks for the statements the importer relies on in the package and, when it is big enough to scan, in the omp binary; when it cannot be scanned `doctor` warns and does not claim parity.

**Tested** on **macOS arm64** (MacBook and Mac mini) and in an **Ubuntu 24.04 arm64 container** as a non-root user; **not tested** on Linux x64, on a Linux whose userland is neither GNU nor BSD (musl, busybox), or with other omp versions. Windows is not supported. On Linux x64, the install script of `onnxruntime-node` (fetched by `setup` through omp's library) downloads CUDA binaries from GitHub unless `ONNXRUNTIME_NODE_INSTALL_CUDA=skip` is set; that was read from the script and not run.

## Bun

The extension runs on the Bun that omp embeds (1.4.4 in omp 18.18.0). `omp-memory`'s importer runs on a Bun 1.4.4 or newer that it finds on `PATH` or in the usual places (never the omp binary: the importer runs `omp --version` itself). So does `omp-private-relay`: it runs as `BUN_BE_BUN=1 <omp> <script>`, never needs a separate Bun or Node, and refuses to start on a Bun older than 1.4.4 (the version collab-web's build needs). Running the tests here needs Bun 1.4.4 or newer.

## Python

`omp-services` and `omp-dashboard` need Python 3.10 or newer and no third-party packages; they are tested on 3.10 and 3.14 on macOS and on Ubuntu. `omp-memory` needs Python 3.10 or newer, standard library only, and was run on 3.14 (macOS) and 3.13 (Debian 13).
macOS's own `/usr/bin/python3` is 3.9 and is refused with a message that says what to install.

## Platforms

macOS and Linux (`omp-memory`: macOS arm64 and an Ubuntu 24.04 arm64 container were run; Linux x64, musl and busybox were not, see `platformsTested` and `platformsNotTested` in `compat.json`; its scripts use POSIX tools plus `sqlite3` and `shasum` or `sha256sum`, and it finds open banks with `lsof` or, on Linux without it, `/proc`). Not Windows: omp uses named pipes there, and this repository's plugins use Unix sockets. Listener ownership checks use `lsof` on macOS and `/proc/net/tcp` (or `ss`) on Linux. Tailscale Serve's header behaviour was tested against a real Serve on macOS (Tailscale 1.104.5: a caller-sent `Tailscale-User-Login` is replaced, a caller-sent Funnel header is removed); the `both` adapter was also run against a real Serve route on macOS (Tailscale 1.104.4) with the local address, the tailnet name and Tailscale stopped. Not tested on Linux, and not with a real non-owner tailnet identity.

## Protocols

| Protocol | Versions | Document |
|---|---|---|
| `session-control` | 1 | [`protocol/control-1.md`](protocol/control-1.md) |
| `health` | 1 | [`protocol/health-1.md`](protocol/health-1.md) |
| `services` | 1 | [`protocol/services-1.md`](protocol/services-1.md) |
| `pairing-store` | 1 | [`protocol/pairing-store-1.md`](protocol/pairing-store-1.md) |
| `relay-check` | 1 | [`protocol/relay-check-1.md`](protocol/relay-check-1.md) |

Protocol numbers change only by spec, independently of plugin versions.
