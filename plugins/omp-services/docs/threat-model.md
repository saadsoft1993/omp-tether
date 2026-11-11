# Threat model: omp-services

For people deciding whether to run it, and for people reviewing it. Plain language. The protocols are in
[`health-1.md`](../../../docs/protocol/health-1.md) and [`services-1.md`](../../../docs/protocol/services-1.md).

Status: independent security reviews have been done and their findings fixed; no external penetration test has been done. The "accepted" items are the author's judgement: the command line is
standard-library Python and the extension is one short file, so both can be read in a sitting.

## What it is, and what it can do

A command line that, after you confirm a plan, writes service-manager units (launchd or `systemd --user`) that run programs
other plugins asked for, keeps a private copy of its own code and some wrapper scripts, and runs a heartbeat that executes
health checks and sends one request to a URL you gave it. It also runs `omp` (to find its version and list plugins). An omp extension, `/omp-services`, starts the command line when you type the command.

Everything it does, it does **as you**. It never uses `sudo` and never changes system settings (it prints the `loginctl enable-linger` command instead of running it). By default it writes only under your home directory; the `OMP_SERVICES_*_DIR` variables can move its directories to any absolute path you choose, and your own service manager acts on the units you let it load.

## Who is trusted

**The user that runs it, and root.** Nobody else. Another account on the machine, a web page, a repository you opened and
a network peer are not trusted and are what the rest of this page is about.

## Accepted risk: a process running as you

Any program you run (an editor plugin, a build script, an installer's script, a compromised dependency) runs as you. It can
already read and change everything this tool reads and changes: your configuration, your units, your secrets, your omp
profile. It can write a drop-in, edit a unit and read the heartbeat URL. The tool adds no power for such a process, and does
not try to stop it. The protections below are for everyone else, and for mistakes.

## Threats and what answers them

| Threat | Answer | Residual |
|---|---|---|
| Another user writes a drop-in that makes the heartbeat run a command or the installer start a service | A drop-in must be a regular file of yours that group and others cannot write, in a directory of yours that they cannot write; symbolic links are refused; the check fails closed (an unusable drop-in is a *failing* check, never silently ignored) | Only the final directory component is checked, so keep the configuration under your home. A shared or network-mounted home is unsupported |
| A plugin you installed writes a hostile drop-in | It is a same-user process: accepted. The drop-in cannot do more than its schema allows: a check can run a command by absolute path (no shell), a service runs one program. `doctor` lists every drop-in and what it declares. A plugin can watch and restart only the services **it** declares | Reading a plugin before you install it is still your job; so is reading the plan before you answer yes |
| The tool unloads, replaces or deletes a unit that is not its own (a typo, a hostile label, a damaged state file) | Every unit it touches must carry your configured label prefix **and** its "Managed by omp-services" marker, or be byte for byte what it recorded (the recorded sha256) in its state file. The unit file path is always derived from the label and the unit directory: a path stored in the state file is ignored. Ownership is checked again at the moment of each action (install, reload, remove, watchdog restart), not only when the plan was made. A unit with a declared label that exists but is not ours is reported and left alone | A person who gives a unit of their own the same prefix and the marker comment gets what they asked for |
| A restart destroys something in progress (a multiplexer server that holds live sessions) | A unit flagged `protected` is never reloaded, restarted or removed by `init`, `uninstall` or a watchdog; only a person naming it (`--restart-protected`) can. A watchdog check cannot override it: there is no such setting. Protection is the union of what was recorded at install time and what the drop-ins declare, so a declaration that drops the flag does not lift it, and a unit of ours that the state file has no record of (lost state) is treated as protected until you name it; a unit that systemd reports as activating, reloading or deactivating is live, not stopped, and is never unloaded, restarted or removed while it changes state (it is reported); and a watchdog restarts nothing when the installed state is missing, a dangling link or unreadable (that is lost state, not a machine with nothing protected); and a protected unit whose file went missing is not reloaded either. `uninstall` leaves protected units and what they still need (wrappers, code release, state); `purge` refuses while any exist | It relies on the plugin flagging its service honestly in the first place |
| Symbolic-link and race tricks in the configuration, data and state directories | Private directories are checked and tightened through an open handle (no chmod by path); files are written through an exclusive temporary file and an atomic rename; secrets, the configuration, the state file and drop-ins are read without following links and **checked on the opened descriptor** (owner, no group or other write bit), in directories that others cannot write; `uninstall`, `init`'s pruning refuse a managed directory that is a link and delete only entries this tool created: a release directory counts only if its name has the right form **and** it holds the marker file written when it was created; wrappers only with the tool's header | A same-user attacker can still replace the directory itself; see above |
| `purge` deletes something it should not (a mistyped environment variable pointing at `$HOME`, an unrelated directory that happens to have the right name) | It deletes a whole directory only when it carries the marker file with the exact content `init` writes, and the marker is written in the step that **creates** the directory, never into one that already existed. A directory that already existed stays unmarked: `init` records the names it creates directly inside it (`.omp-services-entries`, a plain file of yours; a name with a path component, `..`, the marker or a drop-in directory is refused) and `purge` deletes only those names, then the record, and the directory only when that leaves it empty; without a record `purge` refuses it. The directory must be a real directory of yours (not a link) and not your home or an ancestor of it; the checks are repeated at the moment of deletion; a typed word or `--confirm-purge` is required, `--yes` does not count | A directory that carries a forged marker with the right content in your own home is yours to lose; a record you edit to name a file of the directory is yours to lose |
| The heartbeat URL leaks | It is a bearer secret. It lives in a `0600` file in a `0700` directory; it is read from standard input (or a hidden prompt), never from a command-line argument; it is never logged, written to the status record, put in an error message, echoed by `doctor` or `config show`, or included in a unit; a test sweeps every file the tool writes for it, including after failed pings | The third party that receives the ping sees your machine's IP address and the timing of the pings, and the pass/fail verdict; it never sees check names, host names, paths or output (the request has no body and the URL path is the one you gave it). Your shell history keeps the URL only if you pass it some other way than the prompt |
| A ping goes somewhere else or is read on the wire | `https` only, certificate and host name verified, no credentials in the URL, **no redirect is ever followed** (a redirect counts as a failed ping), no proxy from the environment | A certificate authority you trust is trusted |
| A health check is used to probe other machines (server-side request forgery) | `url` and `port` checks refuse hosts that are not loopback unless you set `health.allowRemoteUrls`; no redirects; responses are capped at 64 KiB | If you allow remote hosts, a drop-in can make the machine connect to them |
| A check hangs the heartbeat or runs away | Every check has a deadline (default 10 s, at most 60 s) and the whole round a cycle deadline. Network checks share one absolute deadline across name resolution, connecting, the TLS handshake, the request and the body, and the caller is released at it; name resolution cannot be interrupted, so that thread is abandoned, but once the deadline or a cancellation has passed it opens no connection and sends nothing, and the socket is shut down; `file-age` walks in such a thread too; every call a watchdog makes to the service manager shares the check's remaining budget; a command runs without a shell in its own process group and the whole group is killed at the deadline and also after the command exits (a descendant that stays in that group does not outlive its check; one that starts a session of its own with `setsid()` is outside the group and is **not** reached: that is a stated limit); stopping the command line (SIGTERM) kills the groups it started; output is capped | An abandoned thread (a blocked resolver, a huge directory walk) keeps running until the system call returns, so it can still be using a little CPU and memory. It is told to stop before it does anything with side effects (a request is not sent once its caller has given up), and at most 16 can exist at once; beyond that, checks report a timeout without starting more |
| A health result reveals information | Results carry a status and a short code of words and digits; never a URL, a path, a command line or command output | Check ids and plugin names are chosen by the plugin and appear in the log |
| A command run by a check inherits secrets | Commands get a fixed small environment (`PATH`, `HOME`, `LANG`); nothing of the runner's environment | `HOME` is visible to them |
| Two commands at once (a heartbeat round and a manual run, two first inits) corrupt state or spend a restart allowance twice | `init`, `uninstall` and `purge` take an exclusive lock on `<data directory>.lock`, a file next to the data directory (so `purge` cannot delete the lock it holds, and the first `init` is covered too), checked with `fstat` (a private regular file of yours), and refuse to run while another holds it; a layout that would put the lock file inside a directory purge may delete, or in which the config, data and state directories overlap, is refused; read-only previews take no lock and create nothing; the watchdog reserves its restart budget under its own lock, and an unreadable history file fails closed (no restart) instead of resetting the budget | The lock is advisory: it stops omp-services commands, not other programs |
| The code a service runs is not the code that was installed (a half-written or substituted release) | A release directory is named after the digest of its content, built from the staged copy and verified before it goes live; an existing release whose content does not match its name is refused by `init` and reported by `doctor` (`runtime.integrity`) | A same-user process can still change the files after the check |
| An untrusted configuration or state file chooses what runs (`omp.path`, `python.path`, `tools.tailscale`, interpreters in the state file) | They are held to the drop-in standard (ours, not writable by group or others, in a directory not writable by them, checked on the opened descriptor); state fields are validated, not just its shape; nothing named in an untrusted configuration is executed, not even by `doctor`; `heartbeat set-url` and `clear-url` touch only files inside the secrets directory | Reads of the file by path elsewhere are not covered |
| The one-line installer is tampered with | It is about 100 lines, asks before it installs a plugin or changes anything, and does nothing without a terminal. Download it and read it first if you like | `curl \| sh` is a trust decision in the host that serves it |
| The extension is used to run something | It runs only the plugin's own `bin/omp-services` (or the absolute path in `OMP_SERVICES_BIN`) with fixed arguments: `execFile`, no shell, a deadline, a size cap, a kill signal, and `BUN_BE_BUN` removed. The only changes it can cause are those of `init --yes --expect-plan <digest>`, after a confirmation dialog that shows the complete plan; the plan digest binds the answer to exactly that plan: it covers the displayed text **and** the exact payloads behind it (every unit file, wrapper script, configuration text, release id and deletion target), so a plan that reads the same but would write different bytes has a different digest and is refused, and a plan too long for the dialog is not applied from the session at all (use a terminal). Typed arguments other than `doctor` and `setup` are refused, not passed on | `OMP_SERVICES_BIN` is read from the environment omp starts in (a tool that sets variables per folder can point it elsewhere); it must be an absolute path to an executable |
| An exception in the extension takes down the session (omp treats an uncaught exception as fatal) | Every callback handed to a raw Node API is wrapped; the command handler catches everything; a unit test audits the source for raw timers, unwrapped callbacks, floating promises, other process starts, writes and network use | Future edits could break this; the audit test is there to catch that |
| Logs hold something they should not | Service logs are the service's own output, in a `0700` directory with `Umask 077`; the heartbeat log holds times, check ids, status codes and reasons. The heartbeat trims logs above `logs.maxBytes` (one generation kept) | Whatever a service itself prints goes into its log |
| Units expose data | Unit files are `0644` (the service manager needs to read them) and contain paths, program arguments and the non-secret environment a plugin declared. Secrets belong in `0600` files the service reads itself; the reserved names (`PATH`, `LD_PRELOAD`, `DYLD_*`, `BUN_BE_BUN`, `OMP_SERVICES_*`, `HOME`) cannot be set by a drop-in | Other local users can read the unit files in a default home; do not put secrets in `env` |

## Out of scope

These are decisions, not oversights; nothing in this tool tries to defend against them.

- **Any process that runs as the same user.** It can already read and change everything this tool reads and changes (see
  "Accepted risk" above). The checks on files and directories protect against other users and against mistakes, not
  against code that already runs as you.
- **root.** root can do anything to any file, unit or process; nothing here is a defence against it.
- **Anyone who can already write in a directory this tool trusts**: the configuration, data, state or unit directory, or
  the drop-in directories. Mode and ownership checks refuse to *use* such a directory; they do not stop a person who
  owns it from changing what is in it.
- **Shared machines.** Multi-user hosts, shared or network-mounted homes and other users' access to the same account are
  not a supported setup.
- **Descendants that leave their process group.** A command a check starts, and everything it forks, is killed with its
  group at the deadline; a descendant that starts a session of its own with `setsid()` is outside that group and is not
  reached.
- **No live systemd was tested.** The systemd units, the `systemctl` answer parsing and the transitional-state handling
  were exercised against fixtures and `systemd-analyze verify` in a container without a running systemd. launchd was
  exercised for real, with a scratch label prefix. Run `init --plan` and `doctor` on a disposable user first.
- **Upgrading.** There is no `upgrade` command and no rollback. omp upgrades plugins (`omp plugin upgrade`); `doctor`
  reports newer versions and drift; `init` re-renders units.

## What is not covered

- Multi-user hosts and shared or network-mounted homes.
- Windows.
- Whether a plugin that asks for a service deserves one. Read the plan.
- The Tailscale Serve route check is written against the documented JSON of `tailscale serve status --json` and has not been
  run against a real tailnet; the Linux owner lookup reads `/proc/net/tcp` and the macOS one `lsof`, both tested only
  against fixtures and local listeners.
