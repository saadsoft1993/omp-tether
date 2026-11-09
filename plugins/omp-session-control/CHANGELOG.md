# Changelog

All notable changes to `omp-session-control`. The wire protocol has its own number (`session-control/1`) and changes
only by spec; see `docs/protocol/control-1.md` in the repository.

## 0.1.0 (2026-11-11)

First release. It includes the fixes from two independent reviews.

Watch races:

- A marker, `off` file or directory change made at the moment a directory watch is being armed could be missed (the event
  is lost). The watch is now armed before the state is read, and a bounded recheck every 2 s (through omp's isolated
  timers, one at a time, only while the directory exists or was in use) covers any event a watch drops. Measured on the
  previous code: 3 to 20 of 200 immediate markers were never noticed; now 0 of 600.

Second review:

- A timed-out change keeps the host marked busy until omp actually settles it: later changes get `mutation_pending`
  (new error code), including after an off and on cycle, so a late rename can no longer overwrite a newer one.
- A FIFO or other special file where a plain file is expected (`events.log`, `enabled`, `off`) is never opened, so it
  cannot block the session; the log's mode is enforced through the open descriptor.
- `events.log` holds error categories, not exception text (text only with the new `debugLog` setting, cut to 200
  characters), with at most 2 KiB per record.
- A control directory that vanished is waited for through its parent, so putting it back restores control.
- Publishing the registry is part of starting: a failure closes the listener, removes the temporary file, is reported and
  shows in `/session-control status`.
- When omp's settings internals are unavailable the extension says that live disabling does not work (notice and status).
- A malformed `op` no longer stops the frames pipelined behind it; invalid UTF-8 and non-finite numeric ids are rejected;
  a reply never exceeds a frame and never reflects rejected input.
- The conformance suite checks the directory and registry (no symlinks, ownership, modes) before connecting, never
  follows symlinks or blocks on special files, and creates the off marker exclusively.

First review:

- A session branch republishes the registry with the new session id and a higher generation (it was handled like
  nothing happened).
- A control directory that is removed and recreated, or whose socket or registry file disappears, makes the listener
  close and start again instead of staying bound to nothing.
- An operation omp does not finish within 10 s gets a `timeout` reply and stops blocking the queue; closing a
  listener resets its in-flight accounting; a mutation queued by a client that has disconnected is not run.
- The optional settings-registry lookup can no longer stop the extension from loading when omp renames it.
- Connections that are turned away, or that we say goodbye to, are destroyed even when the peer never closes; at most 16
  connections are open at once; a frame must complete within 10 s of its first byte.
- The final `closing` frame is flushed before the socket is closed.
- Replies to clients never contain omp's error text; an over-long request id is not echoed; a thrown value with a
  hostile `toString` cannot escape the guards; the registry is not published before the socket is bound; a socket that
  cannot be set to `0600` is closed.
- The directory is checked and its mode set through one open handle (no chmod by path); a directory that group or
  others can write to is refused.
- The settings listener is removed at shutdown.

- Extension `omp-session-control` (entry file `src/omp-session-control.ts`, id `extension-module:omp-session-control`).
  One file, no runtime dependencies.
- Inert until the control directory exists with an `enabled` marker. Default directory `~/.config/omp-session-control`.
- Operations `snapshot`, `abort` (stop the running turn) and `rename` over a `0600` Unix socket in a `0700` directory.
- Protocol `session-control/1`: line-delimited JSON, 64 KiB frames, per-listener nonce, session id and generation checks
  on every change, 4 clients, 16 requests in flight, idle timeout.
- `/session-control status | on | off`, the `off` file, and a live `disabledExtensions` edit all close control at once.
- Settings `controlDir` and `idleTimeoutMs`, with `OMP_SESSION_CONTROL_DIR` and `OMP_SESSION_CONTROL_IDLE_MS` overriding
  them.
- Conformance suite, unit suite, real-terminal integration test and a headless extension-load gate.
