# Security policy: omp-dashboard

## Reporting a vulnerability

Please report it privately, not in a public issue: use GitHub's **Report a vulnerability** button on the repository's Security tab
(private vulnerability reporting). Include the omp version, your operating system, the plugin version, which sign-in adapter you use
(`loopback-only`, `tailscale-serve` or `both`), and the smallest steps that reproduce the problem. Please do not attach real cookies, pairing links,
room links, ntfy topics or the contents of your pairing store.

This is a one-person project. I will read every report and answer as soon as I can; I cannot promise a fixed response time. Please give me
reasonable time to fix a problem before you publish it, and tell me if you intend to.

## What counts

In scope: anything that lets someone who is **not** you reach your sessions, hand out a link, stop, rename, kill or start a session, or read
the page's data, without a paired browser (another device, another account on the machine, a web page you visit, a program on your tailnet);
a revoked browser that keeps working; a way to learn a cookie, a pairing token, a room link, the relay's check key or the ntfy topic; a way
to make the hub listen on anything but a loopback address; a signal sent to a process that is not the session that was named (identity
checks in Kill); path handling flaws (symbolic links, races, permissions) in the configuration, the pairing store, the secrets, the control
directory or the page's files; a page file that does not match its pin being served; anything that makes the hub crash or hang from the
network side; secrets or personal data in the hub's output or its audit log.

Out of scope, by design (see [`docs/threat-model.md`](docs/threat-model.md)): a process that already runs as your user or as root (it can read your
pairing store and your sessions anyway); other accounts on the same machine connecting to the hub's loopback port without a paired browser
(they get 401); flaws in omp, Tailscale, your browser, herdr, tmux, ntfy or the third-party packages bundled in the page (report those upstream, and
tell me if this plugin makes one worse); a machine that several people share, which is not a supported setup.

## Supported versions

The latest release only, while the version is below 1.0.

## What is not claimed

Nobody has audited this plugin. What exists instead: unit tests for every module (sign-in adapters including forged headers, pairing, the
control/1 client against hostile sockets, the relay proof against look-alikes, Kill's identity checks against real processes, ntfy and the audit
log, the Start backends against stand-in commands and a real tmux, the page's hash pinning, the drop-ins against omp-services' schemas and
health runner); the pairing-store/1 conformance suite and a cross-check with the relay's own reader and writer; an integration test of the hub
against two live omp sessions with the real session-control extension; and a mutation check that switches off a list of guards one at a time
in a copy and requires a test to fail (`make test-dashboard-mutation`; the list is in `tests/mutation/run_mutations.py` and is not every guard).
These show that the guards that exist work, not that no guard is missing. herdr 0.9.5 (a real server in a scratch home) and Tailscale 1.104.5 Serve on macOS (a real route, called with forged headers) were also exercised once, by hand, on a second machine; what they did not cover is
a real Serve with a second tailnet identity (a different account or a shared device), and herdr on Linux. Treat early releases as beta software, and do not expose the hub beyond a tunnel you control.
