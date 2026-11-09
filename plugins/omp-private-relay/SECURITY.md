# Security policy: omp-private-relay

## Reporting a vulnerability

Please report it privately, not in a public issue: use GitHub's **Report a vulnerability** button on the repository's
Security tab (private vulnerability reporting). Include the omp version, your operating system, the plugin version, which
sign-in adapter you use (`token` or `tailscale-serve`), and the smallest steps that reproduce the problem. Please do not
attach real cookies, pairing links, room links or the contents of your pairing store.

This is a one-person project. I will read every report and answer as soon as I can; I cannot promise a fixed response
time. Please give me reasonable time to fix a problem before you publish it, and tell me if you intend to.

## What counts

In scope: anything that lets someone who is **not** you reach your sessions or the web client without a paired browser
(another device, another account on the machine, a web page you visit, a program on your tailnet); anything that lets a
browser that was revoked keep a connection; a way to learn a cookie, a pairing token, a room link or the check key; a way to
make the relay listen on anything but `127.0.0.1`; a way to make `init`, `pair`, `rebuild-web` or the web client download
install a file that was not verified, or write outside its own directories; path handling flaws (symlinks, races,
permissions) in the pairing store, the check key, the config file or the installed web client; anything that makes the relay
crash or hang from the network side; secrets or personal data in the relay's output.

Out of scope, by design (see [`docs/threat-model.md`](docs/threat-model.md)): a process that already runs as your user or
as root (it can read your pairing store and your sessions anyway); other accounts on the same machine opening rooms on the
host listener (squatting and denial of service, bounded by `limits.maxRooms`, never content); flaws in omp, in Tailscale, in
your browser or in the third-party packages bundled in the web client (report those upstream, and tell me if this plugin
makes one worse); a machine that several people share, which is not a supported setup.

## Supported versions

The latest release only, while the version is below 1.0. The web client is built per omp minor: a release lists the minors
it has a build for in `web-builds.json`.

## What is not claimed

The relay generalises one whose first design had an independent security review; the findings were fixed and each has a
test. This plugin has had independent security reviews, and every finding was fixed or answered with evidence (the changelog lists
them; `make test-mutation` reverts each fix in a copy and requires its regression test to fail). The fixes themselves have not
been reviewed again, and no third-party audit has been done, so treat early releases as beta software. The patched web client is a build of
omp's own browser client: its code is omp's, with the three changes listed in the README. Do not run the relay on an
account you share with people you do not trust.
