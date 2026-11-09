# omp-dashboard

A local dashboard for **your own** [omp](https://github.com/can1357/oh-my-pi) sessions. It lists the sessions running on a machine,
opens one in your browser through your own [private relay](../omp-private-relay), stops its running turn, renames it, ends it, and
starts new ones, from a phone or another computer as well as from the machine itself. Every browser must be paired first.

- **Nothing happens until you run it.** Installing the plugin starts nothing, opens no port and writes no file.
- **Loopback only.** The hub speaks plain HTTP on `127.0.0.1` (or `::1`) and refuses to start on any other address. Other devices
  reach it through a tunnel you set up (an SSH forward, or `tailscale serve`).
- **Every browser is paired** with a one-time link ([`pairing-store/1`](../../docs/protocol/pairing-store-1.md)); you can list and
  revoke browsers. [`omp-private-relay`](../omp-private-relay) reads the same store, so one pairing opens both.
- **Safe defaults.** Start is `off`, no folder is offered, alerts are off until you set a topic, no relay is assumed, pairings
  expire after 90 days. Nothing is started, edited or sent without a click or a command of yours.
- **No accounts, no analytics, no third-party assets.** The page is served from this plugin with a strict Content-Security-Policy.

Supported: macOS and Linux, omp 18.18.x (tested on 18.18.3), Python 3.10 or newer (standard library only), one trusted user account.
Not supported: Windows; machines several people share (see [Who this protects against](#who-this-protects-against)).

## Install and run

```
omp plugin marketplace add saadsoft1993/omp-tether
omp plugin install omp-dashboard@omp-tether
ln -s ~/.omp/plugins/node_modules/omp-dashboard/bin/omp-dashboard ~/.local/bin/omp-dashboard   # optional
omp-dashboard run          # keep this terminal open (or use tmux/screen), or see "Keep it running"
omp-dashboard pair         # in another terminal: prints a link that pairs one browser
```

`bin/omp-dashboard` is a short shell script that finds a Python 3.10 or newer (macOS's own `/usr/bin/python3` is 3.9 and is not
enough; set `OMP_DASHBOARD_PYTHON` to an absolute path to force one). Open the printed link in the browser you want to pair: the page
removes the token from the address bar (it is in the fragment, which a browser never sends), redeems it, and the browser receives a
cookie. The link works **once**, for **5 minutes**; do not paste it anywhere, and run `pair` in a terminal, not inside an omp session.
`pair` refuses (and creates no token) unless the hub's port is held by your own account.

```
omp-dashboard devices       # id, date and label of each paired browser
omp-dashboard revoke <id>   # unpair one (4 or more characters of the id); `revoke all` unpairs every browser
omp-dashboard doctor        # what is wrong, and the command that fixes it; changes nothing
omp-dashboard config path | show | validate | init | set-ntfy-topic [--stdin] [--replace] | clear-ntfy-topic
omp-dashboard dropins [--write]   # the service and health drop-ins for omp-services
```

## What it needs from other plugins (all optional)

| Feature | Needs |
|---|---|
| Stop and Rename | [`omp-session-control`](../omp-session-control), turned on, in sessions that started after it was installed. Kill needs nothing. |
| Open and Watch | [`omp-private-relay`](../omp-private-relay) running, `relay.enabled` set, and its pairing in `shared` mode on this plugin's pairing directory and cookie name |
| Start a session | `start.backend` set to `herdr` or `tmux`, and `start.roots` listing the folders it may use |
| Keep it running | [`omp-services`](../omp-services): `omp-dashboard dropins --write`, then `omp-services init` |
| Phone alerts | a ntfy topic (`omp-dashboard config set-ntfy-topic`) |

Without the first two, the Sessions page still lists sessions; the buttons that cannot work say why. A link is only ever handed out
for a session that is on **your private relay** (`omp collab link` is asked for the room's view link first, to tell): a session
still on omp's public relay is never opened from here, and each link is fetched at click time and proven (`relay-check/1`) before it
is returned. `omp config set collab.relayUrl ws://127.0.0.1:<relay.hostPort>` and `collab.webUrl <relay web address>` point omp at the
relay; `doctor` checks both.

## Sign-in: who may even ask

`auth.adapter` chooses, and the pairing cookie is checked after it, always.

- **`loopback-only`** (default): the request must carry a loopback `Host` for this hub's port (or one in `auth.loopbackOnly.allowedHosts`,
  for an SSH forward on another local port) and no sign of a proxy (`Tailscale-*`, `X-Forwarded-*`, `Forwarded`). Reach it from another
  machine with `ssh -L <port>:127.0.0.1:<port> you@machine` (same port number on both ends).
- **`tailscale-serve`**: expose the hub on your tailnet only, never Funnel: `tailscale serve --bg --https=<servePort> http://127.0.0.1:<listen.port>`.
  Requests must carry your Tailscale login (set by Serve), this machine's name as `Host`, and the matching `Origin`; Funnel requests and
  tagged devices are refused; until the owner is known every request gets 503. The owner and name are read from `tailscale status --json`
  every minute, or fixed with `auth.tailscale.owner` and `publicHost`. A request that carries `Host`, `Tailscale-User-Login` or `Tailscale-Funnel-Request` twice is refused. That Serve replaces a login the caller sent was **not** tested against a real Serve (a stand-in command only); the pairing cookie is what still stands if it does not.

The pairing cookie is not scoped to a port: a browser sends it to every service on the same host name, so on `127.0.0.1` another local web server sees
it, and a hub on another port receives it. Over `tailscale-serve` the cookie is `Secure`. A `__Host-` cookie name is not available (the name is limited to letters, digits and `_`).
When `omp-private-relay` runs in shared mode it reads the same store and applies no age limit of its own; `pairing.maxAgeDays` is enforced by this hub.

## Keep it running

With omp-services: `omp-dashboard dropins` shows, and `--write` writes, `services.d/omp-dashboard.json` (a `services/1` description:
`python .../omp_dashboard_main.py run`, pinned to this configuration file) and `health.d/omp-dashboard.json` (`health/1`: `/healthz`
freshness, the port, who owns it, a watchdog, and the Serve route with tailscale-serve). Then `omp-services init` shows its plan and asks.

Without it, run `omp-dashboard run` yourself, or write a unit. A minimal `~/.config/systemd/user/omp-dashboard.service`:

```
[Unit]
Description=omp dashboard hub

[Service]
ExecStart=/absolute/path/to/omp-dashboard run
Environment=PATH=/usr/local/bin:/usr/bin:/bin
Restart=always
UMask=0077
NoNewPrivileges=yes

[Install]
WantedBy=default.target
```

A service starts with a small `PATH`: set `omp.bin`, `start.herdr.bin`, `start.tmux.bin`, `auth.tailscale.bin` as absolute paths if they
are not in `/usr/local/bin`, `/usr/bin`, `~/.local/bin` or `/opt/homebrew/bin`. Never set `BUN_BE_BUN` for the hub: it makes omp behave
as a plain Bun (the hub removes it from the environment of the omp it runs).

## Configuration

`~/.config/omp-dashboard/config.json` (or `$XDG_CONFIG_HOME/omp-dashboard/config.json`; another file with `OMP_DASHBOARD_CONFIG`, an
absolute path). Every key is optional except `schemaVersion`. A file that is not valid, has an unknown key or a wrong type, is writable
by group or others, or sits below a directory others can replace is **refused with every problem listed** (the defaults are never mixed
with a half-read file). The JSON Schema is [`python/omp_dashboard/schemas/config-1.schema.json`](python/omp_dashboard/schemas/config-1.schema.json).
Secrets are never in this file. Environment variables beat the file, which beats the defaults; a service does not see variables you set in
your shell (`dropins --write` refuses while any are set). Changes need a restart of the hub.

| Key | Environment variable | Default | Meaning |
|---|---|---|---|
| `schemaVersion` | | `1` | Required in the file |
| `listen.host` |  | `"127.0.0.1"` | The address the hub binds. Loopback only: the hub speaks plain HTTP and refuses to start on any other address. To reach it from another device, put tailscale serve or an SSH forward in front of it. |
| `listen.port` | `OMP_DASHBOARD_PORT` | `17795` | The port the hub listens on. |
| `auth.adapter` | `OMP_DASHBOARD_AUTH` | `"loopback-only"` | Who may even ask. loopback-only: the pairing cookie alone, on a loopback Host. tailscale-serve: requests must come through Tailscale Serve and name this machine's owner, besides the cookie. |
| `auth.loopbackOnly.allowedHosts` |  | `[]` | Extra host:port values the browser may use, for an SSH forward on another local port. |
| `auth.tailscale.bin` | `OMP_DASHBOARD_TAILSCALE_BIN` | `null` | Absolute path of the tailscale command. null: found on PATH, then the macOS app bundle. |
| `auth.tailscale.servePort` | `OMP_DASHBOARD_TAILSCALE_SERVE_PORT` | `null` | The https port of `tailscale serve` that forwards to the hub. null: the same number as listen.port. |
| `auth.tailscale.owner` | `OMP_DASHBOARD_TAILSCALE_OWNER` | `null` | Owner login. null: read from Tailscale. With publicHost, Tailscale is not asked at all. |
| `auth.tailscale.publicHost` | `OMP_DASHBOARD_TAILSCALE_HOST` | `null` | This machine's MagicDNS name. null: read from Tailscale. |
| `pairing.dir` | `OMP_DASHBOARD_PAIRING_DIR` | `null` | The pairing store (pairing-store/1). null: <config dir>/pairing. To let omp-private-relay accept the same browsers, point its pairing.dir here and set its pairing.mode to shared. |
| `pairing.cookieName` | `OMP_DASHBOARD_COOKIE_NAME` | `"omp_dashboard"` | The cookie that holds the browser's session id. omp-private-relay in shared mode must use the same name. |
| `pairing.maxAgeDays` | `OMP_DASHBOARD_PAIRING_MAX_AGE_DAYS` | `90` | A paired browser stops being paired this many days after it was paired (its cookie expires then too); 1 to 3650, or null for never. `omp-private-relay` in shared mode does not apply this limit. |
| `omp.bin` | `OMP_DASHBOARD_OMP_BIN` | `null` | Absolute path of omp. null: OMP_BIN, then PATH. |
| `control.dir` | `OMP_DASHBOARD_CONTROL_DIR` | `null` | omp-session-control's control directory (session-control/1). null: OMP_SESSION_CONTROL_DIR, then ~/.config/omp-session-control. Stop and Rename need it. |
| `relay.enabled` | `OMP_DASHBOARD_RELAY_ENABLED` | `false` | Offer Open and Watch through omp-private-relay. Off: sessions are listed, and no link is ever handed out. |
| `relay.hostPort` | `OMP_DASHBOARD_RELAY_HOST_PORT` | `17797` | The relay's host listener (where omp sessions connect). omp's collab.relayUrl must be ws://127.0.0.1:<this>. |
| `relay.guestPort` | `OMP_DASHBOARD_RELAY_GUEST_PORT` | `17796` | The relay's guest listener (where browsers connect). |
| `relay.checkDir` | `OMP_DASHBOARD_RELAY_CHECK_DIR` | `null` | The relay's check directory (relay-check/1). null: <config home>/omp-private-relay/check. |
| `relay.webUrl` | `OMP_DASHBOARD_RELAY_WEB_URL` | `null` | The address a browser uses for the relay's web client, and omp's collab.webUrl. null: http://127.0.0.1:<guestPort> with loopback-only, https://<name>:<servePort> with tailscale-serve. |
| `relay.servePort` | `OMP_DASHBOARD_RELAY_SERVE_PORT` | `null` | With tailscale-serve: the https port of `tailscale serve` for the relay's guest listener. null: guestPort. |
| `start.backend` | `OMP_DASHBOARD_START_BACKEND` | `"off"` | How Start opens a new session: in a herdr tab, in a tmux window, or not at all. |
| `start.roots` | `OMP_DASHBOARD_START_ROOTS` | `[]` | Folders Start may open sessions in: each one, and every git repository below it. Empty: nothing can be started. |
| `start.maxDepth` |  | `3` | How deep below a root to look for git repositories. |
| `start.herdr.bin` | `OMP_DASHBOARD_HERDR_BIN` | `null` | Absolute path of herdr. null: HERDR_BIN, then PATH. |
| `start.tmux.bin` | `OMP_DASHBOARD_TMUX_BIN` | `null` | Absolute path of tmux. null: PATH. |
| `start.tmux.session` | `OMP_DASHBOARD_TMUX_SESSION` | `"omp"` | The tmux session Start adds windows to (created when it does not exist). |
| `projects.rankBySessions` | `OMP_DASHBOARD_RANK_BY_SESSIONS` | `false` | Order the Start list by recent use. Reads only the names and modification times of the folders in omp's session directory, never what is in them. |
| `projects.sessionsDir` | `OMP_DASHBOARD_SESSIONS_DIR` | `null` | omp's session directory. null: $PI_CODING_AGENT_DIR/sessions, else ~/.omp/agent/sessions. |
| `alerts.server` | `OMP_DASHBOARD_NTFY_SERVER` | `"https://ntfy.sh"` | The ntfy server. https only (http only to loopback, for tests). Alerts name the project folder and the model to this server. |
| `alerts.topicFile` | `OMP_DASHBOARD_NTFY_TOPIC_FILE` | `null` | The file that holds the ntfy topic (a bearer secret, 0600). null: <config dir>/secrets/ntfy-topic. Alerts are off while the file does not exist. |
| `alerts.minTurnSeconds` | `OMP_DASHBOARD_ALERT_MIN_TURN_SECONDS` | `60` | A finished turn alerts only when it ran at least this long. |
| `stats.url` | `OMP_DASHBOARD_STATS_URL` | `null` | Where a stock `omp stats` is reachable from your browser. The Usage page links there; the hub never talks to it. null: no link. |
| `audit.file` | `OMP_DASHBOARD_AUDIT_FILE` | `null` | The audit log. null: <state dir>/audit.jsonl. |
| `audit.maxBytes` |  | `1048576` | The log moves to <file>.1 when it passes this size (one previous file is kept). |
| `machine.name` | `OMP_DASHBOARD_MACHINE_NAME` | `null` | The name shown in the banner. null: omp's collab.displayName, else this machine's host name. |

Paths in the config file and in `OMP_DASHBOARD_*` variables are used as written: a path with a `..` component is refused, not collapsed.

Other variables read: `OMP_BIN`, `HERDR_BIN`, `OMP_SESSION_CONTROL_DIR`, `PI_CODING_AGENT_DIR`, `OMP_DASHBOARD_CONFIG`, `OMP_DASHBOARD_PYTHON`
(the launcher), `XDG_CONFIG_HOME`, `XDG_STATE_HOME`.

## Start a session

`start.backend` `herdr` opens a tab labelled `omp` in the project's herdr workspace (created when there is none); `tmux` opens a window in
one tmux session (`start.tmux.session`, created when missing). Only folders from the hub's own list (`start.roots` and the git repositories
below them, to `start.maxDepth`) are accepted, passed as one argument (no shell), and the **only text typed into the new pane is `omp`**.
tmux 3.4 was exercised (a real server); herdr was not available and its backend is tested against a stand-in command only. In tmux a `#` in a folder name is doubled so a folder called `a#(x)` is that folder, and a path with a `;` is refused. The Start list is in alphabetical order unless you set
`projects.rankBySessions`, which reads the names and modification times (never the contents) of omp's session folders.

## Phone alerts (ntfy)

Off until `omp-dashboard config set-ntfy-topic` writes a topic (a random one of 16 or more characters, or your own on standard input with `--stdin`; never an argument, which `ps` and shell history would show). The topic is a **bearer secret**: anyone who knows it reads your alerts and
can send you messages. It lives in a `0600` file in a `0700` directory (`alerts.topicFile`), is printed once when made, and is never shown on
the page or written to a log. Messages go to `alerts.server` (default `https://ntfy.sh`, a **third-party service**; `https` only) and name the
project folder, the model and the state: no transcript, no session title, no link. While no topic is set the hub does not poll omp for alerts.

## Usage (stats)

`stats.url` makes the page link to a stock `omp stats` you run (`omp stats --host 127.0.0.1 --port <n>`); the hub never talks to it, and the link
is shown only for an `http(s)` address. There is no stats proxy: the hub shows no usage numbers itself.

## What it keeps on disk

| Where | What |
|---|---|
| `~/.config/omp-dashboard/config.json` | Settings, no secrets |
| `~/.config/omp-dashboard/pairing/` (`700`) | `devices.json` (hashes of paired browsers' cookies), `pending.json` (hashes of unused tokens), `.lock` |
| `~/.config/omp-dashboard/secrets/` (`700`) | `ntfy-topic` (`600`) |
| `~/.local/state/omp-dashboard/audit.jsonl` (+ `.1`) | One line per pair (and refused or rate-limited pairing), link (and refused link), start, stop, rename, kill, force-kill, alerts test, and the command line's `pair`/`revoke`: time, browser id, action, session or instance id, outcome. Never names, prompts, paths or links |

Uninstall and purge: `omp plugin uninstall omp-dashboard@omp-tether`, then `rm -r ~/.config/omp-dashboard ~/.local/state/omp-dashboard` and
`rm -f ~/.config/omp-services/*.d/omp-dashboard.json` (and `omp-services uninstall` for a unit you installed). Uninstalling keeps your data.

## The page

The dashboard page is built from [`ui/`](ui) and **committed** as [`ui-dist/`](ui-dist) with [`ui-build.json`](ui-build.json), which pins the
SHA-256 of every file. A marketplace install copies the repository tree only, so a committed page works with no network and no extra step.
The hub reads the files once at start, refuses the whole page unless every hash matches and nothing else is in the directory, walks the
directory like every other trusted path, and serves from memory. `make verify-ui` rebuilds from the sources with the Bun version recorded in
the pin and compares. The pin lives in the same tree as the files, so it catches damage and mixed-up builds, not someone who can already write the install directory (out of scope, see the threat model). Sources derived from omp's stats package are listed in [`NOTICE`](NOTICE).

## Who this protects against

One person, one machine, one account: you, and root, are trusted. It is meant to stop a web page you visit (DNS rebinding, a cross-site request),
another device on your network or tailnet, another account that tries to get a pairing link or a session link out of you (`pair` and links check that
the port is yours), a lost browser (revoke it), and anyone who finds the audit log or the store files (neither holds a cookie, token or link). It does
**not** stop a process running as you, and it does not make a machine several people share safe: see [`docs/threat-model.md`](docs/threat-model.md).

## Develop

```
make test-dashboard              # Python suite: scratch HOME, free ports, fake omp, no network; needs Bun for the cross-implementation tests
make test-dashboard-conformance  # the pairing-store/1 suite (kept with omp-private-relay) against this plugin's reader and writer
make ui-install                  # once, with network: the page's build packages (react, lucide-react, TypeScript)
make test-ui typecheck-ui verify-ui
make test-integration            # includes the hub against two live omp sessions (needs omp and Bun; about a minute)
make test-dashboard-mutation     # switches each listed guard off in a copy; a test must fail
make build-ui                    # rebuild ui-dist and ui-build.json (use Bun 1.4.4, the version recorded in the pin)
```

What was run for the current release candidate is in [`docs/release-evidence.md`](docs/release-evidence.md). The HTTP API is documented in [`docs/api.md`](docs/api.md).

## Licence

MIT, see [`LICENSE`](LICENSE); the page is partly derived from omp (MIT), see [`NOTICE`](NOTICE). Security reports: [`SECURITY.md`](SECURITY.md).
