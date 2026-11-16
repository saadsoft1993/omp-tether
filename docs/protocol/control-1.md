# session-control/1

The wire protocol that an omp session speaks on a private Unix socket so that a local program can read its state, stop
its running turn, rename it and, when a plan review is open, read the plan and answer the review. The reference implementation is the `omp-session-control` extension in this repository.
This document is the contract: other implementations and other clients should be able to work from it alone, and
`plugins/omp-session-control/tests/conformance/suite.ts` checks an implementation against it.

Words in capitals (MUST, SHOULD, MAY) have their usual RFC 2119 meaning.

## 1. Scope and trust

- The peers are processes of **one user on one machine**. There is no authentication beyond file permissions: the control
  directory is `0700`, and a process that can enter it is trusted completely. A process running as the same user can
  already do everything this protocol does (and more), so the protocol does not try to stop it.
- Nothing here is reachable over a network. A client that wants to reach a session from another machine has to
  carry the requests itself, and then owns the security of that hop.
- The protocol is **not** a way to read a conversation. Always exposed: the session id, a generation counter, the name,
  whether a turn is running, and the process id. Since protocol 1 revision 2 (plugin 0.2.0), while an interactive **plan
  review** is open, it also exposes that review: its title, the path of the plan file, its choices and execution tiers,
  and the **full text of the plan as proposed** (through `plan-review`; the registry carries only the review's id, a
  title cut to 200 characters and the time it opened). A client of the plan operations can also answer the review,
  which for an approval **starts executing the plan**, and send feedback text that omp uses as the next planning
  prompt. Nothing else of the conversation, the files or the settings is reachable. Note that omp may derive the name
  from your first message, so the name can contain text you typed, and that a plan can quote anything the model read.

## 2. The control directory

All state lives in one directory, the *control directory*.

| Item | Rule |
|---|---|
| Default path | `~/.config/omp-session-control` |
| Override | environment variable `OMP_SESSION_CONTROL_DIR`, or the plugin setting `controlDir`; the path MUST be absolute |
| Mode and owner | `0700`, owned by the user running the session, a real directory (not a symlink). An implementation MUST refuse a directory that is a symlink, is owned by someone else, or is writable by group or others, and SHOULD tighten a mode that is merely too readable (for example `0755`) to `0700`. It does so through the open directory, not by path. Only the final path component is checked: keep the directory under a parent that only you can write to (the default `~/.config` is) |
| Length | the full socket path MUST fit the platform's Unix socket limit (103 bytes on macOS, 107 on Linux); an implementation refuses to start rather than truncate |

Files in the directory:

| File | Mode | Written by | Meaning |
|---|---|---|---|
| `enabled` | any plain file | the user (or a setup tool) | Presence turns control on. A FIFO, device or other non-regular entry of that name is never opened (`/session-control on` reports it) and does not count. An implementation MUST do nothing at all (create no files, listen on nothing) while this file is absent. A symlink does not count |
| `off` | any | the user | Presence turns control off for **every** session at once, whatever `enabled` says. Removing it turns control back on |
| `<nonce>.json` | `0600` | the session | The registry entry of one listening session (section 3) |
| `<id>.sock` | `0600` | the session | The session's Unix stream socket; `<id>` is 16 lowercase hex characters |
| `events.log` | `0600` | the session | Starts, stops and errors only, one JSON object per line of at most 2 KiB, emptied before it would pass 256 KiB. Errors are recorded as a category (our error code, a system error code such as `EACCES`, or an error class name), never as the text of an exception; the debug setting adds the text, cut to 200 characters, and that text may then be sensitive. Request content and names are never written deliberately. A file that already exists must be a plain file you own: it is not opened if it is anything else (a FIFO would block the session) and its mode is set to `0600` |

### Activation

A session listens only when **all** of the following hold; it re-evaluates them whenever `enabled` or `off` changes
while it runs, and closes at once when any stops holding:

1. it is the main session of an interactive terminal run (not a subagent, not a print, JSON or RPC run);
2. the control directory exists and passes the checks above (a directory that is replaced or removed while a session runs makes it close and, once a directory that passes is there again, start again with a new nonce; so does the loss of its own socket or registry file. After a removal the session watches the directory's parent, only for that name, so putting the directory back is enough). The marker files and the directory are noticed through file watches that are armed before the state is read, and a recheck every 2 s (only while the directory exists or was in use) covers events a watch loses, so a change takes effect within about 2 s even then;
3. the `enabled` file is a plain file in it;
4. there is no `off` file;
5. the extension is not listed in omp's `disabledExtensions` setting (extension id
   `extension-module:omp-session-control`). The extension learns of a live edit of that setting through omp-internal modules; if they are unavailable it says so (a notice when control starts, and `/session-control status`) and keeps working, so the reliable switches are the `off` file and removing `enabled`.

Closing means: send `{"v":1,"id":null,"ok":false,"code":"closing","message":"..."}` to every open connection, close
them, close the listener, unlink the socket and the registry entry.

## 3. The registry entry

`<nonce>.json`, at most 4096 bytes, written atomically (temporary file, `fsync`, rename) **after** the socket is bound and set to `0600` (if the socket cannot be set to `0600` the listener is closed). Never before:

```json
{
  "v": 1,
  "versions": [1],
  "nonce": "3f9a...32 lowercase hex characters",
  "pid": 12345,
  "socket": "/home/u/.config/omp-session-control/0123456789abcdef.sock",
  "sessionId": "01a1c0db-82df-745a-95c4-c1388130d934",
  "generation": 0,
  "plugin": { "name": "omp-session-control", "version": "0.2.0" },
  "ops": ["snapshot", "abort", "rename", "plan-review", "plan-answer"],
  "planReview": { "reviewId": "2f1b...", "title": "Add a widget", "openedAt": "2026-11-16T08:01:12.004Z" },
  "writtenAt": "2026-11-16T08:01:12.010Z"
}
```

| Field | Meaning |
|---|---|
| `v` | version of this registry format; 1 |
| `versions` | wire protocol versions the session speaks (section 5). A client picks one it knows |
| `nonce` | random per listener start; every request MUST carry it. The file is named after it. It proves the client read the registry of *this* process, not a stale file |
| `pid` | process id of the session. A client SHOULD check that it is alive before trusting the entry |
| `socket` | absolute path of the socket, inside the control directory |
| `sessionId` | omp's id of the session as of `writtenAt` |
| `generation` | starts at 0 and increases by 1 each time the session switches to another session (`/new`, `/resume`, `/fork`, a branch) in the same process. The registry is rewritten at every switch and every branch |
| `plugin` | informational |
| `ops` | the operations this session answers, the same list as `snapshot.ops`. `plan-review` and `plan-answer` are listed only when the host omp has both `getPendingPlanReview` and `resolvePlanReview` (see Capability below). A registry with no `ops` field (plugin 0.1.x) means `snapshot`, `abort` and `rename` only |
| `planReview` | `null`, or `{reviewId, title, openedAt}` for the interactive plan review that is open now: `reviewId` is at most 128 UTF-16 units, `title` at most 200 UTF-16 code units (cut so that the whole file stays within 4096 bytes), `openedAt` is the ISO-8601 UTC time the plugin first saw the review. Present on every registry of a 0.2.0 plugin; a client MUST treat a missing field as `null`. The registry is rewritten when a review opens, changes or closes |

A client MUST ignore fields it does not know. Entries whose `pid` no longer exists are left behind by crashed sessions;
a starting session removes them (and their sockets), and a client MAY ignore them.

## 4. Messages

One **frame** is one UTF-8 JSON value followed by `\n`. Frames are at most **65 536 bytes** (without the newline).
Blank lines are ignored. Both directions use the same framing.

### Request

```json
{"v":1,"id":"r1","nonce":"<from the registry>","op":"rename",
 "sessionId":"<from the registry>","generation":0,"args":{"name":"Release notes"}}
```

| Field | Rule |
|---|---|
| `v` | MUST be a protocol version the session speaks (`1`) |
| `id` | string of at most 128 characters, or a number. Echoed in the reply |
| `nonce` | MUST equal the registry's `nonce` |
| `op` | one of the operations in section 6 |
| `sessionId`, `generation` | required for `abort`, `rename`, `plan-review` and `plan-answer`, ignored by `snapshot` |
| `args` | object; absent means `{}` |

### Reply

```json
{"v":1,"id":"r1","ok":true,"result":{"before":null,"name":"Release notes"}}
{"v":1,"id":"r1","ok":false,"code":"stale_generation","message":"generation does not match",
 "detail":{"sessionId":"...","generation":1}}
```

`id` is `null` when the request had no usable id. `detail` is optional. A client MUST NOT parse `message`; it is for
people. The `message` of an `internal` error is a fixed text: omp's own error text (which can contain file paths) goes to `events.log` only. Replies to read-only operations (`snapshot`, `plan-review`) MAY arrive in any order, so a client with several requests in flight matches
them by `id`. Changing operations are executed one at a time, in the order they arrived on the socket, and their
replies come in that order. Serialization lasts until omp has actually settled the operation, not just until the client
stopped waiting: after a `timeout`, further changing operations get `mutation_pending` until omp finishes the one that
timed out (reads keep working), including across an off and on cycle. A changing operation whose client has disconnected, or whose listener has closed, before the operation starts is not run.

### Error codes

| Code | Meaning |
|---|---|
| `bad_json` | the frame is not JSON or not valid UTF-8 (`id` null); the connection stays open |
| `bad_request` | the frame is not an object, or `id` is missing, mistyped or over 128 characters |
| `bad_version` | `v` is not supported; `detail.supported` lists what is |
| `bad_nonce` | `nonce` missing or wrong |
| `disabled` | control was turned off (the listener is closing) |
| `unknown_op` | `op` is not one of the supported operations (inherited object names such as `constructor` are unknown too), including `plan-review` and `plan-answer` on a session whose omp lacks the plan-review methods |
| `invalid_args` | `args` is not an object, or an argument is invalid (names: section 6) |
| `not_accepted` | `plan-answer` only: the terminal did not accept the answer (see section 6) |
| `stale_session` | `sessionId` is not the live session; `detail` has the live `sessionId` and `generation` |
| `stale_generation` | `generation` is not the live one; `detail` as above |
| `overloaded` | more than 16 requests are in flight; retry later |
| `not_renamed` | omp accepted the call but kept the old name; `detail` has `before` and `after` |
| `not_ready` | the session is not bound yet |
| `internal` | omp failed while running the operation; the message is fixed, the cause is in `events.log` |
| `timeout` | omp did not finish the operation within 10 s. It may still take effect later; until it settles, changing operations get `mutation_pending` |
| `mutation_pending` | an earlier changing operation has not finished in omp yet (see `timeout`); retry later |
| `too_large` | a frame exceeded 65 536 bytes; the connection is closed after this reply. Also, for `plan-review` only: the review's metadata alone does not fit in one reply frame (the connection stays open) |
| `too_many_clients` | the 4-client limit is reached; sent on a new connection, which is then closed |
| `closing` | sent unsolicited (`id` null) just before the session closes the connection |

The checks happen in this order: JSON, object, `v`, `id`, `nonce`, off switch, `op`, `args`, in-flight limit, then (for
`abort`, `rename`, `plan-review` and `plan-answer`) `sessionId` and `generation`, then the operation's own argument checks.

## 5. Versions

`v` is the wire protocol version. This document describes version 1. A session lists the versions it speaks in
`versions` (registry) and `protocols` (snapshot result). A client picks the highest version it knows that is in the
list; if there is none, it MUST NOT send requests. A session answers a request with an unsupported `v` with
`bad_version` and `detail.supported`.

Within a version, additions are compatible: new optional fields, new operations (a client discovers them through
`snapshot.ops` or the registry's `ops`), new error codes. Clients MUST ignore unknown reply fields and treat unknown error codes as failures.
Anything else needs a new version.

## 6. Operations

### `snapshot`

Read-only. `args` is ignored.

```json
{"sessionId":"...","generation":0,"name":null,"busy":false,"pid":12345,
 "protocol":1,"protocols":[1],"ops":["snapshot","abort","rename","plan-review","plan-answer"],
 "plugin":{"name":"omp-session-control","version":"0.2.0"}}
```

`busy` is true while a turn is running (streaming or running tools). `name` is the session's current title or `null`. `ops` is the capability list: a client MUST NOT send `plan-review` or `plan-answer` to a session whose `ops` (or registry `ops`) does not list them; one that does gets `unknown_op`.

### `abort`

Changes the session; needs `sessionId` and `generation`. Stops the running turn exactly like pressing Esc: running tool
calls are interrupted. Result `{"aborted": true}` when a turn was running and was stopped, `{"aborted": false}` when the
session was idle (not an error).

### `rename`

Changes the session; needs `sessionId` and `generation`. `args.name` is a string. The implementation trims it, collapses
runs of spaces to one, and requires 1 to 120 characters (code points, not UTF-16 units) with no control characters (C0,
DEL, C1, U+2028, U+2029) and no invisible format characters (Unicode category Cf: bidirectional overrides, zero-width
marks, the byte order mark). A name that fails gets `invalid_args`, and nothing changes. Result:
`{"before": <old name or null>, "name": <new name>}`. If omp did not take the new name the reply is `not_renamed`.

### `plan-review`

Read-only, but **target-checked like a change**: it needs `sessionId` and `generation`, so a read that straddles a
session switch is refused with `stale_session` or `stale_generation`. `args` is `{"offset": <integer>}`, a position in
the plan text counted in **UTF-16 code units** (JavaScript string indexes), `0 <= offset <= total`; absent means 0. An
offset that falls between the two halves of a surrogate pair is `invalid_args`.

Result when no review is open: `{"open": false}`. Otherwise:

```json
{"open":true,"reviewId":"2f1b...","title":"Add a widget","planFilePath":"local://PLAN.md",
 "options":[{"id":"execute","label":"Approve and execute","disabled":false}, ...],
 "tiers":[{"role":"default","provider":"anthropic","model":"claude-opus-5-5"}, ...],
 "total":150000,"offset":0,"nextOffset":21873,"done":false,"chunk":"# Plan\n..."}
```

| Field | Meaning |
|---|---|
| `reviewId` | the review these chunks belong to. A reader MUST check that it is the same in every chunk; if it changes, the plan was replaced and the read starts over |
| `options` | the terminal's choices in display order: `id` is `execute`, `compact`, `keep`, `refine` or `save`; `label` is the terminal's text (the `keep` label carries a token count); `disabled` is true for a choice the terminal refuses (`keep` when context use is above 95%) |
| `tiers` | `null`, or the execution tiers of the terminal's "continue with" selector, in slider order: `role`, `provider`, `model` and an optional `thinkingLevel` |
| `total` | length of the whole plan in UTF-16 code units. Fixed for a given `reviewId`: the plugin pins the plan text it was given when the review opened |
| `offset`, `nextOffset`, `done` | this chunk is `plan[offset, nextOffset)`; `done` is true when `nextOffset == total`. A client reads from `offset` 0 and then from each `nextOffset` until `done`. Every chunk of a non-empty remainder is non-empty (progress is guaranteed) |
| `chunk` | the text. It never splits a surrogate pair, so it is well-formed Unicode |

The plugin serializes the whole reply, envelope included, and shrinks `chunk` until the frame is at most 65 536 bytes;
a JSON-escaped character costs 1 to 6 bytes, so chunks vary in length. If the metadata alone (title, path, options,
tiers) leaves no room, the reply is `too_large` (connection open). The text is the plan **as the review opened**: edits
made inside the terminal's review overlay are not visible, and an approval executes the edited text.

### `plan-answer`

Changes the session; needs `sessionId` and `generation`; serialized with the other changing operations. Args:

| Arg | Rule |
|---|---|
| `reviewId` | string, 1 to 128 UTF-16 units; the review being answered |
| `choice` | `execute`, `compact`, `keep`, `refine` or `save` |
| `feedback` | optional string of at most 8000 UTF-16 code units (JavaScript `.length`); REQUIRED and non-blank for `refine` (sent verbatim as the next planning prompt); ignored for the other choices |
| `role` | optional string of 1 to 64 UTF-16 units; only with `execute`, `compact` or `keep`, and it MUST name a `role` in the review's `tiers`. Without it the approval runs on the tier selected in the terminal |

Any other argument problem is `invalid_args`. Then the plugin calls the host's `resolvePlanReview`, once and never
retried; result `{"accepted": true}`. When the review is closed, was replaced by another `reviewId`, the choice is
disabled, or the host returns false for any reason, the code is `not_accepted` and nothing changed. **`accepted` means
the terminal took the pick, not that what follows worked**: writing, compaction and execution happen afterwards and can
still fail. An approval starts executing the plan in that session; `save` writes the plan to its default file, leaves
plan mode and clears into a new session (the omp process keeps running).

## 7. Limits

| Limit | Value |
|---|---|
| Frame size | 65 536 bytes; a longer line with no newline gets `too_large` and the connection closes |
| Simultaneous clients | 4; a fifth gets `too_many_clients` and is closed |
| Requests in flight | 16 across all clients; more get `overloaded` |
| Idle connection | closed after 60 s without data (the plugin setting `idleTimeoutMs` / `OMP_SESSION_CONTROL_IDLE_MS`, 100 to 3 600 000 ms). Any received byte resets the timer |
| Incomplete frame | a frame must be complete 10 s after its first byte; a client that trickles bytes is disconnected whatever the idle timeout |
| Operation | 10 s; after that the client gets `timeout` and the request stops counting against the in-flight limit |
| Open connections | at most 16 in total, accepted or turned away; beyond that a connection is dropped without a reply. A connection that was sent a final frame (`closing`, `too_many_clients`, `disabled`, `too_large`) is destroyed once the frame is flushed or after 2 s, whichever is first |
| Unread replies | a client that lets more than 256 KiB of replies pile up unread is disconnected |
| Name | 120 characters |
| Plan review id | 128 UTF-16 units |
| Registry title | 200 UTF-16 units, never splitting a surrogate pair; shortened further if the registry would pass 4096 bytes |
| Plan feedback | 8000 UTF-16 units |
| Plan role | 64 UTF-16 units |
| Registry entry | 4096 bytes |
| Reply `id` | an `id` that is not a string of at most 128 characters or a finite number is never echoed: the reply has `id: null`. Requests with such an id get `bad_request` |
| Reply size | a reply never exceeds a frame (65 536 bytes); a reply that would is replaced by an `internal` error (`plan-review` sizes its chunk so that it does not). Rejected input (an unknown op name, an over-long id) is never reflected in a reply |

## 8. Lifecycle summary

1. A session starts; the activation rules (section 2) hold; it binds `<id>.sock` (mode `0600`), then writes
   `<nonce>.json`.
2. A client reads the registry, checks the pid, connects, sends requests carrying the nonce.
3. On a session switch or branch the registry is rewritten with the new `sessionId` and a higher `generation`; requests that
   carry the old values get `stale_session` or `stale_generation` and the client reads the registry again.
4. When the `off` file appears, `enabled` disappears, the extension is disabled, or the session ends, everything is
   closed and removed (section 2). When the condition clears, a new listener starts with a **new nonce**.
5. A session that dies without cleaning up leaves its files; the next session to start removes entries whose pid is
   gone.

## 9. Conformance

`plugins/omp-session-control/tests/conformance/suite.ts` connects to a live session through its control directory and
checks the rules above. It never imports the implementation, so it runs against any implementation of the protocol:

```
bun plugins/omp-session-control/tests/conformance/suite.ts --dir <control dir> [--pid N]
    [--exclusive] [--idle-ms N] [--mutate] [--off-switch]
```

| Group | Needs | Checks |
|---|---|---|
| default | a live session | R1 to R5 directory (a real directory, not a symlink), registry, socket, pid, all checked before anything is connected: if one fails the run stops with S0 and attempts no connection and no mutation; P1 to P15 snapshot and every framing, version, id, nonce, op, args, staleness, name and size rule, abuse survival |
| `--exclusive` | no other client connected | L1 the 4-client limit |
| `--idle-ms N` | the session's idle timeout is about N ms | L2 idle close |
| `--mutate` | an idle session whose name may change | M1 abort on idle, M2 rename, M3 ordered mutations |
| `--off-switch` | permission to create `off` in the directory | O1 close on `off`, new nonce after removal |

Exit code 0 when every selected check passes, 1 when one fails, 2 when no single session is found. The suite reads registry files without following symlinks or blocking on special files, and its off-switch check creates `off` exclusively and removes only a marker it created.
