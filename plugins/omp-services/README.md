# omp-services

The set-up and operations base for [omp](https://github.com/can1357/oh-my-pi) plugins that need something to keep
running in the background. omp plugins cannot install a daemon, a command or a hook; `omp-services` is the one step you
run to get those, safely:

- **`omp-services init`** shows a plan, asks, and then creates launchd (macOS) or `systemd --user` (Linux) units for the
  services other plugins declare, a private copy of its own code, and small wrapper scripts that pin the absolute path of
  your omp.
- **`omp-services doctor`** looks at everything and says what is wrong in words: unreachable, stale, not on the relay,
  wrong file modes, a stopped unit, a newer version available. It changes nothing.
- **A heartbeat** that runs the health checks other plugins contribute and pings a dead-man URL of your choosing
  (healthchecks.io style): `<URL>` when everything is fine, `<URL>/fail` when it is not. Off until you give it a URL.
- **`omp-services uninstall`** (keeps your data) and **`purge`** (asks separately).
- **`/omp-services doctor`** and **`/omp-services setup`** inside an omp session.

Supported: macOS and Linux, omp 18.18.0 or newer (tested on 18.18.0; `doctor` says when your version is not one of the
tested ones), Python 3.10 or newer. Not Windows. **No third-party Python packages.**

## What it never does

- **Nothing is installed or started until you say yes** to a plan that lists every file and unit. `init --plan` shows the
  plan and stops. Without a terminal and without `--yes` it changes nothing.
- **No network until you configure it.** What can contact a network: the heartbeat ping to the URL you store; `url` and
  `port` health checks that a plugin's drop-in declares (loopback only unless you set `health.allowRemoteUrls`; `doctor`
  runs them too); and, on request, `doctor --refresh-catalog`, which makes *omp* refetch its marketplace catalog.
- **It does not touch units that are not its own.** Every unit it manages starts with your label prefix (default
  `omp-tether.`) and carries its "Managed by omp-services" comment (or is byte for byte what it recorded); the unit file
  is derived from the label, never from a path in a file, and ownership is checked again at the moment it acts. A unit
  with the same label that it did not write is reported and left alone.
- **It never restarts a unit you flagged `protected`**: not `init`, not a watchdog. Only you, by naming
  it (`init --restart-protected <plugin>.<name>`). The flag is remembered from what was installed, so a later declaration
  that drops it does not lift the protection.
- **It never deletes a folder it did not create.** The marker that `purge` requires is written in the step that creates a
  directory, never into one that already existed; such a directory is left unmarked. `init` records the entries it creates
  inside it (in `.omp-services-entries`), and `purge` deletes exactly those, never the directory itself unless that leaves it
  empty, and never a drop-in or anything else that is there (for example `~/.config/omp-services/health.d/*.json` that another plugin wrote).
- **It does not put secrets in logs, in the config file or in command lines.** The heartbeat URL is stored in a `0600`
  file in a `0700` directory, read from standard input when you set it, and never printed.

## Install

Install the plugin (this adds the `/omp-services` command and the command line; it changes nothing else):

```
omp plugin marketplace add saadsoft1993/omp-tether
omp plugin install omp-services@omp-tether
```

Restart omp sessions that were already running: omp loads a new extension in new sessions only. Then set it up, either
inside a session with `/omp-services setup` (it shows the plan and asks), or from a terminal with this one-liner, which
finds omp and Python, installs the plugin if it is missing (after asking), and runs `init`. Download a **release tag**
(easier to audit than `main`; a tag can still be moved), compare it with the SHA-256 printed in that release's notes (this catches a corrupted download; it does not prove who published the script, because the notes come from the same place), then run it:

```
curl -fsSLo install.sh https://raw.githubusercontent.com/saadsoft1993/omp-tether/omp-services-v0.1.0/plugins/omp-services/install.sh
echo "<sha256 from the release notes>  install.sh" | shasum -a 256 -c
sh install.sh
```

This verifies **the installer**. The plugin it installs comes from the marketplace source, which follows the repository's
default branch; it is not tied to the installer's commit. To install a plugin tied to a commit you checked, clone the
repository, `git checkout <commit>` (verify it as you see fit), and run the installer with
`OMP_TETHER_SOURCE_DIR=/absolute/path/to/the/checkout`.

[`install.sh`](install.sh) is about 140 lines; read it first if you like. It asks before it installs or changes anything,
refuses an `OMP_TETHER_REPO` that is not plain `owner/name`, and does nothing without a terminal. (The repository also
carries [`install.sh.sha256`](install.sh.sha256) so tests can check it matches; a checksum fetched from the same place as
the script proves nothing against a hijacked repository, which is why it is an integrity check and not a proof of origin.)
Or run the plugin's launcher yourself: `<plugin dir>/bin/omp-services init` (`omp plugin list --json` shows the plugin
dir). `init --plan` shows what would happen without doing it.

After the first `init`, `~/.local/share/omp-services/bin/omp-services` is a wrapper that pins your Python and the
installed code; put that directory on your `PATH` if you want a plain `omp-services` command.

### What `init` creates

| Where | What | Mode |
|---|---|---|
| `~/.config/omp-services/` | `config.json`, `health.d/`, `services.d/`, `secrets/` (your configuration) | dirs `0700`, `config.json` and secrets `0600` |
| `~/.local/share/omp-services/` | `releases/<version>-<digest>/` (a copy of the code), `current` (link to one), `bin/` (wrappers), `state.json` | `0700` |
| `~/.local/state/omp-services/` | `logs/`, `heartbeat-status.json` | `0700` |
| `~/Library/LaunchAgents/<label>.plist` (macOS) or `~/.config/systemd/user/<label>.service` (Linux) | one unit per service | `0644` |

Directories follow `XDG_CONFIG_HOME`, `XDG_DATA_HOME` and `XDG_STATE_HOME` (the defaults above apply on macOS too). Paths
with spaces work. Services keep running from the private code release, not from omp's plugin cache, because
`omp plugin upgrade` deletes the old version's directory.

On Linux, `systemd --user` services stop when you log out unless lingering is on. `init` and `doctor` tell you; the
command is `loginctl enable-linger` and `omp-services` never runs it for you.

## Configuration

`~/.config/omp-services/config.json` (created by `init` with every key at its default; validated against
[`config-1.schema.json`](python/omp_services/schemas/config-1.schema.json)). Environment variables override it.

| Key | Environment variable | Default | Meaning |
|---|---|---|---|
| `labelPrefix` | `OMP_SERVICES_LABEL_PREFIX` | `omp-tether` | Prefix of every label and unit this tool manages |
| `omp.path` | `OMP_SERVICES_OMP_PATH` | found at init, then pinned | Absolute path of omp for services |
| `python.path` | `OMP_SERVICES_PYTHON` | the Python running `init` | Absolute path of the Python 3.10+ for services |
| `heartbeat.enabled` | `OMP_SERVICES_HEARTBEAT_ENABLED` | `false` | Install the heartbeat unit |
| `heartbeat.allowNoChecks` | `OMP_SERVICES_HEARTBEAT_ALLOW_NO_CHECKS` | `false` | With no health checks at all, send the ping (`true`) instead of `<URL>/fail` |
| `heartbeat.intervalSeconds` | `OMP_SERVICES_HEARTBEAT_INTERVAL_SECONDS` | `300` | 30 to 86 400 |
| `heartbeat.urlFile` | `OMP_SERVICES_HEARTBEAT_URL_FILE` | `<config>/secrets/heartbeat-url` | Where the `0600` URL file is |
| `heartbeat.pingTimeoutSeconds`, `checkTimeoutSeconds`, `cycleTimeoutSeconds` | | `15`, `10`, `120` | Deadlines |
| `health.allowRemoteUrls` | `OMP_SERVICES_ALLOW_REMOTE_URLS` | `false` | Let `url` and `port` checks reach hosts other than loopback |
| `tools.tailscale` | `OMP_SERVICES_TAILSCALE` | found on `PATH` | The tailscale CLI, for `serve-route` checks |
| `catalog.marketplace` | `OMP_SERVICES_MARKETPLACE` | `omp-tether` | Marketplace to compare versions against |
| `logs.maxBytes` | | `1048576` | The heartbeat trims a service log above this |

Other variables: `OMP_SERVICES_CONFIG_DIR`, `OMP_SERVICES_DATA_DIR`, `OMP_SERVICES_STATE_DIR` and
`OMP_SERVICES_UNIT_DIR` move the directories (for a scratch run), `OMP_SERVICES_PLATFORM=darwin|linux` makes `render`
produce the other platform's units. `omp-services config validate` checks your file and variables. A moved
`OMP_SERVICES_UNIT_DIR` is for tests and previews: on Linux `systemctl --user` only loads units found in its own search
path, so use the default directory for a real setup.

## The heartbeat

A dead-man switch: a service that tells an outside check "I am alive and well" every few minutes, so that you hear about a
silent machine (off, asleep, offline) and about checks that fail. Use any service that accepts a `GET` on a secret URL and
a `/fail` suffix (healthchecks.io, many self-hosted ones). It is a third party: the URL is a bearer secret.

```
omp-services heartbeat set-url        # type or paste the https:// URL; it is not echoed and not kept in shell history
omp-services init                     # installs and starts the heartbeat unit
omp-services heartbeat status         # what the last round saw
```

Every interval it runs every check from `health.d/*.json`, one after the other, then requests `<URL>` (all fine) or
`<URL>/fail` (something counts). **With no health checks at all it sends `<URL>/fail`**, so a plugin whose drop-in never
arrived (or a typo in a file name) is not a green light; set `heartbeat.allowNoChecks` to `true` if the heartbeat is only
meant to say "this machine is alive". `doctor` warns about it. The request is a `GET` with no body, TLS is verified, a redirect is never followed and
counts as a failed ping, and the URL is never logged. `omp-services heartbeat clear-url` removes the URL and disables it again.

### Checks other plugins contribute

A plugin writes `~/.config/omp-services/health.d/<plugin>.json` ([protocol `health/1`](../../docs/protocol/health-1.md)):

```json
{
  "protocol": "health/1",
  "plugin": "omp-example",
  "checks": [
    { "id": "api",   "type": "url",       "url": "http://127.0.0.1:8123/health" },
    { "id": "owner", "type": "uid-owner", "port": 8123 },
    { "id": "route", "type": "serve-route", "port": 8443, "target": "http://127.0.0.1:8123" }
  ]
}
```

Types: `url`, `port`, `uid-owner`, `serve-route`, `command` (absolute path, no shell, hard deadline), `file-age`, and
`watchdog` (a service must be running; bounded restarts; never for a `protected` service). A failure is named by kind: **unreachable** (nothing answered),
**stale** (answered, but too old), **not-on-relay** (works locally, the Tailscale Serve route is missing), **failed**,
**invalid**. Drop-ins that others can write are refused, and an unusable drop-in counts as a failing check instead of
being skipped. The refusal is about *other accounts*: a plugin you installed runs as you and can write a `command` check
or a service that runs a program of its choosing. `doctor` therefore lists, for every drop-in, the programs it makes this
machine run. Try a drop-in without installing anything: `omp-services health --file path/to/plugin.json` (it only
reports; it never restarts anything).

### Services other plugins ask for

`~/.config/omp-services/services.d/<plugin>.json` ([protocol `services/1`](../../docs/protocol/services-1.md)) declares a
long-running program (an omp script on omp's own Bun, a Python script, or an executable). `init` renders it, shows the
unit, and loads it after your yes.

**Reload only what changed.** A unit that is loaded and whose rendered text is unchanged is left running. A changed unit is
reloaded, only that one. A service flagged `"protected": true` (a program whose restart would destroy something you are in
the middle of) is **never** reloaded, restarted or removed unless you name it:
`omp-services init --restart-protected <plugin>.<name>`. `uninstall` leaves protected units, and the wrappers, code
release and state they still need, in place; add `--include-protected` to remove them too.

## Commands

| Command | Does |
|---|---|
| `init [--plan [--json]] [--yes [--expect-plan DIGEST]] [--repin] [--restart-protected NAME]` | Shows the plan; applies it after a yes. Safe to repeat. `--expect-plan` applies only a plan with exactly the digest that was shown (the extension uses it) |
| `doctor [--json] [--no-health] [--no-catalog] [--refresh-catalog]` | Reports; exit status 1 when anything fails |
| `uninstall [--plan] [--yes] [--include-protected]` | Removes units, wrappers, code releases, state; **keeps** config, drop-ins, secrets, logs |
| `purge [--plan] [--confirm-purge] [--include-protected]` | Everything above, then deletes the config, data and state directories. `--yes` is not enough: type `purge` or pass `--confirm-purge`. A directory it created is deleted whole; one that already existed (another plugin's `dropins --write` made `~/.config/omp-services`) loses only what `init` recorded creating in it. Refuses a directory with neither its marker file nor such a record, a link, or your home |
| `heartbeat run [--once] [--no-ping] \| set-url \| clear-url \| status` | The service loop, or its settings |
| `health --file FILE [--json]` | Runs one health drop-in once, report only |
| `config path \| show \| validate` | The configuration |
| `render [--platform launchd\|systemd] [--out DIR]` | Prints the units a setup would install, for either platform |

### Updating

There is no `upgrade` command: omp already has one, and omp-services does not wrap it.

1. `omp plugin upgrade omp-services@omp-tether` (omp's own command; it downloads the new plugin code from the marketplace
   source).
2. `omp-services doctor`: it reports **"newer version available"** (installed versus omp's cached catalog; add
   `--refresh-catalog` to make omp refetch it first) and every other drift, such as a unit whose file changed.
3. Run `init` from the upgraded plugin, not from the old wrapper: `<plugin dir>/bin/omp-services init` (`omp plugin list
   --json` shows the plugin dir) or `/omp-services setup` in an omp session. It shows a plan; after a yes it installs the
   new code as a new release, moves the `current` link and re-renders the units. Only units whose file changed are
   reloaded; a unit you flagged `protected` is never touched.

A running service keeps running the code it started with: the heartbeat, for one, picks up a new release at its next
restart (`launchctl kickstart -k gui/$(id -u)/<label>` or `systemctl --user restart <label>`). There is no automatic
rollback; the previous releases stay in `releases/` until the plan lists them for pruning.

## Troubleshooting

- `doctor` says **has not been set up**: run `omp-services init`.
- **needs Python 3.10 or newer**: macOS's `/usr/bin/python3` is 3.9. Install a newer Python (Homebrew, uv, python.org) or
  set `OMP_SERVICES_PYTHON`.
- A unit is **loaded but not running**: read its log in `~/.local/state/omp-services/logs/<plugin>.<name>.log`.
- **The launchd domain is not available**: LaunchAgents need a logged-in desktop session for your user; an ssh session
  alone is not enough.
- **`systemctl --user` does not answer**: log in normally (not `su`), or set `XDG_RUNTIME_DIR`.
- Units use your label prefix; `omp-services doctor` lists those that exist but are no longer wanted.
- Service logs are trimmed by the heartbeat each round and by every `init`; with the heartbeat off and no `init`, a log
  grows until you run `omp-services init` (`doctor` warns when one has outgrown its bound). A service that crashes keeps
  being restarted by the service manager.
- `PATH` for services includes `/usr/local/bin`, which on some Macs a non-admin can write to; put what a service needs in
  an absolute path instead.

## Uninstall and purge

```
omp-services uninstall        # units, wrappers, code releases, state. Your data stays. (This removes the wrapper too.)
# or, to remove your data as well, run this INSTEAD of `uninstall` (it does everything `uninstall` does, then deletes
# the directories omp-services created):
omp-services purge
# then, either way:
omp plugin uninstall omp-services@omp-tether
```

`purge` needs the wrapper, so run it before the plugin is uninstalled (if the wrapper is already gone, use the plugin's own
launcher: `<plugin dir>/bin/omp-services purge`). `purge` deletes a directory omp-services itself created whole. A directory
that already existed (a plugin may have written a drop-in into `~/.config/omp-services` before `init` ran) is not deleted:
`purge` removes from it only the entries `init` recorded creating there (`config.json`, `secrets/`), and leaves the directory,
the drop-ins and everything else; remove those by hand. If `init` was never run, there is no record and `purge` refuses that directory.

## Develop

Needs Python 3.10+, Bun 1.4.4+, and for the repository checks ruff and shellcheck.

```
make test-services    # extension unit tests, Python suite, health/1 conformance suite and its self-test
make check            # everything, including the load gate against a real omp
```

Layout: `src/omp-services.ts` (the extension), `python/omp_services/` (the command line, standard library only),
`bin/omp-services` (finds a Python 3.10+ and runs it), `install.sh`, `tests/python`, `tests/unit`, `tests/conformance`
(the health/1 suite, runnable against any implementation).

## Licence

MIT. See [`LICENSE`](LICENSE). Security reports: see [`SECURITY.md`](SECURITY.md); the model is in
[`docs/threat-model.md`](docs/threat-model.md).
