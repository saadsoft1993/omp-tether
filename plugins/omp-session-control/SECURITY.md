# Security policy: omp-session-control

## Reporting a vulnerability

Please report it privately, not in a public issue: use GitHub's **Report a vulnerability** button on the repository's
Security tab (private vulnerability reporting). Include the omp version, your operating system, the plugin version, and
the smallest steps that reproduce the problem.

This is a one-person project. I will read every report and answer as soon as I can; I cannot promise a fixed response time.
Please give me reasonable time to fix a problem before you publish it, and tell me if you intend to.

## What counts

In scope: anything that lets a process or user that is **not** you (another account, a web page, a repository you opened)
stop or rename your sessions, read information from them, or run code through this extension; anything that makes the
extension crash or hang an omp session; anything that makes it turn itself on, or listen anywhere other than the private
socket in your control directory; a path-handling flaw (symlinks, races, permissions) in how it creates or removes files.

Out of scope, by design (see [`docs/threat-model.md`](docs/threat-model.md)): a process that already runs as your user
or as root using the socket. It can already do much more than stop and rename sessions.

## Supported versions

The latest release only, while the version is below 1.0.

## What is not claimed

This is not a certification, and there has been no third-party audit. The threat model lists what it is meant to defend against and what it
accepts. Do not enable it on an account you share with people you do not trust.
