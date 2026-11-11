# Threat model: omp-session-control

For people deciding whether to enable it, and for people reviewing it. Plain language; the protocol itself is in
[`docs/protocol/control-1.md`](../../../docs/protocol/control-1.md).

Status: beta. This is not a certification: nothing here has been audited by a third party, and the "accepted" items below
are the author's judgement. Read the source, it is one file.

## What it is, and what it gives an attacker

An extension that runs **inside** every interactive omp session, with no sandbox (omp extensions have none). When you
enable it, the session listens on a Unix socket and answers three requests: `snapshot` (session id, generation, name,
busy flag, process id), `abort` (stop the running turn) and `rename`.

What control over the socket gives: stopping the turn a session is running, changing its title, and reading the title. omp
may derive the title from your first message, so it can contain text you typed. It does **not** give
reading the conversation, sending prompts, running commands, reading or writing files, or changing settings.

Worst realistic outcome of someone controlling the socket: they interrupt your running turns again and again, put a
misleading title on a session, or read session titles (which omp may derive from your first message). The first two are
annoying; reading a title discloses that text to the process asking. None of it runs code or exposes the conversation.

## Who is trusted

**The user that runs omp, and root.** Nobody else. Access is decided by one thing: whether a process can enter the
control directory, which is mode `0700` and owned by you. The runtime provides no peer-credential check on Unix sockets,
so the extension cannot tell two same-user processes apart, and does not try.

## Accepted risk: a process running as you

Any program you run (an editor plugin, a build script, an npm package's install script, a compromised dependency) runs as
you. It can already read your omp session files and credentials, send signals to omp (`kill`), and attach a debugger. It
can use this socket too. That adds one more way to stop and rename sessions, and no capability it did not have. This is
the same trust model as an SSH agent socket or a Docker socket in your home: **if you do not trust the programs running
under your account, do not enable it.**

## Threats and what answers them

| Threat | Answer | Residual |
|---|---|---|
| Another user on the same machine connects | The directory is `0700` and the socket `0600`; the extension refuses a directory owned by someone else, reached through a symlink in its last component, or writable by group or others, and tightens a merely too-readable mode to `0700` through the open directory | A user with root, or one who knows your password, is out of scope. Only the last path component is checked: a configured directory under a parent that other users can write to could be swapped from underneath, so keep it under your home (the default is). On shared multi-user hosts see the supported-environment note below |
| The feature turns on by itself (a hostile project, a supply-chain update) | It is inert until the control directory exists **and** holds an `enabled` file you (or your setup command) created. Installing, updating or loading the extension creates nothing and opens nothing | The two settings come from the environment and from the user-level plugin settings only; the extension asks omp for the settings of the home directory, so a repository's own `.omp` folder cannot redirect the socket | `OMP_SESSION_CONTROL_DIR` is read from the environment omp starts in: a tool that sets environment variables per project folder (direnv, mise and similar) can point it elsewhere. The directory still has to be yours, private, and contain `enabled` |
| A client talks to the wrong or a stale session (pid reuse, a registry left by a crash) | The registry is named after a per-start random nonce that every request must carry; changing requests must carry the live session id and generation, which change when the session switches or branches (`/new`, `/resume`, `/fork`, a branch) | A client that skips the pid-alive check may connect to a dead socket and simply fail |
| Malformed or hostile frames crash or wedge the session | One frame is at most 64 KiB; JSON parsing is wrapped; unknown fields and operations get error replies; at most 4 clients and 16 connections in all, 16 requests in flight, idle connections closed after 60 s, a frame must finish within 10 s of its first byte, an operation omp does not finish in 10 s is abandoned with a `timeout` reply, readers that never read are cut off at 256 KiB of backlog, turned-away sockets are destroyed even when the peer never closes | A same-user client can still keep 4 connections busy and make real dashboards wait (denial of service by a trusted party) |
| An exception in a callback takes down the whole session (omp treats an uncaught exception as fatal) | Every callback given to a socket, watcher or promise is wrapped; timers use omp's isolated `ctx.setTimeout`; a unit test audits the source for raw timers and unwrapped handlers, and a crash test runs the extension in a bare process with failing omp calls and hostile clients | Dependencies or future edits could break this; the audit test is there to catch the second |
| Symlink and race tricks in the control directory (same user only) | Files are opened with `O_NOFOLLOW`, the directory is checked and its mode set through one open handle, registries are written through a temporary file and an atomic rename, the log never follows a symlink, stale cleanup only unlinks plain files and sockets inside the directory. A directory that is replaced or removed, or a deleted socket or registry, makes the listener close and start again | A same-user attacker can delete or replace files in your own directory; that only closes or restarts control. Stale cleanup decides liveness by process id, so a home directory shared between machines or containers is unsupported |
| A misleading or disguised session name (bidirectional overrides, zero-width characters, control characters, very long names) | Names are limited to 120 characters, trimmed, with control characters and invisible format characters refused | A dashboard must still escape names when it shows them (HTML, terminal). That is the client's job |
| Resource use | 4 clients, 16 connections, 16 in flight, one small listener per session. File watches are backed by a bounded recheck: while the control directory exists (or was in use), every 2 s the session looks at a few files again (three `lstat` calls and no writes), through omp's isolated timers (one pending at a time, cleared at shutdown). Nothing is scheduled when no control directory exists | A watch that misses an event (one made while the watch is being armed, or dropped under load) delays the effect of a marker, `off` file or directory change by at most about 2 s |
| Supply chain: the plugin itself | One source file, no dependencies, no install scripts (omp runs none for marketplace installs), no network access, no child processes. The catalog entry can be pinned to a commit | You are trusting this repository's author and the release you pinned. Read `src/omp-session-control.ts` before you enable it |
| Information in logs | `events.log` (mode `0600`, emptied before it passes 256 KiB, at most 2 KiB per record) records starts, stops and errors: kind, time, process id, session id and an error category (never exception text). Replies to clients never carry omp's error text, only a fixed message. The `debugLog` setting adds error text cut to 200 characters | Request content and names are not written deliberately, but with `debugLog` on, an exception from omp could carry any value (a name, a path, a credential). It stays in the 0700 directory; switch the setting off after use |
| A FIFO, device or other special file where the extension expects a plain file (`events.log`, `enabled`, `off`) | Such an entry is never opened: opening a FIFO for writing blocks the whole session, and no guard or timer can rescue a blocked synchronous call. The log's mode is set to `0600` through the open descriptor | A same-user attacker can still delete or replace files; that only closes control |
| Settings internals missing, so a live `disabledExtensions` edit cannot be noticed | The extension says so once when control starts and in `/session-control status`, and the documented reliable switches are the `off` file and removing `enabled` | Until the session ends or one of those switches is used, an already open socket keeps answering after `disabledExtensions` was edited |
| A change that omp never finishes (a hung rename) | The request gets `timeout`; further changes get `mutation_pending` until omp settles the first, so changes never overlap; reads keep working | A hung omp call blocks renames in that session until it ends |

## What is stored, and where

- `~/.config/omp-session-control/` (or the directory you configured): the marker files, one registry file and one socket
  per listening session, and `events.log`. Nothing secret. The nonce in the registry is an anti-confusion token, not a
  secret against the same user.
- omp's plugin lockfile may hold the two settings (`controlDir`, `idleTimeoutMs`). Neither is secret.
- The extension sends nothing anywhere. It makes no network connection and spawns no process.

## Supported environment

One person's machine: macOS or Linux, one trusted user account, nobody else with root. On a Linux host with several
people sharing the machine, the directory permissions keep other accounts out, but this has not been checked against
shared-home or network-home setups, so it is unsupported there. Windows is unsupported (no Unix sockets).

## Advice for authors of clients

- Read the registry, check the pid is alive and the socket is a socket owned by the current user inside a `0700`
  directory, then connect. Do not accept a registry path from anywhere else.
- Always send the nonce. Take `sessionId` and `generation` from a fresh registry read or snapshot, and on `stale_*`
  re-read; never retry blindly.
- Treat every reply field as untrusted data when you display it.
- Do not forward the socket to a network. If you must reach a session from another machine, authenticate that hop
  yourself.

## Open items

- No third-party audit has been done.
- The default directory follows `~/.config`, not `$XDG_CONFIG_HOME`; set `OMP_SESSION_CONTROL_DIR` if you need another.
- `getPluginSettings` is an omp internal, not a documented extension API. If omp changes it, the extension falls back to
  the defaults (and the environment), which is the safe direction.
