# Threat model: omp-private-relay

For people deciding whether to run it, and for people reviewing it. Plain language. The two contracts it implements are in
[`pairing-store-1.md`](../../../docs/protocol/pairing-store-1.md) and [`relay-check-1.md`](../../../docs/protocol/relay-check-1.md).

Status: the design comes from a relay that had an independent security review (findings fixed, each covered by a test).
This plugin as generalised has had **independent security reviews**; every finding was fixed or answered with evidence, and
`make test-mutation` reverts each fix in a copy and requires the designated regression test to fail (it covers the guards a
test can observe; a second layer that backs up another is not mutated alone). It has had no third-party audit and no review of
the fixes themselves. The
"accepted" items below are the author's judgement. Treat early releases as beta software.

## What it is, and what it gives an attacker

A relay for omp's Collab feature. omp sessions on your machine connect to it (the *host listener*); browsers connect to it
(the *guest listener*) and download a patched copy of omp's browser client from it. A Collab link carries a room id and an
encryption key in its fragment; the relay passes sealed frames between the session and the browsers and **never sees
plaintext**. A browser holding a full-control link can drive the session (prompts, tool approvals, subagents), and omp
sessions run commands as you. So whoever gets a **full-control room link and passes the relay's checks** controls your
machine as you. That is the thing this design protects.

What the relay itself can see, and an attacker who controls the relay's process (not just its port) could do: connection
metadata (which room ids exist, how many browsers, sizes and timing of sealed frames); it can drop or delay frames; it
cannot read them or forge them without the room key. The relay holds no room keys.

## Who is trusted

**The user that runs the relay, and root.** Nobody else. A browser is trusted only after the user pairs it with a one-time
link, and only until the user revokes it. On the `tailscale-serve` adapter, a request must also carry the owner's
Tailscale login, set by Tailscale Serve.

## Accepted risk: a process running as you

Any program you run (an editor plugin, a build script, a compromised dependency) runs as you. It can read your pairing
store (hashes only: that does not let it in), the check key, your omp sessions and credentials, and it can send signals to
omp. It can also open the relay's host listener, ask for `/stats`, pair a browser (it can create a pairing token, as
`omp-private-relay pair` does) or answer the listener proof. That adds convenience for such a process and little else: it
could already do more by acting on your sessions directly. **If you do not trust the programs running under your account,
do not run this.**

## Threats and what answers them

| Threat | Answer | Residual |
|---|---|---|
| A web page you visit reaches the relay (DNS rebinding, a cross-site request, a hostile script) | Every guest request checks `Host` (exact loopback name and port, or your machine's name) and `Origin` (exact, on upgrades; absent or exact on plain requests), refuses `Sec-Fetch-Site: cross-site`, and needs the pairing cookie (`HttpOnly`, `SameSite=Strict`). The host listener refuses any request that carries an `Origin` (a browser always sends one on a WebSocket upgrade), any proxy header, and any `Host` but its own loopback name | A same-origin script is not blocked: the web client's Content-Security-Policy keeps scripts to the page's own files and a hash of one inline script, and the page connects only to its own address |
| Another device on your network or tailnet reaches the relay | Both listeners bind `127.0.0.1` only, and there is no setting that changes that (an unknown key in the config is refused). Reaching it from elsewhere takes a tunnel you create; with `tailscale-serve` the request must name **you** as owner, this machine as host and carry the matching Origin, and Funnel requests are refused. `doctor` warns when Funnel is on for the route | A tunnel you create (SSH forward, Serve) is as strong as its own login. Tailscale Serve is trusted to set `Tailscale-User-Login` and to overwrite a caller's value; this was observed on macOS, and is **unverified on Linux** |
| An unpaired browser with a valid-looking request | The pairing cookie must hash to a key in the pairing store; the store is read on every request. The cookie is 256 bits and only its hash is stored. Redemption of a pairing link is limited to 10 failures a minute, needs a custom request header and a matching Origin, and a link works once, for five minutes | A link that is shown on a screen or sent in a chat is a bearer secret for those five minutes. Pairing links travel in the URL fragment, so they are not sent to any server or written to the relay's output |
| A lost or stolen browser, or a copied cookie | `revoke` removes it. Open pages are re-checked every `limits.recheckMs` (10 s default, 30 s at most) and closed with 4401; the relay forwards nothing from a connection after it started closing, and cuts one that ignores the close frame after 5 s | A copied cookie works until you revoke it (the cookie lasts a year). Anyone with the cookie **and** a room link can use the session. **Cookies are not port-scoped**: on the `token` adapter the cookie is sent to every port of `127.0.0.1` (and of `localhost`), so any other local service the browser visits (a dev server, another account's process) receives it, and a local program can replay it with forged `Host` and `Origin` headers, which are only strings on a loopback port. `HttpOnly` and `SameSite` do not change that. Under Serve the cookie is `Secure` and travels only to the machine's name, but is still not port-scoped |
| Another account on the machine takes the relay's port while it is down (a crash, a restart, login order) and poses as it | `relay-check/1`: at every start the relay writes a fresh random key that only your account can read; `omp-private-relay check`, `doctor` and the health drop-in ask each listener a fresh challenge and accept only the right answer. `status` believes the relay's counts only when they carry a MAC over exactly those bytes under a fresh nonce (not merely because an earlier request proved the listener), reads at most 64 KiB (65,536 bytes, counted as bytes before a chunk is kept), follows no redirect, and shows only numbers; `doctor` also reports who owns each listening socket where the platform shows it, and "cannot see" counts as unknown, never as yours | A browser page that is **already open** keeps talking to whatever now holds the port: through Serve, a page loaded just before the relay died may talk to an impostor, which could see its cookie. A link fetched just before the relay died stays valid for its room. **`pair` refuses to make a link unless the guest port proves to be this relay, but nothing makes a browser check before it opens a session link (omp prints those): the proof is detection (`doctor`, `check`, the health drop-in) and a gate on `pair`, not prevention for ordinary navigation, and there is a gap between the check and the click.** Check before you hand a link to a browser; do not leave pages open across a relay restart you did not expect |
| Another account on the machine uses the host listener | It can open rooms with random ids (up to `limits.maxRooms`, then the next real host gets 4029), and open sockets that stream large frames. It cannot read content (sealed) or join your rooms without the room key and a paired cookie | Squatting and denial of service by another local account; the relay's availability is **not** protected from other local accounts. This is one of the accepted limitations (see also the cookie, the listener proof and the forged owner header, above and below); shared machines are unsupported |
| A local process forges the Tailscale owner header | Nothing stops it from sending the header: it is a plain header on a loopback port. It narrows who may *ask*; the pairing cookie is the credential. With the forged header a local process can reach the pairing page and the listener proof, and spend the pairing failure budget (10 failures a minute **in total, not per caller**, counted together with attempts still in flight), which blocks a real pairing for a minute at a time, and a caller that keeps failing can keep it blocked | A repeatable local denial of service of pairing; no access. The lookup of the owner runs in the background and never blocks the relay | 
| Another account reads `/stats`, `/healthz` or the logs | `/stats` and `/healthz` are on the host listener, loopback only, and hold counts and caps only: no owner, machine name, paths or room ids. Logs hold connection metadata (listener, role, 6 characters of a room id, peer number, 8 characters of a cookie hash, close code, kind of refusal, and for a refused request a plain path of at most 64 characters, or `(other)`) and never a cookie, token, link, key or content | Other accounts can see *that* you have rooms open, and how many browsers, through `/stats`. The log file belongs to the service manager: in a scratch run launchd created it mode 0644 although the unit set `Umask 63`, so keep the log in a directory only you can enter (or have the unit create the file 0600) and do not rely on the umask alone. The log holds no secret, only the metadata listed |
| A hostile or broken client crashes the relay or exhausts memory | Frame caps (`maxHostFrame`, `maxGuestFrame`), a queue cap per connection and across the relay with a forced cut (4008), a cap on guests per room, on rooms and a hard cap on open WebSockets (`limits.maxConnections`; the upgrade is refused before a socket exists; plain HTTP connections are not counted), a restart grace of 60 s by default and at most 120 s, WebSocket pings and idle timeout, Bun's development error pages off, every handler error answered with a plain 500 and logged by class name only | A trusted local process can still keep many connections busy |
| A path trick reads files outside the web client (`..`, encoded dots, dot files, symlinks) | Decoded names with `.`, `..` or a leading dot are refused; the resolved real path must stay inside the real web directory; only regular files are served; a malformed percent sequence gets 400 | A same-user attacker can put files in the web directory; they could do worse anyway |
| The pairing store is tampered with, replaced by a link, or opened to others | The reader refuses (nobody is paired, open pages are closed) when the directory or `devices.json` is a link, not a regular file, owned by someone else, or readable by others, when it is too large or not a JSON object, and never repairs it. It opens with `O_NOFOLLOW` and checks the open file, and a FIFO is refused without blocking. The writer refuses to overwrite what it cannot parse | A same-user attacker can write the store (and add themselves). Accepted |
| A lost update or a torn write in the pairing store | One writer at a time (`flock` on `.lock`), a temporary file renamed over the target, so readers never see a partial file; a spent token is removed before its session is created | `flock` over a network file system is not reliable: keep the directory on a local disk |
| Another account replaces the web client on disk (a directory it can write is on the path of `web.dir`, or on the path a symbolic link in it leads to) | The path is walked component by component with `lstat`, following links and `..` as the kernel does, so the ancestors of every link target are checked too. Every directory passed through or led into belongs to you or root and is not writable by others (sticky directories excepted); a directory writable by a group is refused unless that group is shown to have no member but you or root (primary members from `/etc/passwd`, listed members from `/etc/group`, all resolved; a group the files do not describe is refused) and **always refused on macOS**; every link belongs to you or root; ACLs are inspected (`ls -le`; `getfacl`, required when `ls` shows an ACL) and an ACL that cannot be inspected is not trusted. The relay serves from the resolved directory only while the approval holds (re-checked every 5 s, and its identity on every request), answers an unsafe tree with a fixed page that opens nothing in it, and `doctor` fails; installs refuse an unsafe parent; directories the plugin creates are 0700 whatever the umask; a configured key directory that is open to others is refused, not chmod-ed | Up to 5 s between a change by someone who can already change your directories and its refusal. Groups kept by a directory service are refused, not resolved. See "Out of scope" below |
| A hostile download replaces the web client | The archive's sha256 and its content hash are pinned inside the plugin (`web-builds.json`, part of what you install) and checked before a file is written; the extractor accepts only regular files with plain names, no links, devices, dot files or `..`, with size limits; downloads must be `https` (or loopback `http`), each redirect hop is checked before it is followed, and the install is serialised by an advisory `flock` held on a validated descriptor for the whole install (the lock file is never unlinked or recreated, and a dead holder releases it with its process) and swaps a staged directory in with two renames (the install is recorded first, under unpredictable names it creates exclusively, so a crash between them is recovered from that record, which names exactly the directories to restore or remove; a record that is damaged or not ours stops the next install instead of being skipped, and no other path beside the target is ever deleted; there is a short window in which the relay answers 503 for the web client). `doctor` recomputes the hash of what is installed | You trust the plugin release you installed (pin it to a commit) and omp's build of the client. A mirror (`OMP_PRIVATE_RELAY_ASSET_BASE`) cannot supply different bytes, only the same ones |
| The patched web client is a modified build of upstream code | The patch is three small changes and is in the repository; the build is reproducible (same tag, same Bun, same content hash), the release job compares a fresh build with the pinned hash, and `rebuild-web` lets you build your own and compare. omp's third-party analytics script is removed, and the Content-Security-Policy would block it anyway (`default-src 'none'`) | The bundle contains React, katex and lucide-react (notices in `NOTICE`); their code is not audited here. The patched files can differ between omp minors, which is why each minor has its own build and patch |
| The config file, or an environment variable, turns a safe setting into an unsafe one | The file is opened once and checked on the open descriptor (a link, FIFO or another owner is refused), must be a regular file of yours that group and others cannot write, and is refused when its directory is writable by others without the sticky bit; `init` validates the whole configuration before it runs the omp that the file names; unknown keys, wrong types and out-of-range values are refused with every problem listed; the schema has no bind address, no `--no-auth`, no way to switch the pairing check off | An environment variable set by something that launches the relay (a directory-based environment tool) can change ports and paths. Any change still goes through the same validation |
| The relay runs on an old Bun, or the wrong program is run as Bun | It refuses to start below Bun 1.4.4 (the version collab-web's build needs); `BUN_BE_BUN` is set only on the one command that runs a script, and is removed for any child that runs omp itself | An omp upgrade may embed another Bun; `doctor` prints the version the relay runs on |
| Secrets in the relay's output, in files it writes, or in what `status` and `doctor` print | None of them contains a cookie, token, key, link or content, with one deliberate exception: `omp-private-relay pair` prints the one-time pairing link to the terminal (it is the command's purpose; do not run it into a log). `status --json` and `doctor` print paths and counts, not secrets; the pairing and check directories are `0700` with `0600` files | A person pasting `devices.json` (hashes) somewhere does not give access, but pasting a pairing link or a Collab link does |
| Supply chain: this plugin | No dependencies, no install scripts (omp runs none for marketplace installs), no network access except the one explicit download in `init` and the one `bun install` in `rebuild-web` | You trust this repository's author and the release you pinned |

## Platform notes

- **macOS and Linux.** Listener ownership comes from `lsof` (macOS) and from `/proc/net/tcp` and `/proc/net/tcp6` (Linux,
  with `ss -e` as a fallback). A listener whose owner the tool cannot show (an `lsof` run by an ordinary user prints nothing
  for another account's socket) is reported as unknown, never as yours: `doctor` says so, and the `uid-owner` check of the health drop-in (run by omp-services) fails on it. `omp-private-relay check` rests on the listener proof instead and fails only when the owner is seen to be another account.
- **Tailscale on Linux** has not been tested for what Serve puts in its headers. On macOS a real Serve (Tailscale 1.104.5) was tested: it replaced a caller-sent
  `Tailscale-User-Login`, removed a caller-sent Funnel header and replaced `X-Forwarded-For`; a non-owner tailnet identity was not available to test. Until Linux has been tested, treat the `tailscale-serve` adapter as macOS-tested.
- **Windows** is unsupported (omp's own transport there differs, and this plugin's file-permission checks assume Unix).

## What is stored, and where

See the table in the [README](../README.md#what-it-keeps-on-disk). Nothing is sent anywhere by the relay. The only network
connections the plugin makes are the download during `init` and `bun install` during `rebuild-web`, both on request.

## Advice for authors of dashboards that share the pairing store

- Write the store exactly as `pairing-store/1` says and run its conformance suite against your writer.
- Use a cookie name of your own and tell the relay (`pairing.cookieName`, `pairing.mode: shared`). A cookie is sent to every
  port of a host, so a cookie set by your dashboard on one port is also sent to the relay's: that is how one pairing can
  serve both, and also why a dashboard's pairing cookie MUST be `HttpOnly` and `SameSite=Strict`.
- Check the relay's identity before you send a browser to it, with `relay-check/1`.

## Accepted limitations, in one place

- Another account can take the relay's port while it is down. `pair`, `doctor` and the health drop-in detect it; nothing stops a
  browser from opening a session link that omp printed, so the proof is detection and a gate on `pair`, not protection for that
  navigation.
- The `token` adapter's cookie is sent to every port of `127.0.0.1`, and loopback `Host`/`Origin` headers can be forged by a local
  program; the Tailscale owner header likewise.
- Other local accounts can use the host listener (squatting, denial of service) and can keep the pairing failure budget spent.
- A process running as you can do all of it and more.

## Out of scope: attacker capabilities this design does not address

These are stated so that nobody expects more than the plugin gives. Each is a power an attacker would already hold over a
single-user machine, or a layer below what the plugin can see.

- **root**, and any process that runs as **your own account**. Either can read the cookie hashes, replace the web client, open the
  key file and attach to the relay. Nothing here defends against them.
- **Changes to the machine's view of the file system or of accounts**: a tampered group database or directory service, mounts,
  bind mounts or FUSE layers, a file system that lies about ownership or ACLs, network file systems (also why `flock` needs a local
  disk), and backups or disk images of your files.
- **Someone who can change your directories**, in the up to five seconds the approval of the web tree is cached, or between the
  check and the use of a file (a time-of-check to time-of-use race) when that someone is already root or you.
- **Another local account that holds none of the above** is in scope only for what the tables above list. Shared machines are
  unsupported: that account can squat on the relay's port, spend the pairing failure budget and keep many connections busy, and
  can serve a page when you navigate to a link while the relay is down (the proof detects and gates `pair`; it cannot gate a
  browser).
- **The browser and what runs in it**: a browser extension, a compromised profile, a page you opened on purpose. A paired cookie
  is a credential for as long as you leave it paired.
- **Everything the transport adds**: the strength of a tunnel you create (SSH forward, Tailscale Serve), its login, and the
  headers a proxy sets.
- **Physical access, firmware and the kernel.**

## Open items

- A further independent security review of this tree before the first public release.
- The `tailscale-serve` adapter's header behaviour on Linux, and with a real non-owner tailnet identity.
- A `local-tls` adapter (the relay on a non-loopback address behind its own certificate) is not implemented; the interface
  is built for it, and the listeners cannot be moved off loopback until it exists.
- omp does not distribute a self-hostable relay; if it ever does, much of this plugin's value goes with it.
