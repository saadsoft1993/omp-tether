# health/1

How a plugin tells `omp-services` what to watch. A plugin writes one small JSON file listing checks; the heartbeat
runner of `omp-services` (or any other implementation of this protocol) runs them, classifies each result, and pings a
dead-man URL with one verdict. The reference implementation is `plugins/omp-services`; the schema is
`plugins/omp-services/python/omp_services/schemas/health-1.schema.json`; `plugins/omp-services/tests/conformance/health_1_suite.py` checks an
implementation against this document.

Words in capitals (MUST, SHOULD, MAY) have their usual RFC 2119 meaning.

## 1. Scope and trust

- The writer of a drop-in and the runner are **one user on one machine**. A drop-in can make the runner run a command as
  that user, so it is held to the standard of a secret: nobody else may be able to write it.
- Nothing here is a network protocol. Checks look at loopback sockets, local files, local commands and a local
  service manager. A runner reaches other hosts only when its own configuration says so.
- A check result never contains a URL, a path, a command line or anything a command printed. Results say *what kind* of
  failure happened, in a short code. This is what lets a dashboard or a log show results without leaking.

## 2. The drop-in file

| Item | Rule |
|---|---|
| Directory | `$XDG_CONFIG_HOME/omp-services/health.d/` (default `~/.config/omp-services/health.d/`) |
| File name | `<plugin>.json`; `<plugin>` is 2 to 64 characters of `a-z`, `0-9` and `-`, starting and ending with a letter or digit. Files not ending in `.json` and files starting with `.` are ignored, so a writer can write a temporary file and rename it |
| Owner and mode | a regular file owned by the user that runs the runner; group and others MUST NOT be able to write it. A symbolic link is refused |
| Directory mode | owned by the same user, not writable by group or others. A directory that fails this disables **all** its drop-ins |
| Size | at most 64 KiB; at most 64 drop-ins are read |
| Encoding | UTF-8 JSON, one object |

A file that fails any rule is **not used**, and the runner MUST report it as a failing check (status `invalid`, check id
`dropin`, reason `dropin-invalid`, or `dropin-directory-unsafe` for the directory) instead of skipping it silently. The reasons themselves (which can quote a path or a field name from the file) are for the person at the terminal, not part of the result: a monitor that quietly ignores a broken file is worse than none.

## 3. The document

```json
{
  "protocol": "health/1",
  "plugin": "omp-example",
  "checks": [
    { "id": "api", "type": "url", "url": "http://127.0.0.1:8080/health" },
    { "id": "api-owner", "type": "uid-owner", "port": 8080 }
  ]
}
```

| Field | Rule |
|---|---|
| `protocol` | the string `health/1` |
| `plugin` | MUST equal the file name without `.json` |
| `checks` | 1 to 64 objects; `id` unique within the file |
| every check | `id` (`a-z 0-9 . _ -`, 1 to 64 characters, starts with a letter or digit), `type`, optional `severity`, optional `timeoutSeconds` (1 to 60) |
| unknown fields | refused, at every level, so that a typo cannot silently disable a check |

`severity` is `fail` (default) or `note`. A `note` check is run and reported, but never counts toward the verdict.

`timeoutSeconds` is the check's deadline. Without it the runner's default applies (10 s in the reference). A runner MUST
stop every check at its deadline, name resolution and directory walks included: a connection that sends a byte a second, a command that ignores signals and a
file system call that hangs all end at the deadline with a failing result (section 5).

## 4. Check types

### `url`

`{ "type": "url", "url": "http://127.0.0.1:8080/health", "expectStatus": [200], "freshness": {...} }`

One `GET`. No request body, no cookies, no proxy from the environment, **no redirect is ever followed** (a 3xx is just a
status). `https` verifies the certificate and the host name. The scheme is `http` or `https`, the URL has no user name,
password or fragment.

- A runner MUST refuse a host that is not loopback (`127.0.0.0/8`, `::1`, `localhost`) unless its configuration allows
  remote hosts: status `invalid`.
- `expectStatus`: accepted status codes. Default: any 2xx.
- `freshness`: `{ "jsonPointer": "/updatedAt", "maxAgeSeconds": 60, "format": "epoch-seconds" | "iso8601" }`. The
  response body (at most 64 KiB) is JSON; the pointer (RFC 6901) names a timestamp. Older than `maxAgeSeconds` is `stale`;
  more than 60 s in the future, or missing, unreadable or not a finite number (`NaN`, `Infinity`), is `failed`.

Results: nothing answered (refused, timeout, DNS, TLS handshake failure) `unreachable`; wrong status `failed`; too old
`stale`.

### `port`

`{ "type": "port", "host": "127.0.0.1", "port": 8080 }` A TCP connection succeeds (`host` defaults to `127.0.0.1`, same
loopback rule as `url`). Refused or timed out: `unreachable`.

### `uid-owner`

`{ "type": "uid-owner", "port": 8080, "uid": "self" }` The TCP listener on `port` (any address) is owned by `uid`
(`self`, the default, or a number). This is how a plugin proves that the thing answering on its port is *its own* process
and not another user's.

- Nothing listening: `unreachable`. Owned by another uid: `failed`.
- A listener exists but the runner cannot see who owns it, or the platform tool is missing: `failed`. Never `ok`.

### `serve-route`

`{ "type": "serve-route", "port": 443, "path": "/", "target": "http://127.0.0.1:8080" }` A Tailscale Serve route exists
that forwards `https://<this machine>:<port><path>` to `target` (any host name; trailing `/` ignored). The runner reads
`tailscale serve status --json`.

- The tool is missing or cannot start: `failed`. It does not answer (non-zero exit, timeout): `unreachable`.
- The route is absent or points elsewhere: **`not-on-relay`**. This is the case where the service is up locally but not
  reachable through the relay, which is a different problem with a different cure from `unreachable`.

### `command`

`{ "type": "command", "argv": ["/usr/bin/true"], "expectExit": 0, "expectOutputContains": "ok" }`

- `argv[0]` MUST be an absolute path: otherwise `invalid`. There is no shell, no PATH lookup and no expansion.
- The environment is small and fixed (`PATH=/usr/bin:/bin:/usr/sbin:/sbin`, `HOME`, `LANG=C`); nothing of the runner's
  environment reaches the command. Standard input is empty. At most 64 KiB of output is kept.
- It runs in its own process group; at the deadline the whole group is killed. Deadline, not starting, wrong exit status
  or missing output text are all `failed`.

### `file-age`

`{ "type": "file-age", "path": "/var/backups/db/*.dump", "maxAgeSeconds": 93600 }` The newest regular file matching the
absolute path or glob (`*`, `?`, `[...]`, at most 10 000 matches looked at) is not older than `maxAgeSeconds`. No match:
`failed`. Too old: `stale`.

### `watchdog`

`{ "type": "watchdog", "service": "hub", "restart": { "maxRestarts": 3, "windowSeconds": 3600, "cooldownSeconds": 60 } }`

`service` names a service the **same plugin** declares in its `services.d/<plugin>.json` (see
[`services-1.md`](services-1.md)); a plugin cannot watch or restart another plugin's service, and an unknown name is
`invalid`.

- Running: `ok`.
- Not running and no `restart` object: `failed` (report only). An empty object `{}` means "restart, with the defaults".
- With `restart`, and only when the runner is the heartbeat (a diagnostic run never acts): restart through the service
  manager, then wait up to `graceSeconds` (default 10) for it to run. Success is `ok` (reason `restarted`).
- **Bounded**: at most `maxRestarts` (default 3) restarts per rolling `windowSeconds` (default 3600), and at least
  `cooldownSeconds` (default 60) between two. A restart that fails still counts. At the limit the result is `failed`
  (`restart-limit`), and nothing is restarted until the window passes.
- A service flagged `protected` is **never** restarted by a watchdog (`failed`, reason `protected-not-restarted`); a person restarts it. There is no setting to override this. A service is protected if its declaration says so **or** the installed state recorded it as protected at install time; when that state is missing, a dangling link, unreadable, or has no record of the service, nothing is restarted (`protection-state-unreadable`). A service that the manager reports as activating, reloading or deactivating is neither up nor down: it is reported `failed`, reason `transitional-state`, and not restarted. A service manager that cannot be asked yields `failed`, reason `manager-query-failed`.
- A service that is not loaded at all is not loaded by the watchdog (somebody unloaded it on purpose): `failed`. A unit the runner did not install itself (no marker, not recorded) is not restarted either: `failed`, reason `not-managed`.
- An unreadable restart history fails closed: no restart (`restart-state-unreadable`), instead of resetting the budget. The history is updated under a lock, so two runners cannot spend one allowance twice.

## 5. Results

Each check yields `{ "id", "status", "reason" }` (a runner MAY add `plugin`, `type`, `severity`, `ms`):

| `status` | Meaning | Cure usually |
|---|---|---|
| `ok` | the check passed | none |
| `unreachable` | nothing answered: refused, timeout, no listener, tool not answering | start the service, check the port |
| `stale` | it answered, but data or file is older than allowed | it is up but not doing its work |
| `not-on-relay` | works locally, but the forwarding route is missing | fix the Serve route |
| `failed` | anything else: wrong status, wrong owner, non-zero exit, deadline, unreadable answer | read the reason |
| `invalid` | the check cannot run: bad definition, a policy refusal, an unusable drop-in | fix the file |

`reason` is a short code made of words, digits and hyphens (`connection-refused`, `status-503`, `exit-7`,
`older-than-60s`, `owner-mismatch`). It is for people; implementations MAY use other codes, and consumers MUST NOT parse
it beyond showing it. It MUST NOT contain the URL, a path, a command line or command output.

A check **counts** when its status is not `ok` and its `severity` is `fail`. The verdict is "healthy" when no check
counts. An `invalid` drop-in always counts.

## 6. The heartbeat verdict (reference runner)

Every `intervalSeconds` (default 300) the runner runs every check, one after the other, all within a cycle deadline
(default 120 s; checks not reached count as `failed`, reason `cycle-deadline`), then sends one request:

| Verdict | Request |
|---|---|
| healthy | `GET <URL>` |
| not healthy | `GET <URL>/fail` (appended to the URL's path; a query string is kept) |

- The URL comes from a `0600` file in a `0700` directory, is `https://` without credentials, and is checked for
  certificate and host name. The request has no body; a redirect is never followed and counts as a failed ping.
- The URL is a secret: it is never logged, never written to the status record, never put in an error message.
- With **no checks at all** the verdict is "not healthy" (a single result `omp-services/checks`, status `failed`, reason `no-health-checks-configured`), unless the runner is configured to accept that (`heartbeat.allowNoChecks`, off by default): a missing drop-in must not look like a green light.
- The runner records `heartbeat-status.json` (state directory): the time, each result and the ping outcome, without the
  URL. A reader MUST treat a record older than three intervals as stale.

## 7. Versioning

`health/1` changes only by this document. New check types or fields require `health/2`; a runner that meets a
`protocol` it does not know MUST report the drop-in as `invalid` rather than guess.

## 8. Conformance

An implementation is checked through one command contract: `<impl> <drop-in file>` reads one drop-in, runs every check
once **without restart actions**, prints a JSON array of results (`id`, `status`, `reason`) in check order and exits

| Exit | Meaning |
|---|---|
| 0 | no result counts |
| 1 | at least one result counts |
| 2 | the drop-in is invalid; the output is `{"invalid": ["reason", ...]}` |

The reference: `omp-services health --json --no-actions --file <drop-in file>`. Run the suite:

```
python3 plugins/omp-services/tests/conformance/health_1_suite.py --impl "bin/omp-services health --json --no-actions --file"
```

The suite builds its own fixtures at run time (local servers on random ports, scripts, a stand-in `tailscale` first on
`PATH`), needs nothing but Python 3.10, and runs the implementation with a scratch `HOME`. It checks, among other
things: every type's ok and failing outcomes and their classes; that a redirect is never followed; that a slow peer and
a command that ignores its deadline are cut off in time and the command's whole process group dies; that results never
contain a URL, a path or command output; that a note-only failure does not count; that each kind of unusable drop-in is
refused (writable by others, wrong plugin name, unknown field, unknown type, duplicate ids, symbolic link).
`--selftest` runs the suite against deliberately broken implementations and passes only if it rejects each.
