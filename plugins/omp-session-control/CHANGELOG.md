# Changelog

All notable changes to `omp-session-control`. The wire protocol has its own number (`session-control/1`) and changes
only by spec; see `docs/protocol/control-1.md` in the repository.

## 0.3.0 (2026-11-16)

Session commands from a dashboard (each is offered only when the host omp has the API behind it; omp 18.19.0 has them):

- New operations `session-info` (current model, thinking level and the levels the model supports, the models the session
  can switch to, and the status of the plan, vibe, goal and loop modes), `compact`, `set-model`, `set-thinking` and
  `set-mode`. All five name the live session and generation; the changing ones are serialized with the other changing
  operations. New error codes `unsupported` (the host lacks the API) and `failed` (omp threw or refused; the message is
  fixed, the cause goes to `events.log`). Details: `docs/protocol/control-1.md`, section 6.
- **`compact` returns when the compaction has finished**, with its own 180 s deadline (other operations: 10 s). A
  `timeout` for `compact` means it is not finished yet, not that it failed; later changes are refused at once with
  `mutation_pending` until omp settles it. It refuses (`failed`) while a turn is running instead of aborting the turn as omp's own manual
  compaction does.
- `set-mode` accepts what `ctx.sessionModes.set` accepts, except starting a loop (a loop needs a prompt and a limit):
  plan and vibe enter/exit, goal enter (with an objective)/pause/resume/drop, loop exit.
- A connection with a request that is still unanswered is no longer closed as idle (a compaction can take longer than
  the 60 s idle timeout).
- `ops` in the registry and in `snapshot` lists the new operations after the plan operations, for the ones the host has.
- **What is now exposed changes.** A client of the socket can switch the session's model (spending against another
  provider account), change its thinking level, start a compaction (which rewrites the context) and change plan, goal and
  loop modes. See the updated threat model.

## 0.2.0 (2026-11-16)

Plan review from a dashboard (needs an omp that has `ctx.getPendingPlanReview` and `ctx.resolvePlanReview`; 18.19.0 has them):

- New operations `plan-review` (read the open review's metadata and the plan text in chunks, with a cursor counted in
  UTF-16 code units) and `plan-answer` (pick `execute`, `compact`, `keep`, `refine` or `save`, with feedback for refine and
  an optional execution tier). Both name the live session and generation. A new error code `not_accepted` means the
  terminal refused the answer. Details: `docs/protocol/control-1.md`, section 6.
- The registry gains `ops` (the operations the session answers; plan operations only when the host has both methods) and
  `planReview` (`null`, or the open review's id, title and opening time). Older clients ignore both.
- The plugin follows `plan_review_requested` and `plan_review_resolved` and checks them against the host's
  `getPendingPlanReview` after every event, answer and bind, so a late resolve for a replaced review cannot hide the new one.
- **What is now exposed changes.** While a review is open, a client of the socket can read the full plan text as
  proposed and answer the review, which for an approval starts execution. See the updated threat model. Edits made inside
  the terminal's review are not visible through the socket.
- On an omp without the two methods nothing changes: the plan operations are not offered and answer `unknown_op`.

## 0.1.0 (2026-11-11)

First release.

Watch races:

- A marker, `off` file or directory change made at the moment a directory watch is being armed could be missed (the event
  is lost). The watch is now armed before the state is read, and a bounded recheck every 2 s (through omp's isolated
  timers, one at a time, only while the directory exists or was in use) covers any event a watch drops. Result: 0 of 600
  immediate markers missed (a bare watch missed 3 to 20 of 200).

Control directory, operations and wire format:

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

Listener, directory and connections:

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
