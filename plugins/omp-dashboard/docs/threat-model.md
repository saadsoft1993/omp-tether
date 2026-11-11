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

## The `both` sign-in

`auth.adapter: "both"` serves the local address and Tailscale Serve from one hub. It adds no rule of its own: **every request is judged by exactly one of the two rule sets above**, never by both and never by a mixture, and the pairing cookie is checked after that on both.

| Request | Judged by | What it needs |
|---|---|---|
| carries **any** sign of a proxy: a header named `Tailscale-*` or `X-Forwarded-*`, or `Forwarded`, `Via`, `X-Real-IP` (any letter case; looked for by the names the request carries, so a header a proxy adds later counts too) | exactly the `tailscale-serve` rules | the owner's `Tailscale-User-Login`, this machine's name and Serve's port as `Host`; no `Tailscale-Funnel-Request`; no repeated `Host`, login or Funnel header; the owner known (else 503) |
| carries **none**, and its `Host` is `127.0.0.1:<port>`, `localhost:<port>`, `[::1]:<port>` or an `auth.loopbackOnly.allowedHosts` entry | exactly the `loopback-only` rules | nothing more than the cookie |
| carries none and any other `Host` (the tailnet name, a rebound name, no port, none) | refused (403) | |

What follows the request follows the same choice: the `Origin` a POST must carry is that of the way the request came, and the pairing cookie is `Secure` only for a request that came through Serve. The classification reads the request only, so a request cannot choose the rule set that suits it better: adding a proxy header moves it to the stricter-for-a-local-browser set (the tailnet rules, which a loopback `Host` never satisfies), and leaving every one off moves it to the local set (which a tailnet `Host` never satisfies).

- **A request that looks direct but carries a forged `Tailscale-User-Login`** is a proxied request. It needs the owner's login *and* this machine's tailnet name as `Host`, so a browser or a web page on this machine cannot use it; a program on this machine can write those headers (see below).
- **A request with no proxy header cannot use the tailnet rules**, whatever `Host` it names.
- **DNS rebinding** (a page on another site whose name resolves to `127.0.0.1` or to the tailnet address) is stopped by the `Host` check on both sides: the page's own name is neither a loopback host nor this machine's tailnet name.
- **CSRF**: the cookie is `SameSite=Strict`, and every POST needs the `X-Omp-Dashboard` header (a CORS preflight, which this server answers 405, never approves) and an `Origin` equal to the origin of its own way in. A page on another port of `localhost` is the same *site* (ports are not part of it) and so receives the cookie, but it cannot read an answer (no CORS headers) and cannot pass the POST gates.
- **A same-uid process on this machine** is trusted completely and out of scope (it can write a pairing token into the store it already writes, read a browser profile's cookies, and write any header). What the two paths add for it: nothing beyond the cookie it needs on each. It cannot turn a local cookie into the tailnet path or the reverse, see the next item.
- **A device is let in only the way it was paired.** The pairing store is shared, but each device is recorded with the way it came (`access`: `local` or `tailnet`, see `pairing-store/1`), and the hub accepts a cookie only on a request of that way. The reason is that the local cookie is the weak one: it is scoped by host and not by port, is not `Secure`, and so the browser sends it to every other service on `127.0.0.1` and `localhost` and to any program that holds the port while the hub is down. If the same value were valid through Serve, a program that got it and wrote `Tailscale-User-Login` and the tailnet `Host` itself would control your sessions through the tailnet path. With the binding it gets a 401 there, whatever headers it forges; and a cookie paired through Serve (`Secure`, TLS only) is a 401 on the local address. A device with no recorded way in (paired by something else: `omp-private-relay`'s own `pair`, an older hub) is refused on both and has to be paired again (`doctor` counts them).
- **The local device is also short-lived**: its cookie and its record last `pairing.localMaxAgeDays` (default 7, never more than `pairing.maxAgeDays`), the tailnet one `pairing.maxAgeDays` (default 90). `omp-private-relay` in shared mode applies neither the binding nor the age: it reads the same store by key and accepts a device of either way on its own addresses, so what this item guarantees is the hub's, not the relay's.
- **Another account on this machine** can reach the port (as in `loopback-only`), can forge every header, and without a cookie gets 401 on every API call. A local cookie replayed from another port works on the local path only, as in `loopback-only`.
- **A cookie belongs to the address its browser used** (`127.0.0.1`, `localhost` and the tailnet name are separate cookie jars), so a browser pairs once per address; one browser on two addresses is two devices. `revoke` ends a device everywhere it is let in.
- **A pairing link is not tied to an address**: the token is redeemed once, on whichever address the browser opens first, and the device is of that address's way. It is single-use and expires in 5 minutes on either.
- **A forward that keeps the hub's port number cannot be told apart from a direct request, and Open and Watch are affected.** With a `local` relay the hub offers its link at `127.0.0.1` or `localhost` when the `Host` is exactly that name at the hub's own port, and a same-port `ssh -L`, Docker mapping or VS Code forward sends exactly that. If the browser is on another machine, the link then names *that machine's* loopback: a link that does not work, or navigation to an unrelated local listener, which would receive the host-scoped cookie and could read the room-bearing fragment. The relay proof (`relay-check/1`) shows that the listener on the hub's machine is your relay, not where the browser's address leads. A different name, `[::1]` or another port is refused (409 `relay_address`), which does not cover the same-port case. Forward the relay's guest port too, to the same relay, before using Open and Watch through a forward, or do not use them there.
- **Port forwards and TCP proxies are served as direct requests.** `ssh -L`, a Docker port mapping, a VS Code forwarded port and `tailscale serve --tcp` carry no HTTP header of their own and keep the `Host` the browser used, so a request through one with a loopback `Host` (`127.0.0.1:<port>`, `localhost:<port>`, or an `auth.loopbackOnly.allowedHosts` entry for a different local port) has no sign of a proxy and is judged by the local rules, and a device paired through it is a local device. The `Host` check and the cookie are what protect that path, not a header. A forward that exposes the hub to other people (a Docker mapping on `0.0.0.0`, a VS Code port made public, `tailscale serve --tcp` to the tailnet) gives them the local path: they still need a paired local cookie (a pairing link is made only by you, on this machine) and a `Host` the check accepts, but nothing else stands in front of it, so do not forward the hub's port to other people. Tailscale Serve's HTTPS mode (`tailscale serve --https`) is the only forward that adds the headers the tailnet path depends on.
- **A reverse proxy you add yourself** that rewrites `Host` to the hub's loopback address and adds none of the headers above looks like a local browser and is judged by the local rules, which is the same position `loopback-only` is in. Do not put one in front of this hub; Tailscale Serve is the supported way in.
- **A local browser behind a local proxy** that adds `Via`, `X-Forwarded-*` or the like is judged as proxied and is refused on the local address (it would need the owner's login). Use the tailnet address, or remove the proxy for `127.0.0.1`.
- **Tailscale off**: the tailnet rules answer 503 while the owner and the machine's names are unknown, and the local address is not affected. That is a statement about identity, not about the transport: with `auth.tailscale.owner` and `publicHost` in the config, or within a minute of a successful lookup, the hub knows both and keeps judging proxied requests whether Tailscale is up or not (a proxied request cannot arrive while it is down, and a forged one meets the same rules as ever). `pair --tailnet` therefore asks Tailscale for its own state before it prints a Serve link. `doctor` reports an off, missing or logged-out Tailscale and an absent route as warnings for the tailnet path; a Serve route that points somewhere else and Funnel being on are failures in every sign-in, and the health drop-in keeps them failing through its own `serve-integrity` check while the route check itself is a note.
- **Open and Watch** go through the bundled relay, which admits **one** way (its own sign-in is `token` for direct browsers, or `tailscale-serve` for browsers through Serve; the relay applies it before it looks at the cookie), named by `relay.admits`. A link is handed out only to browsers of that way, and only when its web address is one of this machine's own loopback names (or the tailnet one) and it routes through your relay; the other way gets a message and no link. A direct browser is sent to the relay at the host name it used (`127.0.0.1` or `localhost`), so the cookie it holds is the one the relay sees; through a port forward, `[::1]` or another name no link is made, because the relay's address would then be the browser machine's own, and the relay proof (`relay-check/1`) only shows that the listener on the hub machine's loopback is your relay. This release does not make one relay serve both ways: that needs a change in `omp-private-relay`.

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
a test to fail; it is not a list of every guard. For `both`: every proxy-sign header variant (case, prefix, duplicates, mixed with a loopback or a tailnet `Host`, IPv6, `allowedHosts`)
against the classification, the cookie's `Secure` flag and the POST `Origin` per path, pairing on both addresses, forged headers over a real socket, Tailscale off, and a real run on macOS with a real Serve route
(local address, tailnet name, route removed, Tailscale stopped) and a real browser paired on both addresses.

Not tested: a real tailnet identity that is not the owner arriving through Serve (the test tailnet had one user, so that refusal is proven only with forged headers on loopback); Serve on Linux;
herdr on Linux (herdr 0.9.5 on macOS was tested for real); Safari (the `Secure` rule for the local address rests on how browsers treat a `Secure` cookie from plain `http://127.0.0.1`, not on a test); Force kill of a process tree that a real omp left running, beyond real processes the tests started; Linux beyond the container runs
recorded in the release evidence.
