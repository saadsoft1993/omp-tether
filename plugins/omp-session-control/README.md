# omp-session-control

An [omp](https://github.com/can1357/oh-my-pi) extension that lets a program on your own machine **stop the running turn**
of an interactive omp session, **rename** it, **answer its plan review** and **run session commands** (compact, switch the model or the thinking level, change plan, goal and loop modes), over a private Unix socket. It is the session-side half of a local
dashboard: the dashboard lists your sessions and gets a Stop and a Rename button; this extension does the stopping and
renaming inside the session, from 0.2.0 shows a pending plan review and answers it, and from 0.3.0 reads and changes the model, thinking level and modes and compacts the context. Each of those commands needs its omp API (`ctx.compact`, `pi.setModel`, `pi.setThinkingLevel`, `ctx.sessionModes`, all in 18.19.0) and is offered only when it exists; `compact` answers when the compaction has finished (up to 180 s) and never aborts a running turn.

- **Inert until you turn it on.** Installing it changes nothing. It creates no files and opens no socket until you
  enable it (below). It registers one slash command, `/session-control`, and, when the control directory already exists,
  watches that directory for the marker files (and looks again every 2 s, a few `lstat` calls, only while the
  directory exists), but it never listens before you enable it.
- **Same user, same machine.** The socket lives in a `0700` directory in your home. Any process that runs as you can
  use it, and can already do far worse to your session. Read [the same-user caveat](#the-same-user-caveat) before you
  enable it on a machine you share.
- **No dependencies, one file.** `src/omp-session-control.ts` imports only Node built-ins. It runs inside every
  interactive session, so it is kept small enough to read in one sitting.
- **A documented protocol.** [`docs/protocol/control-1.md`](../../docs/protocol/control-1.md) specifies it, and a
  conformance suite checks any implementation against it, so you can write your own client.

Supported: macOS and Linux, with omp 18.18.0 or newer (tested on 18.18.0). Plan review needs an omp that has `ctx.getPendingPlanReview` and `ctx.resolvePlanReview` (18.19.0 does); on an older omp the other operations work and the plan operations are simply not offered. Not supported: Windows (omp uses named pipes
there; this extension uses Unix sockets).

## Install

```
omp plugin marketplace add saadsoft1993/omp-tether
omp plugin install omp-session-control@omp-tether
```

Or from a checkout: `omp plugin marketplace add /path/to/omp-tether`, then the same `install` line.

Installing a plugin takes effect in **new** sessions only. Restart sessions that are already running; `/reload-plugins`
does not load a new extension.

## Turn it on

Control is on only when the control directory exists, is private, and contains a file called `enabled`. Any of these
does it:

- In an omp session, run `/session-control on`. It creates the directory (mode `0700`) and the marker and starts
  control in that session. Sessions that were already running pick it up only if they were already watching the
  directory; restart the rest.
- Or by hand: `mkdir -m 700 ~/.config/omp-session-control && touch ~/.config/omp-session-control/enabled`, then start
  sessions. A dashboard's setup command does the same thing.

Check what a session is doing with `/session-control status`. It prints the plugin version, the control directory and one
of: listening, inert (and why), off, disabled, or an unsafe-directory reason.

## Turn it off

Use whichever you like; each takes effect in running sessions within a moment, without a restart:

| Do this | Effect |
|---|---|
| `/session-control off` | Writes `<dir>/off`: every session that watches the directory closes control. `/session-control on` reverses it |
| `touch ~/.config/omp-session-control/off` (remove the file to re-enable) | The same, from a shell |
| Delete `~/.config/omp-session-control/enabled` | Back to inert |
| Add `extension-module:omp-session-control` to `disabledExtensions` in omp's `config.yml` | omp suspends the extension and, when omp's settings internals are available, it closes its socket at once. If they are not, `/session-control status` and a notice say so and the socket stays open until the session ends: use one of the filesystem switches above |
| `omp plugin disable omp-session-control@omp-tether` | Disabled from the next session |
| `omp plugin uninstall omp-session-control@omp-tether` | Removed |

Closing removes the socket and registry entry and tells connected clients (`closing`).

### Purge

Uninstalling leaves the control directory in place. To remove every trace:

```
omp plugin uninstall omp-session-control@omp-tether
rm -r ~/.config/omp-session-control        # or the directory you configured
omp plugin config delete omp-session-control controlDir     # only if you had set it
```

## What it exposes

| Op | Does |
|---|---|
| `snapshot` | Returns the session id, a generation counter, the name (omp may derive it from your first message), whether a turn is running, the process id, the protocol it speaks and the operations it offers |
| `abort` | Stops the running turn, like pressing Esc. Does nothing when idle |
| `rename` | Sets the session name (1 to 120 characters; control and invisible characters are refused) |
| `plan-review` | While a plan review is open in the terminal: its title, plan file path, choices, execution tiers and the **full text of the plan as proposed**, in chunks |
| `plan-answer` | Answers that review as if you had picked the choice in the terminal: approve and execute, approve and compact, approve and keep context, refine (with feedback) or save and quit |

**Approving a plan starts executing it** in that session, so a client that can use `plan-answer` can make the session
act. It still cannot read the rest of the conversation, run commands itself, send arbitrary prompts or change files or
settings; but the plan text itself can contain whatever the model put in it, and the registry shows the review's title
to anything that can read the control directory. Edits you make inside the terminal's review overlay are not visible
through the socket, and an approval uses the edited text. Changing operations (and `plan-review`) must carry the live
session id and generation, so a dashboard that looked at a session before you switched it (`/new`, `/resume`) cannot
rename, stop or answer the new one by accident.

## The same-user caveat

The access control is the directory mode. The runtime exposes no peer credentials on a Unix socket, so the extension
cannot tell *which* process connected, only that it could enter a `0700` directory in your home, which means it runs as
you (or as root). A process that runs as you can already read your session files, kill omp, and run anything you can.
So this extension adds no capability such a process lacks, but it does add a convenient way to **stop, rename and,
with a plan review open, read and approve** your sessions. If you share a Unix account with people you do not trust, do not enable it. Machines where several people
have separate accounts are not a supported setup: the directory permissions should keep the other accounts out, but that
has not been checked on shared or network-mounted homes (a home shared between machines or containers can also make
one session delete another's files, because liveness is judged by process id). Full reasoning:
[`docs/threat-model.md`](docs/threat-model.md).

## Settings

Set with `omp plugin config set omp-session-control <key> <value>`; the environment variable beats the stored setting,
which beats the default. Changes apply to sessions started afterwards.

| Setting | Environment variable | Default | Meaning |
|---|---|---|---|
| `controlDir` | `OMP_SESSION_CONTROL_DIR` | `~/.config/omp-session-control` | Absolute path of the control directory. A relative path turns control off and says so |
| `idleTimeoutMs` | `OMP_SESSION_CONTROL_IDLE_MS` | `60000` | Close a client that sends nothing for this long (100 to 3 600 000) |
| `debugLog` | `OMP_SESSION_CONTROL_DEBUG` | off | Also write the text of unexpected errors (cut to 200 characters) to `events.log`. That text can contain sensitive values: switch it on to diagnose a problem, then off again |

The full socket path must fit the platform limit (103 bytes on macOS, 107 on Linux); a directory with a long path is
refused with a message.

## Troubleshooting

- `/session-control status` says **inert**: the directory or the `enabled` file is missing, or the session was started
  before you created them (restart it, or run `/session-control on`).
- It says **unsafe**: the control directory is a symlink or belongs to another user. Use a directory you own.
- Nothing listens: `<dir>/events.log` lists starts, stops and errors (kinds and categories, process ids, session ids; names and request content are not recorded deliberately, and error text is recorded only with `debugLog`).
- A client sees `stale_session` or `stale_generation`: the session switched; read the registry again.

## Develop

Needs [Bun](https://bun.sh) (1.4 or newer), Python 3.10 or newer, and for the integration test omp itself.

```
make test-unit          # no omp needed
make test-integration   # real omp in a scratch HOME, fake model, scratch relay on a random port
make gate               # headless install-and-load check of every plugin
make check              # everything above plus lint and the public-tree check
```

Layout: `src/omp-session-control.ts` (the extension), `tests/unit` (fake omp, crash tests, source audit),
`tests/conformance` (the protocol suite, usable against any implementation), `tests/integration` (real terminal).

## Licence

MIT. See [`LICENSE`](LICENSE). Security reports: see [`SECURITY.md`](SECURITY.md).
