# Threat model: omp-dashboard

## What it protects

A person's own omp sessions: reading which exist, opening one with full control (a link is as good as the keyboard), stopping its turn, renaming it,
ending it, starting another, and what is on the page. Plus the secrets that guard them: the browsers' cookies, pairing tokens, the relay's check key and
the ntfy topic.

## Who is trusted

The user account that runs the hub, and root. **A process that runs as that account is trusted completely**: it can already read the pairing store,
the config, the control sockets and every session, and send signals to omp. Nothing here tries to stop it, and nothing claims to.

## Who is not, and what stops them

| Attacker | Capability | What stops them | Residual |
|---|---|---|---|
| A web page you visit | cross-site requests, DNS rebinding | pairing cookie is `SameSite=Strict` and `HttpOnly`; `Host` must name the hub's own address (rebinding fails); every POST needs a custom header (a CORS preflight the hub never approves) and the hub's own `Origin`; strict CSP, no third-party asset, `frame-ancestors 'none'` | a page open in a browser that is paired AND on the same origin |
| Another device on the network or tailnet | connect to a port | the hub binds loopback only and refuses any other address; with `tailscale-serve` the owner's login and this machine's name are required, Funnel and tagged devices are refused | a Tailscale bug or a mistaken Serve rule (`doctor` checks the route and Funnel) |
| Another account on the same machine | connect to the loopback port; bind the port while the hub is down | every API call needs a paired cookie (401); `pair` creates no token and a link is never handed out unless the port is held by your account (`lsof`, `/proc/net/tcp`; "cannot see" is not "yours"); links need `relay-check/1`'s HMAC proof from the relay | a browser that is already open can still talk to whatever holds a port; the loopback cookie is sent to every port of `127.0.0.1` (cookies are not port-scoped); a local program can forge `Host` and the Tailscale headers, which is why the cookie is the credential. A machine that several people share is not supported |
| Someone who finds the files or logs | read them | stores hold SHA-256 hashes only; the audit log holds ids and outcomes, never names, paths, prompts or links; the ntfy topic is a `0600` file and never in the page, config or logs | the audit log shows that sessions exist and when they were acted on |
| A lost or stolen browser | holds a cookie | `revoke` removes it; the relay cuts its open pages within seconds | a stolen browser is paired until you revoke it |
| A process that reuses a pid | be signalled by Kill | Kill checks the process still belongs to you, is an `omp` process, started no later than the room (+2 s), and that the name was typed; Force kill only within 10 minutes of a Kill that failed, for the same process | a same-uid process named `omp` with an old start time |
| A directory another account can write | swap a file or a link | every trusted path is walked lexically: links followed and checked, every directory yours or root's and not group/other-writable (sticky excepted), ACLs checked; files are read through one descriptor | root; a file system without ownership |
| The ntfy server's operator | read alerts | alerts name the project folder, the model and a state only; `https` only; topic secret; off until set | a third party sees those words |

## Out of scope

These are not defended against, and nothing here claims otherwise.

- **Root.** It can read every file, every process and every socket, and change any of them.
- **Other processes that run as you.** They can already read the pairing store, the config, the control sockets and the cookie of every browser
  profile you own, and signal omp; the dashboard adds nothing they cannot do. This includes a program that connects to the hub's port and writes the sign-in
  headers itself (the paired-browser cookie is what stops a different account, not a same-account program), and one that guesses pairing tokens: ten wrong guesses lock
  pairing for a few minutes for everybody on the machine, because every local client is `127.0.0.1`; a link that is on file and unexpired bypasses that
  limit, so the owner can still pair while it is being abused, but a program that fills the limit can still stop a *mistyped* link from being retried.
- **Shared machines.** A machine several people log in to is not supported: another account can reach a loopback port, bind a port while the hub is down,
  and read what the process table and the network table show.
- **Anyone who can already write inside a trusted directory.** The trust walk refuses a directory or file others can write, but it checks at the time it looks:
  an account that is allowed to change a directory you trust (root, or an owner you added to a group you declared private) is inside the boundary.
  On Linux a group-writable directory is accepted only when `/etc/group` and `/etc/passwd` show that the group has no member but you or root; a group that
  a directory service resolves, and any group on macOS, is refused.
- **Browser extensions and malware in a paired browser.** The cookie is `HttpOnly`, but a page, an extension or a debugger in that browser acts as
  the browser. `Max-Age` and `pairing.maxAgeDays` (default 90) limit how long a lost browser stays paired, and `revoke` ends it at once.
- **The tailnet transport.** With `tailscale-serve` the hub trusts what Tailscale Serve says (the caller's login, the host the caller used) because the
  hub listens on loopback only and Serve is the only thing in front. A flaw in Tailscale, a mistaken Serve rule or a Serve version that does not strip a
  caller's own `Tailscale-User-Login` is theirs: the cookie is what still stands. A real Serve (Tailscale 1.104.5, macOS) was tested: it replaced a
  `Tailscale-User-Login` the caller sent (once or twice) with the real owner's login, removed a caller-sent `Tailscale-Funnel-Request`, and replaced a caller-sent
  `X-Forwarded-For`; the hub also refuses a request that carries the headers it reads twice.
- **Pid reuse between a check and a signal.** Kill and Force kill look at a process's owner, command and start time just before signalling; a pidfd would close the
  remaining gap but is not available on macOS, so the gap (microseconds) is documented rather than closed.
- **The contents of a session.** The relay passes sealed messages; the hub never sees them.
- **The ntfy server's operator,** as the table above says: a third party that sees the words an alert holds.

## What the pairing store's safety rests on

The hub judges the store before every use: the directories above it, the store directory and `devices.json`, `pending.json` and `.lock` (owner, mode, links,
ACLs). The verdict is reused only while the store directory and its files are unchanged (inode, mode, owner, modification and change times; an ACL change moves
the change time) and the directories above keep their identity and mode, for at most five seconds. **An ACL added to a directory *above* the store is noticed
within those five seconds, not at once.** A replaced or re-permissioned store file is judged again before it is read.

`omp-private-relay` in shared mode reads the same store, and what it enforces is narrower: the mode and owner of the store directory and its files. It does not
walk the directories above the store and it does not inspect ACLs. A store that this hub refuses can therefore still be accepted by the relay, so keep the store
under a directory only you can change, and run `omp-dashboard doctor`. The 90-day age limit is applied by this hub only; the relay does not apply it, and
`revoke` ends a browser's open relay pages only because the relay re-reads the store.

## What happens to a session link

Open and Watch fetch a link from `omp collab link` at click time and open it in a new browser tab. That link carries the room's bearer material in its
fragment. The hub never stores it and never logs it, but the browser does: the new tab's address, and with it the link, is in that browser's history, in its
session restore data and in anything that syncs them. Whoever can read the profile of a paired browser can read links the browser opened. Close the room
(end the session, or restart collab) to retire one.

## What is audited

One line per action that changes something or hands out access: `link` (with the outcome: handed out, or refused and why), `start`, `stop`, `rename`, `kill`,
`force-kill`, `pair` (paired, refused, malformed, rate limited), `pair-token` and `revoke` from the command line (refusals too), `start` (including refusals before a path is looked at) and `alerts-test`. A request that cannot be told apart from noise (a bad target, an oversized header block, a missing custom header, no cookie) is answered and not audited. Reading (the session list, the machine banner, the Alerts and Relay pages) is not audited. Each line holds the time, the kind, an id, an outcome and
the first eight characters of a browser's id, never a name, a path, a prompt, a link or a token. A failure to write a line is logged and does not change the answer.

## What logs never contain

Cookies, tokens, links, topics, keys, session names, prompts, project paths. They hold kinds of events, ids, outcomes and error categories.

## Test coverage and what is not tested

No third-party audit. Covered by tests: the sign-in adapters (forged and missing headers, wrong host or port, Funnel, unknown owner, non-loopback
binds); pairing (conformance suite, cross-check with the relay's implementation, ancestry of the store); the HTTP layer (bounded headers, bodies and
time, fixed errors, origin and custom-header gates, failure budget); the control client against hostile or broken sockets; the relay proof against
look-alikes and oversized or multi-byte answers; Kill and Force kill against real processes and against two live omp sessions; alerts and the audit log;
Start path validation and the typed text; the page's pins; the file-trust walk. A mutation check switches off a list of guards one at a time and requires
a test to fail; it is not a list of every guard.

Not tested: a real tailnet identity that is not the owner arriving through Serve (the test tailnet had one user, so that refusal is proven only with forged headers on loopback); Serve on Linux;
herdr on Linux (herdr 0.9.5 on macOS was tested for real); Force kill of a process tree that a real omp left running, beyond real processes the tests started; Linux beyond the container runs
recorded in the release evidence.
