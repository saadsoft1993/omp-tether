# omp-private-relay

A **private Collab relay** for [omp](https://github.com/can1357/oh-my-pi) that runs on your own machine, so opening a
session in a browser never goes through omp's public relay. It passes the end-to-end encrypted room traffic between your
omp sessions and your browsers (it never sees plaintext), serves a patched copy of omp's browser client, and lets only
browsers that **you** paired in.

- **Nothing happens until you run `init`.** Installing the plugin starts no program, opens no port and writes no file.
  `init` shows what it will do and asks before each step; it never edits omp's settings unless you say so.
- **Loopback only.** Both listeners bind `127.0.0.1` and there is no setting that moves them. Reaching the relay from
  another device is done on purpose, through a tunnel you set up (an SSH port forward, or `tailscale serve`).
- **Every browser is paired.** A browser needs a cookie that you handed it with a one-time link. You can list and
  revoke browsers; a revoked browser's open page is cut within seconds.
- **Swappable sign-in checks.** `token` (default: loopback only, no other service needed) and `tailscale-serve` (reached
  through Tailscale Serve, which must also name you as the owner). The relay does not look at which one it has.
- **Documented contracts.** [`pairing-store/1`](../../docs/protocol/pairing-store-1.md) (what a dashboard writes and the
  relay reads) and [`relay-check/1`](../../docs/protocol/relay-check-1.md) (proof that a port is really this relay), each
  with tests that any implementation can run.

Supported: macOS and Linux, omp 18.18.x (tested on 18.18.0 and 18.18.3), one trusted user account. Not supported:
Windows; machines that several people share (see [Who this protects against](#who-this-protects-against)).

## What you need

- omp. The relay and its tools run on the Bun that omp embeds; you do not install Bun or Node.
- To reach it from another device: an SSH connection to this machine (token adapter) or Tailscale (tailscale adapter).
- Network access once, to download the web client during `init` (about 1 MB, from this repository's release page, checked
  against a hash that ships inside the plugin). You can fetch it yourself and pass `--asset-file` instead.

## Install

```
omp plugin marketplace add saadsoft1993/omp-tether
omp plugin install omp-private-relay@omp-tether
```

omp links installed plugins under `~/.omp/plugins/node_modules/`. The command line is
`~/.omp/plugins/node_modules/omp-private-relay/bin/omp-private-relay`; it is a short shell script that runs
`src/cli.ts` on omp's embedded Bun. Put it on your `PATH` if you like, for example:

```
ln -s ~/.omp/plugins/node_modules/omp-private-relay/bin/omp-private-relay ~/.local/bin/omp-private-relay
```

Everything below calls it `omp-private-relay`. `omp-private-relay help` lists the commands.

## Set it up

```
omp-private-relay init
```

It finds omp, then asks about each step in turn:

1. **The config file** `~/.config/omp-private-relay/config.json` (mode `600`), with two free loopback ports chosen from
   17796 and 17797 upward.
2. **The web client** for your omp version, downloaded and verified (see [Web client](#web-client)).
3. **omp's settings.** It prints the two exact lines for your setup and asks whether to run them:

   ```
   omp config set collab.relayUrl ws://127.0.0.1:17797
   omp config set collab.webUrl http://127.0.0.1:17796
   ```

   omp reads these when a session starts hosting. Sessions that are already running keep their old relay until you run
   `/collab stop` and `/collab` in them (or restart them). To undo: `omp config reset collab.relayUrl` and
   `omp config reset collab.webUrl`.

`--yes` answers yes to creating files and downloading; it never edits omp's settings. Use `--set-omp-settings` for that,
or answer `y` when asked.

## Run it

Without any service manager, in a terminal you keep open (or in `tmux`/`screen`):

```
omp-private-relay run
```

To have it start at login and restart when it stops, use [`omp-services`](../omp-services) (a separate plugin in this
repository):

```
omp-private-relay dropins          # shows the two files; nothing is written
omp-private-relay dropins --write  # writes them under ~/.config/omp-services/ (needs the config file; settings made only by OMP_PRIVATE_RELAY_* variables are refused)
omp-services init                  # shows its plan and asks before installing the unit
```

`dropins` writes a `services/1` description of the relay (so omp-services can render a launchd or `systemd --user` unit)
and a `health/1` file (so its heartbeat watches the ports and proves they are this relay). This plugin does not install
units itself. If you would rather write a unit by hand, it needs exactly this command, run as your user with a private
umask: `BUN_BE_BUN=1 /absolute/path/to/omp /absolute/path/to/omp-private-relay/src/main.ts`. A minimal
`~/.config/systemd/user/omp-private-relay.service`:

```
[Unit]
Description=omp private relay

[Service]
Environment=BUN_BE_BUN=1
ExecStart=/absolute/path/to/omp /absolute/path/to/omp-private-relay/src/main.ts
Restart=always
UMask=0077
NoNewPrivileges=yes

[Install]
WantedBy=default.target
```

Use the real plugin directory rather than a link if you do not want `omp plugin upgrade` to move it under you, and
re-check the path after an upgrade. Set `BUN_BE_BUN` only on this one command: with it set, omp behaves as a plain Bun, so
it must never reach a program that runs `omp` itself.

## Pair a browser

```
omp-private-relay pair
```

needs the relay to be running: it first checks that the guest port proves to be this relay, and prints no link (and creates no token) if it does not. It prints a link that works **once**, for **5 minutes**; treat it as a secret and do not run it into a log. Open it in the browser you want to pair; the page removes the token
from the address bar, redeems it, and the browser receives a cookie. Then open a session link from omp (`/collab`) in the
same browser.

With the token adapter the browser must reach `127.0.0.1:<guest port>` on this machine: open it here, or from another
machine through an SSH forward **with the same port number** on both ends:

```
ssh -L 17796:127.0.0.1:17796 you@this-machine
```

(A forward with another local port needs that `host:port` in `auth.token.allowedHosts`.)

```
omp-private-relay devices       # id, date and label of each paired browser
omp-private-relay revoke <id>   # unpair one (4 or more characters of the id)
omp-private-relay revoke all
```

A revoked browser's open connection is closed with code 4401 within `limits.recheckMs` (10 s by default). Its page then
says it is not paired and does not retry.

## Reach it from your phone with Tailscale

Set `auth.adapter` to `tailscale-serve` (`omp-private-relay init --adapter tailscale-serve` on a new setup), then expose
the guest listener on your tailnet **only** (never Funnel):

```
tailscale serve --bg --https=17796 http://127.0.0.1:17796
```

Every request must then carry your Tailscale login (set by Serve), this machine's name as `Host`, and the matching
`Origin`; requests that arrive through Funnel are refused. The owner and the name are read from `tailscale status --json`
and refreshed every minute; until they are known, every browser request gets `503`, and when they change the open pages
are closed. `omp-private-relay doctor` checks the Serve route and warns when Funnel is switched on for it. Pair the phone
with `omp-private-relay pair` (the link names this machine's tailnet address). A real Serve was tested on macOS (Tailscale 1.104.5): it replaced a `Tailscale-User-Login` header sent by the caller with the real owner's login and removed a caller-sent Funnel header; Linux and a second, non-owner tailnet identity were not tested (see `docs/threat-model.md`).

A pairing link that is opened from another site (a chat, a mail client, a redirect) is refused with a short explanation: copy the link and paste it into the address bar of the browser you want to pair.

## Check it

```
omp-private-relay doctor     # what is wrong, and the command that fixes it
omp-private-relay status     # what is configured, and whether it is running
omp-private-relay check      # quiet yes/no (exit code): do both listeners prove to be this relay?
```

`doctor` verifies the config, omp and its embedded Bun, the web client's content hash, the pairing store, that **each
listener answers a fresh challenge with the relay's private key** (so it is not another program that took the port), who
owns each listening socket (`lsof` on macOS, `/proc/net/tcp` or `ss` on Linux; an owner it cannot see counts as unknown,
never as yours), and that omp's `collab.relayUrl` points here.

## Configuration

`~/.config/omp-private-relay/config.json` (or `$XDG_CONFIG_HOME/omp-private-relay/config.json`; another file with
`OMP_PRIVATE_RELAY_CONFIG`). Every key is optional except `schemaVersion`; an unknown key, a wrong type or a file that
group or others can write is refused, with every problem listed. The JSON Schema is
[`schemas/config-1.schema.json`](schemas/config-1.schema.json). Secrets are never in this file.

| Key | Environment variable | Default | Meaning |
|---|---|---|---|
| `schemaVersion` | | `1` | Required in the file |
| `omp.bin` | `OMP_PRIVATE_RELAY_OMP_BIN` | `null` | Absolute path of omp (otherwise `OMP_BIN`, then `PATH`) |
| `listen.hostPort` | `OMP_PRIVATE_RELAY_HOST_PORT` | 17797 | Where omp sessions connect |
| `listen.guestPort` | `OMP_PRIVATE_RELAY_GUEST_PORT` | 17796 | Where browsers connect |
| `auth.adapter` | `OMP_PRIVATE_RELAY_AUTH` | `token` | `token` or `tailscale-serve` |
| `auth.token.allowedHosts` | | `[]` | Extra `host:port` values for an SSH forward on another local port |
| `auth.tailscale.bin` | `OMP_PRIVATE_RELAY_TAILSCALE_BIN` | `tailscale` | The tailscale command: the bare name is looked up on `PATH`, then in `/usr/local/bin`, `/opt/homebrew/bin` and the macOS app bundle (`/Applications/Tailscale.app/Contents/MacOS/Tailscale`); an absolute path is used as given. `doctor` and `init` name this key when the command cannot be found (a service or an ssh session often has a `PATH` without `/usr/local/bin`) |
| `auth.tailscale.servePort` | `OMP_PRIVATE_RELAY_TAILSCALE_SERVE_PORT` | guest port | The https port of `tailscale serve` |
| `auth.tailscale.owner` | `OMP_PRIVATE_RELAY_TAILSCALE_OWNER` | read from Tailscale | Owner login (with `publicHost`: no Tailscale lookups at all) |
| `auth.tailscale.publicHost` | `OMP_PRIVATE_RELAY_TAILSCALE_HOST` | read from Tailscale | This machine's MagicDNS name |
| `pairing.mode` | `OMP_PRIVATE_RELAY_PAIRING_MODE` | `own` | `own`: the relay pairs browsers itself. `shared`: another program writes the store and the relay only reads it |
| `pairing.dir` | `OMP_PRIVATE_RELAY_PAIRING_DIR` | `<config dir>/pairing` | The pairing store ([`pairing-store/1`](../../docs/protocol/pairing-store-1.md)) |
| `pairing.cookieName` | `OMP_PRIVATE_RELAY_COOKIE_NAME` | `omp_relay` | The cookie that holds the session id; in `shared` mode it must match the program that sets it |
| `web.dir` | `OMP_PRIVATE_RELAY_WEB_DIR` | `<data dir>/web/current` | Where the web client is served from |
| `checkDir` | `OMP_PRIVATE_RELAY_CHECK_DIR` | `<config dir>/check` | Holds the listener check key ([`relay-check/1`](../../docs/protocol/relay-check-1.md)) |
| `limits.maxRooms` | `OMP_PRIVATE_RELAY_MAX_ROOMS` | 32 | Rooms at once; the next host gets 4029 |
| `limits.maxGuestsPerRoom` | `OMP_PRIVATE_RELAY_MAX_GUESTS` | 8 | Browsers per room; the next gets 4029 |
| `limits.maxConnections` | `OMP_PRIVATE_RELAY_MAX_CONNECTIONS` | 256 | A hard cap on open WebSockets (hosts and browsers, counting upgrades in progress): beyond it the upgrade is refused with 503 before a socket exists. Plain HTTP connections are not counted (Bun closes idle ones) |
| `limits.maxHostFrame` | `OMP_PRIVATE_RELAY_MAX_HOST_FRAME` | 16 MiB | Largest frame from an omp session |
| `limits.maxGuestFrame` | `OMP_PRIVATE_RELAY_MAX_GUEST_FRAME` | 4 MiB | Largest frame from a browser |
| `limits.connQueueMax` | `OMP_PRIVATE_RELAY_CONN_QUEUE_MAX` | 32 MiB | Queued bytes per connection before it is cut with 4008 |
| `limits.relayQueueMax` | `OMP_PRIVATE_RELAY_QUEUE_MAX` | 128 MiB | Queued bytes in all before slow connections are cut |
| `limits.recheckMs` | `OMP_PRIVATE_RELAY_RECHECK_MS` | 10000 | How often open browsers are checked against the store (at most 30000) |
| `limits.restartGraceMs` | `OMP_PRIVATE_RELAY_RESTART_GRACE_MS` | 60000 | At most 120000. After a start, a missing room answers 4001 (retry) instead of 4004 |

Environment variables beat the file, which beats the defaults. Other variables read: `OMP_BIN` (the omp binary),
`OMP_SRC` (the omp checkout for `rebuild-web`) and `OMP_PRIVATE_RELAY_ASSET_BASE` (a mirror of the release assets; `https`,
or `http` to loopback). Directories follow `XDG_CONFIG_HOME` and `XDG_DATA_HOME`.

Close codes a browser can see: **4401** not paired (final: no retry), **4008** too slow (it reconnects), **4029** room
or relay full, **4001** room closed or not back yet (it retries), **4004** no such room, **4009** a host is already
connected for that room.

## Web client

omp's browser client (collab-web) is a private package of omp's repository, so it cannot be installed from npm. This
plugin ships **a patched build per omp minor** (`18.18` for 18.18.x) as a release asset,
`omp-private-relay-web-<minor>.tar`, pinned in [`web-builds.json`](web-builds.json) by two hashes: of the archive, and of
its content. `init` downloads it, checks both before writing a file, refuses anything but plain files with plain names,
and installs it with a staged directory and two renames in `~/.local/share/omp-private-relay/web/current` (a crash between the renames leaves the old copy aside; the next install or `rebuild-web` puts it back). `doctor` recomputes the content hash, so a
damaged or edited copy is noticed.

The patch ([`patches/`](patches)) makes three changes: a link that names the loopback relay connects back to the page's
own address (`wss://` behind Serve, `ws://` on loopback http), close code 4401 shows "this browser is not paired with
the relay" and is final, and omp's third-party analytics script is removed. The build is reproducible: the same omp tag
built with the same Bun gives the same content hash. Licences of the bundled code are in [`NOTICE`](NOTICE).

If your omp is a minor the plugin has no build for, build it yourself from a checkout of omp that has the release tags:

```
omp-private-relay rebuild-web --omp-src ~/src/oh-my-pi --tag v18.19.0
```

It exports the tag with `git archive` (your checkout is only read), applies `patches/collab-web-<minor>.patch`, builds on
omp's embedded Bun through a small `bun` shim, checks that the patch took effect, and installs the result. It needs
network access once for `bun install`, and a patch for that minor; if upstream changed the three files, the patch must be
rebased.

## The web client's directory is checked

The client is served from `web.dir` (default under `~/.local/share/omp-private-relay`). The path is walked the way the kernel
resolves it, component by component, following symbolic links (where a link leads is walked too) and `..`. Every directory
passed through or led into must belong to you or root, must not be writable by other accounts (a sticky directory such as
`/tmp` is the exception), and must not be writable by a group unless that group is shown to have no member but you (and root):
the primary members from `/etc/passwd` and the listed members from `/etc/group` are all checked, a group the files do not
describe is "not private", and **on macOS a group-writable directory is never accepted** (`admin`, `staff` and the like have
members the files do not list). Every link on the way must belong to you or root. Access control lists are inspected too
(`ls -le` on macOS; on Linux `getfacl`, which must be installed when `ls` shows that a directory has an ACL): an entry that lets
another principal write is refused, deny entries are harmless, and an ACL that cannot be inspected is not trusted.

Otherwise the relay answers 503 for the client with a fixed message (it does not open anything in the rejected tree) and
`doctor` fails (`web-dir-safe`): an account that could rename a directory on that path could swap in a client that reads the
room links you open. The approval is cached for five seconds; the relay serves from the directory the walk resolved and
re-checks that it is still the same directory on every request. `install` and `rebuild-web` refuse to write into an unsafe
parent. Directories the plugin creates are always mode 700, whatever your umask. If `doctor` reports a group-writable ancestor
(a common case is a umask of 002 on a home directory), run `chmod g-w` on it.

## What it keeps on disk

| Where | What |
|---|---|
| `~/.config/omp-private-relay/config.json` | Settings, no secrets (`600`) |
| `~/.config/omp-private-relay/pairing/` (`700`) | `devices.json` (hashes of paired browsers' cookies), `pending.json` (hashes of unused pairing tokens), `.lock` (`600`) |
| `~/.config/omp-private-relay/check/check.key` (`600`, directory `700`) | A random key, replaced at every start |
| `~/.local/share/omp-private-relay/web/current/` | The web client and a build record |
| The relay's output | JSON lines of connection metadata. See [`docs/threat-model.md`](docs/threat-model.md) |

Uninstall and purge:

```
omp plugin uninstall omp-private-relay@omp-tether
omp config reset collab.relayUrl && omp config reset collab.webUrl
rm -r ~/.config/omp-private-relay ~/.local/share/omp-private-relay
rm -f ~/.config/omp-services/services.d/omp-private-relay.json ~/.config/omp-services/health.d/omp-private-relay.json
```

Uninstalling the plugin leaves your configuration, paired browsers and web client in place; `omp-services` units, if you
installed them, are removed with `omp-services uninstall`.

## Who this protects against

The design assumes **one person, one machine, one account**: you, and root, are trusted. It is meant to stop:

- a web page you visit (DNS rebinding, a cross-site request, a hostile script) from reaching the relay or your sessions;
- another device on your network, or on your tailnet, that is not you;
- another account on the same machine from joining a room without a room link and a paired browser, or from handing out a
  pairing link of yours (`pair` refuses to print one unless the port proves to be this relay);
- a lost or stolen browser, which you revoke;
- anyone who finds the relay's logs or store files (neither holds a cookie, token, key or link).

It does **not** stop a process that runs as you: it can read your pairing store and your sessions anyway. And it does not
make a machine that several people share safe: the listeners are ordinary TCP ports on loopback, so any other account on
the machine can connect to them. The host listener will then let them open rooms with random ids (squatting and
denial of service, capped at `limits.maxRooms`, never content: the traffic is end-to-end encrypted); everything on the
guest listener needs a paired browser. Two things follow from the listeners being ordinary loopback ports. Any local
account can read the counts on `/stats` (rooms, guests: no names, paths or room ids). And the pairing cookie of the `token`
adapter is sent by a browser to **every port** of `127.0.0.1` (cookies are not port-scoped), so another local service you
visit in that browser, such as a dev server or another account's process on another port, receives it, and a local program
can replay it with forged `Host` and `Origin` headers; revoke the browser if you suspect that. The `tailscale-serve`
adapter's owner header is a plain header on a loopback port, so a local program can forge it too: the pairing cookie is
the credential, the header only narrows who can ask. The one serious case is the **port takeover while the relay is
down**: `pair` and `doctor` check the listener proof (`relay-check/1`), but nothing makes a browser check before it opens a
session link, so a program that took the port can serve a page to a browser that opens a link before the proof is asked.
A browser page that is already open can still be talking to
whatever holds the port. Read [`docs/threat-model.md`](docs/threat-model.md) before you use it on a machine you share.

## Troubleshooting

- `doctor` says the web client is missing: run `init` (or `rebuild-web` for an omp minor without a build).
- A browser shows "browser not paired": run `omp-private-relay pair` and open the link in **that** browser.
- Every page answers 503 (tailscale adapter): the relay does not know its owner yet. Check `tailscale status`, or set
  `auth.tailscale.owner` and `publicHost`.
- A session says it still uses the public relay: it was started before you set `collab.relayUrl`. Run `/collab stop`,
  then `/collab`, in it.
- "port in use": choose other ports in the config file (a free pair is found by `init`).
- The relay logs `pairing_store_refused`: the pairing directory is a link, someone else's, or readable by others
  (`doctor` says which).
- It will not start on an old omp: the embedded Bun must be 1.4.4 or newer (omp 18.18.0 embeds 1.4.4).

## Develop

Needs Bun 1.4.4 or newer for the tests, Python 3.10 or newer and ruff for the repository checks, and omp for the
integration checks. From the repository root:

```
make test-relay      # unit suite for this plugin: real relay processes on free ports, fake omp, no network
make test-conformance
make check           # everything: lint, all plugins, gate, public-tree check
```

Layout: `src/` (the relay, the adapters in `src/auth/`, the command line), `schemas/` (the config schema), `patches/`
(the web client patch), `scripts/` (the web build and release helpers), `tests/unit`, `tests/conformance` (the
pairing-store suite, usable against any implementation; it is in the repository, not in the installed plugin).

Before publishing the web client, `make release-check-relay WEB_OMP_SRC=<checkout> OMP_TAG=v18.18.3 ASSET=<archive>
SERVICES_SCHEMAS=<omp-services schemas dir>` rebuilds from the tag and compares it with the pin, installs nothing, and runs the
whole suite with the archive and omp-services' schemas required. It prints the versions and the archive hash it checked. The
browser check against real omp is manual but scripted: `scripts/browser-check.py --asset <archive>` starts a scratch HOME on
random ports with an omp session hosting through the relay, prints the pairing and session links and takes `restart`,
`revoke-all`, `pair` and `stop` from a control file. [docs/release-evidence.md](docs/release-evidence.md) lists the scenarios
to drive a real browser through and records what was run for the archive that is released.

Releasing the web client for an omp release, on a machine with that omp and a checkout with its tag:

```
scripts/release-web.sh --omp-src ~/src/oh-my-pi --tag v18.18.0 --omp-bin "$(command -v omp)" \
  --release-tag omp-private-relay-v0.1.0 --out /tmp/relay-release
```

builds, packs, pins the hashes in `web-builds.json` and writes `SHA256SUMS`. Publishing the asset is a separate, manual
step. Before a release, `scripts/verify-web.sh` (or `make verify-web WEB_OMP_SRC=<checkout> OMP_TAG=<tag>`) rebuilds from the tag and compares the archive and content hashes with the pin; editing `NOTICE` changes the archive, so it needs `release-web.sh` again (`noticeSha256` in `web-builds.json` pins it separately, and a test fails if they differ).

## Licence

MIT, see [`LICENSE`](LICENSE). The web client is a modified build of omp's collab-web (MIT); its notices are in
[`NOTICE`](NOTICE). Security reports: see [`SECURITY.md`](SECURITY.md).
