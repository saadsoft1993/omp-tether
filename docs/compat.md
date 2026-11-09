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

Tested with omp 18.18.3 (the web client's patch also applies, byte for byte, to the 18.18.0 sources). The relay uses omp
through its command line and its Collab wire contract only (see `ompInterfaces` in `compat.json`), not through the
extension API. Its web client is **built per omp minor** from omp's release tag and pinned by hash (`webBuilds` in
`compat.json`, mirrored in `plugins/omp-private-relay/web-builds.json`); a minor without a build needs a patch file
(`patches/collab-web-<minor>.patch`) and a new build before it can be listed. The build is reproducible: the same tag
built with the same Bun gives the same content hash, which a release job compares with the pinned value.

### `omp-dashboard`

Tested with omp 18.18.3. It uses omp through its command line only (`omp --version`, `omp collab list --json` and `omp collab link --json` at JSON version 1, `omp config get`), plus `session-control/1` for Stop and Rename and `relay-check/1` for the relay. `doctor` fails on an omp older than 18.18.0 and warns on a version not listed in `ompTested`. The dashboard page is built with Bun and committed, pinned by hashes (`plugins/omp-dashboard/ui-build.json`).

## Bun

The extension runs on the Bun that omp embeds (1.4.4 in omp 18.18.0). So does `omp-private-relay`: it runs as `BUN_BE_BUN=1 <omp> <script>`, never needs a separate Bun or Node, and refuses to start on a Bun older than 1.4.4 (the version collab-web's build needs). Running the tests here needs Bun 1.4.4 or newer.

## Python

`omp-services` and `omp-dashboard` need Python 3.10 or newer and no third-party packages. It is tested on 3.10 and 3.14 on macOS and on
Ubuntu. macOS's own `/usr/bin/python3` is 3.9 and is refused with a message that says what to install.

## Platforms

macOS and Linux. Not Windows: omp uses named pipes there, and this repository's plugins use Unix sockets. Listener ownership checks use `lsof` on macOS and `/proc/net/tcp` (or `ss`) on Linux. Tailscale Serve's header behaviour was not tested against a real Serve.

## Protocols

| Protocol | Versions | Document |
|---|---|---|
| `session-control` | 1 | [`protocol/control-1.md`](protocol/control-1.md) |
| `health` | 1 | [`protocol/health-1.md`](protocol/health-1.md) |
| `services` | 1 | [`protocol/services-1.md`](protocol/services-1.md) |
| `pairing-store` | 1 | [`protocol/pairing-store-1.md`](protocol/pairing-store-1.md) |
| `relay-check` | 1 | [`protocol/relay-check-1.md`](protocol/relay-check-1.md) |

Protocol numbers change only by spec, independently of plugin versions.
